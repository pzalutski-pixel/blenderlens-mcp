"""Tests for live mode, in a real Blender window.

The add-on is installed and started from its own operator, its timer serves the socket, and the MCP
server attaches in live mode. The tests open a Blender window (small, behind other windows, and without
focus), so they run only when asked:

    BLENDERLENS_TEST_LIVE=1 pytest -m live

The add-on is installed into a throwaway user folder, as in ``test_addon_install``.
"""

import os
import subprocess
import time

import pytest

from blenderlens_mcp import __version__
from blenderlens_mcp.cli import build_addon
from tests.conftest import MCPProcess, free_port, isolated_blender_env, png_info

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("BLENDERLENS_TEST_LIVE") != "1",
                       reason="This test opens a Blender window. Set BLENDERLENS_TEST_LIVE=1 to run it."),
]

#: Code that Blender runs as it starts: a timer sets the add-on's port to PORT a second later and presses Start.
START = r"""
import bpy
def _start():
    prefs = bpy.context.preferences.addons["bl_ext.user_default.blenderlens"].preferences
    prefs.port = PORT
    bpy.ops.blenderlens.start()
    return None
bpy.app.timers.register(_start, first_interval=1.0)
"""


@pytest.fixture(scope="module")
def live(blender_binary, tmp_path_factory):
    """Install the add-on, open a Blender window that starts it, and attach a server in live mode.

    Provide the server, the test's folder and the Blender process.
    """
    root = tmp_path_factory.mktemp("live")
    env = isolated_blender_env(root / "user")
    archive = build_addon(str(root / "blenderlens.zip"))
    install = subprocess.run([blender_binary, "--command", "extension", "install-file", "--repo", "user_default",
                              "--enable", str(archive)], env=env, capture_output=True, text=True, timeout=180)
    assert install.returncode == 0, install.stdout + install.stderr
    port = free_port()
    log = open(root / "blender.log", "wb")
    # No --factory-startup, because it skips the user preferences, where the add-on is enabled. The
    # throwaway user folder has no startup file, so the scene is Blender's default anyway.
    blender = subprocess.Popen([blender_binary, "--no-window-focus", "--window-geometry", "40", "40", "900", "600",
                                "--python-expr", START.replace("PORT", str(port))],
                               env=env, stdout=log, stderr=subprocess.STDOUT)
    server = MCPProcess(cwd=str(root), env={"BLENDERLENS_MODE": "live", "BLENDERLENS_PORT": str(port),
                                            "BLENDER_BIN": blender_binary,
                                            "BLENDERLENS_RENDER_DIR": str(root / "renders")})
    server.initialize()
    deadline = time.time() + 120
    while not server.ok("get_status")["live_addon"]["responding"]:
        if blender.poll() is not None or time.time() > deadline:
            blender.kill()
            server.close()
            pytest.fail("the add-on never responded:\n" + (root / "blender.log").read_text(errors="replace")[-2000:])
        time.sleep(1)
    try:
        yield server, root, blender
    finally:
        server.close()
        blender.terminate()
        try:
            blender.wait(timeout=20)
        except subprocess.TimeoutExpired:
            blender.kill()
        log.close()


def test_the_server_attaches_in_live_mode(live):
    server, _, _ = live
    status = server.ok("get_status")
    assert status["live_addon"]["responding"] is True
    assert status["live_addon"]["addon_version"] == __version__
    scene = server.ok("get_scene")
    assert {"Cube", "Camera", "Light"} <= {o["name"] for o in scene["objects"]}, "the scene is not the Blender window's"
    assert server.ok("get_status")["session"]["mode"] == "live"


def test_building_exporting_and_rendering_in_live_mode(live):
    server, root, _ = live
    server.ok("create_object", kind="cube", name="Nightlight", size=[1, 1, 0.5], location=[3, 0, 0.25])
    inset = server.ok("edit_mesh", object="Nightlight", operation="inset", facing="top", amount=0.1)
    server.ok("set_material", material="Shade", object="Nightlight", faces=inset["result_faces"],
              emission_color="#66ccff", emission_strength=5)
    exported = server.ok("export_glb", path=str(root / "nightlight.glb"), objects=["Nightlight"])
    assert exported["contents"]["materials"][-1]["emissive"] is True
    shot = server.ok("render_preview", objects=["Nightlight"], resolution=128)
    assert png_info(shot["path"])["width"] == 128


