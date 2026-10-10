"""Tests for the safeguards of the bridge, the socket server in the add-on that the MCP server talks to.

- A connection must prove itself on its first line, a ``hello`` request carrying the token, or the
  bridge closes it without a response. The bridge also closes a connection that sends any line that
  is not a JSON object, or any request with the wrong token.
- Tokens are compared in constant time.
- Only a few connections may wait to prove themselves, and the total number of connections is capped.
- Nothing a request raises, ``SystemExit`` and ``KeyboardInterrupt`` included, stops the bridge.
- Responses are strict JSON: non-finite numbers are sent as the strings "nan", "inf" and "-inf".
- The bridge listens on this machine only: localhost or a loopback IPv4 address (``::1`` is refused).
- The reply to ``shutdown`` says whether Blender stops: a background Blender does, live mode does not.

The first part of the module loads ``bridge.py`` itself into this process, with the two add-on modules
it imports replaced by stubs, because the real ones need ``bpy``. It calls ``poll`` by hand, as
Blender's timer would. The second part talks to a real background Blender, which runs the same bridge.

A connection is proven once its first line, an authenticated ``hello``, has been read, and unproven
until then.
"""

import hmac
import importlib.util
import itertools
import json
import os
import socket
import subprocess
import sys
import time
import types

import pytest

from blenderlens_mcp.session import BACKGROUND_SCRIPT, PROTOCOL
from tests.conftest import SRC

ADDON = SRC / "blenderlens_mcp" / "addon"
TOKEN = "s3cret-token"
_names = itertools.count()


# ---------------------------------------------------------------------------
# bridge.py in this process
# ---------------------------------------------------------------------------


def _strict(text):
    """Parse ``text`` as a strict JSON reader does, raising ValueError on NaN and Infinity, which are not JSON."""
    def refuse(constant):
        """Raise ValueError for ``constant``, which strict JSON does not have."""
        raise ValueError(f"{constant} is not JSON")
    return json.loads(text, parse_constant=refuse)


def _stub_jsonable(value):
    """Convert ``value`` as ``common.jsonable`` does for the values these tests use.

    The real ``common.jsonable`` needs ``bpy``, which this process does not have.
    """
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return repr(value)


