"""Tests for ``run_script``, run against a real background Blender.

A script runs as a file: beside its own helper modules, with the folders above it up to a ``blenderlens.toml``
importable, given its ``args``, and with its helpers read afresh on each run. The result names the objects the
run added, removed and changed, and the tests that read those names compare them with what Blender holds.
"""

import pytest

from tests.test_refusals_change_nothing import refused_unchanged

pytestmark = pytest.mark.integration

HELPERS = '''
import bmesh
import bpy


def cube(name, size):
    """Return a new cube of edge ``size`` metres, linked into the scene."""
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=size)
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj
'''

BUILD = '''
import helpers

obj = helpers.cube(args["name"], args["size"])
print("built", obj.name)
result = {"made": obj.name, "running_as": __name__, "file": __file__}
'''


@pytest.fixture
def project(tmp_path):
    """Return a model's folder, holding build.py and the helpers.py it imports."""
    folder = tmp_path / "crate"
    folder.mkdir()
    (folder / "helpers.py").write_text(HELPERS, encoding="utf-8")
    (folder / "build.py").write_text(BUILD, encoding="utf-8")
    return folder


def test_a_script_beside_its_helpers_builds_from_args_and_names_what_it_added(blender, project):
    script = project / "build.py"
    out = blender.ok("run_script", path=str(script), args={"name": "Crate", "size": 0.5})
    assert out["result"] == {"made": "Crate", "running_as": "__main__", "file": str(script)}
    assert "built Crate" in out["stdout"]
    assert out["added"] == ["Crate"] and out["removed"] == [] and out["changed"] == []
    assert out["counts"] == {"added": 1, "removed": 0, "changed": 0, "objects": 1}
    crate = blender.ok("get_object", object="Crate")
    assert crate["dimensions"] == pytest.approx([0.5, 0.5, 0.5])
    assert blender.ok("get_scene")["unsaved_changes"] is True


def test_the_run_leaves_sys_path_and_the_helper_modules_as_they_were(blender, project):
    blender.ok("run_script", path=str(project / "build.py"), args={"name": "Crate", "size": 0.5})
    left = blender.ok("run_python", code=f"""
import sys
result = {{"path": [p for p in sys.path if {str(project)!r} in p], "helpers": "helpers" in sys.modules}}
""")["result"]
    assert left == {"path": [], "helpers": False}
    assert not list(project.glob("__pycache__")), "bytecode was written beside the scripts"


def test_an_edited_helper_is_read_again_on_the_next_run(blender, project):
    blender.ok("run_script", path=str(project / "build.py"), args={"name": "Crate", "size": 0.5})
    (project / "helpers.py").write_text(HELPERS.replace("size=size", "size=size * 2"), encoding="utf-8")
    out = blender.ok("run_script", path=str(project / "build.py"), args={"name": "Crate", "size": 0.5}, fresh=True,
                     discard=True)
    assert out["added"] == ["Crate"], "with fresh, the second run did not start from an empty file"
    assert blender.ok("get_object", object="Crate")["dimensions"] == pytest.approx([1.0, 1.0, 1.0])


def test_folders_up_to_a_blenderlens_toml_are_importable(blender, tmp_path):
    """The folders up to the one holding blenderlens.toml are importable, and their modules are read afresh.

    A model's build script imports the project's shared palette from a folder above its own.
    """
    root = tmp_path / "models"
    (root / "crate").mkdir(parents=True)
    (root / "shared").mkdir()
    (root / "blenderlens.toml").write_text("", encoding="utf-8")
    (root / "palette.py").write_text("PINE = (0.8, 0.6, 0.4)\n", encoding="utf-8")
    # A folder without __init__.py, which Python imports as a namespace package.
    (root / "shared" / "card.py").write_text("SIZE = 0.5\n", encoding="utf-8")
    (root / "crate" / "build.py").write_text("import palette\nfrom shared import card\n"
                                             "result = [*palette.PINE, card.SIZE]\n", encoding="utf-8")
    assert blender.ok("run_script", path=str(root / "crate" / "build.py"))["result"] == [0.8, 0.6, 0.4, 0.5]
    left = blender.ok("run_python", code="import sys\n"
                                         "result = sorted({'palette', 'shared', 'shared.card'} & set(sys.modules))")
    assert left["result"] == [], "the project's modules, the namespace package among them, were left in sys.modules"

    (root / "blenderlens.toml").unlink()
    payload = blender.refused("run_script", "python_error", path=str(root / "crate" / "build.py"))
    assert "palette" in payload["error"], "without the marker, a folder other than the script's own was importable"


