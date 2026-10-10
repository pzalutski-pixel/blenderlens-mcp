"""Tests for a project's asset rules, checked against asset descriptions built here, without Blender.

The add-on's check_asset and the ``check`` command both describe an asset in the same neutral form and hand it
to the same rules module, so one set of rules reads the same for a .blend as for a .glb. The description lists
the objects with their names, parents, extras, triangle counts and world bounds in Blender's axes, and the
materials with their values.
"""

import importlib.util
from pathlib import Path

import pytest

from blenderlens_mcp import server

RULES_FILE = Path(server.__file__).with_name("addon") / "rules.py"
#: The example rule set that ships with the repository.
EXAMPLE_RULES = Path(__file__).resolve().parent.parent / "examples" / "rules.json"


def load_rules():
    """Load and return addon/rules.py by its path, because importing the add-on package would import ``bpy``."""
    spec = importlib.util.spec_from_file_location("blenderlens_rules_under_test", RULES_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rules = load_rules()


def obj(name, parent=None, *, triangles=12, bounds=((-0.5, -0.5, 0.0), (0.5, 0.5, 1.0)), origin=(0.0, 0.0, 0.0),
        extras=None, materials=(), kind="MESH"):
    """Return an object as the asset description lists it, a unit box standing on the origin unless told otherwise."""
    return {"name": name, "parent": parent, "type": kind, "triangles": triangles, "extras": extras or {},
            "bounds": [list(bounds[0]), list(bounds[1])] if bounds else None, "origin": list(origin),
            "materials": list(materials)}


def material(name, base=(0.5, 0.5, 0.5), metallic=0.0, roughness=0.5, emission=(0.0, 0.0, 0.0)):
    """Return a material as the asset description lists it; ``None`` stands for a value that nodes set."""
    return {"name": name, "base_color": list(base) if base is not None else None, "metallic": metallic,
            "roughness": roughness, "emission": list(emission) if emission is not None else None}


def asset(*objects, materials=()):
    """Return an asset description holding ``objects`` and ``materials``."""
    return {"objects": list(objects), "materials": list(materials)}


def codes(issues) -> list:
    """Return the sorted codes of ``issues``."""
    return sorted(issue["code"] for issue in issues)


def issue(issues, code) -> dict:
    """Return the first issue with ``code``, failing the test when there is none."""
    found = [i for i in issues if i["code"] == code]
    assert found, f"no {code} among {issues}"
    return found[0]


# ---------------------------------------------------------------------------
# The format itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("given", "words"), [
    ([], "object"),
    ({"requird": ["A"]}, "requird"),
    ({"required": "A"}, "list"),
    ({"required": [3]}, "name"),
    ({"required": ["re:("]}, "pattern"),
    ({"single_root": "yes"}, "true or false"),
    ({"palette": {"Wood": {"colour": "#ffffff"}}}, "colour"),
    ({"palette": {"Wood": {"base_color": "#12"}}}, "base_color"),
    ({"palette": {"Wood": {"metallic": 2}}}, "metallic"),
    ({"palette_tolerance": -1}, "palette_tolerance"),
    ({"extras": [{"objects": "A", "keys": {"k": "float"}}]}, "float"),
    ({"extras": [{"keys": {"k": "number"}}]}, "objects"),
    ({"size": [{"objects": "A", "min": [1, 2]}]}, "min"),
    ({"size": [{"objects": "A", "max": [1, 2, -3]}]}, "max"),
    ({"budget": [{"objects": "A", "triangles": -5}]}, "triangles"),
    ({"budget": "lots"}, "budget"),
    ({"apart": [{"a": "A"}]}, "b"),
    ({"apart": [{"a": "A", "b": "B", "allow": [["A"]]}]}, "allow"),
    ({"origin_at_base": 5}, "origin_at_base"),
])
def test_a_rule_set_that_does_not_fit_the_format_is_refused_naming_the_key(given, words):
    with pytest.raises(rules.RulesError) as caught:
        rules.validate(given)
    assert words in str(caught.value), str(caught.value)


def test_an_empty_rule_set_finds_nothing():
    assert rules.evaluate(rules.validate({}), asset(obj("A"))) == []


# ---------------------------------------------------------------------------
# Each rule
# ---------------------------------------------------------------------------


