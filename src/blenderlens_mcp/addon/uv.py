"""Unwrap UVs through Blender's own UV operators, and report what a UV map holds.

The UV operators work only in edit mode, on the selected faces of the active object. So the object is
made active, its faces are selected, and it is switched into edit mode for the call and back to object
mode after, under a context override. The active object, the selection, the mesh's own selection flags
and the UV select sync setting are put back whatever happens, so a user working in a Blender window
running the add-on finds everything as it was. The call is refused before anything changes when this
object, or the active object, is already in edit mode or another mode, since switching modes would take
the user out of their edit.

Four traits of the operators are handled here:

- Cylinder and sphere projection follow the 3D view unless told to follow the object, and a background
  Blender has no view, so they are told to (ALIGN_TO_OBJECT).
- Packing islands with the default shape method (CONCAVE) takes seconds on a bevelled cube, and AABB a
  millisecond, so packing uses AABB.
- Unwrap on a closed region without seams finishes without an error, with every face its own island, so
  such a region is refused before anything runs, with a hint to mark seams.
- The operators' selection flags differ between Blender 4.2 and 5.0, so the result is measured from the
  mesh data alone, not from what the operators selected.

The operators act only on objects the view layer shows, so an object that is hidden, itself or through
its collections, is shown for the call and hidden again after.
"""

import itertools
import math

import bmesh
import bpy
from mathutils import Vector
from mathutils.geometry import area_tri, intersect_tri_tri_2d

from .common import (
    CommandError,
    Rollback,
    get_mesh_object,
    number,
    refuse_outside_object_mode,
    require,
    rnd,
    text,
    update,
)

#: The unwrap methods unwrap_uv takes.
METHODS = ("smart", "cube", "cylinder", "sphere", "unwrap", "lightmap")
#: The largest margin between islands, as a share of the UV square.
MAX_MARGIN = 0.1
#: The largest margin the lightmap method takes. Blender's Lightmap Pack reads its margin in hundredths of the
#: UV square, from 0.001 to at most 1, so 0.01 of the square is the most it can leave.
MAX_LIGHTMAP_MARGIN = 0.01
#: The texture width, in pixels, that texel density is reported for: the density is in pixels per metre
#: on a texture of this size.
TEXTURE_SIZE = 1024
#: Smart projection's angle limit, Blender's own default.
SMART_ANGLE = math.radians(66.0)
# The next two limit the work of counting overlapping faces, for check_asset and unwrap_uv alike. Past
# either limit, overlapping() returns the overlaps found so far and says the count is not complete.
#: The most entries the grid of triangles may hold.
MAX_GRID_ENTRIES = 4_000_000
#: The most triangle pairs met in the grid's cells, counting a pair skipped as already tested too.
MAX_PAIR_TESTS = 4_000_000
#: The hint for an object that the view layer does not show, so its UVs cannot be unwrapped.
SHOWN_HINT = "Call set_collection to put it in a collection that this scene shows."
#: A triangle covering more grid cells than this is tested against every other triangle instead.
MAX_CELLS_PER_TRIANGLE = 256
#: How many triangles' bounding boxes are sampled to estimate the work of each candidate grid cell size.
GRID_SAMPLE = 4000
#: The share of triangles at each side of the layout left out when the grid is sized, so a few UVs far
#: outside the square do not stretch every cell over the whole layout.
OUTLYING = 0.01
#: The share by which UV triangles are shrunk toward their centre before testing, so neighbours that only
#: share an edge do not count as overlapping. With it, the test finds the faces Blender's own Select
#: Overlap selects.
SHRINK = 1e-3


