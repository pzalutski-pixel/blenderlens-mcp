"""Keep one undo step per call that changed something, in live mode and in a background Blender.

Blender keeps the steps. Python can push one, undo one, and ask whether there is one to undo, but it
cannot read their names: the undo history is drawn by Blender's C code, and no property of Blender's
Python API reaches it. So BlenderLens keeps its own list of the steps it pushed since the file was loaded,
newest last, and that list is what undo_change names.

- In a background Blender there is no user, and an operator run from Python pushes no step unless asked
  to, so the list holds every step unless agent code pushes steps of its own (see below). Blender pushes
  no first step there when a file loads, so one is pushed before the first change; without it, that change
  could not be undone.
- In live mode, Blender pushes the first step itself, and the user's own edits are steps too.

Steps BlenderLens did not push are not on its list, so it watches for what makes them, and never names a
step it cannot identify:

- Between calls, any depsgraph update, undo, redo or new entry in the window manager's operator history
  counts as an edit made outside BlenderLens (in live mode, usually an edit by the user). The step
  BlenderLens pushed last is then not known to be the newest.
- During a call, agent code that pushes, undoes or redoes steps itself (ed.undo_push, ed.undo, an operator
  called with its undo argument) leaves steps beneath the call's own step that BlenderLens cannot name.

undo_change undoes by name only the steps that no such edit lies above. Past such an edit it refuses unless
given ``force``, which undoes the newest steps whatever made them and names only those it can identify.

The same watch tells whether run_python changed anything at all. A call during which the depsgraph saw no
update, nothing was loaded or undone, and the file's datablocks, custom properties, texts, frames and mesh
counts stayed as they were, changed nothing, so it pushes no step and leaves a saved file saved.
"""

import contextlib

import bpy
from bpy.app.handlers import persistent

from . import scripts
from .common import CommandError, boolean, joined, number, refuse_outside_object_mode, update

#: The start of the name of every undo step BlenderLens pushes.
PREFIX = "BlenderLens: "
#: The name of the first step, as Blender names it in live mode.
BASE = "Original"
#: The most steps one call undoes: Blender keeps at most 256.
MAX_STEPS = 256
#: The name undo_change gives a step it undid but cannot identify.
UNNAMED = "a step BlenderLens cannot name"
#: The most datablocks run_python's fingerprint covers; past this none is taken, and the call counts as a change.
MAX_FINGERPRINT_BLOCKS = 100_000

#: The steps BlenderLens pushed since the file was loaded, oldest first. The last is the state the file is in
#: when nothing was done since. Each is a dict with an "id", a "name", an "after" naming what made steps after
#: this one that BlenderLens did not push (None when nothing did, "edit" for an edit between calls, or "code"
#: for agent code during a call), and "own_steps", which says whether the call's code pushed, undid or redid
#: steps of its own beneath it.
_steps = []
#: The id the next pushed step gets. The id 0 stands for the state the file was loaded in, beneath every step.
_next_id = 1
#: Where in the steps the file was last saved or opened: the id of the newest step at that moment, 0 when there
#: was none, or None when that state is gone or uncertain. In a background Blender, an undo back to it leaves
#: the file counted as saved.
_saved_at = None
#: Whether the first step, which the other steps are undone back to, has been pushed. Only a background Blender
#: needs it.
_based = False
#: What the watch saw during each call still running, innermost last. A call can run inside another when agent
#: code dispatches one. The list is empty between calls.
_calls = []
#: The window manager's operator history, as _operator_history summarizes it, when the last call ended.
_operators = None


def forget():
    """Forget the steps and the saved position, because a file was loaded and its undo history holds none of them."""
    global _based, _saved_at
    _steps.clear()
    _based = False
    _saved_at = None


def _top():
    """Return the id of the newest step BlenderLens pushed, or 0 when there is none."""
    return _steps[-1]["id"] if _steps else 0


def mark_saved():
    """Record that the file was just saved or opened, so its current state is the one on disk."""
    global _saved_at
    _saved_at = _top()


def at_saved():
    """Return whether the file is back where it was last saved or opened.

    It is when the newest step is the one the file was saved or opened at, and nothing BlenderLens did not push
    came after that step.
    """
    return _saved_at is not None and _top() == _saved_at and (not _steps or _steps[-1]["after"] is None)


