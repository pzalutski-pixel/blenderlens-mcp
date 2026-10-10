"""Tests for the server's policy settings: whether Python may run, and which folders tools may use.

- ``BLENDERLENS_ALLOW_PYTHON=0`` leaves ``run_python`` and ``run_script`` out of ``tools/list``, and a call to
  either is refused with ``not_allowed`` (exit code 13).
- ``BLENDERLENS_ALLOWED_DIRS`` limits every path a tool reads or writes to those folders, compared after
  ``realpath``. The render folder, ``render_dir``, is allowed only for the ``path`` of ``render_preview``. When
  the variable is unset, paths are not limited. The library links inside a .blend are not paths a tool is given:
  ``open_file`` and ``import_file`` list them, and warn of any outside the allowed folders.

The refusals happen in the server before anything reaches Blender, so most of these tests need no Blender.
Three need one: they check that allowed paths still work, that saving without a path is checked against where the
file is, and that the library links of an opened .blend are reported.
"""

import os
import sys

import pytest

from blenderlens_mcp.tools import PYTHON, TOOLS
from tests.conftest import free_port

OFF = {"BLENDERLENS_ALLOW_PYTHON": "0"}


def test_run_python_is_listed_by_default(mcp_process, no_blender_env):
    proc = mcp_process(env=no_blender_env)
    proc.initialize()
    names = {t["name"] for t in proc.request("tools/list")["result"]["tools"]}
    assert "run_python" in names and len(names) == len(TOOLS)


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_with_python_turned_off_run_python_is_not_listed(mcp_process, no_blender_env, value):
    proc = mcp_process(env={**no_blender_env, "BLENDERLENS_ALLOW_PYTHON": value})
    proc.initialize()
    names = {t["name"] for t in proc.request("tools/list")["result"]["tools"]}
    assert "run_python" not in names and "run_script" not in names
    assert len(names) == len(TOOLS) - len(PYTHON)


def test_with_python_turned_off_a_call_is_not_allowed(mcp_process, no_blender_env):
    proc = mcp_process(env={**no_blender_env, **OFF, "BLENDERLENS_MODE": "live"})
    proc.initialize()
    payload = proc.refused("run_python", "not_allowed", code="result = 1")
    assert payload["exit"] == 13
    assert "BLENDERLENS_ALLOW_PYTHON" in payload["error"] + payload["hint"]
    assert proc.request("ping")["result"] == {}


def test_with_python_turned_off_a_script_is_not_allowed(mcp_process, no_blender_env, tmp_path):
    """With Python turned off, run_script is refused too, before anything reaches Blender, since it runs Python."""
    script = tmp_path / "build.py"
    script.write_text("result = 1\n", encoding="utf-8")
    proc = mcp_process(env={**no_blender_env, **OFF, "BLENDERLENS_MODE": "live"})
    proc.initialize()
    payload = proc.refused("run_script", "not_allowed", path=str(script))
    assert payload["exit"] == 13
    assert "run_script" in payload["error"] and "BLENDERLENS_ALLOW_PYTHON" in payload["error"]


# ---------------------------------------------------------------------------
# The folders that tools may use
# ---------------------------------------------------------------------------


def allowed_dirs_env(base_env, *folders):
    """Return ``base_env`` with BLENDERLENS_ALLOWED_DIRS set to ``folders``."""
    return {**base_env, "BLENDERLENS_ALLOWED_DIRS": os.pathsep.join(str(f) for f in folders)}


OUTSIDE_CALLS = [
    ("export_glb", "path", "out.glb", {}),
    ("import_file", "path", "in.glb", {}),
    ("save_file", "path", "work.blend", {}),
    ("open_file", "path", "work.blend", {}),
    ("render_preview", "path", "shot.png", {}),
    ("open_session", "file", "work.blend", {"mode": "background"}),
    # A tool may take several paths, and each one is checked.
    ("set_material", "base_color_image", "wood.png", {"material": "Wood"}),
    ("set_material", "normal_image", "bumps.png", {"material": "Wood", "base_color_image": ""}),
    ("set_material", "emission_image", "glow.png", {"material": "Wood"}),
    ("run_script", "path", "build.py", {}),
    ("check_asset", "rules_file", "rules.json", {}),
]


