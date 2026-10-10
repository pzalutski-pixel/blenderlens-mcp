"""Bake a material's look into a PNG with Cycles on the CPU.

Cycles bakes into the active Image Texture node of every material on the object, in the layout of the
UV map named. So a temporary image node holding a new image is made the active node of each material
for the bake, and removed afterwards. The render engine, Cycles' device and samples, and whether the
objects render, are put back whatever happens. The bake operator takes its settings as arguments, so
the scene's own bake settings are never touched, and it reads the target and the source objects from a
context override, so the selection and the active object stay as the user left them.

With use_in_material, the PNG then feeds the material the way the glTF exporter reads it: the base
colour for diffuse and combined bakes, the base colour multiplied by it for ambient occlusion (glTF's
baseColorFactor times baseColorTexture), a Normal Map node for normal bakes, and the emission colour for
emit bakes.
"""

import os
import time

import bpy

# The commands module imports this one to build its table of commands, so the helpers of commands are
# looked up on the module when a bake runs, rather than imported by name here, which would be circular.
from . import commands, paint, preview
from .common import (
    CommandError,
    Rollback,
    boolean,
    get_mesh_object,
    quoted,
    refuse_outside_object_mode,
    require,
    rnd,
    text,
    update,
    world_bounds,
)

#: The bake types bake_texture takes, each with Cycles' name for it and the passes it bakes (None leaves
#: Cycles' default passes).
TYPES = {"diffuse": ("DIFFUSE", {"COLOR"}), "ao": ("AO", None), "normal": ("NORMAL", None),
         "emit": ("EMIT", None), "combined": ("COMBINED", None)}
#: The channel of the material that each bake type feeds when the PNG is wired in.
CHANNELS = {"diffuse": "base_color", "combined": "base_color", "ao": "base_color", "normal": "normal",
            "emit": "emission"}
#: The smallest side of the baked image, in pixels.
MIN_SIZE = 16
#: The largest side of the baked image, in pixels.
MAX_SIZE = 4096
#: The most samples a bake may take.
MAX_SAMPLES = 4096
#: The side of the baked image, in pixels, when no size is given.
DEFAULT_SIZE = 1024
#: The samples a bake takes when none are given.
DEFAULT_SAMPLES = 32
#: The most objects from_objects may list.
MAX_SOURCES = 64
#: How far the target's surface is pushed out to meet a high-poly source, as a share of its diagonal.
CAGE_SHARE = 0.02

#: The name of the temporary image node that Cycles bakes into.
TARGET_NODE = "BlenderLens Bake Target"
#: The name of the image node that holds a baked ambient occlusion in the material.
OCCLUSION_IMAGE_NODE = "BlenderLens Occlusion Image"
#: The name of the node that multiplies the base colour by the ambient occlusion image.
OCCLUSION_MIX_NODE = "BlenderLens Occlusion Mix"


def _whole(params, key, default, low, high):
    """Return a whole-number parameter from ``low`` to ``high``, or ``default`` when it is not given."""
    value = params.get(key)
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise CommandError("bad_argument", f"'{key}' must be a whole number from {low} to {high}, got {value!r}.")
    return value


def _uv_map(obj, params):
    """Return the name of the UV map to bake into: the one named, else the active one."""
    mesh = obj.data
    name = text(params, "uv_map")
    if not mesh.uv_layers:
        raise CommandError("bad_argument", f"'{obj.name}' has no UV map to bake into.",
                           "Call unwrap_uv to make one. Its method smart needs no seams.")
    if name is None:
        return mesh.uv_layers.active.name
    if mesh.uv_layers.get(name) is None:
        raise CommandError("bad_argument", f"'{obj.name}' has no UV map named '{name}'.",
                           "Its UV maps are: " + ", ".join(layer.name for layer in mesh.uv_layers)
                           + ". Call unwrap_uv to make another.")
    return name


