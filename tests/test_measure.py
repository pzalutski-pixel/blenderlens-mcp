"""Tests for ``measure_objects``, ``set_transform`` with ``drop``, and ``apply_transform`` with ``origin``.

They run against a real Blender. Everything is measured in world space after modifiers, and results are
checked against what Blender itself reports: world bounds, world locations, and the faces that ``get_mesh``
lists.
"""

import pytest

from tests.test_refusals_change_nothing import refused_unchanged

pytestmark = pytest.mark.integration


def world_bounds(blender, name: str) -> dict:
    """Return the world bounds that get_object reports for the object ``name``."""
    return blender.ok("get_object", object=name)["world_bounds"]


def face_facing(blender, name: str, side: str) -> int:
    """Return the index of the one face of ``name`` that get_mesh reports as facing ``side``."""
    faces = blender.ok("get_mesh", object=name)["faces"]
    [index] = [f["index"] for f in faces if f["facing"] == side]
    return index


# ---------------------------------------------------------------------------
# measure_objects
# ---------------------------------------------------------------------------


def test_two_cubes_a_metre_apart_measure_one_metre(blender):
    blender.ok("create_object", kind="cube", name="A", size=1)
    blender.ok("create_object", kind="cube", name="B", size=1, location=[2, 0, 0])
    [pair] = blender.ok("measure_objects", objects=["A", "B"])["pairs"]
    assert pair["a"] == "A" and pair["b"] == "B"
    assert pair["distance"] == pytest.approx(1.0, abs=1e-6)
    assert pair["closest"]["a"][0] == pytest.approx(0.5, abs=1e-6)
    assert pair["closest"]["b"][0] == pytest.approx(1.5, abs=1e-6)
    assert pair["overlapping"] is False


def test_distance_is_world_space_through_rotation_and_scale(blender):
    blender.ok("create_object", kind="cube", name="A", size=1)
    blender.ok("create_object", kind="cube", name="B", size=1, location=[3, 0, 0], scale=2, rotation=[0, 0, 45])
    [pair] = blender.ok("measure_objects", objects=["A", "B"])["pairs"]
    # B is a 2 m cube turned 45 degrees, so its nearest corner is at x = 3 - sqrt(2), facing A's face at x = 0.5.
    assert pair["distance"] == pytest.approx(3 - 2 ** 0.5 - 0.5, abs=1e-5)


def test_overlapping_cubes_report_the_overlap_and_its_faces(blender):
    blender.ok("create_object", kind="cube", name="A", size=1)
    blender.ok("create_object", kind="cube", name="B", size=1, location=[0.5, 0.2, 0.1])
    [pair] = blender.ok("measure_objects", objects=["A", "B"])["pairs"]
    assert pair["overlapping"] is True and pair["distance"] == 0
    assert pair["overlap_faces"]["a"] > 0 and pair["overlap_faces"]["b"] > 0
    assert set(pair["faces"]["a"]) <= set(range(6)) and pair["faces"]["a"]


def test_every_pair_is_measured(blender):
    for i, x in enumerate((0, 2, 5)):
        blender.ok("create_object", kind="cube", name=f"C{i}", size=1, location=[x, 0, 0])
    pairs = blender.ok("measure_objects", objects=["C0", "C1", "C2"])["pairs"]
    assert [(p["a"], p["b"]) for p in pairs] == [("C0", "C1"), ("C0", "C2"), ("C1", "C2")]
    assert [p["distance"] for p in pairs] == pytest.approx([1.0, 4.0, 2.0], abs=1e-6)


def test_modifiers_count_in_the_measure(blender):
    blender.ok("create_object", kind="cube", name="A", size=1)
    blender.ok("create_object", kind="cube", name="B", size=1, location=[4, 0, 0])
    blender.ok("add_modifier", object="A", type="array", settings={"count": 3})
    [pair] = blender.ok("measure_objects", objects=["A", "B"])["pairs"]
    # The array modifier makes three cubes in a row that reach x = 2.5, and B starts at x = 3.5.
    assert pair["distance"] == pytest.approx(1.0, abs=1e-6)


