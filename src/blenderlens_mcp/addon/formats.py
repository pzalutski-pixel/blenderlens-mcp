"""Export to FBX, OBJ, STL, PLY and USD through Blender's own exporters, and read the file back.

The objects exported are chosen as export_glb chooses them, through its helpers: objects with their
children, a collection, or the scene. Hidden objects are exported only with visible_only false, and are
then revealed for the export and hidden again after. Cameras and lights are left out. Modifiers are
applied, as each exporter does by default, and USD is told to use the viewport's modifier settings, as
the other exporters do, rather than the render settings.

What the file holds is then read back by importing it into a temporary scene, which is removed with
everything the import made. The user's scene, selection and frame range are never touched (the USD
importer sets the frame range of the scene it imports into). The result compares the meshes and
triangles read back with those exported. STL and PLY files hold one mesh, so their objects come back as
one.
"""

import os

import bpy

# The commands module imports this one to build its table of commands, so the helpers of commands are
# looked up on the module when an export runs, rather than imported by name here, which would be circular.
from . import commands
from .common import (
    CommandError,
    Rollback,
    boolean,
    evaluated_counts,
    joined,
    plural,
    refuse_outside_object_mode,
    require,
    update,
)

#: For each file extension: (format name, the exporter as (operator module, operator name), the exporter's
#: keyword arguments, the importer as (operator module, operator name)).
FORMATS = {
    ".fbx": ("fbx", ("export_scene", "fbx"), {"use_selection": True, "use_visible": False,
                                               "use_mesh_modifiers": True}, ("import_scene", "fbx")),
    ".obj": ("obj", ("wm", "obj_export"), {"export_selected_objects": True, "apply_modifiers": True},
             ("wm", "obj_import")),
    ".stl": ("stl", ("wm", "stl_export"), {"export_selected_objects": True, "apply_modifiers": True,
                                           "ascii_format": False, "use_batch": False}, ("wm", "stl_import")),
    ".ply": ("ply", ("wm", "ply_export"), {"export_selected_objects": True, "apply_modifiers": True,
                                           "ascii_format": False}, ("wm", "ply_import")),
}
for _extension in (".usd", ".usda", ".usdc", ".usdz"):
    # visible_objects_only is not passed: Blender 4.2's USD exporter has it and 5.0's does not, and the
    # targets are revealed for the export anyway.
    FORMATS[_extension] = ("usd", ("wm", "usd_export"), {"selected_objects_only": True, "evaluation_mode": "VIEWPORT"},
                           ("wm", "usd_import"))
#: Formats whose file holds every object as one mesh.
MERGED = {"stl", "ply"}
# The next three limit the search for files the exporter wrote beside the file, such as USD's textures
# folder and OBJ's .mtl.
#: How many levels of subfolders deep the search looks.
BESIDE_DEPTH = 2
#: How many files the search looks at.
BESIDE_FILES = 5_000
#: How many of the files found the result lists.
BESIDE_LISTED = 50
#: The bpy.data collections an import may add to besides those import_file tracks, so that the read-back
#: removes everything the import made.
MORE_MADE_KINDS = ("grease_pencils", "volumes", "pointclouds", "hair_curves")
#: The object types export_file leaves out.
SKIPPED_TYPES = {"CAMERA", "LIGHT"}


def _operator(module, name, what):
    """Return the operator ``bpy.ops.<module>.<name>``, or raise a CommandError when this Blender lacks it.

    An operator is missing when its add-on is switched off, or in a Blender build without USD. ``what``
    names the operator in the error.
    """
    operator = getattr(getattr(bpy.ops, module), name)
    try:
        operator.get_rna_type()
    except KeyError:
        raise CommandError("blender_error", f"This Blender has no {what} ({module}.{name}).",
                           "Its add-on may be switched off in Blender's preferences.") from None
    return operator


def _kinds():
    """Return the bpy.data collections an import may add to, those this Blender has."""
    kinds = (*commands.IMPORTED_KINDS, *MORE_MADE_KINDS)
    return [kind for kind in kinds if isinstance(getattr(bpy.data, kind, None), bpy.types.bpy_prop_collection)]


