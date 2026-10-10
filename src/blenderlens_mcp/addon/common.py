"""Helpers every command shares: the refusal error, parameter checks, lookups by name, and reporting.

Parameters are converted from JSON into Blender values, and Blender values back into JSON. The module
runs inside Blender. Every refusal a command makes is a ``CommandError`` with a kind the MCP server
knows; any other exception that escapes a command is reported as ``blender_error`` with the last
lines of its traceback.
"""

import difflib
import math
import re

import bpy
from mathutils import Color, Euler, Matrix, Quaternion, Vector

#: Directions a face can be selected by, in world space. Blender's front view looks
#: from -Y toward +Y, so the front of a model faces -Y.
DIRECTIONS = {
    "top": (0.0, 0.0, 1.0),
    "bottom": (0.0, 0.0, -1.0),
    "front": (0.0, -1.0, 0.0),
    "back": (0.0, 1.0, 0.0),
    "right": (1.0, 0.0, 0.0),
    "left": (-1.0, 0.0, 0.0),
}

#: The kinds of datablock a pointer setting may be given by name, mapped to the ``bpy.data`` collection
#: that holds them.
ID_COLLECTIONS = {
    "Object": "objects",
    "Collection": "collections",
    "Material": "materials",
    "Texture": "textures",
    "Image": "images",
    "NodeTree": "node_groups",
    "Curve": "curves",
}

#: Properties left out of the settings that are reported and may be set, since they say nothing about what
#: a modifier does.
SETTINGS_SKIP = {
    "rna_type", "name", "type", "is_override_data_editable", "persistent_uid",
    "is_active", "is_pinned", "show_expanded", "show_on_cage", "show_in_editmode",
    "use_apply_on_spline", "execution_time", "use_pin_to_last", "debug_options",
    "show_group_selector", "show_manage_panel", "open_output_attributes_panel",
    "open_bake_panel", "open_named_attributes_panel", "open_warnings_panel",
    "open_bake_data_blocks_panel", "bake_directory", "bake_target",
}


#: The hint for every refusal caused by edit mode.
EDIT_MODE_HINT = "Leave edit mode (Tab), then call again."
#: The hint for every refusal caused by a mode other than object mode and edit mode, such as sculpt or pose mode.
OTHER_MODE_HINT = "Return to object mode, then call again."

#: Each object type, as Blender names it, in plain words.
OBJECT_KINDS = {
    "MESH": "mesh", "CURVE": "curve", "SURFACE": "surface", "META": "metaball", "FONT": "text object",
    "EMPTY": "empty", "ARMATURE": "armature", "LATTICE": "lattice", "LIGHT": "light", "LIGHT_PROBE": "light probe",
    "CAMERA": "camera", "SPEAKER": "speaker", "GPENCIL": "grease pencil object",
    "GREASEPENCIL": "grease pencil object", "CURVES": "hair curves object", "POINTCLOUD": "point cloud",
    "VOLUME": "volume",
}

#: The largest magnitude a number parameter may have. Blender keeps coordinates, sizes and most
#: settings as 32-bit floats: 1e39 is finite to Python but infinite to Blender, and a cube that size
#: gets NaN vertices. A billion metres is far larger than any game asset needs.
MAX_MAGNITUDE = 1e9


class CommandError(Exception):
    """An error raised when a command refuses a call.

    It carries the kind the MCP server reports, a message saying what happened, and an optional hint saying
    what to do. Any further keyword arguments become extra fields of the failure response.
    """

    def __init__(self, kind, message, hint=None, **extra):
        """Keep the kind, the message, the hint and the extra fields of the failure response."""
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
        self.extra = extra


