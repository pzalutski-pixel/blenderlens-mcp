"""Tests for ``check_asset``, run against a real Blender.

Most tests plant one issue in a small mesh and check that it is found and reported with the face indices that
``edit_mesh`` and ``set_material`` take. A clean cube passes, and a project's own rules are applied.
"""

import json
import struct
import zlib

import pytest

from tests.test_export_targets import add_metaball

pytestmark = pytest.mark.integration

#: Code that moves a mesh up so its origin sits on its base, as a game asset's origin should.
ON_ITS_BASE = "bpy.data.objects[{name!r}].data.transform(Matrix.Translation((0, 0, {lift})))"


def issues_of(report: dict, name: str) -> dict:
    """Return the issues of one object in a ``check_asset`` result, keyed by issue code."""
    [row] = [o for o in report["objects"] if o["name"] == name]
    return {issue["code"]: issue for issue in row["issues"]}


def asset_issues(report: dict) -> dict:
    """Return the issues of the whole asset in a ``check_asset`` result, keyed by issue code."""
    return {issue["code"]: issue for issue in report["issues"]}


def png_bytes() -> bytes:
    """Return a 2 x 2 RGB PNG, built byte by byte."""
    def chunk(kind, data):
        """Return one PNG chunk: its length, its kind, its data and its CRC."""
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * 2 for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def textured(blender, name: str, image: str) -> None:
    """Give the object ``name`` a material whose base colour comes from an image texture.

    ``image`` is Python code, run in Blender, that sets a variable named ``image`` to the image to use.
    """
    blender.ok("set_material", material=f"{name}Mat", object=name)
    blender.ok("run_python", code=f"""
{image}
mat = bpy.data.materials["{name}Mat"]
tree = mat.node_tree
node = tree.nodes.new("ShaderNodeTexImage")
node.image = image
bsdf = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
""")


# ---------------------------------------------------------------------------
# A clean asset
# ---------------------------------------------------------------------------


def test_a_clean_cube_on_its_base_passes_with_nothing_to_say(blender):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    blender.ok("run_python", code=ON_ITS_BASE.format(name="Crate", lift=0.5))
    report = blender.ok("check_asset", objects=["Crate"])
    assert report["passed"] is True
    assert report["issues"] == []
    [row] = report["objects"]
    assert row["issues"] == [], row
    assert row["stats"]["triangles"] == 12 and row["stats"]["faces"] == 6
    assert row["stats"]["size"] == [1.0, 1.0, 1.0]
    assert report["counts"] == {"errors": 0, "warnings": 0}


def test_every_object_is_checked_when_none_are_named(blender):
    blender.ok("create_object", kind="cube", name="A", size=1)
    blender.ok("create_object", kind="empty", name="Marker", location=[3, 0, 0])
    report = blender.ok("check_asset")
    assert sorted(o["name"] for o in report["objects"]) == ["A", "Marker"]


# ---------------------------------------------------------------------------
# Each generic issue, planted
# ---------------------------------------------------------------------------


def test_non_manifold_edges_and_their_faces(blender):
    # Three triangles share the edge from vertex 0 to vertex 1, a fin that no solid has.
    blender.ok("create_mesh", name="Fin", vertices=[[0, 0, 0], [1, 0, 0], [0.5, 1, 0], [0.5, -1, 0], [0.5, 0, 1]],
               faces=[[0, 1, 2], [0, 3, 1], [0, 1, 4]], recalc_normals=False)
    found = issues_of(blender.ok("check_asset", objects=["Fin"]), "Fin")
    assert found["non_manifold"]["faces"] == [0, 1, 2]
    assert found["non_manifold"]["edges"] == 1


def test_boundary_edges_of_an_open_plane(blender):
    blender.ok("create_object", kind="plane", name="Sheet")
    found = issues_of(blender.ok("check_asset", objects=["Sheet"]), "Sheet")
    assert found["boundary_edges"]["edges"] == 4 and found["boundary_edges"]["faces"] == [0]
    assert found["boundary_edges"]["severity"] == "warning"


