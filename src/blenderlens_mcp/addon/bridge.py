"""Serve the MCP server's requests over a socket: newline-delimited JSON over TCP on localhost.

Each request is one line, and each response is one line::

    -> {"id": 7, "command": "get_object", "params": {"object": "Body"}, "token": "..."}
    <- {"id": 7, "ok": true, "result": {...}}
    <- {"id": 7, "ok": false, "error": "...", "kind": "not_found", "hint": "..."}

A connection proves itself with its first line, a ``hello`` carrying the token, and every request
after it carries the token too. Any other line closes the connection without a response: a line
that is not a JSON object, a line that fails to parse for any reason, a first request that is not
an authenticated ``hello``, or a wrong token. An HTTP request from a web page fails on its first
line and so runs nothing, and a client without the token learns nothing, not even that this is
BlenderLens. Connections are capped: at most ``MAX_UNPROVEN`` may wait to prove themselves, the
oldest closed to make room for a new one, and at most ``MAX_CLIENTS`` may be open in all, so idle
connections that never prove themselves cannot block the bridge.

``Bridge.poll`` serves whatever is waiting and returns. It never blocks for long and
uses no threads, because bpy may be touched only from Blender's main thread: in live
mode a timer calls it every few hundredths of a second, and in a background Blender
a loop calls it until told to stop. The command handlers are the same in both modes.
"""

import hmac
import ipaddress
import json
import math
import select
import socket
import sys
import time

from .commands import describe_failure, dispatch, log
from .common import jsonable

#: A line longer than this is refused rather than buffered without end.
MAX_LINE = 64 * 1024 * 1024
#: The longest first line accepted. A hello is a few hundred bytes, and a connection that has not proved
#: itself must not make Blender buffer more than this.
MAX_FIRST_LINE = 64 * 1024
#: A connection that has not proved itself within this many seconds is closed.
FIRST_LINE_SECONDS = 10.0
#: The most connections that may wait to prove themselves; a new one closes the oldest. A real client sends
#: its hello as soon as it connects, so its hello is read long before eight newer unproven connections arrive.
MAX_UNPROVEN = 8
#: The most connections open in all. On Windows, select accepts at most 512 sockets and raises on every
#: poll past that; this cap keeps the bridge far below it.
MAX_CLIENTS = 64


class _Client:
    """What the bridge keeps about one connection: the bytes waiting, whether it is proven, and since when."""

    __slots__ = ("buffer", "late", "proven", "since")

    def __init__(self):
        """Start an unproven connection with nothing waiting, timed from now."""
        self.buffer = bytearray()
        self.proven = False
        self.since = time.monotonic()
        # Set when the connection is past FIRST_LINE_SECONDS with data waiting: it is read once more, and
        # closed if it is still unproven after that.
        self.late = False


def _finite(value):
    """Return ``value`` as plain JSON types, with non-finite floats as the strings "nan", "inf" and "-inf".

    Strict JSON has no NaN or Infinity, and a strict reader refuses the whole response for one such value.
    Values that json cannot write go through ``jsonable`` first, and its result, which may itself hold such
    floats, is converted in turn.
    """
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return _finite(jsonable(value))


def is_loopback(host):
    """Return whether ``host`` names this machine only: localhost, or a loopback IPv4 address."""
    if host == "localhost":
        return True
    try:
        return ipaddress.IPv4Address(host).is_loopback
    except ValueError:
        return False


