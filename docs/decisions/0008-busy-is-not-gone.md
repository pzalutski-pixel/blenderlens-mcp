# 0008 — Busy is not gone
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

A Blender that has not replied yet is busy, and a busy Blender stays attached. Only a closed socket or an
exited process (`connection_lost`) ends a session, and ending a session always stops it. In background mode,
where the server started its own `blender --background`, that Blender is killed and its working folder (its
temporary folder) is removed. In live mode, where the user's Blender window runs the add-on, the socket to that
Blender is closed.

- **One request in flight.** The server's connection to Blender keeps the request Blender is still working on
  as `outstanding`: `{id, command, started, deadline}`. A `deadline` refusal, returned when a call runs out of
  time, leaves it there.
- **Nothing piles up.** Before sending, a call waits for the outstanding reply within its own timeout, and
  then discards that reply, since its caller has given up on it. If the timeout runs out first, the call is
  refused with the kind `busy` (exit 12), naming what Blender is running and for how long, and nothing is
  sent. A call's timeout covers the whole call: the wait, then its own reply. Replies are still matched to
  requests by id.
- **`get_status` never waits and never sends while Blender is busy.** It takes in a reply that has already
  arrived (one look at the socket, with a zero timeout), and otherwise reports `session.busy: {command,
  seconds}` from the record alone. When Blender is idle, `get_status` refreshes the session's details with
  `hello`. If that `hello` runs out of time, it becomes the outstanding request, and the session is reported
  busy, not dropped.
- **Closing and reopening.** `close_session` and `open_session` on a busy session are refused with `busy` at
  once, unless `discard` is given. With `discard`, a background Blender is killed (asking it to shut down
  would only queue behind its work), and a Blender window is let go of, with its work kept.
- **Auto mode.** With `BLENDERLENS_MODE` set to `auto`, a port that accepts the connection but does not reply
  to `hello` within 10 seconds is a busy Blender window. The refusal is `busy`, saying that a Blender window
  running the add-on accepted the connection but did not respond to `hello` within 10 seconds. It is the same
  in auto and live mode, and from `open_session` with mode auto. Only a refused connection (`not_connected`)
  makes the server fall back to a background Blender. A Blender window whose add-on replies but refuses the
  server (`token_mismatch`, `version_mismatch`) is not a reason to fall back either.
- **The wait loop** waits on the socket for at most `BlenderConnection.POLL_SECONDS` seconds (0.25) at a time. After
  each quarter-second poll that brought nothing, it calls `on_poll`, if that is set. Decision 0016 uses this
  hook for cancellation and progress.

## Why

Without these rules, each of the following happens (reproduced against Blender 5.0.1):

- `run_python` with `time.sleep(12)` and `timeout: 1`, followed by `get_status`: the status call's own `hello`
  times out behind the sleep, and `get_status` drops the session. The next call starts a second background
  Blender, so the object made before disappears from the agent's view, and the first Blender runs on with
  nobody attached.
- After a render or a sleep runs past its deadline, `close_session` without `discard` closes the background
  Blender. Its `hello` cannot get through, so the server judges whether there are unsaved changes from a stale
  reply, and closes.
- In auto mode, a Blender window that accepts the connection but is busy rendering is taken for an absent
  one, and a background Blender starts beside it. The agent's work then goes to a Blender the user cannot see.
- Every call sends its command whether or not Blender has finished the last one. Blender runs one command at a
  time on its main thread, so a client retrying a slow call queues copies of it, and each copy runs in turn
  after the client has stopped waiting.

Nothing outside Blender can interrupt its main thread during a command. So the honest response to "Blender has
not replied" is "it is still working": keep the session, send nothing more, and say so. Waiting within the
caller's own timeout keeps the common case (a call that ran a little long) invisible to the agent, while
`busy` makes the uncommon case explicit and cheap to retry.
