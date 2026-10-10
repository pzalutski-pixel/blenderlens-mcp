# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-10

The first public release. BlenderLens is an MCP server, with an optional Blender add-on, that lets an AI agent
build 3D assets in Blender, check what it built, export a `.glb` file for Godot or three.js, and look at rendered
previews along the way. After each export, BlenderLens reads the written file back and warns about anything the
target engine will handle badly.

- **Tested:** CI runs the unit tests on Linux, Windows and macOS with Python 3.10 to 3.13, and the integration
  tests with Blender 4.2 LTS and 5.0 on Linux and Windows, and 5.0 on macOS.
- **Requirements:** Python 3.10 or later. The server has no dependencies.
- **Distribution:** `blenderlens-mcp` on PyPI and npm, and `io.github.pzalutski-pixel/blenderlens` in the MCP
  Registry.

### Added

- **Two ways to reach Blender, sharing the same command code.**
  - In **live mode**, the BlenderLens add-on runs in a Blender window. You can watch the agent work, and each
    tool call that changes the scene is one undo step.
  - In **background mode**, the server starts `blender --background` itself. No add-on is needed, and that
    Blender runs for as long as the server does.
  - In both modes, the server talks to Blender over a local socket that Blender polls on its main thread.
- **40 tools:**
  - **Session:** `get_status`, `open_session`, `close_session`.
  - **Inspecting:** `get_scene`, `get_object`, `get_mesh`, `list_materials`.
  - **Checking:**
    - `check_asset` checks the mesh as it will be exported: topology, transforms, UVs, textures, a triangle
      budget, and a project's own rules, given inline or in a `rules_file`.
    - `measure_objects` measures distances between objects, tells parts that touch from parts that pass
      through each other, finds objects inside others, and casts rays.
  - **Building:**
    - `create_object` adds a primitive, a tube along a path, a lathed shape or 3D text, each built as a mesh, or
      an empty;
    - `create_mesh` builds a mesh from vertices and faces;
    - `edit_mesh` extrudes, insets, bevels (optionally only edges sharper than an angle), bisects, solidifies,
      dissolves, marks seams and more, on faces chosen by index or by the direction they face;
    - `add_modifier`, `set_modifier`, `apply_modifier` and `remove_modifier` manage modifiers;
    - `set_material` sets PBR values, emission, transparency that exports as blend or clip, back-face culling and
      images;
    - `set_vertex_color`, `unwrap_uv`, `join_objects`, `animate_object`, `create_collision` and `bake_texture`.
  - **Arranging:**
    - `set_transform`, which can also drop an object until it rests on what is below it;
    - `apply_transform`, which can also move the origin;
    - `set_parent`, `set_collection`, `set_object` (including nested custom properties, exported as glTF
      extras), `duplicate_object` and `delete_object`.
  - **Files:**
    - `export_glb` writes a `.glb` for `godot`, `threejs` or `generic` use, reads it back from disk and lists
      warnings. With `at_origin`, it writes each root object at the origin wherever it stands in the scene;
    - `export_file` writes FBX, OBJ, STL, PLY or USD, and checks the result by importing it again;
    - `import_file` imports common formats and appends objects, collections and materials from a `.blend`;
    - `save_file` (with `copy` for checkpoints), `open_file` and `create_file`.
  - **Seeing:** `render_preview` renders framed PNGs from any angle, sheets of several views, clay, wireframe and
    per-part colour styles, surrounding objects for context, cutaways, an orthographic camera of fixed width
    (`ortho_scale`), and the pixel positions of chosen objects. Its default style renders with Cycles on the CPU
    and needs no GPU.
  - **Python:** `run_python`, and `run_script`, which runs a build script with its helper modules importable.
  - **Undo:** `undo_change`, in live and background mode, with each step named after the tool and the object it
    changed.
