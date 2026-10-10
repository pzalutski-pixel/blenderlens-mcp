"""Tests that a refused tool call changes nothing, run against a real background Blender.

Most tests build a small scene and save it, then take a snapshot of everything an agent can read about it:
get_scene, list_materials, get_object for each object, and the raw state, which covers every datablock, the
meshes, and the scene's own properties, frame, render and image settings. They then make a call that must be
refused, and compare. A refusal that leaves half its work behind shows up here as a difference, whatever kind
of refusal it is, and one that marks the saved file as changed shows up as unsaved changes. The other tests
check when a call marks the file as changed, and that numbers which are not finite come back as strict JSON.
"""

import json

import pytest

pytestmark = pytest.mark.integration

#: Code that returns the state the tools do not report but a half-applied call could change.
RAW = """
import hashlib
scene = bpy.context.scene
render = scene.render
image = render.image_settings
layer = bpy.context.view_layer

def plain(value):
    for method in ("to_dict", "to_list"):
        if hasattr(value, method):
            return getattr(value, method)()
    return value.name if isinstance(value, bpy.types.ID) else value

def digest(mesh):
    h = hashlib.md5()
    for v in mesh.vertices:
        h.update(("%.6f,%.6f,%.6f;" % tuple(v.co)).encode())
    for p in mesh.polygons:
        h.update((str(list(p.vertices)) + str(p.material_index) + str(p.use_smooth)).encode())
    return [len(mesh.vertices), len(mesh.edges), len(mesh.polygons), h.hexdigest()]

skipped = {"window_managers", "screens", "workspaces"}
result = {
    "datablocks": {kind: sorted(i.name for i in getattr(bpy.data, kind)) for kind in dir(bpy.data)
                   if kind not in skipped and isinstance(getattr(bpy.data, kind, None), bpy.types.bpy_prop_collection)},
    "meshes": {m.name: digest(m) for m in bpy.data.meshes},
    "slots": {o.name: [s.material.name if s.material else None for s in o.material_slots] for o in bpy.data.objects},
    "properties": {o.name: sorted(o.keys()) for o in bpy.data.objects},
    "hidden": sorted(o.name for o in bpy.data.objects if o.hide_get()),
    "modes": {o.name: o.mode for o in bpy.data.objects},
    "active": layer.objects.active.name if layer.objects.active else None,
    "selected": sorted(o.name for o in bpy.data.objects if o.select_get()),
    "scene_properties": {k: repr(plain(scene[k])) for k in scene.keys()},
    "frame": [scene.frame_current, scene.frame_subframe],
    "camera_world": [plain(scene.camera), plain(scene.world)],
    "render": [render.engine, render.resolution_x, render.resolution_y, render.resolution_percentage, render.filepath,
               render.film_transparent, render.use_border, render.use_crop_to_border, render.use_sequencer,
               render.use_compositing, render.use_multiview, render.use_single_layer, render.use_file_extension],
    "image": [image.file_format, image.color_mode, image.color_depth],
    "view_layers": [[v.name, v.use] for v in scene.view_layers],
}
"""


def snapshot(server) -> dict:
    """Return everything an agent can read about the open scene, and the raw state that RAW collects."""
    scene = server.ok("get_scene")
    # Unsaved changes are checked separately, right after the refusal and against the file saved just before
    # it, because a snapshot may be taken before that save.
    scene.pop("unsaved_changes")
    return {
        "scene": scene,
        "materials": server.ok("list_materials"),
        "objects": {o["name"]: server.ok("get_object", object=o["name"]) for o in scene["objects"]},
        "raw": server.ok("run_python", code=RAW)["result"],
    }


def same(before: dict, after: dict, what: str) -> None:
    """Assert that every part of the snapshot ``after`` equals the same part of ``before``, naming ``what`` if not."""
    for part in before:
        assert after[part] == before[part], (
            f"{what} changed {part}:\n"
            f"before {json.dumps(before[part])[:1500]}\nafter  {json.dumps(after[part])[:1500]}")


def refused_unchanged(server, tool, kind, /, **arguments) -> dict:
    """Make a call that must be refused with ``kind`` and must leave the file exactly as it was.

    The file is saved just before the call, so a refusal that marks it as changed is caught too. Return the
    refusal's payload.
    """
    before = snapshot(server)
    server.ok("save_file")
    payload = server.refused(tool, kind, **arguments)
    assert server.ok("get_scene")["unsaved_changes"] is False, \
        f"{tool} was refused ({payload['error']}) but marked the saved file as changed"
    same(before, snapshot(server), f"{tool}, refused ({payload['error']}),")
    return payload


