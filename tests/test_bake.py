"""Tests for ``bake_texture``, run against a real Blender.

Cycles bakes on the CPU into a PNG of the requested size, the material is wired to the baked image, and the
scene's render settings, selection and active object are restored afterwards. Image sizes and sample counts are
kept small, because a bake's memory and time grow with both.
"""

import pytest

from tests.conftest import png_info
from tests.test_modelling import glb
from tests.test_motion import saved_scene
from tests.test_refusals_change_nothing import refused_unchanged

pytestmark = pytest.mark.integration


@pytest.fixture
def scene(blender, tmp_path):
    """Provide the server with the saved scene that ``saved_scene`` builds."""
    return saved_scene(blender, tmp_path)


#: Code that returns the scene settings, the selection, the objects hidden from rendering and the images, which a bake
#: must leave as they were.
STATE = """
scene = bpy.context.scene
layer = bpy.context.view_layer
result = {"engine": scene.render.engine, "samples": scene.cycles.samples, "device": scene.cycles.device,
          "bake_margin": scene.render.bake.margin, "selected": sorted(o.name for o in layer.objects if o.select_get()),
          "active": layer.objects.active.name if layer.objects.active else None,
          "hide_render": sorted(o.name for o in scene.objects if o.hide_render),
          "images": sorted(i.name for i in bpy.data.images)}
"""

#: Code that returns the size and source of the image ``NAME``, and its red value under the Crate's bottom face and
#: under its top face.
PIXELS = """
image = bpy.data.images[NAME]
mesh = bpy.data.objects["Crate"].data
uv = mesh.uv_layers.active.data
width, height = image.size
values = list(image.pixels)

def under(face):
    u = sum(uv[i].uv.x for i in face.loop_indices) / face.loop_total
    v = sum(uv[i].uv.y for i in face.loop_indices) / face.loop_total
    x, y = min(int(u * width), width - 1), min(int(v * height), height - 1)
    return values[(y * width + x) * 4]

result = {"size": [width, height], "source": image.source,
          "bottom": under(next(f for f in mesh.polygons if f.normal.z < -0.9)),
          "top": under(next(f for f in mesh.polygons if f.normal.z > 0.9))}
"""


#: Code that lists every material's nodes and links, so a test can check that a refused bake leaves the shaders exactly
#: as they were.
NODES = """
result = {m.name: [sorted(n.name for n in m.node_tree.nodes),
                   sorted([l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier]
                          for l in m.node_tree.links)] if m.node_tree else None
          for m in bpy.data.materials}
"""


def bake_refused_unchanged(server, kind, /, **arguments) -> dict:
    """Check that ``bake_texture`` is refused with ``kind`` and changes nothing, and return the error payload.

    On top of the scene comparison that ``refused_unchanged`` makes, every material's nodes and links are compared
    before and after the call.
    """
    nodes = server.ok("run_python", code=NODES)["result"]
    payload = refused_unchanged(server, "bake_texture", kind, **arguments)
    assert server.ok("run_python", code=NODES)["result"] == nodes, "the refused bake changed a material's nodes"
    return payload


def cube_on_a_plane(server):
    """Build a painted, unwrapped 1 m cube named Crate, standing on a 4 m plane named Floor."""
    server.ok("create_object", kind="plane", name="Floor", size=4)
    server.ok("create_object", kind="cube", name="Crate", size=1, location=[0, 0, 0.5])
    server.ok("set_material", material="Paint", object="Crate", base_color=[0.8, 0.2, 0.1])
    server.ok("unwrap_uv", object="Crate", method="smart")