def test_a_ray_hits_the_right_face(blender):
    blender.ok("create_object", kind="cube", name="Box", size=2)
    blender.ok("create_object", kind="cube", name="Other", size=1, location=[5, 0, 0])
    hit = blender.ok("measure_objects", objects=["Box"], origin=[0.2, 0.3, 5], direction="bottom")["ray"]
    assert hit["object"] == "Box" and hit["hit"] is True
    assert hit["location"] == pytest.approx([0.2, 0.3, 1.0], abs=1e-6)
    assert hit["normal"] == pytest.approx([0, 0, 1], abs=1e-6)
    assert hit["distance"] == pytest.approx(4.0, abs=1e-6)
    assert hit["face"] == face_facing(blender, "Box", "top")
    anything = blender.ok("measure_objects", origin=[0.2, 0.3, 5], direction=[0, 0, -1])["ray"]
    assert anything["object"] == "Box" and anything["face"] == hit["face"]
    side = blender.ok("measure_objects", origin=[10, 0, 0], direction="left")["ray"]
    assert side["object"] == "Other" and side["face"] == face_facing(blender, "Other", "right")


def test_a_ray_that_hits_nothing(blender):
    blender.ok("create_object", kind="cube", name="Box", size=1)
    assert blender.ok("measure_objects", origin=[0, 0, 5], direction="top")["ray"] == {"hit": False}


def test_a_cube_wholly_inside_another_is_said_to_be_inside(blender):
    blender.ok("create_object", kind="cube", name="Big", size=2)
    blender.ok("create_object", kind="cube", name="Inner", size=0.4, location=[0.3, 0, 0])
    [pair] = blender.ok("measure_objects", objects=["Big", "Inner"])["pairs"]
    assert pair["inside"] == "Inner" and pair["overlapping"] is False
    assert pair["distance"] == pytest.approx(0.5, abs=1e-6), "the distance is not the gap between the surfaces"
    [apart] = blender.ok("measure_objects", objects=["Inner", "Big"], origin=[5, 0, 0], direction="top")["pairs"]
    assert apart["inside"] == "Inner"
    overlaps = {i["code"]: i for i in blender.ok("check_asset")["issues"]}["overlaps"]
    assert overlaps["inside"] == [["Inner", "Big"]]
    rules = {"apart": [{"a": "Inner", "b": "Big"}]}
    crossing = {i["code"]: i for i in blender.ok("check_asset", rules=rules)["issues"]}["sets_intersect"]
    assert crossing["inside"] == [["Inner", "Big"]]


def test_side_by_side_cubes_and_an_open_sheet_are_not_inside_anything(blender):
    blender.ok("create_object", kind="cube", name="A", size=1)
    blender.ok("create_object", kind="cube", name="B", size=1, location=[2, 0, 0])
    blender.ok("create_object", kind="grid", name="Sheet", size=[4, 4, 1], location=[0, 0, -2])
    for pair in blender.ok("measure_objects", objects=["A", "B", "Sheet"])["pairs"]:
        assert "inside" not in pair, pair


def test_faces_lying_on_each_other_are_counted(blender):
    blender.ok("create_object", kind="plane", name="P1", size=2)
    blender.ok("create_object", kind="plane", name="P2", size=2)
    [pair] = blender.ok("measure_objects", objects=["P1", "P2"])["pairs"]
    assert pair["touching"] is True and pair["coplanar_faces"] == {"a": 1, "b": 1}


