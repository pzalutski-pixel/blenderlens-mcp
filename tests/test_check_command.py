"""Tests for the ``blenderlens-mcp check`` command.

The command line is ``blenderlens-mcp check <file.glb|.blend> [--rules] [--budget] [--target] [--json]``, and no
language model is involved. A .glb is checked from the file alone, without Blender. A .blend is opened in a
background Blender and checked with ``check_asset``. The command exits with 0 when the asset passes, 1 when it
fails, and 2 when it could not be checked: a bad option or file, no Blender, or a check that could not run.
"""

import json
import subprocess
import sys

import pytest

from blenderlens_mcp import cli
from tests.conftest import checkout_env
from tests.test_glb import TRIANGLE, build_glb


def glb(tmp_path, changes=None, name="asset.glb") -> str:
    """Write the test triangle as a .glb, with top-level ``changes`` applied, and return its path."""
    document = json.loads(json.dumps(TRIANGLE))
    for key, value in (changes or {}).items():
        document[key] = value
    path = tmp_path / name
    path.write_bytes(build_glb(document, b"\x00" * 42))
    return str(path)


def rules_file(tmp_path, rules, name="rules.json") -> str:
    """Write ``rules`` to a file, as JSON or, when given a str, as it is, and return its path."""
    path = tmp_path / name
    path.write_text(json.dumps(rules) if not isinstance(rules, str) else rules, encoding="utf-8")
    return str(path)


def run(capsys, *argv) -> tuple[int, str, str]:
    """Run ``blenderlens-mcp check`` with ``argv`` in this process, and return the exit code, stdout and stderr."""
    try:
        code = cli.main(["check", *argv])
    except SystemExit as exc:  # argparse raises SystemExit for its own usage errors.
        code = exc.code
    out = capsys.readouterr()
    return code, out.out, out.err


# ---------------------------------------------------------------------------
# A .glb, with no Blender
# ---------------------------------------------------------------------------


def test_a_passing_glb_exits_0(tmp_path, capsys):
    code, out, _ = run(capsys, glb(tmp_path))
    assert code == 0, out
    assert "PASS" in out


def test_a_glb_failing_its_rules_exits_1_and_says_why(tmp_path, capsys):
    rules = rules_file(tmp_path, {"required": ["Lamp", "Lamp/Shade", "Cord"], "single_root": True})
    code, out, _ = run(capsys, glb(tmp_path), "--rules", rules)
    assert code == 1
    assert "FAIL" in out and "missing_required" in out and "Cord" in out


def test_the_rules_read_the_glb_in_blender_axes(tmp_path, capsys):
    """The rules measure a .glb in Blender's axes, where glTF's upward Y axis is Z.

    The triangle spans 1 m along glTF's X and Y axes, so in Blender's axes it spans 1 m in X and Z.
    """
    fits = rules_file(tmp_path, {"size": [{"objects": "Lamp", "min": [0.99, 0, 0.99], "max": [1.01, 0.01, 1.01]}],
                                 "extras": [{"objects": "Lamp", "keys": {"bulbs": "integer"}}],
                                 "palette": {"Glow": {"base_color": [0.9, 0.85, 0.7], "metallic": 0, "roughness": 0.8,
                                                      "emission": [1, 0.9, 0.6], "strength": 3}}})
    code, out, _ = run(capsys, glb(tmp_path), "--rules", fits)
    assert code == 0, out


def test_a_triangle_budget(tmp_path, capsys):
    assert run(capsys, glb(tmp_path), "--budget", "1")[0] == 0
    code, out, _ = run(capsys, glb(tmp_path), "--budget", "0")
    assert code == 1 and "over_budget" in out


def test_a_glb_its_target_cannot_load_fails(tmp_path, capsys):
    path = glb(tmp_path, {"extensionsUsed": ["KHR_draco_mesh_compression"],
                          "extensionsRequired": ["KHR_draco_mesh_compression"]})
    code, out, _ = run(capsys, path, "--target", "godot")
    assert code == 1 and "extension_not_supported" in out
    assert run(capsys, path, "--target", "threejs")[0] == 0


def test_json_output_carries_the_whole_report(tmp_path, capsys):
    code, out, _ = run(capsys, glb(tmp_path), "--json", "--budget", "0")
    report = json.loads(out)
    assert code == 1 and report["passed"] is False
    assert report["file"].endswith("asset.glb") and report["kind"] == "glb"
    assert [i["code"] for i in report["issues"]] == ["over_budget"]
    assert report["contents"]["triangles"] == 1


