"""Headless command-line front end — the engine with no AI in the loop.

cabineteer's primary interface is the MCP server (an AI assistant calls its
tools). This CLI is the other door: it drives the *same* tool handlers the
server and the eval harness use — ``server.TOOL_DISPATCH`` — straight from a
shell, so you can list the catalogue, evaluate a saved project, and generate
cut sheets, a 3D viewer, or assembly docs without a chat window or an API key.

    cabineteer-cli list-tools
    cabineteer-cli projects
    cabineteer-cli evaluate dining-sideboards-v2-hardwood
    cabineteer-cli cutlist   dining-sideboards-v2-hardwood --sheet-length 2453 --sheet-width 1234
    cabineteer-cli visualize dining-sideboards-v2-hardwood --finish rift_white_oak

Every friendly subcommand is a thin wrapper that builds an argument dict and
calls one tool. `run` is the escape hatch to any of the 30 tools by name:

    cabineteer-cli run apply_preset --arg name=kitchen_base_3_drawer
    cabineteer-cli run generate_project_cutlist --json '{"project_name": "hall-tree"}'

Output is the tool's JSON, pretty-printed. A tool error prints to stderr and
exits non-zero, so the CLI composes in scripts and Makefiles.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from cabineteer import server


def _run_tool(name: str, args: dict[str, Any], *, drop_none: bool = True) -> int:
    """Call one tool through the server's canonical path; print and return exit code.

    ``drop_none`` strips keys whose value is None so an unset friendly-wrapper
    option (e.g. ``--finish`` not passed) falls back to the tool default. The
    generic ``run`` path passes ``drop_none=False`` so an *explicit* JSON null
    reaches the handler — several tools (e.g. update_project) use null to mean
    "clear this key", which dropping it would silently defeat.
    """
    if name not in server.TOOL_DISPATCH:
        avail = ", ".join(sorted(server.TOOL_DISPATCH))
        print(f"Unknown tool: {name}\nAvailable: {avail}", file=sys.stderr)
        return 2
    if drop_none:
        args = {k: v for k, v in args.items() if v is not None}
    blocks = asyncio.run(server.call_tool(name, args))
    text = "\n".join(b.text for b in blocks)

    if text.startswith(server.ERROR_PREFIX):
        print(text, file=sys.stderr)
        return 1
    # Pretty-print JSON payloads; pass other text through untouched.
    try:
        print(json.dumps(json.loads(text), indent=2))
    except (json.JSONDecodeError, ValueError):
        print(text)
    return 0


def _parse_kv(pairs: list[str] | None) -> dict[str, Any]:
    """`--arg key=value`, value parsed as JSON when possible, else a bare string."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--arg expects key=value, got: {pair!r}")
        key, _, raw = pair.partition("=")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def _cmd_list_tools(_: argparse.Namespace) -> int:
    tools = asyncio.run(server.list_tools())
    width = max(len(t.name) for t in tools)
    for t in sorted(tools, key=lambda x: x.name):
        first_line = (t.description or "").strip().splitlines()[0] if t.description else ""
        print(f"  {t.name:<{width}}  {first_line}")
    print(f"\n{len(tools)} tools. Call any of them with: cabineteer-cli run <tool> --arg k=v", file=sys.stderr)
    return 0


