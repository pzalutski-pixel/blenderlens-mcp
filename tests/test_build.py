"""Tests for building in a real Blender: primitives, mesh edits, modifiers, materials and arranging objects.

Some tests check the call's own result; others also read the scene back with get_object or get_mesh
(dimensions, face areas, normals, material slots) to check what Blender holds after the call.
"""

import math

import pytest

from tests.test_refusals_change_nothing import refused_unchanged

pytestmark = pytest.mark.integration


def close(a, b, tol=1e-3):
    """Return whether the sequences ``a`` and ``b`` match, item by item, within ``tol``."""
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "args", "dimensions", "faces"), [
    ("cube", {"size": [2.0, 1.5, 1.2]}, [2.0, 1.5, 1.2], 6),
    ("cube", {"size": 0.5}, [0.5, 0.5, 0.5], 6),
    ("plane", {"size": [3.0, 1.0, 0]}, [3.0, 1.0, 0.0], 1),
    ("grid", {"size": [2, 2, 0], "subdivisions": 4}, [2.0, 2.0, 0.0], 16),
    ("cylinder", {"radius": 0.5, "depth": 2.0, "segments": 16}, [1.0, 1.0, 2.0], 18),
    ("cone", {"radius": 1.0, "radius_top": 0.0, "depth": 1.0, "segments": 8}, [2.0, 2.0, 1.0], None),
    ("uv_sphere", {"radius": 0.75}, [1.5, 1.5, 1.5], None),
    ("ico_sphere", {"radius": 1.0, "subdivisions": 1}, None, 20),
    ("torus", {"radius": 1.0, "minor_radius": 0.25, "segments": 24, "rings": 8}, [2.5, 2.5, 0.5], 192),
    ("circle", {"radius": 1.0, "segments": 12, "fill": True}, [2.0, 2.0, 0.0], 1),
    ("monkey", {}, None, 500),
])
def test_a_primitive_has_its_size_in_the_mesh_and_scale_one(blender, kind, args, dimensions, faces):
    made = blender.ok("create_object", kind=kind, **args)
    assert made["type"] == "MESH"
    assert made["scale"] == [1.0, 1.0, 1.0]
    if dimensions:
        assert close(made["dimensions"], dimensions, 1e-2), made["dimensions"]
    if faces:
        assert made["mesh"]["faces"] == faces
    details = blender.ok("get_object", object=made["name"])
    assert details["dimensions"] == made["dimensions"]


def test_a_four_sided_cone_with_a_top_radius_is_a_square_frustum(blender):
    made = blender.ok("create_object", kind="cone", name="Plinth", segments=4, radius=1.0, radius_top=0.4,
                      depth=0.5, rotation=[0, 0, 45])
    assert made["mesh"]["faces"] == 6
    mesh = blender.ok("get_mesh", object="Plinth")
    tops = [f for f in mesh["faces"] if f["facing"] == "top"]
    bottoms = [f for f in mesh["faces"] if f["facing"] == "bottom"]
    assert len(tops) == 1 and len(bottoms) == 1
    assert bottoms[0]["area"] > tops[0]["area"] * 3, "the base is not wider than the top"


def test_placement_collection_and_parent_are_applied(blender):
    blender.ok("create_object", kind="empty", name="Rig")
    made = blender.ok("create_object", kind="cube", name="Leg", size=[0.2, 0.2, 1], location=[1, 2, 0.5],
                      rotation=[0, 0, 90], collection="Parts", parent="Rig")
    assert made["parent"] == "Rig"
    assert made["collections"] == ["Parts"]
    assert made["rotation"] == [0.0, 0.0, 90.0]
    assert made["location"] == [1.0, 2.0, 0.5]


def test_a_name_clash_gets_blenders_suffix_and_the_result_says_so(blender):
    blender.ok("create_object", kind="cube", name="Part")
    assert blender.ok("create_object", kind="cube", name="Part")["name"] == "Part.001"


def test_an_unknown_kind_is_refused_with_the_kinds(blender):
    payload = blender.refused("create_object", "bad_argument", kind="dodecahedron")
    assert "torus" in payload["hint"]


def test_a_bad_size_is_refused(blender):
    blender.refused("create_object", "bad_argument", kind="cube", size=[1, 2])


# ---------------------------------------------------------------------------
# create_mesh
# ---------------------------------------------------------------------------


