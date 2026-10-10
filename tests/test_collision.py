"""Tests for ``create_collision``, run against a real Blender.

They check collision proxies as Blender holds them, and as ``export_glb`` writes them for Godot and leaves them out
for three.js.
"""

import pytest

from tests.test_files import node_names
from tests.test_motion import saved_scene
from tests.test_refusals_change_nothing import refused_unchanged

pytestmark = pytest.mark.integration


@pytest.fixture
def scene(blender, tmp_path):
    """Provide the server with the saved scene that ``saved_scene`` builds."""
    return saved_scene(blender, tmp_path)


#: Code that returns how many of the object SOURCE's vertices lie outside the proxy PROXY, and how the proxy is shown,
#: parented, placed and linked.
INSIDE = """
source = bpy.data.objects[SOURCE]
proxy = bpy.data.objects[PROXY]
proxy_world = proxy.matrix_world
planes = [(proxy_world @ p.center, (proxy_world.to_3x3() @ p.normal).normalized()) for p in proxy.data.polygons]
outside = 0
for v in source.data.vertices:
    point = source.matrix_world @ v.co
    if any((point - centre).dot(normal) > 1e-4 for centre, normal in planes):
        outside += 1
result = {"outside": outside, "display": proxy.display_type, "hidden": proxy.hide_get(),
          "hide_render": proxy.hide_render, "parent": proxy.parent.name if proxy.parent else None,
          "same_place": all(abs(a - b) < 1e-6 for ra, rb in zip(proxy_world, source.matrix_world)
                            for a, b in zip(ra, rb)),
          "collections": [c.name for c in proxy.users_collection]}
"""


def inside(server, source, proxy) -> dict:
    """Return how many of the source's vertices lie outside the proxy, and the proxy's display, parent and place."""
    return server.ok("run_python", code=f"SOURCE = {source!r}\nPROXY = {proxy!r}\n" + INSIDE)["result"]


def test_a_convex_collider_of_a_cylinder_is_closed_and_holds_every_vertex(blender):
    blender.ok("create_object", kind="cylinder", name="Pillar", radius=0.5, depth=2, segments=16,
               location=[1, 2, 1], rotation=[0, 30, 0], collection="Props")
    out = blender.ok("create_collision", object="Pillar", shape="convex")
    assert out["object"] == "Pillar-convcolonly"
    assert out["closed"] is True and out["parent"] == "Pillar"
    assert abs(out["volume_ratio"] - 1.0) < 1e-3, "the hull of a cylinder is not the cylinder"
    held = inside(blender, "Pillar", "Pillar-convcolonly")
    assert held["outside"] == 0, "a vertex of the cylinder lies outside the hull"
    assert held["display"] == "WIRE" and held["hidden"] is False and held["hide_render"] is True
    assert held["parent"] == "Pillar" and held["same_place"], "the proxy does not sit exactly over its object"
    assert held["collections"] == ["Props"]