@pytest.fixture
def bridge_module():
    """Load ``bridge.py`` under a throwaway package name, with stub ``commands`` and ``common`` modules.

    ``module.calls`` records every command that reached dispatch. ``module.handlers`` maps command names
    to the functions that produce their results, and a command it does not hold returns its own name.
    """
    package = f"bltest_addon_{next(_names)}"
    parent = types.ModuleType(package)
    parent.__path__ = [str(ADDON)]
    commands = types.ModuleType(f"{package}.commands")
    common = types.ModuleType(f"{package}.common")
    calls = []

    def hello(params, mode):
        """Return the stub's result for ``hello``."""
        return {"protocol": PROTOCOL, "addon_version": "test", "blender_version": "test", "mode": mode,
                "pid": os.getpid()}

    handlers = {"hello": hello}

    def dispatch(command, params, mode):
        """Record the command and return what its handler returns."""
        calls.append(command)
        return handlers.get(command, lambda params, mode: {"command": command})(params, mode)

    def describe_failure(exc):
        """Return the failure fields for ``exc``, as the real ``describe_failure`` does in outline."""
        return {"error": f"{type(exc).__name__}: {exc}", "kind": "blender_error"}

    commands.dispatch = dispatch
    commands.describe_failure = describe_failure
    commands.log = lambda message: None
    common.jsonable = _stub_jsonable
    added = {package: parent, f"{package}.commands": commands, f"{package}.common": common}
    sys.modules.update(added)
    spec = importlib.util.spec_from_file_location(f"{package}.bridge", ADDON / "bridge.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    added[spec.name] = module
    try:
        spec.loader.exec_module(module)
        module.calls = calls
        module.handlers = handlers
        yield module
    finally:
        for name in added:
            sys.modules.pop(name, None)


class Client:
    """A test client connected to a bridge running in this process.

    Reading from the connection calls the bridge's ``poll``, as Blender's timer would, so the bridge
    serves the request while the client waits.
    """

    def __init__(self, bridge):
        """Connect to ``bridge`` on its port."""
        self.bridge = bridge
        self.sock = socket.create_connection(("127.0.0.1", bridge.port), timeout=5)
        self.sock.settimeout(0.02)
        self.buffer = b""

    def send(self, payload):
        """Send ``payload`` and a newline; a dict is sent as JSON, a str as UTF-8, bytes as they are."""
        if isinstance(payload, dict):
            payload = json.dumps(payload)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        try:
            self.sock.sendall(payload + b"\n")
        except OSError:
            pass  # The bridge has already closed the connection, and the next read reports that.

    def pump(self):
        """Let the bridge serve what is waiting, failing the test if anything escapes its ``poll``."""
        try:
            self.bridge.poll(0.01)
        except BaseException as exc:  # noqa: BLE001 -- a KeyboardInterrupt here must fail the test, not end pytest
            pytest.fail(f"{exc!r} escaped the bridge's poll")

    def line(self, seconds=5.0):
        """Return the next raw line the bridge sent, or None when it closed the connection without one."""
        deadline = time.monotonic() + seconds
        while b"\n" not in self.buffer:
            if time.monotonic() > deadline:
                pytest.fail("the bridge neither replied nor closed the connection")
            self.pump()
            try:
                chunk = self.sock.recv(1 << 16)
            except TimeoutError:
                continue
            except (ConnectionResetError, ConnectionAbortedError):
                return None
            if not chunk:
                return None
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line

    def reply(self, seconds=5.0):
        """Return the next line the bridge sent, parsed as strict JSON, or None when it closed the connection."""
        line = self.line(seconds)
        return None if line is None else _strict(line.decode("utf-8"))

    def ask(self, payload):
        """Send ``payload`` and return the bridge's reply, or None when it closed the connection."""
        self.send(payload)
        return self.reply()

    def close(self):
        """Close the connection."""
        self.sock.close()


@pytest.fixture
def served(bridge_module):
    """Start a bridge in live mode, and provide the module, the bridge and a function that opens a ``Client``."""
    bridge = bridge_module.Bridge(mode="live", token=TOKEN)
    bridge.start("127.0.0.1", 0)
    clients = []

    def connect():
        """Open a client connection to the bridge, closed when the test ends."""
        client = Client(bridge)
        clients.append(client)
        return client

    yield bridge_module, bridge, connect
    for client in clients:
        client.close()
    bridge.stop()


def proven(connect):
    """Open a connection, prove it with an authenticated ``hello``, and return it."""
    client = connect()
    hello = client.ask({"id": 0, "command": "hello", "token": TOKEN})
    assert hello is not None and hello["ok"] is True, hello
    return client


def test_a_hello_with_the_token_gets_a_reply_and_then_requests_are_served(served):
    module, _, connect = served
    client = proven(connect)
    reply = client.ask({"id": 1, "command": "get_scene", "token": TOKEN})
    assert reply == {"id": 1, "ok": True, "result": {"command": "get_scene"}, "seconds": reply["seconds"]}
    assert module.calls == ["hello", "get_scene"]


@pytest.mark.parametrize("first", [
    {"id": 1, "command": "get_scene", "token": TOKEN},
    {"id": 1, "command": "run_python", "params": {"code": "result = 1"}, "token": TOKEN},
    {"id": 1, "command": "shutdown", "token": TOKEN},
    {"id": 1, "command": "hello"},
    {"id": 1, "command": "hello", "token": "wrong"},
    {"id": 1, "command": "hello", "token": TOKEN + "x"},
    {"id": 1, "command": "hello", "token": 12345},
    {"id": 1, "command": "hello", "token": "tökén"},
    {"id": 1, "command": "hello", "token": None},
], ids=["get_scene", "run_python", "shutdown", "no-token", "wrong-token", "longer-token", "number-token",
        "non-ascii-token", "null-token"])
def test_a_first_request_that_is_not_an_authenticated_hello_is_closed_without_a_reply(served, first):
    module, bridge, connect = served
    client = connect()
    client.send(first)
    assert client.line() is None, "the bridge replied instead of closing the connection"
    assert module.calls == [], f"{module.calls} reached Blender"
    assert bridge.stop_requested is False


NOT_OBJECTS = {
    "http-request-line": b"POST / HTTP/1.1\r",
    "garbage": b"this is not json",
    "array": b'[{"id": 1, "command": "hello", "token": "' + TOKEN.encode() + b'"}]',
    "string": b'"hello"',
    "number": b"42",
    "null": b"null",
    "blank": b"",
    "bad-utf8": b'{"id": 1, "command": "hello", "token": "\xff\xfe"}',
    "deep-array": b"[" * 5000,
    "deep-object": b'{"a":' * 5000,
}


@pytest.mark.parametrize("line", NOT_OBJECTS.values(), ids=NOT_OBJECTS.keys())
def test_a_first_line_that_is_not_a_json_object_is_closed_without_a_reply(served, line):
    module, _, connect = served
    client = connect()
    client.send(line)
    assert client.line() is None, "the bridge replied instead of closing the connection"
    assert module.calls == []


@pytest.mark.parametrize("line", NOT_OBJECTS.values(), ids=NOT_OBJECTS.keys())
def test_a_later_line_that_is_not_a_json_object_closes_the_connection_too(served, line):
    module, _, connect = served
    client = proven(connect)
    client.send(line)
    assert client.line() is None, "the bridge replied instead of closing the connection"
    assert module.calls == ["hello"]


@pytest.mark.parametrize("request_", [
    {"id": 2, "command": "get_scene"},
    {"id": 2, "command": "get_scene", "token": "wrong"},
    {"id": 2, "command": "hello", "token": "wrong"},
], ids=["no-token", "wrong-token", "hello-wrong-token"])
def test_a_later_request_with_the_wrong_token_closes_the_connection(served, request_):
    module, _, connect = served
    client = proven(connect)
    client.send(request_)
    assert client.line() is None
    assert module.calls == ["hello"]


def test_an_http_post_carrying_a_request_runs_nothing(served):
    """An HTTP POST to the bridge's port with a JSON request line in its body runs nothing.

    This is the cross-protocol attack, in which a web page in a browser posts to the local port.
    """
    module, bridge, connect = served
    body = '\n{"id":99,"command":"run_python","params":{"code":"result = 6*7"}}\n'
    http = (f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{bridge.port}\r\nContent-Type: text/plain\r\n"
            f"Origin: https://evil.example\r\nContent-Length: {len(body)}\r\n\r\n{body}")
    client = connect()
    client.sock.sendall(http.encode())
    assert client.line() is None
    assert module.calls == []


def test_closing_one_connection_leaves_the_others_served(served):
    _, _, connect = served
    good = proven(connect)
    bad = connect()
    bad.send(b"[" * 5000)
    assert bad.line() is None
    assert good.ask({"id": 3, "command": "get_scene", "token": TOKEN})["ok"] is True


def test_a_connection_that_does_not_prove_itself_in_time_is_closed(served):
    module, _, connect = served
    module.FIRST_LINE_SECONDS = 0.3
    client = connect()
    assert client.line(seconds=5) is None
    assert module.calls == []


def test_an_endless_first_line_is_closed_without_buffering_it(served):
    module, _, connect = served
    client = connect()
    data = b'{"id": 1, "command": "hello", "token": "' + b"x" * (module.MAX_FIRST_LINE + 1024)
    for start in range(0, len(data), 8192):  # Send in pieces, polling between them, so no buffer fills up.
        try:
            client.sock.sendall(data[start:start + 8192])
        except OSError:
            break  # The bridge has already closed the connection.
        client.pump()
    assert client.line() is None
    assert module.calls == []


def _closed_by_bridge(sock):
    """Return whether the other end has closed ``sock``, without waiting."""
    sock.setblocking(False)
    try:
        return sock.recv(1) == b""
    except BlockingIOError:
        return False
    except (ConnectionResetError, ConnectionAbortedError):
        return True


def _closed_soon(sock, seconds=2.0):
    """Return whether the other end closes ``sock`` within ``seconds``; a close can take a moment to arrive."""
    deadline = time.monotonic() + seconds
    while not _closed_by_bridge(sock):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def _pump_until(bridge, condition, seconds=5.0, what="the bridge to settle"):
    """Poll the bridge until ``condition()`` holds, failing the test after ``seconds``."""
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        bridge.poll(0.01)


def _idle_connections(bridge, count):
    """Open ``count`` connections that never send a byte, and return them.

    The bridge is polled after each one, so it accepts them one at a time, because the listen backlog is
    short.
    """
    opened = []
    for _ in range(count):
        opened.append(socket.create_connection(("127.0.0.1", bridge.port), timeout=5))
        bridge.poll(0.01)
    return opened


def _unproven(bridge):
    """Return the bridge's state for each connection that has not proved itself yet."""
    return [state for state in bridge.clients.values() if not state.proven]


def test_at_most_eight_connections_wait_to_prove_themselves_and_the_oldest_goes_first(served):
    """At most eight connections may wait to prove themselves, and the oldest is closed first when more arrive.

    The cap keeps a client without the token from opening connections until ``select`` can watch no more.
    """
    module, bridge, connect = served
    idle = _idle_connections(bridge, 20)
    try:
        _pump_until(bridge, lambda: sum(map(_closed_by_bridge, idle)) == 12,
                    what="the bridge to close the 12 oldest of 20 idle connections")
        assert len(_unproven(bridge)) == module.MAX_UNPROVEN == 8
        assert [_closed_by_bridge(s) for s in idle] == [True] * 12 + [False] * 8, \
            "the bridge did not close the oldest connections first"
        client = proven(connect)  # A new connection still gets in and proves itself.
        assert client.ask({"id": 1, "command": "get_scene", "token": TOKEN})["ok"] is True
        assert len(_unproven(bridge)) <= 8
    finally:
        for sock in idle:
            sock.close()


def test_connections_are_capped_in_all_and_unproven_ones_give_way_first(served):
    module, bridge, connect = served
    module.MAX_CLIENTS = 3
    first, second = proven(connect), proven(connect)
    unproven = _idle_connections(bridge, 1)[0]
    try:
        _pump_until(bridge, lambda: len(bridge.clients) == 3, what="the unproven connection to be accepted")
        third = proven(connect)  # The unproven connection is closed to make room for this one.
        _pump_until(bridge, lambda: _closed_by_bridge(unproven), what="the unproven connection to be closed")
        late = connect()
        late.send({"id": 1, "command": "hello", "token": TOKEN})
        assert late.line() is None, "a fourth connection got a reply, past the cap of three"
        for client in (first, second, third):
            assert client.ask({"id": 2, "command": "get_scene", "token": TOKEN})["ok"] is True
    finally:
        unproven.close()


def test_an_unproven_connection_out_of_time_is_closed_before_select_sees_it(served, monkeypatch):
    """An unproven connection whose time to prove itself has run out is closed before ``select`` is called.

    Connections out of time are removed before the list of sockets to watch is built, so a flood of them can
    never overflow ``select``.
    """
    module, bridge, _ = served
    module.FIRST_LINE_SECONDS = 0.2
    unproven = _idle_connections(bridge, 1)[0]
    try:
        _pump_until(bridge, lambda: len(bridge.clients) == 1, what="the unproven connection to be accepted")
        time.sleep(0.3)
        watched = []
        real = module.select.select

        def spy(readable, writable, failed, timeout):
            """Record the sockets ``select`` is asked to watch, then call the real one."""
            watched.append(list(readable))
            return real(readable, writable, failed, timeout)

        monkeypatch.setattr(module.select, "select", spy)
        bridge.poll(0.01)
        assert watched == [[bridge.listener]], "the connection out of time was still watched"
        assert _closed_soon(unproven)
    finally:
        unproven.close()


def test_a_hello_that_waited_through_a_long_command_is_still_read(served):
    """A ``hello`` sent in time but read late, because Blender was busy, still proves the connection.

    Blender may be busy past the time allowed for the first line, during a render for example, and that
    must not cost a real client its connection.
    """
    module, bridge, connect = served
    module.FIRST_LINE_SECONDS = 0.2
    client = connect()
    _pump_until(bridge, lambda: len(bridge.clients) == 1, what="the client to be accepted")
    client.send({"id": 0, "command": "hello", "token": TOKEN})
    time.sleep(0.4)  # The bridge is not polled during this wait, as when Blender is busy.
    hello = client.reply()
    assert hello is not None and hello["ok"] is True, "the hello that waited was not read"


def test_when_select_fails_the_unproven_connections_go_and_the_proven_one_stays(served, monkeypatch):
    """When ``select`` fails, the bridge closes the unproven connections and keeps the proven one.

    If ``select`` fails despite the caps, the bridge must not fail the same way on every poll from then on.
    """
    module, bridge, connect = served
    client = proven(connect)
    idle = _idle_connections(bridge, 3)
    try:
        _pump_until(bridge, lambda: len(bridge.clients) == 4, what="the unproven connections to be accepted")
        real = module.select.select

        def fail_once(*args):
            """Raise as ``select`` does past its limit, once, and put the real one back."""
            monkeypatch.setattr(module.select, "select", real)
            raise ValueError("too many file descriptors in select()")

        monkeypatch.setattr(module.select, "select", fail_once)
        bridge.poll(0.01)
        assert len(bridge.clients) == 1, "unproven connections are still open"
        assert all(_closed_soon(s) for s in idle)
        assert client.ask({"id": 1, "command": "get_scene", "token": TOKEN})["ok"] is True
    finally:
        for sock in idle:
            sock.close()


def test_tokens_are_compared_with_compare_digest(served, monkeypatch):
    _, _, connect = served
    seen = []
    real = hmac.compare_digest

    def spy(a, b):
        """Record the two values compared, then compare them with the real function."""
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    client = proven(connect)
    client.ask({"id": 1, "command": "get_scene", "token": TOKEN})
    assert len(seen) >= 2, "a request's token did not go through hmac.compare_digest"
    assert all(TOKEN.encode() in pair or TOKEN in pair for pair in seen)


def test_a_bridge_cannot_start_without_a_token(bridge_module):
    for token in (None, ""):
        with pytest.raises(ValueError):
            bridge_module.Bridge(mode="live", token=token)


@pytest.mark.parametrize("raised", [SystemExit(3), KeyboardInterrupt(), GeneratorExit()],
                         ids=["SystemExit", "KeyboardInterrupt", "GeneratorExit"])
def test_a_base_exception_from_a_command_gets_a_failure_reply_and_serving_goes_on(served, raised):
    module, _, connect = served

    def explode(params, mode):
        """Raise the exception under test."""
        raise raised

    module.handlers["explode"] = explode
    client = proven(connect)
    reply = client.ask({"id": 4, "command": "explode", "token": TOKEN})
    assert reply is not None, "the connection was dropped"
    assert reply["ok"] is False and reply["kind"] == "blender_error"
    assert type(raised).__name__ in reply["error"]
    assert client.ask({"id": 5, "command": "get_scene", "token": TOKEN})["ok"] is True


def test_a_base_exception_outside_the_command_closes_only_that_connection(served):
    """A KeyboardInterrupt raised while sending a response, after the command ran, closes only that connection.

    The bridge itself keeps running and serves new connections.
    """
    _, bridge, connect = served
    first = proven(connect)
    real = bridge._send

    def broken(client, payload):
        """Raise KeyboardInterrupt once, in place of sending the response, and put the real send back."""
        bridge._send = real
        raise KeyboardInterrupt

    bridge._send = broken
    first.send({"id": 1, "command": "get_scene", "token": TOKEN})
    assert first.line() is None
    assert proven(connect).ask({"id": 2, "command": "get_scene", "token": TOKEN})["ok"] is True


def test_a_token_that_utf8_cannot_encode_is_closed_without_a_reply(served):
    module, _, connect = served
    client = connect()
    client.send('{"id": 1, "command": "hello", "token": "\\ud800"}')
    assert client.line() is None
    assert module.calls == []


def test_replies_are_strict_json_with_non_finite_numbers_as_strings(served):
    module, _, connect = served
    nan, inf = float("nan"), float("inf")
    module.handlers["numbers"] = lambda params, mode: {
        "nan": nan, "inf": inf, "minus": -inf, "list": [1.5, nan], "nested": {"deep": [{"x": -inf}]},
        "converted": {inf},  # A set has no JSON form, so jsonable turns it into a list, which still holds inf.
    }
    client = proven(connect)
    client.send({"id": 6, "command": "numbers", "token": TOKEN})
    line = client.line()
    assert line is not None
    reply = _strict(line.decode("utf-8"))  # Raises ValueError on NaN or Infinity.
    assert reply["result"] == {"nan": "nan", "inf": "inf", "minus": "-inf", "list": [1.5, "nan"],
                                "nested": {"deep": [{"x": "-inf"}]}, "converted": ["inf"]}


def test_a_result_that_cannot_be_encoded_is_a_refusal_not_a_dropped_connection(served):
    module, _, connect = served

    def deep(params, mode):
        """Return a list nested 100,000 deep, which json cannot encode."""
        value = []
        for _ in range(100_000):
            value = [value]
        return value

    module.handlers["deep"] = deep
    client = proven(connect)
    reply = client.ask({"id": 7, "command": "deep", "token": TOKEN})
    assert reply["ok"] is False and reply["id"] == 7
    assert client.ask({"id": 8, "command": "get_scene", "token": TOKEN})["ok"] is True


@pytest.mark.parametrize(("mode", "stopping"), [("live", False), ("background", True)])
def test_shutdown_says_whether_blender_stops(bridge_module, mode, stopping):
    """A ``shutdown`` request sets ``stop_requested``, and the reply says whether Blender stops.

    A background Blender's loop ends once it sees the request. The add-on in live mode keeps listening until the
    user presses Stop, and the reply carries a note saying so.
    """
    bridge = bridge_module.Bridge(mode=mode, token=TOKEN)
    bridge.start("127.0.0.1", 0)
    client = Client(bridge)
    try:
        assert client.ask({"id": 0, "command": "hello", "token": TOKEN})["ok"] is True
        reply = client.ask({"id": 1, "command": "shutdown", "token": TOKEN})
        assert reply["ok"] is True and reply["result"]["stopping"] is stopping, reply
        assert ("note" in reply["result"]) is not stopping, reply
        assert bridge.stop_requested is True
        assert bridge_module.calls == ["hello"], "shutdown reached the command dispatcher"
    finally:
        client.close()
        bridge.stop()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "example.com", "", "::1"])
