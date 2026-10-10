"""Tests for the release kit: the versions, the registry entry, the npm package and the workflows.

All of them must agree with the code. The npm bundle is built as the workflows build it, and its launcher is run:
it must start the bundled server and respond. Every manifest must carry the package's version, and the npm
bundle must hold the licence and notice, which the workflows check again in the packed tarball. The other tests
check the workflows' safeguards: actions pinned to commits, read-only permissions, job timeouts, and checks that
run before anything is published.
"""

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from blenderlens_mcp import __version__, session
from blenderlens_mcp.cli import build_addon
from blenderlens_mcp.tools import TOOLS
from tests.test_packaging import ENV_VARS, REPOSITORY, read_toml
from tests.test_tools_table import VERBS

ROOT = Path(__file__).resolve().parent.parent
NPM = ROOT / "npm"
WORKFLOWS = ROOT / ".github" / "workflows"
KIT_SCRIPT = ROOT / ".github" / "scripts" / "release_kit.py"
REGISTRY_NAME = "io.github.pzalutski-pixel/blenderlens"
SCHEMA = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
#: A name in backticks that starts with one of the tool verbs defined in tests/test_tools_table.py.
TOOL_NAME = re.compile(rf"`((?:{'|'.join(sorted(VERBS))})_[a-z_]+)`")
NODE = shutil.which("node")


def server_json() -> dict:
    """Return server.json, the entry that the MCP Registry shows."""
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def npm_package() -> dict:
    """Return the npm package's package.json."""
    return json.loads((NPM / "package.json").read_text(encoding="utf-8"))


def workflow(name: str) -> str:
    """Return the text of the workflow file ``name`` in .github/workflows."""
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def jobs(text: str) -> dict[str, str]:
    """Split a workflow into its jobs, and return each job's text by job id.

    A job's text runs from its ``  <id>:`` line to the next job.
    """
    body = text.split("\njobs:\n", 1)[1]
    found = re.split(r"^  ([A-Za-z0-9_-]+):[ \t]*$", body, flags=re.MULTILINE)
    return dict(zip(found[1::2], found[2::2], strict=True))


