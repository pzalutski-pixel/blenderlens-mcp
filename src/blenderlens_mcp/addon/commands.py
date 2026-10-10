"""Every command the MCP server can send, by name, and the dispatcher that runs them.

The commands run inside Blender on its main thread, whether that Blender is one the user is
working in with the add-on started (live mode, ``mode == "live"``) or one started with
``--background``. In both, each call that changes something is pushed onto the undo stack as one
step named after its command, so the user and the agent can step back through what the agent did.
undo_change, which steps back, pushes no step.

A refused call changes nothing: each command checks every parameter before it changes
anything, and puts back what it did if Blender refuses partway. run_python and run_script are
the exception: when the agent's code fails, what it changed before failing stays, as an undo
step of its own.

Each handler takes the tool's arguments as tools.py describes them and returns a dict.
"""

import contextlib
import json
import math
import os
import re
import struct
import sys
import tomllib
import traceback

import bmesh
import bpy
import mathutils
from mathutils import Matrix

from . import bake, checks, collision, formats, measure, meshops, motion, paint, preview, scripts, uv
from . import undo as undo_stack
from .common import (
    EDIT_MODE_HINT,
    OUTSIDE_SCENE_HINT,
    CommandError,
    Rollback,
    apply_settings,
    article,
    boolean,
    closest,
    color,
    descendants,
    did_you_mean,
    evaluated_counts,
    finite,
    get_collection,
    get_mesh_object,
    get_object,
    in_scene,
    is_number,
    joined,
    jsonable,
    mesh_counts,
    object_kind,
    object_summary,
    quoted,
    refuse_outside_object_mode,
    require,
    resolve_settings,
    rna_settings,
    rnd,
    rotation_degrees,
    select_only,
    sync_edit_mode,
    text,
    triple,
    update,
    vec,
    world_bounds,
)

#: The version of the wire protocol between the MCP server and this add-on. It is increased whenever
#: a command's parameters or result change shape, so an outdated add-on is refused rather than misread.
PROTOCOL = 2

HERE = os.path.dirname(os.path.abspath(__file__))

#: The hint for a path that names no file.
FILE_HINT = "Check the path. A relative path is resolved against the MCP server's current directory."

#: Whether a command changed the file since it was last saved, opened or started.
#: Blender's own bpy.data.is_dirty is always true in a background Blender, even right
#: after a save, so it cannot tell this there; in live mode both are consulted.
_changed = False


def unsaved():
    """Return whether the file has changes that were not saved."""
    return _changed or (bool(bpy.data.is_dirty) and not bpy.app.background)


def mark_saved():
    """Record that the file was just saved, opened or started, so nothing is unsaved.

    An undo back to this state then leaves the file counted as saved.
    """
    global _changed
    _changed = False
    _watching(undo_stack.mark_saved)


def addon_version():
    """Return the add-on's version from its blender_manifest.toml, or "unknown" when the file cannot be read."""
    try:
        with open(os.path.join(HERE, "blender_manifest.toml"), "rb") as handle:
            return tomllib.load(handle).get("version", "unknown")
    except OSError:
        return "unknown"


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def hello(params, mode):
    """Return what the MCP server checks when it connects: the protocol, the versions, the mode and the file."""
    return {
        "protocol": PROTOCOL,
        "addon_version": addon_version(),
        "blender_version": bpy.app.version_string,
        "mode": mode,
        "background": bpy.app.background,
        "file": bpy.data.filepath or None,
        "unsaved_changes": unsaved(),
        "render_engines": preview.available_engines(),
        "commands": sorted(COMMANDS),
        "pid": os.getpid(),
    }


# ---------------------------------------------------------------------------
# Inspecting
# ---------------------------------------------------------------------------


def _collection_tree(collection, parent=None):
    """Return ``collection`` and every collection below it, each with its parent, objects and children by name."""
    out = [{
        "name": collection.name,
        "parent": parent,
        "objects": [o.name for o in collection.objects],
        "children": [c.name for c in collection.children],
    }]
    for child in collection.children:
        out.extend(_collection_tree(child, collection.name))
    return out


def get_scene(params):
    """Return the scene's objects, collections, materials, units and bounds."""
    sync_edit_mode()
    update()
    scene = bpy.context.scene
    objects = [object_summary(o) for o in scene.objects]
    for summary in objects:
        obj = scene.objects[summary["name"]]
        summary["visible"] = obj.visible_get()
        summary.pop("rotation", None)
        summary.pop("scale", None)
    bounds = world_bounds([o for o in scene.objects if o.type == "MESH"])
    return {
        "file": bpy.data.filepath or None,
        "unsaved_changes": unsaved(),
        "scene": scene.name,
        "units": {"system": scene.unit_settings.system, "scale": rnd(scene.unit_settings.scale_length),
                  "length": scene.unit_settings.length_unit},
        "frame": scene.frame_current,
        "render_engine": scene.render.engine,
        "camera": scene.camera.name if scene.camera else None,
        "objects": objects,
        "collections": _collection_tree(scene.collection),
        "materials": [m.name for m in bpy.data.materials],
        "bounds": {"min": vec(bounds[0]), "max": vec(bounds[1])} if bounds else None,
        "counts": {"objects": len(scene.objects), "meshes": len(bpy.data.meshes),
                   "materials": len(bpy.data.materials), "collections": len(bpy.data.collections)},
    }


def get_object_details(params):
    """Return one object's transform, bounds, custom properties, and for a mesh its modifiers and material slots."""
    obj = get_object(require(params, "object"))
    sync_edit_mode()
    update()
    out = object_summary(obj)
    out["rotation_mode"] = obj.rotation_mode
    out["world_location"] = vec(obj.matrix_world.translation)
    out["children"] = [c.name for c in obj.children]
    out["visible"] = obj.visible_get()
    out["hide_render"] = obj.hide_render
    out["display"] = obj.display_type
    bounds = world_bounds([obj])
    if bounds:
        out["world_bounds"] = {"min": vec(bounds[0]), "max": vec(bounds[1])}
    out["properties"] = {k: jsonable(obj[k]) for k in obj.keys() if not k.startswith("_")}
    if obj.type == "MESH":
        out["mesh_data"] = obj.data.name
        out["mesh_users"] = obj.data.users
        out["mesh_evaluated"] = evaluated_counts(obj)
        out["modifiers"] = [{"name": m.name, "type": m.type, "show_viewport": m.show_viewport,
                             "show_render": m.show_render, "settings": rna_settings(m)} for m in obj.modifiers]
        out["materials"] = [{"slot": i, "material": s.material.name if s.material else None,
                             "faces": sum(1 for p in obj.data.polygons if p.material_index == i)}
                            for i, s in enumerate(obj.material_slots)]
    elif obj.type == "EMPTY":
        out["empty_display"] = {"type": obj.empty_display_type, "size": rnd(obj.empty_display_size)}
    return out


def _material_values(material):
    """Return a material's values as list_materials reports them: its Principled BSDF's inputs, alpha and images.

    A material without a Principled BSDF node gives its viewport colour instead.
    """
    node = paint.principled(material)
    out = {"name": material.name, "users": material.users, "backface_culling": material.use_backface_culling}
    if node is None:
        out["principled"] = False
        out["viewport_color"] = vec(material.diffuse_color)
        return out

    def value(name):
        """Return the input's value, "linked" when a node feeds it, or None when the node has no such input."""
        socket = node.inputs.get(name)
        if socket is None:
            return None
        if socket.is_linked:
            return "linked"
        return jsonable(socket.default_value)

    out.update({
        "principled": True,
        "base_color": value("Base Color"),
        "metallic": value("Metallic"),
        "roughness": value("Roughness"),
        "emission_color": value("Emission Color"),
        "emission_strength": value("Emission Strength"),
        "alpha": value("Alpha"),
    })
    mode, cutoff = _alpha_state(material, node)
    out["alpha_mode"] = mode
    if mode == "clip":
        out["alpha_cutoff"] = rnd(cutoff)
    out["vertex_color"] = paint.base_color_chain(material)[0]
    out["images"] = {key: os.path.normpath(bpy.path.abspath(image.filepath, library=image.library))
                     if image.filepath else image.name for key, image in _material_images(material, node).items()}
    return out


def list_materials(params):
    """Return every material in the file with its values, images and the objects that use it."""
    users = {}
    for obj in bpy.data.objects:
        for slot in getattr(obj, "material_slots", []):
            if slot.material:
                users.setdefault(slot.material.name, []).append(obj.name)
    out = []
    for material in bpy.data.materials:
        entry = _material_values(material)
        entry["objects"] = sorted(set(users.get(material.name, [])))
        out.append(entry)
    return {"materials": out, "count": len(out)}


# ---------------------------------------------------------------------------
# Modifiers
# ---------------------------------------------------------------------------

MODIFIER_ALIASES = {
    "subdivision": "SUBSURF",
    "subdivision_surface": "SUBSURF",
    "subsurf": "SUBSURF",
    "weighted_normals": "WEIGHTED_NORMAL",
    "simple_deform": "SIMPLE_DEFORM",
    "edge_split": "EDGE_SPLIT",
}


def _modifier_type(name):
    """Return Blender's identifier for a modifier type given by name or alias, such as "subdivision" for SUBSURF."""
    types = [e.identifier for e in bpy.types.Modifier.bl_rna.properties["type"].enum_items]
    wanted = MODIFIER_ALIASES.get(str(name).lower(), str(name).upper())
    if wanted not in types:
        common = ["bevel", "array", "boolean", "subdivision", "mirror", "solidify", "weighted_normal",
                  "decimate", "triangulate", "screw", "wireframe", "displace", "smooth", "weld", "remesh"]
        raise CommandError("bad_argument", f"Unknown modifier type '{name}'.",
                           "Common types: " + ", ".join(common) + ". Any Blender modifier type name works.")
    return wanted


def _get_modifier(obj, name):
    """Return the modifier of ``obj`` named ``name``, refusing with the names of its modifiers when there is none."""
    modifier = obj.modifiers.get(name) if isinstance(name, str) else None
    if modifier is None:
        names = [m.name for m in obj.modifiers]
        near = closest(name, names)
        raise CommandError("not_found", f"'{obj.name}' has no modifier named '{name}'.",
                           did_you_mean(near) + f"Its modifiers: {', '.join(names) or 'none'}.")
    return modifier


def _modifier_report(obj, modifier):
    """Return a modifier with its settings, the object's modifier stack, and for a mesh its counts and size."""
    update()
    out = {
        "object": obj.name,
        "modifier": {"name": modifier.name, "type": modifier.type, "index": list(obj.modifiers).index(modifier),
                     "settings": rna_settings(modifier)},
        "stack": [m.name for m in obj.modifiers],
    }
    if obj.type == "MESH":
        out["mesh"] = mesh_counts(obj.data)
        out["mesh_evaluated"] = evaluated_counts(obj)
        out["dimensions"] = vec(obj.dimensions)
    return out


#: Modifier settings that multiply what a modifier makes, and the most each may be set to. One of
#: these alone can ask for millions of faces (an array count of 300,000 makes 1.8 million from a
#: cube); the estimate below covers their product with the mesh they apply to.
MODIFIER_MAXIMUMS = {
    "ARRAY": {"count": 10_000},
    "SUBSURF": {"levels": 6, "render_levels": 6},
    "SCREW": {"steps": 1024, "render_steps": 1024, "iterations": 64},
    "REMESH": {"octree_depth": 10},
    "BEVEL": {"segments": 64},
    "OCEAN": {"resolution": 32, "viewport_resolution": 32, "repeat_x": 64, "repeat_y": 64},
}


def _check_maximums(modifier, settings):
    """Refuse a multiplying setting over its maximum, naming both. Only the settings given are checked."""
    maximums = MODIFIER_MAXIMUMS.get(modifier.type, {})
    for key, value in settings:
        most = maximums.get(key)
        if most is not None and is_number(value) and value > most:
            raise CommandError("bad_argument", f"The {modifier.type} modifier's '{key}' is {value}, more than the "
                                               f"{most:,} it may be.",
                               f"Above {most:,}, this one setting alone can produce millions of faces.")


def _array_copies(modifier, size):
    """Return how many copies an array modifier makes, counting the ones a fitted length implies.

    With a fitted length and no offset between the copies, the count is infinite.
    """
    if modifier.fit_type == "FIXED_COUNT":
        return modifier.count
    step = mathutils.Vector((0.0, 0.0, 0.0))
    if modifier.use_relative_offset:
        step += mathutils.Vector([o * s for o, s in zip(modifier.relative_offset_displace, size, strict=True)])
    if modifier.use_constant_offset:
        step += mathutils.Vector(modifier.constant_offset_displace)
    length = modifier.fit_length
    if modifier.fit_type == "FIT_CURVE":
        # The curve's dimensions stand in for its length, which only evaluating the curve would give.
        length = sum(modifier.curve.dimensions) if modifier.curve is not None else 0.0
    if step.length < 1e-6:
        return 1 if modifier.use_object_offset or not length else math.inf
    return max(1, math.ceil(length / step.length))


