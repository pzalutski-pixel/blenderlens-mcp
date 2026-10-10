"""Tests for ``render_preview``, run against a real background Blender.

A test that checks the image reads the PNG that Blender wrote, not the tool's report of it. Others check that a
preview leaves the scene and the user's output settings as they were: a preview must change nothing the user can
see.
"""

import base64
from pathlib import Path

import pytest

from tests.conftest import png_info

pytestmark = pytest.mark.integration

#: Code that returns the parts of the scene a preview could disturb.
STATE = """
s = bpy.context.scene
result = {
    "objects": sorted(o.name for o in bpy.data.objects),
    "cameras": len(bpy.data.cameras), "lights": len(bpy.data.lights), "worlds": len(bpy.data.worlds),
    "camera": s.camera.name if s.camera else None, "world": s.world.name if s.world else None,
    "engine": s.render.engine, "x": s.render.resolution_x, "y": s.render.resolution_y,
    "path": s.render.filepath, "transparent": s.render.film_transparent,
    "hidden": sorted(o.name for o in bpy.data.objects if o.hide_render),
}
"""


@pytest.fixture
def scene(blender):
    """Return the Blender session holding a painted cabinet to preview, and a sphere 6 m away from it."""
    blender.ok("create_object", kind="cube", name="Cabinet", size=[1, 0.5, 2], location=[0, 0, 1])
    blender.ok("set_material", material="Cream paint", object="Cabinet", base_color="#e8dcc8", roughness=0.7)
    blender.ok("create_object", kind="uv_sphere", name="Elsewhere", location=[6, 0, 1])
    return blender


def test_the_default_preview_is_a_cycles_png_of_the_asked_size(scene):
    out = scene.ok("render_preview", objects=["Cabinet"], resolution=256)
    info = png_info(out["path"])
    assert (info["width"], info["height"]) == (256, 256)
    assert info["color_type"] == 2, "the preview is not RGB, though its background is not transparent"
    assert out["engine"] == "cycles"
    assert out["objects"] == ["Cabinet"]
    assert out["view"]["name"] == "three_quarter"
    assert Path(out["path"]).name.startswith("Cabinet-three_quarter-")


def test_a_preview_leaves_the_scene_as_it_was(scene, tmp_path):
    before = scene.ok("run_python", code=STATE)["result"]
    scene.ok("render_preview", objects=["Cabinet"], resolution=64, transparent=True, path=str(tmp_path / "a.png"))
    after = scene.ok("run_python", code=STATE)["result"]
    assert after == before


def test_named_views_and_orthographic_frame_the_object(scene, tmp_path):
    for view in ("front", "top", "iso"):
        out = scene.ok("render_preview", objects=["Cabinet"], view=view, projection="orthographic",
                       resolution=[160, 96], path=str(tmp_path / f"{view}.png"))
        info = png_info(out["path"])
        assert (info["width"], info["height"]) == (160, 96)
        assert out["view"]["projection"] == "orthographic"
    front = scene.ok("render_preview", objects=["Cabinet"], view="front", resolution=64, path=str(tmp_path / "f.png"))
    camera = front["camera"]["location"]
    assert camera[0] == pytest.approx(0, abs=1e-4) and camera[1] < -1, "the front view does not look from -Y"


def test_azimuth_and_elevation_override_the_view(scene, tmp_path):
    out = scene.ok("render_preview", objects=["Cabinet"], azimuth=90, elevation=0, resolution=64,
                   path=str(tmp_path / "side.png"))
    camera = out["camera"]["location"]
    assert camera[0] > 1 and abs(camera[1]) < 1e-3, "azimuth 90 does not look from +X"


def test_transparent_background_gives_rgba(scene, tmp_path):
    out = scene.ok("render_preview", objects=["Cabinet"], transparent=True, resolution=64,
                   path=str(tmp_path / "t.png"))
    assert png_info(out["path"])["color_type"] == 6


@pytest.mark.parametrize("engine", ["workbench", "eevee"])
def test_workbench_and_eevee_render_too(scene, tmp_path, engine):
    out = scene.ok("render_preview", objects=["Cabinet"], engine=engine, resolution=96, samples=8,
                   path=str(tmp_path / f"{engine}.png"), timeout=240)
    assert out["engine"] == engine
    assert png_info(out["path"])["width"] == 96


