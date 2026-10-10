"""Tests for the MCP protocol itself, against the real server over stdio.

Most of these tests need no Blender. A fake add-on (tests/fake_addon.py) speaks the add-on's bridge protocol,
including the token check and the opening ``hello`` command. Where a test needs a slow call, the fake's
render_preview takes three seconds, standing in for a real render. Two tests use a real Blender: one cancels a
real render, and one watches a background Blender start.
"""

import base64
import json
import re
import time
from itertools import pairwise
from pathlib import Path

import pytest

from blenderlens_mcp import __version__, server
from blenderlens_mcp.errors import ToolFailure
from blenderlens_mcp.tools import BY_NAME, TOOLS
from tests.conftest import strict_json
from tests.fake_addon import HELLO, FakeAddon, commands, png_bytes, rendering
from tests.test_glb import build_glb
from tests.test_render import STATE
from tests.test_sessions import background_env

TOKEN = "protocol-test-token"
STATELESS = "2026-07-28"
META_KEY = "io.modelcontextprotocol/protocolVersion"
META = {META_KEY: STATELESS}
INFO_KEY = "io.modelcontextprotocol/serverInfo"
ALL_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
#: How many seconds the fake add-on's render_preview or bake_texture takes, standing in for a slow render or bake.
SLOW = 3.0


@pytest.fixture
def start_fake_addon(tmp_path):
    """Yield a function that starts fake add-ons, each requiring the test token, and close them afterwards.

    Each fake add-on writes its connection file where the server looks for it.
    """
    started = []

    def start(handler) -> FakeAddon:
        """Start a fake add-on that serves each request with ``handler``, and return it."""
        fake = FakeAddon(handler, token=TOKEN)
        fake.write_connection(tmp_path / "runtime")
        started.append(fake)
        return fake

    yield start
    for fake in started:
        fake.close()


def live_env(fake: FakeAddon, tmp_path: Path, **extra) -> dict:
    """Return the environment for a live-mode server that attaches to ``fake``.

    The fake add-on stands in for the Blender window running the add-on. The token is left empty, so the server
    reads it from the connection file.
    """
    return {"BLENDERLENS_MODE": "live", "BLENDERLENS_PORT": str(fake.port), "BLENDERLENS_TOKEN": "",
            "BLENDERLENS_RUNTIME_DIR": str(tmp_path / "runtime"), "BLENDERLENS_RENDER_DIR": str(tmp_path / "renders"),
            **extra}


def an_empty_scene(params: dict) -> dict:
    """Return the fake add-on's get_scene result for a scene with nothing in it."""
    return {"file": None, "objects": []}


def text_of(result: dict) -> dict:
    """Return the JSON in a tools/call result's first text block."""
    return json.loads(result["content"][0]["text"])


def ids_seen(proc) -> list:
    """Return the id of every message the server has written to ``proc`` so far, in order."""
    return [message.get("id") for message in proc.seen if isinstance(message, dict) and "id" in message]


# ---------------------------------------------------------------------------
# Reading stdin on its own thread: cancellation and progress
# ---------------------------------------------------------------------------


def test_a_cancelled_call_gets_no_response_while_ping_and_the_next_call_do(mcp_process, start_fake_addon, tmp_path):
    fake = start_fake_addon(commands(render_preview=rendering(SLOW), get_scene=an_empty_scene))
    proc = mcp_process(env=live_env(fake, tmp_path))
    proc.initialize()
    proc.ok("get_scene")  # Attach first, so the timing below measures the render alone.

    render = proc.send("tools/call", {"name": "render_preview", "arguments": {"path": str(tmp_path / "a.png")}})
    assert fake.wait_for("render_preview")
    asked = time.monotonic()
    ping = proc.send("ping")
    pong = proc.read(timeout=SLOW + 5)
    assert pong["id"] == ping, f"the ping waited for the render to finish: {pong}"
    assert pong["result"] == {}
    assert time.monotonic() - asked < 1.0

    proc.notify("notifications/cancelled", {"requestId": render, "reason": "the test gave up on it"})
    after = proc.send("tools/call", {"name": "get_scene", "arguments": {}})
    response = proc.read(timeout=30)
    assert response["id"] == after, response
    assert response["result"]["isError"] is False
    assert proc.quiet(1.0) == []
    assert render not in ids_seen(proc), "a cancelled request got a response"
    # Blender never held two commands at once: the next call waited for the render's result before it was sent.
    assert fake.commands() == ["hello", "get_scene", "render_preview", "get_scene"]