def _estimated_faces(obj):
    """Return the faces ``obj``'s modifiers would make, estimated from their settings without evaluating them.

    Only the modifiers whose settings multiply the mesh are estimated, at the higher of their
    viewport and render settings, and whether they are shown or not. The rest pass the count on
    unchanged, and what they make is counted once the stack is evaluated. An object that is not a
    mesh gives 0.
    """
    if obj.type != "MESH":
        return 0
    faces, edges = float(len(obj.data.polygons)), float(len(obj.data.edges))
    corners = [mathutils.Vector(c) for c in obj.bound_box]
    size = [max(c[i] for c in corners) - min(c[i] for c in corners) for i in range(3)]
    for modifier in obj.modifiers:
        kind = modifier.type
        if kind == "ARRAY":
            copies = _array_copies(modifier, size)
            faces, edges = faces * copies, edges * copies
        elif kind in ("SUBSURF", "MULTIRES"):
            levels = max(getattr(modifier, name, 0) for name in ("levels", "render_levels", "sculpt_levels"))
            faces, edges = faces * 4 ** levels, edges * 4 ** levels
        elif kind == "SCREW":
            turns = max(modifier.steps, modifier.render_steps) * modifier.iterations
            faces, edges = faces * 2 + edges * turns, edges * 2 * turns
        elif kind == "BEVEL":
            faces, edges = faces + edges * modifier.segments, edges * (1 + 2 * modifier.segments)
        elif kind == "REMESH":
            if modifier.mode == "VOXEL":
                area = 2 * (size[0] * size[1] + size[1] * size[2] + size[2] * size[0])
                faces = area / max(modifier.voxel_size, 1e-9) ** 2
            else:
                faces = 6 * 4 ** modifier.octree_depth
            edges = 2 * faces
        elif kind == "OCEAN" and modifier.geometry_mode == "GENERATE":
            faces = max(modifier.resolution, modifier.viewport_resolution) ** 4 * modifier.repeat_x * modifier.repeat_y
            edges = 2 * faces
    return faces


def _bound_modifier_work(obj, modifier, settings, before, estimated):
    """Refuse a modifier change whose result would be too big.

    The settings are checked against their maximums, then the estimate against the face limit, both before
    Blender builds anything, and last the evaluated result against the face and vertex limits. ``before`` and
    ``estimated`` are the evaluated counts and the estimate from before the change: a stack already past a
    limit may still be changed, as long as the change does not make it bigger. This runs inside the caller's
    rollback, which takes the change back if this refuses.
    """
    _check_maximums(modifier, settings)
    after = _estimated_faces(obj)
    if after > meshops.MAX_FACES and after > estimated:
        raise CommandError("bad_argument", f"'{obj.name}' would have about {after:,.0f} faces with this "
                                           f"{modifier.type} modifier, more than the {meshops.MAX_FACES:,} a mesh "
                                           "may have.",
                           "Lower the count, levels or resolution, or apply the modifiers before it.")
    update()
    counts = evaluated_counts(obj)
    if counts is None:
        return
    for what, most in (("faces", meshops.MAX_FACES), ("vertices", meshops.MAX_VERTICES)):
        if counts[what] > most and counts[what] > (before or {}).get(what, 0):
            raise CommandError("bad_argument", f"'{obj.name}' had {counts[what]:,} {what} with this {modifier.type} "
                                               f"modifier, more than the {most:,} a mesh may have.",
                               "The change was taken back. Lower the modifier's settings, or use it on a lighter "
                               "mesh.")


def add_modifier(params):
    """Add a modifier to an object, with its settings, and return the modifier and the stack."""
    obj = get_object(require(params, "object"))
    kind = _modifier_type(require(params, "type"))
    # An empty name makes Blender use its own default, as its Add Modifier menu does: "Subdivision", "Bevel".
    name = text(params, "name", "")
    before, estimated = evaluated_counts(obj), _estimated_faces(obj)
    modifier = obj.modifiers.new(name=name, type=kind)
    if modifier is None:
        added = article(f"{kind} modifier")
        raise CommandError("bad_argument", f"{added[0].upper()}{added[1:]} cannot be added to {object_kind(obj)}.")
    with Rollback() as undo:
        # The settings can only be checked against the modifier itself, so it is made first and
        # taken off again if they are refused, or what they make is too big.
        undo.add(lambda: obj.modifiers.remove(modifier))
        settings = apply_settings(modifier, params.get("settings"), f"{kind} modifier")
        _bound_modifier_work(obj, modifier, settings, before, estimated)
    return _modifier_report(obj, modifier)


def _move_modifier(obj, modifier, index):
    """Move ``modifier`` to position ``index`` in the stack of ``obj``, refusing if Blender does not."""
    with bpy.context.temp_override(object=obj, active_object=obj):
        result = bpy.ops.object.modifier_move_to_index(modifier=modifier.name, index=index)
    if "FINISHED" not in result:
        raise CommandError("blender_error", f"Blender did not move '{modifier.name}' to position {index} "
                                            f"({sorted(result)}).")


def set_modifier(params):
    """Change a modifier's settings, its place in the stack or whether it shows, and return the modifier."""
    obj = get_object(require(params, "object"))
    modifier = _get_modifier(obj, require(params, "modifier"))
    index = params.get("index")
    if index is not None and (not isinstance(index, int) or isinstance(index, bool)
                              or not 0 <= index < len(obj.modifiers)):
        raise CommandError("bad_argument", f"'index' must be between 0 and {len(obj.modifiers) - 1}.")
    show = boolean(params, "show")
    before, estimated = evaluated_counts(obj), _estimated_faces(obj)
    with Rollback() as undo:
        settings = apply_settings(modifier, params.get("settings"), f"{modifier.type} modifier", undo)
        here = list(obj.modifiers).index(modifier)
        if index is not None and index != here:
            _move_modifier(obj, modifier, index)
            undo.add(lambda: _move_modifier(obj, modifier, here))
        if show is not None:
            undo.set(modifier, "show_viewport", show)
            undo.set(modifier, "show_render", show)
        _bound_modifier_work(obj, modifier, settings, before, estimated)
    return _modifier_report(obj, modifier)


def _blender_said(exc):
    """Return the words of an operator's error report, without Blender's "Error: " prefix."""
    return str(exc).strip().removeprefix("Error:").strip()


def apply_modifier(params):
    """Apply a modifier to its mesh, and return the stack and the mesh's counts."""
    obj = get_mesh_object(require(params, "object"))
    modifier = _get_modifier(obj, require(params, "modifier"))
    refuse_outside_object_mode([obj], "Blender cannot apply modifiers")
    name = modifier.name
    try:
        with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj],
                                       selected_editable_objects=[obj]):
            result = bpy.ops.object.modifier_apply(modifier=name)
    except RuntimeError as exc:
        # The operator reported an error and cancelled, changing nothing, so this is a refusal like any other.
        raise CommandError("blender_error", f"Blender did not apply '{name}': {_blender_said(exc)}",
                           "Blender cannot apply a disabled modifier, a modifier with nothing to act on, or any "
                           "modifier on a mesh with shape keys.") from None
    if "FINISHED" not in result:
        raise CommandError("blender_error", f"Blender did not apply '{name}' ({sorted(result)}).",
                           "A mesh shared by several objects cannot have modifiers applied.")
    update()
    return {"object": obj.name, "applied": name, "stack": [m.name for m in obj.modifiers],
            "mesh": mesh_counts(obj.data), "dimensions": vec(obj.dimensions)}


def remove_modifier(params):
    """Remove a modifier from an object, and return the stack left."""
    obj = get_object(require(params, "object"))
    modifier = _get_modifier(obj, require(params, "modifier"))
    name = modifier.name
    obj.modifiers.remove(modifier)
    update()
    out = {"object": obj.name, "removed": name, "stack": [m.name for m in obj.modifiers]}
    if obj.type == "MESH":
        out["mesh_evaluated"] = evaluated_counts(obj)
    return out


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------


def _number_in(params, key, low, high):
    """Return an optional number parameter, refusing one outside ``low`` to ``high``, NaN and the infinities."""
    value = params.get(key)
    if value is None:
        return None
    # The range test also refuses NaN, which compares false with everything, and the infinities.
    if not is_number(value) or not low <= value <= high:
        raise CommandError("bad_argument", f"'{key}' must be a number from {low} to {high}.")
    return finite(value, key)


#: How a material's alpha may export: as glTF's alphaMode OPAQUE, BLEND or MASK, in that order.
ALPHA_MODES = ("opaque", "blend", "clip")
#: The name of the node BlenderLens adds for a normal image, by which it finds the node again.
NORMAL_IMAGE_NODE = "BlenderLens Normal Image"
#: The name of the Normal Map node BlenderLens adds between a normal image and the Principled BSDF.
NORMAL_MAP_NODE = "BlenderLens Normal Map"
#: The name of the node BlenderLens adds for an emission image.
EMISSION_IMAGE_NODE = "BlenderLens Emission Image"
#: The name of the node BlenderLens adds for alpha clipping that tests the alpha against the cutoff.
CLIP_TEST_NODE = "BlenderLens Alpha Below Cutoff"
#: The name of the node BlenderLens adds for alpha clipping that feeds the Principled BSDF's alpha.
CLIP_NODE = "BlenderLens Alpha Clip"


def _alpha_state(material, node):
    """Return how the material's alpha exports, as (mode, cutoff), read from its nodes as the glTF exporter reads them.

    The exporter, in Blender 4.2 and 5.0 alike, takes MASK from alpha = 1 - (alpha < cutoff), and BLEND
    from an alpha below 1 or fed by a node; Blender's own blend settings do not decide it. The cutoff is
    None unless the mode is clip.
    """
    socket = node.inputs["Alpha"]
    source = paint.feeding(socket)
    if source is not None and source.name == CLIP_NODE:
        test = material.node_tree.nodes.get(CLIP_TEST_NODE)
        return "clip", (test.inputs[1].default_value if test is not None else 0.5)
    if source is not None or socket.default_value < 1.0:
        return "blend", None
    return "opaque", None


def _wanted_alpha_mode(asked, alpha, current, linked):
    """Return the alpha mode a call ends with.

    It is the mode asked for, if any. Otherwise clip stays clip, and an alpha value given alone blends below 1
    and is opaque at 1, unless something already feeds the alpha. With no alpha value, the mode stays as it is.
    """
    if asked is not None:
        return asked
    if current == "clip" or alpha is None or linked:
        return current
    return "blend" if alpha < 1 else "opaque"


def _material_images(material, node):
    """Return the images feeding the base colour, the normal and the emission, keyed by those names."""
    def texture(socket, through=None):
        """Return the image feeding ``socket``, looking through a node of type ``through`` when given."""
        source = paint.feeding(socket)
        if source is not None and through is not None and source.bl_idname == through:
            source = paint.feeding(source.inputs["Color"])
        if source is not None and source.bl_idname == "ShaderNodeTexImage" and source.image is not None:
            return source.image
        return None

    _, base = paint.base_color_chain(material)
    found = {"base_color": base.image if base is not None else texture(node.inputs["Base Color"]),
             "normal": texture(node.inputs["Normal"], "ShaderNodeNormalMap"),
             "emission": texture(node.inputs["Emission Color"])}
    return {key: image for key, image in found.items() if image is not None}


def _image_path(params, key):
    """Return an image parameter: KEEP when not given, None for '' (take the image away), else its path."""
    value = params.get(key)
    if value is None:
        return paint.KEEP
    if not isinstance(value, str):
        raise CommandError("bad_argument", f"'{key}' must be the path of an image file, or '' to take it away.")
    if value == "":
        return None
    if not os.path.isfile(value):
        raise CommandError("not_found", f"No image file at {value}.",
                           f"'{key}' is a path. A relative path is resolved against the MCP server's current "
                           "directory.")
    return value


def _load_image(path, key, undo):
    """Load the image at ``path``, or reuse the one already loaded from it, refusing a file Blender cannot read.

    A newly loaded image is recorded in ``undo``, so a refusal removes it again.
    """
    before = set(bpy.data.images)
    try:
        image = bpy.data.images.load(path, check_existing=True)
    except RuntimeError as exc:
        raise CommandError("bad_argument", f"Blender cannot read {path} ({key}): {_blender_said(exc)}",
                           "Use a PNG or JPEG, which glTF carries, or another format Blender opens.") from None
    if image not in before:
        undo.add(lambda: bpy.data.images.remove(image))
    # Blender reads the pixels only when they are first asked for, so a file it cannot decode loads without
    # an error, and has no size.
    if not image.size[0] or not image.size[1]:
        raise CommandError("bad_argument", f"Blender cannot read {path} ({key}) as an image.",
                           "Use a PNG or JPEG, which glTF carries, or another format Blender opens.")
    return image


def _wire_normal(material, node, image, undo):
    """Feed the Principled BSDF's normal from ``image`` through a Normal Map node, or unlink it for None."""
    if image is paint.KEEP:
        return
    tree = material.node_tree
    socket = node.inputs["Normal"]
    if image is None:
        if socket.is_linked:
            paint.unlink(tree, socket, undo)
        return
    texture = paint.own_node(tree, "ShaderNodeTexImage", NORMAL_IMAGE_NODE, undo, node, (-600.0, -550.0))
    if texture.image != image:
        undo.set(texture, "image", image)
    # A normal map holds directions, not colours: read as sRGB, every normal would tilt.
    if image.colorspace_settings.name != "Non-Color":
        undo.set(image.colorspace_settings, "name", "Non-Color")
    normal_map = paint.own_node(tree, "ShaderNodeNormalMap", NORMAL_MAP_NODE, undo, node, (-250.0, -550.0))
    paint.link(tree, texture.outputs["Color"], normal_map.inputs["Color"], undo)
    paint.link(tree, normal_map.outputs["Normal"], socket, undo)


def _wire_emission(material, node, image, strength_given, undo):
    """Feed the Principled BSDF's emission colour from ``image``, or unlink it for None."""
    if image is paint.KEEP:
        return
    tree = material.node_tree
    socket = node.inputs["Emission Color"]
    if image is None:
        if socket.is_linked:
            paint.unlink(tree, socket, undo)
        return
    texture = paint.own_node(tree, "ShaderNodeTexImage", EMISSION_IMAGE_NODE, undo, node, (-600.0, -300.0))
    if texture.image != image:
        undo.set(texture, "image", image)
    paint.link(tree, texture.outputs["Color"], socket, undo)
    # As with emission_color given alone, a strength of 0 is raised to 1, since an emission image at strength 0
    # would not glow.
    strength = node.inputs["Emission Strength"]
    if not strength_given and strength.default_value == 0:
        undo.set(strength, "default_value", 1.0)


