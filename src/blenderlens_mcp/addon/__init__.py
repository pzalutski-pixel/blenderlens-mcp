"""The Blender add-on that lets the BlenderLens MCP server work in a running Blender window.

Press Start in the 3D viewport's sidebar (N) > BlenderLens, and an agent connected to
blenderlens-mcp builds in this Blender window while you watch. Each change it makes is one
undo step, named after the tool that made it.

Start opens the bridge: a socket on this machine that the MCP server connects to and sends its
commands through. The bridge listens only on localhost or a loopback IPv4 address (127.0.0.1 by
default; ::1 is refused), and serves only clients that hold its token. Start makes a token if the
add-on preferences have none, stores it there, and writes it to a connection file in the BlenderLens
runtime folder, where the MCP server reads it, so nobody has to copy the token by hand. Stop removes
the file. The bridge runs Python sent to it unless the preference "Allow Python from the agent" is
off, so leave it stopped when no agent should be working in this Blender.
"""

import atexit
import datetime
import os
import secrets

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty

from . import bridge, commands, runtime

#: The port the bridge listens on unless the preferences name another.
DEFAULT_PORT = 9877
#: How often, in seconds, the timer serves what the bridge has waiting.
POLL_INTERVAL = 0.05

#: The bridge while it runs, else None.
_bridge = None
#: The path of the connection file this Blender wrote, or None.
_connection_file = None
#: The message saying why the connection file could not be written, or None.
_connection_problem = None


def _package():
    """Return the add-on's package name, under which Blender keeps its preferences."""
    return __package__ or __name__


def _prefs():
    """Return the add-on's preferences, or None when the add-on is not enabled."""
    addon = bpy.context.preferences.addons.get(_package())
    return addon.preferences if addon else None


def _tick():
    """Serve what the bridge has waiting, redraw the 3D viewports if anything was served, and ask to run again."""
    if _bridge is None or not _bridge.running:
        return None
    try:
        if _bridge.poll(0.0):
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "VIEW_3D":
                        area.tag_redraw()
    except BaseException as exc:  # noqa: BLE001 -- KeyboardInterrupt too: Blender stops calling a timer that raises
        commands.log(f"bridge poll failed: {exc!r}")
    return POLL_INTERVAL


def _token(prefs):
    """Return the token stored in the add-on preferences, making and storing one on the first Start.

    Without preferences, a new token is returned on every call and nothing is stored.
    """
    token = prefs.token if prefs is not None else ""
    if token:
        return token
    token = secrets.token_urlsafe(24)
    if prefs is not None:
        prefs.token = token
        try:
            # Marked as changed so that Blender saves the preferences with the token, and a Start after a
            # restart uses the same token.
            bpy.context.preferences.is_dirty = True
        except (AttributeError, TypeError):
            pass
    return token


def _write_connection_file(server):
    """Write the connection file for ``server``, or record why it could not be written."""
    global _connection_file, _connection_problem
    info = {
        "host": server.host,
        "port": server.port,
        "token": server.token,
        "pid": os.getpid(),
        "blender_version": bpy.app.version_string,
        "addon_version": commands.addon_version(),
        "started": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        _connection_file = runtime.write_connection(info)
        _connection_problem = None
    except OSError as exc:
        # The bridge keeps listening: an MCP server given the token in BLENDERLENS_TOKEN can connect without
        # the file.
        _connection_file = None
        _connection_problem = f"Could not write the connection file: {exc}"
        commands.log(_connection_problem)


def start_bridge(host=None, port=None, token=None):
    """Start the bridge and return it, or return the bridge that is already running.

    Arguments left out are taken from the add-on preferences. Once the bridge listens, the connection file is
    written and the poll timer registered. Raises OSError when the port is taken or the host is neither
    localhost nor a loopback IPv4 address.
    """
    global _bridge
    if _bridge is not None and _bridge.running:
        return _bridge
    prefs = _prefs()
    host = host or (prefs.host if prefs else "127.0.0.1")
    port = port if port is not None else (prefs.port if prefs else DEFAULT_PORT)
    server = bridge.Bridge(mode="live", token=token or _token(prefs))
    server.start(host, port)
    _bridge = server
    _write_connection_file(server)
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=POLL_INTERVAL, persistent=True)
    commands.log(f"listening on {server.host}:{server.port}")
    return server


def stop_bridge():
    """Stop the bridge and its timer, and remove the connection file this Blender wrote."""
    global _bridge, _connection_file, _connection_problem
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    if _bridge is not None:
        _bridge.stop()
        if _bridge.port is not None:
            runtime.remove_connection(_bridge.port)
        commands.log("stopped")
    _bridge = None
    _connection_file = _connection_problem = None


def _remove_connection_file_at_exit():
    """Remove the connection file when Python exits.

    Blender does not unregister add-ons when it quits, so without this the file would outlive the bridge it
    describes.
    """
    if _bridge is not None and _bridge.port is not None:
        runtime.remove_connection(_bridge.port)


