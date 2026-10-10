"""Tests for sessions with a real Blender.

They cover the status report, run_python, deadlines, closing and restarting a session, the lifetime of the
background Blender, and the add-on bridge's own protocol.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from blenderlens_mcp import __version__
from blenderlens_mcp.session import BACKGROUND_SCRIPT, PROTOCOL
from tests.conftest import free_port

pytestmark = pytest.mark.integration


def process_alive(pid: int) -> bool:
    """Return whether the process ``pid`` is still running."""
    if sys.platform == "win32":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_gone(pid: int, seconds: float = 20.0) -> bool:
    """Wait up to ``seconds`` for the process ``pid`` to end, and return whether it did."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.2)
    return False


def background_env(blender_binary, tmp_path):
    """Return the environment for a server that starts its own background Blender on a free port."""
    return {"BLENDER_BIN": blender_binary, "BLENDERLENS_MODE": "background",
            "BLENDERLENS_PORT": str(free_port()), "BLENDERLENS_RENDER_DIR": str(tmp_path / "renders")}


# ---------------------------------------------------------------------------
# Through the shared server fixture
# ---------------------------------------------------------------------------


def test_status_describes_the_background_session(blender):
    status = blender.ok("get_status")
    session = status["session"]
    assert session["mode"] == "background"
    assert session["addon_version"] == __version__
    assert session["blender_version"].split(".")[0].isdigit()
    assert "cycles" in session["render_engines"]
    assert Path(session["log"]).is_file()
    assert status["next_call_attaches"] is None
    assert status["blender"]["found"] is True


def test_run_python_returns_result_stdout_and_keeps_names(blender):
    out = blender.ok("run_python", code="def area(r):\n    return math.pi * r * r\nprint('defined')\nresult = 1")
    assert out["stdout"] == "defined\n"
    assert out["result"] == 1
    again = blender.ok("run_python", code="result = round(area(2), 3)")
    assert again["result"] == pytest.approx(12.566)


def test_run_python_errors_carry_the_traceback(blender):
    payload = blender.refused("run_python", "python_error", code="x = 1\ny = undefined_name\n")
    assert "NameError" in payload["error"]
    assert 'line 2' in payload["traceback"]
    assert payload["exit"] == 4


def test_run_python_converts_blender_values(blender):
    blender.ok("create_object", kind="cube", name="V", location=[1, 2, 3])
    out = blender.ok("run_python",
                     code="result = {'loc': bpy.data.objects['V'].location, 'obj': bpy.data.objects['V']}")
    assert out["result"] == {"loc": [1.0, 2.0, 3.0], "obj": "V"}


def test_a_deadline_is_reported_and_the_late_result_is_not_mistaken_for_the_next(blender):
    payload = blender.refused("run_python", "deadline", code="import time\ntime.sleep(3)\nresult = 'late'",
                              timeout=1)
    assert payload["exit"] == 124
    # The next call waits behind the slow one and must get its own result, not 'late'.
    out = blender.ok("run_python", code="result = 'mine'")
    assert out["result"] == "mine"


# ---------------------------------------------------------------------------
# Session lifecycle, each with its own server
# ---------------------------------------------------------------------------


