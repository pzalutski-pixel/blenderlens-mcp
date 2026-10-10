"""Checks and the npm bundle step shared by the CI and release workflows, kept in one place so they always agree.

Run from the repository root:

    release_kit.py versions [--tag v0.2.0]                   check that all versions and the tag agree, then print it
    release_kit.py changelog 0.2.0                           check that CHANGELOG.md has "## [0.2.0] - YYYY-MM-DD"
    release_kit.py notes 0.2.0                               print that entry's text, for the GitHub release
    release_kit.py tool-count                                print how many tools tools.py defines
    release_kit.py npm-bundle                                copy the server, LICENSE and NOTICE into npm/
    release_kit.py npm-tarball FILE --version V              check the npm tarball
    release_kit.py dist DIR --version V                      check the wheel and the sdist in DIR
    release_kit.py addon-zip FILE --version V                check the add-on zip
    release_kit.py mcp-responses FILE --tools N --version V  check recorded responses to initialize and tools/list
    release_kit.py pypi-token                                check that $PYPI_API_TOKEN is an API token for pypi.org

Every check reads the built artifact itself, never a manifest that describes it: a package.json can list
LICENSE and NOTICE while npm pack cannot reach them, and a check of the manifest alone would pass. Archives are
read with tarfile and zipfile rather than `tar | grep -q`, because grep's early exit gives tar a SIGPIPE that
`set -o pipefail` reports as a missing file.

A failure prints a GitHub `::error::` annotation to stdout and exits with 1. The script uses the standard
library only, because the workflows run it before anything is installed. It runs on Python 3.10, the oldest
version the server supports, because the unit tests run it on every Python version that CI tests.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

PACKAGE = "blenderlens_mcp"
REGISTRY_NAME = "io.github.pzalutski-pixel/blenderlens"

#: The files an sdist may hold, relative to its top folder. pyproject.toml's only-include names the same paths,
#: and PKG-INFO is the metadata file that the build adds.
SDIST_FILES = frozenset({"PKG-INFO", ".gitignore", "CHANGELOG.md", "LICENSE", "NOTICE", "README.md",
                         "pyproject.toml", "server.json", "examples/build_lantern.py", "examples/rules.json"})
#: The folders whose contents an sdist may hold, relative to its top folder.
SDIST_FOLDERS = (".github/", "docs/", "npm/", "src/", "tests/")


class Failed(Exception):
    """An error raised when a check fails; its message becomes the text of the ``::error::`` annotation."""


def _read(path: Path) -> str:
    """Return a UTF-8 file's text, or raise Failed naming the file when it does not exist."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Failed(f"{path.as_posix()} does not exist") from None


# ---------------------------------------------------------------------------
# Versions and the changelog
# ---------------------------------------------------------------------------


def versions(root: Path) -> dict[str, str]:
    """Return every version written in the repository, keyed by the place where it is written."""
    found = {}
    init = re.search(r'^__version__ = "([^"]+)"$', _read(root / "src" / PACKAGE / "__init__.py"), re.MULTILINE)
    found[f"src/{PACKAGE}/__init__.py"] = init.group(1) if init else "(none)"
    manifest = _read(root / "src" / PACKAGE / "addon" / "blender_manifest.toml").split("\n[", 1)[0]
    addon = re.search(r'^version = "([^"]+)"$', manifest, re.MULTILINE)
    found[f"src/{PACKAGE}/addon/blender_manifest.toml"] = addon.group(1) if addon else "(none)"
    found["npm/package.json"] = json.loads(_read(root / "npm" / "package.json")).get("version", "(none)")
    server = json.loads(_read(root / "server.json"))
    found["server.json"] = server.get("version", "(none)")
    for package in server.get("packages", []):
        found[f"server.json packages[{package.get('registryType')}]"] = package.get("version", "(none)")
    return found


def check_versions(root: Path, tag: str | None) -> str:
    """Return the one version that every manifest, and the tag when one is given, carries.

    Raise Failed, listing every place and its version, when they disagree.
    """
    found = versions(root)
    if tag is not None:
        found["the tag"] = tag[1:] if tag.startswith("v") else tag
    if len(set(found.values())) != 1:
        raise Failed("The versions disagree: " + "; ".join(f"{where} has {v}" for where, v in found.items()))
    return next(iter(found.values()))