def test_loose_vertices_and_edges(blender):
    blender.ok("create_mesh", name="Stray", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [5, 5, 5], [6, 0, 0], [7, 0, 0]],
               faces=[[0, 1, 2]], edges=[[4, 5]])
    loose = issues_of(blender.ok("check_asset", objects=["Stray"]), "Stray")["loose_geometry"]
    assert loose["vertices"] == 1 and loose["edges"] == 1 and loose["count"] == 2


def test_zero_area_faces(blender):
    blender.ok("create_mesh", name="Sliver", faces=[[0, 1, 2], [3, 4, 5]], recalc_normals=False,
               vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [2, 0, 0], [3, 0, 0], [4, 0, 0]])
    found = issues_of(blender.ok("check_asset", objects=["Sliver"]), "Sliver")
    assert found["zero_area_faces"]["faces"] == [1]


def test_duplicate_vertices(blender):
    blender.ok("create_mesh", name="Twins", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 0, 0], [2, 0, 0], [1, 1, 0]],
               faces=[[0, 1, 2], [3, 4, 5]])
    found = issues_of(blender.ok("check_asset", objects=["Twins"]), "Twins")
    assert found["duplicate_vertices"]["vertices"] == 1
    assert found["duplicate_vertices"]["faces"] == [0, 1]


def test_ngons(blender):
    blender.ok("create_object", kind="circle", name="Disc", fill=True, segments=12)
    found = issues_of(blender.ok("check_asset", objects=["Disc"]), "Disc")
    assert found["ngons"]["faces"] == [0]


def test_a_flipped_face_is_named_by_the_index_edit_mesh_takes(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    blender.ok("edit_mesh", object="Box", operation="flip_normals", faces=[2])
    report = blender.ok("check_asset", objects=["Box"])
    flipped = issues_of(report, "Box")["flipped_normals"]
    assert flipped["faces"] == [2] and flipped["severity"] == "error"
    assert report["passed"] is False
    # The index is the one edit_mesh takes, so flipping that face back clears the issue.
    blender.ok("edit_mesh", object="Box", operation="flip_normals", faces=flipped["faces"])
    assert "flipped_normals" not in issues_of(blender.ok("check_asset", objects=["Box"]), "Box")


def test_a_mesh_turned_inside_out(blender):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    blender.ok("edit_mesh", object="Box", operation="flip_normals")
    found = issues_of(blender.ok("check_asset", objects=["Box"]), "Box")
    assert found["inside_out"]["count"] == 6
    assert "flipped_normals" not in found


def test_an_open_surface_facing_down_is_not_called_flipped(blender):
    blender.ok("create_object", kind="plane", name="Ceiling", rotation=[180, 0, 0])
    found = issues_of(blender.ok("check_asset", objects=["Ceiling"]), "Ceiling")
    assert "flipped_normals" not in found and "inside_out" not in found


def test_a_mesh_crossing_itself(blender):
    # Two quads crossing at right angles, sharing no vertex.
    blender.ok("create_mesh", name="Cross",
               vertices=[[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0],
                         [0, -1, -1], [0, 1, -1], [0, 1, 1], [0, -1, 1]],
               faces=[[0, 1, 2, 3], [4, 5, 6, 7]], recalc_normals=False)
    found = issues_of(blender.ok("check_asset", objects=["Cross"]), "Cross")
    assert found["self_intersecting"]["faces"] == [0, 1]


@pytest.mark.parametrize(("scale", "codes"), [(2, {"unapplied_scale"}), ([-1, 1, 1], {"unapplied_scale",
                                                                                        "negative_scale"})])
def test_unapplied_and_negative_scale(blender, scale, codes):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    blender.ok("set_transform", object="Box", scale=scale)
    found = issues_of(blender.ok("check_asset", objects=["Box"]), "Box")
    assert codes <= set(found)
    assert "inside_out" not in found and "flipped_normals" not in found, \
        "negative scale was also reported as inside_out or flipped_normals"


def test_the_origin_of_a_root_not_at_its_base(blender):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    found = issues_of(blender.ok("check_asset", objects=["Box"]), "Box")
    assert found["origin_not_at_base"]["severity"] == "warning"


def test_no_uv_map(blender):
    blender.ok("create_mesh", name="Bare", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])
    assert "no_uv" in issues_of(blender.ok("check_asset", objects=["Bare"]), "Bare")