def kit():
    """Import and return the release workflows' own checks from .github/scripts, so the tests run the same code."""
    spec = importlib.util.spec_from_file_location("release_kit", KIT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_kit(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    """Run release_kit.py with ``args`` in ``cwd``, as the workflows run it, and return the finished process."""
    return subprocess.run([sys.executable, str(KIT_SCRIPT), *args], cwd=cwd, capture_output=True, text=True,
                          timeout=120)


# ---------------------------------------------------------------------------
# One version in every manifest
# ---------------------------------------------------------------------------


def test_every_manifest_carries_the_package_version():
    """Every manifest carries the package version.

    PyPI reads it from __init__.py, Blender from the add-on manifest, npm from package.json, and the MCP Registry
    from server.json.
    """
    manifest = read_toml(ROOT / "src" / "blenderlens_mcp" / "addon" / "blender_manifest.toml")
    assert manifest["version"] == __version__
    assert npm_package()["version"] == __version__
    entry = server_json()
    assert entry["version"] == __version__
    assert [p["version"] for p in entry["packages"]] == [__version__] * len(entry["packages"])


def test_the_release_check_agrees_and_refuses_a_tag_that_does_not():
    agreed = run_kit("versions")
    assert agreed.returncode == 0, agreed.stdout + agreed.stderr
    assert agreed.stdout.strip() == __version__
    assert run_kit("versions", "--tag", f"v{__version__}").returncode == 0
    refused = run_kit("versions", "--tag", "v9.9.9")
    assert refused.returncode == 1
    assert "::error::" in refused.stdout and "9.9.9" in refused.stdout


def test_the_release_check_finds_every_version_it_compares(tmp_path):
    """The version check reads every manifest it compares, and names the one whose version differs."""
    for rel in ("src/blenderlens_mcp/__init__.py", "src/blenderlens_mcp/addon/blender_manifest.toml",
                "npm/package.json", "server.json"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    drifted = tmp_path / "npm" / "package.json"
    drifted.write_text(drifted.read_text(encoding="utf-8").replace(f'"{__version__}"', '"0.0.1"'), encoding="utf-8")
    refused = run_kit("versions", cwd=tmp_path)
    assert refused.returncode == 1
    assert "npm/package.json" in refused.stdout and "0.0.1" in refused.stdout


def test_a_tag_is_refused_until_the_changelog_dates_its_entry(tmp_path):
    """A tag is refused while its CHANGELOG entry is headed "Unreleased", the marker that no date is set yet."""
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n- Something.\n", encoding="utf-8")
    refused = run_kit("changelog", "0.2.0", cwd=tmp_path)
    assert refused.returncode == 1 and "::error::" in refused.stdout
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [0.2.0] - 2026-11-02\n\n- Something.\n",
                                           encoding="utf-8")
    assert run_kit("changelog", "0.2.0", cwd=tmp_path).returncode == 0
    notes = run_kit("notes", "0.2.0", cwd=tmp_path)
    assert notes.returncode == 0 and notes.stdout.strip() == "- Something."


def test_the_tool_count_that_the_workflows_require_comes_from_the_code():
    counted = run_kit("tool-count")
    assert counted.returncode == 0, counted.stderr
    assert counted.stdout.strip() == str(len(TOOLS))


# ---------------------------------------------------------------------------
# server.json, the entry the MCP Registry shows
# ---------------------------------------------------------------------------


def test_server_json_carries_the_registry_name_title_and_repository():
    entry = server_json()
    assert entry["$schema"] == SCHEMA
    assert entry["name"] == REGISTRY_NAME
    assert entry["title"] == "BlenderLens"
    assert entry["repository"] == {"url": REPOSITORY, "source": "github"}


def test_server_json_description_fits_the_registry_limit():
    """The registry rejects a description over 100 characters."""
    description = server_json()["description"]
    assert 0 < len(description) <= 100, len(description)


def test_server_json_offers_the_npm_and_the_pypi_package():
    packages = {p["registryType"]: p for p in server_json()["packages"]}
    assert set(packages) == {"npm", "pypi"}
    assert packages["npm"]["identifier"] == packages["pypi"]["identifier"] == "blenderlens-mcp"
    assert packages["npm"]["runtimeHint"] == "npx"
    assert packages["pypi"]["runtimeHint"] == "uvx"
    for package in packages.values():
        assert package["transport"] == {"type": "stdio"}


def test_server_json_advertises_exactly_the_environment_variables_the_code_reads():
    """server.json lists exactly the environment variables the code reads, each with a description and a format.

    A test in test_packaging.py checks ENV_VARS against a scan of the source, and a variable missing from
    server.json would otherwise go unnoticed by maintainers and users alike.
    """
    for package in server_json()["packages"]:
        declared = [entry["name"] for entry in package["environmentVariables"]]
        assert len(declared) == len(set(declared)), f"{package['registryType']} repeats a variable"
        assert set(declared) == ENV_VARS, package["registryType"]
        for entry in package["environmentVariables"]:
            assert entry.get("description"), entry["name"]
            assert entry.get("format", "string") in {"string", "number", "boolean", "filepath"}, entry["name"]


def test_server_json_packages_describe_the_variables_identically():
    npm, pypi = (p["environmentVariables"] for p in sorted(server_json()["packages"],
                                                            key=lambda p: p["registryType"]))
    assert npm == pypi


def test_server_json_defaults_are_the_codes_defaults_and_the_token_is_secret():
    variables = {e["name"]: e for e in server_json()["packages"][0]["environmentVariables"]}
    assert variables["BLENDERLENS_HOST"]["default"] == session.DEFAULT_HOST
    assert variables["BLENDERLENS_PORT"]["default"] == str(session.DEFAULT_PORT)
    assert variables["BLENDERLENS_MODE"]["default"] == "auto"
    assert set(variables["BLENDERLENS_MODE"]["choices"]) == set(session.MODES)
    assert variables["BLENDERLENS_TOKEN"].get("isSecret") is True


def test_readme_ends_with_the_registry_name():
    """The README ends with the registry name, which the MCP Registry looks for to confirm the PyPI package's owner."""
    last = (ROOT / "README.md").read_text(encoding="utf-8").rstrip("\n").splitlines()[-1]
    assert last == f"<!-- mcp-name: {REGISTRY_NAME} -->"


# ---------------------------------------------------------------------------
# The npm package
# ---------------------------------------------------------------------------


def test_the_npm_package_carries_its_name_registry_name_command_licence_and_repository():
    package = npm_package()
    assert package["name"] == "blenderlens-mcp"
    assert package["mcpName"] == REGISTRY_NAME, \
        "mcpName is not the registry name, by which the registry proves npm ownership"
    assert package["bin"] == {"blenderlens-mcp": "./bin/blenderlens-mcp.js"}
    assert package["license"] == "Apache-2.0"
    assert REPOSITORY in package["repository"]["url"]
    assert package["engines"]["node"]
    for shipped in ("bin/", "server/", "README.md", "LICENSE", "NOTICE"):
        assert shipped in package["files"], shipped


def test_the_launcher_points_at_the_bundled_layout():
    """The launcher runs the entry point where the bundle puts it, and its shebang has no carriage return."""
    raw = (NPM / "bin" / "blenderlens-mcp.js").read_bytes()
    assert raw.startswith(b"#!/usr/bin/env node\n"), \
        "the shebang does not end in LF; a CRLF shebang names a program called 'node\\r'"
    assert b"\r\n" not in raw, "the launcher has CRLF line endings, which npm/.gitattributes should prevent"
    launcher = raw.decode("utf-8")
    assert 'path.join(SERVER_DIR, "blenderlens_mcp")' in launcher
    assert "PYTHONPATH: SERVER_DIR" in launcher
    assert "existsSync(ENTRY_POINT)" in launcher, \
        "the launcher does not check that the bundle exists, so it cannot fail loudly when it is missing"
    for candidate in ('"python3"', '"python"', '"py"'):
        assert candidate in launcher, candidate


def test_the_npm_readme_does_not_name_tools_that_do_not_exist():
    """The npm README names only tools that exist.

    The main README's tool tables are checked in test_packaging.py instead, because its prose names parameters
    such as render_dir, which look like tool names.
    """
    named = set(TOOL_NAME.findall((NPM / "README.md").read_text(encoding="utf-8")))
    assert named, "the npm README names no tools"
    phantom = named - {tool["name"] for tool in TOOLS}
    assert not phantom, f"npm/README.md names tools that do not exist: {sorted(phantom)}"


def test_the_npm_readme_states_the_tool_count_correctly_if_at_all():
    """A tool count stated in the npm README, which npmjs.com shows, is the real one.

    The main README's count is checked in test_packaging.py.
    """
    for stated in re.findall(r"\*?\*?(\d+)\*?\*? tools", (NPM / "README.md").read_text(encoding="utf-8")):
        assert int(stated) == len(TOOLS), f"npm/README.md claims {stated} tools; there are {len(TOOLS)}"


def test_the_npm_readme_documents_every_variable_and_the_licence():
    text = (NPM / "README.md").read_text(encoding="utf-8")
    missing = {var for var in ENV_VARS if f"`{var}`" not in text}
    assert not missing, sorted(missing)
    assert "Apache" in text
    assert "Python 3.10" in text, "the npm README does not say that the package needs Python 3.10"


@pytest.fixture
def npm_bundle(tmp_path) -> Path:
    """Return a copy of the npm package's folder, built as the workflows build it, before packing."""
    target = tmp_path / "npm"
    shutil.copytree(NPM, target, ignore=shutil.ignore_patterns("server", "node_modules", "*.tgz", "LICENSE",
                                                               "NOTICE"))
    kit().build_npm_bundle(target, root=ROOT)
    return target


def run_launcher(bundle: Path, *args: str, stdin: str = "", env: dict | None = None, cwd: Path | None = None):
    """Run the bundle's npm launcher under Node with ``args`` and ``stdin``, and return the finished process."""
    return subprocess.run([NODE, str(bundle / "bin" / "blenderlens-mcp.js"), *args], input=stdin,
                          capture_output=True, text=True, timeout=120, env=env, cwd=cwd)


HANDSHAKE = "\n".join(json.dumps(m) for m in (
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
)) + "\n"


def test_the_bundle_holds_what_the_launcher_runs_and_nothing_compiled(npm_bundle):
    assert (npm_bundle / "server" / "blenderlens_mcp" / "__main__.py").is_file()
    assert (npm_bundle / "server" / "blenderlens_mcp" / "addon" / "blender_manifest.toml").is_file()
    assert (npm_bundle / "LICENSE").is_file() and (npm_bundle / "NOTICE").is_file()
    assert not list(npm_bundle.rglob("__pycache__")) and not list(npm_bundle.rglob("*.pyc"))


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_the_launcher_responds_to_initialize_and_lists_every_tool(npm_bundle, tmp_path):
    run = run_launcher(npm_bundle, stdin=HANDSHAKE, cwd=tmp_path)
    assert run.returncode == 0, run.stderr[-2000:]
    responses = {r["id"]: r for r in map(json.loads, run.stdout.splitlines())}
    assert responses[1]["result"]["serverInfo"] == {"name": "blenderlens-mcp", "version": __version__}
    assert len(responses[2]["result"]["tools"]) == len(TOOLS)
    # The check that the workflows run on the same responses must agree.
    recorded = tmp_path / "responses.jsonl"
    recorded.write_text(run.stdout, encoding="utf-8")
    checked = run_kit("mcp-responses", "--tools", str(len(TOOLS)), "--version", __version__, str(recorded))
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_the_launcher_passes_the_arguments_and_the_exit_code_through(npm_bundle, tmp_path):
    version = run_launcher(npm_bundle, "--version", cwd=tmp_path)
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == f"blenderlens-mcp {__version__}"
    bad = run_launcher(npm_bundle, "--no-such-option", cwd=tmp_path)
    assert bad.returncode == 2, (bad.returncode, bad.stderr)
    assert "unrecognized arguments" in bad.stderr


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_the_launcher_refuses_clearly_when_the_bundle_is_missing(npm_bundle, tmp_path):
    shutil.rmtree(npm_bundle / "server")
    run = run_launcher(npm_bundle, stdin=HANDSHAKE, cwd=tmp_path)
    assert run.returncode == 1
    assert run.stdout == "", "the launcher wrote to stdout, which belongs to the MCP stream"
    assert "missing" in run.stderr and "reinstall" in run.stderr.lower()


def _env_with_path(path: str) -> dict:
    """Return a minimal environment whose PATH is ``path`` alone, keeping only the variables Windows needs."""
    keep = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME",
                                                                  "USERPROFILE", "COMSPEC", "PATHEXT"}}
    return {**keep, "PATH": path}


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_the_launcher_says_when_no_python_is_found(npm_bundle, tmp_path):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    run = run_launcher(npm_bundle, stdin=HANDSHAKE, env=_env_with_path(str(empty)), cwd=empty)
    assert run.returncode == 1
    assert run.stdout == ""
    assert "Python 3.10" in run.stderr


