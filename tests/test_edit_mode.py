"""Tests for how the tools handle an object in edit mode, in a real background Blender.

In edit mode, Blender keeps the mesh being edited in a separate BMesh and copies it back into the mesh
data only when edit mode ends, so a read of the mesh data alone sees the shape from before the edit. The
tools must neither misreport the mesh nor take the object out of edit mode behind the user's back. Edit
mode is entered here the way a script in a Blender window would enter it, through ``run_python``, which
needs no window.
"""

import pytest

pytestmark = pytest.mark.integration

#: Code that makes the object NAME active and selected and puts it into edit mode, as a script would.
ENTER = """
obj = bpy.data.objects[NAME]
layer = bpy.context.view_layer
layer.objects.active = obj
obj.select_set(True)
with bpy.context.temp_override(active_object=obj, object=obj):
    bpy.ops.object.mode_set(mode="EDIT")
result = obj.mode
"""

#: Code that takes the object NAME back to object mode.
LEAVE = """
obj = bpy.data.objects[NAME]
with bpy.context.temp_override(active_object=obj, object=obj):
    bpy.ops.object.mode_set(mode="OBJECT")
result = obj.mode
"""


def enter(server, name):
    """Put the object ``name`` into edit mode through ``run_python``, as a script in Blender would."""
    assert server.ok("run_python", code=f"NAME = {name!r}\n{ENTER}")["result"] == "EDIT"


def leave(server, name):
    """Return the object ``name`` to object mode through ``run_python``."""
    assert server.ok("run_python", code=f"NAME = {name!r}\n{LEAVE}")["result"] == "OBJECT"


def mode(server, name):
    """Return the mode the object ``name`` is in, as Blender names it: "EDIT", "OBJECT" and so on."""
    return server.ok("run_python", code=f"result = bpy.data.objects[{name!r}].mode")["result"]


@pytest.fixture
def editing(blender):
    """Provide the server with a 2 m cube in edit mode, as it is after someone presses Tab in a Blender window."""
    blender.ok("create_object", kind="cube", name="Box", size=[2, 2, 2])
    enter(blender, "Box")
    return blender


def test_an_extrude_in_edit_mode_reports_the_new_counts(editing):
    out = editing.ok("edit_mesh", object="Box", operation="extrude", facing="top", amount=0.5)
    assert out["mesh"]["faces"] == 10, out["mesh"]
    assert out["mesh"]["vertices"] == 12
    assert out["dimensions"][2] == pytest.approx(2.5, abs=1e-3)
    assert mode(editing, "Box") == "EDIT", "the edit took the user out of edit mode"


def test_reads_in_edit_mode_see_the_edit(editing):
    editing.ok("edit_mesh", object="Box", operation="extrude", facing="top", amount=0.5)
    assert editing.ok("get_mesh", object="Box")["counts"]["faces"] == 10
    details = editing.ok("get_object", object="Box")
    assert details["mesh"]["faces"] == 10
    assert details["mesh_evaluated"]["faces"] == 10
    scene = editing.ok("get_scene")
    assert scene["objects"][0]["mesh"]["faces"] == 10
    assert mode(editing, "Box") == "EDIT"


def test_a_duplicate_made_in_edit_mode_copies_the_edited_mesh(editing):
    editing.ok("edit_mesh", object="Box", operation="extrude", facing="top", amount=0.5)
    copy = editing.ok("duplicate_object", object="Box", name="Copy")
    assert copy["mesh"]["faces"] == 10


@pytest.mark.parametrize(("tool", "arguments"), [
    ("export_glb", {"path": "PATH", "objects": ["Box"]}),
    ("export_glb", {"path": "PATH"}),
    ("apply_modifier", {"object": "Box", "modifier": "Bevel"}),
    ("apply_transform", {"object": "Box"}),
])
def test_export_and_apply_refuse_in_edit_mode_with_the_tab_hint(editing, tmp_path, tool, arguments):
    leave(editing, "Box")
    editing.ok("add_modifier", object="Box", type="bevel")
    enter(editing, "Box")
    arguments = {k: str(tmp_path / "e.glb") if v == "PATH" else v for k, v in arguments.items()}
    payload = editing.refused(tool, "bad_argument", **arguments)
    assert "leave edit mode (tab)" in payload["hint"].lower(), payload
    assert "Box" in payload["error"]
    assert mode(editing, "Box") == "EDIT", "the refusal took the user out of edit mode"
    assert not (tmp_path / "e.glb").exists()