def test_required_names_patterns_and_parent_child_paths():
    found = asset(obj("Lamp"), obj("Body", "Lamp"), obj("Hook", "Lamp", kind="EMPTY"))
    checked = rules.validate({"required": ["Lamp", "Lamp/Hook", "re:^Bod.$", "Cord", "Body/Hook",
                                           "re:^Panel_"]})
    missing = issue(rules.evaluate(checked, found), "missing_required")
    assert missing["names"] == ["Cord", "Body/Hook", "re:^Panel_"]
    assert missing["severity"] == "error" and missing["count"] == 3


def test_a_name_matches_itself_with_blenders_suffix_as_clean_names_writes_it():
    """A rule naming Handle also matches Handle.001, as export_glb's ``clean_names`` writes Handle.001 as Handle.

    Only Blender's suffix, a dot and three or more digits, is ignored.
    """
    found = asset(obj("Tray"), obj("Handle.001", "Tray", kind="EMPTY"), obj("Plank.002"), obj("Plank_1"),
                  obj("Cap.1"))
    checked = rules.validate({"required": ["Tray/Handle", "Plank", "Cap", "re:^Tray$/re:^Handle"],
                              "size": [{"objects": "Plank", "max": [0.1, 0.1, 0.1]}]})
    issues = rules.evaluate(checked, found)
    assert issue(issues, "missing_required")["names"] == ["Cap"], \
        "Cap.1 was taken for Cap, though Blender's suffix is a dot and three or more digits"
    assert issue(issues, "size_out_of_range")["objects"] == ["Plank.002"], "Plank_1 was taken for Plank"


def test_either_half_of_a_path_may_be_a_pattern():
    found = asset(obj("Tray"), obj("Handle.001", "Tray", kind="EMPTY"), obj("CrateLid"),
                  obj("Handle.002", "CrateLid", kind="EMPTY"))
    checked = rules.validate({"required": ["Tray/re:^Handle", "re:^Crate/Handle",
                                           "re:^(Tray|Crate)/re:^Handle\\.00[12]$", "re:^Plank/Handle"]})
    assert issue(rules.evaluate(checked, found), "missing_required")["names"] == ["re:^Plank/Handle"]
    with pytest.raises(rules.RulesError, match="pattern"):
        rules.validate({"required": ["Tray/re:("]})


def test_a_missing_path_names_the_children_its_parent_has():
    found = asset(obj("Tray"), obj("Knob", "Tray", kind="EMPTY"), obj("Rib", "Tray"))
    missing = issue(rules.evaluate(rules.validate({"required": ["Tray/Handle", "Ghost/Handle"]}), found),
                    "missing_required")
    assert missing["names"] == ["Tray/Handle", "Ghost/Handle"]
    assert missing["children"] == {"Tray/Handle": ["Knob", "Rib"], "Ghost/Handle": []}
    assert "Tray has the children Knob, Rib." in missing["message"], missing["message"]
    assert "No object matches Ghost." in missing["message"], missing["message"]


def test_a_check_narrowed_to_some_objects_skips_the_rules_that_match_none_of_them():
    every = rules.validate({"size": [{"objects": "Tray", "max": [1, 1, 1]}], "budget": [{"objects": "Tray",
                                                                                        "triangles": 9}],
                            "extras": [{"objects": "Tray", "keys": {"k": "number"}}],
                            "origin_at_base": ["Tray"], "apart": [{"a": "Tray", "b": "Plank"}]})
    found = asset(obj("Plank"))
    assert rules.evaluate(every, found, narrowed=True) == []
    assert codes(rules.evaluate(every, found)) == ["rule_matches_nothing"] * 5


def test_a_single_root():
    rule = rules.validate({"single_root": True})
    assert rules.evaluate(rule, asset(obj("A"), obj("B", "A"))) == []
    roots = issue(rules.evaluate(rule, asset(obj("A"), obj("B"))), "not_single_root")
    assert roots["objects"] == ["A", "B"]


def test_retired_names_of_objects_and_materials():
    rule = rules.validate({"retired": ["Pine_Old", "re:^Shelf_v[12]$"]})
    found = rules.evaluate(rule, asset(obj("Shelf_v1", materials=["Pine_Old"]), obj("Shelf_v3"),
                                       materials=[material("Pine_Old")]))
    retired = issue(found, "retired_name")
    assert retired["objects"] == ["Shelf_v1"] and retired["materials"] == ["Pine_Old"]