@pytest.mark.parametrize("problem", ["missing file", "wrong extension", "bad json", "bad rules", "bad budget",
                                     "bad target", "no rules file"])
def test_an_unusable_file_or_option_exits_2(tmp_path, capsys, problem):
    path = glb(tmp_path)
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    argv, words = {
        "missing file": ([str(tmp_path / "ghost.glb")], "No file"),
        "wrong extension": ([str(tmp_path / "notes.txt")], ".txt"),
        "bad json": ([path, "--rules", rules_file(tmp_path, "{nope", "broken.json")], "not JSON"),
        "bad rules": ([path, "--rules", rules_file(tmp_path, {"requird": ["A"]}, "misspelt.json")], "requird"),
        "bad budget": ([path, "--budget", "lots"], "--budget"),
        "bad target": ([path, "--target", "unity"], "unity"),
        "no rules file": ([path, "--rules", str(tmp_path / "ghost.json")], "ghost.json"),
    }[problem]
    code, _, err = run(capsys, *argv)
    assert code == 2, (problem, err)
    assert words in err, f"the error does not say what was wrong: {err}"


def test_a_file_that_is_not_a_glb_fails(tmp_path, capsys):
    broken = tmp_path / "broken.glb"
    broken.write_bytes(b"not a glb at all, just bytes")
    code, out, _ = run(capsys, str(broken))
    assert code == 1 and "glTF" in out


def test_nodes_sharing_a_name_are_measured_as_their_own_subtrees(tmp_path, capsys):
    """Nodes that share a name are each measured as their own subtree.

    Two parents 10 m apart each hold a node named ``Hand`` that holds a ``Finger``, and each ``Hand`` must
    measure 1 m wide, not 11 m.
    """
    nodes = [
        {"name": "Left", "children": [2], "translation": [-5, 0, 0]},
        {"name": "Right", "children": [3], "translation": [5, 0, 0]},
        {"name": "Hand", "children": [4]},
        {"name": "Hand", "children": [5]},
        {"name": "Finger", "mesh": 0},
        {"name": "Finger", "mesh": 0},
    ]
    path = glb(tmp_path, {"nodes": nodes, "scenes": [{"nodes": [0, 1]}]})
    rules = rules_file(tmp_path, {"size": [{"objects": "Hand", "max": [1.5, 1.5, 1.5]}]})
    code, out, _ = run(capsys, path, "--rules", rules)
    assert code == 0, out


def test_a_mesh_placed_many_times_is_measured_once_for_each_rotation_and_scale(tmp_path):
    """A mesh placed many times is measured once for each distinct rotation and scale, then moved to each place.

    The file holds 300 trees of 4,000 vertices each, 1.2 million points if each were moved one by one, and the
    read-back must take less than a second.
    """
    import struct
    import time

    from blenderlens_mcp.glb import describe
    from tests.test_glb import build_glb

    points = [(x / 63, 0.0, z / 62) for x in range(64) for z in range(63)][:4000]
    binary = b"".join(struct.pack("<3f", *p) for p in points)
    document = {
        "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": list(range(300))}],
        "nodes": [{"name": f"Tree{i}", "mesh": 0, "translation": [i * 2.0, 0, 0]} for i in range(299)]
        + [{"name": "Tree299", "mesh": 0, "translation": [0, 0, 5], "rotation": [0, 0, 0.7071068, 0.7071068]}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(points), "type": "VEC3",
                       "min": [0, 0, 0], "max": [1, 0, 1]}],
        "bufferViews": [{"buffer": 0, "byteLength": len(binary)}],
        "buffers": [{"byteLength": len(binary)}],
    }
    path = tmp_path / "forest.glb"
    path.write_bytes(build_glb(document, binary))
    started = time.perf_counter()
    objects = describe(str(path))["objects"]
    assert time.perf_counter() - started < 1.0
    assert objects[10]["bounds"] == [[20.0, -1.0, 0.0], [21.0, 0.0, 0.0]]
    turned = objects[299]["bounds"]  # Turned 90 degrees about glTF's Z, then moved 5 m along it, Blender's -Y.
    assert turned[0] == pytest.approx([-0.0, -6.0, 0.0], abs=1e-6) and turned[1] == pytest.approx([0.0, -5.0, 1.0],
                                                                                                   abs=1e-6)


