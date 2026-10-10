"""The MCP server for modelling in Blender: JSON-RPC 2.0 over stdio, one message per line.

stdin is read on a thread of its own. That thread replies to ``ping`` and handles
``notifications/cancelled`` at once, and queues every other message for the main thread. The
main thread serves the queued messages one at a time, and it is the only thread that uses the
session and the socket to Blender. Reading on a separate thread lets pings and cancellations
through while the main thread waits for Blender.

The server supports both kinds of MCP protocol revision. The session-based revisions (2024-11-05
to 2025-11-25) start with ``initialize``, which selects one of them for the whole session. Under
the stateless revision, 2026-07-28, each request names its revision in ``_meta`` and is served on
its own.
"""

from __future__ import annotations

import base64
import json
import math
import os
import queue
import re
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

from blenderlens_mcp import __version__
from blenderlens_mcp.arguments import check_arguments
from blenderlens_mcp.errors import ToolFailure
from blenderlens_mcp.glb import TARGETS, GLBError, summarize
from blenderlens_mcp.resources import GUIDE_URI, RESOURCES, ResourceNotFound, read_resource
from blenderlens_mcp.session import SessionManager, housekeeping, log
from blenderlens_mcp.tools import BY_NAME, LOCAL, PYTHON, TOOLS, path_keys

# The MCP protocol revisions this server implements. initialize selects one of the session-based revisions:
# the client's when this server supports it, otherwise the newest this server has, as the specification says.
# A request that names its revision in _meta is served on its own under the stateless revision, 2026-07-28.
STATELESS_PROTOCOL_VERSION = "2026-07-28"
SESSION_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_PROTOCOL_VERSIONS = (STATELESS_PROTOCOL_VERSION, *SESSION_PROTOCOL_VERSIONS)
LATEST_SESSION_PROTOCOL_VERSION = SESSION_PROTOCOL_VERSIONS[0]
# structuredContent and resource_link content first appear in revision 2025-06-18, so a client on an older
# revision gets text and image content only.
STRUCTURED_SINCE = "2025-06-18"
# JSON-RPC batches (an array of messages on one line) exist only in revision 2025-03-26; 2025-06-18 removed them.
BATCH_VERSION = "2025-03-26"
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
UNSUPPORTED_PROTOCOL_VERSION = -32022
# The tool and resource lists, and the guide's text, are fixed for the life of the process, so an hour
# is a safe freshness hint for a client's cache.
LIST_TTL_MS = 3_600_000
SERVER_INFO = {"name": "blenderlens-mcp", "version": __version__}
CAPABILITIES = {"tools": {}, "resources": {}}
#: The JSON-RPC error code for a resources/read of a URI this server does not have (the specification's
#: "Resource not found").
RESOURCE_NOT_FOUND = -32002
#: A new id each time the server starts. get_status returns it as server_session, so a model can tell that the
#: server has restarted.
SERVER_SESSION = secrets.token_hex(8)
#: Tools that take a ``timeout`` argument of their own, for work that may need longer than BLENDERLENS_TIMEOUT.
#: The server uses that timeout for its own wait, and never sends it to Blender.
TIMED = ("render_preview", *PYTHON, "bake_texture")
#: The render_preview styles that render with Workbench whatever engine is asked for, as the add-on's
#: preview.WORKBENCH_STYLES says.
WORKBENCH_STYLES = ("clay", "random")

SERVER_INSTRUCTIONS = (
    "BlenderLens gives you a Blender session to model in. In live mode it is the Blender window the user "
    "watches, with the BlenderLens add-on running. In background mode it is a Blender the server starts itself "
    "when no such window is open.\n"
    "- You work on the open .blend file's objects, their meshes, modifiers, materials, transforms and "
    "collections, and on .glb exports and preview renders.\n"
    "- Every call goes to the attached session. `get_status` says which session that is, without starting one. "
    "The first call that needs Blender attaches by BLENDERLENS_MODE. In auto mode that is the Blender window "
    "running the add-on when it responds, and otherwise a background Blender. `open_session` and "
    "`close_session` choose explicitly. A background session's unsaved work is lost when it ends, so call "
    "`save_file` to keep it.\n"
    "- Build with `create_object`, `create_mesh`, `edit_mesh`, `add_modifier` and `set_material`. Look with "
    "`render_preview`, whose PNG you open (or get inline), and `get_object`. Check with `check_asset` and "
    "`measure_objects`. Deliver with `export_glb`, which reads the written file back and reports what it holds. "
    "`undo_change` takes calls back.\n"
    "- Units are metres and rotations are degrees. Z is up, and a model's front faces -Y. export_glb converts to "
    "glTF's Y-up, where that front faces +Z. Colours are linear unless given as '#RRGGBB'.\n"
    "- No tool paints textures, sculpts or rigs. `run_python` and `run_script`, when enabled, reach those and "
    "anything else, but use the tools first.\n"
    f"- Read the resource {GUIDE_URI} for the conventions, the traps and the build, look, check, export loop.\n"
    "- A refusal is a structured error with `error`, `exit`, `kind` and `hint`. Kind `not_found` says what is "
    "missing. `bad_argument` names what is accepted. `not_connected` means no add-on responded. `connection_lost` "
    "means Blender went away. `unsaved_changes` asks for save_file or discard. `deadline` means this call ran out "
    "of time while Blender kept working on it. `busy` means Blender was still working on an earlier call, so this "
    "one was not sent. `not_allowed` means this server or that Blender forbids it. `blender_missing` means no "
    "executable was found."
)

