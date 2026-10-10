# 0013 — Bounded, finite inputs; world-space directions; a robust GLB reader
- Status: accepted
- Date: 2026-10-05
- Supersedes: —

## Decision

**Bounded inputs.** A value over its maximum is refused with `bad_argument`, and the refusal names the
maximum:

| Input | Maximum |
|---|---|
| `segments`, `rings` (circle, cylinder, cone, uv_sphere, torus) | 1024 |
| `ico_sphere` `subdivisions` | 8 |
| `plane` and `grid` `subdivisions` | 1000 |
| `edit_mesh` `cuts` | 64 |
| `edit_mesh` bevel `segments` | 64 |
| `create_mesh` vertices | 1,000,000 |
| `create_mesh` faces | 2,000,000 |

The maximums are constants at the top of `addon/meshops.py`. `render_preview` sets its own bounds: `samples`
from 1 to 4096, and `resolution` from 16 to 4096 pixels on each side.

**Bounded work, not only bounded parameters.** A parameter within its maximum can still multiply what the mesh
already holds, so the work itself is bounded too, by the mesh maximums above (2,000,000 faces and 1,000,000
vertices). Each refusal names the limit.

- `edit_mesh` `subdivide` is refused before it starts when the selected faces times (cuts + 1)², plus the
  other faces, would exceed 2,000,000 faces. `bevel` is refused likewise, counting the faces plus the rim
  edges times the segments.
- `add_modifier` and `set_modifier` check the following, in this order, so that nothing slow is built before a
  refusal:
  1. the maximum of each multiplying setting given, which the refusal names:

     | Modifier | Setting | Maximum |
     |---|---|---|
     | array | `count` | 10,000 |
     | subdivision | `levels`, `render_levels` | 6 |
     | screw | `steps`, `render_steps` | 1024 |
     | screw | `iterations` | 64 |
     | remesh | `octree_depth` | 10 |
     | bevel | `segments` | 64 |
     | ocean | `resolution`, `viewport_resolution` | 32 |
     | ocean | `repeat_x`, `repeat_y` | 64 |

  2. an estimate, from the settings alone, of the faces the whole modifier stack makes, refused past
     2,000,000. The estimate counts array copies (fitted lengths included), 4 to the power of the subdivision
     levels (viewport or render, whichever is higher), screw turns, bevel segments, remesh voxels or octree
     depth, and a generated ocean;
  3. the evaluated result, refused past 2,000,000 faces or 1,000,000 vertices.

  Each refusal is rolled back. A stack that is already past a limit (made by hand in a Blender window, or by
  `run_python`) may still be changed, as long as the change does not make it bigger.
- `render_preview` with an engine that samples (cycles or eevee) is refused when width times height times
  samples, over all its views (decision 0032), exceeds 1.1e9. That allows 1024 x 1024 at 1024 samples, or 4096
  x 4096 at 64.

**No NaN or infinity, coming in or going out.**
- Coming in: every number parameter refuses a value that is not finite (`common.finite`, used by `triple`,
  `color`, `number`, `_number_in`, preview's `_number`, modifier settings and custom properties). Every one
  except a custom property also refuses a magnitude above 1e9. Blender keeps sizes, coordinates and most
  settings as 32-bit floats, which at 1e9 can only step 64 m at a time, and which become infinite past 3.4e38.
  Custom properties are doubles in Blender, so only their finiteness is checked.
- Going out: `rnd`, and through it `vec` and `jsonable`, turn a float that is not finite into the string
  `"nan"`, `"inf"` or `"-inf"`, so every result is strict JSON whatever Blender holds. The bridge (the
  add-on's socket server inside Blender) and the server (decision 0016) convert in the same way, and write
  with `allow_nan=False`.

**World-space directions.** `edit_mesh`'s `direction`, given as a name or a vector, is in world space, as
`facing` is. It is turned into the mesh's own space by the inverse of the object's rotation and scale, and
normalised; `amount` stays a distance in the mesh's own space. `translate`'s `offset`, and the `pivot` of
`scale` and `rotate`, stay in mesh space, and the tool description says so.

**A robust GLB reader.** `glb.read_json_chunk` and `glb.summarize` either return a result or raise `GLBError`
naming the file. The reader checks:
- every index for its type and range, negative ones included, which Python would otherwise count from the end;
- every value for its type;
- that the JSON is UTF-8 and strict: no `NaN`, no `Infinity`, no number that overflows a float, and no integer
  too long for Python to read;
- that the nesting is not too deep;
- that the node tree is a tree: a node reached twice, through a cycle or a child with two parents, is a fault.

What glTF allows is read, not refused: a primitive without `POSITION` (it adds no bounds, and its triangles
are counted from its indices or another attribute), and triangle strips and fans (one triangle per index after
the first two). `tests/test_glb.py` feeds the reader known malformed files and more like them, every fifth
truncation of a good file, and 400 seeded byte mutations.

## Why

**Bounds.** One call can take the session down. `ico_sphere` with `subdivisions: 12` asks for 83 million
faces, which takes gigabytes of memory and many minutes on Blender's main thread, while every other call waits.
The maximums are far above any game asset (an `ico_sphere` with 8 subdivisions already has 327,680 faces) and
low enough that the worst allowed call finishes in seconds. The bounds test uses values just over each maximum
(`subdivisions: 9`, not 12), so that, run against code without the bounds, it fails in seconds instead of
exhausting memory.

**Finite numbers.** JSON has no NaN, but `1e999` is valid JSON syntax, and Python parses it as infinity. Given
`scale: [1e999, 1, 1]`, Blender stores the value, and the result then carries `Infinity`, which a strict JSON
reader rejects, so the client loses the whole result. `run_python` returning a vector that holds NaN does the
same. Blender clamps an infinite location to the largest 32-bit float but keeps NaN, so the output side needs
the conversion whatever the input side refuses.

**World-space directions.** `facing` is in world space, and a `direction` in mesh space would make "top" on a
rotated object mean the mesh's own top: a cube rotated 90° about X, extruded from its world-top face along
`top`, would grow toward world -Y. Directions named after the world's sides must mean the world's sides.
Amounts, offsets and pivots stay in mesh space, where `get_mesh space: local` reports them.

**The GLB reader.** `export_glb` reads back whatever the exporter wrote, and the server catches only `OSError`
and `GLBError` around that read. Without these checks, small malformed files make a reader raise
`UnicodeDecodeError`, `AttributeError`, `IndexError`, `RecursionError` or `TypeError`, and each would reach the
agent as an `internal_error`. A node cycle recurses until Python gives up, and a node shared by many parents
would instead grow the summary exponentially, so both are refused.

**Bounded parameters and finite numbers are not enough on their own.** The parameter maximums do not bound the
work: an array `count` of 300,000 makes 1.8 million faces from a cube, subdivision `levels` 8 nearly 400,000,
and 64 cuts (within the maximum) on a 30 by 30 grid (within its own maximum) make 3.8 million faces. The
estimate is checked before anything is built, because evaluating such a stack can itself take minutes. Finite is
not enough either: `size: 1e39` is finite to Python, but it overflows Blender's 32-bit floats and makes a cube
of NaN vertices, and `scale: 1e39` becomes 3.4e38 with infinite dimensions. A preview of 4096 x 4096 at 4096
samples asks for hours of CPU rendering. And a reader without these rules lets a `ValueError` escape on an
integer literal of 5,000 digits, refuses a primitive without `POSITION` that glTF allows, and counts strips and
fans as no triangles.
