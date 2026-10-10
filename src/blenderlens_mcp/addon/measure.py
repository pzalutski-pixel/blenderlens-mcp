"""Measure between objects and place them, for measure_objects, set_transform's drop and apply_transform's origin.

Every measurement is in world space and after modifiers, as the glTF export writes the object. Curves,
surfaces, metaballs and text are measured by the mesh they make in Blender, though the export writes a
metaball as a node with no mesh. A BVH tree built by ``BVHTree.FromObject`` is in the object's own space,
so the trees here are built instead from the evaluated mesh's vertices moved by ``matrix_world``. Face
indices refer to the evaluated mesh's polygons. These are the indices get_mesh lists with ``evaluated``
true, and the indices edit_mesh takes when the modifiers leave the faces as they are.
"""

import bpy
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree
from mathutils.geometry import tessellate_polygon

from .common import (
    CommandError,
    article,
    descendants,
    direction,
    get_object,
    object_kind,
    plural,
    rnd,
    triple,
    update,
    vec,
)

#: The most objects measure_objects takes, since every pair of them is measured.
MAX_OBJECTS = 16
#: The most face indices a measurement lists for each object.
FACES_LISTED = 32
#: The most points of the dropped object that a drop casts rays from, and the most points of what it may land
#: on. Past this many, the points are sampled evenly.
MAX_DROP_POINTS = 50_000
#: How many times a ray going down may pass through the object being dropped before the drop gives up on it.
MAX_PASSES = 256
#: The distance, in metres, by which a ray starts above the object and steps past each surface it passes through.
EPSILON = 1e-5
#: How many of the closest vertex pairs a distance is refined from.
REFINED_STARTS = 8
#: The most steps each refinement of a distance takes.
REFINE_STEPS = 32
#: Object types with a surface, each measured as the mesh it makes in Blender.
GEOMETRY = ("MESH", "CURVE", "SURFACE", "FONT", "META")
#: The types of GEOMETRY that the glTF export writes as a mesh. It converts curves, surfaces and text to
#: meshes, but writes a metaball as a node with no mesh.
EXPORTED_GEOMETRY = ("MESH", "CURVE", "SURFACE", "FONT")
#: The directions of the rays that test whether a point lies inside a closed surface, by whether each ray
#: crosses it an odd number of times. None lies along an axis or a diagonal, so a ray seldom runs exactly
#: along an edge or through a vertex.
RAYS = (Vector((0.267, 0.535, 0.802)).normalized(), Vector((-0.601, 0.311, -0.736)).normalized(),
        Vector((0.812, -0.497, 0.306)).normalized())
#: The most surfaces a single ray counts.
MAX_CROSSINGS = 4096
#: The distance, in metres, within which surfaces count as touching rather than crossing: faces lying on each
#: other, an edge or a corner resting on a face, and a surface sunk into another by no more than this. A drop
#: leaves an object resting within this distance, because of float precision.
TOUCH_DISTANCE = 1e-5
#: Two faces count as parallel when the absolute cosine between their normals is at least this. Parallel faces
#: within TOUCH_DISTANCE, or within the objects' float precision if that is coarser, lie on each other.
COPLANAR_ALIGNMENT = 0.9999
#: The most face pairs a crossing test examines to tell touching from crossing. Past this many, the remaining
#: pairs count as crossing.
MAX_CROSSING_TESTS = 100_000


# ---------------------------------------------------------------------------
# World-space geometry
# ---------------------------------------------------------------------------


def world_points(obj, depsgraph):
    """Return the evaluated mesh's vertices in world space, and its polygons as tuples of vertex indices.

    An object with no surface, or one that yields no mesh, gives ``([], [])``.
    """
    if obj.type not in GEOMETRY:
        return [], []
    holder = obj.evaluated_get(depsgraph)
    try:
        mesh = holder.to_mesh()
    except RuntimeError:
        return [], []
    if mesh is None:
        return [], []
    try:
        matrix = holder.matrix_world
        return [matrix @ v.co for v in mesh.vertices], [tuple(p.vertices) for p in mesh.polygons]
    finally:
        holder.to_mesh_clear()