def _alpha_source(material):
    """Return the alpha that varies over the surface, if the base colour has one, or None.

    It is the base colour image's alpha, else the color attribute's alpha, which is what marks the see-through
    faces of a vertex-coloured part.
    """
    attribute, image_node = paint.base_color_chain(material)
    if image_node is not None:
        return image_node.outputs["Alpha"]
    if attribute is not None:
        return material.node_tree.nodes[paint.ATTRIBUTE_NODE].outputs["Alpha"]
    return None


def _wire_alpha(material, node, mode, cutoff, follow, undo):
    """Feed the Principled BSDF's alpha the way the glTF exporter reads each mode.

    Opaque feeds 1. Blend feeds the varying alpha (the image's, else the color attribute's) when ``follow``
    is true, and otherwise the socket's own value. Clip feeds 1 - (varying alpha < cutoff). The viewport's
    blending and diffuse alpha are set to match.
    """
    tree = material.node_tree
    socket = node.inputs["Alpha"]
    source = _alpha_source(material)
    if mode == "opaque":
        if socket.is_linked:
            paint.unlink(tree, socket, undo)
        if socket.default_value != 1.0:
            undo.set(socket, "default_value", 1.0)
    elif mode == "blend":
        if follow and source is not None:
            paint.link(tree, source, socket, undo)
        elif socket.is_linked:
            paint.unlink(tree, socket, undo)
    else:
        test = paint.own_node(tree, "ShaderNodeMath", CLIP_TEST_NODE, undo, node, (-500.0, -800.0))
        clip = paint.own_node(tree, "ShaderNodeMath", CLIP_NODE, undo, node, (-250.0, -800.0))
        for math_node, operation in ((test, "LESS_THAN"), (clip, "SUBTRACT")):
            if math_node.operation != operation:
                undo.set(math_node, "operation", operation)
        if clip.inputs[0].default_value != 1.0:
            undo.set(clip.inputs[0], "default_value", 1.0)
        # source is never None here: set_material refuses clip when nothing varies to clip by (see _check_clip).
        paint.link(tree, source, test.inputs[0], undo)
        undo.set(test.inputs[1], "default_value", cutoff)
        paint.link(tree, test.outputs[0], clip.inputs[1], undo)
        paint.link(tree, clip.outputs[0], socket, undo)
    if hasattr(material, "surface_render_method"):
        method = "BLENDED" if mode == "blend" else "DITHERED"
        if material.surface_render_method != method:
            undo.set(material, "surface_render_method", method)
    # Workbench previews read the material's viewport colour, not its nodes.
    shown = socket.default_value if mode == "blend" else 1.0
    if material.diffuse_color[3] != shown:
        undo.set(material, "diffuse_color", (*list(material.diffuse_color)[:3], shown))


def _check_clip(existing, image, attribute):
    """Refuse clipping a material whose alpha would be one value everywhere.

    The glTF exporter (in Blender 4.2 and 5.0) finds MASK only where the clipped alpha comes from a texture or
    a color attribute. Clipping one value, which would show all of the surface or none of it, it exports as
    BLEND.
    """
    chain = paint.base_color_chain(existing) if existing is not None else (None, None)
    has_image = isinstance(image, str) or (image is paint.KEEP and chain[1] is not None)
    has_attribute = isinstance(attribute, str) or (attribute is paint.KEEP and chain[0] is not None)
    if not (has_image or has_attribute):
        raise CommandError("bad_argument", "alpha_mode clip cuts out where the alpha falls below alpha_cutoff, so it "
                                           "needs an alpha that varies: this material's is one value everywhere.",
                           "Give base_color_image, whose alpha is used, or vertex_color, whose attribute's alpha "
                           "is used. For one alpha everywhere, use blend.")


def set_material(params):
    """Make or change a material, and assign it to an object's faces when an object is given."""
    name = require(params, "material")
    if not isinstance(name, str) or not name:
        raise CommandError("bad_argument", "'material' must be a material name.")
    obj = get_mesh_object(params["object"]) if params.get("object") else None

    base = color(params["base_color"], "base_color") if params.get("base_color") is not None else None
    emission = color(params["emission_color"], "emission_color") if params.get("emission_color") is not None else None
    metallic = _number_in(params, "metallic", 0, 1)
    roughness = _number_in(params, "roughness", 0, 1)
    strength = _number_in(params, "emission_strength", 0, 1e6)
    alpha = _number_in(params, "alpha", 0, 1)
    cutoff = _number_in(params, "alpha_cutoff", 0, 1)
    culling = boolean(params, "backface_culling")
    asked_mode = text(params, "alpha_mode")
    if asked_mode is not None and asked_mode not in ALPHA_MODES:
        raise CommandError("bad_argument", f"'alpha_mode' must be one of {', '.join(ALPHA_MODES)}, got {asked_mode!r}.")
    if asked_mode == "opaque" and alpha is not None and alpha < 1:
        raise CommandError("bad_argument", f"alpha_mode opaque shows no transparency, but alpha is {alpha}.",
                           "Leave alpha out, or use alpha_mode blend or clip.")
    attribute = params.get("vertex_color")
    if attribute is None:
        attribute = paint.KEEP
    elif not isinstance(attribute, str):
        raise CommandError("bad_argument", f"'vertex_color' must be a color attribute's name, got {attribute!r}.")
    elif attribute == "":
        attribute = None
    elif obj is not None and obj.data.color_attributes.get(attribute) is None:
        names = [a.name for a in obj.data.color_attributes]
        raise CommandError("bad_argument", f"'{obj.name}' has no color attribute named '{attribute}'.",
                           "Call set_vertex_color to paint one. "
                           + (f"Its color attributes are: {', '.join(names)}." if names else
                              "It has no color attributes yet."))
    images = {"base_color_image": _image_path(params, "base_color_image"),
              "normal_image": _image_path(params, "normal_image"),
              "emission_image": _image_path(params, "emission_image")}
    # The cutoff applies only to clipping, so it is refused, not ignored, on a material that will not clip.
    existing = bpy.data.materials.get(name)
    existing_node = paint.principled(existing) if existing is not None else None
    current_mode = _alpha_state(existing, existing_node)[0] if existing_node is not None else "opaque"
    linked = existing_node is not None and existing_node.inputs["Alpha"].is_linked
    wanted_mode = _wanted_alpha_mode(asked_mode, alpha, current_mode, linked)
    if cutoff is not None and wanted_mode != "clip":
        raise CommandError("bad_argument", f"alpha_cutoff applies only to alpha_mode clip, and this material's "
                                           f"alpha mode would be {wanted_mode}.", "Pass alpha_mode: \"clip\" with it.")
    if wanted_mode == "clip":
        _check_clip(existing, images["base_color_image"], attribute)
        if alpha is not None:
            raise CommandError("bad_argument", f"'{name}' uses alpha_mode clip, so its alpha comes from its base "
                                               "colour image or color attribute, and an alpha value would change "
                                               "nothing.",
                               "Leave alpha out, or pass alpha_mode: \"blend\" for one alpha everywhere.")
    # Nodes are changed only when a value, an image or a vertex colour is given, and BlenderLens never takes
    # over nodes it did not make: such a change is refused, while a material with another shader at its output
    # can still be assigned as it is.
    wiring = attribute is not paint.KEEP or images["base_color_image"] is not paint.KEEP
    needs_node = wiring or any(value is not None for value in (base, emission, metallic, roughness, strength, alpha,
                                                               cutoff, asked_mode)) \
        or any(image is not paint.KEEP for image in images.values())
    if existing is not None and needs_node:
        sockets = [socket for socket, given in (("Base Color", wiring),
                                                ("Normal", images["normal_image"] is not paint.KEEP),
                                                ("Emission Color", images["emission_image"] is not paint.KEEP))
                   if given]
        reason = paint.not_ours(existing, sockets)
        if reason is not None:
            raise CommandError("bad_argument", reason, paint.NOT_OURS_HINT)

    # The faces are chosen before the material is made or changed, so a refused selection leaves
    # both the material list and every face as they were.
    bm = None
    if obj is not None:
        if obj.data.is_editmode:
            raise CommandError("bad_argument", f"'{obj.name}' is in edit mode, so its faces cannot be assigned.",
                               EDIT_MODE_HINT)
        # ``facing`` is judged by matrix_world, which lags behind until the view layer is updated.
        update()
        selecting = params.get("faces") is not None or params.get("facing") is not None
        bm = bmesh.new()
        try:
            bm.from_mesh(obj.data)
            faces = meshops.select_faces(bm, obj, params) if selecting else list(bm.faces)
        except BaseException:
            bm.free()
            raise

    try:
        with Rollback() as undo:
            material = bpy.data.materials.get(name)
            created = material is None
            if created:
                material = bpy.data.materials.new(name)
                undo.add(lambda: bpy.data.materials.remove(material))
            # A new material culls back faces unless asked not to. With culling off the glTF exporter marks a
            # material doubleSided, and an engine then draws twice the fragments for closed meshes that never
            # show their insides. An existing material keeps its own setting, since someone chose it.
            if culling is None and created:
                culling = True
            if culling is not None and material.use_backface_culling != culling:
                undo.set(material, "use_backface_culling", culling)
            if created or needs_node:
                node = paint.principled_for_change(material, undo)
                inputs = node.inputs
                strength_given = strength is not None
                if base is not None:
                    undo.set(inputs["Base Color"], "default_value", base)
                    undo.set(material, "diffuse_color", base)
                if metallic is not None:
                    undo.set(inputs["Metallic"], "default_value", metallic)
                    undo.set(material, "metallic", metallic)
                if roughness is not None:
                    undo.set(inputs["Roughness"], "default_value", roughness)
                    undo.set(material, "roughness", roughness)
                if emission is not None:
                    undo.set(inputs["Emission Color"], "default_value", emission)
                    if strength is None and inputs["Emission Strength"].default_value == 0:
                        strength = 1.0
                if strength is not None:
                    undo.set(inputs["Emission Strength"], "default_value", strength)
                if alpha is not None:
                    undo.set(inputs["Alpha"], "default_value", alpha)
                loaded = {key: _load_image(path, key, undo) if isinstance(path, str) else path
                          for key, path in images.items()}
                if attribute is not paint.KEEP or loaded["base_color_image"] is not paint.KEEP:
                    paint.wire_base_color(material, node, undo, attribute=attribute,
                                          image=loaded["base_color_image"])
                _wire_normal(material, node, loaded["normal_image"], undo)
                _wire_emission(material, node, loaded["emission_image"], strength_given, undo)
                # An alpha mode asked for follows the varying alpha. An alpha value given alone is one value
                # everywhere, and leaves alone a link it did not make, such as an imported texture's alpha. A new
                # image or color attribute is followed where the old one was.
                following = linked or current_mode == "clip"
                new_image = loaded["base_color_image"] is not paint.KEEP or attribute is not paint.KEEP
                if (asked_mode is not None or cutoff is not None or (alpha is not None and not linked)
                        or (new_image and following)):
                    current_cutoff = _alpha_state(material, node)[1]
                    _wire_alpha(material, node, wanted_mode, cutoff if cutoff is not None else current_cutoff or 0.5,
                                asked_mode is not None or following, undo)

            out = {"created": created}
            if obj is not None:
                mesh = obj.data
                slot = next((i for i, m in enumerate(mesh.materials) if m == material), None)
                if slot is None:
                    mesh.materials.append(material)
                    undo.add(lambda: mesh.materials.pop())
                    slot = len(mesh.materials) - 1
                for face in faces:
                    face.material_index = slot
                bm.to_mesh(mesh)
                mesh.update()
                out.update({"object": obj.name, "slot": slot, "faces_assigned": len(faces),
                            "faces_total": len(mesh.polygons)})
                out.update(_what_the_slot_shows(obj, slot, material))
    finally:
        if bm is not None:
            bm.free()
    out["material"] = _material_values(material)
    return out


def _what_the_slot_shows(obj, slot, material):
    """Return what the object shows in the assigned slot, with a note where that is not the whole story.

    A slot linked to the object shows the object's own material there, whatever the mesh holds. Faces
    belong to the mesh, so on a mesh shared by several objects they change in all of them.
    """
    shown = obj.material_slots[slot].material if slot < len(obj.material_slots) else material
    out = {"shows": shown.name if shown else None}
    notes = []
    if shown != material:
        notes.append(f"Slot {slot} of '{obj.name}' is linked to the object, which shows "
                     f"'{out['shows']}' there, not '{material.name}'.")
    users = obj.data.users
    if users > 1:
        others = sorted(o.name for o in bpy.data.objects if o.data == obj.data and o != obj)
        out["mesh_users"] = users
        notes.append(f"The mesh is shared by {users} objects ({', '.join([obj.name, *others][:6])}); the faces "
                     "changed in every one of them.")
    if notes:
        out["note"] = " ".join(notes)
    return out


# ---------------------------------------------------------------------------
# Transforms, hierarchy, collections
# ---------------------------------------------------------------------------


def _transform_report(obj):
    """Return the object's location, rotation, scale and dimensions, and its location in the world."""
    update()
    return {"object": obj.name, "location": vec(obj.location), "rotation": rotation_degrees(obj),
            "scale": vec(obj.scale), "dimensions": vec(obj.dimensions),
            "world_location": vec(obj.matrix_world.translation)}