@pytest.mark.skipif(NODE is None or sys.platform != "win32",
                    reason="This test needs Node.js on Windows, where the launcher falls back to the py launcher.")
def test_the_launcher_falls_back_to_the_py_launcher_on_windows(npm_bundle, tmp_path):
    py = shutil.which("py")
    if py is None:
        pytest.skip("no py launcher on this machine")
    folder = Path(py).parent
    if any((folder / name).exists() for name in ("python.exe", "python3.exe")):
        pytest.skip(f"{folder} holds python.exe as well, so py would not be the one found")
    probe = subprocess.run([py, "-3", "-c", "import sys; print(sys.version_info >= (3, 10))"],
                           capture_output=True, text=True, timeout=60)
    if probe.stdout.strip() != "True":
        pytest.skip("py -3 runs a Python older than 3.10 here")
    run = run_launcher(npm_bundle, stdin=HANDSHAKE, env=_env_with_path(str(folder)), cwd=tmp_path)
    assert run.returncode == 0, run.stderr[-2000:]
    assert len(json.loads(run.stdout.splitlines()[-1])["result"]["tools"]) == len(TOOLS)


PLANTED_NAMES = ("python3", "python", "py")


def _plant_interpreters(folder: Path, marker: Path) -> dict:
    """Create programs named like every interpreter the launcher looks for, each writing ``marker`` if it runs.

    Return the environment variables the programs need in order to work. On Windows each program is a copy of
    node.exe, and NODE_OPTIONS makes it run plant.js first, which writes the marker only when it runs under a
    Python name. The launcher's own node, which also gets NODE_OPTIONS, therefore writes nothing. Elsewhere each
    program is a shell script.
    """
    if sys.platform != "win32":
        for name in PLANTED_NAMES:
            script = folder / name
            script.write_text(f"#!/bin/sh\necho \"$0 ran\" > '{marker}'\n", encoding="utf-8")
            script.chmod(0o755)
        return {}
    plant = folder.parent / "plant.js"
    plant.write_text(
        "const path = require('path');\n"
        "if (path.basename(process.execPath).toLowerCase().startsWith('py')) {\n"
        f"  require('fs').writeFileSync({json.dumps(str(marker))}, process.execPath + ' ran');\n"
        "  process.exit(0);\n"
        "}\n", encoding="utf-8")
    first = folder / f"{PLANTED_NAMES[0]}.exe"
    shutil.copy(NODE, first)
    for name in PLANTED_NAMES[1:]:
        try:
            os.link(first, folder / f"{name}.exe")  # A hard link, so the 90 MB copy is stored only once.
        except OSError:
            shutil.copy(first, folder / f"{name}.exe")
    # Use forward slashes, because inside the quotes of NODE_OPTIONS a backslash escapes the next character.
    return {"NODE_OPTIONS": f'--require "{plant.as_posix()}"'}


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
@pytest.mark.parametrize("dotted", [False, True], ids=["path-as-is", "dot-and-empty-entries"])
def test_an_interpreter_planted_in_the_current_working_directory_never_runs(npm_bundle, tmp_path, dotted):
    """An interpreter planted in the current working directory never runs.

    On Windows, libuv looks in the current working directory before PATH for a bare program name, and on POSIX an
    empty or "." entry on PATH does the same, so the launcher searches PATH itself and runs absolute paths only.
    """
    project = tmp_path / "project"
    project.mkdir()
    marker = tmp_path / "planted-interpreter-ran.txt"
    extra = _plant_interpreters(project, marker)
    real_path = os.environ.get("PATH", "")
    # Put ".", an empty entry and a relative entry first. On POSIX, execvp reads the first two as the current
    # working directory, and a search written by hand might resolve any of the three against it.
    entries = [".", "", "project", real_path] if dotted else [real_path]
    env = {**os.environ, **extra, "PATH": os.pathsep.join(entries)}
    run = run_launcher(npm_bundle, stdin=HANDSHAKE, env=env, cwd=project)
    assert not marker.exists(), f"a planted interpreter ran: {marker.read_text(encoding='utf-8', errors='replace')}"
    assert run.returncode == 0, run.stderr[-2000:]
    responses = {r["id"]: r for r in map(json.loads, run.stdout.splitlines())}
    assert len(responses[2]["result"]["tools"]) == len(TOOLS), "the real server did not list every tool"


