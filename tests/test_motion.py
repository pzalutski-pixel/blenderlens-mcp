"""Tests for ``animate_object``, run against a real Blender.

Clips keyed into NLA (non-linear animation) tracks must come out of ``export_glb`` as named glTF animations, and
the tests that export read them back from the exported file. One test, of the rule that decides from a clip's
name whether Godot loops it, needs no Blender.
"""

import ast
import math
from pathlib import Path

import pytest

from blenderlens_mcp import glb as GLB
from tests.test_modelling import accessor, close, glb
from tests.test_refusals_change_nothing import refused_unchanged

MOTION = Path(GLB.__file__).with_name("addon") / "motion.py"
FPS = 24  # The frame rate of a new, empty Blender file.
#: Marks the tests that drive a real Blender. The test of the loop-name rule needs no Blender.
integration = pytest.mark.integration


def saved_scene(server, folder):
    """Build and save the scene the refusal tests start from: a cube with a material and a bevel, and an empty.

    Return ``server``, so a fixture can hand it on.
    """
    server.ok("create_object", kind="cube", name="Box", size=[2, 2, 2])
    server.ok("set_material", material="Paint", object="Box", base_color=[0.2, 0.3, 0.4])
    server.ok("add_modifier", object="Box", type="bevel", settings={"width": 0.1})
    server.ok("create_object", kind="empty", name="Pivot", location=[3, 0, 0])
    server.ok("save_file", path=str(folder / "scene.blend"))
    return server


@pytest.fixture
def scene(blender, tmp_path):
    """Return the Blender session holding the saved scene that the refusal tests start from."""
    return saved_scene(blender, tmp_path)


def animations(path) -> dict:
    """Read the animations in a .glb file, keyed by animation name.

    Each value is ``{"seconds": duration, "channels": {(node name, target path): last sampled value}}``.
    """
    document, binary = glb(path)
    out = {}
    for animation in document.get("animations", []):
        starts, ends, channels = [], [], {}
        for channel in animation["channels"]:
            sampler = animation["samplers"][channel["sampler"]]
            times = document["accessors"][sampler["input"]]
            starts.append(times["min"][0])
            ends.append(times["max"][0])
            node = document["nodes"][channel["target"]["node"]]["name"]
            channels[(node, channel["target"]["path"])] = accessor(document, binary, sampler["output"])[-1]
        out[animation["name"]] = {"seconds": max(ends) - min(starts), "channels": channels}
    return out


def nla(server, name) -> dict:
    """Return the NLA tracks of an object and of its shape keys, each with the names of its strips' actions.

    The result also lists every action in the file, and gives the name of the object's active action, or None.
    """
    return server.ok("run_python", code=f"""
obj = bpy.data.objects[{name!r}]
def tracks(owner):
    data = owner.animation_data if owner is not None else None
    return [[t.name, [s.action.name for s in t.strips]] for t in data.nla_tracks] if data else []
keys = obj.data.shape_keys if obj.data is not None and hasattr(obj.data, "shape_keys") else None
result = {{"object": tracks(obj), "shape_keys": tracks(keys), "actions": sorted(a.name for a in bpy.data.actions),
          "active": obj.animation_data.action.name if obj.animation_data and obj.animation_data.action else None}}
""")["result"]


