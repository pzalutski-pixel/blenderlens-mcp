"""Read a .glb file back and describe what it holds.

``export_glb`` returns this description with the path, so the caller sees what a game engine
such as Godot or three.js will see: the node tree, each mesh's triangles and bounds, and each
material's PBR values and emission, with warnings about what the engine will make of them. It is
read from the written file, never from Blender's scene, so a material the exporter dropped or a
modifier it did not apply shows up here as missing, and so does an object that was asked for but
is not in the file. ``describe`` reads the same file in the form the asset rules check, for the
``blenderlens-mcp check`` command.

Whatever the file holds, the reader either returns a description or raises ``GLBError`` naming
the file: a missing index, a value of the wrong type, bad UTF-8 or a node cycle is a fault in the
file, never a crash in the server.
"""

from __future__ import annotations

import json
import math
import re
import struct

GLB_MAGIC = b"glTF"
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942


class GLBError(ValueError):
    """An error raised when a file is not a well-formed binary glTF 2.0 (.glb)."""


class _Fault(Exception):
    """An error raised for a structural fault found while reading, which the caller turns into a GLBError."""


def _reject_constant(name: str):
    """Refuse NaN, Infinity and -Infinity, which Python's JSON reader accepts but JSON does not allow."""
    raise _Fault(f"its JSON holds {name}, which is not JSON")


def _finite_float(text: str) -> float:
    """Read a JSON number as a float, refusing one too large to be finite."""
    value = float(text)
    if not math.isfinite(value):
        raise _Fault(f"its JSON holds the number {text}, too large for a float")
    return value


def read_json_chunk(path: str) -> tuple[dict, int]:
    """Return the glTF JSON document and the byte length of the binary chunk."""
    document, binary = read_glb(path)
    return document, len(binary)


def read_glb(path: str) -> tuple[dict, bytes]:
    """Return the glTF JSON document and the binary chunk's bytes, which are empty when there is no such chunk.

    Raise GLBError, naming the file, when it is not a well-formed .glb.
    """
    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) < 20:
        raise GLBError(f"{path} is {len(data)} bytes, too short for a GLB header.")
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != GLB_MAGIC:
        raise GLBError(f"{path} does not start with the glTF magic bytes.")
    if version != 2:
        raise GLBError(f"{path} is glTF binary version {version}; only 2 exists.")
    if length > len(data):
        raise GLBError(f"{path} declares {length} bytes but holds only {len(data)}; it is truncated.")
    if length < len(data):
        raise GLBError(f"{path} declares {length} bytes but holds {len(data)}; {len(data) - length} bytes follow "
                       "its declared end.")
    offset = 12
    document = None
    binary = b""
    while offset + 8 <= len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        body = data[offset + 8: offset + 8 + chunk_length]
        if len(body) != chunk_length:
            raise GLBError(f"{path} has a chunk that runs past the end of the file.")
        if chunk_type == CHUNK_JSON:
            document = _parse_json(path, body)
        elif chunk_type == CHUNK_BIN:
            binary = body
        offset += 8 + chunk_length
    if document is None:
        raise GLBError(f"{path} has no JSON chunk.")
    return document, binary


def _parse_json(path: str, body: bytes) -> dict:
    """Parse the JSON chunk into a dictionary, raising GLBError, naming the file, when it is not a valid JSON object."""
    try:
        document = json.loads(body.decode("utf-8"), parse_constant=_reject_constant, parse_float=_finite_float)
    except UnicodeDecodeError as exc:
        raise GLBError(f"{path}: its JSON chunk is not UTF-8 ({exc.reason} at byte {exc.start}).") from None
    except json.JSONDecodeError as exc:
        raise GLBError(f"{path}: its JSON chunk does not parse ({exc.msg}, line {exc.lineno} column {exc.colno}).") \
            from None
    except ValueError:
        # Python refuses to read an integer of more than 4300 digits, with a ValueError of its own.
        raise GLBError(f"{path}: its JSON chunk holds an integer too long to read (over 4300 digits).") from None
    except RecursionError:
        raise GLBError(f"{path}: its JSON chunk is nested too deeply to read.") from None
    except _Fault as fault:
        raise GLBError(f"{path}: {fault}.") from None
    if not isinstance(document, dict):
        raise GLBError(f"{path}: its JSON chunk is a {type(document).__name__}, not an object.")
    return document


# ---------------------------------------------------------------------------
# Typed access: every lookup checks what it finds, so a wrong file is a _Fault, not a crash
# ---------------------------------------------------------------------------


def _object(value, what: str) -> dict:
    """Return ``value`` when it is a JSON object, or raise a _Fault naming ``what``."""
    if not isinstance(value, dict):
        raise _Fault(f"{what} is a {type(value).__name__}, not an object")
    return value


def _array(owner: dict, key: str, what: str) -> list:
    """Return ``owner[key]`` when it is an array, an empty list when it is absent, or raise a _Fault."""
    value = owner.get(key, [])
    if not isinstance(value, list):
        raise _Fault(f"{what}.{key} is a {type(value).__name__}, not an array")
    return value


def _index(value, items: list, what: str) -> int:
    """Return ``value`` when it is a valid index into ``items``, or raise a _Fault naming ``what``."""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < len(items):
        raise _Fault(f"{what} refers to {value!r}, but there are {len(items)}")
    return value


