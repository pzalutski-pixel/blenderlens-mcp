"""The tool table: every tool's name, title, description, input schema and annotations.

Names are a verb and the resource it acts on, and never repeat the server's name. Each
description says what the tool does, ``Returns:`` what, and ``WHEN TO CALL`` it, in as few
words as carry the facts. What a parameter means is said once, in that parameter, and what
the schema already says (types, enums) is not said again. Whether a tool reads, writes or
runs code is explicit in its annotations, which come from the five sets below. A test keeps
the serialised tools/list result under a fixed size, so that the descriptions stay short.
"""

#: Annotations of a tool that only reads. The MCP specification reads destructiveHint and idempotentHint
#: only when readOnlyHint is false, so they are left out. openWorldHint defaults to true, so false is given.
READ = {"readOnlyHint": True, "openWorldHint": False}
#: Annotations of a tool that adds something new: calling it twice adds two.
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
#: Annotations of a tool that replaces values, a file or a session: calling it twice with the same arguments
#: ends the same way as calling it once. render_preview has them too: given no path, each call writes one more
#: PNG to render_dir, but the scene, and any path it is given, end as one call leaves them.
REWRITE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}
#: Annotations of a tool that changes what exists in a way a second call compounds or cannot repeat.
CHANGE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
#: Annotations of a tool that runs arbitrary code, which can reach the file system and the network.
ANY = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True}

#: Tools the server handles itself; every other tool is a command sent to Blender.
LOCAL = {"get_status", "open_session", "close_session"}

#: Tools that run the agent's Python code. BLENDERLENS_ALLOW_PYTHON=0 refuses them and leaves them out of
#: tools/list.
PYTHON = ("run_python", "run_script")

#: The parameters of each tool that hold a file path, resolved against the server's current working directory:
#: one key, or a tuple of keys.
PATH_PARAMS = {
    "export_glb": "path", "import_file": "path", "save_file": "path", "open_file": "path",
    "render_preview": "path", "open_session": "file", "run_script": "path",
    "set_material": ("base_color_image", "normal_image", "emission_image"),
    "bake_texture": "path", "export_file": "path", "check_asset": "rules_file",
}


def path_keys(name: str) -> tuple:
    """Return the keys of a tool's arguments that hold a file path (from PATH_PARAMS), always as a tuple."""
    keys = PATH_PARAMS.get(name, ())
    return (keys,) if isinstance(keys, str) else keys

OBJECT = {"type": "string", "description": "Name"}
OBJECTS = {"type": "array", "items": {"type": "string"}, "description": "Names"}
MODIFIER = {"type": "string", "description": "Name"}
#: [x, y, z], where a null component keeps the value the command starts from.
VEC3 = {"type": "array", "items": {"type": ["number", "null"]}, "minItems": 3, "maxItems": 3}
#: [x, y, z] with no null component, for vectors that have no current value to keep: edit_mesh's vectors,
#: facing, measure_objects' ray, and apply_transform's origin point.
XYZ = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
NUMBER_OR_VEC3 = {"anyOf": [{"type": "number"}, VEC3]}
SIDE_OR_XYZ = {"anyOf": [{"type": "string", "enum": ["top", "bottom", "front", "back", "left", "right"]}, XYZ]}
COLOR = {"anyOf": [
    {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 4},
    {"type": "string", "pattern": "^#?([0-9A-Fa-f]{2}){3,4}$"},
]}
#: The discard parameter of the session tools, which covers a busy Blender as well as unsaved work: without
#: it, both are refused.
LET_GO = {"type": "boolean", "description": "Allow losing unsaved or busy work"}
#: The discard parameter of open_file, create_file and run_script: without it, unsaved changes are refused.
DISCARD = {"type": "boolean", "description": "Allow losing unsaved changes"}
#: render_preview's named views; the add-on's preview.VIEWS holds their angles.
VIEWS = ("front", "back", "left", "right", "top", "bottom", "iso", "three_quarter")

FACE_SELECTION = {
    "faces": {"type": "array", "items": {"type": "integer"}, "description": "Indices from get_mesh"},
    "facing": {**SIDE_OR_XYZ, "description": "World direction"},
    "tolerance": {"type": "number", "description": "Degrees off facing (30)"},
}

PLACEMENT = {
    "location": {**VEC3, "description": "Metres"},
    "rotation": {**VEC3, "description": "Degrees"},
    "scale": {**NUMBER_OR_VEC3, "description": "Default 1"},
    "collection": {"type": "string", "description": "Created if missing"},
    "parent": {"type": "string", "description": "location relative to it"},
}