def test_each_change_is_an_undo_step_in_live_mode(live):
    server, _, _ = live
    server.ok("create_object", kind="cube", name="UndoMe")
    undo = ("w = bpy.context.window_manager.windows[0]\n"
            "with bpy.context.temp_override(window=w):\n"
            "    bpy.ops.ed.undo()\n"
            "result = 'UndoMe' in bpy.data.objects")
    assert server.ok("run_python", code=undo)["result"] is False, "one undo did not remove the object"


#: Code that links an object named Half into the scene and then raises, as a call that fails halfway does.
HALF_DONE = """
mesh = bpy.data.meshes.new("Half")
bpy.context.scene.collection.objects.link(bpy.data.objects.new("Half", mesh))
raise RuntimeError("stops halfway, after linking an object")
"""

#: Code that presses Ctrl+Z once in the Blender window and returns which of NAMES still exist.
UNDO_ONCE = """
w = bpy.context.window_manager.windows[0]
with bpy.context.temp_override(window=w):
    bpy.ops.ed.undo()
result = {name: name in bpy.data.objects for name in NAMES}
"""


def test_a_refused_call_is_its_own_undo_step_in_live_mode(live):
    """A call that fails halfway is an undo step of its own, so its half-change is never folded into the next step.

    The add-on pushes the undo step in a ``finally`` block, whatever happened during the call.
    """
    server, _, _ = live
    server.ok("create_object", kind="cube", name="KeptBefore", location=[0, 6, 0])
    server.refused("run_python", "python_error", code=HALF_DONE)
    server.refused("create_object", "bad_argument", kind="cube", name="Never", rotation="x")
    server.ok("create_object", kind="cube", name="GoodAfter", location=[0, 9, 0])
    names = ["KeptBefore", "Half", "Never", "GoodAfter"]
    present = server.ok("run_python", code=f"NAMES = {names!r}\n{UNDO_ONCE}")["result"]
    assert present == {"KeptBefore": True, "Half": True, "Never": False, "GoodAfter": False}, \
        "one Ctrl+Z did not remove the good call alone, so the half-done call is not its own step"


#: Code that makes the object NAME active and switches it to the mode MODE in the Blender window.
EDIT_MODE = """
obj = bpy.data.objects[NAME]
w = bpy.context.window_manager.windows[0]
bpy.context.view_layer.objects.active = obj
obj.select_set(True)
with bpy.context.temp_override(window=w, active_object=obj, object=obj):
    bpy.ops.object.mode_set(mode=MODE)
result = obj.mode
"""


def test_edit_mode_in_live_mode_is_read_and_refused_correctly(live):
    """In live mode, a mesh in edit mode is read with its edit, and exporting it is refused until edit mode ends."""
    server, root, _ = live
    server.ok("create_object", kind="cube", name="Tabbed", size=[1, 1, 1], location=[0, -6, 0])
    assert server.ok("run_python", code=f"NAME = 'Tabbed'\nMODE = 'EDIT'\n{EDIT_MODE}")["result"] == "EDIT"
    out = server.ok("edit_mesh", object="Tabbed", operation="extrude", facing="top", amount=0.5)
    assert out["mesh"]["faces"] == 10
    assert server.ok("get_mesh", object="Tabbed")["counts"]["faces"] == 10
    payload = server.refused("export_glb", "bad_argument", path=str(root / "tabbed.glb"), objects=["Tabbed"])
    assert "leave edit mode (tab)" in payload["hint"].lower()
    assert server.ok("run_python", code="result = bpy.data.objects['Tabbed'].mode")["result"] == "EDIT"
    assert server.ok("run_python", code=f"NAME = 'Tabbed'\nMODE = 'OBJECT'\n{EDIT_MODE}")["result"] == "OBJECT"
    exported = server.ok("export_glb", path=str(root / "tabbed.glb"), objects=["Tabbed"])
    assert exported["contents"]["triangles"] == 20


#: Code that presses Tab in the Blender window and returns the active object before it and the mode of each of NAMES
#: after it.
TAB = """
w = bpy.context.window_manager.windows[0]
layer = bpy.context.view_layer
active = layer.objects.active.name if layer.objects.active else None
with bpy.context.temp_override(window=w):
    bpy.ops.object.editmode_toggle()
result = {"active": active, "modes": {name: bpy.data.objects[name].mode for name in NAMES}}
"""


