"""Key an object's transform and shape keys into a named animation clip.

A clip becomes one glTF animation. Each clip an object takes part in sits on an NLA track of the clip's
name, holding one strip, on the object and, for shape keys, on its mesh's shape key datablock. The
active action is left alone. The glTF exporter's default animation mode (ACTIONS, in Blender 4.2 and 5.0
alike) writes each such strip as an animation of its own, so clips keyed one after another come out side
by side.

Where the F-curves live depends on the Blender version, and the code switches at 4.4:

- Before 4.4 (4.2 and 4.3), the action holds them (``Action.fcurves``), and an action animates one kind of
  datablock. So the object and its shape keys each get an action of their own, both on tracks named after
  the clip. The 4.2 exporter merges the animations of tracks that share a name and names the result after
  the track.
- From 4.4, actions are slotted, and 5.0 removed ``Action.fcurves``. An action holds layers, strips, and a
  channelbag of F-curves for each slot, one slot per datablock it animates. So a clip is one action named
  after it, with a slot for the object and one for its shape keys (and one for each other object keyed
  into the same clip). The 5.0 exporter merges by action name, so all of them come out as one animation.

A call states the whole of the object's part in the clip, replacing what the clip held for it before.
The new keys are written first, beside the old ones. Only when all of them are in place is the old part
removed, so a call refused partway leaves the clip as it was.
"""

import itertools
import math

import bpy
from bpy_extras import anim_utils
from mathutils import Euler, Quaternion

from .common import (
    CommandError,
    Rollback,
    finite,
    get_object,
    is_number,
    mode_hint,
    mode_name,
    plural,
    require,
    rnd,
    text,
    update,
)

#: Whether this Blender has slotted actions. They arrived in Blender 4.4, and from 5.0 they are the only kind.
SLOTTED = bpy.app.version >= (4, 4, 0)

#: The interpolations a call may ask for, mapped to Blender's names for them.
INTERPOLATIONS = {"linear": "LINEAR", "constant": "CONSTANT", "bezier": "BEZIER"}
#: The most keys one call takes.
MAX_KEYS = 10_000
#: The most shape keys one key sets.
MAX_SHAPE_KEYS = 256
#: The highest frame Blender allows (its MAXFRAME). Negative frames are refused although Blender allows
#: them, because the glTF exporter moves them, so they would not come out where they were keyed.
MAX_FRAME = 1_048_574
#: The most bytes of a clip's name. An NLA track name holds at most 63 bytes, in Blender 4.2 and 5.0 alike, and
#: so do action and slot names in 4.2.
MAX_NAME_BYTES = 63
#: The rotation modes that are Euler angles.
EULER_MODES = {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}
#: The fields one key may have.
KEY_FIELDS = ("frame", "location", "rotation", "scale", "shape_key")

# The Godot game engine loops an imported animation by its name, in two places, and the next two constants
# hold the words each place looks for. The MCP server's glTF read-back (glb.godot_hint) holds the same scene
# importer test. The add-on runs inside Blender and cannot import the server's code, so it keeps its own copy.
#: The words that make Godot's glTF importer (GLTFDocument::_parse_animations) loop an animation whose name, in
#: lower case, begins or ends with one of them.
GODOT_LOOP_WORDS = ("loop", "cycle")
#: The import hints that make Godot's scene importer (ResourceImporterScene, _teststr) loop an animation when
#: one follows a - or _ at the end of the name, or a $ anywhere, once trailing digits, spaces and underscores
#: are stripped.
GODOT_LOOP_HINTS = ("loop_mode", "loop", "cycle")

#: The custom property set on every action animate_object makes, holding the clip's name. An action named
#: after the clip but without this property was made by something else, and is never written into.
MARK = "blenderlens_clip"
#: The widest turn, in degrees, between two quaternion keys. A quaternion takes the short way between keys,
#: so a turn of 180 degrees or more is keyed in steps no wider than this.
QUATERNION_STEP = 90.0


