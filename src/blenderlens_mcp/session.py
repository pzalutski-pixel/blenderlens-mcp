"""The two ways to reach Blender, live and background, behind one interface.

* **live** — a Blender window running the BlenderLens add-on, with its bridge started. The
  server connects to the add-on's socket. The user watches the work happen and can step in,
  and every change is one undo step in that window.
* **background** — no window. The server starts ``blender --background`` running
  ``background.py``, which serves the same commands from the same add-on code over the
  same socket protocol. Its unsaved work ends when the session does, except that a copy
  is kept in the recovery folder when the server's client goes away.

Either way, a call is one JSON line out and one JSON line back. ``SessionManager``
decides which way to use (``BLENDERLENS_MODE``: auto, live or background), attaches on
the first call that needs Blender, and reports failures as ``ToolFailure``.

A Blender still working on a call that ran past its deadline is busy, not gone: the
session stays attached, and nothing more is sent to Blender until it returns that call's
result. A session ends only when its socket closes, its Blender exits, or it is closed on
purpose: by close_session, by open_session in the other mode, by open_session with discard
while its Blender is busy, or at server exit. When a background session ends, its working
folder is removed.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from blenderlens_mcp import __version__
from blenderlens_mcp.errors import ToolFailure

#: The version of the socket protocol between this server and the add-on. It must equal PROTOCOL in
#: addon/commands.py, and a test checks that the two match.
PROTOCOL = 2

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
BACKGROUND_SCRIPT = Path(__file__).with_name("background.py")
MODES = ("auto", "live", "background")

#: The oldest Blender a background session starts on. CI tests 4.2 LTS and 5.0.
MIN_BLENDER = (4, 2)
#: A background session keeps its files (the ready file, the owner file and Blender's log) in a temporary
#: working folder, whose name starts with this prefix.
FOLDER_PREFIX = "blenderlens-"
#: A file written into a working folder once its Blender runs, naming the server and Blender processes the
#: folder belongs to, so a later server can tell an abandoned folder from one in use.
OWNER_FILE = "owner.json"
#: A working folder with no record of its processes counts as abandoned after this many seconds.
ORPHAN_AGE = 24 * 3600
#: Days a preview is kept in the default render folder.
RENDER_KEEP_DAYS = 7
#: Days a copy is kept in the recovery folder.
RECOVERY_KEEP_DAYS = 14
#: get_status lists this many recovery copies, newest first.
RECOVERY_LISTED = 5
#: At server exit, the seconds a call that Blender is still running gets to finish before the recovery
#: copy is given up.
RECOVERY_WAIT = 10.0
#: At server exit, the seconds that saving the recovery copy may take.
RECOVERY_SAVE_TIMEOUT = 60.0


def log(message: str) -> None:
    """Write a message to stderr, because stdout carries the MCP stream."""
    print(f"[blenderlens] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Finding Blender
# ---------------------------------------------------------------------------


def _version_key(path: str) -> tuple:
    """Return the (major, minor) version in an install path such as ``Blender 4.2``, or (0, 0) without one."""
    found = re.search(r"Blender[ _-]?(\d+)\.(\d+)", path, re.IGNORECASE)
    return (int(found.group(1)), int(found.group(2))) if found else (0, 0)


def find_blender_binary() -> str | None:
    """Return the Blender executable to run, or None when none is found.

    BLENDER_BIN wins when it is set, and gives None when it is not a file. Otherwise ``blender`` on PATH
    is used, and then the usual install locations are searched, newest version first.
    """
    explicit = os.environ.get("BLENDER_BIN")
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    on_path = shutil.which("blender")
    if on_path:
        return on_path
    candidates: list[str] = []
    if sys.platform == "win32":
        for root in {os.environ.get("ProgramFiles", r"C:\Program Files"), r"C:\Program Files"}:
            candidates += glob.glob(os.path.join(root, "Blender Foundation", "Blender*", "blender.exe"))
        candidates.sort(key=_version_key, reverse=True)
    elif sys.platform == "darwin":
        candidates = ["/Applications/Blender.app/Contents/MacOS/Blender"]
    else:
        candidates = ["/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"]
    return next((c for c in candidates if os.path.isfile(c)), None)


_VERSIONS: dict[str, str | None] = {}


def blender_version(binary: str) -> str | None:
    """Return the version ``blender --version`` prints, such as '5.0.1', or None; cached per executable."""
    if binary not in _VERSIONS:
        try:
            run = subprocess.run([binary, "--version"], capture_output=True, timeout=60,
                                 creationflags=_NO_WINDOW)
            found = re.search(r"Blender (\d+\.\d+(?:\.\d+)?)", run.stdout.decode("utf-8", "replace"))
            _VERSIONS[binary] = found.group(1) if found else None
        except (OSError, subprocess.TimeoutExpired):
            _VERSIONS[binary] = None
    return _VERSIONS[binary]


def check_floor(binary: str) -> None:
    """Refuse a Blender older than MIN_BLENDER. A Blender whose version cannot be read is let through to try."""
    version = blender_version(binary)
    found = re.match(r"(\d+)\.(\d+)", version or "")
    if found and (int(found.group(1)), int(found.group(2))) < MIN_BLENDER:
        floor = ".".join(str(part) for part in MIN_BLENDER)
        raise ToolFailure("start_failed", f"Blender {version} at {binary} is older than {floor}, the oldest Blender "
                                          "BlenderLens runs on.",
                          f"Install Blender {floor} or newer and point BLENDER_BIN at it; this one is {version}.")


def missing_blender() -> ToolFailure:
    """Return the failure for a Blender executable that cannot be found, naming BLENDER_BIN when it is set."""
    explicit = os.environ.get("BLENDER_BIN")
    if explicit:
        return ToolFailure("blender_missing", f"BLENDER_BIN points at {explicit}, which is not a file.")
    return ToolFailure("blender_missing", "No Blender executable was found on PATH or in the usual install "
                                          "locations.")


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# BlenderLens's own folders, and tidying them
# ---------------------------------------------------------------------------


def _runtime_dir() -> str:
    """Return the folder where BlenderLens keeps its own files, such as recovery copies, as the add-on defines it."""
    return runtime.runtime_dir()


def recovery_dir() -> str:
    """Return the folder that recovery copies of unsaved background work go to."""
    return os.path.join(_runtime_dir(), "recovery")


def default_render_dir() -> str:
    """Return the folder render_preview writes to when BLENDERLENS_RENDER_DIR is unset and no path is given."""
    return os.path.join(tempfile.gettempdir(), "blenderlens", "renders")


def recovery_files(limit: int = RECOVERY_LISTED) -> list[dict]:
    """Return the newest recovery copies, newest first, for get_status."""
    found = []
    for path in glob.glob(os.path.join(glob.escape(recovery_dir()), "*.blend")):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        found.append((stat.st_mtime, path, stat.st_size))
    found.sort(reverse=True)
    return [{"path": path, "saved": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)), "bytes": size}
            for mtime, path, size in found[:limit]]


def _prune(folder: str, pattern: str, days: float) -> int:
    """Remove the files in ``folder`` matching ``pattern`` that are older than ``days``, and return how many.

    Subfolders are not searched.
    """
    cutoff = time.time() - days * 24 * 3600
    removed = 0
    for path in glob.glob(os.path.join(glob.escape(folder), pattern)):
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def _remove_tree(path: str, attempts: int = 10) -> bool:
    """Remove a folder and return whether it is gone.

    Windows can keep a file locked for a moment after the process that had it open exits, so a failed
    removal is retried briefly before it is logged.
    """
    for attempt in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return True
        if attempt + 1 < attempts:
            time.sleep(0.1)
    log(f"could not remove {path}")
    return False


def _read_json(path: str) -> dict | None:
    """Return the JSON object in a file, or None when the file is missing, unreadable or not a JSON object."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _pid_alive(pid) -> bool:
    """Return whether a process with this id is running. When unsure, return True, so nothing in use is removed."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: the process exists but is another user's.
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # PermissionError: the process exists but is another user's.
    return True


def _folder_in_use(path: str) -> bool:
    """Return whether a background session's working folder may still be in use.

    It is in use while a process its owner file names is running, or, without an owner file, while the
    process its ready file names is running. With neither file, it counts as in use until ORPHAN_AGE
    seconds after it last changed.
    """
    owner = _read_json(os.path.join(path, OWNER_FILE))
    if owner is not None:
        return _pid_alive(owner.get("server_pid")) or _pid_alive(owner.get("blender_pid"))
    ready = _read_json(os.path.join(path, "ready.json"))  # Written by Blender itself, with its process id.
    if ready is not None:
        return _pid_alive(ready.get("pid"))
    try:
        return time.time() - os.path.getmtime(path) < ORPHAN_AGE
    except OSError:
        return True


def _sweep_working_folders() -> int:
    """Remove the working folders of sessions whose server and Blender have both exited, and return how many.

    A server that is killed outright cannot remove its session's working folder, and its Blender, which
    stops when the server's pipe closes, holds the log open until then. The next server to start
    removes it instead.
    """
    removed = 0
    for path in glob.glob(os.path.join(glob.escape(tempfile.gettempdir()), FOLDER_PREFIX + "*")):
        # Only a session's working folder has a Blender log; other folders may share the prefix.
        if not os.path.isfile(os.path.join(path, "blender.log")) or _folder_in_use(path):
            continue
        removed += _remove_tree(path, attempts=1)
    return removed


def housekeeping() -> None:
    """Remove what earlier runs left behind, when the server starts.

    That is previews older than RENDER_KEEP_DAYS in the default render folder (never in a folder set
    with BLENDERLENS_RENDER_DIR), recovery copies older than RECOVERY_KEEP_DAYS, and the working folders
    of abandoned background sessions. A failure is logged, and never keeps the server from starting.
    """
    try:
        previews = 0 if os.environ.get("BLENDERLENS_RENDER_DIR") else _prune(
            default_render_dir(), "*.png", RENDER_KEEP_DAYS)
        # Blender writes to "<name>.blend@" and renames it when done, so a copy cut short leaves a .blend@ file.
        copies = sum(_prune(recovery_dir(), pattern, RECOVERY_KEEP_DAYS) for pattern in ("*.blend", "*.blend@"))
        folders = _sweep_working_folders()
    except Exception as exc:  # noqa: BLE001 -- tidying up never keeps the server from starting
        log(f"tidying up at start failed: {exc!r}")
        return
    if previews or copies or folders:
        log(f"removed {previews} old previews, {copies} old recovery copies and {folders} abandoned session "
            "folders")


# ---------------------------------------------------------------------------
# The socket connection to Blender
# ---------------------------------------------------------------------------


class BlenderConnection:
    """A TCP connection to a BlenderLens bridge, carrying one JSON line out and one JSON line back per call.

    At most one request is in flight. A request whose caller stopped waiting stays ``outstanding``:
    Blender is still working on it, so the next call first waits for its result within its own
    timeout, and is refused ``busy`` without sending anything when that time runs out. This way
    commands never pile up inside Blender.
    """

    #: The seconds each poll of the socket waits for data before ``on_poll`` runs and the wait goes on.
    #: The polls are a quarter of a second, so a cancelled request stops waiting within that time.
    POLL_SECONDS = 0.25

    def __init__(self, host: str, port: int, token: str | None = None):
        self.host = host
        self.port = port
        self.token = token or None
        self.sock: socket.socket | None = None
        self._buffer = bytearray()
        self._next_id = 0
        #: The request Blender is still working on: id, command, and started and deadline in
        #: time.monotonic() seconds; None when Blender has returned a result for everything sent.
        self.outstanding: dict | None = None
        #: Called after each quarter-second poll that brought no result, to check for a cancellation and
        #: send progress. When it returns True, the wait stops as if its deadline had passed, and the
        #: request stays outstanding.
        self.on_poll: Callable[[], bool] | None = None

    def open(self, timeout: float = 2.0) -> None:
        """Connect to the bridge, with TCP_NODELAY set so each short JSON line is sent at once."""
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def close(self) -> None:
        """Close the socket, and forget the outstanding request and any data not yet read."""
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.outstanding = None
        self._buffer.clear()

    def busy(self) -> dict | None:
        """Return what Blender is still working on, as ``{command, seconds}``, or None."""
        if self.outstanding is None:
            return None
        return {"command": self.outstanding["command"],
                "seconds": round(time.monotonic() - self.outstanding["started"], 1)}

    def collect(self) -> None:
        """Take in the outstanding request's result if it has already arrived. It never sends and never waits."""
        if self.outstanding is not None and self.sock is not None:
            if self._await(self.outstanding["id"], time.monotonic(), self.outstanding["command"]) is not None:
                self.outstanding = None

    def wait_idle(self, until: float, command: str) -> None:
        """Wait until ``until`` (time.monotonic() seconds) for the outstanding request's result, and discard it.

        The result is discarded because its caller gave up on it. Raise ``busy`` if Blender is still working
        at that time; ``command`` is the command that was about to be sent, and the refusal says it was not.
        """
        if self.outstanding is None:
            return
        if self.sock is None:
            raise ToolFailure("connection_lost", "The connection to Blender is closed.")
        if self._await(self.outstanding["id"], until, command) is None:
            busy = self.busy()
            raise ToolFailure("busy", f"Blender is still running {busy['command']}, sent {busy['seconds']:g}s ago; "
                                      f"{command} was not sent.", busy=busy)
        self.outstanding = None

    def call(self, command: str, params: dict | None, timeout: float) -> dict:
        """Send one command and return Blender's reply, whose ``ok`` is true or false.

        ``timeout`` covers the whole call: first the wait for an outstanding request's result, then the wait
        for this command's. Raise ``deadline`` when no reply comes in time; the request then stays outstanding.
        """
        if self.sock is None:
            raise ToolFailure("connection_lost", "The connection to Blender is closed.")
        until = time.monotonic() + timeout
        self.wait_idle(until, command)
        self._next_id += 1
        request_id = self._next_id
        request = {"id": request_id, "command": command, "params": params or {}}
        if self.token:
            request["token"] = self.token
        try:
            self.sock.settimeout(30.0)
            self.sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        except OSError as exc:
            self.close()
            raise ToolFailure("connection_lost", f"Could not send to Blender: {exc}") from None
        self.outstanding = {"id": request_id, "command": command, "started": time.monotonic(), "deadline": until}
        reply = self._await(request_id, until, command)
        if reply is None:
            # Blender is still working on it, so the request stays outstanding and the session stays attached.
            raise ToolFailure("deadline", f"Blender did not respond to {command} within {timeout:g}s.")
        self.outstanding = None
        return reply

    def _await(self, request_id: int, until: float, during: str) -> dict | None:
        """Return the reply to ``request_id``, or None once ``until`` passes.

        Replies to other requests, and lines that are not JSON, are skipped.
        """
        while True:
            line = self._readline(until, during)
            if line is None:
                return None
            try:
                reply = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(reply, dict) and reply.get("id") == request_id:
                return reply

    def _readline(self, until: float, during: str) -> bytes | None:
        """Return the next line from the socket, or None once ``until`` passes.

        After that time, it returns only a line that has already arrived.
        """
        while True:
            cut = self._buffer.find(b"\n")
            if cut >= 0:
                line = bytes(self._buffer[:cut])
                del self._buffer[:cut + 1]
                return line
            remaining = until - time.monotonic()
            try:
                # A zero timeout makes the socket non-blocking, so recv takes only what has already arrived.
                self.sock.settimeout(min(remaining, self.POLL_SECONDS) if remaining > 0 else 0.0)
                chunk = self.sock.recv(1 << 20)
            except (TimeoutError, BlockingIOError):
                if remaining <= 0:
                    return None
                if self.on_poll is not None and self.on_poll():
                    return None  # The request was cancelled: stop as if the deadline had passed.
                continue
            except OSError as exc:
                self.close()
                raise ToolFailure("connection_lost", f"The connection to Blender broke during {during}: {exc}") \
                    from None
            if not chunk:
                self.close()
                raise ToolFailure("connection_lost", f"Blender closed the connection during {during}.")
            self._buffer.extend(chunk)


