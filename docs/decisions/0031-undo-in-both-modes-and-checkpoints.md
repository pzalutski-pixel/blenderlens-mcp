# 0031 — Undo in both modes, and checkpoints
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**Every call that may have changed the file pushes one undo step**, in background mode (a Blender the server
starts without a window) as in live mode (a Blender window running the add-on). The step is named
`BlenderLens: <tool> <object>` after the object the call acted on: its `object`, the first of its `objects` with
`+N` for the rest, or the object that `create_object` or `create_mesh` made. A call that acted on no object
names the step `BlenderLens: <tool>`. A call that failed but may still have changed something (a rollback that
failed, or Python code that raised) adds ` (refused)`. When to push is decision 0010's rule: exactly when the call
may have changed something (a finished change, a rollback that failed, `run_python` unless it changed nothing at
all, `run_script` once its code ran). Reads, exports, previews and refusals that put everything back push
nothing.

- **The first step.** In live mode, Blender itself pushes a step named "Original" when a file loads. In
  background mode it pushes none, so the first change could not be undone; there the add-on pushes "Original"
  before the first change after each load (`run_script` with `fresh` pushes it after emptying the file). A call
  during which a file loaded (`run_python` code opening one) pushes, in background mode, that "Original" instead
  of a step with its own name, because the steps before it belong to the other file. In live mode Blender pushed
  "Original" as the file loaded, and the call's own step goes on top.
- **`undo_change`** (in the CHANGE set) undoes `steps` steps (default 1, at most 256) with `bpy.ops.ed.undo`.
  With `list: true` it undoes nothing and instead names the steps, with `by_name`: how many of the newest it can
  undo by name. It returns the names `undone`, the `steps` left (newest first), `counts` (objects, meshes,
  materials, vertices, faces) and `unsaved_changes`. It pushes no step of its own: a step pushed after an undo
  would be the next one undone, so a second `undo_change` would take back nothing.
- **Back to the save.** `save_file`, `open_file` and a new file record where the steps stand: the newest step's
  id, or the file's own load. In background mode, an undo that comes back to that position, with no step above
  it that BlenderLens did not push and none undone that it cannot name, leaves the file saved again:
  `unsaved_changes` is false, and `close_session`, `open_file` and `create_file` need no `discard`. A new step
  after the undo is a change again, and undoing it comes back once more. The position is forgotten when the
  saved state passes out of the steps Blender keeps, or when an undo takes back a step it cannot name or a step
  whose code pushed steps of its own. In live mode the position is not used: after `undo_change` the file
  counts as unsaved.
- **The names come from BlenderLens's own list.** Python can push a step, undo one, and ask whether there is one
  to undo, but it cannot read the steps' names. So the add-on keeps the names of the steps it pushed since the
  file was loaded, newest last, trimmed to the preference `undo_steps` (Blender keeps that many steps besides
  the first). It forgets them whenever a file loads: a `load_post` handler, added once, catches a load made by a
  tool, by `run_python` code, or by the user.
- **A step is named only when BlenderLens can vouch for it.** Steps that BlenderLens did not push are not on its
  list, so the add-on watches for what makes them (`addon/undo.py`):
  - *Between calls*, a `depsgraph_update_post`, an `undo_post` or `redo_post`, or a new newest entry in the
    window manager's operator history (where Blender lists the operators a user runs with undo; an operator
    called from Python is not listed unless it asks to push its own step) is an edit outside BlenderLens, in live
    mode the user's. The step BlenderLens pushed last is marked as having one above it. Each call starts by
    bringing the depsgraph up to date, so a change still waiting for it counts as made before the call, and ends
    the same way, so the call's own updates count as its own.
  - *During a call*, agent code (Python the agent sends through `run_python` or `run_script`) that pushes, undoes
    or redoes steps itself leaves steps beneath the call's own. The signs are code that names Blender's undo
    operators (`ed.undo`, `ed.redo`, the undo push, `undo_history`), an `undo_post` or `redo_post` during the
    call, or an operator that listed itself in the history. The step below the call's is marked, and undoing the
    call's own step says that it took back only what the code did after its own last step.
  - Without `force`, `undo_change` undoes by name only the newest steps that have no such edit above them. A
    request to go past them is refused and nothing is undone. The refusal names the step above which the file was
    changed outside BlenderLens's calls (by hand, in live mode), or agent code pushed or undid steps itself, and
    says how many of the newest steps can be undone by name. Its hint offers `force: true`, and in live mode
    Ctrl+Z as well. In background mode the list holds every step there is except those agent code made, and a
    `steps` count past it is refused, naming the steps there are. Nothing ever undoes past the file's own load.
  - With `force: true`, it undoes up to `steps` steps as Ctrl+Z does, stopping when Blender has nothing left to
    undo. A step it can vouch for is named. Any other is reported as "a step BlenderLens cannot name", and
    BlenderLens's own step below such edits stays on the list, marked, since nothing tells which of the two an
    undo took back. In live mode the results carry a `note` saying that the names are BlenderLens's.
