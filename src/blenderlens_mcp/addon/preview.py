"""Render a preview: frame some objects from chosen angles and write a PNG.

The scene is left exactly as it was found. The camera, lights, world and wire overlays used for the
render are temporary, every render setting that is changed is put back afterwards, and the "Render
Result" image that a render leaves behind is removed unless it existed before. A preview taken in a
Blender window running the add-on, while the user works in it, therefore changes nothing they can see.

Several views make one PNG, a contact sheet, with the views laid out left to right and top to bottom
in the order they were asked for.
"""

import math
import os
import shutil
import struct
import tempfile
import time
import zlib

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Euler, Matrix, Vector

from .common import (
    CommandError,
    Rollback,
    boolean,
    descendants,
    finite,
    get_object,
    rnd,
    update,
    vec,
    world_bounds,
)
from .paint import principled

#: The first eight bytes of every PNG file.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: The most pixels times samples, summed over all its views, that a preview may ask of an engine that
#: takes samples (Cycles or EEVEE): about 1024 x 1024 at 1024 samples, or 4096 x 4096 at 64. 4096 x 4096
#: at 4096 samples would be hours of CPU rendering, and the tool call waits for it the whole time.
MAX_PIXEL_SAMPLES = 1_100_000_000

#: The most pixels a contact sheet may span on each side, with all its views together.
MAX_SHEET_SIDE = 4096

#: Render settings that would keep the framed objects out of the picture, or the picture from the size
#: asked for. A sequencer strip or the compositor replaces the render, a render border crops it, and
#: stereo (multiview) writes two files beside the path instead of one. Each one that is on is turned off
#: for the preview and turned back on afterwards.
RENDER_SWITCHES = ("use_sequencer", "use_compositing", "use_border", "use_crop_to_border", "use_multiview")

#: Named views as (azimuth, elevation) in degrees. Azimuth turns from the front (-Y)
#: toward the right (+X), and elevation rises from the horizon.
VIEWS = {
    "front": (0.0, 0.0),
    "back": (180.0, 0.0),
    "right": (90.0, 0.0),
    "left": (270.0, 0.0),
    "top": (0.0, 90.0),
    "bottom": (0.0, -90.0),
    "iso": (45.0, math.degrees(math.atan(1 / math.sqrt(2)))),
    "three_quarter": (35.0, 30.0),
}

#: The engine names render_preview takes, each with the identifiers Blender may use for it. EEVEE's
#: identifier differs between Blender versions, so each is tried in turn.
ENGINES = {
    "cycles": ("CYCLES",),
    "eevee": ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"),
    "workbench": ("BLENDER_WORKBENCH",),
}

#: The looks a preview can take.
STYLES = ("shaded", "clay", "wireframe", "random")
#: The looks that are Workbench shading settings, so a preview in either renders with Workbench, whatever
#: engine was asked for.
WORKBENCH_STYLES = ("clay", "random")

#: The colour of the wire overlay that the wireframe style draws.
WIRE_COLOR = (0.01, 0.01, 0.012, 1.0)
#: The thickness of the wire overlay, in pixels of the picture.
WIRE_PIXELS = 1.5