def refusal_from(reply: dict) -> ToolFailure:
    """Turn Blender's ``ok: false`` reply into a ToolFailure, keeping its traceback and stdout."""
    extra = {key: reply[key] for key in ("traceback", "stdout") if reply.get(key)}
    return ToolFailure(reply.get("kind") or "blender_error", reply.get("error") or "Blender refused the call.",
                       reply.get("hint"), **extra)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class Session:
    """A session with one Blender: its connection, and the details from Blender's last ``hello`` reply."""

    mode = "none"

    def __init__(self, connection: BlenderConnection, hello: dict):
        self.connection = connection
        self.hello = hello

    def describe(self) -> dict:
        """Return the session as get_status shows it."""
        return {
            "mode": self.mode,
            "blender_version": self.hello.get("blender_version"),
            "addon_version": self.hello.get("addon_version"),
            "file": self.hello.get("file"),
            "unsaved_changes": self.hello.get("unsaved_changes"),
            "render_engines": self.hello.get("render_engines"),
            "pid": self.hello.get("pid"),
            "busy": self.connection.busy(),
        }

    def refresh(self, timeout: float = 10.0) -> dict:
        """Ask Blender for its current details with ``hello``, and return them.

        When Blender refuses the ``hello``, the previous details are kept and returned.
        """
        reply = self.connection.call("hello", {}, timeout)
        if reply.get("ok"):
            self.hello = reply["result"]
        return self.hello

    def stop(self, force: bool = False) -> None:
        """Disconnect from the session.

        In live mode this only closes the socket; the Blender window and its work stay as they are, and
        ``force`` makes no difference.
        """
        self.connection.close()


