# 0003 — Refusals: kinds, exit codes, hints
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

Every failure a tool reports is a structured refusal: a tool result with `isError` set, carrying these fields:
- `error`: what happened, in Blender's own words where Blender reported it;
- `exit`: a stable number for each kind;
- `kind`: one of a fixed list of kinds;
- `hint`: what the agent can do about it.

Extra fields, such as `traceback`, `argument` or `busy`, are added when there are any.

The kinds and their exit codes are fixed. A new kind gets the next free number, and no kind is ever
renumbered:

| exit | kind |
|---|---|
| 1 | `blender_error` |
| 2 | `not_found` |
| 3 | `bad_argument` |
| 4 | `python_error` |
| 5 | `unsaved_changes` |
| 6 | `blender_missing` |
| 7 | `not_connected` |
| 8 | `connection_lost` |
| 9 | `start_failed` |
| 10 | `version_mismatch` |
| 11 | `token_mismatch` |
| 12 | `busy` |
| 13 | `not_allowed` |
| 124 | `deadline` |
| 70 | `internal_error` |

How kinds and hints are used:
- **`not_found` says what is missing, and `bad_argument` names what is accepted**, so the agent's next call can
  be right. When a name is not found, the hint starts with the names closest to it (found with
  `difflib.get_close_matches`), then lists up to 40 of the names there are and says how many more there are, so
  the likely meaning of a typo comes first and the agent need not call `get_scene` to look. A `not_found` refusal
  that carries no hint of its own gets the default one, which says that the error names what is missing and that
  `get_scene` lists the objects and `list_materials` the materials.
- **The add-on uses the same kinds.** Every kind that the add-on (the BlenderLens code running inside Blender)
  raises is one the server knows. A test reads the source of both and checks that they agree.
- **Protocol errors are separate.** A JSON-RPC protocol error, such as an unknown tool or params that cannot
  be read, is not a refusal. Decision 0018 draws that line.

## Why

An agent can recover from a refusal it can read. From exception text alone, it has to guess. A kind it can
branch on, and a hint it can follow, turn most refusals into a single corrective call.

Stable exit numbers let scripts and the `check` command (decision 0025) report the same failures the same way,
so a script that tests for a number keeps working from one version to the next.

The closest names come first because a full list buries the useful one. In a scene of 30 objects, a refusal
that lists every name for a mistyped one leaves its likely meaning somewhere in the middle.
