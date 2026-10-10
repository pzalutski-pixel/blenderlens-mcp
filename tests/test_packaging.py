"""Tests for the packaging and the documentation, which must agree with the code.

The add-on is installed into Blender separately from the pip package, so its manifest must carry the same
version. The README must document every tool and every environment variable the code reads, and nothing that
does not exist.
"""

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from blenderlens_mcp import __version__, errors
from blenderlens_mcp.tools import TOOLS
from tests.conftest import checkout_env

# The unit tests also run on Python 3.10, which has no tomllib, and a plain top-level import would stop this
# whole module from being collected there. read_toml below falls back to parsing the simple lines these tests
# compare.
try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "blenderlens_mcp"
README = (ROOT / "README.md").read_text(encoding="utf-8")

#: Every environment variable the server reads.
ENV_VARS = {"BLENDER_BIN", "BLENDERLENS_MODE", "BLENDERLENS_HOST", "BLENDERLENS_PORT", "BLENDERLENS_TOKEN",
            "BLENDERLENS_TIMEOUT", "BLENDERLENS_START_TIMEOUT", "BLENDERLENS_RENDER_DIR", "BLENDERLENS_ALLOW_PYTHON",
            "BLENDERLENS_ALLOWED_DIRS", "BLENDERLENS_RUNTIME_DIR"}
#: Variables the code reads that are not BlenderLens settings: the platform's own, and the one the server sets for
#: the Blender it starts.
NOT_OURS = {"ProgramFiles", "LOCALAPPDATA", "XDG_STATE_HOME", "BLENDERLENS_SESSION_TOKEN"}

TOOL_ROW = re.compile(r"^\| `([a-z]+_[a-z_]+)` \|", re.MULTILINE)

#: The URL of the project's repository.
REPOSITORY = "https://github.com/pzalutski-pixel/blenderlens-mcp"


def read_toml(path: Path) -> dict:
    """Read a TOML file into a dict, with tomllib where it exists and with a minimal parser elsewhere.

    The minimal parser reads only ``key = "string"`` and ``key = ["strings"]`` lines. It puts the top-level keys
    at the top of the result and each table's keys under the table's dotted name (``"project.urls"``), which is
    all that the add-on manifest and the project URLs need. A test below checks that it agrees with tomllib on
    Python 3.11 and later.
    """
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(text)
    tables: dict = {"": {}}
    table = ""
    for line in text.splitlines():
        header = re.fullmatch(r"\[([A-Za-z0-9_.-]+)\]\s*", line)
        if header:
            table = header.group(1)
            tables.setdefault(table, {})
            continue
        pair = re.fullmatch(r'([A-Za-z0-9_-]+) = ("[^"]*"|\[\s*(?:"[^"]*"\s*,?\s*)*\])\s*', line)
        if pair:
            tables[table][pair.group(1)] = json.loads(pair.group(2))
    return {**tables.pop(""), **tables}


def project_urls() -> dict:
    """Return the project URLs that pyproject.toml declares."""
    parsed = read_toml(ROOT / "pyproject.toml")
    return parsed["project"]["urls"] if tomllib is not None else parsed["project.urls"]


def test_the_addon_manifest_carries_the_package_version():
    manifest = read_toml(SRC / "addon" / "blender_manifest.toml")
    assert manifest["version"] == __version__
    assert manifest["id"] == "blenderlens"
    assert manifest["license"] == ["SPDX:Apache-2.0"]


@pytest.mark.skipif(tomllib is None, reason="tomllib arrived in Python 3.11")
def test_the_python_310_reader_agrees_with_tomllib_on_what_the_tests_compare(monkeypatch):
    manifest_path = SRC / "addon" / "blender_manifest.toml"
    real_manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    real_urls = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["urls"]
    monkeypatch.setattr(sys.modules[__name__], "tomllib", None)
    fallback = read_toml(manifest_path)
    for key in ("id", "version", "name", "license", "blender_version_min", "tags"):
        assert fallback[key] == real_manifest[key], key
    assert project_urls() == real_urls


def test_the_tests_and_the_server_import_without_tomllib():
    """Every test module and every server module imports with tomllib blocked, as on Python 3.10, which has none.

    CI runs the unit tests on Python 3.10, and blocking tomllib here makes an unguarded import of it fail on any
    Python. The add-on and ``background.py`` are left out, because they run on Blender's own Python, which is 3.11
    or later.
    """
    modules = ["tests.conftest"] + [f"tests.{p.stem}" for p in sorted((ROOT / "tests").glob("test_*.py"))]
    modules += ["blenderlens_mcp"] + [f"blenderlens_mcp.{p.stem}" for p in sorted(SRC.glob("*.py"))
                                      if p.stem not in ("__init__", "__main__", "background")]
    code = ("import importlib, sys\n"
            "sys.modules['tomllib'] = None\n"
            f"for name in {modules!r}:\n"
            "    importlib.import_module(name)\n")
    run = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=checkout_env(), capture_output=True,
                         text=True, timeout=120)
    assert run.returncode == 0, run.stderr[-3000:]


def test_pyproject_links_the_repository():
    """pyproject.toml links the repository as the homepage, the source and the issue tracker.

    PyPI's sidebar, and every tool that reads the package metadata, take their links from these URLs.
    """
    assert project_urls() == {"Homepage": REPOSITORY, "Repository": REPOSITORY, "Issues": f"{REPOSITORY}/issues"}


