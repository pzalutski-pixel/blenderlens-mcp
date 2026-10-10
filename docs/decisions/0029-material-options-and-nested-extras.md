# 0029 — Material options and nested extras
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`set_material` takes six things beyond base colour, metallic, roughness and emission.** Each is written the way
the glTF exporter reads it, and each is read back by `list_materials` (`backface_culling`, `alpha_mode`,
`alpha_cutoff`, `vertex_color`, `images`).
- **`backface_culling`** sets `Material.use_backface_culling`. A material that `set_material` creates culls back
  faces unless told otherwise, as does the "Vertex Color" material of decision 0026. An existing material keeps
  its own setting unless the parameter is given.
- **`alpha_mode`** is `opaque` (the alpha input unlinked and set to 1), `blend`, or `clip` with **`alpha_cutoff`**
  (0 to 1, default 0.5). Clipping feeds the alpha `1 - (alpha < cutoff)` through two math nodes. Blend and clip
  take the alpha that varies over the surface: the base colour image's alpha, or else the color attribute's.
  Blend without either takes the `alpha` value. These are refused: `clip` with nothing varying (one value
  clipped shows all of the surface or none, and exports as BLEND), `alpha_cutoff` on a material that will not
  clip, and `opaque` with an `alpha` below 1. The viewport follows the mode: `surface_render_method` is BLENDED
  for blend and DITHERED otherwise, and the Workbench colour's alpha is set to match. An `alpha` given alone is
  one value: below 1 it makes the material blend, at 1 opaque, and it leaves alone an alpha link that
  BlenderLens did not make (an imported texture's, for example). An `alpha` on a material that clips is refused,
  because the clip reads the varying alpha and the value would change nothing; `blend` accepts it.
- **Only nodes that BlenderLens made are rewired** (decision 0026). A material whose output another shader
  feeds, or whose image, normal or emission input one of its own nodes feeds, is refused, naming that node,
  before anything changes.
- **`vertex_color`** makes a color attribute the base colour, through the node chain of decision 0026. When an
  object is given, the attribute must exist on it.
- **`base_color_image`, `normal_image` and `emission_image`** are image files. Each is loaded once (`images.load`
  with `check_existing`) and fed through a `ShaderNodeTexImage` node: into the base colour (multiplied by the
  vertex colour when there is one), through a Normal Map node into the normal (with the image read as
  Non-Color), and into the emission colour (setting the strength to 1 if it was 0, as `emission_color` does). A
  missing file is refused with `not_found`. A file Blender cannot decode is refused, and its image is removed.
- `''` takes an image or the vertex colour away. `diffuse_color`, which Workbench previews show, follows
  `base_color`; an image or a color attribute cannot be shown there.
- **Paths** go through the server's path handling. `PATH_PARAMS` maps a tool to one key or a tuple of keys, and
  `tools.path_keys` always returns them as a tuple. The server resolves each image path against its current
  working directory and confines it to `BLENDERLENS_ALLOWED_DIRS` (decision 0007) before anything reaches Blender.

**`set_object` properties take any JSON value**: text, numbers, true/false, objects and lists, nested.
- A property may nest at most 8 levels deep and hold at most 65,536 bytes of JSON. A name inside an object is 1
  to 63 bytes long, like a property's own name.
- A list holds one kind of value: all numbers (integers and decimals may mix, and Blender keeps them all as
  decimals), all text, all true/false, all objects or all lists. A mix is refused, naming the property and the
  place in it.
- Integers must fit in 32 bits and numbers must be finite, at every level (decision 0013). Null removes a whole
  property; inside a property it is refused.
- Every value is checked before any is set (decision 0010). If the call fails, a replaced property is put back
  from a plain copy taken at every level.
- The properties export nested in the glTF node's extras. Vectors stored there stay in Blender's axes:
  `[x, y, z]` in Blender is `[x, z, -y]` in glTF's Y-up axes. The README says so, and BlenderLens converts
  nothing inside extras.

## Why

**Back faces.** The glTF exporter writes `doubleSided: true` for every material whose `use_backface_culling` is
off (`__gather_double_sided`, the same in 4.2 and 5.0), and a new Blender material has it off. Left at that, every
material BlenderLens creates would reach Godot double-sided, which draws twice the fragments for closed meshes
that never show their insides. Existing materials keep their setting, because someone chose it.

**Alpha is read from the nodes.** In Blender 4.2 and later, the exporter decides `alphaMode` from the node tree, not
from the material's blend settings (`gather_alpha_info` and `detect_alpha_clip` in the glTF add-on, identical in
4.2.57 and 5.0). An alpha of exactly 1 is OPAQUE; `1 - (x < cutoff)`, `round(x)` or `x > cutoff` is MASK; anything
else is BLEND. The exporter chooses MASK only when `x` is not a constant (a Value node counts as one), so clipping a
plain 0.8 exports BLEND. So `clip` clips a texture's or a color attribute's alpha, and is refused without one,
rather than exporting something other than what was asked. Vertex-coloured materials commonly carry transparency in
the color attribute's alpha, which `clip` and `blend` both use.

**Two refusals prevent silent damage.** Without the refusal of an `alpha` on a clipping material, `alpha: 0.5`
on a clipped leaf would report success while the file still clipped at 0.5, because the value would go into an
alpha input that the clip nodes feed. Without the rule of decision 0026, `set_material` would rewire the base
colour and the surface of materials it had not made.

**Image paths are paths.** The server resolves and confines every path a tool is given, so a tool that takes
three paths needs the table to say so. `PATH_PARAMS` accepts a tuple, and the one helper, `path_keys`, serves the
two places in the server that read the table.

**Nested extras.** Extras for a game engine are often tables: attachment points, sockets, state flags. Blender
stores an object as an ID property group and a list as an array (for numbers and true/false) or as a list of
properties (for everything else). It refuses a list that mixes kinds ("only floats, ints, booleans and dicts are
allowed in ID property arrays"), and it turns `[1, 2.5]` into decimals. The exporter writes groups through
`to_dict` and lists item by item (`extras.py`, `__to_json_compatible`), so what was set arrives nested; the test
reads the node's extras from the exported file. The bounds keep a property a table rather than a payload. The
extras check before export (decision 0013's NaN refusal) also reads properties at every level, since a list of
groups reads back as a Python list of views.

**The axes.** glTF is Y-up, and the exporter turns the node transforms but not the numbers inside extras. A
script that stores a direction in extras otherwise has to guess which axis convention it ends up in, or store it
twice, once in each; one sentence in the README removes the guesswork.