def schema(properties: dict | None = None, required: list | None = None) -> dict:
    """Build an input schema from its properties and required keys.

    A key the schema does not name is refused, never silently ignored. An empty required list is
    left out, as JSON Schema allows.
    """
    out = {"type": "object", "properties": properties or {}, "additionalProperties": False}
    if required:
        out["required"] = required
    return out


TOOLS = [
    # -- the session ------------------------------------------------------
    {
        "name": "get_status",
        "title": "Get Status",
        "description": (
            "Report the attached Blender without starting one. "
            "Returns: the session (busy: what it runs), add-on, Blender, render_dir, recovery copies. "
            "WHEN TO CALL: first; after not_connected, connection_lost, busy."
        ),
        "inputSchema": schema(),
        "annotations": READ,
    },
    {
        "name": "open_session",
        "title": "Open Session",
        "description": (
            "Attach to the live Blender window or start a background one. "
            "Returns: the session. "
            "WHEN TO CALL: to pick a mode or file; else the first call attaches by BLENDERLENS_MODE."
        ),
        "inputSchema": schema({
            "mode": {"type": "string", "enum": ["auto", "live", "background"],
                     "description": "auto (default): live if the add-on replies"},
            "file": {"type": "string", "description": ".blend to open"},
            "discard": LET_GO,
        }),
        "annotations": REWRITE,
    },
    {
        "name": "close_session",
        "title": "Close Session",
        "description": (
            "Disconnect: a background Blender exits, a live one stays open. "
            "Returns: the mode closed. "
            "WHEN TO CALL: when the work is saved."
        ),
        "inputSchema": schema({"discard": LET_GO}),
        "annotations": REWRITE,
    },
    # -- inspecting -------------------------------------------------------
    {
        "name": "get_scene",
        "title": "Get Scene",
        "description": (
            "Read the scene. "
            "Returns: file state, units, and each object's type, parent, location, dimensions, counts, "
            "materials, modifiers. "
            "WHEN TO CALL: before building; after not_found."
        ),
        "inputSchema": schema(),
        "annotations": READ,
    },
    {
        "name": "get_object",
        "title": "Get Object",
        "description": (
            "Read one object in full. "
            "Returns: transform, bounds, properties, counts before and after modifiers, modifier settings by "
            "Blender's names, slots. "
            "WHEN TO CALL: before set_modifier."
        ),
        "inputSchema": schema({"object": OBJECT}, ["object"]),
        "annotations": READ,
    },
    {
        "name": "get_mesh",
        "title": "Get Mesh",
        "description": (
            "List a mesh's faces. "
            "Returns: each face's index, center, normal, side, area, material slot. "
            "WHEN TO CALL: for face indices; again after faces change."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "space": {"type": "string", "enum": ["world", "local"], "description": "Default world"},
            "limit": {"type": "integer", "description": "Default 200"},
            "include_vertices": {"type": "boolean", "description": "List vertices too"},
            "evaluated": {"type": "boolean", "description": "After modifiers"},
            "material": {"type": ["integer", "string"], "description": "Its faces only: slot or name"},
        }, ["object"]),
        "annotations": READ,
    },
    {
        "name": "list_materials",
        "title": "List Materials",
        "description": (
            "List materials. "
            "Returns: each one's values (linear, as glTF) and users. "
            "WHEN TO CALL: before set_material, to reuse one."
        ),
        "inputSchema": schema(),
        "annotations": READ,
    },
    # -- checking --------------------------------------------------------
    {
        "name": "check_asset",
        "title": "Check Asset",
        "description": (
            "Check meshes as exported: topology, normals, scale, origin, UVs, textures, morphs, overlaps; and a "
            "project's rules or rules_file (the guide lists the keys). "
            "Returns: passed; per object stats, issues {code, severity, count, faces}. "
            "WHEN TO CALL: before export_glb."
        ),
        "inputSchema": schema({
            "objects": {**OBJECTS, "description": "With children (default: all export_glb writes)"},
            "rules": {"type": "object", "description": "The project's rules"},
            "rules_file": {"type": "string", "description": "Rules as a .json file"},
            "budget": {"type": "integer", "description": "Total triangle limit"},
            "evaluated": {"type": "boolean", "description": "After modifiers (true)"},
        }),
        "annotations": READ,
    },
    {
        "name": "measure_objects",
        "title": "Measure Objects",
        "description": (
            "Measure between objects' surfaces as exported, in world space, or cast a ray. "
            "Returns: per pair distance, closest points, touching, overlapping, overlap_faces, inside; ray object, "
            "location, "
            "normal, face. "
            "WHEN TO CALL: to fit parts, find gaps."
        ),
        "inputSchema": schema({
            "objects": {**OBJECTS, "description": "2 to 16; a ray may hit only these"},
            "origin": {**XYZ, "description": "Ray start"},
            "direction": {**SIDE_OR_XYZ, "description": "Ray, world"},
        }),
        "annotations": READ,
    },
    # -- building ---------------------------------------------------------
    {
        "name": "create_object",
        "title": "Create Object",
        "description": (
            "Add a primitive (tube, lathe and text too) or an empty, sized in the mesh so scale stays 1. "
            "Returns: its name (.001 on a clash), transform, counts. "
            "WHEN TO CALL: to start a part."
        ),
        "inputSchema": schema({
            "kind": {"type": "string", "enum": ["cube", "plane", "grid", "circle", "cylinder", "cone", "uv_sphere",
                                                "ico_sphere", "torus", "monkey", "tube", "lathe", "text", "empty"],
                     "description": "empty has no mesh"},
            "name": {"type": "string", "description": "Default from kind"},
            "size": {"anyOf": [{"type": "number"}, {**VEC3, "minItems": 2}],
                     "description": "cube edges (2); plane, grid [x, y]; monkey, empty, text: a number"},
            "radius": {"type": "number", "description": "Default 1; tube 0.05"},
            "radius_top": {"type": "number", "description": "Cone top (0)"},
            "minor_radius": {"type": "number", "description": "Torus tube (0.25)"},
            "depth": {"type": "number", "description": "Cylinder, cone height (2)"},
            "segments": {"type": "integer", "description": "Default 32, torus 48"},
            "rings": {"type": "integer", "description": "uv_sphere 16, torus 12"},
            "subdivisions": {"type": "integer", "description": "ico_sphere 2, grid 10"},
            "fill": {"type": "boolean", "description": "Caps (true; circle: false)"},
            "points": {"type": "array", "items": {"type": "array"}, "description": "tube path, mesh space"},
            "smooth": {"type": "boolean", "description": "tube: curved (true) or straight"},
            "bevel_resolution": {"type": "integer", "description": "tube: 4+2n sides (2)"},
            "profile": {"type": "array", "items": {"type": "array"}, "description": "lathe: [radius, z] about Z"},
            "text": {"type": "string", "description": "Its words"},
            "extrude": {"type": "number", "description": "text depth (size/10)"},
            **PLACEMENT,
        }, ["kind"]),
        "annotations": WRITE,
    },
    {
        "name": "create_mesh",
        "title": "Create Mesh",
        "description": (
            "Build a mesh object from vertices and faces in its own space. "
            "Returns: as create_object. "
            "WHEN TO CALL: for a shape no primitive gives."
        ),
        "inputSchema": schema({
            "name": {"type": "string", "description": "Object and mesh"},
            "vertices": {"type": "array", "items": {"type": "array", "items": {"type": "number"},
                                                    "minItems": 3, "maxItems": 3},
                         "description": "Metres"},
            "faces": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}, "minItems": 3},
                      "description": "Vertex indices around each"},
            "edges": {"type": "array", "items": {"type": "array", "items": {"type": "integer"},
                                                 "minItems": 2, "maxItems": 2},
                      "description": "Loose edges"},
            "recalc_normals": {"type": "boolean", "description": "Outward (default true)"},
            **PLACEMENT,
        }, ["name", "vertices"]),
        "annotations": WRITE,
    },
    {
        "name": "edit_mesh",
        "title": "Edit Mesh",
        "description": (
            "Run one operation on the faces picked (faces, facing) or all. bevel: the picked faces' rim edges (all "
            "edges if all faces), or with angle the edges sharper than it. amount, offset, pivot: mesh space. "
            "Returns: result_faces (extrude: the cap; inset: inner faces), counts. "
            "WHEN TO CALL: to shape a primitive."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "operation": {"type": "string", "enum": ["extrude", "inset", "translate", "scale", "rotate", "delete",
                                                     "subdivide", "bevel", "recalc_normals", "flip_normals",
                                                     "merge", "triangulate", "shade_smooth", "shade_flat", "bisect",
                                                     "solidify", "dissolve_planar", "mark_seams"],
                          "description": "What to do"},
            **FACE_SELECTION,
            "amount": {"type": "number", "description": "extrude distance, inset border, bevel width, merge distance, "
                                                        "solidify thickness (inward)"},
            "direction": {**SIDE_OR_XYZ, "description": "extrude this way; bisect plane normal (world)"},
            "depth": {"type": "number", "description": "inset: in (-) or out"},
            "individual": {"type": "boolean", "description": "inset each face alone"},
            "offset": {**XYZ, "description": "translate by"},
            "factor": {"anyOf": [{"type": "number"}, XYZ], "description": "scale by"},
            "rotation": {**XYZ, "description": "rotate by, degrees"},
            "pivot": {**XYZ, "description": "scale, rotate about; bisect plane point (default center)"},
            "cuts": {"type": "integer", "description": "subdivide (1)"},
            "segments": {"type": "integer", "description": "bevel (1)"},
            "angle": {"type": "number", "description": "Degrees: sharper edges stay sharp (shade_smooth), bevel, "
                                                       "seam (30); dissolve_planar (5)"},
            "keep": {"type": "string", "enum": ["below", "above", "both"], "description": "bisect: side kept (below)"},
            "fill": {"type": "boolean", "description": "bisect: cap the cut (true)"},
        }, ["object", "operation"]),
        "annotations": CHANGE,
    },
    {
        "name": "add_modifier",
        "title": "Add Modifier",
        "description": (
            "Add a modifier to an object's stack. "
            "Returns: its settings, the stack, counts. "
            "WHEN TO CALL: for detail that stays editable."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "type": {"type": "string", "description": "bevel, array, boolean, subdivision, mirror, solidify..."},
            "name": {"type": "string", "description": "Default from type"},
            "settings": {"type": "object", "description": "Blender's names (width, count, levels, operation...); "
                                                          "objects by name"},
        }, ["object", "type"]),
        "annotations": WRITE,
    },
    {
        "name": "set_modifier",
        "title": "Set Modifier",
        "description": (
            "Change a modifier's settings, place or visibility. "
            "Returns: as add_modifier. "
            "WHEN TO CALL: to tune one."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "modifier": MODIFIER,
            "settings": {"type": "object", "description": "By Blender's property names"},
            "index": {"type": "integer", "description": "Stack position, 0 first"},
            "show": {"type": "boolean", "description": "In viewport and render"},
        }, ["object", "modifier"]),
        "annotations": REWRITE,
    },
    {
        "name": "apply_modifier",
        "title": "Apply Modifier",
        "description": (
            "Bake a modifier into the mesh; refused for a shared mesh or in edit mode. "
            "Returns: the stack, counts. "
            "WHEN TO CALL: when edit_mesh must see its result."
        ),
        "inputSchema": schema({"object": OBJECT, "modifier": MODIFIER}, ["object", "modifier"]),
        "annotations": CHANGE,
    },
    {
        "name": "remove_modifier",
        "title": "Remove Modifier",
        "description": (
            "Remove a modifier unapplied. "
            "Returns: the stack left, counts. "
            "WHEN TO CALL: to undo add_modifier."
        ),
        "inputSchema": schema({"object": OBJECT, "modifier": MODIFIER}, ["object", "modifier"]),
        "annotations": CHANGE,
    },
    {
        "name": "set_material",
        "title": "Set Material",
        "description": (
            "Create or update a material, optionally on an object or its picked faces; all its users change; '' "
            "clears an image or vertex_color. "
            "Returns: its values, slot, faces. "
            "WHEN TO CALL: after shaping."
        ),
        "inputSchema": schema({
            "material": {"type": "string", "description": "Created if missing"},
            "object": {"type": "string", "description": "Mesh to put it on"},
            "base_color": {**COLOR, "description": "[r, g, b(, a)] linear, or '#RRGGBB' sRGB"},
            "metallic": {"type": "number", "description": "0 to 1"},
            "roughness": {"type": "number", "description": "0 to 1"},
            "emission_color": {**COLOR, "description": "Alone sets strength 1"},
            "emission_strength": {"type": "number", "description": "Above 1 glows"},
            "alpha": {"type": "number", "description": "Below 1 blends"},
            "alpha_mode": {"type": "string", "enum": ["opaque", "blend", "clip"],
                           "description": "With an image or vertex_color, their alpha"},
            "alpha_cutoff": {"type": "number", "description": "clip below (0.5)"},
            "backface_culling": {"type": "boolean", "description": "Hide back faces (true for new)"},
            "vertex_color": {"type": "string", "description": "Color attribute as base colour"},
            "base_color_image": {"type": "string", "description": "Image file"},
            "normal_image": {"type": "string", "description": "Image file"},
            "emission_image": {"type": "string", "description": "Image file"},
            **FACE_SELECTION,
        }, ["material"]),
        "annotations": REWRITE,
    },
    {
        "name": "set_vertex_color",
        "title": "Set Vertex Color",
        "description": (
            "Paint the faces picked (faces, facing) or all into a color attribute. "
            "Returns: faces painted, materials wired, and not_wired (setups not BlenderLens's). "
            "WHEN TO CALL: for colour without textures; exports as COLOR_0."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "color": {**COLOR, "description": "As base_color"},
            **FACE_SELECTION,
            "attribute": {"type": "string", "description": "Default Col, made white"},
            "use_in_material": {"type": "boolean", "description": "Wire into its materials (true)"},
        }, ["object", "color"]),
        "annotations": REWRITE,
    },
    {
        "name": "unwrap_uv",
        "title": "Unwrap UVs",
        "description": (
            "Unwrap a mesh into a UV map; projections are packed. "
            "Returns: islands, area used, overlapping and out-of-bounds faces, texel density (px/m at 1024). "
            "WHEN TO CALL: before texturing or baking; unwrap needs seams (edit_mesh mark_seams)."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "method": {"type": "string", "enum": ["smart", "cube", "cylinder", "sphere", "unwrap", "lightmap"],
                       "description": "Default smart"},
            "uv_map": {"type": "string", "description": "Made if missing (default active)"},
            "margin": {"type": "number", "description": "Between islands, to 0.1; lightmap to 0.01 (0.01)"},
        }, ["object"]),
        "annotations": CHANGE,
    },
    {
        "name": "join_objects",
        "title": "Join Objects",
        "description": (
            "Join meshes into the first, keeping each piece's materials and faces. "
            "Returns: the object, slots and their faces. "
            "WHEN TO CALL: to make one part of pieces."
        ),
        "inputSchema": schema({"objects": {**OBJECTS, "minItems": 2, "description": "The first stays"}}, ["objects"]),
        "annotations": CHANGE,
    },
    # -- arranging --------------------------------------------------------
    {
        "name": "set_transform",
        "title": "Set Transform",
        "description": (
            "Set an object's location, rotation, scale or dimensions; null keeps a component. "
            "Returns: the transform; after drop, landed_on and moved_by. "
            "WHEN TO CALL: to place parts."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "location": {**VEC3, "description": "Metres, parent space"},
            "rotation": {**VEC3, "description": "Degrees"},
            "scale": {**NUMBER_OR_VEC3, "description": "Factors"},
            "dimensions": {**VEC3, "description": "Metres, reached by scale"},
            "drop": {"type": ["boolean", "string"], "description": "Then move along Z to rest on what is below, "
                                                                   "or on this object"},
        }, ["object"]),
        "annotations": REWRITE,
    },
    {
        "name": "apply_transform",
        "title": "Apply Transform",
        "description": (
            "Bake rotation and scale into the mesh, or move its origin, mesh and children staying put; refused for "
            "a shared mesh or in edit mode. "
            "Returns: the transform. "
            "WHEN TO CALL: after scaling, before bevel or solidify."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "location": {"type": "boolean", "description": "Default false"},
            "rotation": {"type": "boolean", "description": "Default true; false with origin"},
            "scale": {"type": "boolean", "description": "Default true; false with origin"},
            "origin": {"anyOf": [{"type": "string", "enum": ["bottom_center", "bounds_center", "geometry"]}, XYZ],
                       "description": "New origin, or a world point"},
        }, ["object"]),
        "annotations": CHANGE,
    },
    {
        "name": "set_parent",
        "title": "Set Parent",
        "description": (
            "Parent an object, or clear its parent. "
            "Returns: the transform. "
            "WHEN TO CALL: to assemble parts into one node tree."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "parent": {"type": ["string", "null"], "description": "null clears"},
            "keep_transform": {"type": "boolean", "description": "Keep world position (true)"},
        }, ["object"]),
        "annotations": REWRITE,
    },
    {
        "name": "set_collection",
        "title": "Set Collection",
        "description": (
            "Move objects into a collection. "
            "Returns: their collections. "
            "WHEN TO CALL: to group parts."
        ),
        "inputSchema": schema({
            "objects": OBJECTS,
            "collection": {"type": "string", "description": "Created if missing"},
            "parent": {"type": "string", "description": "Parent of a new one"},
            "keep_other": {"type": "boolean", "description": "Stay in others too"},
        }, ["objects", "collection"]),
        "annotations": REWRITE,
    },
    {
        "name": "set_object",
        "title": "Set Object Properties",
        "description": (
            "Rename or hide an object, or set its display or custom properties (exported as glTF extras). "
            "Returns: its state. "
            "WHEN TO CALL: to name, hide or tag parts."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "rename": {"type": "string", "description": "New name"},
            "rename_data": {"type": "boolean", "description": "Unshared mesh too (default true)"},
            "hide": {"type": "boolean", "description": "Viewport; export skips it"},
            "hide_render": {"type": "boolean", "description": "Renders"},
            "display": {"type": "string", "enum": ["TEXTURED", "SOLID", "WIRE", "BOUNDS"],
                        "description": "Viewport"},
            "empty_display": {"type": "string", "description": "An empty's shape: ARROWS, CUBE, SPHERE..."},
            "properties": {"type": "object", "description": "JSON values, 8 levels deep; null removes"},
        }, ["object"]),
        "annotations": REWRITE,
    },
    {
        "name": "duplicate_object",
        "title": "Duplicate Object",
        "description": (
            "Copy an object with modifiers, materials and children. "
            "Returns: the copies. "
            "WHEN TO CALL: for repeated parts."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "name": {"type": "string", "description": "For the copy"},
            "linked": {"type": "boolean", "description": "Share the mesh"},
            "with_children": {"type": "boolean", "description": "Default true"},
            "offset": {**VEC3, "description": "Metres"},
        }, ["object"]),
        "annotations": WRITE,
    },
    {
        "name": "delete_object",
        "title": "Delete Objects",
        "description": (
            "Delete objects and unused mesh data. "
            "Returns: the names deleted. "
            "WHEN TO CALL: to remove parts."
        ),
        "inputSchema": schema({
            "objects": OBJECTS,
            "with_children": {"type": "boolean", "description": "Else they stay in place"},
        }, ["objects"]),
        "annotations": CHANGE,
    },
    # -- files ------------------------------------------------------------
    {
        "name": "export_glb",
        "title": "Export glTF Binary",
        "description": (
            "Write a .glb, Y-up (-Y front becomes +Z), for a target engine; what it cannot load is refused, as is "
            "edit mode. "
            "Returns: the file read back, with warnings for the target. "
            "WHEN TO CALL: to deliver."
        ),
        "inputSchema": schema({
            "path": {"type": "string", "description": "Ends in .glb"},
            "objects": {**OBJECTS, "description": "With children (default all)"},
            "collection": {"type": "string", "description": "Its objects"},
            "target": {"type": "string", "enum": ["godot", "threejs", "generic"],
                       "description": "Default generic; threejs drops -colonly proxies"},
            "apply_modifiers": {"type": "boolean", "description": "Default true"},
            "visible_only": {"type": "boolean", "description": "Default true"},
            "extras": {"type": "boolean", "description": "Custom properties (default true)"},
            "clean_names": {"type": "boolean", "description": "Drop .001 where unique (true for godot)"},
            "draco": {"type": "boolean", "description": "Compress meshes"},
            "image_format": {"type": "string", "enum": ["auto", "jpeg", "webp", "none"], "description": "Default auto"},
            "vertex_colors": {"type": "string", "enum": ["material", "active", "none"],
                              "description": "Default material: those a material reads"},
            "animations": {"type": "boolean", "description": "Default true"},
            "tangents": {"type": "boolean", "description": "For normal maps"},
            "texcoords": {"type": "boolean", "description": "UVs (default true)"},
            "lights": {"type": "boolean", "description": "Default false"},
            "cameras": {"type": "boolean", "description": "Default false"},
            "at_origin": {"type": "boolean", "description": "Roots written at 0,0,0"},
        }, ["path"]),
        "annotations": REWRITE,
    },
    {
        "name": "import_file",
        "title": "Import File",
        "description": (
            "Import a .glb, .gltf, .obj, .fbx, .stl or .ply, or append from a .blend what is named. "
            "Returns: what it added. "
            "WHEN TO CALL: for a reference, or to check an export."
        ),
        "inputSchema": schema({
            "path": {"type": "string", "description": "File"},
            "collection": {"type": "string", "description": "For what it adds"},
            "objects": {**OBJECTS, "description": ".blend: with children"},
            "collections": {**OBJECTS, "description": ".blend"},
            "materials": {**OBJECTS, "description": ".blend"},
        }, ["path"]),
        "annotations": WRITE,
    },
    {
        "name": "save_file",
        "title": "Save File",
        "description": (
            "Save the open file. "
            "Returns: path, size. "
            "WHEN TO CALL: at milestones; before closing a background session."
        ),
        "inputSchema": schema({
            "path": {"type": "string", "description": ".blend; default the last save"},
            "copy": {"type": "boolean", "description": "A checkpoint; the open file keeps its path"},
        }),
        "annotations": REWRITE,
    },
    {
        "name": "open_file",
        "title": "Open File",
        "description": (
            "Open a .blend in place of the open file. "
            "Returns: the scene. "
            "WHEN TO CALL: to resume work."
        ),
        "inputSchema": schema({"path": {"type": "string", "description": ".blend"}, "discard": DISCARD}, ["path"]),
        "annotations": REWRITE,
    },
    {
        "name": "create_file",
        "title": "Create File",
        "description": (
            "Start a new, unsaved file in place of the open one. "
            "Returns: the scene. "
            "WHEN TO CALL: for a new model."
        ),
        "inputSchema": schema({
            "empty": {"type": "boolean", "description": "Default true; else Blender's default scene"},
            "discard": DISCARD,
        }),
        "annotations": REWRITE,
    },
    # -- seeing -----------------------------------------------------------
    {
        "name": "render_preview",
        "title": "Render Preview",
        "description": (
            "Render a PNG of objects (and children) or all visible geometry, framed, others hidden, the scene "
            "unchanged. "
            "Returns: its path (open it, or inline). "
            "WHEN TO CALL: after each build step."
        ),
        "inputSchema": schema({
            "objects": {**OBJECTS, "description": "Default all visible"},
            "view": {"type": "string", "enum": list(VIEWS), "description": "Default three_quarter"},
            "azimuth": {"type": "number", "description": "Degrees, front 0, right 90"},
            "elevation": {"type": "number", "description": "Degrees up"},
            "projection": {"type": "string", "enum": ["perspective", "orthographic"],
                           "description": "Default perspective"},
            "resolution": {"anyOf": [{"type": "integer"},
                                     {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}],
                           "description": "Pixels (768)"},
            "engine": {"type": "string", "enum": ["auto", "cycles", "eevee", "workbench"],
                       "description": "Default auto: cycles (CPU); eevee needs GPU"},
            "samples": {"type": "integer", "description": "Default 32"},
            "lighting": {"type": "string", "enum": ["studio", "scene"], "description": "Default studio"},
            "transparent": {"type": "boolean", "description": "Background"},
            "margin": {"type": "number", "description": "Framing slack (1.1)"},
            "path": {"type": "string", "description": "*.png, only over a PNG (default render_dir)"},
            "timeout": {"type": "number", "description": "Seconds (120)"},
            "inline": {"type": "boolean", "description": "Return the image too"},
            "views": {"type": "array", "items": {"type": "string", "enum": list(VIEWS)},
                      "description": "Several, in one PNG"},
            "style": {"type": "string", "enum": ["shaded", "clay", "wireframe", "random"],
                      "description": "clay, random use workbench"},
            "context": {**OBJECTS, "description": "Shown, not framed"},
            "hide_from_camera": {**OBJECTS, "description": "Unseen, still casting shadows"},
            "markers": {**OBJECTS, "description": "Return their pixels"},
            "ortho_scale": {"type": "number", "description": "Metres on long side"},
        }),
        "annotations": REWRITE,
    },
    # -- motion, collision, baking, other formats ------------------------
    {
        "name": "animate_object",
        "title": "Animate Object",
        "description": (
            "Key an object's transform and shape keys into a named clip on an NLA track, exported as one glTF "
            "animation; a call replaces the object's keys in it. "
            "Returns: channels, frames, seconds, Godot warnings. "
            "WHEN TO CALL: for doors, rotors, morphs."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "clip": {"type": "string", "description": "Animation name, shared across objects"},
            "keys": {"type": "array", "items": {"type": "object"}, "minItems": 1,
                     "description": "{frame, location, rotation (degrees), scale, shape_key: {name: value}}; null "
                                    "skips an axis"},
            "interpolation": {"type": "string", "enum": ["linear", "constant", "bezier"],
                              "description": "Default linear"},
            "loop": {"type": "boolean", "description": "Repeat; default as Godot reads the name"},
        }, ["object", "clip", "keys"]),
        "annotations": CHANGE,
    },
    {
        "name": "create_collision",
        "title": "Create Collision",
        "description": (
            "Add a collision proxy over a mesh, wire-drawn and parented; named for Godot, three.js exports leave it "
            "out. "
            "Returns: its name, counts, closed, volume_ratio. "
            "WHEN TO CALL: for physics props."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "shape": {"type": "string", "enum": ["box", "convex", "decimated"], "description": "Default convex"},
            "naming": {"type": "string", "enum": ["godot", "plain"],
                       "description": "godot (default): -colonly, convex -convcolonly; plain: _collision"},
            "ratio": {"type": "number", "description": "decimated: faces kept (0.25)"},
        }, ["object"]),
        "annotations": WRITE,
    },
    {
        "name": "bake_texture",
        "title": "Bake Texture",
        "description": (
            "Bake a mesh's materials into a PNG through a UV map with Cycles (CPU), wired into them; render "
            "settings and selection are put back. "
            "Returns: path, size, seconds, image_driven channels. "
            "WHEN TO CALL: for procedural materials, AO."
        ),
        "inputSchema": schema({
            "object": OBJECT,
            "type": {"type": "string", "enum": ["diffuse", "ao", "normal", "emit", "combined"],
                     "description": "Default diffuse; ao multiplies base colour"},
            "from_objects": {**OBJECTS, "description": "High-poly sources"},
            "size": {"type": "integer", "description": "Pixels a side (1024)"},
            "samples": {"type": "integer", "description": "Default 32"},
            "uv_map": {"type": "string", "description": "Default active"},
            "path": {"type": "string", "description": "*.png, only over a PNG"},
            "use_in_material": {"type": "boolean", "description": "Default true"},
            "timeout": {"type": "number", "description": "Seconds (120)"},
        }, ["object", "path"]),
        "annotations": CHANGE,
    },
    {
        "name": "export_file",
        "title": "Export File",
        "description": (
            "Write FBX, OBJ, STL, PLY or USD (by extension), objects chosen as export_glb does; the file is read "
            "back in a scratch scene. "
            "Returns: path, bytes, read_back counts. "
            "WHEN TO CALL: for tools that take no glTF."
        ),
        "inputSchema": schema({
            "path": {"type": "string", "description": ".fbx .obj .stl .ply .usd(a|c|z)"},
            "objects": {**OBJECTS, "description": "With children (default all)"},
            "collection": {"type": "string", "description": "Its objects"},
            "visible_only": {"type": "boolean", "description": "Default true"},
        }, ["path"]),
        "annotations": REWRITE,
    },
    # -- the escape hatch ---------------------------------------------------
    {
        "name": "run_python",
        "title": "Run Python",
        "description": (
            "Run Python in Blender (bpy, bmesh, mathutils); names persist; set result to return it. "
            "Returns: stdout, stderr, result. "
            "WHEN TO CALL: only for what no tool covers."
        ),
        "inputSchema": schema({
            "code": {"type": "string", "description": "Python source"},
            "timeout": {"type": "number", "description": "Seconds (120)"},
        }, ["code"]),
        "annotations": ANY,
    },
    {
        "name": "run_script",
        "title": "Run Script",
        "description": (
            "Run a .py file in Blender as run_python does, with args; its folder, and parents up to a "
            "blenderlens.toml, are importable afresh each run. "
            "Returns: result, stdout, stderr, objects added, removed, changed. "
            "WHEN TO CALL: to build a model from its script."
        ),
        "inputSchema": schema({
            "path": {"type": "string", "description": ".py file"},
            "args": {"type": "object", "description": "The script's args"},
            "fresh": {"type": "boolean", "description": "Start from an empty file"},
            "discard": DISCARD,
            "timeout": {"type": "number", "description": "Seconds (120)"},
        }, ["path"]),
        "annotations": ANY,
    },
    # -- undoing ------------------------------------------------------------
    {
        "name": "undo_change",
        "title": "Undo Change",
        "description": (
            "Step back through undo steps, one per call that changed something; Python references go stale. "
            "Returns: steps undone, counts, unsaved_changes. "
            "WHEN TO CALL: to take back calls that went wrong."
        ),
        "inputSchema": schema({
            "steps": {"type": "integer", "description": "Default 1"},
            "list": {"type": "boolean", "description": "Name the steps; undo none"},
            "force": {"type": "boolean", "description": "Undo steps whatever made them"},
        }),
        "annotations": CHANGE,
    },
]

BY_NAME = {tool["name"]: tool for tool in TOOLS}