def _sources(obj, params):
    """Return the mesh objects named in from_objects, which the bake takes its look from."""
    names = params.get("from_objects")
    if names is None:
        return []
    if not isinstance(names, list):
        raise CommandError("bad_argument", "'from_objects' must be a list of object names.")
    if len(names) > MAX_SOURCES:
        raise CommandError("bad_argument", f"'from_objects' lists {len(names)} objects, and bake_texture takes at "
                                           f"most {MAX_SOURCES}.")
    out = []
    for name in names:
        source = get_mesh_object(name, "from_objects")
        if source == obj:
            raise CommandError("bad_argument", f"'{name}' is the object being baked into, so it cannot also be in "
                                               "from_objects, which lists the objects baked from.")
        if source not in out:
            out.append(source)
    return out


def _reaches_output(material, node):
    """Return whether ``node`` feeds the surface of the material's Cycles output, directly or through other nodes.

    The search follows links through nodes such as a Mix Shader, and skips muted links.
    """
    tree = material.node_tree
    output = tree.get_output_node("CYCLES") if hasattr(tree, "get_output_node") else None
    if output is None:
        return False
    surface = output.inputs.get("Surface")
    stack = [link.from_node for link in (surface.links if surface is not None else ()) if not link.is_muted]
    seen = set()
    while stack:
        current = stack.pop()
        if current.name in seen:
            continue
        seen.add(current.name)
        if current == node:
            return True
        stack += [link.from_node for socket in current.inputs for link in socket.links if not link.is_muted]
    return False


def _check_principled(materials):
    """Raise a CommandError for a material whose output no Principled BSDF feeds.

    The bake goes through the material's Principled BSDF, which is made if the material has no nodes at
    all, and the PNG is wired into it. A shader without one would be rewired, and baked as something it
    is not.
    """
    for material in materials:
        if material.node_tree is None:
            continue  # A material with no nodes yet gets a Principled BSDF and an output for the bake.
        node = paint.principled(material)
        if node is None or not _reaches_output(material, node):
            raise CommandError("bad_argument", f"Material '{material.name}' has no Principled BSDF feeding its "
                                               "output, so the bake cannot go through it.",
                               "Bake an object whose materials set_material made, or connect a Principled BSDF to "
                               "the Material Output. run_python can bake any other shader.")


def _check_occlusion(materials):
    """Raise a CommandError when occlusion would be wired into a base colour that something else already feeds.

    glTF has one base colour texture, and a texture multiplied by another texture cannot be written as one.
    """
    for material in materials:
        node = paint.principled(material)
        source = paint.feeding(node.inputs["Base Color"]) if node is not None else None
        if source is not None and source.name != OCCLUSION_MIX_NODE:
            raise CommandError("bad_argument", f"The base colour of '{material.name}' is already fed by "
                                               f"'{source.name}', so occlusion cannot multiply it in the file.",
                               "Bake combined instead, which takes the occlusion in, or pass use_in_material false "
                               "to keep the PNG apart.")


def _wire_occlusion(material, node, image, undo):
    """Feed the base colour with its own colour multiplied by the occlusion image, recording changes in ``undo``.

    The multiply node takes the base colour's current value in its first input and the image in its
    second, which the glTF exporter writes as baseColorFactor and baseColorTexture.
    """
    tree = material.node_tree
    target = node.inputs["Base Color"]
    mix = paint.own_node(tree, "ShaderNodeMix", OCCLUSION_MIX_NODE, undo, node, (-250.0, 150.0))
    for key, value in (("data_type", "RGBA"), ("blend_type", "MULTIPLY"), ("clamp_result", False)):
        if getattr(mix, key) != value:
            undo.set(mix, key, value)
    if mix.inputs["Factor"].default_value != 1.0:
        undo.set(mix.inputs["Factor"], "default_value", 1.0)
    colour = paint.socket_by_id(mix.inputs, "A_Color")
    if paint.feeding(target) != mix:
        undo.set(colour, "default_value", tuple(target.default_value))
    texture = paint.own_node(tree, "ShaderNodeTexImage", OCCLUSION_IMAGE_NODE, undo, node, (-600.0, 150.0))
    if texture.image != image:
        undo.set(texture, "image", image)
    paint.link(tree, texture.outputs["Color"], paint.socket_by_id(mix.inputs, "B_Color"), undo)
    paint.link(tree, paint.socket_by_id(mix.outputs, "Result_Color"), target, undo)


