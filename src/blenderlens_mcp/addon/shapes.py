"""Build the shapes that no bmesh primitive provides: a tube along a path, a lathe of a profile, and text.

Each shape is returned as a new bmesh, as ``meshops.build_primitive`` returns a primitive, so
``create_object`` places it the same way and everything after that sees an ordinary mesh. Tubes and text
are built as curves that Blender converts to a mesh itself. ``bpy.data.meshes.new_from_object`` on an
original curve object evaluates a temporary copy (in Blender 4.2 and 5.0 alike), so the curve is never
linked into the scene and no operator runs. The temporary curve, object and mesh are removed before the
bmesh is returned, whether or not the conversion succeeds. The lathe is spun with ``bmesh.ops.spin``.

Every parameter is read and checked before anything is made. The one check that needs the mesh, whether the
font has shapes for the text, runs after the conversion, and the temporary data is removed either way, so a
refused call leaves nothing behind.
"""

import itertools
import math

import bmesh
import bpy
from mathutils import Vector

from . import meshops
from .common import CommandError, boolean, finite, is_number, number, require, text, triple

#: The kinds of object this module builds, which create_object takes besides the primitives.
KINDS = ("tube", "lathe", "text")

# The next four bound what a single call can ask Blender to build. Past them a call takes minutes and
# gigabytes of memory, or never finishes.
#: The most points of a tube's path. A tube of 1,000 points at 68 sides is about 400,000 faces.
MAX_PATH_POINTS = 1000
#: The most points of a lathe's profile. A lathe of 1,000 profile points at 1,024 segments is about a million
#: faces.
MAX_PROFILE_POINTS = 1000
#: The most bevel resolution of a tube, which is Blender's own maximum for a curve's bevel resolution.
MAX_BEVEL_RESOLUTION = 32
#: The most characters of text. 1,000 characters make a few hundred thousand faces.
MAX_TEXT = 1000

#: Samples along each span of a curved tube. Six per span keep a bend looking round at a game asset's sizes.
TUBE_SAMPLES = 6
#: The cosine between one segment of a path and the next below which the path counts as turning straight back
#: on itself. Blender cannot build a closed tube around such a turn, because the tube's cross-section folds flat.
TURNS_BACK = -0.999999
#: The angle above which an edge stays sharp on a tube shaded smooth. The rims of the caps (90 degrees), the
#: bends of a path made of straight runs, and the edges of a four-sided tube stay sharp, while the 60 degrees
#: between the faces of a six-sided tube are smoothed.
TUBE_SHARP = math.radians(61.0)


def build_shape(kind, params):
    """Return a new bmesh holding a tube, a lathe or text, with its size already applied to the mesh data.

    A parameter that does not fit raises CommandError before anything is made. The caller owns the bmesh
    and must free it.
    """
    if kind == "tube":
        return _tube(**_tube_args(params))
    if kind == "lathe":
        return _lathe(**_lathe_args(params))
    return _text(**_text_args(params))


def _count(rows, key, least, most):
    """Refuse a list of points that is not a list, or holds fewer than ``least`` or more than ``most``."""
    if not isinstance(rows, list):
        raise CommandError("bad_argument", f"'{key}' must be a list.")
    if len(rows) < least:
        raise CommandError("bad_argument", f"'{key}' needs at least {least} points, got {len(rows)}.")
    if len(rows) > most:
        raise CommandError("bad_argument", f"'{key}' holds {len(rows):,} points, more than the {most:,} allowed.")


# ---------------------------------------------------------------------------
# Tube
# ---------------------------------------------------------------------------


def _tube_args(params):
    """Return a tube's checked parameters: its path, radius, bevel resolution, caps and shading."""
    rows = require(params, "points")
    _count(rows, "points", 2, MAX_PATH_POINTS)
    points = [Vector(triple(row, f"points[{i}]", uniform=False)) for i, row in enumerate(rows)]
    for i in range(1, len(points)):
        if (points[i] - points[i - 1]).length < 1e-6:
            raise CommandError("bad_argument", f"points[{i}] repeats the point before it.",
                               "Each point must lie away from the one before it.")
    for i in range(1, len(points) - 1):
        before, after = (points[i] - points[i - 1]).normalized(), (points[i + 1] - points[i]).normalized()
        if before.dot(after) < TURNS_BACK:
            raise CommandError("bad_argument", f"At points[{i}] the path turns straight back along itself, which "
                                               "folds the tube flat there.",
                               "Turn the path by less than 180 degrees there, or end the tube at that point and "
                               "start another.")
    radius = number(params, "radius", 0.05, minimum=0)
    if radius == 0:
        raise CommandError("bad_argument", "A tube's 'radius' must be above 0.")
    resolution = number(params, "bevel_resolution", 2, 0, MAX_BEVEL_RESOLUTION, whole=True)
    return {"points": points, "radius": radius, "resolution": resolution,
            "caps": boolean(params, "fill", True), "smooth": boolean(params, "smooth", True)}


