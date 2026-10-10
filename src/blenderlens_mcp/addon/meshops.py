"""Build, edit and read meshes through bmesh.

Everything here works on mesh data directly rather than through the ``bpy.ops.mesh`` operators, so it
needs no edit mode, no 3D viewport and no selection state. It behaves the same in a background Blender as
in a Blender window running the add-on. Faces are picked by index or by the direction they face in world
space.
"""

import math

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector

from . import shapes
from .common import (
    CommandError,
    Rollback,
    active_collection,
    boolean,
    closest,
    did_you_mean,
    direction,
    get_collection,
    get_mesh_object,
    get_object,
    mesh_counts,
    number,
    object_summary,
    plural,
    require,
    rnd,
    select_only,
    sync_edit_mode,
    text,
    triple,
    update,
    vec,
)

#: The kinds of object create_object makes.
KINDS = ("cube", "plane", "grid", "circle", "cylinder", "cone", "uv_sphere", "ico_sphere", "torus",
         "monkey", *shapes.KINDS, "empty")

#: The operations edit_mesh runs.
OPERATIONS = ("extrude", "inset", "translate", "scale", "rotate", "delete", "subdivide", "bevel",
              "recalc_normals", "flip_normals", "merge", "triangulate", "shade_smooth", "shade_flat",
              "bisect", "solidify", "dissolve_planar", "mark_seams")

#: The default angle, in degrees, of mark_seams, at which it cuts a box into its six sides and a cylinder at its
#: rims.
SEAM_ANGLE = 30.0
#: The default angle, in degrees, of dissolve_planar, at which it merges faces as nearly flat as Blender's own
#: Limited Dissolve merges by default.
PLANAR_ANGLE = 5.0
#: The sides of the cutting plane that bisect can keep.
BISECT_SIDES = ("below", "above", "both")

# The next seven bound what a single call can ask Blender to build. Past them a call takes minutes and
# gigabytes of memory, or never finishes, and the Blender session is lost with it.
#: The most segments around a shape, and rings along it.
MAX_SEGMENTS = 1024
#: The most subdivisions of an ico sphere. At 8 it has 20 * 4**7 = 327,680 faces.
MAX_ICO_SUBDIVISIONS = 8
#: The most subdivisions of a grid along each side: 1,000 by 1,000 is a million faces.
MAX_GRID_SUBDIVISIONS = 1000
#: The most cuts of the subdivide operation.
MAX_CUTS = 64
#: The most segments of the bevel operation.
MAX_BEVEL_SEGMENTS = 64
#: The most vertices create_mesh takes, and the most a modifier stack may make.
MAX_VERTICES = 1_000_000
#: The most faces create_mesh takes, and the most an edit_mesh operation or a modifier stack may make.
MAX_FACES = 2_000_000


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


def _number(params, key, default, minimum=None, maximum=None):
    """Return the number ``params[key]``, or ``default`` when it is not given, checked to be finite and in bounds."""
    return number(params, key, default, minimum, maximum)


def _whole(params, key, default, minimum, maximum):
    """Return a count, such as segments, rings or subdivisions, from the parameters.

    A fractional number is truncated to a whole number rather than refused.
    """
    return int(number(params, key, default, minimum, maximum))


def _torus(bm, major_radius, minor_radius, major_segments, minor_segments, uv_layer):
    """Build a torus around the Z axis into ``bm``, with its UVs laid out over the whole square."""
    rings = []
    for i in range(major_segments):
        u = 2 * math.pi * i / major_segments
        ring = []
        for j in range(minor_segments):
            v = 2 * math.pi * j / minor_segments
            radial = major_radius + minor_radius * math.cos(v)
            ring.append(bm.verts.new((radial * math.cos(u), radial * math.sin(u), minor_radius * math.sin(v))))
        rings.append(ring)
    for i in range(major_segments):
        a, b = rings[i], rings[(i + 1) % major_segments]
        for j in range(minor_segments):
            k = (j + 1) % minor_segments
            face = bm.faces.new((a[j], b[j], b[k], a[k]))
            corners = ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1))
            for loop, (ci, cj) in zip(face.loops, corners, strict=True):
                loop[uv_layer].uv = (ci / major_segments, cj / minor_segments)


