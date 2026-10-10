"""Tests for the rules that the tool definitions in tools.py follow.

Every tool acts on Blender and nothing else. A tool's name is a verb and the resource it acts on, and never
repeats the server's name. Whether a tool reads, writes or runs code is stated explicitly, and its description
says what it does, what it returns and when to call it. These tests check those rules, and check that tools.py
and the add-on's command table list the same commands.
"""

import ast
import json
import re
from pathlib import Path

import pytest

from blenderlens_mcp import errors, server, session, tools

ADDON = Path(server.__file__).with_name("addon")
#: The verbs that a tool's name may start with.
VERBS = {"add", "animate", "apply", "bake", "check", "close", "create", "delete", "duplicate", "edit", "export",
         "get", "import", "join", "list", "measure", "open", "remove", "render", "run", "save", "set", "undo",
         "unwrap"}

#: The most bytes the serialised tools/list result may take: 38 KB, which leaves each tool under 1 KB on average,
#: about two thirds of it structure (schemas, enums and annotations).
TOOLS_LIST_MAX_BYTES = 38 * 1024

#: Whether each tool may destroy something: unsaved work, a file, or what the scene holds. Read-only tools carry
#: no destructiveHint at all, because the MCP specification reads it only when readOnlyHint is false.
DESTRUCTIVE = {
    # These only read and measure.
    "get_status": False, "get_scene": False, "get_object": False, "get_mesh": False, "list_materials": False,
    "check_asset": False, "measure_objects": False,
    # With discard, these drop a background session's unsaved work, or stop a busy Blender.
    "open_session": True, "close_session": True,
    # These add beside what exists. create_collision adds a collision proxy beside the object.
    "create_object": False, "create_mesh": False, "add_modifier": False, "duplicate_object": False,
    "import_file": False, "create_collision": False,
    # These replace values, or remove what was there. animate_object replaces a clip of the same name.
    "edit_mesh": True, "set_modifier": True, "apply_modifier": True, "remove_modifier": True, "set_material": True,
    "set_transform": True, "apply_transform": True, "set_parent": True, "set_collection": True, "set_object": True,
    "delete_object": True, "set_vertex_color": True, "unwrap_uv": True, "join_objects": True,
    "animate_object": True,
    # These overwrite a file on disk, or the open file. bake_texture also rewires the material's nodes.
    "export_glb": True, "export_file": True, "save_file": True, "open_file": True, "create_file": True,
    "render_preview": True, "bake_texture": True,
    # run_python and run_script run any Python code, and undo_change takes back what earlier calls did.
    "run_python": True, "run_script": True, "undo_change": True,
}

#: Words a Title Case title leaves in lower case, and names that keep their own case.
TITLE_SMALL_WORDS = {"a", "an", "and", "as", "for", "from", "in", "of", "on", "or", "to"}
TITLE_OWN_CASE = {"glTF"}

#: The add-on's helpers that read one named parameter, called as helper(params, "key", ...).
PARAM_READERS = {"require", "number", "boolean", "text", "_number", "_whole", "_number_in", "_image_path"}
ADDON_MODULES = ("commands.py", "meshops.py", "preview.py", "common.py", "paint.py", "uv.py", "shapes.py",
                 "checks.py", "measure.py", "scripts.py", "undo.py", "motion.py", "collision.py", "bake.py",
                 "formats.py")


def annotation_sets() -> list[dict]:
    """Return the annotation sets that tools.py defines, one of which every tool must carry."""
    return [tools.READ, tools.WRITE, tools.REWRITE, tools.CHANGE, tools.ANY]


def listed_tools(monkeypatch) -> dict:
    """Return the tools/list result as the server returns it, with run_python included, which is the longest list."""
    monkeypatch.setenv("BLENDERLENS_ALLOW_PYTHON", "1")
    return server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]


