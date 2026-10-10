"""Tests for the file tools in a real Blender: .glb export read back from disk, imports, and saving and opening.

The export tests judge what was exported by the written file, read through ``glb.summarize`` and through
Blender's own glTF importer, not by Blender's scene, because what matters is what an engine such as Godot
will load.
"""

import json

import pytest

from blenderlens_mcp.glb import summarize

pytestmark = pytest.mark.integration


@pytest.fixture
def prop(blender):
    """Build a prop: a wooden cabinet with a screen set into its front, a handle parented to it, and a bevel.

    The screen is a second material, which glows.
    """
    blender.ok("create_object", kind="cube", name="Cabinet", size=[1, 0.5, 1.6], location=[0, 0, 0.8])
    screen = blender.ok("edit_mesh", object="Cabinet", operation="inset", facing="front", amount=0.1)
    blender.ok("set_material", material="Oak", object="Cabinet", base_color="#a67b5b", metallic=0.0, roughness=0.6)
    blender.ok("set_material", material="Screen", object="Cabinet", faces=screen["result_faces"],
               emission_color="#cfe8ff", emission_strength=4)
    blender.ok("add_modifier", object="Cabinet", type="bevel", settings={"width": 0.02, "segments": 2})
    blender.ok("create_object", kind="cylinder", name="Handle", radius=0.02, depth=0.3, segments=12,
               location=[0.55, 0, 0.8], parent="Cabinet")
    return blender


def test_export_writes_a_glb_and_reads_its_contents_back(prop, tmp_path):
    out = prop.ok("export_glb", path=str(tmp_path / "prop.glb"))
    assert out["bytes"] > 1000
    contents = out["contents"]
    assert contents["valid"] is True
    assert contents["up_axis"] == "Y"
    root = contents["nodes"][0]
    assert root["name"] == "Cabinet"
    assert root["children"][0]["name"] == "Handle"
    materials = {m["name"]: m for m in contents["materials"]}
    assert materials["Screen"]["emissive"] is True
    assert materials["Screen"]["emissive_strength"] == pytest.approx(4.0)
    assert materials["Oak"]["metallic"] == pytest.approx(0.0, abs=1e-4)
    assert materials["Oak"]["roughness"] == pytest.approx(0.6, abs=1e-4)
    assert "KHR_materials_emissive_strength" in contents["extensions_used"]
    cabinet = next(m for m in contents["meshes"] if m["name"] == "Cabinet")
    assert cabinet["triangles"] > 20, "the bevel modifier was not applied on export"
    assert cabinet["bounds_yup"]["max"][1] == pytest.approx(0.8, abs=1e-3), "Blender's Z did not become glTF's Y"
    assert summarize(out["path"]) == contents, "the result is not exactly what the file holds"


def test_export_without_modifiers_keeps_the_base_mesh(prop, tmp_path):
    out = prop.ok("export_glb", path=str(tmp_path / "raw.glb"), apply_modifiers=False, objects=["Cabinet"])
    cabinet = next(m for m in out["contents"]["meshes"] if m["name"] == "Cabinet")
    assert cabinet["triangles"] == 20


def node_names(contents) -> set:
    """Return the name of every node in the scene tree of a read-back's ``contents``, at any depth."""
    names = set()
    stack = list(contents["nodes"])
    while stack:
        node = stack.pop()
        names.add(node["name"])
        stack.extend(node.get("children", []))
    return names


def test_hidden_objects_stay_out_unless_asked(prop, tmp_path):
    prop.ok("create_object", kind="cylinder", name="Cutter", location=[3, 0, 0])
    prop.ok("set_object", object="Cutter", hide=True)
    shown = prop.ok("export_glb", path=str(tmp_path / "a.glb"))
    assert "Cutter" not in shown["objects"]
    assert "Cutter" not in node_names(shown["contents"])
    everything = prop.ok("export_glb", path=str(tmp_path / "b.glb"), visible_only=False)
    assert "Cutter" in node_names(everything["contents"]), "the file does not hold the hidden cutter"
    assert set(everything["objects"]) == node_names(everything["contents"]), "the result misstates what the file holds"
    assert everything["contents"]["warnings"] == []
    assert prop.ok("get_object", object="Cutter")["visible"] is False, "the export left the cutter visible"