def build_primitive(kind, params):
    """Return a new bmesh holding a primitive of ``kind``, with its dimensions already applied to the mesh data.

    Every parameter is read and checked before the bmesh is made, so a refused call leaves nothing behind.
    The caller owns the bmesh and must free it.
    """
    if kind == "cube":
        size = triple(params.get("size"), "size", (2.0, 2.0, 2.0))
    elif kind in ("plane", "grid"):
        given = params.get("size")
        if isinstance(given, list) and len(given) == 2:
            given = [*given, None]  # A flat sheet has no depth, so [x, y] is accepted and Z keeps its default.
        size = triple(given, "size", (2.0, 2.0, 1.0))
        cuts = _whole(params, "subdivisions", 1 if kind == "plane" else 10, 1, MAX_GRID_SUBDIVISIONS)
    elif kind == "circle":
        fill = boolean(params, "fill", False)
        segments = _whole(params, "segments", 32, 3, MAX_SEGMENTS)
        radius = _number(params, "radius", 1.0, 0)
    elif kind in ("cylinder", "cone"):
        radius = _number(params, "radius", 1.0, 0)
        top = radius if kind == "cylinder" else _number(params, "radius_top", 0.0, 0)
        fill = boolean(params, "fill", True)
        segments = _whole(params, "segments", 32, 3, MAX_SEGMENTS)
        depth = _number(params, "depth", 2.0, 0)
    elif kind == "uv_sphere":
        segments = _whole(params, "segments", 32, 3, MAX_SEGMENTS)
        rings = _whole(params, "rings", 16, 3, MAX_SEGMENTS)
        radius = _number(params, "radius", 1.0, 0)
    elif kind == "ico_sphere":
        subdivisions = _whole(params, "subdivisions", 2, 1, MAX_ICO_SUBDIVISIONS)
        radius = _number(params, "radius", 1.0, 0)
    elif kind == "torus":
        radius = _number(params, "radius", 1.0, 0)
        minor = _number(params, "minor_radius", 0.25, 0)
        segments = _whole(params, "segments", 48, 3, MAX_SEGMENTS)
        rings = _whole(params, "rings", 12, 3, MAX_SEGMENTS)
    elif kind == "monkey":
        factor = _number(params, "size", 1.0, 0)
    else:
        raise CommandError("bad_argument", f"Unknown kind '{kind}'.", "The kinds are: " + ", ".join(KINDS) + ".")

    bm = bmesh.new()
    try:
        uv = bm.loops.layers.uv.new("UVMap")
        if kind == "cube":
            bmesh.ops.create_cube(bm, size=1.0, calc_uvs=True)
            bmesh.ops.scale(bm, vec=size, verts=bm.verts)
        elif kind in ("plane", "grid"):
            # create_grid's size is the half-width, so size 1 spans -1 to 1. A size of 0.5 makes a unit grid,
            # which the scale below stretches to the requested size.
            bmesh.ops.create_grid(bm, x_segments=cuts, y_segments=cuts, size=0.5, calc_uvs=True)
            bmesh.ops.scale(bm, vec=(size[0], size[1], 1.0), verts=bm.verts)
        elif kind == "circle":
            bmesh.ops.create_circle(bm, cap_ends=fill, segments=segments, radius=radius, calc_uvs=True)
        elif kind in ("cylinder", "cone"):
            bmesh.ops.create_cone(bm, cap_ends=fill, cap_tris=False, segments=segments, radius1=radius,
                                  radius2=top, depth=depth, calc_uvs=True)
        elif kind == "uv_sphere":
            bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=rings, radius=radius, calc_uvs=True)
        elif kind == "ico_sphere":
            bmesh.ops.create_icosphere(bm, subdivisions=subdivisions, radius=radius, calc_uvs=True)
        elif kind == "torus":
            _torus(bm, radius, minor, segments, rings, uv)
        else:
            bmesh.ops.create_monkey(bm, calc_uvs=True)
            bmesh.ops.scale(bm, vec=(factor, factor, factor), verts=bm.verts)
    except BaseException:
        bm.free()
        raise
    return bm


def _placement(params):
    """Return where a new object goes: its location, rotation in radians, scale, parent and collection name.

    Every placement parameter is checked here, before anything is made, so a bad one refuses the call
    without leaving a half-made object behind.
    """
    collection = params.get("collection")
    if collection not in (None, "") and not isinstance(collection, str):
        raise CommandError("bad_argument", f"'collection' must be a collection name, got {collection!r}.")
    return {
        "location": triple(params.get("location"), "location", (0.0, 0.0, 0.0), uniform=False),
        "rotation": [math.radians(a) for a in triple(params.get("rotation"), "rotation", (0.0, 0.0, 0.0),
                                                     uniform=False)],
        "scale": triple(params.get("scale"), "scale", (1.0, 1.0, 1.0)),
        "parent": get_object(params["parent"], "parent") if params.get("parent") else None,
        "collection": collection or None,
    }


def _object_name(params, default):
    """Return the name a new object gets: the ``name`` parameter, or ``default`` when it is not given."""
    name = params.get("name")
    if name is None or name == "":
        return default
    if not isinstance(name, str):
        raise CommandError("bad_argument", f"'name' must be text, got {name!r}.")
    return name