def unwrap_uv(params):
    """Unwrap a mesh's UVs with one of the METHODS, and return what the UV map holds."""
    obj = get_mesh_object(require(params, "object"))
    method = text(params, "method", "smart")
    if method not in METHODS:
        raise CommandError("bad_argument", f"Unknown method '{method}'.",
                           "The methods are: " + ", ".join(METHODS) + ".")
    margin = number(params, "margin", 0.01, 0, MAX_MARGIN)
    if method == "lightmap" and margin > MAX_LIGHTMAP_MARGIN:
        raise CommandError("bad_argument", f"'margin' must be at most {MAX_LIGHTMAP_MARGIN} for method lightmap, "
                                           f"got {margin}.",
                           "Blender's Lightmap Pack leaves a margin of at most 0.01 of the UV square. The other "
                           f"methods take up to {MAX_MARGIN}.")
    wanted = text(params, "uv_map")
    refuse_outside_object_mode([obj], "UVs cannot be unwrapped")
    update()
    if bpy.context.view_layer.objects.get(obj.name) is None:
        raise CommandError("bad_argument", f"'{obj.name}' is not in this view layer, so it cannot enter edit mode "
                                           "to be unwrapped.", SHOWN_HINT)
    mesh = obj.data
    if not mesh.polygons:
        raise CommandError("not_found", f"'{obj.name}' has no faces to unwrap.",
                           "unwrap_uv lays out faces, so give the mesh faces first.")
    if method == "unwrap":
        closed = _closed_regions(mesh)
        if closed:
            raise CommandError("bad_argument", f"'{obj.name}' has {closed} closed region{'s' if closed > 1 else ''} "
                                               "without seams, which unwrap cannot lay flat. Blender would make "
                                               "every face its own island.",
                               "Mark seams first with edit_mesh's mark_seams operation (its default angle of 30 "
                               "degrees cuts a box into its six sides), or use method smart, which needs no seams.")

    layers = mesh.uv_layers
    name = wanted or (layers.active.name if layers.active is not None else "UVMap")
    created = layers.get(name) is None
    previous = layers.active.name if layers.active is not None else None
    with Rollback() as undo:
        if created:
            layers.new(name=name)
            undo.add(lambda: layers.remove(layers[name]))
        else:
            kept = [0.0] * (2 * len(layers[name].data))
            layers[name].data.foreach_get("uv", kept)
            undo.add(lambda: layers[name].data.foreach_set("uv", kept))
        # The operators write into the active UV map, so the target map is made active for the call, and
        # the map that was active before is made active again afterwards.
        layers.active = layers[name]
        if previous is not None:
            undo.add(lambda: setattr(layers, "active", layers[previous]))
        _in_edit_mode(obj, lambda: _run(method, margin))
        if previous is not None and previous != name:
            layers.active = layers[previous]
    mesh.update()
    out = {"object": obj.name, "method": method, "uv_map": name, "created": created,
           "uv_maps": [layer.name for layer in layers], "active": layers.active.name, "faces": len(mesh.polygons),
           **uv_report(obj, name)}
    if mesh.users > 1:
        out["mesh_users"] = mesh.users
        out["note"] = f"The mesh is shared by {mesh.users} objects, and every one of them has these UVs."
    return out


def _closed_regions(mesh):
    """Return how many regions of faces, joined across edges that are not seams, have no border at all.

    An edge that is a seam, or that is not shared by exactly two faces, borders a region. A closed
    region, such as a sphere or a box with no seams, cannot be laid flat without being cut somewhere.
    """
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        seen = set()
        closed = 0
        for start in bm.faces:
            if start in seen:
                continue
            seen.add(start)
            stack, bordered = [start], False
            while stack:
                face = stack.pop()
                for edge in face.edges:
                    if edge.seam or len(edge.link_faces) != 2:
                        bordered = True
                        continue
                    for other in edge.link_faces:
                        if other not in seen:
                            seen.add(other)
                            stack.append(other)
            closed += not bordered
        return closed
    finally:
        bm.free()


def _run(method, margin):
    """Run one unwrap method on the selected faces of the object in edit mode.

    Raise a CommandError when an operator reports an error or does not finish.
    """
    ops = bpy.ops.uv
    if method == "smart":
        steps = [("smart_project", {"angle_limit": SMART_ANGLE, "island_margin": margin})]
    elif method == "unwrap":
        steps = [("unwrap", {"method": "ANGLE_BASED", "margin": margin})]
    elif method == "lightmap":
        # Lightmap Pack takes its margin in hundredths of the UV square, from 0.001 to 1. unwrap_uv refuses a
        # margin past 0.01, and a margin below 0.00001, the operator's least, is raised to it.
        steps = [("lightmap_pack", {"PREF_CONTEXT": "SEL_FACES", "PREF_PACK_IN_ONE": True, "PREF_NEW_UVLAYER": False,
                                    "PREF_MARGIN_DIV": max(0.001, min(1.0, margin * 100))})]
    else:
        projection = {"cube": ("cube_project", {"scale_to_bounds": False}),
                      "cylinder": ("cylinder_project", {"direction": "ALIGN_TO_OBJECT", "scale_to_bounds": False}),
                      "sphere": ("sphere_project", {"direction": "ALIGN_TO_OBJECT", "scale_to_bounds": False})}
        # A projection leaves its islands where it put them, overlapping or outside the UV square, so they
        # are packed afterwards. Packing uses the AABB shape method because CONCAVE takes seconds on a
        # bevelled cube.
        steps = [projection[method], ("pack_islands", {"margin": margin, "shape_method": "AABB"})]
    for operator, settings in steps:
        try:
            result = getattr(ops, operator)(**settings)
        except RuntimeError as exc:
            said = str(exc).strip().removeprefix("Error:").strip()
            raise CommandError("blender_error", f"Blender's {operator} did not run: {said}") from None
        if "FINISHED" not in result:
            raise CommandError("blender_error", f"Blender's {operator} did not finish ({sorted(result)}).")