#: Code that hides objects in every way the viewport allows, and sets a selection the export must leave alone.
HIDE_EVERY_WAY = """
layer = bpy.context.view_layer
objects = bpy.data.objects
objects["ByHide"].hide_set(True)
objects["ByViewport"].hide_viewport = True
objects["ByUnselectable"].hide_select = True
layer.layer_collection.children["Excluded"].exclude = True
layer.layer_collection.children["EyeClosed"].hide_viewport = True
bpy.data.collections["Disabled"].hide_viewport = True
layer.update()
for obj in layer.objects:
    obj.select_set(False)
objects["Active"].select_set(True)
layer.objects.active = objects["Active"]
"""

#: Code that returns each way of hiding that ``HIDE_EVERY_WAY`` sets, the selection, the active object and the current
#: frame.
HIDDEN_STATE = """
layer = bpy.context.view_layer
objects = bpy.data.objects
scene = bpy.context.scene
result = {
    "hide": objects["ByHide"].hide_get(),
    "viewport": objects["ByViewport"].hide_viewport,
    "unselectable": objects["ByUnselectable"].hide_select,
    "excluded": layer.layer_collection.children["Excluded"].exclude,
    "eye": layer.layer_collection.children["EyeClosed"].hide_viewport,
    "disabled": bpy.data.collections["Disabled"].hide_viewport,
    "locked": bpy.data.collections["Locked"].hide_select,
    "selected": sorted(o.name for o in objects if o.select_get()),
    "active": layer.objects.active.name if layer.objects.active else None,
    "frame": [scene.frame_current, round(scene.frame_subframe, 4)],
}
"""


@pytest.fixture
def hidden_every_way(blender):
    """Build objects hidden in every way the viewport allows, plus a selection and a frame the export must keep.

    Return the server and the state recorded before any export.
    """
    blender.ok("create_object", kind="cube", name="Shown")
    blender.ok("create_object", kind="cube", name="Active", location=[0, -3, 0])
    for i, name in enumerate(("ByHide", "ByViewport", "ByUnselectable")):
        blender.ok("create_object", kind="cube", name=name, size=0.5, location=[2 * (i + 1), 0, 0])
    for name, collection in (("InExcluded", "Excluded"), ("InEyeClosed", "EyeClosed"), ("InDisabled", "Disabled"),
                             ("InLocked", "Locked")):
        blender.ok("create_object", kind="cube", name=name, size=0.5, collection=collection, location=[0, 3, 0])
    blender.ok("run_python", code=HIDE_EVERY_WAY + "\nbpy.data.collections['Locked'].hide_select = True\n"
                                                   "bpy.context.scene.frame_set(7, subframe=0.25)")
    before = blender.ok("run_python", code=HIDDEN_STATE)["result"]
    assert before["selected"] == ["Active"] and before["active"] == "Active" and before["frame"] == [7, 0.25]
    return blender, before


def test_visible_only_false_exports_objects_hidden_every_way_and_puts_them_back(hidden_every_way, tmp_path):
    blender, before = hidden_every_way
    visible = blender.ok("export_glb", path=str(tmp_path / "visible.glb"))
    assert node_names(visible["contents"]) == {"Shown", "Active", "ByUnselectable", "InLocked"}, \
        "a visible object was left out because it or its collection is unselectable"
    assert visible["contents"]["warnings"] == []
    assert blender.ok("run_python", code=HIDDEN_STATE)["result"] == before

    everything = blender.ok("export_glb", path=str(tmp_path / "all.glb"), visible_only=False)
    wanted = {"Shown", "Active", "ByHide", "ByViewport", "ByUnselectable", "InExcluded", "InEyeClosed", "InDisabled",
              "InLocked"}
    assert node_names(everything["contents"]) == wanted
    assert set(everything["objects"]) == wanted
    assert everything["contents"]["warnings"] == []
    assert blender.ok("run_python", code=HIDDEN_STATE)["result"] == before, "a way of hiding was not put back"


def test_a_failed_export_puts_everything_back(hidden_every_way, tmp_path):
    """When the exporter fails, every hidden object, the selection and the frame are restored.

    Writing to a folder makes the exporter fail after the export has revealed and selected everything.
    """
    blender, before = hidden_every_way
    folder = tmp_path / "folder.glb"
    folder.mkdir()
    blender.refused("export_glb", "blender_error", path=str(folder), visible_only=False)
    assert blender.ok("run_python", code=HIDDEN_STATE)["result"] == before, "the failed export did not put it all back"