def test_close_guards_unsaved_work_and_the_next_call_starts_afresh(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    proc.ok("create_object", kind="cube", name="Work")
    first_pid = proc.ok("get_status")["session"]["pid"]

    payload = proc.refused("close_session", "unsaved_changes")
    assert "save_file with a path" in payload["hint"], \
        "the hint does not ask for a path, though the file was never saved"
    assert proc.ok("close_session", discard=True)["closed"] == "background"
    assert wait_gone(first_pid), "the background Blender did not exit"

    scene = proc.ok("get_scene")
    assert scene["objects"] == []
    assert proc.ok("get_status")["session"]["pid"] != first_pid


def test_close_after_save_needs_no_discard(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    proc.ok("create_object", kind="cube")
    proc.ok("save_file", path=str(tmp_path / "kept.blend"))
    assert proc.ok("close_session")["closed"] == "background"


def test_open_session_with_a_file_opens_it(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    proc.ok("create_object", kind="cube", name="FromDisk")
    blend = str(tmp_path / "start.blend")
    proc.ok("save_file", path=blend)
    proc.ok("close_session")

    opened = proc.ok("open_session", mode="background", file=blend)
    assert opened["opened"] == "background" and opened["already_attached"] is False
    assert opened["session"]["file"] == blend
    assert [o["name"] for o in proc.ok("get_scene")["objects"]] == ["FromDisk"]
    again = proc.ok("open_session", mode="background")
    assert again["already_attached"] is True


def test_the_background_blender_exits_when_the_server_dies(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    proc.ok("get_scene")
    session = proc.ok("get_status")["session"]
    pid = session["pid"]
    assert process_alive(pid)
    proc.proc.kill()  # No shutdown command and no orderly exit: the server process simply disappears.
    proc.proc.wait(timeout=10)
    assert wait_gone(pid), "Blender did not stop when its stdin closed"
    # No server was left to remove the session's working folder, so the next server to start removes it.
    folder = Path(session["log"]).parent
    assert folder.is_dir()
    mcp_process(env=background_env(blender_binary, tmp_path)).initialize()
    assert not folder.exists(), "the next server did not remove the orphaned working folder"


def test_a_crashed_blender_is_connection_lost_then_a_fresh_start(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    proc.ok("create_object", kind="cube", name="Lost")
    pid = proc.ok("get_status")["session"]["pid"]
    subprocess.run(["taskkill", "/F", "/PID", str(pid)] if sys.platform == "win32" else ["kill", "-9", str(pid)],
                   capture_output=True, check=False)
    assert wait_gone(pid)
    payload = proc.refused("get_scene", "connection_lost")
    assert "gone" in payload["hint"]
    assert proc.ok("get_scene")["objects"] == [], "the next call did not start a new Blender"


# ---------------------------------------------------------------------------
# The add-on's bridge, spoken to directly over its socket
# ---------------------------------------------------------------------------


class Raw:
    """A connection to the add-on's bridge that sends request lines as they are given, with no server between."""

    def __init__(self, port):
        """Connect to the bridge listening on ``port`` on the loopback address."""
        self.port = port
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.buffer = b""

    def send(self, payload):
        """Send one request line and return the response, or None when the bridge closes the connection instead."""
        line = payload if isinstance(payload, str) else json.dumps(payload)
        self.sock.sendall(line.encode("utf-8") + b"\n")
        while b"\n" not in self.buffer:
            try:
                chunk = self.sock.recv(65536)
            except (ConnectionResetError, ConnectionAbortedError):
                return None
            if not chunk:
                return None
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def hello(self):
        """Send the opening hello with the token, assert that it succeeds, and return this connection."""
        response = self.send({"id": 0, "command": "hello", "token": "secret-token"})
        assert response and response["ok"], response
        return self


@pytest.fixture
def raw_bridge(blender_binary, tmp_path):
    """Start a background Blender running the bridge with no server, and yield a Raw connection and the process."""
    ready = tmp_path / "ready.json"
    env = dict(os.environ, BLENDERLENS_SESSION_TOKEN="secret-token")
    log = open(tmp_path / "blender.log", "wb")
    blender = subprocess.Popen([blender_binary, "--background", "--factory-startup", "--python", str(BACKGROUND_SCRIPT),
                                "--", "--port", "0", "--ready-file", str(ready), "--empty"],
                               stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, env=env)
    deadline = time.time() + 90
    while not ready.exists() and time.time() < deadline and blender.poll() is None:
        time.sleep(0.1)
    assert ready.exists(), (tmp_path / "blender.log").read_text(errors="replace")[-2000:]
    port = json.loads(ready.read_text())["port"]
    try:
        yield Raw(port), blender
    finally:
        if blender.poll() is None:
            blender.kill()
        blender.wait(timeout=10)
        log.close()


def test_the_bridge_checks_the_token(raw_bridge):
    raw, _ = raw_bridge
    # Without the token, the bridge closes the connection without a response.
    assert raw.send({"id": 1, "command": "hello"}) is None
    hello = Raw(raw.port).send({"id": 2, "command": "hello", "token": "secret-token"})
    assert hello["ok"] and hello["result"]["mode"] == "background" and hello["result"]["protocol"] == PROTOCOL


def test_the_bridge_closes_on_garbage_refuses_unknown_commands_and_stays_up(raw_bridge):
    raw, _ = raw_bridge
    raw.hello()
    assert raw.send("this is not json") is None, \
        "the bridge replied to a line that is not a JSON object instead of closing the connection"
    raw = Raw(raw.port).hello()
    unknown = raw.send({"id": 3, "command": "frobnicate", "token": "secret-token"})
    assert unknown["kind"] == "bad_argument" and "install-addon" in unknown["hint"]
    bad_params = raw.send({"id": 4, "command": "get_object", "params": {"object": 5}, "token": "secret-token"})
    assert bad_params["kind"] == "bad_argument"
    assert raw.send({"id": 5, "command": "get_scene", "token": "secret-token"})["ok"] is True


def test_the_bridge_reports_blenders_own_failure_with_a_traceback(raw_bridge, tmp_path):
    raw, _ = raw_bridge
    raw.hello()
    broken = tmp_path / "broken.glb"
    broken.write_bytes(b"glTF" + b"\x00" * 40)
    response = raw.send({"id": 6, "command": "import_file", "params": {"path": str(broken)},
                         "token": "secret-token"})
    assert response["ok"] is False
    assert response["kind"] == "blender_error"
    assert "Traceback" in response["traceback"]
    assert "Unreadable request" not in response["error"], "a failure inside a command was reported as a bad request"
    assert raw.send({"id": 7, "command": "get_scene", "token": "secret-token"})["ok"] is True


def test_shutdown_stops_the_background_blender(raw_bridge):
    raw, blender = raw_bridge
    raw.hello()
    assert raw.send({"id": 8, "command": "shutdown", "token": "secret-token"})["result"] == {"stopping": True}
    assert blender.wait(timeout=30) == 0
