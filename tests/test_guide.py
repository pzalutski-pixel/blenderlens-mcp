"""Tests for the guide resource and the server instructions that point at it, against the real server over stdio.

No Blender is needed, because resources belong to the server itself. Both kinds of MCP protocol
revision that the server speaks serve them: the session-based revisions (2024-11-05 to 2025-11-25), which start
with ``initialize``, and the stateless revision (2026-07-28).
"""

import re

from blenderlens_mcp import server
from blenderlens_mcp.resources import GUIDE, GUIDE_URI
from blenderlens_mcp.tools import BY_NAME
from tests.test_tools_table import VERBS

STATELESS = "2026-07-28"
META = {"io.modelcontextprotocol/protocolVersion": STATELESS}

#: Matches a word shaped like a tool name: a verb from ``VERBS`` in ``test_tools_table``, an underscore, and more
#: letters.
TOOL_NAME = re.compile(r"\b(?:" + "|".join(sorted(VERBS)) + r")_[a-z_]+\b")


def test_resources_are_a_declared_capability_in_both_kinds_of_revision(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    assert proc.initialize()["result"]["capabilities"]["resources"] == {}
    discovered = proc.request("server/discover", {"_meta": META}, timeout=10)["result"]
    assert discovered["capabilities"]["resources"] == {}


def test_resources_list_and_read_round_trip(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    [listed] = proc.request("resources/list", timeout=10)["result"]["resources"]
    assert listed["uri"] == GUIDE_URI == "blenderlens://guide"
    assert listed["mimeType"] == "text/markdown"
    assert listed["name"] and listed["title"] and listed["description"]
    [content] = proc.request("resources/read", {"uri": GUIDE_URI}, timeout=10)["result"]["contents"]
    assert content == {"uri": GUIDE_URI, "mimeType": "text/markdown", "text": GUIDE}
    assert listed["size"] == len(GUIDE.encode("utf-8"))
    assert proc.request("resources/templates/list", timeout=10)["result"] == {"resourceTemplates": []}


def test_the_stateless_revision_serves_resources(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)  # No initialize, because the 2026-07-28 revision is stateless.
    listed = proc.request("resources/list", {"_meta": META}, timeout=10)["result"]
    assert listed["resultType"] == "complete"
    assert listed["ttlMs"] == 3_600_000 and listed["cacheScope"] == "private"
    read = proc.request("resources/read", {"uri": GUIDE_URI, "_meta": META}, timeout=10)["result"]
    assert read["resultType"] == "complete"
    assert read["contents"][0]["text"] == GUIDE


def test_an_unknown_or_missing_uri_is_an_error(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    unknown = proc.request("resources/read", {"uri": "blenderlens://nothing"}, timeout=10)["error"]
    assert unknown["code"] == -32002, "the error is not the specification's Resource not found"
    assert GUIDE_URI in unknown["message"] and unknown["data"] == {"uri": "blenderlens://nothing"}
    for params in ({}, {"uri": 7}, {"uri": ""}):
        assert proc.request("resources/read", params, timeout=10)["error"]["code"] == -32602, params


def test_the_guide_names_only_tools_that_exist():
    named = set(TOOL_NAME.findall(GUIDE))
    assert {"check_asset", "render_preview", "export_glb", "run_script", "undo_change"} <= named
    assert named <= set(BY_NAME), f"the guide names tools that do not exist: {sorted(named - set(BY_NAME))}"


def test_the_guide_names_the_tools_for_motion_collision_baking_and_other_formats():
    named = set(TOOL_NAME.findall(GUIDE))
    assert {"animate_object", "create_collision", "bake_texture", "export_file"} <= named, sorted(named)
    assert "loop" in GUIDE and "cycle" in GUIDE, "the guide does not say that Godot loops a clip by its name"
    assert "force" in GUIDE, "the guide does not name undo_change's force, for steps BlenderLens cannot name"


def test_the_guide_holds_the_conventions_the_loop_and_godot():
    for words in ("-Y", "+Z", "'#RRGGBB'", "result_faces", "check_asset", "target: \"godot\"", "-colonly",
                  "blenderlens.toml", "stale"):
        assert words in GUIDE, words
    assert len(GUIDE.encode("utf-8")) < 8192, "the guide is 8 KB or more, too long to read in one go"


def test_the_instructions_point_at_the_guide_and_name_the_checking_tools():
    instructions = server.SERVER_INSTRUCTIONS
    assert GUIDE_URI in instructions
    for tool in ("check_asset", "measure_objects", "undo_change"):
        assert f"`{tool}`" in instructions, tool
    named = set(TOOL_NAME.findall(instructions))
    assert named <= set(BY_NAME), sorted(named - set(BY_NAME))


def test_the_guide_holds_the_rules_format_with_an_example_that_validates():
    """The guide's rules section lists every rules key, and its example validates.

    The description of ``check_asset`` points to this section for the keys.
    """
    import json

    from tests.test_rules import rules

    section = GUIDE[GUIDE.index("## Rules"):]
    for key in rules.KEYS:
        assert f"`{key}`" in section, key
    assert "rules_file" in section and "check_asset" in section
    example = section[section.index("```json") + len("```json"):]
    example = example[:example.index("```")]
    assert rules.validate(json.loads(example))
