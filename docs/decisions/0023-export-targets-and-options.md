# 0023 — Export targets and options
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`export_glb` takes a `target`: `godot`, `threejs` or `generic`.** The default is `generic`. It leaves names and
collision proxies as they are, and the read-back after the export warns about what either engine would do with
them (decision 0024).

| | godot | threejs | generic |
|---|---|---|---|
| `draco` | refused | allowed (the three.js app must give the loader a DRACOLoader) | allowed |
| `image_format` `webp` | allowed | allowed | allowed |
| `lights`, `cameras` | off unless asked | off unless asked | off unless asked |
| `clean_names` | on | off | off |
| Godot's collision-only proxies (`-colonly`, `-convcolonly`) | exported | left out, named under `left_out` | exported |

Every target applies modifiers (unless `apply_modifiers` is false), writes Y-up, and writes custom properties as
glTF extras (unless `extras` is false).

**The add-on sets every exporter option explicitly** and never leaves one to the exporter's default:

| Option | Default | Exporter keyword |
|---|---|---|
| `draco` | false | `export_draco_mesh_compression_enable` |
| `image_format` | `auto` (`jpeg`, `webp`, `none`) | `export_image_format` |
| `animations` | true | `export_animations` |
| `vertex_colors` | `material` (`active`, `none`) | `export_vertex_color`, with `export_all_vertex_colors` and `export_active_vertex_color_when_no_material` false |
| `tangents` | false | `export_tangents` |
| `texcoords` | true | `export_texcoords` |
| `lights` | false | `export_lights` |
| `cameras` | false | `export_cameras` |

- **`vertex_colors: material`** writes only the colours that a material reads. With Blender's own defaults, the
  exporter also writes the active colour of a mesh that has no material, and a stand-in `COLOR_0` for a mesh
  whose colours its materials do not read. Both engines multiply the base colour by `COLOR_0`, so a colour that
  no material asked for changes how the asset looks.
- **A combination the target cannot load is refused** with `bad_argument`, before anything is written. That
  means Draco for Godot, and `texcoords: false` while an exported material maps an image (the texture would have
  no UVs to go on). For an asset without images, `texcoords: false` leaves its UVs out of the file, which suits
  an asset coloured from a palette.
- **`at_origin: true`** writes each root (each object exported without its parent) as if its world location
  were the origin. For the export, the add-on sets the root's location, in its parent's space, so that it stands
  at the origin. Afterwards it puts that location back along with everything else the export changed, so the
  scene is as it was and `unsaved_changes` stays false. Rotation and scale are kept. A root whose location is
  animated is refused, because the animation would place it on every frame. Without `at_origin`, the read-back
  warns `root_off_origin` when the file's single root stands somewhere else (decision 0024).
- **The proxies left out for three.js** are the names that Godot's importer turns into collision only:
  `colonly` or `convcolonly` after `-` or `_` at the end of the name, or after `$` anywhere in it, once trailing
  digits, underscores and spaces are stripped. This is Godot's own test, `_teststr`. `create_collision`
  (decision 0035) makes such proxies.
- **The result** adds `target`, the `options` used, `left_out`, `renamed` (decision 0024), and `morph_targets`.
  `morph_targets` lists the objects with shape keys, each as `{node, count}`: the index of its node in the file,
  read before names are cleaned, and how many shape keys it has. The read-back checks each against that node.
  With `image_format: none` and `vertex_colors: material`, a file left without `COLOR_0`, although a material
  reads a color attribute, gets a `note`. The exporter finds those colours through the base colour's texture,
  which `none` skips.
- **What has a surface.** "Nothing to export" is refused only when no chosen object is written as a mesh: a mesh,
  a curve, a surface or text (each written as the mesh it makes), or a collection instance of any of them. Blender's
  glTF exporter writes a metaball as an empty node, so metaballs alone are refused, and when metaballs are among
  the objects, the result's `warnings` include `surface_not_exported`.
