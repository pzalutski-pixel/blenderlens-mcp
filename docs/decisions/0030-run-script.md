# 0030 — run_script: a model's script as the source of truth
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`run_script` runs a `.py` file in the session's Blender**: the Blender window running the add-on (live
mode) or the Blender the server started without a window (background mode). Its annotations are the ANY set,
because it runs arbitrary code (decision 0020).

- **It runs as a script.** The file runs as `__main__`, with `__file__` set to its path, in a namespace that is
  made afresh for each run. The namespace holds `bpy`, `bmesh`, `mathutils`, `Vector`, `Matrix`, `Euler`, `math`,
  and `args`, a copy of the JSON object the caller passed (`{}` when none was passed). If the script sets
  `result`, the tool returns its value as JSON. Nothing persists from one run to the next; the names
  `run_python` keeps between calls live in its own namespace.
- **Its imports.** For the duration of the run, the add-on puts the script's folder first on `sys.path`, followed
  by each folder above it up to the nearest one that holds a `blenderlens.toml` file (the file may be empty). If
  no such file lies above the script, only the script's own folder is added. Afterwards the add-on puts `sys.path`
  back exactly as it was, whatever the script did to it. It also removes from `sys.modules` every module first
  imported during the run and found through the run's own folders (those it added, and any the script put on
  `sys.path` itself), so the next run reads an edited helper again. That covers a module whose file lies directly
  in such a folder, a package whose folder does (including a namespace package, which is a folder of modules
  without an `__init__.py`), and every module below such a package. A module found through a folder that was on
  `sys.path` before the run stays, wherever that folder lies, and so does every module imported before the run,
  Blender's and the add-on's among them. No bytecode is written beside the scripts (`sys.dont_write_bytecode` is
  set for the run).
- **`fresh: true`** first replaces the open file with an empty one. As with `create_file`, this is refused with
  `unsaved_changes` when the open file has unsaved work, unless the caller passes `discard`. The empty file is
  pushed as the undo step to come back to (decision 0031), so a single `undo_change` takes back the whole run.
- **What it returns.** The result holds `result`, `stdout` (the last 20,000 characters), `stderr` (the last
  8,000), and what changed in `bpy.data.objects`, by object name: `added`, `removed` and `changed` (each up to
  200 names), and `counts`, which gives the full totals and the number of objects there are. While the code
  prints, the add-on keeps only the end of stdout and stderr (`scripts.Tail`), so millions of printed lines
  cost no more memory than the result carries.
- **When an object counts as changed.** An object is in `changed` when its type, parent, world matrix (to
  1e-5), data, modifiers (name, type, shown in viewport and render), hide flags or custom properties differ, or
  its slot materials or how they look: viewport colour, metallic, roughness, culling, custom properties, and each
  node's name, type, mute state, image and input values with the links, up to 512 nodes and 64 inputs each. For a
  mesh it also compares the counts, the vertex coordinates (a CRC of them), the UV map and colour attribute names,
  the smooth shading and face materials (a CRC of each), the shape key values and the custom properties. With
  `fresh`, the comparison starts from the empty file.
- **The same containment, and the same two ways to turn it off, as `run_python`.** The add-on catches
  `BaseException`, so a `SystemExit` is returned as an error reading "`run_script` cannot exit Blender" (decision
  0006). The add-on's preference **Allow Python from the agent** and the server's `BLENDERLENS_ALLOW_PYTHON=0` both
  refuse it with `not_allowed`, and `tools/list` leaves it out while the variable is off (decision 0007);
  `tools.PYTHON` names both Python tools. A traceback starts from the script's own frames, without the add-on's.
  `path` is in `PATH_PARAMS`, so `BLENDERLENS_ALLOWED_DIRS` confines it.
- **Change tracking (decision 0010).** A refusal before the script starts (because of its path, `args`, Python being
  turned off, or unsaved work) changes nothing and pushes no undo step. Once the script has run, whether it finished
  or raised, the file counts as changed and the call is one undo step, as a `run_python` call that changed anything
  is (decision 0031).
- `run_python` lives beside it in `addon/scripts.py`, together with the namespace helpers the two share.

## Why

A common way to build models is one Python script per model, rerun after each edit with
`blender --background --python`. Each run starts a new Blender, nothing is previewed between runs, and each
script walks up its folders by hand to import shared helpers such as a colour palette. `run_script` runs that
loop inside the persistent session, beside `render_preview`, `check_asset` and the read-back of `export_glb`.

- **`__main__`**, because such scripts end in `main()` or `if __name__ == "__main__":`, as scripts written for
  `blender --python` do.
- **A marker file, not a guess.** Walking up to a `blenderlens.toml` puts exactly the project's folders on the
  path and no further. Without the marker only the script's folder is added, so a script cannot import from the
  folders above it by accident. The test builds a project layout (a model's folder below a root that holds the
  palette) and shows the import failing once the marker is removed.
- **Helpers are read again on each run.** A model's helpers change between runs as often as the script does.
  Python caches modules in `sys.modules`, so without removing them a second run would build with the first run's
  helpers. Removing only what the run imported through its own folders keeps the add-on safe: a marker placed high
  up (a home folder that also holds Blender) can never unload Blender's modules or BlenderLens's own. A module
  under the marker's folder but found through a folder already on `sys.path` (an add-on's) is not the run's, so
  it stays.
- **The script's own folders count too.** A build script that keeps its helpers in a folder above its own often
  adds that folder to `sys.path` itself. If only the marker's folders were handled, such a script would keep the
  first run's helpers for good, and `sys.path` would grow by one entry each run.
- **A bounded tail of output.** Holding all a script prints until the run ends would cost unbounded memory for
  a result that carries the last 20,000 characters.
- **Names, not pointers, for what changed**, because names are what an agent passes to the next tool. A renamed
  object reads as one removed and one added. The vertex CRC catches a mesh moved in place, which counts alone
  would miss, and a script that changes only a material, a UV map, a custom property or the shading has still
  changed the object.
- **ANY, not CHANGE** (decision 0020), because a script can do anything Python can.

Tests: `tests/test_run_script.py` (a script beside a `helpers.py` that it imports, built from `args`; `sys.path`
and `sys.modules` as they were; an edited helper read again; the `blenderlens.toml` walk; a folder of helpers
that the script puts on `sys.path` itself; a module found through a folder already there; a script printing
400,000 characters, of which stdout holds at most 40,000 while it prints and returns the last 20,000; changed
and removed objects, and each kind of edit named changed; `fresh` over unsaved work; a failing script's
traceback, and its run counted as a change; `SystemExit`; refusals that change nothing), `tests/test_policy.py`
(`BLENDERLENS_ALLOW_PYTHON`, path confinement), `tests/test_addon_security.py` (the add-on's preference),
`tests/test_undo.py` (one run is one undo step).
