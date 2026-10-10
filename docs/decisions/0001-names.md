# 0001 — Names
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision
| Surface | Name |
|---|---|
| Repository | `pzalutski-pixel/blenderlens-mcp` |
| PyPI package and npm package | `blenderlens-mcp` |
| Command | `blenderlens-mcp` |
| MCP Registry | `io.github.pzalutski-pixel/blenderlens`, title "BlenderLens" |
| Python package | `blenderlens_mcp` |
| Blender add-on id | `blenderlens` |
| Environment | `BLENDERLENS_*`, plus `BLENDER_BIN` for the Blender executable |

Tests keep these names consistent across the files that carry them: the project URLs in `pyproject.toml`; the
`name`, `bin` and `mcpName` in `npm/package.json`; the `name`, `title` and repository in `server.json`; and
the last line of the README, `<!-- mcp-name: io.github.pzalutski-pixel/blenderlens -->`.

## Why
Every surface applies one pattern to the product name, so a user who knows one of the names can work out the
others. The `-mcp` suffix goes wherever a listing mixes MCP servers with everything else: the repository,
PyPI, npm and the command. The registry name is the product name alone, because the MCP Registry lists nothing
but MCP servers. The Python package uses the underscore form of the distribution name, because a Python import
name cannot contain a hyphen.

The command keeps the suffix because `uvx` and `npx` run a package's command by the package's name, and the
package is `blenderlens-mcp`. A bare `blenderlens` command would need a second entry point and would gain
nothing.

The add-on id is the product name alone. Blender shows it in its list of extensions, where every entry is an
add-on, so a suffix would say nothing. The environment variables take the product name in upper case as their
prefix. The one exception is `BLENDER_BIN`, which is named for the program it locates: the Blender executable.

The MCP Registry uses these names to confirm that the packages belong to this server. It looks for `mcpName`
in the npm package, and for the `mcp-name:` line in the PyPI package's description, which is the README. Both
are therefore tested.