def _flags(items, key):
    """Return a true-or-false property of every item in a mesh collection, such as each vertex's select flag."""
    values = [False] * len(items)
    items.foreach_get(key, values)
    return values


def _layer_path(root, collection):
    """Return the layer collections from ``root`` down to the one for ``collection``, or [] when there is none."""
    if root.collection == collection:
        return [root]
    for child in root.children:
        found = _layer_path(child, collection)
        if found:
            return [root, *found]
    return []


def _hiding_collections(obj, layer):
    """Return the layer collections and collections that hide ``obj`` in the viewport.

    Each is returned as an (owner, "hide_viewport") pair, ready to be set and put back.
    """
    out = []
    for collection in obj.users_collection:
        for layer_collection in _layer_path(layer.layer_collection, collection):
            for owner in (layer_collection, layer_collection.collection):
                if owner.hide_viewport and (owner, "hide_viewport") not in out:
                    out.append((owner, "hide_viewport"))
    return out


def _in_edit_mode(obj, work):
    """Run ``work`` with ``obj`` active, shown and in edit mode, with every face of it selected and shown.

    Afterwards, whatever happens, the object returns to object mode, and the active object, the
    object's selection and visibility (its own and its collections'), the mesh's selection and hidden
    flags, and UV select sync are put back. Raise a CommandError when the object cannot be shown or
    does not enter edit mode. A setting that cannot be put back is counted in ``Rollback.failures``.
    """
    mesh = obj.data
    layer = bpy.context.view_layer
    tool = bpy.context.scene.tool_settings
    kept = {(kind, key): _flags(getattr(mesh, kind), key) for kind in ("vertices", "edges", "polygons")
            for key in ("select", "hide")}
    active, selected, sync = layer.objects.active, obj.select_get(), tool.use_uv_select_sync
    hidden, disabled = obj.hide_get(), obj.hide_viewport
    hiding = _hiding_collections(obj, layer)
    try:
        for kind in ("vertices", "edges", "polygons"):
            items = getattr(mesh, kind)
            items.foreach_set("select", [True] * len(items))
            items.foreach_set("hide", [False] * len(items))
        # The UV operators act only on objects the view layer shows. On a hidden object, or one in a hidden
        # collection, they finish without doing anything.
        if hidden:
            obj.hide_set(False)
        if disabled:
            obj.hide_viewport = False
        for owner, key in hiding:
            setattr(owner, key, False)
        if hiding:
            layer.update()
        if not obj.visible_get():
            raise CommandError("bad_argument", f"'{obj.name}' is not shown in this view layer, so its UVs cannot be "
                                               "unwrapped.", SHOWN_HINT)
        layer.objects.active = obj
        obj.select_set(True)
        # With UV select sync on, the UV operators act on the mesh's own selection, the same way in Blender
        # 4.2 and 5.0.
        tool.use_uv_select_sync = True
        with bpy.context.temp_override(object=obj, active_object=obj, edit_object=obj, selected_objects=[obj],
                                       selected_editable_objects=[obj]):
            try:
                bpy.ops.object.mode_set(mode="EDIT")
            except RuntimeError as exc:
                said = str(exc).strip().removeprefix("Error:").strip()
                raise CommandError("blender_error", f"Blender did not take '{obj.name}' into edit mode: {said}") \
                    from None
            try:
                if obj.mode != "EDIT":
                    raise CommandError("blender_error", f"Blender did not take '{obj.name}' into edit mode, so its UVs "
                                                        "could not be unwrapped.",
                                       "An object in a hidden or excluded collection cannot enter edit mode; "
                                       "get_scene shows the collections.")
                work()
            finally:
                if obj.mode != "OBJECT":
                    bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        tool.use_uv_select_sync = sync
        for (kind, key), values in kept.items():
            getattr(mesh, kind).foreach_set(key, values)
        try:
            if disabled:
                obj.hide_viewport = True
            if hidden:
                obj.hide_set(True)
            obj.select_set(selected)
            layer.objects.active = active
            for owner, key in hiding:
                setattr(owner, key, True)
            if hiding:
                layer.update()
        except (RuntimeError, ReferenceError, ValueError):
            Rollback.failures += 1


