# 0007 — Turning off the Python tools, and path confinement
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`run_python` and `run_script` can be turned off, both by whoever configures the server and by the person at
the Blender window.**

- `BLENDERLENS_ALLOW_PYTHON` defaults to `1`. When it is `0` (or `false`, `off` or `no`):
  - `run_python` and `run_script` are left out of `tools/list`;
  - a call to either is refused before anything reaches Blender.
- The add-on has the preference **Allow Python from the agent**, on by default.
  - When it is off, the add-on refuses `run_python` and `run_script`, whichever server sends them.
  - It is shown in the add-on's preferences and in its panel.
  - The add-on reads it from the installed add-on's preferences. In background mode, where the server starts
    its own `blender --background`, Blender loads the same code under another module name and has no add-on
    preferences, so there the server's variable alone decides.
- Both refusals are of kind **`not_allowed`, exit 13**. The refusal names the setting that turned the tool
  off, and says that the other tools still work.
- `run_script` (decision 0030) follows the same two settings as `run_python`: `tools.PYTHON` names both
  tools.

**Optional path confinement.**

- `BLENDERLENS_ALLOWED_DIRS` lists folders, separated by `os.pathsep`. An empty entry is skipped, never taken
  as the current working directory.
- When it is set, the server checks every path parameter before the call reaches Blender. The path parameters
  are those that `tools.PATH_PARAMS` names, and `tools.path_keys` returns them as a tuple for each tool:
  `path` of `export_glb`, `export_file`, `import_file`, `save_file`, `open_file`, `render_preview`,
  `bake_texture` and `run_script`; `file` of `open_session`; `rules_file` of `check_asset`; and
  `base_color_image`, `normal_image` and `emission_image` of `set_material`.
- The server resolves each path as the tool would (against the server's current working directory), then with
  `os.path.realpath`, which follows symbolic links and junctions. The result must lie inside one of the
  allowed folders, compared with `os.path.commonpath` after `normcase`. Otherwise the call is refused with
  `not_allowed`, and the refusal names the path and the allowed folders.
- `save_file` with no path writes to where the file was last saved. With confinement on, the server asks
  Blender for that path and checks it too.
- `render_dir` (`BLENDERLENS_RENDER_DIR`, or the default preview folder) is allowed for `render_preview`'s
  `path` only, so previews with no `path`, or with a path in that folder, keep working. No other tool may read
  or write in `render_dir` unless it lies inside an allowed folder, and a refusal's hint offers the render folder
  only for a preview.
- When the variable is unset or empty, nothing is confined.
- Confinement covers the tools. It does not cover `run_python` or `run_script`, whose code can open any file,
  and the README says so. For a real boundary, turn the Python tools off as well.
- **Confinement checks the paths tools are given, not a .blend file's own library links.** When Blender opens
  a .blend file, it reads the libraries that file links to from wherever they point, and confinement does not
  stop that. Instead, the libraries are reported:
  - `open_file` and `import_file` return `libraries`: the absolute path of every library the open file links
    to, after the open or the import, resolved against the file that links it;
  - with confinement on, the server adds a warning to `warnings` with the code `libraries_outside_allowed_dirs`,
    naming each library outside the allowed folders.

  The README states this gap.

## Why

Users want an agent whose reach they can limit. Without these settings, the only limit is the user's own
account. The two settings serve the two people involved:

- The person configuring the MCP client decides for every session, with environment variables like the rest of
  the configuration.
- The person at the Blender window decides for that window, in the add-on's preferences, without editing any
  client configuration.

Leaving `run_python` out of `tools/list` means the model never plans around a tool it cannot use. Refusing a
direct call anyway covers clients that cache the tool list.

The server checks confinement because it has already resolved every path, so the add-on never sees a path the
server would refuse. `realpath` is what makes `allowed/../elsewhere`, and a link out of an allowed folder,
fail. A plain string-prefix test would also let `allowed2` pass for `allowed`; `commonpath` does not.

Confinement has a refusal kind of its own, rather than `bad_argument`, because the arguments are well formed
and retrying with other values of the same kind will not help. The agent has to tell the user instead.

A looser rule leaves two holes:

- If `render_dir` were allowed for every tool, any tool could export, save, open or import in a folder the
  user never put on the list. Only previews need it, so only previews get it.
- A .blend file's library links are paths no tool is given, so a file inside an allowed folder can make
  Blender read a library from anywhere. Checking them before the open would mean reading the .blend file in
  the server, and blocking them after would mean undoing an open. Returning them in the result, with a
  warning, tells the agent, and through it the user, what was read. That is what confinement can honestly
  promise.