def _number(value, what: str) -> float:
    """Return ``value`` when it is a number, or raise a _Fault naming ``what``."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _Fault(f"{what} is {value!r}, not a number")
    return value


def _numbers(value, what: str, size: int | None = None) -> list:
    """Return ``value`` when it is a list of numbers, at least ``size`` long when given, or raise a _Fault."""
    if not isinstance(value, list) or (size is not None and len(value) < size):
        raise _Fault(f"{what} is {value!r}, not {size or 'a list of'} numbers")
    return [_number(v, what) for v in value]


def _text(value, what: str):
    """Return ``value`` when it is text or None, or raise a _Fault naming ``what``."""
    if value is not None and not isinstance(value, str):
        raise _Fault(f"{what} is {value!r}, not text")
    return value


def _round(values, places=4):
    """Return each number in ``values`` rounded to ``places`` decimal places."""
    return [round(v, places) for v in values]


def summarize(path: str, expected: list | None = None, target: str = "generic", morphs: dict | None = None) -> dict:
    """Describe what a .glb contains, in the terms a Godot or three.js import uses.

    ``expected`` names the objects that were asked for; any that is not a node in the file's scene is
    listed under ``warnings``, so an object the exporter left out does not go unnoticed. ``target``
    (godot, threejs, or generic, which covers both) chooses which engine's import the other warnings
    speak for. ``morphs`` maps the objects that had shape keys to their node in the file and their
    number of shape keys (``{"node", "count"}``, or a bare count for a node named as the object); a
    warning names each one whose mesh in the file has no morph targets. The summary also carries
    estimates of what the file costs to draw.

    Raise ValueError for an unknown target, and GLBError, naming the file, for a fault in the file.
    """
    if target not in TARGETS:
        raise ValueError(f"target must be one of {', '.join(TARGETS)}, got {target!r}")
    doc, binary = read_glb(path)
    try:
        summary = _summarize(doc, len(binary), expected or [])
        summary["target"] = target
        summary["estimates"] = _estimates(doc, binary)
        summary["warnings"] += _warnings(doc, summary, target, morphs or {})
        return summary
    except _Fault as fault:
        raise GLBError(f"{path}: {fault}.") from None
    except RecursionError:
        raise GLBError(f"{path}: its node tree is nested too deeply to read.") from None


def _summarize(doc: dict, binary_length: int, expected: list) -> dict:
    """Build the summary of a parsed file: its node tree, meshes, materials and counts, and missing objects."""
    accessors = _array(doc, "accessors", "the file")
    nodes = _array(doc, "nodes", "the file")
    meshes = _array(doc, "meshes", "the file")
    materials = _array(doc, "materials", "the file")
    asset = _object(doc.get("asset", {}), "asset")

    def accessor(index, what: str) -> dict:
        """Return the accessor at ``index``, which ``what`` names in a fault."""
        return _object(accessors[_index(index, accessors, what)], f"accessors[{index}]")

    def triangles(primitive: dict, attributes: dict, where: str) -> int:
        """Return the triangles a primitive draws.

        A triangle list makes one per three indices, a strip or a fan one per index after the first two,
        and points and lines make none.
        """
        mode = primitive.get("mode", 4)
        if "indices" in primitive:
            count = accessor(primitive["indices"], f"{where}.indices").get("count")
        elif attributes:
            # Every attribute holds one value per vertex; POSITION is the usual one, but not required.
            name = "POSITION" if "POSITION" in attributes else next(iter(attributes))
            count = accessor(attributes[name], f"{where}.attributes.{name}").get("count")
        else:
            return 0
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise _Fault(f"{where} has an accessor count of {count!r}")
        if mode == 4:
            return count // 3
        if mode in (5, 6):
            return max(count - 2, 0)
        return 0

    mesh_rows = []
    for m, mesh in enumerate(meshes):
        mesh = _object(mesh, f"meshes[{m}]")
        primitives = _array(mesh, "primitives", f"meshes[{m}]")
        lo, hi = [math.inf] * 3, [-math.inf] * 3
        names, attribute_names, total, bounded = [], set(), 0, False
        for p, primitive in enumerate(primitives):
            where = f"meshes[{m}].primitives[{p}]"
            primitive = _object(primitive, where)
            attributes = _object(primitive.get("attributes", {}), f"{where}.attributes")
            # glTF lets a primitive leave POSITION out (an extension may supply it); it adds no bounds.
            if "POSITION" in attributes:
                position = accessor(attributes["POSITION"], f"{where}.attributes.POSITION")
                low = _numbers(position.get("min", [0, 0, 0]), f"{where} POSITION min", 3)
                high = _numbers(position.get("max", [0, 0, 0]), f"{where} POSITION max", 3)
                for i in range(3):
                    lo[i] = min(lo[i], low[i])
                    hi[i] = max(hi[i], high[i])
                bounded = True
            total += triangles(primitive, attributes, where)
            if "material" in primitive:
                index = _index(primitive["material"], materials, f"{where}.material")
                names.append(_text(_object(materials[index], f"materials[{index}]").get("name"), "a material name"))
            else:
                names.append(None)
            attribute_names.update(str(a) for a in attributes)
        mesh_rows.append({
            "name": _text(mesh.get("name"), f"meshes[{m}].name"),
            "primitives": len(primitives),
            "triangles": total,
            "materials": names,
            "attributes": sorted(attribute_names),
            "bounds_yup": {"min": _round(lo), "max": _round(hi)} if bounded else None,
        })

    material_rows = []
    for m, material in enumerate(materials):
        where = f"materials[{m}]"
        material = _object(material, where)
        pbr = _object(material.get("pbrMetallicRoughness", {}), f"{where}.pbrMetallicRoughness")
        emissive = _numbers(material.get("emissiveFactor", [0, 0, 0]), f"{where}.emissiveFactor", 3)
        extensions = _object(material.get("extensions", {}), f"{where}.extensions")
        strength = _number(_object(extensions.get("KHR_materials_emissive_strength", {}),
                                   f"{where} emissive strength").get("emissiveStrength", 1.0),
                           f"{where} emissiveStrength")
        material_rows.append({
            "name": _text(material.get("name"), f"{where}.name"),
            "base_color": _round(_numbers(pbr.get("baseColorFactor", [1, 1, 1, 1]), f"{where} baseColorFactor", 4)),
            # Rounded as the colours are, because the file holds 32-bit floats whose extra digits (0.6000000238)
            # are noise.
            "metallic": round(_number(pbr.get("metallicFactor", 1.0), f"{where} metallicFactor"), 4),
            "roughness": round(_number(pbr.get("roughnessFactor", 1.0), f"{where} roughnessFactor"), 4),
            "emissive_factor": _round(emissive),
            "emissive_strength": round(strength, 4),
            # Called emissive, not "emits light": in Godot and three.js emission makes a surface glow, but it
            # lights nothing around it without global illumination.
            "emissive": any(v > 0 for v in emissive) and strength > 0,
            "double_sided": material.get("doubleSided") is True,
            "alpha_mode": _text(material.get("alphaMode", "OPAQUE"), f"{where}.alphaMode"),
            "textures": sorted(k for k in ("baseColorTexture", "metallicRoughnessTexture") if k in pbr)
            + sorted(k for k in ("normalTexture", "emissiveTexture", "occlusionTexture") if k in material),
        })

    # glTF requires the node hierarchy to be a set of disjoint trees: no node has two parents and
    # none is its own ancestor. A file that breaks this would otherwise recurse without end, or,
    # with shared children, grow its summary exponentially.
    seen = set()
    # Triangles drawn: each mesh counts once per node of the scene that uses it, since linked duplicates share
    # one mesh.
    drawn = [0]

    def node_row(index, what: str) -> dict:
        """Return the summary row of one node and, within it, its children's, refusing a node met twice."""
        index = _index(index, nodes, what)
        if index in seen:
            raise _Fault(f"node {index} appears twice in the scene's tree (a cycle, or a child with two parents)")
        seen.add(index)
        node = _object(nodes[index], f"nodes[{index}]")
        row = {"name": _text(node.get("name"), f"nodes[{index}].name")}
        if "mesh" in node:
            mesh = _index(node["mesh"], meshes, f"nodes[{index}].mesh")
            row["mesh"] = mesh_rows[mesh]["name"]
            drawn[0] += mesh_rows[mesh]["triangles"]
        for key in ("translation", "rotation", "scale"):
            if key in node:
                row[key] = _round(_numbers(node[key], f"nodes[{index}].{key}"))
        if node.get("extras"):
            row["extras"] = node["extras"]
        children = _array(node, "children", f"nodes[{index}]")
        if children:
            row["children"] = [node_row(c, f"nodes[{index}].children") for c in children]
        return row

    scenes = _array(doc, "scenes", "the file")
    roots = []
    if scenes:
        scene_index = _index(doc.get("scene", 0), scenes, "scene")
        roots = _array(_object(scenes[scene_index], f"scenes[{scene_index}]"), "nodes", f"scenes[{scene_index}]")
    tree = [node_row(i, f"scenes[{doc.get('scene', 0)}].nodes") for i in roots]

    return {
        "valid": True,
        "generator": _text(asset.get("generator"), "asset.generator"),
        "gltf_version": _text(asset.get("version"), "asset.version"),
        "up_axis": "Y",
        "nodes": tree,
        "node_count": len(nodes),
        "meshes": mesh_rows,
        "materials": material_rows,
        "triangles": drawn[0],
        "extensions_used": _array(doc, "extensionsUsed", "the file"),
        "extensions_required": _array(doc, "extensionsRequired", "the file"),
        "animations": len(_array(doc, "animations", "the file")),
        "images": len(_array(doc, "images", "the file")),
        "vertex_colors": any("COLOR_0" in m["attributes"] for m in mesh_rows),
        "binary_bytes": binary_length,
        "warnings": _missing(tree, expected),
    }