#: The object types a render draws, which a preview frames.
RENDERABLE = {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME", "GREASEPENCIL"}


def available_engines():
    """Return the render engines this Blender has, by the names render_preview takes.

    Cycles is an add-on, so it is listed only when it is registered. The engines are found
    without setting ``scene.render.engine``, because setting it, even back to the value it had,
    marks the file as having unsaved changes.
    """
    found = []
    registered = {getattr(c, "bl_idname", "") for c in bpy.types.RenderEngine.__subclasses__()}
    if "CYCLES" in registered:
        found.append("cycles")
    found += ["eevee", "workbench"]  # EEVEE and Workbench are built into Blender itself.
    return found


def _set_engine(scene, name):
    """Set the scene's render engine to the one render_preview calls ``name``, and return Blender's identifier."""
    for identifier in ENGINES[name]:
        try:
            scene.render.engine = identifier
            return identifier
        except TypeError:
            continue
    raise CommandError("bad_argument", f"This Blender has no {name} render engine.",
                       "The engines this Blender has are: " + ", ".join(available_engines()) + ".")


def _number(params, key):
    """Return the number parameter ``key``, refusing NaN, the infinities and magnitudes past 1e9."""
    return finite(params[key], key)


def _png_path(path):
    """Return ``path`` when it names a .png file that holds a PNG or does not exist yet.

    A preview overwrites whatever is at its path, so a folder, or a file that is not a PNG whatever
    its name, is refused with a CommandError rather than replaced.
    """
    if not isinstance(path, str) or not path.lower().endswith(".png"):
        raise CommandError("bad_argument", f"'{path}' does not end in .png; previews are written only as PNG files.",
                           "Name the file *.png, or leave out path, and the MCP server writes a new file in its "
                           "render folder (BLENDERLENS_RENDER_DIR).")
    if os.path.isdir(path):
        raise CommandError("bad_argument", f"'{path}' is a folder, not a PNG file.", "Name a file *.png.")
    if os.path.exists(path):
        with open(path, "rb") as handle:
            head = handle.read(len(PNG_SIGNATURE))
        if head != PNG_SIGNATURE:
            raise CommandError("bad_argument", f"'{path}' exists and is not a PNG, so it is not overwritten.",
                               "Choose another path, or remove that file first.")
    return path


def _direction(azimuth, elevation):
    """Return the unit vector from the framed objects toward a view at ``azimuth`` and ``elevation`` degrees."""
    az, el = math.radians(azimuth), math.radians(elevation)
    return Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))


def _aim(obj, direction_from_target, azimuth):
    """Turn ``obj`` so its -Z axis points back along ``direction_from_target``, with the picture's up toward world Z.

    Looking straight down or straight up, world Z cannot be the picture's up, so the object is
    turned about world Z by ``azimuth`` instead, and the picture's up still turns with the azimuth.
    """
    if abs(direction_from_target.z) > 0.9999:
        flip = 0.0 if direction_from_target.z > 0 else math.pi
        obj.rotation_euler = Euler((flip, 0.0, math.radians(azimuth)), "XYZ")
    else:
        obj.rotation_euler = (-direction_from_target).to_track_quat("-Z", "Y").to_euler()


def _put_back_format(settings, kept):
    """Put back the render's output format, then its colour mode and colour depth.

    The colour depths on offer depend on the format, and a depth the current format does not offer
    is refused or clamped. If the depth were put back while the format is still PNG, an EXR's 32-bit
    depth would become 16, so the format goes back first. A value Blender refuses is counted in
    ``Rollback.failures``.
    """
    for attribute, value in zip(("file_format", "color_mode", "color_depth"), kept, strict=True):
        try:
            setattr(settings, attribute, value)
        except (TypeError, ValueError):
            Rollback.failures += 1


def _layer_holding(objects):
    """Return the view layer to render: the current one if it holds every framed object, else the first that does.

    By default Blender renders every view layer and writes only one of them to the PNG, not
    necessarily one that holds the objects, and a layer that excludes them would make a picture of
    nothing. Each layer other than the current one is updated before its objects are read. When no
    layer holds them all, the current one is returned.
    """
    current = bpy.context.view_layer
    for layer in [current, *[v for v in bpy.context.scene.view_layers if v != current]]:
        if layer != current:
            layer.update()
        if all(layer.objects.get(o.name) is not None for o in objects):
            return layer
    return current


def _with_children(names, key):
    """Return the objects in ``names``, each followed by every object below it in the hierarchy, without repeats.

    ``key`` is the parameter the names came from, used in error messages.
    """
    if not isinstance(names, list):
        raise CommandError("bad_argument", f"'{key}' must be a list of object names.")
    out = []
    for obj in [get_object(n, key) for n in names]:
        for candidate in [obj, *descendants(obj)]:
            if candidate not in out:
                out.append(candidate)
    return out


def _objects(params, key):
    """Return the objects named in the parameter ``key``, without their children, or [] when it is absent."""
    names = params.get(key)
    if names is None:
        return []
    if not isinstance(names, list):
        raise CommandError("bad_argument", f"'{key}' must be a list of object names.")
    return [get_object(n, key) for n in names]