def test_the_palette_with_its_tolerance():
    wood = material("Wood", base=(0.2582, 0.1022, 0.0242), metallic=0.0, roughness=0.8)
    off = material("Steel", base=(0.2, 0.2, 0.2), metallic=0.5, roughness=0.5)
    stray = material("Scratch", base=(1, 0, 0))
    rule = rules.validate({
        "palette": {"Wood": {"base_color": [0.2583, 0.1022, 0.0242], "metallic": 0.0, "roughness": 0.8},
                    "Steel": {"base_color": "#7a7a7a", "metallic": 0.5, "roughness": 0.5}},
        "palette_only": True,
    })
    found = rules.evaluate(rule, asset(obj("Plank", materials=["Wood", "Steel", "Scratch"]),
                                       materials=[wood, off, stray]))
    assert codes(found) == ["not_in_palette", "off_palette"]
    assert issue(found, "off_palette")["materials"] == ["Steel"]
    assert "base_color" in issue(found, "off_palette")["message"]
    assert issue(found, "not_in_palette")["materials"] == ["Scratch"]


def test_the_palette_compares_emission_as_colour_times_strength():
    rule = rules.validate({"palette": {"Bulb": {"base_color": [0.9, 0.9, 0.9], "emission": [0.5, 0.4, 0.25],
                                                "strength": 2.0}}})
    glowing = material("Bulb", base=(0.9, 0.9, 0.9), emission=(1.0, 0.8, 0.5))
    assert rules.evaluate(rule, asset(obj("Lamp", materials=["Bulb"]), materials=[glowing])) == []
    dim = material("Bulb", base=(0.9, 0.9, 0.9), emission=(0.5, 0.4, 0.25))
    assert codes(rules.evaluate(rule, asset(obj("Lamp", materials=["Bulb"]), materials=[dim]))) == ["off_palette"]
    # A palette colour without emission must not glow.
    plain = rules.validate({"palette": {"Wood": {"base_color": [0.1, 0.1, 0.1]}}})
    lit = material("Wood", base=(0.1, 0.1, 0.1), emission=(1.0, 0.0, 0.0))
    assert codes(rules.evaluate(plain, asset(obj("Plank", materials=["Wood"]), materials=[lit]))) == ["off_palette"]


def test_a_palette_value_driven_by_nodes_cannot_match():
    rule = rules.validate({"palette": {"Wood": {"base_color": [0.1, 0.1, 0.1]}}})
    linked = material("Wood", base=None)
    off = issue(rules.evaluate(rule, asset(obj("Plank", materials=["Wood"]), materials=[linked])), "off_palette")
    assert "nodes" in off["message"]


def test_required_extras_their_types_and_extras_only_one_object_may_carry():
    rule = rules.validate({"extras": [
        {"objects": "re:^(Plank|Board)$", "keys": {"weight_kg": "number", "kind": "string"}},
        {"objects": "Lid_Open", "keys": {"hidden_by_default": "boolean"}, "only": True},
    ]})
    found = rules.evaluate(rule, asset(
        obj("Plank", extras={"weight_kg": 1.5, "kind": "plank"}),
        obj("Board", extras={"weight_kg": "heavy"}),
        obj("Lid_Open", extras={"hidden_by_default": True}, kind="EMPTY"),
        obj("Lid_Closed", extras={"hidden_by_default": False}, kind="EMPTY"),
    ))
    assert codes(found) == ["extra_not_allowed", "missing_extra", "wrong_extra_type"]
    assert issue(found, "missing_extra")["objects"] == ["Board"]
    assert issue(found, "wrong_extra_type")["objects"] == ["Board"]
    assert issue(found, "extra_not_allowed")["objects"] == ["Lid_Closed"]


def test_size_ranges_measure_the_whole_subtree_in_blender_axes():
    rule = rules.validate({"size": [{"objects": "Tray", "min": [0.33, 0.23, None], "max": [0.37, 0.27, 0.02]}]})
    flat = asset(obj("Tray", bounds=((-0.175, -0.125, 0), (0.175, 0.125, 0.015))))
    assert rules.evaluate(rule, flat) == []
    # A child sticking up makes the tray too thick.
    lumpy = asset(obj("Tray", bounds=((-0.175, -0.125, 0), (0.175, 0.125, 0.015))),
                  obj("Peg", "Tray", bounds=((0, 0, 0), (0.01, 0.01, 0.05))))
    out = issue(rules.evaluate(rule, lumpy), "size_out_of_range")
    assert out["objects"] == ["Tray"]
    assert out["sizes"]["Tray"] == pytest.approx([0.35, 0.25, 0.05])


