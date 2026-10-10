# 0034 — Animation clips, with and without slotted actions
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`animate_object` keys an object's transform and shape keys into a named clip.** It is in the CHANGE annotation
set (decision 0020).

- **A clip is a glTF animation.** Each object's part in a clip is an NLA (non-linear animation) track named after
  the clip, holding one strip; shape keys get a track of the same name on the mesh's shape key datablock. The active
  action is left alone. `export_glb` writes every such track as an animation (its `animations` parameter, on by
  default, leaves the exporter's mode at `ACTIONS`, the default in 4.2 and 5.0).
- **Keys** take the form `{frame, location, rotation, scale, shape_key: {name: value}}`. Frames are whole numbers
  from 0 to 1,048,574. Locations are metres in the parent's space, rotations are degrees, and scales are factors
  (one number sets all three axes). A `null` component leaves that axis unkeyed at that frame, so a rotor can key
  Z alone. A frame may key each channel only once. A call takes at most 10,000 keys.
- **Rotation** keys `rotation_euler` in the object's own Euler order. An object rotated by quaternion (the mode the
  glTF importer sets) is keyed in `rotation_quaternion` instead: each key is converted from XYZ degrees and
  flipped into the same hemisphere as the key before it, and such a key must give all three axes. Blender
  interpolates a quaternion the short way between two keys. So where two keys turn 180 degrees or more apart (the
  three axes' turns added up), the add-on puts keys between them, at proportional frames, no two more than 90
  degrees apart. Keys at 0 and 360 degrees then turn the object all the way round, as they do an Euler object.
  Constant keys need none, since each holds until the next. The result's `note` says how many were added; more
  than 10,000 in all is refused. Axis-angle rotation is refused with a hint to call `set_transform`.
- **Shape keys** must already exist (the hint says that `run_python` makes them). The basis cannot be keyed, and a
  value must lie within the key's slider range.
- **`interpolation`** is `linear` (the default), `constant` or `bezier`, and applies to every key.
- **`loop`** adds a Cycles modifier (an F-curve modifier, not the renderer) to each F-curve, so the clip repeats
  in Blender. The exported duration is the keys' range either way. If `loop` is left out, it follows the clip's
  name the way Godot does. If it is given and differs from what Godot will do, the result warns with
  `godot_loops` (Godot loops a clip of that name, by the rule below) or `godot_plays_once`.
- **A call states the object's whole part in the clip**, replacing its old tracks for the clip. The new keys are
  written beside the old, and only when all are in place are the old tracks removed, with the slots (Blender 4.4
  and later) or actions (before 4.4) that nothing plays any more. A call refused partway leaves the clip as it was.
- **Several objects keyed into one clip play together.** The exported file holds one animation of that name with
  each object's channels. Each datablock's strip starts at its own first key and plays only its own keys (the
  strip's action range is set to them), so an object that joins a clip at frame 20 moves at frame 20, both in
  Blender and in the file.
- **A clip's name belongs to `animate_object`.** Every action it makes carries the custom property
  `blenderlens_clip`. If an action with the clip's name lacks that property (one the user keyed, or one an import
  named), or if anything other than the clip's own tracks plays it (as an active action, or on a track of another
  name), the call is refused before anything changes, with a hint to name the clip differently. Otherwise, with
  slotted actions the clip would be written into that action, and in either layout the export would hold two
  animations with the same name.
- **The add-on chooses its layout by `bpy.app.version`.** Blender 4.4 brought slotted actions, and 5.0 removed
  `Action.fcurves`.
  - **Before 4.4** (4.2 and 4.3): one action per datablock, with its `id_root` set (`OBJECT` or `KEY`) and its
    F-curves made through `Action.fcurves.new`. The object's action and the shape keys' action sit on tracks named
    after the clip. An action takes the clip's name when that name is free.
  - **4.4 and later**: one action named after the clip, with a slot per datablock (`action.slots.new(id_type,
    name)`) and one layer holding one keyframe strip. F-curves are made through `strip.channelbag(slot,
    ensure=True)`, and each NLA strip is given its slot (`strip.action_slot`). The channels in the result are read
    back through `bpy_extras.anim_utils.action_get_channelbag_for_slot` (before 4.4, through `Action.fcurves`).
- **The result** gives the actions, frames, seconds at the scene's frame rate, the channels Blender holds per
  datablock after the call (`{data path: [indices]}`), interpolation, loop, whether a part was replaced, the
  object's clips, and any warnings.

## Why