def test_building_beside_an_object_in_edit_mode_keeps_tab_on_it_in_live_mode(live):
    """A new object leaves the edited object active, so pressing Tab in the Blender window still leaves edit mode."""
    server, _, _ = live
    server.ok("create_object", kind="cube", name="Carved", location=[0, -9, 0])
    assert server.ok("run_python", code=f"NAME = 'Carved'\nMODE = 'EDIT'\n{EDIT_MODE}")["result"] == "EDIT"
    server.ok("create_object", kind="cube", name="Beside", location=[3, -9, 0])
    server.ok("duplicate_object", object="Beside", name="Twin", offset=[3, 0, 0])
    names = ["Carved", "Beside", "Twin"]
    pressed = server.ok("run_python", code=f"NAMES = {names!r}\n{TAB}")["result"]
    assert pressed == {"active": "Carved", "modes": {"Carved": "OBJECT", "Beside": "OBJECT", "Twin": "OBJECT"}}


def test_a_drop_and_an_origin_are_one_undo_step_each_and_a_check_pushes_none_in_live_mode(live):
    """In live mode, a drop and an origin change are one Ctrl+Z each, and a check or a measure pushes no step."""
    server, _, _ = live
    server.ok("create_object", kind="plane", name="LiveFloor", size=[4, 4, 1], location=[20, 0, 0])
    server.ok("create_object", kind="cube", name="LiveCrate", size=1, location=[20, 0, 3])
    assert server.ok("set_transform", object="LiveCrate", drop=True)["landed_on"] == "LiveFloor"
    server.ok("apply_transform", object="LiveCrate", origin="bottom_center")
    assert server.ok("get_object", object="LiveCrate")["location"][2] == pytest.approx(0.0, abs=1e-5)
    server.ok("check_asset", objects=["LiveCrate"])
    server.ok("measure_objects", objects=["LiveCrate", "LiveFloor"])
    # Both undos happen in one call, because run_python is itself an undo step, pushed after it runs.
    undo = ("w = bpy.context.window_manager.windows[0]\n"
            "heights = []\n"
            "for _ in range(2):\n"
            "    with bpy.context.temp_override(window=w):\n"
            "        bpy.ops.ed.undo()\n"
            "    heights.append(bpy.data.objects['LiveCrate'].location[2])\n"
            "result = heights")
    heights = server.ok("run_python", code=undo)["result"]
    assert heights == pytest.approx([0.5, 3.0], abs=1e-5), \
        "the origin and the drop are not one undo step each, or the check or the measure pushed a step"


def test_a_refused_call_leaves_a_saved_file_saved_in_live_mode(live):
    """In live mode, a refusal that put everything back leaves a saved file saved, because it pushes no undo step.

    In a Blender window, an undo step itself marks the file as modified.
    """
    server, root, _ = live
    server.ok("create_object", kind="cube", name="Clean", location=[0, 12, 0])
    server.ok("save_file", path=str(root / "clean.blend"))
    assert server.ok("get_scene")["unsaved_changes"] is False
    server.refused("set_transform", "bad_argument", object="Clean", location=[1, 2])
    server.refused("create_object", "bad_argument", kind="cube", rotation="x")
    server.refused("set_collection", "bad_argument", objects=["Clean"], collection="Loop", parent="Loop")
    assert server.ok("get_scene")["unsaved_changes"] is False
    server.ok("create_file")


def test_saving_starting_afresh_and_reopening_in_live_mode(live):
    """Saving, starting a new file and reopening all work in live mode, and the bridge keeps serving afterwards.

    File loads run from the add-on's timer, and the bridge must survive them.
    """
    server, root, _ = live
    server.ok("create_object", kind="cube", name="Saved")
    blend = str(root / "live.blend")
    server.ok("save_file", path=blend)
    fresh = server.ok("create_file")
    assert fresh["objects"] == []
    reopened = server.ok("open_file", path=blend)
    assert "Saved" in {o["name"] for o in reopened["objects"]}
    assert server.ok("get_status")["session"]["file"] == blend