def test_a_visible_object_in_an_unselectable_collection_is_exported(blender, tmp_path):
    """A visible object in a collection that cannot be selected is still written to the file, with no warning."""
    blender.ok("create_object", kind="cube", name="Body")
    blender.ok("create_object", kind="cube", name="Locked", location=[3, 0, 0], collection="Fixtures")
    blender.ok("run_python", code="bpy.data.collections['Fixtures'].hide_select = True")
    out = blender.ok("export_glb", path=str(tmp_path / "locked.glb"))
    assert node_names(out["contents"]) == {"Body", "Locked"}
    assert out["contents"]["warnings"] == []
    assert blender.ok("run_python", code="result = bpy.data.collections['Fixtures'].hide_select")["result"] is True


def test_export_a_collection_with_extras(blender, tmp_path):
    blender.ok("create_object", kind="cylinder", name="Vase", radius=0.1, depth=0.3, collection="Shelf")
    blender.ok("create_object", kind="cube", name="Elsewhere", location=[3, 0, 0])
    blender.ok("set_object", object="Vase", properties={"capacity_l": 1.5})
    out = blender.ok("export_glb", path=str(tmp_path / "shelf.glb"), collection="Shelf", extras=True)
    assert out["objects"] == ["Vase"]
    assert out["contents"]["nodes"] == [{"name": "Vase", "mesh": "Vase", "extras": {"capacity_l": 1.5}}]


