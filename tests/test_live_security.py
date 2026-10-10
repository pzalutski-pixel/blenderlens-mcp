"""Tests for the live bridge's safeguards in a real Blender window.

They cover the token that Start generates, the connection file, the proof that every connection must
give on its first line, ``run_python`` that cannot end Blender, the add-on's timer that keeps running when
agent code raises SystemExit or KeyboardInterrupt or the bridge's own poll raises KeyboardInterrupt, and the
preference with which the person at the Blender window turns off Python from the agent. The tests open a
Blender window (small, behind other windows, and without focus), so they run only when asked:

    BLENDERLENS_TEST_LIVE=1 pytest -m live

The add-on is installed into a throwaway user folder, and the connection file goes to the suite's own
runtime folder (see ``conftest``), so the machine's real folders are never touched. The Blender window starts
with no token in the add-on's preferences, as a fresh install does. The tests run in order, and the last one
turns Python off, which no agent can turn back on.
"""

import json
import os
import socket
import subprocess
import time

import pytest

from blenderlens_mcp.cli import build_addon
from tests.conftest import MCPProcess, free_port, isolated_blender_env

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("BLENDERLENS_TEST_LIVE") != "1",
                       reason="This test opens a Blender window. Set BLENDERLENS_TEST_LIVE=1 to run it."),
]

ADDON = "bl_ext.user_default.blenderlens"

#: Code that Blender runs as it starts: a timer sets the add-on's port to PORT a second later and presses Start.
START = r"""
import bpy
def _start():
    prefs = bpy.context.preferences.addons["bl_ext.user_default.blenderlens"].preferences
    prefs.port = PORT
    bpy.ops.blenderlens.start()
    return None
bpy.app.timers.register(_start, first_interval=1.0)
"""

MODULE = f"import sys\nm = sys.modules[{ADDON!r}]\nprefs = bpy.context.preferences.addons[{ADDON!r}].preferences\n"


@pytest.fixture(scope="module")
def live(blender_binary, tmp_path_factory, runtime_dir):
    """Install the add-on, open a Blender window that starts it, and attach a server in live mode.

    Provide the server, the test's folder, the Blender process, the port and the path of the connection file.
    """
    root = tmp_path_factory.mktemp("live-security")
    env = isolated_blender_env(root / "user")
    archive = build_addon(str(root / "blenderlens.zip"))
    install = subprocess.run([blender_binary, "--command", "extension", "install-file", "--repo", "user_default",
                              "--enable", str(archive)], env=env, capture_output=True, text=True, timeout=180)
    assert install.returncode == 0, install.stdout + install.stderr
    port = free_port()
    log = open(root / "blender.log", "wb")
    blender = subprocess.Popen([blender_binary, "--no-window-focus", "--window-geometry", "40", "40", "900", "600",
                                "--python-expr", START.replace("PORT", str(port))],
                               env=env, stdout=log, stderr=subprocess.STDOUT)
    # BLENDERLENS_TOKEN is empty, so the server must find the token in the connection file on its own.
    server = MCPProcess(cwd=str(root), env={"BLENDERLENS_MODE": "live", "BLENDERLENS_PORT": str(port),
                                            "BLENDERLENS_TOKEN": "", "BLENDER_BIN": blender_binary,
                                            "BLENDERLENS_RENDER_DIR": str(root / "renders")})
    server.initialize()
    deadline = time.time() + 120
    while not server.ok("get_status")["live_addon"]["responding"]:
        if blender.poll() is not None or time.time() > deadline:
            blender.kill()
            server.close()
            pytest.fail("the add-on never responded:\n" + (root / "blender.log").read_text(errors="replace")[-2000:])
        time.sleep(1)
    try:
        yield {"server": server, "root": root, "blender": blender, "port": port,
               "file": runtime_dir / f"live-{port}.json"}
    finally:
        server.close()
        blender.terminate()
        try:
            blender.wait(timeout=20)
        except subprocess.TimeoutExpired:
            blender.kill()
        log.close()