SHARED_BY_HAND = '''
import os
import sys


def _shared_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")


sys.path.insert(0, _shared_dir())
import palette  # noqa: E402

result = {"wood": palette.WOOD, "on_path": sys.path.count(_shared_dir())}
'''


def test_a_folder_the_script_puts_on_sys_path_itself_is_taken_off_and_its_modules_read_again(blender, tmp_path):
    """A folder the script puts on sys.path itself is taken off after the run, and its modules are read again.

    This covers build scripts that add a shared folder to sys.path by hand, with no blenderlens.toml.
    """
    shared = tmp_path / "models" / "shared"
    shared.mkdir(parents=True)
    (tmp_path / "models" / "crate").mkdir()
    (shared / "palette.py").write_text("WOOD = 'v1'\n", encoding="utf-8")
    script = tmp_path / "models" / "crate" / "build.py"
    script.write_text(SHARED_BY_HAND, encoding="utf-8")
    assert blender.ok("run_script", path=str(script))["result"] == {"wood": "v1", "on_path": 1}
    (shared / "palette.py").write_text("WOOD = 'v2'\n", encoding="utf-8")
    assert blender.ok("run_script", path=str(script))["result"] == {"wood": "v2", "on_path": 1}
    left = blender.ok("run_python", code=f"import sys\n"
                                         f"result = [{str(shared)!r} in sys.path, 'palette' in sys.modules]")
    assert left["result"] == [False, False]


def test_a_module_found_through_a_folder_already_on_sys_path_stays_imported(blender, tmp_path):
    """After a run, only the modules imported through the run's own folders are dropped.

    A module under the project's root that was reached through a path entry already there before the run, such as
    an add-on's folder or site-packages, stays imported.
    """
    root = tmp_path / "root"
    (root / "lib").mkdir(parents=True)
    (root / "model").mkdir()
    (root / "blenderlens.toml").write_text("", encoding="utf-8")
    (root / "lib" / "lazymod_9b.py").write_text("VALUE = 42\n", encoding="utf-8")
    (root / "model" / "build.py").write_text("import lazymod_9b\nresult = lazymod_9b.VALUE\n", encoding="utf-8")
    lib = str(root / "lib")
    blender.ok("run_python", code=f"import sys\nsys.path.append({lib!r})")
    try:
        assert blender.ok("run_script", path=str(root / "model" / "build.py"))["result"] == 42
        left = blender.ok("run_python", code=f"import sys\nresult = ['lazymod_9b' in sys.modules, {lib!r} in sys.path]")
        assert left["result"] == [True, True]
    finally:
        blender.ok("run_python", code=f"import sys\nsys.modules.pop('lazymod_9b', None)\n"
                                      f"sys.path[:] = [p for p in sys.path if p != {lib!r}]")


def test_what_a_script_prints_is_kept_to_its_end_while_it_runs(blender, project):
    """The output is captured in a bounded buffer while the script prints, and the result keeps its last 20,000.

    The buffer holds at most 40,000 characters at any time, rather than all the output until the end.
    """
    script = project / "chatty.py"
    script.write_text("import sys\nfor i in range(4000):\n    print('x' * 99)\nprint('the end')\n"
                      "result = len(sys.stdout.getvalue())\n", encoding="utf-8")
    out = blender.ok("run_script", path=str(script))
    assert out["result"] <= 40_000, "the capture did not keep a bounded tail"
    assert out["stdout"].endswith("the end\n") and len(out["stdout"]) == 20_000


EDITS = {
    "material_colour": "bpy.data.materials['Paint'].diffuse_color = (0, 1, 0, 1)",
    "material_node": "bpy.data.materials['Paint'].node_tree.nodes['Principled BSDF'].inputs['Roughness']"
                     ".default_value = 0.123",
    "uv_map": "bpy.data.objects['Crate'].data.uv_layers.new(name='Second')",
    "custom_property": "bpy.data.objects['Crate']['tag'] = 3",
    "smoothing": "bpy.data.objects['Crate'].data.polygons[0].use_smooth = True",
    "face_material": "bpy.data.objects['Crate'].data.polygons[0].material_index = 1",
}