def test_names_outside_the_console_code_page_are_printed(tmp_path):
    path = glb(tmp_path, {"nodes": [{"name": "Łódź 模型", "mesh": 0}], "scenes": [{"nodes": [0]}]}, name="ł.glb")
    rules = rules_file(tmp_path, {"retired": ["re:^Ł"]})
    env = checkout_env({"PYTHONIOENCODING": "cp1252"})
    env.pop("PYTHONUTF8", None)
    result = subprocess.run([sys.executable, "-m", "blenderlens_mcp", "check", path, "--rules", rules],
                            capture_output=True, env=env, timeout=60)
    assert b"Traceback" not in result.stderr, result.stderr.decode("utf-8", "replace")
    assert result.returncode == 1
    assert "retired_name" in result.stdout.decode("utf-8") and "Łódź 模型" in result.stdout.decode("utf-8")


def test_a_blend_takes_its_verdict_and_counts_from_check_asset(tmp_path, capsys, monkeypatch):
    """For a .blend, the command's verdict and counts are those of ``check_asset``, whichever objects it listed.

    Here ``check_asset`` lists 100 passing objects and leaves 105 out, so its one error is in an object not listed.
    """
    blend = tmp_path / "many.blend"
    blend.write_bytes(b"BLENDER")
    listed = [{"name": f"Part{i:03d}", "issues": []} for i in range(100)]
    check = {"passed": False, "counts": {"errors": 1, "warnings": 0}, "objects": listed, "issues": [],
             "objects_not_listed": 105}
    monkeypatch.setattr(cli, "_blend_report", lambda path, rules, budget, target: {
        "file": path, "kind": "blend", "target": target, "check": check, "issues": []})
    code, out, _ = run(capsys, str(blend))
    assert code == 1 and "FAIL (1 error," in out, out


def test_the_command_runs_as_a_program(tmp_path):
    result = subprocess.run([sys.executable, "-m", "blenderlens_mcp", "check", glb(tmp_path), "--budget", "0"],
                            capture_output=True, text=True, env=checkout_env(), timeout=60)
    assert result.returncode == 1, result.stderr
    assert "over_budget" in result.stdout


# ---------------------------------------------------------------------------
# A .blend, in a background Blender
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_blend_is_checked_in_a_background_blender(blender, blender_binary, tmp_path):
    blender.ok("create_object", kind="cube", name="Crate", size=1)
    blender.ok("run_python", code="bpy.data.objects['Crate'].data.transform(Matrix.Translation((0, 0, 0.5)))")
    good = tmp_path / "good.blend"
    blender.ok("save_file", path=str(good))
    blender.ok("edit_mesh", object="Crate", operation="flip_normals", faces=[3])
    bad = tmp_path / "bad.blend"
    blender.ok("save_file", path=str(bad))
    env = checkout_env({"BLENDER_BIN": blender_binary})
    rules = rules_file(tmp_path, {"required": ["Crate"], "origin_at_base": True})

    passed = subprocess.run([sys.executable, "-m", "blenderlens_mcp", "check", str(good), "--rules", rules,
                             "--target", "godot", "--json"], capture_output=True, text=True, env=env, timeout=240)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    report = json.loads(passed.stdout)
    assert report["kind"] == "blend" and report["passed"] is True
    assert report["check"]["objects"][0]["stats"]["triangles"] == 12
    assert report["contents"]["valid"] is True, "the export was not read back, though a target was given"

    failed = subprocess.run([sys.executable, "-m", "blenderlens_mcp", "check", str(bad)], capture_output=True,
                            text=True, env=env, timeout=240)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert "flipped_normals" in failed.stdout and "Crate" in failed.stdout


@pytest.mark.integration
def test_a_blend_failing_on_its_two_hundred_and_fifth_object_fails(blender, blender_binary, tmp_path):
    blender.ok("run_python", code="""
for i in range(205):
    mesh = bpy.data.meshes.new(f"C{i}")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=0.5, calc_uvs=True)
    bmesh.ops.translate(bm, verts=bm.verts, vec=(0, 0, 0.25))
    if i == 204:
        bmesh.ops.reverse_faces(bm, faces=list(bm.faces))
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(f"Part{i:03d}", mesh)
    obj.location = (i, 0, 0)
    bpy.context.scene.collection.objects.link(obj)
""")
    many = tmp_path / "many.blend"
    blender.ok("save_file", path=str(many))
    result = subprocess.run([sys.executable, "-m", "blenderlens_mcp", "check", str(many)], capture_output=True,
                            text=True, env=checkout_env({"BLENDER_BIN": blender_binary}), timeout=240)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL" in result.stdout and "Part204" in result.stdout