def _resolve(name: str, path_entries: list, cwd: Path, pathext: str | None = None):
    """Run the launcher's own PATH search in Node, and return the absolute path it would spawn for ``name``, or None."""
    env = {"PATH": os.pathsep.join(str(e) for e in path_entries)}
    if pathext is not None:
        env["PATHEXT"] = pathext
    script = ("const l = require(process.argv[1]);"
              "process.stdout.write(JSON.stringify(l.resolveCommand(process.argv[2], JSON.parse(process.argv[3]))));")
    run = subprocess.run([NODE, "-e", script, str(NPM / "bin" / "blenderlens-mcp.js"), name, json.dumps(env)],
                         capture_output=True, text=True, timeout=60, cwd=cwd)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def _fake_program(folder: Path, name: str) -> Path:
    """Create an executable file named like the program ``name`` in ``folder``, and return its path."""
    folder.mkdir(parents=True, exist_ok=True)
    program = folder / (f"{name}.exe" if sys.platform == "win32" else name)
    program.write_bytes(b"#!/bin/sh\nexit 0\n")
    program.chmod(0o755)
    return program


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_the_launcher_resolves_only_absolute_path_entries(tmp_path):
    """The launcher skips empty, "." and relative PATH entries, which resolve against the current working directory."""
    work = tmp_path / "work"
    for relative in (work, work / "rel"):
        _fake_program(relative, "python3")
    real = _fake_program(tmp_path / "bin", "python3")
    found = _resolve("python3", ["", ".", "rel", str(tmp_path / "missing"), str(tmp_path / "bin")], cwd=work)
    assert found == str(real)
    assert _resolve("python3", ["", ".", "rel"], cwd=work) is None


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_the_launcher_skips_a_folder_named_like_the_interpreter(tmp_path):
    (tmp_path / "a" / ("python3.exe" if sys.platform == "win32" else "python3")).mkdir(parents=True)
    real = _fake_program(tmp_path / "b", "python3")
    assert _resolve("python3", [tmp_path / "a", tmp_path / "b"], cwd=tmp_path) == str(real)