# ---------------------------------------------------------------------------
# What a UV map holds
# ---------------------------------------------------------------------------


def _islands(bm, layer):
    """Return how many UV islands the faces of ``bm`` form in the UV layer ``layer``.

    Two faces are in the same island when they share an edge whose two ends have the same UVs on both
    sides.
    """
    parent = {face: face for face in bm.faces}

    def root(face):
        """Return the face that stands for the island ``face`` is in."""
        while parent[face] is not face:
            parent[face] = parent[parent[face]]
            face = parent[face]
        return face

    def corner(face, vert):
        """Return the UV of ``vert``'s corner in ``face``."""
        return next(loop[layer].uv for loop in face.loops if loop.vert == vert)

    for edge in bm.edges:
        for a, b in itertools.pairwise(edge.link_faces):
            if all((corner(a, v) - corner(b, v)).length < 1e-5 for v in edge.verts):
                parent[root(a)] = root(b)
    return len({root(face) for face in bm.faces})


def _cells(box, left, bottom, cell):
    """Return the (first column, first row, last column, last row) of the grid cells that ``box`` covers."""
    return (math.floor((box[0] - left) / cell), math.floor((box[1] - bottom) / cell),
            math.floor((box[2] - left) / cell), math.floor((box[3] - bottom) / cell))


