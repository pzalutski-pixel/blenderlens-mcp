"""Tests for the modelling tools, run against a real Blender.

They cover vertex colours, UV unwrapping, tubes, lathes and text, the mesh operations, joining, the material
settings and nested extras. Most tests check what Blender holds, or what the exported file holds when read back
from disk, rather than only the tool's own result.
"""

import json
import struct
import zlib
from pathlib import Path

import pytest

from tests.test_refusals_change_nothing import refused_unchanged

pytestmark = pytest.mark.integration


def close(a, b, tol=1e-3):
    """Return whether the sequences ``a`` and ``b`` agree element by element to within ``tol``."""
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


# ---------------------------------------------------------------------------
# Reading a .glb's binary chunk: what Godot and three.js will load
# ---------------------------------------------------------------------------

COMPONENTS = {5126: ("f", 4, None), 5125: ("I", 4, None), 5123: ("H", 2, 65535), 5121: ("B", 1, 255)}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def glb(path) -> tuple[dict, bytes]:
    """Read a .glb file, and return its JSON document and its binary chunk (empty when it has none)."""
    data = Path(path).read_bytes()
    assert data[:4] == b"glTF", path
    length = struct.unpack_from("<I", data, 12)[0]
    document = json.loads(data[20:20 + length].decode("utf-8"))
    rest = 20 + length
    binary = b""
    if rest < len(data):
        size = struct.unpack_from("<I", data, rest)[0]
        binary = data[rest + 8:rest + 8 + size]
    return document, binary


def accessor(document: dict, binary: bytes, index: int) -> list[tuple]:
    """Return every element of an accessor, with normalised integers read as values from 0 to 1."""
    found = document["accessors"][index]
    view = document["bufferViews"][found["bufferView"]]
    code, size, top = COMPONENTS[found["componentType"]]
    width = WIDTHS[found["type"]]
    stride = view.get("byteStride") or size * width
    start = view.get("byteOffset", 0) + found.get("byteOffset", 0)
    rows = []
    for i in range(found["count"]):
        row = struct.unpack_from("<" + code * width, binary, start + i * stride)
        if found.get("normalized") and top:
            row = tuple(v / top for v in row)
        rows.append(row)
    return rows


def primitives(document: dict, binary: bytes) -> list[dict]:
    """Return each primitive of each mesh, with its attributes read out, its indices, and its material's name."""
    out = []
    for mesh in document["meshes"]:
        for primitive in mesh["primitives"]:
            row = {name: accessor(document, binary, index) for name, index in primitive["attributes"].items()}
            row["indices"] = [i[0] for i in accessor(document, binary, primitive["indices"])]
            material = primitive.get("material")
            row["material"] = document["materials"][material]["name"] if material is not None else None
            out.append(row)
    return out


def png_rgba(path: Path, pixels: list[tuple[int, int, int, int]], width: int = 2) -> str:
    """Write a small RGBA PNG byte by byte, and return its path as a string."""
    height = len(pixels) // width

    def chunk(kind: bytes, data: bytes) -> bytes:
        """Return one PNG chunk: its length, its kind, its data and its CRC."""
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"".join(bytes(p) for p in pixels[y * width:(y + 1) * width]) for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows))
                     + chunk(b"IEND", b""))
    return str(path)


#: Code that returns the shape of the object NAME's mesh: whether it is closed, its boundary edges, signed volume,
#: faces, seams, smooth faces and sharp edges, and its UV maps.
SHAPE = """
import bmesh
bm = bmesh.new()
bm.from_mesh(bpy.data.objects[NAME].data)
bm.normal_update()
result = {"closed": all(e.is_manifold for e in bm.edges), "boundary": sum(1 for e in bm.edges if e.is_boundary),
          "volume": bm.calc_volume(signed=True), "faces": len(bm.faces),
          "seams": sum(1 for e in bm.edges if e.seam), "smooth": sum(1 for f in bm.faces if f.smooth),
          "sharp": sum(1 for e in bm.edges if not e.smooth),
          "uv_maps": [u.name for u in bpy.data.objects[NAME].data.uv_layers]}
bm.free()
"""


def shape(server, name) -> dict:
    """Return whether a mesh is closed, its signed volume, its counts of faces and edges, and its UV map names.

    The counts are of all faces, smooth faces, boundary edges, sharp edges and seams.

    The signed volume is positive when the normals point outward.
    """
    return server.ok("run_python", code=f"NAME = {name!r}\n{SHAPE}")["result"]


def uvs(server, name, layer) -> list:
    """Return the UV coordinates of every loop of the object ``name`` in the UV map ``layer``, rounded."""
    code = (f"me = bpy.data.objects[{name!r}].data\n"
            f"result = [[round(c, 5) for c in d.uv] for d in me.uv_layers[{layer!r}].data]")
    return server.ok("run_python", code=code)["result"]


# ---------------------------------------------------------------------------
# set_vertex_color
# ---------------------------------------------------------------------------


def test_a_cube_painted_on_its_top_face_carries_that_colour_on_its_top_vertices_in_the_file(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=2)
    blender.ok("set_material", material="Wood", object="Crate", base_color=[0.4, 0.3, 0.2])
    painted = blender.ok("set_vertex_color", object="Crate", color=[1, 0, 0], facing="top")
    assert painted["attribute"] == "Col" and painted["faces_painted"] == 1
    assert painted["materials_wired"] == ["Wood"]
    path = tmp_path / "crate.glb"
    blender.ok("export_glb", path=str(path))
    document, binary = glb(path)
    (primitive,) = primitives(document, binary)
    assert "COLOR_0" in primitive, sorted(primitive)
    top = [i for i, n in enumerate(primitive["NORMAL"]) if n[1] > 0.99]  # glTF's up axis is Y.
    assert len(top) == 4, "the file does not hold the top face's four corners"
    for i, (position, colour) in enumerate(zip(primitive["POSITION"], primitive["COLOR_0"], strict=True)):
        if i in top:
            assert position[1] == pytest.approx(1.0, abs=1e-4)
            assert close(colour[:3], (1, 0, 0), 0.01), (i, colour)
        else:
            assert close(colour[:3], (1, 1, 1), 0.01), (i, colour)


