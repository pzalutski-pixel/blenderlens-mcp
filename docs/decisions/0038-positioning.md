# 0038 — Positioning: a game-asset pipeline tool
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

BlenderLens is a tool for game-asset pipelines: an agent builds, checks, exports and verifies assets through
Blender's own operations, and sees the results.

- **The loop it serves.** The agent builds an asset with the tools or a script, looks at a rendered preview,
  checks the asset against budgets and rules, and exports a `.glb` file, which the server reads back and warns
  about for the target engine. `examples/build_lantern.py` runs this loop from start to finish.
- **Checked assets, not generated scenes.** The tools build an asset step by step, and each result reports what
  Blender holds. Asset generators and asset stores are not part of BlenderLens.
- **Local only.** BlenderLens runs on the user's machine, sends no telemetry and makes no network calls of its
  own.
- **The checks work without an LLM.** The `blenderlens-mcp check` command (decision 0025) runs in CI with no
  agent involved.
- **Not affiliated.** The README states that BlenderLens is not affiliated with or endorsed by the Blender
  Foundation. BlenderLens does not use Blender's logo, and it does not call itself the Blender MCP server.

## Why

An agent that cannot look at what it built, or measure it against a budget, can only guess, and arbitrary code
that runs unguarded is a risk the user carries. So BlenderLens gives the agent previews, checks, exports that are
read back, refusals that change nothing, and settings that turn off the tools that run code (decision 0007).
Keeping everything on the user's machine means that an asset, and the files it reads, never leave it. A check
that needs no agent can guard a repository in CI, where no model runs. Blender is the Blender Foundation's name,
and no one should take BlenderLens for an official Blender product.