def _grid_size(boxes):
    """Return (left, bottom, cell) for a grid over the UV boxes, with the cell size that makes the least work.

    Two cell sizes are candidates, each also halved and doubled: one that puts about one triangle in a
    cell over the bulk of the layout, and the size of a typical triangle. The bulk is the extent of the
    boxes with the outer OUTLYING share left out at each side, so a few UVs far outside the square land
    in cells of their own instead of stretching every cell. With the typical size, a stack of faces
    over the whole square fills a few cells of a triangle's size, not thousands. Each candidate's work
    is its cell entries, plus the pairs its cells hold, plus a test against every other triangle for each
    triangle too large for the grid, estimated on a sample of about GRID_SAMPLE boxes before any pair is
    tested.
    """
    count = len(boxes)
    cut = int(count * OUTLYING)
    left = sorted(box[0] for box in boxes)[cut]
    bottom = sorted(box[1] for box in boxes)[cut]
    right = sorted(box[2] for box in boxes)[count - 1 - cut]
    top = sorted(box[3] for box in boxes)[count - 1 - cut]
    typical = sorted(max(box[2] - box[0], box[3] - box[1]) for box in boxes)[count // 2]
    span = max(right - left, top - bottom)
    bulk = span / max(1, min(1024, int(math.sqrt(count))))
    # The work is estimated on an evenly spaced sample of the boxes. Entries grow in proportion to the
    # share sampled, and pairs with its square, so each count is scaled up by that much.
    sample = boxes[::max(1, count // GRID_SAMPLE)]
    share = len(sample) / count
    best = None
    for cell in dict.fromkeys(size * factor for factor in (1.0, 2.0, 0.5) for size in (typical, bulk) if size > 1e-9):
        entries, large, members = 0, 0, {}
        for box in sample:
            x0, y0, x1, y1 = _cells(box, left, bottom, cell)
            covered = (x1 - x0 + 1) * (y1 - y0 + 1)
            if covered > MAX_CELLS_PER_TRIANGLE:
                large += 1
                continue
            entries += covered
            if best is not None and entries / share > best[0]:
                break
            for gx in range(x0, x1 + 1):
                for gy in range(y0, y1 + 1):
                    members[gx, gy] = members.get((gx, gy), 0) + 1
        else:
            pairs = sum(m * (m - 1) // 2 for m in members.values())
            work = entries / share + pairs / share ** 2 + large / share * count
            if best is None or work < best[0]:
                best = (work, cell)
    return left, bottom, best[1] if best is not None else max(bulk, typical, 1e-9)


def overlapping(triangles):
    """Return the faces whose UV triangles overlap another face's, as (set of face indices, complete).

    ``triangles`` holds (face index, three 2D points) tuples. Each triangle goes into the grid cells its
    bounding box covers, and each pair sharing a cell is tested once, in the first cell both cover. A
    triangle covering more than MAX_CELLS_PER_TRIANGLE cells is tested against every other triangle
    instead. Every pair met counts toward MAX_PAIR_TESTS, and every cell entry toward MAX_GRID_ENTRIES.
    Past either limit, the faces found so far are returned with ``complete`` false.
    """
    budget, room = MAX_PAIR_TESTS, MAX_GRID_ENTRIES
    if len(triangles) < 2:
        return set(), True
    boxes = []
    for _, tri in triangles:
        xs, ys = (tri[0].x, tri[1].x, tri[2].x), (tri[0].y, tri[1].y, tri[2].y)
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    left, bottom, cell = _grid_size(boxes)
    grid, spans, large = {}, [], []
    for i, box in enumerate(boxes):
        gx0, gy0, gx1, gy1 = _cells(box, left, bottom, cell)
        spans.append((gx0, gy0))
        cells = (gx1 - gx0 + 1) * (gy1 - gy0 + 1)
        if cells > MAX_CELLS_PER_TRIANGLE:
            large.append(i)
            continue
        room -= cells
        if room < 0:
            return set(), False
        for gx in range(gx0, gx1 + 1):
            for gy in range(gy0, gy1 + 1):
                grid.setdefault((gx, gy), []).append(i)
    found, work = set(), 0

    def test(i, j):
        """Test triangles ``i`` and ``j``, and record both faces when they overlap."""
        face_i, face_j = triangles[i][0], triangles[j][0]
        if face_i != face_j and not (face_i in found and face_j in found) \
                and intersect_tri_tri_2d(*triangles[i][1], *triangles[j][1]):
            found.add(face_i)
            found.add(face_j)

    tested_whole = set()
    for i in large:
        tested_whole.add(i)
        for j in range(len(triangles)):
            if j in tested_whole:
                continue
            work += 1
            if work > budget:
                return found, False
            test(i, j)
    for key, members in grid.items():
        for a in range(len(members)):
            i = members[a]
            for b in range(a + 1, len(members)):
                work += 1
                if work > budget:
                    return found, False
                j = members[b]
                if (max(spans[i][0], spans[j][0]), max(spans[i][1], spans[j][1])) == key:
                    test(i, j)
    return found, True


def uv_report(obj, name):
    """Return what the UV map ``name`` of ``obj`` holds: islands, area used, overlapping and out-of-bounds faces.

    Texel density is in pixels per metre on a TEXTURE_SIZE texture, measured against the faces' world
    space area, and is None for a mesh with no area. ``overlap_note`` is added when the overlap count
    stopped at its work limits.
    """
    mesh = obj.data
    data = mesh.uv_layers[name].data
    mesh.calc_loop_triangles()
    world = obj.matrix_world
    triangles, uv_area, world_area = [], 0.0, 0.0
    for tri in mesh.loop_triangles:
        points = [Vector(data[i].uv) for i in tri.loops]
        area = area_tri(*points)
        uv_area += area
        world_area += area_tri(*(world @ mesh.vertices[v].co for v in tri.vertices))
        if area > 1e-12:
            middle = (points[0] + points[1] + points[2]) / 3
            triangles.append((tri.polygon_index, [middle + (p - middle) * (1 - SHRINK) for p in points]))
    outside = {poly.index for poly in mesh.polygons
               if any(not (-1e-6 <= c <= 1 + 1e-6) for i in poly.loop_indices for c in data[i].uv)}
    found, complete = overlapping(triangles)
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        islands = _islands(bm, bm.loops.layers.uv[name])
    finally:
        bm.free()
    out = {"islands": islands, "area_used": rnd(uv_area, 4), "overlapping_faces": len(found),
           "out_of_bounds_faces": len(outside),
           "texel_density": rnd(math.sqrt(uv_area / world_area) * TEXTURE_SIZE, 1) if world_area > 0 else None}
    if not complete:
        out["overlap_note"] = "Too many faces share the same UV space to test every pair, so overlapping_faces " \
                              "counts those found."
    return out