def commands_table() -> dict:
    """Return the add-on's COMMANDS table as syntax nodes, read from its source.

    The add-on imports ``bpy``, so it cannot be imported here.
    """
    tree = ast.parse((ADDON / "commands.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == "COMMANDS" for t in targets):
                return dict(zip(node.value.keys, node.value.values, strict=True))
    raise AssertionError("COMMANDS not found in addon/commands.py")


def blender_commands() -> set[str]:
    """Return the names of the commands in the add-on's COMMANDS table."""
    return {key.value for key in commands_table()}


def _callee(call: ast.Call) -> str:
    """Return the name of the function or method that ``call`` calls, or "" when it has none."""
    return call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")


def _is_params(node) -> bool:
    """Return whether ``node`` is the name ``params``."""
    return isinstance(node, ast.Name) and node.id == "params"


def _is_text(node) -> bool:
    """Return whether ``node`` is a string constant."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def parameters_each_command_reads() -> dict[str, set[str]]:
    """Return every key each Blender command reads from its params, followed through the helpers it passes them to.

    A key counts as read in ``params["key"]``, ``params.get("key")``, or ``helper(params, "key", ...)`` for a
    helper in PARAM_READERS. Any other call given ``params`` is followed into that function. A key held in a
    variable is not seen, as when apply_transform reads its three flags in a loop, so such keys must be checked
    by reading the code.
    """
    reads: dict[str, set[str]] = {}
    hands: dict[str, set[str]] = {}
    for module in ADDON_MODULES:
        for func in ast.walk(ast.parse((ADDON / module).read_text(encoding="utf-8"))):
            if not isinstance(func, ast.FunctionDef):
                continue
            own, callees = reads.setdefault(func.name, set()), hands.setdefault(func.name, set())
            for node in ast.walk(func):
                if isinstance(node, ast.Subscript) and _is_params(node.value) and _is_text(node.slice):
                    own.add(node.slice.value)
                elif not isinstance(node, ast.Call):
                    continue
                elif (isinstance(node.func, ast.Attribute) and node.func.attr == "get" and _is_params(node.func.value)
                      and node.args and _is_text(node.args[0])):
                    own.add(node.args[0].value)
                elif any(_is_params(arg) for arg in node.args):
                    at = next(i for i, arg in enumerate(node.args) if _is_params(arg))
                    if _callee(node) in PARAM_READERS:
                        if len(node.args) > at + 1 and _is_text(node.args[at + 1]):
                            own.add(node.args[at + 1].value)
                    else:
                        callees.add(_callee(node))
    out = {}
    for key, value in commands_table().items():
        handler = value.elts[0]
        todo, seen, keys = [handler.id if isinstance(handler, ast.Name) else handler.attr], set(), set()
        while todo:
            name = todo.pop()
            if name not in seen:
                seen.add(name)
                keys |= reads.get(name, set())
                todo.extend(hands.get(name, set()))
        out[key.value] = keys
    return out


def constant(path: Path, name: str):
    """Return the literal value assigned to the module-level name ``name`` in the source file ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path.name}")


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t["name"])
def test_a_tool_is_a_verb_and_a_resource_and_never_names_the_server(tool):
    name = tool["name"]
    assert re.fullmatch(r"[a-z]+_[a-z_]+", name), name
    assert name.split("_")[0] in VERBS, f"{name} does not start with a verb from {sorted(VERBS)}"
    assert "blender" not in name and "lens" not in name


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t["name"])
def test_a_description_says_what_it_returns_and_when_to_call_it(tool):
    description = tool["description"]
    assert "Returns:" in description, tool["name"]
    assert "WHEN TO CALL" in description, tool["name"]
    assert description.index("Returns:") < description.index("WHEN TO CALL"), tool["name"]


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t["name"])
def test_a_tool_has_a_short_human_title(tool):
    title = tool.get("title")
    assert isinstance(title, str) and title, f"{tool['name']} has no title"
    assert len(title) <= 32 and "_" not in title, f"{tool['name']}: {title!r} is not a short human title"
    words = title.split()
    assert words[0][0].isupper(), f"{tool['name']}: {title!r} is not Title Case"
    for word in words[1:]:
        assert word[0].isupper() or word in TITLE_SMALL_WORDS | TITLE_OWN_CASE, \
            f"{tool['name']}: {title!r} is not Title Case ({word!r})"