@pytest.fixture
def scene(blender, tmp_path):
    """Return a server whose scene, saved to a file, holds a cube with a material and a bevel, and an empty."""
    blender.ok("create_object", kind="cube", name="Box", size=[2, 2, 2])
    blender.ok("set_material", material="Paint", object="Box", base_color=[0.2, 0.3, 0.4])
    blender.ok("add_modifier", object="Box", type="bevel", settings={"width": 0.1})
    blender.ok("create_object", kind="empty", name="Pivot", location=[3, 0, 0])
    blender.ok("save_file", path=str(tmp_path / "scene.blend"))
    return blender


# ---------------------------------------------------------------------------
# create_object and create_mesh
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("arguments", "kind"), [
    ({"rotation": "x"}, "bad_argument"),
    ({"rotation": [0, "x", 0]}, "bad_argument"),
    ({"location": [1, 2]}, "bad_argument"),
    ({"scale": "big"}, "bad_argument"),
    ({"parent": "NoSuchParent"}, "not_found"),
    ({"collection": 5}, "bad_argument"),
    ({"collection": "Fresh", "parent": "NoSuchParent"}, "not_found"),
    ({"name": 7}, "bad_argument"),
])
@pytest.mark.parametrize("shape", ["cube", "empty"])
def test_create_object_refused_leaves_no_object_mesh_or_collection(scene, shape, arguments, kind):
    refused_unchanged(scene, "create_object", kind, kind=shape, **arguments)


@pytest.mark.parametrize(("arguments", "kind"), [
    ({"rotation": [0, "x", 0]}, "bad_argument"),
    ({"parent": "NoSuchParent"}, "not_found"),
    ({"location": "here"}, "bad_argument"),
    ({"collection": "Fresh", "scale": [1, 1]}, "bad_argument"),
])
def test_create_mesh_refused_leaves_no_object_or_mesh(scene, arguments, kind):
    refused_unchanged(scene, "create_mesh", kind, name="Shard", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                      faces=[[0, 1, 2]], **arguments)


# ---------------------------------------------------------------------------
# set_material
# ---------------------------------------------------------------------------


def test_set_material_with_a_missing_face_changes_no_face_and_makes_no_material(scene):
    refused_unchanged(scene, "set_material", "bad_argument", material="Red", object="Box", base_color=[1, 0, 0],
                      faces=[99])


def test_set_material_refused_leaves_an_existing_material_as_it_was(scene):
    refused_unchanged(scene, "set_material", "bad_argument", material="Paint", object="Box", base_color=[1, 0, 0],
                      metallic=1, faces=[99])
    # No face of a cube points along the diagonal, so this selection is empty.
    refused_unchanged(scene, "set_material", "not_found", material="Paint", object="Box", base_color=[1, 0, 0],
                      facing=[1, 1, 1], tolerance=1)
    refused_unchanged(scene, "set_material", "bad_argument", material="Glass", object="Box", facing="top",
                      tolerance=500)


# ---------------------------------------------------------------------------
# set_object, set_transform and set_collection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arguments", [
    {"rename": "Renamed", "properties": {"b": 2 ** 40}},
    {"rename": "Renamed", "hide": True, "display": "SIDEWAYS"},
    # Nested values are accepted, but a null inside one is not.
    {"hide": True, "properties": {"ok": 1, "bad": {"nested": None}}},
    {"properties": {"ok": 1, "mixed": [True, 1]}},
    {"properties": {"ok": 1, "x" * 64: 1}},
    {"properties": {"ok": 1, "cycles": 1}},
    {"hide_render": True, "rename": 5},
    {"rename": "Renamed", "hide": "yes"},
])
def test_set_object_with_one_good_key_and_one_bad_key_changes_nothing(scene, arguments):
    refused_unchanged(scene, "set_object", "bad_argument", object="Box", **arguments)


@pytest.mark.parametrize("arguments", [
    {"location": [1, 2, 3], "scale": [1, "x", 1]},
    {"rotation": [0, 0, 45], "dimensions": [1, 2]},
    {"location": [1, 2, 3], "rotation": "tilted"},
])
def test_set_transform_with_one_bad_key_changes_nothing(scene, arguments):
    refused_unchanged(scene, "set_transform", "bad_argument", object="Box", **arguments)


