"""Tests for the targets and options of ``export_glb``, for read-back warnings on real exports, and for ``clean_names``.

They run against a real Blender, and they check the file written to disk, read directly or through the read-back.
"""

import pytest

from blenderlens_mcp.glb import read_json_chunk
from tests.test_files import node_names
from tests.test_refusals_change_nothing import refused_unchanged

pytestmark = pytest.mark.integration


def document(path) -> dict:
    """Return the JSON chunk of the .glb at ``path``."""
    return read_json_chunk(str(path))[0]


def attributes(path) -> set:
    """Return the names of every vertex attribute in the .glb at ``path``, across all its primitives."""
    return {a for mesh in document(path).get("meshes", []) for p in mesh["primitives"] for a in p["attributes"]}


def warning_codes(result) -> dict:
    """Return the read-back warnings of an export's result, keyed by code."""
    return {w["code"]: w for w in result["contents"]["warnings"]}


#: Code that adds a metaball object named NAME, one ball of radius 0.5, at LOCATION.
METABALL = """
ball = bpy.data.metaballs.new(NAME)
ball.elements.new().radius = 0.5
obj = bpy.data.objects.new(NAME, ball)
obj.location = LOCATION
bpy.context.scene.collection.objects.link(obj)
"""


def add_metaball(server, name: str, location=(0, 0, 0)) -> None:
    """Add a metaball object named ``name`` at ``location`` through run_python."""
    server.ok("run_python", code=f"NAME = {name!r}\nLOCATION = {tuple(location)!r}\n{METABALL}")


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