def world_tree(obj, depsgraph):
    """Return ``(tree, points)``, a BVH tree of the object's evaluated surface and its vertices, in world space.

    The tree is None for an object with no faces.
    """
    points, polygons = world_points(obj, depsgraph)
    if not polygons:
        return None, points
    return BVHTree.FromPolygons(points, polygons, epsilon=0.0), points


def closed(polygons):
    """Return whether the polygons form a closed surface, every edge shared by exactly two of them."""
    edges = {}
    for polygon in polygons:
        for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=True):
            key = (a, b) if a < b else (b, a)
            edges[key] = edges.get(key, 0) + 1
    return bool(edges) and all(count == 2 for count in edges.values())


def step_for(points):
    """Return a step past a surface that is small next to the geometry's size but large next to its float precision."""
    if not points:
        return EPSILON
    reach = max(max(abs(c) for c in p) for p in points)
    return max(reach * 1e-6, 1e-9)


def crossings(tree, start, way, step):
    """Return the indices of the faces that a ray from ``start`` along ``way`` passes through, in order.

    After each hit the ray starts again ``step`` past it, so each surface it crosses is counted once. At most
    MAX_CROSSINGS faces are returned.
    """
    out, origin = [], start
    for _ in range(MAX_CROSSINGS):
        location, _, index, _ = tree.ray_cast(origin, way)
        if location is None:
            break
        out.append(index)
        origin = location + way * step
    return out


def inside(tree, point, step):
    """Return whether ``point`` lies inside the closed surface that ``tree`` holds.

    The point is inside when at least two of the three rays in RAYS cross the surface an odd number of times.
    """
    return sum(len(crossings(tree, point, way, step)) % 2 for way in RAYS) >= 2


def box_within(inner, outer, slack=1e-6):
    """Return whether the box ``inner``, given as ``(lo, hi)``, lies within ``outer``, give or take ``slack``."""
    return all(outer[0][i] - slack <= inner[0][i] and inner[1][i] <= outer[1][i] + slack for i in range(3))


def _triangles(points, polygon):
    """Return a polygon's triangles as lists of corner points.

    A triangle is returned as it is, a quad is split along its first diagonal, and a larger polygon is
    triangulated with ``tessellate_polygon``.
    """
    corners = [points[i] for i in polygon]
    if len(corners) == 3:
        return [corners]
    if len(corners) == 4:
        return [corners[:3], [corners[0], corners[2], corners[3]]]
    return [[corners[a], corners[b], corners[c]] for a, b, c in tessellate_polygon([corners])]


def _sides(triangle, other, tolerance):
    """Return the signed distances of ``triangle``'s corners from the plane of ``other``, and that plane's normal.

    Return None unless the triangle reaches more than ``tolerance`` beyond the plane on both sides, and also
    when ``other`` is degenerate and has no plane.
    """
    normal = (other[1] - other[0]).cross(other[2] - other[0])
    if normal.length == 0:
        return None
    normal.normalize()
    distances = [normal.dot(p - other[0]) for p in triangle]
    if max(distances) <= tolerance or min(distances) >= -tolerance:
        return None
    return distances, normal


def _span(triangle, distances, line):
    """Return where ``triangle`` meets the other triangle's plane, as an interval ``(low, high)`` along ``line``.

    ``distances`` are the corners' signed distances from that plane. Return None when no edge reaches the plane.
    """
    out = []
    for k in range(3):
        p, q = triangle[k], triangle[(k + 1) % 3]
        dp, dq = distances[k], distances[(k + 1) % 3]
        if (dp <= 0 <= dq or dq <= 0 <= dp) and dp != dq:
            out.append(line.dot(p + (q - p) * (dp / (dp - dq))))
    return (min(out), max(out)) if out else None


