"""Tests for how the server treats a busy Blender, which it never mistakes for one that has gone.

A Blender still working on a call that ran past its deadline stays attached. ``get_status`` reports
what it is busy with, the next call waits for it within its own timeout or is refused with ``busy``
without sending anything, and closing or reopening the session needs ``discard``. A Blender window
running the add-on that accepts the connection but responds slowly is busy too, and is never a reason
to start a background Blender.
"""

import time
from pathlib import Path

import pytest

from tests.conftest import free_port
from tests.fake_addon import FakeAddon, hello_then_silent, silent
from tests.test_sessions import background_env, process_alive, wait_gone

SLEEP = "import time\ntime.sleep({})\nresult = 'slept'"


@pytest.mark.integration
def test_a_deadline_leaves_the_session_attached_and_get_status_says_busy(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    proc.ok("create_object", kind="cube", name="Precious")
    pid = proc.ok("get_status")["session"]["pid"]

    proc.refused("run_python", "deadline", code=SLEEP.format(12), timeout=1)
    asked = time.monotonic()
    session = proc.ok("get_status")["session"]
    assert time.monotonic() - asked < 5, "get_status waited for the busy Blender"
    assert session is not None, "the deadline dropped the session"
    assert session["pid"] == pid
    assert session["busy"]["command"] == "run_python"
    assert session["busy"]["seconds"] >= 1
    assert process_alive(pid)

    # The next call waits for the sleep within its own timeout, then returns its result from the same Blender.
    assert [o["name"] for o in proc.ok("get_scene")["objects"]] == ["Precious"]
    session = proc.ok("get_status")["session"]
    assert session["pid"] == pid
    assert session["busy"] is None


@pytest.mark.integration
def test_a_call_behind_a_busy_blender_is_refused_busy_and_nothing_is_sent(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env={**background_env(blender_binary, tmp_path), "BLENDERLENS_TIMEOUT": "3"})
    proc.initialize()
    proc.ok("run_python", code="counter = 0\nresult = counter", timeout=60)
    proc.refused("run_python", "deadline", code=SLEEP.format(12), timeout=1)

    busy = proc.refused("get_scene", "busy")  # Waits its own 3 seconds for the sleep, then gives up.
    assert busy["exit"] == 12
    assert "run_python" in busy["error"]
    proc.refused("run_python", "busy", code="counter += 1", timeout=1)
    proc.refused("create_object", "busy", kind="cube", name="Piled")

    # Once the sleep is over, calls succeed again, and none of the refused ones ever ran.
    after = proc.ok("run_python", code="result = [counter, [o.name for o in bpy.data.objects]]", timeout=60)
    assert after["result"] == [0, []]
    assert proc.ok("get_scene")["objects"] == []


@pytest.mark.integration
def test_close_session_while_busy_is_refused_unless_discard(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    proc.ok("create_object", kind="cube", name="Precious")
    session = proc.ok("get_status")["session"]
    pid, working_folder = session["pid"], Path(session["log"]).parent
    proc.refused("run_python", "deadline", code=SLEEP.format(40), timeout=1)

    busy = proc.refused("close_session", "busy")
    assert "run_python" in busy["error"]
    assert "discard" in busy["hint"]
    proc.refused("open_session", "busy", mode="background")
    # The work survives, because the same Blender is still attached and running.
    assert process_alive(pid)
    assert proc.ok("get_status")["session"]["pid"] == pid

    assert proc.ok("close_session", discard=True)["closed"] == "background"
    assert wait_gone(pid, 10), "the busy background Blender is still running"
    assert not working_folder.exists(), "the session's working folder is still there"
    assert proc.ok("get_status")["session"] is None


@pytest.mark.integration
def test_open_session_with_discard_while_busy_starts_afresh(mcp_process, blender_binary, tmp_path):
    proc = mcp_process(env=background_env(blender_binary, tmp_path))
    proc.initialize()
    proc.ok("create_object", kind="cube", name="Precious")
    session = proc.ok("get_status")["session"]
    pid, working_folder = session["pid"], Path(session["log"]).parent
    proc.refused("run_python", "deadline", code=SLEEP.format(40), timeout=1)

    opened = proc.ok("open_session", mode="background", discard=True)
    assert opened["already_attached"] is False
    assert opened["session"]["pid"] != pid
    assert wait_gone(pid, 10)
    assert not working_folder.exists()
    assert proc.ok("get_scene")["objects"] == []


def test_a_busy_live_session_is_kept_and_let_go_only_with_discard(mcp_process, tmp_path):
    with FakeAddon(hello_then_silent) as fake:
        proc = mcp_process(env={"BLENDERLENS_MODE": "live", "BLENDERLENS_PORT": str(fake.port),
                                "BLENDERLENS_TOKEN": "fake-token", "BLENDERLENS_TIMEOUT": "2",
                                "BLENDERLENS_RENDER_DIR": str(tmp_path / "r")})
        proc.initialize()
        proc.refused("run_python", "deadline", code="anything", timeout=1)

        session = proc.ok("get_status")["session"]
        assert session is not None, "the deadline dropped the live session"
        assert session["mode"] == "live"
        assert session["busy"]["command"] == "run_python"
        proc.refused("get_scene", "busy")
        proc.refused("close_session", "busy")

        closed = proc.ok("close_session", discard=True)
        assert closed["closed"] == "live"
        assert fake.wait_for_peer_close(), "the server did not close its connection to the add-on"
        assert fake.commands().count("run_python") == 1
        assert "get_scene" not in fake.commands(), "a call was sent behind the stuck command"


@pytest.mark.integration
def test_auto_mode_reports_a_slow_addon_as_busy_and_starts_no_background_blender(mcp_process, blender_binary,
                                                                                   tmp_path):
    # BLENDER_BIN is real, so falling back to a background Blender would succeed, and the test checks that the
    # server does not fall back.
    with FakeAddon(silent) as fake:
        proc = mcp_process(env={"BLENDER_BIN": blender_binary, "BLENDERLENS_MODE": "auto",
                                "BLENDERLENS_PORT": str(fake.port), "BLENDERLENS_RENDER_DIR": str(tmp_path / "r")})
        proc.initialize()
        payload = proc.refused("get_scene", "busy")
        assert f"port {fake.port}" in payload["error"]
        assert "busy" in payload["error"]
        assert payload["exit"] == 12
        proc.refused("open_session", "busy", mode="auto")
        stderr = proc.close()
    assert "started background Blender" not in stderr


def test_a_refused_port_in_auto_mode_still_falls_back(mcp_process, tmp_path):
    # Nothing listens on the port, so auto mode falls back to a background Blender, and with no Blender binary
    # at the given path that fails with ``blender_missing``.
    proc = mcp_process(env={"BLENDERLENS_MODE": "auto", "BLENDERLENS_PORT": str(free_port()),
                            "BLENDER_BIN": str(tmp_path / "no-blender.exe"),
                            "BLENDERLENS_RENDER_DIR": str(tmp_path / "r")})
    proc.initialize()
    proc.refused("get_scene", "blender_missing")