- **A `run_python` call that changes nothing is no step.** The same watch, kept for the call, tells whether it
  changed anything: no depsgraph update, no load, no undo or redo, no undo code, nothing new in the operator
  history, `bpy.data.is_dirty` as it was, and the same fingerprint of what the depsgraph does not see. The
  fingerprint covers every datablock's name and custom properties, each text's content, each scene's frame and
  each mesh's counts; past 100,000 datablocks it is not taken, and the call counts as a change. Such a call
  pushes no step and leaves `unsaved_changes` as it was, so a read in live mode leaves the user's saved file
  saved.
- **`undo_change` is refused in edit mode** (decision 0012), with the hint to press Tab, because there Blender's
  undo steps through the mesh edit's own history, not the calls.
- **Python references go stale.** An undo replaces the file's data, so the add-on removes every name in
  `run_python`'s namespace that holds Blender data (any `bpy_struct` but the context, a `bpy_prop_collection` or
  `bpy_prop_array`, or a `mathutils` vector, matrix or similar value still bound to a property). It finds such
  data within reach: in lists, tuples, sets and dicts and in the attributes of objects, nested up to 6 levels,
  1,000 items per container and 20,000 values per name (classes, modules and functions are not searched). The
  result lists the names in `python_names_cleared`, so the next use is a `NameError` naming them rather than a
  `ReferenceError` or a read of freed memory.

**A `save_file` in background mode leaves no `.blend1` backup** beside the file. The add-on sets the preference
`save_version` to 0 for the save and puts it back afterwards, marked unchanged, so nothing about it is saved. In
live mode the user's own preference applies.

**`save_file` with `copy: true` writes a checkpoint**, a copy of the open file, with
`bpy.ops.wm.save_as_mainfile(copy=True)` to `path`. `path` is required and must not be the open file's own
path. The open file keeps its path and its unsaved state. The result gives the copy's `path` and `bytes`, and the
session's `file` and `unsaved_changes`.

## Why

**A background Blender can undo.** In Blender 5.0.1 in background mode, steps pushed with `ed.undo_push` can be
undone and redone: an extrude's 54 faces go back to 6. Blender's source explains why it can appear not to, and
what a tool must get right:
- `ed_undo_push_exec` creates the undo stack in background on the first explicit push (`ed_undo.cc`; the same at
  v4.2.0). Before that there is no stack, and the poll of `ed.undo` fails.
- After `read_homefile` or `open_mainfile` in background, the first push is the bottom of the stack: the undo
  poll is false with one step, so the first change cannot be undone until a step is pushed under it.
  `wm_file_read_post` initialises the stack only `if (!G.background)` (`wm_files.cc` at v4.2.0), which is why a
  Blender with a window has "Original" and a background Blender does not.
- An operator called from Python pushes no step of its own: `WM_operator_call_py` raises the undo depth unless
  the call passes its `undo` argument (`wm_event_system.cc` at v4.2.0, "so Python never triggers undo"). Two
  cubes joined with `bpy.ops.object.join()` between two pushes go back to two cubes in one undo. So one call
  is one step even when it uses operators.
- With `undo_steps` at 32, 40 pushes leave 32 undos.
- `load_post` fires for `read_homefile` in background, so the handler sees a new file made by a tool as well as
  one the user opens.