def test_the_bridge_listens_on_this_machine_only(bridge_module, host):
    """A host other than localhost or a loopback IPv4 address is refused before the bridge binds to it, ``::1`` too.

    The token crosses the socket in plain text, so the bridge must not be reachable from another machine.
    """
    with pytest.raises(OSError, match="loopback"):
        bridge_module.Bridge(mode="live", token=TOKEN).start(host, 0)


@pytest.mark.parametrize("host", [
    "127.0.0.1",
    pytest.param("127.0.0.2", marks=pytest.mark.skipif(
        sys.platform == "darwin", reason="macOS configures only 127.0.0.1 on its loopback interface")),
    "localhost",
])
def test_loopback_hosts_are_accepted(bridge_module, host):
    bridge = bridge_module.Bridge(mode="live", token=TOKEN)
    bridge.start(host, 0)
    try:
        assert bridge.running
    finally:
        bridge.stop()


# ---------------------------------------------------------------------------
# A real background Blender
# ---------------------------------------------------------------------------


class Raw:
    """A plain socket connection to the bridge in a background Blender."""

    def __init__(self, port):
        """Connect to the bridge on ``port``."""
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.buffer = b""

    def send(self, payload):
        """Send ``payload`` and a newline; bytes are sent as they are, anything else as JSON."""
        line = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        try:
            self.sock.sendall(line + b"\n")
        except OSError:
            pass

    def reply(self, seconds=30.0):
        """Return the next line the bridge sent, parsed, or None when it closed the connection without one."""
        self.sock.settimeout(seconds)
        while b"\n" not in self.buffer:
            try:
                chunk = self.sock.recv(1 << 16)
            except (ConnectionResetError, ConnectionAbortedError):
                return None
            except TimeoutError:
                pytest.fail("the bridge neither replied nor closed the connection")
            if not chunk:
                return None
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def ask(self, payload):
        """Send ``payload`` and return the bridge's reply, failing the test if it closed the connection instead."""
        self.send(payload)
        reply = self.reply()
        assert reply is not None, f"the background Blender closed the connection instead of replying to {payload}"
        return reply

    def drain(self, seconds=10.0):
        """Return every line the bridge sends within ``seconds``, parsed, and whether it closed the connection."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                chunk = self.sock.recv(1 << 16)
            except (ConnectionResetError, ConnectionAbortedError):
                closed = True
                break
            except TimeoutError:
                closed = False
                break
            if not chunk:
                closed = True
                break
            self.buffer += chunk
        else:
            closed = False
        lines = [json.loads(line) for line in self.buffer.split(b"\n") if line.strip()]
        return lines, closed


def start_background(blender_binary, tmp_path, token):
    """Start ``background.py`` in Blender as the server starts it, and wait for its ready file.

    When ``token`` is None, no session token is passed to Blender. Return the process, the open log file,
    and the ready file's contents.
    """
    ready = tmp_path / "ready.json"
    env = {k: v for k, v in os.environ.items() if k != "BLENDERLENS_SESSION_TOKEN"}
    if token:
        env["BLENDERLENS_SESSION_TOKEN"] = token
    log = open(tmp_path / "blender.log", "wb")
    process = subprocess.Popen([blender_binary, "--background", "--factory-startup", "--python",
                                str(BACKGROUND_SCRIPT), "--", "--port", "0", "--ready-file", str(ready), "--empty"],
                               stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, env=env)
    deadline = time.time() + 90
    while not ready.exists() and time.time() < deadline and process.poll() is None:
        time.sleep(0.1)
    assert ready.exists(), (tmp_path / "blender.log").read_text(errors="replace")[-2000:]
    return process, log, json.loads(ready.read_text())


@pytest.fixture
def background(blender_binary, tmp_path):
    """Start a background Blender whose bridge requires ``TOKEN``, and provide its process and port."""
    process, log, ready = start_background(blender_binary, tmp_path, TOKEN)
    try:
        yield process, ready["port"]
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        log.close()


def proven_raw(port):
    """Open a connection to the background Blender on ``port``, prove it with ``hello``, and return it."""
    raw = Raw(port)
    hello = raw.ask({"id": 0, "command": "hello", "token": TOKEN})
    assert hello["ok"] is True, hello
    return raw


@pytest.mark.integration
def test_an_http_post_to_a_background_blender_without_a_session_token_runs_nothing(blender_binary, tmp_path):
    """An HTTP POST to a background Blender started without a session token runs nothing.

    Without a token from the server, ``background.py`` makes one of its own, so its bridge still requires one.
    """
    marker = tmp_path / "ran.txt"
    process, log, ready = start_background(blender_binary, tmp_path, None)
    try:
        code = f"open({str(marker)!r}, 'w').write('ran')"
        body = "\n" + json.dumps({"id": 99, "command": "run_python", "params": {"code": code}}) + "\n"
        http = (f"POST / HTTP/1.1\r\nHost: 127.0.0.1:{ready['port']}\r\nContent-Type: text/plain\r\n"
                f"Origin: https://evil.example\r\nContent-Length: {len(body)}\r\n\r\n{body}")
        raw = Raw(ready["port"])
        raw.sock.sendall(http.encode())
        replies, closed = raw.drain(10)
        assert replies == [], f"the bridge replied to the POST: {replies}"
        assert closed, "the bridge left the connection open"
        assert not marker.exists(), "the request smuggled in the POST body ran"
        # The bridge is still running, and serves whoever holds the token it made for itself.
        assert ready.get("token"), "background.py wrote no token to the ready file"
        hello = Raw(ready["port"]).ask({"id": 1, "command": "hello", "token": ready["token"]})
        assert hello["ok"] is True
    finally:
        process.kill()
        process.wait(timeout=10)
        log.close()


@pytest.mark.integration
def test_deep_nesting_closes_only_that_connection_and_the_session_keeps_its_work(background):
    process, port = background
    kept = proven_raw(port)
    made = kept.ask({"id": 1, "command": "create_object", "params": {"kind": "cube", "name": "Precious"},
                     "token": TOKEN})
    assert made["ok"] is True, made
    pid = kept.ask({"id": 2, "command": "hello", "token": TOKEN})["result"]["pid"]

    unproven = Raw(port)
    unproven.send(b"[" * 200_000)
    assert unproven.reply() is None, "the bridge replied to a first line nested 200000 deep"
    time.sleep(1)
    assert process.poll() is None, f"the background Blender exited ({process.poll()}) after an unproven line"

    second = proven_raw(port)
    second.send(b"[" * 200_000)
    assert second.reply() is None, "the bridge replied to a line nested as deep on a proven connection"

    time.sleep(0.5)
    assert process.poll() is None, "the background Blender exited"
    scene = kept.ask({"id": 3, "command": "get_scene", "token": TOKEN})
    assert [o["name"] for o in scene["result"]["objects"]] == ["Precious"]
    assert kept.ask({"id": 4, "command": "hello", "token": TOKEN})["result"]["pid"] == pid


@pytest.mark.integration
def test_six_hundred_idle_connections_do_not_block_a_background_blender(background):
    """With 600 idle connections open, more than ``select`` takes on Windows, an authenticated request is served.

    At most 8 connections wait to prove themselves, the oldest are closed first to make room, and the rest
    are closed when their time to prove themselves runs out.
    """
    process, port = background
    first = proven_raw(port)  # Proven first, as the MCP server's own connection is.
    flood = []
    try:
        deadline = time.monotonic() + 20
        while len(flood) < 600 and time.monotonic() < deadline:
            try:
                flood.append(socket.create_connection(("127.0.0.1", port), timeout=2))
            except OSError:
                time.sleep(0.01)  # The listen backlog is full, and the bridge empties it as it polls.
        first.send({"id": 1, "command": "get_scene", "token": TOKEN})
        reply = first.reply(seconds=15)
        assert reply is not None and reply["ok"] is True, reply
        assert len(flood) == 600, f"only {len(flood)} of 600 connections were accepted"
        newcomer = proven_raw(port)
        assert newcomer.ask({"id": 2, "command": "get_scene", "token": TOKEN})["ok"] is True

        still_open = list(flood)
        deadline = time.monotonic() + 25  # The newest stay open until their 10 seconds to prove themselves end.
        while still_open and time.monotonic() < deadline:
            still_open = [s for s in still_open if not _closed_by_bridge(s)]
            time.sleep(0.2)
        assert not still_open, f"{len(still_open)} idle connections are still open"
        assert process.poll() is None
        assert first.ask({"id": 3, "command": "get_scene", "token": TOKEN})["ok"] is True
    finally:
        for sock in flood:
            sock.close()


@pytest.mark.integration
def test_the_background_loop_survives_a_base_exception_outside_any_request(background):
    """A KeyboardInterrupt raised in ``poll`` itself, here while accepting a connection, stops nothing."""
    process, port = background
    first = proven_raw(port)
    arm = (
        "import gc, sys\n"
        "kind = sys.modules['blenderlens_addon.bridge'].Bridge\n"
        "bridge = next(o for o in gc.get_objects() if isinstance(o, kind))\n"
        "real = bridge._accept\n"
        "def boom():\n"
        "    bridge._accept = real\n"
        "    raise KeyboardInterrupt\n"
        "bridge._accept = boom\n"
        "result = 'armed'\n"
    )
    armed = first.ask({"id": 1, "command": "run_python", "params": {"code": arm}, "token": TOKEN})
    assert armed.get("result", {}).get("result") == "armed", armed
    late = proven_raw(port)  # Accepting it raised KeyboardInterrupt, and the next poll accepted it.
    assert late.ask({"id": 2, "command": "get_scene", "token": TOKEN})["ok"] is True
    assert process.poll() is None
    assert first.ask({"id": 3, "command": "get_scene", "token": TOKEN})["ok"] is True


@pytest.mark.integration
@pytest.mark.parametrize("code, raised", [
    ("raise SystemExit(3)", "SystemExit"),
    ("import sys\nsys.exit(0)", "SystemExit"),
    ("raise KeyboardInterrupt", "KeyboardInterrupt"),
    ("raise GeneratorExit", "GeneratorExit"),
], ids=["SystemExit-3", "sys.exit-0", "KeyboardInterrupt", "GeneratorExit"])
def test_run_python_cannot_end_the_background_blender(blender, code, raised):
    blender.ok("create_object", kind="cube", name="Kept")
    pid = blender.ok("get_status")["session"]["pid"]
    payload = blender.refused("run_python", "python_error", code=code)
    assert payload["exit"] == 4
    assert payload["error"].startswith(raised), payload["error"]
    if raised == "SystemExit":
        assert "run_python cannot exit Blender" in payload["error"]
    assert blender.ok("get_status")["session"]["pid"] == pid, "a different Blender process serves the session"
    assert [o["name"] for o in blender.ok("get_scene")["objects"]] == ["Kept"]
    assert blender.ok("run_python", code="result = 'still here'")["result"] == "still here"