def test_draco_for_godot_is_refused_and_changes_nothing(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    blender.ok("save_file", path=str(tmp_path / "crate.blend"))
    payload = refused_unchanged(blender, "export_glb", "bad_argument", path=str(tmp_path / "crate.glb"),
                                target="godot", draco=True)
    assert "KHR_draco_mesh_compression" in payload["error"] and "Godot" in payload["error"]
    assert not (tmp_path / "crate.glb").exists()


def test_draco_for_three_js_is_required_in_the_file(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    out = blender.ok("export_glb", path=str(tmp_path / "web.glb"), target="threejs", draco=True)
    assert "KHR_draco_mesh_compression" in document(tmp_path / "web.glb")["extensionsRequired"]
    assert out["contents"]["extensions_required"] == ["KHR_draco_mesh_compression"]
    decoder = warning_codes(out)["extension_needs_decoder"]
    assert "DRACOLoader" in decoder["message"]
    assert out["target"] == "threejs"


def test_three_js_leaves_out_godot_collision_proxies(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    blender.ok("create_object", kind="cube", name="Crate-colonly", size=1.1, parent="Crate")
    blender.ok("create_object", kind="cube", name="Lid_convcolonly", size=0.5, location=[0, 0, 1], parent="Crate")
    blender.ok("create_object", kind="cube", name="Wheel_col", size=0.3, location=[2, 0, 0])
    web = blender.ok("export_glb", path=str(tmp_path / "web.glb"), target="threejs")
    assert node_names(web["contents"]) == {"Crate", "Wheel_col"}
    assert sorted(web["left_out"]) == ["Crate-colonly", "Lid_convcolonly"]
    assert web["contents"]["warnings"] == [], "what was left out on purpose was reported as missing"
    godot = blender.ok("export_glb", path=str(tmp_path / "godot.glb"), target="godot")
    assert node_names(godot["contents"]) == {"Crate", "Crate-colonly", "Lid_convcolonly", "Wheel_col"}


def test_lights_and_cameras_only_when_asked(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    blender.ok("run_python", code="""
light = bpy.data.objects.new("Lamp", bpy.data.lights.new("Lamp", "POINT"))
camera = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
for obj in (light, camera):
    bpy.context.scene.collection.objects.link(obj)
""")
    plain = blender.ok("export_glb", path=str(tmp_path / "plain.glb"), target="godot")
    assert node_names(plain["contents"]) == {"Crate"}
    lit = blender.ok("export_glb", path=str(tmp_path / "lit.glb"), target="godot", lights=True, cameras=True)
    assert node_names(lit["contents"]) == {"Crate", "Lamp", "Cam"}
    assert "KHR_lights_punctual" in lit["contents"]["extensions_used"]
    assert document(tmp_path / "lit.glb").get("cameras")


#: Code that feeds the material Paint's base colour from an 8 x 8 grey image.
IMAGE_MATERIAL = """
mat = bpy.data.materials["Paint"]
tree = mat.node_tree
image = bpy.data.images.new("Paint", 8, 8)
image.pixels = [0.5] * (8 * 8 * 4)
node = tree.nodes.new("ShaderNodeTexImage")
node.image = image
bsdf = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
"""


@pytest.mark.parametrize(("target", "image_format", "extension", "images"), [
    ("godot", "webp", "EXT_texture_webp", 1),
    ("threejs", "webp", "EXT_texture_webp", 1),
    ("godot", "none", None, 0),
    ("generic", "jpeg", None, 1),
])
def test_image_formats(blender, tmp_path, target, image_format, extension, images):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    blender.ok("set_material", material="Paint", object="Crate")
    blender.ok("run_python", code=IMAGE_MATERIAL)
    out = blender.ok("export_glb", path=str(tmp_path / "img.glb"), target=target, image_format=image_format)
    assert out["contents"]["images"] == images
    if extension:
        assert extension in document(tmp_path / "img.glb").get("extensionsRequired", [])
    if images:
        width = out["contents"]["estimates"]["textures"][0]["width"]
        assert width == 8, out["contents"]["estimates"]


#: Code that gives the Crate's mesh a red colour attribute on every face corner.
COLOUR_ATTRIBUTE = """
mesh = bpy.data.objects["Crate"].data
attribute = mesh.color_attributes.new("Col", "BYTE_COLOR", "CORNER")
for item in attribute.data:
    item.color = (1.0, 0.0, 0.0, 1.0)
"""


@pytest.mark.parametrize(("vertex_colors", "present"), [("material", False), ("active", True), ("none", False)])
def test_vertex_colours_are_exported_only_as_asked(blender, tmp_path, vertex_colors, present):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    blender.ok("set_material", material="Plain", object="Crate")
    blender.ok("run_python", code=COLOUR_ATTRIBUTE)
    out = blender.ok("export_glb", path=str(tmp_path / "vc.glb"), vertex_colors=vertex_colors)
    assert ("COLOR_0" in attributes(tmp_path / "vc.glb")) is present
    assert out["contents"]["vertex_colors"] is present
    assert ("vertex_colors" in warning_codes(out)) is present


def test_tangents_when_asked(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    blender.ok("export_glb", path=str(tmp_path / "plain.glb"))
    assert "TANGENT" not in attributes(tmp_path / "plain.glb")
    blender.ok("export_glb", path=str(tmp_path / "tangents.glb"), tangents=True)
    assert "TANGENT" in attributes(tmp_path / "tangents.glb")


#: Code that keys the Rotor's rotation about Z from 0 at frame 1 to half a turn at frame 24.
SPIN = """
obj = bpy.data.objects["Rotor"]
obj.rotation_euler = (0, 0, 0)
obj.keyframe_insert("rotation_euler", frame=1)
obj.rotation_euler = (0, 0, 3.14159)
obj.keyframe_insert("rotation_euler", frame=24)
"""


def test_animations_can_be_left_out(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Rotor", size=1)
    blender.ok("run_python", code=SPIN)
    assert blender.ok("export_glb", path=str(tmp_path / "a.glb"))["contents"]["animations"] == 1
    assert blender.ok("export_glb", path=str(tmp_path / "b.glb"), animations=False)["contents"]["animations"] == 0


def node(path, name) -> dict:
    """Return the node named ``name`` in the .glb at ``path``, which must hold exactly one."""
    [found] = [n for n in document(path)["nodes"] if n["name"] == name]
    return found


def test_at_origin_exports_the_root_at_the_origin_and_leaves_it_where_it_stands(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Radio", size=[0.4, 0.15, 0.25], location=[-2, -1, 0.5],
               rotation=[0, 0, 20])
    blender.ok("create_object", kind="empty", name="Dial", parent="Radio", location=[0.1, 0, 0])
    blender.ok("save_file", path=str(tmp_path / "radio.blend"))
    where = blender.ok("get_object", object="Radio")
    loose = blender.ok("export_glb", path=str(tmp_path / "loose.glb"), objects=["Radio"])
    off = warning_codes(loose)["root_off_origin"]
    assert off["severity"] == "warning" and "Radio" in off["message"] and "at_origin" in off["message"]
    assert off["translation"] == pytest.approx([-2, 0.5, 1], abs=1e-4), \
        "the root's translation is not the object's location in glTF's Y-up axes"

    out = blender.ok("export_glb", path=str(tmp_path / "radio.glb"), objects=["Radio"], at_origin=True)
    assert out["options"]["at_origin"] is True and "root_off_origin" not in warning_codes(out)
    assert node(tmp_path / "radio.glb", "Radio").get("translation", [0, 0, 0]) == pytest.approx([0, 0, 0], abs=1e-6)
    assert "rotation" in node(tmp_path / "radio.glb", "Radio"), "the rotation was left out with the location"
    assert node(tmp_path / "radio.glb", "Dial")["translation"] == pytest.approx([0.1, 0, 0], abs=1e-6)
    assert blender.ok("get_object", object="Radio") == where, "the export changed the scene"
    assert blender.ok("get_scene")["unsaved_changes"] is False


def test_at_origin_puts_each_root_and_a_child_exported_alone_at_the_origin(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Body", size=1, location=[3, 2, 1])
    blender.ok("create_object", kind="cube", name="Lid", size=0.5, location=[0, 0, 1], parent="Body")
    blender.ok("create_object", kind="cube", name="Other", size=1, location=[-4, 0, 0])
    apart = blender.ok("export_glb", path=str(tmp_path / "apart.glb"), objects=["Body", "Other"])
    assert "root_off_origin" not in warning_codes(apart), \
        "two roots, a layout, were warned of as one asset off its origin"
    blender.ok("export_glb", path=str(tmp_path / "both.glb"), objects=["Body", "Other"], at_origin=True)
    for name in ("Body", "Other"):
        assert node(tmp_path / "both.glb", name).get("translation", [0, 0, 0]) == pytest.approx([0, 0, 0], abs=1e-6)
    blender.ok("export_glb", path=str(tmp_path / "lid.glb"), objects=["Lid"], at_origin=True)
    assert node(tmp_path / "lid.glb", "Lid").get("translation", [0, 0, 0]) == pytest.approx([0, 0, 0], abs=1e-6)
    assert blender.ok("get_object", object="Lid")["world_location"] == pytest.approx([3, 2, 2])


def test_at_origin_is_refused_for_a_root_whose_location_is_animated(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Door", size=1, location=[2, 0, 0])
    blender.ok("animate_object", object="Door", clip="Open", keys=[{"frame": 0, "location": [2, 0, 0]},
                                                                   {"frame": 10, "location": [2, 0, 2]}])
    blender.ok("save_file", path=str(tmp_path / "door.blend"))
    payload = refused_unchanged(blender, "export_glb", "bad_argument", path=str(tmp_path / "door.glb"),
                                at_origin=True)
    assert "Door" in payload["error"] and "animat" in payload["error"]
    assert not (tmp_path / "door.glb").exists()


def test_texcoords_false_leaves_the_uvs_out_unless_a_texture_needs_them(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    blender.ok("set_material", material="Paint", object="Crate", base_color="#808080")
    out = blender.ok("export_glb", path=str(tmp_path / "plain.glb"), texcoords=False)
    assert "TEXCOORD_0" not in attributes(tmp_path / "plain.glb") and out["options"]["texcoords"] is False
    blender.ok("export_glb", path=str(tmp_path / "uv.glb"))
    assert "TEXCOORD_0" in attributes(tmp_path / "uv.glb")
    image = tmp_path / "wood.png"
    from tests.test_checks import png_bytes
    image.write_bytes(png_bytes())
    blender.ok("set_material", material="Paint", base_color_image=str(image))
    blender.ok("save_file", path=str(tmp_path / "crate.blend"))
    payload = refused_unchanged(blender, "export_glb", "bad_argument", path=str(tmp_path / "t.glb"), texcoords=False)
    assert "Paint" in payload["error"]


def test_an_unknown_target_or_option_is_refused(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    for key, value in (("target", "unity"), ("image_format", "tiff"), ("vertex_colors", "all")):
        payload = blender.refused("export_glb", "bad_argument", path=str(tmp_path / "x.glb"), **{key: value})
        assert value in payload["error"] and "does not take" in payload["error"], payload


# ---------------------------------------------------------------------------
# Read-back warnings on real exports
# ---------------------------------------------------------------------------


def test_an_object_named_chair_col_warns_of_a_godot_collision_hint(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Chair_col", size=0.5)
    out = blender.ok("export_glb", path=str(tmp_path / "chair.glb"), target="godot")
    [row] = warning_codes(out)["godot_name_hint"]["names"]
    assert row["name"] == "Chair_col" and row["hint"] == "col"
    assert "godot_name_hint" not in warning_codes(
        blender.ok("export_glb", path=str(tmp_path / "web.glb"), target="threejs"))


def test_shape_keys_dropped_by_a_modifier_are_warned_about(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Blob", size=1)
    blender.ok("run_python", code="obj = bpy.data.objects['Blob']\nobj.shape_key_add(name='Basis')\n"
                                  "obj.shape_key_add(name='Squash')")
    blender.ok("add_modifier", object="Blob", type="bevel", settings={"width": 0.05})
    out = blender.ok("export_glb", path=str(tmp_path / "blob.glb"))
    assert warning_codes(out)["morphs_dropped"]["objects"] == ["Blob"]
    kept = blender.ok("export_glb", path=str(tmp_path / "kept.glb"), apply_modifiers=False)
    assert "morphs_dropped" not in warning_codes(kept)
    assert any(p.get("targets") for m in document(tmp_path / "kept.glb")["meshes"] for p in m["primitives"])


def test_extras_carry_the_godot_version_note(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Lamp", size=1)
    blender.ok("set_object", object="Lamp", properties={"brightness": 0.5})
    note = warning_codes(blender.ok("export_glb", path=str(tmp_path / "lamp.glb"), target="godot"))["godot_version"]
    assert "4.4" in note["message"]


# ---------------------------------------------------------------------------
# Metaballs, which the glTF export writes as nodes with no mesh
# ---------------------------------------------------------------------------


def test_an_export_of_a_metaball_alone_is_refused_and_writes_nothing(blender, tmp_path):
    """export_glb refuses a scene whose only object is a metaball, since the export would hold no surface."""
    add_metaball(blender, "Bubble")
    blender.ok("save_file", path=str(tmp_path / "bubble.blend"))
    payload = refused_unchanged(blender, "export_glb", "not_found", path=str(tmp_path / "bubble.glb"))
    assert "metaball" in payload["hint"], payload
    assert not (tmp_path / "bubble.glb").exists(), "the refused export wrote a file"


def test_a_metaball_exported_beside_a_mesh_is_warned_of_beside_the_read_back(blender, tmp_path):
    """A metaball exported beside a mesh is named in a surface_not_exported warning in the result.

    The add-on gives the warning in the result's own ``warnings``, and the server keeps it there beside the
    read-back it adds in ``contents``.
    """
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    add_metaball(blender, "Bubble", location=[3, 0, 0])
    out = blender.ok("export_glb", path=str(tmp_path / "crate.glb"))
    found = [w for w in out.get("warnings", []) if w["code"] == "surface_not_exported"]
    assert len(found) == 1, f"the result has no single surface_not_exported warning: {out.get('warnings')}"
    assert found[0]["objects"] == ["Bubble"] and found[0]["count"] == 1, found
    assert "metaball" in found[0]["message"], found
    assert out["contents"]["valid"] is True, out["contents"]
    assert "Crate" in node_names(out["contents"]), "the mesh is not in the file"
    assert "surface_not_exported" not in warning_codes(out), "the read-back repeats the add-on's warning"


# ---------------------------------------------------------------------------
# clean_names
# ---------------------------------------------------------------------------


def test_pieces_under_two_roots_both_come_out_as_piece(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Left", size=1)
    blender.ok("create_object", kind="cube", name="Right", size=1, location=[3, 0, 0])
    blender.ok("create_object", kind="cube", name="Piece", size=0.2, location=[0, 0, 10])
    blender.ok("create_object", kind="cube", name="Piece", size=0.2, location=[0, 0, 1], parent="Left")
    blender.ok("create_object", kind="cube", name="Piece", size=0.2, location=[0, 0, 1], parent="Right")
    blender.ok("delete_object", objects=["Piece"])
    out = blender.ok("export_glb", path=str(tmp_path / "pieces.glb"), target="godot")
    roots = {node["name"]: node for node in out["contents"]["nodes"]}
    assert [child["name"] for child in roots["Left"]["children"]] == ["Piece"]
    assert [child["name"] for child in roots["Right"]["children"]] == ["Piece"]
    assert out["renamed"] == {"Piece.001": "Piece", "Piece.002": "Piece"}
    assert sorted(out["objects"]) == ["Left", "Piece", "Piece", "Right"], "the result does not name the file's nodes"
    assert "missing_objects" not in warning_codes(out)
    renamed = warning_codes(out)["renamed_on_import"]
    assert {row["as"] for row in renamed["godot"]} == {"Piece2"}, \
        "the warning does not give Piece2, the unique name Godot gives the second Piece"
    blend = blender.ok("get_scene")
    assert {o["name"] for o in blend["objects"]} == {"Left", "Right", "Piece.001", "Piece.002"}, \
        "the export renamed objects in the .blend"


def test_names_that_would_collide_under_one_parent_keep_their_suffix(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Body", size=1)
    for _ in range(2):
        blender.ok("create_object", kind="cube", name="Bolt.001", size=0.1, parent="Body")
    blender.ok("create_object", kind="cube", name="Lid.003", size=0.5, location=[0, 0, 1], parent="Body")
    blender.ok("set_material", material="Steel.001", object="Lid.003")
    out = blender.ok("export_glb", path=str(tmp_path / "body.glb"), target="godot")
    children = sorted(child["name"] for child in out["contents"]["nodes"][0]["children"])
    assert children == ["Bolt.001", "Bolt.002", "Lid"]
    assert [m["name"] for m in out["contents"]["materials"]] == ["Steel"]
    raw = blender.ok("export_glb", path=str(tmp_path / "raw.glb"), target="godot", clean_names=False)
    assert "Lid.003" in node_names(raw["contents"])
    generic = blender.ok("export_glb", path=str(tmp_path / "generic.glb"))
    assert "Lid.003" in node_names(generic["contents"]), \
        "the generic export cleaned the names, though clean_names is on by default for godot alone"


def test_clean_names_keeps_the_file_valid_and_the_hidden_extras_out(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Fan.004", size=1)
    blender.ok("run_python", code="bpy.data.objects['Fan.004']['_RNA_UI'] = {'speeds': {'min': 1}}\n"
                                  "bpy.data.objects['Fan.004']['speeds'] = 3")
    out = blender.ok("export_glb", path=str(tmp_path / "fan.glb"), target="godot")
    assert out["contents"]["nodes"][0] == {"name": "Fan", "mesh": "Fan", "extras": {"speeds": 3}}
    assert b"_RNA_UI" not in (tmp_path / "fan.glb").read_bytes()
    blender.ok("create_file", discard=True)
    imported = blender.ok("import_file", path=str(tmp_path / "fan.glb"))
    assert [o["name"] for o in imported["objects"]] == ["Fan"]


def test_a_cleaned_name_never_repeats_a_name_under_its_parent(blender, tmp_path):
    blender.ok("create_object", kind="empty", name="Root")
    for name in ("B", "B.001", "B.001.001"):
        blender.ok("create_object", kind="cube", name=name, size=0.5, parent="Root")
    out = blender.ok("export_glb", path=str(tmp_path / "b.glb"), target="godot")
    nodes = document(tmp_path / "b.glb")["nodes"]
    [root] = [n for n in nodes if n["name"] == "Root"]
    children = sorted(nodes[i]["name"] for i in root["children"])
    assert len(set(children)) == 3, children
    assert "B.001.001" not in out.get("renamed", {}), out.get("renamed")


def test_shape_keys_on_one_of_two_cleaned_names_are_found_on_their_own_node(blender, tmp_path):
    blender.ok("create_object", kind="empty", name="Left")
    blender.ok("create_object", kind="empty", name="Right", location=[3, 0, 0])
    blender.ok("create_object", kind="cube", name="Pad.001", size=0.5, parent="Left")
    blender.ok("create_object", kind="cube", name="Pad.002", size=0.5, parent="Right")
    blender.ok("run_python", code="obj = bpy.data.objects['Pad.002']\nobj.shape_key_add(name='Basis')\n"
                                  "key = obj.shape_key_add(name='Squash')\n"
                                  "for point in key.data: point.co.z *= 0.5")
    out = blender.ok("export_glb", path=str(tmp_path / "pads.glb"), target="godot")
    assert out["renamed"] == {"Pad.001": "Pad", "Pad.002": "Pad"}
    assert "morphs_dropped" not in warning_codes(out), warning_codes(out).get("morphs_dropped")
    nodes = document(tmp_path / "pads.glb")["nodes"]
    assert out["morph_targets"]["Pad.002"]["count"] == 1
    assert nodes[out["morph_targets"]["Pad.002"]["node"]]["name"] == "Pad"


def test_a_scene_of_collection_instances_exports(blender, tmp_path):
    blender.ok("run_python", code="""
coll = bpy.data.collections.new("Props")
mesh = bpy.data.meshes.new("C")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1)
bm.to_mesh(mesh)
bm.free()
coll.objects.link(bpy.data.objects.new("C", mesh))
holder = bpy.data.objects.new("PropInstance", None)
holder.instance_type = "COLLECTION"
holder.instance_collection = coll
bpy.context.scene.collection.objects.link(holder)
""")
    out = blender.ok("export_glb", path=str(tmp_path / "instanced.glb"))
    assert out["contents"]["triangles"] == 12, out["contents"]


def test_a_note_says_when_image_format_none_loses_the_vertex_colours(blender, tmp_path):
    """The result carries a note exactly when an export with ``image_format`` none leaves the vertex colours out.

    The glTF exporter may then write no COLOR_0, because it does not trace the base colour back to the colour
    attribute, so the note names COLOR_0 and the option to change.
    """
    from tests.test_modelling import png_rgba

    blender.ok("create_object", kind="cube", name="Crate")
    blender.ok("set_material", material="Crate", object="Crate",
               base_color_image=png_rgba(tmp_path / "wood.png", [(150, 100, 60, 255)] * 4))
    blender.ok("set_vertex_color", object="Crate", color=[1, 0, 0], facing="top")
    out = blender.ok("export_glb", path=str(tmp_path / "crate.glb"), image_format="none")
    lost = "COLOR_0" not in attributes(tmp_path / "crate.glb")
    assert ("note" in out) is lost, out
    if lost:
        assert "COLOR_0" in out["note"] and "image_format" in out["note"], out
    kept = blender.ok("export_glb", path=str(tmp_path / "kept.glb"))
    assert "COLOR_0" in attributes(tmp_path / "kept.glb") and "note" not in kept