class LiveSession(Session):
    """A session with a Blender window running the add-on (live mode)."""

    mode = "live"


class BackgroundSession(Session):
    """A session with a ``blender --background`` process this server started, which ends with the session."""

    mode = "background"

    def __init__(self, connection, hello, process: subprocess.Popen, folder: str, log_path: str):
        super().__init__(connection, hello)
        self.process = process
        #: The session's temporary working folder, which holds the ready file, the owner file and the log.
        self.folder = folder
        self.log_path = log_path

    def describe(self) -> dict:
        """Return the session as get_status shows it, with the path of Blender's log."""
        out = super().describe()
        out["log"] = self.log_path
        return out

    def alive(self) -> bool:
        """Return whether the Blender process is still running."""
        return self.process.poll() is None

    def stop(self, force: bool = False) -> None:
        """End this Blender and remove its working folder.

        With ``force``, the process is killed at once, for a Blender that is busy or cannot be reached.
        Otherwise Blender is asked to shut down, and is killed only if it has not exited in time.
        """
        if not force and self.connection.sock is not None and self.connection.outstanding is None:
            try:
                self.connection.call("shutdown", {}, 10.0)
            except ToolFailure:
                pass
        self.connection.close()
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        if force and self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log(f"the background Blender (pid {self.process.pid}) did not exit")
        _remove_tree(self.folder)