def _missing(tree: list, expected: list) -> list:
    """Return a warning naming the expected objects that are not nodes in the file's scene, or an empty list."""
    present = set()
    stack = list(tree)
    while stack:
        row = stack.pop()
        present.add(row["name"])
        stack.extend(row.get("children", []))
    missing = [name for name in expected if name not in present]
    if not missing:
        return []
    return [{
        "code": "missing_objects",
        "severity": "warning",
        "objects": missing,
        "message": f"These objects were asked for but are not in the file: {', '.join(missing)}. The glTF "
                   "exporter left them out; it skips an object in a collection named glTF_not_exported, or of a "
                   "type glTF cannot hold.",
    }]


# ---------------------------------------------------------------------------
# What Godot and three.js do with a file
# ---------------------------------------------------------------------------

TARGETS = ("godot", "threejs", "generic")
#: The most names, materials or extensions a warning lists; its count says how many there are.
LISTED = 32

#: The extensions Godot's glTF importer loads: GLTFDocument's own list and its registered document
#: extensions (modules/gltf, 4.7.2-stable). A file that requires any other is refused on import.
GODOT_EXTENSIONS = frozenset({
    "GODOT_single_root", "KHR_animation_pointer", "KHR_lights_punctual", "KHR_materials_emissive_strength",
    "KHR_materials_pbrSpecularGlossiness", "KHR_materials_unlit", "KHR_node_visibility", "KHR_texture_transform",
    "KHR_texture_basisu", "EXT_texture_webp", "OMI_collider", "OMI_physics_body", "OMI_physics_shape",
})
#: The first Godot release that loads each extension, for those added after 4.2, as Godot's modules/gltf
#: sources for 4.3, 4.4 and 4.5 show.
GODOT_SINCE = {"GODOT_single_root": "4.4", "KHR_animation_pointer": "4.4", "KHR_node_visibility": "4.5"}

#: The extensions three.js's GLTFLoader loads: its EXTENSIONS table and plugins in release r186, and
#: KHR_materials_diffuse_roughness, which three.js's GLTFLoader supports after r186.
THREEJS_EXTENSIONS = frozenset({
    "KHR_binary_glTF", "KHR_draco_mesh_compression", "KHR_lights_punctual", "KHR_materials_clearcoat",
    "KHR_materials_diffuse_roughness", "KHR_materials_dispersion", "KHR_materials_ior", "KHR_materials_sheen",
    "KHR_materials_specular", "KHR_materials_transmission", "KHR_materials_iridescence",
    "KHR_materials_anisotropy", "KHR_materials_unlit", "KHR_materials_volume", "KHR_texture_basisu",
    "KHR_texture_transform", "KHR_mesh_quantization", "KHR_materials_emissive_strength", "EXT_materials_bump",
    "EXT_texture_webp", "EXT_texture_avif", "EXT_meshopt_compression", "KHR_meshopt_compression",
    "EXT_mesh_gpu_instancing",
})
#: Extensions three.js loads only with a decoder that the app sets up on the loader; without one, GLTFLoader
#: throws.
THREEJS_DECODERS = {
    "KHR_draco_mesh_compression": "a DRACOLoader (loader.setDRACOLoader)",
    "KHR_texture_basisu": "a KTX2Loader (loader.setKTX2Loader)",
    "EXT_meshopt_compression": "a MeshoptDecoder (loader.setMeshoptDecoder)",
    "KHR_meshopt_compression": "a MeshoptDecoder (loader.setMeshoptDecoder)",
}

#: Godot's node-type name hints, in the order its importer tests them (ResourceImporterScene::_post_fix_node,
#: 4.7). Each is (hint, whether it applies to mesh nodes only, what Godot makes of the node).
GODOT_NODE_HINTS = (
    ("colonly", True, "becomes a StaticBody3D with collision shapes, and the mesh is not drawn"),
    ("convcolonly", True, "becomes a StaticBody3D with a convex collision shape, and the mesh is not drawn"),
    ("rigid", True, "becomes a RigidBody3D holding the mesh"),
    ("col", True, "gets a StaticBody3D child with collision shapes"),
    ("convcol", True, "gets a StaticBody3D child with a convex collision shape"),
    ("navmesh", True, "becomes a NavigationRegion3D, and the mesh is not drawn"),
    ("occ", True, "is baked into an occluder as well as drawn"),
    ("occonly", True, "becomes an occluder only, and the mesh is not drawn"),
    ("vehicle", False, "becomes a VehicleBody3D"),
    ("wheel", False, "becomes a VehicleWheel3D"),
)
#: Godot's material name hints: (hint, what Godot does with the material).
GODOT_MATERIAL_HINTS = (("alpha", "turns on alpha transparency"), ("vcol", "takes its albedo from vertex colours"))
#: Godot's mesh name hints, tested on a mesh's own name when its node's name carries no hint.
GODOT_MESH_HINTS = (("col", "gets collision shapes"), ("convcol", "gets a convex collision shape"),
                    ("occ", "is baked into an occluder"))
# Godot loops an imported animation by its name, in two places: its glTF importer and its scene importer.
# The add-on applies the same rule when it keys a clip, in motion.py, which keeps its own copy because it runs
# inside Blender and cannot import this module; change the two together.
#: The words that make Godot's glTF importer (GLTFDocument::_parse_animations) loop an animation whose name, in
#: lower case, begins or ends with one of them.
GODOT_LOOP_WORDS = ("loop", "cycle")
#: The import hints that make Godot's scene importer (ResourceImporterScene, _teststr) loop an animation, and
#: drop the hint from its name, when one follows a - or _ at the end of the name, or a $ anywhere, once
#: trailing digits, spaces and underscores are stripped.
GODOT_LOOP_HINTS = ("loop_mode", "loop", "cycle")
#: The hints that make collision shapes, which take no non-uniform or negative scale.
GODOT_COLLISION_HINTS = {"colonly", "convcolonly", "rigid", "col", "convcol"}