def test_create_mesh_points_normals_outward_even_when_wound_inward(blender):
    # A tetrahedron with every face wound the wrong way.
    vertices = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
    inward = [[0, 1, 2], [0, 3, 1], [0, 2, 3], [1, 3, 2]]
    inward = [list(reversed(f)) for f in inward]
    blender.ok("create_mesh", name="Shard", vertices=vertices, faces=inward)
    mesh = blender.ok("get_mesh", object="Shard")
    centroid = [0.25, 0.25, 0.25]
    for face in mesh["faces"]:
        outward = [c - m for c, m in zip(face["center"], centroid, strict=True)]
        assert sum(n * o for n, o in zip(face["normal"], outward, strict=True)) > 0, face


def test_create_mesh_refuses_an_index_past_the_vertices(blender):
    payload = blender.refused("create_mesh", "bad_argument", name="Bad", vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]],
                              faces=[[0, 1, 5]])
    assert "5" in payload["error"]


# ---------------------------------------------------------------------------
# edit_mesh
# ---------------------------------------------------------------------------


@pytest.fixture
def box(blender):
    """Provide the server with a 2 m cube named Box."""
    blender.ok("create_object", kind="cube", name="Box", size=[2, 2, 2])
    return blender


def test_extrude_the_top_grows_the_box_and_leaves_no_inner_face(box):
    out = box.ok("edit_mesh", object="Box", operation="extrude", facing="top", amount=0.5)
    assert out["selected_faces"] == 1
    assert out["mesh"]["faces"] == 10, \
        "the cube extruded at the top does not have 10 faces, so an inner face may be left"
    assert close(out["dimensions"], [2, 2, 2.5])
    mesh = box.ok("get_mesh", object="Box", limit=100)
    cap = [f for f in mesh["faces"] if f["index"] in out["result_faces"] and f["facing"] == "top"]
    assert cap and abs(cap[0]["center"][2] - 1.5) < 1e-4


def test_extrude_along_a_direction(box):
    out = box.ok("edit_mesh", object="Box", operation="extrude", facing="front", amount=1.0, direction="bottom")
    assert close(out["dimensions"], [2, 2, 3])


def test_inset_returns_the_inner_face_and_it_is_smaller(box):
    out = box.ok("edit_mesh", object="Box", operation="inset", facing="front", amount=0.3)
    assert out["mesh"]["faces"] == 10
    inner = out["result_faces"]
    assert len(inner) == 1
    mesh = box.ok("get_mesh", object="Box", limit=100)
    face = next(f for f in mesh["faces"] if f["index"] == inner[0])
    assert face["facing"] == "front"
    assert face["area"] == pytest.approx(1.4 * 1.4, abs=1e-3)


def test_scaling_the_top_face_widens_the_top_of_a_box(box):
    box.ok("edit_mesh", object="Box", operation="scale", facing="top", factor=[1.5, 1.5, 1])
    details = box.ok("get_object", object="Box")
    assert close(details["dimensions"], [3, 3, 2])
    mesh = box.ok("get_mesh", object="Box")
    assert next(f for f in mesh["faces"] if f["facing"] == "top")["area"] == pytest.approx(9.0, abs=1e-3)


def test_translate_and_rotate_move_only_the_selection(box):
    out = box.ok("edit_mesh", object="Box", operation="translate", facing="top", offset=[0, 0, 1])
    assert close(out["dimensions"], [2, 2, 3])
    out = box.ok("edit_mesh", object="Box", operation="rotate", facing="top", rotation=[0, 0, 45])
    assert out["dimensions"][0] > 2.5, "the rotated top square does not reach further along x"


def test_delete_subdivide_triangulate_and_merge(box):
    assert box.ok("edit_mesh", object="Box", operation="delete", facing="top")["mesh"]["faces"] == 5
    assert box.ok("edit_mesh", object="Box", operation="subdivide", cuts=1)["mesh"]["faces"] == 20
    tri = box.ok("edit_mesh", object="Box", operation="triangulate")
    assert tri["mesh"]["faces"] == tri["mesh"]["triangles"] == 40
    merged = box.ok("edit_mesh", object="Box", operation="merge", amount=0.001)
    assert merged["merged_vertices"] == 0


def test_bevel_all_edges_rounds_the_box(box):
    out = box.ok("edit_mesh", object="Box", operation="bevel", amount=0.1, segments=2)
    assert out["mesh"]["faces"] > 6
    assert close(out["dimensions"], [2, 2, 2])


def test_bevel_the_top_rim_only(box):
    out = box.ok("edit_mesh", object="Box", operation="bevel", facing="top", amount=0.1)
    assert out["mesh"]["faces"] == 10, "the bevel did not add one face for each of the four rim edges"