_manager: SessionManager | None = None
#: The protocol revision that initialize negotiated. Requests that name no revision of their own are served
#: under it, and batches are accepted only when it is 2025-03-26. Whether a result may carry structuredContent
#: and resource links is decided by the revision that serves its request.
_session_version = LATEST_SESSION_PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------


class ParseFailure:
    """A stdin line that could not be turned into a JSON-RPC message."""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message


def parse_line(line: bytes) -> dict | list | ParseFailure:
    """Parse one stdin line into a message, or into a list for a batch.

    Every way a line can fail to parse gives a ParseFailure, which the main thread replies to, in
    turn, with a JSON-RPC error; the server keeps running.
    """
    text = line.strip()
    # Windows PowerShell 5.1 starts piped input with a byte order mark, which is not valid JSON, so it is stripped
    # before the line is parsed.
    if text.startswith(b"\xef\xbb\xbf"):
        text = text[3:]
    try:
        payload = json.loads(text.decode("utf-8"))
    except UnicodeDecodeError:
        return ParseFailure(-32700, "Parse error: stdin was not valid UTF-8")
    except json.JSONDecodeError as exc:
        return ParseFailure(-32700, f"Parse error: {exc}")
    except Exception as exc:  # noqa: BLE001 -- deep nesting raises RecursionError, which must not end the server
        return ParseFailure(-32700, f"Parse error: {type(exc).__name__}: {exc}")
    if isinstance(payload, (dict, list)):
        return payload
    return ParseFailure(-32600, f"Invalid Request: expected a JSON object, got {type(payload).__name__}")