class Rollback:
    """A list of steps that put back what a command changed, run newest first when the command fails partway.

    Commands check every parameter before they change anything, so that a refused call changes nothing.
    A rollback covers what Blender itself refuses after that point, so such a call still leaves the file
    as it found it. Used as ``with Rollback() as undo:``, it runs the steps on any exception and re-raises
    the exception. An exception for which every step succeeded is marked ``blenderlens_rolled_back``, so
    the dispatcher can tell a clean refusal from one that may have left a change.
    """

    #: How many steps, counted over every rollback, could not be run. A command that leaves this count
    #: higher than it found it may have changed the file after all, so the dispatcher counts it as a change.
    failures = 0

    def __init__(self):
        """Start with no steps."""
        self.steps = []

    def add(self, step):
        """Remember a callable that reverses something just done."""
        self.steps.append(step)

    def set(self, owner, attribute, value):
        """Set an attribute, and remember its old value as a step."""
        previous = _kept(getattr(owner, attribute))
        setattr(owner, attribute, value)
        self.steps.append(lambda: setattr(owner, attribute, previous))

    def undo(self):
        """Run the steps, newest first, and bring the view layer up to date if any ran."""
        ran = bool(self.steps)
        while self.steps:
            step = self.steps.pop()
            try:
                step()
            except Exception:  # noqa: BLE001 -- put back as much as possible; the original failure is reported
                Rollback.failures += 1
        if ran:
            # Putting values back does not bring matrix_world or the evaluated meshes up to date. Left
            # stale, they would make the next command select faces by ``facing`` using the transform that
            # was just refused.
            try:
                update()
            except Exception:  # noqa: BLE001 -- the values are back; a failed update must not hide the refusal
                pass

    def __enter__(self):
        """Return the rollback, for the ``with`` block to record its steps in."""
        return self

    def __exit__(self, kind, exc, tb):
        """Run the steps when the block raised, mark the exception if every step succeeded, and let it propagate."""
        if kind is not None:
            failures = Rollback.failures
            self.undo()
            if Rollback.failures == failures and exc is not None:
                try:
                    exc.blenderlens_rolled_back = True
                except AttributeError:
                    pass
        return False


def _kept(value):
    """Return a copy of a property's value that does not change when the property does.

    Blender hands out vectors and arrays that stay bound to the object they came from.
    """
    if isinstance(value, (Vector, Color, Euler, Quaternion, Matrix)):
        return value.copy()
    if isinstance(value, (set, dict, list)):
        return value.copy()
    if type(value).__name__ == "bpy_prop_array":
        return tuple(_kept(v) for v in value)
    return value