#: Code that returns the active object, the mode of the object NAME, the UV sync setting and the object's UVs.
UV_STATE = """
layer = bpy.context.view_layer
me = bpy.data.objects[NAME].data
result = {"active": layer.objects.active.name if layer.objects.active else None, "mode": bpy.data.objects[NAME].mode,
          "sync": bpy.context.scene.tool_settings.use_uv_select_sync,
          "uv": [round(c, 4) for d in me.uv_layers["UVMap"].data for c in d.uv]}
"""


def test_unwrapping_and_joining_in_live_mode_are_one_undo_step_each(live):
    """In live mode, an unwrap and a join are one undo step each, and the unwrap restores the editing state.

    The UV operators need edit mode, so the object enters it for the call. Afterwards the object is back in
    object mode, the active object and the UV sync setting are as they were, and one Ctrl+Z takes the unwrap back.
    """
    server, _, _ = live
    server.ok("create_object", kind="cube", name="Crated", location=[0, 15, 0])
    server.ok("create_object", kind="cube", name="Lidded", location=[3, 15, 0])
    before = server.ok("run_python", code=f"NAME = 'Crated'\n{UV_STATE}")["result"]
    assert server.ok("unwrap_uv", object="Crated", method="smart")["islands"] == 6
    # run_python counts as a change and pushes its own undo step, so the state after the unwrap is read in the
    # same call that undoes it. The one Ctrl+Z then lands on the unwrap.
    read_then_undo = (f"NAME = 'Crated'\n{UV_STATE}\nafter = result\nNAMES = []\n{UNDO_ONCE}\n{UV_STATE}\n"
                      "result = {'after': after, 'undone': result}")
    states = server.ok("run_python", code=read_then_undo)["result"]
    after, undone = states["after"], states["undone"]
    assert (after["active"], after["mode"], after["sync"]) == (before["active"], "OBJECT", before["sync"])
    assert after["uv"] != before["uv"]
    assert undone["uv"] == before["uv"]
    joined = server.ok("join_objects", objects=["Crated", "Lidded"])
    assert joined["joined"] == ["Lidded"]
    present = server.ok("run_python", code=f"NAMES = {['Crated', 'Lidded']!r}\n{UNDO_ONCE}")["result"]
    assert present == {"Crated": True, "Lidded": True}, "one Ctrl+Z did not give back the joined piece"


#: Code that adds a sphere named LiveBall in a collection hidden in the viewport.
HIDDEN_BALL = """
collection = bpy.data.collections.new("LiveHidden")
bpy.context.scene.collection.children.link(collection)
mesh = bpy.data.meshes.new("LiveBall")
bm = bmesh.new()
bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=1)
bm.to_mesh(mesh)
bm.free()
ball = bpy.data.objects.new("LiveBall", mesh)
ball.location = (0, 18, 0)
collection.objects.link(ball)
bpy.context.view_layer.layer_collection.children["LiveHidden"].hide_viewport = True
"""


def test_an_object_in_a_hidden_collection_is_unwrapped_in_live_mode_and_stays_hidden(live):
    """In live mode, an object in a hidden collection is unwrapped and stays hidden.

    The collection hiding the object is shown for the call and hidden again, the operator lays out real islands,
    and the active object and the mode stay as they were.
    """
    server, _, _ = live
    server.ok("create_object", kind="cube", name="LiveActive", location=[3, 18, 0])
    server.ok("run_python", code=HIDDEN_BALL)
    out = server.ok("unwrap_uv", object="LiveBall", method="smart", uv_map="Fresh")
    assert out["area_used"] < 1.01 and out["islands"] < 128, out
    state = server.ok("run_python", code="""
layer = bpy.context.view_layer
data = bpy.data.objects["LiveBall"].data.uv_layers["Fresh"].data
result = {"hidden": layer.layer_collection.children["LiveHidden"].hide_viewport,
          "active": layer.objects.active.name if layer.objects.active else None,
          "mode": bpy.data.objects["LiveBall"].mode, "spread": len({tuple(round(c, 4) for c in d.uv) for d in data})}
""")["result"]
    assert state["hidden"] is True and state["active"] == "LiveActive" and state["mode"] == "OBJECT", state
    assert state["spread"] > 100, "the operator left the faces on one point instead of laying them out"


