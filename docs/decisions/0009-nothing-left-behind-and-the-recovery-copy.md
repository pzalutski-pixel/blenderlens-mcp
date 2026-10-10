# 0009 — Nothing left behind; the recovery copy
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**Nothing left behind.**
- A background session, a `blender --background` the server started, keeps its files and Blender's log in a
  working folder: a `blenderlens-*` folder in the temp folder. Every way a background session ends removes its
  working folder:
  - `close_session`, or `open_session` replacing the session;
  - `discard` while it is busy;
  - a crashed or exited Blender, found by a call or by `get_status`;
  - the server exiting;
  - a start that fails at any step. In that case, Blender is killed and waited for first, so that Windows
    releases its log, and the end of the log is read into the refusal before the folder is removed.
- When a live session (the user's Blender window, running the add-on) is dropped, its socket is closed.
- A server that is killed outright cannot clean up, and its Blender keeps its log open until it notices that the
  server's pipe has closed. So each working folder records the process ids of its server and its Blender
  (`owner.json`), and a server starting up removes the working folders whose server and Blender have both gone.
  A working folder without that record is removed when the Blender named in its `ready.json` has gone; one with
  neither file is removed after a day. Only folders holding a `blender.log` are considered, and a process whose
  state cannot be read counts as still running.
- At server start, PNG files older than 7 days in the **default** render folder (`<temp>/blenderlens/renders`)
  are removed. A `BLENDERLENS_RENDER_DIR` that the user chose is never pruned.
- The test suite gives every server it starts a temp folder of its own, and fails if any `blenderlens-*`
  folder is left in it at the end.

**The recovery copy.**
- When the server's standard input closes and a background session holds unsaved changes, the server first
  lets an outstanding call finish, for up to 10 seconds. Then it asks Blender for a copy through `save_copy`,
  an internal add-on command that runs `bpy.ops.wm.save_as_mainfile(copy=True)`. It is not a tool; the add-on
  handles it beside `hello`. Because it saves a copy, the open file's own path and unsaved state do not
  change.
- The copy goes to `<runtime folder>/recovery/<yyyymmdd-hhmmss>-<pid>.blend`, the pid being the Blender
  process's, and the server logs the path to stderr. A Blender still busy after the wait is killed without a
  copy, and that is logged too.
- `get_status` lists the five newest recovery copies (`recovery`: path, saved, bytes).
- Copies older than 14 days are removed at server start.
- The runtime folder is `BLENDERLENS_RUNTIME_DIR`, else `%LOCALAPPDATA%\blenderlens` on Windows,
  `~/Library/Application Support/blenderlens` on macOS, and `$XDG_STATE_HOME/blenderlens` (or
  `~/.local/state/blenderlens`) elsewhere. It is the folder that also holds the live bridge's connection file
  (decision 0005).

## Why

Without these rules, the test suite itself leaves `blenderlens-*` folders, with Blender's log inside, in the
temp folder on every run: a crashed Blender, a session that `get_status` dropped, a failed start and a killed
server each leave one. Previews pile up in the default render folder with nothing ever removing them. The user
notices neither until the disk fills.

A background session's work lives only in that Blender's memory. An MCP client that exits, crashes or restarts
its server closes the server's standard input. The server then ends its sessions, and a background Blender
stops on its own once the server's pipe closes. That is right for the process and wrong for the work. Unsaved
background work does end with the session, but that is no comfort: an agent that has spent an hour building
rarely saves on a schedule. A copy at exit costs a second and keeps the user's work, and the 14-day limit
stops the folder growing without end. It is a copy, not a save, so the session still reports its changes as
unsaved for as long as it runs, and nothing the user owns is overwritten.

Pruning is limited to what BlenderLens itself chose to write. A folder the user named may hold files the user
wants, so tidying it is up to the user.