def test_flip_then_recalc_normals(box):
    box.ok("edit_mesh", object="Box", operation="flip_normals", facing="top")
    flipped = box.ok("get_mesh", object="Box")
    assert sum(1 for f in flipped["faces"] if f["facing"] == "bottom") == 2
    box.ok("edit_mesh", object="Box", operation="recalc_normals")
    fixed = box.ok("get_mesh", object="Box")
    assert sorted(f["facing"] for f in fixed["faces"]) == sorted(["top", "bottom", "front", "back", "left", "right"])


def test_shade_smooth_with_an_angle(box):
    out = box.ok("edit_mesh", object="Box", operation="shade_smooth", angle=30)
    run = box.ok("run_python", code="m = bpy.data.objects['Box'].data\nresult = [p.use_smooth for p in m.polygons]")
    assert all(run["result"])
    assert "result_faces" not in out, "shading listed result faces, though it makes none"
    assert "result_faces" not in box.ok("edit_mesh", object="Box", operation="shade_flat")


def test_faces_by_index_and_facing_together(box):
    out = box.ok("edit_mesh", object="Box", operation="extrude", faces=[0, 1, 2, 3, 4, 5], facing="top",
                 amount=0.25)
    assert out["selected_faces"] == 1


def test_a_selection_that_matches_nothing_says_how_to_find_faces(box):
    box.ok("edit_mesh", object="Box", operation="delete", facing="top")
    payload = box.refused("edit_mesh", "not_found", object="Box", operation="extrude", facing="top", amount=1)
    assert "get_mesh" in payload["hint"]


def test_out_of_range_faces_and_unknown_operations_are_refused(box):
    box.refused("edit_mesh", "bad_argument", object="Box", operation="extrude", faces=[99], amount=1)
    box.refused("edit_mesh", "bad_argument", object="Box", operation="melt")
    box.refused("edit_mesh", "bad_argument", object="Box", operation="extrude")


def test_a_name_not_found_suggests_the_closest_names_first(blender):
    blender.ok("create_object", kind="cube", name="Book_Stack", size=1)
    blender.ok("create_object", kind="cube", name="Tray", size=1, location=[3, 0, 0], collection="Items")
    blender.ok("add_modifier", object="Tray", type="bevel")
    payload = blender.refused("get_object", "not_found", object="Book_stack")
    assert payload["hint"].startswith("Did you mean 'Book_Stack'? Objects in this file:"), payload["hint"]
    payload = blender.refused("set_modifier", "not_found", object="Tray", modifier="Bevl", show=False)
    assert payload["hint"].startswith("Did you mean 'Bevel'?"), payload["hint"]
    payload = blender.refused("export_file", "not_found", path="unused.obj", collection="Itms")
    assert payload["hint"].startswith("Did you mean 'Items'?"), payload["hint"]
    assert blender.refused("get_object", "not_found", object="Zzz")["hint"].startswith("Objects in this file")


def test_editing_an_empty_is_refused(blender):
    blender.ok("create_object", kind="empty", name="Pivot")
    payload = blender.refused("edit_mesh", "bad_argument", object="Pivot", operation="delete")
    assert "not a mesh" in payload["error"]


def test_after_a_refused_transform_facing_is_judged_by_the_transform_kept(blender):
    """After a refused ``set_transform``, ``edit_mesh`` judges which way faces point by the transform the object kept.

    A refused call restores the rotation, and the world matrix must be restored with it, or faces would be judged
    by the refused rotation.
    """
    blender.ok("create_object", kind="plane", name="Wall", rotation=[90, 0, 0])  # Its face points to the front.
    blender.refused("set_transform", "bad_argument", object="Wall", rotation=[0, 0, 0], dimensions=[1, 1, 1])
    blender.ok("edit_mesh", object="Wall", operation="extrude", facing="front", amount=1)
    bounds = blender.ok("get_object", object="Wall")["world_bounds"]
    assert bounds["min"][1] == pytest.approx(-1.0, abs=1e-3), "the front face did not move one metre to the front"

    blender.ok("create_object", kind="plane", name="Floor", location=[5, 0, 0])  # Its face points up.
    blender.refused("set_transform", "bad_argument", object="Floor", rotation=[90, 0, 0], dimensions=[1, 1, 1])
    blender.refused("edit_mesh", "not_found", object="Floor", operation="extrude", facing="front", amount=1)
    assert [f["facing"] for f in blender.ok("get_mesh", object="Floor")["faces"]] == ["top"]


