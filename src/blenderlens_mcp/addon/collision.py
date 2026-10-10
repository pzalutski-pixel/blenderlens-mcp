"""Make collision proxies: simple meshes that stand in for an object in a game engine's physics.

The proxy is built from the object's mesh after modifiers, as export sees it, in the object's own space,
and parented to the object with no transform of its own, so it sits exactly over the object and follows
it. It is drawn as wire, so the object shows through, and left out of renders, so previews show the
object alone. It is never hidden, because export_glb's visible_only option leaves hidden objects out of
the file.

With Godot naming, the default, the proxy's name carries the suffix that the Godot engine's importer
reads: -colonly makes a static body with a collision shape built from the mesh (a box, or the decimated
mesh), and -convcolonly one with a convex shape. Either way the mesh itself is not drawn. export_glb
with target threejs leaves such proxies out.

Every proxy carries the custom property blenderlens_collision, holding its shape, which the export
writes as the node's extras. In the file it tells a collision name given on purpose from one that reads
as a hint by accident.
"""

import bmesh
import bpy
from mathutils import Vector

from .common import (
    CommandError,
    Rollback,
    get_mesh_object,
    number,
    require,
    rnd,
    sync_edit_mode,
    text,
    update,
    vec,
)
from .motion import godot_hint, godot_node_name

#: The shapes a proxy can take.
SHAPES = ("box", "convex", "decimated")
#: The ways a proxy can be named: with Godot's import hint, or with PLAIN_SUFFIX.
NAMINGS = ("godot", "plain")
#: The import hint suffix Godot reads for each shape: a box or a decimated mesh becomes a trimesh shape, and a
#: hull a convex shape.
GODOT_SUFFIXES = {"box": "-colonly", "decimated": "-colonly", "convex": "-convcolonly"}
#: The suffix of a proxy named for no engine in particular.
PLAIN_SUFFIX = "_collision"
#: The share of faces a decimated proxy keeps when no ratio is given.
DEFAULT_RATIO = 0.25
#: The smallest share of faces a decimated proxy may keep.
MIN_RATIO = 0.01
#: The most vertices a source may have after its modifiers. A hull or a decimation of more would keep Blender
#: busy for a long time.
MAX_SOURCE_VERTICES = 2_000_000
#: The custom property every proxy carries, holding its shape. It is exported as the node's extras.
MARK = "blenderlens_collision"
#: Godot's import hints that make a node collision only, with no mesh drawn.
COLLISION_ONLY_HINTS = ("colonly", "convcolonly")


def _refuse_a_proxy(obj):
    """Raise a CommandError when ``obj`` is itself a collision proxy.

    A proxy stands in for an object, so a proxy of a proxy would be a second collision shape of the
    first object. An object counts as a proxy when it carries MARK, or when its name, as Godot reads it,
    has a collision-only hint.
    """
    name = godot_node_name(obj.name)
    if obj.get(MARK) is None and not any(godot_hint(name, hint) for hint in COLLISION_ONLY_HINTS):
        return
    where = f"Make it from '{obj.parent.name}', the object it stands for." if obj.parent is not None else \
        "Make the proxy from the object it stands for."
    raise CommandError("bad_argument", f"'{obj.name}' is a collision proxy itself.", where)


