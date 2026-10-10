"""Tests for the connection file, which hands the live bridge's token to the server without anyone copying it.

The add-on writes ``live-<port>.json`` (host, port, token, process id, versions and start time) to the
BlenderLens runtime folder while it listens, and removes it when it stops. The server reads the file for the
configured port, but ``BLENDERLENS_TOKEN`` takes precedence when it is set. When the add-on refuses the token,
the error kind is ``token_mismatch`` and its hint names the file the server read.

The runtime module does not import ``bpy``, so it is tested here directly, loaded as the server loads it. The
server's side is tested against ``bridge.py`` running in this process, as in ``test_bridge_security``.
"""

import importlib.util
import json
import os
import stat
import sys
import threading

import pytest

from blenderlens_mcp import __version__, session
from blenderlens_mcp.errors import ToolFailure
from tests.conftest import SRC, free_port
from tests.test_bridge_security import TOKEN, bridge_module  # noqa: F401 -- bridge_module is a fixture

ADDON = SRC / "blenderlens_mcp" / "addon"


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """Point ``BLENDERLENS_RUNTIME_DIR`` at a runtime folder of this test's own, and return its path.

    The folder is not created yet.
    """
    path = tmp_path / "runtime"
    monkeypatch.setenv("BLENDERLENS_RUNTIME_DIR", str(path))
    return path


@pytest.fixture
def runtime(folder):
    """Load ``addon/runtime.py`` from its file, as the server loads it, and return the module."""
    spec = importlib.util.spec_from_file_location("bltest_runtime", ADDON / "runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.folder = folder
    return module


# ---------------------------------------------------------------------------
# The runtime folder and the file
# ---------------------------------------------------------------------------


def test_the_runtime_folder_follows_the_variable_then_the_platform(runtime, monkeypatch, tmp_path):
    assert runtime.runtime_dir() == str(runtime.folder)
    monkeypatch.delenv("BLENDERLENS_RUNTIME_DIR")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert runtime.runtime_dir() == os.path.join(str(tmp_path / "local"), "blenderlens")

    monkeypatch.setattr(sys, "platform", "darwin")
    assert runtime.runtime_dir() == os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                                                 "blenderlens")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert runtime.runtime_dir() == os.path.join(str(tmp_path / "state"), "blenderlens")
    monkeypatch.delenv("XDG_STATE_HOME")
    assert runtime.runtime_dir() == os.path.join(os.path.expanduser("~"), ".local", "state", "blenderlens")


def test_the_connection_file_is_written_whole_and_read_back(runtime):
    info = {"host": "127.0.0.1", "port": 9911, "token": "abc", "pid": os.getpid(), "blender_version": "5.0.1",
            "addon_version": __version__, "started": "2026-10-05T12:00:00+00:00"}
    path = runtime.write_connection(info)
    assert path == str(runtime.folder / "live-9911.json") == runtime.connection_path(9911)
    assert json.loads((runtime.folder / "live-9911.json").read_text(encoding="utf-8")) == info
    assert runtime.read_connection(9911) == info
    assert [p.name for p in runtime.folder.iterdir()] == ["live-9911.json"], "a partial file was left behind"


def test_only_the_process_that_wrote_the_file_removes_it(runtime):
    runtime.write_connection({"host": "127.0.0.1", "port": 9912, "token": "abc", "pid": os.getpid() + 1})
    assert runtime.remove_connection(9912) is False, "the file another Blender wrote was removed"
    assert (runtime.folder / "live-9912.json").exists()
    runtime.write_connection({"host": "127.0.0.1", "port": 9912, "token": "abc", "pid": os.getpid()})
    assert runtime.remove_connection(9912) is True
    assert not (runtime.folder / "live-9912.json").exists()
    assert runtime.remove_connection(9912) is False, "removing a connection file already gone did not report False"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX file modes. There, the runtime folder "
                                                     "is private because it is under the user's own AppData folder.")