def set_transform(params):
    """Set an object's location, rotation, scale or dimensions, drop it onto what is below, and return its transform."""
    obj = get_object(require(params, "object"))
    # Location, rotation, scale and the drop target are checked before anything is set. A null component of
    # ``dimensions`` keeps the size the new scale gives, so only the shape of ``dimensions`` can be checked
    # here; its values are checked once the scale is set, and a refusal then puts back what was set before.
    location = triple(params.get("location"), "location", tuple(obj.location), uniform=False) \
        if params.get("location") is not None else None
    degrees = triple(params.get("rotation"), "rotation", tuple(rotation_degrees(obj)), uniform=False) \
        if params.get("rotation") is not None else None
    scale = triple(params["scale"], "scale", tuple(obj.scale)) if params.get("scale") is not None else None
    dimensions = params.get("dimensions")
    if dimensions is not None:
        triple(dimensions, "dimensions", (0.0, 0.0, 0.0), uniform=False)
    onto = measure.drop_target(params.get("drop"), obj)
    landed = None
    with Rollback() as undo:
        if location is not None:
            undo.set(obj, "location", location)
        if degrees is not None:
            if obj.rotation_mode in ("QUATERNION", "AXIS_ANGLE"):
                undo.set(obj, "rotation_mode", "XYZ")
            undo.set(obj, "rotation_euler", [math.radians(a) for a in degrees])
        if scale is not None:
            undo.set(obj, "scale", scale)
        if dimensions is not None:
            update()
            wanted = triple(dimensions, "dimensions", tuple(obj.dimensions), uniform=False)
            if any(d == 0 for d, w in zip(obj.dimensions, wanted, strict=True) if w != 0):
                raise CommandError("bad_argument", f"'{obj.name}' is flat along an axis, so its size there cannot "
                                                   "be scaled; set 'scale' instead.")
            undo.set(obj, "dimensions", wanted)
        if onto is not False:
            # Dropped last, so the object falls from where the values above put it.
            landed = measure.drop(obj, onto, undo)
    out = _transform_report(obj)
    if landed is not None:
        out["landed_on"], out["moved_by"] = landed
    return out


def apply_transform(params):
    """Bake an object's rotation, scale or location into its data, or move its origin, and return its transform."""
    obj = get_object(require(params, "object"))
    origin = measure.origin_target(params.get("origin"))
    # Rotation and scale are applied by default, but not when an origin is given: moving the origin alone
    # applies nothing else unless asked. Location is applied only when asked for.
    defaults = (("location", False), ("rotation", origin is None), ("scale", origin is None))
    flags = {key: boolean(params, key, default) for key, default in defaults}
    refuse_outside_object_mode([obj], "Blender cannot apply transforms")
    if origin is not None:
        # Everything the origin needs is checked before the transform is applied, which cannot be put back.
        measure.check_origin(obj, origin)
    if any(flags.values()):
        try:
            with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=[obj],
                                           selected_editable_objects=[obj]):
                result = bpy.ops.object.transform_apply(**flags)
        except RuntimeError as exc:
            # The operator reported an error and cancelled, changing nothing, so this is a refusal like any other.
            raise CommandError("blender_error", f"Blender did not apply the transform: {_blender_said(exc)}",
                               "A mesh shared by several objects cannot have its transform applied.") from None
        if "FINISHED" not in result:
            raise CommandError("blender_error", f"Blender did not apply the transform ({sorted(result)}).",
                               "A mesh shared by several objects cannot have its transform applied.")
    if origin is not None:
        with Rollback() as undo:
            measure.move_origin(obj, origin, undo)
    out = _transform_report(obj)
    out["applied"] = [k for k, v in flags.items() if v] + (["origin"] if origin is not None else [])
    return out


def set_parent(params):
    """Parent an object to another, or unparent it, keeping its place in the world unless asked not to."""
    obj = get_object(require(params, "object"))
    parent = get_object(params["parent"], "parent") if params.get("parent") else None
    keep = boolean(params, "keep_transform", True)
    if parent is not None and parent == obj:
        raise CommandError("bad_argument", f"'{obj.name}' cannot be its own parent.")
    if parent is not None and parent in descendants(obj):
        raise CommandError("bad_argument", f"'{parent.name}' is inside '{obj.name}', so parenting '{obj.name}' to it "
                                           "would make a loop.")
    update()
    world = obj.matrix_world.copy()
    obj.parent = parent
    obj.matrix_parent_inverse = Matrix.Identity(4)
    if keep:
        obj.matrix_world = world
    out = _transform_report(obj)
    out["parent"] = parent.name if parent else None
    return out


def _object_list(params):
    """Return the objects named in the parameter ``objects``, which may also be a single name."""
    names = require(params, "objects")
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, list):
        raise CommandError("bad_argument", f"'objects' must be a list of object names, got {names!r}.")
    return [get_object(n, "objects") for n in names]


def set_collection(params):
    """Put objects into a collection, making it or linking it into the scene when needed."""
    objects = _object_list(params)
    name = require(params, "collection")
    if not isinstance(name, str):
        raise CommandError("bad_argument", f"'collection' must be a collection name, got {name!r}.")
    parent = params.get("parent")
    if parent is not None and not isinstance(parent, str):
        raise CommandError("bad_argument", f"'parent' must be a collection name, got {parent!r}.")
    keep = boolean(params, "keep_other", False)
    with Rollback() as undo:
        # The collection is made, or linked into the scene under ``parent``, with each step recorded as it
        # happens, so a refusal halfway (a collection named as its own parent) leaves no collection behind.
        collection = get_collection(name, create=True, parent=parent or None, undo=undo)
        for obj in objects:
            if obj.name not in collection.objects:
                collection.objects.link(obj)
                undo.add(lambda obj=obj: collection.objects.unlink(obj))
            if not keep:
                for other in list(obj.users_collection):
                    if other != collection:
                        other.objects.unlink(obj)
                        undo.add(lambda obj=obj, other=other: other.objects.link(obj))
    return {"collection": collection.name,
            "objects": {o.name: [c.name for c in o.users_collection] for o in objects},
            "collection_objects": [o.name for o in collection.objects]}


#: The range of Blender's integer properties, which hold 32 bits.
INT_RANGE = (-2 ** 31, 2 ** 31 - 1)

#: How deep one custom property may nest lists and objects. Blender keeps every level as an ID property of
#: its own.
MAX_PROPERTY_DEPTH = 8
#: How many bytes of JSON one custom property may hold. Custom properties exported as glTF extras are small
#: tables of game data.
MAX_PROPERTY_BYTES = 65_536


def _property_name(key, where=""):
    """Refuse a custom property name that is not text of 1 to 63 bytes, the most Blender keeps of a property's name."""
    if not isinstance(key, str) or not key or len(key.encode("utf-8")) > 63:
        raise CommandError("bad_argument", f"Property name {key!r}{where} must be text of 1 to 63 bytes.")


def _json_kind(value):
    """Return the kind of a JSON value in the plural, as a refusal names it: "numbers", "text", "lists"."""
    if isinstance(value, bool):
        return "true/false"
    if is_number(value):
        return "numbers"
    if isinstance(value, str):
        return "text"
    return {dict: "objects", list: "lists"}.get(type(value), "null" if value is None else type(value).__name__)


def _json_value(value, where, depth=0):
    """Check one custom property's value all the way down, before anything is set.

    Blender stores text, true/false, numbers, objects (as groups) and lists. A list holds one kind of
    item, since Blender keeps numbers as an array and the rest as a list of properties, and refuses a
    mix. Integers must fit Blender's 32-bit integers, and every number must be finite. Null is refused
    here, since it exists only as a whole property's value, meaning "remove this property".
    """
    if depth > MAX_PROPERTY_DEPTH:
        raise CommandError("bad_argument", f"Property '{where}' nests deeper than {MAX_PROPERTY_DEPTH} levels.",
                           "Flatten it, or keep the deep part as text.")
    if isinstance(value, (str, bool)):
        return
    if is_number(value):
        if isinstance(value, int) and not INT_RANGE[0] <= value <= INT_RANGE[1]:
            raise CommandError("bad_argument", f"Property '{where}' holds {value}, which does not fit Blender's "
                                               "32-bit integers.",
                               "Pass it as a decimal number (with a point) or as text.")
        # Custom properties keep floats as doubles, so only finiteness is checked, not the 32-bit float bound.
        finite(value, f"properties.{where}", limit=None)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _property_name(key, f" in '{where}'")
            _json_value(item, f"{where}.{key}", depth + 1)
        return
    if isinstance(value, list):
        kinds = sorted({_json_kind(item) for item in value})
        if len(kinds) > 1:
            raise CommandError("bad_argument", f"Property '{where}' mixes {' and '.join(kinds)} in one list.",
                               "Blender keeps a list of one kind: all numbers, all text, all true/false, all objects "
                               "or all lists.")
        for i, item in enumerate(value):
            _json_value(item, f"{where}[{i}]", depth + 1)
        return
    if value is None:
        raise CommandError("bad_argument", f"Property '{where}' holds null, which Blender cannot store.",
                           "null removes a whole property. Inside a property, leave the key out instead.")
    raise CommandError("bad_argument", f"Property '{where}' must hold JSON values, got {value!r}.")


def _property_value(key, value, obj):
    """Check one custom property before any is set, and return the value to store.

    A name that is one of the object's built-in settings is refused, and so is a value of more than
    MAX_PROPERTY_BYTES of JSON. None, which removes the property, is returned as it is.
    """
    _property_name(key)
    if key in obj.bl_rna.properties.keys():
        raise CommandError("bad_argument", f"'{key}' is a setting Blender or an add-on keeps on every object; "
                                           "a custom property by that name would replace it.",
                           "Choose another name.")
    if value is None:
        return value
    _json_value(value, key)
    size = len(json.dumps(value))
    if size > MAX_PROPERTY_BYTES:
        raise CommandError("bad_argument", f"Property '{key}' is {size:,} bytes of JSON, more than the "
                                           f"{MAX_PROPERTY_BYTES:,} allowed.",
                           "Keep large data in a file the game loads, and its name in the property.")
    return value


def set_object(params):
    """Rename an object, hide or show it, set its display, and set or remove its custom properties."""
    obj = get_object(require(params, "object"))
    rename = text(params, "rename")
    rename_data = boolean(params, "rename_data", True)
    hide = boolean(params, "hide")
    hide_render = boolean(params, "hide_render")
    display = resolve_settings(obj, {"display_type": params["display"]}, "object") \
        if params.get("display") is not None else []
    shape = []
    if params.get("empty_display") is not None:
        if obj.type != "EMPTY":
            raise CommandError("bad_argument", f"'{obj.name}' is {object_kind(obj)}, not an empty, and "
                                               "empty_display sets the shape an empty is drawn as.",
                               "Use display for other objects.")
        shape = resolve_settings(obj, {"empty_display_type": params["empty_display"]}, "object")
    properties = params.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise CommandError("bad_argument", "'properties' must be a JSON object that maps names to values.")
        properties = {key: _property_value(key, value, obj) for key, value in properties.items()}

    changed = []
    with Rollback() as undo:
        if rename:
            undo.set(obj, "name", rename)
            changed.append("name")
            if obj.data is not None and obj.data.users == 1 and rename_data:
                undo.set(obj.data, "name", obj.name)
        if hide is not None:
            was = obj.hide_get()
            obj.hide_set(hide)
            undo.add(lambda: obj.hide_set(was))
            changed.append("hide")
        if hide_render is not None:
            undo.set(obj, "hide_render", hide_render)
            changed.append("hide_render")
        for key, value in display:
            undo.set(obj, key, value)
            changed.append("display")
        for key, value in shape:
            undo.set(obj, key, value)
            changed.append("empty_display")
        if properties is not None:
            for key, value in properties.items():
                if key in obj:
                    # Keep a plain copy, all the way down, since a view of the old value becomes invalid once
                    # the property is replaced.
                    kept = _plain(obj[key])
                    undo.add(lambda key=key, kept=kept: obj.__setitem__(key, kept))
                else:
                    undo.add(lambda key=key: obj.pop(key, None))
                try:
                    if value is None:
                        obj.pop(key, None)
                    else:
                        obj[key] = value
                except (TypeError, ValueError, KeyError, OverflowError) as exc:
                    raise CommandError("bad_argument", f"Blender refused {value!r} for property '{key}': {exc}") \
                        from None
            changed.append("properties")
    out = {"object": obj.name, "changed": changed, "visible": obj.visible_get(),
           "hide_render": obj.hide_render, "display": obj.display_type,
           "properties": {k: jsonable(obj[k]) for k in obj.keys() if not k.startswith("_")}}
    if obj.type == "EMPTY":
        out["empty_display"] = obj.empty_display_type
    return out


def _repoint(struct, twins, depth=1):
    """Point every datablock pointer of ``struct`` that targets an original in ``twins`` at its copy.

    The struct's own pointers are walked generically, so a modifier's object, mirror_object,
    offset_object or target and a constraint's target are all found, and the walk goes ``depth``
    levels into nested structs and collections (an armature constraint's targets). ID properties
    are followed too, since a geometry-nodes modifier keeps its object inputs there.
    """
    for prop in struct.bl_rna.properties:
        key = prop.identifier
        if key == "rna_type" or prop.type not in ("POINTER", "COLLECTION"):
            continue
        try:
            value = getattr(struct, key)
        except Exception:  # noqa: BLE001 -- some properties raise in some states
            continue
        if prop.type == "POINTER":
            if isinstance(value, bpy.types.ID):
                twin = twins.get(value)
                if twin is not None and not prop.is_readonly:
                    try:
                        setattr(struct, key, twin)
                    except (TypeError, AttributeError, ValueError):
                        pass  # a pointer that refuses the copy keeps the original
            elif value is not None and depth > 0:
                _repoint(value, twins, depth - 1)
        elif depth > 0:
            for item in value:
                if not isinstance(item, bpy.types.ID):
                    _repoint(item, twins, depth - 1)
    try:
        keys = list(struct.keys())
    except TypeError:
        return  # this kind of struct holds no ID properties
    for key in keys:
        value = struct[key]
        if isinstance(value, bpy.types.ID) and value in twins:
            struct[key] = twins[value]