def test_an_ao_bake_of_a_cube_on_a_plane_writes_a_png_of_its_size_and_drives_the_base_colour(blender, tmp_path):
    cube_on_a_plane(blender)
    path = tmp_path / "crate_ao.png"
    out = blender.ok("bake_texture", object="Crate", type="ao", size=64, samples=4, path=str(path))
    assert png_info(path)["width"] == 64 and png_info(path)["height"] == 64
    assert out["size"] == [64, 64] and out["path"] == str(path)
    assert out["image_driven"] == {"Paint": ["base_color"]}
    paint = next(m for m in blender.ok("list_materials")["materials"] if m["name"] == "Paint")
    assert paint["base_color"] == "linked"
    pixels = blender.ok("run_python", code=f"NAME = {out['image']!r}\n" + PIXELS)["result"]
    assert pixels["size"] == [64, 64] and pixels["source"] == "FILE"
    assert pixels["bottom"] < 0.2 and pixels["top"] > 0.8, "the face on the floor is not dark, or the top is not light"
    # In the exported file, the baked occlusion is the base colour texture and the paint colour is its factor.
    blender.ok("export_glb", path=str(tmp_path / "crate.glb"), objects=["Crate"])
    document, _ = glb(tmp_path / "crate.glb")
    material = document["materials"][0]["pbrMetallicRoughness"]
    assert "baseColorTexture" in material
    assert [round(c, 3) for c in material["baseColorFactor"][:3]] == [0.8, 0.2, 0.1]


def test_baking_again_to_one_path_keeps_one_image_and_shows_the_new_bake(blender, tmp_path):
    cube_on_a_plane(blender)
    path = str(tmp_path / "crate_ao.png")
    first = blender.ok("bake_texture", object="Crate", type="ao", size=32, samples=2, path=path)
    blender.ok("set_transform", object="Crate", location=[50, 0, 0.5])
    second = blender.ok("bake_texture", object="Crate", type="ao", size=32, samples=2, path=path)
    assert second["image"] == first["image"]
    images = blender.ok("run_python", code="result = [i.name for i in bpy.data.images]")["result"]
    assert images == [first["image"]]
    moved = blender.ok("run_python", code=f"NAME = {first['image']!r}\n" + PIXELS)["result"]
    assert moved["bottom"] > 0.8, "the image does not show the second bake, made where nothing occludes the crate"


def test_a_bake_puts_back_the_render_settings_the_selection_and_the_active_object(blender, tmp_path):
    cube_on_a_plane(blender)
    blender.ok("run_python", code="""
scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.cycles.samples = 7
bpy.data.objects["Floor"].hide_render = True
for obj in bpy.context.view_layer.objects:
    obj.select_set(obj.name == "Floor")
bpy.context.view_layer.objects.active = bpy.data.objects["Floor"]
""")
    before = blender.ok("run_python", code=STATE)["result"]
    blender.ok("bake_texture", object="Crate", type="diffuse", size=32, samples=1, path=str(tmp_path / "d.png"),
               use_in_material=False)
    after = blender.ok("run_python", code=STATE)["result"]
    assert after == before, "the bake changed the settings, the selection or the active object, or left an image"