@pytest.mark.skipif(NODE is None or sys.platform == "win32",
                    reason="This test needs Node.js on a POSIX system, where files have an execute permission.")
def test_the_launcher_skips_a_file_it_cannot_execute(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "python3").write_text("not a program", encoding="utf-8")
    real = _fake_program(tmp_path / "b", "python3")
    assert _resolve("python3", [tmp_path / "a", tmp_path / "b"], cwd=tmp_path) == str(real)


@pytest.mark.skipif(NODE is None or sys.platform != "win32",
                    reason="This test needs Node.js on Windows, the only system with PATHEXT.")
def test_the_launcher_applies_pathext_and_spawns_only_what_runs_without_a_shell(tmp_path):
    """The launcher applies PATHEXT, but takes only .exe and .com files, which run without a shell.

    A .cmd or .bat file needs cmd.exe, which would search the current working directory again.
    """
    batch = tmp_path / "a"
    batch.mkdir()
    (batch / "python3.cmd").write_text("@echo off\r\n", encoding="utf-8")
    com = tmp_path / "c"
    com.mkdir()
    (com / "python3.com").write_bytes(b"MZ")
    real = _fake_program(tmp_path / "b", "python3")
    assert _resolve("python3", [batch, real.parent], cwd=tmp_path, pathext=".CMD;.BAT;.EXE") == str(real)
    assert _resolve("python3", [com, real.parent], cwd=tmp_path, pathext=".COM;.EXE") == str(com / "python3.com")
    assert _resolve("python3", [com, real.parent], cwd=tmp_path, pathext=".EXE") == str(real)
    assert _resolve("python3", [com, real.parent], cwd=tmp_path) == str(com / "python3.com"), \
        "without PATHEXT, the launcher does not take .COM;.EXE"


@pytest.mark.skipif(NODE is None or sys.platform != "win32",
                    reason="This test needs Node.js on Windows, the only system with Microsoft Store aliases.")
def test_the_launcher_still_finds_the_microsoft_store_alias(tmp_path):
    """The launcher keeps the Microsoft Store's python.exe as a candidate.

    That python.exe is an app execution alias: a reparse point that fs.statSync cannot follow (it fails with
    EACCES) and that fs.existsSync reports as missing, though Windows runs it. For some users it is their only
    Python, so it must stay a candidate, and the launcher's version probe then tells a working Python from the
    Store's placeholder.
    """
    aliases = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps"
    alias = next((aliases / n for n in ("python3.exe", "python.exe") if os.path.lexists(aliases / n)), None)
    if alias is None:
        pytest.skip("no Microsoft Store python alias on this machine")
    assert _resolve(alias.stem, [aliases], cwd=tmp_path) == str(alias)


def test_the_response_check_counts_the_tools_and_checks_the_version(tmp_path):
    responses = tmp_path / "responses.jsonl"
    tools = [{"name": f"t{i}", "inputSchema": {}} for i in range(3)]
    responses.write_text(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {
        "name": "blenderlens-mcp", "version": "1.2.3"}}}) + "\n" + json.dumps(
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}) + "\n", encoding="utf-8")
    assert run_kit("mcp-responses", "--tools", "3", "--version", "1.2.3", str(responses)).returncode == 0
    wrong = run_kit("mcp-responses", "--tools", "4", "--version", "1.2.3", str(responses))
    assert wrong.returncode == 1 and "3" in wrong.stdout
    assert run_kit("mcp-responses", "--tools", "3", "--version", "1.2.4", str(responses)).returncode == 1


def test_the_addon_zip_check_wants_the_manifest_at_the_root_and_no_bytecode(tmp_path):
    good = build_addon(str(tmp_path / "good.zip"))
    assert run_kit("addon-zip", str(good), "--version", __version__).returncode == 0
    nested = tmp_path / "nested.zip"
    with zipfile.ZipFile(good) as source, zipfile.ZipFile(nested, "w") as target:
        for name in source.namelist():
            target.writestr(f"blenderlens/{name}", source.read(name))
    assert run_kit("addon-zip", str(nested), "--version", __version__).returncode == 1
    cached = tmp_path / "cached.zip"
    shutil.copy(good, cached)
    with zipfile.ZipFile(cached, "a") as archive:
        archive.writestr("__pycache__/bridge.cpython-311.pyc", b"")
    assert run_kit("addon-zip", str(cached), "--version", __version__).returncode == 1


def _synthetic_dist(folder: Path, version: str, extra: tuple[str, ...] = ()) -> Path:
    """Write a wheel and an sdist that pass the dist check into ``folder``, and return the folder.

    The files in ``extra``, relative to the sdist's top folder, are added to the sdist.
    """
    folder.mkdir()
    package = "blenderlens_mcp"
    info = f"{package}-{version}.dist-info"
    with zipfile.ZipFile(folder / f"{package}-{version}-py3-none-any.whl", "w") as wheel:
        for name in (f"{package}/__main__.py", f"{package}/background.py", f"{package}/addon/blender_manifest.toml",
                     f"{package}/addon/commands.py", f"{info}/licenses/LICENSE", f"{info}/licenses/NOTICE"):
            wheel.writestr(name, "")
        wheel.writestr(f"{info}/METADATA", f"Name: blenderlens-mcp\nVersion: {version}\n\nmcp-name: {REGISTRY_NAME}\n")
    top = f"{package}-{version}"
    shipped = ("PKG-INFO", "LICENSE", "NOTICE", "README.md", "pyproject.toml", "examples/rules.json",
               f"src/{package}/addon/blender_manifest.toml", f"src/{package}/background.py", *extra)
    with tarfile.open(folder / f"{top}.tar.gz", "w:gz") as sdist:
        for name in shipped:
            sdist.addfile(tarfile.TarInfo(f"{top}/{name}"), io.BytesIO(b""))
    return folder


