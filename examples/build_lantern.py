"""Build a lantern through BlenderLens, check and export it, render previews of it, and load it back.

    python examples/build_lantern.py [--out DIR] [--server COMMAND]

The script drives the MCP server over stdio, as an agent's MCP client would. It builds a box body with bevelled
edges and a recessed front panel that glows, and puts a four-sided cap on top as a second part, parented to the
body. It puts the origin at the base, checks the lantern against a triangle budget and a set of project rules
written in this script, and exports it as a .glb for Godot. It renders two preview PNGs, one from a three-quarter
view and one from the front, and saves the scene as a .blend. It then opens a new file, imports the .glb, and
checks that the parts and the materials came back.

examples/rules.json is a different rule set: the worked example of design record 0021, for a game's crates,
barrels and lanterns. It does not fit this lantern, whose rules are the ones that build() passes to check_asset.

Every file is written to the --out folder, examples/out by default. The script prints one JSON report, and exits
with a non-zero status if any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class Client:
    """A minimal MCP client that writes one JSON-RPC line per request and reads one line back."""

    def __init__(self, command: list[str], env: dict):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        self.next_id = 0
        reply = self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                            "clientInfo": {"name": "build-lantern", "version": "1"}})
        self.server = reply["result"]["serverInfo"]
        self.proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        """Send one JSON-RPC request, and return the server's response."""
        self.next_id += 1
        line = json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        self.proc.stdin.write(line.encode("utf-8") + b"\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def tool(self, tool: str, /, **arguments) -> dict:
        """Call a tool and return its result, or raise RuntimeError when the tool refuses the call."""
        # The tool name is positional-only, so a tool argument called "name" can be passed as a keyword.
        result = self.request("tools/call", {"name": tool, "arguments": arguments})["result"]
        payload = json.loads(result["content"][0]["text"])
        if result.get("isError"):
            raise RuntimeError(f"{tool} refused: {json.dumps(payload, indent=1)}")
        return payload

    def close(self) -> int:
        """Close the server's stdin, which ends the server, and return its exit status."""
        self.proc.stdin.close()
        return self.proc.wait(timeout=60)


def build(client: Client, out: Path) -> dict:
    """Build, check, export, render and reload the lantern, writing its files to ``out``, and return the report."""
    client.tool("open_session", mode="background")
    client.tool("create_file", discard=True)

    # The body is a box with its size built into the mesh rather than its scale, so the bevel stays even.
    client.tool("create_object", kind="cube", name="Lantern", size=[0.3, 0.3, 0.45], location=[0, 0, 0.225])
    panel = client.tool("edit_mesh", object="Lantern", operation="inset", facing="front", amount=0.05)
    # Push the panel in, so it sits in a recess in the frame.
    client.tool("edit_mesh", object="Lantern", operation="translate", faces=panel["result_faces"],
                offset=[0, 0.03, 0])
    client.tool("set_material", material="Frame", object="Lantern", base_color="#2f3337", metallic=0.8,
                roughness=0.4)
    client.tool("set_material", material="Glow", object="Lantern", faces=panel["result_faces"],
                base_color="#3a2410", emission_color="#ffb04a", emission_strength=8)
    client.tool("add_modifier", object="Lantern", type="bevel", settings={"width": 0.01, "segments": 3})

    # The cap is a four-sided cone, narrow at the top, turned so its sides face the body's sides. Its bottom
    # face is removed and the solidify modifier gives the open shell a thickness.
    client.tool("create_object", kind="cone", name="Cap", segments=4, radius=0.24, radius_top=0.04,
                depth=0.15, rotation=[0, 0, 45], location=[0, 0, 0.525])
    client.tool("edit_mesh", object="Cap", operation="delete", facing="bottom")
    client.tool("add_modifier", object="Cap", type="solidify", settings={"thickness": 0.01})
    client.tool("set_material", material="Cap Metal", object="Cap", base_color="#1f2326", metallic=0.9,
                roughness=0.5)
    client.tool("set_parent", object="Cap", parent="Lantern")
    # An engine places an asset by its origin, so the origin goes to the bottom of the lantern, where it stands.
    client.tool("apply_transform", object="Lantern", origin="bottom_center")

    # The project's rules: a single root object holding the cap, only the palette's materials, and the origin at
    # the base.
    rules = {
        "single_root": True,
        "required": ["Lantern/Cap"],
        "palette": {
            "Frame": {"base_color": "#2f3337", "metallic": 0.8, "roughness": 0.4},
            "Glow": {"base_color": "#3a2410", "emission": "#ffb04a", "strength": 8},
            "Cap Metal": {"base_color": "#1f2326", "metallic": 0.9, "roughness": 0.5},
        },
        "palette_only": True,
        "origin_at_base": True,
    }
    check = client.tool("check_asset", objects=["Lantern"], budget=2000, rules=rules)

    glb = client.tool("export_glb", path=str(out / "lantern.glb"), objects=["Lantern"], target="godot")
    hero = client.tool("render_preview", objects=["Lantern"], view="three_quarter", resolution=768,
                       path=str(out / "lantern.png"))
    front = client.tool("render_preview", objects=["Lantern"], view="front", resolution=512,
                        path=str(out / "lantern_front.png"))
    blend = client.tool("save_file", path=str(out / "lantern.blend"))

    # Load the export back into a new file and check what came back.
    client.tool("create_file")
    imported = client.tool("import_file", path=glb["path"])
    materials = {m["name"]: m for m in client.tool("list_materials")["materials"]}
    client.tool("close_session", discard=True)

    names = sorted(o["name"] for o in imported["objects"])
    contents = glb["contents"]
    checks = {
        "asset_passes_its_checks": check["passed"],
        "glb_is_valid": contents["valid"],
        "node_tree_is_lantern_then_cap": contents["nodes"][0]["name"] == "Lantern"
        and contents["nodes"][0]["children"][0]["name"] == "Cap",
        "panel_glows_in_the_file": any(m["name"] == "Glow" and m["emissive"] for m in contents["materials"]),
        "bevel_and_solidify_were_applied": all(m["triangles"] > 12 for m in contents["meshes"]),
        "reimport_gives_both_parts": names == ["Cap", "Lantern"],
        "reimport_keeps_the_glow": materials.get("Glow", {}).get("emission_strength", 0) >= 8,
        "previews_written": Path(hero["path"]).is_file() and Path(front["path"]).is_file(),
    }
    return {
        "server": client.server,
        "glb": glb["path"],
        "glb_bytes": glb["bytes"],
        "triangles": contents["triangles"],
        "check": {"passed": check["passed"], "counts": check["counts"]},
        "materials": [{k: m[k] for k in ("name", "base_color", "metallic", "emissive_strength", "emissive")}
                      for m in contents["materials"]],
        "preview": hero["path"],
        "preview_front": front["path"],
        "render_seconds": [hero["seconds"], front["seconds"]],
        "blend": blend["path"],
        "reimported_objects": names,
        "checks": checks,
    }


def main() -> int:
    """Start the server, build the lantern, print the report, and return 0 when every check passed."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default=str(Path(__file__).with_name("out")), help="where to write the files")
    parser.add_argument("--server", default=None,
                        help="server command (default blenderlens-mcp on PATH, else python -m blenderlens_mcp)")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.server:
        command = args.server.split()
    elif shutil.which("blenderlens-mcp"):
        command = [shutil.which("blenderlens-mcp")]
    else:
        command = [sys.executable, "-m", "blenderlens_mcp"]
    env = dict(os.environ, BLENDERLENS_MODE="background", BLENDERLENS_RENDER_DIR=str(out))
    client = Client(command, env)
    try:
        report = build(client, out)
    finally:
        client.close()
    report["command"] = command
    print(json.dumps(report, indent=1))
    return 0 if all(report["checks"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