def _place(obj, placement, undo):
    """Link a new object, give it its transform and parent, and select it.

    A collection made here, or an existing collection linked into the scene here, is recorded in ``undo``,
    so if Blender refuses partway the caller's rollback removes it along with the object.
    """
    name = placement["collection"]
    collection = get_collection(name, create=True, undo=undo) if name else active_collection()
    collection.objects.link(obj)
    obj.location = placement["location"]
    obj.rotation_euler = placement["rotation"]
    obj.scale = placement["scale"]
    if placement["parent"] is not None:
        obj.parent = placement["parent"]
        obj.matrix_parent_inverse = Matrix.Identity(4)
    # select_only updates the view layer first, because until then the view layer's object list does not
    # include the newly linked object.
    select_only([obj])


def create_object(params):
    """Make an object of a kind such as a cube, a tube or an empty, place it, and return it."""
    kind = str(require(params, "kind")).lower()
    if kind not in KINDS:
        raise CommandError("bad_argument", f"Unknown kind '{kind}'.", "The kinds are: " + ", ".join(KINDS) + ".")
    name = _object_name(params, kind.replace("_", " ").title().replace(" ", ""))
    placement = _placement(params)
    if kind == "empty":
        size = _number(params, "size", 1.0, 0)
        with Rollback() as undo:
            obj = bpy.data.objects.new(name, None)
            undo.add(lambda: bpy.data.objects.remove(obj, do_unlink=True))
            obj.empty_display_type = "PLAIN_AXES"
            obj.empty_display_size = size
            _place(obj, placement, undo)
        return object_summary(obj)
    bm = shapes.build_shape(kind, params) if kind in shapes.KINDS else build_primitive(kind, params)
    try:
        with Rollback() as undo:
            mesh = bpy.data.meshes.new(name)
            undo.add(lambda: bpy.data.meshes.remove(mesh))
            bm.to_mesh(mesh)
            mesh.update()
            obj = bpy.data.objects.new(name, mesh)
            undo.add(lambda: bpy.data.objects.remove(obj, do_unlink=True))
            _place(obj, placement, undo)
    finally:
        bm.free()
    return object_summary(obj)


def create_mesh(params):
    """Make a mesh object from vertices, faces and edges given one by one, place it, and return it."""
    name = require(params, "name")
    if not isinstance(name, str) or not name:
        raise CommandError("bad_argument", f"'name' must be text, got {name!r}.")
    vertices = require(params, "vertices")
    faces = params.get("faces") or []
    edges = params.get("edges") or []
    if not isinstance(vertices, list) or not vertices:
        raise CommandError("bad_argument", "'vertices' must be a non-empty list of [x, y, z].")
    if len(vertices) > MAX_VERTICES:
        raise CommandError("bad_argument", f"'vertices' holds {len(vertices):,} positions, more than the "
                                           f"{MAX_VERTICES:,} create_mesh takes.")
    if isinstance(faces, list) and len(faces) > MAX_FACES:
        raise CommandError("bad_argument", f"'faces' holds {len(faces):,} faces, more than the {MAX_FACES:,} "
                                           "create_mesh takes.")
    coords = [triple(v, f"vertices[{i}]", uniform=False) for i, v in enumerate(vertices)]
    count = len(coords)
    for kind, rows, least in (("faces", faces, 3), ("edges", edges, 2)):
        if not isinstance(rows, list):
            raise CommandError("bad_argument", f"'{kind}' must be a list of vertex index lists.")
        for i, row in enumerate(rows):
            if not isinstance(row, list) or len(row) < least or (kind == "edges" and len(row) != 2):
                many = "at least 3" if kind == "faces" else "exactly 2"
                raise CommandError("bad_argument", f"{kind}[{i}] must list {many} vertex indices.")
            bad = [v for v in row if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v < count]
            if bad:
                raise CommandError("bad_argument", f"{kind}[{i}] refers to vertices {bad}, but the mesh has {count:,} "
                                                   f"vertices, numbered 0 to {count - 1:,}.")
            if len(set(row)) != len(row):
                raise CommandError("bad_argument", f"{kind}[{i}] repeats a vertex.")
    recalc = boolean(params, "recalc_normals", True)
    placement = _placement(params)
    with Rollback() as undo:
        mesh = bpy.data.meshes.new(name)
        undo.add(lambda: bpy.data.meshes.remove(mesh))
        mesh.from_pydata(coords, edges, faces)
        invalid = mesh.validate(clean_customdata=False)
        if faces and recalc:
            bm = bmesh.new()
            try:
                bm.from_mesh(mesh)
                bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
                bm.to_mesh(mesh)
            finally:
                bm.free()
        mesh.update()
        obj = bpy.data.objects.new(name, mesh)
        undo.add(lambda: bpy.data.objects.remove(obj, do_unlink=True))
        _place(obj, placement, undo)
    out = object_summary(obj)
    if invalid:
        out["note"] = "Blender corrected invalid geometry while building the mesh; get_mesh shows what it kept."
    return out


