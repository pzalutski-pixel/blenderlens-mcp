"""Run check_asset: the checks every game asset should pass, and a project's own rules.

The built-in checks cover topology, transforms, UVs, textures, and what the glTF export will drop or
exceed. A project's own rules are evaluated by addon/rules.py, which the server's check command also uses
for .glb files.

Everything here only reads, and nothing changes the file. The objects checked are the ones export_glb
writes for the same ``objects`` parameter: the visible ones, without cameras and lights, and the objects
that the collection instances among them show. An object named in ``objects`` is checked even when it is
hidden, a camera or a light, since it was asked for. Geometry is checked as it will be exported, after
modifiers, unless ``evaluated`` is false. Curves, surfaces and text are checked as the mesh they make. The
glTF export writes a metaball as a node with no mesh, so a metaball gets a surface_not_exported issue
instead.

Face indices are those of the mesh itself, which edit_mesh and set_material take, as long as the
modifiers leave the faces unchanged. Otherwise they are the evaluated mesh's, which get_mesh lists with
``evaluated`` true. Each object's ``faces_of`` field says which.
"""

import json
import os
import re

import bmesh
import bpy
from mathutils.bvhtree import BVHTree
from mathutils.geometry import area_tri

from . import collision, measure, paint
from . import rules as project_rules
from .common import (
    CommandError,
    boolean,
    descendants,
    get_object,
    jsonable,
    number,
    object_kind,
    rnd,
    sync_edit_mode,
    update,
    vec,
)
from .uv import overlapping

#: The most face indices an issue lists. The issue's ``count`` says how many faces it concerns.
FACES_LISTED = 32
#: The most objects the result lists, those with errors first and then those with warnings. The error and
#: warning counts cover them all.
OBJECTS_LISTED = 100
#: The most hidden objects the result names as left out of the check.
HIDDEN_LISTED = 32
#: The most object pairs the overlap check lists.
PAIRS_LISTED = 32
# Past the next three sizes a check is skipped and reported as not checked, because its work grows faster
# than the mesh does.
#: The most faces a mesh may have for the self_intersecting check to run.
SELF_INTERSECTION_MAX_FACES = 200_000
#: The most triangles a mesh may have for the uv_overlap check to run.
UV_OVERLAP_MAX_TRIANGLES = 100_000
#: The most meshes the overlaps check compares with each other.
OVERLAP_MAX_OBJECTS = 400
#: The most closed parts of one mesh tested for lying inside each other.
NESTING_MAX_PARTS = 1000
#: The most collection instances whose triangles are counted.
MAX_INSTANCES = 10_000
#: The distance, in metres, below which two vertices count as duplicates.
DUPLICATE_DISTANCE = 1e-5
#: The area, in square metres, below which a face counts as having no area.
ZERO_AREA = 1e-10
#: How far a UV corner may lie outside the 0 to 1 range and still count as inside it.
UV_SLACK = 1e-4
#: The object types export_glb leaves out unless they are asked for.
LEFT_OUT = ("CAMERA", "LIGHT")

#: The severity of each built-in issue. An error fails the check, and a warning is reported but passes.
SEVERITY = {
    "non_manifold": "warning", "boundary_edges": "warning", "loose_geometry": "warning",
    "zero_area_faces": "warning", "duplicate_vertices": "warning", "ngons": "warning",
    "flipped_normals": "error", "inside_out": "error", "self_intersecting": "warning",
    "unapplied_scale": "warning", "negative_scale": "warning", "origin_not_at_base": "warning",
    "no_uv": "warning", "uv_overlap": "warning", "uv_out_of_bounds": "warning",
    "texture_without_uv": "error", "absolute_texture_path": "warning", "missing_image": "error",
    "procedural_material": "warning", "morphs_dropped_by_modifiers": "warning",
    "over_budget": "error", "overlaps": "warning", "surface_not_exported": "error", "not_checked": "info",
    "hidden": "info",
}

#: Shader nodes whose type names start with ShaderNodeTex but that are not procedural textures. The glTF
#: exporter either carries them or has nothing of theirs to carry.
NOT_PROCEDURAL = {"ShaderNodeTexImage", "ShaderNodeTexEnvironment", "ShaderNodeTexCoord"}

#: Blender's suffix for a name already taken, a dot and three or more digits.
BLENDER_SUFFIX = re.compile(r"^(.+)\.\d{3,}$")


def _issue(code, count, faces=None, message=None, **extra):
    """Return an issue of the built-in checks, with its severity, its count and at most FACES_LISTED faces."""
    out = {"code": code, "severity": SEVERITY.get(code, "error"), "count": count}
    if faces is not None:
        listed = sorted(faces)
        out["faces"] = listed[:FACES_LISTED]
        if len(listed) > FACES_LISTED:
            out["truncated"] = True
    if message:
        out["message"] = message
    out.update(extra)
    return out


def _faces_of_edges(edges):
    """Return the indices of the faces that use any of ``edges``."""
    return {f.index for e in edges for f in e.link_faces}