def note_outside(what="edit"):
    """Record that something made a step BlenderLens did not push, after the newest step it did push.

    ``what`` is "edit" for an edit between calls, or "code" for agent code during a call.
    """
    if _steps and _steps[-1]["after"] is None:
        _steps[-1]["after"] = what


@persistent
def _blenderlens_forget_undo_steps(*_args):
    """Forget the steps when a file loads, and note the load in every call running."""
    forget()
    for call in _calls:
        call["loaded"] = True


@persistent
def _blenderlens_watch_updates(*_args):
    """Count a depsgraph update: as an edit outside BlenderLens between calls, else in every call running."""
    if not _calls:
        note_outside()
    for call in _calls:
        call["updates"] += 1


@persistent
def _blenderlens_watch_undo(*_args):
    """Count an undo or redo: as an edit outside BlenderLens between calls, else in every call running."""
    if not _calls:
        note_outside()
    for call in _calls:
        call["undone"] = True


#: Each bpy.app.handlers list BlenderLens watches, with the handler it adds there.
_HANDLERS = (("load_post", _blenderlens_forget_undo_steps), ("depsgraph_update_post", _blenderlens_watch_updates),
             ("undo_post", _blenderlens_watch_undo), ("redo_post", _blenderlens_watch_undo))


def _watch():
    """Add the handlers, once.

    A handler left by an earlier load of this module, when the add-on was disabled and enabled again, is
    replaced rather than doubled. Handlers are matched by function name.
    """
    for kind, handler in _HANDLERS:
        handlers = getattr(bpy.app.handlers, kind)
        for old in [h for h in handlers if getattr(h, "__name__", "") == handler.__name__]:
            handlers.remove(old)
        handlers.append(handler)


_watch()


def _operator_history():
    """Return the length of the operator history, with its newest entry's pointer and idname.

    Blender lists the operators a user ran that support undo. An operator called from Python is not listed
    unless it is asked to push its own undo step.
    """
    wm = bpy.context.window_manager
    operators = getattr(wm, "operators", None) if wm is not None else None
    if not operators:
        return (0, None, None)
    newest = operators[-1]
    return (len(operators), newest.as_pointer(), newest.bl_idname)


def _fingerprint():
    """Return what run_python could change without the depsgraph seeing it.

    The fingerprint holds every datablock's name and custom properties, each text's content, each scene's
    frame, and each mesh's counts. Past MAX_FINGERPRINT_BLOCKS datablocks it is None.
    """
    out = []
    blocks = 0
    for kind in sorted(dir(bpy.data)):
        collection = getattr(bpy.data, kind, None)
        if not isinstance(collection, bpy.types.bpy_prop_collection):
            continue
        blocks += len(collection)
        if blocks > MAX_FINGERPRINT_BLOCKS:
            return None
        for block in collection:
            keys = block.keys() if hasattr(block, "keys") else ()
            out.append((kind, block.name, scripts.properties_signature(block) if keys else None))
    out += [hash(text.as_string()) for text in bpy.data.texts]
    out += [(scene.frame_current, scene.frame_subframe) for scene in bpy.data.scenes]
    out += [(len(m.vertices), len(m.edges), len(m.loops), len(m.polygons)) for m in bpy.data.meshes]
    return out


def begin(command, params):
    """Start watching a call.

    Whatever happened since the last call is an edit made outside BlenderLens. From here on, the watch counts
    what happens as this call's own.
    """
    if not _calls:
        # An update still waiting for the depsgraph comes from something done before the call, outside it, so
        # it is processed before the call is watched.
        with contextlib.suppress(Exception):
            update()
        if _operators is not None and _operator_history() != _operators:
            note_outside()
    call = {"command": command, "updates": 0, "loaded": False, "undone": False, "operators": _operator_history(),
            "dirty": bool(bpy.data.is_dirty), "code": False, "fingerprint": None}
    if command in ("run_python", "run_script"):
        call["code"] = scripts.handles_undo(command, params)
    if command == "run_python":
        with contextlib.suppress(Exception):
            call["fingerprint"] = _fingerprint()
    _calls.append(call)


