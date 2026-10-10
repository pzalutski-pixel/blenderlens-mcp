"""Serve BlenderLens commands inside ``blender --background`` until told to stop.

The MCP server starts Blender as::

    blender --background --factory-startup [file.blend] --python background.py -- \
        --host 127.0.0.1 --port 0 --ready-file <path> [--empty]

and reads the port this script bound from the ready file. The command handlers are the
add-on's own, loaded from the folder beside this file, so a background session and a
Blender window running the add-on handle every command the same way.

Blender stops on the ``shutdown`` command, or when stdin closes. The MCP server holds the
other end of that pipe, so if the server dies for any reason, this Blender exits too instead
of running on with nobody to talk to. The ``--no-watch`` option, which the server never
passes, keeps Blender running when stdin closes, for a session started by hand. Nothing a
request sends, and nothing a command raises, stops it otherwise, so a failing command cannot
end the session. ``run_python`` runs arbitrary Python, though, so code that calls
``os._exit`` can still end it.

The bridge serves only connections whose first line carries the session's token, so that no
other local process can send it commands. The server passes the token in
BLENDERLENS_SESSION_TOKEN; when it is started without one, this script makes its own and
writes it to the ready file.
"""

import argparse
import importlib.util
import json
import os
import secrets
import sys
import threading
import time

ADDON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "addon")
MODULE = "blenderlens_addon"


def load_addon():
    """Import the add-on package under a name that cannot collide with Blender's own modules."""
    spec = importlib.util.spec_from_file_location(
        MODULE, os.path.join(ADDON_DIR, "__init__.py"), submodule_search_locations=[ADDON_DIR])
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE] = module
    spec.loader.exec_module(module)
    return module


def watch_parent(flag):
    """Set ``flag`` when stdin reaches end of file, which happens when the MCP server exits.

    It reads file descriptor 0 directly, because Blender replaces ``sys.stdin`` with None
    unless it runs with --debug, while the pipe the server handed over is still descriptor 0.
    """
    try:
        while True:
            if not os.read(0, 1024):
                flag.set()
                return
    except OSError:
        return  # With no usable stdin there is nothing to watch; the shutdown command still works.


def main():
    """Start the bridge, write the ready file, and serve commands until told to stop.

    The ready file is written under a temporary name and renamed into place, so the server never
    reads half of it. Because it can hold the token, it is created with mode 0600, which on POSIX
    makes it readable by its owner only; on Windows the mode has no effect, and the file has the
    permissions of the folder it is in.
    """
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(prog="blenderlens-background")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--empty", action="store_true", help="start from an empty scene")
    parser.add_argument("--no-watch", action="store_true", help="do not stop when stdin closes")
    args = parser.parse_args(argv)

    load_addon()
    from blenderlens_addon import bridge, commands

    if args.empty:
        commands.reset_to_empty()

    token = os.environ.get("BLENDERLENS_SESSION_TOKEN")
    made = not token
    if made:
        token = secrets.token_urlsafe(24)
    server = bridge.Bridge(mode="background", token=token)
    server.start(args.host, args.port)

    parent_gone = threading.Event()
    if not args.no_watch:
        threading.Thread(target=watch_parent, args=(parent_gone,), daemon=True).start()

    ready = {"port": server.port, "host": server.host, "pid": os.getpid()}
    if made:
        ready["token"] = token
    partial = args.ready_file + ".part"
    handle = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(handle, json.dumps(ready).encode("utf-8"))
    finally:
        os.close(handle)
    os.replace(partial, args.ready_file)
    commands.log(f"background session listening on {server.host}:{server.port}")

    try:
        while not server.stop_requested and not parent_gone.is_set():
            try:
                server.poll(0.25)
            except BaseException as exc:  # noqa: BLE001 -- KeyboardInterrupt too: only shutdown or EOF stops it
                commands.log(f"bridge poll failed: {exc!r}")
                time.sleep(0.05)  # A failure may repeat at once; the pause keeps the loop from spinning on it.
    finally:
        server.stop()
    commands.log("background session stopped" + (" (parent gone)" if parent_gone.is_set() else ""))


main()