def rnd(value, places=5):
    """Round a float for reporting, and return any other value unchanged.

    NaN and the infinities are not valid JSON, so they are returned as the strings "nan", "inf"
    and "-inf". A float that rounds to zero is returned as 0.0, never -0.0.
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
        rounded = round(value, places)
        return 0.0 if rounded == 0 else rounded
    return value


def vec(value):
    """Return a mathutils vector, or any sequence of numbers, as a list of rounded floats."""
    return [rnd(float(v)) for v in value]


def jsonable(value, depth=0):
    """Convert a Blender or Python value into something json.dumps accepts.

    The depth limit is deep enough for a custom property nested as deeply as set_object allows, and it
    bounds the recursion, since a value may contain itself. A value it cannot convert becomes its repr.
    """
    if depth > 16:
        return repr(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return rnd(value)
    if isinstance(value, (Vector, Color, Euler, Quaternion)):
        return vec(value)
    if isinstance(value, Matrix):
        return [vec(row) for row in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, depth + 1) for v in value]
    if isinstance(value, bpy.types.ID):
        return value.name
    if hasattr(value, "to_dict"):
        try:
            return jsonable(value.to_dict(), depth + 1)
        except Exception:  # noqa: BLE001 -- fall through to the sequence and repr forms
            pass
    if hasattr(value, "__len__") and hasattr(value, "__getitem__"):
        try:
            return [jsonable(v, depth + 1) for v in value]
        except Exception:  # noqa: BLE001 -- a value that only looks like a sequence falls through to its repr
            pass
    return repr(value)


# ---------------------------------------------------------------------------
# Wording
# ---------------------------------------------------------------------------


#: Starts of words that begin with the letter u said as "you", as in "a UV_PROJECT modifier", "a unit" or
#: "a user", which take "a" although u is a vowel.
SAID_AS_YOU = ("uv", "uni", "us")


def article(words):
    """Return ``words`` with "a" or "an" before it, chosen by how it starts: "a mesh", "an empty", "a UV map".

    A word starting with a vowel takes "an", except one starting as in SAID_AS_YOU.
    """
    start = words.lower()
    vowel = start[:1] in ("a", "e", "i", "o", "u") and not start.startswith(SAID_AS_YOU)
    return f"{'an' if vowel else 'a'} {words}"


def object_kind(obj):
    """Return what kind of object ``obj`` is, in plain words with "a" or "an": "a mesh", "an empty"."""
    return article(OBJECT_KINDS.get(obj.type, obj.type.replace("_", " ").lower()))


def type_words(identifier):
    """Return a Blender type name such as "NodeTree" as plain lower-case words, such as "node tree"."""
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", identifier).lower()
    return "node group" if words == "node tree" else words


def plural(count, word, words=None):
    """Return ``count`` with ``word``, or with its plural when the count is not 1: "1 face", "3 faces".

    ``words`` is the plural when adding an s is not right, as for "mesh".
    """
    return f"{count:,} {word if count == 1 else (words or word + 's')}"


def joined(items, last="and"):
    """Return the items as a list in a sentence: "A", "A and B", "A, B and C", with ``last`` before the last."""
    items = [str(item) for item in items]
    if len(items) < 2:
        return "".join(items)
    return f"{', '.join(items[:-1])} {last} {items[-1]}"


def quoted(names, last="and"):
    """Return the names in single quotes as a list in a sentence: "'A'", "'A' and 'B'", "'A', 'B' and 'C'"."""
    return joined([f"'{name}'" for name in names], last)


def did_you_mean(near):
    """Return "Did you mean 'A' or 'B'? " for names close to a typo, or "" when there are none."""
    return f"Did you mean {joined([repr(name) for name in near], 'or')}? " if near else ""


def mode_name(obj):
    """Return the mode ``obj`` is in, in plain words: "edit", "sculpt", "weight paint" and so on."""
    return obj.mode.replace("_", " ").lower()


def mode_hint(obj):
    """Return the hint for a refusal caused by the mode ``obj`` is in: leave edit mode, or return to object mode."""
    return EDIT_MODE_HINT if obj.mode == "EDIT" else OTHER_MODE_HINT


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def require(params, key):
    """Return the parameter ``key``, refusing the call when it is missing or null."""
    if key not in params or params[key] is None:
        raise CommandError("bad_argument", f"'{key}' is required.")
    return params[key]


def is_number(value):
    """Return whether ``value`` is an int or a float; true and false, which Python counts as ints, are not."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def finite(value, key, limit=MAX_MAGNITUDE):
    """Return a number as a float, refusing NaN, the infinities and magnitudes past ``limit``.

    JSON has no NaN, but a literal such as 1e999 parses to infinity, and Blender would store it.
    A finite 1e39 overflows Blender's 32-bit floats instead. With ``limit`` None, only finiteness
    is checked, for values Blender keeps as doubles, such as custom properties.
    """
    if not is_number(value):
        raise CommandError("bad_argument", f"'{key}' must be a number, got {value!r}.")
    try:
        number = float(value)
    except OverflowError:  # an integer literal too long for any float
        number = math.inf
    if not math.isfinite(number):
        raise CommandError("bad_argument", f"'{key}' must be a finite number, got {str(value)[:40]}.")
    if limit is not None and abs(number) > limit:
        raise CommandError("bad_argument", f"'{key}' must lie between -1e9 and 1e9, got {value!r}.",
                           "Give a value from -1e9 to 1e9, a billion metres either way, which is far more than any "
                           "asset needs. Blender keeps sizes and coordinates as 32-bit floats, which cannot hold "
                           "every whole metre beyond about 1.7e7, and become infinite beyond 3.4e38.")
    return number