def closed_without_a_reply(port, payload):
    """Send ``payload`` on a new connection, and return whether the add-on closed it without replying."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=20)
    try:
        sock.sendall((json.dumps(payload) + "\n").encode())
        try:
            return sock.recv(65536) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            return True
    finally:
        sock.close()


def test_start_generated_a_token_and_the_server_attached_with_the_file_alone(live):
    server = live["server"]
    token = server.ok("run_python", code=MODULE + "result = prefs.token")["result"]
    assert token and len(token) >= 32
    info = json.loads(live["file"].read_text(encoding="utf-8"))
    assert info["token"] == token and info["port"] == live["port"]
    assert info["pid"] == live["blender"].pid
    assert server.ok("get_status")["session"]["mode"] == "live"


def test_a_hello_without_the_token_is_closed_without_a_reply(live):
    assert closed_without_a_reply(live["port"], {"id": 1, "command": "hello"})
    assert closed_without_a_reply(live["port"], {"id": 1, "command": "hello", "token": "guess"})
    assert live["server"].ok("get_scene") is not None, "the add-on stopped serving the server"


def test_the_panel_says_a_token_is_required_and_names_the_last_command(live):
    code = MODULE + (
        "labels = []\n"
        "class Layout:\n"
        "    def label(self, text='', **kw): labels.append(text)\n"
        "    def operator(self, *a, **kw): pass\n"
        "    def prop(self, *a, **kw): pass\n"
        "    def row(self, *a, **kw): return self\n"
        "    def column(self, *a, **kw): return self\n"
        "panel = type('P', (), {})()\n"
        "panel.layout = Layout()\n"
        "m.BLENDERLENS_PT_panel.draw(panel, bpy.context)\n"
        "result = labels\n"
    )
    labels = live["server"].ok("run_python", code=code)["result"]
    assert "Token required" in labels, labels
    assert "Last command: run_python" in labels, labels


@pytest.mark.parametrize("code, raised", [("raise SystemExit(3)", "SystemExit"),
                                          ("raise KeyboardInterrupt", "KeyboardInterrupt")])
def test_run_python_cannot_end_blender_in_live_mode_and_the_timer_stays(live, code, raised):
    server, blender = live["server"], live["blender"]
    payload = server.refused("run_python", "python_error", code=code)
    assert payload["error"].startswith(raised)
    if raised == "SystemExit":
        assert "run_python cannot exit Blender" in payload["error"]
    time.sleep(2)
    assert blender.poll() is None, "the Blender window closed"
    alive = server.ok("run_python", code=MODULE + "result = bpy.app.timers.is_registered(m._tick)")
    assert alive["result"] is True


def test_the_timer_survives_a_keyboard_interrupt_in_its_own_poll(live):
    """The add-on's timer survives a KeyboardInterrupt raised by the bridge's poll itself, outside any command.

    Blender drops a timer whose function raises, so the timer function must catch the interrupt.
    """
    server = live["server"]
    arm = MODULE + (
        "bridge = m._bridge\n"
        "def boom(*args, **kwargs):\n"
        "    del bridge.poll\n"
        "    raise KeyboardInterrupt\n"
        "bridge.poll = boom\n"
        "result = 'armed'\n"
    )
    assert server.ok("run_python", code=arm)["result"] == "armed"
    time.sleep(1)  # Wait for several ticks: the first raises, and the ones after it serve requests.
    assert server.ok("get_scene") is not None
    assert server.ok("run_python", code=MODULE + "result = bpy.app.timers.is_registered(m._tick)")["result"] is True


def _closed_by_the_addon(sock):
    """Return whether the add-on has closed ``sock``, without waiting."""
    sock.setblocking(False)
    try:
        return sock.recv(1) == b""
    except BlockingIOError:
        return False
    except (ConnectionResetError, ConnectionAbortedError):
        return True


def test_six_hundred_idle_connections_do_not_block_live_mode(live):
    """With 600 idle connections open, the add-on in live mode still serves the server and closes the idle ones.

    In live mode, the add-on's timer polls the same bridge, with the same connection caps, as a background
    Blender does.
    """
    server, port = live["server"], live["port"]
    flood = []
    try:
        deadline = time.monotonic() + 45
        while len(flood) < 600 and time.monotonic() < deadline:
            try:
                flood.append(socket.create_connection(("127.0.0.1", port), timeout=2))
            except OSError:
                time.sleep(0.01)  # The listen backlog is full until the next timer tick accepts connections.
        assert server.ok("get_scene") is not None, "the add-on stopped serving the server's own connection"
        # More than 512, the most sockets that select takes on Windows. The timer in a busy Blender window may
        # accept the rest too slowly for all 600 to connect before the deadline.
        assert len(flood) > 512, f"only {len(flood)} connections were accepted"
        assert closed_without_a_reply(port, {"id": 1, "command": "hello", "token": "guess"}), \
            "a hello with a wrong token got a reply"
        open_ = list(flood)
        deadline = time.monotonic() + 25  # The newest stay open until their 10 seconds to prove themselves end.
        while open_ and time.monotonic() < deadline:
            open_ = [s for s in open_ if not _closed_by_the_addon(s)]
            time.sleep(0.2)
        assert not open_, f"{len(open_)} idle connections are still open"
        assert server.ok("run_python", code=MODULE + "result = len(m._bridge.clients)")["result"] <= 2
    finally:
        for sock in flood:
            sock.close()


def test_stop_removes_the_connection_file_and_start_writes_it_again_with_the_same_token(live):
    server = live["server"]
    token = json.loads(live["file"].read_text(encoding="utf-8"))["token"]
    # Stop from a timer, after this call's result is sent, then Start again two seconds later.
    cycle = MODULE + (
        "def _stop():\n"
        "    m.stop_bridge()\n"
        "    bpy.app.timers.register(_start, first_interval=2.0)\n"
        "def _start():\n"
        "    m.start_bridge()\n"
        "bpy.app.timers.register(_stop, first_interval=0.5)\n"
        "result = 'cycling'\n"
    )
    assert server.ok("run_python", code=cycle)["result"] == "cycling"
    deadline = time.time() + 10
    while live["file"].exists() and time.time() < deadline:
        time.sleep(0.05)
    assert not live["file"].exists(), "Stop did not remove the connection file"
    deadline = time.time() + 20
    while not live["file"].exists() and time.time() < deadline:
        time.sleep(0.1)
    assert live["file"].exists(), "Start did not write the connection file again"
    assert json.loads(live["file"].read_text(encoding="utf-8"))["token"] == token, "the token changed"
    # Stop dropped the server's connection. The first call may fail on it, and the call after it attaches again.
    server.call("get_scene")
    assert server.ok("get_scene") is not None


def test_with_allow_python_off_the_addon_refuses_run_python_in_live_mode(live):
    """With Allow Python turned off, the add-on in live mode refuses ``run_python`` and still serves the other tools.

    This test must run last, because once Python is off, no agent can turn it back on.
    """
    server = live["server"]
    assert server.ok("run_python", code=MODULE + "prefs.allow_python = False\nresult = 1")["result"] == 1
    payload = server.refused("run_python", "not_allowed", code="result = 2")
    assert payload["exit"] == 13
    assert server.ok("get_scene") is not None, "the other tools stopped working"