def test_facing_is_judged_in_world_space(blender):
    blender.ok("create_object", kind="cube", name="Tilted", rotation=[90, 0, 0])
    mesh = blender.ok("get_mesh", object="Tilted")
    assert sorted(f["facing"] for f in mesh["faces"]) == sorted(["top", "bottom", "front", "back", "left", "right"])
    out = blender.ok("edit_mesh", object="Tilted", operation="extrude", facing="top", amount=1)
    assert out["dimensions"][1] == pytest.approx(3.0, abs=1e-3), "the face that points up in the world did not grow"


@pytest.mark.parametrize(("direction", "grows"), [
    ("top", (2, 1)),
    ([0, 0, 1], (2, 1)),
    ("front", (1, -1)),
])
def test_an_extrude_direction_is_in_world_space(blender, direction, grows):
    """An extrude ``direction`` is a world direction, even on a cube rotated 90 degrees about X.

    The face extruded is the one on top in the world, and the box grows one metre along the world direction only.
    """
    blender.ok("create_object", kind="cube", name="Tilted", rotation=[90, 0, 0])
    blender.ok("edit_mesh", object="Tilted", operation="extrude", facing="top", amount=1, direction=direction)
    bounds = blender.ok("get_object", object="Tilted")["world_bounds"]
    axis, sign = grows
    reach = bounds["max"][axis] if sign > 0 else -bounds["min"][axis]
    assert reach == pytest.approx(2.0, abs=1e-3), f"the box did not grow one metre along world {direction}: {bounds}"
    for other in {0, 1, 2} - {axis}:
        assert bounds["max"][other] == pytest.approx(1.0, abs=1e-3), bounds
        assert bounds["min"][other] == pytest.approx(-1.0, abs=1e-3), bounds


def test_an_extrude_direction_keeps_the_amount_in_mesh_space(blender):
    """The extrude amount stays in the mesh's own space when a direction is given.

    On an object scaled by 2, an amount of one unit is two metres in the world.
    """
    blender.ok("create_object", kind="cube", name="Big", scale=2)
    blender.ok("edit_mesh", object="Big", operation="extrude", facing="top", amount=1, direction="top")
    bounds = blender.ok("get_object", object="Big")["world_bounds"]
    assert bounds["max"][2] == pytest.approx(4.0, abs=1e-3)


# ---------------------------------------------------------------------------
# get_mesh
# ---------------------------------------------------------------------------


def test_get_mesh_limits_and_lists_vertices(box):
    out = box.ok("get_mesh", object="Box", limit=2, include_vertices=True)
    assert len(out["faces"]) == 2 and out["truncated"] is True
    assert len(out["vertices"]) == 8
    assert all(abs(abs(c) - 1) < 1e-6 for v in out["vertices"] for c in v)


def test_get_mesh_lists_only_the_faces_of_one_material(box):
    box.ok("set_material", material="Wood", object="Box")
    box.ok("set_material", material="Label", object="Box", facing="top")
    by_name = box.ok("get_mesh", object="Box", material="Label")
    assert [f["facing"] for f in by_name["faces"]] == ["top"] and by_name["truncated"] is False
    by_slot = box.ok("get_mesh", object="Box", material=0, limit=3)
    assert len(by_slot["faces"]) == 3 and {f["material"] for f in by_slot["faces"]} == {0}
    assert by_slot["truncated"] is True and by_slot["material_faces"] == 5
    assert by_slot["counts"]["faces"] == 6, "the counts are of the faces listed, not of the whole mesh"
    payload = box.refused("get_mesh", "not_found", object="Box", material="Labl")
    assert "Label" in payload["hint"] and "Wood" in payload["hint"]
    box.refused("get_mesh", "bad_argument", object="Box", material=7)


def test_get_mesh_evaluated_sees_modifiers(box):
    box.ok("add_modifier", object="Box", type="subdivision", settings={"levels": 1})
    assert box.ok("get_mesh", object="Box", evaluated=True)["counts"]["faces"] == 24
    assert box.ok("get_mesh", object="Box")["counts"]["faces"] == 6


# ---------------------------------------------------------------------------
# Modifiers
# ---------------------------------------------------------------------------


def test_add_bevel_reports_settings_and_the_evaluated_mesh(box):
    out = box.ok("add_modifier", object="Box", type="bevel", settings={"width": 0.1, "segments": 3})
    assert out["modifier"]["settings"]["width"] == pytest.approx(0.1)
    assert out["modifier"]["settings"]["segments"] == 3
    assert out["mesh"]["faces"] == 6
    assert out["mesh_evaluated"]["faces"] > 6