def test_the_dist_check_passes_a_wheel_and_an_sdist_that_hold_only_what_they_should(tmp_path):
    checked = run_kit("dist", str(_synthetic_dist(tmp_path / "dist", "1.2.3")), "--version", "1.2.3")
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize("stray", ["examples/out/lamp.glb", ".venv/pyvenv.cfg", "notes.txt"])
def test_the_dist_check_refuses_an_sdist_that_holds_a_file_outside_its_allowed_paths(tmp_path, stray):
    refused = run_kit("dist", str(_synthetic_dist(tmp_path / "dist", "1.2.3", (stray,))), "--version", "1.2.3")
    assert refused.returncode == 1
    assert "::error::" in refused.stdout and stray in refused.stdout


def test_the_dist_check_refuses_an_sdist_that_holds_bytecode(tmp_path):
    """A .pyc file in the sdist is refused, even in a folder whose sources the sdist may hold."""
    compiled = "src/blenderlens_mcp/__pycache__/server.cpython-312.pyc"
    refused = run_kit("dist", str(_synthetic_dist(tmp_path / "dist", "1.2.3", (compiled,))), "--version", "1.2.3")
    assert refused.returncode == 1
    assert "bytecode" in refused.stdout and compiled in refused.stdout


# ---------------------------------------------------------------------------
# The workflows
# ---------------------------------------------------------------------------

PUBLISH_WORKFLOWS = ["publish-pypi.yml", "publish-npm.yml", "publish-mcp-registry.yml"]
WORKFLOW_FILES = ["ci.yml", "release.yml", *PUBLISH_WORKFLOWS]
BUILD_WORKFLOWS = ["ci.yml", "release.yml"]


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_every_action_is_pinned_to_a_commit_with_its_version_beside_it(name):
    """Every action is pinned to a commit SHA, with its version in a comment beside it.

    A tag can be moved to other code but a commit cannot, and Dependabot updates the SHA and the comment together.
    """
    uses = re.findall(r"^\s*(?:- )?uses: (\S+)(.*)$", workflow(name), re.MULTILINE)
    assert uses, name
    for action, rest in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}", action), action
        assert re.fullmatch(r"\s+# v\d+(\.\d+)*", rest), f"{action} has no '# vX.Y.Z' comment"


def test_one_action_is_pinned_to_one_commit_across_the_workflows():
    pins: dict[str, set] = {}
    for name in WORKFLOW_FILES:
        for action, sha, version in re.findall(r"uses: ([\w./-]+)@([0-9a-f]{40}) # (v[\d.]+)", workflow(name)):
            pins.setdefault(action, set()).add((sha, version))
    drifted = {action: pinned for action, pinned in pins.items() if len(pinned) > 1}
    assert not drifted, drifted


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_a_workflow_starts_read_only_and_every_job_has_a_timeout(name):
    text = workflow(name)
    head = text.split("\njobs:\n", 1)[0]
    assert re.search(r"^permissions:\n  contents: read\n(?!  )", head + "\n", re.MULTILINE), \
        "the top-level permissions are not contents: read alone"
    for job, block in jobs(text).items():
        assert re.search(r"^    timeout-minutes: \d+$", block, re.MULTILINE), f"{name}: job {job} has no timeout"


def test_only_the_jobs_that_publish_ask_for_more_than_reading():
    for name in WORKFLOW_FILES:
        for job, block in jobs(workflow(name)).items():
            for grant in re.findall(r"^      ([a-z-]+): write", block, re.MULTILINE):
                assert name != "ci.yml" and job.startswith(("publish-", "github-release")), \
                    f"{name}: job {job} asks for {grant}: write"


def test_ci_runs_on_main_on_pull_requests_into_main_and_by_hand():
    head = workflow("ci.yml").split("\njobs:\n", 1)[0]
    assert re.search(r"^  push:\n    branches: \[main\]$", head, re.MULTILINE)
    assert re.search(r"^  pull_request:\n    branches: \[main\]$", head, re.MULTILINE)
    assert re.search(r"^  workflow_dispatch:$", head, re.MULTILINE)


def test_ci_runs_lint_the_unit_matrix_integration_and_the_package_check():
    ci = jobs(workflow("ci.yml"))
    assert set(ci) == {"lint", "test", "integration", "package"}
    assert "ruff check ." in ci["lint"]
    for os_name in ("ubuntu-latest", "windows-latest", "macos-latest"):
        assert os_name in ci["test"] and os_name in ci["package"]
    for python in ("'3.10'", "'3.11'", "'3.12'", "'3.13'"):
        assert python in ci["test"], python
    assert '-m "not integration and not live"' in ci["test"]
    assert "-m integration" in ci["integration"]
    assert "fetch_blender.py" in ci["integration"]
    fetch = (ROOT / ".github" / "scripts" / "fetch_blender.py").read_text(encoding="utf-8")
    assert "BLENDER_BIN" in fetch and "GITHUB_ENV" in fetch, \
        "fetch_blender.py does not set BLENDER_BIN, by which the integration suite finds Blender"