def _wire(kind, material, image, undo):
    """Feed the baked image into the channel of the material that a bake of ``kind`` belongs in."""
    node = paint.principled_for_change(material, undo)
    if kind in ("diffuse", "combined"):
        # The bake holds the whole base colour, a color attribute's share included, so the attribute is unwired.
        paint.wire_base_color(material, node, undo, attribute=None, image=image)
    elif kind == "ao":
        _wire_occlusion(material, node, image, undo)
    elif kind == "normal":
        commands._wire_normal(material, node, image, undo)
    else:
        commands._wire_emission(material, node, image, False, undo)


def _same_file(image, path):
    """Return whether ``image`` was loaded from the file at ``path``."""
    if not image.filepath or image.library is not None:
        return False
    try:
        return os.path.normcase(os.path.abspath(bpy.path.abspath(image.filepath))) == \
            os.path.normcase(os.path.abspath(path))
    except (ValueError, OSError):
        return False


def _discard(image):
    """Remove an image this call made, if it still exists."""
    try:
        bpy.data.images.remove(image)
    except ReferenceError:
        pass


def bake_texture(params):
    """Bake an object's look into a PNG with Cycles, and wire the PNG into its materials unless asked not to."""
    obj = get_mesh_object(require(params, "object"))
    kind = text(params, "type", "diffuse")
    if kind not in TYPES:
        raise CommandError("bad_argument", f"'type' takes one of {', '.join(TYPES)}, got {kind!r}.")
    size = _whole(params, "size", DEFAULT_SIZE, MIN_SIZE, MAX_SIZE)
    samples = _whole(params, "samples", DEFAULT_SAMPLES, 1, MAX_SAMPLES)
    if size * size * samples > preview.MAX_PIXEL_SAMPLES:
        raise CommandError("bad_argument", f"{size} x {size} pixels at {samples} samples is "
                                           f"{size * size * samples:,} pixel samples, more than the "
                                           f"{preview.MAX_PIXEL_SAMPLES:,} allowed.",
                           "Lower 'size' or 'samples'. Diffuse, normal and emit bakes need few samples.")
    path = preview._png_path(require(params, "path"))
    uv_map = _uv_map(obj, params)
    sources = _sources(obj, params)
    wire = boolean(params, "use_in_material", True)
    refuse_outside_object_mode([obj, *sources], "Blender cannot bake")
    if "cycles" not in preview.available_engines():
        raise CommandError("blender_error", "This Blender has no Cycles, and bake_texture bakes with Cycles.",
                           "Enable the Cycles add-on in Blender's preferences.")
    slots = obj.material_slots
    empty = [index for index, slot in enumerate(slots) if slot.material is None]
    if empty:
        raise CommandError("bad_argument", f"Material slot {empty[0]} of '{obj.name}' is empty, and Cycles bakes "
                                           "through each slot's material.", "Call set_material to put a material "
                                                                             "there.")
    materials = list(dict.fromkeys(slot.material for slot in slots))
    _check_principled(materials)
    if kind == "ao" and wire:
        _check_occlusion(materials)
    update()
    cage = 0.0
    bounds = world_bounds([obj]) if sources else None
    if bounds is not None:
        cage = max((bounds[1] - bounds[0]).length * CAGE_SHARE, 1e-4)

    scene = bpy.context.scene
    restore = Rollback()
    made_material = None
    started = time.time()
    with Rollback() as undo:
        # Changes only the bake needs are recorded in restore and put back after it. With use_in_material,
        # changes the wired material keeps are recorded in undo, which takes them back only if the call fails.
        holder = undo if wire else restore
        try:
            if not materials:
                # Cycles bakes through a material, so an object with none gets one. Like every material
                # BlenderLens makes, it culls back faces, so the glTF exporter does not mark it doubleSided.
                made_material = bpy.data.materials.new(obj.name)
                material = made_material
                holder.add(lambda: bpy.data.materials.remove(material))
                holder.set(material, "use_backface_culling", True)
                obj.data.materials.append(material)
                holder.add(lambda: obj.data.materials.pop())
                materials = [material]
            image = bpy.data.images.new(os.path.basename(path), size, size, alpha=False)
            undo.add(lambda: _discard(image))
            if kind == "normal":
                # A normal map holds directions, not colours, so its values are kept as they are, never
                # converted from sRGB.
                image.colorspace_settings.name = "Non-Color"
            for material in materials:
                paint.principled_for_change(material, holder)
                tree = material.node_tree
                target = paint.own_node(tree, "ShaderNodeTexImage", TARGET_NODE, restore)
                target.image = image
                previous = tree.nodes.active
                tree.nodes.active = target
                restore.add(lambda tree=tree, previous=previous: setattr(tree.nodes, "active", previous))
            preview._set_engine_restorable(scene, "cycles", restore)
            restore.set(scene.cycles, "device", "CPU")
            restore.set(scene.cycles, "samples", samples)
            for each in (obj, *sources):
                # Cycles bakes only objects enabled for rendering.
                if each.hide_render:
                    restore.set(each, "hide_render", False)
            update()
            cycles_type, passes = TYPES[kind]
            settings = {"type": cycles_type, "margin": max(2, size // 64), "uv_layer": uv_map, "use_clear": True,
                        "target": "IMAGE_TEXTURES", "save_mode": "INTERNAL",
                        "use_selected_to_active": bool(sources), "cage_extrusion": cage}
            if passes:
                settings["pass_filter"] = passes
            selected = [*sources, obj]
            try:
                with bpy.context.temp_override(object=obj, active_object=obj, selected_objects=selected,
                                               selected_editable_objects=selected):
                    result = bpy.ops.object.bake(**settings)
            except RuntimeError as exc:
                raise CommandError("blender_error", f"Cycles did not bake: {commands._blender_said(exc)}") from None
            if "FINISHED" not in result:
                raise CommandError("blender_error", f"Cycles did not finish the bake ({sorted(result)}).")
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            image.filepath_raw = path
            image.file_format = "PNG"
            image.save()
            if not os.path.isfile(path):
                raise CommandError("blender_error", f"Blender baked but wrote no file at {path}.")
        finally:
            restore.undo()
        seconds = time.time() - started
        # Images already loaded from this file are reloaded to show the new bake, and the first of them is
        # the one wired, so baking again to the same path leaves one image instead of adding one each time.
        loaded = [i for i in bpy.data.images if i != image and _same_file(i, path)]
        for other in loaded:
            other.reload()
        wired = {}
        if wire:
            for material in materials:
                _wire(kind, material, loaded[0] if loaded else image, undo)
                wired[material.name] = [CHANNELS[kind]]
    if not wire or loaded:
        _discard(image)
        image = loaded[0] if loaded else None
    update()
    out = {
        "object": obj.name,
        "type": kind,
        "path": path,
        "bytes": os.path.getsize(path),
        "size": [size, size],
        "samples": samples,
        "seconds": rnd(seconds, 2),
        "uv_map": uv_map,
        "image_driven": wired,
    }
    if sources:
        out["from_objects"] = [s.name for s in sources]
        out["cage_extrusion"] = rnd(cage, 5)
    if wire:
        out["image"] = image.name
        if made_material is not None:
            out["created_material"] = made_material.name
        shared = sorted({o.name for o in bpy.data.objects if o != obj and o.type == "MESH"
                         for slot in o.material_slots if slot.material in materials})
        if shared:
            show = "shows" if len(shared) == 1 else "show"
            out["note"] = (f"The materials the bake was wired into are also on {quoted(shared[:6])}, which now "
                           f"{show} this bake too.")
    return out