def test_undo_change_steps_back_in_live_mode(live):
    """In live mode, ``undo_change`` steps back as Ctrl+Z does, one step per call.

    Its result carries a note saying that it names only BlenderLens's own steps, not the user's.
    """
    server, _, _ = live
    server.ok("create_object", kind="cube", name="Stepped", location=[0, 18, 0])
    server.ok("edit_mesh", object="Stepped", operation="extrude", facing="top", amount=0.5)
    listed = server.ok("undo_change", list=True)
    assert listed["steps"][:2] == ["BlenderLens: edit_mesh Stepped", "BlenderLens: create_object Stepped"]
    assert listed["can_undo"] is True and "note" in listed
    out = server.ok("undo_change")
    assert out["undone"] == ["BlenderLens: edit_mesh Stepped"] and "note" in out
    assert server.ok("get_object", object="Stepped")["mesh"]["faces"] == 6
    server.ok("undo_change")
    assert "Stepped" not in {o["name"] for o in server.ok("get_scene")["objects"]}


def test_a_contact_sheet_a_script_and_a_checkpoint_in_live_mode(live):
    """A contact sheet preview, a script and a checkpoint each behave correctly in live mode.

    The preview leaves the Blender window's scene as it was, a script is one undo step, and a checkpoint leaves the
    session's file with its unsaved changes.
    """
    server, root, _ = live
    server.ok("create_object", kind="cube", name="Sheeted", location=[0, 21, 0])
    shot = server.ok("render_preview", objects=["Sheeted"], views=["front", "top"], style="wireframe",
                     engine="workbench", resolution=64)
    assert (png_info(shot["path"])["width"], png_info(shot["path"])["height"]) == (128, 64)
    script = root / "live_build.py"
    script.write_text("o = bpy.data.objects.new('Scripted', None)\nbpy.context.scene.collection.objects.link(o)\n",
                      encoding="utf-8")
    assert server.ok("run_script", path=str(script))["added"] == ["Scripted"]
    assert server.ok("undo_change")["undone"] == ["BlenderLens: run_script"]
    assert "Scripted" not in {o["name"] for o in server.ok("get_scene")["objects"]}
    work = str(root / "live_work.blend")
    server.ok("save_file", path=work)
    server.ok("create_object", kind="cone", name="AfterSave", location=[3, 21, 0])
    copied = server.ok("save_file", path=str(root / "live_work.ck.blend"), copy=True)
    assert copied["file"] == work and copied["unsaved_changes"] is True
    assert server.ok("get_status")["session"]["file"] == work


#: Code that presses Ctrl+Z twice in the Blender window and returns, after each, whether LiveDoor's collision proxy
#: exists and which NLA tracks LiveDoor has.
CLIP_AND_PROXY = """
w = bpy.context.window_manager.windows[0]
door = bpy.data.objects["LiveDoor"]
steps = []
for _ in range(2):
    with bpy.context.temp_override(window=w):
        bpy.ops.ed.undo()
    door = bpy.data.objects["LiveDoor"]
    tracks = [t.name for t in door.animation_data.nla_tracks] if door.animation_data else []
    steps.append({"proxy": "LiveDoor-colonly" in bpy.data.objects, "tracks": tracks})
result = steps
"""


def test_clips_proxies_bakes_and_other_formats_in_live_mode(live):
    """Other formats, bakes, clips and collision proxies each behave correctly in live mode.

    ``export_file`` reads its file back in a scratch scene and leaves a saved file saved, a bake restores the
    Blender window's render engine, and a clip and a proxy are one undo step each.
    """
    server, root, _ = live
    server.ok("create_object", kind="cube", name="LiveDoor", location=[0, 18, 0])
    server.ok("save_file", path=str(root / "formats.blend"))
    exported = server.ok("export_file", path=str(root / "door.fbx"), objects=["LiveDoor"])
    assert exported["read_back"]["matches"] is True
    after = server.ok("get_scene")
    assert after["unsaved_changes"] is False, "the read-back's scratch scene left a change behind"
    engine = "result = bpy.context.scene.render.engine"
    before_engine = server.ok("run_python", code=engine)["result"]
    server.ok("bake_texture", object="LiveDoor", type="ao", size=32, samples=1, path=str(root / "door_ao.png"))
    assert server.ok("run_python", code=engine)["result"] == before_engine
    server.ok("animate_object", object="LiveDoor", clip="LiveOpen",
              keys=[{"frame": 1, "rotation": [0, 0, 0]}, {"frame": 11, "rotation": [0, 0, 90]}])
    server.ok("create_collision", object="LiveDoor", shape="box")
    steps = server.ok("run_python", code=CLIP_AND_PROXY)["result"]
    assert steps == [{"proxy": False, "tracks": ["LiveOpen"]}, {"proxy": False, "tracks": []}], \
        "one Ctrl+Z did not take back the proxy, or the next did not take back the clip"