def _views(params):
    """Return the views asked for, as a list of (name, azimuth, elevation) tuples.

    ``views`` names several views for a contact sheet, and cannot be combined with ``view``,
    ``azimuth`` or ``elevation``, which describe a single view. A single view defaults to
    three_quarter; ``azimuth`` and ``elevation`` override its angles, and the elevation is clamped
    to the range -90 to 90 degrees.
    """
    names = params.get("views")
    if names is not None:
        if not isinstance(names, list) or not 1 <= len(names) <= len(VIEWS):
            raise CommandError("bad_argument", f"'views' must list 1 to {len(VIEWS)} view names.")
        if params.get("view") is not None or params.get("azimuth") is not None or params.get("elevation") is not None:
            raise CommandError("bad_argument", "'views' cannot be combined with 'view', 'azimuth' or 'elevation'.",
                               "Give either views, or view with azimuth and elevation.")
        unknown = [n for n in names if n not in VIEWS]
        if unknown:
            raise CommandError("bad_argument", f"Unknown view '{unknown[0]}'.",
                               "The views are: " + ", ".join(VIEWS) + ".")
        return [(name, *VIEWS[name]) for name in names]
    view = params.get("view") or "three_quarter"
    if view not in VIEWS:
        raise CommandError("bad_argument", f"Unknown view '{view}'.", "The views are: " + ", ".join(VIEWS) + ".")
    azimuth, elevation = VIEWS[view]
    if params.get("azimuth") is not None:
        azimuth = _number(params, "azimuth")
    if params.get("elevation") is not None:
        elevation = max(-90.0, min(90.0, _number(params, "elevation")))
    return [(view, azimuth, elevation)]


def _sheet_layout(count):
    """Return the (columns, rows) of a contact sheet of ``count`` views.

    Up to three views sit in one row. More are laid out in a grid as near to a square as holds them all.
    """
    columns = count if count <= 3 else math.ceil(math.sqrt(count))
    return columns, math.ceil(count / columns)


def _corners(objects, depsgraph):
    """Return the world-space corners of the objects' bounding boxes after modifiers.

    An empty has no bounding box, so it contributes its location instead.
    """
    out = []
    for obj in objects:
        if obj.type == "EMPTY":
            out.append(obj.matrix_world.translation.copy())
            continue
        evaluated = obj.evaluated_get(depsgraph)
        out += [evaluated.matrix_world @ Vector(c) for c in evaluated.bound_box]
    return out


#: How many times a perspective camera is shifted sideways to centre what it frames. Near parts look
#: larger in perspective, so the middle of the framed box is not the middle of the picture.
CENTRING_PASSES = 4


def _view_tangents(data, aspect):
    """Return the tangents of half the horizontal and half the vertical field of view of the camera ``data``.

    With ``sensor_fit`` AUTO the sensor's width spans the picture's longer side, so that side's tangent
    is half the sensor width over the focal length, and the shorter side's is scaled down by ``aspect``.
    """
    longer = data.sensor_width / 2 / data.lens
    return (longer, longer / aspect) if aspect >= 1 else (longer * aspect, longer)


def _perspective_fit(corners, look, right, up, way, tangents, margin, radius):
    """Return the point to look at and the camera's distance from it that fit every corner in the picture.

    A corner that lies ``z`` toward the camera from the point looked at and ``x`` across the view fits
    when the camera is at least ``z + |x| * margin / tan`` away, where ``tan`` is the tangent of half
    the field of view on that axis. Both the horizontal and the vertical field of view are checked,
    and the distance is the largest any corner needs, keeping the camera at least 5% of ``radius`` in
    front of every corner. Each of the CENTRING_PASSES passes then moves the point looked at sideways
    by how far the corners' projections lean off centre, and the distance is fitted once more at the end.
    """
    tan_h, tan_v = tangents

    def fit(at):
        """Return the distance from ``at`` at which the camera fits every corner."""
        need = radius * 0.05
        for c in corners:
            offset = c - at
            z = offset.dot(way)
            need = max(need, z + radius * 0.05,
                       z + max(abs(offset.dot(right)) / tan_h, abs(offset.dot(up)) / tan_v) * margin)
        return need

    for _ in range(CENTRING_PASSES):
        distance = fit(look)
        depths = [distance - (c - look).dot(way) for c in corners]
        across = [(c - look).dot(right) / d for c, d in zip(corners, depths, strict=True)]
        upward = [(c - look).dot(up) / d for c, d in zip(corners, depths, strict=True)]
        look = look + right * (max(across) + min(across)) / 2 * distance \
            + up * (max(upward) + min(upward)) / 2 * distance
    return look, fit(look)


