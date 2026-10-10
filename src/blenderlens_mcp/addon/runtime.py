"""Find the BlenderLens runtime folder, and write, read and remove the live bridge's connection files.

While the add-on listens in live mode, it writes ``live-<port>.json`` in this folder, holding the
host, port and token the MCP server needs, and removes the file when it stops. The server reads
the file for the port it is configured with, so nobody has to copy a token by hand.

This module does not import bpy. The add-on imports it as usual, and the MCP server loads it
from this file's path, since importing the add-on package would import bpy. One copy of this
code thus serves both sides.

The folder is ``BLENDERLENS_RUNTIME_DIR`` when set, otherwise the platform's per-user place:
``%LOCALAPPDATA%\\blenderlens`` on Windows, ``~/Library/Application Support/blenderlens`` on macOS,
and ``$XDG_STATE_HOME/blenderlens`` or ``~/.local/state/blenderlens`` elsewhere. On POSIX the
folder is created 0700 and the file written 0600, so other accounts cannot read the token.
"""

import json
import os
import sys


def runtime_dir():
    """Return the path of the runtime folder, which may not exist yet; ``ensure_runtime_dir`` creates it."""
    explicit = os.environ.get("BLENDERLENS_RUNTIME_DIR")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return os.path.join(base, "blenderlens")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "blenderlens")
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
    return os.path.join(state, "blenderlens")


def ensure_runtime_dir():
    """Create the runtime folder if it is missing (with mode 0700 on POSIX), and return its path."""
    folder = runtime_dir()
    if not os.path.isdir(folder):
        os.makedirs(folder, mode=0o700, exist_ok=True)
    return folder


def connection_path(port):
    """Return the path of the connection file for ``port``, which may not exist."""
    return os.path.join(runtime_dir(), f"live-{int(port)}.json")


def write_connection(info):
    """Write ``info``, which holds ``port``, as the connection file for that port, and return the file's path.

    The file is written in full under a temporary name and then renamed into place, so a reader never sees half
    a file. On POSIX it is created with mode 0600.
    """
    ensure_runtime_dir()
    path = connection_path(info["port"])
    partial = f"{path}.{os.getpid()}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    handle = os.open(partial, flags, 0o600)
    try:
        os.write(handle, json.dumps(info, indent=1).encode("utf-8"))
    finally:
        os.close(handle)
    os.replace(partial, path)
    return path


def read_connection(port):
    """Return the connection file for ``port`` as a dict.

    Raises FileNotFoundError when there is none, and OSError or ValueError when it cannot be read.
    """
    with open(connection_path(port), encoding="utf-8") as handle:
        info = json.load(handle)
    if not isinstance(info, dict):
        raise ValueError("it does not hold a JSON object")
    return info


def remove_connection(port):
    """Remove the connection file for ``port`` if this process wrote it, and return whether it did.

    Another Blender may have started on the port since and written its own file; that file is left alone, and
    so is a file that cannot be read.
    """
    try:
        if read_connection(port).get("pid") != os.getpid():
            return False
        os.remove(connection_path(port))
    except (OSError, ValueError):
        return False
    return True
