"""Tests for the sheets, context objects, styles, markers, cutaways and fixed cameras of ``render_preview``.

They run against a real background Blender. The tests that check the picture read the PNG that Blender wrote
and count its pixels in Blender itself, rather than trusting the tool's report of it. Workbench is used wherever
the look does not matter, since it renders in a moment and gives the same picture every time.
"""

import pytest

from tests.conftest import png_info
from tests.test_render import STATE, STUB_RENDER

pytestmark = pytest.mark.integration

#: Code that returns a PNG's size, the mean RGB of a region of it, and the share of the region's pixels that differ from
#: the background colour by more than 0.03 in any channel. ``BOX`` is the region as (x0, y0, x1, y1) in pixels from the
#: top left, or None for the whole picture, and the background is the colour of the top left pixel.
REGION = """
image = bpy.data.images.load(PATH, check_existing=False)
w, h = image.size
values = list(image.pixels)
bpy.data.images.remove(image)
x0, y0, x1, y1 = BOX if BOX else (0, 0, w, h)
def at(x, y):
    i = ((h - 1 - y) * w + x) * 4
    return values[i:i + 3]
background = at(0, 0)
seen = [at(x, y) for y in range(y0, y1) for x in range(x0, x1)]
result = {"size": [w, h], "rgb": [round(sum(p[c] for p in seen) / len(seen), 4) for c in range(3)],
          "covered": round(sum(1 for p in seen if max(abs(p[c] - background[c]) for c in range(3)) > 0.03)
                           / len(seen), 4)}
"""


def region(server, path, box=None) -> dict:
    """Return what REGION reports for the PNG at ``path``, or for the ``box`` of it."""
    return server.ok("run_python", code=f"PATH = {str(path)!r}\nBOX = {box!r}\n{REGION}")["result"]


@pytest.fixture
def scene(blender):
    """Return the Blender session holding a terracotta box named Body, standing on a large pale floor."""
    blender.ok("create_object", kind="cube", name="Body", size=[2, 1.5, 1.2], location=[0, 0, 0.6])
    blender.ok("set_material", material="Terracotta", object="Body", base_color="#c8643c")
    blender.ok("create_object", kind="plane", name="Floor", size=[12, 12, 0])
    blender.ok("set_material", material="Ground", object="Floor", base_color="#dddddd")
    return blender


@pytest.mark.opengl
def test_two_views_make_one_png_of_twice_the_width(scene, tmp_path):
    out = scene.ok("render_preview", objects=["Body"], views=["front", "right"], engine="workbench", resolution=32,
                   path=str(tmp_path / "sheet.png"))
    info = png_info(out["path"])
    assert (info["width"], info["height"]) == (64, 32)
    assert info["color_type"] == 2
    assert out["resolution"] == [64, 32]
    assert [(v["name"], v["cell"]) for v in out["views"]] == [("front", [0, 0, 32, 32]), ("right", [32, 0, 32, 32])]
    # Each cell matches the same view rendered as a preview of its own.
    for view, box in (("front", [0, 0, 32, 32]), ("right", [32, 0, 64, 32])):
        alone = scene.ok("render_preview", objects=["Body"], view=view, engine="workbench", resolution=32,
                         path=str(tmp_path / f"{view}.png"))
        assert region(scene, out["path"], box)["rgb"] == pytest.approx(region(scene, alone["path"])["rgb"], abs=0.002)


@pytest.mark.opengl
def test_four_views_make_a_square_sheet_and_transparency_is_kept(scene, tmp_path):
    out = scene.ok("render_preview", objects=["Body"], views=["front", "right", "top", "three_quarter"],
                   engine="workbench", resolution=[40, 24], transparent=True, path=str(tmp_path / "four.png"))
    info = png_info(out["path"])
    assert (info["width"], info["height"]) == (80, 48)
    assert info["color_type"] == 6, "the sheet is not RGBA"
    assert out["views"][3]["cell"] == [40, 24, 40, 24]


@pytest.mark.opengl
def test_a_sheet_leaves_the_scene_as_it_was(scene, tmp_path):
    before = scene.ok("run_python", code=STATE)["result"]
    images = "result = sorted((i.name, i.type) for i in bpy.data.images)"
    images_before = scene.ok("run_python", code=images)["result"]
    scene.ok("render_preview", objects=["Body"], views=["front", "top"], style="wireframe", context=["Floor"],
             markers=["Body"], hide_from_camera=["Floor"], engine="workbench", resolution=24,
             path=str(tmp_path / "s.png"))
    assert scene.ok("run_python", code=STATE)["result"] == before
    assert scene.ok("run_python", code=images)["result"] == images_before, \
        "a Render Result or a view's image was left behind"
    meshes = scene.ok("run_python", code="result = sorted(m.name for m in bpy.data.meshes)")["result"]
    assert meshes == ["Body", "Floor"], "the wire overlays were left behind"


