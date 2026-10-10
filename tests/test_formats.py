"""Tests for ``export_file``, run against a real Blender.

FBX, OBJ, STL, PLY and USD files are written through Blender's own exporters, and each file is read back. The
read-back must leave the open file untouched.
"""

import pytest

from tests.test_motion import saved_scene
from tests.test_refusals_change_nothing import refused_unchanged, same, snapshot

pytestmark = pytest.mark.integration


@pytest.fixture
def scene(blender, tmp_path):
    """Provide the server with the saved scene that ``saved_scene`` builds."""
    return saved_scene(blender, tmp_path)


def a_crate_with_a_lid_and_a_hidden_post(server):
    """Build a crate with a lid parented to it, and a hidden post: two meshes shown, and a third hidden."""
    server.ok("create_object", kind="cube", name="Crate", size=1)
    server.ok("set_material", material="Wood", object="Crate", base_color="#8B5A2B")
    server.ok("create_object", kind="cube", name="Lid", size=[1, 1, 0.1], location=[0, 0, 0.6], parent="Crate")
    server.ok("create_object", kind="cylinder", name="Post", radius=0.1, depth=2, segments=8, location=[3, 0, 1])
    server.ok("set_object", object="Post", hide=True)


@pytest.mark.parametrize(("extension", "meshes", "importable"), [
    (".fbx", 2, True), (".obj", 2, True), (".stl", 1, True), (".ply", 1, True), (".usdz", 2, False),
    (".usda", 2, False),
])
def test_each_format_writes_a_file_that_reads_back_with_what_was_exported(blender, tmp_path, extension, meshes,
                                                                           importable):
    a_crate_with_a_lid_and_a_hidden_post(blender)
    path = tmp_path / f"crate{extension}"
    out = blender.ok("export_file", path=str(path), objects=["Crate"])
    assert path.is_file() and out["bytes"] == path.stat().st_size > 0
    assert sorted(out["objects"]) == ["Crate", "Lid"]
    assert out["read_back"] == {"meshes": meshes, "expected_meshes": meshes, "triangles": 24,
                                "expected_triangles": 24, "matches": True}
    if importable:
        imported = blender.ok("import_file", path=str(path), collection="Check")
        assert len([o for o in imported["objects"] if o["type"] == "MESH"]) == meshes


def test_the_read_back_leaves_the_open_file_as_it_was(blender, tmp_path):
    a_crate_with_a_lid_and_a_hidden_post(blender)
    blender.ok("save_file", path=str(tmp_path / "crate.blend"))
    before = snapshot(blender)
    for extension in (".fbx", ".obj", ".usdz"):
        blender.ok("export_file", path=str(tmp_path / f"all{extension}"))
    same(before, snapshot(blender), "export_file")


def test_hidden_objects_go_out_only_when_asked_and_stay_hidden(blender, tmp_path):
    a_crate_with_a_lid_and_a_hidden_post(blender)
    shown = blender.ok("export_file", path=str(tmp_path / "shown.obj"))
    assert sorted(shown["objects"]) == ["Crate", "Lid"]
    every = blender.ok("export_file", path=str(tmp_path / "every.obj"), visible_only=False)
    assert sorted(every["objects"]) == ["Crate", "Lid", "Post"]
    assert every["read_back"]["meshes"] == 3 and every["read_back"]["matches"] is True
    assert every["materials_file"] == str(tmp_path / "every.mtl")
    post = blender.ok("get_object", object="Post")
    assert post["visible"] is False


def test_a_collection_is_exported_on_its_own(blender, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1, collection="Props")
    blender.ok("create_object", kind="cube", name="Wall", size=1, location=[4, 0, 0])
    out = blender.ok("export_file", path=str(tmp_path / "props.ply"), collection="Props")
    assert out["objects"] == ["Crate"] and out["read_back"]["matches"] is True


def test_files_an_exporter_writes_beside_its_own_are_named(blender, tmp_path):
    """The result names every file an exporter writes besides the one asked for.

    The USD exporter writes the images a material uses into a textures folder beside the file, and the OBJ exporter
    writes a .mtl file.
    """
    blender.ok("run_python", code=f"image = bpy.data.images.new('Grain', 4, 4)\n"
                                  f"image.filepath_raw = {str(tmp_path / 'grain.png')!r}\n"
                                  "image.file_format = 'PNG'\nimage.save()")
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    blender.ok("unwrap_uv", object="Crate", method="smart")
    blender.ok("set_material", material="Wood", object="Crate", base_color_image=str(tmp_path / "grain.png"))
    out_dir = tmp_path / "out"
    usd = blender.ok("export_file", path=str(out_dir / "crate.usdc"))
    written = sorted(p for p in out_dir.rglob("*") if p.is_file() and p.name != "crate.usdc")
    assert written, "the USD exporter wrote no textures beside the file"
    assert usd["also_written"] == [str(p) for p in written]
    obj = blender.ok("export_file", path=str(out_dir / "crate.obj"))
    assert obj["also_written"] == [str(out_dir / "crate.mtl")]


@pytest.mark.parametrize(("arguments", "kind"), [
    ({"path": "box.glb"}, "bad_argument"),
    ({"path": "box.dae"}, "bad_argument"),
    ({"path": "box.fbx", "objects": ["Nobody"]}, "not_found"),
    ({"path": "box.fbx", "objects": ["Pivot"]}, "not_found"),
    ({"path": "box.fbx", "collection": "Nowhere"}, "not_found"),
])
def test_export_file_refused_changes_nothing(scene, tmp_path, arguments, kind):
    arguments = {**arguments, "path": str(tmp_path / arguments["path"])}
    payload = refused_unchanged(scene, "export_file", kind, **arguments)
    if arguments["path"].endswith(".glb"):
        assert "export_glb" in payload["hint"]
    assert not list(tmp_path.glob("box.*"))
