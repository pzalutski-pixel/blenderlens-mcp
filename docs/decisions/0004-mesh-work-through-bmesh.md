# 0004 — Mesh work through bmesh, not `bpy.ops.mesh`
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

Meshes are built and edited through `bmesh`, Blender's module for working on mesh data directly, and through
the data API (the datablocks in `bpy.data` and their own properties and methods). That covers the primitives,
the tube, lathe and text shapes, `edit_mesh`'s operations, vertex colours, and the checks of `check_asset`.

Where to use `bpy.ops`, Blender's operators (the functions behind its menus and buttons):
- **Only where Blender offers no data-level path:**
  - applying a modifier or a transform, moving a modifier in the stack, and joining objects;
  - UV projection, with `mode_set` to enter edit mode for it and to leave again;
  - baking and rendering;
  - the glTF exporter, and the other exporters and importers;
  - opening, saving and starting files;
  - pushing and undoing undo steps.
- **Always on named objects.** An operator that acts on objects runs under `bpy.context.temp_override(...)`,
  naming the objects it acts on, and never relies on whatever the user has selected or made active. The glTF
  exporter works on the selection, so `export_glb` selects exactly its targets and puts the user's selection
  back afterwards (decision 0011).
- **Edit mode only for the call.** An operator that needs edit mode (UV projection) enters it for the call,
  and afterwards restores the user's mode, selection and active object.

Faces are picked by index or by the way they face in world space, never by the user's selection.

## Why

`bpy.ops.mesh` works on the edit-mode selection of the active object in the active 3D viewport. A background
Blender has no viewport, and in live mode, where the add-on runs in the user's own Blender window, that state
belongs to the user. Driving these operators would make every call depend on, and disturb, what the user last
clicked.

bmesh has neither problem. It behaves the same with and without a window, and it lets a command check
everything before it changes the mesh (decision 0010). Every bmesh path runs in a background Blender, while
`render.opengl` and the UV projections that depend on a view do not.