@pytest.mark.parametrize("tool, key, name, extra", OUTSIDE_CALLS, ids=[c[0] for c in OUTSIDE_CALLS])
def test_a_path_outside_the_allowed_folders_is_not_allowed(mcp_process, no_blender_env, tmp_path, tool, key, name,
                                                           extra):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / name).write_bytes(b"x")  # The file exists, so only the allowed folders can refuse it.
    proc = mcp_process(env={**allowed_dirs_env(no_blender_env, allowed), "BLENDERLENS_MODE": "live"})
    proc.initialize()
    payload = proc.refused(tool, "not_allowed", **{key: str(outside / name), **extra})
    assert payload["exit"] == 13
    assert str(outside / name) in payload["error"]
    assert os.path.realpath(allowed) in payload["hint"]


@pytest.mark.parametrize("sneaky", ["{allowed}/../outside/out.glb", "{allowed}2/out.glb", "outside/out.glb"],
                         ids=["dot-dot", "sibling-prefix", "relative"])
def test_a_path_that_only_looks_inside_is_not_allowed(mcp_process, no_blender_env, tmp_path, sneaky):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (tmp_path / "outside").mkdir()
    proc = mcp_process(cwd=tmp_path, env={**allowed_dirs_env(no_blender_env, allowed), "BLENDERLENS_MODE": "live"})
    proc.initialize()
    proc.refused("export_glb", "not_allowed", path=sneaky.format(allowed=allowed))


def _link(link, target):
    """Make ``link`` a directory symlink to ``target``, or a junction on Windows when symlinks are not permitted.

    ``realpath`` follows both kinds of link, so either one serves the test.
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        if sys.platform != "win32":
            raise
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def test_a_link_inside_an_allowed_folder_that_leads_out_is_not_allowed(mcp_process, no_blender_env, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _link(allowed / "door", outside)
    proc = mcp_process(env={**allowed_dirs_env(no_blender_env, allowed), "BLENDERLENS_MODE": "live"})
    proc.initialize()
    proc.refused("export_glb", "not_allowed", path=str(allowed / "door" / "out.glb"))


RENDER_DIR_CALLS = [
    ("export_glb", "path", "out.glb", {}),
    ("import_file", "path", "in.glb", {}),
    ("save_file", "path", "work.blend", {}),
    ("open_file", "path", "work.blend", {}),
    ("open_session", "file", "work.blend", {"mode": "live"}),
]


@pytest.mark.parametrize("tool, key, name, extra", RENDER_DIR_CALLS, ids=[c[0] for c in RENDER_DIR_CALLS])
def test_render_dir_is_allowed_only_for_a_preview(mcp_process, no_blender_env, tmp_path, tool, key, name, extra):
    """The render folder is allowed for the ``path`` of render_preview only, never for another tool's path."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    renders = tmp_path / "renders"  # The BLENDERLENS_RENDER_DIR that no_blender_env sets.
    renders.mkdir()
    proc = mcp_process(env={**allowed_dirs_env(no_blender_env, allowed), "BLENDERLENS_MODE": "live"})
    proc.initialize()
    payload = proc.refused(tool, "not_allowed", **{key: str(renders / name), **extra})
    assert str(renders / name) in payload["error"]
    assert "the render folder (" not in payload["hint"], "the hint offers the render folder to another tool"
    # A preview's own path in the render folder passes the check, and with no add-on running, the call then fails
    # with not_connected.
    proc.refused("render_preview", "not_connected", path=str(renders / "shot.png"))


