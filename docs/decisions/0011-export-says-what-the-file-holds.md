# 0011 — Export says what the file holds; the server trusts only its own paths
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`visible_only: false` really exports hidden objects.** For the export, `export_glb` makes every target
object selectable and present in the viewport's evaluated scene, and puts each change back in a `finally`
block. It changes:

- the object's `hide_set` (the eye icon), `hide_viewport` and `hide_select`;
- along the path from the view layer's root to each of the object's collections: each layer collection's
  `exclude` and `hide_viewport`, and each collection's own `hide_viewport` and `hide_select`.

With `visible_only` true, a visible target that `select_set` still could not select (because it, or a
collection on its path, is unselectable) is revealed in the same way and selected again: whenever selecting an
object fails, the export reveals it. The selection, the active object, and the frame and subframe are restored
too, whether the exporter finished or failed. The glTF exporter itself goes to frame 0, and when it finishes it
goes back to the current frame but drops the subframe; when it fails, it does not go back at all.

**Selection is made with `select_set`, after `view_layer.update()`.** The view layer's object list lags behind
objects linked since its last update, so a loop over that list, or a membership test against it, misses a new
object. `select_only` (used by `create_object`, `create_mesh` and `duplicate_object`) and `export_glb` update
the view layer first, then select each object directly.

**The result is checked against the written file.** `glb.summarize(path, expected=None, ...)` returns
`warnings`, a list of `{code, severity, message, ...}`. When `expected` is given, any expected name that is
not a node in the file's scene tree is listed under `objects` in a warning with the code `missing_objects`.
The server passes the names of the exported objects as `expected`. The list is always present, and empty when
nothing is wrong. The read-back's checks for the target engine add to it (decision 0024).

**`extras` defaults to true.** Custom properties are exported as glTF extras unless `extras: false` is given.
With extras on, an export is refused when its scene, objects, meshes or materials hold a custom property
containing NaN or infinity, since JSON can hold neither; the refusal names the datablock and the property.
Properties whose names start with `_` (the bookkeeping of Blender and of add-ons, such as the old `_RNA_UI`)
are removed from the extras in the written file; the properties in the open file are left as they are.

**`render_preview` writes only `*.png`.** A path that does not end in `.png` is refused. So is a path where a
folder exists, or where a file exists whose first eight bytes are not the PNG signature. An existing PNG is
replaced. The server's half of this decision is in decision 0019: for the inline image, the `resource_link`
and the GLB read-back, the server uses the path it resolved itself, never a path from Blender's reply.

**The preview shows the objects, at the size asked for.** For the render, these are switched off, and restored
afterwards: the sequencer, the compositor, the render border and its crop, and stereo (multiview). Only the
view layer that holds the framed objects renders: `use_single_layer` is set, and the render call names that
layer. It is the current view layer when that layer holds them all, and otherwise the first layer that does.
Afterwards the user's output settings are put back in order: the file format first, then the colour mode and
the colour depth, because the depths on offer depend on the format.

## Why

Without these rules, exporting a scene that holds a hidden cutter with `visible_only: false` returns `objects`
listing `Cutter`, while the file holds only `Body`. The glTF exporter (Blender 5.0,
`io_scene_gltf2/blender/exp/tree.py`) walks the *evaluated* scene's objects and, with `use_selection`, keeps
those whose `select_get()` is true. On Blender 5.0.1, `select_set(True)` silently does nothing on an object
hidden with `hide_set`, `hide_viewport` or `hide_select`, or on one in a collection disabled in viewports, and
it raises for an object in an excluded collection. So the export must reveal such objects, and the result must
be checked against the file rather than against the request.

The view layer's lag shows on the same version. After `collection.objects.link(obj)`, `obj.name in
view_layer.objects` is false, and iterating `view_layer.objects` yields nothing until something syncs the
layer (`update()`, `select_set`, or the `active` getter). A `select_only` that deselects by looping over the
stale list, and then selects only the names found in it, never selects or activates the first object in an
empty file. An export that selects the same way writes an empty 132-byte GLB and still reports FINISHED.

Extras default to true because Godot 4.4 and later import node extras as metadata, which is how a game reads
tags put on parts with `set_object`.

A preview path is a file write that the agent chooses. A preview written over a file such as `victim.txt`
destroys a file that was never an image. Refusing anything that is not a PNG keeps a mistyped path from
costing the user data.

Each remaining rule prevents a failure that happens without it:
- A visible object in a collection set to unselectable is named in the result and missing from the file.
- An export that fails (its path is a folder) leaves the scene on frame 0.
- A custom property holding infinity crashes the exporter, since extras are on by default, and `_RNA_UI` goes
  into the file.
- A preview turns a 32-bit EXR output setting into 16-bit (10- and 12-bit likewise) when the depth is restored
  while the format is still PNG.
- A sequencer strip, a render border (an 8 x 8 PNG when 32 x 32 was asked for), stereo (two files beside the
  path and none at it) or a second view layer that excludes the object each give a picture that is not the
  object at its size.
