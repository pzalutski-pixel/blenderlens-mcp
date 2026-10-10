# 0020 — The tool list: titles, annotations, the size cap
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**Every tool's name is a verb and the thing it acts on**, in lowercase words joined by underscores
(`render_preview`, `check_asset`, `undo_change`). The verb comes from a fixed list, `VERBS` in
`tests/test_tools_table.py`, and no name contains "blender" or "lens". A test checks every tool's name against
these rules, so a tool whose verb is not in the list fails until the verb is added to `VERBS`.

**Every tool has a `title`.** The title is a short human name in Title Case ("Render Preview", "Export glTF
Binary"), and the server's `tools/list` result carries it beside the tool's `name`. A client shows the title
wherever it shows a tool to a person.

**Every tool takes its annotations from one of five named sets** defined in `tools.py`. A test pins each tool's
`destructiveHint` individually (`DESTRUCTIVE` in `tests/test_tools_table.py`). The 40 tools fall into the sets
as follows:

| Set | readOnly | destructive | idempotent | openWorld | Tools |
|---|---|---|---|---|---|
| READ | true | — | — | false | `get_status`, `get_scene`, `get_object`, `get_mesh`, `list_materials`, `check_asset`, `measure_objects` |
| WRITE | false | false | false | false | `create_object`, `create_mesh`, `add_modifier`, `duplicate_object`, `import_file`, `create_collision` |
| REWRITE | false | true | true | false | `open_session`, `close_session`, `set_modifier`, `set_material`, `set_vertex_color`, `set_transform`, `set_parent`, `set_collection`, `set_object`, `export_glb`, `export_file`, `save_file`, `open_file`, `create_file`, `render_preview` |
| CHANGE | false | true | false | false | `edit_mesh`, `apply_modifier`, `remove_modifier`, `apply_transform`, `delete_object`, `unwrap_uv`, `join_objects`, `animate_object`, `bake_texture`, `undo_change` |
| ANY | false | true | false | true | `run_python`, `run_script` |

- A **WRITE** tool adds something new, so calling it twice adds two things. A **REWRITE** tool replaces values,
  a file or a session, so calling it twice with the same arguments leaves the same result. A **CHANGE** tool
  alters what already exists in a way that a second call compounds or cannot repeat. An **ANY** tool runs
  arbitrary code.
- **`open_session` is destructive.** With `discard`, it drops a background session's unsaved work, or kills a
  busy Blender (decision 0008). It belongs in REWRITE: two calls with the same arguments leave the server
  attached to the same session.
- **`render_preview` is destructive.** It overwrites the PNG at its path (decision 0011 limits that to PNG
  files). It belongs in REWRITE: two renders to one path leave the same file. With no path, each call adds a
  file to the render folder (`BLENDERLENS_RENDER_DIR`), which is harmless to retry.
- **`run_python` and `run_script` have a set of their own, ANY.** Python can delete files, write anywhere and
  reach the network, and running code twice can do its work twice. They are the only open-world tools.
- **There is no separate set for tools that "run" something.** `open_session` and `render_preview` each replace
  something, so they belong in REWRITE with the other tools that replace.
- **The READ set carries only `readOnlyHint: true` and `openWorldHint: false`.** The MCP specification reads
  `destructiveHint` and `idempotentHint` only when `readOnlyHint` is false, so a client ignores them on a
  read-only tool. `openWorldHint` defaults to true, so the set states false: these tools read the file open in
  a local Blender and nothing beyond it. The other sets carry all four keys, so whether a tool changes anything
  is never left to the specification's defaults.
- The records of the other tools explain where each one belongs. `check_asset` and `measure_objects` only read
  (decisions 0021, 0022). `set_vertex_color` leaves the same result when repeated (0026). `unwrap_uv` and
  `join_objects` change the mesh (0027, 0028). `undo_change` takes back what earlier calls did (0031).
  `animate_object` replaces a clip (0034). `create_collision` adds a proxy beside the object (0035).
  `bake_texture` reads the material wiring that the previous bake left (0036). `export_file` overwrites its
  file (0037).

**Every input schema refuses keys it does not name.** The `schema()` helper adds `additionalProperties: false`
to each input schema, and the server enforces it (decision 0018).
- A test walks the add-on's source from each command through every helper the command hands `params` to.
  Every key the add-on reads must be in that tool's schema. For example, `set_object` reads `rename_data`
  (default true: a mesh no other object uses is renamed with its object), so the schema lists it.
- The walk sees only keys written as literals. `apply_transform` reads its three flags in a loop, so those are
  checked by hand, as are the keys of the three tools the server handles itself without the add-on.
- A schema with no required keys leaves out the `required` list rather than sending it empty.
- The vectors `edit_mesh` takes (`offset`, `factor`, `rotation`, `pivot`, `direction`) and `facing` hold
  numbers only. The add-on refuses a null component there, since there is no current value to keep.

**The serialised `tools/list` result is capped at 38 KB** (`TOOLS_LIST_MAX_BYTES = 38 * 1024`, 38,912 bytes).
- The test measures `json.dumps` of the result with `BLENDERLENS_ALLOW_PYTHON=1`, so `run_python` and
  `run_script` are listed: the longest the list can be.
- The 40 tools measure about 38,700 bytes, just under the cap, about 0.95 KB per tool.
- Every tool must fit in the bytes the cap leaves, by the rules below for what a description says.
- To fit, a tool description keeps "Returns:" before "WHEN TO CALL", and states each fact once, where it
  belongs:
  - a parameter's meaning and default go in that parameter's own description;
  - what the schema already says (types, enums, lengths) is not repeated;
  - examples are left out;
  - "Returns:" names the parts of the result that a caller looks for, not every field.
- The maximums set by decision 0013 are not in the descriptions. A value over a maximum is refused, and the
  refusal names the maximum. The README lists the main ones.
- One clause stays in the descriptions for each of these decisions: `direction` is in world space while
  `amount`, `offset` and `pivot` are in mesh space (0013); on the session tools, `discard` drops a busy
  Blender's work as well as unsaved work (0008); previews are PNG only (0011); and the refusals in edit mode
  (0012).

**The server's instructions**, which a client receives when it connects, name `busy` and `not_allowed` among the
refusals. They offer `run_python` and `run_script` only "when enabled", because `BLENDERLENS_ALLOW_PYTHON=0` or
the add-on's preference can switch them off (decision 0007).

## Why

**The size.** A client that loads every tool sends the whole list with every request, so each byte is paid for
on every turn of every session. Claude Code defers MCP tools until they are needed, but not every client does.

**Most of the list is structure, not words.** With every description emptied, the list still measures about
27,400 of its 38,700 bytes:
- about 4.2 KB are annotations, 1.2 KB are `additionalProperties: false` and 1.0 KB are titles;
- most of the rest is the 225 parameters with their types, enums and lengths.

That leaves about 11 KB of words for 40 tools and 225 parameters. The largest tools are the ones with the most
parameters:

| Tool | Parameters | Size |
|---|---|---|
| `edit_mesh` | 18 | 2.9 KB |
| `create_object` | 22 | 2.6 KB |
| `render_preview` | 20 | 2.4 KB |
| `set_material` | 18 | 2.3 KB |
| `export_glb` | 17 | 1.9 KB |
| `create_mesh` | 10 | 1.6 KB |

**Why 38 KB.** Structure grows with every parameter, and the words are what is left over. A cap of 34 KB, about
0.85 KB per tool, would leave about 7 KB of words for 40 tools and 225 parameters, too few to say what each
parameter does, so parameters that agents need would have to go. 38 KB holds the 40 tools and their
parameters, and leaves no room for prose.

The test measures the `result` object, not the whole JSON-RPC response, and it measures it as the server returns
it under the session-based revisions, which start with `initialize`. Under the stateless revision, 2026-07-28,
every result also carries `resultType` and the server's name and version in `_meta` (decision 0017), and those
fields say nothing about the tools.

**Each fact once, in its place.** A model reads a parameter's description when it fills in that parameter, so a
default or a unit belongs there, not in a paragraph above. Of the facts a call needs, the maximums are the only
ones moved out entirely. They matter only to a call that exceeds one, and the server refuses that call, naming
the maximum, before Blender does any work. Keeping them would cost about 90 bytes in a list that has no room.
Everything else that goes is examples and the fields of results, which the result itself shows.

**The annotations.** `open_session` with `discard` throws away a background session, and `render_preview` overwrites
a file (decision 0011 limits it to PNG files, but it overwrites any PNG). `run_python` and `run_script` can open
sockets and write anywhere. A client that asks the user before running destructive or open-world tools, and not
before others, would run these tools without asking if they were marked as anything less.

**The names.** A name that reads as an action tells an agent what a call does before it reads the description.
A fixed list of verbs keeps one action under one word, so that changing a value is always `set_` and never also
`update_` or `change_`. The client already shows which server a tool comes from, so the server's name in a
tool's name would only repeat it.

**Closed schemas.** Without them, a misspelt key (`colour` for `base_color`, `with_child` for `with_children`) is
ignored by the command, and the call succeeds while doing something other than what was meant. Declaring
`additionalProperties: false` is what lets the server refuse such a key and name the keys that exist (decision
0018). Checking the add-on's reads against the schemas first ensures that no key a command actually uses is
refused.
