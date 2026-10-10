# 0017 — Session-based and stateless protocol revisions
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

MCP has two kinds of protocol revision. In the session-based revisions (2024-11-05 to 2025-11-25), a client
opens a session with `initialize`, and the revision negotiated there holds for the session. In the stateless
revision, `2026-07-28`, each request names its own revision and is served on its own. BlenderLens serves both
kinds in one process.

**`initialize` selects a session-based revision.**
- The revisions are `2025-11-25`, `2025-06-18`, `2025-03-26` and `2024-11-05`.
- The server echoes the client's version back when it is one of these; otherwise it replies with `2025-11-25`.
- The capabilities are `tools` and `resources` (decision 0033).

**A request that names its revision is served statelessly under `2026-07-28`.** Such a request carries
`_meta["io.modelcontextprotocol/protocolVersion"]`. `server/discover` is served this way even without it.
- `server/discover` returns:
  - `supportedVersions`: `2026-07-28`, then the four above;
  - `capabilities` and `instructions`.
- Every result carries `resultType: "complete"` and `_meta["io.modelcontextprotocol/serverInfo"]` (name and
  version).
- The results that the schema makes cacheable carry `ttlMs: 3600000` and `cacheScope: "private"`:
  `server/discover`, the lists (`tools/list`, `resources/list`, `resources/templates/list`)
  and `resources/read`. All of them are fixed for the life of the process.
- A request naming any other revision in `_meta`, a session-based one included, gets the error `-32022`,
  "Unsupported protocol version". Its message says that only `2026-07-28` may be named in `_meta`, and that the
  session-based revisions start with `initialize`. Its `data` holds `requested` and `supported`, which lists
  `2026-07-28` alone, because a client retries with a revision from that list, and only that one may be named
  there. `server/discover` still lists all five.
- `initialize` and `ping` belong to the session-based revisions; under `2026-07-28` they get `-32601`.

**The same tools, in the same order.** Both kinds of revision list the same tools, in the same order, and run
them the same way. Only the envelope differs.

**What a request is sent depends on that request's own revision**, never on a setting for the whole process.
- `structuredContent` and `resource_link` blocks go to a request whose revision is `2025-06-18` or later.
- A request that names no revision uses the one `initialize` negotiated.
- So a `2026-07-28` request gets `structuredContent` even beside a session that negotiated `2025-03-26`, and
  that session's own requests still do not.

**Batches are served under `2025-03-26` only.** A JSON array of messages on one line is served only when
`initialize` negotiated `2025-03-26`.
- Each message is served in order. Notifications get no reply, and a member that is not an object gets
  `-32600`.
- The replies go back as one array, or nothing at all when none are owed.
- An empty array gets `-32600`.
- Under any other revision, an array gets `-32600`, with a message naming `2025-03-26`.

**`get_status` carries `server_session`**, an id made fresh each time the server starts.

## Why

Clients of the session-based revisions open with `initialize`. Clients of the stateless revision probe with
`server/discover` and carry the revision on every request. A server that speaks only one kind fails the other
kind of client, and serving both costs one method and a few fields.

`2024-11-05` is echoed back rather than replaced with `2025-11-25`, because a client pinned to the older
revision may not speak the newer one, and the specification's rule is to echo a supported version. Such a
client is sent no `structuredContent` and no links. The tool list's annotations are extra fields to it, which
it ignores.

Batches belong to `2025-03-26`, which required servers to accept them; `2025-06-18` removed them.
Accepting them under other revisions would invite clients to rely on something the revision they negotiated
does not have.

A revision held for the whole process would give a stateless request the envelope that a session negotiated.
Once one process serves stateless requests beside a session, a revision per request is the only correct
choice.

A restarted server has lost its session, and with it a background Blender's unsaved work (decision 0009 keeps
a recovery copy). A model that compares `server_session` across `get_status` calls can tell that the server
restarted, rather than wondering why its objects are gone.

`cacheScope` is `private` because the tool list depends on this server's configuration:
`BLENDERLENS_ALLOW_PYTHON=0` leaves `run_python` and `run_script` out.

Tests: `tests/test_protocol.py`, the section on protocol revisions. It covers:
- `server/discover` with and without `_meta`;
- `-32022` for a session-based revision and for an unknown one in `_meta`;
- `resultType` and `ttlMs` on a stateless `tools/list`;
- `ping` refused under `2026-07-28`;
- `2024-11-05` echoed back without `structuredContent`;
- a batch served under `2025-03-26` and refused otherwise;
- `server_session`.