def godot_collision_proxy(name):
    """Return whether Godot's importer reads ``name`` as collision only, as with -colonly, _convcolonly or $colonly.

    Godot's own test (_teststr in resource_importer_scene.cpp) strips trailing digits, whitespace and
    underscores first, and Godot's node names have _ where Blender's names have a dot. The server's
    glb.godot_hint holds the same test for reading a .glb file back. The add-on cannot import the server's
    code, so the test is repeated here.
    """
    what = name.replace(".", "_")
    while what and (what[-1] in "0123456789_" or ord(what[-1]) <= 32):
        what = what[:-1]
    lower = what.lower()
    return any(f"${hint}" in lower or lower.endswith(f"-{hint}") or lower.endswith(f"_{hint}")
               for hint in ("colonly", "convcolonly"))


def collision_proxy(name):
    """Return whether ``name`` marks a collision proxy, which no engine draws.

    A proxy's name carries one of Godot's collision-only hints, or ends in create_collision's plain suffix
    (collision.PLAIN_SUFFIX) before any Blender .001-style suffix. A proxy needs no UVs, and overlaps its
    object by design.
    """
    match = BLENDER_SUFFIX.match(name)
    return godot_collision_proxy(name) or (match.group(1) if match else name).endswith(collision.PLAIN_SUFFIX)


# ---------------------------------------------------------------------------
# One mesh
# ---------------------------------------------------------------------------


def _islands(bm):
    """Return the mesh's faces in groups joined across edges that two faces share, each as ``(faces, closed)``.

    A group is closed when every edge of its faces is shared by exactly two faces.
    """
    seen, out = set(), []
    for start in bm.faces:
        if start.index in seen:
            continue
        seen.add(start.index)
        stack, members, closed = [start], [], True
        while stack:
            face = stack.pop()
            members.append(face)
            for edge in face.edges:
                linked = edge.link_faces
                if len(linked) != 2:
                    closed = False
                    continue
                for other in linked:
                    if other.index not in seen:
                        seen.add(other.index)
                        stack.append(other)
        out.append((members, closed))
    return out


def _enclosed(bm, islands):
    """Return the indices, in ``islands``, of the closed parts that lie inside another closed part of the mesh.

    A part can lie inside another only when its bounding box lies within the other's. A point of the part is
    then inside the other when at least two of three rays from it cross the other part an odd number of
    times. Nothing is tested when the mesh has more than NESTING_MAX_PARTS closed parts.
    """
    closed = [k for k, (_, is_closed) in enumerate(islands) if is_closed]
    if len(closed) < 2 or len(closed) > NESTING_MAX_PARTS:
        return set()
    boxes, owner = {}, {}
    for k in closed:
        corners = [v.co for face in islands[k][0] for v in face.verts]
        boxes[k] = ([min(c[i] for c in corners) for i in range(3)], [max(c[i] for c in corners) for i in range(3)])
        for face in islands[k][0]:
            owner[face.index] = k
    tree, step, out = None, measure.step_for([v.co for v in bm.verts]), set()
    for k in closed:
        around = [t for t in closed if t != k and measure.box_within(boxes[k], boxes[t])]
        if not around:
            continue
        if tree is None:
            tree = BVHTree.FromBMesh(bm, epsilon=0.0)
        start = islands[k][0][0].calc_center_median()
        votes = dict.fromkeys(around, 0)
        for way in measure.RAYS:
            counts = {}
            for index in measure.crossings(tree, start, way, step):
                part = owner.get(index)
                if part in votes:
                    counts[part] = counts.get(part, 0) + 1
            for part in around:
                votes[part] += counts.get(part, 0) % 2
        if any(vote >= 2 for vote in votes.values()):
            out.add(k)
    return out


def _orientation(bm):
    """Return ``(flipped, inside_out)``, the indices of faces pointing against their neighbours and of inward faces.

    ``inside_out`` holds the faces of closed parts whose every face points inward. Blender's recalculation of
    normals, run on a copy, makes the normals consistent, and outward for each closed part. A closed part that
    it turns wholly around is inside out. A closed part that lies inside another closed part of the same mesh
    may face either way as a whole. The wall of a cavity faces in, as the inner shell of a solidified sphere
    does, while a part set inside another faces out. On such a part, as on an open part where outward means
    nothing, only the faces that disagree with most of their part count as flipped, so a plane facing down is
    just a plane.
    """
    duplicate = bm.copy()
    try:
        bmesh.ops.recalc_face_normals(duplicate, faces=list(duplicate.faces))
        duplicate.faces.ensure_lookup_table()
        turned = {f.index for f in bm.faces
                  if f.normal.length > 0 and duplicate.faces[f.index].normal.dot(f.normal) < 0}
    finally:
        duplicate.free()
    islands = _islands(bm)
    enclosed = _enclosed(bm, islands) if turned else set()
    flipped, inside_out = set(), set()
    for k, (members, closed) in enumerate(islands):
        mine = {f.index for f in members if f.index in turned}
        if not mine:
            continue
        real = [f for f in members if f.normal.length > 0]
        if closed and k not in enclosed:
            if len(mine) == len(real):
                inside_out |= mine
            else:
                flipped |= mine
        elif len(mine) * 2 < len(real):
            flipped |= mine
        elif len(mine) * 2 > len(real):
            flipped |= {f.index for f in real} - mine
    return flipped, inside_out


