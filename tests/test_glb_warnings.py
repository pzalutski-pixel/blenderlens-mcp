"""Tests for the read-back's warnings, against files built byte by byte here.

The warnings say what Godot and three.js will do with a file, judged from the file alone. The facts they state
come from the source code of Godot's glTF importer and of three.js's GLTFLoader.
"""

import json
import struct
import zlib

import pytest

from blenderlens_mcp.glb import GLBError, summarize
from tests.test_glb import TRIANGLE, build_glb


def doc(**changes) -> dict:
    """Return a copy of the test triangle's document, with top-level ``changes`` applied."""
    out = json.loads(json.dumps(TRIANGLE))
    out.update(changes)
    return out


def write(tmp_path, document, binary=b"\x00" * 42, name="w.glb") -> str:
    """Write ``document`` and ``binary`` as a .glb named ``name``, and return its path."""
    path = tmp_path / name
    path.write_bytes(build_glb(document, binary))
    return str(path)


def warnings(path, **kwargs) -> dict:
    """Return the warnings ``summarize`` gives for ``path``, keyed by code, after checking each one's fields."""
    out = {}
    for warning in summarize(path, **kwargs)["warnings"]:
        assert set(warning) >= {"code", "severity", "message"}, warning
        assert warning["severity"] in ("error", "warning", "info"), warning
        out.setdefault(warning["code"], warning)
    return out


def nodes_named(*names, parents=None) -> dict:
    """Return a glTF document whose scene holds nodes with these names, each showing the triangle mesh.

    ``parents`` maps a child's index to its parent's index, and the nodes without a parent are the scene's roots.
    """
    parents = parents or {}
    nodes = [{"name": name, "mesh": 0} for name in names]
    for child, parent in parents.items():
        nodes[parent].setdefault("children", []).append(child)
    roots = [i for i in range(len(names)) if i not in parents]
    return doc(nodes=nodes, scenes=[{"name": "Scene", "nodes": roots}])


# ---------------------------------------------------------------------------
# Extensions
# ---------------------------------------------------------------------------


def test_a_required_extension_godot_cannot_load_is_fatal_for_godot(tmp_path):
    path = write(tmp_path, doc(extensionsUsed=["KHR_draco_mesh_compression"],
                               extensionsRequired=["KHR_draco_mesh_compression"]))
    godot = warnings(path, target="godot")
    assert godot["extension_not_supported"]["severity"] == "error"
    assert godot["extension_not_supported"]["extensions"] == ["KHR_draco_mesh_compression"]
    three = warnings(path, target="threejs")
    assert "extension_not_supported" not in three
    assert three["extension_needs_decoder"]["extensions"] == ["KHR_draco_mesh_compression"]
    assert "DRACOLoader" in three["extension_needs_decoder"]["message"]
    both = warnings(path, target="generic")
    assert {"extension_not_supported", "extension_needs_decoder"} <= set(both)


def test_an_extension_used_but_not_required_is_ignored_where_unsupported(tmp_path):
    path = write(tmp_path, doc(extensionsUsed=["KHR_materials_emissive_strength", "KHR_materials_transmission"]))
    godot = warnings(path, target="godot")
    assert godot["extension_ignored"]["extensions"] == ["KHR_materials_transmission"]
    assert "extension_ignored" not in warnings(path, target="threejs")


def test_three_js_does_not_load_spec_gloss_or_node_visibility(tmp_path):
    path = write(tmp_path, doc(extensionsUsed=["KHR_materials_pbrSpecularGlossiness", "KHR_node_visibility"]))
    three = warnings(path, target="threejs")
    assert three["extension_ignored"]["extensions"] == ["KHR_materials_pbrSpecularGlossiness", "KHR_node_visibility"]


def test_node_visibility_needs_godot_4_5(tmp_path):
    path = write(tmp_path, doc(extensionsUsed=["KHR_node_visibility"], extensionsRequired=["KHR_node_visibility"]))
    note = warnings(path, target="godot")["godot_version"]
    assert "4.5" in note["message"] and "KHR_node_visibility" in note["message"]