def test_vertex_colours_on_several_faces_and_an_object_with_no_material(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Block")
    first = blender.ok("set_vertex_color", object="Block", color="#00ff00", facing="front")
    assert first["created_material"] == "Vertex Color", \
        "an object without a material did not get one that uses the colours"
    second = blender.ok("set_vertex_color", object="Block", color=[0, 0, 1], faces=[0])
    assert "created_material" not in second and second["materials_wired"] == ["Vertex Color"]
    materials = {m["name"]: m for m in blender.ok("list_materials")["materials"]}
    assert materials["Vertex Color"]["vertex_color"] == "Col"
    document, binary = glb(blender.ok("export_glb", path=str(tmp_path / "block.glb"))["path"])
    (primitive,) = primitives(document, binary)
    colours = {tuple(round(c, 2) for c in colour[:3]) for colour in primitive["COLOR_0"]}
    assert {(0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 1.0, 1.0)} <= colours, colours


def test_painting_without_the_material_leaves_the_material_alone(blender):
    blender.ok("create_object", kind="cube", name="Crate")
    blender.ok("set_material", material="Wood", object="Crate", base_color=[0.4, 0.3, 0.2])
    out = blender.ok("set_vertex_color", object="Crate", color=[1, 1, 0], use_in_material=False)
    assert out["faces_painted"] == 6 and out["materials_wired"] == []
    wood = blender.ok("list_materials")["materials"][0]
    assert wood["vertex_color"] is None and wood["base_color"] == [0.4, 0.3, 0.2, 1.0]


def test_a_shared_material_says_which_other_objects_lack_the_colours(blender):
    blender.ok("create_object", kind="cube", name="A")
    blender.ok("create_object", kind="cube", name="B", location=[3, 0, 0])
    blender.ok("set_material", material="Paint", object="A")
    blender.ok("set_material", material="Paint", object="B")
    out = blender.ok("set_vertex_color", object="A", color=[1, 0, 0], facing="top")
    assert "B" in out["note"] and "Col" in out["note"], out


@pytest.mark.parametrize(("arguments", "kind"), [
    ({"color": [1, 0]}, "bad_argument"),
    ({"color": [1, 0, 0], "faces": [99]}, "bad_argument"),
    ({"color": [1, 0, 0], "facing": [1, 1, 1], "tolerance": 1}, "not_found"),
    ({"color": [1, 0, 0], "attribute": "UVMap"}, "bad_argument"),
    ({"color": [1, 0, 0], "use_in_material": "yes"}, "bad_argument"),
    ({"color": [float("nan"), 0, 0]}, "bad_argument"),
])
def test_set_vertex_color_refused_changes_nothing(scene, arguments, kind):
    refused_unchanged(scene, "set_vertex_color", kind, object="Box", **arguments)


#: Code that gives the object OBJECT a material whose surface is an Emission shader alone, with no Principled BSDF.
GLOW = """
material = bpy.data.materials.new("Glow")
material.use_nodes = True
tree = material.node_tree
for node in [n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"]:
    tree.nodes.remove(node)
emission = tree.nodes.new("ShaderNodeEmission")
emission.inputs["Strength"].default_value = 5
output = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial")
tree.links.new(emission.outputs[0], output.inputs["Surface"])
bpy.data.objects[OBJECT].data.materials.append(material)
"""

#: Code that gives the object OBJECT a material named NAME whose base colour is fed from the user's own image.
USER_IMAGE = """
material = bpy.data.materials.new(NAME)
material.use_nodes = True
tree = material.node_tree
texture = tree.nodes.new("ShaderNodeTexImage")
texture.name = "Their Image"
texture.image = bpy.data.images.new("Painted", 4, 4)
bsdf = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
tree.links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
bpy.data.objects[OBJECT].data.materials.append(material)
"""

#: Code that returns the names of the material NAME's nodes and every link between them.
WIRING = """
tree = bpy.data.materials[NAME].node_tree
result = {"nodes": sorted(n.name for n in tree.nodes),
          "links": sorted([l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier]
                          for l in tree.links)}
"""


def wiring(server, name) -> dict:
    """Return the names of a material's nodes and every link between them."""
    return server.ok("run_python", code=f"NAME = {name!r}\n{WIRING}")["result"]


def test_painting_leaves_an_emission_material_alone_and_says_so(blender):
    blender.ok("create_object", kind="cube", name="Lamp")
    blender.ok("run_python", code=f"OBJECT = 'Lamp'\n{GLOW}")
    before = wiring(blender, "Glow")
    out = blender.ok("set_vertex_color", object="Lamp", color="#ff0000", facing="top")
    assert out["materials_wired"] == [] and list(out["not_wired"]) == ["Glow"], out
    assert "Glow" in out["note"]
    assert wiring(blender, "Glow") == before, "painting changed the emission material's nodes"


def test_painting_leaves_a_users_image_on_the_base_colour_alone(blender):
    blender.ok("create_object", kind="cube", name="Crate")
    blender.ok("run_python", code=f"OBJECT = 'Crate'\nNAME = 'Painted'\n{USER_IMAGE}")
    before = wiring(blender, "Painted")
    out = blender.ok("set_vertex_color", object="Crate", color="#ff0000", facing="top")
    assert out["materials_wired"] == [] and "Their Image" in out["not_wired"]["Painted"], out
    assert wiring(blender, "Painted") == before


def test_set_material_leaves_node_setups_it_did_not_make_and_changes_nothing(scene, tmp_path):
    scene.ok("run_python", code=f"OBJECT = 'Box'\n{GLOW}\nOBJECT = 'Box'\nNAME = 'Painted'\n{USER_IMAGE}")
    scene.ok("save_file")
    glow, painted = wiring(scene, "Glow"), wiring(scene, "Painted")
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not an image at all")
    good = png_rgba(tmp_path / "good.png", [(255, 255, 255, 255)] * 4)
    for arguments in ({"base_color_image": str(junk)}, {"base_color": [1, 0, 0]}, {"alpha_mode": "blend"}):
        payload = refused_unchanged(scene, "set_material", "bad_argument", material="Glow", **arguments)
        assert "Principled BSDF" in payload["error"] and "Emission" in payload["error"], payload
        assert wiring(scene, "Glow") == glow
    for arguments in ({"base_color_image": good}, {"vertex_color": ""}):
        payload = refused_unchanged(scene, "set_material", "bad_argument", material="Painted", **arguments)
        assert "Their Image" in payload["error"], payload
        assert wiring(scene, "Painted") == painted
    assigned = scene.ok("set_material", material="Glow", object="Box", faces=[0])
    assert assigned["faces_assigned"] == 1 and wiring(scene, "Glow") == glow, \
        "assigning the material to a face changed its nodes"
    scene.ok("set_material", material="Painted", roughness=0.3)
    assert wiring(scene, "Painted") == painted, "setting a value of the material's own Principled BSDF changed a link"


def test_a_material_whose_surface_is_empty_gets_a_principled_bsdf(blender, tmp_path):
    blender.ok("run_python", code="""
material = bpy.data.materials.new("Bare")
material.use_nodes = True
tree = material.node_tree
for node in [n for n in tree.nodes if n.bl_idname != "ShaderNodeOutputMaterial"]:
    tree.nodes.remove(node)
""")
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not an image at all")
    before = wiring(blender, "Bare")
    blender.refused("set_material", "bad_argument", material="Bare", base_color_image=str(junk))
    assert wiring(blender, "Bare") == before, "the refused call left behind the Principled BSDF it made, or its link"
    out = blender.ok("set_material", material="Bare", base_color=[1, 0, 0])
    assert out["material"]["principled"] is True and out["material"]["base_color"][:3] == [1.0, 0.0, 0.0]


def test_set_vertex_color_on_an_empty_or_in_edit_mode_is_refused(scene):
    refused_unchanged(scene, "set_vertex_color", "bad_argument", object="Pivot", color=[1, 0, 0])
    scene.ok("run_python", code="o = bpy.data.objects['Box']\nbpy.context.view_layer.objects.active = o\n"
                                "with bpy.context.temp_override(active_object=o, object=o):\n"
                                "    bpy.ops.object.mode_set(mode='EDIT')")
    payload = scene.refused("set_vertex_color", "bad_argument", object="Box", color=[1, 0, 0])
    assert "Tab" in payload["hint"]


@pytest.fixture
def scene(blender, tmp_path):
    """Return a server whose scene, saved to a file, holds a cube with a material and a bevel, and an empty."""
    blender.ok("create_object", kind="cube", name="Box", size=[2, 2, 2])
    blender.ok("set_material", material="Paint", object="Box", base_color=[0.2, 0.3, 0.4])
    blender.ok("add_modifier", object="Box", type="bevel", settings={"width": 0.1})
    blender.ok("create_object", kind="empty", name="Pivot", location=[3, 0, 0])
    blender.ok("save_file", path=str(tmp_path / "scene.blend"))
    return blender


# ---------------------------------------------------------------------------
# unwrap_uv
# ---------------------------------------------------------------------------


def triangles_overlap(a, b) -> bool:
    """Return whether two 2D triangles overlap by more than touching, that is, no edge gives a separating axis."""
    for tri, other in ((a, b), (b, a)):
        for i in range(3):
            p, q = tri[i], tri[(i + 1) % 3]
            axis = (q[1] - p[1], p[0] - q[0])
            ours = [axis[0] * v[0] + axis[1] * v[1] for v in tri]
            theirs = [axis[0] * v[0] + axis[1] * v[1] for v in other]
            if max(ours) <= min(theirs) + 1e-7 or max(theirs) <= min(ours) + 1e-7:
                return False
    return True


def uv_overlaps_in_file(path) -> int:
    """Return how many pairs of triangles overlap in the first UV map of a .glb file, primitive by primitive."""
    document, binary = glb(path)
    count = 0
    for primitive in primitives(document, binary):
        uv = primitive["TEXCOORD_0"]
        tris = [[uv[i] for i in primitive["indices"][k:k + 3]] for k in range(0, len(primitive["indices"]), 3)]
        for i in range(len(tris)):
            for j in range(i + 1, len(tris)):
                count += triangles_overlap(tris[i], tris[j])
    return count


def test_a_smart_unwrapped_cube_has_a_uv_map_with_no_overlap(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=[2, 1, 1])
    out = blender.ok("unwrap_uv", object="Crate", method="smart")
    assert out["uv_map"] == "UVMap" and out["active"] == "UVMap"
    assert out["overlapping_faces"] == 0 and out["out_of_bounds_faces"] == 0
    assert out["islands"] >= 1 and 0.2 < out["area_used"] <= 1.0
    assert out["texel_density"] > 0
    path = tmp_path / "crate.glb"
    blender.ok("export_glb", path=str(path))
    assert uv_overlaps_in_file(path) == 0, "triangles share UV space in the file"


def test_lightmap_on_uv2_leaves_uvmap_untouched(blender):
    blender.ok("create_object", kind="cube", name="Crate")
    before = uvs(blender, "Crate", "UVMap")
    out = blender.ok("unwrap_uv", object="Crate", method="lightmap", uv_map="UV2")
    assert out["uv_maps"] == ["UVMap", "UV2"] and out["uv_map"] == "UV2"
    assert out["active"] == "UVMap", "the map the textures use is no longer the active one"
    assert out["overlapping_faces"] == 0 and out["out_of_bounds_faces"] == 0
    assert uvs(blender, "Crate", "UVMap") == before
    assert len({tuple(uv) for uv in uvs(blender, "Crate", "UV2")}) > 8, "UV2 holds no layout of its own"


def test_unwrap_on_a_closed_mesh_without_seams_is_refused_until_seams_are_marked(blender):
    blender.ok("create_object", kind="cube", name="Crate")
    before = uvs(blender, "Crate", "UVMap")
    payload = blender.refused("unwrap_uv", "bad_argument", object="Crate", method="unwrap")
    assert "mark_seams" in payload["hint"], payload
    assert uvs(blender, "Crate", "UVMap") == before
    seams = blender.ok("edit_mesh", object="Crate", operation="mark_seams", angle=30)
    assert seams["seams"] == 12
    out = blender.ok("unwrap_uv", object="Crate", method="unwrap")
    assert out["islands"] == 6 and out["overlapping_faces"] == 0


@pytest.mark.parametrize("method", ["cylinder", "sphere"])
def test_cylinder_and_sphere_projection_follow_the_object_not_a_view(blender, method):
    """Cylinder and sphere projection follow the object's own axes, so an upright and a lying cylinder unwrap alike.

    Without ALIGN_TO_OBJECT the projection would follow a view, which a background Blender does not have.
    """
    blender.ok("create_object", kind="cylinder", name="Upright", segments=12, radius=0.5, depth=2)
    blender.ok("create_object", kind="cylinder", name="Lying", segments=12, radius=0.5, depth=2,
               rotation=[90, 0, 0], location=[3, 0, 0])
    first = blender.ok("unwrap_uv", object="Upright", method=method)
    second = blender.ok("unwrap_uv", object="Lying", method=method)
    assert first["islands"] == second["islands"]
    assert uvs(blender, "Upright", "UVMap") == uvs(blender, "Lying", "UVMap")


def test_cube_projection_packs_and_reports_what_it_made(blender):
    blender.ok("create_object", kind="cube", name="Crate", size=[2, 1, 1])
    out = blender.ok("unwrap_uv", object="Crate", method="cube")
    assert out["out_of_bounds_faces"] == 0, "packing did not bring the islands into the 0-1 square"
    assert out["faces"] == 6


#: Code that returns the active object, the selection, the mode of the object NAME, its faces' selected and hidden
#: flags, and the UV sync setting.
MESH_STATE = """
layer = bpy.context.view_layer
mesh = bpy.data.objects[NAME].data
result = [layer.objects.active.name, sorted(o.name for o in bpy.data.objects if o.select_get()),
          bpy.data.objects[NAME].mode, [p.select for p in mesh.polygons], [p.hide for p in mesh.polygons],
          bpy.context.scene.tool_settings.use_uv_select_sync]
"""


def test_unwrap_uv_puts_back_the_mode_the_active_object_and_every_selection(blender):
    blender.ok("create_object", kind="cube", name="Crate")
    blender.ok("create_object", kind="cube", name="Other", location=[3, 0, 0])
    blender.ok("run_python", code="mesh = bpy.data.objects['Crate'].data\n"
                                  "for p in mesh.polygons: p.select = p.index == 2\n"
                                  "mesh.polygons[4].hide = True")
    before = blender.ok("run_python", code=f"NAME = 'Crate'\n{MESH_STATE}")["result"]
    assert before[:3] == ["Other", ["Other"], "OBJECT"]
    out = blender.ok("unwrap_uv", object="Crate", method="smart")
    assert out["faces"] == 6, "hidden faces were not unwrapped"
    after = blender.ok("run_python", code=f"NAME = 'Crate'\n{MESH_STATE}")["result"]
    assert after == before, "unwrap_uv changed the active object, the selection, the mode, the face flags or UV sync"


def test_a_hidden_object_is_unwrapped_and_stays_hidden(blender):
    """A hidden object is unwrapped, and stays hidden.

    A hidden object cannot enter edit mode, and without edit mode the UV operator would finish having done nothing.
    """
    blender.ok("create_object", kind="cube", name="Proxy")
    before = uvs(blender, "Proxy", "UVMap")
    blender.ok("set_object", object="Proxy", hide=True)
    out = blender.ok("unwrap_uv", object="Proxy", method="smart")
    assert out["islands"] == 6, "smart projection did not cut the cube's single cross of UVs into its six sides"
    assert uvs(blender, "Proxy", "UVMap") != before
    assert blender.ok("get_object", object="Proxy")["visible"] is False


@pytest.mark.parametrize("hidden_by", ["layer_collection", "collection"])
def test_an_object_in_a_hidden_collection_is_unwrapped_and_stays_hidden(blender, hidden_by):
    blender.ok("run_python", code=f"""
collection = bpy.data.collections.new("Hidden")
bpy.context.scene.collection.children.link(collection)
mesh = bpy.data.meshes.new("Ball")
bm = bmesh.new()
bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=1)
bm.to_mesh(mesh)
bm.free()
collection.objects.link(bpy.data.objects.new("Ball", mesh))
if {hidden_by!r} == "layer_collection":
    bpy.context.view_layer.layer_collection.children["Hidden"].hide_viewport = True
else:
    collection.hide_viewport = True
""")
    out = blender.ok("unwrap_uv", object="Ball", method="smart", uv_map="Fresh")
    assert out["area_used"] < 1.01 and out["islands"] < 128, out
    assert len({tuple(uv) for uv in uvs(blender, "Ball", "Fresh")}) > 100, "the new map holds no real layout"
    still = blender.ok("run_python", code="result = [bpy.context.view_layer.layer_collection.children['Hidden']"
                                          ".hide_viewport, bpy.data.collections['Hidden'].hide_viewport]")["result"]
    assert still == ([True, False] if hidden_by == "layer_collection" else [False, True])


def test_unwrap_uv_refused_changes_nothing(scene):
    refused_unchanged(scene, "unwrap_uv", "bad_argument", object="Box", method="unwrap")
    refused_unchanged(scene, "unwrap_uv", "bad_argument", object="Box", method="smart", margin=2)
    lightmap = refused_unchanged(scene, "unwrap_uv", "bad_argument", object="Box", method="lightmap", margin=0.05)
    assert "0.01" in lightmap["error"] and "lightmap" in lightmap["error"], lightmap
    refused_unchanged(scene, "unwrap_uv", "bad_argument", object="Pivot", method="smart")
    refused_unchanged(scene, "unwrap_uv", "bad_argument", object="Box", method="unwrap", uv_map="UV2")
    payload = refused_unchanged(scene, "unwrap_uv", "not_found", object="Ghost")
    assert "Box" in payload["hint"]


# ---------------------------------------------------------------------------
# Tubes, lathes and text
# ---------------------------------------------------------------------------


def test_a_tube_is_closed_and_as_long_as_its_path(blender):
    made = blender.ok("create_object", kind="tube", name="Hose", points=[[0, 0, 0], [2, 0, 0]], radius=0.1)
    assert made["type"] == "MESH" and made["scale"] == [1.0, 1.0, 1.0]
    assert close(made["dimensions"], [2.0, 0.2, 0.2]), made["dimensions"]
    held = shape(blender, "Hose")
    assert held["closed"] and held["volume"] > 0, held
    assert held["uv_maps"] == ["UVMap"]


def test_a_tube_follows_a_bent_path_and_its_caps_are_optional(blender):
    bent = blender.ok("create_object", kind="tube", name="Pipe", points=[[0, 0, 0], [1, 0, 0], [1, 1, 0]],
                      radius=0.1, smooth=False, bevel_resolution=2)
    assert close(bent["dimensions"], [1.1, 1.1, 0.2], 2e-3), bent["dimensions"]
    assert shape(blender, "Pipe")["closed"]
    curved = blender.ok("create_object", kind="tube", name="Cable", points=[[0, 0, 0], [1, 0, 0.5], [2, 1, 0.5]],
                        radius=0.05, fill=False)
    held = shape(blender, "Cable")
    assert not held["closed"] and held["boundary"] > 0, "the tube is closed, or its ends are capped"
    assert held["smooth"] == held["faces"], "the round tube is not shaded smooth"
    assert curved["dimensions"][2] > 0.5


def test_a_straight_tube_turning_straight_back_is_refused(scene):
    payload = refused_unchanged(scene, "create_object", "bad_argument", kind="tube", smooth=False,
                                points=[[0, 0, 0], [1, 0, 0], [0, 0, 0]])
    assert "points[1]" in payload["error"]


def test_a_grid_takes_a_size_of_two_numbers(blender):
    made = blender.ok("create_object", kind="grid", name="Field", size=[3, 2])
    assert close(made["dimensions"], [3.0, 2.0, 0.0]), made["dimensions"]


def test_a_lathe_is_closed_and_as_wide_as_its_profile(blender):
    made = blender.ok("create_object", kind="lathe", name="Knob", segments=24,
                      profile=[[0, 0], [0.5, 0], [0.5, 0.2], [0.3, 0.3], [0, 0.3]])
    assert close(made["dimensions"], [1.0, 1.0, 0.3]), made["dimensions"]
    held = shape(blender, "Knob")
    assert held["closed"] and held["volume"] > 0, held
    assert held["uv_maps"] == ["UVMap"]


def test_a_lathe_profile_off_the_axis_is_capped_unless_asked_not_to(blender):
    capped = blender.ok("create_object", kind="lathe", name="Can", profile=[[0.5, 0], [0.5, 1]], segments=16)
    assert close(capped["dimensions"], [1.0, 1.0, 1.0], 2e-2)
    assert shape(blender, "Can")["closed"]
    blender.ok("create_object", kind="lathe", name="Ring", profile=[[0.5, 0], [0.5, 1]], segments=16, fill=False)
    assert shape(blender, "Ring")["boundary"] == 32


def test_text_is_a_closed_mesh_of_its_size(blender):
    made = blender.ok("create_object", kind="text", name="Sign", text="EXIT", size=0.5, extrude=0.1)
    width, height, depth = made["dimensions"]
    assert depth == pytest.approx(0.1, abs=1e-3)
    assert 0.8 < width < 2.0 and 0.25 < height < 0.6, made["dimensions"]
    held = shape(blender, "Sign")
    assert held["closed"] and held["volume"] > 0, held


@pytest.mark.parametrize(("arguments", "maximum"), [
    ({"kind": "tube", "points": [[i * 0.01, 0, 0] for i in range(1001)], "radius": 0.01}, "1000"),
    ({"kind": "tube", "points": [[0, 0, 0], [1, 0, 0]], "bevel_resolution": 33}, "32"),
    ({"kind": "lathe", "profile": [[0, 0], [1, 0], [0, 1]], "segments": 1025}, "1024"),
    ({"kind": "lathe", "profile": [[0.5, i * 0.001] for i in range(1001)]}, "1000"),
    ({"kind": "text", "text": "x" * 1001}, "1000"),
])
def test_tubes_lathes_and_text_are_bounded(scene, arguments, maximum):
    payload = refused_unchanged(scene, "create_object", "bad_argument", **arguments)
    assert maximum in payload["error"].replace(",", ""), payload["error"]


@pytest.mark.parametrize(("arguments", "kind"), [
    ({"kind": "tube", "points": [[0, 0, 0]]}, "bad_argument"),
    ({"kind": "tube"}, "bad_argument"),
    ({"kind": "tube", "points": [[0, 0, 0], [0, 0, 0]]}, "bad_argument"),
    ({"kind": "tube", "points": [[0, 0, 0], [1, 0, 0]], "radius": 0}, "bad_argument"),
    ({"kind": "tube", "points": [[0, 0, 0], [1, 0, 0]], "rotation": "x"}, "bad_argument"),
    ({"kind": "lathe", "profile": [[-0.5, 0], [0.5, 1]]}, "bad_argument"),
    ({"kind": "lathe", "profile": [[0, 0], [0, 1]]}, "bad_argument"),
    ({"kind": "lathe"}, "bad_argument"),
    ({"kind": "text", "text": "   "}, "bad_argument"),
    ({"kind": "text", "text": "OK", "extrude": -1}, "bad_argument"),
    ({"kind": "text", "text": "OK", "size": [1, 1, 1]}, "bad_argument"),
])
def test_tubes_lathes_and_text_refused_leave_nothing_behind(scene, arguments, kind):
    refused_unchanged(scene, "create_object", kind, **arguments)


# ---------------------------------------------------------------------------
# bisect, solidify, dissolve_planar, mark_seams and bevel by angle
# ---------------------------------------------------------------------------


def test_bisect_cuts_a_sphere_into_a_closed_bowl(blender):
    blender.ok("create_object", kind="uv_sphere", name="Ball", radius=1, location=[0, 0, 5])
    out = blender.ok("edit_mesh", object="Ball", operation="bisect", pivot=[0, 0, 0.2], direction="top")
    assert out["dimensions"][2] == pytest.approx(1.2, abs=1e-3)
    assert shape(blender, "Ball")["closed"], "the cut is not filled"
    blender.ok("create_object", kind="uv_sphere", name="Cap", radius=1)
    out = blender.ok("edit_mesh", object="Cap", operation="bisect", pivot=[0, 0, 0.2], direction="top", keep="above",
                     fill=False)
    assert out["dimensions"][2] == pytest.approx(0.8, abs=1e-3)
    assert not shape(blender, "Cap")["closed"]


def test_bisect_keeping_both_sides_only_cuts(blender):
    made = blender.ok("create_object", kind="cube", name="Crate", size=2)
    out = blender.ok("edit_mesh", object="Crate", operation="bisect", direction="top", keep="both")
    assert out["mesh"]["faces"] == 10 and close(out["dimensions"], made["dimensions"])


def test_bisect_direction_is_world_space(blender):
    blender.ok("create_object", kind="cube", name="Turned", size=2, rotation=[90, 0, 0])
    out = blender.ok("edit_mesh", object="Turned", operation="bisect", direction="top", pivot=[0, 0, 0])
    world = blender.ok("get_object", object="Turned")["world_bounds"]
    assert world["max"][2] == pytest.approx(0.0, abs=1e-4), "the cut is not along world Z"
    assert out["dimensions"][1] == pytest.approx(1.0, abs=1e-3)


def test_solidify_gives_a_plane_a_closed_thickness(blender):
    blender.ok("create_object", kind="plane", name="Panel", size=[2, 1, 0])
    out = blender.ok("edit_mesh", object="Panel", operation="solidify", amount=0.1)
    assert out["mesh"]["faces"] == 6 and out["dimensions"][2] == pytest.approx(0.1, abs=1e-4)
    assert shape(blender, "Panel")["closed"]


def test_dissolve_planar_merges_flat_faces_and_keeps_the_shape(blender):
    blender.ok("create_object", kind="cube", name="Crate", size=2)
    blender.ok("edit_mesh", object="Crate", operation="subdivide", cuts=2)
    out = blender.ok("edit_mesh", object="Crate", operation="dissolve_planar")
    assert out["mesh"]["faces"] == 6 and close(out["dimensions"], [2, 2, 2])


def test_mark_seams_marks_only_the_sharp_edges(blender):
    blender.ok("create_object", kind="cylinder", name="Can", segments=16)
    out = blender.ok("edit_mesh", object="Can", operation="mark_seams", angle=30)
    assert out["seams"] == 32 == shape(blender, "Can")["seams"], "the seams are not the two rims alone"


def test_an_angle_limited_bevel_touches_only_the_sharp_edges(blender):
    blender.ok("create_object", kind="cylinder", name="Can", segments=16)
    out = blender.ok("edit_mesh", object="Can", operation="bevel", amount=0.05, angle=30)
    # Each of the 32 rim edges becomes a face, and the 16 side edges, at 22.5 degrees, are left as they were.
    assert out["mesh"]["faces"] == 18 + 32 and out["mesh"]["vertices"] == 64, out["mesh"]
    blender.ok("create_object", kind="cylinder", name="All", segments=16)
    everything = blender.ok("edit_mesh", object="All", operation="bevel", amount=0.05)
    assert everything["mesh"]["faces"] > out["mesh"]["faces"]


@pytest.mark.parametrize("arguments", [
    {"operation": "bisect"},
    {"operation": "bisect", "direction": "top", "pivot": [0, 0, 5]},
    {"operation": "bisect", "direction": "top", "keep": "middle"},
    {"operation": "bisect", "direction": "top", "fill": "yes"},
    {"operation": "solidify"},
    {"operation": "dissolve_planar", "angle": 200},
    {"operation": "mark_seams", "angle": -1},
    {"operation": "bevel", "amount": 0.05, "angle": 100, "facing": "top"},
])
def test_bisect_solidify_dissolve_planar_mark_seams_and_angle_bevel_refused_change_nothing(scene, arguments):
    kind = "not_found" if arguments.get("angle") == 100 else "bad_argument"
    refused_unchanged(scene, "edit_mesh", kind, object="Box", **arguments)


# ---------------------------------------------------------------------------
# join_objects
# ---------------------------------------------------------------------------


@pytest.fixture
def pieces(blender):
    """Return the Blender session holding three pieces, Body, Lid and Knob, each with a material of its own."""
    blender.ok("create_object", kind="cube", name="Body", size=1)
    blender.ok("create_object", kind="cube", name="Lid", size=1, location=[3, 0, 0])
    blender.ok("create_object", kind="cylinder", name="Knob", segments=8, radius=0.2, depth=0.4, location=[6, 0, 0])
    for name, material, colour in (("Body", "Red", [1, 0, 0]), ("Lid", "Green", [0, 1, 0]),
                                   ("Knob", "Blue", [0, 0, 1])):
        blender.ok("set_material", material=material, object=name, base_color=colour)
    return blender


def test_three_pieces_joined_keep_three_materials_each_on_its_own_faces(pieces, tmp_path):
    out = pieces.ok("join_objects", objects=["Body", "Lid", "Knob"])
    assert out["name"] == "Body" and out["joined"] == ["Lid", "Knob"]
    assert [(s["material"], s["faces"]) for s in out["slots"]] == [("Red", 6), ("Green", 6), ("Blue", 10)]
    names = [o["name"] for o in pieces.ok("get_scene")["objects"]]
    assert names == ["Body"]
    mesh = pieces.ok("get_mesh", object="Body")
    for face in mesh["faces"]:
        expected_x = {0: 0.0, 1: 3.0, 2: 6.0}[face["material"]]
        assert abs(face["center"][0] - expected_x) < 0.6, face
    document, binary = glb(pieces.ok("export_glb", path=str(tmp_path / "joined.glb"))["path"])
    assert sorted(p["material"] for p in primitives(document, binary)) == ["Blue", "Green", "Red"]
    meshes = pieces.ok("run_python", code="result = sorted(m.name for m in bpy.data.meshes)")["result"]
    assert meshes == ["Body"], "the pieces' meshes were left behind"


def test_join_keeps_the_pieces_children_where_they_are(pieces):
    pieces.ok("create_object", kind="empty", name="Handle", parent="Lid", location=[0, 0, 1])
    pieces.ok("join_objects", objects=["Body", "Lid"])
    handle = pieces.ok("get_object", object="Handle")
    assert handle["parent"] == "Body" and close(handle["world_location"], [3, 0, 1])


def test_join_objects_refused_changes_nothing(pieces, tmp_path):
    pieces.ok("create_object", kind="empty", name="Pivot")
    pieces.ok("duplicate_object", object="Lid", name="Twin", linked=True, offset=[0, 3, 0])
    pieces.ok("add_modifier", object="Knob", type="bevel")
    pieces.ok("save_file", path=str(tmp_path / "pieces.blend"))
    refused_unchanged(pieces, "join_objects", "bad_argument", objects=["Body"])
    refused_unchanged(pieces, "join_objects", "bad_argument", objects=["Body", "Pivot"])
    refused_unchanged(pieces, "join_objects", "bad_argument", objects=["Body", "Body"])
    refused_unchanged(pieces, "join_objects", "not_found", objects=["Body", "Ghost"])
    payload = refused_unchanged(pieces, "join_objects", "bad_argument", objects=["Lid", "Body"])
    assert "Twin" in payload["error"], "the error does not name Twin, which shares the target's mesh"
    payload = refused_unchanged(pieces, "join_objects", "bad_argument", objects=["Body", "Knob"])
    assert "apply_modifier" in payload["hint"]


def test_a_mirrored_piece_joins_with_its_faces_pointing_out(blender):
    blender.ok("create_object", kind="cube", name="Body", size=1)
    blender.ok("create_object", kind="cube", name="Lid", size=1, location=[0, 0, 3], scale=[1, 1, -1])
    blender.ok("duplicate_object", object="Lid", name="Twin", linked=True, offset=[3, 0, 0])
    out = blender.ok("join_objects", objects=["Body", "Lid"])
    assert "Lid" in out["note"], out
    held = shape(blender, "Body")
    assert held["closed"] and held["volume"] == pytest.approx(2.0, abs=1e-4), \
        "the joined cubes do not both point outward"
    found = {i["code"] for i in blender.ok("check_asset", objects=["Body"])["objects"][0]["issues"]}
    assert "inside_out" not in found and "flipped_normals" not in found, found
    assert shape(blender, "Twin")["volume"] == pytest.approx(1.0, abs=1e-4), "the mesh it shared was changed"


def test_pieces_whose_uv_maps_have_other_names_keep_their_uvs(blender):
    blender.ok("create_object", kind="cube", name="Body", size=1)
    blender.ok("create_object", kind="cube", name="Lid", size=1, location=[0, 0, 2])
    blender.ok("run_python", code="bpy.data.objects['Lid'].data.uv_layers[0].name = 'UVChannel'")
    out = blender.ok("join_objects", objects=["Body", "Lid"])
    assert out["uv_maps"] == ["UVMap"]
    assert out["uv_renamed"] == {"Lid": {"UVChannel": "UVMap"}}
    lid = blender.ok("run_python", code="""
mesh = bpy.data.objects["Body"].data
layer = mesh.uv_layers["UVMap"].data
result = sorted({tuple(round(c, 4) for c in layer[i].uv) for p in mesh.polygons if p.center.z > 1
                 for i in p.loop_indices})
""")["result"]
    assert len(lid) > 4, f"the lid's faces lost the cube's layout in UVMap: {lid}"


def test_join_objects_in_edit_mode_is_refused(pieces):
    pieces.ok("run_python", code="o = bpy.data.objects['Body']\nbpy.context.view_layer.objects.active = o\n"
                                 "with bpy.context.temp_override(active_object=o, object=o):\n"
                                 "    bpy.ops.object.mode_set(mode='EDIT')")
    payload = pieces.refused("join_objects", "bad_argument", objects=["Body", "Lid"])
    assert "Tab" in payload["hint"]


# ---------------------------------------------------------------------------
# set_material: alpha modes, back faces, vertex colours and images
# ---------------------------------------------------------------------------


def material_in_file(path, name) -> dict:
    """Return the material named ``name`` from a .glb file's JSON document."""
    document, _ = glb(path)
    return next(m for m in document["materials"] if m["name"] == name)


def test_a_material_with_alpha_clip_exports_mask_with_the_cutoff(blender, tmp_path):
    leaf = png_rgba(tmp_path / "leaf.png", [(40, 160, 40, 255), (40, 160, 40, 0)] * 2)
    blender.ok("create_object", kind="plane", name="Leaf")
    out = blender.ok("set_material", material="Leafy", object="Leaf", base_color_image=leaf, alpha_mode="clip",
                     alpha_cutoff=0.3)
    assert out["material"]["alpha_mode"] == "clip" and out["material"]["alpha_cutoff"] == pytest.approx(0.3)
    blender.ok("create_object", kind="cube", name="Fence", location=[3, 0, 0])
    blender.ok("set_vertex_color", object="Fence", color=[0.5, 0.5, 0.5, 0.0], facing="top", use_in_material=False)
    blender.ok("set_material", material="Wire", object="Fence", vertex_color="Col", alpha_mode="clip")
    path = tmp_path / "leaf.glb"
    blender.ok("export_glb", path=str(path))
    written = material_in_file(path, "Leafy")
    assert written["alphaMode"] == "MASK" and written["alphaCutoff"] == pytest.approx(0.3, abs=1e-6)
    assert written["pbrMetallicRoughness"]["baseColorTexture"]["index"] == 0
    wire = material_in_file(path, "Wire")
    assert wire["alphaMode"] == "MASK" and "alphaCutoff" not in wire, \
        "alphaCutoff was written, though 0.5 is glTF's default"


def test_clipping_one_alpha_everywhere_is_refused(scene):
    """Clipping a material whose alpha is one value everywhere is refused.

    The glTF exporter writes MASK only for an alpha that varies, and exports a single clipped value as BLEND.
    """
    payload = refused_unchanged(scene, "set_material", "bad_argument", material="Paint", alpha=0.8, alpha_mode="clip")
    assert "base_color_image" in payload["hint"] and "vertex_color" in payload["hint"]
    refused_unchanged(scene, "set_material", "bad_argument", material="Fresh", alpha_mode="clip", alpha_cutoff=0.2)


def test_an_alpha_value_on_a_clipped_material_is_refused(blender, tmp_path):
    """An alpha value on a clipped material is refused.

    A clipped material's alpha comes from its image or vertex colours, so a value set there would change nothing.
    """
    leaf = png_rgba(tmp_path / "leaf.png", [(40, 160, 40, 255), (40, 160, 40, 0)] * 2)
    blender.ok("create_object", kind="plane", name="Leaf")
    blender.ok("set_material", material="Leafy", object="Leaf", base_color_image=leaf, alpha_mode="clip")
    payload = blender.refused("set_material", "bad_argument", material="Leafy", alpha=0.5)
    assert "blend" in payload["hint"]
    blender.refused("set_material", "bad_argument", material="Leafy", alpha=0.5, alpha_mode="clip")
    out = blender.ok("set_material", material="Leafy", alpha=0.5, alpha_mode="blend")
    assert out["material"]["alpha_mode"] == "blend"


def test_alpha_modes_blend_and_back_to_opaque(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Pane")
    blender.ok("set_material", material="Glass", object="Pane", alpha=0.4, alpha_mode="blend")
    blender.ok("export_glb", path=str(tmp_path / "blend.glb"))
    assert material_in_file(tmp_path / "blend.glb", "Glass")["alphaMode"] == "BLEND"
    out = blender.ok("set_material", material="Glass", alpha_mode="opaque")
    assert out["material"]["alpha_mode"] == "opaque" and out["material"]["alpha"] == 1.0
    blender.ok("export_glb", path=str(tmp_path / "opaque.glb"))
    assert "alphaMode" not in material_in_file(tmp_path / "opaque.glb", "Glass")
    blender.refused("set_material", "bad_argument", material="Glass", alpha_mode="opaque", alpha=0.5)


def test_new_materials_cull_back_faces_unless_asked(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate")
    blender.ok("create_object", kind="plane", name="Card", location=[3, 0, 0])
    made = blender.ok("set_material", material="Wood", object="Crate")
    assert made["material"]["backface_culling"] is True
    blender.ok("set_material", material="Paper", object="Card", backface_culling=False)
    blender.ok("export_glb", path=str(tmp_path / "sides.glb"))
    assert "doubleSided" not in material_in_file(tmp_path / "sides.glb", "Wood")
    assert material_in_file(tmp_path / "sides.glb", "Paper")["doubleSided"] is True
    blender.ok("run_python", code="bpy.data.materials.new('Old').use_backface_culling = False")
    kept = blender.ok("set_material", material="Old", roughness=0.2)
    assert kept["material"]["backface_culling"] is False, "an existing material lost its own backface culling setting"


def test_a_vertex_color_material_exports_color_0_and_can_be_unwired(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate")
    blender.ok("set_vertex_color", object="Crate", color=[1, 0, 0], facing="top", use_in_material=False)
    out = blender.ok("set_material", material="Painted", object="Crate", vertex_color="Col")
    assert out["material"]["vertex_color"] == "Col" and out["material"]["base_color"] == "linked"
    document, binary = glb(blender.ok("export_glb", path=str(tmp_path / "painted.glb"))["path"])
    assert "COLOR_0" in primitives(document, binary)[0]
    out = blender.ok("set_material", material="Painted", vertex_color="")
    assert out["material"]["vertex_color"] is None and out["material"]["base_color"] != "linked"


def test_images_go_through_texture_nodes_into_the_file(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate")
    colour = png_rgba(tmp_path / "colour.png", [(255, 0, 0, 255), (0, 255, 0, 0)] * 2)
    normal = png_rgba(tmp_path / "normal.png", [(128, 128, 255, 255)] * 4)
    glow = png_rgba(tmp_path / "glow.png", [(255, 200, 0, 255)] * 4)
    out = blender.ok("set_material", material="Decal", object="Crate", base_color_image=colour, normal_image=normal,
                     emission_image=glow, alpha_mode="clip")
    images = out["material"]["images"]
    assert {key: Path(value).name for key, value in images.items()} == {
        "base_color": "colour.png", "normal": "normal.png", "emission": "glow.png"}
    path = tmp_path / "decal.glb"
    contents = blender.ok("export_glb", path=str(path))["contents"]
    (written,) = contents["materials"]
    assert written["textures"] == ["baseColorTexture", "emissiveTexture", "normalTexture"]
    assert written["alpha_mode"] == "MASK", "clip did not take the image's alpha as a mask"
    assert contents["images"] == 3
    removed = blender.ok("set_material", material="Decal", normal_image="", emission_image="")
    assert set(removed["material"]["images"]) == {"base_color"}


def test_a_missing_image_or_a_bad_material_value_changes_nothing(scene, tmp_path):
    refused_unchanged(scene, "set_material", "not_found", material="Paint", base_color_image=str(tmp_path / "no.png"))
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not an image at all")
    refused_unchanged(scene, "set_material", "bad_argument", material="Paint", base_color_image=str(junk))
    refused_unchanged(scene, "set_material", "bad_argument", material="Fresh", alpha_mode="cutout")
    refused_unchanged(scene, "set_material", "bad_argument", material="Fresh", alpha_mode="clip", alpha_cutoff=2)
    refused_unchanged(scene, "set_material", "bad_argument", material="Paint", backface_culling="no")
    refused_unchanged(scene, "set_material", "bad_argument", material="Fresh", object="Box", vertex_color="Missing")
    good = png_rgba(tmp_path / "good.png", [(255, 255, 255, 255)] * 4)
    refused_unchanged(scene, "set_material", "bad_argument", material="Fresh", object="Box", base_color_image=good,
                      faces=[99])


# ---------------------------------------------------------------------------
# Nested extras
# ---------------------------------------------------------------------------

NESTED = {
    "hinge": {"pins": [[0, 0.5, 0.25], [0, 0.5, 1]], "side": "back", "flags": [True, False], "empty": {}},
    "tags": ["oak", "antique"],
    "drawers": [{"n": 1, "label": "socks"}, {"n": 2, "label": "shirts"}],
    "plain": 3,
}


def test_a_dict_extra_arrives_nested_in_the_node_extras(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Chest")
    out = blender.ok("set_object", object="Chest", properties=NESTED)
    assert out["properties"] == NESTED
    assert blender.ok("get_object", object="Chest")["properties"] == NESTED
    path = tmp_path / "chest.glb"
    contents = blender.ok("export_glb", path=str(path))["contents"]
    assert contents["nodes"][0]["extras"] == NESTED
    document, _ = glb(path)
    assert document["nodes"][0]["extras"]["hinge"]["pins"][1] == [0, 0.5, 1]


def test_nested_extras_replace_and_remove_and_a_refusal_puts_them_back(scene):
    scene.ok("set_object", object="Box", properties={"lamp": {"offset": [1, 2, 3]}, "keep": 1})
    payload = refused_unchanged(scene, "set_object", "bad_argument", object="Box",
                                properties={"lamp": {"offset": [4, 5, 6]}, "bad": [1, "x"]})
    assert "bad" in payload["error"]
    assert scene.ok("get_object", object="Box")["properties"] == {"lamp": {"offset": [1, 2, 3]}, "keep": 1}
    out = scene.ok("set_object", object="Box", properties={"lamp": None, "keep": {"x": [{"y": 1}]}})
    assert out["properties"] == {"keep": {"x": [{"y": 1}]}}


def nest(depth: int):
    """Return the number 1 wrapped in ``depth`` levels of single-key objects."""
    value = 1
    for _ in range(depth):
        value = {"in": value}
    return value


@pytest.mark.parametrize("value", [
    [1, "a"],
    [True, 1],
    {"a": None},
    {"": 1},
    {"k" * 64: 1},
    [[1, 2], [3, 2 ** 40]],
    {"deep": [float("inf")]},
    nest(9),
    ["x" * 70_000],
], ids=["mixed", "bool-and-number", "nested-null", "empty-key", "long-key", "big-int", "infinite", "too-deep",
        "too-big"])
def test_nested_extras_are_checked_all_the_way_down(scene, value):
    refused_unchanged(scene, "set_object", "bad_argument", object="Box", properties={"ok": 1, "bad": value})


def test_eight_levels_deep_is_accepted(blender):
    blender.ok("create_object", kind="cube", name="Crate")
    assert blender.ok("set_object", object="Crate", properties={"deep": nest(8)})["properties"]["deep"] == nest(8)


# ---------------------------------------------------------------------------
# A worked example: a prop built from pieces with tool calls
# ---------------------------------------------------------------------------


def test_a_prop_built_from_pieces_with_tools(blender, tmp_path):
    """A radio built from four pieces joins into one object that exports with all four materials.

    The pieces are a box body, a grille panel bevelled by angle, a tube handle and a lathe knob, each with its own
    material.
    """
    blender.ok("create_object", kind="cube", name="Body", size=[0.4, 0.15, 0.25], location=[0, 0, 0.125])
    blender.ok("edit_mesh", object="Body", operation="bevel", amount=0.01, angle=50)
    blender.ok("set_material", material="Walnut", object="Body", base_color="#6b4426", roughness=0.5)
    blender.ok("create_object", kind="cube", name="Grille", size=[0.22, 0.01, 0.16], location=[-0.06, -0.08, 0.13])
    blender.ok("edit_mesh", object="Grille", operation="bevel", amount=0.003, angle=50)
    blender.ok("set_material", material="Cloth", object="Grille", base_color="#d9c9a3", roughness=1)
    blender.ok("create_object", kind="tube", name="Handle", radius=0.012,
               points=[[-0.15, 0, 0.25], [-0.1, 0, 0.32], [0.1, 0, 0.32], [0.15, 0, 0.25]])
    blender.ok("set_material", material="Leather", object="Handle", base_color="#4a2e1c", roughness=0.7)
    blender.ok("create_object", kind="lathe", name="Knob", segments=16, location=[0.12, -0.075, 0.15],
               rotation=[90, 0, 0], profile=[[0, 0], [0.025, 0], [0.025, 0.015], [0.02, 0.025], [0, 0.025]])
    blender.ok("set_material", material="Brass", object="Knob", base_color="#b5a642", metallic=1, roughness=0.3)
    radio = blender.ok("join_objects", objects=["Body", "Grille", "Handle", "Knob"])
    blender.ok("edit_mesh", object="Body", operation="shade_smooth", angle=40)
    assert [s["material"] for s in radio["slots"]] == ["Walnut", "Cloth", "Leather", "Brass"]
    assert all(s["faces"] > 0 for s in radio["slots"])
    contents = blender.ok("export_glb", path=str(tmp_path / "radio.glb"))["contents"]
    assert [n["name"] for n in contents["nodes"]] == ["Body"]
    assert sorted(m["name"] for m in contents["materials"]) == ["Brass", "Cloth", "Leather", "Walnut"]
