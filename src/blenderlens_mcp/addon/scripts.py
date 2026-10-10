"""Run Python from the agent: run_python's code in a namespace kept between calls, and run_script's files.

Both can do anything Blender can, so both obey the same two settings: the add-on's preference "Allow Python
from the agent" here, and BLENDERLENS_ALLOW_PYTHON in the MCP server. Both catch BaseException, so nothing the
code raises ends Blender.

A script runs in a namespace of its own, made afresh for each run, with its folder (and the folders above it up
to one holding a ``blenderlens.toml``) importable for that run alone. Its helper modules are imported afresh
too, so an edit to them shows in the next run.
"""

import array
import contextlib
import io
import itertools
import json
import math
import os
import re
import sys
import traceback
import types
import zlib

import bmesh
import bpy
import mathutils

from .common import CommandError, boolean, jsonable, require, update

HERE = os.path.dirname(os.path.abspath(__file__))

#: The file that marks a project's root: run_script puts every folder from the script's own up to the one
#: holding this file on sys.path.
PROJECT_MARKER = "blenderlens.toml"

#: The namespace run_python's code runs in, kept from call to call for the whole session.
NAMESPACE = {}

#: The most names run_script's added, removed and changed lists hold; the counts beside them cover every name.
MAX_NAMES = 200


def switched_off():
    """Return whether the user of this Blender turned off the preference "Allow Python from the agent".

    Only an installed, enabled add-on has preferences. A background Blender loads this code under
    another package name, so it finds none and leaves the choice to the MCP server's
    BLENDERLENS_ALLOW_PYTHON.
    """
    addon = bpy.context.preferences.addons.get(__package__ or "")
    prefs = getattr(addon, "preferences", None)
    return prefs is not None and not getattr(prefs, "allow_python", True)


def _refuse_if_switched_off(tool):
    """Refuse ``tool`` when the add-on's preference "Allow Python from the agent" is off."""
    if switched_off():
        raise CommandError("not_allowed", f"{tool} is turned off in this Blender: the BlenderLens add-on's preference "
                                          "\"Allow Python from the agent\" is off.",
                           "The person using this Blender can turn \"Allow Python from the agent\" on in the "
                           "BlenderLens panel of the 3D viewport's sidebar (N), or under Edit > Preferences > Add-ons "
                           "> BlenderLens. The other tools still work.")


def base_names():
    """Return the names agent code finds without importing: Blender's modules, math, and common mathutils types."""
    return {"bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "Vector": mathutils.Vector,
            "Matrix": mathutils.Matrix, "Euler": mathutils.Euler, "math": math}


def _ours(entry):
    """Return whether a traceback entry is a frame of this add-on rather than of the agent's code."""
    return entry.startswith("  File ") and f'"{HERE}{os.sep}' in entry


#: The most characters of stdout a result carries, taken from the end of what the code printed.
STDOUT_KEPT = 20_000
#: The most characters of stderr a result carries, taken from the end of what the code printed.
STDERR_KEPT = 8_000


class Tail(io.TextIOBase):
    """A text stream that keeps the end of what is written to it, discarding older text as it goes.

    It holds at least the last ``limit`` characters and never more than twice that, and ``getvalue`` returns the
    last ``limit``. Without it, code that prints millions of lines would hold all of them in memory until the run
    ends, for a result that carries only their end.
    """

    def __init__(self, limit):
        """Make an empty stream that keeps the last ``limit`` characters."""
        super().__init__()
        self.limit = limit
        self._parts = []
        self._size = 0

    def writable(self):
        """Return True: the stream takes writes."""
        return True

    def write(self, text):
        """Add ``text`` to the stream, dropping the oldest text once it holds more than twice ``limit``."""
        if not isinstance(text, str):
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        self._parts.append(text)
        self._size += len(text)
        if self._size > 2 * self.limit:
            kept = "".join(self._parts)[-self.limit:]
            self._parts, self._size = [kept], len(kept)
        return len(text)

    def getvalue(self):
        """Return the last ``limit`` characters written."""
        return "".join(self._parts)[-self.limit:]