def test_an_unknown_setting_is_refused_with_the_real_names(box):
    payload = box.refused("add_modifier", "bad_argument", object="Box", type="bevel", settings={"radius": 1})
    assert "width" in payload["hint"]
    assert box.ok("get_object", object="Box")["modifiers"] == [], "the half-made modifier was left on the object"


def test_enum_settings_match_in_any_case(box):
    out = box.ok("add_modifier", object="Box", type="BEVEL", settings={"limit_method": "none"})
    assert out["modifier"]["settings"]["limit_method"] == "NONE"
    payload = box.refused("set_modifier", "bad_argument", object="Box", modifier="Bevel",
                          settings={"limit_method": "sideways"})
    assert "ANGLE" in payload["error"]


def test_array_repeats_along_x(box):
    out = box.ok("add_modifier", object="Box", type="array", settings={"count": 3})
    assert close(out["dimensions"], [6, 2, 2])


def test_boolean_takes_its_cutter_by_name(box):
    box.ok("create_object", kind="cylinder", name="Cutter", radius=0.5, depth=4)
    before = box.ok("get_object", object="Box")["mesh_evaluated"]["faces"]
    out = box.ok("add_modifier", object="Box", type="boolean",
                 settings={"operation": "difference", "object": "Cutter"})
    assert out["modifier"]["settings"]["object"] == "Cutter"
    assert out["mesh_evaluated"]["faces"] > before
    box.refused("set_modifier", "not_found", object="Box", modifier="Boolean", settings={"object": "Ghost"})


def test_set_modifier_moves_and_hides(box):
    box.ok("add_modifier", object="Box", type="bevel")
    box.ok("add_modifier", object="Box", type="subdivision")
    out = box.ok("set_modifier", object="Box", modifier="Subdivision", index=0, show=False,
                 settings={"levels": 2})
    assert out["stack"] == ["Subdivision", "Bevel"]
    assert out["modifier"]["settings"]["levels"] == 2
    assert out["modifier"]["settings"]["show_viewport"] is False


def test_apply_bakes_and_remove_drops(box):
    box.ok("add_modifier", object="Box", type="subdivision", settings={"levels": 1})
    applied = box.ok("apply_modifier", object="Box", modifier="Subdivision")
    assert applied["stack"] == []
    assert applied["mesh"]["faces"] == 24
    box.ok("add_modifier", object="Box", type="bevel")
    removed = box.ok("remove_modifier", object="Box", modifier="Bevel")
    assert removed["stack"] == []
    assert removed["mesh_evaluated"]["faces"] == 24


def test_missing_modifier_and_unknown_type(box):
    payload = box.refused("apply_modifier", "not_found", object="Box", modifier="Nothing")
    assert "none" in payload["hint"]
    box.refused("add_modifier", "bad_argument", object="Box", type="sparkle")
    box.refused("add_modifier", "not_found", object="Ghost", type="bevel")


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------


def test_hex_is_srgb_and_lists_are_linear(blender):
    from_hex = blender.ok("set_material", material="Grey", base_color="#808080")["material"]
    assert from_hex["base_color"][0] == pytest.approx(0.21586, abs=1e-4)
    from_list = blender.ok("set_material", material="Grey2", base_color=[0.5, 0.5, 0.5])["material"]
    assert from_list["base_color"][:3] == [0.5, 0.5, 0.5]


def test_emission_color_alone_turns_the_glow_on(blender):
    out = blender.ok("set_material", material="Screen", emission_color=[0.25, 0.5, 1.0])["material"]
    assert out["emission_strength"] == 1.0
    stronger = blender.ok("set_material", material="Screen", emission_strength=4)["material"]
    assert stronger["emission_strength"] == 4.0
    assert stronger["emission_color"][:3] == [0.25, 0.5, 1.0], "changing the strength changed the colour"


def test_a_material_on_some_faces_only(box):
    box.ok("set_material", material="Plastic", object="Box", base_color=[0.9, 0.85, 0.7], roughness=0.5)
    grille = box.ok("edit_mesh", object="Box", operation="inset", facing="front", amount=0.5)
    out = box.ok("set_material", material="Grille", object="Box", faces=grille["result_faces"],
                 base_color="#333333", roughness=0.9)
    assert out["faces_assigned"] == 1
    assert out["slot"] == 1
    slots = box.ok("get_object", object="Box")["materials"]
    assert slots == [{"slot": 0, "material": "Plastic", "faces": 9}, {"slot": 1, "material": "Grille", "faces": 1}]
    listed = {m["name"]: m for m in box.ok("list_materials")["materials"]}
    assert listed["Grille"]["objects"] == ["Box"]
    assert listed["Grille"]["users"] >= 1