def number(params, key, default=None, minimum=None, maximum=None, whole=False):
    """Return an optional numeric parameter, checked against its bounds.

    A missing or null parameter returns ``default``. With ``whole``, the value must be an integer and is
    returned as given; otherwise it is returned as a float.
    """
    value = params.get(key)
    if value is None:
        return default
    if whole and (not isinstance(value, int) or isinstance(value, bool)):
        raise CommandError("bad_argument", f"'{key}' must be a whole number, got {value!r}.")
    checked = finite(value, key)
    if minimum is not None and checked < minimum:
        raise CommandError("bad_argument", f"'{key}' must be at least {minimum}, got {value}.")
    if maximum is not None and checked > maximum:
        raise CommandError("bad_argument", f"'{key}' must be at most {maximum}, got {value}.")
    return value if whole else checked


def boolean(params, key, default=None):
    """Return an optional true/false parameter; any other value is refused rather than guessed at."""
    value = params.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise CommandError("bad_argument", f"'{key}' must be true or false, got {value!r}.")
    return value


def text(params, key, default=None):
    """Return an optional non-empty string parameter; an empty string counts as missing."""
    value = params.get(key)
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise CommandError("bad_argument", f"'{key}' must be text, got {value!r}.")
    return value


def triple(value, key, default=None, uniform=True):
    """Return a 3-vector from a list of three numbers, or from one number when ``uniform``.

    A null component keeps the default's component, so ``[2, null, null]`` changes X only.
    """
    if value is None:
        return default
    if is_number(value):
        if not uniform:
            raise CommandError("bad_argument", f"'{key}' must be a list of three numbers.")
        return (finite(value, key),) * 3
    if isinstance(value, (list, tuple)) and len(value) == 3:
        out = []
        for i, component in enumerate(value):
            if component is None:
                if default is None:
                    raise CommandError("bad_argument", f"'{key}' has a null component, but there is no current "
                                                       "value for null to keep.")
                out.append(float(default[i]))
            elif is_number(component):
                out.append(finite(component, key))
            else:
                raise CommandError("bad_argument", f"'{key}' must hold numbers, got {component!r}.")
        return tuple(out)
    raise CommandError("bad_argument", f"'{key}' must be a number or a list of three numbers, got {value!r}.")


def direction(value, key="facing"):
    """Return a unit vector in world space from a direction name or a 3-vector."""
    if isinstance(value, str):
        named = DIRECTIONS.get(value.lower())
        if named is None:
            raise CommandError("bad_argument", f"'{key}' must be one of {', '.join(DIRECTIONS)} or [x, y, z].")
        return Vector(named)
    v = Vector(triple(value, key, uniform=False))
    if v.length == 0:
        raise CommandError("bad_argument", f"'{key}' must not be the zero vector.")
    return v.normalized()