def test_a_normal_bake_from_a_high_poly_source_feeds_the_normal_map(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Low", size=1)
    blender.ok("set_material", material="Skin", object="Low")
    blender.ok("create_object", kind="uv_sphere", name="High", radius=0.7, segments=24, rings=12)
    blender.ok("unwrap_uv", object="Low", method="smart")
    path = tmp_path / "low_normal.png"
    out = blender.ok("bake_texture", object="Low", type="normal", from_objects=["High"], size=64, samples=1,
                     path=str(path))
    assert out["from_objects"] == ["High"] and out["image_driven"] == {"Skin": ["normal"]}
    assert png_info(path)["width"] == 64
    skin = next(m for m in blender.ok("list_materials")["materials"] if m["name"] == "Skin")
    assert skin["images"]["normal"] == str(path)


def test_an_object_without_a_material_gets_one_to_bake_through(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Bare", size=1)
    out = blender.ok("bake_texture", object="Bare", type="emit", size=16, samples=1, path=str(tmp_path / "e.png"))
    assert out["created_material"] == "Bare" and out["image_driven"] == {"Bare": ["emission"]}
    assert blender.ok("get_scene")["objects"][0]["materials"] == ["Bare"]


@pytest.mark.parametrize(("arguments", "kind"), [
    ({"object": "Nobody"}, "not_found"),
    ({"object": "Pivot"}, "bad_argument"),
    ({"type": "shadow"}, "bad_argument"),
    ({"uv_map": "Nope"}, "bad_argument"),
    ({"size": 8}, "bad_argument"),
    ({"size": 8192}, "bad_argument"),
    ({"size": 4096, "samples": 100}, "bad_argument"),
    ({"samples": 0}, "bad_argument"),
    ({"from_objects": ["Box"]}, "bad_argument"),
    ({"from_objects": ["Nobody"]}, "not_found"),
    ({"path": "box.jpg"}, "bad_argument"),
])
def test_bake_texture_refused_changes_nothing(scene, tmp_path, arguments, kind):
    arguments = {"object": "Box", "type": "ao", "size": 32, "samples": 1, "path": str(tmp_path / "box.png"),
                 **arguments}
    if arguments["path"] == "box.jpg":
        arguments["path"] = str(tmp_path / "box.jpg")
    payload = bake_refused_unchanged(scene, kind, **arguments)
    if "uv_map" in arguments:
        assert "unwrap_uv" in payload["hint"]
    assert not (tmp_path / "box.png").exists()


def test_a_mesh_without_uvs_is_refused_with_a_hint_to_unwrap(scene, tmp_path):
    scene.ok("create_mesh", name="Tri", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])
    scene.ok("save_file")
    payload = bake_refused_unchanged(scene, "bad_argument", object="Tri", type="ao", size=16, samples=1,
                                     path=str(tmp_path / "tri.png"))
    assert "unwrap_uv" in payload["hint"]


#: Code that adds a material whose output is fed by an Emission shader alone, as a light panel's material often is. With
#: ``SPARE`` set, the material also holds a Principled BSDF that is not connected to anything.
GLOW = """
material = bpy.data.materials.new("Glow")
material.use_nodes = True
tree = material.node_tree
for node in [n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"]:
    tree.nodes.remove(node)
light = tree.nodes.new("ShaderNodeEmission")
light.name = "Panel Light"
light.inputs["Color"].default_value = (0.0, 0.0, 1.0, 1.0)
output = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial")
tree.links.new(light.outputs[0], output.inputs["Surface"])
if SPARE:
    tree.nodes.new("ShaderNodeBsdfPrincipled").name = "Unused Principled"
bpy.data.objects["Lamp"].data.materials.append(material)
"""


@pytest.mark.parametrize("spare", [False, True], ids=["emission_only", "principled_not_connected"])
@pytest.mark.parametrize("wired", [True, False], ids=["wired", "unwired"])
def test_a_material_without_a_principled_bsdf_at_its_output_is_refused_before_anything_changes(
        scene, tmp_path, spare, wired):
    """A material whose output no Principled BSDF feeds is refused by name, before anything changes.

    The bake is made through a Principled BSDF and its image is wired into one. The material's nodes and links stay
    as they were, and no PNG is written.
    """
    scene.ok("create_object", kind="cube", name="Lamp", size=1, location=[4, 0, 0])
    scene.ok("unwrap_uv", object="Lamp", method="smart")
    scene.ok("run_python", code=f"SPARE = {spare}\n" + GLOW)
    scene.ok("save_file")
    payload = bake_refused_unchanged(scene, "bad_argument", object="Lamp", type="emit", size=16, samples=1,
                                     path=str(tmp_path / "lamp.png"), use_in_material=wired)
    assert "Glow" in payload["error"] and "Principled BSDF" in payload["error"], payload
    assert not (tmp_path / "lamp.png").exists()


#: Code that feeds the material Paint's output through a Mix Shader of its Principled BSDF and a Transparent BSDF.
MIXED = """
tree = bpy.data.materials["Paint"].node_tree
output = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial")
principled = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
mix = tree.nodes.new("ShaderNodeMixShader")
tree.links.new(principled.outputs[0], mix.inputs[1])
tree.links.new(tree.nodes.new("ShaderNodeBsdfTransparent").outputs[0], mix.inputs[2])
tree.links.new(mix.outputs[0], output.inputs["Surface"])
"""


def test_a_principled_bsdf_that_reaches_the_output_through_a_mix_shader_is_baked_and_wired(scene, tmp_path):
    scene.ok("unwrap_uv", object="Box", method="smart")
    scene.ok("run_python", code=MIXED)
    out = scene.ok("bake_texture", object="Box", type="diffuse", size=16, samples=1, path=str(tmp_path / "box.png"))
    assert out["image_driven"] == {"Paint": ["base_color"]}