def test_a_material_on_a_shared_mesh_says_every_user_changed(blender):
    blender.ok("create_object", kind="cube", name="A")
    blender.ok("duplicate_object", object="A", linked=True, name="B", offset=[3, 0, 0])
    out = blender.ok("set_material", material="Red", object="A", base_color=[1, 0, 0], faces=[0])
    assert out["mesh_users"] == 2
    assert "B" in out["note"] and "2" in out["note"], out
    alone = blender.ok("set_material", material="Blue", object=blender.ok("create_object", kind="cube")["name"])
    assert "mesh_users" not in alone and "note" not in alone


def test_a_material_slot_linked_to_the_object_reports_what_the_object_shows(blender):
    blender.ok("create_object", kind="cube", name="A")
    blender.ok("set_material", material="Blue", object="A", base_color=[0, 0, 1])
    blender.ok("run_python", code="o = bpy.data.objects['A']; o.material_slots[0].link = 'OBJECT'\n"
                                  "o.material_slots[0].material = bpy.data.materials.new('Painted')")
    out = blender.ok("set_material", material="Blue", object="A", faces=[1])
    assert out["slot"] == 0
    assert out["shows"] == "Painted", out
    assert "Painted" in out["note"]
    assert blender.ok("get_object", object="A")["materials"][0]["material"] == "Painted"


def test_material_values_are_checked(box):
    box.refused("set_material", "bad_argument", material="M", metallic=2)
    box.refused("set_material", "bad_argument", material="M", base_color="#12")
    box.refused("set_material", "bad_argument", material="M", base_color=[1, 2])


# ---------------------------------------------------------------------------
# Arranging
# ---------------------------------------------------------------------------


def test_set_transform_keeps_null_components(box):
    out = box.ok("set_transform", object="Box", location=[1, 2, 3])
    out = box.ok("set_transform", object="Box", location=[None, None, 0], rotation=[0, 0, 30], scale=2)
    assert out["location"] == [1.0, 2.0, 0.0]
    assert out["rotation"] == [0.0, 0.0, 30.0]
    assert out["scale"] == [2.0, 2.0, 2.0]
    out = box.ok("set_transform", object="Box", dimensions=[1, None, None])
    assert out["dimensions"][0] == pytest.approx(1.0)
    assert out["dimensions"][1] == pytest.approx(4.0)


def test_apply_transform_bakes_scale_into_the_mesh(box):
    box.ok("set_transform", object="Box", scale=[2, 1, 0.5], rotation=[0, 0, 0])
    out = box.ok("apply_transform", object="Box")
    assert out["scale"] == [1.0, 1.0, 1.0]
    assert close(out["dimensions"], [4, 2, 1])
    assert out["applied"] == ["rotation", "scale"]


def test_parenting_keeps_the_world_position_and_refuses_loops(blender):
    blender.ok("create_object", kind="cube", name="Base", location=[5, 0, 0])
    blender.ok("create_object", kind="cube", name="Top", location=[5, 0, 3])
    out = blender.ok("set_parent", object="Top", parent="Base")
    assert out["world_location"] == [5.0, 0.0, 3.0]
    assert out["location"] == [0.0, 0.0, 3.0]
    blender.refused("set_parent", "bad_argument", object="Base", parent="Top")
    cleared = blender.ok("set_parent", object="Top", parent=None)
    assert cleared["parent"] is None and cleared["location"] == [5.0, 0.0, 3.0]


def test_parenting_without_keeping_moves_with_the_parent(blender):
    blender.ok("create_object", kind="cube", name="Base", location=[5, 0, 0])
    blender.ok("create_object", kind="cube", name="Top", location=[0, 0, 3])
    out = blender.ok("set_parent", object="Top", parent="Base", keep_transform=False)
    assert out["world_location"] == [5.0, 0.0, 3.0]


def test_set_collection_creates_moves_and_keeps(blender):
    blender.ok("create_object", kind="cube", name="A")
    blender.ok("create_object", kind="cube", name="B")
    out = blender.ok("set_collection", objects=["A", "B"], collection="Props")
    assert out["objects"] == {"A": ["Props"], "B": ["Props"]}
    kept = blender.ok("set_collection", objects=["A"], collection="Exports", keep_other=True)
    assert sorted(kept["objects"]["A"]) == ["Exports", "Props"]
    tree = {c["name"]: c for c in blender.ok("get_scene")["collections"]}
    assert tree["Props"]["objects"] == ["A", "B"]