def srgb_to_linear(c):
    """Return the linear value of the sRGB colour component ``c``, both from 0 to 1."""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def color(value, key):
    """Return RGBA from [r, g, b] or [r, g, b, a] in linear values, or from '#RRGGBB[AA]' in sRGB.

    Lists are read as Blender stores colours and as glTF exports them: linear. A hex string is
    read the way Blender's own hex field reads it: as sRGB, with its alpha left unconverted.
    """
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) not in (6, 8):
            raise CommandError("bad_argument", f"'{key}' as hex must be #RRGGBB or #RRGGBBAA.")
        try:
            raw = [int(text[i:i + 2], 16) / 255.0 for i in range(0, len(text), 2)]
        except ValueError:
            raise CommandError("bad_argument", f"'{key}' is not a hex colour: {value!r}.") from None
        rgb = [srgb_to_linear(c) for c in raw[:3]]
        return (*rgb, raw[3] if len(raw) == 4 else 1.0)
    if isinstance(value, (list, tuple)) and len(value) in (3, 4):
        if not all(is_number(c) for c in value):
            raise CommandError("bad_argument", f"'{key}' must hold numbers.")
        rgba = [finite(c, key) for c in value]
        return tuple(rgba if len(rgba) == 4 else [*rgba, 1.0])
    raise CommandError("bad_argument", f"'{key}' must be [r, g, b], [r, g, b, a], '#RRGGBB' or '#RRGGBBAA', "
                                       f"got {value!r}.")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def closest(name, names):
    """Return the names most like ``name``, nearest first, as a typo's likely meaning; empty when none is close."""
    return difflib.get_close_matches(name, list(names), n=3) if isinstance(name, str) else []


def names_hint(collection, what, wanted=None):
    """Return a hint for a not_found refusal: the names nearest ``wanted`` first, then up to 40 names there are.

    ``what`` names the kind of datablock in the plural, in plain words, such as "objects" or "node groups".
    """
    names = [item.name for item in collection]
    if not names:
        return f"There are no {what} in this file."
    shown = ", ".join(names[:40])
    more = f" and {len(names) - 40} more" if len(names) > 40 else ""
    return did_you_mean(closest(wanted, names)) + f"{what.capitalize()} in this file: {shown}{more}."


def get_object(name, key="object"):
    """Return the object named ``name``, refusing the call when there is none; ``key`` names the parameter."""
    if not isinstance(name, str) or not name:
        raise CommandError("bad_argument", f"'{key}' must be an object name.")
    obj = bpy.data.objects.get(name)
    if obj is None:
        raise CommandError("not_found", f"No object named '{name}'.", names_hint(bpy.data.objects, "objects", name))
    return obj


def get_mesh_object(name, key="object"):
    """Return the mesh object named ``name``, refusing the call when there is none or it is not a mesh."""
    obj = get_object(name, key)
    if obj.type != "MESH":
        raise CommandError("bad_argument", f"'{name}' is {object_kind(obj)}, not a mesh.",
                           "Only mesh objects have faces to edit or select.")
    return obj


#: The hint for a collection that exists but is not in the scene being worked on.
OUTSIDE_SCENE_HINT = ("Name a collection in this scene, or link this one into it: set_collection with 'parent' "
                      "puts it under the parent collection named.")


def get_collection(name, create=False, parent=None, undo=None):
    """Return a collection by name, or the scene's own collection when no name is given.

    With ``create``, the collection is one that objects are about to be put in, so it must be in this
    scene. A missing one is made under ``parent`` (which is made too if missing) or under the scene's
    own collection. An existing one that this scene does not show is linked under ``parent``, or
    refused when no parent is named, since objects moved into it would leave the scene. Everything
    made or linked is recorded in ``undo``.
    """
    if not name:
        return bpy.context.scene.collection
    for given in (name, parent):
        if given is not None and not isinstance(given, str):
            raise CommandError("bad_argument", f"A collection name must be text, got {given!r}.")
    scene = bpy.context.scene
    found = bpy.data.collections.get(name)
    if found is None and name == scene.collection.name:
        return scene.collection
    if found is None and not create:
        raise CommandError("not_found", f"No collection named '{name}'.",
                           names_hint(bpy.data.collections, "collections", name))
    if not create or (found is not None and in_scene(found)):
        return found
    if parent == name:
        raise CommandError("bad_argument", f"Collection '{name}' cannot be its own parent.",
                           "Name another collection as 'parent', or leave it out for the scene's own.")
    if found is None:
        found = bpy.data.collections.new(name)
        made = found
        if undo is not None:
            undo.add(lambda: bpy.data.collections.remove(made))
    elif not parent:
        raise CommandError("bad_argument", f"Collection '{name}' is not in scene '{scene.name}', so objects put "
                                           "there would leave the scene.", OUTSIDE_SCENE_HINT)
    host = get_collection(parent, create=True, undo=undo) if parent else scene.collection
    if host == found or host in found.children_recursive:
        raise CommandError("bad_argument", f"Collection '{parent}' is inside '{name}'; that would be a loop.")
    host.children.link(found)
    if undo is not None:
        undo.add(lambda: host.children.unlink(found) if found.name in host.children else None)
    return found