def _point_copies_at_each_other(twins):
    """Point the copies at each other, as Shift+D does after copying.

    Whatever in a copy points at an original that was copied with it is pointed at that original's
    copy, so a duplicated body's boolean modifier cuts with the duplicated cutter, not the original
    one. Parents are set as the copies are made; this covers modifiers, constraints (the object's and
    its pose bones'), drivers and the object's own ID properties.
    """
    for twin in twins.values():
        for modifier in twin.modifiers:
            _repoint(modifier, twins)
        constraints = list(twin.constraints)
        if twin.pose is not None:
            constraints += [c for bone in twin.pose.bones for c in bone.constraints]
        for constraint in constraints:
            _repoint(constraint, twins)
        if twin.animation_data is not None:
            for curve in twin.animation_data.drivers:
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if target.id in twins:
                            target.id = twins[target.id]
        _repoint(twin, twins, depth=0)


def duplicate_object(params):
    """Copy an object, with its children unless asked not to, and return the copy."""
    source = get_object(require(params, "object"))
    linked = boolean(params, "linked", False)
    with_children = boolean(params, "with_children", True)
    offset = triple(params.get("offset"), "offset", (0.0, 0.0, 0.0), uniform=False)
    name = text(params, "name")
    # A mesh in edit mode holds its edit in a separate BMesh, so the edit is written back first, and the copy
    # holds what the user sees.
    sync_edit_mode()

    with Rollback() as undo:
        twins = {}

        def copy(obj, parent):
            """Copy ``obj`` under ``parent``, with its children when asked, and return every copy made."""
            twin = obj.copy()
            twins[obj] = twin
            undo.add(lambda: bpy.data.objects.remove(twin, do_unlink=True))
            if obj.data is not None and not linked:
                twin.data = obj.data.copy()
                data = twin.data
                undo.add(lambda: bpy.data.batch_remove([data]))
            for collection in obj.users_collection:
                collection.objects.link(twin)
            if parent is not None:
                twin.parent = parent
            made = [twin]
            if with_children:
                for child in obj.children:
                    made.extend(copy(child, twin))
            return made

        made = copy(source, source.parent)
        _point_copies_at_each_other(twins)
        top = made[0]
        if name:
            top.name = name
            if top.data is not None and not linked:
                top.data.name = top.name
        top.location = (top.location[0] + offset[0], top.location[1] + offset[1], top.location[2] + offset[2])
        select_only([top])
    out = object_summary(top)
    out["copied"] = [o.name for o in made]
    out["linked"] = linked
    return out


def delete_object(params):
    """Delete objects, with their children when asked, keeping any other children where they stand."""
    objects = _object_list(params)
    with_children = boolean(params, "with_children", False)
    doomed = []
    for obj in objects:
        for candidate in [obj, *(descendants(obj) if with_children else [])]:
            if candidate not in doomed:
                doomed.append(candidate)
    update()
    for obj in doomed:
        for child in list(obj.children):
            if child not in doomed:
                world = child.matrix_world.copy()
                child.parent = None
                child.matrix_world = world
    deleted = []
    for obj in doomed:
        data = obj.data
        deleted.append(obj.name)
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and data.users == 0:
            for where in ("meshes", "curves", "lights", "cameras"):
                pool = getattr(bpy.data, where)
                if data.name in pool and pool[data.name] == data:
                    pool.remove(data)
                    break
    return {"deleted": deleted, "remaining": len(bpy.context.scene.objects)}


def _keep_child_in_place(child, parent, undo):
    """Parent ``child`` to ``parent`` where it stands, recording how to put it back."""
    kept = (child.parent, child.matrix_parent_inverse.copy(), child.matrix_basis.copy())

    def put_back():
        """Give the child back its parent and its place."""
        child.parent = kept[0]
        child.matrix_parent_inverse = kept[1]
        child.matrix_basis = kept[2]

    world = child.matrix_world.copy()
    child.parent = parent
    child.matrix_parent_inverse = Matrix.Identity(4)
    child.matrix_world = world
    undo.add(put_back)


def _own_mesh(piece, undo):
    """Return the piece's mesh, giving the piece its own copy first when other objects share the mesh.

    Changing the mesh returned then changes no other object. Making the copy is recorded in ``undo``.
    """
    mesh = piece.data
    if mesh.users <= 1:
        return mesh
    copy = mesh.copy()
    piece.data = copy

    def put_back():
        """Give the piece back its shared mesh, and remove the copy."""
        piece.data = mesh
        bpy.data.meshes.remove(copy)

    undo.add(put_back)
    return copy


def _match_uv_maps(mesh, names, undo):
    """Rename the mesh's UV maps to ``names``, in order, and return {old: new} for each map renamed.

    Maps past the end of ``names`` keep their names. Blender's join merges UV maps by name, so a piece whose
    map has another name than the first object's would bring its UVs into a map of its own, and leave the
    first object's map empty on its faces.
    """
    layers = mesh.uv_layers
    old = [layer.name for layer in layers]
    wanted = [names[i] if i < len(names) else name for i, name in enumerate(old)]
    if wanted == old:
        return {}

    def rename(to):
        """Give the maps the names ``to``, in order."""
        # Rename through temporary names first, so no rename collides with a name another map still has.
        for i, layer in enumerate(layers):
            layer.name = f"BlenderLens UV {i}"
        for layer, name in zip(layers, to, strict=True):
            layer.name = name

    rename(wanted)
    undo.add(lambda: rename(old))
    return {before: layer.name for before, layer in zip(old, layers, strict=True) if before != layer.name}


def join_objects(params):
    """Join meshes into the first one named, as Ctrl+J does.

    Blender's join keeps each piece's faces and material slots (merging slots that hold the same
    material), and puts the pieces where they were, in the first object's space. Five things it does
    silently are refused or put right first: it drops the pieces' modifiers, it changes every object
    sharing the first one's mesh, it unparents the pieces' children, it leaves a piece mirrored against
    the first object with its faces pointing inward, and it gives a piece's UV map with a different name
    a map of its own.
    """
    objects = _object_list(params)
    if len(objects) < 2:
        raise CommandError("bad_argument", "join_objects needs at least two objects: the one to join into, and one "
                                           "or more to join into it.")
    names = [o.name for o in objects]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        raise CommandError("bad_argument", f"'objects' lists {', '.join(twice)} more than once.")
    for obj in objects:
        if obj.type != "MESH":
            raise CommandError("bad_argument", f"'{obj.name}' is {object_kind(obj)}, not a mesh, and only meshes can "
                                               "be joined.")
    target, pieces = objects[0], objects[1:]
    refuse_outside_object_mode(objects, "Blender cannot join objects")
    sharing = sorted(o.name for o in bpy.data.objects if o.data == target.data and o != target)
    if sharing:
        raise CommandError("bad_argument", f"The mesh of '{target.name}' is shared with {', '.join(sharing[:6])}; "
                                           "joining into it would change them too.",
                           f"Put another object first, or give '{target.name}' a mesh of its own: copy it with "
                           "duplicate_object (linked false), delete the original, and join into the copy.")
    modified = [p for p in pieces if len(p.modifiers)]
    if modified:
        piece = modified[0]
        listed = ", ".join(m.name for m in piece.modifiers)
        raise CommandError("bad_argument", f"'{piece.name}' has modifiers ({listed}), which joining would drop.",
                           f"Call apply_modifier or remove_modifier on '{piece.name}' first. The first object's own "
                           "modifiers stay, and act on the joined whole.")
    if target.parent in pieces:
        raise CommandError("bad_argument", f"'{target.parent.name}' is the parent of '{target.name}', and a parent "
                                           "cannot be joined into its own child.",
                           f"Put '{target.parent.name}' first, or first call set_parent on '{target.name}' to give it "
                           "another parent.")
    update()
    layer = bpy.context.view_layer
    outside = [o.name for o in objects if layer.objects.get(o.name) is None]
    if outside:
        verb, them = ("is", "it") if len(outside) == 1 else ("are", "them")
        raise CommandError("bad_argument", f"{joined(outside)} {verb} not in this view layer, so Blender cannot "
                                           f"join {them}.", OUTSIDE_SCENE_HINT)
    joined_names = [p.name for p in pieces]
    target_inverse = target.matrix_world.inverted_safe()
    target_maps = [layer.name for layer in target.data.uv_layers]
    turned, renamed = [], {}
    with Rollback() as undo:
        # Blender unparents the joined objects' children, which makes them jump, so they are first parented to
        # the first object where they stand.
        for piece in pieces:
            for child in list(piece.children):
                if child not in pieces:
                    _keep_child_in_place(child, target, undo)
        for piece in pieces:
            mirrored = (target_inverse @ piece.matrix_world).determinant() < 0
            maps = [layer.name for layer in piece.data.uv_layers]
            mismatched = target_maps and any(a != b for a, b in zip(maps, target_maps, strict=False))
            if not (mirrored or mismatched):
                continue
            mesh = _own_mesh(piece, undo)
            if mirrored:
                # Moved into the first object's space through a mirror, the faces would point inward.
                mesh.flip_normals()
                undo.add(mesh.flip_normals)
                turned.append(piece.name)
            if mismatched:
                renamed[piece.name] = _match_uv_maps(mesh, target_maps, undo)
        meshes = list(dict.fromkeys(p.data for p in pieces))
        try:
            with bpy.context.temp_override(active_object=target, object=target, selected_objects=objects,
                                           selected_editable_objects=objects):
                result = bpy.ops.object.join()
        except RuntimeError as exc:
            raise CommandError("blender_error", f"Blender did not join: {_blender_said(exc)}") from None
        if "FINISHED" not in result:
            raise CommandError("blender_error", f"Blender did not join ({sorted(result)}).")
    # The pieces' meshes are left without users, so they are removed, as delete_object removes unused data.
    unused = [m for m in meshes if m.users == 0]
    if unused:
        bpy.data.batch_remove(unused)
    select_only([target])
    mesh = target.data
    out = object_summary(target)
    out["joined"] = joined_names
    out["slots"] = [{"slot": i, "material": s.material.name if s.material else None,
                     "faces": sum(1 for p in mesh.polygons if p.material_index == i)}
                    for i, s in enumerate(target.material_slots)]
    out["uv_maps"] = [u.name for u in mesh.uv_layers]
    notes = []
    if renamed:
        out["uv_renamed"] = renamed
        notes.append(f"Some pieces had UV maps named differently from those of '{target.name}', so their maps were "
                     "joined into its maps by position, not by name. "
                     + " ".join(f"In '{piece}', " + joined(f"{a} became {b}" for a, b in names.items()) + "."
                                for piece, names in renamed.items()))
    if turned:
        were = "it was" if len(turned) == 1 else "they were"
        notes.append(f"The faces of {joined(turned)} were turned over, because {were} mirrored against "
                     f"'{target.name}' and would have pointed inward.")
    if target.modifiers:
        notes.append(f"The modifiers of '{target.name}' ({', '.join(m.name for m in target.modifiers)}) now act on "
                     "the joined pieces too.")
    if notes:
        out["note"] = " ".join(notes)
    return out


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _targets_for_export(params):
    """Return the objects an export writes: those named with their descendants, a collection's, or the scene's."""
    scene = bpy.context.scene
    if params.get("objects"):
        roots = [get_object(n, "objects") for n in params["objects"]]
        chosen = []
        for obj in roots:
            for candidate in [obj, *descendants(obj)]:
                if candidate not in chosen:
                    chosen.append(candidate)
        return chosen
    if params.get("collection"):
        collection = get_collection(params["collection"])
        return list(collection.all_objects)
    return list(scene.objects)


def _layer_collection_path(root, collection):
    """Return the layer collections from the view layer's root down to the one showing ``collection``, or []."""
    if root.collection == collection:
        return [root]
    for child in root.children:
        found = _layer_collection_path(child, collection)
        if found:
            return [root, *found]
    return []


def _reveal(targets, undo):
    """Make every target selectable for the exporter, recording in ``undo`` how to hide it again.

    The exporter reads the viewport's evaluated scene and takes only selected objects. An
    object hidden in the viewport (hide_set), disabled in viewports (hide_viewport, its own or
    its collection's), unselectable (hide_select, its own or its collection's), or in a hidden or
    excluded layer collection, is either missing from that scene or cannot be selected, so it
    would be left out of the file.
    """
    layer = bpy.context.view_layer
    for obj in targets:
        for collection in obj.users_collection:
            for layer_collection in _layer_collection_path(layer.layer_collection, collection):
                if layer_collection.exclude:
                    undo.set(layer_collection, "exclude", False)
                if layer_collection.hide_viewport:
                    undo.set(layer_collection, "hide_viewport", False)
                if layer_collection.collection.hide_viewport:
                    undo.set(layer_collection.collection, "hide_viewport", False)
                if layer_collection.collection.hide_select:
                    undo.set(layer_collection.collection, "hide_select", False)
        if obj.hide_viewport:
            undo.set(obj, "hide_viewport", False)
        if obj.hide_select:
            undo.set(obj, "hide_select", False)
    # Including an excluded collection rebuilds the view layer's objects, and hide_get needs that list current.
    update()
    for obj in targets:
        try:
            hidden = obj.hide_get()
        except RuntimeError:
            continue
        if hidden:
            obj.hide_set(False)
            undo.add(lambda obj=obj: obj.hide_set(True))
    update()


def _extras_owners(targets):
    """Return everything whose custom properties the exporter writes as extras.

    That is the scene, and each object with its mesh and its materials, each listed once.
    """
    owners = [bpy.context.scene]
    for obj in targets:
        owners.append(obj)
        if isinstance(obj.data, bpy.types.Mesh):
            owners.append(obj.data)
        owners += [slot.material for slot in getattr(obj, "material_slots", []) if slot.material]
    return list(dict.fromkeys(owners))


def _plain(value):
    """Return a custom property's value as plain Python, all the way down: groups as dicts, arrays as lists.

    A list of groups (as nested extras are stored) reads back as a Python list of group views, which point
    into the property and become invalid when it is replaced, so each item is converted too.
    """
    for method in ("to_dict", "to_list"):
        if hasattr(value, method):
            return getattr(value, method)()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _not_finite(value):
    """Return whether ``value``, or anything inside it, is NaN or infinite."""
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_not_finite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_not_finite(v) for v in value)
    return False