# ---------------------------------------------------------------------------
# Selecting faces
# ---------------------------------------------------------------------------


def _normal_matrix(obj):
    """Return the matrix that turns the object's face normals into world space."""
    return obj.matrix_world.to_3x3().inverted_safe().transposed()


def world_normal(obj, normal, matrix=None):
    """Return a face normal of ``obj`` in world space, as a unit vector; ``matrix`` is its normal matrix if known."""
    n = (matrix or _normal_matrix(obj)) @ normal
    return n.normalized() if n.length else n


def facing_label(normal):
    """Return the side, such as "top", that a world-space normal faces within 1 degree, or None."""
    for label, axis in (("top", (0, 0, 1)), ("bottom", (0, 0, -1)), ("front", (0, -1, 0)),
                        ("back", (0, 1, 0)), ("right", (1, 0, 0)), ("left", (-1, 0, 0))):
        if normal.dot(Vector(axis)) > math.cos(math.radians(1.0)):
            return label
    return None


def select_faces(bm, obj, params):
    """Return the faces of ``bm`` that ``params`` picks by index, by the world direction they face, or both.

    With both, a face must meet both. With neither ``faces`` nor ``facing`` given, every face is returned.
    Raise CommandError when no face matches.
    """
    bm.faces.ensure_lookup_table()
    faces = list(bm.faces)
    indices = params.get("faces")
    if indices is not None:
        if not isinstance(indices, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in indices):
            raise CommandError("bad_argument", "'faces' must be a list of face indices.")
        out_of_range = [i for i in indices if not 0 <= i < len(faces)]
        if out_of_range:
            raise CommandError("bad_argument", f"Faces {out_of_range} do not exist: '{obj.name}' has "
                                               f"{len(faces):,} faces.",
                               "get_mesh lists the faces with their index and the way they face.")
        wanted = set(indices)
        faces = [f for f in faces if f.index in wanted]
    facing = params.get("facing")
    if facing is not None:
        target = direction(facing)
        tolerance = params.get("tolerance")
        if tolerance is None:
            tolerance = 30.0
        elif not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) or not 0 <= tolerance <= 180:
            # The range test also refuses NaN, because NaN compares false with every number.
            raise CommandError("bad_argument", "'tolerance' must be degrees between 0 and 180.")
        limit = math.cos(math.radians(tolerance)) - 1e-6
        matrix = _normal_matrix(obj)
        faces = [f for f in faces if world_normal(obj, f.normal, matrix).dot(target) >= limit]
    if not faces:
        raise CommandError("not_found", f"No face of '{obj.name}' matches the selection.",
                           "get_mesh lists the faces with their index and the way they face. Widen 'tolerance', "
                           "or name faces by index.")
    return faces


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def _center(verts):
    """Return the average position of ``verts``, in the mesh's own space."""
    total = Vector((0.0, 0.0, 0.0))
    for v in verts:
        total += v.co
    return total / max(len(verts), 1)


def _verts_of(faces):
    """Return the vertices of ``faces``, each once."""
    seen = {}
    for f in faces:
        for v in f.verts:
            seen[v.index] = v
    return list(seen.values())


def _boundary_edges(bm, faces):
    """Return the edges on the rim of the selected region, or every edge when the region is the whole mesh."""
    chosen = set(faces)
    if len(chosen) == len(bm.faces):
        return list(bm.edges)
    rim = []
    for f in faces:
        for e in f.edges:
            inside = sum(1 for linked in e.link_faces if linked in chosen)
            if inside == 1 or len(e.link_faces) == 1:
                rim.append(e)
    return list(dict.fromkeys(rim))


def _sharp_edges(faces, angle):
    """Return the edges of the selected faces whose two faces meet at more than ``angle`` radians.

    With a moderate angle this picks the edges of a box, but not the edges between the sides of a cylinder.
    """
    edges = dict.fromkeys(e for f in faces for e in f.edges)
    return [e for e in edges if e.is_manifold and e.calc_face_angle(0.0) > angle]


def _bevel_edges(bm, faces, args):
    """Return the edges a bevel takes: those sharper than its angle when one is given, else the rim's."""
    return _sharp_edges(faces, args["angle"]) if args.get("angle") is not None else _boundary_edges(bm, faces)


