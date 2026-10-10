"""Tests for the end-to-end example, which works through the server from start to finish as an agent would.

The example script builds a lantern through the server, exports it, renders it, and loads the export back.
Every check the script makes must hold, and the files it writes must be what it says they are.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from blenderlens_mcp.glb import summarize
from tests.conftest import ROOT, checkout_env, png_info

pytestmark = pytest.mark.integration


def test_the_lantern_example_builds_exports_renders_and_loads_back(blender_binary, tmp_path):
    run = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "build_lantern.py"), "--out", str(tmp_path),
         "--server", f"{sys.executable} -m blenderlens_mcp"],
        capture_output=True, text=True, timeout=600, env=checkout_env({"BLENDER_BIN": blender_binary}))
    assert run.returncode == 0, run.stdout[-4000:] + run.stderr[-4000:]
    report = json.loads(run.stdout)
    assert all(report["checks"].values()), report["checks"]

    glb = summarize(report["glb"])
    assert [n["name"] for n in glb["nodes"]] == ["Lantern"]
    assert {m["name"] for m in glb["materials"]} == {"Frame", "Glow", "Cap Metal"}
    assert png_info(report["preview"])["width"] == 768
    assert png_info(report["preview_front"])["width"] == 512
    assert Path(report["blend"]).stat().st_size > 10_000