def test_titles_are_unique():
    titles = [t.get("title") for t in tools.TOOLS]
    assert len(titles) == len(set(titles))


def test_tools_list_carries_each_tools_title(monkeypatch):
    for tool in listed_tools(monkeypatch)["tools"]:
        assert set(tool) == {"name", "title", "description", "inputSchema", "annotations"}, tool["name"]
        assert tool["title"] == tools.BY_NAME[tool["name"]].get("title"), tool["name"]


def test_the_tool_list_stays_under_its_size_cap(monkeypatch):
    """The tools/list result, serialised by json.dumps with run_python included, stays under its size cap."""
    result = listed_tools(monkeypatch)
    assert len(result["tools"]) == len(tools.TOOLS)
    size = len(json.dumps(result).encode("utf-8"))
    largest = sorted(((len(json.dumps(t)), t["name"]) for t in result["tools"]), reverse=True)[:5]
    assert size <= TOOLS_LIST_MAX_BYTES, (f"tools/list is {size} bytes, over the cap of {TOOLS_LIST_MAX_BYTES}. "
                                          f"The largest tools: {largest}")


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t["name"])
def test_whether_a_tool_reads_writes_or_runs_is_explicit(tool):
    annotations = tool["annotations"]
    assert any(annotations is s for s in annotation_sets()), tool["name"]
    if annotations["readOnlyHint"]:
        # The MCP specification reads destructiveHint and idempotentHint only when readOnlyHint is false.
        assert set(annotations) == {"readOnlyHint", "openWorldHint"}, tool["name"]
    else:
        assert set(annotations) == {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}


def test_whether_a_tool_may_destroy_something_is_pinned_per_tool():
    actual = {t["name"]: t["annotations"].get("destructiveHint", False) for t in tools.TOOLS}
    assert actual == DESTRUCTIVE, {name: actual.get(name) for name in set(actual) | set(DESTRUCTIVE)
                                   if actual.get(name) != DESTRUCTIVE.get(name)}


def test_the_python_tools_may_do_anything_python_can():
    """Python reaches the file system and the network, so run_python and run_script are the open-world tools."""
    assert tools.ANY == {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False,
                         "openWorldHint": True}
    assert tools.BY_NAME["run_python"]["annotations"] is tools.ANY
    assert tools.BY_NAME["run_script"]["annotations"] is tools.ANY
    assert [t["name"] for t in tools.TOOLS if t["annotations"]["openWorldHint"]] == list(tools.PYTHON)


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t["name"])
def test_every_parameter_is_described_and_every_required_one_exists(tool):
    properties = tool["inputSchema"]["properties"]
    for name, spec in properties.items():
        assert spec.get("description"), f"{tool['name']}.{name} has no description"
    required = tool["inputSchema"].get("required")
    # An empty list of required parameters is left out rather than sent.
    assert required is None or (required and set(required) <= set(properties)), tool["name"]


@pytest.mark.parametrize("tool", tools.TOOLS, ids=lambda t: t["name"])
def test_a_schema_refuses_keys_it_does_not_name(tool):
    assert tool["inputSchema"].get("additionalProperties") is False, tool["name"]


def test_every_parameter_a_command_reads_is_in_its_schema():
    """Every parameter a command reads is in its schema.

    With additionalProperties false, a key that a command reads but its schema lacks could never be sent.
    """
    read = parameters_each_command_reads()
    # Each of these commands reads keys through helpers in the add-on's modules, so the walk must follow the
    # helpers into every module, or this test passes for nothing.
    assert {"radius", "location", "subdivisions", "points", "profile", "text", "bevel_resolution"} \
        <= read["create_object"]
    assert {"tolerance", "cuts", "direction", "keep", "fill"} <= read["edit_mesh"]
    assert "discard" in read["open_file"]
    assert {"attribute", "use_in_material", "facing"} <= read["set_vertex_color"]
    assert {"method", "uv_map", "margin"} <= read["unwrap_uv"]
    assert {"vertex_color", "alpha_mode", "normal_image"} <= read["set_material"]
    assert {"path", "args", "fresh", "discard"} <= read["run_script"]
    assert {"steps", "list"} <= read["undo_change"]
    assert {"copy"} <= read["save_file"]
    assert {"context", "views", "style", "ortho_scale"} <= read["render_preview"]
    assert {"clip", "keys", "loop", "interpolation"} <= read["animate_object"]
    assert {"shape", "naming", "ratio"} <= read["create_collision"]
    assert {"size", "samples", "uv_map", "from_objects", "path"} <= read["bake_texture"]
    assert {"path", "objects", "collection", "visible_only"} <= read["export_file"]
    missing = {name: sorted(keys - set(tools.BY_NAME[name]["inputSchema"]["properties"]))
               for name, keys in read.items()}
    assert not {name: keys for name, keys in missing.items() if keys}