def settle():
    """Finish the call's work, and return whether run_python changed nothing at all.

    The depsgraph is brought up to date while the call still owns its updates. The result is True only for a
    run_python call during which the watch saw no change of any kind; for every other call it is False.
    """
    with contextlib.suppress(Exception):
        update()
    call = _calls[-1] if _calls else None
    if call is None or call["command"] != "run_python" or call["fingerprint"] is None:
        return False
    try:
        return (call["updates"] == 0 and not call["loaded"] and not call["undone"] and not call["code"]
                and _operator_history() == call["operators"] and bool(bpy.data.is_dirty) == call["dirty"]
                and _fingerprint() == call["fingerprint"])
    except Exception:  # noqa: BLE001 -- when unsure, count the call as a change
        return False


def end():
    """Stop watching the call; whatever happens until the next call is outside BlenderLens."""
    global _operators
    if _calls:
        _calls.pop()
    if not _calls:
        _operators = _operator_history()


@contextlib.contextmanager
def _in_window():
    """Give the undo operators the window they need in live mode; in a background Blender, change nothing."""
    wm = bpy.context.window_manager
    window = wm.windows[0] if wm is not None and wm.windows else None
    if window is None:
        yield
    else:
        with bpy.context.temp_override(window=window):
            yield


def _push(message):
    """Push an undo step named ``message``, and return whether Blender pushed it."""
    with _in_window():
        return "FINISHED" in bpy.ops.ed.undo_push(message=message)


def ensure_base():
    """Push the step the file starts from (background Blender only), once after a load and before the first change."""
    global _based
    if bpy.app.background and not _based:
        _based = _push(BASE)


def record(command, refused=False, acted_on=None):
    """Push the undo step for a call that may have changed the file, and remember the step's name.

    The step is named after the command and the object it acted on. In a background Blender, a call during
    which a file was loaded (run_python opening one) pushes the step the loaded file starts from instead: the
    steps before it belong to the other file, and the call's name would label a step that undoes none of the
    call's work. In live mode Blender pushed that step itself as the file loaded, so the call's own step goes
    on top as usual.
    """
    global _based, _next_id, _saved_at
    call = _calls[-1] if _calls else {}
    if bpy.app.background and call.get("loaded") and not _based:
        _based = _push(BASE)
        return
    own_steps = bool(call.get("undone") or call.get("code")
                     or (call.get("operators") is not None and _operator_history() != call["operators"]))
    name = PREFIX + command + (f" {acted_on}" if acted_on else "") + (" (refused)" if refused else "")
    if not _push(name):
        return
    if own_steps and _steps and _steps[-1]["after"] is None:
        _steps[-1]["after"] = "code"
    _steps.append({"id": _next_id, "name": name, "after": None, "own_steps": own_steps})
    _next_id += 1
    # Blender keeps as many steps as the undo_steps preference says, besides the first, and drops the oldest
    # past that.
    excess = len(_steps) - max(bpy.context.preferences.edit.undo_steps, 0)
    if excess > 0:
        if _saved_at is not None and _saved_at <= _steps[excess - 1]["id"]:
            _saved_at = None  # the saved state was among the steps Blender dropped
        del _steps[:excess]


def _can_undo():
    """Return whether Blender has a step to undo."""
    with _in_window():
        return bpy.ops.ed.undo.poll()


def _counts():
    """Return the scene's object count and the file's mesh, material, vertex and face counts."""
    meshes = bpy.data.meshes
    return {"objects": len(bpy.context.scene.objects), "meshes": len(meshes), "materials": len(bpy.data.materials),
            "vertices": sum(len(m.vertices) for m in meshes), "faces": sum(len(m.polygons) for m in meshes)}


def _by_name():
    """Return how many of the newest steps undo_change can undo by name.

    These are the newest steps, up to the first one that has a step BlenderLens did not push above it.
    """
    count = 0
    for step in reversed(_steps):
        if step["after"] is not None:
            break
        count += 1
    return count