class Bridge:
    """The socket server that takes the MCP server's requests and runs each through the command dispatcher.

    ``mode`` is "live" for the add-on in a Blender the user works in, or "background" for a Blender started by
    the MCP server. Every connection must prove itself with ``token`` before anything it sends is run.
    """

    def __init__(self, mode, token):
        """Make a bridge for ``mode`` that serves only connections proven with ``token``; it listens once started."""
        if not token:
            # No bridge listens without a token, in live mode or in a background Blender.
            raise ValueError("The BlenderLens bridge needs a token.")
        self.mode = mode
        self.token = token
        self._token = token.encode("utf-8")
        self.listener = None
        self.clients = {}
        self.host = None
        self.port = None
        # Set by the shutdown command, for whatever drives the polling to act on. A background Blender's loop
        # ends when it is set. The add-on's timer in live mode does not read it, so the add-on keeps listening
        # until the user presses Stop.
        self.stop_requested = False
        self.served = 0
        self.last_command = None
        self.last_error = None

    @property
    def running(self):
        """Return whether the bridge is listening."""
        return self.listener is not None

    def start(self, host="127.0.0.1", port=0):
        """Listen on ``host`` and ``port``, port 0 for any free one; raise OSError for a host is_loopback refuses."""
        if not is_loopback(host):
            # The token crosses the socket in plain text, and the bridge runs Python, so only this machine may
            # connect, whatever the Host preference says.
            raise OSError(f"BlenderLens listens on this machine only; {host!r} is neither localhost nor a loopback "
                          "IPv4 address. Set the Host preference to 127.0.0.1.")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":
            # Without this, Windows lets a second process bind the same port and
            # silently splits the connections between the two.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, port))
            listener.listen(8)
        except OSError:
            listener.close()
            raise
        listener.setblocking(False)
        self.listener = listener
        self.host, self.port = listener.getsockname()[:2]

    def stop(self):
        """Close every connection, then stop listening."""
        for client in list(self.clients):
            self._drop(client)
        if self.listener is not None:
            try:
                self.listener.close()
            finally:
                self.listener = None

    def poll(self, timeout=0.0):
        """Serve what is waiting now, or what arrives within ``timeout`` seconds, and return the requests served."""
        if self.listener is None:
            return 0
        # Connections that ran out of time to prove themselves are closed first, so select does not watch them.
        self._reap()
        watched = [self.listener, *self.clients]
        try:
            readable, _, _ = select.select(watched, [], [], timeout)
        except (OSError, ValueError) as exc:
            # A watched socket was closed, or select could not take the list. Either way the unproven
            # connections and any closed socket are dropped, so select cannot fail the same way on every
            # later poll; the other proven clients stay.
            log(f"select failed with {len(watched)} sockets ({exc!r}); closing unproven connections")
            for client, state in list(self.clients.items()):
                if not state.proven or client.fileno() < 0:
                    self._drop(client)
            return 0
        served = 0
        # Open connections are read before new ones are accepted, so a new connection can push out a
        # waiting unproven one, but never one whose hello has already arrived.
        for sock in readable:
            if sock is not self.listener:
                served += self._read(sock)
        if self.listener is not None and self.listener in readable:
            self._accept()
        return served

    def _accept(self):
        """Accept the connections waiting, as many as MAX_UNPROVEN, closing any for which there is no room."""
        # Several are accepted per poll so a backlog drains quickly, but no more than MAX_UNPROVEN, so no
        # connection accepted in this poll is pushed out by another before the next poll reads its hello.
        for _ in range(MAX_UNPROVEN):
            try:
                client, _ = self.listener.accept()
            except OSError:  # including BlockingIOError, raised when nothing more is waiting
                return
            client.setblocking(False)
            if self._make_room():
                self.clients[client] = _Client()
            else:
                client.close()  # every slot is held by a connection that proved itself

    def _make_room(self):
        """Return whether one more connection fits, closing the oldest unproven one if that makes it fit."""
        unproven = [client for client, state in self.clients.items() if not state.proven]
        if len(unproven) < MAX_UNPROVEN and len(self.clients) < MAX_CLIENTS:
            return True
        if not unproven:
            return False
        self._drop(unproven[0])  # the dict keeps accept order, so the first is the oldest
        return True

    def _reap(self):
        """Close connections that have not proved themselves in time, since each holds a slot for nothing.

        A connection with data waiting is read once more, however long it waited, because a long command
        such as a render may have kept Blender from reading its hello. If that read does not prove it, it is
        closed. The time is checked only when Blender polls, so a connection may outlast FIRST_LINE_SECONDS
        while a command runs; MAX_UNPROVEN bounds how many such connections there can be.
        """
        now = time.monotonic()
        for client, state in list(self.clients.items()):
            if state.proven or now - state.since <= FIRST_LINE_SECONDS:
                continue
            if not state.late and self._has_data(client):
                state.late = True
                continue
            self._drop(client)

    @staticmethod
    def _has_data(client):
        """Return whether ``client`` has data waiting to be read, without reading it."""
        try:
            return bool(client.recv(1, socket.MSG_PEEK))
        except OSError:  # BlockingIOError means nothing is waiting; any other error means the connection is gone
            return False

    def _read(self, client):
        """Read what ``client`` sent, handle each complete line, and return how many requests were served."""
        try:
            data = client.recv(1 << 20)
        except (BlockingIOError, InterruptedError):
            return 0
        except OSError:
            self._drop(client)
            return 0
        if not data:
            self._drop(client)
            return 0
        state = self.clients.get(client)
        if state is None:
            return 0
        state.buffer.extend(data)
        served = 0
        while client in self.clients:
            buffer = state.buffer
            limit = MAX_LINE if state.proven else MAX_FIRST_LINE
            cut = buffer.find(b"\n")
            if cut < 0 and len(buffer) <= limit:
                break
            if cut < 0 or cut > limit:
                if state.proven:
                    self._send(client, {"id": None, "ok": False, "kind": "bad_argument",
                                        "error": f"The request is larger than {MAX_LINE // (1024 * 1024)} MiB."})
                self._drop(client)
                break
            line = bytes(buffer[:cut])
            del buffer[:cut + 1]
            try:
                self._handle(client, state, line)
            except BaseException as exc:  # noqa: BLE001 -- no single request may end the bridge
                log(f"a request failed outside its command: {exc!r}")
                self._drop(client)
                break
            served += 1
        return served

    def _authentic(self, request):
        """Return whether the request carries this bridge's token, compared in constant time."""
        offered = request.get("token")
        if not isinstance(offered, str):
            return False
        # surrogatepass, because JSON may carry a lone surrogate, which plain UTF-8 refuses to encode.
        return hmac.compare_digest(offered.encode("utf-8", "surrogatepass"), self._token)

    def _handle(self, client, state, line):
        """Run the request on one line and send its response, or close the connection without responding.

        A line that is not a JSON object carrying the token, or a first request other than ``hello``, closes
        the connection. Nothing a request does may end the bridge: every exception, BaseException included,
        either closes this connection or is sent back as a failure.
        """
        try:
            request = json.loads(line.decode("utf-8"))
        except Exception:  # noqa: BLE001 -- any parse failure closes the connection, including RecursionError
            request = None
        if not isinstance(request, dict) or not self._authentic(request):
            self._drop(client)
            return
        command = request.get("command")
        if not state.proven:
            if command != "hello":
                self._drop(client)
                return
            state.proven = True
        self.served += 1
        if command == "shutdown":
            # A background Blender exits once its loop sees the request. The add-on in live mode keeps listening,
            # since only the user stops it, and the reply says so.
            stopping = self.mode != "live"
            reply = {"ok": True, "result": {"stopping": stopping}}
            if not stopping:
                reply["result"]["note"] = ("The add-on keeps listening in live mode. Press Stop in its panel in "
                                           "Blender to stop it.")
            self.stop_requested = True
        else:
            started = time.time()
            self.last_command = command
            try:
                result = dispatch(command, request.get("params") or {}, self.mode)
                reply = {"ok": True, "result": result, "seconds": round(time.time() - started, 3)}
                self.last_error = None
            except BaseException as exc:  # noqa: BLE001 -- SystemExit and KeyboardInterrupt too: reported, not raised
                reply = {"ok": False, **self._describe(exc)}
                self.last_error = reply.get("error")
        reply["id"] = request.get("id")
        self._send(client, reply)

    @staticmethod
    def _describe(exc):
        """Return the failure response's fields for ``exc``, even when describing it fails."""
        try:
            return describe_failure(exc)
        except BaseException:  # noqa: BLE001 -- describing the failure failed, so report what is known
            return {"error": type(exc).__name__, "kind": "blender_error"}

    def _send(self, client, payload):
        """Send ``payload`` to ``client`` as one line of JSON, closing the connection if sending fails."""
        try:
            data = (json.dumps(_finite(payload), allow_nan=False) + "\n").encode("utf-8")
        except Exception as exc:  # noqa: BLE001 -- including RecursionError on a deeply nested result
            reply_id = payload.get("id")
            data = (json.dumps({"id": reply_id if isinstance(reply_id, (int, str)) else None, "ok": False,
                                "kind": "blender_error", "error": f"The reply could not be encoded as JSON: "
                                                                  f"{type(exc).__name__}: {exc}"[:2000]})
                    + "\n").encode("utf-8")
        try:
            client.setblocking(True)
            client.settimeout(30.0)
            client.sendall(data)
            client.setblocking(False)
        except OSError:
            self._drop(client)

    def _drop(self, client):
        """Forget ``client`` and close its connection."""
        self.clients.pop(client, None)
        try:
            client.close()
        except OSError:
            pass