def _self_intersections(bm):
    """Return the pairs of face indices whose faces pass through each other and share no vertex."""
    tree = BVHTree.FromBMesh(bm, epsilon=0.0)
    corners = [{v.index for v in f.verts} for f in bm.faces]
    pairs = [(i, j) for i, j in tree.overlap(tree) if i < j and not (corners[i] & corners[j])]
    return pairs


def _uv_overlaps(bm, layer):
    """Return ``(faces, complete)``, the faces whose UV triangles overlap another face's and whether all were tested.

    ``BVHTree.overlap`` misses coplanar triangles, and all UV triangles lie in one plane, so they are tested in
    2D with uv.overlapping over a grid, within that function's limits on work. Each triangle is shrunk a
    little toward its centre, so neighbours that only share an edge do not count, and triangles with no area
    are left out.
    """
    triangles = []
    for loops in bm.calc_loop_triangles():
        points = [loop[layer].uv.copy() for loop in loops]
        centre = (points[0] + points[1] + points[2]) / 3
        points = [centre + (p - centre) * (1 - 1e-4) for p in points]
        if area_tri(*points) > 1e-12:
            triangles.append((loops[0].face.index, points))
    return overlapping(triangles)


def material_images(material):
    """Return a material's image texture nodes and procedural texture nodes, among those whose output is linked.

    A node counts when any of its outputs is linked to another node, whether or not that link reaches the
    material's output.
    """
    tree = material.node_tree if material is not None else None
    if tree is None:
        return [], []
    images, procedural = [], []
    for node in tree.nodes:
        if not any(output.is_linked for output in node.outputs):
            continue
        if node.bl_idname == "ShaderNodeTexImage":
            images.append(node)
        elif node.bl_idname.startswith("ShaderNodeTex") and node.bl_idname not in NOT_PROCEDURAL:
            procedural.append(node)
    return images, procedural


def _image_file_problems(image):
    """Return "missing" or "absolute" for an image file the exporter cannot read or reaches by an absolute path.

    Return None for an image that is fine, and for one that is packed or not loaded from a file, since the
    exporter needs nothing from disk for it. A missing image datablock also counts as "missing".
    """
    if image is None:
        return "missing"
    if image.source not in ("FILE", "SEQUENCE", "TILED") or image.packed_file is not None:
        return None
    path = image.filepath
    if not path:
        return "missing"
    if not os.path.exists(bpy.path.abspath(path, library=image.library)):
        return "missing"
    return None if path.startswith("//") else "absolute"


def _material_issues(obj, bm, has_uv):
    """Return the issues of the materials on ``obj``'s faces: textures without UVs, missing images and the like."""
    out = []
    by_slot = {}
    for face in bm.faces:
        by_slot.setdefault(face.material_index, set()).add(face.index)
    for slot, faces in sorted(by_slot.items()):
        if slot >= len(obj.material_slots):
            continue
        material = obj.material_slots[slot].material
        images, procedural = material_images(material)
        if images and not has_uv:
            out.append(_issue("texture_without_uv", len(faces), faces,
                              f"'{material.name}' is textured, but the mesh has no UV map, so the texture has "
                              "nowhere to go.", material=material.name))
        for kind in ("missing", "absolute"):
            hit = [n.image.name if n.image else f"(node {n.name})" for n in images
                   if _image_file_problems(n.image) == kind]
            if hit:
                code = "missing_image" if kind == "missing" else "absolute_texture_path"
                words = (f"uses {', '.join(hit)}, which cannot be read from disk" if kind == "missing" else
                         f"reads {', '.join(hit)} by an absolute path, which breaks when the project moves")
                out.append(_issue(code, len(faces), faces, f"'{material.name}' {words}.",
                                  material=material.name, images=hit))
        if procedural:
            out.append(_issue("procedural_material", len(faces), faces,
                              f"'{material.name}' uses {', '.join(sorted({n.bl_idname[13:] for n in procedural}))} "
                              "textures, which glTF cannot carry: it exports as a flat colour. Bake it to an image.",
                              material=material.name))
    return out