def log_tail(path: str, chars: int = 2000) -> str:
    """Return the last ``chars`` characters of a log file, or an empty string when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()[-chars:].strip()
    except OSError:
        return ""


def _check_protocol(hello: dict, where: str) -> None:
    """Raise ``version_mismatch`` unless the add-on speaks this server's protocol; ``where`` names the Blender."""
    theirs = hello.get("protocol")
    if theirs != PROTOCOL:
        raise ToolFailure("version_mismatch",
                          f"The BlenderLens add-on {where} speaks protocol {theirs} (add-on "
                          f"{hello.get('addon_version')}); this server speaks {PROTOCOL} (server {__version__}).")


def _load_runtime():
    """Load addon/runtime.py from its file, because importing the add-on package would import bpy.

    bpy exists only inside Blender. The add-on writes the connection file with this code, and the
    server reads it with the same code, so the two always agree on where the file is and what it holds.
    """
    path = Path(__file__).with_name("addon") / "runtime.py"
    spec = importlib.util.spec_from_file_location("blenderlens_mcp._runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The add-on's runtime module, which locates the runtime folder and the live bridge's connection file
#: (``runtime.runtime_dir()``, ``runtime.connection_path()`` and ``runtime.read_connection()``).
runtime = _load_runtime()


def live_token(port: int, token: str | None) -> tuple[str | None, str]:
    """Return the token for the add-on on ``port``, and where it came from.

    BLENDERLENS_TOKEN (``token``) wins. Otherwise the token is read from the connection file the
    add-on wrote for that port. When there is no usable file, return None and a description of the
    problem.
    """
    if token:
        return token, "BLENDERLENS_TOKEN"
    path = runtime.connection_path(port)
    try:
        info = runtime.read_connection(port)
    except FileNotFoundError:
        return None, f"there is no connection file at {path}"
    except (OSError, ValueError) as exc:
        return None, f"the connection file {path} could not be read ({exc})"
    found = info.get("token")
    if not isinstance(found, str) or not found:
        return None, f"the connection file {path} holds no token"
    return found, path


def _token_refused(host: str, port: int, token: str | None, source: str) -> ToolFailure:
    """Return the failure for an add-on that closed the connection at the first ``hello``.

    The add-on closes the connection when it does not accept the token, or when there was none.
    """
    where = f"{host}:{port}"
    if source == "BLENDERLENS_TOKEN":
        return ToolFailure("token_mismatch", f"The BlenderLens add-on on {where} refused the token in "
                                             "BLENDERLENS_TOKEN.",
                           "BLENDERLENS_TOKEN must equal the token in the add-on's preferences; unset it, and the "
                           f"server reads the token from {runtime.connection_path(port)}.")
    if token is None:
        return ToolFailure("token_mismatch", f"Something listens on {where}, but this server has no token for it: "
                                             f"{source}.",
                           "Press Start in that Blender's BlenderLens panel, which writes "
                           f"{runtime.connection_path(port)}; or set BLENDERLENS_TOKEN to the token in the add-on's "
                           "preferences. Blender and this server must see the same BLENDERLENS_RUNTIME_DIR.")
    return ToolFailure("token_mismatch", f"The BlenderLens add-on on {where} refused the token in {source}.",
                       f"{source} is stale or from another Blender: press Stop and Start in the BlenderLens panel to "
                       "write it again, or set BLENDERLENS_TOKEN to the token in the add-on's preferences. If "
                       f"something other than the add-on listens on {port}, change BLENDERLENS_PORT.")


def attach_live(host: str, port: int, token: str | None, timeout: float = 1.5,
                on_poll: Callable[[], bool] | None = None) -> LiveSession:
    """Attach to the add-on on ``host:port``, with BLENDERLENS_TOKEN (``token``) or the connection file's token.

    ``on_poll`` becomes the connection's hook (BlenderConnection.on_poll). A refused connection raises
    ``not_connected``, and a Blender that accepts the connection but does not reply to ``hello`` in time
    raises ``busy``.
    """
    token, source = live_token(port, token)
    connection = BlenderConnection(host, port, token)
    connection.on_poll = on_poll
    try:
        connection.open(timeout)
    except OSError:
        raise ToolFailure("not_connected", f"No BlenderLens add-on is listening on {host}:{port}.") from None
    try:
        reply = connection.call("hello", {}, 10.0)
    except ToolFailure as failure:
        connection.close()
        # A port that accepts the connection but is slow to reply to hello belongs to a busy Blender running the
        # add-on, not an absent one, so auto mode must not start a background Blender beside it.
        if failure.kind == "deadline":
            raise ToolFailure("busy", f"A Blender window running the add-on accepted the connection on port {port}, "
                                      "but it is busy and did not respond to hello within 10s.",
                              "Let it finish what it is doing (a render, a modal tool) and call again, or call "
                              "open_session with mode background to work without it.") from None
        if failure.kind == "connection_lost":
            # The add-on closes, without a reply, any connection whose first line lacks its token, so that no
            # other local process can send it commands.
            raise _token_refused(host, port, token, source) from None
        raise
    if not reply.get("ok"):
        connection.close()
        raise refusal_from(reply)
    _check_protocol(reply["result"], f"on {host}:{port}")
    return LiveSession(connection, reply["result"])


def _abandon_start(process: subprocess.Popen | None, connection: BlenderConnection | None, folder: str) -> None:
    """Clean up after a background start that failed, so that no Blender, socket or working folder is left behind."""
    if connection is not None:
        connection.close()
    if process is not None:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log(f"the background Blender (pid {process.pid}) did not exit")
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
    _remove_tree(folder)


def start_background(binary: str, file: str | None, start_timeout: float,
                     on_poll: Callable[[], bool] | None = None) -> BackgroundSession:
    """Start ``blender --background`` serving the add-on's commands, and attach to it.

    ``on_poll`` runs on every turn of the wait for Blender to come up, and is the hook of the first
    ``hello`` too. When it returns True (the request was cancelled), the start is abandoned and
    nothing is left behind.
    """
    check_floor(binary)
    folder = tempfile.mkdtemp(prefix=FOLDER_PREFIX)
    ready = os.path.join(folder, "ready.json")
    log_path = os.path.join(folder, "blender.log")
    token = secrets.token_hex(16)
    command = [binary, "--background", "--factory-startup"]
    if file:
        command.append(file)
    command += ["--python", str(BACKGROUND_SCRIPT), "--", "--host", DEFAULT_HOST, "--port", "0",
                "--ready-file", ready]
    if not file:
        command.append("--empty")
    env = dict(os.environ, BLENDERLENS_SESSION_TOKEN=token)
    process = connection = None
    started = False
    try:
        try:
            with open(log_path, "wb") as log_file:
                # stdout goes to the log file, never to a pipe: Blender writes a lot, and a pipe that nobody
                # reads fills up and blocks Blender in the middle of a call.
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log_file, stderr=subprocess.STDOUT,
                                           env=env, creationflags=_NO_WINDOW)
        except OSError as exc:
            raise ToolFailure("start_failed", f"Could not run {binary}: {exc}", command=" ".join(command)) from None
        try:
            with open(os.path.join(folder, OWNER_FILE), "w", encoding="utf-8") as handle:
                json.dump({"server_pid": os.getpid(), "blender_pid": process.pid}, handle)
        except OSError:
            pass  # Only the sweep of abandoned working folders reads it, and without it the sweep errs toward keeping.
        deadline = time.monotonic() + start_timeout
        info = None
        while time.monotonic() < deadline:
            if os.path.isfile(ready):
                try:
                    with open(ready, encoding="utf-8") as handle:
                        info = json.load(handle)
                    break
                except (OSError, json.JSONDecodeError):
                    pass
            if process.poll() is not None:
                break
            if on_poll is not None and on_poll():
                raise ToolFailure("start_failed", "The start was abandoned: the request was cancelled.")
            time.sleep(0.1)
        if info is None:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
                reason = f"Blender did not become ready within {start_timeout:g}s."
            else:
                reason = f"Blender exited with code {process.returncode} before it was ready."
            raise ToolFailure("start_failed", reason, log=log_tail(log_path), command=" ".join(command))
        connection = BlenderConnection(info.get("host", DEFAULT_HOST), int(info["port"]), token)
        connection.on_poll = on_poll
        try:
            connection.open(5.0)
            reply = connection.call("hello", {}, 30.0)
        except (OSError, ToolFailure) as exc:
            raise ToolFailure("start_failed", f"Blender started, but its socket did not respond: {exc}",
                              log=log_tail(log_path)) from None
        if not reply.get("ok"):
            raise refusal_from(reply)
        _check_protocol(reply["result"], "in the background Blender")
        session = BackgroundSession(connection, reply["result"], process, folder, log_path)
        started = True
        return session
    finally:
        if not started:
            _abandon_start(process, connection, folder)