def test_origin_at_the_base_of_the_subtree():
    rule = rules.validate({"origin_at_base": True})
    standing = asset(obj("Crate", bounds=((-0.5, -0.5, 0), (0.5, 0.5, 1)), origin=(0, 0, 0)))
    assert rules.evaluate(rule, standing) == []
    centred = asset(obj("Crate", bounds=((-0.5, -0.5, 0), (0.5, 0.5, 1)), origin=(0, 0, 0.5)))
    assert issue(rules.evaluate(rule, centred), "origin_not_at_base")["objects"] == ["Crate"]
    # With true, only root objects are checked, and a child is not a root. A list of names can name any object.
    child = asset(obj("Crate"), obj("Lid", "Crate", bounds=((-0.5, -0.5, 1), (0.5, 0.5, 1.1)), origin=(0, 0, 0)))
    assert rules.evaluate(rule, child) == []
    named = rules.validate({"origin_at_base": ["Lid"]})
    assert issue(rules.evaluate(named, child), "origin_not_at_base")["objects"] == ["Lid"]


def test_a_triangle_budget_per_subtree_and_for_everything():
    found = asset(obj("Tower", triangles=1000), obj("Bell", "Tower", triangles=600), obj("Bench", triangles=50))
    per = rules.validate({"budget": [{"objects": "Tower", "triangles": 1500}, {"objects": "Bench", "triangles": 60}]})
    over = issue(rules.evaluate(per, found), "over_budget")
    assert over["objects"] == ["Tower"] and over["triangles"] == {"Tower": 1600}
    total = rules.validate({"budget": 1500})
    assert issue(rules.evaluate(total, found), "over_budget")["triangles"] == {"all": 1650}


def test_sets_that_must_not_intersect_with_an_allow_list():
    rule = rules.validate({"apart": [{"a": "re:^Trim_", "b": ["Body", "Drawer"],
                                      "allow": [["Body", "Trim_Strip"]]}]})
    found = asset(obj("Body"), obj("Drawer"), obj("Trim_Strip"), obj("Trim_Knob"))
    touching = {frozenset(("Trim_Strip", "Body")), frozenset(("Trim_Knob", "Drawer"))}
    issues = rules.evaluate(rule, found, overlap=lambda a, b: frozenset((a, b)) in touching)
    crossing = issue(issues, "sets_intersect")
    assert crossing["pairs"] == [["Trim_Knob", "Drawer"]]


def test_a_rule_that_needs_geometry_is_reported_unchecked_without_it():
    rule = rules.validate({"apart": [{"a": "A", "b": "B"}]})
    unchecked = issue(rules.evaluate(rule, asset(obj("A"), obj("B"))), "not_checked")
    assert unchecked["severity"] == "info"


def test_a_rule_that_matches_nothing_says_so():
    rule = rules.validate({"size": [{"objects": "Tary", "max": [1, 1, 1]}]})
    nothing = issue(rules.evaluate(rule, asset(obj("Tray"))), "rule_matches_nothing")
    assert nothing["severity"] == "warning" and "Tary" in nothing["message"]


def test_the_example_rule_set_is_valid_and_passes_a_crate_stack():
    """examples/rules.json is a valid rule set, and a crate stack that follows it passes.

    The stack has no errors under the example's rules. Rules that match none of its objects only warn.
    """
    import json

    checked = rules.validate(json.loads(EXAMPLE_RULES.read_text(encoding="utf-8")))
    stack = asset(obj("Crate_Stack", bounds=((-0.3, -0.3, 0), (0.3, 0.3, 1.0))),
                  obj("Crate_1", "Crate_Stack", bounds=((-0.25, -0.25, 0), (0.25, 0.25, 0.5)), materials=["Wood"]),
                  materials=[material("Wood", base=rules.colour("#8b5a2b", "x"), metallic=0.0, roughness=0.8)])
    assert [i["code"] for i in rules.evaluate(checked, stack, overlap=lambda a, b: False)
            if i["severity"] == "error"] == []


def test_issue_lists_are_bounded_with_a_count():
    many = asset(*[obj(f"Part{i}") for i in range(500)])
    roots = issue(rules.evaluate(rules.validate({"single_root": True}), many), "not_single_root")
    assert roots["count"] == 500 and len(roots["objects"]) <= rules.LISTED