def jsonable(value: Any) -> Any:
    """Return ``value`` in a form that strict JSON can carry.

    Strict JSON has no NaN or infinities, so they become the strings "nan", "inf" and "-inf". Tuples become
    lists, dictionary keys that are not strings become their text, and anything else JSON has no form for
    becomes its text.
    """
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"
    if value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, dict):
        return {key if isinstance(key, str) else str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return str(value)


def encode(message: Any) -> bytes:
    """Return one message as a line of strict JSON, without the newline.

    Raise an exception when the message cannot be encoded, for example when it is nested too deeply.
    """
    return json.dumps(jsonable(message), allow_nan=False).encode("utf-8")


class Wire:
    """Standard output, written one whole line at a time under a lock.

    Two threads write to it: the reader thread replies to pings while the main thread writes results
    and progress notifications. The lock keeps their lines from mixing.
    """

    def __init__(self, stream=None):
        self.stream = stream
        self.lock = threading.Lock()

    def write(self, data: bytes) -> None:
        """Write ``data`` and a newline, and flush. A failure to write is logged, not raised."""
        with self.lock:
            stream = self.stream if self.stream is not None else sys.stdout.buffer
            try:
                stream.write(data + b"\n")
                stream.flush()
            except (OSError, ValueError) as exc:
                # The client has gone away. stdin reaches its end soon after, and that stops the server.
                log(f"writing to stdout failed: {exc!r}")

    def send(self, message: Any) -> None:
        """Write ``message`` as one line of strict JSON."""
        self.write(encode(message))


_wire = Wire()

#: The item the reader thread puts on the queue when stdin ends.
EOF = object()


def _request_key(request_id: Any) -> tuple | None:
    """Return a request id as a key that keeps 1 and "1" apart, or None for a value no MCP request id may be."""
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        return None
    return type(request_id).__name__, request_id


class Inbox:
    """The reader of stdin, which runs on a thread of its own.

    The thread parses each line. It replies to ``ping`` itself, at once, and handles
    ``notifications/cancelled`` by setting the cancel flag of the request it names. Everything else
    goes on the queue for the main thread, each request with its cancel flag. A flag exists from the
    moment its request is read until its response is sent, so a cancellation reaches a request still
    waiting in the queue as well as one being served. A cancellation for any other id is ignored,
    and nothing is written for it.
    """

    def __init__(self, stream, wire: Wire):
        self.stream = stream
        self.wire = wire
        self.queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._flags: dict[tuple, threading.Event] = {}

    def start(self) -> None:
        """Start the reader thread, which runs until stdin ends."""
        threading.Thread(target=self._run, name="blenderlens-stdin", daemon=True).start()

    def get(self) -> Any:
        """Return the next queued item, waiting for one: a (message, cancel flag) pair, or EOF."""
        return self.queue.get()

    def _run(self) -> None:
        """Read and take in stdin line by line until it ends or breaks, then queue EOF."""
        try:
            while True:
                line = self.stream.readline()
                if not line:
                    break
                if line.strip():
                    self._take(parse_line(line))
        except Exception as exc:  # noqa: BLE001 -- a broken stdin ends the server as EOF does, recovery copy included
            log(f"reading stdin failed: {exc!r}")
        finally:
            self.queue.put(EOF)

    def _take(self, message: Any) -> None:
        """Handle one parsed line: a cancellation or a ping at once, anything else onto the queue."""
        if isinstance(message, dict) and message.get("method") == "notifications/cancelled" and "id" not in message:
            self.cancel(message.get("params"))
            return
        if isinstance(message, dict) and message.get("method") == "ping" and "id" in message:
            # Replied to here, so a ping gets its reply even while the main thread waits for Blender. A ping
            # changes no state and touches neither the session nor Blender, so serving it on this thread is safe.
            reply = serve_message(message)
            if reply is not None:
                self.wire.write(reply)
            return
        if isinstance(message, list):
            self.queue.put((message, [self._flag(element) for element in message]))
        else:
            self.queue.put((message, self._flag(message)))

    def _flag(self, message: Any) -> threading.Event | None:
        """Make and register the cancel flag of a request, or return None for a message with no valid id."""
        key = _request_key(message.get("id")) if isinstance(message, dict) else None
        if key is None:
            return None
        flag = threading.Event()
        with self._lock:
            self._flags[key] = flag
        return flag

    def done(self, message: Any, flag: threading.Event | None) -> None:
        """Forget the cancel flag of a request whose response has been sent, or never will be.

        A later cancellation for its id is then ignored.
        """
        if flag is None:
            return
        key = _request_key(message.get("id"))
        with self._lock:
            if self._flags.get(key) is flag:
                del self._flags[key]

    def cancel(self, params: Any) -> None:
        """Handle notifications/cancelled: the named request stops waiting for Blender and gets no response."""
        if not isinstance(params, dict):
            return
        key = _request_key(params.get("requestId"))
        with self._lock:
            flag = self._flags.get(key) if key is not None else None
        if flag is None:
            return  # An unknown request, or one already responded to: the specification lets a server ignore it.
        flag.set()
        reason = params.get("reason")
        log(f"request {params['requestId']!r} cancelled" + (f": {str(reason)[:200]}" if reason else ""))


def jsonrpc_response(req_id: Any, result: Any) -> dict:
    """Build a JSON-RPC response that carries ``result``."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    """Build a JSON-RPC error response, with ``data`` when it is given."""
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def tool_result(payload: dict, is_error: bool = False, blocks: list | None = None, structured: bool = True) -> dict:
    """Build a tool's MCP result: the payload as JSON text, then any other content blocks.

    With ``structured`` (revision 2025-06-18 and later), the payload is also given as structuredContent.
    Non-finite numbers go out as "nan", "inf" and "-inf". A resource_link block, which revision 2025-06-18
    introduced, is left out for an older client, which has no such content type.
    """
    payload = jsonable(payload)
    content = [{"type": "text", "text": json.dumps(payload, indent=1, ensure_ascii=False, allow_nan=False)}]
    content.extend(block for block in blocks or [] if structured or block.get("type") != "resource_link")
    out = {"content": content, "isError": is_error}
    if structured:
        out["structuredContent"] = payload
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def manager() -> SessionManager:
    """Return the session manager, creating it on first use."""
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager


def _resolve(path: str) -> str:
    """Return ``path`` as an absolute path, with a leading ~ expanded to the home folder."""
    return os.path.abspath(os.path.expanduser(path))


def _slug(text: str) -> str:
    """Return ``text`` reduced to letters, digits, - and _, at most 40 characters, for use in a file name."""
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:40] or "scene"


# -- what this server allows ---------------------------------------------------


def python_allowed() -> bool:
    """Return whether run_python and run_script are allowed.

    BLENDERLENS_ALLOW_PYTHON set to 0, false, off or no switches them off. They are allowed by default.
    """
    return os.environ.get("BLENDERLENS_ALLOW_PYTHON", "1").strip().lower() not in ("0", "false", "off", "no")


def allowed_dirs() -> list[str]:
    """Return the folders in BLENDERLENS_ALLOWED_DIRS as real paths, or an empty list, which confines nothing."""
    listed = os.environ.get("BLENDERLENS_ALLOWED_DIRS", "")
    # An empty entry (a stray separator) is skipped rather than taken as the current working directory.
    return [os.path.realpath(_resolve(entry.strip())) for entry in listed.split(os.pathsep) if entry.strip()]


def _inside(path: str, folder: str) -> bool:
    """Return whether ``path`` lies inside ``folder``, ignoring case where the file system does (Windows)."""
    path, folder = os.path.normcase(path), os.path.normcase(folder)
    try:
        return os.path.commonpath([path, folder]) == folder
    except ValueError:  # The paths are on different drives (Windows).
        return False


def check_confined(path: str, preview: bool = False) -> None:
    """Refuse ``path``, already resolved, unless it lies inside a folder BLENDERLENS_ALLOWED_DIRS allows.

    Nothing is refused when the variable is unset. With ``preview``, the path is render_preview's, which
    may also lie in render_dir even when no allowed folder holds it. Every other tool may use a path in
    render_dir only when an allowed folder holds it.
    """
    folders = allowed_dirs()
    if not folders:
        return
    real = os.path.realpath(path)
    render_dir = os.path.realpath(_resolve(manager().render_dir))
    if any(_inside(real, folder) for folder in [*folders, *([render_dir] if preview else [])]):
        return
    where = f"Use a path inside {', '.join(folders)}"
    raise ToolFailure("not_allowed", f"{path} is outside the folders BLENDERLENS_ALLOWED_DIRS allows.",
                      f"{where}, or the render folder ({render_dir}, set by BLENDERLENS_RENDER_DIR)." if preview
                      else f"{where}.")


def _check_paths(name: str, arguments: dict) -> None:
    """Check every path a tool reads or writes against BLENDERLENS_ALLOWED_DIRS, before anything reaches Blender."""
    if not allowed_dirs():
        return
    given = [key for key in path_keys(name) if arguments.get(key)]
    for key in given:
        check_confined(_resolve(str(arguments[key])), preview=name == "render_preview")
    if not given and name == "save_file":
        # Without a path, Blender saves where the file was last saved, which may be anywhere, so that path is checked.
        file = manager().ensure().refresh().get("file")
        if file:
            check_confined(file)


def _warn_of_libraries(name: str, result: dict) -> None:
    """Add a warning naming the libraries an opened or imported .blend links from outside the allowed folders.

    BLENDERLENS_ALLOWED_DIRS limits the paths given to tools. Blender reads a .blend's own library links
    from wherever they point when it opens the file, so the server cannot block them; it reports them in
    the tool result's ``warnings`` list instead.
    """
    folders = allowed_dirs()
    if name not in ("open_file", "import_file") or not folders or not isinstance(result, dict):
        return
    outside = [str(library) for library in result.get("libraries") or []
               if not any(_inside(os.path.realpath(str(library)), folder) for folder in folders)]
    if not outside:
        return
    result.setdefault("warnings", []).append({
        "code": "libraries_outside_allowed_dirs",
        "libraries": outside,
        "message": f"The open file links {len(outside)} {'library' if len(outside) == 1 else 'libraries'} "
                   f"outside BLENDERLENS_ALLOWED_DIRS, which Blender read from there: {', '.join(outside)}. "
                   "BLENDERLENS_ALLOWED_DIRS limits the paths given to tools, not the links inside a .blend.",
    })


def _default_render_path(arguments: dict) -> str:
    """Return a preview's default path in render_dir, named after its first object, its view and the time."""
    objects = arguments.get("objects") or []
    subject = _slug(objects[0]) if objects else "scene"
    view = _slug(str(arguments.get("view") or ("custom" if arguments.get("azimuth") is not None
                                               or arguments.get("elevation") is not None else "three_quarter")))
    if arguments.get("views"):
        view = "views"
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    return os.path.join(manager().render_dir, f"{subject}-{view}-{stamp}.png")


def _flag(arguments: dict, key: str, default: bool = False) -> bool:
    """Return a true/false argument, or ``default`` when it is absent.

    Any other value is refused rather than guessed at, because in Python bool("false") is true.
    """
    value = arguments.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ToolFailure("bad_argument", f"'{key}' must be true or false, got {value!r}.")
    return value


def preview_link(path: str) -> dict:
    """Return a resource_link block for a preview's PNG, so a client can open it without reading the text.

    Only revision 2025-06-18 and later have resource links.
    """
    return {"type": "resource_link", "uri": Path(path).as_uri(), "name": os.path.basename(path),
            "description": "The preview render", "mimeType": "image/png"}


def call_tool(name: str, arguments: dict) -> tuple[dict, list]:
    """Run one tool and return its result and any extra content blocks, as (result, blocks).

    BLENDERLENS_ALLOW_PYTHON and BLENDERLENS_ALLOWED_DIRS are checked first. The session tools are handled here;
    every other tool is sent to Blender with its paths resolved, and without the arguments the server
    keeps for itself (``timeout`` and ``inline``). Raise ToolFailure when the tool fails.
    """
    sessions = manager()
    if name in PYTHON and not python_allowed():
        setting = os.environ.get("BLENDERLENS_ALLOW_PYTHON", "").strip()
        raise ToolFailure("not_allowed", f"{name} is turned off on this server, whose environment sets "
                                         f"BLENDERLENS_ALLOW_PYTHON={setting}.",
                          "The other tools still work. To allow run_python and run_script, remove "
                          "BLENDERLENS_ALLOW_PYTHON from this server's environment (set in the MCP client's "
                          "configuration) or set it to 1, then restart the server.")
    _check_paths(name, arguments)
    if name == "get_status":
        return {**sessions.status(), "server_session": SERVER_SESSION}, []
    if name == "open_session":
        file = arguments.get("file")
        discard = _flag(arguments, "discard")
        return sessions.open(str(arguments.get("mode") or "auto").lower(), _resolve(file) if file else None,
                             discard), []
    if name == "close_session":
        return sessions.close(_flag(arguments, "discard")), []

    params = dict(arguments)
    for key in path_keys(name):
        if params.get(key):
            params[key] = _resolve(str(params[key]))
    timeout = None
    if name in TIMED:
        timeout = params.pop("timeout", None)
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            raise ToolFailure("bad_argument", "'timeout' must be a positive number of seconds.")
    inline = bool(params.pop("inline", False)) if name == "render_preview" else False
    if name == "render_preview" and not params.get("path"):
        params["path"] = _default_render_path(params)

    result = sessions.call(name, params, timeout)
    _warn_of_libraries(name, result)

    # What the server reads back, links to and embeds is the file it asked for, never a path from Blender's
    # result, so a Blender that returns a different path cannot make the server read some other file.
    blocks = []
    if name == "export_glb":
        path = str(params.get("path") or "")
        result["path"] = path
        # The target is taken from this server's own request, whose value the input schema and Blender have
        # both already checked.
        target = params.get("target") if params.get("target") in TARGETS else "generic"
        morphs = result.get("morph_targets")
        try:
            result["contents"] = summarize(path, expected=result.get("objects"), target=target,
                                           morphs=morphs if isinstance(morphs, dict) else None)
        except (OSError, GLBError) as exc:
            result["contents"] = {"valid": False, "problem": str(exc)}
    elif name == "render_preview":
        path = params["path"]
        result["path"] = path
        blocks.append(preview_link(path))
        if inline:
            with open(path, "rb") as handle:
                blocks.append({"type": "image", "mimeType": "image/png",
                               "data": base64.b64encode(handle.read()).decode("ascii")})
    return result, blocks


def handle_tool_call(name: str, arguments: dict, request: Request | None = None) -> dict:
    """Check the arguments, run the tool, and build its MCP result.

    A failure, a fault in BlenderLens itself included, is a result with isError set, never a protocol error.
    """
    structured = request.structured if request is not None else True
    try:
        check_arguments(name, BY_NAME[name]["inputSchema"], arguments)
        result, blocks = call_tool(name, arguments)
        return tool_result(result, blocks=blocks, structured=structured)
    except ToolFailure as failure:
        return tool_result(failure.payload(), is_error=True, structured=structured)
    except Exception as exc:  # noqa: BLE001 -- the server stays up whatever a tool does
        log(f"tool {name} failed: {exc!r}")
        failure = ToolFailure("internal_error", f"{type(exc).__name__}: {exc}")
        return tool_result(failure.payload(), is_error=True, structured=structured)


class Waiting:
    """A callback the session calls on each poll of a wait for Blender during one tools/call.

    The polls come every 0.1 s while a background Blender comes up, and every quarter of a second while a
    reply is awaited on the socket. A cancelled request stops the wait at once. Blender carries on with the
    command, and the request stays outstanding on the connection, so the next call waits for it to finish
    first. When the request has a progressToken, a notifications/progress goes out at most once a second:
    ``progress`` is the seconds since the call began, ``total`` the most it may take where that is known,
    and the message says what Blender is doing.
    """

    def __init__(self, request: Request, name: str, arguments: dict, sessions: SessionManager):
        self.request = request
        self.label = name
        if name == "render_preview":
            # The engine the add-on renders with: the clay and random styles always use Workbench, and auto
            # means Cycles.
            engine = arguments.get("engine")
            if arguments.get("style") in WORKBENCH_STYLES:
                engine = "workbench"
            elif engine in (None, "auto"):
                engine = "cycles"
            self.label = f"{name} ({engine})"
        timeout = arguments.get("timeout") if name in TIMED else None
        if name in LOCAL:
            self.limit = None  # The session tools' waits each have a bound of their own.
        elif isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            self.limit = float(timeout)
        else:
            self.limit = sessions.timeout
        self.start_limit = sessions.start_timeout
        self.began = time.monotonic()
        # The first poll reports at once, so the client learns that the call is alive however soon Blender
        # then returns; after that, reports go out at most once a second.
        self.last = float("-inf")
        self.phase = "call"
        #: Seconds a background start took before the call's own timeout began to run.
        self.offset = 0.0

    def __call__(self, phase: str) -> bool:
        """Return True when the wait should stop because the request was cancelled; send progress when due.

        ``phase`` is "start" while a background Blender starts, and "call" while a command runs.
        """
        if self.request.is_cancelled():
            return True
        token = self.request.progress_token
        if token is None:
            return False
        now = time.monotonic()
        if phase != self.phase:
            if phase == "call":
                self.offset = now - self.began
            self.phase = phase
        if now - self.last < 1.0:
            return False
        self.last = now
        elapsed = round(now - self.began, 1)
        if phase == "start":
            total = self.start_limit
            message = f"{self.label}: starting a background Blender, {elapsed:.0f} s of up to {total:.0f} s"
        elif self.limit is not None:
            total = round(self.offset + self.limit, 1)
            message = f"{self.label}: {elapsed:.0f} s of up to {total:.0f} s"
        else:
            total = None
            message = f"{self.label}: {elapsed:.0f} s"
        params = {"progressToken": token, "progress": elapsed, "message": message}
        if total is not None:
            params["total"] = total
        _wire.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})
        return False


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class Request:
    """One request being served: its id, method and parameters, the revision serving it, and its cancel flag."""

    def __init__(self, req_id: Any, method: str, params: dict, version: str,
                 cancelled: threading.Event | None = None):
        self.id = req_id
        self.method = method
        self.params = params
        self.version = version
        self.cancelled = cancelled

    @property
    def structured(self) -> bool:
        """Return whether this request's results may carry structuredContent and resource links."""
        return self.version >= STRUCTURED_SINCE

    @property
    def progress_token(self) -> str | int | None:
        """Return the request's progressToken, or None when it has none or it is not a string or an integer."""
        meta = self.params.get("_meta")
        token = meta.get("progressToken") if isinstance(meta, dict) else None
        return token if _request_key(token) is not None else None

    def is_cancelled(self) -> bool:
        """Return whether the client has cancelled this request."""
        return self.cancelled is not None and self.cancelled.is_set()