def stack_two_books(blender):
    """Build a book with a thin bump raised 1 mm above its cover, then drop a second book, turned 5 degrees, onto it.

    The lower book's cover is at z = 0.1 and the bump's top at z = 0.101, so the upper book comes to rest with its
    centre at z = 0.151. Return the result of the ``set_transform`` call that drops it.
    """
    blender.ok("create_object", kind="cube", name="Low", size=[0.4, 0.2, 0.1], location=[0, 0, 0.05])
    blender.ok("create_object", kind="cube", name="Bump", size=[0.04, 0.04, 0.001], location=[0.1, 0.05, 0.1005])
    blender.ok("join_objects", objects=["Low", "Bump"])
    blender.ok("create_object", kind="cube", name="High", size=[0.4, 0.2, 0.1], location=[0.01, 0, 0.3],
               rotation=[0, 0, 5])
    return blender.ok("set_transform", object="High", drop=True)


def test_a_book_dropped_onto_a_book_touches_it_and_does_not_cross_it(blender):
    dropped = stack_two_books(blender)
    assert dropped["landed_on"] == "Low"
    assert blender.ok("get_object", object="High")["world_location"][2] == pytest.approx(0.151, abs=1e-5)
    [pair] = blender.ok("measure_objects", objects=["Low", "High"])["pairs"]
    assert pair["overlapping"] is False, pair
    assert pair["touching"] is True and pair["distance"] == pytest.approx(0.0, abs=1e-5)
    rules = {"apart": [{"a": "Low", "b": "High"}]}
    checked = blender.ok("check_asset", rules=rules)
    codes = {i["code"] for i in checked["issues"]}
    assert "sets_intersect" not in codes and "overlaps" not in codes, checked["issues"]


def test_books_pushed_a_centimetre_into_each_other_cross(blender):
    stack_two_books(blender)
    blender.ok("set_transform", object="High", location=[None, None, 0.151 - 0.01])
    [pair] = blender.ok("measure_objects", objects=["Low", "High"])["pairs"]
    assert pair["overlapping"] is True and "touching" not in pair
    assert pair["overlap_faces"]["a"] > 0 and pair["overlap_faces"]["b"] > 0
    codes = {i["code"] for i in blender.ok("check_asset", rules={"apart": [{"a": "Low", "b": "High"}]})["issues"]}
    assert {"sets_intersect", "overlaps"} <= codes


def test_a_cube_resting_on_another_within_ten_microns_touches(blender):
    blender.ok("create_object", kind="cube", name="A", size=1)
    blender.ok("create_object", kind="cube", name="B", size=1, location=[0.3, 0.2, 1.000004])
    [pair] = blender.ok("measure_objects", objects=["A", "B"])["pairs"]
    assert pair["overlapping"] is False and pair["touching"] is True
    blender.ok("set_transform", object="B", location=[None, None, 0.999996])
    [sunk] = blender.ok("measure_objects", objects=["A", "B"])["pairs"]
    assert sunk["overlapping"] is False and sunk["touching"] is True, \
        "a contact 4 microns deep was taken for a crossing"


@pytest.mark.parametrize(("arguments", "kind"), [
    ({"objects": ["A"]}, "bad_argument"),
    ({"objects": ["A", "A"]}, "bad_argument"),
    ({"objects": ["A", "Ghost"]}, "not_found"),
    ({"origin": [0, 0, 1]}, "bad_argument"),
    ({"direction": "top"}, "bad_argument"),
    ({}, "bad_argument"),
    ({"objects": ["A", "B"], "origin": [0, 0, 1], "direction": [0, 0, 0]}, "bad_argument"),
    ({"objects": [f"N{i}" for i in range(17)]}, "bad_argument"),
])
def test_measure_objects_refuses_what_it_cannot_measure(blender, arguments, kind):
    blender.ok("create_object", kind="cube", name="A", size=1)
    blender.ok("create_object", kind="cube", name="B", size=1, location=[2, 0, 0])
    blender.refused("measure_objects", kind, **arguments)


# ---------------------------------------------------------------------------
# set_transform drop
# ---------------------------------------------------------------------------