def _mesh_issues(obj, bm, matrix, proxy=False):
    """Return the built-in checks' issues for one mesh, given in world space with the object's own face orientation.

    A collision proxy (``proxy`` true) is not required to have UVs.
    """
    out = []
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    more = [e for e in bm.edges if len(e.link_faces) > 2]
    if more:
        faces = _faces_of_edges(more)
        out.append(_issue("non_manifold", len(faces), faces, edges=len(more)))
    rim = [e for e in bm.edges if len(e.link_faces) == 1]
    if rim:
        faces = _faces_of_edges(rim)
        out.append(_issue("boundary_edges", len(faces), faces, edges=len(rim)))
    loose_verts = [v for v in bm.verts if not v.link_edges]
    wire = [e for e in bm.edges if not e.link_faces]
    if loose_verts or wire:
        out.append(_issue("loose_geometry", len(loose_verts) + len(wire), vertices=len(loose_verts), edges=len(wire)))
    flat = {f.index for f in bm.faces if f.calc_area() < ZERO_AREA}
    if flat:
        out.append(_issue("zero_area_faces", len(flat), flat))
    doubles = bmesh.ops.find_doubles(bm, verts=list(bm.verts), dist=DUPLICATE_DISTANCE)["targetmap"]
    if doubles:
        faces = {f.index for pair in doubles.items() for v in pair for f in v.link_faces}
        out.append(_issue("duplicate_vertices", len(faces), faces, vertices=len(doubles)))
    ngons = {f.index for f in bm.faces if len(f.verts) > 4}
    if ngons:
        out.append(_issue("ngons", len(ngons), ngons))

    flipped, inside_out = _orientation(bm)
    if inside_out:
        out.append(_issue("inside_out", len(inside_out), inside_out,
                          "These closed parts have every face pointing inward. edit_mesh with flip_normals turns "
                          "them outward."))
    if flipped:
        out.append(_issue("flipped_normals", len(flipped), flipped,
                          "These faces point against their neighbours. edit_mesh with flip_normals on these faces "
                          "turns them back."))

    if len(bm.faces) <= SELF_INTERSECTION_MAX_FACES:
        pairs = _self_intersections(bm)
        if pairs:
            faces = {i for pair in pairs for i in pair}
            out.append(_issue("self_intersecting", len(faces), faces, pairs=len(pairs)))
    else:
        out.append(_issue("not_checked", 1, message=f"self_intersecting was not checked: the mesh has "
                                                     f"{len(bm.faces):,} faces, more than the "
                                                     f"{SELF_INTERSECTION_MAX_FACES:,} this check handles."))

    scale = obj.scale
    if any(abs(s - 1) > 1e-4 for s in scale):
        out.append(_issue("unapplied_scale", 1, scale=vec(scale),
                          message="The object's scale is not 1, so modifiers such as bevel and solidify measure "
                                  "their widths before the scale, and an uneven scale stretches what they make. "
                                  "apply_transform bakes the scale into the mesh."))
    if matrix.determinant() < 0:
        out.append(_issue("negative_scale", 1, message="A mirroring (negative) scale turns faces inside out in "
                                                       "engines that do not correct for it. apply_transform bakes "
                                                       "the scale into the mesh."))

    layer = bm.loops.layers.uv.active
    uv_issues_from = len(out)
    if layer is None:
        if not proxy:
            out.append(_issue("no_uv", 1, message="The mesh has no UV map, which textures, baking and lightmaps "
                                                  "need."))
    else:
        triangles = sum(len(f.verts) - 2 for f in bm.faces)
        if triangles <= UV_OVERLAP_MAX_TRIANGLES:
            stacked, complete = _uv_overlaps(bm, layer)
            if stacked:
                out.append(_issue("uv_overlap", len(stacked), stacked,
                                  "UV islands on top of each other share texels. That is fine for mirrored detail, "
                                  "but wrong for baking and lightmaps."))
            if not complete:
                out.append(_issue("not_checked", 1, message="uv_overlap was not fully checked: so many faces share "
                                                             "the same UV space that not every pair was tested. "
                                                             "The faces listed are those found."))
        else:
            out.append(_issue("not_checked", 1, message=f"uv_overlap was not checked: the mesh has {triangles:,} "
                                                         f"triangles, more than the {UV_OVERLAP_MAX_TRIANGLES:,} "
                                                         "this check handles."))
        outside = {f.index for f in bm.faces for loop in f.loops
                   if not all(-UV_SLACK <= c <= 1 + UV_SLACK for c in loop[layer].uv)}
        if outside:
            have = "face has" if len(outside) == 1 else "faces have"
            out.append(_issue("uv_out_of_bounds", len(outside), outside,
                              message=f"{len(outside):,} {have} UVs outside 0 to 1, where a texture repeats or "
                                      "clamps. unwrap_uv lays them out inside that range."))
    if not any(slot.material is not None and material_images(slot.material)[0] for slot in obj.material_slots):
        # The materials give plain colours and map no image through these UVs, so how the UVs lie shows nowhere
        # until the mesh is textured or baked. Their issues are lowered to info.
        for issue in out[uv_issues_from:]:
            if issue["code"] in ("uv_overlap", "uv_out_of_bounds"):
                issue["severity"] = "info"
                issue["message"] = (issue.get("message", "") + " No material maps an image through these UVs, so "
                                    "this matters only once one does, or for baking.").strip()
    out.extend(_material_issues(obj, bm, layer is not None))
    return out