def _cmd_run(ns: argparse.Namespace) -> int:
    args = _parse_kv(ns.arg)
    if ns.json_file:
        args.update(json.loads(Path(ns.json_file).read_text(encoding="utf-8")))
    if ns.json:
        args.update(json.loads(ns.json))
    # drop_none=False: an explicit null in --json is a real value (clears a key).
    return _run_tool(ns.tool, args, drop_none=False)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cabineteer-cli",
        description="Drive the cabineteer engine from a shell — no AI, no MCP client.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # ── catalogue / read-only ────────────────────────────────────────────────
    sp = sub.add_parser("list-tools", help="list every tool the engine exposes")
    sp.set_defaults(func=_cmd_list_tools)

    sp = sub.add_parser("presets", help="list starting-point presets")
    sp.add_argument("--category")
    sp.add_argument("--tag")
    sp.set_defaults(func=lambda ns: _run_tool(
        "list_presets", {"category": ns.category, "tag": ns.tag}))

    sp = sub.add_parser("projects", help="list saved projects")
    sp.add_argument("--query")
    sp.add_argument("--sort")
    sp.add_argument("--all", dest="include_all", action="store_true", default=None,
                    help="include dev-artifact names")
    sp.set_defaults(func=lambda ns: _run_tool(
        "list_projects", {"query": ns.query, "sort": ns.sort, "include_all": ns.include_all}))

    sp = sub.add_parser("hardware", help="list slides / hinges / pulls / legs")
    sp.add_argument("--category")
    sp.add_argument("--brand")
    sp.add_argument("--mount-style", dest="mount_style")
    sp.set_defaults(func=lambda ns: _run_tool(
        "list_hardware",
        {"category": ns.category, "brand": ns.brand, "mount_style": ns.mount_style}))

    # ── produce paperwork from a saved project (zero AI) ─────────────────────
    sp = sub.add_parser("evaluate", help="evaluate a saved project (errors/warnings)")
    sp.add_argument("project_name")
    sp.set_defaults(func=lambda ns: _run_tool(
        "evaluate_project", {"project_name": ns.project_name}))

    sp = sub.add_parser("cutlist", help="generate a project cutlist (files under ~/.cabineteer/cutlists)")
    sp.add_argument("project_name")
    sp.add_argument("--sheet-length", dest="sheet_length", type=float)
    sp.add_argument("--sheet-width", dest="sheet_width", type=float)
    sp.add_argument("--kerf", type=float)
    sp.add_argument("--optimizer")
    sp.add_argument("--format")
    sp.add_argument("--paper", choices=["letter", "a4"])
    sp.set_defaults(func=lambda ns: _run_tool("generate_project_cutlist", {
        "project_name": ns.project_name, "sheet_length": ns.sheet_length,
        "sheet_width": ns.sheet_width, "kerf": ns.kerf, "optimizer": ns.optimizer,
        "format": ns.format, "paper": ns.paper}))

    sp = sub.add_parser("visualize", help="generate a self-contained 3D viewer HTML")
    sp.add_argument("project_name")
    sp.add_argument("--finish")
    sp.add_argument("--drawer-box-finish", dest="drawer_box_finish")
    sp.add_argument("--grain-direction", dest="grain_direction",
                    choices=["vertical", "horizontal"])
    # --furniture-top is a DEPRECATED boolean alias for
    # (--face-top-style, --face-bottom-style); prefer the two style flags.
    sp.add_argument("--furniture-top", dest="furniture_top", action="store_true", default=None)
    sp.add_argument("--face-top-style", dest="face_top_style",
                    choices=["plain", "cap", "flush"], default=None)
    sp.add_argument("--face-bottom-style", dest="face_bottom_style",
                    choices=["plain", "flush"], default=None)
    sp.add_argument("--manga", action="store_true", default=None)
    sp.set_defaults(func=lambda ns: _run_tool("visualize_project", {
        "project_name": ns.project_name, "finish": ns.finish,
        "drawer_box_finish": ns.drawer_box_finish, "grain_direction": ns.grain_direction,
        "furniture_top": ns.furniture_top, "face_top_style": ns.face_top_style,
        "face_bottom_style": ns.face_bottom_style,
        "manga": ns.manga, "open_browser": False}))

    sp = sub.add_parser("assembly", help="generate carcass assembly instructions")
    sp.add_argument("project_name")
    sp.add_argument("--format")
    sp.add_argument("--paper", choices=["letter", "a4"])
    sp.set_defaults(func=lambda ns: _run_tool("generate_assembly_instructions", {
        "project_name": ns.project_name, "format": ns.format, "paper": ns.paper}))

    # ── generic escape hatch to any tool ─────────────────────────────────────
    sp = sub.add_parser("run", help="call any tool by name with raw args")
    sp.add_argument("tool", help="tool name (see: cabineteer-cli list-tools)")
    sp.add_argument("--arg", action="append", metavar="KEY=VALUE",
                    help="repeatable; VALUE parsed as JSON when it can be "
                         "(so 123/true become int/bool) — use --json for a "
                         "value that must stay a string like \"123\"")
    sp.add_argument("--json", metavar="JSON", help="args as a JSON object string")
    sp.add_argument("--json-file", metavar="PATH", help="args as a JSON object file")
    sp.set_defaults(func=_cmd_run)

    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())