- After an undo, an object reference held across it raises `ReferenceError: StructRNA of type Object has been
  removed`. `bpy.data` and the collections reached through it are replaced as well, and not all of their old
  wrappers are checked, which is why such names are removed rather than left to fail on their next use.

**Names kept by BlenderLens, and no claim it cannot vouch for.** Blender's undo history is drawn by C code
(`TOPBAR_MT_undo_history`), and no type of Blender's Python API reaches the stack; `ed.undo_history` takes an index,
not a name. Keeping the list is the only way to name steps. The add-on cannot see the steps of a live-mode user or
of agent code, but it can see what makes them, and it stops there.

- **What the watch sees.** In background mode on 5.0.1, after `view_layer.update()`, `depsgraph_update_post`
  fires for a moved object, a new or removed datablock, a material's colour or node, a new UV map, smooth
  shading, a moved vertex (directly, through bmesh or through `foreach_set`), a selection, a rename, a modifier,
  a keyframe, and a render setting. It does not fire for a custom property (on an object, a mesh or the scene),
  a text written, `frame_set`, `vertices.add` or an undo push, hence the fingerprint beside it. In live mode,
  the tools' own work and the idle time between calls leave every step nameable, previews and exports included.
  An edit made between calls that pushes its own undo step, such as a move run by a timer, marks the newest step:
  `undo_change` is then refused, and with `force` it undoes the move.
- **Why watch for edits outside a call, not the user's steps.** The depsgraph also updates for things that push
  no step (a frame change), so the watch can refuse when only the frame moved; `force` and Ctrl+Z are there for
  that. The other way round, an edit that pushed a step but neither updated the depsgraph nor listed an operator
  would not be seen. The undo push that agent code makes from Python is one such edit, which is why the code is
  read for the undo operators' names; code that reaches them some other way is not seen. Refusing where a step
  might lie is the side that never undoes the user's work under BlenderLens's name.
- **What goes wrong without the watch.** In live mode `undo_change` would undo the user's own move and name it
  "BlenderLens: create_object". After a `run_python` call that pushed its own undo steps, `undo_change` would
  name two calls while undoing one of them, leaving the object in place. A `run_python` call that opened a file
  would leave a step with its name under the new file's "Original", which undoes nothing of it. A read-only
  `run_python` call in live mode would push an empty step and mark the user's saved file modified. And names
  nested in a dict of lists, or held in an object's attribute, would survive the undo.
- **Back to the save, objects in the names, no backups.** Without the saved position, a change and its undo
  leave a file equal to its last save, yet `unsaved_changes` would read true and `close_session` would refuse,
  asking for "save_file with a path" of a file that has one. Without the object in each name, 32 steps named
  `set_transform`, `duplicate_object` and the like give no way to tell which step holds the mistake. And
  Blender's default backup leaves a `.blend1` beside the `.blend` on every save, which undo and checkpoints make
  unnecessary in background mode.

**Checkpoints.** Saving a copy is quick and leaves the file's path unchanged. A checkpoint is what an agent wants
before a risky step that undo may not reach (past 32 steps, or across a file load); `open_file` on the copy
restores it.

Tests: `tests/test_undo.py` (an extrude undone in background gives the faces back; the first change after a new
file; several steps; a second undo; reads and refusals push nothing; a new or opened file starts over and never
undoes into the other file; too many steps refused and nothing undone; edit mode; stale Python names cleared, nested
ones too; a `fresh` script is one step; steps that `run_python` pushes itself stop undo by name, and `force` undoes
them unnamed; an edit outside the calls stops undo by name; a file `run_python` opens is where undo stops; a
read-only `run_python` is no step and leaves a saved file saved, and each kind of change, a custom property and a
frame among them, is one; undone back to the last save, a file is saved again, but not past it nor after a new
change; a background session then closes without `discard`; each step names the object it acted on),
`tests/test_files.py` (a background save leaves no `.blend1`), `tests/test_live.py` (in live mode, a move made by a
timer between calls is refused and undone only with `force`; a read leaves the saved file saved),
`tests/test_refusals_change_nothing.py` (the push rule), and `tests/test_checkpoints_and_append.py` (a checkpoint
leaves the session on its file, and the copy opens).