def test_extras_need_godot_4_4(tmp_path):
    path = write(tmp_path, doc())  # The triangle document's root node carries extras.
    note = warnings(path, target="godot")["godot_version"]
    assert "4.4" in note["message"] and note["severity"] == "info"
    assert "godot_version" not in warnings(path, target="threejs")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "hint"), [
    ("Chair_col", "col"), ("Chair-col", "col"), ("Chair_col.001", "col"), ("Fence-convcol", "convcol"),
    ("Table_colonly", "colonly"), ("Table-convcolonly", "convcolonly"), ("Crate-rigid", "rigid"),
    ("Lamp-noimp", "noimp"), ("Floor_navmesh", "navmesh"), ("Wall_occ", "occ"), ("Wall-occonly", "occonly"),
    ("Car-vehicle", "vehicle"), ("steering_wheel", "wheel"), ("Front_Wheel_2", "wheel"), ("Gear$col", "col"),
])
def test_a_node_name_that_triggers_a_godot_import_hint(tmp_path, name, hint):
    path = write(tmp_path, nodes_named(name))
    found = warnings(path, target="godot")["godot_name_hint"]
    [row] = found["names"]
    assert row["name"] == name and row["hint"] == hint and row["on"] == "node"


@pytest.mark.parametrize("name", ["Pistol", "Collar", "Wheelbarrow", "Flywheel", "Colonnade", "Rigidity", "Body"])
def test_names_that_only_look_like_hints_do_not_warn(tmp_path, name):
    path = write(tmp_path, nodes_named(name))
    assert "godot_name_hint" not in warnings(path, target="godot")


def test_material_mesh_and_animation_names_are_tested_too(tmp_path):
    document = nodes_named("Window")
    document["materials"][0]["name"] = "Glass_alpha"
    document["meshes"][0]["name"] = "Window_col"
    document["animations"] = [{"name": "Idle-loop", "channels": [], "samplers": []},
                              {"name": "Spin_cycle", "channels": [], "samplers": []},
                              {"name": "Open", "channels": [], "samplers": []}]
    found = warnings(write(tmp_path, document), target="godot")
    hints = {(row["on"], row["hint"]) for row in found["godot_name_hint"]["names"]}
    assert hints == {("material", "alpha"), ("mesh", "col")}
    loops = found["animation_loops_by_name"]
    assert loops["animations"] == ["Idle-loop", "Spin_cycle"]


def test_hint_warnings_are_godots_alone(tmp_path):
    assert "godot_name_hint" not in warnings(write(tmp_path, nodes_named("Chair_col")), target="threejs")


def test_a_hinted_node_with_non_uniform_or_negative_scale(tmp_path):
    document = nodes_named("Wall_col", "Table_colonly", "Plain")
    document["nodes"][0]["scale"] = [2, 1, 1]
    document["nodes"][1]["scale"] = [-1, 1, 1]
    document["nodes"][2]["scale"] = [3, 1, 1]
    scaled = warnings(write(tmp_path, document), target="godot")["scaled_hint_node"]
    assert scaled["nodes"] == ["Wall_col", "Table_colonly"]


def test_the_names_godot_and_three_js_will_use_with_collisions(tmp_path):
    document = nodes_named("Left", "Piece", "Right", "Piece", "Gear.001", "Scene", parents={1: 0, 3: 2})
    renamed = warnings(write(tmp_path, document), target="generic")["renamed_on_import"]
    godot = {row["index"]: row["as"] for row in renamed["godot"]}
    three = {row["index"]: row["as"] for row in renamed["threejs"]}
    assert godot == {3: "Piece2", 4: "Gear_001"}
    assert three == {3: "Piece_1", 4: "Gear001", 5: "Scene_1"}
    assert renamed["collisions"]["godot"] == ["Piece"]
    assert sorted(renamed["collisions"]["threejs"]) == ["Piece", "Scene"]