def test_set_transform_refused_for_a_flat_axis_puts_back_what_it_set_first(scene):
    scene.ok("create_object", kind="plane", name="Sheet")
    refused_unchanged(scene, "set_transform", "bad_argument", object="Sheet", location=[5, 5, 5], scale=2,
                      dimensions=[1, 1, 1])


def test_set_collection_with_a_bad_parent_creates_no_collection(scene):
    refused_unchanged(scene, "set_collection", "bad_argument", objects=["Box"], collection="Fresh", parent=5)
    refused_unchanged(scene, "set_collection", "not_found", objects=["Box", "Ghost"], collection="Fresh")
    refused_unchanged(scene, "set_collection", "bad_argument", objects=["Box"], collection=5)


def test_a_collection_cannot_be_its_own_parent(scene):
    """A collection named as its own parent is refused, and no collection is left behind.

    Blender refuses the self-link only after the collection has been made, which would leave it behind.
    """
    refused_unchanged(scene, "set_collection", "bad_argument", objects=["Box"], collection="Loop", parent="Loop")


def test_a_collection_outside_the_scene_is_refused_with_a_hint_naming_parent(scene, tmp_path):
    """Putting objects into a collection that no scene shows is refused with a hint naming ``parent``.

    The objects stay in the scene, and naming a parent collection in the scene makes the same move succeed.
    """
    part = str(tmp_path / "part.glb")
    scene.ok("export_glb", path=part, objects=["Box"])
    scene.ok("run_python", code="bpy.data.collections.new('Shelf')")
    payload = refused_unchanged(scene, "set_collection", "bad_argument", objects=["Box"], collection="Shelf")
    assert "parent" in payload["hint"], payload
    refused_unchanged(scene, "set_collection", "bad_argument", objects=["Box"], collection="Shelf", keep_other=True)
    refused_unchanged(scene, "create_object", "bad_argument", kind="cube", name="Lost", collection="Shelf")
    refused_unchanged(scene, "import_file", "bad_argument", path=part, collection="Shelf")
    moved = scene.ok("set_collection", objects=["Box"], collection="Shelf", parent="Scene Collection")
    assert moved["objects"] == {"Box": ["Shelf"]}
    assert "Box" in [o["name"] for o in scene.ok("get_scene")["objects"]], \
        "Box left the scene: Shelf was not linked under its parent"


# ---------------------------------------------------------------------------
# The other commands that change the scene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arguments", [
    {"operation": "shade_smooth", "angle": "sharp"},
    {"operation": "inset", "facing": "top", "amount": 0.2, "depth": "deep"},
    {"operation": "extrude", "facing": "top", "amount": 1, "direction": "sideways"},
    {"operation": "scale", "facing": "top", "factor": 2, "pivot": [0, 0]},
    {"operation": "bevel", "amount": 0.1, "segments": 0},
])
def test_edit_mesh_refused_changes_no_geometry(scene, arguments):
    refused_unchanged(scene, "edit_mesh", "bad_argument", object="Box", **arguments)


def test_set_modifier_with_a_bad_key_keeps_the_settings_it_had(scene):
    refused_unchanged(scene, "set_modifier", "bad_argument", object="Box", modifier="Bevel",
                      settings={"width": 0.3}, index=99)
    refused_unchanged(scene, "set_modifier", "bad_argument", object="Box", modifier="Bevel",
                      settings={"width": 0.3, "segments": "many"})
    refused_unchanged(scene, "set_modifier", "bad_argument", object="Box", modifier="Bevel",
                      settings={"width": 0.3}, show="maybe")


def test_add_modifier_refused_leaves_no_modifier(scene):
    refused_unchanged(scene, "add_modifier", "bad_argument", object="Box", type="array",
                      settings={"count": 3, "relative_offset_displace": "far"})
    refused_unchanged(scene, "add_modifier", "bad_argument", object="Box", type="array", name=5)


def test_a_pointer_blender_silently_drops_is_refused(scene):
    """A modifier setting that Blender would silently drop is refused.

    A Boolean modifier takes only a mesh, so given an empty, Blender keeps None and the call would seem to succeed.
    """
    refused_unchanged(scene, "add_modifier", "bad_argument", object="Box", type="boolean",
                      settings={"object": "Pivot"})
    scene.ok("create_object", kind="cylinder", name="Cutter", radius=0.3, depth=4)
    scene.ok("add_modifier", object="Box", type="boolean", settings={"object": "Cutter"})
    payload = refused_unchanged(scene, "set_modifier", "bad_argument", object="Box", modifier="Boolean",
                                settings={"solver": "EXACT", "object": "Pivot"})
    assert "Pivot" in payload["error"]