def triangles_cross(first, second, tolerance=TOUCH_DISTANCE):
    """Return whether two triangles pass through each other.

    They do when each reaches more than ``tolerance`` beyond the other's plane on both sides, and the stretches
    where they cut the line the two planes share overlap by more than ``tolerance``. Triangles lying on each
    other, meeting at an edge or a corner, or sunk into each other no deeper than ``tolerance`` only touch.
    """
    a = _sides(first, second, tolerance)
    b = _sides(second, first, tolerance) if a is not None else None
    if b is None:
        return False
    line = b[1].cross(a[1])
    if line.length == 0:
        return False
    line.normalize()
    span_a, span_b = _span(first, a[0], line), _span(second, b[0], line)
    if span_a is None or span_b is None:
        return False
    return min(span_a[1], span_b[1]) - max(span_a[0], span_b[0]) > tolerance


def crossing_faces(hits, points_a, polygons_a, points_b, polygons_b, first=False):
    """Return the face pairs among ``hits`` whose faces cross, and whether every pair was tested.

    ``hits`` holds the ``(face of a, face of b)`` pairs that ``BVHTree.overlap`` reports. That method reports
    triangles that meet at all, so a crate resting on another reports the faces along which they meet. Only
    faces that pass through each other, by ``triangles_cross``, count as crossing, and the rest only touch.
    With ``first``, the search stops at the first crossing. Past MAX_CROSSING_TESTS pairs, the remaining pairs
    count as crossing without a test, and the second value is False.
    """
    out, cache = [], {}

    def triangles(points, polygons, index, side):
        """Return the triangles of face ``index`` of one side, worked out once and then kept."""
        key = (side, index)
        if key not in cache:
            cache[key] = _triangles(points, polygons[index])
        return cache[key]

    pairs = list(dict.fromkeys(hits))
    for tested, (i, j) in enumerate(pairs):
        if tested >= MAX_CROSSING_TESTS:
            out.extend(pairs[tested:])
            return out, False
        mine, theirs = triangles(points_a, polygons_a, i, 0), triangles(points_b, polygons_b, j, 1)
        if any(triangles_cross(s, t) for s in mine for t in theirs):
            out.append((i, j))
            if first:
                break
    return out, True


class Shape:
    """An object's evaluated surface in world space, with its BVH tree, points, polygons and bounding box.

    Whether the surface is closed is worked out the first time ``closed`` is read, and then kept.
    """

    def __init__(self, points, polygons):
        """Build the shape from world-space points and polygons given as tuples of point indices."""
        self.points, self.polygons = points, polygons
        self.tree = BVHTree.FromPolygons(points, polygons, epsilon=0.0) if polygons else None
        self.box = ([min(p[i] for p in points) for i in range(3)],
                    [max(p[i] for p in points) for i in range(3)]) if points else None
        self.step = step_for(points)
        self._closed = None

    @property
    def closed(self):
        """Return whether the surface is closed, every edge shared by exactly two faces."""
        if self._closed is None:
            self._closed = closed(self.polygons)
        return self._closed


def _faces_of(obj, depsgraph):
    """Return "mesh" when the evaluated faces are the mesh's own, whose indices edit_mesh takes, else "evaluated".

    The faces count as the mesh's own when the evaluated mesh has as many polygons and vertices as the mesh data.
    """
    if obj.type != "MESH":
        return "evaluated"
    holder = obj.evaluated_get(depsgraph)
    mesh = holder.to_mesh()
    try:
        same = len(mesh.polygons) == len(obj.data.polygons) and len(mesh.vertices) == len(obj.data.vertices)
    finally:
        holder.to_mesh_clear()
    return "mesh" if same else "evaluated"


# ---------------------------------------------------------------------------
# measure_objects
# ---------------------------------------------------------------------------