def _mesh_from_curve(curve):
    """Return a bmesh of the mesh Blender makes from ``curve``.

    The temporary object, the intermediate mesh and ``curve`` itself are removed afterwards, whether or not
    the conversion succeeds.
    """
    obj = mesh = None
    try:
        obj = bpy.data.objects.new(curve.name, curve)
        mesh = bpy.data.meshes.new_from_object(obj)
        bm = bmesh.new()
        bm.from_mesh(mesh)
        return bm
    finally:
        if obj is not None:
            bpy.data.objects.remove(obj)
        if mesh is not None:
            bpy.data.meshes.remove(mesh)
        bpy.data.curves.remove(curve)


def _weld(bm, distance):
    """Merge vertices closer together than ``distance``.

    A converted curve leaves duplicate vertices where its caps meet its sides, and they would leave the
    surface open.
    """
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=distance)


def _tube(points, radius, resolution, caps, smooth):
    """Return a bmesh of a tube swept along ``points``, made through a curve with a round bevel."""
    curve = bpy.data.curves.new("BlenderLensTube", "CURVE")
    try:
        curve.dimensions = "3D"
        if smooth:
            # A Bezier spline with automatic handles passes through every given point, as a Catmull-Rom
            # curve does.
            spline = curve.splines.new("BEZIER")
            spline.bezier_points.add(len(points) - 1)
            for handle, point in zip(spline.bezier_points, points, strict=True):
                handle.co = point
                handle.handle_left_type = handle.handle_right_type = "AUTO"
        else:
            spline = curve.splines.new("POLY")
            spline.points.add(len(points) - 1)
            for corner, point in zip(spline.points, points, strict=True):
                corner.co = (*point, 1.0)
        spline.use_smooth = True
        curve.resolution_u = TUBE_SAMPLES
        curve.bevel_mode = "ROUND"
        curve.bevel_depth = radius
        curve.bevel_resolution = resolution
        curve.use_fill_caps = caps
    except BaseException:
        bpy.data.curves.remove(curve)
        raise
    bm = _mesh_from_curve(curve)
    try:
        _weld(bm, radius * 1e-4)
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        for face in bm.faces:
            face.smooth = True
        for edge in bm.edges:
            edge.smooth = not (edge.is_manifold and edge.calc_face_angle(0.0) > TUBE_SHARP)
    except BaseException:
        bm.free()
        raise
    return bm


# ---------------------------------------------------------------------------
# Lathe
# ---------------------------------------------------------------------------


def _lathe_args(params):
    """Return a lathe's checked parameters: its profile, segments and caps."""
    rows = require(params, "profile")
    _count(rows, "profile", 2, MAX_PROFILE_POINTS)
    profile = []
    for i, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != 2 or not all(is_number(v) for v in row):
            raise CommandError("bad_argument", f"profile[{i}] must be [radius, z], got {row!r}.")
        radius, z = finite(row[0], f"profile[{i}]"), finite(row[1], f"profile[{i}]")
        if radius < 0:
            raise CommandError("bad_argument", f"profile[{i}] has a negative radius, {row[0]}.",
                               "Each profile point is [distance from the Z axis, height], with the distance 0 or "
                               "more. Spinning the profile around the axis makes the other side.")
        if profile and abs(radius - profile[-1][0]) < 1e-6 and abs(z - profile[-1][1]) < 1e-6:
            raise CommandError("bad_argument", f"profile[{i}] repeats the point before it.")
        profile.append((radius, z))
    if all(radius < 1e-6 for radius, _ in profile):
        raise CommandError("bad_argument", "Every profile point lies on the Z axis, so spinning makes no surface.",
                           "Give the profile points away from the axis: [radius, z].")
    segments = int(number(params, "segments", 32, 3, meshops.MAX_SEGMENTS, whole=True))
    return {"profile": profile, "segments": segments, "caps": boolean(params, "fill", True)}