def changelog_entry(root: Path, version: str) -> str:
    """Return the text under the "## [version] - YYYY-MM-DD" heading, which a release requires.

    Raise Failed when CHANGELOG.md has no such heading.
    """
    text = _read(root / "CHANGELOG.md")
    heading = re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}[ \t]*$", text, re.MULTILINE)
    if not heading:
        present = re.findall(r"^## \[.*$", text, re.MULTILINE)
        raise Failed(f"CHANGELOG.md has no '## [{version}] - YYYY-MM-DD' heading; it has {present}. "
                     "'Unreleased' means the date is not set yet.")
    rest = text[heading.end():]
    following = re.search(r"^## \[", rest, re.MULTILINE)
    return (rest[: following.start()] if following else rest).strip()


def tool_count(root: Path) -> int:
    """Return the number of tools that this checkout's tools.py defines."""
    sys.path.insert(0, str(root / "src"))
    from blenderlens_mcp.tools import TOOLS

    return len(TOOLS)


# ---------------------------------------------------------------------------
# The npm bundle
# ---------------------------------------------------------------------------


def build_npm_bundle(npm_dir: Path, root: Path) -> None:
    """Put the server where bin/blenderlens-mcp.js runs it, and the licence files beside package.json.

    npm pack runs inside npm/ and cannot reach the repository's LICENSE and NOTICE, which Apache 2.0
    sections 4(a) and 4(d) require to travel with the code, so they are copied in. Compiled bytecode is not
    copied, because a local build would otherwise ship it.
    """
    server = npm_dir / "server"
    if server.exists():
        shutil.rmtree(server)
    shutil.copytree(root / "src" / PACKAGE, server / PACKAGE,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(root / name, npm_dir / name)


def _no_bytecode(names: list[str], what: str) -> None:
    """Raise Failed when any of an archive's names is compiled Python bytecode."""
    compiled = [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]
    if compiled:
        raise Failed(f"{what} carries compiled bytecode: {compiled[:5]}")


def check_npm_tarball(path: Path, version: str) -> list[str]:
    """Check the npm tarball's contents, version and registry name, and return its entry names.

    The tarball must hold the launcher, the server, the add-on and the licence files, and no bytecode.
    """
    with tarfile.open(path, "r:gz") as tar:
        names = tar.getnames()
        package = json.loads(tar.extractfile("package/package.json").read().decode("utf-8"))
    required = ["package/package.json", "package/LICENSE", "package/NOTICE", "package/README.md",
                "package/bin/blenderlens-mcp.js", f"package/server/{PACKAGE}/__main__.py",
                f"package/server/{PACKAGE}/background.py", f"package/server/{PACKAGE}/addon/blender_manifest.toml",
                f"package/server/{PACKAGE}/addon/commands.py"]
    missing = [n for n in required if n not in names]
    if missing:
        raise Failed(f"{path.name} is missing {missing}")
    _no_bytecode(names, path.name)
    if package.get("version") != version or package.get("mcpName") != REGISTRY_NAME:
        raise Failed(f"{path.name} holds package.json version {package.get('version')} and mcpName "
                     f"{package.get('mcpName')}; expected {version} and {REGISTRY_NAME}")
    return names


# ---------------------------------------------------------------------------
# The Python distributions and the add-on zip
# ---------------------------------------------------------------------------


def check_dist(folder: Path, version: str) -> list[Path]:
    """Check the wheel and the sdist in ``folder``, and return their paths.

    The folder must hold exactly one wheel and one sdist of this version. The wheel must hold the server, the
    add-on and the licence files, carry no bytecode, and name the registry entry in its description. The sdist
    must hold the licence files, README.md, pyproject.toml, the add-on's manifest and background.py, carry no
    bytecode, and hold nothing outside SDIST_FILES and SDIST_FOLDERS.
    """
    wheels = sorted(folder.glob("*.whl"))
    sdists = sorted(folder.glob("*.tar.gz"))
    wheel_name = f"{PACKAGE}-{version}-py3-none-any.whl"
    sdist_name = f"{PACKAGE}-{version}.tar.gz"
    if [w.name for w in wheels] != [wheel_name] or [s.name for s in sdists] != [sdist_name]:
        raise Failed(f"{folder} holds {[p.name for p in wheels + sdists]}; expected {wheel_name} and {sdist_name}")
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        metadata = wheel.read(f"{PACKAGE}-{version}.dist-info/METADATA").decode("utf-8")
    required = [f"{PACKAGE}/__main__.py", f"{PACKAGE}/background.py", f"{PACKAGE}/addon/blender_manifest.toml",
                f"{PACKAGE}/addon/commands.py", f"{PACKAGE}-{version}.dist-info/licenses/LICENSE",
                f"{PACKAGE}-{version}.dist-info/licenses/NOTICE"]
    missing = [n for n in required if n not in names]
    if missing:
        raise Failed(f"{wheel_name} is missing {missing}")
    _no_bytecode(names, wheel_name)
    # The MCP Registry confirms that the PyPI package is this server by finding this line in its description.
    if f"mcp-name: {REGISTRY_NAME}" not in metadata:
        raise Failed(f"{wheel_name}'s description lacks 'mcp-name: {REGISTRY_NAME}'")
    with tarfile.open(sdists[0], "r:gz") as tar:
        members = tar.getmembers()
    names = [m.name for m in members]
    top = f"{PACKAGE}-{version}"
    required = [f"{top}/LICENSE", f"{top}/NOTICE", f"{top}/README.md", f"{top}/pyproject.toml",
                f"{top}/src/{PACKAGE}/addon/blender_manifest.toml", f"{top}/src/{PACKAGE}/background.py"]
    missing = [n for n in required if n not in names]
    if missing:
        raise Failed(f"{sdist_name} is missing {missing}")
    files = [m.name for m in members if not m.isdir()]
    outside = [n for n in files if not (n.startswith(f"{top}/") and _belongs_in_sdist(n[len(top) + 1:]))]
    if outside:
        raise Failed(f"{sdist_name} holds files outside the paths an sdist may hold: {outside[:5]}")
    _no_bytecode(files, sdist_name)
    return wheels + sdists


def _belongs_in_sdist(relative: str) -> bool:
    """Return whether a path, relative to the sdist's top folder, is one that the sdist may hold."""
    return relative in SDIST_FILES or relative.startswith(SDIST_FOLDERS)


def check_addon_zip(path: Path, version: str) -> list[str]:
    """Check the add-on zip's layout, bytecode and version, and return its file names.

    Blender's extension installer needs blender_manifest.toml at the zip's root, not inside a folder.
    """
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "blender_manifest.toml" not in names or "__init__.py" not in names:
            raise Failed(f"{path.name} has no blender_manifest.toml and __init__.py at its root: {names[:8]}")
        # A zip built from a Windows checkout carries the checkout's CRLF line endings, and Blender reads both.
        manifest = archive.read("blender_manifest.toml").decode("utf-8").replace("\r\n", "\n").split("\n[", 1)[0]
    _no_bytecode(names, path.name)
    found = re.search(r'^version = "([^"]+)"$', manifest, re.MULTILINE)
    if not found or found.group(1) != version:
        raise Failed(f"{path.name}'s manifest says version {found.group(1) if found else '(none)'}; expected {version}")
    return names


# ---------------------------------------------------------------------------
# A real MCP response
# ---------------------------------------------------------------------------


def check_mcp_responses(text: str, tools: int, version: str) -> str:
    """Check the responses to initialize (id 1) and tools/list (id 2), as the server wrote them to stdout.

    Return a one-line summary, and raise Failed when the server info, the tool count or a tool's schema is wrong.
    """
    responses = {}
    for line in text.splitlines():
        if line.strip():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                raise Failed(f"stdout carried a line that is not JSON: {line[:200]!r}") from None
            responses[message.get("id")] = message
    info = responses.get(1, {}).get("result", {}).get("serverInfo")
    if info != {"name": "blenderlens-mcp", "version": version}:
        raise Failed(f"initialize returned serverInfo {info}, but blenderlens-mcp {version} was expected")
    listed = responses.get(2, {}).get("result", {}).get("tools")
    if not isinstance(listed, list):
        raise Failed(f"tools/list did not return a list of tools: {responses.get(2)}")
    names = [tool.get("name") for tool in listed]
    if len(listed) != tools or len(set(names)) != len(names):
        raise Failed(f"tools/list returned {len(listed)} tools ({len(set(names))} distinct), but tools.py defines "
                     f"{tools}")
    if not all(isinstance(tool.get("inputSchema"), dict) for tool in listed):
        raise Failed("a listed tool has no inputSchema")
    return f"blenderlens-mcp {version} responded to initialize and listed {tools} tools"


def check_pypi_token(token: str | None) -> str:
    """Check that ``token`` is a PyPI API token for pypi.org, without printing or sending it.

    A PyPI API token is "pypi-" followed by a base64url-encoded macaroon in version 2 of the binary format,
    whose first field is its location: "pypi.org", or "test.pypi.org" for TestPyPI. Only an upload proves that
    PyPI accepts the token; this check catches a missing secret, a value pasted from elsewhere and a TestPyPI
    token before anything is published.
    """
    if not token:
        raise Failed("PYPI_API_TOKEN is not set. Add it under the repository's Settings > Secrets and variables > "
                     "Actions.")
    if not token.startswith("pypi-"):
        raise Failed("PYPI_API_TOKEN is not a PyPI API token: a PyPI API token starts with pypi-.")
    body = token[len("pypi-"):]
    try:
        macaroon = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, binascii.Error):
        raise Failed("PYPI_API_TOKEN is not a PyPI API token: the part after pypi- is not base64.") from None
    # Version 2, then field type 1 (the location), then the location's length and bytes.
    if len(macaroon) < 3 or macaroon[0] != 2 or macaroon[1] != 1:
        raise Failed("PYPI_API_TOKEN is not a PyPI API token: it does not hold a macaroon with a location.")
    location = macaroon[3:3 + macaroon[2]].decode("utf-8", "replace")
    if location != "pypi.org":
        raise Failed(f"PYPI_API_TOKEN is a token for {location}, not for pypi.org.")
    return "PYPI_API_TOKEN is an API token for pypi.org."


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand, and return 0 when it succeeds or 1 when a check fails."""
    # Bash captures the output with $(...), and a Windows "\r\n" line ending would leave "\r" on the value.
    sys.stdout.reconfigure(newline="\n")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("versions")
    command.add_argument("--tag")
    for name in ("changelog", "notes"):
        commands.add_parser(name).add_argument("version")
    commands.add_parser("tool-count")
    commands.add_parser("npm-bundle")
    commands.add_parser("pypi-token")
    for name in ("npm-tarball", "dist", "addon-zip", "mcp-responses"):
        command = commands.add_parser(name)
        command.add_argument("path", type=Path)
        command.add_argument("--version", required=True)
        if name == "mcp-responses":
            command.add_argument("--tools", required=True, type=int)
    args = parser.parse_args(argv)
    root = Path.cwd()

    try:
        if args.command == "versions":
            print(check_versions(root, args.tag))
        elif args.command == "changelog":
            changelog_entry(root, args.version)
            print(f"CHANGELOG.md carries a dated entry for {args.version}.")
        elif args.command == "notes":
            print(changelog_entry(root, args.version))
        elif args.command == "tool-count":
            print(tool_count(root))
        elif args.command == "npm-bundle":
            build_npm_bundle(root / "npm", root)
            print(f"npm/server/{PACKAGE}, npm/LICENSE and npm/NOTICE are in place.")
        elif args.command == "npm-tarball":
            names = check_npm_tarball(args.path, args.version)
            print(f"{args.path.name}: {len(names)} entries, the server, the add-on and the licence files present.")
        elif args.command == "dist":
            built = check_dist(args.path, args.version)
            print("Checked " + ", ".join(p.name for p in built) + ".")
        elif args.command == "addon-zip":
            names = check_addon_zip(args.path, args.version)
            print(f"{args.path.name}: {len(names)} files, blender_manifest.toml at the root, version {args.version}.")
        elif args.command == "mcp-responses":
            print(check_mcp_responses(_read(args.path), args.tools, args.version))
        elif args.command == "pypi-token":
            print(check_pypi_token(os.environ.get("PYPI_API_TOKEN")))
    except Failed as failure:
        print(f"::error::{failure}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