def execute(source, filename, namespace, tool):
    """Run ``source`` in ``namespace``, and return the end of its stdout and stderr.

    Every exception the code raises, SystemExit and KeyboardInterrupt included, becomes a python_error
    refusal. The refusal carries the last 4,000 characters of the traceback, without this add-on's frames,
    and the last 8,000 characters of what the code printed to stdout until then.
    """
    stdout, stderr = Tail(STDOUT_KEPT), Tail(STDERR_KEPT)
    try:
        compiled = compile(source, filename, "exec")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(compiled, namespace)
    except BaseException as exc:  # noqa: BLE001 -- SystemExit included: the agent's code fails as a refusal
        lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        mine = [line for line in lines if not _ours(line)]
        said = str(exc)
        error = f"{type(exc).__name__}: {said}" if said else type(exc).__name__
        if isinstance(exc, SystemExit):
            # The agent's code never ends the Blender it runs in.
            error += f" ({tool} cannot exit Blender)"
        raise CommandError("python_error", error,
                           "The traceback names the line in your code.",
                           traceback="".join(mine)[-4000:], stdout=stdout.getvalue()[-8000:]) from None
    return stdout.getvalue(), stderr.getvalue()


def _result(stdout, stderr, namespace):
    """Return the result of a run: the end of its output, and the ``result`` name it set, if any."""
    out = {"stdout": stdout[-STDOUT_KEPT:], "stderr": stderr[-STDERR_KEPT:]}
    if "result" in namespace:
        out["result"] = jsonable(namespace["result"])
    return out


#: Matches agent code that pushes, undoes or redoes undo steps itself, which makes steps BlenderLens cannot name:
#: any mention of the undo_push, undo_history or undo_redo operators, or a call of ed.undo or ed.redo. A call of
#: their poll() undoes nothing, so it does not match.
UNDO_CODE = re.compile(r"\b(?:undo_push|undo_history|undo_redo)\b|\bed\s*\.\s*(?:undo|redo)\s*\(")
#: How many characters of a script's file are read when looking for UNDO_CODE.
SOURCE_READ = 1_000_000


def handles_undo(command, params):
    """Return whether the code a run_python or run_script call runs mentions Blender's undo operators.

    For run_script the script's file is read, up to SOURCE_READ characters; a file that cannot be read
    counts as not mentioning them.
    """
    source = params.get("code") if command == "run_python" else None
    if command == "run_script" and isinstance(params.get("path"), str):
        try:
            with open(params["path"], encoding="utf-8", errors="replace") as handle:
                source = handle.read(SOURCE_READ)
        except OSError:
            source = None
    return isinstance(source, str) and UNDO_CODE.search(source) is not None


def run_python(params):
    """Run Python code in the namespace kept between calls, and return its output and its ``result``."""
    _refuse_if_switched_off("run_python")
    code = require(params, "code")
    if not isinstance(code, str):
        raise CommandError("bad_argument", "'code' must be a string of Python.")
    if not NAMESPACE:
        NAMESPACE.update({"__name__": "__blenderlens__", **base_names()})
    NAMESPACE.pop("result", None)
    stdout, stderr = execute(code, "<run_python>", NAMESPACE, "run_python")
    update()
    return _result(stdout, stderr, NAMESPACE)


# ---------------------------------------------------------------------------
# Names that hold Blender data
# ---------------------------------------------------------------------------


def _holds_blender_data(value):
    """Return whether ``value`` itself is Blender data, or a vector or matrix bound to Blender data."""
    if isinstance(value, bpy.types.Context):
        return False  # bpy.context itself, which stays valid through every undo and every file load
    if isinstance(value, (bpy.types.bpy_struct, bpy.types.bpy_prop_collection, bpy.types.bpy_prop_array)):
        return True
    # A vector or matrix read from a property stays bound to the data it was read from.
    return isinstance(value, (mathutils.Vector, mathutils.Matrix, mathutils.Euler, mathutils.Quaternion,
                              mathutils.Color)) and value.owner is not None