def _world_bmesh(mesh, matrix):
    """Return a BMesh of ``mesh`` moved by ``matrix`` into world space, its faces in the order of the polygons.

    A mirroring transform reverses the winding as it moves the points, so the faces are turned back. The
    inside-out check judges the mesh's own orientation, and negative scale is reported as an issue of its
    own. The caller frees the BMesh.
    """
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.transform(matrix)
    if matrix.determinant() < 0:
        bmesh.ops.reverse_faces(bm, faces=list(bm.faces))
    bm.normal_update()
    return bm


def _morphs_dropped(obj):
    """Return the morphs_dropped_by_modifiers issue when exporting with modifiers applied would drop shape keys."""
    keys = obj.data.shape_keys
    if keys is None or len(keys.key_blocks) < 2:
        return None
    # The exporter writes an Armature modifier as a skin rather than applying it, so an object whose only
    # modifiers are Armature modifiers keeps its morph targets.
    if not any(m.show_viewport and m.type != "ARMATURE" for m in obj.modifiers):
        return None
    return _issue("morphs_dropped_by_modifiers", len(keys.key_blocks) - 1, shape_keys=len(keys.key_blocks) - 1,
                  message="Exporting with modifiers applied drops shape keys, so the file gets no morph targets. "
                          "Blender cannot apply modifiers to a mesh with shape keys: remove the modifiers, or export "
                          "with apply_modifiers false.")


def _surface_not_exported(obj):
    """Return the surface_not_exported issue for an object whose surface the glTF export does not write."""
    return _issue("surface_not_exported", 1,
                  message=f"The glTF export writes {object_kind(obj)} as a node with no mesh, so its surface is not "
                          "in the file. Convert it to a mesh first: Object > Convert > Mesh in Blender, or "
                          "bpy.ops.object.convert(target='MESH') through run_python.")


def _check_mesh(obj, mesh, matrix, row, entry, key, trees):
    """Check ``mesh``, the mesh of ``obj`` moved by ``matrix``, and return its triangle count.

    The issues and statistics go into ``row``, the bounds and triangle count into the rules description's
    ``entry``, and the BVH tree and the other data the overlap checks need into ``trees`` under ``key``.
    """
    bm = _world_bmesh(mesh, matrix)
    try:
        proxy = collision_proxy(obj.name)
        row["issues"] = row["issues"] + _mesh_issues(obj, bm, matrix, proxy)
        triangles = sum(len(f.verts) - 2 for f in bm.faces)
        size = [0.0, 0.0, 0.0]
        if bm.verts:
            lo = [min(v.co[i] for v in bm.verts) for i in range(3)]
            hi = [max(v.co[i] for v in bm.verts) for i in range(3)]
            entry["bounds"] = [lo, hi]
            size = [rnd(h - low) for low, h in zip(lo, hi, strict=True)]
        row["stats"] = {"vertices": len(bm.verts), "faces": len(bm.faces), "triangles": triangles, "size": size,
                        "uv_maps": len(bm.loops.layers.uv)}
        entry["triangles"] = triangles
        if bm.faces:
            closed = all(len(e.link_faces) == 2 for e in bm.edges)
            points = [v.co.copy() for v in bm.verts]
            trees[key] = {"tree": BVHTree.FromBMesh(bm, epsilon=0.0), "box": entry["bounds"], "closed": closed,
                          "point": points[0], "step": measure.step_for(points), "proxy": proxy,
                          "points": points, "polygons": [tuple(v.index for v in f.verts) for f in bm.faces]}
        return triangles
    finally:
        bm.free()


# ---------------------------------------------------------------------------
# The asset
# ---------------------------------------------------------------------------


def _visible(obj):
    """Return whether ``obj`` is visible, and False when it is not in this view layer."""
    try:
        return obj.visible_get()
    except RuntimeError:  # The object is not in this view layer.
        return False


def _chosen(params):
    """Return the objects to check, the hidden objects left out, and the hidden objects asked for by name.

    The objects are chosen as export_glb chooses them by default: the whole scene, or the objects named in
    ``objects`` with their descendants, visible ones only, and no cameras or lights. A named object is
    checked even when it is hidden or a camera, since it was asked for. The two hidden lists hold names.
    """
    names = params.get("objects")
    named = []
    if names is None:
        candidates = list(bpy.context.scene.objects)
    else:
        if isinstance(names, str):
            names = [names]
        if not isinstance(names, list):
            raise CommandError("bad_argument", f"'objects' must be a list of object names, got {names!r}.")
        named = [get_object(n, "objects") for n in names]
        candidates = []
        for obj in named:
            for candidate in [obj, *descendants(obj)]:
                if candidate not in candidates:
                    candidates.append(candidate)
    chosen, hidden = [], []
    for obj in candidates:
        if obj in named or (obj.type not in LEFT_OUT and _visible(obj)):
            chosen.append(obj)
        elif obj.type not in LEFT_OUT:
            hidden.append(obj.name)
    return chosen, hidden, {obj.name for obj in named if not _visible(obj)}


