# 0006 — Nothing the agent sends or raises ends Blender
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

Nothing the agent sends as data ends Blender or the bridge (the add-on's socket server inside Blender, which
receives the server's commands): no request, no line on the socket, and nothing that `run_python` code raises.
`run_python` itself is full Python, so code written to end Blender or the bridge still can; see "Out of reach"
below.

- **`run_python`** catches `BaseException`, which includes `SystemExit`, `KeyboardInterrupt` and
  `GeneratorExit`. It returns a `python_error` refusal with the traceback, as for any other exception. For a
  `SystemExit`, the error reads `SystemExit: <code> (run_python cannot exit Blender)`.
- **The bridge** catches `BaseException` at two levels:
  - Around each command, the failure becomes a refusal sent back to the server; an exception that is not one
    of the add-on's own refusals is sent as `blender_error`.
  - Around each request as a whole (parsing, running, sending the reply), the failure closes that one
    connection and is logged.
  - A reply that cannot be encoded, a too-deeply nested one included, becomes a `blender_error` reply.
- **The add-on's timer function** (`_tick`), which polls the bridge in live mode (the user's Blender window),
  catches `BaseException` around each poll and always asks to be called again. Blender drops a timer whose
  function raises, and the bridge would go silent with the window still open.
- **The background loop**, which polls the bridge in background mode (a `blender --background` the server
  started), catches `BaseException` around each poll, logs it, and pauses 50 ms so that a repeating failure
  cannot spin. Only the `shutdown` command, or the server's pipe closing, ends it.
- **Out of reach:** what `run_python` code does, rather than raises. `os._exit()` and
  `bpy.ops.wm.quit_blender()` end the process without raising. In live mode, code can also stop the bridge
  from inside: it can call the add-on's `stop_bridge()`, or unregister its timer with
  `bpy.app.timers.unregister`. `run_python` is full Python, and the README says so. Decision 0007 describes
  how to turn it off.

## Why

With only `except Exception`, `sys.exit(0)` through `run_python` would end the background Blender:
`SystemExit` would pass through the command, the bridge and the loop. The server would then start a fresh
Blender, and the user's unsaved work would be gone. In live mode, the same `SystemExit` would escape a timer
function that catches only `Exception`, into Blender's own timer machinery.

`KeyboardInterrupt` is the other `BaseException` that agent code raises by accident, through a `try/except`
that re-raises or a library that turns a signal into one.

Catching `BaseException` costs nothing here. The bridge is the outermost Python frame between Blender and the
agent, and the only things that should end Blender are the user, the `shutdown` command and the server going
away.

The rule says "nothing sent as data, and nothing raised" because the broader claim, "nothing the agent sends
ends Blender or the bridge", does not hold. `run_python` code can reach the add-on's module and unregister
`_tick`, or call `stop_bridge()`, and either stops the bridge. Catching exceptions cannot fence in code that
runs with Blender's own powers.