@pytest.mark.parametrize("edit", list(EDITS))
def test_an_edit_to_an_objects_material_uvs_properties_or_faces_names_it_changed(blender, project, edit):
    blender.ok("create_object", kind="cube", name="Crate")
    blender.ok("set_material", material="Paint", object="Crate", base_color=[0.8, 0.2, 0.1])
    blender.ok("run_python", code="obj = bpy.data.objects['Crate']\n"
                                  "obj.data.materials.append(bpy.data.materials.new('Two'))")
    script = project / "edit.py"
    script.write_text(EDITS[edit] + "\n", encoding="utf-8")
    out = blender.ok("run_script", path=str(script))
    assert out["changed"] == ["Crate"], (edit, out)


def test_the_result_names_what_the_script_changed_and_removed(blender, project):
    blender.ok("create_object", kind="cube", name="Keep")
    blender.ok("create_object", kind="cube", name="Move")
    blender.ok("create_object", kind="cube", name="Gone")
    script = project / "edit.py"
    script.write_text("bpy.data.objects['Move'].location.x = 3\n"
                      "bpy.data.objects.remove(bpy.data.objects['Gone'])\n", encoding="utf-8")
    out = blender.ok("run_script", path=str(script))
    assert (out["added"], out["removed"], out["changed"]) == ([], ["Gone"], ["Move"])
    assert out["counts"] == {"added": 0, "removed": 1, "changed": 1, "objects": 2}
    assert blender.ok("get_object", object="Move")["location"][0] == pytest.approx(3.0)


def test_fresh_refuses_to_drop_unsaved_work_and_runs_nothing(blender, project):
    blender.ok("create_object", kind="cube", name="Work")
    blender.refused("run_script", "unsaved_changes", path=str(project / "build.py"), fresh=True,
                    args={"name": "Crate", "size": 1})
    assert [o["name"] for o in blender.ok("get_scene")["objects"]] == ["Work"]
    out = blender.ok("run_script", path=str(project / "build.py"), fresh=True, discard=True,
                     args={"name": "Crate", "size": 1})
    assert out["fresh"] is True and out["removed"] == []
    assert [o["name"] for o in blender.ok("get_scene")["objects"]] == ["Crate"]


def test_a_failing_script_is_a_python_error_from_its_own_line_and_counts_as_a_change(blender, project):
    script = project / "broken.py"
    script.write_text("import helpers\nhelpers.cube('Half', 1)\nprint('so far')\nraise ValueError('no lid')\n",
                      encoding="utf-8")
    payload = blender.refused("run_script", "python_error", path=str(script))
    assert payload["error"] == "ValueError: no lid"
    assert "broken.py" in payload["traceback"] and "line 4" in payload["traceback"]
    assert "scripts.py" not in payload["traceback"], "the traceback shows the add-on's own frames"
    assert "so far" in payload["stdout"]
    assert blender.ok("get_scene")["unsaved_changes"] is True, \
        "the file is not marked changed, though the script ran and may have changed it"
    left = blender.ok("run_python", code=f"import sys\nresult = [p for p in sys.path if {str(project)!r} in p]")
    assert left["result"] == []


def test_a_script_cannot_exit_blender(blender, project):
    script = project / "leave.py"
    script.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    payload = blender.refused("run_script", "python_error", path=str(script))
    assert "SystemExit" in payload["error"] and "run_script cannot exit Blender" in payload["error"]
    assert blender.ok("get_scene") is not None


def test_a_script_sees_its_args_and_an_empty_object_without_them(blender, project):
    script = project / "args.py"
    script.write_text("result = args\n", encoding="utf-8")
    nested = {"n": 1, "tags": ["a"], "size": {"x": 0.5}}
    assert blender.ok("run_script", path=str(script), args=nested)["result"] == nested
    assert blender.ok("run_script", path=str(script))["result"] == {}


@pytest.mark.parametrize(("name", "kind"), [("missing.py", "not_found"), ("notes.txt", "bad_argument")])
def test_a_script_that_cannot_run_changes_nothing(blender, project, tmp_path, name, kind):
    (project / "notes.txt").write_text("not python", encoding="utf-8")
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("save_file", path=str(tmp_path / "scene.blend"))
    refused_unchanged(blender, "run_script", kind, path=str(project / name))


def test_args_that_are_not_an_object_are_refused_before_anything_runs(blender, project, tmp_path):
    blender.ok("create_object", kind="cube", name="Box")
    blender.ok("save_file", path=str(tmp_path / "scene.blend"))
    refused_unchanged(blender, "run_script", "bad_argument", path=str(project / "build.py"), args=[1, 2])