def _files_beside(folder):
    """Return the files in ``folder`` and its subfolders, as {case-normalised path: (path, size, modification time)}.

    The walk goes BESIDE_DEPTH levels of subfolders deep and stops at BESIDE_FILES files. A file that
    cannot be read with os.stat is skipped.
    """
    out = {}
    top = os.path.normpath(folder)
    for root, folders, files in os.walk(top):
        if os.path.relpath(root, top).count(os.sep) + (root != top) >= BESIDE_DEPTH:
            folders[:] = []
        for name in files:
            path = os.path.join(root, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            out[os.path.normcase(path)] = (path, stat.st_size, stat.st_mtime_ns)
            if len(out) >= BESIDE_FILES:
                return out
    return out


def _written_beside(before, after, path):
    """Return the sorted paths of the files beside ``path`` that the export made or changed.

    ``path`` itself is left out. ``before`` and ``after`` are listings from _files_beside taken before
    and after the export.
    """
    own = os.path.normcase(os.path.normpath(path))
    return sorted(entry[0] for key, entry in after.items() if key != own and before.get(key) != entry)


def _read_back(path, importer, label):
    """Return how many mesh objects and triangles the file holds, read by importing it into a temporary scene.

    The temporary scene, and every datablock the import made, is removed afterwards, whether or not the
    import succeeded. Raise a CommandError when the importer fails or does not finish.
    """
    kinds = _kinds()
    before = {kind: set(getattr(bpy.data, kind)) for kind in kinds}
    scene = bpy.data.scenes.new("BlenderLens Read-Back")
    try:
        with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
            try:
                result = importer(filepath=path)
            except RuntimeError as exc:
                raise CommandError("blender_error", f"The {label} importer could not read the file back: "
                                                    f"{commands._blender_said(exc)}") from None
        if "FINISHED" not in result:
            raise CommandError("blender_error", f"The {label} importer did not read the file back ({sorted(result)}).")
        meshes = [o for o in scene.objects if o.type == "MESH"]
        return len(meshes), sum(sum(len(p.vertices) - 2 for p in o.data.polygons) for o in meshes)
    finally:
        made = [block for kind in kinds for block in getattr(bpy.data, kind) if block not in before[kind]]
        if made:
            bpy.data.batch_remove(made)
        bpy.data.scenes.remove(scene)


def export_file(params):
    """Export objects to FBX, OBJ, STL, PLY or USD, read the file back, and return what it holds."""
    path = require(params, "path")
    extension = os.path.splitext(path)[1].lower() if isinstance(path, str) else ""
    if extension not in FORMATS:
        raise CommandError("bad_argument", f"'{path}' does not end in a format export_file writes.",
                           "export_file writes " + joined(FORMATS) + " files. For .glb, use export_glb.")
    label, (module, name), keywords, (import_module, import_name) = FORMATS[extension]
    visible_only = boolean(params, "visible_only", True)
    exporter = _operator(module, name, f"{label.upper()} exporter")
    importer = _operator(import_module, import_name, f"{label.upper()} importer")
    # The view layer's object list lags behind objects linked since its last update, and such objects cannot
    # be selected.
    update()
    targets = [o for o in commands._targets_for_export(params) if o.type not in SKIPPED_TYPES]
    if visible_only:
        targets = [o for o in targets if o.visible_get()]
    meshes = [o for o in targets if o.type == "MESH"]
    if not meshes:
        if visible_only:
            raise CommandError("not_found", "Nothing to export: none of the visible objects chosen is a mesh.",
                               "get_scene shows each object's type and visibility. Pass visible_only false to "
                               "include hidden objects.")
        raise CommandError("not_found", "Nothing to export: none of the objects chosen is a mesh.",
                           "get_scene shows each object's type.")
    refuse_outside_object_mode(targets, "the exporter would read stale mesh data, or switch it to object mode")
    expected = sum((evaluated_counts(o) or {"triangles": 0})["triangles"] for o in meshes)

    layer = bpy.context.view_layer
    scene = bpy.context.scene
    previous_selection = [o for o in layer.objects if o.select_get()]
    previous_active = layer.objects.active
    previous_frame = (scene.frame_current, scene.frame_subframe)
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    beside = _files_beside(folder)
    undo = Rollback()
    try:
        if not visible_only:
            commands._reveal(targets, undo)
        for obj in layer.objects:
            obj.select_set(False)
        for obj in targets:
            try:
                obj.select_set(True)
            except RuntimeError:
                # Not in this view layer. It is revealed below, and if it still cannot be selected, the
                # read-back counts it as missing.
                pass
        missed = [o for o in targets if not commands._selected(o)]
        if missed:
            commands._reveal(missed, undo)
            for obj in missed:
                try:
                    obj.select_set(True)
                except RuntimeError:
                    pass
        try:
            result = exporter(filepath=path, check_existing=False, **keywords)
        except RuntimeError as exc:
            raise CommandError("blender_error", f"The {label} exporter failed: {commands._blender_said(exc)}") from None
    finally:
        for obj in layer.objects:
            obj.select_set(False)
        undo.undo()
        # The FBX exporter steps through the frames of any animation it bakes, which moves the scene's current
        # frame, so the frame is put back.
        if (scene.frame_current, scene.frame_subframe) != previous_frame:
            try:
                scene.frame_set(previous_frame[0], subframe=previous_frame[1])
            except (RuntimeError, TypeError, ValueError):
                Rollback.failures += 1
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
        raise CommandError("blender_error", f"The {label} exporter did not write {path} ({sorted(result)}).")
    written = _written_beside(beside, _files_beside(folder), path)

    objects, triangles = _read_back(path, importer, label)
    wanted = 1 if label in MERGED else len(meshes)
    out = {
        "path": path,
        "format": label,
        "bytes": os.path.getsize(path),
        "objects": [o.name for o in targets],
        "read_back": {"meshes": objects, "expected_meshes": wanted, "triangles": triangles,
                      "expected_triangles": expected, "matches": objects == wanted and triangles == expected},
        # USD writes the images its materials use into a textures folder beside the file, and OBJ writes its
        # .mtl file there.
        "also_written": written[:BESIDE_LISTED],
    }
    if len(written) > BESIDE_LISTED:
        out["also_written_count"] = len(written)
    if label in MERGED and len(meshes) > 1:
        out["note"] = (f"The {label.upper()} format holds one mesh per file, so the {len(meshes)} meshes exported "
                       "are merged into one in the file.")
    companion = os.path.splitext(path)[0] + ".mtl"
    if label == "obj" and os.path.isfile(companion):
        out["materials_file"] = companion
    if not out["read_back"]["matches"]:
        out["warnings"] = [{"code": "read_back_differs",
                            "message": f"The file reads back as {plural(objects, 'mesh', 'meshes')} and "
                                       f"{plural(triangles, 'triangle')}, but {plural(wanted, 'mesh', 'meshes')} and "
                                       f"{plural(expected, 'triangle')} were exported."}]
    return out
