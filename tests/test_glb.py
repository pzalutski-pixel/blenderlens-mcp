"""Tests for the .glb reader, against files built byte by byte here.

The ``contents`` that ``export_glb`` returns comes from this reader, so the reader must report what a file
holds and refuse anything that is not a well-formed binary glTF 2.0 file.
"""

import json
import struct

import pytest

from blenderlens_mcp.glb import GLBError, read_json_chunk, summarize


def build_glb(document: dict, binary: bytes = b"", *, magic=b"glTF", version=2, lie_about_length=0) -> bytes:
    """Return a binary glTF file holding ``document`` as its JSON chunk and ``binary`` as its BIN chunk.

    ``magic`` and ``version`` go into the header as given, and ``lie_about_length`` is added to the total length
    the header states, so a test can build a malformed file.
    """
    body = json.dumps(document).encode("utf-8")
    body += b" " * (-len(body) % 4)
    binary += b"\x00" * (-len(binary) % 4)
    chunks = struct.pack("<II", len(body), 0x4E4F534A) + body
    if binary:
        chunks += struct.pack("<II", len(binary), 0x004E4942) + binary
    total = 12 + len(chunks)
    return struct.pack("<4sII", magic, version, total + lie_about_length) + chunks


TRIANGLE = {
    "asset": {"version": "2.0", "generator": "hand"},
    "scene": 0,
    "scenes": [{"nodes": [0]}],
    "nodes": [
        {"name": "Lamp", "children": [1], "translation": [0, 0.5, 0], "extras": {"bulbs": 1}},
        {"name": "Shade", "mesh": 0},
    ],
    "meshes": [{"name": "ShadeMesh", "primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]}],
    "materials": [{
        "name": "Glow",
        "pbrMetallicRoughness": {"baseColorFactor": [0.9, 0.85, 0.7, 1], "metallicFactor": 0, "roughnessFactor": 0.8},
        "emissiveFactor": [1, 0.9, 0.6],
        "extensions": {"KHR_materials_emissive_strength": {"emissiveStrength": 3}},
    }],
    "accessors": [
        {"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3", "min": [0, 0, 0], "max": [1, 1, 0]},
        {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"},
    ],
    "bufferViews": [{"buffer": 0, "byteLength": 36}, {"buffer": 0, "byteOffset": 36, "byteLength": 6}],
    "buffers": [{"byteLength": 44}],
    "extensionsUsed": ["KHR_materials_emissive_strength"],
}


def test_a_triangle_with_an_emissive_material(tmp_path):
    path = tmp_path / "t.glb"
    path.write_bytes(build_glb(TRIANGLE, b"\x00" * 42))
    summary = summarize(str(path))
    assert summary["valid"] is True
    assert summary["triangles"] == 1
    assert summary["nodes"] == [{"name": "Lamp", "translation": [0, 0.5, 0], "extras": {"bulbs": 1},
                                 "children": [{"name": "Shade", "mesh": "ShadeMesh"}]}]
    glow = summary["materials"][0]
    assert glow["emissive"] is True
    assert glow["emissive_strength"] == 3
    assert glow["roughness"] == 0.8
    assert summary["meshes"][0]["materials"] == ["Glow"]
    assert summary["meshes"][0]["bounds_yup"] == {"min": [0, 0, 0], "max": [1, 1, 0]}
    assert summary["binary_bytes"] == 44
    assert summary["extensions_used"] == ["KHR_materials_emissive_strength"]


def test_material_values_are_read_back_to_four_decimals(tmp_path):
    """Material values are rounded to four decimals, because the exporter writes 32-bit floats.

    A value of 0.6 comes back as 0.6000000238418579, and the extra digits carry no information.
    """
    doc = json.loads(json.dumps(TRIANGLE))
    doc["materials"][0]["pbrMetallicRoughness"].update({"metallicFactor": 0.6000000238418579,
                                                         "roughnessFactor": 0.41999998688697815})
    doc["materials"][0]["extensions"]["KHR_materials_emissive_strength"]["emissiveStrength"] = 2.5000001
    path = tmp_path / "noise.glb"
    path.write_bytes(build_glb(doc, b"\x00" * 42))
    glow = summarize(str(path))["materials"][0]
    assert (glow["metallic"], glow["roughness"], glow["emissive_strength"]) == (0.6, 0.42, 2.5)


def test_a_black_emissive_does_not_count_as_light(tmp_path):
    doc = json.loads(json.dumps(TRIANGLE))
    doc["materials"][0]["emissiveFactor"] = [0, 0, 0]
    path = tmp_path / "dark.glb"
    path.write_bytes(build_glb(doc, b"\x00" * 42))
    assert summarize(str(path))["materials"][0]["emissive"] is False


def test_unindexed_geometry_counts_from_positions(tmp_path):
    doc = json.loads(json.dumps(TRIANGLE))
    del doc["meshes"][0]["primitives"][0]["indices"]
    path = tmp_path / "u.glb"
    path.write_bytes(build_glb(doc, b"\x00" * 42))
    assert summarize(str(path))["triangles"] == 1


def test_a_primitive_without_positions_is_read(tmp_path):
    """A primitive without positions is read, and has no bounds of its own.

    glTF allows such a primitive, because some KHR extensions supply positions another way.
    """
    doc = json.loads(json.dumps(TRIANGLE))
    doc["accessors"].append({"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"})
    doc["meshes"][0]["primitives"][0]["attributes"] = {"NORMAL": 2}
    path = tmp_path / "nopos.glb"
    path.write_bytes(build_glb(doc, b"\x00" * 42))
    summary = summarize(str(path))
    assert summary["triangles"] == 1, "the triangles are not counted from the indices"
    assert summary["meshes"][0]["bounds_yup"] is None
    del doc["meshes"][0]["primitives"][0]["indices"]
    path.write_bytes(build_glb(doc, b"\x00" * 42))
    assert summarize(str(path))["triangles"] == 1, "the triangles are not counted from the attribute the primitive has"


def stack_of_three(tmp_path):
    """Write a .glb with three nodes under one root sharing one mesh, as linked duplicates export, and an unused mesh.

    Return the file's path.
    """
    doc = json.loads(json.dumps(TRIANGLE))
    doc["nodes"] = [{"name": "Stack", "children": [1, 2, 3]}, {"name": "Piece_1", "mesh": 0},
                    {"name": "Piece_2", "mesh": 0, "translation": [1, 0, 0]},
                    {"name": "Piece_3", "mesh": 0, "translation": [2, 0, 0]}]
    doc["meshes"].append({"name": "Unused", "primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]})
    path = tmp_path / "stack.glb"
    path.write_bytes(build_glb(doc, b"\x00" * 42))
    return str(path)


def test_a_mesh_shared_by_three_nodes_draws_its_triangles_three_times(tmp_path):
    summary = summarize(stack_of_three(tmp_path))
    assert summary["meshes"][0]["triangles"] == 1, "the mesh's own triangle count was multiplied by the nodes using it"
    assert summary["triangles"] == 3, \
        "the total does not count the mesh once per node using it, or counts a mesh no node uses"
    assert summary["estimates"]["draw_calls"] == 3


def test_the_check_command_counts_the_triangles_the_read_back_counts(tmp_path, capsys):
    from blenderlens_mcp import cli

    path = stack_of_three(tmp_path)
    rules = tmp_path / "budget.json"
    rules.write_text(json.dumps({"budget": 2}), encoding="utf-8")
    assert cli.main(["check", path, "--rules", str(rules), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["contents"]["triangles"] == 3
    [over] = [i for i in report["issues"] if i["code"] == "over_budget"]
    assert over["triangles"] == {"all": 3}


@pytest.mark.parametrize(("mode", "count", "triangles"), [
    (4, 9, 3),  # A list of separate triangles.
    (5, 5, 3),  # A strip: one triangle per vertex after the first two.
    (6, 5, 3),  # A fan: likewise, one triangle per vertex after the first two.
    (5, 2, 0),
    (0, 9, 0),  # Points draw no triangles.
    (1, 8, 0),  # Lines draw no triangles.
])
def test_triangles_are_counted_for_lists_strips_and_fans(tmp_path, mode, count, triangles):
    doc = json.loads(json.dumps(TRIANGLE))
    doc["accessors"][1]["count"] = count
    doc["meshes"][0]["primitives"][0]["mode"] = mode
    path = tmp_path / "modes.glb"
    path.write_bytes(build_glb(doc, b"\x00" * 42))
    assert summarize(str(path))["triangles"] == triangles


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"magic": b"NOPE"}, "magic"),
    ({"version": 1}, "version 1"),
    ({"lie_about_length": 100}, "truncated"),
])
def test_malformed_files_are_refused(tmp_path, kwargs, message):
    path = tmp_path / "bad.glb"
    path.write_bytes(build_glb(TRIANGLE, b"\x00" * 42, **kwargs))
    with pytest.raises(GLBError, match=message):
        read_json_chunk(str(path))


def test_a_file_too_short_to_hold_a_header(tmp_path):
    path = tmp_path / "short.glb"
    path.write_bytes(b"glTF")
    with pytest.raises(GLBError, match="too short"):
        read_json_chunk(str(path))


# ---------------------------------------------------------------------------
# Expected objects: the objects an export was asked for, checked against the file
# ---------------------------------------------------------------------------


def test_every_expected_object_found_means_no_warnings(tmp_path):
    doc = json.loads(json.dumps(TRIANGLE))
    del doc["nodes"][0]["translation"]  # The root is at the origin, so root_off_origin does not apply.
    path = tmp_path / "t.glb"
    path.write_bytes(build_glb(doc, b"\x00" * 42))
    assert summarize(str(path), expected=["Lamp", "Shade"])["warnings"] == []
    assert summarize(str(path))["warnings"] == []


def test_a_single_root_off_the_origin_is_a_warning(tmp_path):
    path = tmp_path / "t.glb"
    path.write_bytes(build_glb(TRIANGLE, b"\x00" * 42))
    [off] = summarize(str(path))["warnings"]
    assert off["code"] == "root_off_origin" and off["severity"] == "warning"
    assert off["node"] == "Lamp" and off["translation"] == [0, 0.5, 0]
    doc = json.loads(json.dumps(TRIANGLE))
    doc["nodes"][0] = {"name": "Lamp", "children": [1], "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 2, 0, 0, 1]}
    path.write_bytes(build_glb(doc, b"\x00" * 42))
    assert summarize(str(path))["warnings"][0]["translation"] == [2, 0, 0], \
        "the translation of a root placed by a matrix is not reported"
    doc["scenes"][0]["nodes"] = [0, 1]
    doc["nodes"][0]["children"] = []
    path.write_bytes(build_glb(doc, b"\x00" * 42))
    assert summarize(str(path))["warnings"] == [], "several roots, a layout, were warned of as one asset off its origin"


def test_an_expected_object_missing_from_the_file_is_a_warning(tmp_path):
    path = tmp_path / "t.glb"
    path.write_bytes(build_glb(TRIANGLE, b"\x00" * 42))
    warnings = [w for w in summarize(str(path), expected=["Lamp", "Cutter", "Shade", "Door"])["warnings"]
                if w["code"] != "root_off_origin"]
    assert len(warnings) == 1
    assert warnings[0]["code"] == "missing_objects"
    assert warnings[0]["objects"] == ["Cutter", "Door"]
    assert "Cutter" in warnings[0]["message"] and "Door" in warnings[0]["message"]


def test_a_node_outside_the_scene_tree_does_not_count_as_exported(tmp_path):
    doc = json.loads(json.dumps(TRIANGLE))
    doc["nodes"].append({"name": "Orphan"})
    path = tmp_path / "orphan.glb"
    path.write_bytes(build_glb(doc, b"\x00" * 42))
    warnings = summarize(str(path), expected=["Lamp", "Orphan"])["warnings"]
    assert warnings[0]["objects"] == ["Orphan"]


# ---------------------------------------------------------------------------
# Structural faults raise GLBError, never another exception
# ---------------------------------------------------------------------------


def raw_glb(json_bytes: bytes, extra: bytes = b"") -> bytes:
    """Return a .glb file around ``json_bytes`` exactly as given, valid JSON or not, followed by ``extra``."""
    json_bytes += b" " * (-len(json_bytes) % 4)
    body = struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes + extra
    return b"glTF" + struct.pack("<II", 2, 12 + len(body)) + body


def doc_bytes(document) -> bytes:
    """Return ``document`` as JSON in UTF-8."""
    return json.dumps(document).encode("utf-8")


#: Files with structural faults, which the reader must refuse with GLBError rather than another exception.
FAULTS = {
    "bad utf-8": raw_glb(b"\xff\xfe{}"),
    "a json list": raw_glb(b"[1,2]"),
    "not json": raw_glb(b"{nope"),
    "nan": raw_glb(b'{"asset": {"version": "2.0"}, "nodes": [{"name": "a", "translation": [NaN, 0, 0]}]}'),
    "infinity": raw_glb(b'{"nodes": [{"name": "a", "scale": [1e999, 1, 1]}], "scenes": [{"nodes": [0]}]}'),
    "deep nesting": raw_glb(b"[" * 100000 + b"]" * 100000),
    "accessor out of range": raw_glb(doc_bytes({"meshes": [{"primitives": [{"attributes": {"POSITION": 5}}]}]})),
    "negative accessor": raw_glb(doc_bytes({"accessors": [{"count": 3}],
                                            "meshes": [{"primitives": [{"attributes": {"POSITION": -1}}]}]})),
    "material out of range": raw_glb(doc_bytes({
        "accessors": [{"count": 3, "min": [0, 0, 0], "max": [1, 1, 1]}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "material": 7}]}]})),
    "node cycle": raw_glb(doc_bytes({"nodes": [{"name": "a", "children": [0]}], "scenes": [{"nodes": [0]}]})),
    "longer cycle": raw_glb(doc_bytes({"nodes": [{"name": "a", "children": [1]}, {"name": "b", "children": [0]}],
                                       "scenes": [{"nodes": [0]}]})),
    "shared child": raw_glb(doc_bytes({"nodes": [{"children": [2]}, {"children": [2]}, {"name": "c"}],
                                       "scenes": [{"nodes": [0, 1]}]})),
    "scene out of range": raw_glb(doc_bytes({"scene": 3, "scenes": [{"nodes": []}]})),
    "string min": raw_glb(doc_bytes({"accessors": [{"count": 3, "min": "x", "max": [1, 1, 1]}],
                                     "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}]})),
    "short min": raw_glb(doc_bytes({"accessors": [{"count": 3, "min": [0], "max": [1, 1, 1]}],
                                    "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}]})),
    "string count": raw_glb(doc_bytes({"accessors": [{"count": "3"}],
                                       "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}]})),
    "nodes not a list": raw_glb(doc_bytes({"nodes": {"0": {}}, "scenes": [{"nodes": [0]}]})),
    "a node not an object": raw_glb(doc_bytes({"nodes": [3], "scenes": [{"nodes": [0]}]})),
    "mesh index a string": raw_glb(doc_bytes({"nodes": [{"mesh": "0"}], "meshes": [{}], "scenes": [{"nodes": [0]}]})),
    "attributes not an object": raw_glb(doc_bytes({"meshes": [{"primitives": [{"attributes": [0]}]}]})),
    "asset not an object": raw_glb(doc_bytes({"asset": "2.0"})),
    "extensions not an object": raw_glb(doc_bytes({"materials": [{"extensions": [1]}]})),
    "emissive a string": raw_glb(doc_bytes({"materials": [{"emissiveFactor": "red"}]})),
    "chunk past the end": b"glTF" + struct.pack("<II", 2, 28) + struct.pack("<II", 400, 0x4E4F534A) + b"{}  ",
    # Python refuses to convert more than 4300 digits into an int, and raises a ValueError of its own.
    "a huge integer literal": raw_glb(b'{"nodes": [{"name": "a", "translation": [' + b"9" * 5000 + b", 0, 0]}],"
                                      b' "scenes": [{"nodes": [0]}]}'),
    "a huge count": raw_glb(b'{"accessors": [{"count": ' + b"1" * 4400 + b"}]}"),
}


@pytest.mark.parametrize("name", list(FAULTS))
def test_a_structural_fault_is_a_glb_error_naming_the_file(tmp_path, name):
    path = tmp_path / "fault.glb"
    path.write_bytes(FAULTS[name])
    with pytest.raises(GLBError) as caught:
        summarize(str(path))
    assert str(path) in str(caught.value)


def test_fuzzed_files_raise_glb_error_or_read_cleanly(tmp_path):
    """Truncations of a good file, and hundreds of byte mutations of it, either read cleanly or raise GLBError."""
    import random

    good = build_glb(TRIANGLE, b"\x00" * 42)
    path = tmp_path / "fuzz.glb"
    candidates = [good[:cut] for cut in range(0, len(good), 5)]
    rng = random.Random(1)
    body_start = 20  # Past the file header and the JSON chunk's header, so the mutations land in the JSON.
    for _ in range(400):
        data = bytearray(good)
        for _ in range(rng.randint(1, 4)):
            spot = rng.randrange(body_start, len(data))
            data[spot] = rng.choice([rng.randrange(256), ord("["), ord("{"), ord("-"), ord("9"), ord('"'), ord(",")])
        candidates.append(bytes(data))
    outcomes = {"read": 0, "refused": 0}
    for data in candidates:
        path.write_bytes(data)
        try:
            summary = summarize(str(path), expected=["Lamp"])
        except GLBError as exc:
            assert str(path) in str(exc)
            outcomes["refused"] += 1
            continue
        json.dumps(summary, allow_nan=False)  # Whatever was read must be plain JSON with finite numbers.
        outcomes["read"] += 1
    assert outcomes["refused"] > len(good) // 5, outcomes
