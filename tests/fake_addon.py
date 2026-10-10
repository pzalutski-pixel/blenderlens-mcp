"""A fake BlenderLens add-on, for tests that need a Blender that misbehaves.

The fake listens on localhost and speaks the bridge's wire format, one JSON line in and one JSON
line out, but the test decides what it returns: a ``handler`` maps each request to a response, or
to None for no response at all. No Blender is needed, so a test can imitate a Blender window running
the add-on that is busy, slow or silent, and run in a second.

    with FakeAddon(silent) as fake:          # accepts connections, never responds
        ...BLENDERLENS_PORT=fake.port...

As the real bridge does, the fake closes a connection without a response when a line is not a JSON
object. With a ``token``, it also applies the real bridge's token rule to every connection: the first
line must be a ``hello`` request carrying the token, and every request after it must carry the token
too, or the connection is closed without a response. Without a token, it serves any JSON object.
``write_connection`` writes the ``live-<port>.json`` connection file that a server reads the token
from, so the server attaches to the fake exactly as it attaches to the real add-on.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
import zlib
from collections.abc import Callable

from blenderlens_mcp.session import PROTOCOL

#: The result the fake returns for a ``hello`` request, unless the handler returns something else.
HELLO = {"protocol": PROTOCOL, "addon_version": "0.0.0-fake", "blender_version": "5.0.1", "mode": "live",
         "background": False, "file": None, "unsaved_changes": False, "render_engines": ["workbench"],
         "commands": [], "pid": 0}


def silent(request: dict) -> None:
    """Read each request and never respond, like a Blender window whose main thread is busy elsewhere."""
    return None


def hello_then_silent(request: dict) -> dict | None:
    """Respond to ``hello`` at once and never to anything else.

    This imitates a Blender window that attaches and then gets stuck in a command.
    """
    if request.get("command") == "hello":
        return {"ok": True, "result": dict(HELLO)}
    return None


def png_bytes(rgb: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """Return a 1x1 PNG of one colour.

    Two different colours give two files that a test can tell apart by their bytes.
    """
    def chunk(kind: bytes, data: bytes) -> bytes:
        """Return one PNG chunk: its length, its kind, its data and its CRC."""
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb)))
            + chunk(b"IEND", b""))


def commands(**handlers: Callable[[dict], dict]) -> Callable[[dict], dict]:
    """Return a handler that responds to ``hello`` and to the named commands.

    Each command's function receives the request's params and returns the ``result``, and the fake
    wraps it as ``{"ok": true, "result": ...}``. Any other command is refused, as the add-on refuses a
    command it does not have.
    """
    def handle(request: dict) -> dict:
        """Return the response to ``request``."""
        command = request.get("command")
        if command == "hello":
            return {"ok": True, "result": dict(HELLO)}
        if command in handlers:
            return {"ok": True, "result": handlers[command](request.get("params") or {})}
        return {"ok": False, "kind": "bad_argument", "error": f"The fake add-on has no command {command}."}
    return handle


def rendering(seconds: float = 0.0, reported_path: str | None = None, rgb=(0, 128, 255)) -> Callable[[dict], dict]:
    """Return a ``render_preview`` command function that behaves as Blender does.

    The function stays busy for ``seconds``, then writes a PNG at the path in the request. With
    ``reported_path``, the result names a different path from the one written, as a Blender that cannot
    be trusted might, so a test can check that the server uses the path it asked for.
    """
    def render(params: dict) -> dict:
        """Wait, write the PNG at the requested path, and return the result."""
        time.sleep(seconds)
        path = params["path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(png_bytes(rgb))
        return {"path": reported_path or path, "engine": params.get("engine") or "cycles", "seconds": seconds,
                "resolution": [1, 1], "objects": [], "view": {"name": params.get("view") or "three_quarter"}}
    return render


class FakeAddon:
    """A socket server on 127.0.0.1 that stands in for the add-on, serving each connection on a thread of its own.

    ``requests`` holds every request the fake accepted, in order. ``connections`` holds every connection,
    ``closed_by_peer`` those the server closed, and ``refused`` those the fake closed itself.
    """

    def __init__(self, handler: Callable[[dict], dict | None] = silent, token: str | None = None):
        """Listen on a free port and respond to each accepted request through ``handler``.

        With ``token``, every connection must prove itself with it, as on the real bridge.
        """
        self.handler = handler
        self.token = token
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.requests: list[dict] = []
        self.connections: list[socket.socket] = []
        self.closed_by_peer: list[socket.socket] = []
        #: Connections the fake closed because a line was not a JSON object or did not carry the right token.
        self.refused: list[socket.socket] = []
        self._stopping = False
        threading.Thread(target=self._accept, daemon=True).start()

    def __enter__(self) -> FakeAddon:
        """Return the fake, for use in a ``with`` statement."""
        return self

    def __exit__(self, *exc) -> None:
        """Close the fake at the end of the ``with`` statement."""
        self.close()

    def write_connection(self, folder) -> str:
        """Write the connection file into ``folder``, and return its path.

        A server run with ``BLENDERLENS_RUNTIME_DIR`` set to ``folder`` reads the fake's port and token
        from it.
        """
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"live-{self.port}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"host": "127.0.0.1", "port": self.port, "token": self.token, "pid": os.getpid(),
                       "blender_version": HELLO["blender_version"], "addon_version": HELLO["addon_version"]}, handle)
        return path

    def _accept(self) -> None:
        """Accept connections until the fake closes, and serve each on a thread of its own."""
        while not self._stopping:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            self.connections.append(client)
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _proves_itself(self, request: dict, first: bool) -> bool:
        """Return whether ``request`` passes the token rule; a first request must also be a ``hello``."""
        if self.token is None:
            return True
        if request.get("token") != self.token:
            return False
        return not first or request.get("command") == "hello"

    def _serve(self, client: socket.socket) -> None:
        """Read the requests on one connection and send each response, until either side closes it."""
        buffer = b""
        first = True
        while not self._stopping:
            try:
                chunk = client.recv(65536)
            except OSError:
                return
            if not chunk:
                self.closed_by_peer.append(client)
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    request = json.loads(line)
                except ValueError:
                    request = None
                if not isinstance(request, dict) or not self._proves_itself(request, first):
                    # The real bridge closes such a connection without sending anything, so the fake does too.
                    self.refused.append(client)
                    try:
                        client.close()
                    except OSError:
                        pass
                    return
                first = False
                self.requests.append(request)
                reply = self.handler(request)
                if reply is None:
                    continue
                reply = {**reply, "id": request.get("id")}
                try:
                    client.sendall((json.dumps(reply) + "\n").encode("utf-8"))
                except OSError:
                    return

    def commands(self) -> list[str]:
        """Return the command of every request the fake accepted, in order."""
        return [request.get("command") for request in self.requests]

    def wait_for(self, command: str, seconds: float = 5.0) -> bool:
        """Return True once a request for ``command`` has arrived, or False after ``seconds``."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if command in self.commands():
                return True
            time.sleep(0.02)
        return False

    def wait_for_peer_close(self, seconds: float = 5.0) -> bool:
        """Return True once the server has closed every connection it opened, or False after ``seconds``."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.connections and len(self.closed_by_peer) == len(self.connections):
                return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        """Stop listening, and close every connection."""
        self._stopping = True
        try:
            self.listener.close()
        except OSError:
            pass
        for client in self.connections:
            try:
                client.close()
            except OSError:
                pass