def godot_hint(name, hint):
    """Return whether Godot's scene importer finds the import hint ``hint`` in a node or animation name.

    This mirrors the importer's _teststr. Trailing digits, underscores, spaces and control characters
    are stripped, and the hint must follow a $ anywhere, or a - or _ at the end of the name, compared in
    lower case.
    """
    what = name
    while what and (what[-1] in "0123456789_" or ord(what[-1]) <= 32):
        what = what[:-1]
    lower = what.lower()
    return f"${hint}" in lower or lower.endswith(f"-{hint}") or lower.endswith(f"_{hint}")


def godot_node_name(name):
    """Return a Blender name as Godot names the imported node.

    Each character Godot does not allow in a node name, Blender's dot among them, becomes an underscore.
    """
    return "".join("_" if c in '.:@/"%' else c for c in name)


def godot_loops(name):
    """Return whether Godot imports an animation of this name as looping."""
    lower = name.lower()
    if any(lower.startswith(word) or lower.endswith(word) for word in GODOT_LOOP_WORDS):
        return True
    return any(godot_hint(godot_node_name(name), hint) for hint in GODOT_LOOP_HINTS)


# ---------------------------------------------------------------------------
# Reading the keys
# ---------------------------------------------------------------------------


def _clip_name(params):
    """Return the clip's name, refusing one that is empty or longer than MAX_NAME_BYTES."""
    clip = require(params, "clip")
    if not isinstance(clip, str) or not clip.strip():
        raise CommandError("bad_argument", "'clip' must be the animation's name.")
    if len(clip.encode("utf-8")) > MAX_NAME_BYTES:
        raise CommandError("bad_argument", f"'clip' is longer than {MAX_NAME_BYTES} bytes, the most an NLA track "
                                           "name can hold.", "Use a shorter name.")
    return clip


def _frame(key, where):
    """Return a key's frame, refusing one that is not a whole number from 0 to MAX_FRAME."""
    frame = key.get("frame")
    if not isinstance(frame, int) or isinstance(frame, bool):
        raise CommandError("bad_argument", f"{where} needs 'frame', a whole number, got {frame!r}.")
    if not 0 <= frame <= MAX_FRAME:
        raise CommandError("bad_argument", f"The frame of {where} must be from 0 to {MAX_FRAME:,}, got {frame}.")
    return frame


def _axes(value, key, where, uniform):
    """Return a key's vector value as three numbers, with None for an axis that is not keyed.

    With ``uniform``, a single number stands for all three axes. ``where`` and ``key`` name the value in
    error messages.
    """
    if is_number(value) and uniform:
        return [finite(value, f"{where}.{key}")] * 3
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise CommandError("bad_argument", f"{where}.{key} must be [x, y, z]" + (" or one number" if uniform else "")
                           + f", got {value!r}.", "null leaves an axis unkeyed at that frame.")
    out = []
    for component in value:
        if component is None:
            out.append(None)
        elif is_number(component):
            out.append(finite(component, f"{where}.{key}"))
        else:
            raise CommandError("bad_argument", f"{where}.{key} must hold numbers or null, got {component!r}.")
    return out


def _shape_keys(obj, value, where):
    """Return a key's shape key values as {name: value}, checked against the object's shape keys and sliders."""
    if not isinstance(value, dict) or not value:
        raise CommandError("bad_argument", f"{where}.shape_key must be a JSON object that maps shape key names to "
                                           "values.")
    if len(value) > MAX_SHAPE_KEYS:
        raise CommandError("bad_argument", f"{where}.shape_key sets {len(value)} shape keys, more than the "
                                           f"{MAX_SHAPE_KEYS} one key may set.")
    keys = getattr(obj.data, "shape_keys", None) if obj.data is not None else None
    if keys is None:
        raise CommandError("bad_argument", f"'{obj.name}' has no shape keys to animate.",
                           "Make shape keys in Blender, or with run_python (obj.shape_key_add(name=...)).")
    out = {}
    for name, amount in value.items():
        block = keys.key_blocks.get(name)
        if block is None:
            raise CommandError("not_found", f"'{obj.name}' has no shape key named '{name}'.",
                               "Its shape keys are: " + ", ".join(b.name for b in keys.key_blocks) + ".")
        if block == keys.reference_key:
            raise CommandError("bad_argument", f"'{name}' is the basis shape key of '{obj.name}', and a basis has no "
                                               "value to key.")
        number = finite(amount, f"{where}.shape_key.{name}")
        if not block.slider_min <= number <= block.slider_max:
            raise CommandError("bad_argument", f"The value of shape key '{name}' in {where} must be from "
                                               f"{rnd(block.slider_min)} to {rnd(block.slider_max)}, got {amount}.")
        out[name] = number
    return out


