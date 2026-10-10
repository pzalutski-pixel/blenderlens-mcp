"""Tests for checkpoints and for appending from a .blend, in a real background Blender.

A checkpoint is ``save_file`` with ``copy``: a copy written beside the work, while the session's file keeps
its path and its unsaved state. An append is ``import_file`` from a .blend: it brings named objects with their
children and what they use, collections with their objects, or materials, from another .blend into this one.
"""

import pytest

from tests.test_refusals_change_nothing import refused_unchanged

pytestmark = pytest.mark.integration


def names(server):
    """Return the sorted names of the objects in the open file."""
    return sorted(o["name"] for o in server.ok("get_scene")["objects"])


# ---------------------------------------------------------------------------
# save_file copy
# ---------------------------------------------------------------------------


def test_a_checkpoint_leaves_the_session_on_its_file_and_the_copy_opens(blender, tmp_path):
    work, checkpoint = tmp_path / "work.blend", tmp_path / "work.ck1.blend"
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("save_file", path=str(work))
    blender.ok("create_object", kind="cone", name="Cone")
    out = blender.ok("save_file", path=str(checkpoint), copy=True)
    assert out["path"] == str(checkpoint) and out["bytes"] > 0
    assert out["copy"] is True and out["file"] == str(work)
    assert out["unsaved_changes"] is True, "the checkpoint marked the work saved, but the cone is not in work.blend"
    status = blender.ok("get_status")
    assert status["session"]["file"] == str(work)
    assert blender.ok("get_scene")["unsaved_changes"] is True

    blender.ok("open_file", path=str(checkpoint), discard=True)
    assert names(blender) == ["Box", "Cone"]
    blender.ok("open_file", path=str(work))
    assert names(blender) == ["Box"], "the checkpoint wrote to work.blend itself"


