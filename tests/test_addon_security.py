"""Tests for the add-on's side of the live bridge's safeguards, in a background Blender with the add-on installed.

The safeguards are the token that every connection must prove itself with, the connection file that
hands the token to the server, the preference that turns off Python from the agent, and a timer
function that agent code cannot stop. The code exercised is what a Blender window runs when someone
presses Start in the add-on's panel: the add-on's ``start_bridge`` with its preferences, the
connection file, ``_tick`` (called by hand, because timers never fire in a background Blender), Stop,
and disabling the add-on. Only the timer and the panel need a real Blender window, and
``test_live_security`` covers those.

Everything happens in a throwaway user folder and the suite's own runtime folder.
"""

import json
import socket
import subprocess
import time

import pytest

from blenderlens_mcp import __version__
from blenderlens_mcp.cli import build_addon
from tests.conftest import free_port, isolated_blender_env

pytestmark = pytest.mark.integration

MODULE = "bl_ext.user_default.blenderlens"

#: Code that starts the add-on's bridge on ``PORT`` with Allow Python from the agent off, checks that its timer survives
#: a KeyboardInterrupt, serves until a shutdown request, and then reports what Stop, a Start on ``SECOND`` and disabling
#: the add-on do to the connection file.
SCRIPT = r"""
import bpy, json, os, sys, time
name = "MODULE"
module = sys.modules[name]
prefs = bpy.context.preferences.addons[name].preferences
out = {"token_before": prefs.token, "allow_python_default": getattr(prefs, "allow_python", None)}
if hasattr(prefs, "allow_python"):
    prefs.allow_python = False
server = module.start_bridge(port=PORT)
out["token_after"] = prefs.token
out["bridge_token"] = getattr(server, "token", None)

# The timer's function must survive a KeyboardInterrupt raised in poll, and ask to be called again.
def boom(*args, **kwargs):
    raise KeyboardInterrupt
server.poll = boom
try:
    out["tick_after_interrupt"] = module._tick()
except BaseException as exc:
    out["tick_after_interrupt"] = repr(exc)
del server.poll
print("STARTED=" + json.dumps(out), flush=True)

deadline = time.time() + 170
while not server.stop_requested and time.time() < deadline:
    module._tick()
    time.sleep(0.01)

folder = os.environ["BLENDERLENS_RUNTIME_DIR"]
first = os.path.join(folder, "live-PORT.json")
second = os.path.join(folder, "live-SECOND.json")
out = {}
module.stop_bridge()
out["file_after_stop"] = os.path.exists(first)
module.start_bridge(port=SECOND)
out["file_after_restart"] = os.path.exists(second)
if out["file_after_restart"]:
    with open(second, encoding="utf-8") as handle:
        out["token_after_restart"] = json.load(handle).get("token")
bpy.ops.preferences.addon_disable(module=name)
out["file_after_disable"] = os.path.exists(second)
print("STOPPED=" + json.dumps(out), flush=True)
""".replace("MODULE", MODULE)


def marked(log, prefix, process, seconds=120):
    """Return the JSON that Blender printed after ``prefix`` in ``log``, failing the test if it never does."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        line = next((ln for ln in text.splitlines() if ln.startswith(prefix)), None)
        if line:
            return json.loads(line[len(prefix):])
        if process.poll() is not None:
            break
        time.sleep(0.2)
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    pytest.fail(f"no {prefix} line from Blender:\n{text[-3000:]}")


@pytest.fixture(scope="module")
def started_addon(blender_binary, tmp_path_factory, runtime_dir):
    """Install the add-on, and start its bridge through its own ``start_bridge`` in a background Blender.

    The Blender runs ``SCRIPT``, which records what Start did to the preferences, then keeps calling
    ``_tick`` until a test asks the bridge to shut down.
    """
    root = tmp_path_factory.mktemp("addon-security")
    archive = build_addon(str(root / f"blenderlens-{__version__}.zip"))
    env = isolated_blender_env(root / "user")
    run = subprocess.run([blender_binary, "--command", "extension", "install-file", "--repo", "user_default",
                          "--enable", str(archive)], env=env, capture_output=True, text=True, timeout=180)
    assert run.returncode == 0, run.stdout + run.stderr
    port, second = free_port(), free_port()
    log = root / "blender.log"
    with open(log, "wb") as handle:
        process = subprocess.Popen([blender_binary, "--background", "--python-expr",
                                    SCRIPT.replace("SECOND", str(second)).replace("PORT", str(port))],
                                   env=env, stdout=handle, stderr=subprocess.STDOUT)
    try:
        started = marked(log, "STARTED=", process)
        yield {"port": port, "second": second, "started": started, "log": log, "process": process,
               "runtime": runtime_dir}
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def closed_without_a_reply(port, payload):
    """Send ``payload`` on a new connection, and return whether the add-on closed it without replying."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=20)
    try:
        sock.sendall((json.dumps(payload) + "\n").encode())
        try:
            data = sock.recv(65536)
        except (ConnectionResetError, ConnectionAbortedError):
            return True
        return data == b""
    finally:
        sock.close()