#: How deep the search for Blender data goes into containers and objects' attributes.
STALE_DEPTH = 6
#: How many items of each container the search for Blender data reads.
STALE_ITEMS = 1_000
#: How many values the search for Blender data reads in all for one name in the namespace.
STALE_VALUES = 20_000

#: Types whose attributes are code rather than data a script kept, so their attributes are never searched.
_CODE = (type, types.ModuleType, types.FunctionType, types.BuiltinFunctionType, types.MethodType,
         types.CodeType, types.FrameType, types.GeneratorType)


def _reaches_blender_data(value, depth, budget):
    """Return whether ``value`` holds Blender data, either itself or anywhere inside it.

    Lists, tuples, sets, dicts and objects' attributes are searched, to STALE_DEPTH levels. ``budget`` is a
    one-item list holding how many more values may be read, which starts at STALE_VALUES.
    """
    budget[0] -= 1
    if budget[0] < 0:
        return False
    if _holds_blender_data(value):
        return True
    if depth >= STALE_DEPTH or isinstance(value, (str, bytes, int, float, complex, bool)) or value is None:
        return False
    if isinstance(value, dict):
        items = [*itertools.islice(value.keys(), STALE_ITEMS), *itertools.islice(value.values(), STALE_ITEMS)]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(itertools.islice(value, STALE_ITEMS))
    elif not isinstance(value, _CODE) and isinstance(getattr(value, "__dict__", None), dict):
        items = list(itertools.islice(vars(value).values(), STALE_ITEMS))
    else:
        return False
    return any(_reaches_blender_data(item, depth + 1, budget) for item in items)


def drop_stale():
    """Remove from run_python's namespace every name holding Blender data, and return those names, sorted.

    An undo replaces the file's data, so a reference kept from before the undo points at data that is gone:
    Blender raises ReferenceError for some, and others still point at freed memory. A name is removed when its
    value holds such data anywhere within reach: in lists, tuples, sets and dicts, and in the attributes of
    objects, nested to a bounded depth. Names starting with __ are kept. What is left (numbers, text, plain
    Python) is still valid.
    """
    stale = []
    for name, value in list(NAMESPACE.items()):
        if name.startswith("__"):
            continue
        try:
            if _reaches_blender_data(value, 0, [STALE_VALUES]):
                stale.append(name)
        except ReferenceError:
            stale.append(name)
    for name in stale:
        del NAMESPACE[name]
    return sorted(stale)


# ---------------------------------------------------------------------------
# run_script
# ---------------------------------------------------------------------------


def import_folders(path):
    """Return the folders a script's imports are found in.

    They are the script's own folder, then each folder above it up to the one holding a blenderlens.toml.
    With no such file above it, the script's own folder is returned alone.
    """
    folder = os.path.dirname(path)
    chain = [folder]
    here = folder
    while True:
        if os.path.isfile(os.path.join(here, PROJECT_MARKER)):
            return chain
        parent = os.path.dirname(here)
        if parent == here:
            return [folder]
        here = parent
        chain.append(here)


def _folder(path):
    """Return ``path`` as an absolute path, in lower case on Windows, so that folders can be compared."""
    return os.path.normcase(os.path.abspath(path))