def test_a_checkpoint_of_a_file_never_saved_keeps_it_unsaved(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box")
    out = blender.ok("save_file", path=str(tmp_path / "ck.blend"), copy=True)
    assert out["file"] is None and out["unsaved_changes"] is True
    blender.refused("create_file", "unsaved_changes")


def test_a_checkpoint_needs_its_own_path(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box")
    blender.refused("save_file", "bad_argument", copy=True)
    blender.ok("save_file", path=str(tmp_path / "work.blend"))
    payload = blender.refused("save_file", "bad_argument", path=str(tmp_path / "work.blend"), copy=True)
    assert "open file itself" in payload["error"]
    blender.refused("save_file", "bad_argument", path=str(tmp_path / "ck.txt"), copy=True)


# ---------------------------------------------------------------------------
# import_file from a .blend
# ---------------------------------------------------------------------------

#: Code that saves the .blend to append from. It holds a table whose drawer and lamp are its children, the lamp with a
#: shade of its own, each with a material, the drawer's the same as the table's. It also holds a vase in a collection
#: named ``Shelf``, a material that nothing uses (kept by a fake user), and a stool on its own.
SOURCE = """
import bmesh
def box(name, x, size, parent=None, material=None):
    bm = bmesh.new(); bmesh.ops.create_cube(bm, size=size)
    mesh = bpy.data.meshes.new(name); bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(name, mesh)
    obj.location.x = x
    obj.parent = parent
    if material:
        mesh.materials.append(bpy.data.materials.get(material) or bpy.data.materials.new(material))
    return obj
scene = bpy.context.scene
table = box("Table", 0, 1.2, None, "Oak")
scene.collection.objects.link(table)
for obj in (box("Drawer", 0.2, 0.4, table, "Oak"), box("Lamp", -0.3, 0.2, table, "Brass"),
            box("Stool", 4, 0.5, None, "Pine")):
    scene.collection.objects.link(obj)
scene.collection.objects.link(box("Shade", 0, 0.3, bpy.data.objects["Lamp"], "Linen"))
shelf = bpy.data.collections.new("Shelf")
scene.collection.children.link(shelf)
shelf.objects.link(box("Vase", 7, 0.25, None, "Ceramic"))
bpy.data.materials.new("Velvet").use_fake_user = True
bpy.ops.wm.save_as_mainfile(filepath=PATH)
"""


@pytest.fixture
def source(blender, tmp_path):
    """Save the .blend to append from, start an empty file to append into, and return the .blend's path."""
    path = tmp_path / "props.blend"
    blender.ok("run_python", code=f"PATH = {str(path)!r}\n{SOURCE}")
    blender.ok("create_file", discard=True)
    return path


def test_appending_an_object_brings_its_children_and_their_materials(blender, source):
    out = blender.ok("import_file", path=str(source), objects=["Table"])
    assert sorted(o["name"] for o in out["objects"]) == ["Drawer", "Lamp", "Shade", "Table"]
    assert sorted(out["materials"]) == ["Brass", "Linen", "Oak"]
    assert out["collections"] == [] and out["libraries"] == []
    assert names(blender) == ["Drawer", "Lamp", "Shade", "Table"]
    assert blender.ok("get_object", object="Shade")["parent"] == "Lamp"
    assert blender.ok("get_object", object="Lamp")["materials"][0]["material"] == "Brass"
    assert sorted(m["name"] for m in blender.ok("list_materials")["materials"]) == ["Brass", "Linen", "Oak"]
    assert blender.ok("run_python", code="result = len(bpy.data.libraries)")["result"] == 0, \
        "the objects were linked, not appended"


def test_appending_a_collection_brings_its_objects_into_the_scene(blender, source):
    out = blender.ok("import_file", path=str(source), collections=["Shelf"])
    assert out["collections"] == ["Shelf"]
    assert names(blender) == ["Vase"]
    assert blender.ok("get_object", object="Vase")["collections"] == ["Shelf"]


def test_appending_a_material_brings_it_alone(blender, source):
    out = blender.ok("import_file", path=str(source), materials=["Velvet"])
    assert out["materials"] == ["Velvet"] and out["objects"] == []
    assert names(blender) == []


def test_what_is_appended_goes_into_the_collection_named(blender, source):
    blender.ok("import_file", path=str(source), objects=["Stool"], collection="Reference")
    assert blender.ok("get_object", object="Stool")["collections"] == ["Reference"]


def test_appending_twice_names_the_second_copy_apart(blender, source):
    blender.ok("import_file", path=str(source), objects=["Stool"])
    out = blender.ok("import_file", path=str(source), objects=["Stool"])
    assert [o["name"] for o in out["objects"]] == ["Stool.001"]
    assert names(blender) == ["Stool", "Stool.001"]


def test_a_name_the_file_lacks_is_refused_naming_what_it_holds(blender, source, tmp_path):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("save_file", path=str(tmp_path / "scene.blend"))
    payload = refused_unchanged(blender, "import_file", "not_found", path=str(source), objects=["Table", "Ghost"])
    assert "Ghost" in payload["error"]
    assert "Table" in payload["hint"] and "Vase" in payload["hint"]
    left = blender.ok("run_python", code="result = [len(bpy.data.libraries), len(bpy.data.meshes)]")["result"]
    assert left == [0, 1], "the refused call left part of the file behind"


def test_nothing_named_is_refused_listing_what_the_file_holds(blender, source):
    payload = blender.refused("import_file", "bad_argument", path=str(source))
    for name in ("Table", "Shelf", "Velvet"):
        assert name in payload["hint"]
    assert names(blender) == []


def test_names_to_append_are_only_for_a_blend(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("export_glb", path=str(tmp_path / "box.glb"))
    payload = blender.refused("import_file", "bad_argument", path=str(tmp_path / "box.glb"), objects=["Box"])
    assert ".blend" in payload["error"]


def test_an_append_that_fails_partway_takes_everything_back(blender, source, tmp_path):
    """When the call fails after the append itself has run, everything the append brought is removed again.

    The test makes looking up the target collection fail, so the call is refused only after the append.
    """
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("save_file", path=str(tmp_path / "scene.blend"))
    blender.ok("run_python", code="import sys\ncommands = sys.modules['blenderlens_addon.commands']\n"
                                  "original = commands.get_collection\n"
                                  "def failing(*a, **k):\n"
                                  "    raise commands.CommandError('blender_error', 'stand-in')\n"
                                  "commands.get_collection = failing")
    try:
        refused_unchanged(blender, "import_file", "blender_error", path=str(source), objects=["Table"],
                          collection="Display")
    finally:
        blender.ok("run_python", code="commands.get_collection = original")
