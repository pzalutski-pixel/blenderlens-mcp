"""The MCP resources this server offers: the guide, a short read on BlenderLens's conventions and traps.

The guide also gives the build, look, check, export loop. A resource costs nothing until a client
reads it, unlike the tool list, which a client hands to the model in full; the server's
instructions point the model at the guide. The guide names only tools that exist: a test checks
every tool-shaped name in it against the tool table.
"""

from __future__ import annotations

GUIDE_URI = "blenderlens://guide"

GUIDE = """# BlenderLens guide

## Conventions
- Metres and degrees. Blender is Z-up, and a model's front faces -Y (the front view looks from -Y).
  `export_glb` writes glTF's Y-up, where that front faces +Z. Vectors in custom properties are not converted:
  Blender's [x, y, z] is glTF's [x, z, -y].
- Sizes go into the mesh, so scale stays 1. When an object's scale is not 1, call `apply_transform` before a
  bevel or solidify, which see the mesh's own sizes.
- Colours as [r, g, b] are linear, as Blender and glTF store them; '#RRGGBB' is sRGB.
- `facing` and `direction` are world space; `amount`, `offset` and `pivot` are the mesh's own space.
- Face indices change after any edit that adds or removes faces. Use the `result_faces` an edit returns,
  or read `get_mesh` again; `get_mesh` with `material` (a slot or a name) lists that material's faces.
- Parent the parts to one root (`set_parent`), so the asset exports as one node tree, and put the root's
  origin on its base (`apply_transform` with `origin: "bottom_center"`), so it stands on the floor where placed.

## The loop: build, look, check, export
1. Build: `create_object`, `edit_mesh`, `add_modifier`, `set_material`, `set_vertex_color`, `join_objects`.
2. Look: `render_preview`, then open the PNG. `views: ["front", "right", "top"]` puts several on one
   sheet. `style: "clay"` shows the shape alone, and `"wireframe"` its edges. `markers` gives the pixel
   where each named object's origin lands.
3. Check: `check_asset` (topology, normals, scale, origin, UVs, overlaps, a triangle `budget`, the project's
   `rules`), and `measure_objects` for gaps and fits. Fix each error before exporting. Parts resting on each
   other (as `set_transform` `drop` leaves them) are `touching`. Only surfaces passing through each other
   overlap: `measure_objects` reports `overlapping`, `check_asset` an `overlaps` issue, an `apart` rule a crossing.
4. Export: `export_glb` with the `target` engine, then read its result: the file as read back from disk,
   with warnings for that engine. `at_origin: true` writes each root at the origin, wherever it stands in
   the scene (one root elsewhere warns `root_off_origin`). `texcoords: false` leaves the UVs out, for an
   asset whose materials use no images.
5. Keep: `save_file` saves the file, and with `copy` writes a checkpoint and leaves the open file where it
   was. `undo_change` takes back the last calls (`list: true` names them, with the object each acted on), in
   live and background mode. When it takes a background file back to its last save, the file counts as saved.

A model built by a script: `run_script` runs a .py file with `args`. The script can import modules from its
folder and those above it, up to one holding a `blenderlens.toml`; each run imports them afresh. `fresh: true`
starts from an empty file. The result names the objects added, removed and changed.

`undo_change` names only steps BlenderLens pushed. After an edit made by hand in the Blender window, or
`run_python` code that pushes or undoes undo steps itself (`bpy.ops.ed`), it stops there. To go further, use
Ctrl+Z, or call it with `force: true`.

## Rules
`check_asset` checks a project's own rules, given as `rules` (a JSON object) or `rules_file` (a .json file of
one), as `blenderlens-mcp check --rules` does. Every key is optional. A name matches the object of that name,
or with Blender's `.001` suffix (which `clean_names` drops in the file); `"re:<pattern>"` is searched for in the
name (anchor it with `^` and `$`). A list may mix names and patterns. When `check_asset` is given `objects`,
rules that match none of them are skipped.

| Key | Takes | Fails when |
|---|---|---|
| `required` | names, patterns, `"Parent/Child"` (either half a pattern) | one finds no object |
| `single_root` | `true` | not exactly one object lacks a parent |
| `retired` | names, patterns | an object or material has one of these names |
| `palette` | `{material: {base_color, metallic, roughness, emission, strength}}` | a value is out of tolerance |
| `palette_tolerance` | a number (0.001), per linear channel | |
| `palette_only` | `true` | a material is not in the palette |
| `extras` | `[{objects, keys: {key: type}, only}]` | a key is missing or mistyped; with `only`, another object has it |
| `size` | `[{objects, min, max}]`, [x, y, z] metres, null for no limit | an object with its children measures outside |
| `origin_at_base` | `true` (every root) or names | the origin is off the bottom of its bounds |
| `budget` | a number, or `[{objects, triangles}]` | more triangles, in all or under an object |
| `apart` | `[{a, b, allow: [[name, name]]}]` | one of `a` crosses or lies inside one of `b`; resting on it is fine |

Types: `number`, `integer`, `string`, `boolean`, `array`, `object`, `any`. Colours are '#RRGGBB' or linear.

```json
{"required": ["Crate", "Crate/Lid"], "single_root": true, "palette_only": true,
 "palette": {"Wood": {"base_color": "#8b5a2b", "metallic": 0.0, "roughness": 0.8}},
 "extras": [{"objects": "re:^(Crate|Barrel)$", "keys": {"weight_kg": "number"}}],
 "size": [{"objects": "Crate", "min": [0.45, 0.45, 0.45], "max": [0.55, 0.55, 0.55]}],
 "budget": [{"objects": "Crate", "triangles": 600}], "apart": [{"a": "re:_1$", "b": "re:_[23]$"}]}
```

## Motion, collision, baking, other formats
- `animate_object` keys a named clip, which exports as one glTF animation. Calling it again for the same
  object and clip replaces that object's keys. Godot loops a clip whose name begins or ends with `loop` or
  `cycle` in any case, or ends in `_loop`, `-loop`, `_cycle` or `-cycle` and a number; `loop` warns when the
  name says otherwise.
- `create_collision` adds a collision proxy, drawn as wire and parented to the mesh. With the default
  `naming: "godot"` it is named `-colonly` (box, decimated) or `-convcolonly` (convex), and a
  `target: "threejs"` export leaves it out; `naming: "plain"` names it `_collision`.
- `bake_texture` bakes with Cycles into a PNG through a UV map (call `unwrap_uv` first), and wires the image
  into the material's Principled BSDF. An `ao` bake multiplies the base colour. Give a long bake a `timeout`.
- `export_file` writes FBX, OBJ, STL, PLY or USD and reads the file back; `also_written` names what the
  exporter put beside it (USD's textures, OBJ's .mtl).

## Godot
- `export_glb` with `target: "godot"`: Draco is refused (Godot cannot load it), and `clean_names` drops
  Blender's `.001` where no sibling has the name.
- Godot reads name suffixes as import hints: `-col` and `-colonly` give collision, `-convcol` convex
  collision, `-noimp` leaves a node out. The export warns of names that read as hints by accident.
- Custom properties (`set_object` `properties`) reach Godot 4.4 and later as node metadata.
- Materials made by `set_material` cull back faces, so the file marks them single-sided. Pass
  `backface_culling: false` for a double-sided one.
- Vertex colours reach the file only when a material uses them; `set_vertex_color` wires that.

## Traps
- `check_asset`, `measure_objects` and exports see the mesh after modifiers; `get_mesh` lists the
  evaluated faces with `evaluated: true`.
- On an object with both shape keys and modifiers, an export that applies the modifiers drops the morph
  targets. `check_asset` and the export both warn.
- `unwrap_uv` method `unwrap` needs seams (`edit_mesh` `mark_seams`), and refuses a closed mesh without
  them; `smart` and the projections need none.
- Previews with engine `workbench` show each material's viewport colour, which `set_material` keeps in
  step with its base colour; materials made by `run_python` may differ there.
- Python references (`obj = bpy.data.objects["Body"]`) go stale after `undo_change`, `open_file` and
  `create_file`; look them up again by name. `undo_change` also removes stale ones from the variables
  `run_python` keeps.
- In live mode, while the user is in edit mode, exports, imports and the tools that rebuild a mesh whole
  are refused until they leave it (Tab). `edit_mesh` works on the mesh being edited.
- Previews use the file's colour management, AgX by default, which turns strong emission toward white.
"""

RESOURCES = [
    {
        "uri": GUIDE_URI,
        "name": "guide",
        "title": "BlenderLens guide",
        "description": "Conventions, traps, and the build, look, check, export loop, with Godot's part of it.",
        "mimeType": "text/markdown",
        "size": len(GUIDE.encode("utf-8")),
    },
]

_TEXTS = {GUIDE_URI: GUIDE}


class ResourceNotFound(LookupError):
    """An error raised by a resources/read for a URI this server does not have."""


def read_resource(uri) -> dict:
    """Return the resources/read result for ``uri``, or raise ResourceNotFound for a URI this server lacks."""
    text = _TEXTS.get(uri) if isinstance(uri, str) else None
    if text is None:
        raise ResourceNotFound(f"Resource not found: {uri}. resources/list names {', '.join(_TEXTS)}.")
    return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]}