def _refuse_unwritable_extras(owners):
    """Refuse an export whose extras would hold NaN or infinity, naming the property that holds it.

    JSON has neither, and the exporter fails while writing one. Extras are on by default, so without
    this refusal any file holding such a property could not be exported at all, and nothing would say why.
    """
    for owner in owners:
        for key in owner.keys():
            if _not_finite(_plain(owner[key])):
                raise CommandError("bad_argument", f"{type(owner).__name__} '{owner.name}' has the custom property "
                                                   f"'{key}' holding NaN or infinity, which glTF extras cannot hold.",
                                   "Set it to a finite number (set_object properties), or export with extras false.")


def _rewrite_json(path, *edits, write=True):
    """Run each edit on the JSON chunk of the GLB at ``path``, and write it back unless ``write`` is false.

    A GLB is a 12-byte header, the JSON chunk (length, type, body), then the binary chunk, which
    is copied as it is. The body is padded with spaces to a multiple of four bytes, as the format
    requires, so the file stays valid whatever length the edits leave. Returns each edit's own
    result, in order.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    length, kind = struct.unpack_from("<II", data, 12)
    document = json.loads(data[20:20 + length].decode("utf-8"))
    results = [edit(document) for edit in edits]
    if not write:
        return results
    body = json.dumps(document, separators=(",", ":"), allow_nan=False).encode("utf-8")
    body += b" " * (-len(body) % 4)
    rest = data[20 + length:]
    chunk = struct.pack("<II", len(body), kind) + body
    with open(path, "wb") as handle:
        handle.write(data[:8] + struct.pack("<I", 12 + len(chunk) + len(rest)) + chunk + rest)
    return results


def _without_hidden_extras(document):
    """Remove the custom properties whose names start with _ from the file's extras.

    Those are Blender's and add-ons' own bookkeeping (such as the old _RNA_UI), not data meant for
    the game, and the exporter writes them with the rest. An extras object left empty is removed too.
    """
    def drop(value):
        """Remove the hidden extras from ``value`` and from everything inside it."""
        if isinstance(value, dict):
            extras = value.get("extras")
            if isinstance(extras, dict):
                for key in [k for k in extras if k.startswith("_")]:
                    del extras[key]
                if not extras:
                    del value["extras"]
            for item in value.values():
                drop(item)
        elif isinstance(value, list):
            for item in value:
                drop(item)

    drop(document)


#: Blender's suffix for a name already taken: a dot and three or more digits.
_BLENDER_SUFFIX = re.compile(r"^(.+)\.\d{3,}$")


def _clean_names(document):
    """Remove Blender's numeric suffixes, such as .001, from names in the file wherever the names stay unique.

    A node's name must stay unique among its siblings (the scene's root nodes are siblings too); a mesh's
    or a material's among all of its kind. A name that would collide keeps its suffix. Blender's own
    names are untouched; only the file changes. Returns {"nodes": {old: new}, "meshes": ..., "materials": ...}.
    """
    out = {}
    nodes = document.get("nodes", [])
    groups = [scene.get("nodes", []) for scene in document.get("scenes", [])]
    groups += [node.get("children", []) for node in nodes]
    renames = {}
    for group in groups:
        members = [i for i in group if isinstance(i, int) and 0 <= i < len(nodes)]
        renames.update(_unsuffixed([nodes[i] for i in members], [i for i in members]))
    out["nodes"] = {}
    for index, new in renames.items():
        out["nodes"][nodes[index]["name"]] = new
        nodes[index]["name"] = new
    for kind in ("meshes", "materials"):
        items = document.get(kind, [])
        out[kind] = {}
        for index, new in _unsuffixed(items, list(range(len(items)))).items():
            out[kind][items[index]["name"]] = new
            items[index]["name"] = new
    return out


def _unsuffixed(items, indices):
    """Return {index: name without its suffix} for the items whose cleaned name no other item has or gets.

    A name with its suffix taken off may itself end in a suffix (B.001.001 cleans to B.001), so each cleaned
    name is checked against every name the items end with, and the item keeps its suffix while any other
    item has that name.
    """
    names = [item.get("name") if isinstance(item, dict) else None for item in items]
    taken = {}
    for name in names:
        if isinstance(name, str):
            match = _BLENDER_SUFFIX.match(name)
            base = match.group(1) if match else name
            taken[base] = taken.get(base, 0) + 1
    cleaned = {}
    for position, name in enumerate(names):
        match = _BLENDER_SUFFIX.match(name) if isinstance(name, str) else None
        if match and taken[match.group(1)] == 1:
            cleaned[position] = match.group(1)
    while True:
        final = [cleaned.get(position, name) for position, name in enumerate(names)]
        counts = {}
        for name in final:
            counts[name] = counts.get(name, 0) + 1
        clashing = [position for position in cleaned if counts[final[position]] > 1]
        if not clashing:
            break
        for position in clashing:
            del cleaned[position]
    return {indices[position]: name for position, name in cleaned.items()}


#: The engines export_glb can write a file for; generic stands for any glTF reader.
EXPORT_TARGETS = ("godot", "threejs", "generic")
#: export_glb's image formats, mapped to the glTF exporter's names for them.
IMAGE_FORMATS = {"auto": "AUTO", "jpeg": "JPEG", "webp": "WEBP", "none": "NONE"}
#: export_glb's vertex colour choices, mapped to the glTF exporter's names for them.
VERTEX_COLORS = {"material": "MATERIAL", "active": "ACTIVE", "none": "NONE"}


def _choice(params, key, choices, default):
    """Return a text parameter that must be one of ``choices``, or ``default`` when it is not given."""
    value = text(params, key, default)
    if value not in choices:
        raise CommandError("bad_argument", f"'{key}' takes one of {', '.join(choices)}, got {value!r}.")
    return value


def _export_options(params):
    """Return the export target and options, each with an explicit value rather than the exporter's default.

    A target names the engine the file is for, and generic stands for any glTF reader. A combination the
    named engine cannot load is refused here, before anything is written. Godot's importer has no Draco
    support (KHR_draco_mesh_compression is not among the extensions it supports), so importing such a file
    there fails.
    """
    target = _choice(params, "target", EXPORT_TARGETS, "generic")
    options = {
        "target": target,
        "draco": boolean(params, "draco", False),
        "image_format": _choice(params, "image_format", tuple(IMAGE_FORMATS), "auto"),
        "animations": boolean(params, "animations", True),
        "vertex_colors": _choice(params, "vertex_colors", tuple(VERTEX_COLORS), "material"),
        "tangents": boolean(params, "tangents", False),
        "texcoords": boolean(params, "texcoords", True),
        "lights": boolean(params, "lights", False),
        "cameras": boolean(params, "cameras", False),
        "clean_names": boolean(params, "clean_names", target == "godot"),
        "at_origin": boolean(params, "at_origin", False),
    }
    if target == "godot" and options["draco"]:
        raise CommandError("bad_argument", "Godot cannot load KHR_draco_mesh_compression: its glTF importer does not "
                                           "support it, so the import fails.",
                           "Leave draco off for godot. Target threejs takes Draco, when the three.js app sets up a "
                           "DRACOLoader.")
    return options


def _exporter_settings(options):
    """Return the glTF exporter's own keyword arguments for the options.

    The keywords are those the glTF exporter's operator (io_scene_gltf2/__init__.py) defines, the same in
    Blender 4.2 and 5.0.
    Each is given, never left to a default that changed between versions. With vertex colours
    'material', only colours a material reads are exported: the exporter's defaults would also add the
    active colour to a mesh with no material, and a stand-in COLOR_0 to a mesh whose materials read none.
    """
    return {
        "export_draco_mesh_compression_enable": options["draco"],
        "export_image_format": IMAGE_FORMATS[options["image_format"]],
        "export_animations": options["animations"],
        "export_vertex_color": VERTEX_COLORS[options["vertex_colors"]],
        "export_all_vertex_colors": False,
        "export_active_vertex_color_when_no_material": False,
        "export_tangents": options["tangents"],
        "export_texcoords": options["texcoords"],
        "export_lights": options["lights"],
        "export_cameras": options["cameras"],
    }


def _selected(obj):
    """Return whether ``obj`` is selected, and False when it is not in this view layer."""
    try:
        return obj.select_get()
    except RuntimeError:  # not in this view layer
        return False


def _shown(obj):
    """Return whether ``obj`` is visible, and False when it is not in this view layer."""
    try:
        return obj.visible_get()
    except RuntimeError:  # not in this view layer
        return False


def _draws_geometry(obj, depth=0):
    """Return whether the exporter writes a surface for ``obj``.

    It does for a mesh, a curve, a surface or a text object (each written as the mesh it makes), and for a
    collection instance of any of them, searched up to 16 levels deep. It does not for a metaball, which it
    writes as a node with no mesh.
    """
    if obj.type in measure.EXPORTED_GEOMETRY:
        return True
    collection = obj.instance_collection if obj.instance_type == "COLLECTION" else None
    return collection is not None and depth < 16 and any(_draws_geometry(o, depth + 1) for o in collection.all_objects)


def _node_indices(document):
    """Return {node name: its first index} in the written file, read before any name is cleaned."""
    out = {}
    for index, node in enumerate(document.get("nodes", [])):
        if isinstance(node, dict) and isinstance(node.get("name"), str):
            out.setdefault(node["name"], index)
    return out


def _has_vertex_colours(document):
    """Return whether any mesh primitive in the written file has a COLOR_0 attribute."""
    return any("COLOR_0" in primitive.get("attributes", {}) for mesh in document.get("meshes", [])
               for primitive in mesh.get("primitives", []) if isinstance(primitive, dict))


def _refuse_textures_without_uvs(targets):
    """Refuse texcoords false while an exported material maps an image, since its texture would have nowhere to go."""
    textured = sorted({slot.material.name for obj in targets for slot in getattr(obj, "material_slots", [])
                       if slot.material is not None and checks.material_images(slot.material)[0]})
    if textured:
        which = f"the material {textured[0]} maps" if len(textured) == 1 else \
            f"the materials {joined(textured[:6])} map"
        raise CommandError("bad_argument", f"texcoords false leaves out the UVs, and {which} an image through them.",
                           "Export with texcoords true, or take the images out of those materials.")


def _export_roots(targets):
    """Return the objects exported without their parent, each of which is a root of the file."""
    chosen = set(targets)
    return [obj for obj in targets if obj.parent not in chosen]


def _refuse_moving_animated_roots(roots):
    """Refuse at_origin for roots whose location is animated, since the animation would put them back on every frame."""
    moving = [obj.name for obj in roots if {"location", "delta_location"} & motion.animated_paths(obj)]
    if moving:
        its, it = ("its location is", "it") if len(moving) == 1 else ("their locations are", "them")
        raise CommandError("bad_argument", f"at_origin cannot move {joined(moving[:6])}: {its} animated, and the "
                                           f"animation places {it} on every frame.",
                           "Key the animation about the origin, or export without at_origin.")


def _roots_to_origin(roots, undo):
    """Move each root's world location to the origin, recording in ``undo`` how to put it back.

    The root is moved by changing its location in its parent's space; its rotation and scale stay.
    """
    for obj in roots:
        place = obj.matrix_world.translation
        if place.length == 0:
            continue
        frame = (obj.parent.matrix_world @ obj.matrix_parent_inverse).to_3x3() if obj.parent is not None \
            else Matrix.Identity(3)
        undo.set(obj, "location", obj.location - frame.inverted_safe() @ place)
    update()


def export_glb(params):
    """Export objects to a .glb file for a target engine, and return what the file holds."""
    path = require(params, "path")
    if not isinstance(path, str) or not path.lower().endswith(".glb"):
        raise CommandError("bad_argument", f"'{path}' does not end in .glb.", "Name the file *.glb.")
    visible_only = boolean(params, "visible_only", True)
    # Custom properties are exported as glTF extras unless asked not to, since Godot 4.4 and later read them as
    # node metadata.
    extras = boolean(params, "extras", True)
    apply = boolean(params, "apply_modifiers", True)
    options = _export_options(params)
    # Bring the view layer up to date first: until then it lacks objects linked since the last
    # update, which then cannot be selected and are silently left out of the file.
    update()
    skipped = {kind for kind, wanted in (("CAMERA", options["cameras"]), ("LIGHT", options["lights"])) if not wanted}
    targets = [o for o in _targets_for_export(params) if o.type not in skipped]
    if visible_only:
        targets = [o for o in targets if _shown(o)]
    left_out = []
    if options["target"] == "threejs":
        # Objects named as Godot collision-only proxies are never drawn in Godot, but three.js would draw them.
        left_out = [o.name for o in targets if checks.godot_collision_proxy(o.name)]
        targets = [o for o in targets if o.name not in left_out]
    if not any(_draws_geometry(o) for o in targets):
        kinds = ("Meshes, curves, surfaces and text are written as meshes, and so are collection instances of them. "
                 "A metaball is written with no mesh, so convert it to a mesh first.")
        if visible_only:
            raise CommandError("not_found", "Nothing to export: none of the visible objects chosen is written as a "
                                            "mesh.",
                               f"get_scene shows each object's type and visibility. {kinds} Pass visible_only false "
                               "to include hidden objects.")
        raise CommandError("not_found", "Nothing to export: none of the objects chosen is written as a mesh.",
                           f"get_scene shows each object's type. {kinds}")
    refuse_outside_object_mode(targets, "the exporter would read stale or broken mesh data, or switch it to "
                                        "object mode")
    owners = _extras_owners(targets) if extras else []
    _refuse_unwritable_extras(owners)
    if not options["texcoords"]:
        _refuse_textures_without_uvs(targets)
    roots = _export_roots(targets) if options["at_origin"] else []
    _refuse_moving_animated_roots(roots)
    hidden_extras = any(key.startswith("_") for owner in owners for key in owner.keys())
    layer = bpy.context.view_layer
    scene = bpy.context.scene
    previous_selection = [o for o in layer.objects if o.select_get()]
    previous_active = layer.objects.active
    # The exporter goes to frame 0 and then back to the frame without its subframe, and does not go back at
    # all when it fails, so the frame is put back afterwards.
    previous_frame = (scene.frame_current, scene.frame_subframe)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    undo = Rollback()
    try:
        if not visible_only:
            _reveal(targets, undo)
        # Each root is written as if it stood at the origin, and put back with the other changes afterwards.
        _roots_to_origin(roots, undo)
        for obj in layer.objects:
            obj.select_set(False)
        for obj in targets:
            try:
                obj.select_set(True)
            except RuntimeError:
                pass  # not in the view layer at all; the MCP server's read-back of the file reports it as missing
        # select_set silently does nothing for an object that cannot be selected: one that is hidden, or
        # unselectable itself or through a collection. Those are revealed too, and selected again.
        missed = [o for o in targets if not _selected(o)]
        if missed:
            _reveal(missed, undo)
            for obj in missed:
                try:
                    obj.select_set(True)
                except RuntimeError:
                    pass
        result = bpy.ops.export_scene.gltf(
            filepath=path,
            check_existing=False,
            export_format="GLB",
            use_selection=True,
            use_visible=False,
            export_apply=apply,
            export_yup=True,
            export_materials="EXPORT",
            export_extras=extras,
            **_exporter_settings(options),
        )
    finally:
        for obj in layer.objects:
            obj.select_set(False)
        undo.undo()
        if (scene.frame_current, scene.frame_subframe) != previous_frame:
            try:
                scene.frame_set(previous_frame[0], subframe=previous_frame[1])
            except (RuntimeError, TypeError, ValueError):
                Rollback.failures += 1  # counted as a change, since the file is left on another frame
        update()
        for obj in previous_selection:
            try:
                obj.select_set(True)
            except (RuntimeError, ReferenceError):
                pass
        try:
            layer.objects.active = previous_active
        except (RuntimeError, ValueError, ReferenceError):
            pass
    if "FINISHED" not in result or not os.path.isfile(path):
        raise CommandError("blender_error", f"The glTF exporter did not write {path} ({sorted(result)}).")
    # The JSON chunk is read once and written back at most once, with whichever of the two edits apply, so the
    # read-back sees the file as it ends up. The nodes' indices are read first, by the names the exporter gave
    # them, which are the objects' names.
    edits = ([_without_hidden_extras] if hidden_extras else []) + ([_clean_names] if options["clean_names"] else [])
    indices, colours, *results = _rewrite_json(path, _node_indices, _has_vertex_colours, *edits, write=bool(edits))
    renamed = results[-1] if options["clean_names"] else {"nodes": {}}
    names = renamed["nodes"]
    out = {"path": path, "bytes": os.path.getsize(path), "target": options["target"], "options": options,
           # The names the file holds, which the MCP server's read-back checks the file against.
           "objects": [names.get(o.name, o.name) for o in targets]}
    if names:
        out["renamed"] = names
    data_renamed = {kind: renamed[kind] for kind in ("meshes", "materials") if renamed.get(kind)}
    if data_renamed:
        out["renamed_data"] = data_renamed
    if left_out:
        out["left_out"] = left_out
    # The exporter writes a metaball as a node with no mesh, which the read-back cannot tell from an empty.
    unmeshed = [o.name for o in targets if o.type in measure.GEOMETRY and o.type not in measure.EXPORTED_GEOMETRY]
    if unmeshed:
        one = len(unmeshed) == 1
        out["warnings"] = [{
            "code": "surface_not_exported", "count": len(unmeshed), "objects": unmeshed[:40],
            "message": f"The glTF exporter wrote {quoted(unmeshed[:6])} as {'a node' if one else 'nodes'} with no "
                       f"mesh, since it does not convert metaballs to meshes. Convert {'it' if one else 'them'} to "
                       "a mesh first: Object > Convert > Mesh in Blender, or bpy.ops.object.convert(target='MESH') "
                       "through run_python."}]
    # Objects with shape keys, each with its node in the file, so the read-back can report morph targets that
    # were lost. The node is given by its index, since cleaning the names can give two nodes the same name.
    morphs = {o.name: {"node": indices[o.name], "count": len(o.data.shape_keys.key_blocks) - 1} for o in targets
              if o.type == "MESH" and o.data.shape_keys is not None and len(o.data.shape_keys.key_blocks) > 1
              and o.name in indices}
    if morphs:
        out["morph_targets"] = morphs
    if options["image_format"] == "none" and options["vertex_colors"] == "material" and not colours:
        reading = sorted({slot.material.name for o in targets for slot in getattr(o, "material_slots", [])
                          if slot.material is not None and paint.base_color_chain(slot.material)[0] is not None})
        if reading:
            out["note"] = (f"The file has no COLOR_0 vertex colours, though {quoted(reading[:6])} "
                           f"{'reads' if len(reading) == 1 else 'read'} a color attribute. With image_format none, "
                           "the exporter does not trace the base colour back to the color attribute, so it writes no "
                           "vertex colours. Export with another image_format, or with vertex_colors active.")
    return out


#: The importer for each file extension import_file reads, as (operator module, operator name).
IMPORTERS = {
    ".glb": ("import_scene", "gltf"),
    ".gltf": ("import_scene", "gltf"),
    ".obj": ("wm", "obj_import"),
    ".fbx": ("import_scene", "fbx"),
    ".stl": ("wm", "stl_import"),
    ".ply": ("wm", "ply_import"),
}


#: The bpy.data collections importers add to, so a failed import can remove everything it made.
IMPORTED_KINDS = ("objects", "meshes", "materials", "collections", "images", "textures", "node_groups", "cameras",
                  "lights", "curves", "armatures", "actions", "worlds", "lattices")


def _datablocks():
    """Return the datablocks of each kind in IMPORTED_KINDS, as {kind: set of datablocks}."""
    return {kind: set(getattr(bpy.data, kind, ())) for kind in IMPORTED_KINDS}


def _remove_new(before):
    """Remove every datablock made since ``before``: what a failed import left behind."""
    new = [block for kind in IMPORTED_KINDS for block in getattr(bpy.data, kind, ()) if block not in before[kind]]
    if new:
        bpy.data.batch_remove(new)


def _put_back_properties(owner, kept):
    """Restore ``owner``'s custom properties to ``kept``.

    The glTF importer writes a file's scene extras into the open scene's custom properties, and a failed
    import leaves them there.
    """
    for key in [k for k in owner.keys() if k not in kept]:
        del owner[key]
    for key, value in kept.items():
        if key not in owner.keys() or _plain(owner[key]) != value:
            owner[key] = value


#: The kinds of datablock import_file appends from a .blend. Its parameters are named after them, as bpy.data
#: names them.
APPENDABLE = ("objects", "collections", "materials")
#: The ID collections an append from a .blend never adds to: those of the user interface.
UI_KINDS = ("window_managers", "screens", "workspaces")


def _every_datablock():
    """Return every datablock in the file by kind, except the user interface's, since an append may add any kind."""
    return {kind: set(getattr(bpy.data, kind)) for kind in dir(bpy.data)
            if kind not in UI_KINDS and isinstance(getattr(bpy.data, kind, None), bpy.types.bpy_prop_collection)}


