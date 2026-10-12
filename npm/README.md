# blenderlens-mcp

[![npm](https://img.shields.io/npm/v/blenderlens-mcp.svg)](https://www.npmjs.com/package/blenderlens-mcp)
[![PyPI](https://img.shields.io/pypi/v/blenderlens-mcp.svg)](https://pypi.org/project/blenderlens-mcp/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/pzalutski-pixel/blenderlens-mcp/blob/main/LICENSE)

An MCP server that lets an AI agent model, texture and animate in **Blender**, from a single object to a whole
animated scene. You can watch the agent work in your own Blender window and undo anything it does, or let it work in
a background Blender with no window. The result is an ordinary `.blend` file, or an export in the format you need,
which the server reads back to report what it holds. Everything runs on your machine, and nothing is sent anywhere.

<p align="center">
  <img src="https://raw.githubusercontent.com/pzalutski-pixel/blenderlens-mcp/main/docs/media/teddy-forest.gif"
       alt="Two teddy bears in a forest: one picks a flower and gives it to the other, and they walk off together"
       width="400">
</p>
<p align="center"><i>A short animated scene that an AI agent made in Blender through BlenderLens, shown at double
speed.</i></p>

## Requirements

| Requirement | Needed for |
|---|---|
| **Blender 4.2 LTS or later** (CI tests 4.2 LTS and 5.0) | Everything. The server finds Blender through `BLENDER_BIN`, then `PATH`, then the usual install folders. |
| **Python 3.10 or later** on `PATH` | This package runs the bundled server with it. |
| **Node.js 18 or later** | `npx` and this package's launcher. |
| The BlenderLens add-on in Blender | Live mode only, where the agent works in a Blender window that you watch. |

No GPU is needed for previews in the default style, which render with Cycles on the CPU.

## Quick start

Add this to your MCP client's configuration:

```json
{
  "mcpServers": {
    "blenderlens": {
      "command": "npx",
      "args": ["-y", "blenderlens-mcp"],
      "env": {
        "BLENDER_BIN": "C:\\Program Files\\Blender Foundation\\Blender 5.0\\blender.exe"
      }
    }
  }
}
```

If the add-on is not started in a Blender window, the server starts `blender --background` itself and keeps it
running. To have the agent work in a Blender window that you watch instead, install the add-on, then press
**Start** in its panel (press N in the 3D viewport and open the BlenderLens tab):

```bash
npx -y blenderlens-mcp install-addon
```

## What this package contains

This package contains the whole server, which is written in Python and has **no dependencies**. It looks for
Python 3.10 or later (`python3`, `python`, or `py -3` on Windows) in the folders listed on `PATH`, using only
absolute paths and never the current working directory, and runs the server over stdio. Nothing is downloaded after
installation. The same server is available from PyPI: `uvx blenderlens-mcp`.

## Tools

- **Session:** `get_status`, `open_session`, `close_session`
- **Inspecting:** `get_scene`, `get_object`, `get_mesh`, `list_materials`
- **Checking:** `check_asset` (geometry, UVs, textures, and optionally a triangle budget and a project's own
  rules, checked as the model will be exported), `measure_objects`
- **Building:** `create_object` (including tubes, lathed shapes and text), `create_mesh`, `edit_mesh`,
  `add_modifier`, `set_modifier`, `apply_modifier`, `remove_modifier`, `set_material`, `set_vertex_color`,
  `unwrap_uv`, `join_objects`, `animate_object`, `create_collision`, `bake_texture`
- **Arranging:** `set_transform` (which can drop an object onto what is below it), `apply_transform` (which can
  move the origin), `set_parent`, `set_collection`, `set_object`, `duplicate_object`, `delete_object`
- **Files:** `save_file`, `open_file`, `create_file`, `import_file`, `export_file` (FBX, OBJ, STL, PLY, USD,
  read back after writing), `export_glb` (glTF, read back from disk, with warnings for Godot or three.js)
- **Seeing:** `render_preview`, including sheets of several views
- **Python:** `run_python`, `run_script`
- **Undoing:** `undo_change`

To check a model without an agent, for example in a build pipeline:
`npx -y blenderlens-mcp check model.glb --budget 5000 --rules rules.json`.

Units are metres, and rotations are in degrees. When a tool refuses a call, it returns a structured error with an
`exit` code, a `kind` and a `hint`.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `BLENDER_BIN` | found automatically | The Blender executable, used for background mode and `install-addon` |
| `BLENDERLENS_MODE` | `auto` | `auto` (live mode if the add-on responds, otherwise background mode), `live` or `background` |
| `BLENDERLENS_HOST` | `127.0.0.1` | The address at which the server connects to the add-on. It must match the add-on's Host preference. |
| `BLENDERLENS_PORT` | `9877` | The add-on's port, as set in its preferences |
| `BLENDERLENS_TOKEN` | not set | The add-on's token. When it is not set, the server reads the token from the add-on's connection file, `live-<port>.json`. |
| `BLENDERLENS_TIMEOUT` | `120` | Seconds to wait for one call |
| `BLENDERLENS_START_TIMEOUT` | `90` | Seconds to wait for a background Blender to start |
| `BLENDERLENS_RENDER_DIR` | `blenderlens/renders` in the temporary folder | Where `render_preview` writes when given no `path` |
| `BLENDERLENS_ALLOW_PYTHON` | `1` | `0` removes `run_python` and `run_script` from the tool list and refuses them |
| `BLENDERLENS_ALLOWED_DIRS` | not set | A list of folders, separated by `;` on Windows and `:` elsewhere. Every path given to a tool must be inside one of them, or the call is refused as `not_allowed`; `render_preview` may also write to the render folder (`BLENDERLENS_RENDER_DIR`, or its default). Libraries linked by a `.blend` are reported, not blocked. |
| `BLENDERLENS_RUNTIME_DIR` | a per-user folder | Where the add-on writes `live-<port>.json` for the server, and where recovery copies of unsaved work are saved |

## Documentation

The full tool reference, the conventions and the list of error kinds are at
[github.com/pzalutski-pixel/blenderlens-mcp](https://github.com/pzalutski-pixel/blenderlens-mcp).

BlenderLens is not affiliated with or endorsed by the Blender Foundation.

## License

Apache License 2.0.