def _material_values(material):
    """Return a material's values as the rules compare them: linear colours, and emission as colour times strength.

    Without a Principled BSDF node, the material's viewport colour, metallic and roughness are used, with no
    emission. A value whose socket is linked to another node is None.
    """
    node = paint.principled(material)
    if node is None:
        return {"name": material.name, "base_color": list(material.diffuse_color)[:3],
                "metallic": material.metallic, "roughness": material.roughness, "emission": [0.0, 0.0, 0.0]}

    def value(name):
        """Return the input's value, or None when the node has no such input or another node feeds it."""
        socket = node.inputs.get(name)
        if socket is None:
            return None
        return None if socket.is_linked else socket.default_value

    base = value("Base Color")
    emission, strength = value("Emission Color"), value("Emission Strength")
    return {
        "name": material.name,
        "base_color": list(base)[:3] if base is not None else None,
        "metallic": value("Metallic"),
        "roughness": value("Roughness"),
        "emission": [c * strength for c in list(emission)[:3]] if emission is not None and strength is not None
        else None,
    }


def _entry(obj, name, matrix, parent):
    """Return the rules' description of one object, without its bounds, triangles and materials yet."""
    return {"name": name, "parent": parent, "type": obj.type, "triangles": 0,
            "extras": {k: jsonable(obj[k]) for k in obj.keys() if not k.startswith("_")},
            "bounds": None, "origin": [float(c) for c in matrix.translation], "materials": []}


def _instances(objects, depsgraph, trees):
    """Check what the collection instances among ``objects`` show, and return ``(rows, entries, triangles, complete)``.

    Each object with a surface that an instance shows is checked once per instancing object, as evaluated and
    where the depsgraph places it, nested instances included, and its triangles count once per instance, as
    the export writes each one. A metaball gets the surface_not_exported issue instead. ``complete`` is False
    when there are more than MAX_INSTANCES instances, and the rest are not counted.
    """
    holders = {o.name for o in objects if o.instance_type == "COLLECTION" and o.instance_collection is not None}
    rows, entries, total, counted, triangles_of = [], [], 0, 0, {}
    if not holders:
        return rows, entries, total, True
    for instance in depsgraph.object_instances:
        parent = instance.parent.original if instance.is_instance and instance.parent is not None else None
        if parent is None or parent.name not in holders or instance.object.type not in measure.GEOMETRY:
            continue
        counted += 1
        if counted > MAX_INSTANCES:
            return rows, entries, total, False
        obj = instance.object.original
        key = f"{parent.name}/{obj.name}"
        if key not in triangles_of:
            matrix = instance.matrix_world.copy()
            row = {"name": obj.name, "type": obj.type, "instanced_by": parent.name, "issues": []}
            entry = {**_entry(obj, obj.name, matrix, parent.name), "id": key, "parent_id": parent.name}
            if obj.type in measure.EXPORTED_GEOMETRY:
                mesh = instance.object.to_mesh()
                try:
                    same = obj.type == "MESH" and len(mesh.polygons) == len(obj.data.polygons) and \
                        len(mesh.vertices) == len(obj.data.vertices)
                    row["faces_of"] = "mesh" if same else "evaluated"
                    triangles_of[key] = _check_mesh(obj, mesh, matrix, row, entry, key, trees)
                finally:
                    instance.object.to_mesh_clear()
                entry["materials"] = [s.material.name for s in obj.material_slots if s.material]
            else:
                row["issues"].append(_surface_not_exported(obj))
                triangles_of[key] = 0
            rows.append(row)
            entries.append(entry)
        total += triangles_of[key]
    return rows, entries, total, True


#: The issues of the mesh itself, its topology, UVs and materials, which are the same for every object sharing it.
MESH_CODES = ("non_manifold", "boundary_edges", "loose_geometry", "zero_area_faces", "duplicate_vertices", "ngons",
              "flipped_normals", "inside_out", "self_intersecting", "no_uv", "uv_overlap", "uv_out_of_bounds",
              "texture_without_uv", "missing_image", "absolute_texture_path", "procedural_material")


def _collapse_shared(rows, objects):
    """Report a mesh's own issues once when linked duplicates share the mesh.

    Each such issue stays on the first object, with ``also`` naming the others. An issue that another object
    finds differently, because its scale or its modifiers change what is found, stays with that object.
    """
    first = {}
    for row, obj in zip(rows, objects, strict=True):
        if obj.type != "MESH":
            continue
        owner = first.setdefault(obj.data.as_pointer(), row)
        if owner is row or owner.get("faces_of") != row.get("faces_of"):
            continue
        kept = []
        for issue in row["issues"]:
            same = None
            if issue["code"] in MESH_CODES:
                same = next((i for i in owner["issues"] if {k: v for k, v in i.items() if k != "also"} == issue),
                            None)
            if same is None:
                kept.append(issue)
            else:
                same.setdefault("also", []).append(row["name"])
        row["issues"] = kept


def _rank(row):
    """Return 0 for an object with errors, 1 for one with warnings only, and 2 for the rest, for sorting."""
    severities = {issue["severity"] for issue in row["issues"]}
    return 0 if "error" in severities else 1 if "warning" in severities else 2


#: The hint for rules that do not fit the format.
RULES_HINT = "The guide resource (blenderlens://guide) lists the rules' keys, with an example."


