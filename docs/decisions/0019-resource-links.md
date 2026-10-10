# 0019 — Resource links
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**A preview is linked.** From protocol revision `2025-06-18` on, the result of `render_preview` holds a
`resource_link` block after the text:

```json
{"type": "resource_link", "uri": "file:///C:/…/Body-three_quarter-20261005-101500-123.png",
 "name": "Body-three_quarter-20261005-101500-123.png", "description": "The preview render",
 "mimeType": "image/png"}
```

- `inline: true` still adds the image itself as an `image` block.
- Under an older revision there is no link, because that revision has no such content type. The path is in the
  text.

**The server uses its own paths.** The link's `uri`, the inline image and the read-back of `export_glb` all
use the path that the server resolved and sent to Blender, never a path from Blender's reply. The result's
`path` is set to that path too. This is the server's half of decision 0011.

**No prompts.** The server offers no MCP prompts. The server's instructions and the guide resource (decision
0033) tell an agent how to work, so the same guidance is not kept in a second place.

## Why

A link lets a client show or open the picture without digging a path out of JSON text, and it costs no
context. An inline image costs its whole size in tokens on every turn it stays in the conversation. The link
is the cheap default, and `inline` remains for a client that cannot open files.

The path the server trusts must be its own. A Blender that replied to `render_preview` with another path,
whether because of a stale add-on, a misbehaving script or a malicious one, could otherwise make the server
read any file as "the preview" and hand it to the model inline. Likewise, `export_glb` could report the
contents of a different .glb than the one written. In the test, a fake add-on replies with a decoy path, and
the server reads, links and embeds the file it asked for.

Tests: `tests/test_protocol.py`.
- A preview has a `resource_link` under 2025-06-18, 2025-11-25 and 2026-07-28, and none under 2025-03-26.
- `inline` embeds the file's own bytes beside the link.
- A decoy path in Blender's reply is ignored, for the preview and for the export.
- The server declares no prompts, and `prompts/list` is an unknown method.