**Each Blender version's glTF exporter merges clips in its own way, and the layout follows it.**
- The 4.2 exporter (`io_scene_gltf2/blender/exp/animation/gltf2_blender_gather_action.py` and
  `gltf2_blender_gather_animation_utils.py` at v4.2.0) collects the active action and each single-strip NLA track of
  an object and of its shape keys. It then merges the animations of tracks with the same name (any name except
  `NlaTrack…` and `[Action Stash]`) and names the result after the track. In 4.2 the object's action and the shape
  keys' action cannot be one action, but they come out as one animation because their tracks share the clip's name.
  Two objects' tracks of the same name merge the same way.
- The 5.0 exporter (`blender/exp/animation/action.py`) gathers actions with their slots, joins the slots of one
  action into one animation, and, with `export_merge_animation` at its default `ACTION`, merges by action name
  across objects. Two actions cannot share a name, so in 5.0 the clip must be one action with a slot for each
  datablock. The add-on uses the same layout in 4.4 and 4.5, which have slotted actions too. On 5.0.1, a door's two
  clips export as `DoorOpen` (1.25 s) and `DoorClose`, and a clip on an object and its shape keys exports as one
  animation with `translation` and `weights` channels.
- The 4.2 API is the one in Blender's source at v4.2.0: `rna_action.cc` (`Action.fcurves.new(data_path, index,
  action_group)`, `id_root`, `frame_range`), `rna_nla.cc` (`NlaStrips.new(name, start, action)`, which keeps the
  action's range and moves it to `start`), `rna_animation.cc` (`NlaTracks.new(prev)`), and
  `bpy_extras/anim_utils.py`. The slotted API is the one at v4.4.0: `rna_action.cc`
  (`ActionSlots.new(id_type, name)`, `ActionLayers.new`, `ActionStrips.new(type)`,
  `ActionKeyframeStrip.channelbag(slot, ensure)`, `ActionChannelbagFCurves.new(data_path, index)`), `rna_nla.cc`
  (`NlaStrip.action_slot`) and `anim_utils.py` (`action_get_channelbag_for_slot`). CI runs the tests on Blender
  4.2.23, which takes the path for Blender before 4.4, as well as on 5.0.1.

**Godot loops an animation by its name, in two places.** Its glTF importer (`GLTFDocument::_parse_animations`, 4.3
to 4.7) loops an animation whose name, in lower case, begins or ends with `loop` or `cycle`. Its scene importer
(`ResourceImporterScene`, `_teststr`) first strips trailing digits, underscores and spaces from the name, then
loops the animation when `loop_mode`, `loop` or `cycle` follows `-` or `_` at the end, or follows `$` anywhere,
and drops that word from the name. The server's read-back of an exported file (`glb.godot_loops`, decision 0024)
applies both tests. The add-on cannot import the server's code, so it holds its own copy of them
(`motion.godot_loops`), and a unit test checks the two copies against each other. Neither glTF nor Godot reads a
loop flag from the file, so the warning is the only place where a disagreement shows.

**Whole frames.** The exporters work in whole frames (`prepare_actions_range` takes `int(frame_range)`) and slide
negative frames, so a fractional or negative frame would not come out where it was keyed.

**Replacing, not merging.** An agent that corrects a clip calls again with the keys it wants. Merging would keep
old keys at frames the new call does not name, a clip neither call described. Replacement is safe to retry, and is
still CHANGE: the call removes what the object held for the clip.

**Each strip's range is its datablock's keys.** A strip made with `NlaStrips.new(name, start, action)` takes the
action's range, and with slotted actions, assigning its slot gives it the slot's range instead.
`rna_NlaStrip_action_start_frame_set` and `_end_frame_set` (v4.2.0) clamp each end against the other, so the add-on
moves the end out first. `frame_start_ui` (v4.2.0) moves a strip while keeping its length. Without this, with
slotted actions a strip would start where the whole action does while Blender plays the slot's own range. An object
keyed at frames 20 to 30, in a clip that another object starts at 0, would then play those keys at frames 0 to 10,
and Blender and the exported file would disagree.

**The clip's mark.** Without it, a clip named after the user's own action "Open" (as a glTF import names one) would
be written into that action as a new slot, and the export would hold two animations whose durations disagree with
the result.

**The in-between keys.** Without them, an object rotated by quaternion and keyed at 0 and 360 degrees does not move.

The tests read Blender's evaluated position and shape key at frames of the object that joins the clip late, and the
file's samples at the same times. They turn a quaternion object all the way round, with linear and with bezier
interpolation, in Blender and in the file. They refuse a clip named after the user's own action, and a clip whose
action another object plays as its active action.
