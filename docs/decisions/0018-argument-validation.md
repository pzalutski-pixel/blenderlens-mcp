# 0018 — Argument validation and protocol error codes
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**The server checks a tool's arguments against the tool's input schema** before anything reaches Blender, for
every tool, the session tools included.
- A small validator built on the standard library (`arguments.py`) covers the part of JSON Schema that the
  tool table uses: `type` (one name or a list), `enum`, `required`, `properties`, `additionalProperties`,
  `items`, `anyOf`, `minItems`, `maxItems` and `pattern`. Other keywords are there for the model to read, and
  are not checked.
- `boolean` accepts `true` or `false` only. The string `"false"` is refused, because Python's `bool("false")`
  is true.
- `number` must be finite. NaN and infinity are not JSON numbers, though Python's JSON reader accepts them.
- `integer` follows JSON Schema: a number with no fractional part, so `4.0` passes and `2.5` does not.
- `additionalProperties: false` refuses keys the schema does not name, wherever a schema says it. Every tool's
  schema says it (decision 0020). Where a schema does not say it, other keys pass, as JSON Schema specifies.
- A misspelt key is reported before a missing required key, since the misspelling is usually why the required
  key seems to be missing.

**A violation is a tool error.** It is a result with `isError` set and the kind `bad_argument` (exit 3), and
it carries:
- `argument`: the path of what is wrong, such as `with_children`, `vertices[1]` or `settings.width`;
- an `error` that says what was received, for example "'location' holds 2 items." or "'with_children' does not
  take "false" (a string).";
- a `hint` that says what the argument takes, as every `bad_argument` hint does: "'location' takes an array of
  3 numbers or nulls. Nothing was sent to Blender." For an unknown key, the hint lists the accepted keys.

**These are protocol errors instead:**

| Case | Code |
|---|---|
| A line that is not JSON, deep nesting and bad UTF-8 included | `-32700` |
| JSON that is not an object or (under 2025-03-26) an array | `-32600` |
| A request id that is not a string or an integer: `null`, an object, an array, `true`, `1.5` | `-32600`, id `null` |
| A missing or non-string `method` | `-32600` |
| `params` that is not an object | `-32602` |
| `tools/call` with no tool name, or a name no tool has | `-32602` |
| `tools/call` with `arguments` that is not an object | `-32602` |
| An unknown method | `-32601` |

**Some messages get no reply at all.**
- A notification, which has no `id`, is never replied to, whatever it holds.
- Neither is a response from the client: a message with `result` or `error` and no `method`. This server sends
  no requests, so there is nothing to match such a response to.
- `params: null` and `arguments: null` are read as absent.

## Why

MCP revision 2025-11-25 (Tools, Error Handling) asks servers to report input validation errors as tool
execution errors. The model reads such an error and corrects the call, which it is far less likely to do from a
protocol error. An unknown tool is the specification's own example of a protocol error.

A server that checks nothing passes whatever arrives on to Blender, and a command that reads a flag with
`bool()` acts on the string: `delete_object` with `with_children: "false"` deletes the children, and
`create_file` with `discard: "false"` drops work. The add-on reads flags strictly (decision 0010), and the
server refuses such a call before it reaches Blender, for every tool, the session tools included.

The protocol codes are those of JSON-RPC 2.0.
- The id rule is MCP's: "Unlike base JSON-RPC, the ID MUST NOT be null" (2025-06-18 and later). So `"id":
  null` is an invalid request, and the server replies to it, rather than taking it for a notification and
  staying silent.
- `params` that is not an object gets `-32602` rather than being read as if absent, so a client bug is named
  rather than half-served.

Each row of the table closes a failure that a looser reader shows: an object id is echoed back with a result;
`"id": null` gets no reply at all; a missing method gets `-32601` with an empty name; array `params` are
served as if empty; string `params` get `-32603`; and an unknown tool, or `arguments` that is not an object,
comes back as a tool result.

Tests: `tests/test_protocol.py` covers the error-code table, and the argument cases through the real server.
Those cases run in live mode with no add-on listening, so a call that reached for Blender would come back
`not_connected`. The validator has its own tests too: every argument that each tool's schema describes is
accepted; numbers must be finite and integers whole; and unknown keys are refused where the schema forbids
them.