@pytest.mark.opengl
def test_a_preview_leaves_no_render_result_behind(scene, tmp_path):
    scene.ok("render_preview", objects=["Body"], engine="workbench", resolution=16, path=str(tmp_path / "p.png"))
    left = scene.ok("run_python", code="result = [i.name for i in bpy.data.images if i.type == 'RENDER_RESULT']")
    assert left["result"] == []


@pytest.mark.opengl
def test_context_objects_show_without_changing_the_framing(scene, tmp_path):
    common = {"objects": ["Body"], "engine": "workbench", "resolution": 48, "view": "three_quarter"}
    alone = scene.ok("render_preview", path=str(tmp_path / "alone.png"), **common)
    with_floor = scene.ok("render_preview", path=str(tmp_path / "floor.png"), context=["Floor"], **common)
    assert with_floor["camera"] == alone["camera"]
    assert with_floor["bounds"] == alone["bounds"]
    assert with_floor["objects"] == ["Body"] and with_floor["context"] == ["Floor"]
    # The floor fills the lower part of the picture, which holds only background without it.
    lower = [0, 40, 48, 48]
    assert region(scene, with_floor["path"], lower)["covered"] > 0.9
    assert region(scene, alone["path"], lower)["covered"] < 0.1


@pytest.mark.opengl
def test_a_marker_at_the_centre_projects_to_the_centre_pixel(scene, tmp_path):
    scene.ok("create_object", kind="empty", name="Right", location=[1, 0, 0.6])
    out = scene.ok("render_preview", objects=["Body"], markers=["Body", "Right"], view="front", engine="workbench",
                   resolution=[64, 48], path=str(tmp_path / "m.png"))
    body, right = out["markers"]
    assert (body["object"], body["x"], body["y"], body["in_frame"]) == ("Body", 32.0, 24.0, True)
    assert right["x"] > 32 and right["y"] == pytest.approx(24.0, abs=0.05), "+X is not to the right from the front"
    sheet = scene.ok("render_preview", objects=["Body"], markers=["Body"], views=["front", "top"], engine="workbench",
                     resolution=[64, 48], path=str(tmp_path / "ms.png"))
    assert [(m["view"], m["x"], m["y"]) for m in sheet["markers"]] == [("front", 32.0, 24.0), ("top", 96.0, 24.0)]


@pytest.mark.opengl
def test_a_fixed_ortho_scale_keeps_metres_to_pixels(scene, tmp_path):
    scene.ok("create_object", kind="empty", name="L", location=[-1, 0, 0.6])
    scene.ok("create_object", kind="empty", name="R", location=[1, 0, 0.6])
    for scale, pixels in ((4.0, 32.0), (8.0, 16.0)):
        out = scene.ok("render_preview", objects=["Body"], markers=["L", "R"], view="front", ortho_scale=scale,
                       engine="workbench", resolution=64, path=str(tmp_path / f"o{scale}.png"))
        left, right = out["markers"]
        assert out["view"]["projection"] == "orthographic"
        # The picture is 64 px across and shows ortho_scale metres, so markers 2 m apart are 128 / scale px apart.
        assert right["x"] - left["x"] == pytest.approx(pixels, abs=0.05), scale
    scene.refused("render_preview", "bad_argument", objects=["Body"], ortho_scale=4, projection="perspective",
                  engine="workbench", resolution=16, path=str(tmp_path / "x.png"))


@pytest.mark.opengl
def test_orthographic_framing_fits_what_the_camera_sees_not_a_sphere(blender, tmp_path):
    """An orthographic view frames what the camera sees, not a sphere around the object.

    A 10 m pole along Y, seen end-on from the front, is a small square, and it fills most of the picture.
    """
    blender.ok("create_object", kind="cube", name="Pole", size=[0.2, 10, 0.2])
    out = blender.ok("render_preview", objects=["Pole"], view="front", projection="orthographic", engine="workbench",
                     resolution=32, path=str(tmp_path / "pole.png"))
    assert region(blender, out["path"])["covered"] > 0.6