def test_three_js_leaves_the_proxies_out_and_godot_keeps_them_with_their_names(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    box = blender.ok("create_collision", object="Crate", shape="box")
    hull = blender.ok("create_collision", object="Crate", shape="convex")
    assert (box["object"], hull["object"]) == ("Crate-colonly", "Crate-convcolonly")
    web = blender.ok("export_glb", path=str(tmp_path / "web.glb"), target="threejs")
    assert node_names(web["contents"]) == {"Crate"}
    assert sorted(web["left_out"]) == ["Crate-colonly", "Crate-convcolonly"]
    godot = blender.ok("export_glb", path=str(tmp_path / "godot.glb"), target="godot")
    crate = next(node for node in godot["contents"]["nodes"] if node["name"] == "Crate")
    assert {child["name"] for child in crate["children"]} == {"Crate-colonly", "Crate-convcolonly"}


def test_a_proxy_is_marked_as_a_collision_proxy_in_the_exported_file(blender, tmp_path):
    """A proxy carries a ``blenderlens_collision`` custom property, which the exported file holds in the node's extras.

    A reader of the file can then tell a collision name made on purpose from a name that looks like an import hint by
    accident.
    """
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    blender.ok("create_collision", object="Crate", shape="box")
    blender.ok("create_collision", object="Crate", shape="convex", naming="plain")
    godot = blender.ok("export_glb", path=str(tmp_path / "godot.glb"), target="godot")
    crate = next(node for node in godot["contents"]["nodes"] if node["name"] == "Crate")
    marks = {child["name"]: child.get("extras", {}).get("blenderlens_collision") for child in crate["children"]}
    assert marks == {"Crate-colonly": "box", "Crate_collision": "convex"}
    assert "blenderlens_collision" not in crate.get("extras", {})


@pytest.mark.parametrize("naming", ["godot", "plain"])
def test_a_proxy_of_a_proxy_is_refused(scene, naming):
    proxy = scene.ok("create_collision", object="Box", shape="box", naming=naming)["object"]
    scene.ok("save_file")
    payload = refused_unchanged(scene, "create_collision", "bad_argument", object=proxy, shape="box")
    assert "Box" in payload["hint"], payload


def test_an_object_named_as_godots_collision_only_is_refused(scene):
    scene.ok("create_object", kind="cube", name="Wall-colonly", size=1)
    scene.ok("save_file")
    refused_unchanged(scene, "create_collision", "bad_argument", object="Wall-colonly")


BOUND = "import sys\nsys.modules['blenderlens_addon.collision'].MAX_SOURCE_VERTICES = {}"


def test_the_vertex_bound_counts_the_mesh_after_modifiers(scene):
    """The limit on a source object's vertices counts the mesh after modifiers, as the export sees it.

    A small cube with an array modifier has many vertices after modifiers, and the test lowers the limit so that
    going past it costs nothing.
    """
    scene.ok("create_object", kind="cube", name="Fence", size=1, location=[0, 5, 0])
    scene.ok("add_modifier", object="Fence", type="array", settings={"count": 20})
    scene.ok("save_file")
    scene.ok("run_python", code=BOUND.format(100))
    try:
        payload = refused_unchanged(scene, "create_collision", "bad_argument", object="Fence", shape="box")
        assert "160" in payload["error"] and "modifiers" in payload["error"], payload
    finally:
        scene.ok("run_python", code=BOUND.format(2_000_000))


def test_a_box_covers_the_bounds_and_a_decimated_proxy_keeps_its_share_of_faces(blender):
    blender.ok("create_object", kind="cube", name="Slab", size=[2, 1, 0.5], location=[0, 0, 3])
    box = blender.ok("create_collision", object="Slab", shape="box")
    assert box["dimensions"] == [2.0, 1.0, 0.5] and box["closed"] is True
    assert abs(box["volume"] - 1.0) < 1e-6 and abs(box["volume_ratio"] - 1.0) < 1e-6
    blender.ok("create_object", kind="uv_sphere", name="Globe", radius=1, segments=32, rings=16)
    whole = blender.ok("get_object", object="Globe")["mesh_evaluated"]["triangles"]
    rough = blender.ok("create_collision", object="Globe", shape="decimated", ratio=0.25)
    assert rough["object"] == "Globe-colonly" and rough["ratio"] == 0.25
    assert 0.15 * whole <= rough["mesh"]["triangles"] <= 0.35 * whole, (rough["mesh"], whole)
    assert 0.8 < rough["volume_ratio"] <= 1.0


def test_plain_naming_is_exported_everywhere(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    out = blender.ok("create_collision", object="Crate", shape="box", naming="plain")
    assert out["object"] == "Crate_collision"
    web = blender.ok("export_glb", path=str(tmp_path / "web.glb"), target="threejs")
    assert node_names(web["contents"]) == {"Crate", "Crate_collision"}


@pytest.mark.parametrize(("arguments", "kind"), [
    ({"object": "Nobody"}, "not_found"),
    ({"object": "Pivot"}, "bad_argument"),
    ({"object": "Box", "shape": "sphere"}, "bad_argument"),
    ({"object": "Box", "naming": "unreal"}, "bad_argument"),
    ({"object": "Box", "shape": "box", "ratio": 0.5}, "bad_argument"),
    ({"object": "Box", "shape": "decimated", "ratio": 0}, "bad_argument"),
    ({"object": "Box", "shape": "decimated", "ratio": 2}, "bad_argument"),
])
def test_create_collision_refused_changes_nothing(scene, arguments, kind):
    refused_unchanged(scene, "create_collision", kind, **arguments)


def test_a_flat_object_has_no_convex_proxy(scene):
    scene.ok("create_object", kind="plane", name="Floor", size=4)
    scene.ok("save_file")
    payload = refused_unchanged(scene, "create_collision", "bad_argument", object="Floor", shape="convex")
    assert "box" in payload["hint"]
    flat = scene.ok("create_collision", object="Floor", shape="box")
    assert flat["closed"] is True and flat["volume"] == 0.0