def _closest(tree_a, points_a, tree_b, points_b):
    """Return the shortest distance between two surfaces and the two points it joins, as ``(distance, a, b)``.

    Each vertex is first measured to the nearest point on the other surface. That misses an edge passing close
    to a face, such as a rotated cube's edge beside a flat side, so the REFINED_STARTS best starting pairs are
    then refined by stepping to the nearest point on each surface in turn until the distance stops shrinking.
    Surfaces with no nearest points at all give ``(inf, None, None)``.
    """
    starts = []
    for point in points_a:
        found, _, _, distance = tree_b.find_nearest(point)
        if found is not None:
            starts.append((distance, point, found))
    for point in points_b:
        found, _, _, distance = tree_a.find_nearest(point)
        if found is not None:
            starts.append((distance, found, point))
    if not starts:
        return float("inf"), None, None
    starts.sort(key=lambda start: start[0])
    best = starts[0]
    for distance, near_a, near_b in starts[:REFINED_STARTS]:
        for _ in range(REFINE_STEPS):
            found_a = tree_a.find_nearest(near_b)[0]
            found_b = tree_b.find_nearest(found_a)[0] if found_a is not None else None
            if found_b is None:
                break
            step = (found_a - found_b).length
            if step >= distance - 1e-9:
                break
            distance, near_a, near_b = step, found_a, found_b
        if distance < best[0]:
            best = (distance, near_a, near_b)
    return best


def inner_of(a, b):
    """Return which of two shapes lies wholly inside the other: "a", "b", or None for neither.

    One point of the inner shape is tested with ``inside``. Only a closed surface has an inside, and a shape
    can lie inside another only when its bounding box lies within the other's.
    """
    if a.box is None or b.box is None:
        return None
    if b.points and a.closed and box_within(b.box, a.box) and inside(a.tree, b.points[0], a.step):
        return "b"
    if a.points and b.closed and box_within(a.box, b.box) and inside(b.tree, a.points[0], b.step):
        return "a"
    return None


def _coplanar(shape, other, tolerance):
    """Return how many faces of ``shape`` lie on a face of ``other``.

    A face counts when its centre is within ``tolerance`` of ``other`` and the nearest face there is parallel
    to it, facing the same way or the opposite way.
    """
    count = 0
    for polygon in shape.polygons:
        corners = [shape.points[i] for i in polygon]
        middle = sum(corners, Vector((0.0, 0.0, 0.0))) / len(corners)
        normal = (corners[1] - corners[0]).cross(corners[-1] - corners[0])
        if normal.length == 0:
            continue
        found, other_normal, _, distance = other.tree.find_nearest(middle)
        if found is not None and distance <= tolerance and \
                abs(normal.normalized().dot(other_normal)) >= COPLANAR_ALIGNMENT:
            count += 1
    return count


def _shape(obj, depsgraph, cache):
    """Return the Shape of ``obj``, made once per call and kept in ``cache``."""
    if obj.name not in cache:
        cache[obj.name] = Shape(*world_points(obj, depsgraph))
    return cache[obj.name]


