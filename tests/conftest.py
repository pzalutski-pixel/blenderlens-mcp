"""Shared fixtures and helpers for the test suite.

Most tests that need Blender drive the real server over real stdio pipes, and the server drives a
real Blender, so nothing is mocked between the tool call and ``bpy``. Most integration tests share one
background Blender session through the ``blender`` fixture, because starting Blender takes seconds,
and each of them starts from an empty file.
"""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from blenderlens_mcp.session import find_blender_binary

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

#: Environment variables that point every server the suite starts at the suite's own temp folder and
#: runtime folder, so nothing lands in the machine's real folders. The ``suite_folders`` fixture fills
#: it for the whole run.
SUITE_ENV: dict[str, str] = {}


def checkout_env(extra: dict | None = None) -> dict:
    """Return the environment for a server subprocess, set up so that it imports this checkout's code.

    Without this, ``python -m blenderlens_mcp`` imports whatever copy pip installed, and the suite
    passes or fails on code that is not the code under test. Values in ``extra`` take precedence over
    the suite's folders.
    """
    merged = {**os.environ, **SUITE_ENV, **(extra or {})}
    merged["PYTHONPATH"] = os.pathsep.join(p for p in (str(SRC), merged.get("PYTHONPATH", "")) if p)
    return merged


@pytest.fixture(scope="session", autouse=True)
def suite_folders(tmp_path_factory):
    """Point every server at a temp folder and a runtime folder of the suite's own.

    When the run ends, fail if any ``blenderlens-*`` session working folder is left in the temp
    folder, because every way a background session ends must remove its working folder. The temp
    folder belongs to the suite's servers, not to the system, so the check sees only what this run
    made, never a session that another server on the machine has open.
    """
    temp = tmp_path_factory.mktemp("server-temp")
    runtime = tmp_path_factory.mktemp("runtime")
    SUITE_ENV.update({"TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp),
                      "BLENDERLENS_RUNTIME_DIR": str(runtime)})
    yield {"temp": temp, "runtime": runtime}
    SUITE_ENV.clear()
    left = sorted(path.name for path in temp.glob("blenderlens-*"))
    assert not left, f"the suite left session working folders behind in {temp}: {left}"


def png_info(path) -> dict:
    """Return the width, height, bit depth and colour type from a PNG's IHDR chunk, and the file size."""
    import struct

    data = Path(path).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    width, height, depth, color_type = struct.unpack(">IIBB", data[16:26])
    return {"width": width, "height": height, "depth": depth, "color_type": color_type, "bytes": len(data)}