@integration
def test_a_door_and_a_rotor_export_as_two_named_animations_with_their_durations(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Door", size=[1, 0.1, 2])
    blender.ok("create_object", kind="cylinder", name="Rotor", radius=0.5, depth=0.1, location=[3, 0, 0])
    door = blender.ok("animate_object", object="Door", clip="DoorOpen",
                      keys=[{"frame": 1, "rotation": [0, 0, 0]}, {"frame": 31, "rotation": [0, 0, 90]}])
    assert door["frames"] == [1, 31] and door["seconds"] == 1.25 and door["clips"] == ["DoorOpen"]
    assert door["channels"]["object"] == {"rotation_euler": [0, 1, 2]}
    rotor = blender.ok("animate_object", object="Rotor", clip="Spin_loop",
                       keys=[{"frame": 1, "rotation": [None, None, 0]}, {"frame": 25, "rotation": [None, None, 360]}])
    assert rotor["loop"] is True, "the clip's name says loop, but it does not loop"
    assert rotor["channels"]["object"] == {"rotation_euler": [2]}
    assert "warnings" not in rotor

    out = blender.ok("export_glb", path=str(tmp_path / "anim.glb"))
    assert out["contents"]["animations"] == 2
    found = animations(tmp_path / "anim.glb")
    assert set(found) == {"DoorOpen", "Spin_loop"}
    assert math.isclose(found["DoorOpen"]["seconds"], 30 / FPS, abs_tol=1e-4)
    # The Cycles F-curve modifier that loops the clip does not lengthen it.
    assert math.isclose(found["Spin_loop"]["seconds"], 24 / FPS, abs_tol=1e-4)
    # Blender's Z axis is glTF's Y axis, so a turn of 90 degrees about it is this quaternion, written (x, y, z, w).
    assert close(found["DoorOpen"]["channels"][("Door", "rotation")], (0, math.sqrt(0.5), 0, math.sqrt(0.5)))
    assert set(found["Spin_loop"]["channels"]) == {("Rotor", "rotation")}


#: Code that gives the object Blob a Basis shape key and a Squash key that halves its height.
SHAPE_KEYS = """
obj = bpy.data.objects["Blob"]
obj.shape_key_add(name="Basis", from_mix=False)
squash = obj.shape_key_add(name="Squash", from_mix=False)
for point in squash.data:
    point.co.z *= 0.5
"""


@integration
def test_a_shape_key_clip_exports_morph_weights_beside_the_transform(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Blob", size=1)
    blender.ok("run_python", code=SHAPE_KEYS)
    out = blender.ok("animate_object", object="Blob", clip="Squish",
                     keys=[{"frame": 1, "shape_key": {"Squash": 0}, "location": [0, 0, 0]},
                           {"frame": 11, "shape_key": {"Squash": 1}, "location": [0, 0, 1]}])
    assert out["channels"] == {"object": {"location": [0, 1, 2]}, "shape_keys": {'key_blocks["Squash"].value': [0]}}
    held = nla(blender, "Blob")
    assert held["object"] == [["Squish", held["object"][0][1]]] and held["shape_keys"][0][0] == "Squish"
    assert held["active"] is None, "the clip was left as the active action, not on the NLA"

    blender.ok("export_glb", path=str(tmp_path / "morph.glb"))
    found = animations(tmp_path / "morph.glb")
    assert list(found) == ["Squish"], "the transform and the morph weights are not one animation"
    channels = found["Squish"]["channels"]
    assert set(channels) == {("Blob", "translation"), ("Blob", "weights")}
    assert close(channels[("Blob", "weights")], (1.0,))
    # Blender's +Z is glTF's +Y.
    assert close(channels[("Blob", "translation")], (0, 1, 0))
    assert math.isclose(found["Squish"]["seconds"], 10 / FPS, abs_tol=1e-4)
    document, _ = glb(tmp_path / "morph.glb")
    assert len(document["meshes"][0]["primitives"][0]["targets"]) == 1


@integration
def test_two_objects_keyed_into_one_clip_play_as_one_animation(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Door", size=[1, 0.1, 2])
    blender.ok("create_object", kind="cube", name="Handle", size=0.1, location=[0.4, -0.1, 0])
    blender.ok("animate_object", object="Door", clip="DoorOpen",
               keys=[{"frame": 1, "rotation": [0, 0, 0]}, {"frame": 31, "rotation": [0, 0, 90]}])
    blender.ok("animate_object", object="Handle", clip="DoorOpen",
               keys=[{"frame": 1, "rotation": [0, 0, 0]}, {"frame": 11, "rotation": [45, 0, 0]}])
    blender.ok("export_glb", path=str(tmp_path / "door.glb"))
    found = animations(tmp_path / "door.glb")
    assert list(found) == ["DoorOpen"]
    assert set(found["DoorOpen"]["channels"]) == {("Door", "rotation"), ("Handle", "rotation")}


@integration
def test_a_second_call_replaces_the_objects_part_of_the_clip(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Door", size=1)
    blender.ok("animate_object", object="Door", clip="DoorOpen",
               keys=[{"frame": 1, "location": [0, 0, 0]}, {"frame": 31, "location": [1, 0, 0]}])
    again = blender.ok("animate_object", object="Door", clip="DoorOpen", interpolation="constant",
                       keys=[{"frame": 1, "rotation": [0, 0, 0]}, {"frame": 13, "rotation": [0, 0, 90]}])
    assert again["replaced"] is True and again["clips"] == ["DoorOpen"]
    assert again["channels"] == {"object": {"rotation_euler": [0, 1, 2]}}
    held = nla(blender, "Door")
    assert [track for track, _ in held["object"]] == ["DoorOpen"]
    assert len([a for a in held["actions"] if a.startswith("DoorOpen")]) == 1, held["actions"]
    blender.ok("export_glb", path=str(tmp_path / "door.glb"))
    found = animations(tmp_path / "door.glb")
    assert set(found["DoorOpen"]["channels"]) == {("Door", "rotation")}, "the old location keys are still there"
    assert math.isclose(found["DoorOpen"]["seconds"], 12 / FPS, abs_tol=1e-4)


@integration
def test_an_object_turned_by_quaternion_is_keyed_in_quaternions(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Lever", size=1)
    blender.ok("run_python", code='bpy.data.objects["Lever"].rotation_mode = "QUATERNION"')
    out = blender.ok("animate_object", object="Lever", clip="Pull",
                     keys=[{"frame": 1, "rotation": [0, 0, 0]}, {"frame": 25, "rotation": [0, 0, 90]}])
    assert out["channels"]["object"] == {"rotation_quaternion": [0, 1, 2, 3]}
    blender.ok("export_glb", path=str(tmp_path / "lever.glb"))
    found = animations(tmp_path / "lever.glb")
    assert close(found["Pull"]["channels"][("Lever", "rotation")], (0, math.sqrt(0.5), 0, math.sqrt(0.5)))
    refused = blender.refused("animate_object", "bad_argument", object="Lever", clip="Pull",
                              keys=[{"frame": 1, "rotation": [0, None, 0]}])
    assert "quaternion" in refused["error"]


@integration
def test_a_loop_setting_that_disagrees_with_the_clips_name_is_warned_about_either_way(blender):
    """A loop setting that Godot would read the other way from the clip's name brings a warning, whichever way it is.

    A clip loops in Blender through a Cycles F-curve modifier, which the test reads back from each clip's curves.
    """
    blender.ok("create_object", kind="cube", name="Fan", size=1)
    keys = [{"frame": 1, "rotation": [0, 0, 0]}, {"frame": 25, "rotation": [0, 0, 360]}]
    once = blender.ok("animate_object", object="Fan", clip="Spin", keys=keys, loop=True)
    assert [w["code"] for w in once["warnings"]] == ["godot_plays_once"]
    looped = blender.ok("animate_object", object="Fan", clip="IdleLoop", keys=keys, loop=False)
    assert [w["code"] for w in looped["warnings"]] == ["godot_loops"]
    plain = blender.ok("animate_object", object="Fan", clip="CycleBlades", keys=keys)
    assert plain["loop"] is True and "warnings" not in plain
    modifiers = blender.ok("run_python", code="""
from bpy_extras import anim_utils
obj = bpy.data.objects["Fan"]
result = {}
for track in obj.animation_data.nla_tracks:
    strip = track.strips[0]
    slot = getattr(strip, "action_slot", None)
    curves = anim_utils.action_get_channelbag_for_slot(strip.action, slot).fcurves if slot else strip.action.fcurves
    result[track.name] = sorted({m.type for c in curves for m in c.modifiers})
""")["result"]
    assert modifiers == {"Spin": ["CYCLES"], "IdleLoop": [], "CycleBlades": ["CYCLES"]}


@integration
@pytest.mark.parametrize(("keys", "kind"), [
    ([], "bad_argument"),
    ([{"rotation": [0, 0, 90]}], "bad_argument"),
    ([{"frame": -1, "rotation": [0, 0, 90]}], "bad_argument"),
    ([{"frame": 1.5, "rotation": [0, 0, 90]}], "bad_argument"),
    ([{"frame": 1, "rotate": [0, 0, 90]}], "bad_argument"),
    ([{"frame": 1, "location": [0, 0]}], "bad_argument"),
    ([{"frame": 1, "location": [0, 0, "up"]}], "bad_argument"),
    ([{"frame": 1, "location": [0, 0, 1e300]}], "bad_argument"),
    ([{"frame": 1, "location": [0, 0, 0]}, {"frame": 1, "location": [1, 0, 0]}], "bad_argument"),
    ([{"frame": 1}], "bad_argument"),
    ([{"frame": 1, "shape_key": {"Squash": 1}}], "bad_argument"),
    ([{"frame": 1, "location": [0, 0, 0]}, {"frame": 2, "scale": "big"}], "bad_argument"),
])
def test_animate_object_refused_changes_nothing(scene, keys, kind):
    refused_unchanged(scene, "animate_object", kind, object="Box", clip="Bounce", keys=keys)


@integration
def test_a_refused_call_leaves_the_clip_it_would_replace(scene):
    scene.ok("animate_object", object="Box", clip="Bounce",
             keys=[{"frame": 1, "location": [0, 0, 0]}, {"frame": 9, "location": [0, 0, 1]}])
    before = nla(scene, "Box")
    refused_unchanged(scene, "animate_object", "bad_argument", object="Box", clip="Bounce",
                      keys=[{"frame": 1, "location": [0, 0, 0]}, {"frame": 5, "rotation": [0, "x", 0]}])
    refused_unchanged(scene, "animate_object", "bad_argument", object="Box", clip="Bounce", interpolation="ease",
                      keys=[{"frame": 1, "location": [0, 0, 0]}])
    refused_unchanged(scene, "animate_object", "not_found", object="Nobody", clip="Bounce",
                      keys=[{"frame": 1, "location": [0, 0, 0]}])
    assert nla(scene, "Box") == before


def sampled(path, node, target, seconds) -> tuple:
    """Return one animation channel's value in a .glb file at ``seconds``, interpolated linearly between samples.

    Raise AssertionError when the file has no such channel.
    """
    document, binary = glb(path)
    for animation in document.get("animations", []):
        for channel in animation["channels"]:
            if (document["nodes"][channel["target"]["node"]]["name"], channel["target"]["path"]) != (node, target):
                continue
            sampler = animation["samplers"][channel["sampler"]]
            times = [t[0] for t in accessor(document, binary, sampler["input"])]
            values = accessor(document, binary, sampler["output"])
            if seconds <= times[0]:
                return values[0]
            for i in range(1, len(times)):
                if seconds <= times[i]:
                    share = (seconds - times[i - 1]) / (times[i] - times[i - 1])
                    return tuple(a + (b - a) * share for a, b in zip(values[i - 1], values[i], strict=True))
            return values[-1]
    raise AssertionError(f"no {target} channel for {node} in {path}")


#: Code that returns, at each of FRAMES, the height of the object B and the value of its Puff shape key, and then goes
#: back to the frame it started on.
HEIGHTS = """
scene = bpy.context.scene
kept = scene.frame_current
result = {}
for frame in FRAMES:
    scene.frame_set(frame)
    body = bpy.data.objects["B"]
    result[frame] = [round(body.matrix_world.translation.z, 4),
                     round(body.data.shape_keys.key_blocks["Puff"].value, 4)]
scene.frame_set(kept)
"""


@integration
def test_an_object_joining_a_clip_at_later_frames_plays_them_where_they_were_keyed(blender, tmp_path):
    """An object that joins a clip at later frames moves at those frames, both in Blender and in the exported file.

    Each datablock's NLA strip starts at its own first key and plays only its own keys.
    """
    blender.ok("create_object", kind="cube", name="A", size=1)
    blender.ok("create_object", kind="cube", name="B", size=1, location=[3, 0, 0])
    blender.ok("run_python", code="body = bpy.data.objects['B']\nbody.shape_key_add(name='Basis')\n"
                                  "body.shape_key_add(name='Puff')")
    blender.ok("animate_object", object="A", clip="Go",
               keys=[{"frame": 0, "location": [0, 0, 0]}, {"frame": 10, "location": [0, 0, 1]}])
    blender.ok("animate_object", object="B", clip="Go",
               keys=[{"frame": 20, "location": [3, 0, 0], "shape_key": {"Puff": 0}},
                     {"frame": 30, "location": [3, 0, 2], "shape_key": {"Puff": 1}}])
    frames = [5, 20, 25, 30]
    held = blender.ok("run_python", code=f"FRAMES = {frames!r}\n" + HEIGHTS)["result"]
    assert held == {"5": [0.0, 0.0], "20": [0.0, 0.0], "25": [1.0, 0.5], "30": [2.0, 1.0]}, held
    blender.ok("export_glb", path=str(tmp_path / "go.glb"))
    # Blender's Z is glTF's Y.
    assert sampled(tmp_path / "go.glb", "B", "translation", 25 / FPS)[1] == pytest.approx(1.0, abs=1e-3)
    assert sampled(tmp_path / "go.glb", "B", "weights", 25 / FPS)[0] == pytest.approx(0.5, abs=1e-3)


#: Code that returns, at each frame of TURNS, how closely the object Fan's rotation matches the turn about Z given for
#: that frame, and then goes back to the frame it started on.
QUATERNION_TURN = """
from mathutils import Euler
scene = bpy.context.scene
kept = scene.frame_current
result = {}
for frame, degrees in TURNS:
    scene.frame_set(frame)
    held = bpy.data.objects["Fan"].matrix_world.to_quaternion()
    result[frame] = round(abs(held.dot(Euler((0, 0, math.radians(degrees))).to_quaternion())), 4)
scene.frame_set(kept)
"""


@integration
@pytest.mark.parametrize("interpolation", ["linear", "bezier"])
def test_a_full_turn_of_an_object_turned_by_quaternion_goes_all_the_way_round(blender, tmp_path, interpolation):
    """A full turn keyed on an object that rotates by quaternion goes all the way round, in Blender and in the file.

    A quaternion takes the shorter way between two keys, so animate_object keys a turn of 180 degrees or more in
    steps.
    """
    blender.ok("create_object", kind="cube", name="Fan", size=1)
    blender.ok("run_python", code='bpy.data.objects["Fan"].rotation_mode = "QUATERNION"')
    blender.ok("animate_object", object="Fan", clip="Spin", interpolation=interpolation,
               keys=[{"frame": 0, "rotation": [0, 0, 0]}, {"frame": 20, "rotation": [0, 0, 360]}])
    turns = [(0, 0), (10, 180), (20, 360)] + ([(5, 90), (15, 270)] if interpolation == "linear" else [])
    agreement = blender.ok("run_python", code=f"TURNS = {turns!r}\n" + QUATERNION_TURN)["result"]
    assert all(value > 0.999 for value in agreement.values()), agreement
    blender.ok("export_glb", path=str(tmp_path / "fan.glb"))
    halfway = sampled(tmp_path / "fan.glb", "Fan", "rotation", 10 / FPS)
    # A turn of 180 degrees about Blender's Z axis is 180 degrees about glTF's Y axis, the quaternion (0, +-1, 0, 0)
    # written (x, y, z, w).
    assert abs(halfway[1]) == pytest.approx(1.0, abs=1e-2), halfway


@integration
def test_a_clip_named_after_an_action_animate_object_did_not_make_is_refused(scene):
    """animate_object refuses a clip named after an action it did not make, and leaves that action as it was.

    Such an action may have been keyed by the user or named by an import.
    """
    scene.ok("run_python", code="obj = bpy.data.objects['Box']\nobj.location.x = 0\n"
                                "obj.keyframe_insert('location', frame=1)\nobj.location.x = 5\n"
                                "obj.keyframe_insert('location', frame=40)\nobj.animation_data.action.name = 'Open'")
    scene.ok("save_file")
    payload = refused_unchanged(scene, "animate_object", "bad_argument", object="Box", clip="Open",
                                keys=[{"frame": 0, "rotation": [0, 0, 0]}, {"frame": 24, "rotation": [0, 0, 90]}])
    assert "'Open'" in payload["error"] and "clip" in payload["hint"], payload
    held = nla(scene, "Box")
    assert held["active"] == "Open" and held["object"] == [], held
    # A clip that animate_object made itself can still be replaced.
    scene.ok("animate_object", object="Box", clip="Opened", keys=[{"frame": 0, "location": [0, 0, 0]}])
    again = scene.ok("animate_object", object="Box", clip="Opened", keys=[{"frame": 0, "location": [0, 0, 1]}])
    assert again["replaced"] is True


@integration
def test_an_action_of_the_clips_name_played_elsewhere_is_refused(scene):
    """animate_object refuses to replace a clip whose action it made when another object has it as its active action."""
    scene.ok("animate_object", object="Box", clip="Bounce",
             keys=[{"frame": 1, "location": [0, 0, 0]}, {"frame": 9, "location": [0, 0, 1]}])
    scene.ok("run_python", code="pivot = bpy.data.objects['Pivot']\npivot.animation_data_create()\n"
                                "action = bpy.data.actions[next(a.name for a in bpy.data.actions "
                                "if a.name.startswith('Bounce'))]\npivot.animation_data.action = action")
    scene.ok("save_file")
    refused_unchanged(scene, "animate_object", "bad_argument", object="Box", clip="Bounce",
                      keys=[{"frame": 1, "location": [0, 0, 0]}, {"frame": 5, "location": [0, 0, 2]}])


@integration
def test_too_many_keys_are_refused_naming_the_maximum(scene):
    keys = [{"frame": i, "location": [0, 0, 0]} for i in range(10_001)]
    payload = refused_unchanged(scene, "animate_object", "bad_argument", object="Box", clip="Bounce", keys=keys)
    assert "10,000" in payload["error"]


def _loop_rule():
    """Return the add-on's ``godot_loops`` function, compiled from the source of ``motion.py``.

    The add-on module imports ``bpy``, which exists only inside Blender, so only the functions and constants the
    rule needs are taken from the module's syntax tree and run here.
    """
    tree = ast.parse(MOTION.read_text(encoding="utf-8"))
    keep = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name in
                                            ("godot_hint", "godot_node_name", "godot_loops")) or
            (isinstance(node, ast.Assign) and any(getattr(t, "id", "") in ("GODOT_LOOP_WORDS", "GODOT_LOOP_HINTS")
                                                  for t in node.targets))]
    namespace = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(MOTION), "exec"), namespace)
    return namespace["godot_loops"]


@pytest.mark.parametrize(("name", "loops"), [
    ("Spin_loop", True), ("Spin-loop", True), ("Walk_cycle", True), ("Idle$loop_extra", True), ("Run_loop_001", True),
    ("Run.loop.001", True), ("IdleLoop", True), ("LoopIdle", True), ("cycleBlades", True), ("Spin_loop_mode", True),
    ("DoorOpen", False), ("Spin", False), ("Bloop_open", False), ("Recycled", False),
])
def test_the_addons_loop_rule_is_godots(name, loops):
    """The add-on's loop rule follows Godot's two rules for clip names, and the server's copy agrees with it.

    Godot's glTF importer loops a name that begins or ends with a loop word, and its scene importer loops a name
    that carries a loop hint. The server's .glb reader keeps its own copy of the rule, ``glb.godot_loops``, for
    its read-back warnings, so the two copies are compared on every name.
    """
    godot_loops = _loop_rule()
    assert godot_loops(name) is loops, f"the add-on's rule is wrong about {name!r}"
    assert GLB.godot_loops(name) is godot_loops(name), f"the server's and the add-on's rules differ on {name!r}"
    addon = godot_loops.__globals__
    assert addon["GODOT_LOOP_WORDS"] == GLB.GODOT_LOOP_WORDS, "the add-on's and the server's loop words differ"
    assert addon["GODOT_LOOP_HINTS"] == GLB.GODOT_LOOP_HINTS, "the add-on's and the server's loop hints differ"
