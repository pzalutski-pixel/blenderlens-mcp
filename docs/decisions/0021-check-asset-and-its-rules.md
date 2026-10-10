# 0021 — check_asset and its rules format
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`check_asset` (a READ tool) checks an asset as it will export, and passes or fails it.** It reads the objects
that `export_glb` writes for the same `objects` argument (the scene, or the named objects with their children):
the visible ones, without cameras and lights, after modifiers unless `evaluated` is false. It changes nothing.
- Hidden objects are left out and named under `hidden` (at most 32, with `hidden_not_listed`). An object named
  in `objects` is checked even when hidden, with a `hidden` issue (info) saying the export leaves it out.
- Curves, surfaces and text are checked as the mesh they make (their `faces_of` is `evaluated`), and count toward
  the budget. A metaball is not: Blender's glTF exporter writes it as an empty node, so `check_asset` reports it
  as `surface_not_exported` (an error) and does not check it as a mesh or count its triangles.
- What a collection instance shows is checked where the depsgraph (Blender's evaluated scene) places it, each
  object once per instancing object, its row carrying `instanced_by`. Its triangles count once per instance, up
  to 10,000 instances; past that the check reports `not_checked`.

**The generic checks and their severities.** An error fails the check. A warning is reported, and the check
still passes.

| Code | Severity | Found by |
|---|---|---|
| `non_manifold` | warning | edges shared by three or more faces |
| `boundary_edges` | warning | edges with only one face |
| `loose_geometry` | warning | vertices with no edge, and edges with no face |
| `zero_area_faces` | warning | faces with an area under 1e-10 m² |
| `duplicate_vertices` | warning | `bmesh.ops.find_doubles` at 1e-5 m |
| `ngons` | warning | faces with more than four corners |
| `flipped_normals` | error | faces pointing against their neighbours (see below) |
| `inside_out` | error | closed parts, not inside another closed part, whose every face points inward |
| `self_intersecting` | warning | `BVHTree.FromBMesh(...).overlap` of the mesh with itself, skipping face pairs that share a vertex |
| `unapplied_scale` | warning | the object's scale is not 1 |
| `negative_scale` | warning | `matrix_world` mirrors the object |
| `origin_not_at_base` | warning | a root mesh's origin is not on the bottom of its bounds (only when there is no `origin_at_base` rule) |
| `no_uv` | warning | the mesh has no UV map (not asked of a collision proxy) |
| `uv_overlap` | warning (info where no material maps an image) | the UV triangles of two faces overlap, found by `intersect_tri_tri_2d` over a grid (see below) |
| `uv_out_of_bounds` | warning (info where no material maps an image) | a UV coordinate lies outside 0 to 1 |
| `texture_without_uv` | error | an image texture feeds a material on a mesh that has no UV map |
| `missing_image` | error | an image node has no image, or its file is neither on disk nor packed |
| `absolute_texture_path` | warning | an image is read from an absolute path |
| `procedural_material` | warning | a procedural texture node feeds the material, so it exports as a flat colour (a Texture Coordinate node does not count as one) |
| `surface_not_exported` | error | a metaball, which Blender's glTF exporter writes as an empty node, with no mesh |
| `morphs_dropped_by_modifiers` | warning | the mesh has shape keys and a modifier shown in the viewport other than an Armature (the exporter writes an Armature as a skin) |
| `over_budget` | error | the asset has more triangles than `budget` |
| `overlaps` | warning | two objects' surfaces cross, or one lies wholly inside the other (under `inside`, inner first): bounds first, then `BVHTree.overlap`, then each face pair told touching from crossing as in decision 0022. Parts resting on each other only touch. Collision proxies are left out. |
| `hidden` | info | an object named in `objects` is hidden, so `export_glb` leaves it out |
| `not_checked` | info | a check was skipped because of its size: self-intersection over 200,000 faces, UV overlap over 100,000 triangles or past its work bound, overlaps over 400 meshes, collection instances past 10,000 |

- **Orientation.** The check runs `recalc_face_normals` on a copy of the mesh. A face the recalculation turns
  was pointing against its neighbours. On a closed part the recalculation points every face outward, so a closed
  part turned entirely is `inside_out`, and otherwise its turned faces are `flipped_normals`. On an open part
  "outward" means nothing, so only the minority of faces that disagree with the rest of their part are flipped.
  A plane facing down is just a plane.
- **Parts inside parts.** A closed part inside another closed part of the same mesh (within its box, and inside
  by the parity of three rays cast from one of its faces) may face either way as a whole. A cavity's wall faces
  in, such as the inner shell of a solidified sphere, while a part set inside another faces out. Only its
  minority faces count as flipped, as on an open part.
- **UV overlap, bounded.** The UV triangles go into the cells of a grid, and each pair is tested once, in the
  first cell both cover. Before any pair is tested, the cell size is chosen as the candidate that makes the
  least work: about one triangle per cell over the bulk of the layout (the outer 1 % of boxes at each side left
  out, so one UV far outside the square does not stretch every cell), or about a typical triangle's size, each
  also halved and doubled. Every pair a cell holds counts toward 4,000,000 (`uv.MAX_PAIR_TESTS`, which
  `unwrap_uv` shares), and every cell entry toward another 4,000,000 (`uv.MAX_GRID_ENTRIES`). Past either, the
  faces found so far are reported with a `not_checked` saying the result is partial.
- **Collision proxies** need no UVs and overlap their objects by design, so neither issue is reported for them.
  A proxy is an object whose name Godot reads as collision only (`-colonly`, `-convcolonly` and their variants),
  or one with the `_collision` suffix that `create_collision` gives (decision 0035).
- **Contained objects.** A mesh wholly inside another closed mesh crosses no surface, so `BVHTree.overlap` misses
  it. When it lies within the other's box, the parity of three rays cast from one of its points decides.
- **Linked copies.** When objects share one mesh (linked duplicates), the mesh's own issues (topology, UVs,
  materials) are reported once, on the first object, and the issue gains `also`, naming the others. An issue
  that another copy finds differently, through its own scale or modifiers, stays on that copy. The counts follow
  what is reported.