def _in_steps(obj, rotations, interpolation):
    """Return a quaternion object's rotation keys, with steps added between keys 180 degrees or more apart.

    A quaternion takes the short way from one key to the next, so keys at 0 and 360 degrees would not
    move the object at all. A turn of 180 degrees or more is keyed in steps of at most QUATERNION_STEP
    degrees, at frames spaced in proportion, so the object turns the way the degrees say. The turn is
    measured as the axes' turns added up, which the actual turn between the two keys never exceeds.
    Constant interpolation holds each key until the next, so it needs no steps. Return (keys sorted by
    frame, how many keys were added), and raise a CommandError when the steps would exceed MAX_KEYS.
    """
    keys = sorted(rotations, key=lambda item: item[0])
    if interpolation == "CONSTANT":
        return keys, 0
    out = keys[:1]
    for (start, before), (end, after) in itertools.pairwise(keys):
        turn = sum(abs(b - a) for a, b in zip(before, after, strict=True))
        steps = math.ceil(turn / QUATERNION_STEP) if turn >= 180 else 1
        if len(out) + steps > MAX_KEYS:
            raise CommandError("bad_argument", f"The rotation keys of '{obj.name}' turn too far to key in steps of "
                                               f"{QUATERNION_STEP:g} degrees: more than {MAX_KEYS:,} keys.",
                               "Turn it less between keys, or first call set_transform with a rotation, which "
                               "switches the object to XYZ Euler.")
        for step in range(1, steps):
            share = step / steps
            out.append((start + (end - start) * share,
                        [a + (b - a) * share for a, b in zip(before, after, strict=True)]))
        out.append((end, after))
    return out, len(out) - len(keys)


def _rotation_curves(obj, rotations, interpolation):
    """Turn rotation keys, [(frame, degrees)], into F-curve keys by (data path, index) in the object's rotation mode.

    Return the curves and how many keys a quaternion object's turns needed besides those given. An
    Euler object gets a curve for each axis keyed. A quaternion object needs all three axes in every
    key, and an object that rotates by axis and angle is refused.
    """
    curves = {}
    if obj.rotation_mode in EULER_MODES:
        for frame, degrees in rotations:
            for axis, angle in enumerate(degrees):
                if angle is not None:
                    curves.setdefault(("rotation_euler", axis), []).append((frame, math.radians(angle)))
        return curves, 0
    if obj.rotation_mode != "QUATERNION":
        raise CommandError("bad_argument", f"'{obj.name}' rotates by axis and angle, which rotation keys in degrees "
                                           "cannot set.", "Set its rotation with set_transform first, which "
                                                          "switches it to XYZ Euler, then animate it.")
    if any(None in degrees for _, degrees in rotations):
        raise CommandError("bad_argument", f"'{obj.name}' rotates by quaternion, so each rotation key must set all "
                                           "three axes, and null is not accepted.")
    keys, added = _in_steps(obj, rotations, interpolation)
    previous = None
    for frame, degrees in keys:
        turn = Euler([math.radians(a) for a in degrees], "XYZ").to_quaternion()
        # A quaternion and its negation are the same turn. The one nearer the previous key makes the
        # interpolation take the short way between the two keys.
        if previous is not None and previous.dot(turn) < 0:
            turn = Quaternion([-c for c in turn])
        previous = turn
        for index, component in enumerate(turn):
            curves.setdefault(("rotation_quaternion", index), []).append((frame, component))
    return curves, added