def listed_tools() -> list[dict]:
    """Return the tools/list entries, the same under every revision; run_python and run_script only while allowed."""
    return [{key: tool[key] for key in ("name", "title", "description", "inputSchema", "annotations") if key in tool}
            for tool in TOOLS if tool["name"] not in PYTHON or python_allowed()]


def respond(message: dict, cancelled: threading.Event | None = None) -> dict | None:
    """Return the response to one JSON-RPC message, or None when it gets none.

    A notification gets no response, whatever it holds, and neither does a response from the client: this
    server sends no requests, so there is nothing to match it to. A request that names 2026-07-28 in
    ``_meta``, and server/discover, are served under that stateless revision. A request that names any
    other revision in ``_meta``, a session-based one included, is refused as unsupported, since the
    session-based revisions are chosen by initialize, never in ``_meta``. Every other request is served
    under the revision that initialize negotiated.
    """
    if "id" not in message:
        return None
    req_id = message["id"]
    method = message.get("method")
    if method is None and ("result" in message or "error" in message):
        return None
    if _request_key(req_id) is None:
        return jsonrpc_error(None, -32600, "Invalid Request: id must be a string or an integer")
    if not isinstance(method, str) or not method:
        return jsonrpc_error(req_id, -32600, "Invalid Request: missing or non-string method")
    params = message.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return jsonrpc_error(req_id, -32602, "Invalid params: params must be an object")
    meta = params.get("_meta")
    declared = meta.get(META_PROTOCOL_VERSION) if isinstance(meta, dict) else None
    if declared is None and method != "server/discover":
        return _session_based(Request(req_id, method, params, _session_version, cancelled))
    version = STATELESS_PROTOCOL_VERSION if declared is None else declared
    if version != STATELESS_PROTOCOL_VERSION:
        # The list in data holds only what a request may name in _meta, because a client retries with a version
        # from it; the session-based revisions are offered by initialize and listed by server/discover.
        return jsonrpc_error(req_id, UNSUPPORTED_PROTOCOL_VERSION,
                             f"Unsupported protocol version in _meta: {version}. A request that names its revision "
                             f"in _meta is served under {STATELESS_PROTOCOL_VERSION} only. The session-based "
                             f"revisions ({SESSION_PROTOCOL_VERSIONS[-1]} to {SESSION_PROTOCOL_VERSIONS[0]}) start "
                             "with initialize, and their requests name no revision in _meta.",
                             {"supported": [STATELESS_PROTOCOL_VERSION], "requested": version})
    return _stateless(Request(req_id, method, params, version, cancelled))