#: The characters Godot replaces with _ in a node name (String::validate_node_name).
_GODOT_INVALID = '.:@/"%'
#: The characters three.js removes from a node name (PropertyBinding.sanitizeNodeName, which also turns
#: whitespace into _).
_THREEJS_RESERVED = re.compile(r"[\[\]\.:/]")


def godot_hint(name: str, hint: str) -> bool:
    """Return whether Godot's importer reads ``hint`` in ``name``, as _teststr in resource_importer_scene.cpp does.

    Trailing digits, whitespace and underscores are stripped first (Blender's .001 arrives as _001). Then the
    hint matches, in any case, when it ends the name after a - or _, or follows a $ anywhere in the name.
    """
    what = name
    while what and (what[-1] in "0123456789_" or ord(what[-1]) <= 32):
        what = what[:-1]
    lower = what.lower()
    return f"${hint}" in lower or lower.endswith(f"-{hint}") or lower.endswith(f"_{hint}")


def godot_node_name(name: str) -> str:
    """Return ``name`` with each character Godot does not allow in a node name replaced with _."""
    return "".join("_" if c in _GODOT_INVALID else c for c in name)


def godot_loops(name: str) -> bool:
    """Return whether Godot imports an animation of this name as looping.

    Its glTF importer loops a name that begins or ends with a word in GODOT_LOOP_WORDS, in any case, and its
    scene importer loops a name that carries a hint in GODOT_LOOP_HINTS.
    """
    lower = name.lower()
    if any(lower.startswith(word) or lower.endswith(word) for word in GODOT_LOOP_WORDS):
        return True
    return any(godot_hint(godot_node_name(name), hint) for hint in GODOT_LOOP_HINTS)


def threejs_node_name(name: str) -> str:
    """Return ``name`` as three.js sanitises a node name: whitespace becomes _, and reserved characters go."""
    return _THREEJS_RESERVED.sub("", re.sub(r"\s", "_", name))


def _warning(code: str, severity: str, message: str, engine: str | None = None, **extra) -> dict:
    """Build one read-back warning: its code, severity, engine (when it concerns one), message and extra fields."""
    out = {"code": code, "severity": severity}
    if engine is not None:
        out["engine"] = engine
    out["message"] = message
    out.update(extra)
    return out


def _engines(target: str) -> tuple:
    """Return the engines a target warns for; generic warns for both."""
    return ("godot", "threejs") if target == "generic" else (target,)


def _godot_names(nodes: list) -> list:
    """Return the name Godot gives each node, by index.

    Invalid characters are replaced with _, and the name is then made unique across the whole file with a
    number (GLTFDocument::_assign_node_names, naming version 2, Godot's default from 4.5).
    """
    used, out = set(), []
    for node in nodes:
        name = node.get("name") if isinstance(node, dict) else None
        if not name:
            name = "Mesh" if isinstance(node, dict) and "mesh" in node else \
                "Camera" if isinstance(node, dict) and "camera" in node else "Node"
        base = godot_node_name(str(name))
        unique, count = base, 1
        while unique in used:
            count += 1
            unique = f"{base}{count}"
        used.add(unique)
        out.append(unique)
    return out


def _threejs_names(doc: dict, nodes: list) -> dict:
    """Return ``{node index: name}`` as three.js's GLTFLoader names the nodes.

    Each scene's name comes first, then its nodes depth first, each sanitised and made unique with _1, _2
    (GLTFParser.createUniqueName).
    """
    used: dict = {}
    out: dict = {}

    def unique(name: str) -> str:
        """Return ``name`` sanitised, with a number appended when an earlier name took it."""
        clean = threejs_node_name(name)
        if clean in used:
            used[clean] += 1
            return f"{clean}_{used[clean]}"
        used[clean] = 0
        return clean

    for scene in _array(doc, "scenes", "the file"):
        if not isinstance(scene, dict):
            continue
        if scene.get("name"):
            unique(str(scene["name"]))
        stack = list(reversed(_array(scene, "nodes", "a scene")))
        while stack:
            index = stack.pop()
            if not isinstance(index, int) or not 0 <= index < len(nodes) or index in out:
                continue
            node = nodes[index] if isinstance(nodes[index], dict) else {}
            out[index] = unique(str(node["name"])) if node.get("name") else ""
            children = node.get("children", [])
            stack.extend(reversed(children if isinstance(children, list) else []))
    return out


def _listed(items: list) -> list:
    """Return the first LISTED items, the most a warning lists."""
    return items[:LISTED]


def _extension_warnings(doc: dict, target: str) -> list:
    """Return warnings about the extensions an engine cannot load, ignores, or needs a decoder for.

    For the godot target only, a note also names the extensions and features that only newer Godot versions
    load.
    """
    used = [e for e in _array(doc, "extensionsUsed", "the file") if isinstance(e, str)]
    required = [e for e in _array(doc, "extensionsRequired", "the file") if isinstance(e, str)]
    out = []
    for engine in _engines(target):
        supported = GODOT_EXTENSIONS if engine == "godot" else THREEJS_EXTENSIONS
        label = "Godot" if engine == "godot" else "three.js"
        refused = [e for e in required if e not in supported]
        if refused:
            effect = "Godot refuses to import the file" if engine == "godot" else \
                "three.js warns and loads the file without what they hold"
            out.append(_warning("extension_not_supported", "error",
                                f"The file requires {', '.join(refused)}, which {label} cannot load: {effect}.",
                                engine, extensions=refused))
        ignored = [e for e in used if e not in supported and e not in required]
        if ignored:
            out.append(_warning("extension_ignored", "warning",
                                f"{label} ignores {', '.join(ignored)}: what they add is lost on import.",
                                engine, extensions=ignored))
        if engine == "threejs":
            decoders = [e for e in used if e in THREEJS_DECODERS]
            if decoders:
                needs = "; ".join(f"{e} needs {THREEJS_DECODERS[e]}" for e in decoders)
                out.append(_warning("extension_needs_decoder", "info",
                                    f"three.js loads this file only with a decoder set up: {needs}.",
                                    engine, extensions=decoders))
        if engine == "godot" and target == "godot":
            # Version notes matter only to a Godot project, so the generic target leaves them out.
            notes = []
            later = [e for e in used if e in GODOT_SINCE]
            for e in later:
                notes.append(f"{e} loads from Godot {GODOT_SINCE[e]}.")
            extras = any(isinstance(item, dict) and item.get("extras")
                         for key in ("nodes", "meshes", "materials")
                         for item in _array(doc, key, "the file"))
            if extras:
                notes.append("Extras become metadata (get_meta(\"extras\")) from Godot 4.4.")
            if notes:
                severity = "warning" if any(e in required for e in later) else "info"
                out.append(_warning("godot_version", severity, " ".join(notes) + " Older versions refuse "
                                    "what the file requires and drop the rest.", engine))
    return out