def _read_keys(obj, keys, interpolation):
    """Check the keys a call gives and return them as F-curve points.

    Return ``(curves, shape_curves, added)``: the object's curves and its shape keys' curves, each as
    {(data path, index): [(frame, value)]}, and how many rotation keys a quaternion object's turns
    added. Raise a CommandError for a malformed key, a channel keyed twice on one frame, or keys that
    set nothing.
    """
    if not isinstance(keys, list) or not keys:
        raise CommandError("bad_argument", "'keys' must be a list of keys, each {frame, location, rotation, scale, "
                                           "shape_key}.")
    if len(keys) > MAX_KEYS:
        raise CommandError("bad_argument", f"'keys' holds {len(keys):,} keys, more than the {MAX_KEYS:,} one call "
                                           "takes.")
    curves, shape_curves, rotations = {}, {}, []
    # Each channel's name as a refusal gives it, by (data path, index).
    labels = {}
    for number, key in enumerate(keys):
        where = f"keys[{number}]"
        if not isinstance(key, dict):
            raise CommandError("bad_argument", f"{where} must be a JSON object, got {key!r}.")
        unknown = sorted(set(key) - set(KEY_FIELDS))
        if unknown:
            raise CommandError("bad_argument", f"{where} has {', '.join(unknown)}, which a key does not take.",
                               "A key takes " + ", ".join(KEY_FIELDS) + ".")
        frame = _frame(key, where)
        for field, uniform in (("location", False), ("scale", True)):
            if key.get(field) is not None:
                for axis, value in enumerate(_axes(key[field], field, where, uniform)):
                    if value is not None:
                        curves.setdefault((field, axis), []).append((frame, value))
                        labels[(field, axis)] = f"{field} {'XYZ'[axis]}"
        if key.get("rotation") is not None:
            rotations.append((frame, _axes(key["rotation"], "rotation", where, False)))
        if key.get("shape_key") is not None:
            for name, value in _shape_keys(obj, key["shape_key"], where).items():
                path = f'key_blocks["{bpy.utils.escape_identifier(name)}"].value'
                shape_curves.setdefault((path, 0), []).append((frame, value))
                labels[(path, 0)] = f"shape key '{name}'"
    frames = [frame for frame, _ in rotations]
    if len(set(frames)) != len(frames):
        twice = sorted({f for f in frames if frames.count(f) > 1})
        raise CommandError("bad_argument", f"Frame {twice[0]} keys rotation twice.",
                           "Give each frame of a channel one value.")
    turned, added = _rotation_curves(obj, rotations, interpolation)
    curves.update(turned)
    for (path, index), points in [*curves.items(), *shape_curves.items()]:
        frames = [frame for frame, _ in points]
        if len(set(frames)) != len(frames):
            twice = sorted({f for f in frames if frames.count(f) > 1})
            channel = labels.get((path, index), f"{path}[{index}]")
            raise CommandError("bad_argument", f"Frame {twice[0]} keys {channel} twice.",
                               "Give each frame of a channel one value.")
    if not curves and not shape_curves:
        raise CommandError("bad_argument", "The keys set nothing: each needs location, rotation, scale or shape_key.")
    return curves, shape_curves, added


# ---------------------------------------------------------------------------
# Writing the clip
# ---------------------------------------------------------------------------


def _players(action):
    """Return every use of ``action`` by any datablock, as ("active", owner name) or ("track", track name)."""
    out = []
    for kind in dir(bpy.data):
        blocks = getattr(bpy.data, kind, None)
        if not isinstance(blocks, bpy.types.bpy_prop_collection):
            continue
        for block in blocks:
            data = getattr(block, "animation_data", None)
            if data is None:
                continue
            if data.action == action:
                out.append(("active", block.name))
            out += [("track", track.name) for track in data.nla_tracks for strip in track.strips
                    if strip.action == action]
    return out