#: Code that imitates an edit someone makes in the Blender window between two calls: a move with its own undo step,
#: run by a timer outside any call.
USER_MOVES = """
def _user_moves():
    w = bpy.context.window_manager.windows[0]
    obj = bpy.data.objects[NAME]
    with bpy.context.temp_override(window=w, active_object=obj, object=obj, selected_objects=[obj],
                                   selected_editable_objects=[obj]):
        bpy.ops.transform.translate('EXEC_DEFAULT', True, value=(0, 0, 5))
    return None
bpy.app.timers.register(_user_moves, first_interval=0.3)
"""


def wait_for_height(server, name, height):
    """Wait until the object ``name`` stands at the height ``height``, failing the test after 20 seconds."""
    deadline = time.time() + 20
    while server.ok("get_object", object=name)["location"][2] != pytest.approx(height, abs=1e-4):
        assert time.time() < deadline, f"{name} never reached z = {height}"
        time.sleep(0.2)


def test_undo_change_never_undoes_a_users_edit_under_a_blenderlens_name(live):
    """``undo_change`` never undoes an edit the user made after the last call while naming it a BlenderLens step.

    The user's edit is the newest undo step. Without ``force``, ``undo_change`` refuses and changes nothing, and
    with ``force`` it undoes the user's step without giving it a BlenderLens name.
    """
    server, _, _ = live
    server.ok("create_object", kind="cube", name="Agent", location=[0, 24, 0])
    server.ok("create_object", kind="cube", name="Users", location=[3, 24, 0])
    assert server.ok("undo_change", list=True)["by_name"] >= 2
    server.ok("run_python", code=f"NAME = 'Users'\n{USER_MOVES}")
    wait_for_height(server, "Users", 5.0)
    listed = server.ok("undo_change", list=True)
    assert listed["steps"][0] == "BlenderLens: create_object Users" and listed["by_name"] == 0, listed
    payload = server.refused("undo_change", "bad_argument")
    assert "Ctrl+Z" in payload["hint"] and "force: true" in payload["hint"], payload
    names = {o["name"] for o in server.ok("get_scene")["objects"]}
    assert {"Agent", "Users"} <= names, "the refused undo_change undid a step"
    assert server.ok("get_object", object="Users")["location"][2] == pytest.approx(5.0, abs=1e-4)
    forced = server.ok("undo_change", force=True)
    assert len(forced["undone"]) == 1 and not forced["undone"][0].startswith("BlenderLens:"), forced
    assert server.ok("get_object", object="Users")["location"][2] == pytest.approx(0.0, abs=1e-4), \
        "force did not take back the user's move, the newest step"


def test_a_run_python_that_only_reads_leaves_a_saved_file_saved_in_live_mode(live):
    """A ``run_python`` call that only reads pushes no undo step, so a saved file stays saved in live mode.

    In a Blender window, an undo step itself marks the file as modified.
    """
    server, root, _ = live
    server.ok("create_object", kind="cube", name="ReadMe", location=[0, 27, 0])
    server.ok("save_file", path=str(root / "read.blend"))
    before = server.ok("undo_change", list=True)["steps"]
    assert server.ok("run_python", code="result = len(bpy.data.objects)")["result"] > 0
    assert server.ok("get_scene")["unsaved_changes"] is False
    assert server.ok("undo_change", list=True)["steps"] == before
    server.ok("run_python", code="bpy.data.objects['ReadMe']['tag'] = 1")
    assert server.ok("get_scene")["unsaved_changes"] is True, \
        "a custom property set by run_python did not mark the file changed"


def test_closing_the_live_session_leaves_the_blender_window_open(live):
    server, _, blender = live
    closed = server.ok("close_session")
    assert closed["closed"] == "live"
    assert blender.poll() is None, "closing the session closed the Blender window"
    assert server.ok("get_status")["live_addon"]["responding"] is True
