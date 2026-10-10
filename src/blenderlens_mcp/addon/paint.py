"""Paint vertex colours into a color attribute, and wire attributes and images into a material's base colour.

Painting works on mesh data in object mode, so it behaves the same in a Blender window running the
add-on as in a background Blender. The glTF exporter writes a color attribute as COLOR_0 when a material
uses it (its export_vertex_color option defaults to MATERIAL in Blender 4.2 and 5.0 alike), and, by
default, writes the active one for an object with no material (export_active_vertex_color_when_no_material).
export_glb turns that second option off, so a painted attribute that no material reads would be left out
of its file without any warning. Painting therefore wires the attribute into the base colour of the
object's materials, giving the object a material if it has none, unless asked not to.

The nodes BlenderLens adds to a material carry fixed names, so a later call finds and rewires them
instead of piling up copies. Every node and link made or removed is recorded in the caller's
Rollback, so a call refused partway leaves the material as it was.

BlenderLens never takes over a node setup it did not make. It works through the Principled BSDF that
the material output renders, and gives a material one only where nothing feeds the output's surface.
A surface that another shader feeds (an Emission shader alone, for example) is left as it is, and so
is an input fed by a node whose name does not start with "BlenderLens".
"""

import bmesh
import bpy

from . import meshops
from .common import (
    EDIT_MODE_HINT,
    CommandError,
    Rollback,
    article,
    boolean,
    color,
    get_mesh_object,
    mode_hint,
    mode_name,
    quoted,
    require,
    text,
    update,
    vec,
)

#: The attribute painted when none is named. Col is also the name that Blender's Mesh.vertex_colors.new()
#: gives a new color attribute.
DEFAULT_ATTRIBUTE = "Col"
#: The material given to an object that has none, so that its colours render and export.
VERTEX_COLOR_MATERIAL = "Vertex Color"

#: The start of the name of every node BlenderLens adds to wire images, colours and alpha. The Principled BSDF
#: and Material Output it gives a material that has none keep Blender's own names.
OWN = "BlenderLens "
#: The name of the Color Attribute node that feeds a painted attribute into the base colour.
ATTRIBUTE_NODE = "BlenderLens Color Attribute"
#: The name of the image node that feeds an image into the base colour.
BASE_IMAGE_NODE = "BlenderLens Base Color Image"
#: The name of the node that multiplies the base colour image by the color attribute.
BASE_MIX_NODE = "BlenderLens Base Color Mix"
#: The most nodes visited when searching for the Principled BSDF behind a material's output.
MAX_NODES_SEARCHED = 1000

#: An argument value that means "leave this as the material has it".
KEEP = object()


# ---------------------------------------------------------------------------
# A material's Principled BSDF, and nodes and links that can be taken back
# ---------------------------------------------------------------------------


def own(node):
    """Return whether BlenderLens made ``node``, judged by its name."""
    return node is not None and node.name.startswith(OWN)