def _pair(a, b, depsgraph, cache):
    """Return how two objects meet: their distance, whether they overlap or touch, and which faces."""
    shape_a, shape_b = _shape(a, depsgraph, cache), _shape(b, depsgraph, cache)
    if shape_a.tree is None or shape_b.tree is None:
        raise CommandError("bad_argument", f"'{(a if shape_a.tree is None else b).name}' has no faces to measure.",
                           "measure_objects measures meshes, and curves, surfaces, metaballs and text by the surface "
                           "they make.")
    tree_a, points_a, tree_b, points_b = shape_a.tree, shape_a.points, shape_b.tree, shape_b.points
    out = {"a": a.name, "b": b.name}
    hits = tree_a.overlap(tree_b)
    crossing = crossing_faces(hits, points_a, shape_a.polygons, points_b, shape_b.polygons)[0] if hits else []
    if crossing:
        faces_a = sorted({i for i, _ in crossing})
        faces_b = sorted({j for _, j in crossing})
        out.update({"distance": 0.0, "overlapping": True,
                    "overlap_faces": {"a": len(faces_a), "b": len(faces_b)},
                    "faces": {"a": faces_a[:FACES_LISTED], "b": faces_b[:FACES_LISTED]}})
        # Where the surfaces cross, any shared point is a closest point. A pair of points is still returned, to
        # show roughly where the objects meet.
        distance, near_a, near_b = _closest(tree_a, points_a, tree_b, points_b)
        out["closest"] = {"a": vec(near_a), "b": vec(near_b)} if near_a is not None else None
        return out
    distance, near_a, near_b = _closest(tree_a, points_a, tree_b, points_b)
    out.update({"distance": rnd(distance, 6), "overlapping": False,
                "closest": {"a": vec(near_a), "b": vec(near_b)}})
    inner = inner_of(shape_a, shape_b)
    if inner is not None:
        out["inside"] = (a if inner == "a" else b).name
    if hits or distance <= TOUCH_DISTANCE:
        out["touching"] = True
        tolerance = max(shape_a.step, shape_b.step, TOUCH_DISTANCE)
        lying = {"a": _coplanar(shape_a, shape_b, tolerance), "b": _coplanar(shape_b, shape_a, tolerance)}
        if lying["a"] or lying["b"]:
            out["coplanar_faces"] = lying
    return out


def _ray(origin, way, objects, depsgraph, cache):
    """Return the first surface a ray meets, among ``objects`` when given, or else anything visible in the scene."""
    best = None
    if objects:
        for obj in objects:
            tree = _shape(obj, depsgraph, cache).tree
            if tree is None:
                continue
            location, normal, index, distance = tree.ray_cast(origin, way)
            if location is not None and (best is None or distance < best[3]):
                best = (obj, location, normal, distance, index)
    else:
        hit, location, normal, index, obj, _ = bpy.context.scene.ray_cast(depsgraph, origin, way)
        if hit:
            best = (obj, location, normal, (location - origin).length, index)
    if best is None:
        return {"hit": False}
    obj, location, normal, distance, index = best
    original = obj.original if hasattr(obj, "original") else obj
    return {"hit": True, "object": original.name, "location": vec(location), "normal": vec(normal.normalized()),
            "distance": rnd(distance, 6), "face": index, "faces_of": _faces_of(original, depsgraph)}


def measure_objects(params):
    """Measure every pair of the objects named, and cast a ray when an origin and a direction are given."""
    names = params.get("objects")
    if names is not None and (not isinstance(names, list) or not all(isinstance(n, str) for n in names)):
        raise CommandError("bad_argument", "'objects' must be a list of object names.")
    if names is not None and len(names) > MAX_OBJECTS:
        raise CommandError("bad_argument", f"'objects' lists {len(names)} objects, and measure_objects takes at most "
                                           f"{MAX_OBJECTS}.",
                           "Every pair is measured, so the work grows with the square of the count.")
    twice = sorted({n for n in names or [] if names.count(n) > 1})
    if twice:
        raise CommandError("bad_argument", f"'objects' lists {', '.join(twice)} more than once.",
                           "An object measured against itself is always 0 m away; name each object once.")
    origin, way = params.get("origin"), params.get("direction")
    if (origin is None) != (way is None):
        raise CommandError("bad_argument", "A ray needs both 'origin' and 'direction'.")
    ray = None
    if origin is not None:
        ray = (Vector(triple(origin, "origin", uniform=False)), direction(way, "direction"))
    if ray is None and (names is None or len(names) < 2):
        raise CommandError("bad_argument", "Name two or more 'objects' to measure between, or give 'origin' and "
                                           "'direction' for a ray.")
    objects = [get_object(n, "objects") for n in names or []]
    update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    cache = {}
    out = {}
    if ray is None or len(objects) >= 2:
        out["pairs"] = [_pair(a, b, depsgraph, cache)
                        for i, a in enumerate(objects) for b in objects[i + 1:]]
    if ray is not None:
        out["ray"] = _ray(ray[0], ray[1], objects, depsgraph, cache)
    return out


