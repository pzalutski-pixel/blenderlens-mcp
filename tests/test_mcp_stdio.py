"""Tests for the MCP server as a process, driven over real stdio pipes without Blender.

The refusals tested here are the ones a user meets before any modelling happens: no Blender installed, no
Blender window running the add-on, or a tool or argument that does not exist. Each must come back as a
structured error and leave the server running and responding to requests.
"""

import json
import subprocess
import sys

import pytest

from blenderlens_mcp import __version__
from blenderlens_mcp.tools import TOOLS
from tests.conftest import checkout_env


def test_initialize_lists_annotated_tools_and_instructions(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    resp = proc.initialize()
    result = resp["result"]
    assert result["serverInfo"]["name"] == "blenderlens-mcp"
    assert result["protocolVersion"] == "2025-11-25"
    assert "A refusal is a structured error" in result["instructions"]

    tools = proc.request("tools/list")["result"]["tools"]
    assert len(tools) == len(TOOLS)
    for tool in tools:
        assert set(tool) == {"name", "title", "description", "inputSchema", "annotations"}


def test_initialize_with_an_unsupported_protocol_version_returns_the_servers_own(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    assert proc.initialize("1999-01-01")["result"]["protocolVersion"] == "2025-11-25"


def test_an_older_client_gets_text_without_structured_content(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize("2025-03-26")
    result = proc.call_raw("get_status", {})
    assert "structuredContent" not in result
    assert result["content"][0]["type"] == "text"


def test_a_current_client_gets_structured_content_matching_the_text(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    result = proc.call_raw("get_status", {})
    assert result["structuredContent"] == json.loads(result["content"][0]["text"])


def test_blank_lines_do_not_end_the_session(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    proc.send_raw("")
    proc.send_raw("   ")
    assert proc.request("ping")["result"] == {}


def test_malformed_json_returns_a_parse_error_and_continues(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    proc.send_raw("NOT JSON")
    assert proc.read()["error"]["code"] == -32700
    assert proc.request("ping")["result"] == {}


def test_a_json_array_is_rejected_and_the_session_continues(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    proc.send_raw('[{"jsonrpc":"2.0","id":9,"method":"ping"}]')
    assert proc.read()["error"]["code"] == -32600
    assert proc.request("ping")["result"] == {}


def test_an_unknown_method_errors_without_dying(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    assert proc.request("no/such/method")["error"]["code"] == -32601
    assert proc.request("ping")["result"] == {}


def test_an_unknown_tool_is_invalid_params_and_the_session_continues(mcp_process, no_blender_env):
    # An unknown tool name is a JSON-RPC protocol error, as in the MCP specification's own example. A bad
    # argument to a known tool is still returned as a tool error.
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    error = proc.request("tools/call", {"name": "blender_get_everything", "arguments": {}})["error"]
    assert error["code"] == -32602
    assert "Unknown tool: blender_get_everything" in error["message"]
    assert proc.request("ping")["result"] == {}


def test_get_status_starts_nothing_and_reports_the_addon_absent(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    status = proc.ok("get_status")
    assert status["session"] is None
    assert status["live_addon"]["responding"] is False
    assert status["live_addon"]["port"] == int(no_blender_env["BLENDERLENS_PORT"])
    assert status["configured_mode"] == "auto"
    assert status["render_dir"] == no_blender_env["BLENDERLENS_RENDER_DIR"]


def test_live_mode_without_the_addon_is_not_connected(mcp_process, no_blender_env):
    proc = mcp_process(env={**no_blender_env, "BLENDERLENS_MODE": "live"})
    proc.initialize()
    payload = proc.refused("get_scene", "not_connected")
    assert no_blender_env["BLENDERLENS_PORT"] in payload["error"]
    assert "Start" in payload["hint"]
    assert payload["exit"] == 7
    # The call is refused, and the server still responds to requests.
    assert proc.request("ping")["result"] == {}


def test_open_session_live_without_the_addon_is_not_connected(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    proc.refused("open_session", "not_connected", mode="live")


def test_a_missing_blender_is_reported_with_the_variable_to_set(mcp_process, no_blender_env, tmp_path):
    ghost = str(tmp_path / "no-blender-here.exe")
    proc = mcp_process(env={**no_blender_env, "BLENDERLENS_MODE": "background", "BLENDER_BIN": ghost})
    proc.initialize()
    payload = proc.refused("create_object", "blender_missing", kind="cube")
    assert ghost in payload["error"]
    assert "BLENDER_BIN" in payload["hint"]
    status = proc.ok("get_status")
    assert status["blender"]["found"] is False
    assert status["blender"]["problem"]["kind"] == "blender_missing"


def test_close_session_with_nothing_attached_succeeds_and_closes_nothing(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    assert proc.ok("close_session")["closed"] is None


def test_open_session_rejects_an_unknown_mode(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    proc.refused("open_session", "bad_argument", mode="sideways")


@pytest.mark.parametrize(("tool", "arguments"), [
    ("open_session", {"mode": "live", "discard": "false"}),
    ("close_session", {"discard": "false"}),
    ("close_session", {"discard": 0}),
])
def test_discard_must_be_true_or_false(mcp_process, no_blender_env, tool, arguments):
    """A ``discard`` that is not a JSON boolean is refused as a bad argument.

    Python reads the string ``"false"`` as true, so accepting it would throw away unsaved work.
    """
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    payload = proc.refused(tool, "bad_argument", **arguments)
    said = payload["error"] + " " + payload["hint"]
    assert "discard" in said and "true or false" in said


def test_open_session_with_a_missing_file_is_not_found(mcp_process, no_blender_env, tmp_path):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    proc.refused("open_session", "not_found", mode="background", file=str(tmp_path / "nope.blend"))


def test_the_console_entry_prints_its_version():
    run = subprocess.run([sys.executable, "-m", "blenderlens_mcp", "--version"], capture_output=True, text=True,
                         timeout=60, env=checkout_env())
    assert run.returncode == 0
    assert __version__ in run.stdout


def test_stdout_carries_only_jsonrpc_and_the_server_exits_cleanly(mcp_process, no_blender_env):
    proc = mcp_process(env={**no_blender_env, "BLENDERLENS_MODE": "live"})
    proc.initialize()
    proc.call("get_scene")
    assert proc.request("ping")["jsonrpc"] == "2.0"
    stderr = proc.close()
    assert proc.proc.returncode == 0, stderr
    assert "Traceback" not in stderr