def test_a_cube_dropped_over_a_plane_rests_on_it(blender):
    blender.ok("create_object", kind="plane", name="Floor", size=[6, 6, 1])
    blender.ok("create_object", kind="cube", name="Crate", size=1, location=[0.3, -0.2, 3], rotation=[0, 0, 30])
    out = blender.ok("set_transform", object="Crate", drop=True)
    assert out["landed_on"] == "Floor"
    assert out["moved_by"] == pytest.approx([0, 0, -2.5], abs=1e-5)
    assert world_bounds(blender, "Crate")["min"][2] == pytest.approx(0.0, abs=1e-5)
    assert blender.ok("get_object", object="Crate")["location"][:2] == pytest.approx([0.3, -0.2], abs=1e-6)


def test_drop_onto_a_named_object_ignores_what_is_between(blender):
    blender.ok("create_object", kind="plane", name="Floor", size=[6, 6, 1])
    blender.ok("create_object", kind="plane", name="Shelf", size=[2, 2, 1], location=[0, 0, 1])
    blender.ok("create_object", kind="cube", name="Crate", size=1, location=[0, 0, 4])
    assert blender.ok("set_transform", object="Crate", drop=True)["landed_on"] == "Shelf"
    assert world_bounds(blender, "Crate")["min"][2] == pytest.approx(1.0, abs=1e-5)
    out = blender.ok("set_transform", object="Crate", location=[None, None, 4], drop="Floor")
    assert out["landed_on"] == "Floor"
    assert world_bounds(blender, "Crate")["min"][2] == pytest.approx(0.0, abs=1e-5)


def test_drop_moves_children_with_it_and_lands_on_the_lowest_point(blender):
    blender.ok("create_object", kind="plane", name="Floor", size=[6, 6, 1])
    blender.ok("create_object", kind="cube", name="Body", size=1, location=[0, 0, 5])
    blender.ok("create_object", kind="cube", name="Leg", size=0.2, location=[0.3, 0.3, -1], parent="Body")
    blender.ok("set_transform", object="Body", drop=True)
    assert world_bounds(blender, "Leg")["min"][2] == pytest.approx(0.0, abs=1e-5)
    assert world_bounds(blender, "Body")["min"][2] == pytest.approx(0.6, abs=1e-5)


def test_a_cube_sunk_into_the_floor_rises_to_rest_on_it(blender):
    blender.ok("create_object", kind="plane", name="Floor", size=[6, 6, 1])
    blender.ok("create_object", kind="cube", name="Crate", size=1, location=[0, 0, 0.2])
    out = blender.ok("set_transform", object="Crate", drop=True)
    assert out["moved_by"] == pytest.approx([0, 0, 0.3], abs=1e-5)
    assert world_bounds(blender, "Crate")["min"][2] == pytest.approx(0.0, abs=1e-5)


def test_a_wide_top_dropped_over_a_narrow_pillar_rests_on_the_pillar(blender):
    blender.ok("create_object", kind="plane", name="Floor", size=[20, 20, 1])
    blender.ok("create_object", kind="cylinder", name="Pillar", radius=0.1, depth=2, location=[0, 0, 1])
    blender.ok("create_object", kind="cube", name="Tabletop", size=[2, 2, 0.1], location=[0, 0, 5])
    out = blender.ok("set_transform", object="Tabletop", drop=True)
    assert out["landed_on"] == "Pillar"
    assert world_bounds(blender, "Tabletop")["min"][2] == pytest.approx(2.0, abs=1e-5)
    named = blender.ok("set_transform", object="Tabletop", location=[None, None, 5], drop="Pillar")
    assert named["landed_on"] == "Pillar" and named["moved_by"] == pytest.approx([0, 0, -2.95], abs=1e-5)


def test_a_crate_dropped_over_a_spike_rests_on_its_tip(blender):
    blender.ok("create_object", kind="plane", name="Floor", size=[20, 20, 1])
    blender.ok("create_object", kind="cone", name="Spike", radius=0.3, depth=1, location=[0.2, 0.1, 0.5])
    blender.ok("create_object", kind="cube", name="Crate", size=1, location=[0, 0, 4])
    out = blender.ok("set_transform", object="Crate", drop=True)
    assert out["landed_on"] == "Spike"
    assert world_bounds(blender, "Crate")["min"][2] == pytest.approx(1.0, abs=1e-5)


