"""Tests for ``undo_change``, run against a real background Blender.

Every call that may have changed the file pushes one undo step named after it, in background mode as in live
mode, and undo_change steps back through them. A test that undoes checks what Blender holds afterwards, not only
the tool's result.
"""

import pytest

from tests.test_edit_mode import enter, leave
from tests.test_refusals_change_nothing import refused_unchanged

pytestmark = pytest.mark.integration


def faces(server, name):
    """Return how many faces the mesh of the object ``name`` has."""
    return server.ok("get_object", object=name)["mesh"]["faces"]


def steps(server):
    """Return the undo steps that undo_change lists, newest first."""
    return server.ok("undo_change", list=True)["steps"]


def test_an_extrude_undone_in_background_gives_the_faces_back(blender):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("edit_mesh", object="Box", operation="extrude", facing="top", amount=0.5)
    assert faces(blender, "Box") == 10
    assert steps(blender) == ["BlenderLens: edit_mesh Box", "BlenderLens: create_object Box"]
    out = blender.ok("undo_change")
    assert out["undone"] == ["BlenderLens: edit_mesh Box"]
    assert out["steps"] == ["BlenderLens: create_object Box"]
    assert out["counts"]["objects"] == 1 and out["counts"]["faces"] == 6
    assert out["unsaved_changes"] is True
    assert faces(blender, "Box") == 6
    assert blender.ok("get_object", object="Box")["dimensions"] == pytest.approx([2, 2, 2])


def test_the_first_change_after_a_new_file_can_be_undone(blender):
    """The first change after a new file can be undone.

    In background mode Blender pushes no undo step when a file loads, so one is pushed before the first change.
    """
    blender.ok("create_object", kind="cube", name="Box")
    assert blender.ok("undo_change")["undone"] == ["BlenderLens: create_object Box"]
    assert blender.ok("get_scene")["objects"] == []


def test_several_steps_undo_together_and_list_names_them_newest_first(blender):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("set_material", material="Paint", object="Box", base_color=[0.2, 0.3, 0.4])
    blender.ok("set_transform", object="Box", location=[1, 2, 3])
    assert steps(blender) == ["BlenderLens: set_transform Box", "BlenderLens: set_material Box",
                              "BlenderLens: create_object Box"]
    out = blender.ok("undo_change", steps=2)
    assert out["undone"] == ["BlenderLens: set_transform Box", "BlenderLens: set_material Box"]
    box = blender.ok("get_object", object="Box")
    assert box["location"] == pytest.approx([0, 0, 0])
    assert box["materials"] == []


def test_each_step_names_the_object_it_acted_on(blender):
    blender.ok("create_object", kind="cube", name="Plank_3")
    blender.ok("set_transform", object="Plank_3", location=[0, 0, 1])
    blender.ok("create_object", kind="cube", location=[3, 0, 0])
    blender.ok("join_objects", objects=["Plank_3", "Cube"])
    blender.ok("set_material", material="Wood")
    blender.refused("set_transform", "bad_argument", object="Plank_3", location=[0, 0, "up"])
    assert steps(blender) == ["BlenderLens: set_material", "BlenderLens: join_objects Plank_3 +1",
                              "BlenderLens: create_object Cube", "BlenderLens: set_transform Plank_3",
                              "BlenderLens: create_object Plank_3"]
    assert blender.ok("undo_change")["undone"] == ["BlenderLens: set_material"]


def test_a_second_undo_takes_back_the_step_before(blender):
    """A second undo_change takes back the step before the one the first took back.

    undo_change pushes no step of its own, because that step would be the next one undone, and undo nothing.
    """
    blender.ok("create_object", kind="cube", name="One")
    blender.ok("create_object", kind="cube", name="Two")
    blender.ok("undo_change")
    blender.ok("undo_change")
    assert blender.ok("get_scene")["objects"] == []


