# 0025 — The check command
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`blenderlens-mcp check FILE [--rules rules.json] [--budget N] [--target godot|threejs|generic] [--json]`**
checks an asset from the command line, with no model involved, so that a build script, a pre-commit hook or a CI
job can run it.

| Exit | Means |
|---|---|
| 0 | the asset passed: there is no issue or warning of severity `error` |
| 1 | the asset failed; a file that is not a readable binary glTF also fails |
| 2 | the check could not run as asked: no such file, not a `.glb` or `.blend`, a rules file that is missing, not JSON or not in the rules format (decision 0021), a bad option, or, for a `.blend`, no Blender, or a Blender that does not start or stops responding |

- **A `.glb` is read on its own, without Blender.** It gets the read-back's warnings for the target (default
  `generic`, decision 0024), the rules and the budget. The rules see the file as `glb.describe` presents it: each
  node of the scene with its parent, extras, triangles, world bounds and origin, in **Blender's axes** (glTF's +Y
  up is Blender's +Z, and glTF's +Z is Blender's -Y). That way one rules file reads the same for the `.blend` and
  for the file exported from it. Bounds come from the mesh's vertex positions when the file holds them as plain
  floats that agree with the accessor's own minimum and maximum. Otherwise they come from the corners of the
  accessor's bounds. A material value that a texture drives is unknown, and no palette entry matches it. The
  `apart` rule needs geometry that this reader does not build, so it is reported as `not_checked`.
- **A `.blend` opens in a background Blender**, a Blender process the command starts without a window. It finds
  Blender through `BLENDER_BIN` as the server does, and uses `BLENDERLENS_START_TIMEOUT` and `BLENDERLENS_TIMEOUT`.
  That Blender runs `check_asset` with the rules and the budget. With `--target`, the command also exports the file
  with that target to a scratch folder, reads the export back, and removes the scratch folder. The Blender is
  stopped either way. The verdict and the counts start from `check_asset`'s own, which cover every object it
  checked, whether listed or not. The export's refusal, if any, and the read-back's warnings are added to them.
- **Names can repeat in a `.glb`**, so `glb.describe` gives each node its index as `id` and its parent's index as
  `parent_id`, and the rules measure each subtree by those (decision 0021). A mesh placed at many nodes is bounded
  once for each distinct rotation and scale, and that box is moved to each place.
- **Output.** The command prints one line, `check FILE: PASS|FAIL (n errors, m warnings)`, then one line for each
  issue or warning: its severity, code, object, faces and message. The output is written in UTF-8 whatever the
  console's code page, because names may hold any character; a character the output cannot take is replaced.
  `--json` prints the whole result instead: `file`, `kind` (`glb` or `blend`), `target`, `passed`, `counts`,
  `issues`, the read-back's `contents`, and, for a `.blend`, the full `check_asset` result under `check`.
- **The rules module is the add-on's own** (`addon/rules.py`), loaded by its file path. Importing the add-on
  package would import `bpy`, which only Blender has. `rules.py` imports nothing beyond the standard library, so
  it runs in the server's Python 3.10 just as it does in Blender's Python.

## Why

**No model needed.** Without this command, verifying an asset means running a Blender script after every build,
started with `blender --background --python`, which prints its findings and exits non-zero on a failure. With the
checks in BlenderLens and the project's own expectations in a rules file, that loop needs no agent: a hook or a CI
job runs `check` and reads the exit code.

**A `.glb` without Blender.** Most of what a game repository holds is exported `.glb` files, and CI machines seldom
have Blender. The read-back's reader is already standard-library Python (decision 0013). Describing the file for
the rules is the same reading plus the node transforms.

**The exit codes.** 0 and 1 are the verdict, and 2 means the check could not run (argparse's own code for usage
errors), so a script can tell a failing asset from a broken invocation. A corrupt `.glb` is an asset that fails,
not a usage error: the file was given correctly, and the file is what is wrong.

**Bounds from positions when they can be trusted.** The accessor's minimum and maximum, once transformed, bound a
rotated part loosely: a part turned 45 degrees would measure about 40 % wider than it is. The positions give the
true bounds. Comparing them with the accessor's own bounds first ensures that a file whose binary chunk does not
hold what its JSON says is not measured wrongly.

**The verdict from `check_asset`'s counts.** The objects `check_asset` lists are not all the objects it checked.
A verdict rebuilt from the listed objects alone would print PASS for a `.blend` whose inside-out object comes
after the listed ones.

**Node ids.** Two parents stand 10 m apart, and each holds a node named Hand, which holds a 1 m Finger. Matched
by name, each Hand would measure 11 m wide, because each Hand's subtree would take both Fingers.

**UTF-8 output.** Written in the console's code page, a node named `Łódź_col` ends the command with a
`UnicodeEncodeError` when the output goes to a file on Windows.

**Bounds per rotation and scale.** A forest of 400 nodes that all show one 20,000-vertex tree means 8,000,000
point transforms in Python when every point is moved for every node. Bounded once for each way the mesh is turned
and scaled, the tree's points are moved once, and each node then moves only one box.