def test_duplicate_and_delete_refused_change_nothing(scene):
    refused_unchanged(scene, "duplicate_object", "bad_argument", object="Box", offset=[1, 2])
    refused_unchanged(scene, "duplicate_object", "bad_argument", object="Box", name=5)
    refused_unchanged(scene, "delete_object", "not_found", objects=["Box", "Ghost"])


def partial_gltf() -> dict:
    """Return a glTF document with scene extras, whose first mesh imports and whose second reads past its buffer.

    The importer makes the first mesh with its material, image and texture, and sets the extras as custom
    properties of the open scene, before it fails on the second mesh.
    """
    import base64
    import struct

    positions = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    indices = struct.pack("<3H", 0, 1, 2) + b"\0\0"
    data = positions + indices
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJ"
                           "RU5ErkJggg==")
    return {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0, 1], "extras": {"injected_scene_prop": 42}}],
        "nodes": [{"name": "Good", "mesh": 0, "extras": {"tag": 1}}, {"name": "Bad", "mesh": 1}],
        "meshes": [
            {"name": "GoodMesh", "primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]},
            {"name": "BadMesh", "primitives": [{"attributes": {"POSITION": 2}, "indices": 1}]},
        ],
        "materials": [{"name": "ImpMat", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "textures": [{"source": 0}],
        "images": [{"uri": "data:image/png;base64," + base64.b64encode(png).decode(), "name": "ImpImage"}],
        "buffers": [{"byteLength": len(data),
                     "uri": "data:application/octet-stream;base64," + base64.b64encode(data).decode()}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36},
                        {"buffer": 0, "byteOffset": 36, "byteLength": 6},
                        {"buffer": 0, "byteOffset": 4000, "byteLength": 36}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]},
            {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"},
            {"bufferView": 2, "componentType": 5126, "count": 3, "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]},
        ],
    }


def test_import_refused_brings_nothing_in(scene, tmp_path):
    good = str(tmp_path / "good.glb")
    scene.ok("export_glb", path=good, objects=["Box"])
    refused_unchanged(scene, "import_file", "bad_argument", path=good, collection=5)
    broken = tmp_path / "broken.glb"
    broken.write_bytes(b"glTF" + b"\x02\x00\x00\x00" + b"\x00" * 40)
    refused_unchanged(scene, "import_file", "blender_error", path=str(broken))


def test_an_import_that_fails_after_making_datablocks_takes_them_all_back(scene, tmp_path):
    """An import that fails halfway, after the importer has made datablocks, takes them all back.

    The broken .glb in test_import_refused_brings_nothing_in fails before the importer makes anything, so it
    cannot show this.
    """
    partial = tmp_path / "partial.gltf"
    partial.write_text(json.dumps(partial_gltf()), encoding="utf-8")
    refused_unchanged(scene, "import_file", "blender_error", path=str(partial))
    refused_unchanged(scene, "import_file", "blender_error", path=str(partial), collection="Imported")


# ---------------------------------------------------------------------------
# A call that may have changed the file marks it as changed and, in live mode, gets its own undo step.
# A refusal that put everything back does neither.
# ---------------------------------------------------------------------------

#: Code that dispatches a refused call, a call refused halfway whose rollback fails, a call that succeeds and a call
#: that only reads, and returns how each ended, whether it left the file unsaved, and the undo steps pushed.
DISPATCH_LIVE = """
import sys
commands = sys.modules["blenderlens_addon.commands"]
common = sys.modules["blenderlens_addon.common"]

def half(params):
    # Refused after a change whose way back fails: the rollback cannot put everything back.
    with common.Rollback() as undo:
        undo.add(lambda: 1 / 0)
        raise common.CommandError("bad_argument", "refused after a change")

pushed = []
original = commands._after_change
commands._after_change = lambda command, refused=False, acted_on=None: pushed.append([command, refused])
commands.COMMANDS["half"] = (half, True)
calls = [("create_object", {"kind": "cube", "rotation": "x"}), ("half", {}),
         ("create_object", {"kind": "cube", "name": "Fine"}), ("get_scene", {})]
outcomes = []
try:
    for command, params in calls:
        commands.mark_saved()
        try:
            commands.dispatch(command, params, "live")
            outcome = "finished"
        except Exception as exc:
            outcome = type(exc).__name__
        outcomes.append([command, outcome, commands.unsaved()])
finally:
    commands._after_change = original
    del commands.COMMANDS["half"]
    commands.mark_saved()
result = {"pushed": pushed, "outcomes": outcomes}
"""


def test_an_undo_step_and_unsaved_changes_come_exactly_when_a_call_may_have_changed_the_file(scene):
    run = scene.ok("run_python", code=DISPATCH_LIVE)["result"]
    assert run["outcomes"] == [
        ["create_object", "CommandError", False],
        ["half", "CommandError", True],
        ["create_object", "finished", True],
        ["get_scene", "finished", False],
    ], "a call marked the file changed or unchanged wrongly; only a refusal that put everything back leaves it alone"
    assert run["pushed"] == [["half", True], ["create_object", False]], \
        "a refusal that could not put everything back is not its own undo step named as refused"


def test_a_refused_call_on_a_fresh_file_leaves_nothing_to_save(blender):
    """A refused call on a new file leaves nothing to save, so create_file afterwards needs no ``discard``."""
    blender.ok("create_file", discard=True)
    blender.refused("set_transform", "not_found", object="Ghost", location=[1, 2, 3])
    blender.refused("create_object", "bad_argument", kind="cube", rotation="x")
    assert blender.ok("get_scene")["unsaved_changes"] is False
    blender.ok("create_file")


def test_run_python_counts_as_a_change_even_when_it_raises(blender):
    blender.ok("create_file", discard=True)
    blender.refused("run_python", "python_error", code="bpy.data.objects.new('Half', None)\nraise ValueError('stop')")
    assert blender.ok("get_scene")["unsaved_changes"] is True


# ---------------------------------------------------------------------------
# Flags are true or false, never text that reads as either
# ---------------------------------------------------------------------------

#: Calls that give a flag as text, to a range of commands. The string "false" is not false, and
#: Python's bool("false") is true.
FLAG_CALLS = [
    ("delete_object", {"objects": ["Box"], "with_children": "false"}),
    ("duplicate_object", {"object": "Box", "linked": "no"}),
    ("duplicate_object", {"object": "Box", "with_children": "false"}),
    ("apply_transform", {"object": "Pivot", "location": "false"}),
    ("set_parent", {"object": "Pivot", "parent": "Box", "keep_transform": "false"}),
    ("set_collection", {"objects": ["Box"], "collection": "Fresh", "keep_other": "true"}),
    ("set_object", {"object": "Box", "rename": "Renamed", "rename_data": "false"}),
    ("set_modifier", {"object": "Box", "modifier": "Bevel", "show": "false"}),
    ("create_object", {"kind": "circle", "fill": "false"}),
    ("export_glb", {"path": "PATH", "visible_only": "false"}),
    ("export_glb", {"path": "PATH", "extras": "false"}),
    ("export_glb", {"path": "PATH", "apply_modifiers": "false"}),
    ("get_mesh", {"object": "Box", "evaluated": "true"}),
    ("get_mesh", {"object": "Box", "include_vertices": "false"}),
    ("render_preview", {"objects": ["Box"], "resolution": 16, "engine": "workbench", "path": "PNG",
                        "transparent": "false"}),
    ("create_file", {"discard": "false"}),
    ("create_file", {"discard": True, "empty": "false"}),
    ("open_file", {"path": "BLEND", "discard": "false"}),
]


def with_paths(arguments: dict, tmp_path) -> dict:
    """Return ``arguments`` with the placeholders PATH, PNG and BLEND replaced by paths in ``tmp_path``."""
    paths = {"PATH": str(tmp_path / "flag.glb"), "PNG": str(tmp_path / "flag.png"),
             "BLEND": str(tmp_path / "scene.blend")}
    return {key: paths.get(value, value) if isinstance(value, str) else value for key, value in arguments.items()}


def flag_id(tool: str, arguments: dict) -> str:
    """Return a test id naming the tool and the flag that a FLAG_CALLS entry gives as text."""
    return f"{tool}-{next(key for key, value in arguments.items() if value in ('true', 'false', 'no'))}"


@pytest.mark.parametrize(("tool", "arguments"), FLAG_CALLS, ids=[flag_id(*call) for call in FLAG_CALLS])
def test_a_flag_given_as_text_is_refused(scene, tmp_path, tool, arguments):
    payload = refused_unchanged(scene, tool, "bad_argument", **with_paths(arguments, tmp_path))
    # The server checks arguments against the schema before Blender sees them, and says what is accepted.
    assert "true or false" in payload["error"] + " " + payload["hint"], payload


def test_with_children_false_as_text_is_refused_and_the_children_survive(scene):
    """with_children given as the text "false" is refused, and the children stay."""
    scene.ok("create_object", kind="cube", name="Lid", parent="Box", location=[0, 0, 1.5], size=0.5)
    refused_unchanged(scene, "delete_object", "bad_argument", objects=["Box"], with_children="false")
    assert scene.ok("get_object", object="Lid")["parent"] == "Box"


#: Code that dispatches each of CALLS to the add-on's commands and returns how each ended: succeeded, or the kind of its
#: refusal.
DISPATCH_FLAGS = """
import sys
commands = sys.modules["blenderlens_addon.commands"]
outcomes = []
for command, params in CALLS:
    try:
        commands.dispatch(command, params, "background")
        outcomes.append([command, "succeeded"])
    except Exception as exc:
        outcomes.append([command, getattr(exc, "kind", type(exc).__name__)])
result = outcomes
"""


def test_the_addon_itself_refuses_a_flag_given_as_text(scene, tmp_path):
    """The add-on itself refuses a flag given as text, without relying on the server's schema check.

    A Blender window running the add-on can be reached without going through this server.
    """
    calls = [[tool, with_paths(arguments, tmp_path)] for tool, arguments in FLAG_CALLS]
    before = snapshot(scene)
    outcomes = scene.ok("run_python", code=f"CALLS = {calls!r}\n{DISPATCH_FLAGS}")["result"]
    assert [kind for _, kind in outcomes] == ["bad_argument"] * len(calls), outcomes
    same(before, snapshot(scene), "a flag given as text")


# ---------------------------------------------------------------------------
# Limits on each parameter
# ---------------------------------------------------------------------------


# Each value is over its maximum, but small enough that if the limit were missing, Blender would build the result
# within seconds and the test would fail quickly, rather than take minutes and gigabytes before failing.
@pytest.mark.parametrize(("tool", "arguments", "maximum"), [
    ("create_object", {"kind": "ico_sphere", "subdivisions": 9}, "8"),
    ("create_object", {"kind": "uv_sphere", "segments": 5000}, "1024"),
    ("create_object", {"kind": "uv_sphere", "rings": 2000}, "1024"),
    ("create_object", {"kind": "cylinder", "segments": 1025}, "1024"),
    ("create_object", {"kind": "circle", "segments": 100000}, "1024"),
    ("create_object", {"kind": "torus", "rings": 4096}, "1024"),
    ("create_object", {"kind": "grid", "subdivisions": 1001}, "1000"),
    ("edit_mesh", {"object": "Box", "operation": "subdivide", "cuts": 65}, "64"),
    ("edit_mesh", {"object": "Box", "operation": "bevel", "amount": 0.1, "segments": 100}, "64"),
])
def test_a_value_over_its_maximum_is_refused_naming_the_maximum(scene, tool, arguments, maximum):
    payload = refused_unchanged(scene, tool, "bad_argument", **arguments)
    assert maximum in payload["error"], payload["error"]


def test_create_mesh_is_bounded_in_vertices_and_faces(scene):
    payload = refused_unchanged(scene, "create_mesh", "bad_argument", name="Huge",
                                vertices=[[0, 0, 0]] * 1_000_001)
    assert "1000000" in payload["error"].replace(",", "")


def test_values_at_the_maximum_are_accepted(scene):
    made = scene.ok("create_object", kind="ico_sphere", subdivisions=8, radius=1)
    assert made["mesh"]["faces"] == 20 * 4 ** 7
    scene.ok("delete_object", objects=[made["name"]])
    scene.ok("edit_mesh", object="Box", operation="bevel", amount=0.01, segments=64, facing="top")


# ---------------------------------------------------------------------------
# Limits on the total work, not only on each parameter
# ---------------------------------------------------------------------------


# Each value is just over its maximum and is applied to the small Box, so that if the limit were missing,
# Blender would build the result in a second and the test would fail quickly.
@pytest.mark.parametrize(("modifier", "settings", "maximum"), [
    ("array", {"count": 10001}, "10000"),
    ("subdivision", {"levels": 7}, "6"),
    ("subdivision", {"render_levels": 7}, "6"),
    ("screw", {"steps": 1025}, "1024"),
    ("screw", {"render_steps": 1025}, "1024"),
    ("screw", {"iterations": 65}, "64"),
    ("remesh", {"octree_depth": 11}, "10"),
    ("bevel", {"segments": 65}, "64"),
    ("ocean", {"viewport_resolution": 33}, "32"),
    ("ocean", {"resolution": 33}, "32"),
    ("ocean", {"repeat_x": 65}, "64"),
])
def test_a_modifier_setting_over_its_maximum_is_refused_naming_it(scene, modifier, settings, maximum):
    payload = refused_unchanged(scene, "add_modifier", "bad_argument", object="Box", type=modifier,
                                settings=settings)
    assert maximum in payload["error"].replace(",", ""), payload["error"]
    assert next(iter(settings)) in payload["error"]


def test_an_array_of_three_hundred_thousand_is_refused_and_the_stack_is_unchanged(scene):
    """An array of 300,000 on a cube is refused before it is built, and the modifier stack is unchanged."""
    scene.ok("create_object", kind="cube", name="Plain", location=[0, 5, 0])
    refused_unchanged(scene, "add_modifier", "bad_argument", object="Plain", type="array",
                      settings={"count": 300000})
    scene.ok("add_modifier", object="Plain", type="array", settings={"count": 3})
    refused_unchanged(scene, "set_modifier", "bad_argument", object="Plain", modifier="Array",
                      settings={"count": 300000})
    assert scene.ok("get_object", object="Plain")["modifiers"][0]["settings"]["count"] == 3


def test_a_modifier_whose_result_would_be_too_big_is_refused_before_it_is_built(scene):
    """A modifier whose result would be too big is refused before it is built.

    A count within its maximum still multiplies what the mesh already holds.
    """
    scene.ok("create_object", kind="grid", name="Field", subdivisions=300)  # 90,000 faces
    payload = refused_unchanged(scene, "add_modifier", "bad_argument", object="Field", type="array",
                                settings={"count": 23})
    assert "2000000" in payload["error"].replace(",", ""), payload["error"]


def test_a_modifier_whose_built_result_is_too_big_is_taken_back_off(scene):
    """A modifier whose built result is too big is removed again, and the call is refused.

    A Mirror modifier at most doubles the mesh per axis, so it is not estimated ahead: its result is counted.
    """
    scene.ok("create_object", kind="grid", name="Field", subdivisions=600)  # 361,201 vertices
    payload = refused_unchanged(scene, "add_modifier", "bad_argument", object="Field", type="mirror",
                                settings={"use_axis": [True, True, False]})
    assert "1000000" in payload["error"].replace(",", ""), payload["error"]
    scene.ok("add_modifier", object="Field", type="mirror", settings={"use_axis": [True, False, False]})


def test_a_subdivide_whose_result_would_be_too_big_is_refused(scene):
    """A subdivide whose result would be too big is refused, though its cuts and its mesh are each within limits.

    Subdividing 2,500 faces with 28 cuts would make 2.1 million faces.
    """
    scene.ok("create_object", kind="grid", name="Field", subdivisions=50)
    payload = refused_unchanged(scene, "edit_mesh", "bad_argument", object="Field", operation="subdivide", cuts=28)
    assert "2000000" in payload["error"].replace(",", ""), payload["error"]
    scene.ok("edit_mesh", object="Field", operation="subdivide", cuts=3)


def test_a_bevel_whose_result_would_be_too_big_is_refused(scene):
    """A bevel whose result would be too big is refused. Without the limit, this bevel would take minutes."""
    scene.ok("create_object", kind="grid", name="Field", subdivisions=150)  # 45,300 edges
    payload = refused_unchanged(scene, "edit_mesh", "bad_argument", object="Field", operation="bevel",
                                amount=0.001, segments=45)
    assert "2000000" in payload["error"].replace(",", ""), payload["error"]


# ---------------------------------------------------------------------------
# Numbers beyond 1e9 either way, the limit on every number
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("tool", "arguments"), [
    ("create_object", {"kind": "cube", "size": 1e39}),
    ("create_object", {"kind": "uv_sphere", "radius": 1e39}),
    ("create_object", {"kind": "cube", "location": [2e9, 0, 0]}),
    ("set_transform", {"object": "Box", "scale": 1e39}),
    ("set_transform", {"object": "Box", "rotation": [1e300, 0, 0]}),
    ("set_transform", {"object": "Box", "dimensions": [1e39, None, None]}),
    ("edit_mesh", {"object": "Box", "operation": "translate", "facing": "top", "offset": [1e39, 0, 0]}),
    ("set_material", {"material": "Paint", "base_color": [1e39, 0, 0]}),
    ("set_modifier", {"object": "Box", "modifier": "Bevel", "settings": {"width": 1e39}}),
])
def test_a_number_too_large_for_blender_is_refused(scene, tool, arguments):
    """A number whose size is past 1e9 is refused, naming the limit.

    A billion metres is far more than any asset needs. A size of 1e39 is finite in Python but infinite as one of
    Blender's 32-bit floats, and 2e9, which a float holds, is past the limit too.
    """
    payload = refused_unchanged(scene, tool, "bad_argument", **arguments)
    assert "1e9" in payload["error"], payload["error"]