def test_uvs_stacked_on_each_other_and_outside_the_square(blender):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    blender.ok("run_python", code="""
mesh = bpy.data.objects["Box"].data
for poly in mesh.polygons:
    for i, loop in enumerate(poly.loop_indices):
        u, v = ((0, 0), (1, 0), (1, 1), (0, 1))[i]
        mesh.uv_layers.active.data[loop].uv = (u * 0.5 + (2.0 if poly.index == 5 else 0.0), v * 0.5)
""")
    found = issues_of(blender.ok("check_asset", objects=["Box"]), "Box")
    assert found["uv_overlap"]["faces"] == [0, 1, 2, 3, 4]
    assert found["uv_out_of_bounds"]["faces"] == [5]
    for code in ("uv_overlap", "uv_out_of_bounds"):
        assert found[code]["severity"] == "info", \
            "a UV issue is more than info, though no material maps an image through these UVs"
    blender.ok("set_material", material="Paint", object="Box", base_color="#808080")
    found = issues_of(blender.ok("check_asset", objects=["Box"]), "Box")
    assert found["uv_overlap"]["severity"] == "info", \
        "a UV issue is more than info, though a plain colour maps no image"
    textured(blender, "Box", 'image = bpy.data.images.new("Gen", 4, 4)')
    report = blender.ok("check_asset", objects=["Box"])
    found = issues_of(report, "Box")
    assert found["uv_overlap"]["severity"] == found["uv_out_of_bounds"]["severity"] == "warning"


def test_the_cube_primitive_unwraps_without_overlap(blender):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    found = issues_of(blender.ok("check_asset", objects=["Box"]), "Box")
    assert "uv_overlap" not in found and "uv_out_of_bounds" not in found and "no_uv" not in found


def test_a_texture_on_a_mesh_with_no_uv_map(blender):
    blender.ok("create_mesh", name="Bare", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]])
    textured(blender, "Bare", 'image = bpy.data.images.new("Gen", 4, 4)')
    report = blender.ok("check_asset", objects=["Bare"])
    found = issues_of(report, "Bare")
    assert found["texture_without_uv"]["severity"] == "error" and found["texture_without_uv"]["faces"] == [0]
    assert report["passed"] is False


def test_image_paths_absolute_and_missing(blender, tmp_path):
    png = tmp_path / "rust.png"
    png.write_bytes(png_bytes())
    blender.ok("create_object", kind="cube", name="Box", size=1)
    textured(blender, "Box", f"image = bpy.data.images.load({str(png)!r})")
    found = issues_of(blender.ok("check_asset", objects=["Box"]), "Box")
    assert found["absolute_texture_path"]["severity"] == "warning"
    assert "missing_image" not in found
    blender.ok("create_object", kind="cube", name="Lost", size=1, location=[3, 0, 0])
    textured(blender, "Lost", "image = bpy.data.images.new('Gone', 2, 2)\nimage.source = 'FILE'\n"
                              f"image.filepath = {str(tmp_path / 'gone.png')!r}")
    missing = issues_of(blender.ok("check_asset", objects=["Lost"]), "Lost")["missing_image"]
    assert missing["severity"] == "error" and missing["faces"] == [0, 1, 2, 3, 4, 5]


def test_a_procedural_material_exports_flat(blender):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    blender.ok("set_material", material="BoxMat", object="Box")
    blender.ok("run_python", code="""
tree = bpy.data.materials["BoxMat"].node_tree
noise = tree.nodes.new("ShaderNodeTexNoise")
bsdf = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
tree.links.new(noise.outputs["Color"], bsdf.inputs["Base Color"])
""")
    found = issues_of(blender.ok("check_asset", objects=["Box"]), "Box")
    assert found["procedural_material"]["material"] == "BoxMat"