def _inner(faces):
    """Return the edges and vertices of the selected faces whose every face is selected.

    These are what dissolving may remove without reaching past the selection.
    """
    chosen = set(faces)
    edges = [e for e in dict.fromkeys(e for f in faces for e in f.edges) if all(g in chosen for g in e.link_faces)]
    verts = [v for v in dict.fromkeys(v for f in faces for v in f.verts) if all(g in chosen for g in v.link_faces)]
    return edges, verts


def bmesh_counts(bm):
    """Return the counts that ``mesh_counts`` gives, read from a BMesh instead of mesh data.

    In edit mode the BMesh holds the current edit, while the mesh data is stale until edit mode ends.
    """
    return {
        "vertices": len(bm.verts),
        "edges": len(bm.edges),
        "faces": len(bm.faces),
        "triangles": sum(len(f.verts) - 2 for f in bm.faces),
    }


def world_to_mesh_direction(obj, way, key="direction"):
    """Return the unit vector in the mesh's own space that points the same way as the world-space ``way``.

    The direction is moved by the inverse of the object's rotation and scale. Directions are given in world
    space, as facing is, so a cube rotated 90 degrees about X and extruded along "top" grows toward world +Z.
    Raise CommandError when the object has zero scale.
    """
    local = obj.matrix_world.to_3x3().inverted_safe() @ way
    if local.length == 0:
        raise CommandError("bad_argument", f"'{key}' cannot be followed: '{obj.name}' has zero scale.")
    return local.normalized()


def world_to_mesh_normal(obj, normal, key="direction"):
    """Return the world-space plane normal ``normal`` as a unit vector in the mesh's own space.

    A normal is moved by the transpose of the object's transform rather than by its inverse. The two agree
    unless the object is scaled unevenly. Raise CommandError when the object has zero scale.
    """
    local = obj.matrix_world.to_3x3().transposed() @ normal
    if local.length == 0:
        raise CommandError("bad_argument", f"'{key}' cannot be followed: '{obj.name}' has zero scale.")
    return local.normalized()


def _edit_args(obj, operation, params):
    """Return every parameter that ``operation`` takes, checked before the mesh is touched.

    In edit mode the operation works directly on the BMesh being edited, so a refusal halfway through would
    leave a half-made edit in the mesh. Nothing may refuse the call once the edit has started.
    """
    args = {}
    amount = number(params, "amount")
    if operation in ("extrude", "inset", "bevel", "solidify") and amount is None:
        what = {"extrude": "the distance to extrude", "inset": "the width of the border", "bevel": "the bevel width",
                "solidify": "the thickness"}
        raise CommandError("bad_argument", f"{operation} needs 'amount', {what[operation]}.")
    if operation == "solidify" and amount == 0:
        raise CommandError("bad_argument", "solidify needs a thickness other than 0.")
    args["amount"] = amount
    if operation == "extrude" and params.get("direction") is not None:
        args["direction"] = world_to_mesh_direction(obj, direction(params["direction"], "direction"))
    elif operation == "inset":
        args["depth"] = number(params, "depth", 0.0)
        args["individual"] = boolean(params, "individual", False)
    elif operation == "translate":
        args["offset"] = triple(params.get("offset"), "offset", uniform=False)
        if args["offset"] is None:
            raise CommandError("bad_argument", "translate needs 'offset', [x, y, z].")
    elif operation in ("scale", "rotate"):
        if operation == "scale":
            args["factor"] = triple(params.get("factor"), "factor")
            if args["factor"] is None:
                raise CommandError("bad_argument", "scale needs 'factor', a number or [x, y, z].")
        else:
            degrees = triple(params.get("rotation"), "rotation", uniform=False)
            if degrees is None:
                raise CommandError("bad_argument", "rotate needs 'rotation', [x, y, z] in degrees.")
            args["matrix"] = Euler([math.radians(a) for a in degrees], "XYZ").to_matrix()
        pivot = triple(params.get("pivot"), "pivot", uniform=False)
        args["pivot"] = Vector(pivot) if pivot is not None else None
    elif operation == "subdivide":
        args["cuts"] = number(params, "cuts", 1, 1, MAX_CUTS, whole=True)
    elif operation == "bevel":
        args["segments"] = number(params, "segments", 1, 1, MAX_BEVEL_SEGMENTS, whole=True)
    elif operation == "bisect":
        if params.get("direction") is None:
            raise CommandError("bad_argument", "bisect needs 'direction', the normal of the cutting plane.",
                               "Give a side such as \"top\", or [x, y, z] in world space. 'pivot' places the "
                               "plane.")
        args["normal"] = world_to_mesh_normal(obj, direction(params["direction"], "direction"))
        pivot = triple(params.get("pivot"), "pivot", uniform=False)
        args["pivot"] = Vector(pivot) if pivot is not None else None
        keep = text(params, "keep", "below")
        if keep not in BISECT_SIDES:
            raise CommandError("bad_argument", f"'keep' must be one of {', '.join(BISECT_SIDES)}, got {keep!r}.")
        args["keep"] = keep
        args["fill"] = boolean(params, "fill", True)
    defaults = {"bevel": None, "shade_smooth": None, "mark_seams": SEAM_ANGLE, "dissolve_planar": PLANAR_ANGLE}
    if operation in defaults:
        angle = number(params, "angle", defaults[operation], 0, 180)
        args["angle"] = math.radians(angle) if angle is not None else None
    return args