- **UVs nothing reads.** When none of an object's materials maps an image texture (an asset coloured from a
  palette), `uv_overlap` and `uv_out_of_bounds` are `info`: how the UVs lie shows nowhere until the mesh is
  textured or baked.
- **World space, own orientation.** The geometry is moved by `matrix_world`. A mirroring transform reverses the
  faces, so the check turns them back: negative scale is its own issue and never also reads as inside out.

**The result.** `check_asset` returns `passed` (no errors), `counts` of errors and warnings, the total
`triangles`, each object's `stats` (vertices, faces, triangles, world size, UV maps) and `issues`, and the
asset-wide `issues`. An issue is `{code, severity, count, faces, message, ...}`.
- `faces` lists at most 32 indices, sorted, with `truncated` when there are more. `count` says how many faces
  the issue concerns, or how many items for an issue with no faces (loose vertices and edges, shape keys).
- The indices are the mesh's own, the ones `edit_mesh` and `set_material` take, when the modifiers leave the
  vertex and face counts unchanged. Otherwise they are the evaluated mesh's, which `get_mesh` lists with
  `evaluated: true`. Each object's `faces_of` says which (`mesh` or `evaluated`).
- At most 100 objects are listed: those with errors first, then those with warnings, each group in the scene's
  order. `objects_not_listed` counts the rest. `passed` and `counts` cover every object, listed or not.

**The rules format is a JSON object in which every key is optional.** It is given as `rules`, or read from the
JSON file at `rules_file` (a path confined like every other, through `tools.PATH_PARAMS`); giving both is
refused. The guide resource lists the keys with an example (decision 0033), so `check_asset`'s description only
points there. A selector is a name, a `"re:<pattern>"` searched for in the name (anchor it with `^` and `$`), or
a list of these. A name matches the object of that name, or of that name with Blender's suffix for a taken name
(a dot and three or more digits): `Lid` finds `Lid.001`, which `clean_names` (decision 0024) writes to the
file as `Lid`. `addon/rules.py` holds the format and its evaluation in plain Python with no `bpy`, so the
server's `check` command (decision 0025) applies the same rules to a `.glb` file.
- A pattern is searched in the first 128 characters of a name, which bounds the time one search can take.
  Blender 4.2 limits names to 63 bytes and Blender 5.0 to 255, and a `.glb` file's node names, which other tools
  may have written, have no limit. On a name longer than 128 characters, `$` therefore matches at character 128,
  as though the name ended there. A pattern that could make Python's regular expression engine backtrack without
  end is refused with `bad_argument`: a group repeated by `*`, `+` or `{n,}` (or by `{n,m}` with m above 1) that
  holds a repeat or a choice, such as `(a+)+` or `(a|aa)*`, and a pattern of more than three open-ended repeats,
  whose cost grows with a power of the name's length.
- Both `check_asset` and `check` describe the asset to `addon/rules.py` in the same neutral form. Where names
  may repeat, that description gives each object an `id` and its parent's `parent_id` (a `.glb` file's node
  indices, or `instancer/object` for an instance), and subtrees, roots and pairs follow those ids.