def test_a_preview_outside_is_pointed_at_the_render_folder(mcp_process, no_blender_env, tmp_path):
    """A preview path outside the allowed folders is refused with a hint naming the render folder and its variable."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    proc = mcp_process(env={**allowed_dirs_env(no_blender_env, allowed), "BLENDERLENS_MODE": "live"})
    proc.initialize()
    payload = proc.refused("render_preview", "not_allowed", path=str(tmp_path / "loose.png"))
    renders = os.path.realpath(no_blender_env["BLENDERLENS_RENDER_DIR"])
    assert f"the render folder ({renders}, set by BLENDERLENS_RENDER_DIR)" in payload["hint"]


#: Code that links the object Shared from the .blend ``library`` into the scene, and returns each library's path with
#: whether the object came from a library.
LINK = """
with bpy.data.libraries.load({library!r}, link=True, relative=True) as (source, target):
    target.objects = ["Shared"]
for obj in target.objects:
    bpy.context.scene.collection.objects.link(obj)
result = [(lib.filepath, bool(obj.library)) for lib in bpy.data.libraries for obj in target.objects]
"""


def _same(paths):
    """Return ``paths`` normalised, so that two spellings of one path compare equal."""
    return [os.path.normcase(os.path.normpath(str(p))) for p in paths]


@pytest.mark.integration
def test_a_blend_linking_a_library_outside_the_allowed_folders_is_reported(mcp_process, blender_binary, tmp_path):
    """open_file and import_file list the libraries the open .blend links, and warn of any outside the allowed folders.

    BLENDERLENS_ALLOWED_DIRS limits only the paths that tools are given, while Blender reads a .blend's library
    links from wherever they point, so the tools report those links rather than refuse them.
    """
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    library = outside / "library.blend"
    linker = allowed / "linker.blend"
    base = {"BLENDER_BIN": blender_binary, "BLENDERLENS_MODE": "background", "BLENDERLENS_PORT": str(free_port()),
            "BLENDERLENS_RENDER_DIR": str(tmp_path / "renders")}
    free = mcp_process(env=base)
    free.initialize()
    free.ok("create_object", kind="cube", name="Shared")
    free.ok("save_file", path=str(library))
    free.ok("create_file", discard=True)
    free.ok("save_file", path=str(linker))  # Saved first, so the link below is stored as the relative //../outside.
    linked = free.ok("run_python", code=LINK.format(library=str(library)))["result"]
    assert linked and linked[0][0].startswith("//") and linked[0][1] is True, linked
    free.ok("save_file")
    free.ok("export_glb", path=str(allowed / "plain.glb"))
    unlimited = free.ok("open_file", path=str(linker))
    assert _same(unlimited["libraries"]) == _same([library]), "the library is not listed by its absolute path"
    assert not unlimited.get("warnings"), "a warning was given with no allowed folders set"
    free.close()

    proc = mcp_process(env={**base, "BLENDERLENS_ALLOWED_DIRS": str(allowed), "BLENDERLENS_PORT": str(free_port())})
    proc.initialize()
    opened = proc.ok("open_file", path=str(linker))
    assert _same(opened["libraries"]) == _same([library])
    warnings = [w for w in opened.get("warnings", []) if w["code"] == "libraries_outside_allowed_dirs"]
    assert len(warnings) == 1, opened.get("warnings")
    assert _same(warnings[0]["libraries"]) == _same([library])
    assert str(library) in warnings[0]["message"]

    imported = proc.ok("import_file", path=str(allowed / "plain.glb"))
    assert _same(imported["libraries"]) == _same([library]), "the library the open file still links is not listed"
    assert [w["code"] for w in imported["warnings"]] == ["libraries_outside_allowed_dirs"]

    proc.ok("create_file", discard=True)
    clean = proc.ok("import_file", path=str(allowed / "plain.glb"))
    assert clean["libraries"] == [] and not clean.get("warnings")


def test_an_unset_or_empty_allowed_dirs_limits_nothing(mcp_process, no_blender_env, tmp_path):
    for env in (no_blender_env, {**no_blender_env, "BLENDERLENS_ALLOWED_DIRS": ""}):
        proc = mcp_process(env={**env, "BLENDERLENS_MODE": "live"})
        proc.initialize()
        # The server does not refuse the path. It goes on to look for the add-on, which is not running.
        proc.refused("export_glb", "not_connected", path=str(tmp_path / "anywhere.glb"))


@pytest.mark.integration
def test_paths_inside_the_allowed_folders_and_render_dir_work(mcp_process, blender_binary, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    renders = tmp_path / "renders"
    proc = mcp_process(cwd=tmp_path, env={
        "BLENDER_BIN": blender_binary, "BLENDERLENS_MODE": "background", "BLENDERLENS_PORT": str(free_port()),
        "BLENDERLENS_RENDER_DIR": str(renders), "BLENDERLENS_ALLOWED_DIRS": os.pathsep.join([str(allowed),
                                                                                              str(other)])})
    proc.initialize()
    proc.ok("create_object", kind="cube", name="Box")
    exported = proc.ok("export_glb", path=str(allowed / "box.glb"))
    assert os.path.isfile(exported["path"])
    relative = proc.ok("export_glb", path="allowed/relative.glb")["path"]
    assert os.path.realpath(relative) == os.path.realpath(allowed / "relative.glb")
    proc.ok("export_glb", path=str(other / "second.glb"))
    refused = proc.refused("export_glb", "not_allowed", path=str(tmp_path / "loose.glb"))
    assert os.path.realpath(allowed) in refused["hint"]
    assert not (tmp_path / "loose.glb").exists(), "nothing was written"

    # With no path, the preview is written to render_dir, which is always allowed.
    shot = proc.ok("render_preview", engine="cycles", samples=1, resolution=32)
    assert os.path.realpath(os.path.dirname(shot["path"])) == os.path.realpath(renders)
    proc.ok("render_preview", engine="cycles", samples=1, resolution=32, path=str(renders / "named.png"))

    proc.ok("save_file", path=str(allowed / "work.blend"))
    proc.ok("save_file")  # Saves where the file was last saved, which is inside an allowed folder.
    proc.ok("open_file", path=str(allowed / "work.blend"))
    proc.ok("import_file", path=str(allowed / "box.glb"))


@pytest.mark.integration
def test_saving_without_a_path_is_checked_against_where_the_file_is(mcp_process, blender_binary, tmp_path):
    """save_file with no path is refused when the file was last saved outside the allowed folders.

    With no path, save_file writes where the file was last saved, so that location is checked like a given path.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    base = {"BLENDER_BIN": blender_binary, "BLENDERLENS_MODE": "background", "BLENDERLENS_PORT": str(free_port()),
            "BLENDERLENS_RENDER_DIR": str(tmp_path / "renders")}
    # Save a .blend outside the allowed folder, from a server with no allowed folders set.
    free = mcp_process(env=base)
    free.initialize()
    free.ok("create_object", kind="cube")
    free.ok("save_file", path=str(outside / "kept.blend"))
    free.close()
    stamp = (outside / "kept.blend").stat().st_mtime_ns

    proc = mcp_process(env={**base, "BLENDERLENS_ALLOWED_DIRS": str(allowed), "BLENDERLENS_PORT": str(free_port())})
    proc.initialize()
    # Open the file with run_python, whose code the allowed folders do not cover. save_file must still not write
    # there.
    proc.ok("run_python", code=f"bpy.ops.wm.open_mainfile(filepath={str(outside / 'kept.blend')!r})")
    payload = proc.refused("save_file", "not_allowed")
    assert str(outside / "kept.blend") in payload["error"]
    assert (outside / "kept.blend").stat().st_mtime_ns == stamp


def test_an_empty_entry_does_not_allow_the_current_working_directory(mcp_process, no_blender_env, tmp_path):
    """An empty entry in BLENDERLENS_ALLOWED_DIRS does not allow the server's current working directory.

    A trailing separator makes such an entry, and an empty path resolves to the current working directory.
    """
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    env = {**no_blender_env, "BLENDERLENS_MODE": "live",
           "BLENDERLENS_ALLOWED_DIRS": str(allowed) + os.pathsep + os.pathsep}
    proc = mcp_process(cwd=tmp_path, env=env)
    proc.initialize()
    proc.refused("export_glb", "not_allowed", path=str(tmp_path / "out.glb"))