def test_shape_keys_that_the_modifiers_will_drop_on_export(blender):
    blender.ok("create_object", kind="cube", name="Blob", size=1)
    blender.ok("run_python", code="obj = bpy.data.objects['Blob']\nobj.shape_key_add(name='Basis')\n"
                                  "obj.shape_key_add(name='Squash')")
    blender.ok("add_modifier", object="Blob", type="bevel", settings={"width": 0.05})
    found = issues_of(blender.ok("check_asset", objects=["Blob"]), "Blob")
    assert found["morphs_dropped_by_modifiers"]["shape_keys"] == 1


def test_over_the_triangle_budget(blender):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    report = blender.ok("check_asset", objects=["Box"], budget=10)
    over = asset_issues(report)["over_budget"]
    assert over["severity"] == "error" and over["triangles"] == {"all": 12}
    assert report["passed"] is False
    assert blender.ok("check_asset", objects=["Box"], budget=12)["issues"] == []


def test_objects_crossing_each_other(blender):
    blender.ok("create_object", kind="cube", name="A", size=1)
    blender.ok("create_object", kind="cube", name="B", size=1, location=[0.5, 0.2, 0.1])
    blender.ok("create_object", kind="cube", name="Far", size=1, location=[5, 0, 0])
    overlaps = asset_issues(blender.ok("check_asset"))["overlaps"]
    assert overlaps["pairs"] == [["A", "B"]]


def test_modifiers_are_checked_as_exported_unless_asked_otherwise(blender):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    blender.ok("add_modifier", object="Box", type="bevel", settings={"width": 0.05, "segments": 2})
    evaluated = blender.ok("check_asset", objects=["Box"])
    assert evaluated["evaluated"] is True
    [row] = evaluated["objects"]
    assert row["stats"]["triangles"] > 12 and row["faces_of"] == "evaluated"
    raw = blender.ok("check_asset", objects=["Box"], evaluated=False)["objects"][0]
    assert raw["stats"]["triangles"] == 12 and raw["faces_of"] == "mesh"


def test_face_lists_are_bounded_with_a_count(blender):
    blender.ok("create_object", kind="grid", name="Field", subdivisions=40)
    boundary = issues_of(blender.ok("check_asset", objects=["Field"]), "Field")["boundary_edges"]
    assert boundary["count"] == 156 and len(boundary["faces"]) == 32 and boundary["edges"] == 160


def test_check_asset_sees_an_edit_in_progress_and_leaves_edit_mode_alone(blender):
    from tests.test_edit_mode import enter, mode

    blender.ok("create_object", kind="cube", name="Box", size=1)
    enter(blender, "Box")
    blender.ok("edit_mesh", object="Box", operation="flip_normals", faces=[2])
    found = issues_of(blender.ok("check_asset", objects=["Box"]), "Box")
    assert found["flipped_normals"]["faces"] == [2], "the edit held in edit mode's own mesh was not checked"
    assert mode(blender, "Box") == "EDIT"