def _session_based(request: Request) -> dict:
    """Serve a request under a session-based revision, which initialize chooses; results get no 2026-07-28 fields."""
    global _session_version
    if request.method == "initialize":
        requested = request.params.get("protocolVersion")
        _session_version = requested if requested in SESSION_PROTOCOL_VERSIONS else LATEST_SESSION_PROTOCOL_VERSION
        if requested and requested != _session_version:
            log(f"client requested unsupported protocol {requested}; offering {_session_version}")
        return jsonrpc_response(request.id, {
            "protocolVersion": _session_version,
            "capabilities": CAPABILITIES,
            "serverInfo": SERVER_INFO,
            "instructions": SERVER_INSTRUCTIONS,
        })
    if request.method == "ping":
        return jsonrpc_response(request.id, {})
    response = _serve_shared(request)
    if response is None:
        return jsonrpc_error(request.id, -32601, f"Method not found: {request.method}")
    return response


def _stateless(request: Request) -> dict:
    """Serve a request statelessly under revision 2026-07-28.

    Every result carries resultType "complete" and, in ``_meta``, the server's name and version.
    """
    if request.method == "server/discover":
        return jsonrpc_response(request.id, _stateless_result({
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": CAPABILITIES,
            "instructions": SERVER_INSTRUCTIONS,
        }, cacheable=True))
    response = _serve_shared(request)
    if response is None:
        # initialize and ping exist only in the session-based revisions; the stateless revision has neither.
        return jsonrpc_error(request.id, -32601, f"Method not found: {request.method}")
    if "result" in response:
        response["result"] = _stateless_result(response["result"], cacheable=request.method in CACHEABLE)
    return response