def _bound_edit(bm, operation, faces, args):
    """Refuse, before it starts, an edit whose result would pass MAX_FACES or that could not do what was asked.

    Each parameter has its own maximum, but cuts and segments multiply what the mesh already holds. For
    example, 64 cuts on a 30 by 30 grid make 3.8 million faces. For bisect the check is that the cutting plane
    crosses the selected faces, and the plane's point (``pivot``, or else the selection's centre) is stored in
    ``args["point"]``.
    """
    if operation == "bisect":
        verts = _verts_of(faces)
        args["point"] = args["pivot"] if args["pivot"] is not None else _center(verts)
        sides = [(v.co - args["point"]).dot(args["normal"]) for v in verts]
        if max(sides) <= 1e-6 or min(sides) >= -1e-6:
            raise CommandError("bad_argument", "The cutting plane does not cross the selected faces, so bisect would "
                                               "cut nothing.",
                               "'pivot' is a point of the plane in the mesh's own space, by default the selection's "
                               "centre. get_mesh with space \"local\" lists the faces' centres in that space.")
        return
    if operation == "subdivide":
        made = len(bm.faces) - len(faces) + len(faces) * (args["cuts"] + 1) ** 2
        what = "cuts"
    elif operation == "bevel":
        made = len(bm.faces) + len(_bevel_edges(bm, faces, args)) * args["segments"]
        what = "segments"
    elif operation == "solidify":
        made = len(bm.faces) + len(faces) + len(_boundary_edges(bm, faces))
        what = None
    else:
        return
    if made > MAX_FACES:
        raise CommandError("bad_argument", f"{operation} would make about {made:,} faces, more than the "
                                           f"{MAX_FACES:,} a mesh may have.",
                           f"Use fewer {what}, or select fewer faces." if what else "Select fewer faces.")


def edit_mesh(params):
    """Run one operation, such as extrude or bevel, on the faces picked, and return the mesh's new counts."""
    obj = get_mesh_object(require(params, "object"))
    operation = str(require(params, "operation")).lower()
    if operation not in OPERATIONS:
        raise CommandError("bad_argument", f"Unknown operation '{operation}'.",
                           "The operations are: " + ", ".join(OPERATIONS) + ".")
    # Directions and facing are judged by matrix_world, which does not reflect a transform just set, or just
    # put back by a refused call, until the view layer is updated.
    update()
    args = _edit_args(obj, operation, params)

    mesh = obj.data
    in_edit_mode = mesh.is_editmode
    bm = bmesh.from_edit_mesh(mesh) if in_edit_mode else bmesh.new()
    try:
        if not in_edit_mode:
            bm.from_mesh(mesh)
        faces = select_faces(bm, obj, params)
        _bound_edit(bm, operation, faces, args)
        new_faces = _apply(bm, operation, faces, args)
        bm.normal_update()
        if operation == "shade_smooth" and args["angle"] is not None:
            _sharpen(bm, args["angle"])
        bm.faces.index_update()
        new_indices = sorted(f.index for f in new_faces if f.is_valid) if new_faces is not None else None
        # In edit mode the mesh data is stale until edit mode ends, so the counts come from the BMesh.
        counts = bmesh_counts(bm)
        if in_edit_mode:
            bmesh.update_edit_mesh(mesh)
        else:
            bm.to_mesh(mesh)
    finally:
        if not in_edit_mode:
            bm.free()
    if in_edit_mode:
        # Blender 4.2 takes the object's bounds from the mesh data until edit mode ends. Copying the edit into
        # the mesh data brings the object's dimensions up to date in 4.2, as Blender 5.x does on its own.
        obj.update_from_editmode()
    else:
        mesh.update()
    update()
    out = {
        "object": obj.name,
        "operation": operation,
        "selected_faces": len(faces),
        "mesh": counts,
        "dimensions": vec(obj.dimensions),
    }
    # Shading neither makes nor moves faces, so the result lists no faces for it, since the list would only
    # repeat the selection.
    if new_indices is not None and operation not in ("shade_smooth", "shade_flat"):
        out["result_faces"] = new_indices
    if "merged" in args:
        out["merged_vertices"] = args["merged"]
    if "seams" in args:
        out["seams"] = args["seams"]
    return out