def _hint_warnings(doc: dict, godot_names: list) -> list:
    """Return Godot's warnings about names: import hints, collision hints on scaled nodes, and looping animations."""
    nodes = _array(doc, "nodes", "the file")
    meshes = _array(doc, "meshes", "the file")
    materials = _array(doc, "materials", "the file")
    hits, scaled = [], []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        name = godot_names[index]
        has_mesh = "mesh" in node
        if godot_hint(name, "noimp"):
            hits.append({"on": "node", "name": node.get("name"), "hint": "noimp",
                         "effect": "is left out of the import, with its children"})
            continue
        found = _godot_node_hint(name, has_mesh)
        if found:
            hits.append({"on": "node", "name": node.get("name"), "hint": found[0], "effect": found[1]})
            if found[0] in GODOT_COLLISION_HINTS and _odd_scale(node, index):
                scaled.append(node.get("name"))
        elif has_mesh and isinstance(node["mesh"], int) and 0 <= node["mesh"] < len(meshes):
            mesh = meshes[node["mesh"]]
            mesh_name = mesh.get("name") if isinstance(mesh, dict) else None
            if isinstance(mesh_name, str):
                for hint, effect in GODOT_MESH_HINTS:
                    if godot_hint(mesh_name, hint):
                        hits.append({"on": "mesh", "name": mesh_name, "hint": hint, "effect": effect})
                        break
    for material in materials:
        name = material.get("name") if isinstance(material, dict) else None
        if isinstance(name, str):
            for hint, effect in GODOT_MATERIAL_HINTS:
                if godot_hint(name, hint):
                    hits.append({"on": "material", "name": name, "hint": hint, "effect": effect})
    out = []
    if hits:
        listed = "; ".join(f"{h['on']} {h['name']!r} ({h['hint']}) {h['effect']}" for h in hits[:8])
        out.append(_warning("godot_name_hint", "warning",
                            f"Godot reads import hints in these names: {listed}. Rename them if that is not intended; "
                            "the import option nodes/use_node_type_suffixes turns node hints off.",
                            "godot", count=len(hits), names=_listed(hits)))
    if scaled:
        shown = ", ".join(str(n) for n in scaled[:LISTED])
        out.append(_warning("scaled_hint_node", "warning",
                            f"These nodes carry collision hints and have non-uniform or negative scale: {shown}. "
                            "Godot's collision shapes take neither; apply_transform bakes the scale into the mesh.",
                            "godot", nodes=_listed(scaled)))
    loops = []
    for animation in _array(doc, "animations", "the file"):
        name = animation.get("name") if isinstance(animation, dict) else None
        if isinstance(name, str) and godot_loops(name):
            loops.append(name)
    if loops:
        out.append(_warning("animation_loops_by_name", "info",
                            f"Godot imports {', '.join(loops[:LISTED])} as looping, because of the names: Godot "
                            "loops an animation whose name begins or ends with loop or cycle, in any case, or "
                            "carries a loop hint such as _loop or -cycle at its end (before any number) or $loop "
                            "anywhere, and it drops such a hint from the name. To have a clip play once, rename it, "
                            "or set its loop mode in Godot's import settings.",
                            "godot", animations=_listed(loops)))
    return out


_HINT_EFFECTS = {hint: effect for hint, _, effect in GODOT_NODE_HINTS}


def _godot_node_hint(name: str, has_mesh: bool):
    """Return (hint, what Godot makes of the node) for the first node hint Godot's importer acts on, or None.

    The tests run in Godot's order, and some end the chain whether or not they act: a node named for
    collision only, or as an occluder, is left alone when it holds no mesh.
    """
    def hit(hint):
        """Return ``hint`` with what Godot makes of a node that carries it."""
        return hint, _HINT_EFFECTS[hint]

    if godot_hint(name, "colonly") or godot_hint(name, "convcolonly"):
        return hit("colonly" if godot_hint(name, "colonly") else "convcolonly") if has_mesh else None
    if has_mesh and godot_hint(name, "rigid"):
        return hit("rigid")
    if has_mesh and (godot_hint(name, "col") or godot_hint(name, "convcol")):
        return hit("col" if godot_hint(name, "col") else "convcol")
    if has_mesh and godot_hint(name, "navmesh"):
        return hit("navmesh")
    if godot_hint(name, "occ") or godot_hint(name, "occonly"):
        return (hit("occ" if godot_hint(name, "occ") else "occonly")) if has_mesh else None
    if godot_hint(name, "vehicle"):
        return hit("vehicle")
    if godot_hint(name, "wheel"):
        return hit("wheel")
    return None


def _odd_scale(node: dict, index: int) -> bool:
    """Return whether a node's own transform scales unevenly or mirrors."""
    if "matrix" in node:
        m = _numbers(node["matrix"], f"nodes[{index}].matrix", 16)
        columns = [m[0:3], m[4:7], m[8:11]]
        lengths = [math.sqrt(sum(c * c for c in column)) for column in columns]
        a, b, c = columns
        det = (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
               + a[2] * (b[0] * c[1] - b[1] * c[0]))
        return det < 0 or max(lengths) - min(lengths) > 1e-4 * max(max(lengths), 1e-9)
    scale = _numbers(node.get("scale", [1, 1, 1]), f"nodes[{index}].scale", 3)
    return any(s < 0 for s in scale) or max(scale) - min(scale) > 1e-4 * max(abs(s) for s in scale)


def _name_warnings(doc: dict, godot_names: list, target: str) -> list:
    """Return a warning listing the nodes each engine renames on import, and the names that collide.

    A path to a node by its name in the file fails in the engine once the node has been renamed.
    """
    nodes = _array(doc, "nodes", "the file")
    out = {"godot": [], "threejs": []}
    collisions = {"godot": [], "threejs": []}
    engines = _engines(target)
    if "godot" in engines:
        seen: dict = {}
        for index, node in enumerate(nodes):
            name = node.get("name") if isinstance(node, dict) else None
            if not isinstance(name, str) or not name:
                continue
            base = godot_node_name(name)
            seen[base] = seen.get(base, 0) + 1
            if godot_names[index] != name:
                out["godot"].append({"index": index, "name": name, "as": godot_names[index]})
        collisions["godot"] = sorted(base for base, n in seen.items() if n > 1)
    if "threejs" in engines:
        names = _threejs_names(doc, nodes)
        seen = {}
        for index in sorted(names):
            node = nodes[index]
            name = node.get("name") if isinstance(node, dict) else None
            if not isinstance(name, str) or not name:
                continue
            base = threejs_node_name(name)
            seen[base] = seen.get(base, 0) + 1
            if names[index] != name:
                out["threejs"].append({"index": index, "name": name, "as": names[index]})
        scenes = _array(doc, "scenes", "the file")
        for scene in scenes:
            if isinstance(scene, dict) and isinstance(scene.get("name"), str):
                base = threejs_node_name(scene["name"])
                if base in seen:
                    seen[base] += 1
        collisions["threejs"] = sorted(base for base, n in seen.items() if n > 1)
    renamed = sum(len(rows) for rows in out.values())
    if not renamed and not any(collisions.values()):
        return []
    extra = {engine: _listed(out[engine]) for engine in engines}
    extra["collisions"] = {engine: _listed(collisions[engine]) for engine in engines}
    words = []
    for engine in engines:
        if out[engine]:
            label = "Godot" if engine == "godot" else "three.js"
            shown = ", ".join(f"{row['name']} as {row['as']}" for row in out[engine][:6])
            words.append(f"{label} renames {len(out[engine])} nodes on import ({shown})")
    return [_warning("renamed_on_import", "info",
                     "; ".join(words) + ". A path to a node by its name in the file then fails; clean_names takes "
                     "Blender's .001 off where it can.", target if target != "generic" else None,
                     count=renamed, **extra)]


