"""Tests for cleaning up after sessions, for the recovery copy, and for the oldest Blender version supported.

Every way a background session ends removes its working folder. Old previews in the default render folder and old
recovery copies are removed when a server starts. When the client goes away while a background Blender holds
unsaved work, that work is first copied to the recovery folder. A Blender older than 4.2 is refused at start.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import free_port
from tests.test_sessions import background_env, wait_gone

DAY = 24 * 3600


def fake_blender(folder: Path, version: str = "5.0.1", code: int = 3) -> str:
    """Write a stand-in Blender executable into ``folder``, and return its path.

    The executable prints a version line for ``--version`` as Blender would, and otherwise exits at once with
    ``code``.
    """
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        path = folder / "blender.cmd"
        path.write_text(f'@echo off\r\nif "%~1"=="--version" (\r\n  echo Blender {version}\r\n  exit /b 0\r\n)\r\n'
                        f"exit /b {code}\r\n")
    else:
        path = folder / "blender"
        path.write_text(f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "Blender {version}"; exit 0; fi\n'
                        f"exit {code}\n")
        path.chmod(0o755)
    return str(path)


def own_temp(tmp_path: Path) -> tuple[Path, dict]:
    """Return a temp folder for one server alone, and the environment variables that point the server at it.

    A check of the folder then sees nothing that another server made.
    """
    temp = tmp_path / "temp"
    temp.mkdir(exist_ok=True)
    return temp, {"TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp)}


def working_folders(temp: Path) -> list[str]:
    """Return the names of the session working folders in ``temp``."""
    return sorted(path.name for path in temp.glob("blenderlens-*"))


def aged(path: Path, days: float) -> Path:
    """Set the modification time of ``path`` to ``days`` ago, and return the path."""
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))
    return path


# ---------------------------------------------------------------------------
# Session working folders
# ---------------------------------------------------------------------------


def test_a_start_that_fails_leaves_no_working_folder(mcp_process, tmp_path):
    temp, temp_env = own_temp(tmp_path)
    proc = mcp_process(env={**temp_env, "BLENDER_BIN": fake_blender(tmp_path / "bin"), "BLENDERLENS_MODE": "background",
                            "BLENDERLENS_PORT": str(free_port()), "BLENDERLENS_RENDER_DIR": str(tmp_path / "r")})
    proc.initialize()
    payload = proc.refused("get_scene", "start_failed")
    assert "exited with code 3" in payload["error"]
    assert working_folders(temp) == []


@pytest.mark.integration
def test_a_start_that_times_out_leaves_no_working_folder(mcp_process, blender_binary, tmp_path):
    temp, temp_env = own_temp(tmp_path)
    proc = mcp_process(env={**background_env(blender_binary, tmp_path), **temp_env,
                            "BLENDERLENS_START_TIMEOUT": "0.05"})
    proc.initialize()
    payload = proc.refused("get_scene", "start_failed")
    assert "did not become ready" in payload["error"]
    assert working_folders(temp) == []


@pytest.mark.integration
def test_the_working_folder_of_a_blender_that_crashed_goes_with_the_session(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    session = proc.ok("open_session", mode="background")["session"]
    pid, working_folder = session["pid"], Path(session["log"]).parent
    assert working_folder.is_dir()
    subprocess.run(["taskkill", "/F", "/PID", str(pid)] if sys.platform == "win32" else ["kill", "-9", str(pid)],
                   capture_output=True, check=False)
    assert wait_gone(pid)
    payload = proc.refused("get_scene", "connection_lost")
    assert payload.get("log") is not None, "the end of the log was not read before the working folder was removed"
    assert not working_folder.exists()


@pytest.mark.integration
def test_get_status_drops_an_exited_blender_and_removes_its_working_folder(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    session = proc.ok("open_session", mode="background")["session"]
    pid, working_folder = session["pid"], Path(session["log"]).parent
    subprocess.run(["taskkill", "/F", "/PID", str(pid)] if sys.platform == "win32" else ["kill", "-9", str(pid)],
                   capture_output=True, check=False)
    assert wait_gone(pid)
    assert proc.ok("get_status")["session"] is None
    assert not working_folder.exists()


@pytest.mark.integration
def test_closing_and_exiting_remove_the_working_folder(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    first = Path(proc.ok("open_session", mode="background")["session"]["log"]).parent
    proc.ok("close_session")
    assert not first.exists()
    second = Path(proc.ok("open_session", mode="background")["session"]["log"]).parent
    proc.close()
    assert not second.exists()


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------


def test_old_previews_in_the_default_render_folder_go_when_a_server_starts(mcp_process, tmp_path):
    temp, temp_env = own_temp(tmp_path)
    renders = temp / "blenderlens" / "renders"
    renders.mkdir(parents=True)
    old, recent, not_a_preview = renders / "old.png", renders / "recent.png", renders / "old.txt"
    for path in (old, recent, not_a_preview):
        path.write_bytes(b"x")
    aged(old, 8)
    aged(recent, 6)
    aged(not_a_preview, 30)

    # An empty BLENDERLENS_RENDER_DIR counts as unset, so the server uses its default folder.
    proc = mcp_process(env={**temp_env, "BLENDERLENS_RENDER_DIR": "", "BLENDERLENS_PORT": str(free_port())})
    proc.initialize()
    assert Path(proc.ok("get_status")["render_dir"]) == renders
    assert not old.exists()
    assert recent.exists()
    assert not_a_preview.exists()


def test_a_render_folder_the_user_chose_is_never_pruned(mcp_process, tmp_path):
    chosen = tmp_path / "my renders"
    chosen.mkdir()
    old = chosen / "old.png"
    old.write_bytes(b"x")
    aged(old, 400)
    proc = mcp_process(env={"BLENDERLENS_RENDER_DIR": str(chosen), "BLENDERLENS_PORT": str(free_port())})
    proc.initialize()
    proc.ok("get_status")
    assert old.exists()


# ---------------------------------------------------------------------------
# The recovery copy
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_unsaved_background_work_is_copied_to_the_recovery_folder_when_stdin_closes(mcp_process, blender_binary,
                                                                                     tmp_path):
    runtime = tmp_path / "runtime"
    env = {**background_env(blender_binary, tmp_path), "BLENDERLENS_RUNTIME_DIR": str(runtime)}
    proc = mcp_process(env=env)
    proc.initialize()
    proc.ok("create_object", kind="cube", name="Precious", location=[1, 2, 3])
    pid = proc.ok("get_status")["session"]["pid"]
    stderr = proc.close()

    copies = sorted((runtime / "recovery").glob("*.blend"))
    assert len(copies) == 1, stderr
    assert re.fullmatch(rf"\d{{8}}-\d{{6}}-{pid}\.blend", copies[0].name)
    assert str(copies[0]) in stderr, "the copy's path is not in the log"

    # The copy opens and holds the object, and get_status lists it.
    again = mcp_process(env=env)
    again.initialize()
    assert again.ok("get_status")["recovery"][0]["path"] == str(copies[0])
    again.ok("open_session", mode="background", file=str(copies[0]))
    assert again.ok("get_object", object="Precious")["location"] == [1.0, 2.0, 3.0]


@pytest.mark.integration
def test_saved_work_leaves_no_recovery_copy(mcp_process, blender_binary, tmp_path):
    runtime = tmp_path / "runtime"
    proc = mcp_process(env={**background_env(blender_binary, tmp_path), "BLENDERLENS_RUNTIME_DIR": str(runtime)})
    proc.initialize()
    proc.ok("create_object", kind="cube")
    proc.ok("save_file", path=str(tmp_path / "kept.blend"))
    proc.close()
    assert list((runtime / "recovery").glob("*.blend")) == []


def test_old_recovery_copies_go_at_start_and_get_status_lists_the_five_newest(mcp_process, no_blender_env, tmp_path):
    folder = tmp_path / "runtime" / "recovery"
    folder.mkdir(parents=True)
    names = [f"2026100{i}-120000-{100 + i}.blend" for i in range(7)]
    for age, name in enumerate(names):
        (folder / name).write_bytes(b"BLENDER")
        aged(folder / name, age / 24)
    ancient = folder / "20250101-000000-1.blend"
    ancient.write_bytes(b"BLENDER")
    aged(ancient, 15)

    proc = mcp_process(env={**no_blender_env, "BLENDERLENS_RUNTIME_DIR": str(tmp_path / "runtime")})
    proc.initialize()
    assert not ancient.exists()
    listed = proc.ok("get_status")["recovery"]
    assert [Path(entry["path"]).name for entry in listed] == names[:5]
    assert (folder / names[6]).exists(), "a copy younger than 14 days was removed"


# ---------------------------------------------------------------------------
# The oldest supported Blender
# ---------------------------------------------------------------------------


def test_a_blender_older_than_4_2_is_refused_at_start(mcp_process, no_blender_env, tmp_path):
    temp, temp_env = own_temp(tmp_path)
    binary = fake_blender(tmp_path / "bin", version="4.1.0")
    proc = mcp_process(env={**no_blender_env, **temp_env, "BLENDERLENS_MODE": "background", "BLENDER_BIN": binary})
    proc.initialize()
    payload = proc.refused("get_scene", "start_failed")
    said = payload["error"] + " " + payload["hint"]
    assert "4.1.0" in said
    assert "4.2" in said
    assert working_folders(temp) == []


def test_blender_4_2_passes_the_floor(mcp_process, no_blender_env, tmp_path):
    binary = fake_blender(tmp_path / "bin", version="4.2.0")
    proc = mcp_process(env={**no_blender_env, "BLENDERLENS_MODE": "background", "BLENDER_BIN": binary})
    proc.initialize()
    payload = proc.refused("get_scene", "start_failed")
    assert "exited with code 3" in payload["error"], "Blender 4.2.0 was refused for its version"


def test_install_addon_leaves_no_folder_in_temp(tmp_path, monkeypatch):
    """``install_addon`` builds the add-on zip in a temporary folder, and removes it once Blender has installed it."""
    from blenderlens_mcp import cli

    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(cli.tempfile, "tempdir", str(temp))
    assert cli.install_addon(fake_blender(tmp_path / "bin", code=0)) == 0
    assert list(temp.iterdir()) == []