#: The methods whose results the 2026-07-28 schema makes cacheable, so they must carry ttlMs and cacheScope. The
#: lists and the guide's text are fixed for the life of the process.
CACHEABLE = ("tools/list", "resources/list", "resources/templates/list", "resources/read")


def _stateless_result(result: dict, cacheable: bool = False) -> dict:
    """Add the stateless revision's fields to a result: resultType, the server's info and, if cacheable, cache hints."""
    out = {"resultType": "complete", **result}
    if cacheable:
        out["ttlMs"] = LIST_TTL_MS
        out["cacheScope"] = "private"
    out["_meta"] = {**(result.get("_meta") or {}), META_SERVER_INFO: SERVER_INFO}
    return out


def _serve_shared(request: Request) -> dict | None:
    """Serve a method that the session-based and stateless revisions share, or return None for any other."""
    if request.method == "tools/list":
        return jsonrpc_response(request.id, {"tools": listed_tools()})
    if request.method == "tools/call":
        return _tools_call(request)
    if request.method == "resources/list":
        return jsonrpc_response(request.id, {"resources": RESOURCES})
    if request.method == "resources/templates/list":
        return jsonrpc_response(request.id, {"resourceTemplates": []})
    if request.method == "resources/read":
        uri = request.params.get("uri")
        if not isinstance(uri, str) or not uri:
            return jsonrpc_error(request.id, -32602, "Invalid params: resources/read needs the resource's uri")
        try:
            return jsonrpc_response(request.id, read_resource(uri))
        except ResourceNotFound as exc:
            return jsonrpc_error(request.id, RESOURCE_NOT_FOUND, str(exc), {"uri": uri})
    return None


