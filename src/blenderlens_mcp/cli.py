"""The ``blenderlens-mcp`` command line.

Usage::

    blenderlens-mcp                                 serve MCP over stdio (what an MCP client runs)
    blenderlens-mcp build-addon [-o OUT]            write the Blender add-on as an installable .zip
    blenderlens-mcp install-addon [--blender PATH]  build the add-on, then install and enable it in Blender
    blenderlens-mcp check FILE [--rules R] [--budget N] [--target T] [--json]
                                                    check a .glb (without Blender) or a .blend (in a
                                                    background Blender); exit 0 if it passed, 1 if it
                                                    failed, 2 if it could not be checked
    blenderlens-mcp --version
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from blenderlens_mcp import __version__

ADDON_DIR = Path(__file__).with_name("addon")

#: The check command's exit code when the asset passed.
PASSED = 0
#: The check command's exit code when the asset failed.
FAILED = 1
#: The check command's exit code when the asset could not be checked: a bad option or file, no Blender, a
#: Blender that does not start or stops responding, or a refused check.
NOT_CHECKED = 2


class CannotCheck(Exception):
    """An error raised when a check cannot run as requested; the command exits with 2, the reason on stderr."""


def load_rules_module():
    """Load addon/rules.py from its file and return the module.

    Importing it through the add-on package would import bpy, which exists only inside Blender.
    """
    spec = importlib.util.spec_from_file_location("blenderlens_rules", ADDON_DIR / "rules.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_rules(path: str | None):
    """Return the rule set in a JSON file, once it validates, or None without a file.

    Raise CannotCheck when the file cannot be read, is not JSON, or is not a valid rule set.
    """
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            given = json.load(handle)
    except OSError as exc:
        raise CannotCheck(f"Cannot read the rules file {path}: {exc.strerror or exc}.") from None
    except json.JSONDecodeError as exc:
        raise CannotCheck(f"The rules file {path} is not JSON: {exc.msg}, line {exc.lineno}.") from None
    try:
        load_rules_module().validate(given)
    except ValueError as exc:
        raise CannotCheck(f"The rules file {path} is not a valid rule set: {exc}") from None
    return given


def _glb_report(path: str, rules, budget: int | None, target: str) -> dict:
    """Check a .glb from the file alone, and return the report the command prints.

    The issues are those the rules and the budget find. The warnings that reading the file back gives for
    the target are in the report's contents. A file that is not a readable .glb fails with a ``not_a_glb``
    issue.
    """
    from blenderlens_mcp.glb import GLBError, describe, summarize

    rules_module = load_rules_module()
    try:
        contents = summarize(path, target=target)
        description = describe(path)
    except (OSError, GLBError) as exc:
        return {"file": path, "kind": "glb", "target": target, "passed": False,
                "issues": [{"code": "not_a_glb", "severity": "error", "count": 1,
                            "message": f"The file is not a readable binary glTF: {exc}"}], "contents": None}
    issues = []
    if rules is not None:
        issues += rules_module.evaluate(rules_module.validate(rules), description)
    if budget is not None:
        issues += rules_module.evaluate(rules_module.validate({"budget": budget}), description)
    return {"file": path, "kind": "glb", "target": target, "issues": issues, "contents": contents}


def _blend_report(path: str, rules, budget: int | None, target: str | None) -> dict:
    """Check a .blend in a background Blender with check_asset, and return the report the command prints.

    With a target, the file is also exported to a temporary .glb and read back: the read-back is the
    report's contents, with its warnings for the target, and a refused export is added as an issue. The
    background Blender is stopped afterwards. Raise CannotCheck when Blender cannot be found, does not
    start, refuses check_asset, or stops responding.
    """
    from blenderlens_mcp.errors import ToolFailure
    from blenderlens_mcp.glb import GLBError, summarize
    from blenderlens_mcp.session import find_blender_binary, start_background

    binary = find_blender_binary()
    if binary is None:
        raise CannotCheck("No Blender executable was found, and checking a .blend needs one. Set BLENDER_BIN.")
    timeout = float(os.environ.get("BLENDERLENS_TIMEOUT", 120) or 120)
    start = float(os.environ.get("BLENDERLENS_START_TIMEOUT", 90) or 90)
    try:
        session = start_background(binary, path, start)
    except ToolFailure as failure:
        raise CannotCheck(f"Blender did not start: {failure.message}") from None
    folder = tempfile.mkdtemp(prefix="check-glb-")
    try:
        params = {}
        if rules is not None:
            params["rules"] = rules
        if budget is not None:
            params["budget"] = budget
        reply = session.connection.call("check_asset", params, timeout)
        if not reply.get("ok"):
            raise CannotCheck(f"check_asset was refused: {reply.get('error')} {reply.get('hint') or ''}".strip())
        out = {"file": path, "kind": "blend", "target": target, "check": reply["result"],
               "issues": [i for row in reply["result"]["objects"] for i in _named(row)] + reply["result"]["issues"],
               "added": []}
        if target is not None:
            glb = os.path.join(folder, "check.glb")
            exported = session.connection.call("export_glb", {"path": glb, "target": target}, timeout)
            if not exported.get("ok"):
                refused = {"code": "export_refused", "severity": "error", "count": 1,
                           "message": f"export_glb refused: {exported.get('error')}"}
                out["issues"].append(refused)
                out["added"].append(refused)
                out["contents"] = None
            else:
                result = exported["result"]
                try:
                    out["contents"] = summarize(glb, expected=result.get("objects"), target=target,
                                                morphs=result.get("morph_targets"))
                except (OSError, GLBError) as exc:
                    out["contents"] = {"valid": False, "problem": str(exc)}
        return out
    except ToolFailure as failure:
        raise CannotCheck(f"Blender stopped responding: {failure.message}") from None
    finally:
        session.stop()
        shutil.rmtree(folder, ignore_errors=True)


def _named(row: dict) -> list:
    """Return an object's issues, each carrying the object's name, for the flat list the command prints."""
    return [{**issue, "object": row["name"]} for issue in row.get("issues", [])]


def check(path: str, rules_path: str | None, budget: int | None, target: str | None, as_json: bool) -> int:
    """Run the check command: check the asset, print the report, and return the exit code.

    Raise CannotCheck when the check cannot run as requested.
    """
    if not os.path.isfile(path):
        raise CannotCheck(f"No file exists at {path}.")
    kind = os.path.splitext(path)[1].lower()
    if kind not in (".glb", ".blend"):
        raise CannotCheck(f"Cannot check {kind or 'a file with no extension'}: give a .glb or a .blend.")
    if budget is not None and budget < 0:
        raise CannotCheck("--budget is a number of triangles, 0 or more.")
    rules = _read_rules(rules_path)
    if kind == ".glb":
        report = _glb_report(path, rules, budget, target or "generic")
    else:
        report = _blend_report(path, rules, budget, target)
    contents = report.get("contents") or {}
    every = report["issues"] + contents.get("warnings", [])
    # A .blend's verdict and counts start from check_asset's own, which cover every object it checked, whether
    # listed or not. What the command adds itself (a refused export, and the warnings from reading the export
    # back) is counted on top.
    checked = report.get("check")
    counted = contents.get("warnings", []) + report.pop("added", []) if checked else every
    errors = sum(1 for i in counted if i.get("severity") == "error")
    warnings = sum(1 for i in counted if i.get("severity") == "warning")
    if checked:
        errors += checked["counts"]["errors"]
        warnings += checked["counts"]["warnings"]
    report["passed"] = not errors and contents.get("valid", True) is not False
    report["counts"] = {"errors": errors, "warnings": warnings}
    # Names in the file may hold any character, and printing them to a console or log on a narrow code page
    # must not end the check.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if as_json:
        # NaN and the infinities are written as the strings the server uses for them, since strict JSON has no
        # form for them; a file with absurd transforms can make its bounds overflow.
        from blenderlens_mcp.server import jsonable

        print(json.dumps(jsonable(report), indent=1, allow_nan=False))
    else:
        verdict = "PASS" if report["passed"] else "FAIL"
        errors, warnings = report["counts"]["errors"], report["counts"]["warnings"]
        print(f"check {path}: {verdict} ({_counted(errors, 'error')}, {_counted(warnings, 'warning')})")
        for item in every:
            where = f" [{item['object']}]" if item.get("object") else ""
            faces = f" faces {item['faces']}" if item.get("faces") else ""
            message = f": {item['message']}" if item.get("message") else ""
            print(f"  {item.get('severity', 'warning'):7} {item['code']}{where}{faces}{message}")
    return PASSED if report["passed"] else FAILED


def build_addon(out: str | None = None) -> Path:
    """Zip the add-on folder with its manifest at the root, as Blender's extension installer requires.

    Return the path of the .zip, which defaults to ``blenderlens-<version>.zip`` in the current folder.
    """
    target = Path(out) if out else Path.cwd() / f"blenderlens-{__version__}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ADDON_DIR.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            archive.write(path, path.relative_to(ADDON_DIR).as_posix())
    return target


def _counted(number: int, noun: str) -> str:
    """Return a count with its noun, in the plural unless the count is 1, such as "1 error" or "3 warnings"."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def install_addon(blender: str | None) -> int:
    """Install the add-on into Blender's user extensions and enable it, and return the exit code."""
    from blenderlens_mcp.session import find_blender_binary

    binary = blender or find_blender_binary()
    if not binary:
        print("No Blender executable was found. Pass --blender or set BLENDER_BIN.", file=sys.stderr)
        return 6
    # Blender copies the zip into its extensions folder, so the zip itself is only needed during the install.
    with tempfile.TemporaryDirectory(prefix="blenderlens-addon-") as folder:
        archive = build_addon(os.path.join(folder, f"blenderlens-{__version__}.zip"))
        command = [binary, "--command", "extension", "install-file", "--repo", "user_default", "--enable",
                   str(archive)]
        print("Running: " + " ".join(command))
        result = subprocess.run(command)
    if result.returncode != 0:
        print(f"Blender's installer exited with {result.returncode}.", file=sys.stderr)
        return result.returncode
    print("\nInstalled and enabled BlenderLens. In Blender: 3D viewport > sidebar (N) > BlenderLens > Start.")
    print("Restart Blender first if it was open while installing.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the command named on the command line, or serve MCP over stdio when none is; return the exit code."""
    parser = argparse.ArgumentParser(prog="blenderlens-mcp",
                                     description="MCP server for modelling in Blender. With no command, serves "
                                                 "MCP over stdio.")
    parser.add_argument("--version", action="version", version=f"blenderlens-mcp {__version__}")
    commands = parser.add_subparsers(dest="command")
    build = commands.add_parser("build-addon", help="write the Blender add-on as an installable .zip")
    build.add_argument("-o", "--out", help="where to write the .zip (default ./blenderlens-<version>.zip)")
    install = commands.add_parser("install-addon", help="install and enable the add-on in Blender")
    install.add_argument("--blender", help="Blender executable (default BLENDER_BIN, PATH, or the usual places)")
    checker = commands.add_parser("check", help="check a .glb or .blend file, and exit with 0 if it passed, 1 if "
                                                 "it failed, or 2 if it could not be checked")
    checker.add_argument("file", help="the .glb (read alone) or .blend (opened in a background Blender)")
    checker.add_argument("--rules", help="a JSON file of project rules, in the format the README describes")
    checker.add_argument("--budget", type=int, help="the most triangles the asset may have")
    checker.add_argument("--target", choices=["godot", "threejs", "generic"],
                         help="the engine to warn for (a .blend is then also exported and read back)")
    checker.add_argument("--json", action="store_true", help="print the whole result as JSON")
    args = parser.parse_args(argv)

    if args.command == "build-addon":
        print(build_addon(args.out))
        return 0
    if args.command == "install-addon":
        return install_addon(args.blender)
    if args.command == "check":
        try:
            return check(os.path.abspath(args.file), args.rules, args.budget, args.target, args.json)
        except CannotCheck as exc:
            print(f"blenderlens-mcp check: {exc}", file=sys.stderr)
            return NOT_CHECKED

    from blenderlens_mcp.server import serve

    return serve()


if __name__ == "__main__":
    sys.exit(main())