def test_check_asset_changes_nothing(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    blender.ok("edit_mesh", object="Box", operation="flip_normals", faces=[1])
    blender.ok("save_file", path=str(tmp_path / "kept.blend"))
    before = blender.ok("get_object", object="Box")
    blender.ok("check_asset", objects=["Box"], rules={"single_root": True})
    assert blender.ok("get_object", object="Box") == before
    assert blender.ok("get_scene")["unsaved_changes"] is False


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_bad_rules_and_unknown_objects_are_refused(blender):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    payload = blender.refused("check_asset", "bad_argument", rules={"requird": ["Box"]})
    assert "requird" in payload["error"] and "required" in payload["error"]
    blender.refused("check_asset", "not_found", objects=["Ghost"])
    blender.refused("check_asset", "bad_argument", budget=-1)


# ---------------------------------------------------------------------------
# A project's rules, end to end
# ---------------------------------------------------------------------------


def test_a_projects_rules_on_a_small_item(blender):
    """A small item passes a full rule set, and fails it once its material leaves the palette and a second root appears.

    The rules ask for one root with a child named ``Seat``, the palette's pine material, a custom property, a size
    range and an origin at the base.
    """
    blender.ok("create_object", kind="cube", name="Stool", size=[0.4, 0.4, 0.45])
    blender.ok("run_python", code=ON_ITS_BASE.format(name="Stool", lift=0.225))
    blender.ok("create_object", kind="empty", name="Seat", parent="Stool")
    blender.ok("set_material", material="Pine", object="Stool", base_color="#d2b48c", metallic=0.0,
               roughness=0.6)
    blender.ok("set_object", object="Stool", properties={"seat_height_m": 0.45})
    rules = {
        "required": ["Stool", "Stool/Seat"],
        "single_root": True,
        "palette": {"Pine": {"base_color": "#d2b48c", "metallic": 0.0, "roughness": 0.6}},
        "palette_only": True,
        "retired": ["re:^Old_"],
        "extras": [{"objects": "Stool", "keys": {"seat_height_m": "number"}}],
        "size": [{"objects": "Stool", "min": [0.35, 0.35, None], "max": [0.45, 0.45, 0.5]}],
        "origin_at_base": True,
        "budget": [{"objects": "Stool", "triangles": 500}],
    }
    report = blender.ok("check_asset", rules=rules)
    assert report["passed"] is True, report
    assert report["issues"] == []

    blender.ok("set_material", material="Pine", roughness=0.9)
    blender.ok("create_object", kind="cube", name="Stray", size=0.1, location=[1, 0, 0])
    found = asset_issues(blender.ok("check_asset", rules=rules))
    assert set(found) >= {"off_palette", "not_single_root"}
    assert found["off_palette"]["materials"] == ["Pine"]


def test_rules_read_from_a_file_check_as_the_same_rules_given_inline(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Stool", size=[0.4, 0.4, 0.45])
    blender.ok("create_object", kind="cube", name="Stray", size=0.1, location=[1, 0, 0])
    rules = {"single_root": True, "required": ["Stool", "Table"]}
    path = tmp_path / "items_rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    from_file = blender.ok("check_asset", rules_file=str(path))
    assert from_file == blender.ok("check_asset", rules=rules)
    assert set(asset_issues(from_file)) == {"not_single_root", "missing_required"}


def test_a_rules_file_that_cannot_be_read_is_refused(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    blender.refused("check_asset", "not_found", rules_file=str(tmp_path / "missing.json"))
    (tmp_path / "broken.json").write_text("{\"single_root\": tru", encoding="utf-8")
    payload = blender.refused("check_asset", "bad_argument", rules_file=str(tmp_path / "broken.json"))
    assert "broken.json" in payload["error"] and "line 1" in payload["error"]
    (tmp_path / "wrong.json").write_text("{\"requird\": []}", encoding="utf-8")
    payload = blender.refused("check_asset", "bad_argument", rules_file=str(tmp_path / "wrong.json"))
    assert "requird" in payload["error"] and "wrong.json" in payload["error"]
    (tmp_path / "ok.json").write_text("{}", encoding="utf-8")
    payload = blender.refused("check_asset", "bad_argument", rules_file=str(tmp_path / "ok.json"), rules={})
    assert "rules_file" in payload["error"]


def test_rules_that_match_none_of_the_objects_checked_are_skipped_when_objects_narrow_the_check(blender):
    """When ``objects`` narrows the check, rules that match none of the checked objects are skipped without a warning.

    One rule set can then serve every item, and checking one item says nothing about the rules for the others.
    """
    blender.ok("create_object", kind="cube", name="Stool", size=[0.4, 0.4, 0.45])
    blender.ok("create_object", kind="cube", name="Shelf", size=[0.8, 0.25, 0.03], location=[1, 0, 0])
    rules = {"size": [{"objects": "Stool", "max": [0.5, 0.5, 0.5]},
                      {"objects": "Shelf", "max": [1.0, 0.3, 0.05]}],
             "extras": [{"objects": "re:^Knob", "keys": {"note": "string"}}],
             "budget": [{"objects": "re:_Stack$", "triangles": 1800}],
             "apart": [{"a": "re:_1$", "b": "re:_[23]$"}]}
    narrowed = blender.ok("check_asset", objects=["Stool"], rules=rules)
    assert narrowed["issues"] == [], narrowed["issues"]
    whole = asset_issues(blender.ok("check_asset", rules=rules))
    assert whole["rule_matches_nothing"]["severity"] == "warning", "checking the whole scene gave no warning"


def test_a_mesh_shared_by_linked_copies_reports_its_own_issues_once(blender):
    blender.ok("create_object", kind="cube", name="Piece_1", size=1)
    blender.ok("edit_mesh", object="Piece_1", operation="flip_normals", faces=[2])
    for i, x in ((2, 2), (3, 4)):
        blender.ok("duplicate_object", object="Piece_1", name=f"Piece_{i}", linked=True, offset=[x, 0, 0])
    report = blender.ok("check_asset")
    first = issues_of(report, "Piece_1")["flipped_normals"]
    assert first["faces"] == [2] and first["also"] == ["Piece_2", "Piece_3"]
    for name in ("Piece_2", "Piece_3"):
        assert "flipped_normals" not in issues_of(report, name)
    assert report["counts"]["errors"] == 1 and report["passed"] is False


def test_sets_that_must_not_intersect_are_measured(blender):
    blender.ok("create_object", kind="cube", name="Cabinet", size=[1, 0.6, 1.2])
    blender.ok("create_object", kind="cube", name="Trim_Top", size=0.3, location=[0.55, 0, 0])
    blender.ok("create_object", kind="cube", name="Trim_Side", size=0.3, location=[-0.55, 0, 0])
    rules = {"apart": [{"a": "re:^Trim_", "b": "Cabinet", "allow": [["Trim_Side", "Cabinet"]]}]}
    crossing = asset_issues(blender.ok("check_asset", rules=rules))["sets_intersect"]
    assert crossing["pairs"] == [["Trim_Top", "Cabinet"]]


# ---------------------------------------------------------------------------
# Bounded work, and the accuracy of each check
# ---------------------------------------------------------------------------

#: Code that lays every face's UVs over the whole unit square, each face on top of every other, as Blender's UV
#: Reset leaves them.
STACKED = """
mesh = bpy.data.objects[{name!r}].data
corners = [(0, 0), (1, 0), (1, 1), (0, 1)]
for poly in mesh.polygons:
    for k, loop in enumerate(poly.loop_indices):
        mesh.uv_layers.active.data[loop].uv = corners[k % 4]
"""

#: Code that runs check_asset inside Blender with the UV overlap test's limit on triangle pairs set to ``bound``,
#: and counts the triangle intersection tests it makes.
BOUNDED_CHECK = """
import sys
checks = sys.modules["blenderlens_addon.checks"]
uv = sys.modules["blenderlens_addon.uv"]
tests = [0]
patched = {{}}
for module in (checks, uv):
    original = getattr(module, "intersect_tri_tri_2d", None)
    if original is not None:
        def counted(*points, original=original):
            tests[0] += 1
            return original(*points)
        patched[module] = original
        module.intersect_tri_tri_2d = counted
kept = uv.MAX_PAIR_TESTS
uv.MAX_PAIR_TESTS = {bound}
try:
    report = checks.check_asset({{"objects": [{name!r}]}})
finally:
    uv.MAX_PAIR_TESTS = kept
    for module, original in patched.items():
        module.intersect_tri_tri_2d = original
result = {{"issues": report["objects"][0]["issues"], "tests": tests[0]}}
"""


def test_uvs_stacked_past_the_pair_bound_say_the_overlap_was_not_all_checked(blender):
    blender.ok("create_object", kind="grid", name="Tiles", subdivisions=4)
    blender.ok("run_python", code=STACKED.format(name="Tiles"))
    found = issues_of(blender.ok("check_asset", objects=["Tiles"]), "Tiles")
    assert found["uv_overlap"]["count"] == 16, "not every stacked face was found under the bound"
    run = blender.ok("run_python", code=BOUNDED_CHECK.format(name="Tiles", bound=100))["result"]
    codes = {issue["code"]: issue for issue in run["issues"]}
    assert "not_checked" in codes and "uv_overlap" in codes["not_checked"]["message"], run["issues"]
    assert run["tests"] <= 100


def test_a_uv_far_outside_the_square_does_not_make_every_face_a_neighbour(blender):
    blender.ok("create_object", kind="grid", name="Sheet", subdivisions=20)
    blender.ok("run_python", code="""
mesh = bpy.data.objects["Sheet"].data
for loop in mesh.polygons[0].loop_indices:
    u, v = mesh.uv_layers.active.data[loop].uv
    mesh.uv_layers.active.data[loop].uv = (u + 60.0, v + 60.0)
""")
    run = blender.ok("run_python", code=BOUNDED_CHECK.format(name="Sheet", bound=4_000_000))["result"]
    codes = {issue["code"]: issue for issue in run["issues"]}
    assert codes["uv_out_of_bounds"]["faces"] == [0]
    assert "uv_overlap" not in codes and "not_checked" not in codes
    # The grid's 400 faces lie side by side, so each triangle is tested against its few neighbours, not against all
    # 800 triangles.
    assert run["tests"] < 20_000, f"{run['tests']} triangle tests for 800 triangles, not only against neighbours"


@pytest.mark.parametrize("how", ["edit_mesh", "modifier"])
def test_a_hollow_closed_shell_faces_its_cavity_and_is_not_inside_out(blender, how):
    blender.ok("create_object", kind="uv_sphere", name="Ball")
    if how == "edit_mesh":
        blender.ok("edit_mesh", object="Ball", operation="solidify", amount=0.1)
    else:
        blender.ok("add_modifier", object="Ball", type="solidify", settings={"thickness": 0.1})
    found = issues_of(blender.ok("check_asset", objects=["Ball"]), "Ball")
    assert "inside_out" not in found and "flipped_normals" not in found, found.get("inside_out")


def test_a_hollow_shell_turned_wholly_is_inside_out_on_its_outer_wall(blender):
    blender.ok("create_object", kind="uv_sphere", name="Ball")
    blender.ok("edit_mesh", object="Ball", operation="solidify", amount=0.1)
    blender.ok("edit_mesh", object="Ball", operation="flip_normals")
    report = blender.ok("check_asset", objects=["Ball"])
    inside_out = issues_of(report, "Ball")["inside_out"]
    assert inside_out["count"] == 512, \
        "inside_out does not count exactly the 512 faces of the outer wall, which point in"
    assert report["passed"] is False


def test_hidden_objects_are_left_out_as_the_export_leaves_them(blender):
    blender.ok("create_object", kind="cube", name="LowPoly", size=1)
    blender.ok("create_object", kind="ico_sphere", name="HighPoly", subdivisions=5, location=[0.2, 0, 0])
    blender.ok("set_object", object="HighPoly", hide=True)
    report = blender.ok("check_asset", budget=5000)
    assert [row["name"] for row in report["objects"]] == ["LowPoly"]
    assert report["triangles"] == 12 and report["passed"] is True, report["issues"]
    assert report["hidden"] == ["HighPoly"]
    named = blender.ok("check_asset", objects=["HighPoly"])
    assert "hidden" in issues_of(named, "HighPoly"), "an object asked for by name is not reported as hidden"


def test_what_a_collection_instance_shows_is_checked(blender):
    blender.ok("run_python", code="""
coll = bpy.data.collections.new("Props")
mesh = bpy.data.meshes.new("BadCube")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1)
bmesh.ops.reverse_faces(bm, faces=list(bm.faces))
bm.to_mesh(mesh)
bm.free()
coll.objects.link(bpy.data.objects.new("BadCube", mesh))
holder = bpy.data.objects.new("PropInstance", None)
holder.instance_type = "COLLECTION"
holder.instance_collection = coll
holder.location = (3, 0, 0)
bpy.context.scene.collection.objects.link(holder)
""")
    report = blender.ok("check_asset")
    [row] = [o for o in report["objects"] if o["name"] == "BadCube"]
    assert row["instanced_by"] == "PropInstance"
    assert {issue["code"]: issue for issue in row["issues"]}["inside_out"]["count"] == 6
    assert report["triangles"] == 12 and report["passed"] is False


def test_text_and_curves_count_in_the_budget(blender):
    blender.ok("create_object", kind="cube", name="Small", size=1)
    blender.ok("run_python", code="""
curve = bpy.data.curves.new("Words", "FONT")
curve.body = "BUDGET"
curve.extrude = 0.1
bpy.context.scene.collection.objects.link(bpy.data.objects.new("Sign", curve))
""")
    report = blender.ok("check_asset", budget=100)
    [sign] = [o for o in report["objects"] if o["name"] == "Sign"]
    assert sign["stats"]["triangles"] > 100 and sign["faces_of"] == "evaluated"
    assert report["triangles"] == 12 + sign["stats"]["triangles"]
    assert asset_issues(report)["over_budget"]["severity"] == "error"


def test_a_metaball_is_an_error_that_the_export_leaves_out_and_counts_no_triangles(blender):
    """A metaball gets a surface_not_exported error, and its surface counts no triangles.

    The glTF export writes a metaball as a node with no mesh, so its surface is not in the file.
    """
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    add_metaball(blender, "Bubble", location=[3, 0, 0])
    report = blender.ok("check_asset")
    issue = issues_of(report, "Bubble")["surface_not_exported"]
    assert issue["severity"] == "error" and "metaball" in issue["message"], issue
    [row] = [o for o in report["objects"] if o["name"] == "Bubble"]
    assert "stats" not in row, f"the metaball's surface was counted: {row}"
    assert report["triangles"] == 12, "the metaball's surface counted in the asset's triangles"
    assert report["passed"] is False
    alone = blender.ok("check_asset", objects=["Bubble"])
    assert alone["triangles"] == 0 and alone["counts"]["errors"] == 1, alone


def test_a_texture_coordinate_node_is_not_a_procedural_texture(blender):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    textured(blender, "Box", 'image = bpy.data.images.new("Gen", 4, 4)')
    blender.ok("run_python", code="""
tree = bpy.data.materials["BoxMat"].node_tree
image = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeTexImage")
coord = tree.nodes.new("ShaderNodeTexCoord")
tree.links.new(coord.outputs["UV"], image.inputs["Vector"])
""")
    assert "procedural_material" not in issues_of(blender.ok("check_asset", objects=["Box"]), "Box")


def test_an_armature_alone_keeps_the_shape_keys_in_the_export(blender):
    blender.ok("create_object", kind="cube", name="Blob", size=1)
    blender.ok("run_python", code="obj = bpy.data.objects['Blob']\nobj.shape_key_add(name='Basis')\n"
                                  "obj.shape_key_add(name='Smile')\nobj.modifiers.new('Armature', 'ARMATURE')")
    assert "morphs_dropped_by_modifiers" not in issues_of(blender.ok("check_asset", objects=["Blob"]), "Blob")


def test_a_collision_proxy_draws_no_overlap_or_uv_warning(blender):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    proxy = blender.ok("create_collision", object="Crate")["object"]
    report = blender.ok("check_asset", objects=["Crate"])
    assert "overlaps" not in asset_issues(report)
    assert "no_uv" not in issues_of(report, proxy)


def test_objects_with_issues_are_listed_first_and_the_rest_counted(blender):
    blender.ok("run_python", code="""
for i in range(120):
    mesh = bpy.data.meshes.new(f"M{i}")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=0.5)
    bmesh.ops.translate(bm, verts=bm.verts, vec=(0, 0, 0.25))
    if i == 119:
        bmesh.ops.reverse_faces(bm, faces=list(bm.faces))
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(f"Part{i:03d}", mesh)
    obj.location = (i, 0, 0)
    bpy.context.scene.collection.objects.link(obj)
""")
    report = blender.ok("check_asset")
    assert report["objects"][0]["name"] == "Part119", "the failing object is not listed first"
    assert len(report["objects"]) + report["objects_not_listed"] == 120
    assert report["passed"] is False and report["counts"]["errors"] == 1