def test_set_object_renames_hides_and_tags(box):
    out = box.ok("set_object", object="Box", rename="Lantern", hide=True, display="WIRE",
                 properties={"style": "storm", "wicks": 2})
    assert out["object"] == "Lantern"
    assert out["visible"] is False
    assert out["properties"] == {"style": "storm", "wicks": 2}
    scene = box.ok("get_scene")
    assert scene["objects"][0]["visible"] is False
    removed = box.ok("set_object", object="Lantern", properties={"wicks": None})
    assert removed["properties"] == {"style": "storm"}


def test_set_object_draws_an_empty_as_another_shape(box, tmp_path):
    box.ok("create_object", kind="empty", name="Handle", parent="Box")
    out = box.ok("set_object", object="Handle", empty_display="arrows")
    assert out["empty_display"] == "ARROWS" and out["changed"] == ["empty_display"]
    assert box.ok("get_object", object="Handle")["empty_display"]["type"] == "ARROWS"
    box.ok("save_file", path=str(tmp_path / "handle.blend"))
    refused_unchanged(box, "set_object", "bad_argument", object="Box", empty_display="CUBE")
    payload = refused_unchanged(box, "set_object", "bad_argument", object="Handle", empty_display="TEAPOT",
                                rename="Knob")
    assert "SINGLE_ARROW" in payload["error"]


def test_duplicate_copies_children_and_linked_shares_the_mesh(blender):
    blender.ok("create_object", kind="cube", name="Leg", size=[0.2, 0.2, 1])
    blender.ok("create_object", kind="cube", name="Foot", size=0.3, parent="Leg", location=[0, 0, -0.5])
    copy = blender.ok("duplicate_object", object="Leg", name="Leg2", offset=[1, 0, 0])
    assert copy["name"] == "Leg2"
    assert len(copy["copied"]) == 2
    assert copy["location"] == [1.0, 0.0, 0.0]
    twin = blender.ok("duplicate_object", object="Leg", linked=True, with_children=False)
    assert blender.ok("get_object", object=twin["name"])["mesh_users"] == 2


#: Code that makes a body's parts refer to each other in the ways Blender allows (modifiers, constraints, a geometry
#: node input and a custom property), and to one object outside the body.
CROSS_LINKED = """
objects = bpy.data.objects
body, cutter, aim, outside = objects["Body"], objects["Cutter"], objects["Aim"], objects["Outside"]
array = body.modifiers.new("Array", "ARRAY")
array.use_relative_offset = False
array.use_object_offset = True
array.offset_object = aim
array.count = 1
deform = body.modifiers.new("Deform", "SIMPLE_DEFORM")
deform.origin = outside
deform.angle = 0.0  # Only the reference is tested; a twist about an outside axis would move each copy differently.
group = bpy.data.node_groups.new("Pick", "GeometryNodeTree")
group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
socket = group.interface.new_socket("Target", in_out="INPUT", socket_type="NodeSocketObject")
group.links.new(group.nodes.new("NodeGroupInput").outputs[0], group.nodes.new("NodeGroupOutput").inputs[0])
nodes = body.modifiers.new("Pick", "NODES")
nodes.node_group = group
nodes[socket.identifier] = cutter
track = cutter.constraints.new("DAMPED_TRACK")
track.target = aim
stay = cutter.constraints.new("COPY_SCALE")
stay.target = outside
body["partner"] = cutter
"""

#: Code that returns, by name, the object each reference that ``CROSS_LINKED`` makes points at.
REFERENCES = """
def name(value):
    return value.name if value is not None else None
body, cutter = bpy.data.objects[BODY], bpy.data.objects[CUTTER]
nodes = body.modifiers["Pick"]
result = {
    "boolean": name(body.modifiers["Boolean"].object),
    "mirror": name(body.modifiers["Mirror"].mirror_object),
    "array": name(body.modifiers["Array"].offset_object),
    "deform": name(body.modifiers["Deform"].origin),
    "nodes": name(nodes[nodes.node_group.interface.items_tree["Target"].identifier]),
    "track": name(cutter.constraints["Damped Track"].target),
    "stay": name(cutter.constraints["Copy Scale"].target),
    "partner": name(body["partner"]),
}
"""