def _check_clip_action(clip):
    """Raise a CommandError when an action of the clip's name exists that animate_object cannot use for the clip.

    From Blender 4.4 an action of the clip's name is written into, and before 4.4 it stands beside the clip's
    own actions, so it must be one animate_object made, played only by tracks of the clip. An imported
    or hand-keyed action of that name would come out of the export as a second animation of the clip's
    name. An action linked from a library, or a legacy action without slots, is refused too.
    """
    action = bpy.data.actions.get(clip)
    if action is None:
        return
    hint = "Name the clip differently."
    if action.library is not None:
        raise CommandError("bad_argument", f"Action '{clip}' cannot hold this clip: it is linked from a library.", hint)
    if getattr(action, "is_action_legacy", False):
        raise CommandError("bad_argument", f"Action '{clip}' cannot hold this clip: it is in the form Blender used "
                                           "before 4.4.", hint)
    if action.get(MARK) is None:
        raise CommandError("bad_argument", f"Action '{clip}' was not made by animate_object, so this clip cannot "
                                           "take its name: the export would hold two animations of it.", hint)
    elsewhere = sorted({f"as the active action of '{name}'" if how == "active" else f"on the NLA track '{name}'"
                        for how, name in _players(action) if (how, name) != ("track", clip)})
    if elsewhere:
        raise CommandError("bad_argument", f"Action '{clip}' is also played outside the clip's tracks: "
                                           + ", ".join(elsewhere[:6]) + ".", hint)


def _clip_action(clip, owner, id_type, undo):
    """Return the action and slot that the new keys of ``owner`` go into, making them and recording them in ``undo``.

    Before Blender 4.4 the slot is None and each call makes a new action. From 4.4 the clip's action is
    reused when it exists, and gets a new slot for ``owner``, plus a layer and a keyframe strip if it
    has none.
    """
    if not SLOTTED:
        # Before Blender 4.4 each datablock gets an action of its own. When the name is taken, Blender adds a
        # suffix such as .001, which the file never shows, because the exporter names a merged animation
        # after its track.
        action = bpy.data.actions.new(clip)
        undo.add(lambda: bpy.data.actions.remove(action))
        action.id_root = id_type
        action[MARK] = clip
        return action, None
    action = bpy.data.actions.get(clip)
    if action is None:
        action = bpy.data.actions.new(clip)
        undo.add(lambda: bpy.data.actions.remove(action))
        action[MARK] = clip
    slot = action.slots.new(id_type=id_type, name=owner.name)
    undo.add(lambda: action.slots.remove(slot))
    if not action.layers:
        layer = action.layers.new("Layer")
        undo.add(lambda: action.layers.remove(layer))
    layer = action.layers[0]
    if not layer.strips:
        strip = layer.strips.new(type="KEYFRAME")
        undo.add(lambda: layer.strips.remove(strip))
    return action, slot


def _fcurves(action, slot):
    """Return the F-curves of one datablock in the clip: the action's own before 4.4, the slot's channelbag from 4.4."""
    if slot is None:
        return action.fcurves
    return action.layers[0].strips[0].channelbag(slot, ensure=True).fcurves


def _write_curves(fcurves, curves, interpolation, loop):
    """Write each curve's points into ``fcurves``, with a Cycles F-curve modifier on each curve when the clip loops."""
    for (path, index), points in curves.items():
        points = sorted(points)
        curve = fcurves.new(path, index=index)
        curve.keyframe_points.add(len(points))
        curve.keyframe_points.foreach_set("co", [c for point in points for c in point])
        for point in curve.keyframe_points:
            point.interpolation = interpolation
        if loop:
            curve.modifiers.new("CYCLES")
        curve.update()