# ---------------------------------------------------------------------------
# Placement: drop and origin
# ---------------------------------------------------------------------------


def drop_target(value, obj):
    """Return what the ``drop`` parameter asks to land on, checked before anything changes.

    The result is False for no drop, None to land on anything below, or the named object. An object cannot
    land on itself or on one of its descendants, which move with it.
    """
    if value is None or value is False:
        return False
    if value is True:
        return None
    if not isinstance(value, str) or not value:
        raise CommandError("bad_argument", f"'drop' takes true or the name of an object to land on, got {value!r}.")
    onto = get_object(value, "drop")
    if onto == obj or onto in descendants(obj):
        raise CommandError("bad_argument", f"'{obj.name}' cannot land on '{onto.name}', which moves with it.",
                           "Name an object outside it, or pass true to land on whatever is below.")
    return onto


def _sampled(points):
    """Return ``points``, or MAX_DROP_POINTS of them spread evenly when there are more."""
    if len(points) <= MAX_DROP_POINTS:
        return points
    step = len(points) / MAX_DROP_POINTS
    return [points[int(i * step)] for i in range(MAX_DROP_POINTS)]


def _visible(obj):
    """Return whether ``obj`` is visible, and False when it is not in this view layer."""
    try:
        return obj.visible_get()
    except RuntimeError:  # The object is not in this view layer.
        return False


def _supports(onto, moving, footprint, top, depsgraph):
    """Return what a drop may land on, with the points of each that lie under the moving objects.

    The candidates are ``onto`` alone when given, or else every visible object with a surface, other than the
    moving objects, whose bounding box reaches under them. Each is returned as ``(object, points)``, the points
    in world space, within ``footprint`` and at or below ``top``. A candidate with no such points is left out.
    """
    candidates = [onto] if onto is not None else [
        o for o in bpy.context.scene.objects if o.type in GEOMETRY and o not in moving and _visible(o)]
    (left, front), (right, back) = footprint
    out = []
    for candidate in candidates:
        if onto is None:
            holder = candidate.evaluated_get(depsgraph)
            corners = [holder.matrix_world @ Vector(corner) for corner in holder.bound_box]
            if min(c.x for c in corners) > right or max(c.x for c in corners) < left or \
                    min(c.y for c in corners) > back or max(c.y for c in corners) < front or \
                    min(c.z for c in corners) > top:
                continue
        points = [p for p in world_points(candidate, depsgraph)[0]
                  if left <= p.x <= right and front <= p.y <= back and p.z <= top]
        if points:
            out.append((candidate, points))
    return out