def _sharpen(bm, angle):
    """Mark edges sharper than ``angle`` (radians) sharp and the rest smooth, over the whole mesh.

    This does what ``Mesh.set_sharp_from_angle`` does, but on the BMesh, so it also holds in edit mode, where
    the mesh data is overwritten from the BMesh when edit mode ends.
    """
    for edge in bm.edges:
        edge.smooth = not (edge.is_manifold and edge.calc_face_angle(0.0) > angle)


def _apply(bm, operation, faces, args):
    """Run one operation on arguments that are already checked.

    Return the faces the operation produced or moved, or None when it has no such faces to report. The merge
    and mark_seams operations also store their counts in ``args``, as ``merged`` and ``seams``.
    """
    amount = args["amount"]

    if operation == "extrude":
        if "direction" in args:
            way = args["direction"]
        else:
            way = Vector((0.0, 0.0, 0.0))
            for f in faces:
                way += f.normal * f.calc_area()
            if way.length == 0:
                raise CommandError("bad_argument", "The selected faces' normals cancel out, so there is no direction "
                                                   "to extrude along.", "Pass 'direction'.")
            way.normalize()
        result = bmesh.ops.extrude_face_region(bm, geom=faces)
        moved = [g for g in result["geom"] if isinstance(g, bmesh.types.BMVert)]
        bmesh.ops.translate(bm, vec=way * amount, verts=moved)
        # extrude_face_region leaves the region's original faces behind inside the solid, so they are removed.
        leftovers = [f for f in faces if f.is_valid]
        if leftovers:
            bmesh.ops.delete(bm, geom=leftovers, context="FACES_ONLY")
        return [g for g in result["geom"] if isinstance(g, bmesh.types.BMFace)]

    if operation == "inset":
        if args["individual"]:
            bmesh.ops.inset_individual(bm, faces=faces, thickness=amount, depth=args["depth"], use_even_offset=True)
        else:
            bmesh.ops.inset_region(bm, faces=faces, thickness=amount, depth=args["depth"], use_even_offset=True)
        return faces

    verts = _verts_of(faces)
    if operation == "translate":
        bmesh.ops.translate(bm, vec=args["offset"], verts=verts)
        return faces

    if operation == "scale":
        pivot = args["pivot"] if args["pivot"] is not None else _center(verts)
        bmesh.ops.scale(bm, vec=args["factor"], verts=verts, space=Matrix.Translation(-pivot))
        return faces

    if operation == "rotate":
        pivot = args["pivot"] if args["pivot"] is not None else _center(verts)
        bmesh.ops.rotate(bm, cent=pivot, matrix=args["matrix"], verts=verts)
        return faces

    if operation == "delete":
        bmesh.ops.delete(bm, geom=faces, context="FACES")
        return None

    if operation == "subdivide":
        edges = list({e for f in faces for e in f.edges})
        bmesh.ops.subdivide_edges(bm, edges=edges, cuts=args["cuts"], use_grid_fill=True)
        return None

    if operation == "bevel":
        edges = _bevel_edges(bm, faces, args)
        if not edges and args.get("angle") is not None:
            raise CommandError("not_found", f"No edge of the selection is sharper than {math.degrees(args['angle']):g} "
                                            "degrees, so there is nothing to bevel.",
                               "Lower 'angle'. A box's faces meet at 90 degrees, and the sides of a 32-sided cylinder "
                               "at 11.25 degrees.")
        if not edges:
            raise CommandError("not_found", "The selection has no edges on its rim to bevel.",
                               "The faces picked form a closed surface. Pass 'angle' to bevel the edges sharper than "
                               "it, or pick fewer faces.")
        geom = list({v for e in edges for v in e.verts}) + edges
        result = bmesh.ops.bevel(bm, geom=geom, offset=amount, offset_type="OFFSET", segments=args["segments"],
                                 profile=0.5, affect="EDGES", clamp_overlap=True)
        return result.get("faces", [])

    if operation == "recalc_normals":
        bmesh.ops.recalc_face_normals(bm, faces=faces)
        return faces

    if operation == "flip_normals":
        bmesh.ops.reverse_faces(bm, faces=faces)
        return faces

    if operation == "merge":
        distance = amount if amount is not None else 0.0001
        before = len(bm.verts)
        bmesh.ops.remove_doubles(bm, verts=verts, dist=distance)
        args["merged"] = before - len(bm.verts)
        return None

    if operation == "triangulate":
        result = bmesh.ops.triangulate(bm, faces=faces)
        return result.get("faces", [])

    if operation in ("shade_smooth", "shade_flat"):
        for f in faces:
            f.smooth = operation == "shade_smooth"
        return faces

    if operation == "bisect":
        geom = [*verts, *dict.fromkeys(e for f in faces for e in f.edges), *faces]
        keep = args["keep"]
        # bisect_plane's clear_outer removes what lies on the side the normal points to, and clear_inner what
        # lies on the other side.
        result = bmesh.ops.bisect_plane(bm, geom=geom, plane_co=args["point"], plane_no=args["normal"], dist=1e-6,
                                        clear_outer=keep == "below", clear_inner=keep == "above")
        if keep == "both" or not args["fill"]:
            return None
        rims = [g for g in result["geom_cut"] if isinstance(g, bmesh.types.BMEdge) and g.is_valid and g.is_boundary]
        return bmesh.ops.holes_fill(bm, edges=rims, sides=0)["faces"] if rims else []

    if operation == "solidify":
        result = bmesh.ops.solidify(bm, geom=faces, thickness=amount)
        return [g for g in result["geom"] if isinstance(g, bmesh.types.BMFace)]

    if operation == "dissolve_planar":
        edges, inner = _inner(faces)
        # Material and seam borders are kept, since dissolving across them would merge two materials into one
        # face, or lose a seam.
        bmesh.ops.dissolve_limit(bm, angle_limit=args["angle"], use_dissolve_boundaries=False, verts=inner,
                                 edges=edges, delimit={"MATERIAL", "SEAM"})
        return None

    if operation == "mark_seams":
        for edge in dict.fromkeys(e for f in faces for e in f.edges):
            edge.seam = edge.is_manifold and edge.calc_face_angle(0.0) > args["angle"]
        args["seams"] = sum(1 for e in bm.edges if e.seam)
        return None

    raise CommandError("bad_argument", f"Unknown operation '{operation}'.")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _material_slots(obj, value):
    """Return the material slot indices that ``value`` picks, or None when ``value`` is None.

    ``value`` is either a slot index, which picks that slot, or a material's name, which picks every slot
    holding that material.
    """
    if value is None:
        return None
    slots = obj.material_slots
    if isinstance(value, int) and not isinstance(value, bool):
        if not 0 <= value < max(len(slots), 1):
            raise CommandError("bad_argument", f"'{obj.name}' has {plural(len(slots), 'material slot')}, so there is "
                                               f"no slot {value}.", "get_object lists the slots.")
        return {value}
    if not isinstance(value, str) or not value:
        raise CommandError("bad_argument", f"'material' takes a slot index or a material's name, got {value!r}.")
    found = {i for i, slot in enumerate(slots) if slot.material is not None and slot.material.name == value}
    if not found:
        names = [slot.material.name for slot in slots if slot.material is not None]
        near = closest(value, names)
        raise CommandError("not_found", f"No slot of '{obj.name}' holds a material named '{value}'.",
                           did_you_mean(near)
                           + (f"Its slots hold {', '.join(names)}." if names else "It has no materials."))
    return found


