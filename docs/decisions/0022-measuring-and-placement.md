# 0022 — Measuring and placement
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`measure_objects` (READ, decision 0020) measures in world space, after modifiers.** It takes `objects` (2 to
16 names, each once) and measures every pair, or casts a ray given by `origin` and `direction`, or both.
- **Each tree is built from the evaluated mesh moved by `matrix_world`**, with `BVHTree.FromPolygons` on the
  world-space vertices. `BVHTree.FromObject` is not used, because its tree is in the object's own space.
- **For a pair**, the tool returns `distance`, the `closest` points `{a, b}` and `overlapping`. When the two
  cross, it adds `overlap_faces` (how many faces of each cross the other) and up to 32 `faces` of each.
- **Touching is not crossing.** `BVHTree.overlap` reports every pair of triangles that meet at all, so a box
  resting on another meets it along edges. The add-on tests each reported pair again (`measure.triangles_cross`).
  Two triangles cross only when each reaches more than 10 microns (`TOUCH_DISTANCE`) beyond the other's plane on
  both sides, and their cuts through the line the two planes share overlap by more than that. Faces lying on each
  other, an edge or a corner resting on a face, and a surface sunk no deeper than 10 microns (as a drop leaves it,
  within float precision) only touch. Past 100,000 face pairs, the pairs not yet tested count as crossing.
  `overlapping`, the `overlaps` issue of `check_asset` and the `apart` rule all mean crossing alone.
- A pair that meets without crossing, or lies within 10 microns, is reported `touching`. Then `coplanar_faces`
  (`{a, b}`) counts the faces lying on each other: faces whose middle lies on the other object's surface, facing
  along the same line.
- When one object lies wholly inside another, closed object, `inside` names the inner one: it lies within the
  other's bounding box, and most of three rays from one of its points cross the other an odd number of times.
  `distance` stays the gap between the surfaces.
- **The distance** starts from each vertex's nearest point on the other surface. The add-on then refines the
  eight best starts by stepping to the nearest point on each surface in turn, until the distance stops shrinking.
  That finds an edge passing beside a face, which measuring from vertices alone misses.
- **For a ray**, the tool returns `hit`, `object`, `location`, `normal`, `distance`, `face` and `faces_of`. With
  `objects`, the ray hits only those (through their own trees); without, anything visible (`Scene.ray_cast`).
- Face indices are those of the evaluated mesh's polygons. `faces_of` says whether those faces are the mesh's own
  (decision 0021).

**`set_transform` takes `drop`.** With `true`, the object lands on anything visible below it. With an object's
name, it lands on that object alone. The drop runs last, after location, rotation, scale and dimensions are set.
- A ray starts just above the object at each of its points and goes straight down, passing through the object
  itself. The points are the evaluated vertices of the object and its children: at most 50,000, sampled evenly
  beyond that.
- A second set of rays goes up, from each point under the object of what it may land on, to the object's lowest
  surface there. What it may land on is the named object, or every visible object whose bounding box reaches under
  it, with at most 50,000 points.
- The object then moves along world Z by the smallest gap of all, so it rests on its first contact. A gap below
  zero moves it up: an object sunk into the floor rises onto it.
- The tool returns `landed_on` and `moved_by` (`[0, 0, dz]`).
- If nothing lies below, the call is refused with `not_found`, and the values the call set before the drop are
  put back (decision 0010). Landing on the object itself or on one of its children is refused with
  `bad_argument` before anything changes.

**`apply_transform` takes `origin`.** The origin can be `bottom_center`, `bounds_center`, `geometry` (the mean of
the vertices), or a world point `[x, y, z]`.
- The add-on uses no operators. The mesh (with its shape keys) moves by the offset one way and the object the
  other, and each child's `matrix_parent_inverse` takes the offset back, so the mesh and the children stay where
  they are in the world. A child parented to vertices (`VERTEX`, `VERTEX_3`) follows them, and they do not move,
  so it is left alone.
