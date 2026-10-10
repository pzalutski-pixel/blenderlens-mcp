# 0035 — Collision proxies
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`create_collision` adds a collision proxy over a mesh.** A collision proxy is a simple mesh that stands in for an
object in a game engine's physics. The tool is in the WRITE annotation set (decision 0020), because a second call
adds a second proxy.

- **Shapes.** The add-on builds the proxy from the mesh after modifiers, as the exporter sees it, in the object's
  own space:
  - `box`: the bounding box of those vertices;
  - `convex` (the default): their convex hull, made with `bmesh.ops.convex_hull`, with the points inside the hull
    removed. A flat or straight mesh has no hull, and is refused with a hint to use `box`;
  - `decimated`: the result of a Decimate (collapse) modifier keeping `ratio` of the faces (0.01 to 1, default
    0.25). `ratio` is refused for the other shapes. The modifier runs on a temporary copy, which is then removed.
- **Naming.** With `naming: "godot"` (the default), the proxy's name is the object's name with `-colonly` appended
  for a box or a decimated mesh, and `-convcolonly` for a hull. From these suffixes Godot's importer makes a static
  body with a trimesh or a convex shape from the mesh, and does not draw the mesh. `plain` appends `_collision`,
  which no engine reads, for a game that finds its colliders by name. If the name clashes, Blender's `.001` suffix is
  added, and Godot's own name test strips it.
- **Placement.** The proxy is parented to the object with no transform of its own and an identity parent inverse,
  so it sits exactly over the object and follows it. It is placed in the object's collections.
- **Visibility.** The proxy is drawn as wire, so the object shows through it, and it is left out of renders
  (`hide_render`), so `render_preview` shows the object alone. It is never hidden, because `export_glb` with
  `visible_only` (the default) leaves hidden objects out, and a proxy must reach the file.
- **Export.** `export_glb` with `target: "threejs"` leaves out every node that Godot reads as collision only
  (decision 0023). The `godot` and `generic` targets keep these nodes, with their names, as children of the object.
- **The result** gives the proxy's name, its parent and collections, its counts, `closed` (whether every edge lies
  between two faces), its volume in world cubic metres, `volume_ratio` against the object's own volume (when the
  object is closed), and its dimensions. A note says when the object is not closed. With `naming: "godot"`, a note
  also says when the object is scaled unevenly or mirrored, which Godot's collision shapes do not accept.
- **Every proxy is marked.** Each one carries the custom property `blenderlens_collision`, holding its shape.
  `export_glb` writes custom properties as the node's extras, so in the file the mark tells a collision name given
  on purpose from a name that reads as a Godot hint by accident. The read-back's `godot_name_hint` warning still
  lists marked nodes, and a reader of the file tells them apart by the mark.
- **A proxy of a proxy is refused.** That covers an object carrying the mark, and one whose name Godot reads as
  collision only (`-colonly` or `-convcolonly`, by Godot's own test). The hint names the proxy's parent, the object
  it stands for.
- The add-on reads the mesh as it is shown, in edit mode too (`sync_edit_mode`). A source with more than 2,000,000
  vertices after its modifiers is refused. The count is taken on the evaluated mesh before it is copied, because an
  array modifier on a small mesh may make millions.

## Why

**Godot reads collision from names.** Its scene importer (`ResourceImporterScene::_post_fix_node`, 4.3 to 4.7) turns
a mesh node whose name ends in `-colonly` into a `StaticBody3D` with collision shapes made from the mesh, and one
ending in `-convcolonly` into one with a convex shape, and in both cases drops the mesh. The server's read-back
warns of these hints (decision 0024), and the three.js target drops such nodes, so a proxy named this way needs
nothing else from the exporter.

**Hiding the proxy is not an option.** `export_glb`'s default `visible_only` would leave a proxy hidden in the
viewport out of the file without a word, which defeats the proxy's purpose. Wire display keeps the object visible
behind it.

**Parented with an identity transform.** The proxy's vertices are in the object's own coordinates, so the identity
transform under the object puts the proxy in the same place, in Blender and in the file, and it moves with the
object. An unevenly scaled object passes that scale on. Godot's shapes take only uniform scale, so the result says
so, and `apply_transform` bakes the scale into the mesh.

**Checks a test can make.** The convex test builds a hull of a tilted 16-sided cylinder and finds every cylinder
vertex on or inside every face plane of the hull, the volume ratio 1.0, and the proxy at the cylinder's world
matrix. `convex_hull` (`input`, `use_existing_faces`; outputs `geom`, `geom_interior`, `geom_unused`, `geom_holes`)
and `create_cube` (`size`, `matrix`, `calc_uvs`) have the same slots in 4.2 (`bmesh_opdefines.cc` at v4.2.0), and
`BMesh.calc_volume(signed)` is in `bmesh_py_types.cc` there.

**The evaluated bound, the refusal and the mark.** If the vertex bound counted the mesh before its modifiers, a cube
under an array modifier would pass whatever the array made. Without the refusal, `create_collision` on a proxy makes
`Crate-colonly-colonly`. Without the mark, the export's warnings about the proxies' own names would read as hints
given by accident, and nothing in the file would tell them apart. The tests lower the bound for one call and refuse
a cube of 8 vertices that an array makes 160; refuse a proxy of a proxy in both namings, and an object named with
`-colonly`; and read the mark from the node extras in the file.