def in_scene(collection, scene=None):
    """Return whether the scene shows the collection: it is the scene's own collection, or inside it."""
    scene = scene or bpy.context.scene
    return collection == scene.collection or collection in scene.collection.children_recursive


def active_collection():
    """Return the collection new objects go into: the active one in the view layer, else the scene's own."""
    layer = bpy.context.view_layer.active_layer_collection
    return layer.collection if layer is not None else bpy.context.scene.collection


def descendants(obj):
    """Return every object below ``obj`` in the hierarchy, each child followed by its own descendants."""
    out = []
    for child in obj.children:
        out.append(child)
        out.extend(descendants(child))
    return out


def update():
    """Bring matrix_world, evaluated geometry and the view layer's object list up to date after a change."""
    bpy.context.view_layer.update()


def select_only(objects):
    """Make these objects the selection, with the first one active. Hidden objects are skipped.

    Each object is selected through select_set directly. The view layer's object list lags
    behind newly linked objects until it is updated, so a loop over it, or a membership test
    against it, would miss an object made a moment ago.

    An active object in edit, sculpt or paint mode keeps the active slot, and objects outside
    object mode keep their selection. If they were taken away, the user's Tab would act on the
    new object and leave the user's own object stuck in edit mode.
    """
    update()
    layer = bpy.context.view_layer
    for obj in layer.objects:
        if obj.select_get() and obj.mode == "OBJECT":
            obj.select_set(False)
    for obj in objects:
        try:
            obj.select_set(True)
        except RuntimeError:
            pass  # not in this view layer, because its collections are excluded
    active = layer.objects.active
    if objects and (active is None or active.mode == "OBJECT"):
        try:
            layer.objects.active = objects[0]
        except (RuntimeError, ValueError, TypeError):
            pass


def sync_edit_mode():
    """Copy every mesh being edited back into its data, so a read sees the edit.

    In edit mode Blender keeps the edit in a separate BMesh and writes it into the mesh only when
    edit mode ends; until then the mesh data still holds the shape from before the edit.
    """
    for obj in bpy.data.objects:
        if obj.mode == "EDIT":
            try:
                obj.update_from_editmode()
            except (RuntimeError, ReferenceError):
                pass


def refuse_outside_object_mode(objects, action):
    """Refuse when a target, or the active object, is in edit mode or any other mode but object mode.

    ``action`` completes the refusal's message, saying what cannot be done. Where it says "it" or "its",
    it means the object in the wrong mode, which may be the active object rather than a target. The active
    object counts too: Blender's apply operators fail in any mode but object mode, and the glTF exporter
    switches the active object to object mode and never back.
    """
    busy = [o for o in objects if o.mode != "OBJECT"]
    active = bpy.context.view_layer.objects.active
    which = ""
    if not busy and active is not None and active.mode != "OBJECT":
        busy, which = [active], ", the active object,"
    if busy:
        obj = busy[0]
        raise CommandError("bad_argument", f"'{obj.name}'{which} is in {mode_name(obj)} mode, so {action}.",
                           mode_hint(obj))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def mesh_counts(mesh):
    """Return the vertex, edge, face and triangle counts of ``mesh``'s data."""
    return {
        "vertices": len(mesh.vertices),
        "edges": len(mesh.edges),
        "faces": len(mesh.polygons),
        "triangles": sum(len(p.vertices) - 2 for p in mesh.polygons),
    }


