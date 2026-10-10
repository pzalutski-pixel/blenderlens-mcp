# 0002 — Two ways to reach Blender, one command code
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

BlenderLens reaches Blender in two ways, and both run the same command code.

- **Live mode.** The user has a Blender window open, with the BlenderLens add-on installed and started. The
  server connects to a socket that the add-on listens on at localhost. The user can watch the agent work and
  step in, and each tool call becomes one undo step in that window.
- **Background mode.** The server starts `blender --background` itself and owns that process. Blender runs
  `background.py`, which loads the add-on's own modules under another module name and serves them over the
  same socket protocol.

Where the code lives and how it runs:
- **One command module.** Every command is written once, in `addon/commands.py` and the modules beside it. For
  each command, the server sends one line of newline-delimited JSON and reads one line back. No command has
  separate live and background versions.
- **Blender's main thread only.** `bpy`, Blender's Python API, may only be used from Blender's main thread, so
  neither mode serves commands on another thread. In live mode, a Blender timer polls the socket. In
  background mode, a loop in `background.py` polls it.
- **Choosing a mode.** `BLENDERLENS_MODE` (`auto`, `live` or `background`) decides which mode a call reaches.
  With `auto`, the server uses the Blender window when its add-on responds, and starts a background Blender
  only when nothing is listening (decision 0008). The `open_session` and `close_session` tools choose a mode
  explicitly.
- **Background lifetime.** A background Blender lives as long as the server that started it. It watches the
  pipe the server holds on its standard input, and stops when that pipe closes, even if the server was killed.

## Why

Blender has two audiences:
- an agent working alone, which needs no window and must not disturb the user's;
- a user who wants to watch, undo and take over.

A server built for only one of them fails the other. A server that drives a window through an add-on hangs or
fails when Blender runs in the background (`-b`). A server that runs without a window and starts a fresh
`blender -b` for every call keeps nothing from one call to the next.

Because both modes run one command module, a test suite that drives background mode also tests the code that
live mode runs. The integration suite runs in background mode. The live tests cover what is particular to live
mode: attaching through the connection file and its token, undo steps in the user's file, the add-on's panel,
timer and preferences, the user's mode and selection, and a run of each group of tools in a real Blender window.