- **Exports that report what the file contains.**
  - `export_glb` reads the written file back and reports its node tree, triangle counts and materials, with
    estimates of draw calls and video memory.
  - It warns about what the target engine will do with the file: extensions it cannot load, names that Godot reads
    as import hints (`Rock_col` becomes a collider), the names nodes will have after import, morph targets that
    the modifiers dropped, animations that Godot will loop because of their names, a single root object that is
    not at the origin, and the Godot version a feature needs.
  - `clean_names` removes Blender's `.001` suffixes where no sibling node has the same name.
- **`blenderlens-mcp check`** checks a `.glb` (without Blender) or a `.blend` against a triangle budget and a
  project's rules, for use in CI. It exits with 0 when the asset passes, 1 when it fails, and 2 when it cannot run.
- **Structured errors.** When a tool refuses a call, it returns `error`, a stable `exit` code, a `kind` and a
  `hint`.
  - A refused call changes nothing and does not mark the file as changed.
  - Arguments are checked against each tool's input schema before anything is sent to Blender.
  - Sizes and amounts of work are limited, so a single call cannot make Blender hang.
- **Sessions that keep your work.**
  - If Blender is still working on a call that timed out, the server waits for it on the next call, or refuses
    that call as `busy`, instead of treating Blender as gone.
  - If the client disconnects while a background Blender has unsaved changes, the file is saved to a recovery
    folder.
  - A background session's working folder is removed when the session ends, and one that an earlier server left
    behind is removed when the next server starts. Previews in the default render folder are deleted after 7
    days, and recovery copies after 14.
- **Protocol support.**
  - Both styles of MCP session: `initialize` (revisions 2024-11-05 to 2025-11-25) and the stateless 2026-07-28
    revision.
  - Cancellation, which stops waiting for a call, and progress notifications for long calls.
  - `resource_link` results for previews, and a guide resource for agents.
- **Safety.**
  - The add-on always requires a token, which it writes to a per-user connection file that the server reads.
  - A connection must prove it has the token in its first message, or it is closed without a reply, so a web page
    cannot reach `run_python`.
  - Agent code cannot end Blender through `SystemExit`.
  - `BLENDERLENS_ALLOW_PYTHON=0` removes the Python tools from the tool list and refuses them. The add-on's
    preference **Allow Python from the agent** makes that Blender refuse them.
  - `BLENDERLENS_ALLOWED_DIRS` restricts the paths that tools accept.
  - Nothing leaves the machine: there is no telemetry and no network access.
- **The add-on** can be built and installed with `blenderlens-mcp build-addon` and `install-addon`, or downloaded
  as a zip from each GitHub release.

### Known limitations

- **The Python tools have full access.** `run_python` and `run_script` can do anything Python can do inside
  Blender, including stopping the add-on, or quitting Blender through its quit operator (a call to `sys.exit`
  is caught). Turn them off where that matters.
- **Undo in live mode.**
  - Edits you make yourself in the Blender window are undo steps too. `undo_change` will not undo past one of
    them unless called with `force: true`. Ctrl+Z in Blender always works.
  - Rendering a preview in live mode marks the file as changed, because Blender marks the file as modified
    whenever the render engine is set, even when it is set back to the same engine.
- **Placement.** `drop` detects contact only at vertices. When the only contact would be edge against edge, such
  as one bar lying across another, that support is not found, and the object drops past it.
- **Materials.** A Principled BSDF inside a node group is not found, so `set_material` refuses to change that
  material's values, and `list_materials` reports it as having no Principled BSDF.
- **Exports.**
  - With `image_format: "none"`, Blender's glTF exporter writes no vertex colours (`COLOR_0`) for materials that
    read a color attribute as their base colour. The result of `export_glb` says when this happens.
  - Godot 4.7.0 to 4.7.2 ignore vertex colours on a mesh's first primitive (its first material). `export_glb`
    warns about affected meshes when the target is `godot`.
  - Blender's glTF exporter writes a metaball as an empty node, with no mesh. `check_asset` reports metaballs as
    errors (`surface_not_exported`), and `export_glb` warns when they are among the objects it writes.