def _place_camera(camera, way, azimuth, framing, projection, aspect, margin, ortho_scale):
    """Place ``camera`` in the direction ``way`` from the framed corners, looking at them, and fit them in the picture.

    Return the point looked at and the camera's distance from it. Both projections fit the corners as
    the camera sees them, not a sphere around them, so a long thin part fills a wide or a tall picture.
    An orthographic camera projects the corners onto its own axes and sets its scale to fit that
    rectangle, unless ``ortho_scale`` fixes the scale instead, as a game's fixed camera does. A
    perspective camera fits every corner inside both the horizontal and the vertical field of view
    (see _perspective_fit), so each view has its own distance.
    """
    corners, center, radius = framing
    data = camera.data
    _aim(camera, way, azimuth)
    rotation = camera.rotation_euler.to_matrix()
    right, up = rotation.col[0], rotation.col[1]
    across = [(c - center).dot(right) for c in corners]
    upward = [(c - center).dot(up) for c in corners]
    look = center + right * (max(across) + min(across)) / 2 + up * (max(upward) + min(upward)) / 2
    if projection == "orthographic":
        data.type = "ORTHO"
        wide, high = max(across) - min(across), max(upward) - min(upward)
        # ortho_scale spans the picture's longer side, so the fit is measured along that side.
        fit = margin * (max(wide, high * aspect) if aspect >= 1 else max(wide / aspect, high))
        data.ortho_scale = ortho_scale if ortho_scale is not None else max(fit, 0.01)
        distance = radius * 4
    else:
        data.type = "PERSP"
        data.lens = 50.0
        data.sensor_fit = "AUTO"
        look, distance = _perspective_fit(corners, look, right, up, way, _view_tangents(data, aspect), margin,
                                          radius)
    location = look + way * distance
    camera.location = location
    # matrix_world is set as well, so a marker projected before the scene updates sees the camera where it is.
    camera.matrix_world = Matrix.Translation(location) @ camera.rotation_euler.to_matrix().to_4x4()
    return look, distance


def _clip(camera, way, distance, radius, shown_corners, look):
    """Set the camera's clip planes so they hold the framed objects and every object shown with them."""
    depths = [(look + way * distance - c).dot(way) for c in shown_corners] or [distance]
    near = min(min(depths) * 0.9, distance - radius * 2)
    camera.data.clip_start = max(near, distance / 1000, 0.001)
    camera.data.clip_end = max(max(depths) * 1.1, distance + radius * 4)


def _markers(scene, camera, objects, view, offset, width, height):
    """Return where each object's origin lands in the PNG, in pixels from the PNG's top left corner.

    ``offset`` is the top left corner of the view's cell on a contact sheet, and ``view``, when
    given, names the view on each entry. ``in_frame`` is false for an origin outside the picture or
    behind the camera.
    """
    out = []
    for obj in objects:
        point = world_to_camera_view(scene, camera, obj.matrix_world.translation)
        entry = {"object": obj.name, "x": rnd(offset[0] + point.x * width, 1),
                 "y": rnd(offset[1] + (1 - point.y) * height, 1),
                 "in_frame": bool(0 <= point.x <= 1 and 0 <= point.y <= 1 and point.z > 0)}
        if view is not None:
            entry["view"] = view
        out.append(entry)
    return out


def _metres_per_pixel(camera, projection, corners, look, way, distance, width, height):
    """Return how many metres one pixel of the picture spans where the framed objects come nearest the camera.

    An orthographic camera's scale spans the picture's longer side at any depth. A perspective
    picture spans more the deeper it looks, so it is measured at the depth of the nearest framed
    corner (at least 5% of the distance): the edges nearest the camera are the largest in the
    picture, and they are the ones measured.
    """
    if projection == "orthographic":
        return camera.data.ortho_scale / max(width, height)
    depth = max(min((look + way * distance - c).dot(way) for c in corners), distance * 0.05)
    return 2 * depth * _view_tangents(camera.data, width / height)[0] / width


