# 0037 — Other formats
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`export_file` writes FBX, OBJ, STL, PLY and USD through Blender's own exporters, and reads the file back.** It is
in the REWRITE annotation set (decision 0020), because it overwrites its file.

- **The path's extension chooses the format**: `.fbx` (`export_scene.fbx`), `.obj` (`wm.obj_export`, which writes a
  `.mtl` beside it), `.stl` (`wm.stl_export`, binary, one file), `.ply` (`wm.ply_export`, binary), and `.usd`,
  `.usda`, `.usdc` or `.usdz` (`wm.usd_export`). `.glb` and any other extension are refused, and the hint names
  `export_glb`. If this Blender lacks the exporter or importer (its add-on is switched off, or the build has no USD),
  the call fails with a `blender_error` naming it.
- **The objects are chosen as `export_glb` chooses them, with its own helpers**: the `objects` named, with their
  children; a `collection`; or the whole scene. `visible_only` defaults to true. When it is false, hidden targets
  are revealed for the export and hidden again afterwards (`_reveal`, decision 0011). Cameras and lights are left
  out. Edit mode is refused (decision 0012). The selection, the active object and the current frame are put back
  afterwards; the frame matters because the FBX exporter steps through the frames of any animation it bakes.
- **Modifiers are applied**, which is each exporter's own default. The USD exporter is told to use the viewport's
  modifier settings (`evaluation_mode='VIEWPORT'`), as the others do, rather than the render settings.
- **The read-back.** The add-on imports the written file into a temporary scene, under a context override, with the
  importer for its format. It counts the mesh objects and their triangles, then removes everything the import made,
  and the temporary scene. The result's `read_back` gives `meshes` and `triangles` against `expected_meshes` (one for
  STL and PLY, which hold every object as one mesh) and `expected_triangles` (the targets' triangles after
  modifiers), and `matches`. A mismatch adds a `read_back_differs` warning.
- **The result** gives the path, format, size in bytes, the objects exported, `read_back`, for OBJ its
  `materials_file`, and `also_written`. That last field lists every file the exporter made or wrote over beside the
  file asked for (USD's `textures` folder holding the images its materials use, OBJ's `.mtl`). The add-on finds
  them by listing the file's folder before and after the export, two folders deep and 5,000 files at most, and
  names up to 50 of them (`also_written_count` gives the total past that).

## Why

**The read-back is cheap and touches nothing.** Importing into a scene of its own keeps the import's side effects
there. On Blender 5.0.1, six exported files, each imported into a temporary scene, leave the user's scene, its
selection, its object count and every datablock count as they were, while the USD importer sets the temporary
scene's frame range from the file (its `set_frame_range` option is on by default). Importing a small file takes
milliseconds, where a separate Blender process would cost seconds and memory for the same result. The test takes a
snapshot of everything a caller can read, exports three formats, and compares.

**Meshes are counted, not objects.** The USD importer adds an empty for the file's `_materials` prim, and FBX may
add empties. An empty holds no geometry, so the mesh objects and their triangles are what is compared.

**Each exporter and importer exists in 4.2.** `io_ops.cc` at v4.2.0 registers `WM_OT_obj_export`/`_import`,
`WM_OT_ply_export`/`_import`, `WM_OT_stl_export`/`_import` and `WM_OT_usd_export`/`_import` (USD when Blender is
built with it), and `export_scene.fbx` and `import_scene.fbx` belong to the core add-on `io_scene_fbx`. The keywords
are defined there: `export_selected_objects` and `apply_modifiers` (`io_obj.cc`, `io_stl_ops.cc`, `io_ply_ops.cc`),
`ascii_format` and `use_batch` (`io_stl_ops.cc`), `ascii_format` (`io_ply_ops.cc`), `selected_objects_only` and
`evaluation_mode` with `VIEWPORT` (`io_usd.cc`), `use_selection`, `use_visible` and `use_mesh_modifiers`
(`io_scene_fbx/__init__.py`), and `check_existing`, which `WM_operator_properties_filesel` gives every exporter that
saves (`wm_operator_props.cc`). 4.2 has USD's `visible_objects_only` and 5.0 does not, so it is not passed;
the targets are revealed for the export anyway. Called from Python, the USD exporter and importer run within the
call, not as a background job (`io_usd.cc` sets `as_background_job` only when they are invoked from the interface).

**Why other formats.** An asset often goes to more than one program. Other engines and content tools want FBX
(Unity, Unreal), 3D printing and CAD take OBJ and STL, point-cloud and scanning tools take PLY, and USD is the
interchange format of Apple's tools and Omniverse. glTF stays the format for Godot and three.js (`export_glb`),
because the read-back and its warnings are written for those engines. Godot can also import FBX and OBJ, and three.js
has loaders for STL, PLY, OBJ, FBX and USDZ, but a glTF file carries the node tree, materials, extras and animations
that `export_glb` checks.

**`also_written`.** A USD export writes a `textures` folder beside the file, and a result that did not mention it
would leave files on disk the caller does not know about. The test exports a textured crate to USD and OBJ and
compares `also_written` with the files on disk.