def free_port() -> int:
    """Return a TCP port on 127.0.0.1 that nothing listens on at the moment of the call."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def isolated_blender_env(user: Path) -> dict:
    """Return an environment in which Blender keeps its user configuration in ``user``.

    The folder and its ``config`` subfolder are created first. When ``BLENDER_USER_RESOURCES`` names a
    folder that does not exist yet, Blender reads the machine's own user preferences instead, so a test
    would see, and copy into its temporary folder, the preferences of the person running it.
    """
    (user / "config").mkdir(parents=True, exist_ok=True)
    return dict(os.environ, BLENDER_USER_RESOURCES=str(user))


def strict_json(line: bytes):
    """Parse a line the server wrote as strict JSON, and raise ValueError if it holds NaN or Infinity.

    Python's ``json`` module accepts ``NaN`` and ``Infinity`` by default, but they are not valid JSON,
    and a client with a strict parser would reject the whole message.
    """
    def refuse(token):
        """Raise ValueError for ``token``, a constant that strict JSON does not have."""
        raise ValueError(f"the server wrote {token}, which is not JSON: {line[:200]!r}")

    return json.loads(line.decode("utf-8"), parse_constant=refuse)


class MCPProcess:
    """A real server subprocess that a test drives over stdio, one message at a time.

    A thread reads the server's stdout into a queue, so a test can wait for a line with a deadline and
    can check that nothing came. Every line is parsed as strict JSON. ``read`` returns the next
    response (a message with an id, or the array returned for a batch) and keeps the notifications it
    passes on the way in ``notifications``. ``seen`` holds every message in the order it arrived.
    """

    def __init__(self, cwd: str, env: dict | None = None):
        """Start the server in ``cwd``, with ``env`` on top of the environment ``checkout_env`` builds."""
        merged = checkout_env(env)
        self.stderr_path = Path(cwd) / f"server-{id(self)}.stderr"
        self._stderr = open(self.stderr_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "blenderlens_mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            cwd=cwd, env=merged,
        )
        self._id = 0
        self._lines: queue.Queue = queue.Queue()
        self.notifications: list[dict] = []
        self.seen: list = []
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        """Put each line the server writes into the queue, then None at end of file."""
        try:
            for line in iter(self.proc.stdout.readline, b""):
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put(None)

    def send_bytes(self, data: bytes) -> None:
        """Write ``data`` and a newline to the server's stdin, as one line of the stdio transport."""
        self.proc.stdin.write(data + b"\n")
        self.proc.stdin.flush()

    def send_raw(self, line: str) -> None:
        """Write ``line``, encoded as UTF-8, to the server's stdin as one line."""
        self.send_bytes(line.encode("utf-8"))

    def next_message(self, timeout: float | None = None):
        """Return the next message the server wrote, parsed, or None at end of file.

        Blank lines are skipped. Raise TimeoutError when no line arrives within ``timeout`` seconds.
        """
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"the server wrote nothing within {timeout}s") from None
        if line is None:
            self._lines.put(None)  # Put the end-of-file marker back, so the next read also sees it.
            return None
        if not line.strip():
            return self.next_message(timeout)
        message = strict_json(line)
        self.seen.append(message)
        return message

    def read(self, timeout: float | None = None):
        """Return the next response, and keep the notifications that arrive before it.

        Return None at end of file, and raise TimeoutError when no response arrives within ``timeout``
        seconds.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            message = self.next_message(left)
            if isinstance(message, dict) and "id" not in message:
                self.notifications.append(message)
                continue
            return message

    def quiet(self, seconds: float) -> list:
        """Return every message the server writes within ``seconds``.

        A test that expects silence checks that the list is empty.
        """
        got = []
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                message = self.next_message(left)
            except TimeoutError:
                break
            if message is None:
                break
            got.append(message)
        return got

    def send(self, method: str, params: dict | None = None) -> int:
        """Send a request without reading its response, and return the request's id."""
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        self.send_raw(json.dumps(payload))
        return self._id

    def notify(self, method: str, params: dict | None = None) -> None:
        """Send a JSON-RPC notification, which has no id and gets no response."""
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.send_raw(json.dumps(payload))

    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        """Send a request and return its response, waiting at most ``timeout`` seconds for it."""
        self.send(method, params)
        return self.read(timeout)

    def initialize(self, version: str = "2025-11-25") -> dict:
        """Start a session at the protocol revision ``version``: send ``initialize``, then ``initialized``.

        Return the response to ``initialize``.
        """
        resp = self.request("initialize", {
            "protocolVersion": version, "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"}})
        self.send_raw('{"jsonrpc":"2.0","method":"notifications/initialized"}')
        return resp

    def call_raw(self, name: str, arguments: dict) -> dict:
        """Call the tool ``name`` and return the ``tools/call`` result as the server sent it."""
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    # The tool name here, and the error kind in ``refused``, are positional-only, so that a tool argument
    # named ``tool`` or ``kind`` passes through to the tool instead of clashing with them.
    def call(self, tool: str, /, **arguments) -> tuple[dict, bool]:
        """Call a tool, and return its result's text parsed as JSON together with its ``isError`` flag."""
        result = self.call_raw(tool, arguments)
        return json.loads(result["content"][0]["text"]), bool(result.get("isError"))

    def ok(self, tool: str, /, **arguments) -> dict:
        """Call a tool that must succeed, and return its parsed result.

        If the tool fails, the test fails and shows the error payload the server returned.
        """
        payload, is_error = self.call(tool, **arguments)
        assert not is_error, f"{tool} failed: {json.dumps(payload, indent=1)}"
        return payload

    def refused(self, tool: str, kind: str, /, **arguments) -> dict:
        """Call a tool that must fail with the error kind ``kind``, and return the error payload.

        The payload must also carry the ``error``, ``exit`` and ``hint`` fields.
        """
        payload, is_error = self.call(tool, **arguments)
        assert is_error, f"{tool} was not refused with {kind}; it returned {json.dumps(payload)[:800]}"
        assert payload["kind"] == kind, json.dumps(payload, indent=1)
        assert {"error", "exit", "kind", "hint"} <= set(payload)
        return payload

    def close(self) -> str:
        """Close the server's stdin, wait for it to exit (killing it if it does not), and return its stderr."""
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=60)
        except Exception:  # noqa: BLE001 -- teardown must not mask the test's own failure
            self.proc.kill()
            self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self._stderr.close()
        return self.stderr_path.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="session", autouse=True)