def test_every_test_a_workflow_deselects_exists():
    """A test a workflow deselects by name exists, so renaming it cannot silently undo the deselection."""
    for name in WORKFLOW_FILES:
        for path, test in re.findall(r"--deselect (tests/\w+\.py)::(\w+)", workflow(name)):
            source = (ROOT / path).read_text(encoding="utf-8")
            assert re.search(rf"^def {test}\(", source, re.MULTILINE), \
                f"{name} deselects {path}::{test}, which does not exist"


def test_ci_runs_the_integration_tests_on_the_oldest_supported_blender_and_on_5_0():
    """CI runs the integration tests on Blender 4.2 LTS, the oldest supported, and 5.0 on Linux and Windows.

    On macOS it runs them on 5.0 only.
    """
    block = jobs(workflow("ci.yml"))["integration"]
    pairs = re.findall(r"- os: (\S+)\n\s+blender: '([\d.]+)'\n\s+file: (\S+)\n\s+sha256: ([0-9a-f]{64})", block)
    combos = {(os_name, version.rsplit(".", 1)[0]) for os_name, version, _, _ in pairs}
    assert combos == {("ubuntu-latest", "4.2"), ("ubuntu-latest", "5.0"), ("windows-latest", "4.2"),
                      ("windows-latest", "5.0"), ("macos-latest", "5.0")}, combos
    for _, version, file, _ in pairs:
        assert file.startswith(f"blender-{version}-"), file
    assert "download.blender.org" in (ROOT / ".github" / "scripts" / "fetch_blender.py").read_text(encoding="utf-8")
    assert "actions/cache@" in block


@pytest.mark.parametrize("name", BUILD_WORKFLOWS)
def test_the_npm_bundle_is_built_and_checked_by_the_same_code_everywhere(name):
    """Each build workflow builds and checks the npm bundle with the same release_kit.py steps.

    npm pack runs inside npm/ and cannot reach the repository's LICENSE and NOTICE, so listing them in
    package.json is not enough: the bundle step copies them in, and the check reads the tarball itself.
    """
    text = workflow(name)
    for step in ("npm-bundle", "npm-tarball", "mcp-responses", "tool-count", "dist", "addon-zip"):
        assert re.search(rf'release_kit\.py"? {step}\b', text), step
    assert "npm pack" in text
    assert "build-addon" in text


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_no_tool_count_is_written_into_a_workflow(name):
    """No workflow hard-codes a tool count.

    The count comes from tools.py at run time, so a new tool needs no workflow change.
    """
    assert not re.search(r"--tools \d", workflow(name))


def test_a_release_starts_from_a_tag_and_verifies_everything_before_it_builds():
    release = workflow("release.yml")
    assert re.search(r"^  push:\n    tags: \['v\*'\]$", release, re.MULTILINE)
    steps = jobs(release)
    verify = steps["verify"]
    assert "fetch-depth: 0" in verify and "git fetch --no-tags origin main" in verify
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" FETCH_HEAD' in verify, "a tag off main is not refused"
    assert 'release_kit.py versions --tag "$GITHUB_REF_NAME"' in verify
    assert "release_kit.py changelog" in verify
    assert "gh run list" in verify and "--workflow ci.yml" in verify, "the release does not check for a green CI run"
    assert "NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}" in verify and "npm whoami" in verify
    assert "PYPI_API_TOKEN: ${{ secrets.PYPI_API_TOKEN }}" in verify and "release_kit.py pypi-token" in verify
    assert "./mcp-publisher validate server.json" in verify
    assert re.search(r"^    needs: verify$", steps["test"], re.MULTILINE)
    assert re.search(r"^    needs: verify$", steps["build"], re.MULTILINE)
    assert re.search(r"^    needs: \[test, build\]$", steps["github-release"], re.MULTILINE)


def test_the_release_workflow_publishes_to_no_registry():
    """Each registry has its own workflow, so a failed upload is retried alone."""
    release = workflow("release.yml")
    assert set(jobs(release)) == {"verify", "test", "build", "github-release"}
    assert "npm publish " not in release and "gh-action-pypi-publish" not in release
    assert "mcp-publisher publish" not in release


def test_the_github_release_attaches_and_attests_every_file_a_registry_receives():
    block = jobs(workflow("release.yml"))["github-release"]
    assert "sha256sum -- * > SHA256SUMS" in block
    attested = block.split("subject-path: |", 1)[1].split("- name:", 1)[0]
    for pattern in ("assets/*.zip", "assets/*.whl", "assets/*.tar.gz", "assets/*.tgz", "assets/SHA256SUMS"):
        assert pattern in attested, pattern
    created = block.split("gh release create", 1)[1]
    for pattern in (".zip", "assets/*.whl", "assets/*.tar.gz", "assets/*.tgz", "assets/SHA256SUMS"):
        assert pattern in created, pattern