def _rules(params):
    """Return the validated rule set from ``rules`` or from the JSON file at ``rules_file``, or None without either."""
    given, path = params.get("rules"), params.get("rules_file")
    if given is not None and path is not None:
        raise CommandError("bad_argument", "'rules' and 'rules_file' were both given; give only one.",
                           "rules_file reads the same JSON object from a file.")
    where = "The rules"
    if path is not None:
        if not isinstance(path, str) or not path:
            raise CommandError("bad_argument", f"'rules_file' must be a path to a JSON file, got {path!r}.")
        if not os.path.isfile(path):
            raise CommandError("not_found", f"No rules file at {path}.",
                               "Check the path. A relative path is resolved against the MCP server's current "
                               "directory.")
        try:
            with open(path, encoding="utf-8") as handle:
                given = json.load(handle)
        except (OSError, UnicodeDecodeError) as exc:
            raise CommandError("bad_argument", f"Cannot read the rules file {path}: {exc}.") from None
        except json.JSONDecodeError as exc:
            raise CommandError("bad_argument", f"The rules file {path} is not JSON: {exc.msg}, line {exc.lineno}.",
                               RULES_HINT) from None
        where = f"The rules file {path}"
    if given is None:
        return None
    try:
        return project_rules.validate(given)
    except project_rules.RulesError as exc:
        raise CommandError("bad_argument", f"{where} cannot be used. {exc}", RULES_HINT) from None


def check_asset(params):
    """Check the objects as export_glb would write them, against the built-in checks and the project's rules."""
    objects, hidden, named_hidden = _chosen(params)
    evaluated = boolean(params, "evaluated", True)
    budget = number(params, "budget", None, 0, 1e9, whole=True)
    rules = _rules(params)
    sync_edit_mode()
    update()
    depsgraph = bpy.context.evaluated_depsgraph_get()

    rows, described, trees, total = [], [], {}, 0
    for obj in objects:
        row = {"name": obj.name, "type": obj.type, "issues": []}
        entry = _entry(obj, obj.name, obj.matrix_world, obj.parent.name if obj.parent else None)
        if obj.name in named_hidden:
            row["issues"].append(_issue("hidden", 1, message="The object is hidden, so export_glb leaves it out "
                                                             "unless visible_only is false."))
        if obj.type in measure.EXPORTED_GEOMETRY:
            # Only a mesh can be read without evaluating it. The other kinds are always read as the mesh they
            # make.
            holder = obj.evaluated_get(depsgraph) if evaluated or obj.type != "MESH" else None
            try:
                mesh = holder.to_mesh() if holder is not None else obj.data
            except RuntimeError:
                mesh = None
            if mesh is not None:
                try:
                    same = obj.type == "MESH" and len(mesh.polygons) == len(obj.data.polygons) and \
                        len(mesh.vertices) == len(obj.data.vertices)
                    row["faces_of"] = "mesh" if same else "evaluated"
                    total += _check_mesh(obj, mesh, obj.matrix_world, row, entry, obj.name, trees)
                finally:
                    if holder is not None:
                        holder.to_mesh_clear()
            if obj.type == "MESH":
                dropped = _morphs_dropped(obj)
                if dropped:
                    row["issues"].append(dropped)
            entry["materials"] = [s.material.name for s in obj.material_slots if s.material]
        elif obj.type in measure.GEOMETRY:
            row["issues"].append(_surface_not_exported(obj))
        rows.append(row)
        described.append(entry)
    _collapse_shared(rows, objects)
    instance_rows, instance_entries, instanced, complete = _instances(objects, depsgraph, trees)
    rows += instance_rows
    described += instance_entries
    total += instanced

    asset_issues = []
    if not complete:
        asset_issues.append(_issue("not_checked", 1, message="Collection instances were not all checked: there are "
                                                             f"more than {MAX_INSTANCES:,}, and the rest are not "
                                                             "counted."))
    names = {o.name for o in objects}
    origin_rule = rules is not None and bool(rules.get("origin_at_base"))
    if not origin_rule:
        # A root object's origin is the point a game places it by. When the rules include origin_at_base, that
        # rule decides which objects are checked, so this built-in check runs only when the rule is absent.
        tree = project_rules.Tree({"objects": described})
        for row, entry, obj in zip(rows[:len(objects)], described[:len(objects)], objects, strict=True):
            if entry["parent"] not in names and obj.type == "MESH":
                box = tree.bounds(entry)
                if box is not None and not project_rules.origin_at_base(entry["origin"], box):
                    base = vec([(box[0][0] + box[1][0]) / 2, (box[0][1] + box[1][1]) / 2, box[0][2]])
                    origin = vec(entry["origin"])
                    row["issues"].append(_issue(
                        "origin_not_at_base", 1, origin=origin, base=base,
                        message=f"The origin is at {origin}, not on the bottom of the bounds at {base}, so the asset "
                                "will not stand on the ground where it is placed. apply_transform with origin "
                                "bottom_center moves it there."))
    if budget is not None and total > budget:
        asset_issues.append(_issue("over_budget", 1, message=f"The asset has {total:,} triangles in all, more than "
                                                             f"the budget of {budget:,}.",
                                   objects=["all"], triangles={"all": total}))
    asset_issues.extend(_overlaps(trees))

    if rules is not None:
        materials = {}
        for obj in objects:
            for slot in getattr(obj, "material_slots", []):
                if slot.material is not None:
                    materials[slot.material.name] = slot.material
        description = {"objects": described, "materials": [_material_values(m) for m in materials.values()]}

        def overlap(a, b):
            """Return how two objects meet, as rules.evaluate asks, or None when either has no mesh."""
            if a not in trees or b not in trees:
                return None
            return _meet(a, b, trees)

        asset_issues.extend(project_rules.evaluate(rules, description, overlap,
                                                   narrowed=params.get("objects") is not None))

    every = [i for row in rows for i in row["issues"]] + asset_issues
    errors = sum(1 for i in every if i["severity"] == "error")
    warnings = sum(1 for i in every if i["severity"] == "warning")
    listed = sorted(rows, key=_rank)
    out = {
        "passed": errors == 0,
        "evaluated": evaluated,
        "counts": {"errors": errors, "warnings": warnings},
        "triangles": total,
        "objects": listed[:OBJECTS_LISTED],
        "issues": asset_issues,
    }
    if len(rows) > OBJECTS_LISTED:
        out["objects_not_listed"] = len(rows) - OBJECTS_LISTED
    if hidden:
        out["hidden"] = hidden[:HIDDEN_LISTED]
        if len(hidden) > HIDDEN_LISTED:
            out["hidden_not_listed"] = len(hidden) - HIDDEN_LISTED
    return out


