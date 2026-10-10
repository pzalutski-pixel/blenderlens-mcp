# 0024 — Read-back warnings and clean names
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**The read-back warns for the export's target.** After `export_glb` writes a file, the server reads it back and
returns what it holds under `contents` (decision 0011), from the file alone (`glb.summarize(path, expected,
target, morphs)`). For `godot` or `threejs`, the warnings speak for that engine. For `generic`, they speak for
both engines: a warning about one engine names it in `engine`, and the Godot version notes are left out. Every warning
has a `code`, a `severity` (`error`, `warning` or `info`) and a `message`. A list in a warning holds at most 32
items, with a `count`.

| Code | Severity | Engine | When |
|---|---|---|---|
| `missing_objects` | warning | both | an object that was asked for is not a node in the file (decision 0011) |
| `extension_not_supported` | error | each | `extensionsRequired` holds an extension the engine cannot load |
| `extension_ignored` | warning | each | `extensionsUsed` holds an extension the engine ignores |
| `extension_needs_decoder` | info | three.js | Draco, KTX2 or meshopt: the decoder the three.js app must set up |
| `godot_version` | warning if the extension is required, else info | Godot (target `godot`) | extras (read from 4.4), `KHR_node_visibility` (from 4.5), `KHR_animation_pointer` and `GODOT_single_root` (from 4.4) |
| `godot_name_hint` | warning | Godot | a node, mesh or material name that Godot reads as an import hint |
| `scaled_hint_node` | warning | Godot | a collision hint on a node with non-uniform or negative scale |
| `animation_loops_by_name` | info | Godot | an animation that Godot imports as looping because of its name |
| `renamed_on_import` | info | both, in one warning with a list per engine | the node names Godot and three.js will use where they differ from the file's, and the names that collide |
| `double_sided` | info | both | materials with `doubleSided` |
| `vertex_colors` | info | both | `COLOR_0` is present; for Godot, also the bug in 4.7.0 to 4.7.2 |
| `normal_map_without_tangents` | warning | both | a material with a normal map on a primitive without `TANGENT` |
| `texture_without_uv` | warning | both | a primitive whose material samples a `TEXCOORD_n` the primitive lacks |
| `root_off_origin` | warning | both | the scene's only root has a translation (or a matrix translation) more than 0.1 mm from the origin, so the asset appears that far from where a game places it. A scene with several roots is a layout of several objects, and is not warned about. |
| `morphs_dropped` | warning | both | an object had shape keys and its node's mesh in the file has no morph targets; the node is found by its index (decision 0023), whatever name cleaning gave it |

**Name hints are tested as Godot tests them.** A name is stripped of trailing digits, spaces and underscores,
and then a hint matches when it follows `-` or `_` at the end, or `$` anywhere, in any case. Node hints are
tested in Godot's order, and the first one that acts is reported: `noimp`; then, on a node that holds a mesh,
`colonly`, `convcolonly`, `rigid`, `col`, `convcol`, `navmesh`, `occ` and `occonly`; then `vehicle` and `wheel`.
A mesh's own name is tested for `col`, `convcol` and `occ` when its node has no hint. A material's name is
tested for `alpha` and `vcol`, and an animation's for `loop_mode`, `loop` and `cycle` (an animation whose name
begins or ends with `loop` or `cycle`, in any case, also loops, as decision 0034 explains). Each hit says what
Godot makes of it ("gets a StaticBody3D child with collision shapes"). Names are tested as Godot names the node,
so Blender's `Rock_col.001` (Godot's `Rock_col_001`) is caught too.

**The names each engine will use.**
- Godot replaces `.` `:` `@` `/` `"` `%` with `_`. It then makes each node's name unique across the whole file,
  in node order, by appending 2, 3 and so on.
- three.js removes `[` `]` `.` `:` `/` and turns whitespace into `_`. It makes each name unique by appending `_1`,
  `_2`, reserving the scene's name first and then the nodes' names, depth first.

**Estimates**, under `contents.estimates`, are `draw_calls` (one per primitive of every mesh node in the scene);
`textures`, with each size read from the image's own header in the binary chunk (PNG, JPEG, WebP, KTX2), or `null`
for an image outside the file or unreadable; `texture_pixels`; and `vram_bytes`, as `uncompressed` (RGBA8 with
mipmaps, as three.js uploads it) and `compressed` (about a byte a pixel with mipmaps, as Godot's default VRAM
compression keeps it). An image that names a missing `bufferView` is a fault in the file (`GLBError`, decision
0013).

**Triangles drawn.** `triangles` in the contents counts each mesh once for every node of the scene that draws it,
just as `draw_calls` counts its primitives. Each mesh row keeps its own count. The `check` command (decision 0025)
reads the same numbers. Material values (`metallic`, `roughness`, `emissive_strength`) are rounded to four places,
as the colours are: the file keeps 32-bit floats, and 0.6000000238 says nothing more than 0.6.

**A material that glows is `emissive`**, and its colour is `emissive_factor`. Each material row also gives
`double_sided`. The contents give `extensions_required` and `vertex_colors`.

**`clean_names`** (on by default for `target: godot`) rewrites the written file's JSON chunk so that names lose
Blender's `.NNN` suffix where that leaves no two the same: a node's among its siblings (the scene's roots are
siblings), a mesh's among the meshes, a material's among the materials. A name that would collide keeps its
suffix. Each cleaned name is checked against every name the siblings end with, so `B.001.001`, which cleans to
`B.001`, keeps its suffix beside a `B.001` that keeps its own. Only the file changes; the `.blend` keeps its
names.
- Cleaning shares one rewrite with the removal of hidden extras (decision 0011). `_rewrite_json(path, *edits)`
  reads the chunk once, runs each edit, and writes the chunk back padded to four bytes.