def test_without_objects_every_visible_mesh_is_framed(scene, tmp_path):
    out = scene.ok("render_preview", resolution=64, path=str(tmp_path / "all.png"))
    assert sorted(out["objects"]) == ["Cabinet", "Elsewhere"]
    assert out["bounds"]["max"][0] == pytest.approx(7.0, abs=1e-3)


def test_inline_returns_the_image_itself(scene, tmp_path):
    result = scene.call_raw("render_preview", {"objects": ["Cabinet"], "resolution": 48, "inline": True,
                                               "path": str(tmp_path / "i.png")})
    assert not result["isError"]
    image = next(block for block in result["content"] if block["type"] == "image")
    assert image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]) == (tmp_path / "i.png").read_bytes()
    assert "data" not in result["structuredContent"], "the image is not duplicated into the JSON"


def test_only_png_paths_are_written(scene, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("important", encoding="utf-8")
    payload = scene.refused("render_preview", "bad_argument", objects=["Cabinet"], resolution=32, path=str(victim))
    assert ".png" in payload["error"] + payload["hint"]
    assert victim.read_text(encoding="utf-8") == "important"
    scene.refused("render_preview", "bad_argument", objects=["Cabinet"], resolution=32, path=str(tmp_path / "noext"))
    assert not (tmp_path / "noext").exists()


def test_an_existing_file_that_is_not_a_png_is_never_overwritten(scene, tmp_path):
    impostor = tmp_path / "notes.png"
    impostor.write_bytes(b"not a picture at all")
    payload = scene.refused("render_preview", "bad_argument", objects=["Cabinet"], resolution=32, path=str(impostor))
    assert "PNG" in payload["error"]
    assert impostor.read_bytes() == b"not a picture at all"
    folder = tmp_path / "folder.png"
    folder.mkdir()
    scene.refused("render_preview", "bad_argument", objects=["Cabinet"], resolution=32, path=str(folder))


def test_an_existing_png_is_replaced(scene, tmp_path):
    path = tmp_path / "again.png"
    scene.ok("render_preview", objects=["Cabinet"], resolution=32, path=str(path))
    out = scene.ok("render_preview", objects=["Cabinet"], resolution=48, path=str(path.with_suffix(".PNG")))
    assert png_info(out["path"])["width"] == 48


@pytest.mark.parametrize(("file_format", "depth"), [("OPEN_EXR", "32"), ("DPX", "10"), ("JPEG2000", "12"),
                                                   ("TIFF", "16")])
def test_the_users_output_format_survives_a_preview(scene, tmp_path, file_format, depth):
    """The scene's output file format and bit depth are the same after a preview, whatever depths the format allows."""
    settings = "s = bpy.context.scene.render.image_settings\n"
    scene.ok("run_python", code=f"{settings}s.file_format = {file_format!r}\ns.color_depth = {depth!r}")
    scene.ok("render_preview", objects=["Cabinet"], resolution=32, engine="workbench", path=str(tmp_path / "f.png"))
    kept = scene.ok("run_python", code=f"{settings}result = [s.file_format, s.color_depth]")["result"]
    assert kept == [file_format, depth]


#: Code that returns the size and mean RGB of the PNG at PATH.
MEAN_COLOUR = """
image = bpy.data.images.load(PATH, check_existing=False)
pixels = list(image.pixels)
count = len(pixels) // 4
result = {"size": list(image.size), "rgb": [round(sum(pixels[i::4]) / count, 3) for i in range(3)]}
bpy.data.images.remove(image)
"""

#: Python, run in Blender, that changes a scene setting which could keep the object out of the preview, or keep the
#: preview from its requested size.
TRAPS = {
    "sequencer": """
editor = bpy.context.scene.sequence_editor_create()
strips = editor.strips if hasattr(editor, "strips") else editor.sequences
try:
    strip = strips.new_effect(name="Red", type="COLOR", channel=1, frame_start=1, length=100)
except TypeError:  # 4.2 takes frame_end
    strip = strips.new_effect(name="Red", type="COLOR", channel=1, frame_start=1, frame_end=101)
strip.color = (1, 0, 0)
""",
    "border": """
render = bpy.context.scene.render
render.use_border = True
render.use_crop_to_border = True
render.border_min_x, render.border_max_x, render.border_min_y, render.border_max_y = 0, 0.25, 0, 0.25
""",
    "stereo": "bpy.context.scene.render.use_multiview = True",
    "view_layer": """
scene = bpy.context.scene
stuff = bpy.data.collections.new("Stuff")
scene.collection.children.link(stuff)
for name in ("Cabinet", "Elsewhere"):
    stuff.objects.link(bpy.data.objects[name])
    scene.collection.objects.unlink(bpy.data.objects[name])
other = scene.view_layers.new("Other")
scene.view_layers.move(len(scene.view_layers) - 1, 0)
other.layer_collection.children["Stuff"].exclude = True
""",
    # The current view layer excludes the object and only another view layer holds it, so the preview must render
    # that other view layer.
    "only_another_view_layer": """
scene = bpy.context.scene
stuff = bpy.data.collections.new("Stuff")
scene.collection.children.link(stuff)
for name in ("Cabinet", "Elsewhere"):
    stuff.objects.link(bpy.data.objects[name])
    scene.collection.objects.unlink(bpy.data.objects[name])
scene.view_layers.new("Other")
bpy.context.view_layer.layer_collection.children["Stuff"].exclude = True
""",
}


@pytest.mark.parametrize("trap", list(TRAPS))
def test_a_preview_shows_the_object_at_its_size_whatever_the_scene_is_set_to(scene, tmp_path, trap):
    """The PNG has the requested size and shows the object, whatever the scene's settings.

    The settings changed are the sequencer, the render border, stereo output and the view layers.
    """
    common = {"objects": ["Cabinet"], "resolution": 32, "engine": "workbench", "lighting": "studio"}
    plain = str(tmp_path / "plain.png")
    scene.ok("render_preview", path=plain, **common)
    expected = scene.ok("run_python", code=f"PATH = {plain!r}\n{MEAN_COLOUR}")["result"]
    scene.ok("run_python", code=TRAPS[trap])
    trapped = str(tmp_path / "trapped.png")
    scene.ok("render_preview", path=trapped, **common)
    info = png_info(trapped)
    assert (info["width"], info["height"]) == (32, 32)
    got = scene.ok("run_python", code=f"PATH = {trapped!r}\n{MEAN_COLOUR}")["result"]
    assert got["rgb"] == pytest.approx(expected["rgb"], abs=0.02), \
        f"the preview changed with {trap}: {got} against {expected}"


#: Code that replaces bpy.ops.render with a stand-in that records each call in CALLS and raises.
STUB_RENDER = """
class _Stub:
    def render(self, **named):
        CALLS.append(named)
        raise RuntimeError("the test's stand-in for a render that would take hours")
CALLS = []
bpy.ops.render = _Stub()
"""


def test_a_preview_that_would_cost_too_much_is_refused_naming_the_limit(scene, tmp_path):
    """A preview whose pixels times samples exceed the limit is refused, and the error names the limit.

    Rendering 4096 x 4096 pixels at 4096 samples would take hours.
    """
    # Replace the render operator with a stub, so a render that got past the limit fails at once instead of
    # running for hours, and is counted.
    scene.ok("run_python", code=STUB_RENDER)
    try:
        payload = scene.refused("render_preview", "bad_argument", objects=["Cabinet"], resolution=4096, samples=4096,
                                path=str(tmp_path / "huge.png"))
        assert "1,100,000,000" in payload["error"], payload
        assert scene.ok("run_python", code="result = len(CALLS)")["result"] == 0, \
            "the render ran before the call was refused"
        payload = scene.refused("render_preview", "bad_argument", objects=["Cabinet"], resolution=[4096, 2048],
                                samples=200, engine="eevee", path=str(tmp_path / "huge.png"))
        assert "1,100,000,000" in payload["error"], payload
    finally:
        scene.ok("run_python", code="del bpy.ops.render")
    scene.ok("render_preview", objects=["Cabinet"], resolution=32, path=str(tmp_path / "small.png"))


def test_nothing_to_render_and_bad_arguments_are_refused(blender, tmp_path):
    blender.refused("render_preview", "not_found", resolution=32)
    blender.ok("create_object", kind="cube", name="C")
    blender.refused("render_preview", "bad_argument", view="sideways")
    blender.refused("render_preview", "bad_argument", resolution=8)
    blender.refused("render_preview", "bad_argument", engine="povray")
    blender.refused("render_preview", "not_found", objects=["Ghost"])
