# 0012 — Edit mode
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

Edit mode, Blender's mode for editing a mesh's vertices, edges and faces directly, is handled and never
misreported. The user enters and leaves it with Tab.

- **Reads see the edit.** `get_scene`, `get_object`, `get_mesh` and `duplicate_object` first call
  `update_from_editmode()` on every object in edit mode (`common.sync_edit_mode`), so they read, and copy, the
  mesh as the user is editing it.
- **`edit_mesh` works on the mesh being edited** and reports its counts from that BMesh. All its parameters
  are checked before the BMesh is touched (decision 0010), since a refusal halfway would leave a half-made
  edit in the user's own mesh. The `angle` of `shade_smooth` marks sharp edges on the BMesh by the same rule
  as `Mesh.set_sharp_from_angle`, so the result survives leaving edit mode.
- **`export_glb`, `apply_modifier` and `apply_transform` refuse** with `bad_argument` and the hint "Leave edit
  mode (Tab), then call again." when a target is in edit mode. They also refuse when the active object is in
  edit mode, or in another mode that is not object mode (with the hint "Return to object mode, then call
  again."), even when it is not a target. `set_material` on an object in edit mode is refused in the same way,
  before anything changes.
- **`import_file` refuses** in the same way while any object is in edit mode or another mode that is not
  object mode, because the importers switch the active object to object mode and make what they import active.
- **The user keeps their place.** `create_object`, `create_mesh` and `duplicate_object` select what they make,
  but they leave the active object alone while it is in edit, sculpt or paint mode, and they leave objects in
  those modes selected, so that Tab still acts on the object the user is editing.

## Why

In edit mode, Blender keeps the mesh being edited in a separate BMesh, and copies it into the mesh data only
when edit mode ends. Without these rules, that affects every tool: an extrude in edit mode reports the old
counts (6 faces, not 10); `get_mesh` and `duplicate_object` read and copy the stale mesh; and `export_glb`
crashes inside the glTF exporter with a UV length mismatch. In live mode, the agent works in the user's own
Blender window, and a user who presses Tab there is the normal case, not an edge case.

Refusing the three operations is safer than leaving edit mode on the user's behalf. The glTF exporter calls
`mode_set(mode='OBJECT')` on the active object and never switches back
(`io_scene_gltf2/blender/exp/export.py`). Blender's apply operators fail their poll in any mode but object
mode, whichever object is in that mode: on Blender 5.0.1, `transform_apply` on a cube in object mode fails
while another object is in edit mode. Taking the user out of edit mode without telling them would lose their
place, while the refusal says exactly which key to press.

The rule about the active object goes further than "when a target is in edit mode", because exporting one
object while the user edits another would otherwise switch the user's object to object mode.

The last two rules prevent two more failures. If a building tool makes its new object active, an object in
edit mode stops being the active one, and leaving edit mode (`mode_set` to object mode, as Tab does) then acts
on the new object while the user's object stays in edit mode. The same happens in sculpt, weight paint and
vertex paint mode. And without its check, `import_file` switches the edited object to object mode without a
word.