def _material_warnings(doc: dict, summary: dict, target: str, morphs: dict) -> list:
    """Return warnings about materials and meshes.

    They cover double-sided materials, vertex colours, normal maps without tangents, textures without the
    UV set they use, and shape keys that the export dropped.
    """
    meshes = _array(doc, "meshes", "the file")
    materials = _array(doc, "materials", "the file")
    engines = _engines(target)
    out = []
    double = [row["name"] for row in summary["materials"] if row["double_sided"]]
    if double:
        out.append(_warning("double_sided", "info",
                            f"{len(double)} materials are double-sided: Godot draws them with culling off, three.js "
                            "with DoubleSide, each paying for both faces. Blender's exporter marks a material "
                            "doubleSided when its Backface Culling is off; set_material with backface_culling true "
                            "turns it on, as it does for every material set_material creates.",
                            materials=_listed([str(n) for n in double])))
    if summary["vertex_colors"]:
        who = {"godot": "Godot multiplies", "threejs": "three.js multiplies",
               "generic": "Godot and three.js multiply"}[target]
        words = f"The file has vertex colours (COLOR_0), and {who} the base colour by them."
        if "godot" in engines:
            words += (" Godot 4.7.0, 4.7.1 and 4.7.2 skip that on a mesh's first primitive, because they read its "
                      "material before its colours. In those versions, a material whose name ends in -vcol shows "
                      "them.")
        out.append(_warning("vertex_colors", "info", words))
    normal_no_tangent, untextured = [], []
    for mesh in meshes:
        if not isinstance(mesh, dict):
            continue
        for primitive in _array(mesh, "primitives", "a mesh"):
            if not isinstance(primitive, dict) or not isinstance(primitive.get("material"), int):
                continue
            index = primitive["material"]
            if not 0 <= index < len(materials) or not isinstance(materials[index], dict):
                continue
            material = materials[index]
            attributes = primitive.get("attributes") if isinstance(primitive.get("attributes"), dict) else {}
            pbr = material.get("pbrMetallicRoughness") if isinstance(material.get("pbrMetallicRoughness"),
                                                                     dict) else {}
            textures = [pbr.get(k) for k in ("baseColorTexture", "metallicRoughnessTexture")] + \
                [material.get(k) for k in ("normalTexture", "emissiveTexture", "occlusionTexture")]
            sets = {t.get("texCoord", 0) for t in textures
                    if isinstance(t, dict) and isinstance(t.get("texCoord", 0), int)}
            if any(f"TEXCOORD_{s}" not in attributes for s in sets):
                untextured.append(mesh.get("name"))
            if isinstance(material.get("normalTexture"), dict) and "TANGENT" not in attributes:
                normal_no_tangent.append(material.get("name"))
    if normal_no_tangent:
        names = list(dict.fromkeys(normal_no_tangent))
        out.append(_warning("normal_map_without_tangents", "warning",
                            f"These materials have normal maps, but the file has no tangents for them: "
                            f"{', '.join(str(n) for n in names[:LISTED])}. Godot makes its own on import, and "
                            "three.js derives them per pixel; either can shade differently from Blender's bake. "
                            "Export with tangents true.",
                            materials=_listed(names)))
    if untextured:
        names = list(dict.fromkeys(untextured))
        out.append(_warning("texture_without_uv", "warning",
                            f"These meshes are textured but lack the UV set their textures use: "
                            f"{', '.join(str(n) for n in names[:LISTED])}. The textures have nowhere to go.",
                            meshes=_listed(names)))
    if morphs:
        dropped = _dropped_morphs(doc, morphs)
        if dropped:
            out.append(_warning("morphs_dropped", "warning",
                                f"These objects had shape keys and their meshes in the file have no morph targets: "
                                f"{', '.join(dropped[:LISTED])}. Exporting with modifiers applied drops them; remove "
                                "the modifiers, or export with apply_modifiers false.",
                                objects=_listed(dropped)))
    return out


def _dropped_morphs(doc: dict, morphs: dict) -> list:
    """Return the names in the file of the nodes whose objects had shape keys but whose meshes have no morph targets.

    ``morphs`` maps each object to ``{"node": index in the file, "count": shape keys}``, so a node is found by
    its index, whatever name cleaning gave it. With a bare count instead, the node is found by the object's
    name.
    """
    nodes = _array(doc, "nodes", "the file")
    meshes = _array(doc, "meshes", "the file")
    out = []
    for name, value in morphs.items():
        if isinstance(value, dict):
            count, index = value.get("count"), value.get("node")
            found = [nodes[index]] if isinstance(index, int) and not isinstance(index, bool) \
                and 0 <= index < len(nodes) else []
        else:
            count = value
            found = [node for node in nodes if isinstance(node, dict) and node.get("name") == name][:1]
        if not count or not found or not isinstance(found[0], dict) or not isinstance(found[0].get("mesh"), int):
            continue
        node = found[0]
        if not 0 <= node["mesh"] < len(meshes) or not isinstance(meshes[node["mesh"]], dict):
            continue
        primitives = _array(meshes[node["mesh"]], "primitives", "a mesh")
        if not any(isinstance(p, dict) and p.get("targets") for p in primitives):
            out.append(str(node.get("name", name)))
    return out


#: How far the scene's only root may stand from the origin, in metres, and still count as at the origin.
ORIGIN_SLACK = 1e-4


def _root_warnings(doc: dict) -> list:
    """Return the root_off_origin warning when the scene's one root stands away from the origin.

    A game places the asset by its root, so the asset then lands that far from wherever it is placed. A
    scene with several roots is a layout, where the roots' positions are the point, so it gets no warning.
    """
    scenes = _array(doc, "scenes", "the file")
    if not scenes:
        return []
    roots = _array(_object(scenes[_index(doc.get("scene", 0), scenes, "scene")], "the scene"), "nodes", "the scene")
    if len(roots) != 1:
        return []
    nodes = _array(doc, "nodes", "the file")
    root = _object(nodes[_index(roots[0], nodes, "the scene's root")], "the scene's root")
    if "matrix" in root:
        matrix = _numbers(root["matrix"], "the root's matrix", 16)
        place = matrix[12:15]
    else:
        place = _numbers(root.get("translation", [0, 0, 0]), "the root's translation", 3)
    if all(abs(c) <= ORIGIN_SLACK for c in place):
        return []
    name = root.get("name") if isinstance(root.get("name"), str) else f"node {roots[0]}"
    return [_warning("root_off_origin", "warning",
                     f"The file's one root, {name}, stands at {_round(place)} (glTF's Y-up), not at the origin: a "
                     "game places the asset by its root, so it lands that far off. export_glb with at_origin true "
                     "writes it at the origin and leaves the scene as it is.", node=name, translation=_round(place))]