def test_duplicate_points_the_copies_at_each_other_as_shift_d_does(blender):
    """Duplicating a body points the copies at each other, as Shift+D does, and leaves outside references alone.

    Otherwise the copy's boolean modifier would cut with the original cutter, five metres away, and so would not
    cut at all.
    """
    blender.ok("create_object", kind="cube", name="Body")
    blender.ok("create_object", kind="cylinder", name="Cutter", parent="Body", location=[1, 0, 0], scale=0.5)
    blender.ok("create_object", kind="empty", name="Aim", parent="Body", location=[0, 0, 2])
    blender.ok("create_object", kind="empty", name="Outside", location=[0, 0, 9])
    blender.ok("add_modifier", object="Body", type="boolean", settings={"object": "Cutter"})
    blender.ok("add_modifier", object="Body", type="mirror", settings={"mirror_object": "Cutter"})
    blender.ok("run_python", code=CROSS_LINKED)
    copy = blender.ok("duplicate_object", object="Body", offset=[5, 0, 0])
    assert sorted(copy["copied"]) == ["Aim.001", "Body.001", "Cutter.001"]

    copied = blender.ok("run_python", code=f"BODY, CUTTER = 'Body.001', 'Cutter.001'\n{REFERENCES}")["result"]
    assert copied == {"boolean": "Cutter.001", "mirror": "Cutter.001", "array": "Aim.001", "deform": "Outside",
                      "nodes": "Cutter.001", "track": "Aim.001", "stay": "Outside", "partner": "Cutter.001"}
    original = blender.ok("run_python", code=f"BODY, CUTTER = 'Body', 'Cutter'\n{REFERENCES}")["result"]
    assert original == {"boolean": "Cutter", "mirror": "Cutter", "array": "Aim", "deform": "Outside",
                        "nodes": "Cutter", "track": "Aim", "stay": "Outside", "partner": "Cutter"}
    body = blender.ok("get_object", object="Body")
    twin = blender.ok("get_object", object="Body.001")
    assert twin["mesh_evaluated"] == body["mesh_evaluated"], "the copy is not cut the same way"
    assert twin["world_bounds"]["min"][0] == pytest.approx(body["world_bounds"]["min"][0] + 5, abs=1e-3)


def test_delete_keeps_children_in_place_unless_asked(blender):
    blender.ok("create_object", kind="cube", name="Base", location=[2, 0, 0])
    blender.ok("create_object", kind="cube", name="Top", location=[0, 0, 1], parent="Base")
    out = blender.ok("delete_object", objects=["Base"])
    assert out["deleted"] == ["Base"]
    top = blender.ok("get_object", object="Top")
    assert top["parent"] is None and top["location"] == [2.0, 0.0, 1.0]
    blender.ok("create_object", kind="cube", name="Base2")
    blender.ok("set_parent", object="Top", parent="Base2")
    out = blender.ok("delete_object", objects=["Base2"], with_children=True)
    assert sorted(out["deleted"]) == ["Base2", "Top"]
    assert out["remaining"] == 0
    meshes = blender.ok("run_python", code="result = len(bpy.data.meshes)")["result"]
    assert meshes == 0, "mesh data the deleted objects used is left behind"


def test_get_scene_lists_objects_collections_and_bounds(blender):
    blender.ok("create_object", kind="cube", name="A", location=[0, 0, 1])
    blender.ok("create_object", kind="cube", name="B", location=[3, 0, 1], collection="Side")
    scene = blender.ok("get_scene")
    assert [o["name"] for o in scene["objects"]] == ["A", "B"]
    assert scene["bounds"] == {"min": [-1.0, -1.0, 0.0], "max": [4.0, 1.0, 2.0]}
    assert scene["unsaved_changes"] is True
    assert any(c["name"] == "Side" and c["objects"] == ["B"] for c in scene["collections"])


def test_get_object_on_a_missing_name_lists_what_exists(blender):
    blender.ok("create_object", kind="cube", name="Present")
    payload = blender.refused("get_object", "not_found", object="Absent")
    assert "Present" in payload["hint"]


def test_rotation_reads_back_in_degrees(blender):
    made = blender.ok("create_object", kind="cube", rotation=[10, 20, 30])
    assert made["rotation"] == [10.0, 20.0, 30.0]
    raw = blender.ok("run_python", code=f"result = list(bpy.data.objects['{made['name']}'].rotation_euler)")
    assert raw["result"][2] == pytest.approx(math.radians(30), abs=1e-5)