@contextlib.contextmanager
def _importable(folders):
    """Put ``folders`` on sys.path for the run alone, and write no bytecode beside the scripts.

    sys.path is put back as it was afterwards, whatever the script did to it, so a script that adds a folder
    to it itself, such as a folder of shared helper modules, leaves nothing behind. The modules first
    imported during the run through the run's own folders (``folders``, and any the script added) are
    dropped from sys.modules, so the next run reads them again and sees any edit. Only those are dropped. A
    module imported before the run stays, Blender's and this add-on's among them, and so does one the run
    imported through any other folder on sys.path, even one inside the script's project.
    """
    saved = list(sys.path)
    existing = set(sys.modules)
    writes = sys.dont_write_bytecode
    sys.path[:0] = [f for f in folders if f not in sys.path]
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = writes
        own = {_folder(f) for f in folders}
        own |= {_folder(p) for p in sys.path if isinstance(p, str) and p and p not in saved}
        # Checked before sys.path is put back, because a namespace package (a folder without __init__.py)
        # computes its folders from sys.path when they are read.
        new = [name for name in list(sys.modules) if name not in existing]
        tops = {name.partition(".")[0] for name in new}
        ours = {top for top in tops if top in sys.modules and _found_through(sys.modules[top], own)}
        for name in new:
            if name.partition(".")[0] in ours:
                sys.modules.pop(name, None)
        sys.path[:] = saved


def _found_through(module, folders):
    """Return whether a top-level module was found through one of ``folders``.

    It was when its file, its package's __init__.py, or a folder of its package lies directly in one of them.
    """
    spec = getattr(module, "__spec__", None)
    places = []
    origin = getattr(spec, "origin", None) if spec is not None else getattr(module, "__file__", None)
    if origin and (spec is None or spec.has_location):
        place = os.path.dirname(origin)
        if os.path.splitext(os.path.basename(origin))[0] == "__init__":
            place = os.path.dirname(place)
        places.append(place)
    locations = getattr(spec, "submodule_search_locations", None) if spec is not None else None
    places += [os.path.dirname(location) for location in list(locations or [])]
    return any(_folder(place) in folders for place in places)


#: The most nodes a material's signature reads. The signature also holds the number of nodes, which stands for
#: any nodes past the limit.
SIGNATURE_NODES = 512
#: The most inputs of each node a material's signature reads.
SIGNATURE_INPUTS = 64


def _plain(value):
    """Return a custom property's value as plain Python, for comparing."""
    for method in ("to_dict", "to_list"):
        if hasattr(value, method):
            return getattr(value, method)()
    return value


def _crc(value):
    """Return a CRC of ``value``'s repr."""
    return zlib.crc32(repr(value).encode("utf-8", "replace"))


def properties_signature(block):
    """Return a CRC of a datablock's custom properties, names and values, or None when they cannot be read."""
    try:
        return _crc(sorted((key, _plain(block[key])) for key in block.keys()))
    except (TypeError, AttributeError):
        return None


def _rounded(value):
    """Return a socket's value rounded to 5 places, so float noise does not count as a change."""
    if isinstance(value, float):
        return round(value, 5)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    try:
        return tuple(round(v, 5) if isinstance(v, float) else v for v in value)
    except TypeError:
        return repr(value)


def _material_signature(material):
    """Return a CRC of what a material shows.

    It covers the viewport colour and settings, the custom properties, and the nodes with their inputs, images
    and links, reading at most SIGNATURE_NODES nodes and SIGNATURE_INPUTS inputs of each.
    """
    out = [tuple(round(c, 5) for c in material.diffuse_color), round(material.metallic, 5),
           round(material.roughness, 5), material.use_backface_culling, properties_signature(material)]
    tree = material.node_tree
    if tree is not None:
        out.append(len(tree.nodes))
        for node in list(tree.nodes)[:SIGNATURE_NODES]:
            image = getattr(node, "image", None)
            out.append((node.name, node.bl_idname, node.mute, image.name if image is not None else None,
                        [(s.identifier, s.is_linked, _rounded(getattr(s, "default_value", None)))
                         for s in list(node.inputs)[:SIGNATURE_INPUTS]]))
        out.append(sorted((link.from_node.name, link.from_socket.identifier, link.to_node.name,
                           link.to_socket.identifier) for link in tree.links))
    return _crc(out)


def _per_face(mesh, attribute, kind, length):
    """Return a CRC of one attribute of every face, read as an array of ``kind``."""
    values = array.array(kind, bytes(array.array(kind).itemsize * length))
    mesh.polygons.foreach_get(attribute, values)
    return zlib.crc32(values.tobytes())