@pytest.mark.parametrize("name", PUBLISH_WORKFLOWS)
def test_a_publish_workflow_follows_a_successful_release_and_runs_by_hand_for_a_tag(name):
    text = workflow(name)
    head = text.split("\njobs:\n", 1)[0]
    assert re.search(r"^  workflow_run:\n    workflows: \[Release\]\n    types: \[completed\]$", head, re.MULTILINE)
    assert re.search(r"^  workflow_dispatch:\n    inputs:\n      tag:", head, re.MULTILINE)
    assert "group: " + name.removesuffix(".yml") in head
    (job,) = jobs(text).values()
    assert "if: github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion == 'success'" in job
    assert "format('refs/tags/{0}', inputs.tag) || github.event.workflow_run.head_sha" in job, \
        "the workflow does not check out the released commit"
    assert 'release_kit.py versions --tag "$tag"' in job
    for line in text.splitlines():
        if "inputs.tag" in line:
            assert line.strip().startswith(("ref:", "DISPATCHED_TAG:")), f"the tag reaches a shell directly: {line}"
        if "workflow_run.head_branch" in line:
            assert line.strip().startswith("RELEASED_TAG:"), f"the tag reaches a shell directly: {line}"
    assert 'echo "PUBLISHED=1" >> "$GITHUB_ENV"' in job, "a version already published is not skipped"
    assert "if: env.PUBLISHED != '1'" in job


@pytest.mark.parametrize("name, package", [("publish-pypi.yml", "dist"), ("publish-npm.yml", "npm-tarball")])
def test_a_package_is_published_from_the_release_files_after_its_checksum_attestation_and_contents(name, package):
    job = workflow(name)
    assert 'gh release download "$TAG"' in job and "--pattern SHA256SUMS" in job
    assert "sha256sum --check --strict -" in job
    assert 'gh attestation verify' in job and '--repo "$GITHUB_REPOSITORY"' in job
    assert f"release_kit.py {package} " in job
    assert job.index("gh attestation verify") < job.rindex("PUBLISHED != '1'")


def test_pypi_is_published_with_its_token_after_checking_it():
    job = workflow("publish-pypi.yml")
    assert "release_kit.py pypi-token" in job
    assert "pypa/gh-action-pypi-publish@" in job and "password: ${{ secrets.PYPI_API_TOKEN }}" in job
    assert job.index("release_kit.py pypi-token") < job.index("pypa/gh-action-pypi-publish@")
    assert "https://pypi.org/pypi/blenderlens-mcp/$VERSION/json" in job


def test_npm_is_published_with_provenance_and_its_token_after_checking_it():
    job = workflow("publish-npm.yml")
    assert "npm whoami" in job and job.index("npm whoami") < job.index("npm publish")
    assert "npm publish" in job and "--provenance" in job and "id-token: write" in job
    assert "NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}" in job
    assert 'npm view "blenderlens-mcp@$VERSION" version' in job


def test_the_registry_is_published_after_both_packages_with_the_release_s_pinned_publisher():
    text = workflow("publish-mcp-registry.yml")
    assert "https://pypi.org/pypi/blenderlens-mcp/$VERSION/json" in text
    assert 'npm view "blenderlens-mcp@$VERSION"' in text
    assert text.index("Wait until PyPI and npm both have the version") < text.index("mcp-publisher publish")
    assert "registry.modelcontextprotocol.io/v0/servers/" in text, "a version already listed is not skipped"
    for pin in ("MCP_PUBLISHER_VERSION", "MCP_PUBLISHER_SHA256"):
        pinned = [re.search(rf"^  {pin}: '(\S+)'$", workflow(name), re.MULTILINE).group(1)
                  for name in ("release.yml", "publish-mcp-registry.yml")]
        assert pinned[0] == pinned[1], pin
    for name in ("release.yml", "publish-mcp-registry.yml"):
        assert "sha256sum --check" in workflow(name) and "releases/latest" not in workflow(name), name
    assert "mcp-publisher login github-oidc" in text and "mcp-publisher publish" in text


def test_the_pypi_token_check_accepts_only_an_api_token_for_pypi_org(capsys, monkeypatch):
    """The check reads the macaroon's location, so a TestPyPI token or a pasted value fails before a release."""
    import base64

    def token(location: str) -> str:
        raw = bytes([2, 1, len(location)]) + location.encode() + bytes([2, 4]) + b"abcd" + bytes([0])
        return "pypi-" + base64.urlsafe_b64encode(raw).decode().rstrip("=")

    assert token("pypi.org").startswith("pypi-AgEIcHlwaS5vcmc"), "the sample does not match PyPI's token format"
    for value, fails in ((token("pypi.org"), False), (token("test.pypi.org"), True), ("", True),
                         ("npm_0123456789", True), ("pypi-not-a-macaroon", True)):
        monkeypatch.setenv("PYPI_API_TOKEN", value)
        code = kit().main(["pypi-token"])
        out = capsys.readouterr().out
        assert code == (1 if fails else 0), (value[:12], out)
        assert value[6:] not in out or not value[6:], "the check printed the token"


def test_dependabot_keeps_the_pinned_actions_current():
    text = (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "package-ecosystem: github-actions" in text


@pytest.mark.parametrize("name", [*WORKFLOW_FILES, "../dependabot.yml"])
def test_the_workflows_parse_as_yaml(name):
    yaml = pytest.importorskip("yaml")
    document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    if name in WORKFLOW_FILES:
        # YAML 1.1 reads the key `on` as True.
        assert document.get("on", document.get(True))
        assert document["permissions"] == {"contents": "read"}
        for job_id, job in document["jobs"].items():
            assert isinstance(job.get("timeout-minutes"), int), job_id
            assert job.get("runs-on"), job_id
            for step in job["steps"]:
                assert ("uses" in step) != ("run" in step), \
                    f"{job_id}: a step both runs a command and uses an action, or does neither"


def test_the_ignored_files_cover_what_the_release_builds():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in ("node_modules/", "npm/server/", "*.tgz", ".coverage*", "htmlcov/"):
        assert pattern in ignored, pattern