- The read-back runs on the rewritten file. The `objects` in the result are the names in the file, so a cleaned
  object is not reported missing. `renamed` maps the old node names to the new ones, and `renamed_data` does the
  same for meshes and materials.

## Why

**Every fact a warning states comes from the engine's source**, at these versions:
- **Extensions.** Godot: `GLTFDocument::get_supported_gltf_extensions_hashset` and the registered document
  extensions (WebP, KTX2, the three OMI physics extensions), 4.7.2-stable. `_parse_gltf_extensions` fails the import
  on any other required extension. three.js: the `EXTENSIONS` table and plugins of `GLTFLoader.js`. An unknown
  required extension only logs "Unknown extension", and the file loads without it. The Draco, KTX2 and meshopt
  extensions throw without their loader.
- **Versions.** In the supported lists of `gltf_document.cpp` from 4.3 to 4.7, `KHR_node_visibility` first
  appears in 4.5, and `KHR_animation_pointer` and `GODOT_single_root` in 4.4. For extras, 4.3 reads a mesh's
  extras only for its target names, and 4.4 adds `_attach_extras_to_meta` for nodes, meshes and materials
  (metadata `"extras"`).
- **The vertex-colour bug.** In 4.7-stable, 4.7.1-stable and 4.7.2-stable, `_parse_meshes` reads each primitive's
  material, and sets `FLAG_ALBEDO_FROM_VERTEX_COLOR` when `has_vertex_color` is true, before the attribute loop
  sets `has_vertex_color` from `COLOR_0`. So a mesh's first primitive never shows its vertex colours (each primitive
  after it inherits the flag from the one before). 4.6.3 read the attributes first, and Godot PR #122315 ("Fix: Vertex
  color not applying regardless of GLTF exporter flags") restores that order. The warning covers the releases
  with the bug, 4.7.0 to 4.7.2. The `vcol` material hint sets the flag by name, so the warning names it as the
  workaround.
- **Name hints.** `_teststr`, `_fixstr` and `_post_fix_node` in `resource_importer_scene.cpp`. The hints are the
  same in 4.3 to 4.7, and from 4.4 the `nodes/use_node_type_suffixes` option turns node hints off.
- **Names.** Godot: `String::validate_node_name` (`core/string/ustring.cpp`, 4.7.2), `_gen_unique_name_static`
  and `_assign_node_names` (node order, across the whole file). The importer's `gltf/naming_version` defaults to 2
  ("Godot 4.5 or later", `editor_scene_importer_gltf.cpp`), under which the scene's name is not reserved.
  three.js: `GLTFParser.createUniqueName`, `PropertyBinding.sanitizeNodeName`, and `loadScene`, which
  names the scene before loading its nodes.
- **Double-sided.** Godot sets `CULL_DISABLED` for `doubleSided`, and three.js sets `side = DoubleSide`. Blender
  writes `doubleSided` whenever Backface Culling is off, which is Blender's default for a material, although
  `set_material` turns culling on for the materials it creates. Drawing both sides costs more but is not a
  fault, so this is a note.
- **Vertex colours.** Godot sets `FLAG_ALBEDO_FROM_VERTEX_COLOR` for a primitive with `COLOR_0`, and three.js sets
  `vertexColors` on a geometry with a `color` attribute. Both multiply the base colour by them.
- **Tangents.** Godot generates missing tangents on import (`meshes/ensure_tangents`, on by default). three.js
  falls back to derivative tangents and flips `normalScale.y` (GLTFLoader, `useDerivativeTangents`). Either can
  differ from the MikkTSpace tangents that Blender baked against.
- **Morphs.** A cube with a shape key and a modifier, exported with `export_apply`, has 0 morph targets in the
  file. The add-on reports which objects had shape keys, and the read-back finds their meshes empty of `targets`.

**Emissive, not "emits light".** Emission in Godot and three.js makes a surface glow, but it lights nothing around
it without global illumination (SDFGI, VoxelGI or baked lightmaps in Godot). A name such as "emits light" would
promise more.

**Clean names, sibling by sibling.** Godot paths address nodes as `Parent/Child`, and a script written against
the `.blend` says `Piece`, not `Piece.001`. Godot's own uniqueness is file-wide, so two cleaned `Piece` nodes under
different parents still import as `Piece` and `Piece2`, and `renamed_on_import` says so rather than hiding it.
Cleaning siblings only where the result is unique keeps the file's own names unambiguous within each parent, which
is what a path needs. A cleaned name is checked against the names the siblings end with, not the ones they start
with; otherwise, under one parent, `B`, `B.001` and `B.001.001` would come out as `B`, `B.001` and `B.001`.

**The node index.** Cleaning can give a node the name of another node. If `morphs_dropped` found nodes by name, it
could look at a node without shape keys and report an object whose shape keys the file kept.

**Generic leaves out the version notes.** Otherwise a file meant for any engine would carry a Godot note on every
export with extras, which is nearly every export, since extras are on by default (decision 0011).

**Triangles per node.** Linked copies share one mesh. A stack of three linked copies of a 116-triangle mesh draws
348 triangles, which is what `check_asset` counts and how draw calls are already counted. Counting each mesh once
would report 116.

**A root off the origin.** A game places an asset by its root. An object exported from its place in a larger
scene, for example at [-2, -1, 0.5], lands that far off wherever it is placed, and without
`root_off_origin` nothing would say so.