#: Code that returns the share of a region's width and of its height that the drawn pixels of a transparent PNG span.
#: ``BOX`` gives the region as it does for ``REGION``.
SPAN = """
image = bpy.data.images.load(PATH, check_existing=False)
w, h = image.size
alpha = list(image.pixels)[3::4]
bpy.data.images.remove(image)
x0, y0, x1, y1 = BOX if BOX else (0, 0, w, h)
drawn = [(x, y) for y in range(y0, y1) for x in range(x0, x1) if alpha[(h - 1 - y) * w + x] > 0.5]
xs, ys = [p[0] for p in drawn], [p[1] for p in drawn]
result = [round((max(xs) - min(xs) + 1) / (x1 - x0), 3), round((max(ys) - min(ys) + 1) / (y1 - y0), 3)] if drawn \
    else [0, 0]
"""


def span(server, path, box=None) -> list:
    """Return what SPAN reports for the transparent PNG at ``path``, or for the ``box`` of it."""
    return server.ok("run_python", code=f"PATH = {str(path)!r}\nBOX = {box!r}\n{SPAN}")["result"]


@pytest.mark.parametrize(("size", "resolution", "axis"), [([2, 0.2, 0.2], [384, 160], 0),
                                                          ([0.2, 0.2, 2], [160, 384], 1)])
@pytest.mark.opengl
def test_a_perspective_view_fills_a_wide_or_a_tall_picture(blender, tmp_path, size, resolution, axis):
    """A long bar in a wide picture, and a column in a tall one, fill most of the picture along their length.

    This holds for a single view and for each cell of a sheet.
    """
    blender.ok("create_object", kind="cube", name="Bar", size=size)
    out = blender.ok("render_preview", objects=["Bar"], view="front", engine="workbench", transparent=True,
                     resolution=resolution, path=str(tmp_path / "one.png"))
    assert span(blender, out["path"])[axis] >= 0.7, "the subject does not fill most of the limiting side"
    sheet = blender.ok("render_preview", objects=["Bar"], views=["front", "three_quarter"], engine="workbench",
                       transparent=True, resolution=resolution, path=str(tmp_path / "sheet.png"))
    for view in sheet["views"]:
        x, y, w, h = view["cell"]
        assert span(blender, sheet["path"], [x, y, x + w, y + h])[axis] >= 0.7, view["name"]


def test_hidden_from_camera_an_object_still_casts_its_shadow(scene, tmp_path):
    """An object in ``hide_from_camera`` is not drawn, but it still casts its shadow.

    Seen from above and framed on the body, the floor under the hidden body is darker than with the body left
    out of the render.
    """
    common = {"objects": ["Body"], "context": ["Floor"], "engine": "cycles", "samples": 4, "resolution": 32,
              "view": "top"}
    cut = scene.ok("render_preview", hide_from_camera=["Body"], path=str(tmp_path / "cut.png"), **common)
    shown = scene.ok("render_preview", path=str(tmp_path / "shown.png"), **common)
    assert scene.ok("run_python", code="result = bpy.data.objects['Body'].visible_camera")["result"] is True
    scene.ok("set_object", object="Body", hide_render=True)
    bare = scene.ok("render_preview", path=str(tmp_path / "bare.png"), **common)
    assert cut["camera"] == bare["camera"] == shown["camera"]
    shadowed, plain, body = (region(scene, r["path"])["rgb"] for r in (cut, bare, shown))
    assert sum(shadowed) < sum(plain) - 0.1, "the body's shadow does not darken the floor"
    assert shadowed != pytest.approx(body, abs=0.02), "the body itself is in the picture"


@pytest.mark.opengl
def test_clay_and_random_render_with_workbench_and_wireframe_draws_edges(scene, tmp_path):
    common = {"objects": ["Body"], "resolution": 48, "view": "front"}
    clay = scene.ok("render_preview", style="clay", path=str(tmp_path / "clay.png"), **common)
    assert clay["engine"] == "workbench" and clay["view"]["style"] == "clay"
    assert scene.ok("render_preview", style="random", path=str(tmp_path / "r.png"), **common)["engine"] == "workbench"
    shaded = scene.ok("render_preview", engine="workbench", path=str(tmp_path / "shaded.png"), **common)
    wire = scene.ok("render_preview", engine="workbench", style="wireframe", path=str(tmp_path / "wire.png"),
                    **common)
    assert sum(region(scene, wire["path"])["rgb"]) < sum(region(scene, shaded["path"])["rgb"]) - 0.01, \
        "the wireframe render is not darker than the shaded one"
    assert region(scene, clay["path"])["rgb"] != pytest.approx(region(scene, shaded["path"])["rgb"], abs=0.01)


