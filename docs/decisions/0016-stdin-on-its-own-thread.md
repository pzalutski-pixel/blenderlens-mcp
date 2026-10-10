# 0016 — stdin on its own thread: cancellation and progress
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

The server reads MCP messages from its standard input (stdin), one JSON message per line.

**A thread of its own reads stdin.** It is a daemon thread, and it parses each line.
- Any line that fails to parse gets the error `-32700` in reply, and the server reads on. That includes a line
  nested deeply enough to raise `RecursionError`, and bytes that are not UTF-8. A leading byte order mark is
  dropped before parsing, because Windows PowerShell 5.1 starts piped input with one.
- The thread handles `notifications/cancelled` itself, by setting the cancel flag of the request named.
- It replies to `ping` itself, at once.
- Everything else goes on a queue for the main thread, each request with its own cancel flag.

**Only the main thread touches the session and Blender.** It serves the queue one message at a time, and it is
the only thread that uses `SessionManager` and the socket to Blender. So Blender still runs one command at a
time. Writes to stdout go through a lock, one whole line at a time.

**Waits for Blender poll several times a second.**
- A wait for a reply waits on the socket for at most 0.25 seconds at a time (`BlenderConnection.POLL_SECONDS`).
  After each quarter-second poll that brought nothing, the connection calls `on_poll`, which the server sets for
  the request it is serving.
- The wait for a background Blender to start up looks for its ready file every tenth of a second, and calls
  `on_poll` each time.
- A true return from `on_poll` stops the wait, as if its deadline had passed.

**A cancelled request stops waiting and gets no reply.**
- The wait stops at once. The request stays Blender's outstanding request (decision 0008), so the next call
  waits for its reply within its own timeout, or is refused with `busy`, and discards that reply.
- A request cancelled while it waits its turn in the queue is never run.
- A cancelled background start is abandoned, and leaves nothing behind (decision 0009).
- Blender itself is not interrupted: its main thread is inside the command, and nothing outside Blender can
  stop it there.
- A cancel naming an id the server does not know, or one it has already replied to, is ignored and writes
  nothing. So is a malformed one.

**Progress is reported to a client that asks for it.** A `tools/call` whose `_meta` holds a `progressToken` (a
string or an integer) gets `notifications/progress` messages, at most once a second and always before the
result.
- `progress` is the number of seconds since the call began, so it rises with each notification.
- `total` is the most time the call may take, where that is known: its timeout (the `timeout` argument, else
  `BLENDERLENS_TIMEOUT`) plus the seconds any background start took before it. During a start, it is
  `BLENDERLENS_START_TIMEOUT`. The session tools have no total.
- `message` says what Blender is doing, for example "render_preview (cycles): 34 s of up to 120 s" or
  "open_session: starting a background Blender, 3 s of up to 90 s".

**Every line written is strict JSON** (the server's half of decision 0013).
- Numbers that are not finite are written as the strings `"nan"`, `"inf"` and `"-inf"`, and the line is then
  encoded with `allow_nan=False`.
- The encoding happens inside the request's `try` block, so a reply that cannot be written becomes that
  request's `-32603` error, never the end of the server.

**When stdin ends**, the reader queues the end after everything it has already read.
- Requests already read still get their replies.
- Then the recovery copy is made and the sessions end, as decision 0009 describes.
- The end of stdin cancels nothing in flight: a client that pipes its requests and then closes the pipe still
  gets every reply.

## Why

A server that reads one line, serves it to completion, and only then reads the next one has single-threaded
state by construction. But it reads `notifications/cancelled` only after the call it would cancel has
finished. That is acceptable when every call is short, and BlenderLens calls are not. A Cycles render, or a
background Blender's first start, can take longer than the TypeScript SDK's default request timeout of 60
seconds (`DEFAULT_REQUEST_TIMEOUT_MSEC` in `@modelcontextprotocol/sdk`).
- When that timeout fires, the SDK sends `notifications/cancelled` and stops listening for the result.
- A server serving one line at a time keeps waiting for Blender, writes a reply nobody reads, and replies to
  nothing in the meantime, not even a `ping`. A client checking that the server is alive concludes that it is
  not.
- With progress, a client that resets its timeout on progress (the SDK's `resetTimeoutOnProgress`) keeps
  waiting for a long render instead of giving up on it.

What serving one line at a time protects is kept: state stays single-threaded. Only the main thread touches
the session, the socket and Blender. The reader thread touches the queue, the cancel flags and the stdout
lock, and nothing else. A `ping` touches no state at all, which is why the reader may reply to it.

A cancelled request is left outstanding rather than forgotten, because Blender is still running it (decision
0008). Forgetting it would let the next command queue up behind the render inside Blender, which is exactly
the pile-up decision 0008 exists to prevent.

A line of 200,000 `[` characters on stdin makes `json.loads` raise `RecursionError`, which is not a
`ValueError`. If that escaped the reader, the server would die, and the user's background session with it. So
every parse failure gets a reply.

Tests: `tests/test_protocol.py`. With a fake add-on whose `render_preview` takes a few seconds:
- a cancelled call gets no reply;
- a `ping` sent during the call gets its reply within a second;
- the next call gets its own result, and was sent to Blender only after the render's reply came;
- a call cancelled in the queue never reaches Blender;
- progress rises, at most once a second, before the result;
- there is no progress without a token.

Against a real Blender, a cancelled render leaves the scene as it was, the next call returns its result, and a
background start reports progress.