def drop(obj, onto, undo):
    """Move ``obj``, and with it its children, straight down or up until it rests on what is below it.

    It lands on ``onto`` alone when given, or else on anything visible. Rays go down from just above the
    object at each of its points and pass through the object itself, so a surface the object already sinks
    into is found too, and the object rises onto it. Rays also go up from each point of what it may land on
    that lies under the object, to the object's underside, so a pillar narrower than the object, or the tip of
    a spike, is found between the object's own points. The smallest gap of all is the first contact. The move
    is recorded in ``undo``. Return the name of the object landed on, and the move as ``[x, y, z]``.
    """
    update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    moving = [obj, *descendants(obj)]
    points, polygons = [], []
    for part in moving:
        part_points, part_polygons = world_points(part, depsgraph)
        offset = len(points)
        points += part_points
        polygons += [tuple(i + offset for i in polygon) for polygon in part_polygons]
    surface = BVHTree.FromPolygons(points, polygons, epsilon=0.0) if polygons else None
    if not points:
        points = [obj.matrix_world.translation.copy()]
    top = max(p.z for p in points) + EPSILON
    lowest = min(p.z for p in points) - EPSILON
    footprint = ((min(p.x for p in points), min(p.y for p in points)),
                 (max(p.x for p in points), max(p.y for p in points)))
    down, up = Vector((0.0, 0.0, -1.0)), Vector((0.0, 0.0, 1.0))
    scene = bpy.context.scene
    tree = world_tree(onto, depsgraph)[0] if onto is not None else None
    if onto is not None and tree is None:
        raise CommandError("bad_argument", f"'{onto.name}' has no faces to land on.")
    names = {part.name for part in moving}
    best = None
    for point in _sampled(points):
        start = Vector((point.x, point.y, top))
        if tree is not None:
            location = tree.ray_cast(start, down)[0]
            landed = onto if location is not None else None
        else:
            location = landed = None
            for _ in range(MAX_PASSES):
                hit, found, _, _, hit_obj, _ = scene.ray_cast(depsgraph, start, down)
                if not hit:
                    break
                if hit_obj.original.name not in names:
                    location, landed = found, hit_obj.original
                    break
                start = found + down * EPSILON
        if location is None:
            continue
        gap = point.z - location.z
        if best is None or gap < best[0]:
            best = (gap, landed)
    if surface is not None:
        supports = _supports(onto, moving, footprint, top, depsgraph)
        everything = [(support, p) for support, found in supports for p in found]
        for support, point in _sampled(everything):
            location = surface.ray_cast(Vector((point.x, point.y, lowest)), up)[0]
            if location is None:
                continue
            gap = location.z - point.z
            if best is None or gap < best[0]:
                best = (gap, support)
    if best is None:
        what = f"No part of '{onto.name}'" if onto is not None else "Nothing visible"
        raise CommandError("not_found", f"{what} lies below '{obj.name}' to land on.",
                           "First move it over what it should rest on, with set_transform's location, or name "
                           "another object.")
    gap, landed = best
    move = Matrix.Translation((0.0, 0.0, -gap))
    undo.set(obj, "matrix_world", move @ obj.matrix_world)
    update()
    return landed.name, [0.0, 0.0, rnd(-gap, 6)]


#: The named places apply_transform can put an origin.
ORIGINS = ("bottom_center", "bounds_center", "geometry")

#: Modifiers that work about the object's origin unless given another object to work about, by modifier type.
#: Each maps to the setting that names that object, and to a phrase for what the modifier does about the
#: origin. Moving the origin of an object with such a modifier would move what the modifier makes.
ABOUT_THE_ORIGIN = {
    "MIRROR": ("mirror_object", "mirrors about"),
    "SCREW": ("object", "turns about"),
    "SIMPLE_DEFORM": ("origin", "bends about"),
    "CAST": ("object", "shapes the mesh about"),
    "WAVE": ("start_position_object", "starts its wave at"),
}
#: Parent types by which a child follows its parent's vertices, which stay where they are in the world when
#: the parent's origin moves.
VERTEX_PARENTS = ("VERTEX", "VERTEX_3")


def origin_target(value):
    """Return the ``origin`` parameter, checked: a name from ORIGINS, a world-space point, or None when not given."""
    if value is None:
        return None
    if isinstance(value, str):
        if value not in ORIGINS:
            raise CommandError("bad_argument", f"'origin' takes {', '.join(ORIGINS)} or [x, y, z], got {value!r}.")
        return value
    return Vector(triple(value, "origin", uniform=False))


def _origin_modifier(obj):
    """Return ``(modifier, phrase, fix)`` for the first modifier that works about the object's origin, or None.

    ``phrase`` says what the modifier does about the origin, and ``fix`` how set_modifier can stop it doing
    so. Besides the types in ABOUT_THE_ORIGIN, this covers an Array modifier with an object offset, and a
    Displace modifier whose texture is placed by the object's local coordinates.
    """
    for modifier in obj.modifiers:
        about = ABOUT_THE_ORIGIN.get(modifier.type)
        if about is not None and getattr(modifier, about[0], None) is None:
            return modifier, about[1], f"call set_modifier to name an object in its {about[0]} setting"
        if modifier.type == "ARRAY" and modifier.use_object_offset and modifier.offset_object is not None:
            return (modifier, "measures its offset object from",
                    "call set_modifier to set its use_object_offset to false")
        if modifier.type == "DISPLACE" and modifier.texture is not None and modifier.texture_coords == "LOCAL":
            return (modifier, "places its texture by coordinates measured from",
                    "call set_modifier to set its texture_coords to another space")
    return None