def runtime_dir(suite_folders):
    """Provide the suite's runtime folder from ``suite_folders``, never the machine's real one.

    The folder is also set in this process's environment, so that every Blender a test starts itself
    sees the same folder as the servers do. The add-on writes its connection file there, and the server
    reads it from there.
    """
    path = suite_folders["runtime"]
    previous = os.environ.get("BLENDERLENS_RUNTIME_DIR")
    os.environ["BLENDERLENS_RUNTIME_DIR"] = str(path)
    yield path
    if previous is None:
        os.environ.pop("BLENDERLENS_RUNTIME_DIR", None)
    else:
        os.environ["BLENDERLENS_RUNTIME_DIR"] = previous


@pytest.fixture
def mcp_process(tmp_path):
    """Provide a factory that starts server subprocesses, and close each of them when the test ends."""
    spawned: list[MCPProcess] = []

    def _make(cwd=None, env: dict | None = None) -> MCPProcess:
        """Start a server in ``cwd``, the test's temp folder by default, with ``env``, and return it."""
        proc = MCPProcess(cwd=str(cwd or tmp_path), env=env)
        spawned.append(proc)
        return proc

    yield _make
    for proc in spawned:
        proc.close()


@pytest.fixture
def no_blender_env(tmp_path):
    """Return an environment whose port has no add-on listening, so the server never attaches to live mode.

    A server in auto mode may still start a background Blender, if it finds one, for a call that needs Blender.
    """
    return {"BLENDERLENS_PORT": str(free_port()), "BLENDERLENS_RENDER_DIR": str(tmp_path / "renders")}


# ---------------------------------------------------------------------------
# Real Blender
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def blender_binary() -> str:
    """Return the Blender executable the server would find, or skip the test when there is none."""
    binary = find_blender_binary()
    if not binary:
        pytest.skip("No Blender executable was found. Set BLENDER_BIN to run this test.")
    return binary


@pytest.fixture(scope="session")
def server_cwd(tmp_path_factory) -> Path:
    """Return the folder the shared server runs in, against which a relative path given to a tool resolves."""
    return tmp_path_factory.mktemp("blender")


@pytest.fixture(scope="session")
def blender_server(blender_binary, server_cwd):
    """Start one server in background mode, shared by the whole integration suite.

    When the session ends, fail if the server's stderr holds a Python traceback.
    """
    env = {
        "BLENDER_BIN": blender_binary,
        "BLENDERLENS_MODE": "background",
        # A port nothing listens on, so the suite never attaches to a Blender window running the add-on
        # that someone has open on this machine.
        "BLENDERLENS_PORT": str(free_port()),
        "BLENDERLENS_RENDER_DIR": str(server_cwd / "renders"),
    }
    proc = MCPProcess(cwd=str(server_cwd), env=env)
    proc.initialize()
    yield proc
    stderr = proc.close()
    assert "Traceback" not in stderr, stderr


@pytest.fixture
def blender(blender_server):
    """Provide the shared server, with a new, empty Blender file to start from."""
    blender_server.ok("create_file", discard=True)
    return blender_server