def evaluated_counts(obj):
    """Return the mesh counts after modifiers, which is what export and render see, or None without a mesh."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    try:
        mesh = evaluated.to_mesh()
    except RuntimeError:
        return None
    try:
        return mesh_counts(mesh) if mesh is not None else None
    finally:
        evaluated.to_mesh_clear()


def world_bounds(objects, depsgraph=None):
    """Return the axis-aligned world bounds of these objects' evaluated geometry as (min, max), or None.

    The geometry is evaluated in ``depsgraph``'s view layer. An empty counts as its origin alone. None is
    returned when there are no objects.
    """
    depsgraph = depsgraph or bpy.context.evaluated_depsgraph_get()
    lo = Vector((math.inf,) * 3)
    hi = Vector((-math.inf,) * 3)
    found = False
    for obj in objects:
        evaluated = obj.evaluated_get(depsgraph)
        corners = [evaluated.matrix_world @ Vector(c) for c in evaluated.bound_box]
        if obj.type == "EMPTY":
            corners = [obj.matrix_world.translation.copy()]
        for corner in corners:
            found = True
            lo = Vector(map(min, lo, corner))
            hi = Vector(map(max, hi, corner))
    if not found:
        return None
    return lo, hi


def rotation_degrees(obj):
    """Return the object's rotation as XYZ Euler angles in degrees, whatever its rotation mode."""
    if obj.rotation_mode in ("QUATERNION", "AXIS_ANGLE"):
        euler = obj.matrix_basis.to_euler("XYZ")
    else:
        euler = obj.rotation_euler
    return [rnd(math.degrees(a), 4) for a in euler]


def object_summary(obj):
    """Return the short description of an object that get_scene lists and the build tools return."""
    out = {
        "name": obj.name,
        "type": obj.type,
        "parent": obj.parent.name if obj.parent else None,
        "location": vec(obj.location),
        "rotation": rotation_degrees(obj),
        "scale": vec(obj.scale),
        "dimensions": vec(obj.dimensions),
        "collections": [c.name for c in obj.users_collection],
    }
    if obj.type == "MESH":
        out["mesh"] = mesh_counts(obj.data)
        out["materials"] = [s.material.name if s.material else None for s in obj.material_slots]
        out["modifiers"] = [m.name for m in obj.modifiers]
    return out


def rna_settings(struct):
    """Return every readable setting of a struct, by its Python name.

    A pointer to a datablock is reported as the datablock's name. Pointers to other structs, and
    collections, are left out.
    """
    out = {}
    for prop in struct.bl_rna.properties:
        key = prop.identifier
        if key in SETTINGS_SKIP or prop.type == "COLLECTION":
            continue
        try:
            value = getattr(struct, key)
        except Exception:  # noqa: BLE001 -- some properties raise in some states
            continue
        if prop.type == "POINTER":
            if value is None:
                out[key] = None
            elif isinstance(value, bpy.types.ID):
                out[key] = value.name
            continue
        out[key] = jsonable(value)
    return out


def settable(struct):
    """Return the sorted names of the settings of ``struct`` that resolve_settings accepts."""
    return sorted(
        p.identifier for p in struct.bl_rna.properties
        if p.identifier not in SETTINGS_SKIP and p.type != "COLLECTION"
        and (not p.is_readonly or p.type == "POINTER")
        and (p.type != "POINTER" or getattr(p.fixed_type, "identifier", "") in ID_COLLECTIONS)
    )