def test_only_inspecting_tools_are_read_only():
    read_only = {t["name"] for t in tools.TOOLS if t["annotations"]["readOnlyHint"]}
    assert read_only == {"get_status", "get_scene", "get_object", "get_mesh", "list_materials", "check_asset",
                         "measure_objects"}


def test_names_are_unique():
    names = [t["name"] for t in tools.TOOLS]
    assert len(names) == len(set(names))


def test_every_forwarded_tool_has_a_blender_command_and_no_command_is_unreachable():
    forwarded = {t["name"] for t in tools.TOOLS} - tools.LOCAL
    assert blender_commands() == forwarded


def test_path_parameters_exist_in_their_schemas():
    for name in tools.PATH_PARAMS:
        for key in tools.path_keys(name):
            assert key in tools.BY_NAME[name]["inputSchema"]["properties"], f"{name} has no {key} parameter"
    assert tools.path_keys("set_material") == ("base_color_image", "normal_image", "emission_image")
    assert tools.path_keys("get_scene") == ()


def test_the_wire_protocol_number_is_the_same_on_both_sides():
    assert constant(ADDON / "commands.py", "PROTOCOL") == session.PROTOCOL


def test_the_default_port_is_the_same_on_both_sides():
    assert constant(ADDON / "__init__.py", "DEFAULT_PORT") == session.DEFAULT_PORT


def test_every_kind_the_blender_side_raises_is_one_the_server_knows():
    raised = set()
    for source in ADDON.glob("*.py"):
        raised |= set(re.findall(r'CommandError\(\s*"([a-z_]+)"', source.read_text(encoding="utf-8")))
        raised |= set(re.findall(r'"kind":\s*"([a-z_]+)"', source.read_text(encoding="utf-8")))
    assert raised, "no refusal kinds were found in the add-on's source, so the search patterns need updating"
    assert raised <= set(errors.KINDS), sorted(raised - set(errors.KINDS))


def test_the_instructions_name_every_refusal_kind_a_user_meets_first():
    for kind in ("not_found", "bad_argument", "not_connected", "connection_lost", "unsaved_changes", "deadline",
                 "blender_missing", "busy", "not_allowed"):
        assert f"`{kind}`" in server.SERVER_INSTRUCTIONS or kind in server.SERVER_INSTRUCTIONS, kind


def test_the_instructions_promise_run_python_only_when_enabled():
    """The server instructions offer run_python only "when enabled".

    BLENDERLENS_ALLOW_PYTHON=0 removes run_python from the tool list, and the add-on's preference refuses its calls.
    """
    mentions = [line for line in server.SERVER_INSTRUCTIONS.splitlines() if "run_python" in line]
    assert mentions
    for line in mentions:
        assert "when enabled" in line, line


def test_a_refusal_payload_carries_error_exit_kind_and_hint():
    payload = errors.ToolFailure("not_found", "No object named 'X'.").payload()
    assert payload == {"error": "No object named 'X'.", "exit": 2, "kind": "not_found",
                       "hint": errors.KINDS["not_found"][1]}
    custom = errors.ToolFailure("python_error", "boom", "look here", traceback="tb", stdout="").payload()
    assert custom["hint"] == "look here"
    assert custom["traceback"] == "tb"
    assert "stdout" not in custom, "empty extras were not left out"


def test_exit_codes_are_distinct():
    codes = [code for code, _ in errors.KINDS.values()]
    assert len(codes) == len(set(codes))