| Key | Takes | Issue |
|---|---|---|
| `required` | selectors, or `"Parent/Child"` paths split at the last `/`, where either half is a name or a pattern | `missing_required`; for a missing path, `children` names what its parent does hold |
| `single_root` | `true` | `not_single_root` |
| `retired` | selector | `retired_name` (objects and materials) |
| `palette` | `{material: {base_color, metallic, roughness, emission, strength}}`; colours as `"#RRGGBB"` (sRGB) or `[r, g, b]` (linear) | `off_palette` |
| `palette_tolerance` | number, default 0.001, per linear channel | |
| `palette_only` | `true` | `not_in_palette` |
| `extras` | `[{objects, keys: {key: type}, only}]`; types `number`, `integer`, `string`, `boolean`, `array`, `object`, `any` | `missing_extra`, `wrong_extra_type`, `extra_not_allowed` (with `only`, when another object has the key) |
| `size` | `[{objects, min, max}]`, each `[x, y, z]` in metres on Blender's axes, null for no limit | `size_out_of_range` |
| `origin_at_base` | `true` (every root) or a selector | `origin_not_at_base` |
| `budget` | a number (for the whole asset) or `[{objects, triangles}]` | `over_budget` |
| `apart` | `[{a, b, allow: [[name, name]]}]` | `sets_intersect` (pairs that cross or where one lies inside the other, the latter listed under `inside`, inner first); `not_checked` without geometry or past 20,000 pairs, counted before any pair is built |

- Rule issues are errors, with two exceptions. `rule_matches_nothing` is a warning: a size, budget, extras,
  origin or apart rule whose selector matches no object, which is usually a typo. `not_checked` is info. When
  `objects` narrows the check, a rule that matches none of the checked objects is skipped silently, so that one
  rule set can serve every asset of a project, checked one at a time. A check of the whole scene still warns.
- The size, budget and origin rules measure an object **together with its descendants**.
- A palette entry compares base colour, metallic and roughness channel by channel within the tolerance. It
  compares emission as colour times strength, within the tolerance or 2 % of the brightest channel, whichever is
  larger. An entry without emission must not glow. A value driven by nodes (a texture or a link) is `None` in
  the description and never matches.
- An unknown key, a value of the wrong type, or a pattern that does not compile is refused with `bad_argument`,
  and the refusal names it.

**A worked example: a set of game props.** This rule set expresses the checks that a game's props have in common:
one root per file, every colour from the palette, the extra each prop carries, each prop's size range, where its
base is, and stacked pieces that cross nothing.

```json
{
  "required": ["re:^(Crate|Barrel|Lantern)(_Stack)?$"],
  "single_root": true,
  "palette": {
    "Wood": {"base_color": "#8b5a2b", "metallic": 0.0, "roughness": 0.8},
    "Wood_Dark": {"base_color": "#5c3a1e", "metallic": 0.0, "roughness": 0.85},
    "Iron": {"base_color": "#707070", "metallic": 1.0, "roughness": 0.5},
    "Paint_Red": {"base_color": "#a03028", "metallic": 0.0, "roughness": 0.6},
    "Glass": {"base_color": "#d0e0e8", "metallic": 0.0, "roughness": 0.1},
    "Flame": {"base_color": "#ffc060", "emission": "#ffa040", "strength": 2.0}
  },
  "palette_only": true,
  "retired": ["re:^Paint_Old$"],
  "extras": [{"objects": "re:^(Crate|Barrel|Lantern)$", "keys": {"weight_kg": "number"}}],
  "size": [
    {"objects": "Crate", "min": [0.45, 0.45, 0.45], "max": [0.55, 0.55, 0.55]},
    {"objects": "Barrel", "min": [0.5, 0.5, 0.8], "max": [0.7, 0.7, 1.0]},
    {"objects": "Lantern", "min": [0.15, 0.15, null], "max": [0.25, 0.25, 0.4]}
  ],
  "origin_at_base": ["re:_Stack$"],
  "apart": [{"a": "re:_1$", "b": "re:_[23]$"}, {"a": "re:_2$", "b": "re:_3$"}]
}
```

The file `examples/rules.json` in the repository holds this rule set, and is introduced here. The set applies to
one prop at a time: `check_asset` with `objects` naming the prop, or
`blenderlens-mcp check crate.glb --rules examples/rules.json`. A stack's root sits on its base, while a single
prop's origin is where a hand holds it, so `origin_at_base` names only the stacks. `apart` needs the geometry, so
on a `.glb` file it is reported as not checked. The lantern that `examples/build_lantern.py` builds is a different
asset, larger than this set's `Lantern` and with its own materials, so that script checks it against rules of its
own, written in the script.