def _auto_start():
    """Start the bridge when the preference "Start with Blender" is on; run once, by a timer."""
    prefs = _prefs()
    if prefs is not None and prefs.auto_start and (_bridge is None or not _bridge.running):
        try:
            start_bridge()
        except OSError as exc:
            commands.log(f"auto-start failed: {exc}")
    return None


class BLENDERLENS_Preferences(bpy.types.AddonPreferences):
    """The add-on's preferences: where it listens, its token, and whether the agent may run Python."""

    bl_idname = _package()

    host: StringProperty(name="Host", default="127.0.0.1",
                         description="Address to listen on: localhost or a loopback IPv4 address such as "
                                     "127.0.0.1, so that only this machine can connect. Any other address, ::1 "
                                     "included, is refused")
    port: IntProperty(name="Port", default=DEFAULT_PORT, min=1024, max=65535,
                      description="Port the BlenderLens MCP server connects to (BLENDERLENS_PORT on its side)")
    token: StringProperty(name="Token", default="", subtype="PASSWORD",
                          description="Requests must carry this token. Start makes one when this is empty and writes "
                                      "it to the connection file the MCP server reads")
    allow_python: BoolProperty(name="Allow Python from the agent", default=True,
                               description="Let the agent run Python in this Blender (run_python and "
                                           "run_script). When this is off, those calls are refused and the "
                                           "other tools still work")
    auto_start: BoolProperty(name="Start with Blender", default=False,
                             description="Start listening for the BlenderLens MCP server whenever Blender starts "
                                         "with this add-on enabled")

    def draw(self, context):
        """Draw the preferences."""
        layout = self.layout
        row = layout.row()
        row.prop(self, "host")
        row.prop(self, "port")
        layout.prop(self, "token")
        layout.prop(self, "allow_python")
        layout.prop(self, "auto_start")


class BLENDERLENS_OT_start(bpy.types.Operator):
    """Start listening for the BlenderLens MCP server."""

    bl_idname = "blenderlens.start"
    bl_label = "Start BlenderLens"
    bl_description = "Listen for the BlenderLens MCP server on localhost"

    def execute(self, context):
        """Start the bridge, and report where it listens or why it could not."""
        try:
            server = start_bridge()
        except OSError as exc:
            self.report({"ERROR"}, f"BlenderLens could not listen: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"BlenderLens listening on {server.host}:{server.port}")
        return {"FINISHED"}


class BLENDERLENS_OT_stop(bpy.types.Operator):
    """Stop listening for the BlenderLens MCP server."""

    bl_idname = "blenderlens.stop"
    bl_label = "Stop BlenderLens"
    bl_description = "Stop listening and disconnect the MCP server"

    def execute(self, context):
        """Stop the bridge."""
        stop_bridge()
        self.report({"INFO"}, "BlenderLens stopped")
        return {"FINISHED"}


class BLENDERLENS_PT_panel(bpy.types.Panel):
    """The BlenderLens panel in the 3D viewport's sidebar."""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BlenderLens"
    bl_label = "BlenderLens"

    def draw(self, context):
        """Draw whether the add-on listens, what it has served, and the Start or Stop button."""
        layout = self.layout
        if _bridge is not None and _bridge.running:
            layout.label(text=f"Listening on {_bridge.host}:{_bridge.port}", icon="LINKED")
            layout.label(text="Token required", icon="LOCKED")
            if _connection_problem:
                layout.label(text=_connection_problem[:80], icon="ERROR")
            layout.label(text=f"Clients: {len(_bridge.clients)}   Requests: {_bridge.served}")
            if _bridge.last_command:
                layout.label(text=f"Last command: {_bridge.last_command}")
            if _bridge.last_error:
                layout.label(text=_bridge.last_error[:80], icon="ERROR")
            layout.operator("blenderlens.stop", icon="CANCEL")
        else:
            layout.label(text="Stopped", icon="UNLINKED")
            layout.operator("blenderlens.start", icon="PLAY")
        prefs = _prefs()
        if prefs is not None:
            layout.prop(prefs, "port")
            layout.prop(prefs, "allow_python")


#: The classes the add-on registers with Blender.
CLASSES = (BLENDERLENS_Preferences, BLENDERLENS_OT_start, BLENDERLENS_OT_stop, BLENDERLENS_PT_panel)


def register():
    """Register the add-on's classes, and start the bridge after a second if "Start with Blender" is on."""
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    atexit.register(_remove_connection_file_at_exit)
    if not bpy.app.background:
        bpy.app.timers.register(_auto_start, first_interval=1.0, persistent=True)


def unregister():
    """Stop the bridge and unregister the add-on's classes."""
    stop_bridge()
    atexit.unregister(_remove_connection_file_at_exit)
    if bpy.app.timers.is_registered(_auto_start):
        bpy.app.timers.unregister(_auto_start)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