def _tools_call(request: Request) -> dict:
    """Serve tools/call.

    A call the server cannot read (no tool name, an unknown tool, arguments that are not an object) is a
    JSON-RPC protocol error. Everything after that, a bad argument included, is the tool's own result with
    isError set, which tells the model what to correct.
    """
    name = request.params.get("name")
    if not isinstance(name, str) or not name:
        return jsonrpc_error(request.id, -32602, "Invalid params: tools/call needs the tool's name")
    if name not in BY_NAME:
        return jsonrpc_error(request.id, -32602, f"Unknown tool: {name}. tools/list names every tool this server has.")
    arguments = request.params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return jsonrpc_error(request.id, -32602, f"Invalid params: the arguments of {name} must be an object")
    sessions = manager()
    sessions.on_poll = Waiting(request, name, arguments, sessions)
    try:
        return jsonrpc_response(request.id, handle_tool_call(name, arguments, request))
    finally:
        sessions.on_poll = None


def serve_message(message: dict, cancelled: threading.Event | None = None) -> bytes | None:
    """Return the encoded response to one message, or None for a notification and for a cancelled request.

    The response is encoded inside the try, so even a failure to encode it becomes that request's error,
    never the end of the server.
    """
    try:
        response = respond(message, cancelled)
        data = None if response is None else encode(response)
    except Exception as exc:  # noqa: BLE001 -- one request's failure is its own error; the server stays up
        log(f"unhandled error in {message.get('method', '?')!r}: {exc!r}")
        if "id" not in message:
            return None
        req_id = message["id"] if _request_key(message["id"]) is not None else None
        data = encode(jsonrpc_error(req_id, -32603, f"Internal error: {type(exc).__name__}: {exc}"))
    if cancelled is not None and cancelled.is_set():
        return None  # A cancelled request gets no response, as the specification asks.
    return data