def test_no_rename_means_no_warning(tmp_path):
    assert "renamed_on_import" not in warnings(write(tmp_path, nodes_named("A", "B")), target="generic")


# ---------------------------------------------------------------------------
# Materials and meshes
# ---------------------------------------------------------------------------


def test_a_material_with_an_emissive_colour_and_strength_is_emissive(tmp_path):
    glow = summarize(write(tmp_path, doc()))["materials"][0]
    assert glow["emissive"] is True


def test_double_sided_materials_are_counted(tmp_path):
    document = doc()
    document["materials"][0]["doubleSided"] = True
    path = write(tmp_path, document)
    summary = summarize(path)
    assert summary["materials"][0]["double_sided"] is True
    assert warnings(path)["double_sided"]["materials"] == ["Glow"]


def test_vertex_colours_and_the_godot_4_7_bug(tmp_path):
    document = doc()
    document["accessors"].append({"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC4"})
    document["meshes"][0]["primitives"][0]["attributes"]["COLOR_0"] = 2
    path = write(tmp_path, document)
    assert summarize(path)["vertex_colors"] is True
    found = warnings(path, target="godot")["vertex_colors"]
    assert "4.7.0" in found["message"] and "4.7.2" in found["message"]
    assert "4.7" not in warnings(path, target="threejs")["vertex_colors"]["message"]


def test_a_normal_map_without_tangents(tmp_path):
    document = doc()
    document["materials"][0]["normalTexture"] = {"index": 0}
    document["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_0"] = 0
    document["textures"] = [{"source": 0}]
    document["images"] = [{"uri": "normal.png"}]
    found = warnings(write(tmp_path, document), target="generic")
    assert found["normal_map_without_tangents"]["materials"] == ["Glow"]