def resolve_settings(struct, settings, what):
    """Check settings by their Python names and resolve them, changing nothing.

    Names of objects and other datablocks become the datablocks, enum values match case-insensitively,
    and numbers must be finite. Returns ``[(name, value)]`` ready to set. A setting the struct does
    not have is refused as ``bad_argument``, with a hint naming the settings it accepts.
    """
    if not settings:
        return []
    if not isinstance(settings, dict):
        raise CommandError("bad_argument", "'settings' must be a JSON object that maps property names to values.")
    props = struct.bl_rna.properties
    resolved = []
    for key, value in settings.items():
        prop = props.get(key)
        if prop is None or key in SETTINGS_SKIP or prop.type == "COLLECTION":
            raise CommandError("bad_argument", f"{what} has no setting '{key}'.",
                               "The settings it takes: " + ", ".join(settable(struct)) + ".")
        if prop.type == "POINTER":
            target = getattr(prop.fixed_type, "identifier", "")
            where = ID_COLLECTIONS.get(target)
            kind = type_words(target) or "struct"
            if where is None:
                raise CommandError("bad_argument", f"{what}.{key} refers to {article(kind)}, which cannot be set "
                                                   "by name.")
            if value is not None:
                if not isinstance(value, str):
                    raise CommandError("bad_argument", f"{what}.{key} takes the name of {article(kind)}, "
                                                       f"got {value!r}.")
                found = getattr(bpy.data, where).get(value)
                if found is None:
                    raise CommandError("not_found", f"There is no {kind} named '{value}' for {what}.{key}.",
                                       names_hint(getattr(bpy.data, where), kind + "s", value))
                value = found
        elif prop.type == "ENUM":
            items = [e.identifier for e in prop.enum_items]
            if prop.is_enum_flag:
                values = value if isinstance(value, (list, tuple, set)) else [value]
                value = {_match_enum(v, items, what, key) for v in values}
            else:
                value = _match_enum(value, items, what, key)
        else:
            for item in value if isinstance(value, (list, tuple)) else [value]:
                if is_number(item):
                    finite(item, f"{what}.{key}")
            if getattr(prop, "array_length", 0) and is_number(value):
                value = [value] * prop.array_length
        resolved.append((key, value))
    return resolved


def apply_settings(struct, settings, what, undo=None):
    """Set properties by their Python names: all of them, or none.

    Every setting is checked first (see ``resolve_settings``). A value Blender still refuses is
    refused as ``bad_argument``, and the settings already made in this call are put back. With
    ``undo``, they are also recorded there, for a caller that changes more afterwards. Returns the
    settings set, as ``[(name, value)]``.
    """
    resolved = resolve_settings(struct, settings, what)
    with Rollback() as mine:
        for key, value in resolved:
            try:
                mine.set(struct, key, value)
            except (TypeError, ValueError, AttributeError, OverflowError) as exc:
                raise CommandError("bad_argument", f"Blender refused {value!r} for {what}.{key}: {exc}") from None
            # Some pointers accept only some datablocks and silently ignore the rest: a boolean modifier's
            # object stays None when given an empty. The value is read back, so such a setting is refused
            # rather than silently ignored.
            if isinstance(value, bpy.types.ID) and getattr(struct, key) != value:
                kind = OBJECT_KINDS.get(getattr(value, "type", ""), type_words(type(value).__name__))
                raise CommandError("bad_argument", f"Blender would not take '{value.name}' ({article(kind)}) as "
                                                   f"{what}.{key}.",
                                   "This setting takes only some kinds of object: a boolean modifier's object, for "
                                   "one, must be a mesh. get_scene lists each object's type.")
    if undo is not None:
        undo.steps.extend(mine.steps)
    return resolved


def _match_enum(value, items, what, key):
    """Return the enum item that ``value`` names, matched case-insensitively, refusing a value that names none."""
    if not isinstance(value, str):
        raise CommandError("bad_argument", f"{what}.{key} takes one of {', '.join(items)}, got {value!r}.")
    if not items:
        return value  # a dynamic enum: Blender validates it on assignment
    for item in items:
        if item.lower() == value.lower():
            return item
    raise CommandError("bad_argument", f"{what}.{key} takes one of {', '.join(items)}, got '{value}'.")