def _warnings(doc: dict, summary: dict, target: str, morphs: dict) -> list:
    """Return the read-back warnings for ``target``, which follow the missing-object warning in the summary."""
    nodes = _array(doc, "nodes", "the file")
    godot_names = _godot_names(nodes)
    out = _root_warnings(doc) + _extension_warnings(doc, target)
    if target in ("godot", "generic"):
        out += _hint_warnings(doc, godot_names)
    out += _name_warnings(doc, godot_names, target)
    out += _material_warnings(doc, summary, target, morphs)
    return out


# ---------------------------------------------------------------------------
# Estimates: draw calls, textures, memory
# ---------------------------------------------------------------------------


def image_size(data: bytes):
    """Return (width, height) from a PNG, JPEG, WebP or KTX2 header, or None when the bytes are none of these."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if data[:2] == b"\xff\xd8":
            offset = 2
            for _ in range(10_000):
                if offset + 4 > len(data) or data[offset] != 0xFF:
                    return None
                marker = data[offset + 1]
                if marker == 0xFF:
                    offset += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    offset += 2
                    continue
                length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
                    return width, height
                offset += 2 + length
            return None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8 ":
                return (struct.unpack("<H", data[26:28])[0] & 0x3FFF, struct.unpack("<H", data[28:30])[0] & 0x3FFF)
            if kind == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if kind == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            return None
        if data[:12] == b"\xabKTX 20\xbb\r\n\x1a\n":
            return struct.unpack("<II", data[20:28])
    except struct.error:
        return None
    return None


def _estimates(doc: dict, binary: bytes) -> dict:
    """Estimate what the file costs to draw.

    That is one draw call per primitive of every mesh node in the scene, and each texture's size, read from
    its own header, with the video memory the textures take when mipmapped: four bytes a pixel as three.js
    uploads them (RGBA8), and about one byte a pixel as Godot's VRAM-compressed import keeps them.
    """
    nodes = _array(doc, "nodes", "the file")
    meshes = _array(doc, "meshes", "the file")
    views = _array(doc, "bufferViews", "the file")
    buffers = _array(doc, "buffers", "the file")
    draws = 0
    scenes = _array(doc, "scenes", "the file")
    if scenes:
        scene = scenes[_index(doc.get("scene", 0), scenes, "scene")]
        stack = list(_array(_object(scene, "the scene"), "nodes", "the scene"))
        seen = set()
        while stack:
            index = _index(stack.pop(), nodes, "a scene node")
            if index in seen:
                continue
            seen.add(index)
            node = _object(nodes[index], f"nodes[{index}]")
            if "mesh" in node:
                mesh = _object(meshes[_index(node["mesh"], meshes, f"nodes[{index}].mesh")], "a mesh")
                draws += len(_array(mesh, "primitives", "a mesh"))
            stack.extend(_array(node, "children", f"nodes[{index}]"))
    textures = []
    for i, image in enumerate(_array(doc, "images", "the file")):
        image = _object(image, f"images[{i}]")
        size = None
        if "bufferView" in image:
            view = _object(views[_index(image["bufferView"], views, f"images[{i}].bufferView")], "a bufferView")
            buffer = view.get("buffer", 0)
            if isinstance(buffer, int) and 0 <= buffer < len(buffers) and isinstance(buffers[buffer], dict) \
                    and "uri" not in buffers[buffer]:
                start = view.get("byteOffset", 0)
                length = view.get("byteLength", 0)
                if isinstance(start, int) and isinstance(length, int) and 0 <= start <= start + length <= len(binary):
                    size = image_size(binary[start:start + length])
        textures.append({"name": _text(image.get("name"), f"images[{i}].name"),
                         "mime": _text(image.get("mimeType"), f"images[{i}].mimeType"),
                         "width": size[0] if size else None, "height": size[1] if size else None})
    pixels = sum(t["width"] * t["height"] for t in textures if t["width"] is not None)
    return {"draw_calls": draws, "textures": _listed(textures), "texture_pixels": pixels,
            "vram_bytes": {"uncompressed": pixels * 4 * 4 // 3, "compressed": pixels * 4 // 3}}


# ---------------------------------------------------------------------------
# The file described for a project's rules
# ---------------------------------------------------------------------------

#: The most vertices of one mesh read from the file to bound it exactly; past this the accessor's own
#: minimum and maximum bound it.
MAX_POSITIONS = 2_000_000


def describe(path: str) -> dict:
    """Describe the file in the form the asset rules in addon/rules.py read, for the check command.

    Each node of the scene comes with its parent, extras, triangles, world bounds and origin, in Blender's
    axes (glTF's +Y up is Blender's +Z, and glTF's +Z is Blender's -Y). Each material comes with its linear
    values, where a value that a texture drives is None.
    """
    doc, binary = read_glb(path)
    try:
        return _describe(doc, binary)
    except _Fault as fault:
        raise GLBError(f"{path}: {fault}.") from None
    except RecursionError:
        raise GLBError(f"{path}: its node tree is nested too deeply to read.") from None


def _matrix(node: dict, index: int) -> list:
    """Return a node's local transform as a 4x4 row-major matrix."""
    if "matrix" in node:
        m = _numbers(node["matrix"], f"nodes[{index}].matrix", 16)
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]  # glTF stores the matrix column by column.
    t = _numbers(node.get("translation", [0, 0, 0]), f"nodes[{index}].translation", 3)
    x, y, z, w = _numbers(node.get("rotation", [0, 0, 0, 1]), f"nodes[{index}].rotation", 4)[:4]
    s = _numbers(node.get("scale", [1, 1, 1]), f"nodes[{index}].scale", 3)
    rotation = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    return [[rotation[r][0] * s[0], rotation[r][1] * s[1], rotation[r][2] * s[2], t[r]] for r in range(3)] + \
        [[0.0, 0.0, 0.0, 1.0]]


def _multiply(a: list, b: list) -> list:
    """Return the product of two 4x4 row-major matrices."""
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _apply(m: list, p) -> tuple:
    """Return the point ``p`` transformed by the 4x4 row-major matrix ``m``."""
    return tuple(m[r][0] * p[0] + m[r][1] * p[1] + m[r][2] * p[2] + m[r][3] for r in range(3))


def _to_blender(p) -> list:
    """Return a glTF point (Y up, +Z front) in Blender's axes (Z up, -Y front)."""
    return [p[0], -p[2], p[1]]