def get_mesh(params):
    """Return a mesh's faces with their index, centre, normal and facing, and its vertices when asked."""
    obj = get_mesh_object(require(params, "object"))
    space = str(params.get("space") or "world").lower()
    if space not in ("world", "local"):
        raise CommandError("bad_argument", "'space' must be world or local.")
    limit = params.get("limit", 200)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise CommandError("bad_argument", "'limit' must be a whole number of 0 or more.")
    evaluated = boolean(params, "evaluated", False)
    with_vertices = boolean(params, "include_vertices", False)
    picked = _material_slots(obj, params.get("material"))
    sync_edit_mode()
    update()

    holder = None
    if evaluated:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        holder = obj.evaluated_get(depsgraph)
        mesh = holder.to_mesh()
    else:
        mesh = obj.data
    try:
        world = obj.matrix_world
        to_space = world if space == "world" else Matrix.Identity(4)
        normal_matrix = _normal_matrix(obj)
        polygons = mesh.polygons if picked is None else [p for p in mesh.polygons if p.material_index in picked]
        faces = []
        for poly in polygons[:limit]:
            n_world = world_normal(obj, poly.normal, normal_matrix)
            faces.append({
                "index": poly.index,
                "vertices": list(poly.vertices),
                "center": vec(to_space @ poly.center),
                "normal": vec(n_world if space == "world" else poly.normal),
                "facing": facing_label(n_world),
                "area": rnd(poly.area),
                "material": poly.material_index,
            })
        out = {
            "object": obj.name,
            "space": space,
            "evaluated": evaluated,
            "counts": mesh_counts(mesh),
            "faces": faces,
            "truncated": len(polygons) > limit,
        }
        if picked is not None:
            out["material_faces"] = len(polygons)
        if with_vertices:
            out["vertices"] = [vec(to_space @ v.co) for v in mesh.vertices]
        return out
    finally:
        if holder is not None:
            holder.to_mesh_clear()
