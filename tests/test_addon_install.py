"""Tests for installing the add-on the way a Blender user does.

The add-on is built into a zip, validated by Blender, installed and enabled with Blender's own
``extension`` command, and then checked to respond on its socket. Everything happens in a throwaway
user folder set through ``BLENDER_USER_RESOURCES``, so the real Blender configuration on the machine
is never touched.
"""

import json
import subprocess
import zipfile

import pytest

from blenderlens_mcp import __version__
from blenderlens_mcp.cli import build_addon
from blenderlens_mcp.session import PROTOCOL
from tests.conftest import isolated_blender_env

pytestmark = pytest.mark.integration

MODULE = "bl_ext.user_default.blenderlens"

#: Code that checks the installed add-on in a background Blender: that it is enabled and has its operators, panel and
#: preferences, and that its bridge, polled by hand, serves requests and stops.
CHECK = r"""
import bpy, json, socket, sys, time
name = "MODULE"
result = {"enabled": name in bpy.context.preferences.addons}
module = sys.modules.get(name)
result["operators"] = [hasattr(bpy.ops.blenderlens, op) for op in ("start", "stop")]
result["panel"] = hasattr(bpy.types, "BLENDERLENS_PT_panel")
prefs = bpy.context.preferences.addons[name].preferences
result["prefs"] = {"port": prefs.port, "host": prefs.host, "auto_start": prefs.auto_start}

# Timers never fire in a background Blender, so pump the bridge by hand, as the add-on's timer would.
server = module.start_bridge(port=0)
client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
client.settimeout(0.05)

def ask(payload):
    # Start made a token, and every request must carry it.
    client.sendall((json.dumps({**payload, "token": server.token}) + "\n").encode())
    buffer, deadline = b"", time.time() + 20
    while b"\n" not in buffer and time.time() < deadline:
        server.poll(0.05)
        try:
            buffer += client.recv(1 << 20)
        except (socket.timeout, BlockingIOError):
            pass
    return json.loads(buffer.split(b"\n")[0])

result["hello"] = ask({"id": 1, "command": "hello"})
result["created"] = ask({"id": 2, "command": "create_object", "params": {"kind": "cube", "name": "FromAddon"}})
module.stop_bridge()
result["stopped"] = module._bridge is None
print("CHECK=" + json.dumps(result))
""".replace("MODULE", MODULE)


@pytest.fixture(scope="module")
def installed(blender_binary, tmp_path_factory):
    """Build the add-on zip, install and enable it with Blender's ``extension`` command, and return the paths."""
    root = tmp_path_factory.mktemp("addon")
    archive = build_addon(str(root / f"blenderlens-{__version__}.zip"))
    env = isolated_blender_env(root / "user")
    run = subprocess.run([blender_binary, "--command", "extension", "install-file", "--repo", "user_default",
                          "--enable", str(archive)], env=env, capture_output=True, text=True, timeout=180)
    assert run.returncode == 0, run.stdout + run.stderr
    return {"archive": archive, "env": env, "root": root, "install_output": run.stdout}


def test_the_zip_holds_the_addon_with_its_manifest_at_the_root(installed):
    names = set(zipfile.ZipFile(installed["archive"]).namelist())
    assert {"blender_manifest.toml", "__init__.py", "bridge.py", "commands.py", "common.py", "meshops.py",
            "preview.py"} <= names
    assert not any(n.endswith(".pyc") or "__pycache__" in n for n in names)


def test_blender_validates_the_zip(installed, blender_binary):
    run = subprocess.run([blender_binary, "--factory-startup", "--command", "extension", "validate",
                          str(installed["archive"])], capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr


def test_the_installed_addon_is_enabled_and_responds_on_its_socket(installed, blender_binary):
    run = subprocess.run([blender_binary, "--background", "--python-expr", CHECK], env=installed["env"],
                         capture_output=True, text=True, timeout=180)
    line = next((ln for ln in run.stdout.splitlines() if ln.startswith("CHECK=")), None)
    assert line, run.stdout[-3000:] + run.stderr[-3000:]
    result = json.loads(line[len("CHECK="):])
    assert result["enabled"] is True
    assert result["operators"] == [True, True]
    assert result["panel"] is True
    assert result["prefs"] == {"port": 9877, "host": "127.0.0.1", "auto_start": False}
    hello = result["hello"]
    assert hello["ok"] is True
    assert hello["result"]["mode"] == "live"
    assert hello["result"]["addon_version"] == __version__
    assert hello["result"]["protocol"] == PROTOCOL
    assert result["created"]["ok"] is True
    assert result["created"]["result"]["name"] == "FromAddon"
    assert result["stopped"] is True