def test_drop_with_nothing_below_or_onto_itself_is_refused_and_changes_nothing(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1, location=[0, 0, 3])
    blender.ok("create_object", kind="cube", name="Lid", size=0.5, location=[0, 0, 1], parent="Crate")
    blender.ok("create_object", kind="plane", name="Far", size=[1, 1, 1], location=[20, 0, 0])
    blender.ok("save_file", path=str(tmp_path / "drop.blend"))
    refused_unchanged(blender, "set_transform", "not_found", object="Crate", location=[1, 1, 3], drop=True)
    refused_unchanged(blender, "set_transform", "bad_argument", object="Crate", drop="Lid")
    refused_unchanged(blender, "set_transform", "not_found", object="Crate", drop="Ghost")
    refused_unchanged(blender, "set_transform", "bad_argument", object="Crate", drop="Far", scale=[1, "x", 1])


# ---------------------------------------------------------------------------
# apply_transform origin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("origin", "expected"), [
    ("bottom_center", [1.5, 2, 2]),
    ("bounds_center", [1.5, 2, 3]),
    ("geometry", [1.5, 2, 3]),
    ([0, 0, 0], [0, 0, 0]),
])
def test_moving_the_origin_keeps_the_mesh_and_the_children_where_they_are(blender, origin, expected):
    blender.ok("create_object", kind="cube", name="Body", size=[3, 2, 2], location=[1.5, 2, 3])
    blender.ok("create_object", kind="cube", name="Knob", size=0.2, location=[0.5, 0.5, 1.5], parent="Body")
    before = {name: world_bounds(blender, name) for name in ("Body", "Knob")}
    knob = blender.ok("get_object", object="Knob")["world_location"]
    out = blender.ok("apply_transform", object="Body", origin=origin)
    assert out["world_location"] == pytest.approx(expected, abs=1e-5)
    for name in ("Body", "Knob"):
        after = world_bounds(blender, name)
        assert after["min"] == pytest.approx(before[name]["min"], abs=1e-5)
        assert after["max"] == pytest.approx(before[name]["max"], abs=1e-5)
    assert blender.ok("get_object", object="Knob")["world_location"] == pytest.approx(knob, abs=1e-5)


def test_the_origin_alone_leaves_rotation_and_scale_unapplied(blender):
    blender.ok("create_object", kind="cube", name="Body", size=1, location=[0, 0, 1])
    blender.ok("set_transform", object="Body", scale=2, rotation=[0, 0, 30])
    out = blender.ok("apply_transform", object="Body", origin="bottom_center")
    assert out["scale"] == [2.0, 2.0, 2.0] and out["rotation"][2] == pytest.approx(30)
    assert out["world_location"] == pytest.approx([0, 0, 0], abs=1e-5)
    both = blender.ok("apply_transform", object="Body", origin="bottom_center", scale=True, rotation=True)
    assert both["scale"] == [1.0, 1.0, 1.0] and both["rotation"] == [0, 0, 0]
    assert world_bounds(blender, "Body")["min"][2] == pytest.approx(0.0, abs=1e-5)


def test_the_origin_moves_shape_keys_too(blender):
    blender.ok("create_object", kind="cube", name="Blob", size=1)
    blender.ok("run_python", code="obj = bpy.data.objects['Blob']\nobj.shape_key_add(name='Basis')\n"
                                  "key = obj.shape_key_add(name='Up')\nkey.value = 1\n"
                                  "for point in key.data: point.co.z += 1")
    before = world_bounds(blender, "Blob")
    blender.ok("apply_transform", object="Blob", origin="bottom_center")
    after = world_bounds(blender, "Blob")
    assert after["min"] == pytest.approx(before["min"], abs=1e-5)
    assert after["max"] == pytest.approx(before["max"], abs=1e-5)