def _positions(doc: dict, binary: bytes, index: int) -> list | None:
    """Return a POSITION accessor's points, read from the binary chunk, or None when they cannot be trusted.

    The points are used only when there are at most MAX_POSITIONS of them, they are plain 32-bit floats
    in the file's own binary chunk, and they agree with the accessor's own bounds. Otherwise the caller
    uses the corners of those bounds in their place.
    """
    accessors = _array(doc, "accessors", "the file")
    views = _array(doc, "bufferViews", "the file")
    buffers = _array(doc, "buffers", "the file")
    accessor = _object(accessors[_index(index, accessors, "a POSITION")], f"accessors[{index}]")
    count = accessor.get("count")
    if accessor.get("componentType") != 5126 or accessor.get("type") != "VEC3" or "sparse" in accessor \
            or not isinstance(count, int) or not 0 < count <= MAX_POSITIONS or "bufferView" not in accessor:
        return None
    view = _object(views[_index(accessor["bufferView"], views, f"accessors[{index}].bufferView")], "a bufferView")
    buffer = view.get("buffer", 0)
    if not isinstance(buffer, int) or not 0 <= buffer < len(buffers) or not isinstance(buffers[buffer], dict) \
            or "uri" in buffers[buffer]:
        return None
    stride = view.get("byteStride") or 12
    start = (view.get("byteOffset") or 0) + (accessor.get("byteOffset") or 0)
    if not all(isinstance(v, int) for v in (stride, start)) or stride < 12 or start < 0 \
            or start + stride * (count - 1) + 12 > len(binary):
        return None
    points = [struct.unpack_from("<3f", binary, start + i * stride) for i in range(count)]
    low, high = accessor.get("min"), accessor.get("max")
    if isinstance(low, list) and isinstance(high, list) and len(low) >= 3 and len(high) >= 3:
        for axis in range(3):
            values = [p[axis] for p in points]
            if not (isinstance(low[axis], (int, float)) and isinstance(high[axis], (int, float))) \
                    or abs(min(values) - low[axis]) > 1e-4 or abs(max(values) - high[axis]) > 1e-4:
                return None
    return points


def _describe(doc: dict, binary: bytes) -> dict:
    """Build the description ``describe`` returns, from the parsed document and the binary chunk."""
    nodes = _array(doc, "nodes", "the file")
    meshes = _array(doc, "meshes", "the file")
    materials = _array(doc, "materials", "the file")
    accessors = _array(doc, "accessors", "the file")
    summary = _summarize(doc, len(binary), [])
    mesh_points: dict = {}
    mesh_bounds: dict = {}

    def points_of(mesh_index: int) -> list:
        """Return a mesh's points, or its accessors' bounding corners where the points cannot be trusted."""
        if mesh_index not in mesh_points:
            out = []
            mesh = _object(meshes[mesh_index], f"meshes[{mesh_index}]")
            for primitive in _array(mesh, "primitives", f"meshes[{mesh_index}]"):
                attributes = _object(_object(primitive, "a primitive").get("attributes", {}), "attributes")
                if "POSITION" not in attributes:
                    continue
                read = _positions(doc, binary, attributes["POSITION"])
                if read is None:
                    accessor = _object(accessors[_index(attributes["POSITION"], accessors, "POSITION")], "POSITION")
                    low = _numbers(accessor.get("min", [0, 0, 0]), "POSITION min", 3)
                    high = _numbers(accessor.get("max", [0, 0, 0]), "POSITION max", 3)
                    read = [(a, b, c) for a in (low[0], high[0]) for b in (low[1], high[1]) for c in (low[2], high[2])]
                out.extend(read)
            mesh_points[mesh_index] = out
        return mesh_points[mesh_index]

    objects = []
    scenes = _array(doc, "scenes", "the file")
    if scenes:
        scene = _object(scenes[_index(doc.get("scene", 0), scenes, "scene")], "the scene")
        stack = [(index, None, None, [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]])
                 for index in reversed(_array(scene, "nodes", "the scene"))]
        while stack:
            index, parent, parent_index, above = stack.pop()
            index = _index(index, nodes, "a scene node")
            node = _object(nodes[index], f"nodes[{index}]")
            world = _multiply(above, _matrix(node, index))
            name = _text(node.get("name"), f"nodes[{index}].name") or f"node {index}"
            # Names may repeat in a file (clean_names gives two parents a Piece each); the node index tells them
            # apart, so each subtree is measured as its own.
            row = {"name": name, "id": index, "parent": parent, "parent_id": parent_index, "type": "EMPTY",
                   "triangles": 0,
                   "extras": node.get("extras") if isinstance(node.get("extras"), dict) else {},
                   "bounds": None, "origin": _to_blender(_apply(world, (0.0, 0.0, 0.0))), "materials": []}
            if "camera" in node:
                row["type"] = "CAMERA"
            if "mesh" in node:
                mesh_index = _index(node["mesh"], meshes, f"nodes[{index}].mesh")
                row["type"] = "MESH"
                row["triangles"] = summary["meshes"][mesh_index]["triangles"]
                row["materials"] = [m for m in summary["meshes"][mesh_index]["materials"] if m]
                # A mesh placed many times (a forest of one tree) usually has the same rotation and scale at
                # most of its places, so its bounds are measured once per rotation and scale, then moved to
                # each place.
                linear = tuple(world[r][c] for r in range(3) for c in range(3))
                if (mesh_index, linear) not in mesh_bounds:
                    turned = [[*world[r][:3], 0.0] for r in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
                    points = [_to_blender(_apply(turned, p)) for p in points_of(mesh_index)]
                    mesh_bounds[mesh_index, linear] = ([min(p[i] for p in points) for i in range(3)],
                                                       [max(p[i] for p in points) for i in range(3)]) \
                        if points else None
                box = mesh_bounds[mesh_index, linear]
                if box is not None:
                    place = _to_blender((world[0][3], world[1][3], world[2][3]))
                    row["bounds"] = [[box[0][i] + place[i] for i in range(3)], [box[1][i] + place[i] for i in range(3)]]
            objects.append(row)
            for child in reversed(_array(node, "children", f"nodes[{index}]")):
                stack.append((child, name, index, world))

    described = []
    for m, material in enumerate(materials):
        material = _object(material, f"materials[{m}]")
        pbr = _object(material.get("pbrMetallicRoughness", {}), f"materials[{m}].pbrMetallicRoughness")
        row = summary["materials"][m]
        textured = "metallicRoughnessTexture" in pbr
        strength = row["emissive_strength"]
        described.append({
            "name": row["name"] or f"material {m}",
            "base_color": None if "baseColorTexture" in pbr else row["base_color"][:3],
            "metallic": None if textured else row["metallic"],
            "roughness": None if textured else row["roughness"],
            "emission": None if "emissiveTexture" in material else [c * strength for c in row["emissive_factor"]],
        })
    return {"objects": objects, "materials": described}