def test_reads_and_refusals_push_no_step(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("get_scene")
    blender.ok("get_mesh", object="Box")
    blender.ok("check_asset")
    blender.ok("render_preview", objects=["Box"], engine="cycles", samples=1, resolution=16,
               path=str(tmp_path / "p.png"))
    blender.ok("export_glb", path=str(tmp_path / "box.glb"))
    blender.refused("set_transform", "not_found", object="Ghost", location=[1, 2, 3])
    blender.refused("edit_mesh", "bad_argument", object="Box", operation="extrude", faces=[99])
    assert steps(blender) == ["BlenderLens: create_object Box"]


def test_listing_the_steps_leaves_a_saved_file_saved(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("save_file", path=str(tmp_path / "listed.blend"))
    blender.ok("undo_change", list=True)
    assert blender.ok("get_scene")["unsaved_changes"] is False


def test_undoing_back_to_the_last_save_leaves_the_file_saved(blender, tmp_path):
    """In background mode the file counts as saved again when undo returns to where it was saved, and only there."""
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("save_file", path=str(tmp_path / "saved.blend"))
    blender.ok("set_transform", object="Box", location=[1, 0, 0])
    blender.ok("set_transform", object="Box", location=[2, 0, 0])
    assert blender.ok("undo_change")["unsaved_changes"] is True, "one step short of the save is not marked unsaved"
    assert blender.ok("undo_change")["unsaved_changes"] is False
    assert blender.ok("get_scene")["unsaved_changes"] is False
    blender.ok("create_file")  # No unsaved work would be lost, so this is not refused.
    blender.ok("open_file", path=str(tmp_path / "saved.blend"))
    blender.ok("set_object", object="Box", rename="Crate")
    assert blender.ok("undo_change")["unsaved_changes"] is False, \
        "undoing back to the file as opened left it marked unsaved"
    blender.ok("set_object", object="Box", hide=True)
    assert blender.ok("get_scene")["unsaved_changes"] is True, "a new change after the undo is not marked unsaved"
    assert blender.ok("undo_change")["unsaved_changes"] is False


def test_close_after_undoing_back_to_the_save_needs_no_discard(mcp_process, blender_binary, tmp_path):
    from tests.test_sessions import background_env

    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    proc.ok("create_object", kind="cube", name="Box")
    proc.ok("save_file", path=str(tmp_path / "kept.blend"))
    proc.ok("set_transform", object="Box", location=[0, 0, 1])
    payload = proc.refused("close_session", "unsaved_changes")
    assert "save_file" in payload["hint"] and "kept.blend" in payload["hint"], payload["hint"]
    proc.ok("undo_change")
    assert proc.ok("close_session")["closed"] == "background"


def test_undoing_past_the_last_save_leaves_the_file_unsaved(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("set_transform", object="Box", location=[1, 0, 0])
    blender.ok("save_file", path=str(tmp_path / "saved.blend"))
    out = blender.ok("undo_change")
    assert out["undone"][0].startswith("BlenderLens: set_transform") and out["unsaved_changes"] is True
    assert blender.ok("get_scene")["unsaved_changes"] is True
    blender.refused("create_file", "unsaved_changes")


def test_a_new_or_opened_file_starts_its_steps_again(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("save_file", path=str(tmp_path / "box.blend"))
    blender.ok("create_file", discard=True)
    assert steps(blender) == []
    blender.refused("undo_change", "bad_argument")
    blender.ok("create_object", kind="cone", name="Cone")
    blender.ok("open_file", path=str(tmp_path / "box.blend"), discard=True)
    assert steps(blender) == []
    payload = blender.refused("undo_change", "bad_argument")
    assert "nothing to undo" in payload["error"].lower() or "0 steps" in payload["error"]
    assert [o["name"] for o in blender.ok("get_scene")["objects"]] == ["Box"], \
        "undo_change went back into the other file"


def test_more_steps_than_there_are_is_refused_and_undoes_nothing(blender):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("edit_mesh", object="Box", operation="extrude", facing="top", amount=0.5)
    payload = blender.refused("undo_change", "bad_argument", steps=3)
    assert "Only 2 steps" in payload["error"]
    assert "BlenderLens: edit_mesh Box" in payload["hint"]
    assert faces(blender, "Box") == 10


def test_a_refused_undo_changes_nothing(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("save_file", path=str(tmp_path / "scene.blend"))
    refused_unchanged(blender, "undo_change", "bad_argument", steps=40)


def test_names_holding_blender_data_are_cleared_from_run_python_after_an_undo(blender):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("run_python", code="box = bpy.data.objects['Box']\nparts = [box]\nsize = 2.0\nwhere = box.location\n"
                                  "copied = box.location.copy()\nCTX = bpy.context")
    blender.ok("create_object", kind="cone", name="Cone")
    out = blender.ok("undo_change")
    cleared = set(out["python_names_cleared"])
    assert {"box", "parts", "where"} <= cleared, cleared
    assert not {"size", "copied", "CTX"} & cleared, "undo_change cleared a value that stays valid after undo"
    kept = blender.ok("run_python", code="result = [size, list(copied), CTX.scene.name]")["result"]
    assert kept[0] == 2.0 and kept[1] == pytest.approx([0, 0, 0])
    payload = blender.refused("run_python", "python_error", code="result = box.name")
    assert "NameError" in payload["error"]


def test_undo_is_refused_in_edit_mode(blender):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("edit_mesh", object="Box", operation="extrude", facing="top", amount=0.5)
    enter(blender, "Box")
    payload = blender.refused("undo_change", "bad_argument")
    assert payload["hint"] == "Leave edit mode (Tab), then call again."
    leave(blender, "Box")
    assert faces(blender, "Box") == 10


def test_a_script_is_one_step_and_fresh_undoes_back_to_the_empty_file(blender, tmp_path):
    script = tmp_path / "two.py"
    script.write_text("for name in ('A', 'B'):\n"
                      "    bpy.context.scene.collection.objects.link(bpy.data.objects.new(name, None))\n",
                      encoding="utf-8")
    blender.ok("create_object", kind="cube", name="Before")
    blender.ok("run_script", path=str(script), fresh=True, discard=True)
    assert steps(blender) == ["BlenderLens: run_script"]
    blender.ok("undo_change")
    assert blender.ok("get_scene")["objects"] == []


def test_list_on_a_fresh_file_is_empty(blender):
    out = blender.ok("undo_change", list=True)
    assert out == {"steps": [], "can_undo": False, "by_name": 0}


#: Code that pushes two undo steps that are not BlenderLens's, around a move of the object G.
OWN_STEPS = """
bpy.ops.ed.undo_push(message="mine")
bpy.data.objects["G"].location.x = 5
bpy.ops.ed.undo_push(message="mine too")
"""


def test_undo_steps_that_run_python_code_pushes_stop_undo_by_name(blender):
    """Undo steps that run_python code pushes itself stop undo_change from going further by name.

    BlenderLens cannot read the names of steps that the agent's code pushes, so going past them needs ``force``.
    """
    blender.ok("create_object", kind="cube", name="G")
    blender.ok("run_python", code=OWN_STEPS)
    listed = blender.ok("undo_change", list=True)
    assert listed["steps"] == ["BlenderLens: run_python", "BlenderLens: create_object G"]
    assert listed["by_name"] == 1
    payload = blender.refused("undo_change", "bad_argument", steps=2)
    assert "force" in payload["hint"], payload
    assert blender.ok("get_object", object="G")["location"][0] == pytest.approx(5.0), \
        "the refused undo_change undid a step"


def test_force_undoes_steps_it_cannot_name_and_does_not_name_them(blender):
    blender.ok("create_object", kind="cube", name="G")
    blender.ok("run_python", code=OWN_STEPS)
    out = blender.ok("undo_change", steps=2, force=True)
    assert out["undone"][0] == "BlenderLens: run_python"
    assert not out["undone"][1].startswith("BlenderLens:"), out
    assert "note" in out


def test_a_change_made_outside_the_calls_stops_undo_by_name(blender):
    """A change made outside the tool calls stops undo_change from going further by name.

    In live mode the dependency graph reports edits made between calls. Here the add-on is told of one directly.
    """
    blender.ok("create_object", kind="cube", name="One")
    blender.ok("create_object", kind="cube", name="Two")
    blender.ok("run_python", code="import sys\nsys.modules['blenderlens_addon.undo'].note_outside()")
    assert blender.ok("undo_change", list=True)["by_name"] == 0
    payload = blender.refused("undo_change", "bad_argument")
    assert "force: true" in payload["hint"], payload
    assert {o["name"] for o in blender.ok("get_scene")["objects"]} == {"One", "Two"}


def test_a_file_opened_by_run_python_is_the_step_undo_comes_back_to(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Kept")
    blender.ok("save_file", path=str(tmp_path / "kept.blend"))
    blender.ok("create_file", discard=True)
    blender.ok("create_object", kind="cone", name="Elsewhere")
    blender.ok("run_python", code=f"bpy.ops.wm.open_mainfile(filepath={str(tmp_path / 'kept.blend')!r})")
    assert steps(blender) == [], "the file run_python opened has BlenderLens undo steps"
    blender.ok("create_object", kind="cube", name="Added")
    assert steps(blender) == ["BlenderLens: create_object Added"]
    blender.refused("undo_change", "bad_argument", steps=2)
    assert blender.ok("undo_change")["undone"] == ["BlenderLens: create_object Added"]
    assert [o["name"] for o in blender.ok("get_scene")["objects"]] == ["Kept"]
    blender.refused("undo_change", "bad_argument")


def test_a_run_python_that_only_reads_is_no_step_and_leaves_a_saved_file_saved(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("save_file", path=str(tmp_path / "box.blend"))
    read = blender.ok("run_python", code="result = [o.name for o in bpy.data.objects]")
    assert read["result"] == ["Box"]
    assert blender.ok("get_scene")["unsaved_changes"] is False
    assert steps(blender) == ["BlenderLens: create_object Box"]


@pytest.mark.parametrize("code", [
    "bpy.data.objects['Box'].location.x = 2",
    "bpy.data.objects['Box']['tag'] = 3",
    "bpy.context.scene['tag'] = 3",
    "bpy.data.texts.new('Notes')",
    "bpy.context.scene.frame_set(7)",
    "bpy.data.objects['Box'].data.vertices.add(1)",
], ids=["location", "custom_property", "scene_property", "text", "frame", "vertex_added"])
def test_a_run_python_that_changes_something_is_a_step_and_marks_the_file_changed(blender, tmp_path, code):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("save_file", path=str(tmp_path / "box.blend"))
    blender.ok("run_python", code=code)
    assert blender.ok("get_scene")["unsaved_changes"] is True
    assert steps(blender)[0] == "BlenderLens: run_python"


def test_names_holding_blender_data_deep_in_containers_and_attributes_are_cleared(blender):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("run_python", code="obj = bpy.data.objects['Box']\n"
                                  "nested = {'a': [obj]}\n"
                                  "deeper = [{'b': ({'c': obj.data},)}]\n"
                                  "class Holder:\n    pass\n"
                                  "holder = Holder()\nholder.target = obj\n"
                                  "plain = {'a': [1, 2, {'b': 'text'}]}\n"
                                  "plain_holder = Holder()\nplain_holder.size = 2")
    blender.ok("create_object", kind="cone", name="Cone")
    cleared = set(blender.ok("undo_change")["python_names_cleared"])
    assert {"obj", "nested", "deeper", "holder"} <= cleared, cleared
    assert not {"plain", "plain_holder", "Holder"} & cleared, cleared