def serve_batch(messages: list, flags: list, inbox: Inbox) -> bytes | None:
    """Serve a JSON-RPC batch, accepted only when initialize negotiated 2025-03-26, the one revision that has them.

    Return the encoded array of responses, or None when no message in the batch gets one. The cancel flags
    of all its messages are released afterwards.
    """
    try:
        if _session_version != BATCH_VERSION:
            return encode(jsonrpc_error(None, -32600, f"Invalid Request: a batch (a JSON array) is served only under "
                                                      f"protocol {BATCH_VERSION}, and this session uses "
                                                      f"{_session_version}; send one message per line"))
        if not messages:
            return encode(jsonrpc_error(None, -32600, "Invalid Request: the batch is empty"))
        parts = []
        for message, flag in zip(messages, flags, strict=True):
            if not isinstance(message, dict):
                parts.append(encode(jsonrpc_error(None, -32600, "Invalid Request: a batch holds JSON objects")))
            elif message.get("method") == "notifications/cancelled" and "id" not in message:
                inbox.cancel(message.get("params"))
            elif flag is None or not flag.is_set():
                data = serve_message(message, flag)
                if data is not None:
                    parts.append(data)
        return b"[" + b", ".join(parts) + b"]" if parts else None
    finally:
        for message, flag in zip(messages, flags, strict=True):
            inbox.done(message, flag)


def serve_item(item: tuple, inbox: Inbox) -> bytes | None:
    """Serve one queued item (a parse failure, a message or a batch) and return the encoded response, if any."""
    message, flag = item
    if isinstance(message, ParseFailure):
        return encode(jsonrpc_error(None, message.code, message.message))
    if isinstance(message, list):
        return serve_batch(message, flag, inbox)
    try:
        if flag is not None and flag.is_set():
            return None  # Cancelled while it waited in the queue, so it is never run.
        return serve_message(message, flag)
    finally:
        inbox.done(message, flag)


def serve() -> int:
    """Run the MCP server over stdio until stdin closes, and return the exit code."""
    log(f"blenderlens-mcp {__version__} serving {len(TOOLS)} tools ({len(LOCAL)} of them handled by the server itself)")
    housekeeping()
    inbox = Inbox(sys.stdin.buffer, _wire)
    inbox.start()
    try:
        while True:
            item = inbox.get()
            if item is EOF:
                break
            try:
                data = serve_item(item, inbox)
            except Exception as exc:  # noqa: BLE001 -- nothing one message holds may end the server
                log(f"serving a message failed: {exc!r}")
                data = None
            if data is not None:
                _wire.write(data)
    finally:
        # stdin has closed. A background Blender with unsaved work first saves a copy to the recovery folder,
        # so the work outlives the client; then every session ends.
        if _manager is not None:
            _manager.shutdown()
    return 0