def _source_bmesh(obj):
    """Return the object's mesh after modifiers, in its own space, as a BMesh the caller frees.

    Raise a CommandError past MAX_SOURCE_VERTICES vertices, counted after modifiers, since an Array
    modifier on a small mesh may make millions.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    holder = obj.evaluated_get(depsgraph)
    vertices = len(holder.data.vertices)
    if vertices > MAX_SOURCE_VERTICES:
        raise CommandError("bad_argument", f"'{obj.name}' has {vertices:,} vertices after its modifiers, more than "
                                           f"the {MAX_SOURCE_VERTICES:,} a proxy may be built from.",
                           "Build the proxy from a simpler object, or lower the modifiers' counts first.")
    mesh = holder.to_mesh()
    bm = bmesh.new()
    try:
        if mesh is not None:
            bm.from_mesh(mesh)
    finally:
        holder.to_mesh_clear()
    return bm


def _closed(bm):
    """Return whether the surface has faces and no border, every edge shared by exactly two faces."""
    return bool(bm.faces) and all(len(edge.link_faces) == 2 for edge in bm.edges)


def _box(source):
    """Return a box spanning the source's bounds, in the source's own space, as a BMesh."""
    lo = Vector([min(v.co[i] for v in source.verts) for i in range(3)])
    hi = Vector([max(v.co[i] for v in source.verts) for i in range(3)])
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for vert in bm.verts:
        vert.co = Vector([(lo[i] + hi[i]) / 2 + vert.co[i] * (hi[i] - lo[i]) for i in range(3)])
    return bm


def _hull(source, name):
    """Return the convex hull of the source's vertices as a BMesh.

    Raise a CommandError, naming the object ``name``, when the vertices are flat or on a line, so the
    hull has no volume.
    """
    bm = bmesh.new()
    for vert in source.verts:
        bm.verts.new(vert.co)
    result = bmesh.ops.convex_hull(bm, input=list(bm.verts), use_existing_faces=False)
    # Vertices inside the hull, or left unused by it, are not part of its surface, so they are deleted.
    dropped = {v for v in [*result["geom_interior"], *result["geom_unused"]] if isinstance(v, bmesh.types.BMVert)}
    bmesh.ops.delete(bm, geom=list(dropped), context="VERTS")
    if not _closed(bm):
        bm.free()
        raise CommandError("bad_argument", f"'{name}' is flat or a line, so it has no convex volume.",
                           "Use shape box instead, which gives a flat object a flat box.")
    return bm


def _decimated(obj, ratio):
    """Return the object's mesh after modifiers, reduced by a Decimate modifier to ``ratio``, as a BMesh.

    The Decimate modifier, in its default collapse mode, runs on a temporary object holding a copy of
    the mesh, and the object and the copy are removed afterwards.
    """
    depsgraph = bpy.context.evaluated_depsgraph_get()
    whole = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph))
    holder = bpy.data.objects.new("BlenderLens Decimate", whole)
    bpy.context.scene.collection.objects.link(holder)
    try:
        modifier = holder.modifiers.new("Decimate", "DECIMATE")
        modifier.ratio = ratio
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated = holder.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
        finally:
            evaluated.to_mesh_clear()
        return bm
    finally:
        bpy.data.objects.remove(holder)
        bpy.data.meshes.remove(whole)


def _counts(bm):
    """Return the vertex, edge, face and triangle counts of a BMesh."""
    return {"vertices": len(bm.verts), "edges": len(bm.edges), "faces": len(bm.faces),
            "triangles": sum(len(face.verts) - 2 for face in bm.faces)}


def create_collision(params):
    """Make a collision proxy for a mesh object, parented to it, and return the proxy's counts and volume."""
    source = get_mesh_object(require(params, "object"))
    shape = text(params, "shape", "convex")
    if shape not in SHAPES:
        raise CommandError("bad_argument", f"'shape' takes one of {', '.join(SHAPES)}, got {shape!r}.")
    naming = text(params, "naming", "godot")
    if naming not in NAMINGS:
        raise CommandError("bad_argument", f"'naming' takes one of {', '.join(NAMINGS)}, got {naming!r}.")
    ratio = number(params, "ratio", None, MIN_RATIO, 1.0)
    if ratio is not None and shape != "decimated":
        raise CommandError("bad_argument", f"'ratio' applies only to shape decimated, and this call asks for shape "
                                           f"{shape}.",
                           "Leave ratio out, or pass shape: \"decimated\".")
    _refuse_a_proxy(source)
    # Edit mode keeps its changes apart from the mesh data until it ends, so they are copied in first, and
    # the proxy is built from the mesh as it is shown.
    sync_edit_mode()
    update()
    original = _source_bmesh(source)
    proxy_bm = None
    try:
        if not original.verts:
            raise CommandError("not_found", f"'{source.name}' has no vertices to build a proxy around.",
                               "Give the object some geometry first, or build the proxy from another object.")
        if shape == "box":
            proxy_bm = _box(original)
        elif shape == "convex":
            proxy_bm = _hull(original, source.name)
        else:
            proxy_bm = _decimated(source, ratio or DEFAULT_RATIO)
        bmesh.ops.recalc_face_normals(proxy_bm, faces=list(proxy_bm.faces))
        source_closed = _closed(original)
        source_volume = original.calc_volume(signed=False) if source_closed else None
        closed = _closed(proxy_bm)
        volume = proxy_bm.calc_volume(signed=False) if closed else None
        counts = _counts(proxy_bm)

        name = source.name + (GODOT_SUFFIXES[shape] if naming == "godot" else PLAIN_SUFFIX)
        with Rollback() as undo:
            mesh = bpy.data.meshes.new(name)
            undo.add(lambda: bpy.data.meshes.remove(mesh))
            proxy_bm.to_mesh(mesh)
            proxy = bpy.data.objects.new(name, mesh)
            undo.add(lambda: bpy.data.objects.remove(proxy))
            collections = [c for c in source.users_collection if c.library is None] or [bpy.context.scene.collection]
            for collection in collections:
                collection.objects.link(proxy)
            # With no transform of its own and an identity parent inverse, the proxy lies in the source's space.
            proxy.parent = source
            proxy.display_type = "WIRE"
            proxy.hide_render = True
            proxy[MARK] = shape
    finally:
        original.free()
        if proxy_bm is not None:
            proxy_bm.free()
    update()

    scale = abs(source.matrix_world.to_3x3().determinant())
    out = {
        "object": proxy.name,
        "source": source.name,
        "shape": shape,
        "naming": naming,
        "parent": source.name,
        "collections": [c.name for c in proxy.users_collection],
        "mesh": counts,
        "closed": closed,
        "volume": rnd(volume * scale, 6) if volume is not None else None,
        "volume_ratio": rnd(volume / source_volume, 4) if volume is not None and source_volume else None,
        "dimensions": vec(proxy.dimensions),
    }
    if shape == "decimated":
        out["ratio"] = ratio or DEFAULT_RATIO
    notes = []
    if not source_closed:
        notes.append(f"'{source.name}' is not closed, so its volume, and the ratio to it, are not known.")
    if naming == "godot" and not _uniform(source.matrix_world):
        notes.append(f"'{source.name}' is scaled unevenly or mirrored, and Godot's collision shapes do not support "
                     "that. Call apply_transform to bake the scale into the mesh, then make the proxy again.")
    if notes:
        out["note"] = " ".join(notes)
    return out


def _uniform(matrix):
    """Return whether a world matrix scales evenly on every axis, without mirroring."""
    sizes = matrix.to_scale()
    return matrix.to_3x3().determinant() > 0 and max(sizes) - min(sizes) <= 1e-6 * max(1.0, max(sizes))