def _lathe(profile, segments, caps):
    """Return a bmesh of ``profile`` spun around the Z axis in ``segments`` steps, capped when asked."""
    bm = bmesh.new()
    try:
        verts = [bm.verts.new((radius, 0.0, z)) for radius, z in profile]
        edges = [bm.edges.new((verts[i], verts[i + 1])) for i in range(len(verts) - 1)]
        bmesh.ops.spin(bm, geom=verts + edges, cent=(0.0, 0.0, 0.0), axis=(0.0, 0.0, 1.0), dvec=(0.0, 0.0, 0.0),
                       angle=math.tau, steps=segments, use_merge=True, use_duplicate=False)
        # A profile point on the axis spins into one copy of itself per segment. Merging the copies turns
        # the quads around it into the triangles of a pole, which closes the surface there.
        size = max(max(abs(r) for r, _ in profile), max(abs(z) for _, z in profile), 1e-3)
        _weld(bm, size * 1e-5)
        # A stretch of the profile that runs along the axis itself spins into faces with no area and loose
        # edges, so those are deleted.
        degenerate = [f for f in bm.faces if f.calc_area() <= size * size * 1e-12]
        if degenerate:
            bmesh.ops.delete(bm, geom=degenerate, context="FACES_ONLY")
        loose = [e for e in bm.edges if not e.link_faces]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context="EDGES")
        alone = [v for v in bm.verts if not v.link_faces]
        if alone:
            bmesh.ops.delete(bm, geom=alone, context="VERTS")
        capped = []
        if caps:
            rims = [e for e in bm.edges if e.is_boundary]
            if rims:
                capped = bmesh.ops.holes_fill(bm, edges=rims, sides=0)["faces"]
        bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        _lathe_uvs(bm, profile, set(capped))
    except BaseException:
        bm.free()
        raise
    return bm


def _lathe_uvs(bm, profile, caps):
    """Lay out the lathe's UVs the way Blender's primitives lay theirs out.

    The side is unrolled into the lower half of the UV square, with u going around the axis and v along the
    profile. The caps are discs in the upper half, the lower cap on the left and the upper cap on the right.
    """
    layer = bm.loops.layers.uv.new("UVMap")
    lengths = [0.0]
    for (r0, z0), (r1, z1) in itertools.pairwise(profile):
        lengths.append(lengths[-1] + math.hypot(r1 - r0, z1 - z0))
    total = lengths[-1] or 1.0
    along = {(round(r, 5), round(z, 5)): length / total for (r, z), length in zip(profile, lengths, strict=True)}
    widest = max(r for r, _ in profile) or 1.0
    middle = (min(z for _, z in profile) + max(z for _, z in profile)) / 2

    def turn(co, fallback):
        """Return how far round the axis ``co`` lies, from 0 to 1, or ``fallback`` for a point on the axis."""
        if math.hypot(co.x, co.y) < widest * 1e-6:
            return fallback
        return (math.atan2(co.y, co.x) / math.tau) % 1.0

    for face in bm.faces:
        loops = list(face.loops)
        if face in caps:
            centre = (0.25 if face.calc_center_median().z < middle else 0.75, 0.75)
            for loop in loops:
                co = loop.vert.co
                loop[layer].uv = (centre[0] + 0.24 * co.x / widest, centre[1] + 0.24 * co.y / widest)
            continue
        centre = face.calc_center_median()
        fallback = (math.atan2(centre.y, centre.x) / math.tau) % 1.0
        us = [turn(loop.vert.co, fallback) for loop in loops]
        if max(us) - min(us) > 0.5:
            us = [u + 1.0 if u < 0.5 else u for u in us]
        for loop, u in zip(loops, us, strict=True):
            co = loop.vert.co
            v = along.get((round(math.hypot(co.x, co.y), 5), round(co.z, 5)))
            if v is None:
                v = min(along.items(), key=lambda item: (item[0][0] - math.hypot(co.x, co.y)) ** 2
                        + (item[0][1] - co.z) ** 2)[1]
            loop[layer].uv = (u, v * 0.5)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def _text_args(params):
    """Return a text object's checked parameters: its words, size and depth."""
    words = text(params, "text")
    if words is None or not words.strip():
        raise CommandError("bad_argument", "A text object needs 'text', the words to write.")
    if len(words) > MAX_TEXT:
        raise CommandError("bad_argument", f"'text' holds {len(words):,} characters, more than the {MAX_TEXT:,} "
                                           "allowed.")
    size = number(params, "size", 1.0, minimum=0)
    if size == 0:
        raise CommandError("bad_argument", "A text's 'size' must be above 0.")
    depth = number(params, "extrude", size / 10, minimum=0)
    return {"words": words, "size": size, "depth": depth}


def _text(words, size, depth):
    """Return a bmesh of ``words`` in Blender's built-in font, centred, extruded to ``depth`` in all."""
    curve = bpy.data.curves.new("BlenderLensText", "FONT")
    try:
        curve.body = words
        curve.size = size
        # Blender extrudes text by this amount to each side of its middle, so half the depth gives the
        # whole depth.
        curve.extrude = depth / 2
        curve.align_x = "CENTER"
        curve.align_y = "CENTER"
    except BaseException:
        bpy.data.curves.remove(curve)
        raise
    bm = _mesh_from_curve(curve)
    try:
        if not bm.faces:
            raise CommandError("bad_argument", f"Blender's font has no shapes for {words[:40]!r}.",
                               "Use letters, digits and punctuation the built-in font has.")
        _weld(bm, size * 1e-5)
        if depth > 0:
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    except BaseException:
        bm.free()
        raise
    return bm