def test_pyproject_says_beta_and_measures_coverage_across_processes():
    """pyproject.toml marks the package as a beta, and has coverage measure the subprocesses the tests start.

    Most of the server runs in subprocesses that the tests spawn, so coverage needs parallel mode and its
    subprocess patch, or server.py appears far less covered than it is.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"Development Status :: 4 - Beta"' in text
    assert re.search(r'"pytest-cov>=[0-9.]+"', text)
    assert "[tool.coverage.run]" in text, "pyproject.toml has no coverage configuration"
    coverage = text.split("[tool.coverage.run]", 1)[1].split("\n[", 1)[0]
    assert 'source = ["blenderlens_mcp"]' in coverage
    assert "parallel = true" in coverage
    assert 'patch = ["subprocess"]' in coverage


def test_pyproject_takes_its_version_from_the_package():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert 'path = "src/blenderlens_mcp/__init__.py"' in text
    assert 'blenderlens-mcp = "blenderlens_mcp.cli:main"' in text
    assert "dependencies = []" in text, "the server depends on packages beyond the standard library"


def test_every_environment_variable_read_is_in_the_list_here():
    read = set()
    # The add-on folder is searched too, because addon/runtime.py, which the server shares, reads
    # BLENDERLENS_RUNTIME_DIR.
    for source in SRC.rglob("*.py"):
        read |= set(re.findall(r'environ(?:\.get)?\(\s*"([A-Z_]+)"', source.read_text(encoding="utf-8")))
        read |= set(re.findall(r'_float_env\(\s*"([A-Z_]+)"', source.read_text(encoding="utf-8")))
    assert read - NOT_OURS == ENV_VARS


def test_readme_tool_tables_list_exactly_the_tools():
    """The README's tool tables list every tool, and no tool that does not exist.

    A row of a tool table starts with the tool's name, while a row of the refusal table starts with its exit
    code, so the pattern matches tool rows only.
    """
    rows = set(TOOL_ROW.findall(README))
    names = {t["name"] for t in TOOLS}
    assert names - rows == set(), f"README is missing {sorted(names - rows)}"
    assert rows - names == set(), f"README lists tools that do not exist: {sorted(rows - names)}"


def test_readme_states_the_tool_count_correctly():
    for stated in re.findall(r"\*?\*?(\d+)\*?\*? tools", README):
        assert int(stated) == len(TOOLS), f"README claims {stated} tools; there are {len(TOOLS)}"


def test_readme_documents_every_environment_variable():
    missing = {var for var in ENV_VARS if var not in README}
    assert not missing, sorted(missing)


def test_readme_documents_every_refusal_kind():
    missing = {kind for kind in errors.KINDS if f"`{kind}`" not in README}
    assert not missing, sorted(missing)


def test_readme_explains_both_ways_to_reach_blender_and_the_addon_install():
    for phrase in ("install-addon", "BlenderLens", "Start", "background", "Blender 5.0", ".mcp.json"):
        assert phrase in README, phrase


def test_license_is_consistent_everywhere():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text and "Version 2.0" in license_text
    assert "[yyyy]" not in license_text
    assert 'license = "Apache-2.0"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert (ROOT / "NOTICE").is_file()
    assert "Apache" in README


def test_changelog_covers_the_current_version():
    """The newest CHANGELOG heading is ``## [Unreleased]``, or ``## [x.y.z] - YYYY-MM-DD`` for the current version.

    The heading stays ``## [Unreleased]`` until the version is tagged, and the release workflow refuses a tag
    that has no dated heading.
    """
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    top = re.search(r"^## \[.*$", text, re.MULTILINE)
    assert top, "CHANGELOG has no entry"
    dated = rf"## \[{re.escape(__version__)}\] - \d{{4}}-\d{{2}}-\d{{2}}"
    assert top.group(0) == "## [Unreleased]" or re.fullmatch(dated, top.group(0)), \
        f"the newest entry is {top.group(0)!r}; it should be ## [Unreleased] or ## [{__version__}] - YYYY-MM-DD"


def test_the_wheel_carries_the_addon_and_the_background_script(tmp_path):
    """A wheel built from the checkout holds the background script and every file of the add-on, which Blender runs.

    Hatch packs everything under the package folder, so the Blender side ships in the same wheel as the server.
    """
    run = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(tmp_path), str(ROOT)],
                         capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    wheel = next(tmp_path.glob("*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    addon = sorted(p.name for p in (SRC / "addon").iterdir() if p.suffix in (".py", ".toml"))
    assert "commands.py" in addon and "blender_manifest.toml" in addon
    for required in ["background.py", *(f"addon/{name}" for name in addon)]:
        assert f"blenderlens_mcp/{required}" in names, required
    assert not any("__pycache__" in n for n in names)


@pytest.mark.parametrize("name", ["README.md", "CHANGELOG.md", "npm/README.md"])
def test_documents_hold_no_control_characters(name):
    """The documents hold no control characters other than tabs and line breaks."""
    text = (ROOT / name).read_text(encoding="utf-8")
    stray = sorted({hex(ord(c)) for c in text if ord(c) < 32 and c not in "\n\r\t"})
    assert not stray, f"{name} holds control characters {stray}"