def _signature(obj, materials):
    """Return the values whose change makes run_script report an object as changed.

    They cover what counts as a change to the object: its place, its parent, its data and what is on it, its
    materials as they look, its visibility and its custom properties. For a mesh they also include its
    counts, a CRC of its vertex positions, its UV maps and colour attributes, its smooth shading and face
    materials, and its shape keys' values.

    ``materials`` caches each material's signature by name, since one material may be on many objects.
    """
    slots = []
    for slot in obj.material_slots:
        material = slot.material
        if material is not None and material.name not in materials:
            materials[material.name] = _material_signature(material)
        slots.append((material.name, materials[material.name]) if material is not None else None)
    out = [obj.type, obj.parent.name if obj.parent else None, [round(v, 5) for row in obj.matrix_world for v in row],
           obj.data.name if obj.data else None,
           [(m.name, m.type, m.show_viewport, m.show_render) for m in getattr(obj, "modifiers", ())], slots,
           obj.hide_viewport, obj.hide_render, properties_signature(obj)]
    if obj.type == "MESH":
        mesh = obj.data
        coords = array.array("f", bytes(12 * len(mesh.vertices)))
        mesh.vertices.foreach_get("co", coords)
        faces = len(mesh.polygons)
        keys = mesh.shape_keys
        out += [len(mesh.vertices), len(mesh.edges), faces, zlib.crc32(coords.tobytes()),
                [layer.name for layer in mesh.uv_layers], [layer.name for layer in mesh.color_attributes],
                _per_face(mesh, "use_smooth", "b", faces), _per_face(mesh, "material_index", "i", faces),
                [(k.name, round(k.value, 5)) for k in keys.key_blocks] if keys is not None else None,
                properties_signature(mesh)]
    return out


def _snapshot():
    """Return every object's signature, by name."""
    materials = {}
    return {obj.name: _signature(obj, materials) for obj in bpy.data.objects}


def _changes(before, after):
    """Return the objects added, removed and changed between two snapshots, with their counts."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(name for name in set(after) & set(before) if after[name] != before[name])
    return {"added": added[:MAX_NAMES], "removed": removed[:MAX_NAMES], "changed": changed[:MAX_NAMES],
            "counts": {"added": len(added), "removed": len(removed), "changed": len(changed),
                       "objects": len(after)}}


def _script_path(params):
    """Return the ``path`` parameter, refusing one that is not an existing .py file."""
    path = require(params, "path")
    if not isinstance(path, str) or not path.lower().endswith(".py"):
        raise CommandError("bad_argument", f"'{path}' does not end in .py.", "Name a Python file.")
    if not os.path.isfile(path):
        raise CommandError("not_found", f"No file at {path}.",
                           "Check the path. A relative path is resolved against the MCP server's current directory.")
    return path


def run_script(params):
    """Run a .py file with ``args``, and return its output and the objects it added, removed or changed."""
    _refuse_if_switched_off("run_script")
    path = _script_path(params)
    args = params.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise CommandError("bad_argument", f"'args' must be a JSON object, got {args!r}.")
    fresh = boolean(params, "fresh", False)
    discard = boolean(params, "discard", False)
    try:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise CommandError("bad_argument", f"Cannot read {path}: {exc}.") from None

    # Imported here because commands imports this module, and commands owns the file's saved state.
    from . import commands
    from . import undo as undo_stack

    if fresh:
        commands._refuse_if_unsaved(discard, "starting the script from an empty file")
        commands.reset_to_empty()
        # The empty file is the undo step to return to, so one undo takes back all of the script's work.
        undo_stack.ensure_base()
    namespace = {"__name__": "__main__", "__file__": path, **base_names(), "args": json.loads(json.dumps(args))}
    before = _snapshot()
    with _importable(import_folders(path)):
        stdout, stderr = execute(source, path, namespace, "run_script")
    update()
    return {"path": path, "fresh": fresh, **_result(stdout, stderr, namespace), **_changes(before, _snapshot())}
