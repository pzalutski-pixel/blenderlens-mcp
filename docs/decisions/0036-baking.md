# 0036 — Baking
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`bake_texture` bakes a mesh's look into a PNG with Cycles on the CPU, and wires the PNG into the object's
materials.** It is in the CHANGE annotation set (decision 0020), because a second bake reads the material wiring
that the first one left.

- **Types**: `diffuse` (the default: the colour alone, with `pass_filter {'COLOR'}` and no light), `ao`, `normal`
  (tangent space, with OpenGL's +Y, as Godot and three.js read it), `emit`, and `combined` (colour and light).
  `from_objects` names high-poly sources to bake onto the object (Blender's "selected to active" bake). The cage
  reaches out from the object by 2% of its diagonal.
- **Bounds** (decision 0013): `size` is 16 to 4096 pixels a side, square (default 1024); `samples` is 1 to 4096
  (default 32); and, as for previews, `size² × samples` is at most 1.1e9. A bake past them is refused before
  anything changes.
- **`uv_map`** must exist, and defaults to the active UV map. A mesh without one, or a name the mesh lacks, is
  refused with a hint to call `unwrap_uv`. The margin is a sixty-fourth of the size (16 px at 1024), and at least 2.
- **`path`** must be a `.png`, and is never written over a file that is not a PNG (decision 0011, the same check
  previews use). It is a path parameter, so `BLENDERLENS_ALLOWED_DIRS` confines it.
- **What is put back** after the bake: the render engine, Cycles' device and samples, every `hide_render` the bake
  had to clear (Cycles bakes only objects enabled for rendering), each material's active node, and the temporary
  image node. The scene's own bake settings are never touched, because the operator is given every setting as an
  argument. The selection and the active object are never touched either: the target and the sources reach the
  operator through a context override.
- **The material.** An object with no material gets one named after it (with back faces culled, decision 0029). An
  empty material slot is refused. A material with nodes is refused, naming it, before anything changes, if no
  Principled BSDF feeds its active output, directly or through other nodes (such as a Mix Shader). This holds with
  or without `use_in_material`: the bake goes through the material's Principled BSDF and is wired into it, so an
  Emission-only shader would be rewired and baked as something it is not. A material with no nodes at all (4.2's
  default) gets the default Principled BSDF and output. With `use_in_material` (the default), the PNG feeds:
  - the base colour, for `diffuse` and `combined`. Any color attribute's share is already in the bake, so the
    attribute is replaced, through `set_vertex_color`'s nodes;
  - the base colour multiplied by the PNG, for `ao`: a Mix node multiplies the colour the base colour held by the
    image, which the exporter writes as `baseColorFactor` and `baseColorTexture`. If something else already feeds
    the base colour, an `ao` bake is refused before baking;
  - a Normal Map node, for `normal` (with the image set to Non-Color), and the emission colour, for `emit`, through
    `set_material`'s own nodes.

  Without `use_in_material`, the bake's image and any material the bake made are removed, and only the PNG is left.
- **One image per file.** An image already loaded from the same path is reloaded and is the one wired, so baking
  again leaves one image, showing the new bake.
- **The result** gives the path, its size in bytes, the image size, samples, seconds, the UV map, the sources and
  cage, the image, and `image_driven`: for each material, the channels the PNG feeds.

## Why

**Cycles on the CPU, headless.** On Blender 5.0.1 in a background Blender (one the server starts without a window),
every type bakes. A small diffuse or AO bake takes a fraction of a second, while a combined bake at 1024 px and 32
samples takes tens of seconds on the CPU. Combined is the slow one, and the bound keeps a call from asking hours of
the CPU. Within the bound, a bake can still run past the session's timeout (`BLENDERLENS_TIMEOUT`, 120 s): 4096 px
at 64 samples is 32 times the pixels and samples of 1024 px at 32. So `bake_texture` takes a `timeout` of its own,
as `render_preview`, `run_python` and `run_script` do. The server keeps that timeout for its own wait and never
sends it to Blender. EEVEE does not bake.

**Arguments, not scene settings.** `bake_set_props` in `object_bake_api.cc` (v4.2.0) copies the scene's bake
settings into the operator only for the arguments not given, and never writes back. After a bake that passed its own
margin, the scene's margin is unchanged. The operator takes its target and sources from `CTX_data_active_object` and
`CTX_data_selected_objects`, which a `temp_override` supplies, so the user's selection need not change. Blender
refuses to bake an object with `hide_render` set ("not enabled for rendering"), hence the temporary clearing.

**4.2.** The operator's arguments (`type`, `pass_filter`, `margin`, `uv_layer`, `use_clear`, `target`, `save_mode`,
`use_selected_to_active`, `cage_extrusion`) and their values (`COMBINED`, `AO`, `NORMAL`, `EMIT`, `DIFFUSE`;
`COLOR`; `IMAGE_TEXTURES`; `INTERNAL`) are in `object_bake_api.cc`, `rna_render.cc` and `rna_scene.cc` at v4.2.0.
The Mix node (`ShaderNodeMix`, RGBA, `A_Color`/`B_Color`) is the one `set_vertex_color` wires (decision 0026), and
it exists in 4.2.

**Occlusion goes into the base colour.** glTF's `occlusionTexture` reaches the Blender exporter only through a "glTF
Material Output" node group, and Godot applies it to ambient light alone. Multiplied into the base colour, the
darkening shows in every engine, and needs nothing but the factor-times-texture form the exporter reads
(`search_node_tree.py`: a Mix set to Multiply with factor 1, a constant on one side and a texture on the other). A
base colour that is already a texture would need a texture times a texture, which glTF cannot hold, so that case is
refused rather than exported wrong. The test bakes a cube resting on a plane and finds the face on the floor dark
and the top light; after the cube is moved away and baked again, that face is light.

**The Principled BSDF rule.** Without it, an `emit` bake of a material whose output only an Emission shader fed would
make a Principled BSDF for the bake, connect it to the output in the Emission shader's place, bake black, and leave
the surface that way. The tests refuse an Emission-only material and one whose Principled BSDF feeds nothing, with
and without wiring, comparing every material's nodes and links before and after. They bake through a Principled BSDF
behind a Mix Shader. With the fake add-on used in the protocol tests, they wait for a bake within its own `timeout`,
end one that runs past it with a `deadline` refusal, and check that the timeout is never sent to Blender.