def check_origin(obj, where=None):
    """Refuse an object whose origin cannot be moved, or cannot be put at ``where``, before anything changes.

    The origin cannot move on an object that is not a mesh, has no vertices, shares its mesh with other
    objects, or has a modifier that works about the origin. A named place also needs vertices left after
    the modifiers.
    """
    if obj.type != "MESH":
        raise CommandError("bad_argument", f"'{obj.name}' is {object_kind(obj)}, and only a mesh's origin can be "
                                           "moved.", "Move the whole object with set_transform instead; its children "
                                                     "follow it.")
    if not obj.data.vertices:
        raise CommandError("bad_argument", f"'{obj.name}' has no vertices to put its origin by.")
    if obj.data.users > 1:
        others = plural(obj.data.users - 1, "other object")
        raise CommandError("bad_argument", f"'{obj.name}' shares its mesh with {others}, which would all move.",
                           "Call duplicate_object (linked false) to make a copy with a mesh of its own, then move the "
                           "copy's origin.")
    found = _origin_modifier(obj)
    if found is not None:
        modifier, does, fix = found
        kind = article(f"{modifier.type.replace('_', ' ').title()} modifier")
        raise CommandError("bad_argument", f"'{obj.name}' has {kind} ('{modifier.name}') that {does} the origin, so "
                                           "moving the origin would move what it makes.",
                           f"Call apply_modifier on '{modifier.name}' first, or {fix}.")
    if where is not None and not isinstance(where, Vector):
        update()
        if not world_points(obj, bpy.context.evaluated_depsgraph_get())[0]:
            raise CommandError("bad_argument", f"'{obj.name}' has no vertices to put its origin by: its modifiers "
                                               "leave none.")


def move_origin(obj, where, undo):
    """Put ``obj``'s origin at ``where`` without moving its mesh in the world or its children.

    No operator is used. The mesh, shape keys included, moves by the offset one way and the object moves the
    other way, and each child's parent inverse takes the offset back. This leaves the mesh where it was, and
    each child within 1e-7 m of where it was. A child parented to vertices follows those vertices, which stay
    where they are, so it is left alone. Every change is recorded in ``undo``. Return the new origin in world
    space.
    """
    update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    if isinstance(where, Vector):
        target = where
    else:
        points = world_points(obj, depsgraph)[0]
        if not points:
            raise CommandError("bad_argument", f"'{obj.name}' has no vertices to put its origin by.")
        lo = Vector([min(p[i] for p in points) for i in range(3)])
        hi = Vector([max(p[i] for p in points) for i in range(3)])
        if where == "bottom_center":
            target = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
        elif where == "bounds_center":
            target = (lo + hi) / 2
        else:
            target = sum(points, Vector((0.0, 0.0, 0.0))) / len(points)
    world = obj.matrix_world.copy()
    inverse = world.inverted_safe()
    offset = inverse @ target
    shift = Matrix.Translation(offset)
    back = Matrix.Translation(-offset)
    mesh = obj.data
    mesh.transform(back, shape_keys=True)
    undo.add(lambda: mesh.transform(shift, shape_keys=True))
    mesh.update()
    undo.set(obj, "matrix_world", world @ shift)
    for child in obj.children:
        if child.parent_type not in VERTEX_PARENTS:
            undo.set(child, "matrix_parent_inverse", back @ child.matrix_parent_inverse)
    update()
    return vec(obj.matrix_world.translation)