# ---------------------------------------------------------------------------
# No NaN or infinity in arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("tool", "arguments"), [
    ("set_transform", {"object": "Box", "scale": [float("inf"), 1, 1]}),
    ("set_transform", {"object": "Box", "location": [float("nan"), 0, 0]}),
    ("create_object", {"kind": "cube", "size": float("inf")}),
    ("create_object", {"kind": "uv_sphere", "radius": float("nan")}),
    ("create_mesh", {"name": "N", "vertices": [[0, 0, float("inf")], [1, 0, 0], [0, 1, 0]], "faces": [[0, 1, 2]]}),
    ("edit_mesh", {"object": "Box", "operation": "extrude", "facing": "top", "amount": float("inf")}),
    ("edit_mesh", {"object": "Box", "operation": "extrude", "facing": "top", "amount": 1,
                   "direction": [0, 0, float("nan")]}),
    ("edit_mesh", {"object": "Box", "operation": "extrude", "facing": "top", "amount": 1,
                   "tolerance": float("nan")}),
    ("set_material", {"material": "Paint", "base_color": [float("nan"), 0, 0]}),
    ("set_material", {"material": "Paint", "emission_strength": float("inf")}),
    ("set_modifier", {"object": "Box", "modifier": "Bevel", "settings": {"width": float("inf")}}),
    ("set_object", {"object": "Box", "properties": {"weight": float("nan")}}),
    ("set_object", {"object": "Box", "properties": {"weights": [1, float("inf")]}}),
    ("duplicate_object", {"object": "Box", "offset": [float("inf"), 0, 0]}),
    ("render_preview", {"objects": ["Box"], "azimuth": float("nan")}),
    ("render_preview", {"objects": ["Box"], "margin": float("inf")}),
])
def test_non_finite_numbers_are_refused(scene, tool, arguments):
    refused_unchanged(scene, tool, "bad_argument", **arguments)


