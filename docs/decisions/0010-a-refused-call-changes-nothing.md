# 0010 — A refused call changes nothing
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

A command that is refused leaves the open file exactly as it found it, and does not mark the file as changed.

- **Check first.** Every command reads and checks all of its parameters (types, finiteness, bounds, and names
  that must exist) before it changes anything. `create_object` and `create_mesh` check location, rotation,
  scale, parent and collection before the object is linked into the scene. `set_material` chooses the faces
  before the material is created or changed. `set_object`, `set_transform`, `set_collection`, `set_modifier`
  and `edit_mesh` check every key before they set the first one. `import_file` checks `collection`, and that
  no object is in edit mode, before it imports.
- **Flags are true or false.** The add-on reads every flag through `common.boolean`, and the server reads the
  flags of `open_session` and `close_session` through its own `_flag`. So `"false"`, `"no"` or `0` is refused
  with `bad_argument`, rather than read the way Python reads it: `bool("false")` is true. The add-on checks
  its own flags even though the server checks arguments against the schema, because in live mode (a Blender
  window running the add-on) the add-on can be reached without going through this server.
- **Objects stay in the scene.** A collection that objects are put into (by `set_collection`, by the
  `collection` of `create_object` and `create_mesh`, or by that of `import_file`) must be in the scene. An
  existing collection that the scene does not contain is refused, with a hint naming `parent`, unless
  `set_collection` is given a `parent` to link it under. A collection cannot be its own parent.
- **Put back what Blender refuses.** Some refusals can only come after a change:
  - Blender rejects some values only when they are assigned.
  - A modifier's settings can only be resolved against the modifier itself.
  - `set_transform`'s `dimensions` keep the size after the new `scale` is set.
  - What a modifier makes can only be counted once it is built.

  These steps run inside a `Rollback` (in `addon/common.py`). The rollback records how to reverse each step: it
  removes the object, mesh, material, collection or modifier made, restores each attribute set, and relinks
  what was unlinked. When anything escapes, it runs those reversals newest first. Each step is recorded as it
  happens, so a collection created inside `get_collection` is removed even when linking it fails a moment
  later. After putting values back, the rollback updates the view layer, so that `matrix_world` and the
  evaluated meshes match the restored values again. A failed import removes every datablock created since it
  started, and restores the scene's custom properties. A datablock pointer that Blender silently drops (a
  boolean modifier's `object` set to an empty) is read back after it is set, and refused.
- **Marked changed only when it may have changed.** The dispatcher counts a call as a change, which marks the
  file unsaved, in these cases:
  - a command that changes things finished;
  - `run_python` ran, whatever happened, since its code may have changed anything before it raised, unless the
    undo watch saw nothing change at all. The undo watch (decision 0031) is the add-on's record of every
    depsgraph update, load and undo during a call, with a comparison of the file's datablocks before and after;
  - `run_script`'s code ran;
  - a rollback step could not be undone (`Rollback.failures` rose);
  - an error other than a refusal escaped a command that changes things, with no rollback vouching for it.

  A refusal (`CommandError`), or an error for which a rollback put everything back, is not a change.
- **For a change, the undo push happens in `finally`.** In both modes, the undo push for a call that may have
  changed the file happens in the `finally` block of `dispatch`, whether or not the command finished. So a
  half-change that Blender could not take back (or one made by `run_python` code that raised) becomes its own
  undo step. Like every step, it is named after the call (decision 0031), with " (refused)" added. A refusal
  that put everything back pushes no step: it has nothing to undo, and in a Blender window an undo push itself
  marks the file as modified.
- **The test pattern** (`tests/test_refusals_change_nothing.py`): save the file; snapshot `get_scene`,
  `list_materials`, `get_object` for every object, and the raw state (every datablock, each mesh's digest,
  material slots, modes, selection, the scene's custom properties, the frame, render and image settings, and
  view layers); make the refused call; check that the file is still saved; and compare.

`run_python` is the exception by nature: code that raises partway has already done what it did. Its failure is
still its own undo step, and it still counts as a change.

## Why

Without these rules, refusals are half-applied, as reproduced against Blender 5.0.1. `create_object` with an
unknown parent, or with a two-component location, links the object before refusing, so a scene the agent
believes unchanged gains objects `A`, `B` and `C`. `set_material` with `faces: [99]` adds the material, and a
slot covering all six faces, before refusing. `set_object` with a property of 2**40 renames and hides the
object before Blender raises `OverflowError`. An agent that reads a refusal retries, and each retry leaves
another copy.

Checking first covers almost every refusal, at no cost. The rollback covers what checking cannot predict. The
undo push in `finally` covers what neither does: without it, a refused call pushes no undo step, so its
half-change is folded into the next call's step, and one Ctrl+Z takes back both.

The strict flags, the rule for counting a change and the reach of the rollback prevent further failures that a
looser design shows:
- `bool()` reads `with_children: "false"` as true and deletes the children, and `create_file` with `discard:
  "false"` drops unsaved work.
- If every refused call marks the file as changed, the next `create_file` demands `discard` on a file nothing
  has touched. In a Blender window, a clean refusal that pushes a "(refused)" undo step does the same, because
  Blender's `ED_undo_push` tags the file as modified. A live test checks this.
- A refused `set_transform` that puts the rotation back but leaves `matrix_world` at the refused one makes the
  next `edit_mesh` extrude the wrong face.
- `set_collection` with a collection as its own parent leaves an orphan collection.
- Moving objects into a collection outside the scene takes them out of the scene, and the call reports
  success.
- A failed glTF import leaves the file's scene extras in the open scene's custom properties.
- A boolean modifier given an empty keeps no object, and the call reports success.

The snapshot runs on a saved file and covers the scene's own state, so a refusal that marks the file changed,
or leaves a frame, a format or a scene property behind, fails the test.