def test_the_origin_in_edit_mode_is_refused_with_the_tab_hint(blender):
    from tests.test_edit_mode import enter, mode

    blender.ok("create_object", kind="cube", name="Box", size=1)
    enter(blender, "Box")
    payload = blender.refused("apply_transform", "bad_argument", object="Box", origin="bottom_center")
    assert "leave edit mode (tab)" in payload["hint"].lower()
    assert mode(blender, "Box") == "EDIT"


def test_origin_refusals_change_nothing(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Body", size=1)
    blender.ok("duplicate_object", object="Body", name="Twin", linked=True, offset=[3, 0, 0])
    blender.ok("create_object", kind="empty", name="Pivot", location=[0, 3, 0])
    blender.ok("save_file", path=str(tmp_path / "origin.blend"))
    shared = refused_unchanged(blender, "apply_transform", "bad_argument", object="Body", origin="bottom_center")
    assert "shares its mesh" in shared["error"], shared
    empty = refused_unchanged(blender, "apply_transform", "bad_argument", object="Pivot", origin="bottom_center")
    assert "only a mesh" in empty["error"], empty
    blender.ok("create_object", kind="cube", name="Solo", size=1, location=[0, -3, 0])
    blender.ok("save_file")
    refused_unchanged(blender, "apply_transform", "bad_argument", object="Solo", origin="middle")
    refused_unchanged(blender, "apply_transform", "bad_argument", object="Solo", origin=[0, 0, "x"])
    refused_unchanged(blender, "apply_transform", "bad_argument", object="Solo", origin=[1e12, 0, 0])


def test_an_origin_refused_after_asking_for_the_scale_leaves_the_scale_unapplied(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Masked", size=1, scale=2)
    blender.ok("run_python", code="obj = bpy.data.objects['Masked']\nobj.vertex_groups.new(name='none')\n"
                                  "obj.modifiers.new('Mask', 'MASK').vertex_group = 'none'")
    blender.ok("save_file", path=str(tmp_path / "masked.blend"))
    refused = refused_unchanged(blender, "apply_transform", "bad_argument", object="Masked", origin="bottom_center",
                                scale=True)
    assert "no vertices" in refused["error"]
    assert blender.ok("get_object", object="Masked")["scale"] == [2.0, 2.0, 2.0]


def test_the_origin_of_a_mesh_mirrored_about_it_is_refused(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Half", size=1)
    blender.ok("edit_mesh", object="Half", operation="translate", offset=[0.7, 0, 0])
    blender.ok("add_modifier", object="Half", type="mirror")
    blender.ok("save_file", path=str(tmp_path / "half.blend"))
    refused = refused_unchanged(blender, "apply_transform", "bad_argument", object="Half", origin=[1, 0, 0])
    assert "Mirror" in refused["error"] and "apply_modifier" in refused["hint"]


def test_a_child_parented_to_a_vertex_stays_where_it_is(blender):
    blender.ok("create_object", kind="cube", name="Body", size=1, location=[1, 2, 3], rotation=[10, 20, 30],
               scale=[1, 2, 3])
    blender.ok("run_python", code="""
body = bpy.data.objects["Body"]
for name, kind in (("Kid", "OBJECT"), ("VertKid", "VERTEX"), ("TriKid", "VERTEX_3")):
    child = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(child)
    child.parent = body
    child.parent_type = kind
    if kind == "VERTEX":
        child.parent_vertices[0] = 2
    elif kind == "VERTEX_3":
        child.parent_vertices = (0, 3, 5)
    child.location = (0.3, 0.1, 0.2)
""")
    where = {name: blender.ok("get_object", object=name)["world_location"] for name in ("Kid", "VertKid", "TriKid")}
    blender.ok("apply_transform", object="Body", origin="bottom_center")
    for name, before in where.items():
        assert blender.ok("get_object", object=name)["world_location"] == pytest.approx(before, abs=1e-5), name