def _remove_added(before):
    """Remove every datablock made since ``before``, from ``_every_datablock``: what a failed append left behind."""
    new = [block for kind, had in before.items() for block in getattr(bpy.data, kind) if block not in had]
    if new:
        bpy.data.batch_remove(new)


def _names_listed(names, limit=40):
    """Return up to ``limit`` names joined by commas, then how many more there are, or "none"."""
    shown = ", ".join(names[:limit]) or "none"
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _append_requests(params):
    """Return the names import_file was asked to append, by kind, each list checked and without repeats."""
    asked = {}
    for kind in APPENDABLE:
        names = params.get(kind)
        if names is None:
            continue
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            raise CommandError("bad_argument", f"'{kind}' must be a list of names.")
        if names:
            asked[kind] = list(dict.fromkeys(names))
    return asked


def _read_blend(path, hierarchy):
    """Return what the .blend at ``path`` holds, by kind, and with ``hierarchy`` each object's children by name.

    The file is read by linking it into temporary data, which leaves the open file untouched, and in a single
    load: loading the names on their own, then the hierarchy, then appending, makes Blender 5.0 crash as it
    exits.
    """
    children = {}
    try:
        with bpy.data.temp_data() as temp:
            with temp.libraries.load(path, link=True) as (source, target):
                available = {kind: list(getattr(source, kind)) for kind in APPENDABLE}
                if hierarchy:
                    target.objects = list(source.objects)
            for obj in temp.objects:
                if obj.parent is not None:
                    children.setdefault(obj.parent.name, []).append(obj.name)
    except OSError as exc:
        raise CommandError("bad_argument", f"Cannot read {path} as a .blend: {exc}.") from None
    return available, children


def _with_descendants(roots, children):
    """Return ``roots`` and every object below them.

    Appending an object brings what it depends on, its parent among them, but not its children, which depend
    on it, so the children are named too.
    """
    out, todo = [], list(roots)
    while todo:
        name = todo.pop(0)
        if name not in out:
            out.append(name)
            todo.extend(children.get(name, []))
    return out


def _import_blend(path, params, collection):
    """Append the named objects (with their children), collections and materials from a .blend.

    The appended data becomes local to the open file; nothing is linked. What is appended goes into
    ``collection``, or into the scene's own collection when none is named.
    """
    asked = _append_requests(params)
    available, children = _read_blend(path, hierarchy="objects" in asked)
    if not asked:
        raise CommandError("bad_argument", "Name what to append from the .blend: 'objects', 'collections' or "
                                           "'materials'.",
                           " ".join(f"{kind.capitalize()} in it: {_names_listed(available[kind])}."
                                    for kind in APPENDABLE))
    for kind, names in asked.items():
        missing = [n for n in names if n not in available[kind]]
        if missing:
            named = f"{kind[:-1]} named {missing[0]}" if len(missing) == 1 else f"{kind} named {joined(missing)}"
            raise CommandError("not_found", f"{path} has no {named}.",
                               f"{kind.capitalize()} in it: {_names_listed(available[kind])}.")
    if "objects" in asked:
        asked["objects"] = _with_descendants(asked["objects"], children)
    scene = bpy.context.scene
    before = _every_datablock()
    with Rollback() as undo:
        undo.add(lambda: _remove_added(before))
        with bpy.data.libraries.load(path, link=False) as (_source, target):
            for kind, names in asked.items():
                setattr(target, kind, names)
        for kind, names in asked.items():
            failed = [name for name, block in zip(names, getattr(target, kind), strict=True) if block is None]
            if failed:
                raise CommandError("blender_error", f"Blender did not append {', '.join(failed)} from {path}.")
        host = get_collection(collection, create=True, undo=undo) if collection else scene.collection
        appended = list(target.collections)
        for block in appended:
            # A collection inside another appended collection is already in the scene through that one.
            if not any(block in other.children_recursive for other in appended):
                host.children.link(block)
        new_collections = [c for c in bpy.data.collections if c not in before["collections"]]
        new_objects = [o for o in bpy.data.objects if o not in before["objects"]]
        for obj in new_objects:
            # An object in no collection has no user, and saving the file would drop it.
            if not obj.users_collection:
                host.objects.link(obj)
        # Appending leaves the source file listed as a library that nothing is linked from, which would be
        # reported as a link this file holds.
        for library in [lib for lib in bpy.data.libraries if lib not in before["libraries"]]:
            if not any(block.library == library for kind in before for block in getattr(bpy.data, kind)):
                bpy.data.libraries.remove(library)
    update()
    return {
        "path": path,
        "objects": [object_summary(o) for o in new_objects],
        "collections": [c.name for c in new_collections],
        "materials": [m.name for m in bpy.data.materials if m not in before["materials"]],
        "libraries": linked_libraries(),
    }


def import_file(params):
    """Import a .glb, .gltf, .obj, .fbx, .stl or .ply file, or append from a .blend, and return what came in."""
    path = require(params, "path")
    if not isinstance(path, str) or not os.path.isfile(path):
        raise CommandError("not_found", f"No file at {path}.", FILE_HINT)
    extension = os.path.splitext(path)[1].lower()
    if extension not in IMPORTERS and extension != ".blend":
        raise CommandError("bad_argument", f"Cannot import {extension or 'a file with no extension'}.",
                           "import_file reads " + joined(sorted([*IMPORTERS, ".blend"])) + " files.")
    named = [kind for kind in APPENDABLE if params.get(kind)]
    if named and extension != ".blend":
        raise CommandError("bad_argument", f"'{named[0]}' names what to append from a .blend, but a {extension} "
                                           "file is imported whole.", f"Leave out {joined(named)}.")
    collection = text(params, "collection")
    existing = bpy.data.collections.get(collection) if collection else None
    if existing is not None and not in_scene(existing):
        # Checked before importing, which may take a while; get_collection would refuse it only after.
        raise CommandError("bad_argument", f"Collection '{collection}' is not in this scene, so what is imported "
                                           "into it would not be either.", OUTSIDE_SCENE_HINT)
    # The importers switch the active object to object mode, and select and activate what they make, so
    # an import would take the user out of edit mode without asking.
    scene = bpy.context.scene
    refuse_outside_object_mode([o for o in scene.objects if o.mode != "OBJECT"],
                               "nothing is appended until it is back in object mode" if extension == ".blend"
                               else "the importer would switch it to object mode")
    if extension == ".blend":
        return _import_blend(path, params, collection)
    module, operator = IMPORTERS[extension]
    before = _datablocks()
    before_objects = before["objects"]
    before_collections = before["collections"]
    before_materials = before["materials"]
    kept = {key: _plain(scene[key]) for key in scene.keys()}
    with Rollback() as undo:
        undo.add(lambda: _remove_new(before))
        undo.add(lambda: _put_back_properties(scene, kept))
        result = getattr(getattr(bpy.ops, module), operator)(filepath=path)
        if "FINISHED" not in result:
            raise CommandError("blender_error", f"The {extension} importer did not finish ({sorted(result)}).")
        new_objects = [o for o in bpy.data.objects if o not in before_objects]
        if collection:
            target = get_collection(collection, create=True, undo=undo)
            for obj in new_objects:
                if obj.name not in target.objects:
                    target.objects.link(obj)
                for other in list(obj.users_collection):
                    if other != target:
                        other.objects.unlink(obj)
    update()
    return {
        "path": path,
        "objects": [object_summary(o) for o in new_objects],
        "collections": [c.name for c in bpy.data.collections if c not in before_collections],
        "materials": [m.name for m in bpy.data.materials if m not in before_materials],
        "libraries": linked_libraries(),
    }


