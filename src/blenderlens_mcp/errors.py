"""The kinds of failure a tool can report, and the exception that carries one.

Every failed tool call (a refusal) is one of the kinds in ``KINDS``. It reaches the client as a
tool result with ``isError`` set, holding ``error`` (what happened, in Blender's own words when
the error came from Blender), ``exit`` (a number that stays the same for each kind), ``kind``,
and ``hint`` (what to do about it). Extra fields, such as ``traceback``, are added when the
failure has them.
"""

#: Each kind of failure, mapped to its exit number and the hint used when the failure carries no hint of its own.
KINDS = {
    "blender_error": (1, "Blender raised an error while running the call. The error gives Blender's own words, and "
                         "the traceback field, when there is one, the end of the traceback."),
    "not_found": (2, "Something the call needs does not exist: an object, material or file it names, or the "
                     "geometry to work on. The error says which. get_scene lists the objects, and list_materials "
                     "the materials."),
    "bad_argument": (3, "A parameter has a value Blender cannot use. The tool's input schema lists what each "
                        "parameter accepts."),
    "python_error": (4, "The Python code (run_python's code, or run_script's file) raised an exception. The "
                        "traceback names the line."),
    "unsaved_changes": (5, "The open file has unsaved changes. Call save_file first, or pass discard: true."),
    "blender_missing": (6, "No Blender executable was found. Set BLENDER_BIN to the path of the blender "
                           "executable."),
    "not_connected": (7, "No BlenderLens add-on responded. In Blender, press Start in the sidebar (N) > "
                         "BlenderLens, or call open_session with mode background."),
    "connection_lost": (8, "The connection to Blender closed during the call: Blender exited or crashed, or the "
                           "add-on was stopped. A background Blender's unsaved work is gone; a Blender window that "
                           "is still open keeps its work. get_status says what is attached now."),
    "start_failed": (9, "The background Blender did not start, or its start was cancelled. The error says why, and "
                        "the log field, when there is one, holds the end of Blender's log."),
    "version_mismatch": (10, "The add-on in that Blender speaks another protocol version. Reinstall it with "
                             "blenderlens-mcp install-addon, then restart Blender."),
    "token_mismatch": (11, "The add-on refused the token. Press Start in its panel to write its connection file "
                           "again, or set BLENDERLENS_TOKEN to the token in its preferences."),
    "busy": (12, "Blender is still working on an earlier call, and nothing was sent. get_status says what it is "
                 "running. Call again later, or call close_session with discard: true. That stops a background "
                 "Blender, or disconnects from the Blender window and leaves it open."),
    "not_allowed": (13, "This server or this Blender does not allow it. Either run_python and run_script are not "
                        "allowed (BLENDERLENS_ALLOW_PYTHON on the server, or the add-on's preference \"Allow Python "
                        "from the agent\"), or the path is outside BLENDERLENS_ALLOWED_DIRS."),
    "deadline": (124, "Blender did not respond in time and may still be working. The next call waits for it "
                      "within its own time limit, or is refused as busy, so do not call again at once. "
                      "render_preview, bake_texture, run_python and run_script take a longer timeout as an "
                      "argument; every other tool waits BLENDERLENS_TIMEOUT seconds (120 by default), set in the "
                      "server's environment."),
    "internal_error": (70, "The fault is in BlenderLens itself. The server is still running, so please report it."),
}


class ToolFailure(Exception):
    """An error raised when a tool call fails, carrying its kind, message, hint and extra fields.

    A kind that is not in ``KINDS`` is reported as ``blender_error``.
    """

    def __init__(self, kind, message, hint=None, **extra):
        super().__init__(message)
        self.kind = kind if kind in KINDS else "blender_error"
        self.message = message
        self.hint = hint
        self.extra = extra

    def payload(self):
        """Return the failure as the dictionary a tool result carries.

        Without a hint of its own, the failure gets its kind's default hint. Extra fields that are
        None or an empty string are left out.
        """
        exit_code, default_hint = KINDS[self.kind]
        out = {"error": self.message, "exit": exit_code, "kind": self.kind, "hint": self.hint or default_hint}
        out.update({k: v for k, v in self.extra.items() if v not in (None, "")})
        return out
