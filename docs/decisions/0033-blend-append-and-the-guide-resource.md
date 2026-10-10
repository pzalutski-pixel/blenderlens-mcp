# 0033 — Appending from a .blend, and the guide resource
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**`import_file` appends from a `.blend` file** what its `objects`, `collections` and `materials` parameters name
(lists of names, at least one needed). The add-on uses `bpy.data.libraries.load(path, link=False)`, so what arrives
is a copy local to the open file, never a link.

- **Objects come with their children.** Blender appends what an object depends on (its mesh, materials, images and
  parent) but not its children, which depend on it. So the add-on first reads the source file's hierarchy by linking
  its objects into temporary data (`bpy.data.temp_data`, which leaves the open file untouched), and appends every
  object below each one named.
- **Collections come with their objects and child collections.** Each is linked under `collection` (or the
  scene's own collection), unless it sits inside another appended collection. Materials come alone.
- **Into the scene.** Every appended object in no collection goes into `collection` (created if missing, as for
  other imports) or the scene's collection. An object in no collection has no user, and saving would drop it.
- **Names.** A clashing name gets Blender's `.001` suffix. The result lists the names as they ended up: `objects`
  (as summaries), `collections`, `materials`, and `libraries`, as every import's result does (decision 0007).
- **A refused call changes nothing (decision 0010).** A name the file lacks is refused with `not_found`, and naming
  nothing with `bad_argument`; both hints list what the file holds, by kind. Names given for a format other than
  `.blend` are refused. The append runs inside a rollback that removes every datablock of any kind made since it
  started. Outside object mode the import is refused, as every import is (decision 0012).
- **No library is left behind.** After appending, Blender still lists the source file as a library that nothing
  is linked from. The add-on removes it, so `libraries` names only what the open file really links.

**The guide is an MCP resource.** The server declares the `resources` capability and serves `resources/list`,
`resources/read` and `resources/templates/list` (empty) under both kinds of protocol revision, the session-based
revisions that start with `initialize` and the stateless revision 2026-07-28 (decision 0017). Under the stateless revision, the lists and the guide's text are marked cacheable.

- There is one resource, `blenderlens://guide`, of type `text/markdown` and under 8 KB. It holds the conventions
  (units, axes, colours, face indices, one root with its origin on its base); the build, look, check, export loop
  with `check_asset`, scripts and undo (where `undo_change` stops, and `force`); clips, collision proxies, baking
  and the other formats (`animate_object` and Godot's looping by name, `create_collision`, `bake_texture`,
  `export_file`); Godot's part (target, collision suffixes, extras, culling, vertex colours); the traps (after
  modifiers, morph targets dropped with modifiers, seams, Workbench's viewport colour, stale Python references, edit
  mode, and AgX, the view transform Blender uses by default from 4.0 on, which turns strong emission toward white);
  and the rules format `check_asset` takes, with every key, what it takes and when it fails, and an example that a
  test validates. `check_asset`'s description points to the guide instead of listing the keys.
- A `resources/read` of an unknown URI returns the JSON-RPC error `-32002` ("Resource not found", as the
  specification says), with the URI in `data`. A missing or non-string URI returns `-32602`.
- **The server's instructions** point to the guide in one line, and name `check_asset`, `measure_objects` and
  `undo_change`.

## Why

**Appending is how parts are reused across files.** A crate, a shared set of materials or a lantern can be kept in
one .blend and brought into others. Without appending, an agent rebuilds the part, or writes `run_python` code
against `libraries.load`, which gets the children wrong: appending a crate that way brings the crate but not the
lid parented to it.

- **The hierarchy is read in temporary data, in one load.** The add-on reads the names and the hierarchy from a
  single load inside `temp_data`. Loading the names on their own first (a `libraries.load` that requests nothing),
  then the hierarchy, then appending, makes Blender 5.0.1 crash on exit every time (`EXCEPTION_INT_DIVIDE_BY_ZERO`,
  or a mismatched free). Any two of those three steps do not. The add-on's own sequence exits cleanly.
  `bpy.data.temp_data` exists in Blender 4.2.0 (`bpy_rna_data.cc`).
- **The leftover library.** After `libraries.load(link=False)`, the open file lists the source as a library with
  one user and nothing linked from it. The import test sees this in `libraries`.

**A resource, not text in the tool list.** The tool list is capped in size (decision 0020) and is sent with every
request. The guide costs nothing until a client reads it, and the server's instructions, which a client receives
once, point to it.

A test holds the guide to naming `create_collision`, `animate_object`, `bake_texture` and `export_file`, and Godot's
rule that loops a clip by its name, since a guide that leaves a tool out leaves an agent without its conventions.
The rules format is in the guide because a client connected over MCP cannot read the README.

Tests: `tests/test_checkpoints_and_append.py` (an object with its children and their materials; a collection with
its objects; a material alone; into the collection named; a second copy named apart; a missing name and an empty
request refused with what the file holds, leaving nothing; names refused for a .glb; a failure after the append
takes it all back), `tests/test_guide.py` (the capability under both kinds of revision, list and read round-trip,
the stateless revision's caching, the errors, every tool the guide names exists, the instructions point to it),
`tests/test_protocol.py` (the capabilities).