def _refuse_if_unsaved(discard, action):
    """Refuse ``action`` when the file has unsaved changes, unless ``discard`` is true."""
    if unsaved() and not discard:
        where = bpy.data.filepath or "an unsaved new file"
        save = "Call save_file first" if bpy.data.filepath else "Call save_file with a path first"
        raise CommandError("unsaved_changes", f"The open file ({where}) has unsaved changes; {action} would lose them.",
                           f"{save}, or pass discard: true to drop them.")


@contextlib.contextmanager
def _no_backup_in_background():
    """Suspend Blender's .blend1 backup of the file being replaced, for saves in a background Blender.

    The agent's own checkpoints and undo serve instead. In live mode the user's own preference applies. The
    preference is put back afterwards, with its changed flag as it was, so nothing about it is saved.
    """
    preferences = bpy.context.preferences
    if not bpy.app.background:
        yield
        return
    kept = (preferences.filepaths.save_version, preferences.is_dirty)
    preferences.filepaths.save_version = 0
    try:
        yield
    finally:
        preferences.filepaths.save_version = kept[0]
        preferences.is_dirty = kept[1]


def save_file(params):
    """Save the open file, to ``path`` when given, or write a checkpoint copy with ``copy``."""
    path = params.get("path")
    copy = boolean(params, "copy", False)
    if path and not path.lower().endswith(".blend"):
        raise CommandError("bad_argument", f"'{path}' does not end in .blend.")
    if copy:
        # A checkpoint is a copy: the open file keeps its path and its unsaved state.
        if not path:
            raise CommandError("bad_argument", "'copy' writes a checkpoint to 'path', and none was given.",
                               "Pass 'path', ending in .blend; save_file without 'copy' saves the file itself.")
        if bpy.data.filepath and os.path.normcase(os.path.abspath(path)) == os.path.normcase(bpy.data.filepath):
            raise CommandError("bad_argument", f"'{path}' is the open file itself, not a checkpoint beside it.",
                               "Name another .blend for the checkpoint, or call save_file without 'copy'.")
        written = save_copy({"path": path})
        return {**written, "copy": True, "file": bpy.data.filepath or None, "unsaved_changes": unsaved()}
    if not path and not bpy.data.filepath:
        raise CommandError("bad_argument", "This file has never been saved, so it has no path yet.",
                           "Pass 'path', ending in .blend.")
    if path:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with _no_backup_in_background():
        if path:
            result = bpy.ops.wm.save_as_mainfile(filepath=path, check_existing=False)
        else:
            result = bpy.ops.wm.save_mainfile()
    if "FINISHED" not in result:
        raise CommandError("blender_error", f"Blender did not save ({sorted(result)}).")
    mark_saved()
    return {"path": bpy.data.filepath, "bytes": os.path.getsize(bpy.data.filepath),
            "unsaved_changes": unsaved()}


def open_file(params):
    """Open a .blend file, refusing to drop unsaved changes unless asked, and return its scene."""
    path = require(params, "path")
    # Checked even when there is nothing to lose: the string "false" is not false, and unchecked it would
    # count as true.
    discard = boolean(params, "discard", False)
    if not os.path.isfile(path):
        raise CommandError("not_found", f"No file at {path}.", FILE_HINT)
    _refuse_if_unsaved(discard, "opening another file")
    result = bpy.ops.wm.open_mainfile(filepath=path)
    if "FINISHED" not in result:
        raise CommandError("blender_error", f"Blender did not open {path} ({sorted(result)}).")
    mark_saved()
    return {**get_scene({}), "libraries": linked_libraries()}


def reset_to_empty():
    """Replace the open file with Blender's empty start file, and count it as saved."""
    bpy.ops.wm.read_homefile(use_empty=True)
    mark_saved()


def create_file(params):
    """Start a new file, empty or from Blender's start file, refusing to drop unsaved changes unless asked."""
    discard = boolean(params, "discard", False)
    empty = boolean(params, "empty", True)
    _refuse_if_unsaved(discard, "starting a new file")
    if empty:
        reset_to_empty()
    else:
        bpy.ops.wm.read_homefile()
        mark_saved()
    return get_scene({})


def linked_libraries():
    """Return the absolute path of every .blend library the open file links, directly or through another library.

    Opening a file reads its libraries from wherever they point, and the MCP server's BLENDERLENS_ALLOWED_DIRS
    limits only the paths given to tools, so the result names the libraries and the server can warn about any
    outside the allowed folders. A relative path is resolved against the file that links it: the open file,
    or for an indirect link its parent library.
    """
    return [os.path.normpath(bpy.path.abspath(library.filepath, library=library.parent))
            for library in bpy.data.libraries]


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

#: Every command by name, as (handler, whether it changes the file).
COMMANDS = {
    # Inspecting the scene
    "get_scene": (get_scene, False),
    "get_object": (get_object_details, False),
    "get_mesh": (meshops.get_mesh, False),
    "list_materials": (list_materials, False),
    # Building and editing meshes
    "create_object": (meshops.create_object, True),
    "create_mesh": (meshops.create_mesh, True),
    "edit_mesh": (meshops.edit_mesh, True),
    "join_objects": (join_objects, True),
    # Modifiers
    "add_modifier": (add_modifier, True),
    "set_modifier": (set_modifier, True),
    "apply_modifier": (apply_modifier, True),
    "remove_modifier": (remove_modifier, True),
    # Materials, vertex colours, UVs and baking
    "set_material": (set_material, True),
    "set_vertex_color": (paint.set_vertex_color, True),
    "unwrap_uv": (uv.unwrap_uv, True),
    "bake_texture": (bake.bake_texture, True),
    # Placing and organising objects
    "set_transform": (set_transform, True),
    "apply_transform": (apply_transform, True),
    "set_parent": (set_parent, True),
    "set_collection": (set_collection, True),
    "set_object": (set_object, True),
    "duplicate_object": (duplicate_object, True),
    "delete_object": (delete_object, True),
    # Animation and collision
    "animate_object": (motion.animate_object, True),
    "create_collision": (collision.create_collision, True),
    # Checking, measuring and previews, none of which changes the file
    "check_asset": (checks.check_asset, False),
    "measure_objects": (measure.measure_objects, False),
    "render_preview": (preview.render_preview, False),
    # Files: exporting, importing, saving and opening
    "export_glb": (export_glb, False),
    "export_file": (formats.export_file, False),
    "import_file": (import_file, True),
    "save_file": (save_file, False),
    "open_file": (open_file, False),
    "create_file": (create_file, False),
    # Python and undo
    "run_python": (scripts.run_python, True),
    "run_script": (scripts.run_script, True),
    "undo_change": (undo_stack.undo_change, True),
}

#: Commands that change the file but push no undo step: a step pushed after an undo would be the next one
#: undone, so a second undo_change would take back nothing.
PUSHES_NO_STEP = ("undo_change",)


#: Commands whose result names the object they made, which the name of their undo step then includes.
MAKES_AN_OBJECT = ("create_object", "create_mesh")


def _acted_on(command, params, result):
    """Return the object a call acted on, for its undo step's name, or None when it acted on none.

    It is the call's ``object``, else the first of its ``objects`` with how many more there are, else the
    object it made.
    """
    named = params.get("object")
    if isinstance(named, str) and named:
        return named
    many = params.get("objects")
    if isinstance(many, list) and many and all(isinstance(n, str) for n in many):
        return many[0] + (f" +{len(many) - 1}" if len(many) > 1 else "")
    if command in MAKES_AN_OBJECT and isinstance(result, dict) and isinstance(result.get("name"), str):
        return result["name"]
    return None


def _after_change(command, refused=False, acted_on=None):
    """Push the undo step for a call that may have changed the file, and redraw so the user sees the change.

    The step is pushed in live mode and in a background Blender alike; the redraw matters only in live mode.
    """
    _watching(undo_stack.record, command, refused, acted_on)
    try:
        wm = bpy.context.window_manager
        for win in getattr(wm, "windows", []):
            for area in win.screen.areas:
                area.tag_redraw()
    except Exception:  # noqa: BLE001 -- a failed redraw must never hide the command's own result or failure
        pass


def save_copy(params):
    """Write a copy of the open file to ``path``, leaving the file's own path and unsaved state as they were.

    This is an internal command, like hello, and not a tool. The MCP server calls it when its client goes
    away while a background Blender holds unsaved work, so that work outlives the session.
    """
    path = require(params, "path")
    if not isinstance(path, str) or not path.lower().endswith(".blend"):
        raise CommandError("bad_argument", f"'{path}' does not end in .blend.")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    result = bpy.ops.wm.save_as_mainfile(filepath=path, copy=True, check_existing=False)
    if "FINISHED" not in result:
        raise CommandError("blender_error", f"Blender did not save a copy ({sorted(result)}).")
    return {"path": path, "bytes": os.path.getsize(path)}


def dispatch(command, params, mode):
    """Run one command and return its result, raising CommandError when the command refuses.

    hello and save_copy run outside the undo watch (undo.py's record of what each call does to Blender's
    undo history). Every other command is watched, and one that may have changed the file is counted as
    unsaved work and, unless it is undo_change, pushed as an undo step.
    """
    if command == "hello":
        return hello(params, mode)
    if command == "save_copy":
        return save_copy(params if isinstance(params, dict) else {})
    entry = COMMANDS.get(command)
    if entry is None:
        raise CommandError("bad_argument", f"Blender has no command '{command}'.",
                           "The add-on may be older than the server; reinstall it with blenderlens-mcp install-addon.")
    handler, changes = entry
    if not isinstance(params, dict):
        raise CommandError("bad_argument", "'params' must be a JSON object.")
    if command == "undo_change" and params.get("list") is True:
        # Listing the steps only reads them; stepping back is what changes the file.
        changes = False
    global _changed
    failures = Rollback.failures
    finished = False
    error = None
    quiet = False
    result = None
    # The watch counts any change made since the last call as made outside BlenderLens, and every change from
    # here to the end of this call as the call's own.
    _watching(undo_stack.begin, command, params)
    if changes and command not in PUSHES_NO_STEP:
        # In a background Blender, push the step this change is undone back to.
        _watching(undo_stack.ensure_base)
    try:
        result = handler(params)
        finished = True
    except BaseException as exc:
        error = exc
        raise
    finally:
        # A command that failed halfway may still have changed something. The undo push happens
        # here, whatever happened, so even an unexpected half-change is its own step and one undo
        # never takes back two calls. A refusal that put everything back changed nothing: it is not
        # counted, and pushes no step, since in live mode an undo step itself marks the file as
        # modified.
        quiet = _watching(undo_stack.settle) is True
        if _may_have_changed(command, changes, finished, error, failures, quiet):
            _changed = True
            if command not in PUSHES_NO_STEP:
                _after_change(command, refused=not finished, acted_on=_acted_on(command, params, result))
            elif finished and bpy.app.background and _watching(undo_stack.at_saved) is True:
                # Undone back to where the file was saved or opened, so it counts as saved again. This is done
                # only in a background Blender; in live mode the file counts as unsaved after any undo.
                _changed = False
        _watching(undo_stack.end)
    return result


def _watching(step, *args):
    """Run one step of the undo watch (see undo.py) and return its result, or None if it fails.

    The watch must never stop a command or hide its result, so its failures are swallowed.
    """
    try:
        return step(*args)
    except Exception:  # noqa: BLE001 -- the command's own result or failure is what the caller gets
        return None


def _may_have_changed(command, changes, finished, error, failures, quiet=False):
    """Return whether a call may have changed the file.

    It is True whenever a rollback could not put everything back. For run_python it is True whatever
    happened, since the code may have changed anything before it raised, unless the undo watch saw nothing
    change at all (``quiet``). For run_script it is True once the script's code ran. For any other command
    that changes things, it is True when the command finished. A refusal (CommandError), or an error that a
    rollback put everything back for, is no change; any other error from a command that changes things may
    have left one.
    """
    if Rollback.failures > failures:
        return True
    if command == "run_python":
        return not quiet
    if command == "run_script" and not finished:
        # A refusal other than python_error came before the script started (its path, the "Allow Python from
        # the agent" preference, unsaved work), so nothing ran.
        return not isinstance(error, CommandError) or error.kind == "python_error"
    if not changes:
        return False
    if finished:
        return True
    return not (isinstance(error, CommandError) or getattr(error, "blenderlens_rolled_back", False))


def describe_failure(exc):
    """Return the fields of the failure response for an exception that escaped a command.

    A CommandError gives its message, kind, hint and extra fields. Any other exception is reported as a
    blender_error with the end of its traceback.
    """
    if isinstance(exc, CommandError):
        out = {"error": exc.message, "kind": exc.kind}
        if exc.hint:
            out["hint"] = exc.hint
        out.update(exc.extra)
        return out
    tail = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return {"error": f"{type(exc).__name__}: {exc}", "kind": "blender_error",
            "traceback": "".join(tail)[-3000:]}


def log(message):
    """Write ``message`` to Blender's stderr, marked as BlenderLens's."""
    print(f"[blenderlens] {message}", file=sys.stderr, flush=True)