def _float_env(name: str, default: float) -> float:
    """Return an environment variable as a number, or ``default`` when it is unset or not a number."""
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class SessionManager:
    """The holder of the one Blender session this server talks to, which it attaches or starts on demand."""

    def __init__(self) -> None:
        self.host = os.environ.get("BLENDERLENS_HOST", DEFAULT_HOST)
        try:
            self.port = int(os.environ.get("BLENDERLENS_PORT", DEFAULT_PORT))
        except ValueError:
            self.port = DEFAULT_PORT
        self.token = os.environ.get("BLENDERLENS_TOKEN") or None
        mode = os.environ.get("BLENDERLENS_MODE", "auto").lower()
        self.mode = mode if mode in MODES else "auto"
        self.timeout = _float_env("BLENDERLENS_TIMEOUT", 120.0)
        self.start_timeout = _float_env("BLENDERLENS_START_TIMEOUT", 90.0)
        self.render_dir = os.environ.get("BLENDERLENS_RENDER_DIR") or default_render_dir()
        self.session: Session | None = None
        #: Set by the server for the request it is serving. It is called on each poll of every wait for Blender,
        #: with what the wait is for, "start" or "call": every 0.1 s while a background Blender comes up, and every
        #: quarter of a second while a reply is awaited on the socket. When it returns True, the wait stops.
        self.on_poll: Callable[[str], bool] | None = None

    def _poll_call(self) -> bool:
        """Run ``on_poll`` for a poll of a wait for a command's result; return True to stop the wait."""
        return self.on_poll is not None and bool(self.on_poll("call"))

    def _poll_start(self) -> bool:
        """Run ``on_poll`` for a poll of a wait for a background Blender to start; return True to stop it."""
        return self.on_poll is not None and bool(self.on_poll("start"))

    # -- attaching ---------------------------------------------------------

    def ensure(self) -> Session:
        """Return the attached session, attaching or starting one by BLENDERLENS_MODE when there is none.

        In auto mode, the Blender window running the add-on is used when it accepts the connection, and a
        background Blender is started only when the connection is refused; a Blender that is busy, or an
        add-on that refuses the token, raises instead. A background Blender found to have exited raises
        ``connection_lost``.
        """
        if self.session is not None:
            if isinstance(self.session, BackgroundSession) and not self.session.alive():
                self._lost("The background Blender exited.")
            return self.session
        if self.mode in ("auto", "live"):
            try:
                self.session = attach_live(self.host, self.port, self.token, on_poll=self._poll_call)
                log(f"attached to the Blender window on {self.host}:{self.port}")
                return self.session
            except ToolFailure as failure:
                # Only a refused connection means there is no Blender window running the add-on. A Blender that
                # is busy, or an add-on that refuses this server's token, is still there, and a background
                # Blender beside it would split the work between two Blenders.
                if self.mode == "live" or failure.kind != "not_connected":
                    raise
        self.session = self._start_background(None)
        return self.session

    def _start_background(self, file: str | None) -> BackgroundSession:
        """Start a background Blender, opening ``file`` when one is given, and return its session."""
        binary = find_blender_binary()
        if binary is None:
            raise missing_blender()
        session = start_background(binary, file, self.start_timeout, on_poll=self._poll_start)
        session.connection.on_poll = self._poll_call
        log(f"started background Blender (pid {session.process.pid})")
        return session

    def _end(self, session: Session, force: bool) -> None:
        """Close a session for good.

        A background Blender exits (killed at once with ``force``) and its working folder is removed; in live mode
        only the socket is closed. A failure while stopping is logged, not raised.
        """
        if self.session is session:
            self.session = None
        try:
            session.stop(force=force)
        except Exception as exc:  # noqa: BLE001 -- the session is dropped here whatever stopping it raises
            log(f"stopping the session failed: {exc!r}")

    def _lost(self, message: str):
        """End the current session and raise ``connection_lost``, with the end of a background Blender's log."""
        session = self.session
        extra = {}
        if isinstance(session, BackgroundSession):
            # Read now: the log is in the working folder, which ending the session removes.
            extra["log"] = log_tail(session.log_path)
        if session is not None:
            self._end(session, force=True)
        raise ToolFailure("connection_lost", message, **extra)

    def _busy_with(self, session: Session) -> dict | None:
        """Return what the session's Blender is still running, after taking in a result that has already arrived.

        An exited Blender, or a connection found closed, ends the session, and None is returned.
        """
        try:
            if isinstance(session, BackgroundSession) and not session.alive():
                raise ToolFailure("connection_lost", "The background Blender exited.")
            session.connection.collect()
        except ToolFailure:
            self._end(session, force=True)
            return None
        return session.connection.busy()

    @staticmethod
    def _busy_refusal(busy: dict, doing: str) -> ToolFailure:
        """Return the ``busy`` refusal of ``doing``, an action refused while Blender runs another command."""
        return ToolFailure("busy", f"Blender is still running {busy['command']} ({busy['seconds']:g}s so far), so "
                                   f"{doing} was refused.",
                           "Call again once it is done (get_status says what it is running), or pass discard: "
                           "true. That stops a background Blender and loses its unsaved work, or disconnects from "
                           "the Blender window, which keeps its work.",
                           busy=busy)

    # -- calling -----------------------------------------------------------

    def call(self, command: str, params: dict | None = None, timeout: float | None = None) -> dict:
        """Send one command to Blender through the attached session, and return its result.

        Raise ToolFailure when Blender refuses the command or cannot be reached. After open_file,
        create_file and save_file, the session's details are refreshed.
        """
        session = self.ensure()
        try:
            reply = session.connection.call(command, params or {}, timeout or self.timeout)
        except ToolFailure as failure:
            # A deadline or busy refusal means Blender is at work, so the session stays attached. Only a closed
            # socket ends it.
            if failure.kind == "connection_lost":
                self._lost(failure.message)
            raise
        if reply.get("ok"):
            if command in ("open_file", "create_file", "save_file"):
                try:
                    session.refresh()
                except ToolFailure:
                    pass
            return reply["result"]
        raise refusal_from(reply)

    # -- the session tools ---------------------------------------------------

    def probe_live(self) -> dict:
        """Return, for get_status, whether the add-on in a Blender window responds on the configured port.

        The probe opens a connection of its own and closes it again; an attached live session counts as
        responding without a probe.
        """
        out = {"host": self.host, "port": self.port}
        if isinstance(self.session, LiveSession):
            out["responding"] = True
            out["addon_version"] = self.session.hello.get("addon_version")
            return out
        try:
            probe = attach_live(self.host, self.port, self.token, timeout=0.5, on_poll=self._poll_call)
        except ToolFailure as failure:
            out["responding"] = False
            if failure.kind != "not_connected":
                out["problem"] = failure.payload()
            return out
        out["responding"] = True
        out["addon_version"] = probe.hello.get("addon_version")
        out["file"] = probe.hello.get("file")
        probe.stop()
        return out

    def _live_addon_responds(self) -> bool:
        """Return whether a Blender window running the add-on accepts a connection, for auto mode.

        Only a refused connection counts as no. A Blender that is busy, or an add-on that refuses this server's
        token, raises instead, so no background Blender is started beside it.
        """
        if isinstance(self.session, LiveSession):
            return True
        try:
            probe = attach_live(self.host, self.port, self.token, timeout=0.5, on_poll=self._poll_call)
        except ToolFailure as failure:
            if failure.kind == "not_connected":
                return False
            raise
        probe.stop()
        return True

    def status(self) -> dict:
        """Return what get_status reports about the server, the attached session, the live add-on and Blender.

        It also gives the render folder, the newest recovery copies, and what the next call would attach to.
        Checking the session takes in any result that has already arrived, and ends a session whose Blender
        has gone.
        """
        binary = find_blender_binary()
        blender = {"path": binary, "found": binary is not None}
        if binary:
            blender["version"] = blender_version(binary)
        else:
            blender["problem"] = missing_blender().payload()
        session = None
        current = self.session
        if current is not None:
            try:
                if isinstance(current, BackgroundSession) and not current.alive():
                    raise ToolFailure("connection_lost", "The background Blender exited.")
                current.connection.collect()
                # While Blender is busy nothing is sent, because the hello would only wait behind its work.
                if current.connection.outstanding is None:
                    current.refresh(5.0)
            except ToolFailure as failure:
                # A deadline or busy refusal means Blender is at work, so the session stays attached.
                if failure.kind == "connection_lost":
                    log(f"the session ended: {failure.message}")
                    self._end(current, force=True)
            if self.session is current:
                session = current.describe()
        return {
            "server_version": __version__,
            "protocol": PROTOCOL,
            "configured_mode": self.mode,
            "session": session,
            "live_addon": self.probe_live(),
            "blender": blender,
            "render_dir": self.render_dir,
            "recovery": recovery_files(),
            "next_call_attaches": None if session else self._next_attach(),
        }

    def _next_attach(self) -> str:
        """Return what the next call that needs Blender attaches to, as get_status's next_call_attaches says it."""
        if self.mode == "background":
            return "background: starts blender --background"
        if self.mode == "live":
            return f"live: the add-on on {self.host}:{self.port}, or a not_connected refusal"
        return "auto: the Blender window running the add-on if it responds, otherwise a background Blender"

    def open(self, mode: str, file: str | None, discard: bool) -> dict:
        """Serve open_session: attach to or start a session in ``mode``, and open ``file`` when one is given.

        A session already attached in that mode is kept, and opens the file. A busy session is refused
        unless ``discard`` is true. A session in the other mode is closed first, which, like the file
        being opened, refuses to lose unsaved work unless ``discard`` is true.
        """
        if mode not in MODES:
            raise ToolFailure("bad_argument", f"'mode' must be one of {', '.join(MODES)}.")
        if file and not os.path.isfile(file):
            raise ToolFailure("not_found", f"No file exists at {file}.", "Give the path of an existing .blend file.")
        current = self.session
        if current is not None:
            busy = self._busy_with(current)
            if busy is not None:
                if not discard:
                    raise self._busy_refusal(busy, "opening a session")
                self._end(current, force=True)
            current = self.session
        if mode == "auto":
            mode = "live" if self._live_addon_responds() else "background"
        if current is not None and current.mode == mode:
            if file:
                self.call("open_file", {"path": file, "discard": discard})
            try:
                current.refresh()
            except ToolFailure as failure:
                if failure.kind == "connection_lost":
                    self._lost(failure.message)
                raise
            return {"opened": mode, "already_attached": True, "session": current.describe()}
        if current is not None:
            self.close(discard)
        if mode == "live":
            self.session = attach_live(self.host, self.port, self.token, on_poll=self._poll_call)
            if file:
                self.call("open_file", {"path": file, "discard": discard})
        else:
            self.session = self._start_background(file)
        return {"opened": mode, "already_attached": False, "session": self.session.describe()}

    def close(self, discard: bool) -> dict:
        """Serve close_session: disconnect from the attached session.

        A background Blender with unsaved changes, and a session whose Blender is busy, are refused unless
        ``discard`` is true. A background Blender exits; a Blender window stays open with its work, and only
        this server disconnects from it.
        """
        session = self.session
        if session is None:
            return {"closed": None, "note": "No session was attached."}
        gone = {"closed": session.mode, "note": "Blender had already gone; there was nothing left to close."}
        if isinstance(session, BackgroundSession) and not session.alive():
            self._end(session, force=True)
            return gone
        busy = self._busy_with(session)
        if self.session is not session:
            return gone
        if busy is None and isinstance(session, BackgroundSession):
            try:
                session.refresh(10.0)
            except ToolFailure as failure:
                if failure.kind == "connection_lost":
                    self._end(session, force=True)
                    return gone
                busy = session.connection.busy()  # The hello itself ran out of time, so Blender is busy.
            if busy is None and session.hello.get("unsaved_changes") and not discard:
                file = session.hello.get("file")
                save = f"Call save_file first (it saves to {file})" if file else "Call save_file with a path first"
                raise ToolFailure("unsaved_changes",
                                  "The background Blender has unsaved changes, which closing it would lose.",
                                  f"{save}, or pass discard: true.")
        if busy is not None:
            if not discard:
                raise self._busy_refusal(busy, "closing the session")
            self._end(session, force=True)
            return {"closed": session.mode,
                    "note": f"The Blender window stays open, still running {busy['command']}. Only this server "
                            "disconnected from it."
                    if session.mode == "live" else
                    f"The background Blender was stopped while it ran {busy['command']}; its unsaved work is gone."}
        self._end(session, force=False)
        return {"closed": session.mode,
                "note": "The Blender window stays open. Only this server disconnected from it."
                if session.mode == "live" else "The background Blender has exited."}

    def shutdown(self) -> None:
        """Keep a recovery copy of unsaved background work, then end the session, when the server exits.

        A background Blender's unsaved work would otherwise be lost when the server's client goes away. A
        Blender still running a call is killed rather than asked to shut down.
        """
        session = self.session
        if session is None:
            return
        if isinstance(session, BackgroundSession) and session.alive():
            try:
                self._save_recovery(session)
            except Exception as exc:  # noqa: BLE001 -- the server exits whether or not the copy was kept
                log(f"keeping a recovery copy failed: {exc!r}")
        self._end(session, force=session.connection.outstanding is not None)

    def _save_recovery(self, session: BackgroundSession) -> str | None:
        """Ask the background Blender to write a copy of its unsaved work to the recovery folder, and log where.

        Return the copy's path, or None when there is no connection, nothing is unsaved, or the copy fails.
        A failure is logged with its reason.
        """
        connection = session.connection
        if connection.sock is None:
            return None
        try:
            connection.wait_idle(time.monotonic() + RECOVERY_WAIT, "save_copy")
            if not session.refresh(10.0).get("unsaved_changes"):
                return None
            folder = recovery_dir()
            os.makedirs(_runtime_dir(), mode=0o700, exist_ok=True)
            os.makedirs(folder, mode=0o700, exist_ok=True)
            pid = session.hello.get("pid") or session.process.pid
            path = os.path.join(folder, f"{time.strftime('%Y%m%d-%H%M%S')}-{pid}.blend")
            reply = connection.call("save_copy", {"path": path}, RECOVERY_SAVE_TIMEOUT)
        except (ToolFailure, OSError) as exc:
            log(f"no recovery copy of the unsaved work: {getattr(exc, 'message', exc)}")
            return None
        if not reply.get("ok"):
            log(f"no recovery copy of the unsaved work: {reply.get('error')}")
            return None
        log(f"the background Blender had unsaved work; a copy is in {path}")
        return path