def _wire_overlays(framed, depsgraph, temporary):
    """Lay a dark wire copy over each framed object for the wireframe style, and return the copies' modifiers.

    A render ignores an object's own display type, in Workbench too, so the wires are made as
    geometry. Each copy is a new mesh of the object after modifiers, with a Wireframe modifier,
    placed where the object is; the objects themselves are not touched. Every datablock made is
    added to ``temporary``, to be removed after the render. Each view sets the modifiers' thickness
    so the wires are the same number of pixels wide in its picture.
    """
    material = bpy.data.materials.new("BlenderLensPreviewWire")
    temporary.append((bpy.data.materials, material))
    material.diffuse_color = WIRE_COLOR  # Workbench draws a material's viewport colour, not its nodes.
    node = principled(material, create=True)
    if node is not None:
        node.inputs["Base Color"].default_value = WIRE_COLOR
        node.inputs["Roughness"].default_value = 1.0
    modifiers = []
    for obj in framed:
        try:
            mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph))
        except RuntimeError:
            continue  # The object yields no mesh, so it gets no wires.
        temporary.append((bpy.data.meshes, mesh))
        mesh.materials.clear()
        mesh.materials.append(material)
        mesh.polygons.foreach_set("material_index", [0] * len(mesh.polygons))
        wire = bpy.data.objects.new(f"BlenderLensPreviewWire{obj.name}", mesh)
        temporary.append((bpy.data.objects, wire))
        wire.matrix_world = obj.matrix_world.copy()
        bpy.context.scene.collection.objects.link(wire)
        modifier = wire.modifiers.new("Wire", "WIREFRAME")
        modifier.use_even_offset = True
        modifier.use_replace = True
        modifiers.append(modifier)
    return modifiers


def _style(scene, style, restore):
    """Set Workbench's shading for ``style``, recording each change in ``restore``.

    The clay style is a matcap with cavity shading in a single grey. The random style gives each
    object a colour of its own under studio light. Any other style rendered with Workbench shows the
    materials' viewport colours under studio light.
    """
    shading = scene.display.shading
    if style == "clay":
        restore.set(shading, "light", "MATCAP")
        restore.set(shading, "color_type", "SINGLE")
        restore.set(shading, "single_color", (0.8, 0.8, 0.8))
        restore.set(shading, "show_cavity", True)
        restore.set(shading, "cavity_type", "BOTH")
    else:
        restore.set(shading, "light", "STUDIO")
        restore.set(shading, "color_type", "RANDOM" if style == "random" else "MATERIAL")


def _png_bytes(pixels, alpha):
    """Return the bytes of a PNG file holding ``pixels``, an 8-bit RGBA array with the top row first.

    With ``alpha`` the PNG is RGBA (colour type 6). Without it the alpha channel is dropped and the
    PNG is RGB (colour type 2).
    """
    height, width = pixels.shape[:2]
    channels = 4 if alpha else 3
    raw = bytearray()
    for row in pixels[:, :, :channels].reshape(height, width * channels):
        raw += b"\x00" + row.tobytes()

    def chunk(kind, data):
        """Return one PNG chunk: its length, its kind, its data and their CRC."""
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    return PNG_SIGNATURE + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")


def _compose_sheet(files, columns, rows, width, height, path, alpha):
    """Lay the views' PNG files out on one contact sheet, left to right and top to bottom, and write it at ``path``.

    The sheet is written to a ``.part`` file beside ``path`` and then moved into place, so a failure
    partway never leaves a half-written PNG at ``path``.
    """
    # numpy ships with Blender, and only this function uses it: it moves millions of pixels, which
    # would take seconds with Python lists.
    import numpy

    sheet = numpy.zeros((rows * height, columns * width, 4), dtype=numpy.uint8)
    for index, file in enumerate(files):
        image = bpy.data.images.load(file, check_existing=False)
        try:
            pixels = numpy.empty(width * height * 4, dtype=numpy.float32)
            image.pixels.foreach_get(pixels)
        finally:
            bpy.data.images.remove(image)
        # Blender returns an 8-bit PNG's pixels as its bytes divided by 255, bottom row first, so rounding
        # them back gives exactly the bytes each view rendered.
        cell = numpy.flipud(numpy.rint(pixels.reshape(height, width, 4) * 255).astype(numpy.uint8))
        row, column = divmod(index, columns)
        sheet[row * height:(row + 1) * height, column * width:(column + 1) * width] = cell
    partial = path + ".part"
    with open(partial, "wb") as handle:
        handle.write(_png_bytes(sheet, alpha))
    os.replace(partial, path)