def _push_down(owner, clip, action, slot, frames, undo):
    """Add an NLA track named after the clip to ``owner``, holding one strip of the action, and return the track.

    The strip plays the datablock's own keys, from the first to the last of ``frames``, and starts at
    the first, so each key plays at the frame it was keyed on. From Blender 4.4 the action's own frame range
    is no guide: it spans every slot's keys, those of every object in the clip, while a strip given a slot
    plays only that slot's keys. Every change is recorded in ``undo``.
    """
    first, last = frames
    # Blender gives a strip at least one frame, as it does an action keyed on a single frame.
    last = max(last, first + 1)
    data = owner.animation_data
    if data is None:
        data = owner.animation_data_create()
        undo.add(owner.animation_data_clear)
    track = data.nla_tracks.new()
    undo.add(lambda: data.nla_tracks.remove(track))
    track.name = clip
    strip = track.strips.new(clip, first, action)
    if slot is not None:
        strip.action_slot = slot
    # Blender clamps each end of the strip's action range to the other, so the end is moved out first,
    # then the start is set, and then the end is set to its final frame.
    strip.action_frame_end = max(strip.action_frame_end, last)
    strip.action_frame_start = first
    strip.action_frame_end = last
    strip.frame_start_ui = first
    strip.name = clip
    return track


def _part(owner, clip):
    """Return the NLA tracks of ``owner`` named after the clip, each with the (action, slot) of each strip.

    Return [] when ``owner`` is None or has no animation data.
    """
    data = owner.animation_data if owner is not None else None
    if data is None:
        return []
    return [(track, [(strip.action, getattr(strip, "action_slot", None)) for strip in track.strips])
            for track in data.nla_tracks if track.name == clip]


def _slot_in_use(action, slot):
    """Return whether any object or shape key datablock still plays this slot of the action.

    A slot counts as played when it is the active action's slot or the slot of an NLA strip.
    """
    for owner in [*bpy.data.objects, *bpy.data.shape_keys]:
        data = owner.animation_data
        if data is None:
            continue
        if data.action == action and getattr(data, "action_slot", None) == slot:
            return True
        if any(strip.action == action and strip.action_slot == slot
               for track in data.nla_tracks for strip in track.strips):
            return True
    return False


def _take_away(owner, old):
    """Remove the clip's old tracks from ``owner``, then each slot or action that nothing plays after that.

    From Blender 4.4 a slot that no datablock plays is removed from its action. Before 4.4 an action left
    with no users is removed.
    """
    data = owner.animation_data
    for track, strips in old:
        data.nla_tracks.remove(track)
        for action, slot in strips:
            if action is None:
                continue
            if slot is not None and not _slot_in_use(action, slot):
                action.slots.remove(slot)
            elif not SLOTTED and action.users == 0:
                bpy.data.actions.remove(action)


def _channels(action, slot):
    """Return the channels Blender holds for one datablock in the clip, as {data path: sorted array indices}."""
    fcurves = action.fcurves if slot is None else getattr(
        anim_utils.action_get_channelbag_for_slot(action, slot), "fcurves", [])
    out = {}
    for curve in fcurves:
        out.setdefault(curve.data_path, []).append(curve.array_index)
    return {path: sorted(indices) for path, indices in out.items()}


def animated_paths(owner):
    """Return the data paths that a datablock's animation moves.

    They are the paths of its drivers and of the F-curves of its active action and its NLA strips: the
    action's own F-curves before Blender 4.4, the slot's channelbag from 4.4. From 4.4 an action played
    without a slot moves nothing, so it is skipped.
    """
    data = getattr(owner, "animation_data", None)
    if data is None:
        return set()
    paths = {driver.data_path for driver in data.drivers}
    played = [(data.action, getattr(data, "action_slot", None))]
    played += [(strip.action, getattr(strip, "action_slot", None)) for track in data.nla_tracks
               for strip in track.strips]
    for action, slot in played:
        if action is not None and (slot is not None or not SLOTTED):
            paths |= set(_channels(action, slot if SLOTTED else None))
    return paths