def test_exporting_another_object_while_one_is_in_edit_mode_is_refused(editing, tmp_path):
    """Exporting another object while one is in edit mode is refused, and the edited object stays in edit mode.

    The glTF exporter would otherwise switch the active object to object mode and never back.
    """
    editing.ok("create_object", kind="cube", name="Other", location=[4, 0, 0])
    enter(editing, "Box")
    payload = editing.refused("export_glb", "bad_argument", path=str(tmp_path / "o.glb"), objects=["Other"])
    assert "leave edit mode (tab)" in payload["hint"].lower()
    assert mode(editing, "Box") == "EDIT"


def test_after_leaving_edit_mode_the_export_holds_the_edit(editing, tmp_path):
    editing.ok("edit_mesh", object="Box", operation="extrude", facing="top", amount=0.5)
    leave(editing, "Box")
    out = editing.ok("export_glb", path=str(tmp_path / "after.glb"), objects=["Box"])
    assert out["contents"]["triangles"] == 20


#: Code that returns the active object's name and the mode of the object NAME.
ACTIVE = """
layer = bpy.context.view_layer
result = [layer.objects.active.name if layer.objects.active else None, bpy.data.objects[NAME].mode]
"""


@pytest.mark.parametrize(("tool", "arguments"), [
    ("create_object", {"kind": "cube", "name": "New", "location": [4, 0, 0]}),
    ("create_object", {"kind": "empty", "name": "Marker", "location": [4, 0, 0]}),
    ("create_mesh", {"name": "Shard", "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0]], "faces": [[0, 1, 2]]}),
    ("duplicate_object", {"object": "Box", "name": "Copy", "offset": [4, 0, 0]}),
])
def test_building_beside_an_object_in_edit_mode_leaves_it_active_and_editing(editing, tool, arguments):
    """A new object leaves the object being edited active, so pressing Tab in Blender still leaves edit mode."""
    editing.ok(tool, **arguments)
    assert editing.ok("run_python", code=f"NAME = 'Box'\n{ACTIVE}")["result"] == ["Box", "EDIT"]


def test_building_beside_an_object_in_sculpt_mode_leaves_it_active(editing):
    leave(editing, "Box")
    run = editing.ok("run_python", code="NAME = 'Box'\n" + ENTER.replace('mode="EDIT"', 'mode="SCULPT"'))
    assert run["result"] == "SCULPT"
    editing.ok("create_object", kind="cube", name="New", location=[4, 0, 0])
    assert editing.ok("run_python", code=f"NAME = 'Box'\n{ACTIVE}")["result"] == ["Box", "SCULPT"]


def test_import_in_edit_mode_is_refused_with_the_tab_hint(editing, tmp_path):
    """Importing while an object is in edit mode is refused, and the object stays in edit mode.

    The importer would otherwise switch the edited object to object mode behind the user's back.
    """
    leave(editing, "Box")
    path = str(tmp_path / "part.glb")
    editing.ok("export_glb", path=path, objects=["Box"])
    enter(editing, "Box")
    payload = editing.refused("import_file", "bad_argument", path=path)
    assert "leave edit mode (tab)" in payload["hint"].lower(), payload
    assert mode(editing, "Box") == "EDIT"
    assert [o["name"] for o in editing.ok("get_scene")["objects"]] == ["Box"]


def test_a_refused_edit_in_edit_mode_changes_nothing(editing):
    editing.refused("edit_mesh", "bad_argument", object="Box", operation="extrude", facing="top", amount=1,
                    direction="sideways")
    editing.refused("edit_mesh", "bad_argument", object="Box", operation="shade_smooth", angle="sharp")
    assert editing.ok("get_mesh", object="Box")["counts"]["faces"] == 6
    leave(editing, "Box")
    smooth = editing.ok("run_python", code="result = [p.use_smooth for p in bpy.data.objects['Box'].data.polygons]")
    assert smooth["result"] == [False] * 6


def test_shade_smooth_with_an_angle_in_edit_mode_survives_leaving_it(editing):
    editing.ok("edit_mesh", object="Box", operation="shade_smooth", angle=30)
    leave(editing, "Box")
    state = editing.ok("run_python", code=(
        "mesh = bpy.data.objects['Box'].data\n"
        "result = [all(p.use_smooth for p in mesh.polygons), all(e.use_edge_sharp for e in mesh.edges)]"))
    assert state["result"] == [True, True], "a face is not smooth, or a 90-degree edge is not sharp"


def test_shade_smooth_with_an_angle_in_object_mode_marks_only_sharper_edges(blender):
    blender.ok("create_object", kind="cylinder", name="Can", segments=16)
    blender.ok("edit_mesh", object="Can", operation="shade_smooth", angle=30)
    sharp = blender.ok("run_python", code=(
        "mesh = bpy.data.objects['Can'].data\n"
        "result = sum(1 for e in mesh.edges if e.use_edge_sharp)"))["result"]
    assert sharp == 32, "the sharp edges are not the 32 rim edges of the caps, the only ones sharper than 30 degrees"