def render_preview(params):
    """Render the objects from one view or several into a PNG, leaving the scene as it was."""
    scene = bpy.context.scene
    path = params.get("path")
    if not path:
        raise CommandError("bad_argument", "'path' is required.")
    path = _png_path(path)
    update()

    names = params.get("objects")
    if names:
        targets = _with_children(names, "objects")
    else:
        targets = [o for o in scene.objects if o.type in RENDERABLE and not o.hide_render and o.visible_get()]
    framed = [o for o in targets if o.type in RENDERABLE]
    if not framed and names:
        raise CommandError("not_found", "There is nothing to render: none of the objects named in 'objects', or "
                                        "their children, draws geometry.",
                           "get_scene lists each object's type. Name a mesh, curve, surface, metaball or text object.")
    if not framed:
        raise CommandError("not_found", "There is nothing to render: the scene has no visible object that draws "
                                        "geometry and is enabled for rendering.",
                           "get_scene lists the objects. Create one, or name one in 'objects'.")
    context = _with_children(params["context"], "context") if params.get("context") is not None else []
    unseen = _objects(params, "hide_from_camera")
    marked = _objects(params, "markers")

    views = _views(params)
    resolution = params.get("resolution") or 768
    if isinstance(resolution, int) and not isinstance(resolution, bool):
        width = height = resolution
    elif isinstance(resolution, list) and len(resolution) == 2 and all(isinstance(r, int) for r in resolution):
        width, height = resolution
    else:
        raise CommandError("bad_argument", "'resolution' must be a whole number or [width, height].")
    if not (16 <= width <= 4096 and 16 <= height <= 4096):
        raise CommandError("bad_argument", "'resolution' must be between 16 and 4096 pixels on each side.")
    columns, rows = _sheet_layout(len(views))
    if columns * width > MAX_SHEET_SIDE or rows * height > MAX_SHEET_SIDE:
        raise CommandError("bad_argument", f"{len(views)} views of {width} x {height} make a sheet of "
                                           f"{columns * width} x {rows * height} pixels; a sheet is at most "
                                           f"{MAX_SHEET_SIDE} x {MAX_SHEET_SIDE}.",
                           "Lower 'resolution', or request fewer views.")

    style = str(params.get("style") or "shaded").lower()
    if style not in STYLES:
        raise CommandError("bad_argument", f"Unknown style '{style}'.", "The styles are: " + ", ".join(STYLES) + ".")
    engine = str(params.get("engine") or "cycles").lower()
    if engine == "auto":
        engine = "cycles"
    if engine not in ENGINES:
        raise CommandError("bad_argument", f"Unknown engine '{engine}'.",
                           "The engines are: auto, cycles, eevee and workbench.")
    if style in WORKBENCH_STYLES:
        engine = "workbench"
    samples = params.get("samples") or 32
    if not isinstance(samples, int) or isinstance(samples, bool) or not 1 <= samples <= 4096:
        raise CommandError("bad_argument", "'samples' must be a whole number from 1 to 4096.")
    cost = len(views) * width * height * samples
    if engine in ("cycles", "eevee") and cost > MAX_PIXEL_SAMPLES:
        many = f"{len(views)} views of " if len(views) > 1 else ""
        raise CommandError("bad_argument", f"{many}{width} x {height} pixels at {samples} samples is "
                                           f"{cost:,} pixel samples, more than the {MAX_PIXEL_SAMPLES:,} allowed.",
                           "Lower 'resolution' or 'samples'. The default, 768 pixels at 32 samples, is plenty to "
                           "check a model, and workbench takes no samples.")
    ortho_scale = None
    if params.get("ortho_scale") is not None:
        ortho_scale = _number(params, "ortho_scale")
        if not 0.001 <= ortho_scale <= 1e6:
            raise CommandError("bad_argument", "'ortho_scale' must be between 0.001 and 1e6 metres.")
    projection = str(params.get("projection") or ("orthographic" if ortho_scale else "perspective")).lower()
    if projection not in ("perspective", "orthographic"):
        raise CommandError("bad_argument", "'projection' must be perspective or orthographic.")
    if ortho_scale is not None and projection != "orthographic":
        raise CommandError("bad_argument", "'ortho_scale' is how many metres an orthographic camera sees along the "
                                           "picture's longer side, so it needs the orthographic projection.",
                           "Leave out projection, or make it orthographic.")
    lighting = str(params.get("lighting") or "studio").lower()
    if lighting not in ("studio", "scene"):
        raise CommandError("bad_argument", "'lighting' must be studio or scene.")
    transparent = boolean(params, "transparent", False)
    margin = _number(params, "margin") if params.get("margin") is not None else 1.1
    if not 0.5 <= margin <= 10:
        raise CommandError("bad_argument", "'margin' must be between 0.5 and 10.")

    layer = _layer_holding(framed)
    bounds = world_bounds(framed, layer.depsgraph)
    lo, hi = bounds
    center = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.01)
    framing = (_corners(framed, layer.depsgraph), center, radius)
    shown = list(dict.fromkeys([*targets, *context]))
    shown_corners = _corners([o for o in shown if o.type in RENDERABLE], layer.depsgraph)
    aspect = width / height

    restore = Rollback()
    temporary = []  # (collection, datablock) pairs to remove afterwards
    results_before = {i for i in bpy.data.images if i.type == "RENDER_RESULT"}
    # A contact sheet's views are rendered into a temporary folder, then laid out on the sheet.
    cells_folder = tempfile.mkdtemp(prefix="bl-preview-") if len(views) > 1 else None
    started = time.time()
    placed = []
    markers = []
    try:
        # Hide from the render everything renderable that is not to be shown, and, with studio lighting,
        # the scene's own lights.
        shown_set = set(shown)
        for obj in scene.objects:
            if obj in shown_set or obj.hide_render:
                continue
            if obj.type in RENDERABLE or (lighting == "studio" and obj.type == "LIGHT"):
                restore.set(obj, "hide_render", True)
        for obj in unseen:
            # Other engines only hide the object from the camera, so its shadows and reflections stay.
            # Workbench ignores ray visibility, so there the object is left out of the render.
            if engine == "workbench":
                restore.set(obj, "hide_render", True)
            elif obj.visible_camera:
                restore.set(obj, "visible_camera", False)
        wires = _wire_overlays(framed, layer.depsgraph, temporary) if style == "wireframe" else []

        camera_data = bpy.data.cameras.new("BlenderLensPreviewCamera")
        temporary.append((bpy.data.cameras, camera_data))
        camera = bpy.data.objects.new("BlenderLensPreviewCamera", camera_data)
        temporary.append((bpy.data.objects, camera))
        scene.collection.objects.link(camera)
        restore.set(scene, "camera", camera)

        if lighting == "studio":
            lights = []
            for label, d_az, el, strength in (("Key", -45.0, 50.0, 3.0), ("Fill", 70.0, 20.0, 1.0),
                                              ("Rim", 180.0, 40.0, 2.0)):
                light_data = bpy.data.lights.new(f"BlenderLensPreview{label}", "SUN")
                light_data.energy = strength
                light_data.angle = math.radians(10)
                temporary.append((bpy.data.lights, light_data))
                light = bpy.data.objects.new(f"BlenderLensPreview{label}", light_data)
                temporary.append((bpy.data.objects, light))
                scene.collection.objects.link(light)
                lights.append((light, d_az, el))
            world = bpy.data.worlds.new("BlenderLensPreviewWorld")
            temporary.append((bpy.data.worlds, world))
            world.color = (0.05, 0.05, 0.055)
            if world.node_tree is None and hasattr(world, "use_nodes"):
                world.use_nodes = True
            background = world.node_tree.nodes.get("Background") if world.node_tree else None
            if background is not None:
                background.inputs["Color"].default_value = (0.05, 0.05, 0.055, 1.0)
                background.inputs["Strength"].default_value = 1.0
            restore.set(scene, "world", world)

        render = scene.render
        identifier = _set_engine_restorable(scene, engine, restore)
        restore.set(render, "resolution_x", width)
        restore.set(render, "resolution_y", height)
        restore.set(render, "resolution_percentage", 100)
        restore.set(render, "pixel_aspect_x", 1.0)
        restore.set(render, "pixel_aspect_y", 1.0)
        restore.set(render, "film_transparent", transparent)
        restore.set(render, "use_file_extension", False)
        for switch in RENDER_SWITCHES:
            if getattr(render, switch):
                restore.set(render, switch, False)
        # Only the view layer holding the framed objects renders, so it is the one written to the PNG.
        restore.set(render, "use_single_layer", True)
        if not layer.use:
            restore.set(layer, "use", True)
        settings = render.image_settings
        kept = (settings.file_format, settings.color_mode, settings.color_depth)
        restore.add(lambda: _put_back_format(settings, kept))
        settings.file_format = "PNG"
        settings.color_mode = "RGBA" if transparent else "RGB"
        settings.color_depth = "8"
        if engine == "cycles":
            cycles = scene.cycles
            restore.set(cycles, "device", "CPU")
            restore.set(cycles, "samples", samples)
            restore.set(cycles, "use_adaptive_sampling", True)
            restore.set(cycles, "use_denoising", True)
        elif engine == "eevee":
            restore.set(scene.eevee, "taa_render_samples", samples)
        else:
            _style(scene, style, restore)
        restore.set(render, "filepath", path)

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        files = []
        for index, (name, azimuth, elevation) in enumerate(views):
            way = _direction(azimuth, elevation)
            look, distance = _place_camera(camera, way, azimuth, framing, projection, aspect, margin, ortho_scale)
            _clip(camera, way, distance, radius, shown_corners, look)
            thickness = _metres_per_pixel(camera, projection, framing[0], look, way, distance, width,
                                          height) * WIRE_PIXELS
            for modifier in wires:
                modifier.thickness = max(thickness, 1e-5)
            if lighting == "studio":
                for light, d_az, el in lights:
                    _aim(light, _direction(azimuth + d_az, el), azimuth + d_az)
            row, column = divmod(index, columns)
            sheet = len(views) > 1
            markers += _markers(scene, camera, marked, name if sheet else None, (column * width, row * height),
                                width, height)
            target = os.path.join(cells_folder, f"{index}.png") if sheet else path
            render.filepath = target
            bpy.ops.render.render(write_still=True, layer=layer.name)
            if not os.path.isfile(target):
                raise CommandError("blender_error", f"Blender finished the render but wrote no file at {target}.")
            files.append(target)
            placed.append({"name": name, "azimuth": rnd(azimuth, 3), "elevation": rnd(elevation, 3),
                           "camera": {"location": vec(look + way * distance), "looking_at": vec(look)},
                           "cell": [column * width, row * height, width, height]})
        if len(views) > 1:
            _compose_sheet(files, columns, rows, width, height, path, transparent)
    finally:
        restore.undo()
        for collection, block in reversed(temporary):
            try:
                collection.remove(block)
            except (ReferenceError, RuntimeError):
                pass
        for image in [i for i in bpy.data.images if i.type == "RENDER_RESULT" and i not in results_before]:
            bpy.data.images.remove(image)
        if cells_folder is not None:
            shutil.rmtree(cells_folder, ignore_errors=True)

    first = placed[0]
    out = {
        "path": path,
        "engine": engine,
        "engine_id": identifier,
        "seconds": rnd(time.time() - started, 2),
        "resolution": [columns * width, rows * height],
        "view": {"name": first["name"], "azimuth": first["azimuth"], "elevation": first["elevation"],
                 "projection": projection, "lighting": lighting, "style": style},
        "camera": first["camera"],
        "objects": [o.name for o in framed],
        "bounds": {"min": vec(lo), "max": vec(hi), "size": vec(hi - lo)},
    }
    if len(views) > 1:
        out["views"] = placed
    if context:
        out["context"] = [o.name for o in context]
    if marked:
        out["markers"] = markers
    return out


def _set_engine_restorable(scene, engine, restore):
    """Set the render engine as _set_engine does, recording in ``restore`` how to put the old one back."""
    previous = scene.render.engine
    identifier = _set_engine(scene, engine)
    restore.add(lambda: setattr(scene.render, "engine", previous))
    return identifier