def test_a_call_cancelled_while_it_waits_its_turn_never_runs(mcp_process, start_fake_addon, tmp_path):
    fake = start_fake_addon(commands(render_preview=rendering(SLOW), get_scene=an_empty_scene,
                                get_object=lambda params: {"name": params["object"]}))
    proc = mcp_process(env=live_env(fake, tmp_path))
    proc.initialize()
    proc.ok("get_scene")

    render = proc.send("tools/call", {"name": "render_preview", "arguments": {"path": str(tmp_path / "a.png")}})
    assert fake.wait_for("render_preview")
    queued = proc.send("tools/call", {"name": "get_object", "arguments": {"object": "Box"}})
    proc.notify("notifications/cancelled", {"requestId": queued})
    assert proc.read(timeout=SLOW + 10)["id"] == render, "the render itself was not cancelled"
    last = proc.send("tools/call", {"name": "get_scene", "arguments": {}})
    assert proc.read(timeout=10)["id"] == last
    assert queued not in ids_seen(proc)
    assert "get_object" not in fake.commands()


def test_cancelling_an_unknown_or_finished_request_writes_nothing(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    finished = proc.send("ping")
    assert proc.read(timeout=5)["id"] == finished
    for params in ({"requestId": 999}, {"requestId": finished}, {"requestId": "nobody"}, {},
                   {"requestId": {"a": 1}}, None):
        proc.notify("notifications/cancelled", params)
    proc.send_raw('{"jsonrpc":"2.0","method":"notifications/cancelled","params":[1]}')
    assert proc.quiet(1.0) == []
    assert proc.request("ping", timeout=5)["result"] == {}


def test_a_progress_token_brings_rising_progress_before_the_result(mcp_process, start_fake_addon, tmp_path):
    fake = start_fake_addon(commands(render_preview=rendering(SLOW), get_scene=an_empty_scene))
    proc = mcp_process(env=live_env(fake, tmp_path))
    proc.initialize()
    proc.ok("get_scene")

    call = proc.send("tools/call", {
        "name": "render_preview", "_meta": {"progressToken": "render-1"},
        "arguments": {"path": str(tmp_path / "a.png"), "engine": "workbench", "timeout": 60}})
    response = proc.read(timeout=SLOW + 10)
    assert response["id"] == call
    assert response["result"]["isError"] is False
    progress = [n["params"] for n in proc.notifications if n.get("method") == "notifications/progress"]
    assert len(progress) >= 2, proc.notifications
    assert {p["progressToken"] for p in progress} == {"render-1"}
    values = [p["progress"] for p in progress]
    assert all(later - earlier >= 0.9 for earlier, later in pairwise(values)), \
        f"progress did not rise by about a second each time: {values}"
    assert values[-1] <= SLOW + 1
    assert {p["total"] for p in progress} == {60}
    assert re.fullmatch(r"render_preview \(workbench\): \d+ s of up to 60 s", progress[-1]["message"]), progress
    assert proc.quiet(1.0) == [], "progress notifications came after the result"


def test_no_progress_without_a_token(mcp_process, start_fake_addon, tmp_path):
    fake = start_fake_addon(commands(render_preview=rendering(SLOW), get_scene=an_empty_scene))
    proc = mcp_process(env=live_env(fake, tmp_path))
    proc.initialize()
    proc.ok("render_preview", path=str(tmp_path / "a.png"))
    assert proc.notifications == []


def test_a_deeply_nested_line_is_a_parse_error_and_the_server_stays_up(mcp_process, no_blender_env):
    """A deeply nested line, or a line that is not UTF-8, is a parse error, and the server keeps responding."""
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    proc.send_raw("[" * 200000)
    response = proc.read(timeout=10)
    assert response["error"]["code"] == -32700, response
    assert response["id"] is None
    proc.send_bytes(b'\xff\xfe{"jsonrpc":"2.0","id":3,"method":"ping"}')
    assert proc.read(timeout=10)["error"]["code"] == -32700
    assert proc.request("ping", timeout=10)["result"] == {}
    assert proc.proc.poll() is None


# ---------------------------------------------------------------------------
# The session-based revisions (2024-11-05 to 2025-11-25) and the stateless revision (2026-07-28)
# ---------------------------------------------------------------------------


def test_initialize_echoes_2024_11_05_and_declares_tools_and_resources(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    result = proc.initialize("2024-11-05")["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {}, "resources": {}}
    assert result["serverInfo"] == {"name": "blenderlens-mcp", "version": __version__}
    assert "structuredContent" not in proc.call_raw("get_status", {})


@pytest.mark.parametrize("version, structured", [
    ("2024-11-05", False), ("2025-03-26", False), ("2025-06-18", True), ("2025-11-25", True)])
def test_structured_content_arrives_with_2025_06_18(mcp_process, no_blender_env, version, structured):
    proc = mcp_process(env=no_blender_env)
    assert proc.initialize(version)["result"]["protocolVersion"] == version
    result = proc.call_raw("get_status", {})
    assert ("structuredContent" in result) is structured
    if structured:
        assert result["structuredContent"] == text_of(result)


def test_server_discover_responds_with_and_without_meta(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)  # No initialize request, because the stateless revision has none.
    for params in ({"_meta": META}, None):
        result = proc.request("server/discover", params, timeout=10)["result"]
        assert result["resultType"] == "complete"
        assert result["supportedVersions"] == ALL_VERSIONS
        assert result["capabilities"] == {"tools": {}, "resources": {}}
        assert result["instructions"] == server.SERVER_INSTRUCTIONS
        assert result["ttlMs"] == 3_600_000
        assert result["cacheScope"] == "private"
        assert result["_meta"][INFO_KEY] == {"name": "blenderlens-mcp", "version": __version__}


@pytest.mark.parametrize("method, params", [
    ("tools/list", {}), ("resources/list", {}), ("resources/templates/list", {}),
    ("resources/read", {"uri": "blenderlens://guide"}),
])
def test_every_result_the_2026_schema_makes_cacheable_carries_its_ttl_and_scope(mcp_process, no_blender_env,
                                                                                method, params):
    """Every result that the 2026 schema makes cacheable carries ``ttlMs`` and ``cacheScope``.

    The schema's CacheableResult requires both, and clients refuse a result without them.
    """
    proc = mcp_process(env=no_blender_env)
    result = proc.request(method, {**params, "_meta": META}, timeout=10)["result"]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == 3_600_000 and result["cacheScope"] == "private"


def test_a_stateless_request_is_served_beside_a_session(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize("2025-03-26")
    stateless = proc.request("tools/list", {"_meta": META}, timeout=10)["result"]
    in_session = proc.request("tools/list", timeout=10)["result"]
    assert stateless["resultType"] == "complete"
    assert stateless["ttlMs"] == 3_600_000 and stateless["cacheScope"] == "private"
    assert stateless["_meta"][INFO_KEY]["name"] == "blenderlens-mcp"
    assert stateless["tools"] == in_session["tools"], \
        "the two kinds of revision list different tools, or in a different order"
    assert not {"resultType", "ttlMs", "cacheScope", "_meta"} & set(in_session)

    called = proc.request("tools/call", {"name": "get_status", "arguments": {}, "_meta": META}, timeout=30)["result"]
    assert called["resultType"] == "complete"
    assert called["_meta"][INFO_KEY]["version"] == __version__
    assert called["structuredContent"] == text_of(called), \
        "the stateless request's result followed the 2025-03-26 session, not its own revision"
    assert "structuredContent" not in proc.call_raw("get_status", {}), \
        "the 2025-03-26 session's own call got structuredContent"


@pytest.mark.parametrize("declared", ["2025-11-25", "1900-01-01"])
def test_an_unsupported_version_in_meta_is_minus_32022_listing_the_one_meta_may_name(mcp_process, no_blender_env,
                                                                                    declared):
    """A request naming any revision but the stateless one in ``_meta`` is refused with -32022.

    The error's data lists only the revision that ``_meta`` may name, since a client retries with one from that
    list, and the message says that the session-based revisions start with ``initialize`` instead.
    """
    proc = mcp_process(env=no_blender_env)
    error = proc.request("tools/list", {"_meta": {META_KEY: declared}}, timeout=10)["error"]
    assert error["code"] == -32022
    assert error["data"] == {"supported": [STATELESS], "requested": declared}
    assert error["message"].startswith(f"Unsupported protocol version in _meta: {declared}."), error
    assert "initialize" in error["message"]


def test_ping_and_initialize_exist_only_in_the_session_based_revisions(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    assert proc.request("ping", timeout=10)["result"] == {}
    assert proc.request("ping", {"_meta": META}, timeout=10)["error"]["code"] == -32601
    assert proc.request("initialize", {"_meta": META, "protocolVersion": "2025-11-25"},
                        timeout=10)["error"]["code"] == -32601


def test_a_batch_is_served_only_under_2025_03_26(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize("2025-03-26")
    proc.send_raw(json.dumps([
        {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "get_status", "arguments": {}}},
        "not a message",
    ]))
    batch = proc.read(timeout=30)
    assert isinstance(batch, list), batch
    assert [response["id"] for response in batch] == [7, 8, None]
    assert batch[0]["result"] == {}
    assert "structuredContent" not in batch[1]["result"]
    assert batch[2]["error"]["code"] == -32600
    proc.send_raw('[{"jsonrpc":"2.0","method":"notifications/initialized"}]')
    assert proc.quiet(0.5) == [], "a batch of notifications alone got a response"
    proc.send_raw("[]")
    assert proc.read(timeout=5)["error"]["code"] == -32600

    newer = mcp_process(env=no_blender_env)
    newer.initialize("2025-06-18")
    newer.send_raw('[{"jsonrpc":"2.0","id":9,"method":"ping"}]')
    refused = newer.read(timeout=5)
    assert refused["error"]["code"] == -32600
    assert "2025-03-26" in refused["error"]["message"]


def test_get_status_names_the_server_session(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    first = proc.ok("get_status")["server_session"]
    assert isinstance(first, str) and len(first) >= 8
    assert proc.ok("get_status")["server_session"] == first
    restarted = mcp_process(env=no_blender_env)
    restarted.initialize()
    assert restarted.ok("get_status")["server_session"] != first


# ---------------------------------------------------------------------------
# Protocol error codes
# ---------------------------------------------------------------------------

ERRORS = [
    # An id of the wrong type gets an error response with a null id, since that id cannot be echoed.
    ('{"jsonrpc":"2.0","id":null,"method":"ping"}', -32600, None),
    ('{"jsonrpc":"2.0","id":{"a":1},"method":"ping"}', -32600, None),
    ('{"jsonrpc":"2.0","id":[1],"method":"tools/list"}', -32600, None),
    ('{"jsonrpc":"2.0","id":true,"method":"tools/list"}', -32600, None),
    ('{"jsonrpc":"2.0","id":1.5,"method":"tools/list"}', -32600, None),
    # A method that is missing or is not a string.
    ('{"jsonrpc":"2.0","id":5}', -32600, 5),
    ('{"jsonrpc":"2.0","id":"five","method":7}', -32600, "five"),
    # A params value that is not an object.
    ('{"jsonrpc":"2.0","id":6,"method":"tools/list","params":[1]}', -32602, 6),
    ('{"jsonrpc":"2.0","id":6,"method":"tools/call","params":"get_scene"}', -32602, 6),
    # An unknown tool, a missing tool name, and an arguments value that is not an object.
    ('{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"get_everything","arguments":{}}}', -32602, 7),
    ('{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"arguments":{}}}', -32602, 7),
    ('{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"get_scene","arguments":[]}}', -32602, 7),
    ('{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"get_scene","arguments":"all"}}', -32602, 7),
    # An unknown method.
    ('{"jsonrpc":"2.0","id":8,"method":"no/such/method"}', -32601, 8),
    # Text that is not valid JSON, and JSON that is not a JSON-RPC message.
    ("NOT JSON", -32700, None),
    ('{"jsonrpc":"2.0","id":9,"method":"ping"', -32700, None),
    ('"a string"', -32600, None),
    ("42", -32600, None),
]

GETS_NO_RESPONSE = [
    '{"jsonrpc":"2.0","method":"no/such/notification"}',
    '{"jsonrpc":"2.0","method":"tools/call","params":[1]}',
    '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_everything"}}',
    '{"jsonrpc":"2.0"}',
    '{"jsonrpc":"2.0","method":"notifications/initialized"}',
    # A response from the client needs no reply, and this server sends no requests it could belong to.
    '{"jsonrpc":"2.0","id":3,"result":{}}',
]


def test_protocol_errors_carry_their_codes_and_notifications_get_no_response(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    wrong = []
    for line, code, echoed in ERRORS:
        proc.send_raw(line)
        try:
            response = proc.read(timeout=5)
        except TimeoutError:
            wrong.append(f"{line[:70]}: no response, wanted {code}")
            continue
        got = response.get("error", {}).get("code") if isinstance(response, dict) else response
        if got != code or response.get("id", "absent") != echoed:
            wrong.append(f"{line[:70]}: got {json.dumps(response)[:160]}, wanted {code} with id {echoed}")
    assert not wrong, "\n".join(wrong)

    for line in GETS_NO_RESPONSE:
        proc.send_raw(line)
    assert proc.quiet(1.0) == [], "a notification got a response"
    assert proc.request("ping", timeout=5)["result"] == {}


# ---------------------------------------------------------------------------
# Arguments checked against each tool's input schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool, arguments, argument, says", [
    ("delete_object", {"objects": ["Body"], "with_children": "false"}, "with_children", "true or false"),
    ("get_object", {}, "object", "needs 'object'"),
    ("create_object", {"kind": "pyramid"}, "kind", '"cube"'),
    ("create_object", {"kind": "cube", "location": [1, 2]}, "location", "an array of 3 numbers or nulls"),
    ("create_object", {"kind": "cube", "segments": 2.5}, "segments", "an integer"),
    ("set_material", {"material": "Paint", "base_color": "#zzzzzz"}, "base_color", "matching"),
    ("render_preview", {"resolution": "big"}, "resolution", "an integer or an array of 2 integers"),
    ("create_mesh", {"name": "M", "vertices": [[0, 0, 0], [1, 0]]}, "vertices[1]", "an array of 3 numbers"),
    ("set_parent", {"object": "A", "parent": 5}, "parent", "a string or null"),
    ("run_python", {"code": "result = 1", "timeout": "soon"}, "timeout", "a number"),
    ("set_transform", {"object": "A", "location": [0, 0, None], "scale": True}, "scale", "a number or"),
])
def test_an_argument_outside_its_schema_is_a_bad_argument_naming_it(mcp_process, no_blender_env, tool, arguments,
                                                                    argument, says):
    # Live mode with no add-on running, so a call that got as far as Blender would fail with not_connected instead.
    proc = mcp_process(env={**no_blender_env, "BLENDERLENS_MODE": "live"})
    proc.initialize()
    payload = proc.refused(tool, "bad_argument", **arguments)
    assert payload["argument"] == argument, payload
    assert f"'{argument}'" in payload["error"], payload
    assert f"'{argument}' takes" in payload["hint"], "the hint does not name what the argument takes"
    assert says in f"{payload['error']} {payload['hint']}", payload


def test_a_misspelt_argument_names_itself_and_the_accepted_ones(mcp_process, no_blender_env):
    proc = mcp_process(env={**no_blender_env, "BLENDERLENS_MODE": "live"})
    proc.initialize()
    payload = proc.refused("get_object", "bad_argument", objct="Body")
    assert payload["argument"] == "objct"
    assert "'objct'" in payload["error"]
    assert "object" in payload["hint"]


def test_unknown_keys_are_refused_where_the_schema_says_so():
    from blenderlens_mcp.arguments import check_arguments

    schema = {**BY_NAME["get_object"]["inputSchema"], "additionalProperties": False}
    with pytest.raises(ToolFailure) as caught:
        check_arguments("get_object", schema, {"objct": "Body"})
    payload = caught.value.payload()
    assert payload["kind"] == "bad_argument"
    assert payload["argument"] == "objct"
    assert "get_object has no argument 'objct'" in payload["error"]
    assert "object" in payload["hint"]
    # Without additionalProperties: false, a key the schema does not name passes, as JSON Schema says.
    open_schema = {k: v for k, v in BY_NAME["get_object"]["inputSchema"].items() if k != "additionalProperties"}
    check_arguments("get_object", open_schema, {"object": "Body", "later": 1})


def example(schema: dict):
    """Return a value that fits ``schema``, built from the schema alone."""
    if "anyOf" in schema:
        return example(schema["anyOf"][0])
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    kind = kind[0] if isinstance(kind, list) else kind
    if kind == "array":
        return [example(schema.get("items", {"type": "number"}))] * max(1, schema.get("minItems", 1))
    if kind == "string" and "pattern" in schema:
        return "#ff8000"
    return {"string": "Body", "number": 1.5, "integer": 2, "boolean": False, "object": {}, "null": None}.get(kind, 1)


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t["name"])
def test_every_argument_a_schema_describes_is_accepted(tool):
    from blenderlens_mcp.arguments import check_arguments

    schema = tool["inputSchema"]
    every = {key: example(prop) for key, prop in schema.get("properties", {}).items()}
    check_arguments(tool["name"], schema, every)
    check_arguments(tool["name"], schema, {key: every[key] for key in schema.get("required", [])})


def test_numbers_must_be_finite_and_integers_whole():
    from blenderlens_mcp.arguments import check_arguments

    schema = {"type": "object", "properties": {"n": {"type": "number"}, "i": {"type": "integer"}}}
    for bad in ({"n": float("nan")}, {"n": float("inf")}, {"n": True}, {"i": 2.5}, {"i": False}):
        with pytest.raises(ToolFailure):
            check_arguments("t", schema, bad)
    check_arguments("t", schema, {"n": 3, "i": 4.0})  # JSON Schema counts 4.0 as an integer.


# ---------------------------------------------------------------------------
# Resource links
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version, linked", [("2025-03-26", False), ("2025-06-18", True), ("2025-11-25", True)])
def test_a_preview_links_its_png_from_2025_06_18(mcp_process, start_fake_addon, tmp_path, version, linked):
    fake = start_fake_addon(commands(render_preview=rendering()))
    proc = mcp_process(env=live_env(fake, tmp_path))
    proc.initialize(version)
    png = tmp_path / "out" / "body.png"
    result = proc.call_raw("render_preview", {"path": str(png)})
    assert result["isError"] is False, result
    links = [block for block in result["content"] if block["type"] == "resource_link"]
    if not linked:
        assert links == []
        return
    assert len(links) == 1
    link = links[0]
    assert link["uri"] == png.as_uri()
    assert link["uri"].startswith("file:///")
    assert link["name"] == "body.png"
    assert link["mimeType"] == "image/png"
    assert link["description"]


def test_a_stateless_preview_is_linked_too(mcp_process, start_fake_addon, tmp_path):
    fake = start_fake_addon(commands(render_preview=rendering()))
    proc = mcp_process(env=live_env(fake, tmp_path))
    png = tmp_path / "stateless.png"
    result = proc.request("tools/call", {"name": "render_preview", "arguments": {"path": str(png)}, "_meta": META},
                          timeout=30)["result"]
    assert [b["uri"] for b in result["content"] if b["type"] == "resource_link"] == [png.as_uri()]


def test_inline_still_embeds_the_image_beside_the_link(mcp_process, start_fake_addon, tmp_path):
    fake = start_fake_addon(commands(render_preview=rendering(rgb=(1, 2, 3))))
    proc = mcp_process(env=live_env(fake, tmp_path))
    proc.initialize()
    png = tmp_path / "inline.png"
    result = proc.call_raw("render_preview", {"path": str(png), "inline": True})
    assert sorted(block["type"] for block in result["content"]) == ["image", "resource_link", "text"]
    image = next(block for block in result["content"] if block["type"] == "image")
    assert image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == png.read_bytes() == png_bytes((1, 2, 3))


def test_no_prompts_are_offered(mcp_process, no_blender_env):
    """The server declares no prompts capability, and prompts/list is an unknown method."""
    proc = mcp_process(env=no_blender_env)
    assert "prompts" not in proc.initialize()["result"]["capabilities"]
    assert proc.request("prompts/list", timeout=10)["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Strict JSON output: every line the server writes is valid JSON
# ---------------------------------------------------------------------------


def test_non_finite_numbers_from_blender_are_written_as_strict_json(mcp_process, start_fake_addon, tmp_path):
    odd = {"name": "Odd", "location": [float("nan"), float("inf"), -float("inf")]}
    fake = start_fake_addon(commands(get_object=lambda params: odd))
    proc = mcp_process(env=live_env(fake, tmp_path))
    proc.initialize()
    result = proc.call_raw("get_object", {"object": "Odd"})  # The test harness parses every line strictly.
    assert result["structuredContent"]["location"] == ["nan", "inf", "-inf"]
    assert text_of(result)["location"] == ["nan", "inf", "-inf"]


def test_a_response_that_cannot_be_written_is_an_internal_error_not_a_crash(monkeypatch):
    loop: list = []
    loop.append(loop)
    monkeypatch.setattr(server, "respond", lambda message, cancelled=None: {
        "jsonrpc": "2.0", "id": message["id"], "result": {"loop": loop}})
    written = server.serve_message({"jsonrpc": "2.0", "id": 41, "method": "tools/list"})
    response = strict_json(written)
    assert response["id"] == 41
    assert response["error"]["code"] == -32603


def test_the_wire_writes_non_finite_numbers_as_strings():
    line = server.encode({"a": float("nan"), "b": [float("inf"), -float("inf")], "c": (1.5,)})
    assert strict_json(line) == {"a": "nan", "b": ["inf", "-inf"], "c": [1.5]}


# ---------------------------------------------------------------------------
# The server trusts only the paths it asked for, not a path that the add-on reports
# ---------------------------------------------------------------------------


def test_a_preview_result_naming_another_file_is_not_believed(mcp_process, start_fake_addon, tmp_path):
    decoy = tmp_path / "decoy.png"
    decoy.write_bytes(png_bytes((255, 255, 255)))
    fake = start_fake_addon(commands(render_preview=rendering(reported_path=str(decoy), rgb=(10, 20, 30))))
    proc = mcp_process(env=live_env(fake, tmp_path))
    proc.initialize()
    png = tmp_path / "asked.png"
    result = proc.call_raw("render_preview", {"path": str(png), "inline": True})
    assert text_of(result)["path"] == str(png)
    image = next(block for block in result["content"] if block["type"] == "image")
    assert base64.b64decode(image["data"]) == png.read_bytes() == png_bytes((10, 20, 30))
    assert [b["uri"] for b in result["content"] if b["type"] == "resource_link"] == [png.as_uri()]


def a_glb_holding(name: str) -> bytes:
    """Return a minimal .glb file whose one node is named ``name``."""
    return build_glb({"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"name": name}]})


def test_an_export_result_naming_another_file_is_not_believed(mcp_process, start_fake_addon, tmp_path):
    decoy = tmp_path / "decoy.glb"
    decoy.write_bytes(a_glb_holding("Decoy"))

    def export(params: dict) -> dict:
        """Write the file asked for, but report the decoy's path in the result."""
        Path(params["path"]).write_bytes(a_glb_holding("Asked"))
        return {"path": str(decoy), "bytes": decoy.stat().st_size, "objects": ["Asked"]}

    fake = start_fake_addon(commands(export_glb=export))
    proc = mcp_process(env=live_env(fake, tmp_path))
    proc.initialize()
    asked = tmp_path / "asked.glb"
    exported = proc.ok("export_glb", path=str(asked))
    assert exported["path"] == str(asked)
    assert [node["name"] for node in exported["contents"]["nodes"]] == ["Asked"]
    assert exported["contents"]["warnings"] == []


# ---------------------------------------------------------------------------
# Tests with a real Blender
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_a_cancelled_render_changes_nothing_and_the_calls_after_it_are_served(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    proc.ok("create_object", kind="uv_sphere", name="Body", segments=64, rings=32)
    # Rough metal converges slowly, so the render is still running when the cancellation arrives. With adaptive
    # sampling, a plain sphere at a small size can finish within the second the test waits.
    proc.ok("set_material", material="Rough metal", object="Body", base_color=[0.8, 0.6, 0.3], metallic=1.0,
            roughness=0.35)
    before = proc.ok("run_python", code=STATE)["result"]

    render = proc.send("tools/call", {"name": "render_preview", "arguments": {
        "objects": ["Body"], "engine": "cycles", "resolution": 1024, "samples": 1000,
        "path": str(tmp_path / "cancelled.png")}})
    time.sleep(1.0)
    proc.notify("notifications/cancelled", {"requestId": render, "reason": "the client timed out"})
    session = proc.ok("get_status")["session"]
    assert session["busy"]["command"] == "render_preview", "the cancel stopped Blender's render, not only the wait"

    after = proc.ok("run_python", code=STATE, timeout=300)["result"]
    assert after == before
    assert render not in ids_seen(proc)
    assert proc.ok("get_status")["session"]["busy"] is None


@pytest.mark.integration
def test_a_background_start_reports_progress(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    call = proc.send("tools/call", {"name": "open_session", "arguments": {"mode": "background"},
                                    "_meta": {"progressToken": 7}})
    response = proc.read(timeout=180)
    assert response["id"] == call
    assert response["result"]["isError"] is False, response
    progress = [n["params"] for n in proc.notifications if n.get("method") == "notifications/progress"]
    assert progress, "no progress was sent while a background Blender started, which takes seconds"
    assert {p["progressToken"] for p in progress} == {7}
    assert "starting a background Blender" in progress[0]["message"]


# ---------------------------------------------------------------------------
# The add-on's protocol, and the tools that take a timeout
# ---------------------------------------------------------------------------


def test_an_add_on_that_speaks_another_protocol_is_refused_with_the_reinstall_hint(mcp_process, start_fake_addon,
                                                                                   tmp_path):
    """Every call to an add-on that speaks another bridge protocol fails with version_mismatch.

    The add-on here reports version 0.0.1 and protocol 1, and no command but ``hello`` reaches it.
    """
    old = {**HELLO, "protocol": 1, "addon_version": "0.0.1", "commands": ["get_scene", "run_python"]}

    def handler(request):
        """Reply to ``hello`` as the add-on with the other protocol, and to any other command with an empty scene."""
        if request.get("command") == "hello":
            return {"ok": True, "result": dict(old)}
        return {"ok": True, "result": {"objects": []}}

    fake = start_fake_addon(handler)
    proc = mcp_process(env=live_env(fake, tmp_path))
    proc.initialize()
    for tool, arguments in (("get_scene", {}), ("run_python", {"code": "result = 1"}), ("undo_change", {})):
        payload = proc.refused(tool, "version_mismatch", **arguments)
        assert "protocol 1" in payload["error"] and "0.0.1" in payload["error"], payload
        assert "install-addon" in payload["hint"], payload
    assert set(fake.commands()) == {"hello"}, fake.commands()


def test_bake_texture_takes_a_timeout_of_its_own(mcp_process, start_fake_addon, tmp_path):
    """bake_texture takes its own timeout, as render_preview does, which the server enforces and never sends to Blender.

    A bake within its limits can run for minutes.
    """
    def bake(params):
        """Take SLOW seconds, as a slow bake would, and return the path asked for."""
        time.sleep(SLOW)
        return {"path": params["path"]}

    fake = start_fake_addon(commands(bake_texture=bake, get_scene=an_empty_scene))
    proc = mcp_process(env=live_env(fake, tmp_path))
    proc.initialize()
    proc.ok("get_scene")
    call = proc.send("tools/call", {"name": "bake_texture", "_meta": {"progressToken": "bake-1"},
                                    "arguments": {"object": "Crate", "path": str(tmp_path / "c.png"), "timeout": 60}})
    response = proc.read(timeout=SLOW + 10)
    assert response["id"] == call and response["result"]["isError"] is False, response
    progress = [n["params"] for n in proc.notifications if n.get("method") == "notifications/progress"]
    assert progress and {p["total"] for p in progress} == {60}, progress
    started = time.monotonic()
    proc.refused("bake_texture", "deadline", object="Crate", path=str(tmp_path / "c.png"), timeout=0.5)
    assert time.monotonic() - started < SLOW
    sent = [r["params"] for r in fake.requests if r.get("command") == "bake_texture"]
    assert len(sent) == 2 and all("timeout" not in params for params in sent), sent