def _refuse_past(steps, named, live):
    """Refuse to undo ``steps`` steps when only the newest ``named`` can be undone by name; nothing is undone.

    ``live`` is true in live mode, where the user can also press Ctrl+Z in Blender.
    """
    if named == len(_steps):
        listed = ", ".join(s["name"] for s in reversed(_steps)) or "none"
        further = " Ctrl+Z in Blender steps further, or pass force: true." if live else ""
        raise CommandError("bad_argument",
                           f"Only {len(_steps)} step{'' if len(_steps) == 1 else 's'} can be undone since the file "
                           f"was loaded, not {steps}.", f"The steps, newest first: {listed}.{further}")
    step = _steps[len(_steps) - named - 1]
    if step["after"] == "edit":
        how = "the file was changed outside BlenderLens's calls"
        if live:
            how += " (by hand in Blender, for example)"
    else:
        how = "agent code pushed or undid undo steps itself"
    if not named:
        count = "no step can be undone by name"
    elif named == 1:
        count = "only the newest step can be undone by name"
    else:
        count = f"only the newest {named} steps can be undone by name"
    if live:
        hint = "Use Ctrl+Z in Blender, or pass force: true to undo the newest steps, whatever made them."
    else:
        hint = "Pass force: true to undo the newest steps, whatever made them."
    raise CommandError("bad_argument", f"After '{step['name']}', {how}, so {count}.", hint)


def undo_change(params):
    """Undo the newest steps BlenderLens pushed, or list them, and return what was undone and what is left."""
    global _saved_at
    listing = boolean(params, "list", False)
    force = boolean(params, "force", False)
    steps = number(params, "steps", 1, minimum=1, maximum=MAX_STEPS, whole=True)
    live = not bpy.app.background
    named = _by_name()
    note = ("In live mode, the user's own edits are undo steps too. BlenderLens names only its own steps, and "
            "does not undo past an edit made outside its calls unless given force: true." if live else None)
    if listing:
        # In a background Blender the list holds every step, unless agent code pushed steps of its own; in live
        # mode Blender also holds the user's own steps.
        out = {"steps": [s["name"] for s in reversed(_steps)], "can_undo": _can_undo() if live else bool(_steps),
               "by_name": named}
        return {**out, "note": note} if note else out

    # In edit mode, Blender's undo steps through the mesh edit's own history instead of the calls' steps.
    refuse_outside_object_mode([o for o in bpy.context.scene.objects if o.mode != "OBJECT"],
                               "an undo would step back through its edit-mode history instead of through the calls' "
                               "steps")
    if steps > named and not force:
        _refuse_past(steps, named, live)
    if not _can_undo():
        raise CommandError("bad_argument", "There is nothing to undo.",
                           "Every step since the file was loaded is undone already.")
    undone, partial = [], []
    while len(undone) < steps and _can_undo():
        with _in_window():
            result = bpy.ops.ed.undo()
        if "FINISHED" not in result:
            break
        if _steps and _steps[-1]["after"] is None:
            step = _steps.pop()
            undone.append(step["name"])
            if step["own_steps"]:
                partial.append(step["name"])
        else:
            # Either a step something else made, or the step BlenderLens pushed beneath it; Blender gives no
            # way to tell which.
            undone.append(UNNAMED)
    update()
    if UNNAMED in undone or partial:
        _saved_at = None  # steps it cannot name may lie between the save and this state
    out = {"undone": undone, "steps": [s["name"] for s in reversed(_steps)], "counts": _counts(),
           # The data is as it was before the steps undone. In a background Blender the file counts as saved
           # again only at the step where it was saved or opened. In live mode it always counts as unsaved after
           # an undo.
           "unsaved_changes": live or not at_saved()}
    cleared = scripts.drop_stale()
    if cleared:
        out["python_names_cleared"] = cleared
        out["python_note"] = ("These run_python names held Blender data that the undo replaced. Read them again "
                              "from bpy.data.")
    notes = [note] if note else []
    if UNNAMED in undone:
        notes.append(f"'{UNNAMED}' is a step made outside BlenderLens's calls, or by agent code, or a step "
                     "BlenderLens pushed beneath one of those. Blender gives no way to tell which.")
    if partial:
        if len(partial) == 1:
            notes.append(f"'{partial[0]}' pushed undo steps of its own, so undoing it took back only what it did "
                         "after the last of them.")
        else:
            notes.append(f"{joined(repr(name) for name in partial)} pushed undo steps of their own, so undoing each "
                         "took back only what it did after the last of them.")
    if notes:
        out["note"] = " ".join(notes)
    return out