def animate_object(params):
    """Key an object's transform or shape keys into a named clip, replacing what the clip held for it."""
    obj = get_object(require(params, "object"))
    clip = _clip_name(params)
    interpolation = text(params, "interpolation", "linear")
    if interpolation not in INTERPOLATIONS:
        raise CommandError("bad_argument", f"'interpolation' takes one of {', '.join(INTERPOLATIONS)}, got "
                                           f"{interpolation!r}.")
    asked_loop = params.get("loop")
    if asked_loop is not None and not isinstance(asked_loop, bool):
        raise CommandError("bad_argument", f"'loop' must be true or false, got {asked_loop!r}.")
    if obj.mode != "OBJECT":
        raise CommandError("bad_argument", f"'{obj.name}' is in {mode_name(obj)} mode, so it cannot be animated.",
                           mode_hint(obj))
    if obj.library is not None:
        raise CommandError("bad_argument", f"'{obj.name}' is linked from a library, so it cannot be animated here.")
    curves, shape_curves, steps_added = _read_keys(obj, require(params, "keys"), INTERPOLATIONS[interpolation])
    _check_clip_action(clip)
    loop = godot_loops(clip) if asked_loop is None else asked_loop
    shapes = obj.data.shape_keys if shape_curves else None
    shape_owner = getattr(obj.data, "shape_keys", None) if obj.data is not None else None
    old = {"object": _part(obj, clip), "shape_keys": _part(shape_owner, clip)}

    parts = []
    with Rollback() as undo:
        for owner, id_type, owner_curves in ((obj, "OBJECT", curves), (shapes, "KEY", shape_curves)):
            if not owner_curves:
                continue
            keyed = [frame for points in owner_curves.values() for frame, _ in points]
            frames = (math.floor(min(keyed)), math.ceil(max(keyed)))
            action, slot = _clip_action(clip, owner, id_type, undo)
            _write_curves(_fcurves(action, slot), owner_curves, INTERPOLATIONS[interpolation], loop)
            parts.append((owner, action, slot, _push_down(owner, clip, action, slot, frames, undo)))

    # Everything new is in place, so the clip's old part is removed, and the new tracks, strips, actions
    # and slots take their final names.
    _take_away(obj, old["object"])
    if shape_owner is not None:
        _take_away(shape_owner, old["shape_keys"])
    for owner, action, slot, track in parts:
        track.name = clip
        track.strips[0].name = clip
        if slot is None and action.name != clip and bpy.data.actions.get(clip) is None:
            action.name = clip
        if slot is not None and hasattr(slot, "name_display") and slot.name_display != owner.name:
            slot.name_display = owner.name
    update()

    frames = [frame for points in [*curves.values(), *shape_curves.values()] for frame, _ in points]
    scene = bpy.context.scene
    fps = scene.render.fps / scene.render.fps_base
    out = {
        "object": obj.name,
        "clip": clip,
        "actions": sorted({action.name for _, action, _, _ in parts}),
        "frames": [min(frames), max(frames)],
        "seconds": rnd((max(frames) - min(frames)) / fps, 4),
        "fps": rnd(fps, 4),
        "keys": len(params["keys"]),
        "channels": {("shape_keys" if owner == shapes else "object"): _channels(action, slot)
                     for owner, action, slot, _ in parts},
        "interpolation": interpolation,
        "loop": loop,
        "replaced": bool(old["object"] or old["shape_keys"]),
        "clips": sorted({track.name for owner in (obj, shape_owner) if owner is not None and owner.animation_data
                         for track in owner.animation_data.nla_tracks}),
    }
    warning = _loop_warning(clip, loop) if asked_loop is not None else None
    if warning:
        out["warnings"] = [warning]
    if steps_added:
        out["note"] = (f"'{obj.name}' rotates by quaternion, which takes the short way between keys. So that it "
                       "turns the way the degrees say, each turn of 180 degrees or more was keyed in steps of at "
                       f"most {QUATERNION_STEP:g} degrees, adding {plural(steps_added, 'key')} between the keys "
                       "given.")
    return out


def _loop_warning(clip, loop):
    """Return a warning when Godot will play the clip differently from what ``loop`` says, else None."""
    if loop == godot_loops(clip):
        return None
    if loop:
        return {"code": "godot_plays_once", "message": f"Godot imports '{clip}' playing once, because it loops an "
                "animation by its name. Give the clip a name ending in -loop, or set its loop mode in Godot's import "
                "settings."}
    return {"code": "godot_loops", "message": f"Godot imports '{clip}' looping, because the name begins or ends with "
            "loop or cycle. Rename the clip, or set its loop mode in Godot's import settings."}