@pytest.mark.parametrize("pattern", ["^(a+)+$", "(a*)*b", "(\\w+\\s?)*$", "(?:ab+)+", "(a|aa)+", "(x+){2,}",
                                     "((ab)*c)*"])
def test_a_pattern_that_can_backtrack_without_end_is_refused(pattern):
    with pytest.raises(rules.RulesError) as caught:
        rules.validate({"required": [f"re:{pattern}"]})
    assert "repeat" in str(caught.value), str(caught.value)


@pytest.mark.parametrize("pattern", ["^(Plank|Board|Beam)(_Stack)?$", "_[23]$", "^a+$", "\\d{3}",
                                     "^(Top|Base)_Shelf$", "^(ab)?c+$", "[(]+x"])
def test_patterns_that_repeat_nothing_nested_are_accepted(pattern):
    rules.validate({"required": [f"re:{pattern}"]})


def test_a_pattern_reads_a_bounded_part_of_a_long_name():
    """A pattern is matched against a bounded part of a long name, so the match finishes quickly.

    Three repeats side by side cost the fourth power of the length searched, and 400 characters would take seconds.
    """
    import time

    checked = rules.validate({"retired": ["re:a*a*a*b"]})
    started = time.perf_counter()
    assert rules.evaluate(checked, asset(obj("a" * 400))) == []
    assert time.perf_counter() - started < 0.3
    assert rules.evaluate(checked, asset(obj("a" * 60 + "b"))) != [], "a name of 61 characters was not read whole"


def test_a_pattern_of_more_than_three_repeats_is_refused():
    with pytest.raises(rules.RulesError) as caught:
        rules.validate({"retired": ["re:a*a*a*a*b"]})
    assert "repeat" in str(caught.value)


def test_objects_sharing_a_name_keep_their_own_subtrees():
    """Objects that share a name keep their own subtrees, told apart by the ids in the asset description.

    Two objects named Hand each hold their own Finger, 10 m from the other, and neither Hand's size or triangle
    count includes the other Hand's Finger.
    """
    hands = asset({**obj("Left", bounds=None, triangles=0), "id": 0},
                  {**obj("Right", bounds=None, triangles=0), "id": 1},
                  {**obj("Hand", "Left", bounds=None, triangles=0), "id": 2, "parent_id": 0},
                  {**obj("Hand", "Right", bounds=None, triangles=0), "id": 3, "parent_id": 1},
                  {**obj("Finger", "Hand", bounds=((-5.5, 0, 0), (-4.5, 1, 1))), "id": 4, "parent_id": 2},
                  {**obj("Finger", "Hand", bounds=((4.5, 0, 0), (5.5, 1, 1))), "id": 5, "parent_id": 3})
    checked = rules.validate({"size": [{"objects": "Hand", "max": [1.5, 1.5, 1.5]}], "single_root": True,
                              "budget": [{"objects": "Hand", "triangles": 12}]})
    found = codes(rules.evaluate(checked, hands))
    assert found == ["not_single_root"], found


def test_an_object_wholly_inside_another_is_not_apart():
    rule = rules.validate({"apart": [{"a": "Core", "b": ["Casing", "Lid"]}]})
    found = asset(obj("Core"), obj("Casing"), obj("Lid"))
    issues = rules.evaluate(rule, found, overlap=lambda a, b: "Core" if {a, b} == {"Core", "Casing"} else False)
    crossing = issue(issues, "sets_intersect")
    assert crossing["pairs"] == [["Core", "Casing"]] and crossing["inside"] == [["Core", "Casing"]]
    assert "inside" in crossing["message"]


def test_an_apart_rule_past_its_pair_bound_is_reported_unchecked_before_listing_the_pairs():
    """An ``apart`` rule with too many pairs is reported as not checked, before the pairs are listed.

    1,500 objects against 1,500 make 2.25 million pairs, so the limit is checked from the counts, not from a list.
    """
    import time

    many = asset(*[obj(f"A{i}") for i in range(1500)], *[obj(f"B{i}") for i in range(1500)])
    rule = rules.validate({"apart": [{"a": "re:^A", "b": "re:^B"}]})
    started = time.perf_counter()
    unchecked = issue(rules.evaluate(rule, many, overlap=lambda a, b: False), "not_checked")
    assert time.perf_counter() - started < 0.5
    assert "2,250,000" in unchecked["message"]
