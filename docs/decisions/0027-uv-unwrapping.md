# 0027 — UV unwrapping
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`unwrap_uv` runs Blender's own UV operators and measures what they made.** It is in the CHANGE annotation set
(decision 0020), because unwrapping again makes a new layout.

- **Methods.** `smart` is the default: `uv.smart_project` with Blender's own angle limit of 66 degrees. `cube`,
  `cylinder` and `sphere` run `uv.cube_project`, `uv.cylinder_project` or `uv.sphere_project`, then
  `uv.pack_islands`. `unwrap` runs `uv.unwrap` (angle based). `lightmap` runs `uv.lightmap_pack` into the map
  named, never into a new one. `margin` (0 to 0.1 of the UV square, default 0.01) is the room left between
  islands. Blender's `uv.lightmap_pack` takes a margin of at most 0.01 of the square, so for `lightmap` a larger
  margin is refused.
- **Projections follow the object.** Cylinder and sphere projection use `direction='ALIGN_TO_OBJECT'`, so a
  cylinder lying on its side unwraps the same as one standing up.
- **Packing is AABB.** A projection leaves its islands overlapping or past the square, so they are packed, with
  `shape_method='AABB'`. Smart projection, unwrap and lightmap pack their own islands.
- **`uv_map`** names the map to write, which is created if missing. The default is the active map, or `UVMap` on a
  mesh with none. Every other map is left untouched, and the active map stays active: a lightmap written into
  `UV2` does not take over from the `UVMap` that the textures read.
- **A closed region without seams is refused for `unwrap`**, before anything runs. Such a region is a set of
  faces joined across edges that are not seams, with no border at all (a box, a sphere). The refusal's hint
  points to the `mark_seams` operation of `edit_mesh` (decision 0028), or to `smart`, which needs no seams.
- **The whole mesh is unwrapped.** The tool takes no face selection, because unwrapping part of a mesh is rarely
  what a game asset needs, and the selection parameters would cost 350 bytes of the tool list (decision 0020).
- **Edit mode during the call, object mode after it.** The operators work only in edit mode, on the selected
  faces of the active object. So the add-on makes the object active and visible (a hidden object cannot enter
  edit mode), selects and reveals every face, turns UV select sync on, and enters edit mode under a context
  override. Afterwards it leaves edit mode and puts back the active object, the object's selection and
  visibility, the mesh's selection and hidden flags, and the sync setting. This works the same in live mode (a
  Blender window running the add-on) and in background mode (a Blender the server starts without a window).
  If the object, or the active object, is already in edit mode, the call is refused with the hint to leave edit
  mode with Tab (decision 0012). If the object did not enter edit mode, the call is refused rather than reporting
  a layout it did not make. If an operator fails, the map's UVs are put back.
- **Hidden collections too.** The operators act only on objects the view layer shows. So the layer collections
  and collections that hide the object in the viewport are shown for the call and hidden again after. An object
  still not shown is refused before any operator runs.
- **The result describes what the map holds:**
  - the maps, and which one is active;
  - `islands`: groups of faces joined across edges whose UVs match on both sides;
  - `area_used`: the UV area covered, with overlaps counted twice;
  - `overlapping_faces`: faces whose UV triangles overlap another face's. Each triangle is shrunk by 1e-3
    toward its middle, so neighbours that only share an edge do not count. Pairs are tested with
    `intersect_tri_tri_2d` through a grid, the same grid `check_asset` uses (decision 0021). The work is bounded
    at 4 million triangle pairs met in the grid's cells and 4 million grid entries; past either bound, the
    result says the count is partial;
  - `out_of_bounds_faces`;
  - `texel_density`, in pixels per metre for a 1024 px texture, computed from the UV and world areas.

## Why

**Four traits of Blender's operators, each handled where it matters:**
- Cylinder and sphere projection default to `VIEW_ON_EQUATOR`, which needs a 3D view, and a background Blender
  has none. `ALIGN_TO_OBJECT` exists under the same name in 4.2 (`uvedit_unwrap_ops.cc` at v4.2.0) and 5.0. The
  test unwraps an upright and a lying cylinder and gets the same UVs.
- `pack_islands` takes seconds on a bevelled cube with its default `CONCAVE` shape method, and nearly as long
  with `CONVEX`, while `AABB` takes about a millisecond. The `shape_method` parameter exists in 4.2.
- `unwrap` on a closed mesh without seams finishes and reports FINISHED, but on a closed sphere every one of its
  480 corners gets its own UV, so every face becomes an island. Nothing says it failed, so the region is checked
  first. A box happens to come out as six squares that way, but a sphere does not, and the check cannot tell
  which case it faces.
- The selection flags of the UV operators differ between 4.2 and 5.0 (`BMLoopUV.select` is gone in 5.0). So the
  report reads only the mesh's UV coordinates, and the operators work on the mesh selection, with UV sync on.

**A hidden object never enters edit mode.** The UV operator, finding no object in edit mode, finishes having
done nothing, and the result would then describe the cube's original one-island cross as if the call had
unwrapped it. So the object is shown for the call, and edit mode is checked. The same holds for an object in a
collection hidden in the view layer: such a sphere enters edit mode, but the operator finishes having done
nothing, and the new map holds 4 distinct UVs that the result would call 128 islands. That is why the hiding
collections are shown for the call as well.

**The result measures, because the methods promise different things.** A cube projection of a cube stacks the faces
on each other by design (240 overlapping triangle pairs before packing); smart projection does not overlap. On a
mesh of 7,872 faces, the overlap test finds exactly the 34 faces that Blender's own Select Overlap selects on all
corners, without running that operator, in a fraction of a second. Texel density tells an agent whether a part
matches the rest of the asset.

**The bound counts all the work.** Every pair met in the grid's cells counts toward the bound, including a pair
already tested in another cell, because each one costs time. The grid is sized on the bulk of the layout, so a
UV far outside the square does not stretch every cell.

**Keeping the active map.** The operators write the active map, so the map named is made active for the call,
and the previous one is made active again after. The map marked for rendering, which an image texture without a
UV Map node reads, is never touched. A lightmap made into `UV2` that silently became the map in use would put the
colour texture on the lightmap layout.
