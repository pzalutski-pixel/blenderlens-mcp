# 0026 — Vertex colours and the material wiring
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`set_vertex_color` paints a colour into a mesh, and wires what it paints into the object's materials.** It is
in the REWRITE annotation set (decision 0020): painting the same colour on the same faces twice leaves the same
result.
- It paints `color` (linear `[r, g, b(a)]`, or `'#RRGGBB'` in sRGB, as `base_color` takes it) into the color
  attribute named by `attribute` (default `Col`), on the faces picked by `faces`, `facing` and `tolerance`, or on
  every face.
- A missing attribute is created as `BYTE_COLOR` on the `CORNER` domain and filled with white first, so the
  unpainted faces do not darken the colour they are multiplied with. An existing color attribute stored on the
  corners or the points is painted where it is. A name held by another kind of attribute or by a UV map is
  refused.
- The painted attribute becomes the mesh's active and default color attribute, as painting in Blender makes it.
- With `use_in_material` (default true), the add-on feeds a Color Attribute node reading that attribute into the
  Principled BSDF base colour of every material the object shows. An object with no material gets one named
  "Vertex Color" (reused if it exists), which culls back faces like every material BlenderLens creates (decision
  0029). The result names the materials wired, and a note names any other object that shows a wired material
  but has no attribute of that name.
- With `use_in_material: false`, the attribute is painted and the materials are left alone.
- The call is refused in edit mode (decision 0012), and a refused call changes nothing (decision 0010). The faces
  are chosen before anything changes. If anything fails afterwards, the attribute's values and the materials'
  nodes and links are put back, and an attribute or material the call created is removed.

**The base colour is one chain of nodes that BlenderLens maintains.** `set_material`'s `vertex_color` and
`base_color_image` (decision 0029) and `set_vertex_color` all rebuild the same chain into the base colour: the
attribute's node, an image's node, both through a multiply node, or neither. The nodes carry fixed names
(`BlenderLens Color Attribute`, `BlenderLens Base Color Image`, `BlenderLens Base Color Mix`), so the next call
finds and rewires them instead of adding copies, and `list_materials` reads the attribute back as
`vertex_color`.

**BlenderLens never takes over a node setup it did not make.** The nodes it adds to feed an input have names
starting with "BlenderLens", and only those nodes count as its own.
- The add-on works through the Principled BSDF behind the material output that Blender renders (the active
  one). It finds it by following the links back from the output's surface input, breadth first through every
  node linked there, visiting at most 1,000 nodes. A material with no node tree, or whose surface input nothing
  feeds, gets a Principled BSDF, connected, which keeps the name Blender gives it. A material whose surface
  another shader feeds (an Emission shader alone, say) gets none.
- `set_vertex_color` still paints, but leaves out of the wiring a material with no Principled BSDF behind its
  output, or whose base colour a node BlenderLens did not make feeds (a user's image). It names each such
  material under `not_wired` with the reason, and in the `note`.
- `set_material` refuses with `bad_argument`, changing nothing, a request to change such a material's values
  (there is no Principled BSDF to hold them), or to rewire an input (the base colour, the normal, the emission
  colour) that one of the material's own nodes feeds. Assigning such a material to faces, or setting its
  back-face culling, needs no node, so those are done.
- A call refused partway takes back what it made: each node it created, and any link that node replaced. The
  helper that does this, `paint.principled_for_change`, is shared with `bake_texture`, which refuses such a
  material in the same way.

The export side is decision 0023's: `export_glb` takes `vertex_colors` explicitly, with `material` as the
default. With the material wired, that default carries the colours into the file.

## Why

**The glTF exporter writes a color attribute only when a material uses it.** Its `export_vertex_color` option
defaults to `MATERIAL` ("Export vertex color when used by material"), in the glTF add-on that ships with Blender
4.2 (4.2.57) and in 5.0's. A painted cube exports `COLOR_0` with `ACTIVE`, and with `MATERIAL` once a Color
Attribute node feeds the base colour, but never when the cube's material ignores the attribute. An agent
that paints a part and exports it would get a file without its colours and no word why, so painting wires the
material by default. The test checks the file: a cube painted red on top has `COLOR_0`, red on the four top
corners and white on the rest.

**A direct link.** A build script that colours a model typically links a Color Attribute node straight into the base
colour and lets the corner colours be the whole look, and the chain with only an attribute is that link. glTF
multiplies `baseColorFactor` by `COLOR_0`, so that link exports as a factor of 1 plus the colours. With an image
too, the multiply node is the shape the exporter reads as texture times `COLOR_0` (its `get_color_attrib` and
`get_multiply_factors`, the same in 4.2 and 5.0).

**White first.** Blender 5.0 starts a new color attribute white, but Blender 4.2's Python API does not say what
a new one holds, so the values are set rather than assumed.

**Shared materials are named, not refused.** A material is shared on purpose, and wiring it is what was asked.
The other objects that show it without the attribute are listed, because what they then show depends on the
renderer.

**Why other node setups are left alone.** Without that rule, painting a cube whose material is an Emission
shader alone would replace its surface with a new Principled BSDF, so the glow would be gone, and painting one
whose base colour a user's image feeds would replace the image's link with the colours. And unless a refusal also
restores the links its nodes replaced, a `set_material` refused for an unreadable image, on a material with no
Principled BSDF, would remove the node it had made without restoring the Emission link that node replaced. The
surface would be fed by nothing, with no undo step and the file not marked as changed.