def output_node(tree):
    """Return the material output Blender renders: the active one, else the first, or None when there is none."""
    outputs = [n for n in tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial"]
    return next((n for n in outputs if n.is_active_output), outputs[0] if outputs else None)


def _behind_output(tree):
    """Return the Principled BSDF nearest the output's surface, searching every node that feeds it.

    The search goes breadth first through the inputs of each node it reaches, and visits at most
    MAX_NODES_SEARCHED nodes. Return None when the tree has no output or no Principled BSDF is found.
    """
    output = output_node(tree)
    if output is None:
        return None
    seen, queue = set(), [link.from_node for link in output.inputs["Surface"].links]
    while queue and len(seen) < MAX_NODES_SEARCHED:
        node = queue.pop(0)
        if node.as_pointer() in seen:
            continue
        seen.add(node.as_pointer())
        if node.bl_idname == "ShaderNodeBsdfPrincipled":
            return node
        queue += [link.from_node for socket in node.inputs for link in socket.links]
    return None


def principled(material, create=False):
    """Return the Principled BSDF that the material's output renders, or None.

    With ``create``, a material with no node tree, or one whose output's surface nothing feeds, gets a
    new Principled BSDF connected to the output, and a new output too if it has none. A material whose
    surface another shader feeds gets none and the result is None, because that setup is not
    BlenderLens's to take over. Nothing made here is recorded for rollback; principled_for_change
    records it.
    """
    tree = material.node_tree
    if tree is None:
        if not create:
            return None
        if hasattr(material, "use_nodes"):
            material.use_nodes = True
        tree = material.node_tree
    node = _behind_output(tree)
    if node is not None or not create:
        return node
    output = output_node(tree)
    if output is not None and output.inputs["Surface"].is_linked:
        return None
    node = tree.nodes.new("ShaderNodeBsdfPrincipled")
    if output is None:
        output = tree.nodes.new("ShaderNodeOutputMaterial")
        node.location = (output.location[0] - 300.0, output.location[1])
    tree.links.new(node.outputs["BSDF"], output.inputs["Surface"])
    return node


def foreign(socket):
    """Return the node feeding ``socket`` when BlenderLens did not make it, else None."""
    source = feeding(socket)
    return source if source is not None and not own(source) else None


def not_ours(material, sockets=()):
    """Return why BlenderLens cannot change ``material`` without taking over nodes it did not make, or None.

    A material whose output's surface another shader feeds has no Principled BSDF to change. Each input
    of its Principled BSDF named in ``sockets`` must be fed by nothing, or by a node BlenderLens made. A
    material with no node tree, or with nothing feeding its surface, can be changed.
    """
    tree = material.node_tree
    if tree is None:
        return None
    node = principled(material)
    if node is None:
        output = output_node(tree)
        surface = feeding(output.inputs["Surface"]) if output is not None else None
        if surface is None:
            return None
        return (f"'{material.name}' has no Principled BSDF behind its output: its surface is fed by "
                f"'{surface.name}' ({surface.bl_label}), a setup BlenderLens did not make and leaves alone.")
    for name in sockets:
        source = foreign(node.inputs[name])
        if source is not None:
            return (f"The {name} of '{material.name}' is fed by '{source.name}', a node BlenderLens did not make "
                    "and leaves alone.")
    return None


def principled_for_change(material, undo):
    """Return the material's Principled BSDF, making it if missing, with everything made recorded in ``undo``.

    Undoing removes the nodes made and puts back the links into the output's surface that they
    replaced. A material whose surface another shader feeds (see not_ours) is refused with a
    CommandError, and nothing is changed.
    """
    reason = not_ours(material)
    if reason is not None:
        raise CommandError("bad_argument", reason, NOT_OURS_HINT)
    tree = material.node_tree
    before = set(tree.nodes) if tree is not None else set()
    if tree is None and hasattr(material, "use_nodes"):
        undo.set(material, "use_nodes", True)
    output = output_node(material.node_tree) if material.node_tree is not None else None
    surface = [link.from_socket for link in output.inputs["Surface"].links] if output is not None else []
    node = principled(material, create=True)
    tree = material.node_tree
    made = [n for n in tree.nodes if n not in before]

    def remove_made():
        """Remove the nodes made, and link back into the output's surface what fed it before."""
        for made_node in made:
            tree.nodes.remove(made_node)
        for source in surface:
            if output is not None and not any(link.from_socket == source for link in output.inputs["Surface"].links):
                tree.links.new(source, output.inputs["Surface"])

    if made:
        undo.add(remove_made)
    return node


#: The hint given with every refusal to take over a node setup BlenderLens did not make.
NOT_OURS_HINT = ("BlenderLens changes only a Principled BSDF behind the output, and the inputs it wired itself. "
                 "Rewire this material in Blender, or give the faces a new material.")


def own_node(tree, kind, name, undo, beside=None, offset=(-300.0, 0.0)):
    """Return the node of type ``kind`` that BlenderLens keeps under ``name`` in ``tree``, making it if missing.

    A new node is labelled with its name, placed at ``offset`` from ``beside`` when that is given, and
    its removal is recorded in ``undo``.
    """
    node = tree.nodes.get(name)
    if node is not None and node.bl_idname == kind:
        return node
    node = tree.nodes.new(kind)
    node.name = node.label = name
    if beside is not None:
        node.location = (beside.location[0] + offset[0], beside.location[1] + offset[1])
    undo.add(lambda: tree.nodes.remove(node))
    return node


def _remove_link(tree, source, target):
    """Remove the link from ``source`` into ``target``, if there is one."""
    for found in list(target.links):
        if found.from_socket == source:
            tree.links.remove(found)


def unlink(tree, target, undo):
    """Remove every link into the socket ``target``, recording in ``undo`` how to put each one back."""
    for found in list(target.links):
        source = found.from_socket
        tree.links.remove(found)
        undo.add(lambda source=source: tree.links.new(source, target))


def link(tree, source, target, undo):
    """Link ``source`` into ``target`` in place of anything feeding it, recording the change in ``undo``.

    Nothing changes when ``source`` already feeds ``target``.
    """
    if any(found.from_socket == source for found in target.links):
        return
    unlink(tree, target, undo)
    tree.links.new(source, target)
    undo.add(lambda: _remove_link(tree, source, target))


def feeding(socket):
    """Return the node linked into ``socket``, or None."""
    return socket.links[0].from_node if socket.is_linked else None


def socket_by_id(sockets, identifier):
    """Return the socket in ``sockets`` with the given identifier.

    Sockets are found by identifier, not by name, because the Mix node has several sockets named
    "A", one for each data type.
    """
    return next(s for s in sockets if s.identifier == identifier)


# ---------------------------------------------------------------------------
# The base colour: a color attribute, an image, both multiplied, or neither
# ---------------------------------------------------------------------------


def base_color_chain(material):
    """Return what feeds the base colour through BlenderLens's nodes, as (attribute name, image node).

    Either is None when no BlenderLens node of that kind feeds the base colour, directly or through
    BlenderLens's multiply node.
    """
    node = principled(material)
    if node is None:
        return None, None
    source = feeding(node.inputs["Base Color"])
    sources = [source]
    if source is not None and source.name == BASE_MIX_NODE:
        sources = [feeding(socket_by_id(source.inputs, key)) for key in ("A_Color", "B_Color")]
    attribute = next((n.layer_name for n in sources if n is not None and n.name == ATTRIBUTE_NODE), None)
    image = next((n for n in sources if n is not None and n.name == BASE_IMAGE_NODE), None)
    return attribute, image


def wire_base_color(material, node, undo, attribute=KEEP, image=KEEP):
    """Feed the base colour of ``node`` from a color attribute, an image, both, or neither.

    Both together are multiplied, as glTF multiplies baseColorTexture by COLOR_0, so the glTF exporter
    finds each one where it looks for it. ``attribute`` is an attribute name and ``image`` an image
    datablock. None takes that source away, and KEEP leaves it as the material has it. With neither,
    the base colour is unlinked. Every change is recorded in ``undo``.
    """
    tree = material.node_tree
    current_attribute, current_image = base_color_chain(material)
    if attribute is KEEP:
        attribute = current_attribute
    if image is KEEP:
        image = current_image.image if current_image is not None else None
    target = node.inputs["Base Color"]
    sources = []
    if image is not None:
        image_node = own_node(tree, "ShaderNodeTexImage", BASE_IMAGE_NODE, undo, node, (-600.0, 250.0))
        if image_node.image != image:
            undo.set(image_node, "image", image)
        sources.append(image_node.outputs["Color"])
    if attribute is not None:
        attribute_node = own_node(tree, "ShaderNodeVertexColor", ATTRIBUTE_NODE, undo, node, (-600.0, 0.0))
        if attribute_node.layer_name != attribute:
            undo.set(attribute_node, "layer_name", attribute)
        sources.append(attribute_node.outputs["Color"])
    if len(sources) == 2:
        mix = own_node(tree, "ShaderNodeMix", BASE_MIX_NODE, undo, node, (-250.0, 150.0))
        for key, value in (("data_type", "RGBA"), ("blend_type", "MULTIPLY"), ("clamp_result", False)):
            if getattr(mix, key) != value:
                undo.set(mix, key, value)
        factor = mix.inputs["Factor"]
        if factor.default_value != 1.0:
            undo.set(factor, "default_value", 1.0)
        link(tree, sources[0], socket_by_id(mix.inputs, "A_Color"), undo)
        link(tree, sources[1], socket_by_id(mix.inputs, "B_Color"), undo)
        link(tree, socket_by_id(mix.outputs, "Result_Color"), target, undo)
    elif sources:
        link(tree, sources[0], target, undo)
    elif target.is_linked:
        unlink(tree, target, undo)


# ---------------------------------------------------------------------------
# set_vertex_color
# ---------------------------------------------------------------------------


def _users_without(material, attribute, painted):
    """Return the sorted names of mesh objects that use ``material`` but have no color attribute ``attribute``.

    The object ``painted`` is left out.
    """
    out = []
    for obj in bpy.data.objects:
        if obj == painted or obj.type != "MESH":
            continue
        if any(slot.material == material for slot in obj.material_slots) and \
                obj.data.color_attributes.get(attribute) is None:
            out.append(obj.name)
    return sorted(out)


#: Each attribute data type, as Blender names it, in plain words.
DATA_TYPES = {"FLOAT": "float", "INT": "integer", "INT8": "8-bit integer", "BOOLEAN": "boolean",
              "FLOAT_VECTOR": "vector", "FLOAT2": "2D vector", "INT32_2D": "2D integer vector", "STRING": "text",
              "QUATERNION": "quaternion", "FLOAT4X4": "4x4 matrix", "FLOAT_COLOR": "colour",
              "BYTE_COLOR": "byte colour"}
#: Each attribute domain, as Blender names it, in plain words and in the plural.
DOMAINS = {"POINT": "points", "EDGE": "edges", "FACE": "faces", "CORNER": "face corners", "CURVE": "curves",
           "INSTANCE": "instances", "LAYER": "layers"}


def _painting_target(obj, name):
    """Return the color attribute named ``name`` on ``obj``'s mesh to paint, or None when one must be made.

    Raise a CommandError when another kind of attribute or a UV map holds the name, or when the color
    attribute is stored on a domain other than face corners or points, which faces cannot be painted into.
    """
    mesh = obj.data
    found = mesh.color_attributes.get(name)
    leave_out = f"Name another attribute, or leave 'attribute' out to paint the one named {DEFAULT_ATTRIBUTE}."
    if found is None and mesh.attributes.get(name) is not None:
        other = mesh.attributes[name]
        kind = DATA_TYPES.get(other.data_type, other.data_type.replace("_", " ").lower())
        where = DOMAINS.get(other.domain, other.domain.lower() + "s")
        raise CommandError("bad_argument", f"'{name}' on '{obj.name}' is {article(kind)} attribute on its {where}, "
                                           "not a color attribute.", leave_out)
    if mesh.uv_layers.get(name) is not None:
        raise CommandError("bad_argument", f"'{name}' is a UV map of '{obj.name}', not a color attribute.", leave_out)
    if found is not None and found.domain not in ("CORNER", "POINT"):
        where = DOMAINS.get(found.domain, found.domain.lower() + "s")
        raise CommandError("bad_argument", f"Color attribute '{name}' is stored on the {where}, and set_vertex_color "
                                           "paints only attributes stored on face corners or on points.",
                           "Name another attribute.")
    return found


def set_vertex_color(params):
    """Paint a colour into a color attribute on an object's faces, and wire it into its materials unless told not to."""
    obj = get_mesh_object(require(params, "object"))
    rgba = color(require(params, "color"), "color")
    name = text(params, "attribute", DEFAULT_ATTRIBUTE)
    wire = boolean(params, "use_in_material", True)
    mesh = obj.data
    if obj.mode != "OBJECT":
        raise CommandError("bad_argument", f"'{obj.name}' is in {mode_name(obj)} mode, so its colours cannot be "
                                           "painted.", mode_hint(obj))
    if mesh.is_editmode:
        raise CommandError("bad_argument", f"The mesh of '{obj.name}' is being edited in edit mode through another "
                                           "object that shares it, so its colours cannot be painted.", EDIT_MODE_HINT)
    found = _painting_target(obj, name)
    # The facing parameter is judged by matrix_world, which lags behind changes until the view layer is updated.
    update()
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        selecting = params.get("faces") is not None or params.get("facing") is not None
        faces = meshops.select_faces(bm, obj, params) if selecting else list(bm.faces)
        chosen = sorted(f.index for f in faces)
    finally:
        bm.free()
    if not chosen:
        raise CommandError("not_found", f"'{obj.name}' has no faces to paint.",
                           "set_vertex_color paints faces, so give the mesh faces first.")

    created = found is None
    made_material = None
    wired, skipped = [], {}
    with Rollback() as undo:
        attributes = mesh.color_attributes
        if created:
            attributes.new(name, "BYTE_COLOR", "CORNER")
            undo.add(lambda: attributes.remove(attributes[name]))
            # Blender 5.0 fills a new attribute with white, and other versions may not, so it is set to white
            # here. Unpainted faces then leave a base colour image unchanged when the two are multiplied.
            attributes[name].data.foreach_set("color", [1.0] * (4 * len(attributes[name].data)))
        else:
            kept = [0.0] * (4 * len(found.data))
            found.data.foreach_get("color", kept)
            undo.add(lambda: attributes[name].data.foreach_set("color", kept))
        attribute = attributes[name]
        data = attribute.data
        if attribute.domain == "CORNER":
            for index in chosen:
                for corner in mesh.polygons[index].loop_indices:
                    data[corner].color = rgba
        else:
            for vertex in {v for index in chosen for v in mesh.polygons[index].vertices}:
                data[vertex].color = rgba
        # The painted attribute is made the active and the default color attribute, so it is the one the
        # viewport shows and the one an export set to "active" writes. A new attribute that is the mesh's
        # first is both already, and removing it again on rollback clears both.
        if attributes.active_color_name != name:
            previous = attributes.active_color_name
            attributes.active_color = attributes[name]
            if previous:
                undo.add(lambda: setattr(attributes, "active_color", attributes[previous]))
        if attributes.default_color_name != name:
            previous_default = attributes.default_color_name
            attributes.default_color_name = name
            if previous_default:
                undo.add(lambda: setattr(attributes, "default_color_name", previous_default))

        if wire:
            materials = list(dict.fromkeys(s.material for s in obj.material_slots if s.material is not None))
            if not materials:
                material = bpy.data.materials.get(VERTEX_COLOR_MATERIAL)
                if material is None:
                    material = bpy.data.materials.new(VERTEX_COLOR_MATERIAL)
                    undo.add(lambda: bpy.data.materials.remove(material))
                    # Like every material BlenderLens makes, it culls back faces. The glTF exporter marks a
                    # material without culling doubleSided, and a game engine such as Godot then draws both
                    # sides of closed meshes whose insides never show.
                    undo.set(material, "use_backface_culling", True)
                mesh.materials.append(material)
                undo.add(lambda: mesh.materials.pop())
                made_material = material.name
                materials = [material]
            for material in materials:
                reason = not_ours(material, ("Base Color",))
                if reason is not None:
                    skipped[material.name] = reason
                    continue
                wire_base_color(material, principled_for_change(material, undo), undo, attribute=name)
                wired.append(material.name)
    mesh.update()

    attribute = mesh.color_attributes[name]
    first = mesh.polygons[chosen[0]]
    sample = attribute.data[first.loop_indices[0] if attribute.domain == "CORNER" else first.vertices[0]].color
    out = {"object": obj.name, "attribute": name, "created": created, "domain": attribute.domain,
           "data_type": attribute.data_type, "faces_painted": len(chosen), "faces_total": len(mesh.polygons),
           "color": vec(sample), "materials_wired": wired}
    if made_material:
        out["created_material"] = made_material
    notes = []
    if skipped:
        out["not_wired"] = skipped
        one = len(skipped) == 1
        notes.append(f"The colours were not wired into {quoted(skipped)}, because BlenderLens did not make "
                     f"{'its' if one else 'their'} nodes, so the colours do not reach the file through "
                     f"{'it' if one else 'them'}. In Blender, feed a Color Attribute node into "
                     f"{'its base colour' if one else 'the base colour of each'}.")
    if mesh.users > 1:
        out["mesh_users"] = mesh.users
        notes.append(f"The mesh is shared by {mesh.users} objects, and every one of them shows the colours.")
    for material_name in wired:
        lacking = _users_without(bpy.data.materials[material_name], name, obj)
        if lacking:
            meshes = "mesh has" if len(lacking) == 1 else "meshes have"
            notes.append(f"Material '{material_name}' is also on {quoted(lacking[:6])}, whose {meshes} no "
                         f"'{name}' attribute.")
    if notes:
        out["note"] = " ".join(notes)
    return out