- **Not offered:** GPU instancing, meshopt and KTX2. Godot ignores `EXT_mesh_gpu_instancing`, and Blender's
  exporter removes the instanced children from the node it puts that extension on, so in Godot they vanish.
  Blender writes meshopt only through an external tool, gltfpack.

## Why

**What each engine loads**, read from their sources:
- **Godot** (`modules/gltf/gltf_document.cpp`, `get_supported_gltf_extensions_hashset`, 4.7.2-stable, and the
  document extensions registered in `modules/gltf/register_types.cpp`) loads `GODOT_single_root`,
  `KHR_animation_pointer`, `KHR_lights_punctual`, `KHR_materials_emissive_strength`,
  `KHR_materials_pbrSpecularGlossiness`, `KHR_materials_unlit`, `KHR_node_visibility`, `KHR_texture_transform`,
  `KHR_texture_basisu`, `EXT_texture_webp`, and the `OMI_` physics extensions. `_parse_gltf_extensions` returns
  `ERR_UNAVAILABLE` for a file that requires any other extension, and the import fails. Blender's exporter puts
  `KHR_draco_mesh_compression` in `extensionsRequired` (`exporter.py`, `add_draco_extension`), so a Draco file
  never imports in Godot.
- **three.js** (`examples/jsm/loaders/GLTFLoader.js`): the `EXTENSIONS` table and its plugins load Draco, but
  `GLTFDracoMeshCompressionExtension` throws "No DRACOLoader instance provided" unless the app has given the
  loader one.

**WebP is allowed for Godot.** Godot registers `GLTFDocumentExtensionTextureWebP`, which loads
`EXT_texture_webp`, in both 4.2-stable and 4.7.2-stable, so a WebP file imports. The rule this record follows is
"refuse what the target cannot load", and WebP is not that.

**Lights and cameras are off by default, but allowed.** Godot loads `KHR_lights_punctual` and cameras, but an
exported asset seldom wants the scene's lights or cameras. A test exports a light and a camera when asked and
finds `KHR_lights_punctual` and the camera nodes in the file.

**The options are explicit, and the same in Blender 4.2 and 5.0.** Each keyword is defined in
`io_scene_gltf2/__init__.py`, both at Blender's `v4.2.0` tag and in Blender 5.0.1, with the same enum values
(`export_image_format`: AUTO, JPEG, WEBP, NONE; `export_vertex_color`: MATERIAL, ACTIVE, NONE, and NAME in 5.0
only). Both versions set `gltf_all_vertex_colors` and `gltf_active_vertex_color_when_no_material` from the two
flags unless the mode is NONE. The stand-in colour comes from `primitive_extract.py`: with
`gltf_all_vertex_colors`, a mesh whose materials read no colour still gets a forced `COLOR_0`. `export_texcoords`
defaults to true and sets `gltf_texcoords`, at `v4.2.0` and in 5.0.1.

**The default target is generic** so that a caller who never names an engine gets no engine's rules applied to
the file. `clean_names` is on only for Godot. three.js strips the dot itself (`Piece.001` loads as `Piece001`),
and a generic file should keep the names the `.blend` has.

**`at_origin` and `texcoords`.** An object is often exported from where it stands in a larger scene. Without
`at_origin`, an object that stands at [-2, -1, 0.5] in the scene is written at that offset from its own file's
origin, so wherever a game places the asset, it appears that far away. `at_origin` writes it at the origin
without moving it in the scene. Without `texcoords: false`, UVs that no material reads would go into every file.

**What has a surface, the node index, and the note.** The exporter writes the instanced geometry of a collection
instance, so a scene holding only a collection instance must not be refused "Nothing to export". `morph_targets`
names each node by its index because cleaning can give two nodes the same name: `Pad.001` and `Pad.002` under
different parents both become `Pad`, and the shape keys of the second would be looked for on the first and
reported dropped. With `image_format: none` and `vertex_colors: material`, a painted, textured crate loses
`COLOR_0`, and without the `note` nothing would say so.