def _boxes_meet(a, b, slack=1e-6):
    """Return whether two boxes, each ``(lo, hi)``, touch or overlap, give or take ``slack``."""
    return all(a[0][i] <= b[1][i] + slack and b[0][i] <= a[1][i] + slack for i in range(3))


def _inner(a, b, trees):
    """Return the key of whichever of two meshes lies wholly inside the other, or None.

    Only a closed mesh has an inside, so only a closed mesh can hold the other.
    """
    for inner, outer in ((a, b), (b, a)):
        holder = trees[outer]
        if holder["closed"] and measure.box_within(trees[inner]["box"], holder["box"]) \
                and measure.inside(holder["tree"], trees[inner]["point"], holder["step"]):
            return inner
    return None


def _crossing(a, b, trees, first=False):
    """Return the face pairs of two meshes that pass through each other, found with measure.crossing_faces.

    Faces that only touch, as where one part rests on another, are not among them. With ``first``, the search
    stops at the first crossing pair.
    """
    hits = trees[a]["tree"].overlap(trees[b]["tree"])
    if not hits:
        return []
    return measure.crossing_faces(hits, trees[a]["points"], trees[a]["polygons"], trees[b]["points"],
                                  trees[b]["polygons"], first=first)[0]


def _meet(a, b, trees):
    """Return True when two meshes' surfaces cross, the inner one's key when one lies inside the other, else False."""
    if not _boxes_meet(trees[a]["box"], trees[b]["box"]):
        return False
    if _crossing(a, b, trees, first=True):
        return True
    return _inner(a, b, trees) or False


def _overlaps(trees):
    """Return a list holding the overlaps issue for meshes that cross or lie inside another, or an empty list.

    Pairs are found by their bounding boxes first, and then by the faces themselves. Meshes that only touch,
    as when one rests on another, are not reported. Collision proxies, which overlap their objects by design,
    are left out. With more than OVERLAP_MAX_OBJECTS meshes, a not_checked issue is returned instead.
    """
    keys = [key for key, held in trees.items() if not held["proxy"]]
    if len(keys) > OVERLAP_MAX_OBJECTS:
        return [_issue("not_checked", 1, message=f"overlaps was not checked: there are {len(keys):,} meshes, more "
                                                  f"than the {OVERLAP_MAX_OBJECTS} this check compares.")]
    pairs, faces, inside = [], {}, []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if not _boxes_meet(trees[a]["box"], trees[b]["box"]):
                continue
            hits = _crossing(a, b, trees)
            if hits:
                pairs.append([a, b])
                faces[f"{a}|{b}"] = [len({x for x, _ in hits}), len({y for _, y in hits})]
                continue
            inner = _inner(a, b, trees)
            if inner is not None:
                inside.append([inner, b if inner == a else a])
    if not pairs and not inside:
        return []
    out = _issue("overlaps", len(pairs) + len(inside), pairs=pairs[:PAIRS_LISTED],
                 faces_crossing={key: value for key, value in list(faces.items())[:PAIRS_LISTED]},
                 message="These objects' surfaces cross (listed under pairs), or one lies wholly inside another "
                         "(listed under inside, inner first). Parts set into each other on purpose are fine; "
                         "measure_objects gives the faces and the depth.")
    if inside:
        out["inside"] = inside[:PAIRS_LISTED]
    return [out]