def test_an_overflowing_literal_is_refused_and_the_response_is_strict_json(scene):
    """A literal of 1e999, valid JSON that parses to infinity, is refused, and the result is strict JSON."""
    scene.send_raw('{"jsonrpc":"2.0","id":9001,"method":"tools/call","params":{"name":"set_transform",'
                   '"arguments":{"object":"Box","scale":[1e999,1,1]}}}')
    response = scene.read()
    assert response["result"]["isError"] is True
    assert json.loads(response["result"]["content"][0]["text"])["kind"] == "bad_argument"
    assert scene.ok("get_object", object="Box")["scale"] == [1.0, 1.0, 1.0]


# ---------------------------------------------------------------------------
# No NaN or infinity in results
# ---------------------------------------------------------------------------


# The harness parses every line the server writes strictly (tests.conftest.strict_json).


@pytest.mark.parametrize("code", [
    "result = float('nan')",
    "result = Vector((float('nan'), float('inf'), -float('inf')))",
    "result = {'a': [float('inf')], 'm': Matrix.Identity(3) * float('nan')}",
])
def test_non_finite_results_come_back_as_strict_json(scene, code):
    scene.send_raw(json.dumps({"jsonrpc": "2.0", "id": 9002, "method": "tools/call",
                               "params": {"name": "run_python", "arguments": {"code": code}}}))
    response = scene.read()
    assert response["result"]["isError"] is False
    text = json.dumps(response["result"]["structuredContent"]["result"])
    assert "nan" in text or "inf" in text


def test_a_non_finite_transform_reads_back_as_strict_json(scene):
    scene.ok("run_python", code="bpy.data.objects['Pivot'].location = (float('nan'), float('inf'), 0)")
    scene.send_raw(json.dumps({"jsonrpc": "2.0", "id": 9003, "method": "tools/call",
                               "params": {"name": "get_object", "arguments": {"object": "Pivot"}}}))
    response = scene.read()
    # Blender keeps the NaN but clamps infinity to the largest 32-bit float.
    assert response["result"]["structuredContent"]["location"][0] == "nan"
