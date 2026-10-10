# 0005 — The live bridge's token and connection file; first-line proof
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

In live mode, the user's Blender window runs the BlenderLens add-on, and the add-on's bridge (a small socket
server inside Blender) receives the server's commands. Two rules protect the bridge.

**The live bridge always requires a token, and the server finds it without anyone copying it.**

- The bridge cannot exist without a token: `Bridge(mode, token)` raises on an empty one.
- When the user presses Start, the add-on uses the token in its preferences. If there is none, it makes one
  with `secrets.token_urlsafe(24)` and keeps it there. The add-on's panel shows that a token is required.
- While the bridge listens, the add-on writes a connection file, `live-<port>.json`, holding `host`, `port`,
  `token`, `pid`, `blender_version`, `addon_version` and `started`. It removes the file on Stop, on unregister
  and when Blender exits, and only when the file's `pid` is its own.
- The file lives in the BlenderLens runtime folder:
  - `BLENDERLENS_RUNTIME_DIR` when it is set;
  - otherwise `%LOCALAPPDATA%\blenderlens` on Windows;
  - `~/Library/Application Support/blenderlens` on macOS;
  - `$XDG_STATE_HOME/blenderlens`, else `~/.local/state/blenderlens`, elsewhere.
- On POSIX systems the folder is created with mode 0700 and the file written with mode 0600. The file is
  written whole under another name and renamed into place.
- These rules live once, in `addon/runtime.py`, which does not import `bpy`. The add-on imports it as usual.
  The server loads it from its file path (`session.runtime`), because importing the add-on package would
  import `bpy`, which exists only inside Blender.
- How the server gets the token:
  - `BLENDERLENS_TOKEN` wins when it is set.
  - Otherwise the server reads `live-<port>.json` for its configured port, every time it attaches.
  - When the port accepts the connection but closes it at the `hello`, the server returns the refusal
    `token_mismatch`. Its hint names the file read (or the variable used), and says what to do.

**A connection proves itself on its first line, or the bridge closes it without a reply.**

- The first request must be `hello`, carrying the token. A connection is unproven until it has sent that
  `hello`, and proven after it. Every later request carries the token too, so any request with a wrong or
  missing token closes the connection.
- Tokens are compared with `hmac.compare_digest`, which takes the same time whether or not they match.
- The bridge closes a connection, without replying, on:
  - a line that is not a JSON object, an empty line included;
  - a line that fails to parse for any reason, `RecursionError` on deep nesting included;
  - a first request that is anything but an authenticated `hello`;
  - a wrong token.
- Before it proves itself, a connection may send at most 64 KiB without a newline, and must prove itself
  within 10 seconds. A connection past its 10 seconds with data waiting is read once more, however long
  Blender was busy, so a render never costs a real client its place; if that read does not prove it, it is
  closed.
- The 10 seconds are measured only while Blender polls the bridge. While a command occupies Blender (a render,
  a long `run_python`), nothing is read and nothing is closed, so the 10-second limit cannot be enforced then.
  What bounds the cost of unproven connections then is a cap:
  - At most 8 connections wait to prove themselves; a new one closes the oldest of them.
  - At most 64 connections are open in all; when every slot holds a proven connection, a new one is closed.
  - Expired connections are closed before `select` is given its list of sockets, so the list is always within
    the caps. If `select` raises anyway, every unproven connection is closed, rather than the same failure
    repeating on every poll.
- New connections are accepted after the waiting ones are read, at most 8 per poll, so a newcomer never pushes
  out a client whose `hello` is already there to read.
- Background mode, where the server starts its own `blender --background`, follows the same rules with that
  session's own token (`BLENDERLENS_SESSION_TOKEN`). Started without one, `background.py` makes its own and
  writes it to the ready file (mode 0600).

## Why

A cross-protocol attack shows why. A web page can POST to the add-on's port with a JSON line in the body, and
a browser sends that request from any site. A bridge whose token was optional, and off by default, would reply
to every HTTP header line with an error and then run the `run_python` command in the body. The token prevents
that, but only if it is always on, and it is only always on if nobody has to copy it. Hence the generated
token and the connection file.

Closing without a reply, rather than returning `token_mismatch`, means a client without the token learns
nothing, not even that BlenderLens is listening. It also means an HTTP request dies on its request line.

A line of `"[" * 200000`, sent before any token check, makes `json.loads` raise `RecursionError`, which is not
a `ValueError`. A bridge that caught only `ValueError` would let it escape, and the background Blender would
exit with the user's unsaved work. So every parse failure closes just that one connection.

A proven connection may send lines of up to 64 MiB (`MAX_LINE`). The 64 KiB limit keeps an unproven
connection from making Blender buffer that much, and the 10-second limit keeps it from holding a connection
open forever.

The limits alone are not enough. On Windows, `select` takes at most 512 sockets, so a bridge that kept every
connection it accepted would fail there: 600 idle connections, opened faster than their 10 seconds run out,
would make every poll raise from then on. The bridge would then reply to nobody, close nothing, and could not
be asked for the recovery copy (decision 0009), all without a token. The 10-second limit cannot help, since it
is measured only while Blender polls. Hence the caps: with them, the same 600 connections leave the bridge
responding throughout, and every idle one is closed.

**Threat model.** These rules keep out web pages and other user accounts on the machine. A process running as
the same user can already read or change Blender's preferences, so it is out of scope, and the README says so.