- The places are measured on the evaluated mesh in world space, so "bottom" means world down and includes what
  the modifiers add.
- With `origin`, `rotation` and `scale` default to false: moving the origin applies nothing else unless asked.
  When asked, they apply first and then the origin moves. Everything the origin needs is checked before they
  apply, because an applied transform cannot be put back.
- Refused before anything changes: a mesh shared by other objects (all of them would move), an object that is
  not a mesh, a mesh with no vertices (or none after its modifiers), edit mode, and a modifier that works about
  the origin, which the refusal names: Mirror without a mirror object, Screw, Simple Deform and Cast without their
  object, Wave without its start object, an Array offset by an object, and a Displace placing its texture by the
  object's own coordinates.

## Why

**Without these tools, an agent writes the `bpy` by hand:** rays cast down to rest one part on another,
world-space BVH trees to check stacked pieces for crossing, and bounds computed in every build script to put
origins at the base.

**What Blender's API does** (measured in Blender 5.0.1):
- `BVHTree.FromObject`'s nearest point to the world origin comes back in the object's local coordinates, so the
  add-on builds world trees from world vertices.
- The clearance between two cubes 1 m apart measures 1.0. Overlapping cubes give 10 overlapping pairs. A sphere
  of 32,768 faces takes a fraction of a second.
- Dropping a cube that is 1 m above another, by `Scene.ray_cast` from its lowest point, moves it by exactly 1.0.
- Setting the origin through the mesh transform, the object matrix and the parent inverse leaves the mesh where
  it was (no drift) and a child within 6e-8 m. `object.origin_set` also works in background mode, but it needs a
  selection and an active object, and it cannot put the origin at the bottom.

**Rays from above, through the object itself.** A single ray from the lowest point misses when that point hangs
over an edge. A ray going down from each vertex cannot see a surface the object already sinks into. Starting
every ray just above the object and passing through its own faces finds the top of whatever lies under each
point, so the smallest gap is the true one in both cases. The test drops a cube sunk 0.3 m into a plane, and it
rises 0.3 m.

**Rays from below.** A pillar narrower than the object, or a spike's tip, lies between the object's own points,
and rays from those points all miss it. Without the upward rays, a 2 m tabletop dropped over a 0.2 m pillar falls
through to the floor, a crate falls past a cone's tip, and `drop: "Pillar"` is refused with `not_found`.

**Refined distances.** Measured from vertices alone, a cube turned 45 degrees beside another measures 1.195 m
where the true gap is 1.086 m. Its nearest feature is a vertical edge whose ends lie beyond the other cube's face.
Alternating nearest points converge on that edge, and a test pins this case.

**`inside`, and each object once.** Without the parity test, a cube wholly inside another measures 0.8 m apart
with `overlapping: false` and nothing else. An object measured against itself is always 0 m away, so each name may
appear only once.

**The origin alone.** `apply_transform` applies rotation and scale by default, but not with `origin`. Otherwise
an agent asking only to put the origin at the base of an object it scaled on purpose would lose that scale.

**Checking before anything applies.** Checked only after the transform applied, a refused origin (a Mask
modifier leaving no vertices) would leave the requested scale applied. A modifier that works about the origin
moves what it makes when the origin moves: a half cube's mirrored half moves by 2 m. Adjusting the parent inverse
of a child parented to a vertex would move that child, although the vertices it follows stay where they are.

**Why touching is not crossing.** A box dropped onto another rests face on face with it. If meeting counted as
crossing, `measure_objects` would report a correct drop as `overlapping`, and an agent would undo it as a mistake.
The tests build a box with a thin bump, 1 mm high, on its top and drop a second box, turned 5 degrees, onto it.
The pair is touching and not overlapping, the `apart` rule passes, and there is no `overlaps` issue. Pushed 1 cm
into each other, the boxes cross. A cube set 4 microns above another, and then 4 microns into it, touches both
times.