def test_custom_properties_export_as_extras_by_default(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Clock", size=0.3)
    blender.ok("set_object", object="Clock", properties={"chimes": 12, "style": "mantel"})
    out = blender.ok("export_glb", path=str(tmp_path / "default.glb"))
    assert out["contents"]["nodes"][0]["extras"] == {"chimes": 12, "style": "mantel"}
    off = blender.ok("export_glb", path=str(tmp_path / "off.glb"), extras=False)
    assert "extras" not in off["contents"]["nodes"][0]


@pytest.mark.parametrize("owner", ["object", "mesh", "material", "scene"])
def test_a_custom_property_that_is_not_finite_refuses_the_export_naming_it(blender, tmp_path, owner):
    """A custom property holding a non-finite number refuses the export and is named in the error.

    The exporter cannot write such a value as extras.
    """
    blender.ok("create_object", kind="cylinder", name="Kettle", radius=0.1, depth=0.2)
    blender.ok("set_material", material="Ceramic", object="Kettle")
    where = {"object": "bpy.data.objects['Kettle']", "mesh": "bpy.data.meshes['Kettle']",
             "material": "bpy.data.materials['Ceramic']", "scene": "bpy.context.scene"}[owner]
    blender.ok("run_python", code=f"{where}['capacity'] = {{'limits': [0.0, float('inf')]}}")
    payload = blender.refused("export_glb", "bad_argument", path=str(tmp_path / "inf.glb"))
    assert "capacity" in payload["error"] and ("Kettle" in payload["error"] or "Ceramic" in payload["error"]
                                               or "Scene" in payload["error"]), payload
    assert not (tmp_path / "inf.glb").exists()
    blender.ok("export_glb", path=str(tmp_path / "inf.glb"), extras=False)


def test_hidden_custom_properties_stay_out_of_the_extras(blender, tmp_path):
    """Custom properties whose names start with ``_`` stay out of the extras, and stay unchanged in the .blend.

    Such names belong to Blender and to add-ons (``_RNA_UI``, for example), not to the asset.
    """
    blender.ok("create_object", kind="cube", name="Book", size=[0.15, 0.04, 0.22])
    blender.ok("set_material", material="Paper", object="Book")
    blender.ok("run_python", code="bpy.data.objects['Book']['_RNA_UI'] = {'pages': {'min': 1}}\n"
                                  "bpy.data.materials['Paper']['_hidden'] = 1\n"
                                  "bpy.data.objects['Book']['pages'] = 320")
    out = blender.ok("export_glb", path=str(tmp_path / "hidden.glb"))
    assert out["contents"]["nodes"][0]["extras"] == {"pages": 320}
    raw = (tmp_path / "hidden.glb").read_bytes()
    assert b"_RNA_UI" not in raw and b"_hidden" not in raw
    kept = blender.ok("run_python", code="result = sorted(bpy.data.objects['Book'].keys())")["result"]
    assert kept == ["_RNA_UI", "pages"], "the export changed the object's own properties"


#: Code that links a new cube named Late into the scene, then raises before anything brings the view layer up to date.
LINK_WITHOUT_UPDATE = """
import bmesh
mesh = bpy.data.meshes.new("Late")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
bm.to_mesh(mesh)
bm.free()
bpy.context.scene.collection.objects.link(bpy.data.objects.new("Late", mesh))
raise RuntimeError("stop before anything brings the view layer up to date")
"""

#: Code that links a new cube named Later into the scene and calls the add-on's export_glb at once, and returns the
#: export's result and whether the view layer held the cube before the call.
EXPORT_WITHOUT_UPDATE = """
import bmesh, sys
commands = sys.modules["blenderlens_addon.commands"]
mesh = bpy.data.meshes.new("Later")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
bm.to_mesh(mesh)
bm.free()
bpy.context.scene.collection.objects.link(bpy.data.objects.new("Later", mesh))
in_layer = "Later" in bpy.context.view_layer.objects
exported = commands.export_glb({"path": PATH, "objects": ["Later"], "visible_only": False})
result = {"in_layer_before": in_layer, "exported": exported}
"""


def test_an_object_linked_without_an_update_is_exported_by_name(blender, tmp_path):
    """An object linked to the scene without a view layer update is still exported when it is named.

    The view layer's object list lags behind linking until something brings it up to date. In a file that
    holds nothing else, nothing does, and an export based on the selection then writes an empty file while
    reporting FINISHED. With ``visible_only`` false, the export skips ``visible_get``, which would bring the
    list up to date by chance.
    """
    blender.refused("run_python", "python_error", code=LINK_WITHOUT_UPDATE)
    out = blender.ok("export_glb", path=str(tmp_path / "late.glb"), objects=["Late"], visible_only=False)
    assert node_names(out["contents"]) == {"Late"}
    assert out["contents"]["triangles"] == 12

    blender.ok("create_file", discard=True)
    path = str(tmp_path / "later.glb")
    run = blender.ok("run_python", code=f"PATH = {json.dumps(path)}\n{EXPORT_WITHOUT_UPDATE}")["result"]
    assert run["in_layer_before"] is False, "the view layer was not behind, so the test proves nothing"
    assert node_names(summarize(path)) == {"Later"}


#: Code that returns the selected objects and the active object.
SELECTION = """
layer = bpy.context.view_layer
result = [sorted(o.name for o in bpy.data.objects if o.select_get()),
          layer.objects.active.name if layer.objects.active else None]
"""


@pytest.mark.parametrize("tool", ["create_object", "duplicate_object"])
def test_a_new_object_in_an_otherwise_empty_file_is_selected_and_active(blender, tool):
    if tool == "create_object":
        blender.ok("create_object", kind="cube", name="Only")
    else:
        blender.refused("run_python", "python_error", code=LINK_WITHOUT_UPDATE)
        blender.ok("duplicate_object", object="Late", name="Only")
        blender.ok("delete_object", objects=["Late"])
    assert blender.ok("run_python", code=SELECTION)["result"] == [["Only"], "Only"]
    blender.ok("create_object", kind="cube", name="Next")
    assert blender.ok("run_python", code=SELECTION)["result"] == [["Next"], "Next"]


def test_an_object_the_exporter_leaves_out_is_named_in_the_warnings(blender, tmp_path):
    """An object that the glTF exporter skips is named in the read-back's ``missing_objects`` warning.

    The exporter skips anything in a collection named ``glTF_not_exported``, whatever it was asked to export.
    """
    blender.ok("create_object", kind="cube", name="Body")
    blender.ok("create_object", kind="cube", name="Skipped", collection="glTF_not_exported", location=[3, 0, 0])
    out = blender.ok("export_glb", path=str(tmp_path / "skipped.glb"))
    assert "Skipped" in out["objects"]
    assert node_names(out["contents"]) == {"Body"}
    missing = [w for w in out["contents"]["warnings"] if w["code"] == "missing_objects"]
    assert missing and missing[0]["objects"] == ["Skipped"], out["contents"]["warnings"]
    assert "Skipped" in missing[0]["message"]


def test_export_refuses_a_wrong_extension_and_an_empty_selection(blender, tmp_path):
    blender.refused("export_glb", "bad_argument", path=str(tmp_path / "x.gltf"))
    blender.refused("export_glb", "not_found", path=str(tmp_path / "x.glb"))


def test_a_relative_path_lands_beside_the_server(blender, server_cwd):
    blender.ok("create_object", kind="cube", name="Rel")
    out = blender.ok("export_glb", path="exports/rel.glb")
    assert out["path"] == str(server_cwd / "exports" / "rel.glb")
    assert (server_cwd / "exports" / "rel.glb").is_file()


def test_reimporting_the_export_gives_back_the_parts_and_materials(prop, tmp_path):
    path = str(tmp_path / "round_trip.glb")
    prop.ok("export_glb", path=path)
    prop.ok("create_file", discard=True)
    imported = prop.ok("import_file", path=path, collection="Imported")
    names = sorted(o["name"] for o in imported["objects"])
    assert names == ["Cabinet", "Handle"]
    assert sorted(imported["materials"]) == ["Oak", "Screen"]
    cabinet = next(o for o in imported["objects"] if o["name"] == "Cabinet")
    assert cabinet["collections"] == ["Imported"]
    assert cabinet["dimensions"][0] == pytest.approx(1.0, abs=1e-3)
    screen = next(m for m in prop.ok("list_materials")["materials"] if m["name"] == "Screen")
    assert screen["emission_strength"] == pytest.approx(4.0)


@pytest.mark.parametrize(("extension", "exporter"), [
    (".obj", "bpy.ops.wm.obj_export(filepath=PATH, export_selected_objects=False)"),
    (".fbx", "bpy.ops.export_scene.fbx(filepath=PATH)"),
    (".stl", "bpy.ops.wm.stl_export(filepath=PATH)"),
])
def test_import_obj_fbx_and_stl(blender, tmp_path, extension, exporter):
    path = str(tmp_path / f"ball{extension}")
    blender.ok("create_object", kind="ico_sphere", name="Ball", radius=1.5)
    blender.ok("run_python", code=f"PATH = {json.dumps(path)}\n{exporter}")
    blender.ok("create_file", discard=True)
    imported = blender.ok("import_file", path=path)
    assert len(imported["objects"]) == 1
    assert imported["objects"][0]["dimensions"][2] == pytest.approx(3.0, rel=0.05)


def test_import_refuses_missing_and_unknown_files(blender, tmp_path):
    blender.refused("import_file", "not_found", path=str(tmp_path / "ghost.glb"))
    odd = tmp_path / "notes.txt"
    odd.write_text("hello", encoding="utf-8")
    payload = blender.refused("import_file", "bad_argument", path=str(odd))
    assert ".fbx" in payload["hint"]


def test_save_open_and_the_unsaved_guard(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Keep")
    blend = tmp_path / "work" / "keep.blend"
    saved = blender.ok("save_file", path=str(blend))
    assert saved["path"] == str(blend) and saved["unsaved_changes"] is False and blend.is_file()

    blender.ok("create_object", kind="cube", name="Extra")
    payload = blender.refused("open_file", "unsaved_changes", path=str(blend))
    assert "discard" in payload["hint"]
    blender.refused("create_file", "unsaved_changes")

    resaved = blender.ok("save_file")
    assert resaved["path"] == str(blend)
    blender.ok("create_file")
    opened = blender.ok("open_file", path=str(blend))
    assert sorted(o["name"] for o in opened["objects"]) == ["Extra", "Keep"]
    assert opened["file"] == str(blend)
    assert opened["unsaved_changes"] is False


def test_saving_over_a_file_in_background_leaves_no_blend1_backup(blender, tmp_path):
    keep = "result = (bpy.context.preferences.filepaths.save_version, bpy.context.preferences.is_dirty)"
    preferences = blender.ok("run_python", code=keep)["result"]
    blender.ok("create_object", kind="cube", name="Keep")
    blend = tmp_path / "items.blend"
    blender.ok("save_file", path=str(blend))
    blender.ok("set_transform", object="Keep", location=[1, 0, 0])
    blender.ok("save_file")
    blender.ok("save_file", path=str(blend))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["items.blend"]
    assert blender.ok("run_python", code=keep)["result"] == preferences, "saving changed the preferences"


def test_open_file_with_discard_drops_the_changes(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Saved")
    blend = str(tmp_path / "a.blend")
    blender.ok("save_file", path=blend)
    blender.ok("create_object", kind="cube", name="Dropped")
    opened = blender.ok("open_file", path=blend, discard=True)
    assert [o["name"] for o in opened["objects"]] == ["Saved"]


def test_save_needs_a_path_the_first_time_and_a_blend_extension(blender, tmp_path):
    payload = blender.refused("save_file", "bad_argument")
    assert "path" in payload["hint"]
    blender.refused("save_file", "bad_argument", path=str(tmp_path / "x.glb"))


def test_create_file_with_the_default_scene(blender):
    scene = blender.ok("create_file", empty=False, discard=True)
    assert {o["name"] for o in scene["objects"]} == {"Cube", "Camera", "Light"}
    assert scene["unsaved_changes"] is False