def test_the_folder_and_the_file_are_private_on_posix(runtime):
    runtime.write_connection({"host": "127.0.0.1", "port": 9913, "token": "abc", "pid": os.getpid()})
    assert stat.S_IMODE(runtime.folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((runtime.folder / "live-9913.json").stat().st_mode) == 0o600


def test_the_server_loads_the_runtime_module_without_importing_bpy(runtime):
    assert session.runtime.runtime_dir() == runtime.runtime_dir()
    assert session.runtime.connection_path(1234) == runtime.connection_path(1234)
    assert "bpy" not in sys.modules


# ---------------------------------------------------------------------------
# The server finds the token
# ---------------------------------------------------------------------------


@pytest.fixture
def listening(bridge_module):  # noqa: F811 -- the fixture imported above
    """Serve ``bridge.py`` on a thread, standing in for a Blender window in which the add-on has been started."""
    bridge = bridge_module.Bridge(mode="live", token=TOKEN)
    bridge.start("127.0.0.1", 0)
    stop = threading.Event()

    def serve():
        """Poll the bridge until the test ends."""
        while not stop.is_set():
            bridge.poll(0.05)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield bridge
    stop.set()
    thread.join(5)
    bridge.stop()


def write_file(folder, port, token):
    """Write a connection file for ``port`` holding ``token`` into ``folder``, and return its path."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"live-{port}.json"
    path.write_text(json.dumps({"host": "127.0.0.1", "port": port, "token": token, "pid": 1}), encoding="utf-8")
    return path


def refusal(port, token):
    """Attach to live mode on ``port`` with ``token``, which must be refused, and return the error payload."""
    with pytest.raises(ToolFailure) as caught:
        session.attach_live("127.0.0.1", port, token).stop()
    return caught.value.payload()


def test_the_server_attaches_with_the_token_from_the_connection_file(folder, listening):
    write_file(folder, listening.port, TOKEN)
    attached = session.attach_live("127.0.0.1", listening.port, None)
    try:
        assert attached.hello["mode"] == "live"
    finally:
        attached.stop()


def test_blenderlens_token_wins_over_the_connection_file(folder, listening):
    write_file(folder, listening.port, "stale")
    attached = session.attach_live("127.0.0.1", listening.port, TOKEN)
    attached.stop()


def test_no_connection_file_is_token_mismatch_naming_the_file(folder, listening):
    payload = refusal(listening.port, None)
    assert payload["kind"] == "token_mismatch" and payload["exit"] == 11
    assert str(folder / f"live-{listening.port}.json") in payload["hint"], payload


def test_a_stale_token_in_the_file_is_token_mismatch_naming_the_file(folder, listening):
    path = write_file(folder, listening.port, "stale")
    payload = refusal(listening.port, None)
    assert payload["kind"] == "token_mismatch"
    assert str(path) in payload["hint"], payload


def test_an_unreadable_connection_file_is_token_mismatch_naming_the_file(folder, listening):
    folder.mkdir(parents=True)
    path = folder / f"live-{listening.port}.json"
    path.write_text("{not json", encoding="utf-8")
    payload = refusal(listening.port, None)
    assert payload["kind"] == "token_mismatch"
    assert str(path) in payload["hint"], payload


def test_a_wrong_blenderlens_token_is_token_mismatch_naming_the_variable(folder, listening):
    write_file(folder, listening.port, TOKEN)
    payload = refusal(listening.port, "wrong")
    assert payload["kind"] == "token_mismatch"
    assert "BLENDERLENS_TOKEN" in payload["hint"], payload


def test_nothing_listening_is_still_not_connected(folder):
    payload = refusal(free_port(), None)
    assert payload["kind"] == "not_connected"


def test_the_mcp_server_attaches_in_live_mode_with_the_file_alone(folder, listening, mcp_process, tmp_path):
    write_file(folder, listening.port, TOKEN)
    proc = mcp_process(env={"BLENDERLENS_MODE": "live", "BLENDERLENS_PORT": str(listening.port),
                            "BLENDERLENS_TOKEN": "", "BLENDERLENS_RUNTIME_DIR": str(folder),
                            "BLENDERLENS_RENDER_DIR": str(tmp_path / "renders")})
    proc.initialize()
    status = proc.ok("get_status")
    assert status["live_addon"]["responding"] is True, status["live_addon"]
    assert proc.ok("get_scene") == {"command": "get_scene"}
    assert proc.ok("get_status")["session"]["mode"] == "live"


def test_the_mcp_server_reports_token_mismatch_in_get_status(folder, listening, mcp_process, tmp_path):
    proc = mcp_process(env={"BLENDERLENS_MODE": "auto", "BLENDERLENS_PORT": str(listening.port),
                            "BLENDERLENS_TOKEN": "", "BLENDERLENS_RUNTIME_DIR": str(folder),
                            "BLENDERLENS_RENDER_DIR": str(tmp_path / "renders")})
    proc.initialize()
    problem = proc.ok("get_status")["live_addon"]["problem"]
    assert problem["kind"] == "token_mismatch"
    assert f"live-{listening.port}.json" in problem["hint"]
    # An add-on that responds but refuses the token is not a reason to start a background Blender.
    payload = proc.refused("get_scene", "token_mismatch")
    assert f"live-{listening.port}.json" in payload["hint"]
