# 0014 — The first public version is 0.2.0, and the Blender floor is 4.2, proven by CI
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision
**The first public version is 0.2.0**, classified "Development Status :: 4 - Beta". A version's CHANGELOG
entry is headed `## [Unreleased]` until that version is released. Before its tag is pushed, the heading becomes
`## [x.y.z] - YYYY-MM-DD`, and the release workflow refuses a tag whose version has no dated heading. The
version is written in four files:
- `src/blenderlens_mcp/__init__.py`, which hatch reads when it builds the PyPI package;
- the add-on's `blender_manifest.toml`;
- `npm/package.json`;
- `server.json`, at the top and in each package.

A test keeps them all equal, and the release workflow refuses a tag that differs from any of them.

**The oldest supported Blender, the floor, is 4.2**, set by the manifest's `blender_version_min = "4.2.0"`.
The floor is proven rather than claimed:
- CI runs the integration suite against Blender 4.2 LTS and 5.0 on Linux and on Windows, and against 5.0 on
  macOS. The pinned builds are 4.2.23, the last 4.2 release, and 5.0.1. The macOS build is the Apple Silicon
  one, because `macos-latest` runs on arm64.
- Each archive is downloaded from download.blender.org, or from mirror.blender.org when the first refuses.
  Each is pinned by the SHA-256 in the release's own `.sha256` file, and cached between runs.
- A background start on a Blender older than 4.2 is refused with `start_failed`, naming the version found and
  the floor. That check lives in `session.py` (`MIN_BLENDER`).
- The README lists the versions CI tests.

## Why
**Beta.** The tools are tested against a real Blender, but the set of tools is still growing.

**4.2 is the floor because the add-on needs it.** The add-on is a Blender extension:
- `blender_manifest.toml` and `blender --command extension install-file`, which `install-addon` and the add-on
  tests use, first shipped in 4.2;
- Blender 4.2's Python is 3.11, where `tomllib` is in the standard library, and the add-on reads its manifest
  with it.

**A floor that CI does not run is a guess.** So the whole integration suite runs on 4.2.23 as well as on 5.0.1,
on Linux and on Windows. On Windows, Blender's glTF exporter fails to import one of its own modules when
Blender is unpacked under a long path, because the module's path then goes past Windows' 260-character limit,
and every `export_glb` fails. CI therefore unpacks Blender into the runner's short temp folder.

**4.2's support has ended.** blender.org lists 4.2 LTS as supported from August 2024 to July 2026, and 4.2.23,
from July 2026, as its last update. 4.2 stays the floor because it is the first Blender the add-on can run on,
and users on it lose nothing by being allowed. Moving the floor to 4.5 LTS, supported until July 2027, would
take a new record, a manifest change and a new CI row.

**macOS runs 5.0 only.** One row covers what is particular to the platform: the `.dmg`, the app bundle's
layout and Metal. The 4.2 rows on Linux and Windows already cover the old Blender API. A macOS 4.2 row would
add the slowest download in the matrix for little that those rows miss.