Checks that depend on more than one asset, or on how a game uses an asset, are not rules: whether one model
fits at another's attachment point, or how far a moving part may travel, for example. They stay in a project's
own scripts, which `run_script` runs (decision 0030), and `measure_objects` (decision 0022) measures the
distances and contacts they need.

## Why

**What verifying an asset needs.** Most of what a review of a game asset checks is the same for every asset:
the expected parts are present under one root, every colour comes from the palette with its metallic and
roughness, glowing parts emit as the palette says, the extras the game reads are present, sizes are in range,
the triangles fit a budget, the origin is at the base, and parts do not pass through each other. Every asset
needs these checks, so they are a tool. The values they check against differ from project to project, and a
project writes them once, so they are a rules object, not code.

**The checks follow how Blender's API behaves** (measured with Blender 5.0.1 in background mode):
- Topology counts, `find_doubles`, the signed volume and `BVHTree.FromBMesh(...).overlap` for self-intersection
  all take a fraction of a second on a sphere of 32,768 faces.
- `BVHTree.overlap` finds no overlap in a known pair of overlapping UV quads, because every UV triangle is
  coplanar. `mathutils.geometry.intersect_tri_tri_2d` over a grid finds it, but on 62,976 triangles it takes
  seconds, so the UV overlap check stops at 100,000 triangles and says so.
- On an open cube with one turned face, counting non-contiguous edges marks the faces on both sides of each bad
  edge. Recalculating the normals of a copy marks the turned face alone, which is the index that `edit_mesh`'s
  `flip_normals` needs. The test turns face 2 of a cube, finds `[2]`, flips it back, and the issue is gone.

**Evaluated by default.** `export_glb` applies modifiers, so a bevel's faces and an array's copies are what the
engine receives. When modifiers change the face count, the evaluated indices are not the ones `edit_mesh` takes.
Saying so in `faces_of`, rather than mapping the indices back, keeps the result exact.

**Bounded.** Without a bound, a mesh with thousands of open edges would return thousands of indices. Thirty-two
per code are enough to act on, and the count says how far the issue goes.

**The origin, reported once.** Every root whose origin is off its base gets a warning, because a game engine
places an asset by its origin. With an `origin_at_base` rule, the rule decides which objects it covers, and the
generic check steps aside rather than report the same thing twice.

**Without the bounds and special cases above, these failures would occur:**
- A UV overlap test with a fixed grid and no work bound is quadratic. Faces stacked on the whole UV square take
  seconds at 100 faces and minutes at 400, and one face's UVs moved to (60, 60) stretch a 64 by 64 grid until
  every triangle shares one cell (40 by 40 faces do not finish in five minutes). With the sized, bounded grid,
  900 stacked faces, and the case with the outlying UV, each take under a second.
- The inner shell of a solidified sphere, rightly facing its cavity, would fail as `inside_out`.
- A hidden high-poly reference would fail a budget the export meets, and draw an overlap with the part it hides
  behind.
- An inside-out cube in a collection instance would pass unchecked, and a text object's 12,384 triangles would
  go uncounted.
- A Texture Coordinate node would read as a procedural texture, an Armature alone as dropping morph targets, and
  a `create_collision` proxy would draw `no_uv` and `overlaps` on every check.
- A cube wholly inside another would be silent, to `overlaps` and to `apart` alike.
- Two `.glb` nodes with the same name, under two parents that stand far apart, would be measured as one
  subtree as wide as the distance between them.
- The rule pattern `^(a+)+$` against a 31-character name keeps Blender busy for tens of seconds, and an apart rule
  that built every pair before counting them could build millions.

**The rules format follows how rules are used:**
- Touching is not crossing. When `set_transform` with `drop` lays one object on another, `BVHTree.overlap`
  reports faces that only meet, so the tool's own placement would fail `apart` and draw `overlaps`.
- A required path finds suffixed names and takes a pattern on either side, so `required: ["Crate/Lid"]` finds
  the crate's `Lid.001`, and `Crate/re:^Lid` and `re:^Crate/Lid` can both be written.
- One rule set serves several assets. Checking one stack against rules written for several props would otherwise
  warn `rule_matches_nothing` for each rule that names another prop.
- Three linked copies of one mesh would otherwise repeat each of its issues three times, tripling the result
  without saying anything new.
- `rules_file` spares each call from resending the whole rule set, and the format is in the guide resource
  because an agent connected over MCP cannot read the README.
- The UV issues of an asset coloured from a palette, which nothing reads, would otherwise be warnings on every
  such asset.