def test_views_with_a_single_view_and_unknown_context_or_marker_objects_are_refused(scene, tmp_path):
    for extra in ({"view": "front"}, {"azimuth": 10}):
        scene.refused("render_preview", "bad_argument", objects=["Body"], views=["front", "top"], resolution=16,
                      engine="workbench", path=str(tmp_path / "x.png"), **extra)
    scene.refused("render_preview", "not_found", objects=["Body"], context=["Ghost"], resolution=16,
                  engine="workbench", path=str(tmp_path / "x.png"))
    scene.refused("render_preview", "not_found", objects=["Body"], markers=["Ghost"], resolution=16,
                  engine="workbench", path=str(tmp_path / "x.png"))


def test_a_sheet_that_would_cost_too_much_is_refused_naming_the_limit(scene, tmp_path):
    """A sheet that would take too much work or be too large is refused, and the error names the limit.

    Each view multiplies the work, so the limits are on views x pixels x samples, and on the sheet's own size.
    """
    # Replace the render operator with a stub, so a render that got past the limits fails at once and is counted.
    scene.ok("run_python", code=STUB_RENDER)
    try:
        payload = scene.refused("render_preview", "bad_argument", objects=["Body"], resolution=1024, samples=300,
                                views=["front", "back", "left", "right"], path=str(tmp_path / "huge.png"))
        assert "1,100,000,000" in payload["error"], payload
        payload = scene.refused("render_preview", "bad_argument", objects=["Body"], resolution=2048,
                                engine="workbench", views=["front", "back", "left", "right", "top"],
                                path=str(tmp_path / "huge.png"))
        assert "4096 x 4096" in payload["error"], payload
        assert scene.ok("run_python", code="result = len(CALLS)")["result"] == 0, \
            "the render ran before the call was refused"
    finally:
        scene.ok("run_python", code="del bpy.ops.render")


def test_a_sheet_wider_or_taller_than_4096_pixels_is_refused(scene, tmp_path):
    """A sheet wider or taller than 4096 pixels is refused, even when its total number of pixels is allowed.

    Two views of 4096 x 16 make a sheet 8192 pixels across.
    """
    scene.ok("run_python", code=STUB_RENDER)
    try:
        for resolution, views in (([4096, 16], ["front", "right"]), ([16, 4096], ["front", "right", "top", "back"])):
            payload = scene.refused("render_preview", "bad_argument", objects=["Body"], resolution=resolution,
                                    engine="workbench", views=views, path=str(tmp_path / "long.png"))
            assert "4096" in payload["error"], payload
        assert scene.ok("run_python", code="result = len(CALLS)")["result"] == 0, \
            "the render ran before the call was refused"
    finally:
        scene.ok("run_python", code="del bpy.ops.render")
    assert not (tmp_path / "long.png").exists()


#: Code that returns the share of the picture the wire overlay darkens: the pixels that are darker in the wireframe
#: render than in the shaded one.
WIRED = """
def brightness(path):
    image = bpy.data.images.load(path, check_existing=False)
    values = list(image.pixels)
    bpy.data.images.remove(image)
    return [sum(values[i:i + 3]) / 3 for i in range(0, len(values), 4)]
wire, shaded = brightness(WIRE), brightness(SHADED)
result = round(sum(1 for w, s in zip(wire, shaded, strict=True) if s - w > 0.05) / len(wire), 4)
"""


@pytest.mark.parametrize("projection", ["orthographic", "perspective"])
@pytest.mark.opengl
def test_wire_lines_are_thin_whatever_the_projection(blender, tmp_path, projection):
    """The wire overlay stays about 1.5 pixels wide in both projections.

    In an orthographic view the line width is set from what the camera sees, so the end of a long pole that
    fills the picture still shows its face inside its edges.
    """
    blender.ok("create_object", kind="cylinder", name="Pole", radius=0.5, depth=10, segments=16,
               rotation=[90, 0, 0])
    paths = {}
    for style in ("wireframe", "shaded"):
        paths[style] = blender.ok("render_preview", objects=["Pole"], view="front", projection=projection,
                                  style=style, engine="workbench", resolution=96,
                                  path=str(tmp_path / f"{style}.png"))["path"]
    share = blender.ok("run_python", code=f"WIRE = {paths['wireframe']!r}\nSHADED = {paths['shaded']!r}\n"
                                          + WIRED)["result"]
    # The rim of a cap that fills the picture is about 300 pixels around and 1.5 pixels wide, about 5 % of 96 x 96.
    assert 0.002 < share < 0.12, share