def ask(port, token, *payloads):
    """Send each payload with ``token`` on one connection, and return the add-on's replies."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=20)
    replies, buffer = [], b""
    try:
        for payload in payloads:
            sock.sendall((json.dumps({**payload, "token": token}) + "\n").encode())
            while b"\n" not in buffer:
                chunk = sock.recv(65536)
                assert chunk, f"the add-on closed the connection at {payload}"
                buffer += chunk
            line, buffer = buffer.split(b"\n", 1)
            replies.append(json.loads(line))
    finally:
        sock.close()
    return replies


def test_start_generates_a_token_keeps_it_and_writes_the_connection_file(started_addon):
    started = started_addon["started"]
    assert started["token_before"] == "", "a fresh install already has a token"
    token = started["token_after"]
    assert token and len(token) >= 32, "Start did not generate a token and keep it in the preferences"
    assert started["bridge_token"] == token
    path = started_addon["runtime"] / f"live-{started_addon['port']}.json"
    info = json.loads(path.read_text(encoding="utf-8"))
    assert set(info) == {"host", "port", "token", "pid", "blender_version", "addon_version", "started"}
    assert info["token"] == token
    assert info["port"] == started_addon["port"] and info["host"] == "127.0.0.1"
    assert info["pid"] == started_addon["process"].pid
    assert info["addon_version"] == __version__
    assert info["blender_version"].split(".")[0].isdigit()


def test_a_hello_without_the_token_or_with_another_is_closed_without_a_reply(started_addon):
    port = started_addon["port"]
    assert closed_without_a_reply(port, {"id": 1, "command": "hello"})
    assert closed_without_a_reply(port, {"id": 1, "command": "hello", "token": "guess"})
    assert closed_without_a_reply(port, {"id": 1, "command": "get_scene",
                                         "token": started_addon["started"]["token_after"]})


def test_the_server_attaches_with_the_connection_file_alone(started_addon, mcp_process, tmp_path):
    proc = mcp_process(env={"BLENDERLENS_MODE": "live", "BLENDERLENS_PORT": str(started_addon["port"]),
                            "BLENDERLENS_TOKEN": "", "BLENDERLENS_RENDER_DIR": str(tmp_path / "renders")})
    proc.initialize()
    assert proc.ok("get_scene")["objects"] is not None
    assert proc.ok("get_status")["session"]["mode"] == "live"


def test_with_allow_python_off_the_addon_refuses_run_python(started_addon, mcp_process, tmp_path):
    assert started_addon["started"]["allow_python_default"] is True, "Allow Python from the agent is off by default"
    proc = mcp_process(env={"BLENDERLENS_MODE": "live", "BLENDERLENS_PORT": str(started_addon["port"]),
                            "BLENDERLENS_TOKEN": "", "BLENDERLENS_RENDER_DIR": str(tmp_path / "renders")})
    proc.initialize()
    payload = proc.refused("run_python", "not_allowed", code="result = 1")
    assert payload["exit"] == 13
    assert "Allow Python from the agent" in payload["error"] + payload["hint"]
    assert proc.ok("get_scene") is not None, "other tools stopped working"


def test_with_allow_python_off_the_addon_refuses_run_script(started_addon, mcp_process, tmp_path):
    script = tmp_path / "build.py"
    script.write_text("result = 1\n", encoding="utf-8")
    proc = mcp_process(env={"BLENDERLENS_MODE": "live", "BLENDERLENS_PORT": str(started_addon["port"]),
                            "BLENDERLENS_TOKEN": "", "BLENDERLENS_RENDER_DIR": str(tmp_path / "renders")})
    proc.initialize()
    payload = proc.refused("run_script", "not_allowed", path=str(script))
    assert payload["exit"] == 13
    assert "run_script" in payload["error"]
    assert "Allow Python from the agent" in payload["hint"]


def test_the_timer_function_survives_a_keyboard_interrupt_in_poll(started_addon):
    assert started_addon["started"]["tick_after_interrupt"] == 0.05, "the timer function did not ask to run again"


def test_stop_and_disable_remove_the_connection_file_and_the_token_is_kept(started_addon):
    """Stop removes the connection file, a second Start writes it again with the same token, and disabling removes it.

    This test must run last in the module, because its ``shutdown`` request ends the loop that serves the add-on's
    bridge. The reply says that the add-on, which runs in live mode, keeps listening.
    """
    token = started_addon["started"]["token_after"]
    shutdown = ask(started_addon["port"], token, {"id": 1, "command": "hello"}, {"id": 2, "command": "shutdown"})[1]
    assert shutdown["ok"] is True and shutdown["result"]["stopping"] is False, shutdown
    assert "Stop" in shutdown["result"]["note"], shutdown
    stopped = marked(started_addon["log"], "STOPPED=", started_addon["process"])
    assert stopped["file_after_stop"] is False
    assert stopped["file_after_restart"] is True
    assert stopped["token_after_restart"] == token
    assert stopped["file_after_disable"] is False
