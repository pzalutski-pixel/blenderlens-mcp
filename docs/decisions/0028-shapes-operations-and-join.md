# 0028 — Shapes beyond the primitives, the mesh operations, and join
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`create_object` has three kinds beyond Blender's primitives, and each ends as a mesh**, so everything
downstream (`edit_mesh`, modifiers, export) sees a mesh. The add-on builds them in `addon/shapes.py` as a bmesh
and places them as it places the primitives, with the same result.
- **`tube`** runs along `points` (`[x, y, z]` in mesh space, 2 to 1,000 of them), with `radius` (default 0.05 m),
  `bevel_resolution` (0 to 32, giving 4 + 2n sides; the default 2 gives eight), `fill` (caps, default true) and
  `smooth` (default true). With `smooth`, the path is a Bezier curve with automatic handles, which passes through
  every point; without it, the path is straight runs with sharp bends. The add-on builds the tube as a curve with a
  round bevel, turns it into a mesh with `bpy.data.meshes.new_from_object` on a curve object that is never linked
  into the scene, and then welds the mesh so that its caps close it. It is shaded smooth, but edges sharper than
  61 degrees stay sharp: the cap rims, the bends of straight runs, and the edges of a four-sided tube. A path that
  turns straight back along itself is refused, because Blender folds the tube's section flat there and leaves it
  open (a turn of 179.9 degrees still closes).
- **`lathe`** spins a `profile` of `[radius, z]` points (2 to 1,000 points, radius 0 or more) about the Z axis in
  `segments` steps (3 to 1,024, default 32), using `bmesh.ops.spin(use_merge=True)`. The copies of points that
  lie on the axis are merged, so the surface closes there, and an end off the axis is capped unless `fill` is
  false. Its UV map unrolls the side into the lower half of the UV square and lays the caps as discs in the
  upper half.
- **`text`** takes `text` (1 to 1,000 characters), `size` (default 1) and `extrude`, the depth (default a tenth
  of the size). The text is centred on the origin, lies in the XY plane and reads along +X. It is a FONT curve,
  meshed the same way as the tube and welded closed. Text for which Blender's font has no shapes is refused.
- `fill` is the same "Caps" parameter that cylinders and cones take; there is no separate `caps`.

**`edit_mesh` has four more operations, and `bevel` can select edges by angle.** All of them work on the bmesh,
so they work in edit mode too (decision 0012), and all are checked before the mesh is touched (decision 0010).
- **`bisect`** cuts along the plane through `pivot` (in mesh space; default the selection's centre) whose normal
  is `direction` (in world space, decision 0013). The normal is turned into the mesh's space by the transpose of
  the object's transform, as a normal must be. `keep` is `below` (the default: what lies on the side the normal
  points to is cut away), `above`, or `both` (only cut). `fill` (default true) caps the cut. A plane that misses
  the selected faces is refused.
- **`solidify`** gives the faces a thickness of `amount`, grown inward from the normals (`bmesh.ops.solidify`).
  An `amount` of 0 is refused.
- **`dissolve_planar`** merges faces that meet at less than `angle` (default 5 degrees, as Blender's Limited
  Dissolve). It works inside the selection only, and never across a material border or a seam.
- **`mark_seams`** makes each edge of the selection a seam when its faces meet at more than `angle` (default 30
  degrees, which picks a box's twelve edges and a cylinder's two rims), and clears the seam otherwise. The
  result counts the mesh's seams. This is what `unwrap_uv` with method `unwrap` needs (decision 0027).
- **`bevel` with `angle`** bevels every edge of the selection whose faces meet at more than `angle`. At
  50 degrees, for example, it takes a box's edges and a cylinder's rims (90 degrees) but not the steps between a
  32-sided cylinder's sides (11.25 degrees). Without `angle`, it bevels the rim edges of the selection, or every
  edge when the whole mesh is selected. If no edge is sharp enough, the call is refused with `not_found`. The
  work bound (decision 0013) counts the edges actually taken.

**`join_objects`** (in the CHANGE set) joins meshes into the first one named, as Ctrl+J does in Blender. It runs
`bpy.ops.object.join` under a context override.
- The first object keeps its name, origin, transform and modifiers. Each piece's faces keep their materials
  (the result lists every slot with its faces) and stay where they were in the world.
- These are refused, changing nothing: fewer than two objects; a name given twice; anything that is not a mesh;
  edit mode (decision 0012); a first object whose mesh another object shares (joining would change that object
  too); a piece with modifiers (joining would drop them, and the hint suggests `apply_modifier`); and a piece
  that is the first object's parent.
- The pieces' children are parented to the first object where they stand. The pieces' meshes, left with no
  users, are removed. When the first object has modifiers, the result notes that they act on the pieces too.
- A piece whose transform is mirrored relative to the first object's has its faces turned over first
  (`Mesh.flip_normals`, in 4.2 and 5.0), so they still point outward once the join moves them into the first
  object's space through the mirror. A piece whose UV maps are named differently from the first object's has
  them renamed to the first object's names, map for map in order, because the join merges maps by name; the
  result reports this under `uv_renamed`. A piece whose mesh other objects share gets its own copy for either
  change, so the others keep theirs.

**A composite part can be built with tools alone.** `test_a_prop_built_from_pieces_with_tools`, in
`tests/test_modelling.py`, builds a prop from four pieces made with `create_object`, including a tube and a lathe
shape. It bevels the boxes by angle, gives each piece its own material, joins the pieces into one object with
four material slots, and checks the exported file.

## Why

**A composite part without a script.** A build script typically makes a composite part by adding pieces into one
bmesh (boxes, cylinders, prisms, swept tubes), giving each piece a material, and bevelling the edges sharper than
some angle. Pieces made with `create_object`, bevelled by `edit_mesh`'s `bevel` with an `angle`, given a material
each with `set_material` and joined with `join_objects` make the same part. The tube's defaults suit hoses, cables
and pipes at the sizes of a game asset. Six samples a span draw each span between two points as six straight
segments, so a right-angle bend over one span turns 15 degrees a segment. Eight sides keep the faces of a tube of
the default 5 cm radius under 4 cm wide. A bend then looks round, and a tube through ten points stays under a
thousand faces.

**Curves are meshed without the scene.** `bpy.data.meshes.new_from_object` on an original curve object makes a
temporary evaluated copy itself (`mesh_new_from_curve_type_object` in `mesh_convert.cc`, the same at v4.2.0 and in
5.0). So the curve need not be linked into the scene, where the view layer would lag behind it, and no operator
runs. The curve, its object and the mesh are removed whatever happens.

**Welding.** A curve's caps come out as separate vertices: a tube of 40 vertices is open, and welded it has 24
and is closed. A text's front, back and sides do the same: 1,712 vertices with 1,712 open edges become 856 with
none. The tests check that each kind is closed with outward normals (a positive signed volume).

**What `object.join` does without saying so.** It drops a piece's modifiers (a bevel modifier, say), it
unparents a piece's child, and it leaves the pieces' meshes in the file with no users. A lid scaled -1 in Z and
joined to a body comes out inside out, with every face pointing in. A lid whose UV map is named `UVChannel` joins
as a second map, and its faces in the body's `UVMap` all sit at (0, 0). Each case is refused or put right first,
so a join never loses anything quietly.

**A tube that turns back.** A straight tube through `[0,0,0]`, `[1,0,0]`, `[0,0,0]` comes out with 8 open edges,
so such a path is refused.

**Bisect's normal** turns with the transpose of the object's transform, not with its inverse, which is what
`direction` uses for extrusion. For a rotation the two agree; for an uneven scale only the transpose keeps the
plane where it was meant to be.