def test_a_texture_on_a_primitive_without_texcoord_0(tmp_path):
    document = doc()
    document["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
    document["textures"] = [{"source": 0}]
    document["images"] = [{"uri": "base.png"}]
    found = warnings(write(tmp_path, document))
    assert found["texture_without_uv"]["meshes"] == ["ShadeMesh"]


def test_morph_targets_dropped_by_the_export(tmp_path):
    path = write(tmp_path, nodes_named("Blob"))
    dropped = warnings(path, morphs={"Blob": 2})["morphs_dropped"]
    assert dropped["objects"] == ["Blob"] and dropped["severity"] == "warning"
    document = nodes_named("Blob")
    document["meshes"][0]["primitives"][0]["targets"] = [{"POSITION": 0}]
    assert "morphs_dropped" not in warnings(write(tmp_path, document, name="kept.glb"), morphs={"Blob": 1})


def test_morph_targets_are_looked_for_on_the_node_the_export_named(tmp_path):
    """Shape keys are looked for on the node the export named, even when two nodes share a cleaned name.

    Here both nodes are named Pad. The second node's mesh has a morph target and the first node's has none, so
    shape keys on the second node's object are kept, and shape keys on the first node's object are dropped.
    """
    document = nodes_named("Pad", "Pad")
    document["meshes"].append(json.loads(json.dumps(document["meshes"][0])))
    document["meshes"][1]["primitives"][0]["targets"] = [{"POSITION": 0}]
    document["nodes"][1]["mesh"] = 1
    path = write(tmp_path, document)
    assert "morphs_dropped" not in warnings(path, morphs={"Pad.002": {"node": 1, "count": 1}})
    dropped = warnings(path, morphs={"Pad.001": {"node": 0, "count": 1}})["morphs_dropped"]
    assert dropped["objects"] == ["Pad"]


# ---------------------------------------------------------------------------
# Estimates
# ---------------------------------------------------------------------------


def png(width: int, height: int) -> bytes:
    """Return the start of a PNG, enough for its size to be read: the signature, the IHDR chunk and IEND."""
    def chunk(kind, data):
        """Return one PNG chunk: its length, its kind, its data and its CRC."""
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IEND", b""))


def jpeg(width: int, height: int) -> bytes:
    """Return the start of a JPEG, enough for its size to be read: SOI, an APP0 segment, SOF0 and EOI."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF" + sof + b"\xff\xd9"


def test_draw_calls_textures_and_vram_are_estimated(tmp_path):
    image_a, image_b = png(512, 256), jpeg(64, 32)
    binary = b"\x00" * 44 + image_a + image_b
    document = nodes_named("A", "B")
    document["meshes"][0]["primitives"].append(dict(document["meshes"][0]["primitives"][0]))
    document["bufferViews"] += [{"buffer": 0, "byteOffset": 44, "byteLength": len(image_a)},
                                {"buffer": 0, "byteOffset": 44 + len(image_a), "byteLength": len(image_b)}]
    document["buffers"][0]["byteLength"] = len(binary)
    document["images"] = [{"bufferView": 2, "mimeType": "image/png", "name": "Albedo"},
                          {"bufferView": 3, "mimeType": "image/jpeg"}]
    estimates = summarize(write(tmp_path, document, binary))["estimates"]
    assert estimates["draw_calls"] == 4, "the draw calls are not two nodes times two primitives"
    assert estimates["textures"] == [{"name": "Albedo", "mime": "image/png", "width": 512, "height": 256},
                                     {"name": None, "mime": "image/jpeg", "width": 64, "height": 32}]
    pixels = 512 * 256 + 64 * 32
    assert estimates["texture_pixels"] == pixels
    assert estimates["vram_bytes"] == {"uncompressed": pixels * 4 * 4 // 3, "compressed": pixels * 4 // 3}


def test_an_unreadable_image_is_counted_without_a_size(tmp_path):
    binary = b"\x00" * 44 + b"garbage!"
    document = doc()
    document["bufferViews"].append({"buffer": 0, "byteOffset": 44, "byteLength": 8})
    document["images"] = [{"bufferView": 2, "mimeType": "image/png"}, {"uri": "outside.png"}]
    estimates = summarize(write(tmp_path, document, binary))["estimates"]
    assert [t["width"] for t in estimates["textures"]] == [None, None]
    document["images"].append({"bufferView": 99})
    with pytest.raises(GLBError, match="bufferView"):
        summarize(write(tmp_path, document, binary, name="broken.glb"))


def test_the_description_for_rules_is_robust_to_fuzzed_files(tmp_path):
    """On fuzzed files, ``describe`` either returns plain JSON or raises GLBError, as ``summarize`` does.

    The check command reads a .glb through ``describe``.
    """
    import random

    from blenderlens_mcp.glb import describe

    document = nodes_named("A", "B", parents={1: 0})
    document["nodes"][1]["rotation"] = [0, 0.7071, 0, 0.7071]
    good = build_glb(document, b"\x00" * 42)
    path = tmp_path / "fuzz.glb"
    rng = random.Random(7)
    outcomes = {"read": 0, "refused": 0}
    for _ in range(300):
        data = bytearray(good)
        for _ in range(rng.randint(1, 4)):
            spot = rng.randrange(20, len(data))
            data[spot] = rng.choice([rng.randrange(256), ord("["), ord("{"), ord("-"), ord("9"), ord('"'), ord(",")])
        path.write_bytes(bytes(data))
        try:
            json.dumps(describe(str(path)), allow_nan=False)
            json.dumps(summarize(str(path), target="generic"), allow_nan=False)
            outcomes["read"] += 1
        except GLBError:
            outcomes["refused"] += 1
    assert outcomes["refused"] and outcomes["read"], outcomes


def test_a_warning_lists_at_most_32_names_with_a_count(tmp_path):
    names = [f"Chair{i}_col" for i in range(50)]
    found = warnings(write(tmp_path, nodes_named(*names)), target="godot")["godot_name_hint"]
    assert found["count"] == 50 and len(found["names"]) == 32
