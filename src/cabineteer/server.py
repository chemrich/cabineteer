"""
MCP server for the cabinet-design toolkit.

Exposes the parametric design, evaluation, and cutlist workflows as MCP tools
so that Claude, Gemini CLI, or any MCP-compatible client can design cabinets
conversationally.

Transports
----------
stdio (default)
    The host process (Claude Desktop, Gemini CLI, …) launches ``cabineteer``
    as a subprocess and communicates over stdin/stdout.  No port is used; no
    port conflicts are possible.  This is the right choice for Claude Desktop
    and Gemini CLI.

HTTP/SSE  (``--http``)
    Runs an HTTP server (Starlette + uvicorn) with a Server-Sent Events
    endpoint at ``GET /sse`` and a POST endpoint at ``/messages/``.  Use this
    when you need a persistent process that multiple clients can reach, or when
    the host does not support stdio transport.

    Port selection
    ~~~~~~~~~~~~~~
    Default starting port is **3749** (distinctive enough to avoid accidental
    collisions with common dev servers).  If that port is already bound, the
    server tries 3750, 3751, … up to ``--max-port-attempts`` tries.  The
    resolved port is:
    - printed to **stderr** on startup
    - written to ``~/.cabineteer/cabineteer.port`` so scripts / other tools
      can discover it without parsing log output
    - removed from ``~/.cabineteer/cabineteer.port`` on clean exit

Entry point: ``cabineteer`` (defined in pyproject.toml [project.scripts]).

Usage::

    # stdio (default) — Claude Desktop / Gemini CLI
    cabineteer

    # HTTP/SSE on default port (3749, auto-increments on collision)
    cabineteer --http

    # HTTP/SSE on a specific starting port
    cabineteer --http --port 4000

    # Via uv without installing
    uv run cabineteer --http --port 4000
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import socket
import sys
import textwrap
from pathlib import Path
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from .cabinet import CabinetConfig, ColumnConfig, build_multi_bay_cabinet as _build_multi_bay_cabinet
from .auto_fix import auto_fix_cabinet as _auto_fix, AutoFixResult, fixable_checks
from .paths import data_dir
from .proportions import (
    graduated_drawer_heights as _grad_heights,
    column_widths as _col_widths,
    RATIO_PRESETS as _RATIO_PRESETS,
    _PRESET_DESCRIPTIONS,
    _mm_to_inches_str,
)
from .describe import describe_design as _describe_design
from .presets import PRESETS, get_preset, list_presets as _list_presets
from .furniture_refs import identify_furniture, get_furniture, SYNONYM_TO_PRESETS
from .visualize import (
    WOOD_FINISHES as _WOOD_FINISHES,
    build_and_visualize as _build_and_visualize,
    visualize_assembly as _visualize_assembly,
)
from .cutlist import (
    assign_part_ids,
    CutlistPanel,
    SheetStock,
    consolidate_bom,
    generate_sheet_layout_html,
    generate_sheet_layout_pdf,
    hardware_bom_for_cabinet_config,
    to_hardware_json,
    optimize_cutlist,
    to_csv,
    to_json,
    _OPCUT_AVAILABLE,
    _RECTPACK_AVAILABLE,
)
from .cabinet import (
    OpeningConfig,
    PartInfo,
    INNER_FACE_OVERLAY_MM,
    back_capture_geometry,
    build_cabinet_config as _build_cabinet_config,
    bays_from_config as _bays_from_config,
    carcass_panel_dims as _carcass_panel_dims,
    face_layout as _face_layout,
    openings_span as _openings_span,
    face_placements as _face_placements,
    stack_from_column as _stack_from_column,
    to_opening as _to_opening,
)
from .door import DoorConfig
from .drawer import DrawerConfig, box_config_for_opening as _box_config_for_opening
from .evaluation import Issue, Severity, evaluate_cabinet
from .hardware import (HINGES, SLIDES, LEGS, PULLS, ClearanceReference, OverlayType,
                       LegPattern, get_leg, get_pull, price_for)
from .joinery import (
    CarcassJoinery,
    DrawerJoineryStyle,
    DEFAULT_DOMINO,
    DEFAULT_POCKET_SCREW,
    DEFAULT_BISCUIT,
    DEFAULT_DOWEL,
    DOMINO_SIZES,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────

# Output-file names (cutlists, visualizations, inline-project names) become
# filename stems on disk.  Restrict them so a supplied name can never contain
# a path separator or traverse out of the intended output directory — the same
# guarantee ``project.project_path`` gives persisted project files.
_SAFE_STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")
_MAX_STEM_LEN = 100


def _safe_stem(name: str, *, kind: str = "name") -> str:
    """Validate ``name`` as a safe filename stem, returning it unchanged.

    Rejects path separators, ``..`` traversal, empty/leading-dot names, and
    over-long names.  Raises ``ValueError`` (surfaced to the client as a plain
    input-validation error, not a traceback) on anything unsafe.
    """
    name = str(name)
    if (
        not name
        or len(name) > _MAX_STEM_LEN
        or ".." in name
        or "/" in name
        or "\\" in name
        or not _SAFE_STEM_RE.match(name)
    ):
        raise ValueError(
            f"Invalid {kind} {name!r}: use letters, digits, spaces, '.', '_' "
            f"or '-' (must start with a letter or digit; max {_MAX_STEM_LEN} chars)."
        )
    return name


def _ok(data: Any) -> list[types.TextContent]:
    """Return a JSON success response."""
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


# Failure sentinel shared with the CLI, which keys its exit code on this prefix.
# Keep _err the single place the wire format is spelled, so the two can't drift.
ERROR_PREFIX = "ERROR: "


def _err(msg: str) -> list[types.TextContent]:
    """Return a plain-text error response."""
    return [types.TextContent(type="text", text=f"{ERROR_PREFIX}{msg}")]


def _issues_to_dicts(issues: list[Issue]) -> list[dict]:
    return [
        {
            "severity": i.severity.value,
            "check": i.check,
            "message": i.message,
            "part_a": i.part_a,
            "part_b": i.part_b,
            "value": i.value,
            "limit": i.limit,
        }
        for i in issues
    ]


def _sort_drawer_config(dc: list) -> list:
    """Sort drawer openings largest-first (bottom); non-drawer openings stay at the end."""
    if not dc:
        return dc

    def _type(row):
        if isinstance(row, OpeningConfig):
            return row.opening_type
        if isinstance(row, dict):
            return str(row.get("opening_type", row.get("slot_type", "open")))
        return str(row[1])

    def _height(row):
        if isinstance(row, OpeningConfig):
            return row.height_mm
        if isinstance(row, dict):
            return float(row["height_mm"])
        return float(row[0])

    types_set = {_type(r) for r in dc}
    if len(types_set) == 1:
        return sorted(dc, key=_height, reverse=True)
    drawers = sorted([r for r in dc if _type(r) == "drawer"], key=_height, reverse=True)
    others  = [r for r in dc if _type(r) != "drawer"]
    return drawers + others


def _build_door_config(args: dict) -> DoorConfig:
    return DoorConfig(**args)


def _build_drawer_config(args: dict) -> DrawerConfig:
    kwargs: dict[str, Any] = {}
    for key, value in args.items():
        if key == "joinery_style" and isinstance(value, str):
            kwargs[key] = DrawerJoineryStyle(value)
        else:
            kwargs[key] = value
    return DrawerConfig(**kwargs)


def _raw_box_height(cfg: DrawerConfig) -> float:
    """Compute the clearance-adjusted height without standard snapping."""
    return cfg.opening_height - cfg.slide.min_bottom_clearance - cfg.vertical_gap


def _pull_placements_to_dicts(placements) -> list[dict]:
    """Convert PullPlacement objects to JSON-friendly dicts."""
    return [
        {
            "pull_key":     pl.pull_key,
            "center_xz_mm": [pl.center[0], pl.center[1]],
            "hole_coords_xz_mm": [list(hc) for hc in pl.hole_coords],
        }
        for pl in placements
    ]


def _hardware_line_to_dict(line) -> dict:
    """Convert a HardwareLine to a JSON-friendly dict including derived fields."""
    return {
        "sku":            line.sku,
        "category":       line.category,
        "name":           line.name,
        "brand":          line.brand,
        "model_number":   line.model_number,
        "pieces_needed":  line.pieces_needed,
        "pack_quantity":  line.pack_quantity,
        "packs_to_order": line.packs_to_order,
        "pieces_ordered": line.pieces_ordered,
        "leftover":       line.leftover,
        "notes":          line.notes,
    }


# ─── Server ───────────────────────────────────────────────────────────────────

server = Server("cabineteer")


def _manga_schema() -> dict:
    """Input schema for the manga scale-reference toggle (visualize tools)."""
    return {
        "type": "boolean",
        "default": False,
        "description": (
            "Add a manga scale-reference stack (5 tankōbon volumes, "
            "112.5×176×15 mm each) in the front-left corner of every drawer "
            "box. The viewer's M key / side-panel button cycles how many are "
            "shown (1…5, hidden). Errors if any drawer interior can't hold "
            "the full 5-volume stack."
        ),
    }


def _worktop_schema() -> dict:
    """Input schema for a project worktop block (design_project / update_project)."""
    return {
        "type": "object",
        "description": (
            "Desk/counter slab spanning part of the run. Positioned in run "
            "coordinates: x_offset_mm is the slab's left edge measured from "
            "the left face of the first cabinet at whatever gap_mm the run "
            "is rendered with. surface_height_mm is the finished top-of-slab "
            "height above the FLOOR (cabinet feet included)."
        ),
        "properties": {
            "width_mm":          {"type": "number"},
            "depth_mm":          {"type": "number"},
            "thickness_mm":      {"type": "number", "default": 19},
            "surface_height_mm": {"type": "number", "default": 736.6,
                                  "description": "Finished slab-top height above the floor. Default 736.6 (29in desk height)."},
            "x_offset_mm":       {"type": "number", "default": 0},
            "y_offset_mm":       {"type": "number", "default": 0,
                                  "description": "Front-edge shift from the cabinet fronts; negative pushes the slab proud (e.g. -18 lands on the drawer-face plane)."},
            "leg_count":         {"type": "integer", "default": 0,
                                  "description": "Support legs rendered floor-to-slab: 4 = corners, 2 = front corners only (rear on cleats), 0 = none. leg_placement left_end/right_end always renders 2."},
            "leg_diameter_mm":   {"type": "number", "default": 50},
            "leg_inset_mm":      {"type": "number", "default": 60},
            "leg_placement":     {"type": "string", "default": "corners",
                                  "enum": ["corners", "left_end", "right_end"],
                                  "description": "Where the legs go. corners = leg_count spread over the corners; left_end/right_end = 2 legs (front + rear) at that end of the slab — single-pedestal desks whose other end sits on a cabinet."},
            "material":          {"type": "string", "default": "finished_wood"},
        },
        "required": ["width_mm", "depth_mm"],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Tool: list_hardware
# ──────────────────────────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_hardware",
            description=(
                "Return the catalogue of available drawer slides, hinges, legs, and pulls. "
                "Use this to discover valid key strings for other tools."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["slides", "hinges", "legs", "pulls", "all"],
                        "description": "Which hardware category to list.",
                        "default": "all",
                    },
                    "brand": {
                        "type": "string",
                        "description": (
                            "Optional case-insensitive brand filter for pulls "
                            "(e.g. 'IKEA', 'Top Knobs'). Ignored for other categories."
                        ),
                    },
                    "mount_style": {
                        "type": "string",
                        "enum": ["surface", "edge", "flush", "knob"],
                        "description": (
                            "Optional mount-style filter for pulls. "
                            "Ignored for other categories."
                        ),
                    },
                },
            },
        ),
        types.Tool(
            name="list_joinery_options",
            description=(
                "Return available joinery styles for drawer boxes and carcass construction, "
                "including Festool Domino tenon sizes."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="design_cabinet",
            description=textwrap.dedent("""\
                Define or update a cabinet configuration and return a summary of its
                parametric layout — panel sizes, opening stack, hardware, joinery — without
                running CadQuery geometry.

                Required: width, height, depth (all in mm).

                drawer_config is a list of [height_mm, slot_type] pairs stacked from
                bottom to top. slot_type options: "drawer", "door", "door_pair",
                "shelf", "open". A row may carry an optional third element — a
                per-opening options dict (e.g. [273, "drawer",
                {"bottom_thickness": 6, "slide_key": "blum_movento_769"}])
                overriding bottom_thickness, slide_key, pull_key, hinge_key,
                hinge_side, num_doors, or door_thickness.

                Drawer bottoms default by size: boxes taller than 5" and at
                least 16" wide get 12 mm (1/2") bottoms, everything else 6 mm
                (1/4"). Use the per-opening override to force either.

                carcass_joinery options: "dado_rabbet", "floating_tenon",
                "pocket_screw", "biscuit", "dowel".

                ── WORKFLOW ──
                After calling this tool you MUST immediately call evaluate_cabinet
                on the returned config before doing anything else. If errors are
                found, call auto_fix_cabinet, then re-evaluate.  Once all errors
                are resolved, call describe_design and present the prose summary
                to the user.  Do NOT call visualize_cabinet until the user has
                reviewed the description and explicitly approved.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "width":  {"type": "number", "description": "Exterior width in mm."},
                    "height": {"type": "number", "description": "Exterior height in mm."},
                    "depth":  {"type": "number", "description": "Exterior depth in mm."},
                    "side_thickness":   {"type": "number", "default": 18.0},
                    "bottom_thickness": {"type": "number", "default": 18.0},
                    "top_thickness":    {"type": "number", "default": 18.0},
                    "shelf_thickness":  {"type": "number", "default": 18.0},
                    "back_thickness":   {"type": "number", "default": 6.0},
                    "drawer_box_thickness": {
                        "type": "number", "default": 15.0,
                        "description": "Drawer box stock (sides + sub-front/back) in mm; bottoms follow the per-drawer size rule.",
                    },
                    "drawer_box_prefinished": {
                        "type": "boolean", "default": False,
                        "description": "Build drawer boxes (incl. bottoms) from pre-finished Baltic birch — separate cutlist material and sheet pricing, no finishing step. Workshop presets default true.",
                    },
                    "face_material": {
                        "type": "string",
                        "default": "finished_wood",
                        "description": (
                            "Cutlist material for show faces (false fronts + door "
                            "leaves). 'baltic_birch' / 'baltic_birch_prefinished' "
                            "pool into sheet optimisation; any other string stays "
                            "a labeled order-out group (e.g. 'rift_white_oak_ply')."
                        ),
                    },
                    "carcass_material": {
                        "type": "string",
                        "default": "baltic_birch",
                        "description": (
                            "Cutlist material for carcass panels (sides, top, "
                            "bottom, shelves, dividers). Sheet materials (Baltic "
                            "birch stocks or names ending '_ply') pack per "
                            "(material, thickness) and price from PRICE_LIST when "
                            "listed. Backs and drawer boxes keep their own stock."
                        ),
                    },
                    "edge_band_mode": {
                        "type": "string",
                        "enum": ["none", "hot_melt", "hardwood"],
                        "default": "none",
                        "description": (
                            "Edge banding for exposed ply edges (fronts of "
                            "carcass panels + all four edges of faces/doors). "
                            "'hot_melt' = iron-on veneer rolls, dims "
                            "unchanged; 'hardwood' = solid strips, panel "
                            "CORES cut smaller so finished dims hold. "
                            "Also a SharedDesign token."
                        ),
                    },
                    "edge_band_thickness_mm": {
                        "type": "number",
                        "default": 0.6,
                        "description": (
                            "Band thickness in mm (hardwood: 3.2–6.4 "
                            "typical; hot-melt ~0.6)."
                        ),
                    },
                    "edge_band_material": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Banding species; empty derives from each "
                            "panel's material (rift white oak ply → "
                            "white_oak rolls, Baltic birch → white_birch)."
                        ),
                    },
                    "edge_band_stock": {
                        "type": ["object", "null"],
                        "default": None,
                        "description": (
                            "Purchasable hardwood banding stock "
                            "{width_mm, length_mm, price_usd, "
                            "strip_width_mm (default 20)}. Board thickness "
                            "IS edge_band_thickness_mm. When set, the BOM "
                            "band line is priced in boards-to-order via "
                            "real piece-into-strip packing (flags edges "
                            "with no flush-trim overhang or longer than "
                            "the stock). Omit for the unpriced "
                            "rip-from-offcuts line. SharedDesign token."
                        ),
                    },
                    "face_gap_mm": {
                        "type": "number", "default": 4.0,
                        "description": (
                            "Vertical reveal between stacked show faces (mm, "
                            "total per joint) — sets false-front heights on "
                            "cutlists and renders alike. SharedDesign token."
                        ),
                    },
                    "furniture_top": {
                        "type": "boolean", "default": False,
                        "description": (
                            "Stored 'furniture top' style: the top panel "
                            "gains a front cap strip flush with the face "
                            "plane (emitted on cutlists) and the lowest face "
                            "drops to the carcass underside. SharedDesign "
                            "token."
                        ),
                    },
                    "carcass_corner_style": {
                        "type": "string",
                        "enum": ["butt", "miter"],
                        "default": "butt",
                        "description": (
                            "'miter': the four exterior corners are 45° "
                            "miters — top/bottom cut to FULL exterior "
                            "width (long-point dims), all four panels "
                            "beveled; divider/shelf joints stay butt "
                            "tenons. Floating-tenon carcasses only. Also "
                            "a SharedDesign token."
                        ),
                    },
                    "back_style": {
                        "type": "string",
                        "enum": ["full_height", "under_top"],
                        "default": "full_height",
                        "description": (
                            "'under_top': the top panel is cut to FULL "
                            "depth (rear edge flush with the sides) and "
                            "the back stops at its underside — no back "
                            "edge visible from above or the sides. Butt "
                            "corners + tenon/screw/biscuit/dowel joinery "
                            "only. Also a SharedDesign token."
                        ),
                    },
                    "back_capture": {
                        "type": "string",
                        "enum": ["pocket", "rabbet", "half_lap", "dado"],
                        "default": "pocket",
                        "description": (
                            "How the back is HELD — independent of "
                            "back_style and carcass_joinery. 'pocket': the "
                            "legacy let-in back, glued onto the rear edges "
                            "of the interior panels, nothing machined. "
                            "'rabbet': a rabbet in the rear inner edge of "
                            "the sides, top and bottom; the back drops in "
                            "from behind after glue-up and finishes flush. "
                            "'half_lap': both halves rabbeted so they lap — "
                            "twice the glue area and the panel is trapped "
                            "fore-and-aft by the step (needs a back at least "
                            "9 mm thick). 'dado': a groove in all four "
                            "members; the back SLIDES IN during glue-up and "
                            "is trapped on four edges, so nothing shows from "
                            "any angle and a solid-wood back can float and "
                            "move. The three machined captures seat the back "
                            "inside the case perimeter, so back_style only "
                            "changes what you see under 'pocket'."
                        ),
                    },
                    "back_groove_setback": {
                        "type": "number",
                        "default": 12.0,
                        "description": (
                            "back_capture 'dado' only: material left "
                            "standing behind the groove, i.e. how far the "
                            "back sits forward of the case rear (mm)."
                        ),
                    },
                    "drawer_config": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "prefixItems": [
                                {"type": "number", "description": "Opening height in mm"},
                                {"type": "string", "description": "Slot type"},
                                {"type": "object", "description": (
                                    "Optional per-opening overrides, e.g. "
                                    "{\"bottom_thickness\": 12} for a 1/2\" drawer bottom."
                                )},
                            ],
                            "minItems": 2,
                            "maxItems": 3,
                        },
                        "description": (
                            "Stack of [height_mm, slot_type] pairs from bottom up. "
                            "An optional third element per row is a per-opening "
                            "options dict (bottom_thickness, slide_key, pull_key, "
                            "hinge_key, hinge_side, num_doors, door_thickness)."
                        ),
                        "default": [],
                    },
                    "carcass_joinery": {
                        "type": "string",
                        "enum": ["dado_rabbet", "floating_tenon", "pocket_screw", "biscuit", "dowel"],
                        "default": "floating_tenon",
                    },
                    "drawer_joinery": {
                        "type": "string",
                        "enum": ["butt", "qqq", "half_lap", "drawer_lock"],
                        "default": "half_lap",
                        "description": "Drawer box corner joint style.",
                    },
                    "drawer_corner_lip_mm": {
                        "type": "number",
                        "description": (
                            "drawer_lock only: the wall the router bit leaves "
                            "outboard of the socket, per corner (mm). Box "
                            "sides are cut 2x this short of the box depth. "
                            "Measure it on a test corner; omitting it takes "
                            "the catalogue nominal (1/8 in)."
                        ),
                    },
                    "adj_shelf_holes": {"type": "boolean", "default": False},
                    "door_hinge": {
                        "type": "string",
                        "description": "Hinge key from list_hardware. Default: blum_clip_top_blumotion_110_full.",
                        "default": "blum_clip_top_blumotion_110_full",
                    },
                    "num_drawers": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Number of drawer openings. When provided without an explicit "
                            "drawer_config, heights are auto-computed from drawer_proportion."
                        ),
                    },
                    "drawer_proportion": {
                        "type": "string",
                        "enum": ["equal", "subtle", "classic", "golden"],
                        "description": (
                            "Height-graduation preset for auto-computed drawer stacks. "
                            "equal=uniform, subtle=1.2×, classic=1.4×, golden=1.618× (φ). "
                            "Default: classic."
                        ),
                    },
                    "drawer_pull": {
                        "type": "string",
                        "description": "Pull catalog key from list_hardware (category='pulls').",
                    },
                    "door_pull": {
                        "type": "string",
                        "description": "Pull catalog key applied to every door / door_pair slot.",
                    },
                    "pull_preset": {
                        "type": "string",
                        "description": "Named pull preset key (see list_pull_presets). Populates drawer_pull, door_pull, and orientation. Explicit drawer_pull/door_pull fields override the preset.",
                    },
                },
                "required": ["width", "height", "depth"],
            },
        ),
        types.Tool(
            name="design_multi_column_cabinet",
            description=textwrap.dedent("""\
                Design a cabinet with multiple side-by-side vertical columns sharing
                a common carcass — e.g. a left column of drawers next to a right
                column with a door, all within one set of exterior panels.

                Columns are separated by interior vertical dividers (same thickness as
                ``side_thickness``).  Column widths are interior measurements and must
                sum to the cabinet's interior_width (= width − 2 × side_thickness).

                Each column has its own ``drawer_config`` stack (same format as
                design_cabinet: list of [height_mm, slot_type] pairs, each row
                optionally carrying a third per-opening options dict, e.g.
                [273, "drawer", {"bottom_thickness": 6}]).

                ── PROPORTION SHORTCUTS ──
                Instead of spelling out every column width and drawer height, you can
                let the tool compute proportional layouts automatically:

                  • column_proportion + num_columns [+ wide_index]: auto-computes
                    column widths using a named graduation preset.  wide_index marks
                    the accent column (0-based); omit for equal widths.

                  • drawer_proportion + num_drawers: auto-computes drawer heights
                    within each column using a geometric graduation preset.

                Presets: "equal" (uniform), "subtle" (1.2×), "classic" (1.4×),
                "golden" (1.618× / φ).

                Returns the same summary as design_cabinet plus:
                  - columns: per-column breakdown (interior width, slot stack, divider x)
                  - column_widths_sum_mm / interior_width_mm: for quick sanity check
                  - proportions_used: which presets were applied (if any)

                ── WORKFLOW ──
                After calling this tool you MUST call evaluate_cabinet on the returned
                config (using width/height/depth plus the columns array) before
                presenting results.  The evaluator will flag any column-width errors.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "width":  {"type": "number", "description": "Exterior width in mm."},
                    "height": {"type": "number", "description": "Exterior height in mm."},
                    "depth":  {"type": "number", "description": "Exterior depth in mm."},
                    "columns": {
                        "type": "array",
                        "description": (
                            "List of column definitions, left to right. "
                            "Interior widths must sum to (width - 2 × side_thickness)."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "width_mm": {
                                    "type": "number",
                                    "description": "Interior width of this column in mm.",
                                },
                                "drawer_config": {
                                    "type": "array",
                                    "description": (
                                        "Stack of [height_mm, slot_type] pairs bottom-to-top; "
                                        "optional third element per row is a per-opening options dict."
                                    ),
                                    "items": {
                                        "type": "array",
                                        "prefixItems": [
                                            {"type": "number"},
                                            {"type": "string"},
                                            {"type": "object"},
                                        ],
                                        "minItems": 2,
                                        "maxItems": 3,
                                    },
                                    "default": [],
                                },
                            },
                            "required": ["width_mm"],
                        },
                        "minItems": 2,
                    },
                    "side_thickness":   {"type": "number", "default": 18.0},
                    "bottom_thickness": {"type": "number", "default": 18.0},
                    "top_thickness":    {"type": "number", "default": 18.0},
                    "shelf_thickness":  {"type": "number", "default": 18.0},
                    "back_thickness":   {"type": "number", "default": 6.0},
                    "drawer_box_thickness": {
                        "type": "number", "default": 15.0,
                        "description": "Drawer box stock (sides + sub-front/back) in mm; bottoms follow the per-drawer size rule.",
                    },
                    "drawer_box_prefinished": {
                        "type": "boolean", "default": False,
                        "description": "Build drawer boxes (incl. bottoms) from pre-finished Baltic birch — separate cutlist material and sheet pricing, no finishing step. Workshop presets default true.",
                    },
                    "face_material": {
                        "type": "string",
                        "default": "finished_wood",
                        "description": (
                            "Cutlist material for show faces (false fronts + door "
                            "leaves). 'baltic_birch' / 'baltic_birch_prefinished' "
                            "pool into sheet optimisation; any other string stays "
                            "a labeled order-out group (e.g. 'rift_white_oak_ply')."
                        ),
                    },
                    "carcass_material": {
                        "type": "string",
                        "default": "baltic_birch",
                        "description": (
                            "Cutlist material for carcass panels (sides, top, "
                            "bottom, shelves, dividers). Sheet materials (Baltic "
                            "birch stocks or names ending '_ply') pack per "
                            "(material, thickness) and price from PRICE_LIST when "
                            "listed. Backs and drawer boxes keep their own stock."
                        ),
                    },
                    "edge_band_mode": {
                        "type": "string",
                        "enum": ["none", "hot_melt", "hardwood"],
                        "default": "none",
                        "description": (
                            "Edge banding for exposed ply edges (fronts of "
                            "carcass panels + all four edges of faces/doors). "
                            "'hot_melt' = iron-on veneer rolls, dims "
                            "unchanged; 'hardwood' = solid strips, panel "
                            "CORES cut smaller so finished dims hold. "
                            "Also a SharedDesign token."
                        ),
                    },
                    "edge_band_thickness_mm": {
                        "type": "number",
                        "default": 0.6,
                        "description": (
                            "Band thickness in mm (hardwood: 3.2–6.4 "
                            "typical; hot-melt ~0.6)."
                        ),
                    },
                    "edge_band_material": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Banding species; empty derives from each "
                            "panel's material (rift white oak ply → "
                            "white_oak rolls, Baltic birch → white_birch)."
                        ),
                    },
                    "edge_band_stock": {
                        "type": ["object", "null"],
                        "default": None,
                        "description": (
                            "Purchasable hardwood banding stock "
                            "{width_mm, length_mm, price_usd, "
                            "strip_width_mm (default 20)}. Board thickness "
                            "IS edge_band_thickness_mm. When set, the BOM "
                            "band line is priced in boards-to-order via "
                            "real piece-into-strip packing (flags edges "
                            "with no flush-trim overhang or longer than "
                            "the stock). Omit for the unpriced "
                            "rip-from-offcuts line. SharedDesign token."
                        ),
                    },
                    "face_gap_mm": {
                        "type": "number", "default": 4.0,
                        "description": (
                            "Vertical reveal between stacked show faces (mm, "
                            "total per joint) — sets false-front heights on "
                            "cutlists and renders alike. SharedDesign token."
                        ),
                    },
                    "furniture_top": {
                        "type": "boolean", "default": False,
                        "description": (
                            "Stored 'furniture top' style: the top panel "
                            "gains a front cap strip flush with the face "
                            "plane (emitted on cutlists) and the lowest face "
                            "drops to the carcass underside. SharedDesign "
                            "token."
                        ),
                    },
                    "carcass_corner_style": {
                        "type": "string",
                        "enum": ["butt", "miter"],
                        "default": "butt",
                        "description": (
                            "'miter': the four exterior corners are 45° "
                            "miters — top/bottom cut to FULL exterior "
                            "width (long-point dims), all four panels "
                            "beveled; divider/shelf joints stay butt "
                            "tenons. Floating-tenon carcasses only. Also "
                            "a SharedDesign token."
                        ),
                    },
                    "back_style": {
                        "type": "string",
                        "enum": ["full_height", "under_top"],
                        "default": "full_height",
                        "description": (
                            "'under_top': the top panel is cut to FULL "
                            "depth (rear edge flush with the sides) and "
                            "the back stops at its underside — no back "
                            "edge visible from above or the sides. Butt "
                            "corners + tenon/screw/biscuit/dowel joinery "
                            "only. Also a SharedDesign token."
                        ),
                    },
                    "back_capture": {
                        "type": "string",
                        "enum": ["pocket", "rabbet", "half_lap", "dado"],
                        "default": "pocket",
                        "description": (
                            "How the back is HELD — independent of "
                            "back_style and carcass_joinery. 'pocket': the "
                            "legacy let-in back, glued onto the rear edges "
                            "of the interior panels, nothing machined. "
                            "'rabbet': a rabbet in the rear inner edge of "
                            "the sides, top and bottom; the back drops in "
                            "from behind after glue-up and finishes flush. "
                            "'half_lap': both halves rabbeted so they lap — "
                            "twice the glue area and the panel is trapped "
                            "fore-and-aft by the step (needs a back at least "
                            "9 mm thick). 'dado': a groove in all four "
                            "members; the back SLIDES IN during glue-up and "
                            "is trapped on four edges, so nothing shows from "
                            "any angle and a solid-wood back can float and "
                            "move. The three machined captures seat the back "
                            "inside the case perimeter, so back_style only "
                            "changes what you see under 'pocket'."
                        ),
                    },
                    "back_groove_setback": {
                        "type": "number",
                        "default": 12.0,
                        "description": (
                            "back_capture 'dado' only: material left "
                            "standing behind the groove, i.e. how far the "
                            "back sits forward of the case rear (mm)."
                        ),
                    },
                    "carcass_joinery": {
                        "type": "string",
                        "enum": ["dado_rabbet", "floating_tenon", "pocket_screw", "biscuit", "dowel"],
                        "default": "floating_tenon",
                    },
                    "drawer_slide": {
                        "type": "string",
                        "default": "blum_tandem_550h",
                    },
                    "door_hinge": {
                        "type": "string",
                        "default": "blum_clip_top_blumotion_110_full",
                    },
                    "num_columns": {
                        "type": "integer",
                        "minimum": 2,
                        "description": (
                            "Number of columns. Use with column_proportion / wide_index "
                            "as an alternative to supplying an explicit columns array."
                        ),
                    },
                    "wide_index": {
                        "type": "integer",
                        "description": "0-based index of the wider accent column (used with column_proportion).",
                    },
                    "column_proportion": {
                        "type": "string",
                        "enum": ["equal", "subtle", "classic", "golden"],
                        "description": (
                            "Width-graduation preset for auto-computed column widths. "
                            "Requires num_columns. Default: golden."
                        ),
                    },
                    "num_drawers": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Drawers per column when drawer_config is omitted from each column entry."
                        ),
                    },
                    "drawer_proportion": {
                        "type": "string",
                        "enum": ["equal", "subtle", "classic", "golden"],
                        "description": (
                            "Height-graduation preset for auto-computed drawer heights. "
                            "Default: classic."
                        ),
                    },
                    "drawer_pull": {
                        "type": "string",
                        "description": "Pull catalog key from list_hardware (category='pulls').",
                    },
                    "door_pull": {
                        "type": "string",
                        "description": "Pull catalog key applied to every door / door_pair slot.",
                    },
                    "pull_preset": {
                        "type": "string",
                        "description": "Named pull preset key (see list_pull_presets). Populates drawer_pull, door_pull, and orientation. Explicit drawer_pull/door_pull fields override the preset.",
                    },
                },
                "required": ["width", "height", "depth"],
            },
        ),
        types.Tool(
            name="evaluate_cabinet",
            description=textwrap.dedent("""\
                Run the full structural/fit evaluation on a cabinet configuration and return
                a list of issues (errors, warnings, info). Pass the same arguments as
                design_cabinet. Optionally include door_configs (list of DoorConfig dicts)
                to also evaluate door hardware.

                ── WORKFLOW ──
                This tool MUST be called after every call to design_cabinet or
                apply_preset.  If the result contains any severity=error issues,
                call auto_fix_cabinet once with the same config, then re-evaluate.
                If errors persist after auto-fix, describe the remaining errors to
                the user and ask for guidance.  Only proceed to describe_design
                (and eventually visualize_cabinet) when there are zero errors.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "width":  {"type": "number"},
                    "height": {"type": "number"},
                    "depth":  {"type": "number"},
                    "side_thickness":   {"type": "number", "default": 18.0},
                    "bottom_thickness": {"type": "number", "default": 18.0},
                    "top_thickness":    {"type": "number", "default": 18.0},
                    "shelf_thickness":  {"type": "number", "default": 18.0},
                    "back_thickness":   {"type": "number", "default": 6.0},
                    "drawer_box_thickness": {
                        "type": "number", "default": 15.0,
                        "description": "Drawer box stock (sides + sub-front/back) in mm; bottoms follow the per-drawer size rule.",
                    },
                    "drawer_box_prefinished": {
                        "type": "boolean", "default": False,
                        "description": "Build drawer boxes (incl. bottoms) from pre-finished Baltic birch — separate cutlist material and sheet pricing, no finishing step. Workshop presets default true.",
                    },
                    "face_material": {
                        "type": "string",
                        "default": "finished_wood",
                        "description": (
                            "Cutlist material for show faces (false fronts + door "
                            "leaves). 'baltic_birch' / 'baltic_birch_prefinished' "
                            "pool into sheet optimisation; any other string stays "
                            "a labeled order-out group (e.g. 'rift_white_oak_ply')."
                        ),
                    },
                    "carcass_material": {
                        "type": "string",
                        "default": "baltic_birch",
                        "description": (
                            "Cutlist material for carcass panels (sides, top, "
                            "bottom, shelves, dividers). Sheet materials (Baltic "
                            "birch stocks or names ending '_ply') pack per "
                            "(material, thickness) and price from PRICE_LIST when "
                            "listed. Backs and drawer boxes keep their own stock."
                        ),
                    },
                    "edge_band_mode": {
                        "type": "string",
                        "enum": ["none", "hot_melt", "hardwood"],
                        "default": "none",
                        "description": (
                            "Edge banding for exposed ply edges (fronts of "
                            "carcass panels + all four edges of faces/doors). "
                            "'hot_melt' = iron-on veneer rolls, dims "
                            "unchanged; 'hardwood' = solid strips, panel "
                            "CORES cut smaller so finished dims hold. "
                            "Also a SharedDesign token."
                        ),
                    },
                    "edge_band_thickness_mm": {
                        "type": "number",
                        "default": 0.6,
                        "description": (
                            "Band thickness in mm (hardwood: 3.2–6.4 "
                            "typical; hot-melt ~0.6)."
                        ),
                    },
                    "edge_band_material": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Banding species; empty derives from each "
                            "panel's material (rift white oak ply → "
                            "white_oak rolls, Baltic birch → white_birch)."
                        ),
                    },
                    "edge_band_stock": {
                        "type": ["object", "null"],
                        "default": None,
                        "description": (
                            "Purchasable hardwood banding stock "
                            "{width_mm, length_mm, price_usd, "
                            "strip_width_mm (default 20)}. Board thickness "
                            "IS edge_band_thickness_mm. When set, the BOM "
                            "band line is priced in boards-to-order via "
                            "real piece-into-strip packing (flags edges "
                            "with no flush-trim overhang or longer than "
                            "the stock). Omit for the unpriced "
                            "rip-from-offcuts line. SharedDesign token."
                        ),
                    },
                    "face_gap_mm": {
                        "type": "number", "default": 4.0,
                        "description": (
                            "Vertical reveal between stacked show faces (mm, "
                            "total per joint) — sets false-front heights on "
                            "cutlists and renders alike. SharedDesign token."
                        ),
                    },
                    "furniture_top": {
                        "type": "boolean", "default": False,
                        "description": (
                            "Stored 'furniture top' style: the top panel "
                            "gains a front cap strip flush with the face "
                            "plane (emitted on cutlists) and the lowest face "
                            "drops to the carcass underside. SharedDesign "
                            "token."
                        ),
                    },
                    "carcass_corner_style": {
                        "type": "string",
                        "enum": ["butt", "miter"],
                        "default": "butt",
                        "description": (
                            "'miter': the four exterior corners are 45° "
                            "miters — top/bottom cut to FULL exterior "
                            "width (long-point dims), all four panels "
                            "beveled; divider/shelf joints stay butt "
                            "tenons. Floating-tenon carcasses only. Also "
                            "a SharedDesign token."
                        ),
                    },
                    "back_style": {
                        "type": "string",
                        "enum": ["full_height", "under_top"],
                        "default": "full_height",
                        "description": (
                            "'under_top': the top panel is cut to FULL "
                            "depth (rear edge flush with the sides) and "
                            "the back stops at its underside — no back "
                            "edge visible from above or the sides. Butt "
                            "corners + tenon/screw/biscuit/dowel joinery "
                            "only. Also a SharedDesign token."
                        ),
                    },
                    "back_capture": {
                        "type": "string",
                        "enum": ["pocket", "rabbet", "half_lap", "dado"],
                        "default": "pocket",
                        "description": (
                            "How the back is HELD — independent of "
                            "back_style and carcass_joinery. 'pocket': the "
                            "legacy let-in back, glued onto the rear edges "
                            "of the interior panels, nothing machined. "
                            "'rabbet': a rabbet in the rear inner edge of "
                            "the sides, top and bottom; the back drops in "
                            "from behind after glue-up and finishes flush. "
                            "'half_lap': both halves rabbeted so they lap — "
                            "twice the glue area and the panel is trapped "
                            "fore-and-aft by the step (needs a back at least "
                            "9 mm thick). 'dado': a groove in all four "
                            "members; the back SLIDES IN during glue-up and "
                            "is trapped on four edges, so nothing shows from "
                            "any angle and a solid-wood back can float and "
                            "move. The three machined captures seat the back "
                            "inside the case perimeter, so back_style only "
                            "changes what you see under 'pocket'."
                        ),
                    },
                    "back_groove_setback": {
                        "type": "number",
                        "default": 12.0,
                        "description": (
                            "back_capture 'dado' only: material left "
                            "standing behind the groove, i.e. how far the "
                            "back sits forward of the case rear (mm)."
                        ),
                    },
                    "drawer_config": {
                        "type": "array",
                        "items": {"type": "array", "minItems": 2, "maxItems": 3},
                        "default": [],
                    },
                    "carcass_joinery": {
                        "type": "string",
                        "enum": ["dado_rabbet", "floating_tenon", "pocket_screw", "biscuit", "dowel"],
                        "default": "floating_tenon",
                    },
                    "door_hinge": {"type": "string", "default": "blum_clip_top_blumotion_110_full"},
                    "door_configs": {
                        "type": "array",
                        "description": "Optional list of DoorConfig parameter dicts to evaluate.",
                        "items": {"type": "object"},
                        "default": [],
                    },
                },
                "required": ["width", "height", "depth"],
            },
        ),
        types.Tool(
            name="design_door",
            description=textwrap.dedent("""\
                Calculate door dimensions, hinge count, and hinge positions for a cabinet
                opening. Returns the DoorConfig summary.

                overlay_type is determined automatically from the hinge_key you choose.
                Use list_hardware to see valid hinge keys.

                Optional pull hardware: pass pull_key (from list_hardware with
                category="pulls") to get placements, the hardware BOM line, and
                fit checks in the response.  pull_vertical controls where the
                pull sits on the door face ("center", "upper_third", or
                "lower_third") — upper_third for base-cabinet doors, lower_third
                for wall cabinets.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "opening_width":  {"type": "number", "description": "Cabinet opening width in mm."},
                    "opening_height": {"type": "number", "description": "Cabinet opening height in mm."},
                    "num_doors": {
                        "type": "integer",
                        "enum": [1, 2],
                        "description": "1 = single door, 2 = door pair.",
                        "default": 1,
                    },
                    "hinge_key": {
                        "type": "string",
                        "description": "Hinge key from list_hardware.",
                        "default": "blum_clip_top_blumotion_110_full",
                    },
                    "door_thickness": {"type": "number", "default": 18.0},
                    "door_weight_kg": {"type": "number", "default": 0.0},
                    "gap_top":    {"type": "number", "default": 2.0},
                    "gap_bottom": {"type": "number", "default": 2.0},
                    "gap_side":   {"type": "number", "default": 2.0},
                    "gap_between": {"type": "number", "default": 2.0},
                    "pull_key": {
                        "type": "string",
                        "description": (
                            "Pull catalog key from list_hardware (category='pulls'). "
                            "Omit for no pull."
                        ),
                    },
                    "pull_count": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Override auto-selected pull count per door leaf. "
                            "0 = auto (single ≤ 600 mm, dual > 600 mm)."
                        ),
                    },
                    "pull_vertical": {
                        "type": "string",
                        "enum": ["center", "upper_third", "lower_third"],
                        "description": "Vertical placement policy on the door face.",
                        "default": "center",
                    },
                },
                "required": ["opening_width", "opening_height"],
            },
        ),
        types.Tool(
            name="design_drawer",
            description=textwrap.dedent("""\
                Calculate drawer box dimensions and joinery geometry for a given opening.
                Returns DrawerConfig summary including side/front-back dimensions and
                joinery cut specs.

                joinery_style options: "butt", "qqq", "half_lap", "drawer_lock".
                slide_key: use list_hardware to see options (default: blum_tandem_550h).

                By default, box_height_mm is snapped down to the nearest standard
                industry size (3"–12" in 1" steps) so boxes can be batch-ordered.
                Set use_standard_height=false to use the full clearance-adjusted height.
                The response always includes both standard_box_height_mm and the raw
                computed height for reference.

                Optional pull hardware: pass pull_key (from list_hardware with
                category="pulls") to get placements, the hardware BOM line, and
                fit checks in the response.  Omit pull_count (or set to 0) to let
                the tool auto-select single vs dual pulls at the 600 mm face
                threshold.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "opening_width":  {"type": "number", "description": "Cabinet opening width in mm."},
                    "opening_height": {"type": "number", "description": "Available height for drawer in mm."},
                    "opening_depth":  {"type": "number", "description": "Cabinet interior depth in mm."},
                    "slide_key": {
                        "type": "string",
                        "description": "Drawer slide key from list_hardware.",
                        "default": "blum_tandem_550h",
                    },
                    "joinery_style": {
                        "type": "string",
                        "enum": ["butt", "qqq", "half_lap", "drawer_lock"],
                        "default": "half_lap",
                    },
                    "corner_lip_mm": {
                        "type": "number",
                        "description": (
                            "drawer_lock only: the wall the router bit leaves "
                            "outboard of the socket, per corner (mm). Sides "
                            "are cut 2x this short of the box depth. Measure "
                            "it on a test corner rather than trusting the "
                            "catalogue nominal."
                        ),
                    },
                    "side_thickness":       {"type": "number", "default": 15.0},
                    "front_back_thickness": {"type": "number", "default": 15.0},
                    "bottom_thickness": {
                        "type": "number",
                        "description": (
                            "Bottom panel thickness in mm. Omit for the size-based "
                            "default: 12 mm (1/2\") when the box is taller than "
                            "127 mm (5\") and at least 406.4 mm (16\") wide, else "
                            "6 mm (1/4\")."
                        ),
                    },
                    "face_thickness":       {"type": "number", "default": 18.0},
                    "use_standard_height": {
                        "type": "boolean",
                        "description": (
                            "Snap box height to the nearest standard industry size "
                            "(3\"–12\" in 1\" steps). Default true."
                        ),
                        "default": True,
                    },
                    "pull_key": {
                        "type": "string",
                        "description": (
                            "Pull catalog key from list_hardware (category='pulls'). "
                            "Omit for no pull."
                        ),
                    },
                    "pull_count": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Override auto-selected pull count. 0 = auto: single "
                            "pull if the drawer face is ≤ 600 mm wide, otherwise "
                            "two pulls placed at quarter-points."
                        ),
                    },
                    "pull_vertical": {
                        "type": "string",
                        "enum": ["center", "upper_third", "lower_third"],
                        "description": "Vertical placement policy on the drawer face.",
                        "default": "center",
                    },
                },
                "required": ["opening_width", "opening_height", "opening_depth"],
            },
        ),
        types.Tool(
            name="generate_cutlist",
            description=textwrap.dedent("""\
                Generate a complete bill of materials and cutlist from a cabinet
                configuration. Supports single-column and multi-column layouts.

                Pass the same cabinet parameters as design_cabinet, plus an
                optional ``columns`` array (same format as design_multi_column_cabinet)
                to include column dividers, drawer box parts, and applied false fronts.

                Returns:
                  - sheet_goods: uncut sheet quantities grouped by material/thickness
                  - panels_summary: flat panel list with dimensions and quantities
                  - cutlist_json / cutlist_csv: full BOM for external tools
                  - files: paths to written CSV and JSON files on disk
                  - optimization: sheets-used and waste% per thickness group (rectpack)

                Drawer box sides/front/back use drawer_box_thickness stock
                (default 5/8\" / 15 mm) with dado-captured bottoms (1/4\" by
                default; 1/2\" for boxes over 5\" tall and 16\"+ wide, or per
                the per-drawer bottom_thickness option). Applied false fronts
                are listed separately as finished_wood (species to be
                specified by the user).
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "width":  {"type": "number"},
                    "height": {"type": "number"},
                    "depth":  {"type": "number"},
                    "name": {
                        "type": "string",
                        "description": "Base filename stem for output files. Default: 'cabinet'.",
                        "default": "cabinet",
                    },
                    "side_thickness":   {"type": "number", "default": 18.0},
                    "bottom_thickness": {"type": "number", "default": 18.0},
                    "top_thickness":    {"type": "number", "default": 18.0},
                    "shelf_thickness":  {"type": "number", "default": 18.0},
                    "back_thickness":   {"type": "number", "default": 6.0},
                    "drawer_box_thickness": {
                        "type": "number", "default": 15.0,
                        "description": "Drawer box stock (sides + sub-front/back) in mm; bottoms follow the per-drawer size rule.",
                    },
                    "drawer_box_prefinished": {
                        "type": "boolean", "default": False,
                        "description": "Build drawer boxes (incl. bottoms) from pre-finished Baltic birch — separate cutlist material and sheet pricing, no finishing step. Workshop presets default true.",
                    },
                    "face_material": {
                        "type": "string",
                        "default": "finished_wood",
                        "description": (
                            "Cutlist material for show faces (false fronts + door "
                            "leaves). 'baltic_birch' / 'baltic_birch_prefinished' "
                            "pool into sheet optimisation; any other string stays "
                            "a labeled order-out group (e.g. 'rift_white_oak_ply')."
                        ),
                    },
                    "carcass_material": {
                        "type": "string",
                        "default": "baltic_birch",
                        "description": (
                            "Cutlist material for carcass panels (sides, top, "
                            "bottom, shelves, dividers). Sheet materials (Baltic "
                            "birch stocks or names ending '_ply') pack per "
                            "(material, thickness) and price from PRICE_LIST when "
                            "listed. Backs and drawer boxes keep their own stock."
                        ),
                    },
                    "edge_band_mode": {
                        "type": "string",
                        "enum": ["none", "hot_melt", "hardwood"],
                        "default": "none",
                        "description": (
                            "Edge banding for exposed ply edges (fronts of "
                            "carcass panels + all four edges of faces/doors). "
                            "'hot_melt' = iron-on veneer rolls, dims "
                            "unchanged; 'hardwood' = solid strips, panel "
                            "CORES cut smaller so finished dims hold. "
                            "Also a SharedDesign token."
                        ),
                    },
                    "edge_band_thickness_mm": {
                        "type": "number",
                        "default": 0.6,
                        "description": (
                            "Band thickness in mm (hardwood: 3.2–6.4 "
                            "typical; hot-melt ~0.6)."
                        ),
                    },
                    "edge_band_material": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Banding species; empty derives from each "
                            "panel's material (rift white oak ply → "
                            "white_oak rolls, Baltic birch → white_birch)."
                        ),
                    },
                    "edge_band_stock": {
                        "type": ["object", "null"],
                        "default": None,
                        "description": (
                            "Purchasable hardwood banding stock "
                            "{width_mm, length_mm, price_usd, "
                            "strip_width_mm (default 20)}. Board thickness "
                            "IS edge_band_thickness_mm. When set, the BOM "
                            "band line is priced in boards-to-order via "
                            "real piece-into-strip packing (flags edges "
                            "with no flush-trim overhang or longer than "
                            "the stock). Omit for the unpriced "
                            "rip-from-offcuts line. SharedDesign token."
                        ),
                    },
                    "face_gap_mm": {
                        "type": "number", "default": 4.0,
                        "description": (
                            "Vertical reveal between stacked show faces (mm, "
                            "total per joint) — sets false-front heights on "
                            "cutlists and renders alike. SharedDesign token."
                        ),
                    },
                    "furniture_top": {
                        "type": "boolean", "default": False,
                        "description": (
                            "Stored 'furniture top' style: the top panel "
                            "gains a front cap strip flush with the face "
                            "plane (emitted on cutlists) and the lowest face "
                            "drops to the carcass underside. SharedDesign "
                            "token."
                        ),
                    },
                    "carcass_corner_style": {
                        "type": "string",
                        "enum": ["butt", "miter"],
                        "default": "butt",
                        "description": (
                            "'miter': the four exterior corners are 45° "
                            "miters — top/bottom cut to FULL exterior "
                            "width (long-point dims), all four panels "
                            "beveled; divider/shelf joints stay butt "
                            "tenons. Floating-tenon carcasses only. Also "
                            "a SharedDesign token."
                        ),
                    },
                    "back_style": {
                        "type": "string",
                        "enum": ["full_height", "under_top"],
                        "default": "full_height",
                        "description": (
                            "'under_top': the top panel is cut to FULL "
                            "depth (rear edge flush with the sides) and "
                            "the back stops at its underside — no back "
                            "edge visible from above or the sides. Butt "
                            "corners + tenon/screw/biscuit/dowel joinery "
                            "only. Also a SharedDesign token."
                        ),
                    },
                    "back_capture": {
                        "type": "string",
                        "enum": ["pocket", "rabbet", "half_lap", "dado"],
                        "default": "pocket",
                        "description": (
                            "How the back is HELD — independent of "
                            "back_style and carcass_joinery. 'pocket': the "
                            "legacy let-in back, glued onto the rear edges "
                            "of the interior panels, nothing machined. "
                            "'rabbet': a rabbet in the rear inner edge of "
                            "the sides, top and bottom; the back drops in "
                            "from behind after glue-up and finishes flush. "
                            "'half_lap': both halves rabbeted so they lap — "
                            "twice the glue area and the panel is trapped "
                            "fore-and-aft by the step (needs a back at least "
                            "9 mm thick). 'dado': a groove in all four "
                            "members; the back SLIDES IN during glue-up and "
                            "is trapped on four edges, so nothing shows from "
                            "any angle and a solid-wood back can float and "
                            "move. The three machined captures seat the back "
                            "inside the case perimeter, so back_style only "
                            "changes what you see under 'pocket'."
                        ),
                    },
                    "back_groove_setback": {
                        "type": "number",
                        "default": 12.0,
                        "description": (
                            "back_capture 'dado' only: material left "
                            "standing behind the groove, i.e. how far the "
                            "back sits forward of the case rear (mm)."
                        ),
                    },
                    "drawer_config": {
                        "type": "array",
                        "items": {"type": "array", "minItems": 2, "maxItems": 3},
                        "default": [],
                    },
                    "columns": {
                        "description": "Multi-column layout from design_multi_column_cabinet. When provided, drawer_config is ignored.",
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "width_mm": {"type": "number"},
                                "drawer_config": {
                                    "type": "array",
                                    "items": {"type": "array", "minItems": 2, "maxItems": 3},
                                    "default": [],
                                },
                            },
                            "required": ["width_mm"],
                        },
                    },
                    "sheet_length": {
                        "type": "number",
                        "description": "Sheet stock length in mm (default 2440 / 4×8).",
                        "default": 2440,
                    },
                    "sheet_width": {
                        "type": "number",
                        "description": "Sheet stock width in mm (default 1220 / 4×8).",
                        "default": 1220,
                    },
                    "optimizer_overrides": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "string",
                            "enum": ["auto", "opcut", "rectpack", "strip",
                                     "rips_first"],
                        },
                        "description": (
                            "Per-group optimizer overrides. Keys match "
                            "(material, thickness) groups by precedence "
                            "'material@thickness' > 'material' > "
                            "'@thickness' (e.g. {'@6': 'opcut'} keeps 6 mm "
                            "stock on opcut while the run-level optimizer "
                            "handles the rest). Non-default; omit for a "
                            "single optimizer."
                        ),
                    },
                    "sheet_size_overrides": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                        "description": (
                            "Per-material sheet size overrides, "
                            "{material: [length_mm, width_mm]} — e.g. "
                            "{'rift_white_oak_ply': [2453, 1234]} when one "
                            "stock runs oversize while the rest stay at "
                            "sheet_length x sheet_width."
                        ),
                    },
                    "kerf": {
                        "type": "number",
                        "description": "Saw-blade kerf in mm added to each panel for layout. Default 3.2 mm.",
                        "default": 3.2,
                    },
                    "format": {
                        "type": "string",
                        "enum": ["json", "csv", "both"],
                        "description": "Output format.",
                        "default": "both",
                    },
                    "paper": {
                        "type": "string",
                        "enum": ["letter", "a4"],
                        "default": "letter",
                        "description": "PDF paper size — US Letter by default; 'a4' for A4.",
                    },
                    "optimizer": {
                        "type": "string",
                        "enum": ["auto", "opcut", "rectpack", "strip", "rips_first"],
                        "description": (
                            "Sheet layout algorithm. 'auto' (default) uses opcut if installed, "
                            "then rectpack if installed, then strip-cutting. 'rectpack' requires "
                            "the rectpack package (uv pip install -e '.[cutlist]')."
                        ),
                        "default": "auto",
                    },
                },
                "required": ["width", "height", "depth"],
            },
        ),
        types.Tool(
            name="compare_joinery",
            description=textwrap.dedent("""\
                Compare drawer joinery styles side-by-side for a given stock thickness.
                Returns cut dimensions and characteristics for butt, qqq, half_lap,
                and drawer_lock joints.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "side_thickness": {
                        "type": "number",
                        "description": "Drawer side thickness in mm.",
                        "default": 12.0,
                    },
                    "front_back_thickness": {
                        "type": "number",
                        "description": "Drawer front/back thickness in mm.",
                        "default": 12.0,
                    },
                },
            },
        ),
        types.Tool(
            name="visualize_cabinet",
            description=textwrap.dedent("""\
                Build a full 3D cabinet assembly, export it as a GLB file, and
                generate a self-contained HTML viewer that opens in the browser.

                Requires CadQuery (pip install cadquery).  The HTML embeds the
                GLB as base64, so no server is needed — just open the file.

                Pass the same cabinet parameters as design_cabinet.
                Returns paths to the GLB and HTML files plus file size info.

                Viewer shortcuts: X = x-ray drawer fronts, O = open drawers.

                ── WORKFLOW ──
                NEVER call this tool directly after design_cabinet or
                apply_preset.  You MUST first:
                  1. evaluate_cabinet → ensure zero errors (auto_fix_cabinet if
                     needed)
                  2. describe_design → present the prose summary to the user
                  3. Wait for the user to explicitly approve or request changes
                Only call visualize_cabinet after the user has approved the
                evaluated, described design.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "width":  {"type": "number", "description": "Exterior width in mm."},
                    "height": {"type": "number", "description": "Exterior height in mm."},
                    "depth":  {"type": "number", "description": "Exterior depth in mm."},
                    "side_thickness":   {"type": "number", "default": 18.0},
                    "bottom_thickness": {"type": "number", "default": 18.0},
                    "top_thickness":    {"type": "number", "default": 18.0},
                    "shelf_thickness":  {"type": "number", "default": 18.0},
                    "back_thickness":   {"type": "number", "default": 6.0},
                    "drawer_box_thickness": {
                        "type": "number", "default": 15.0,
                        "description": "Drawer box stock (sides + sub-front/back) in mm; bottoms follow the per-drawer size rule.",
                    },
                    "drawer_box_prefinished": {
                        "type": "boolean", "default": False,
                        "description": "Build drawer boxes (incl. bottoms) from pre-finished Baltic birch — separate cutlist material and sheet pricing, no finishing step. Workshop presets default true.",
                    },
                    "face_material": {
                        "type": "string",
                        "default": "finished_wood",
                        "description": (
                            "Cutlist material for show faces (false fronts + door "
                            "leaves). 'baltic_birch' / 'baltic_birch_prefinished' "
                            "pool into sheet optimisation; any other string stays "
                            "a labeled order-out group (e.g. 'rift_white_oak_ply')."
                        ),
                    },
                    "carcass_material": {
                        "type": "string",
                        "default": "baltic_birch",
                        "description": (
                            "Cutlist material for carcass panels (sides, top, "
                            "bottom, shelves, dividers). Sheet materials (Baltic "
                            "birch stocks or names ending '_ply') pack per "
                            "(material, thickness) and price from PRICE_LIST when "
                            "listed. Backs and drawer boxes keep their own stock."
                        ),
                    },
                    "edge_band_mode": {
                        "type": "string",
                        "enum": ["none", "hot_melt", "hardwood"],
                        "default": "none",
                        "description": (
                            "Edge banding for exposed ply edges (fronts of "
                            "carcass panels + all four edges of faces/doors). "
                            "'hot_melt' = iron-on veneer rolls, dims "
                            "unchanged; 'hardwood' = solid strips, panel "
                            "CORES cut smaller so finished dims hold. "
                            "Also a SharedDesign token."
                        ),
                    },
                    "edge_band_thickness_mm": {
                        "type": "number",
                        "default": 0.6,
                        "description": (
                            "Band thickness in mm (hardwood: 3.2–6.4 "
                            "typical; hot-melt ~0.6)."
                        ),
                    },
                    "edge_band_material": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Banding species; empty derives from each "
                            "panel's material (rift white oak ply → "
                            "white_oak rolls, Baltic birch → white_birch)."
                        ),
                    },
                    "edge_band_stock": {
                        "type": ["object", "null"],
                        "default": None,
                        "description": (
                            "Purchasable hardwood banding stock "
                            "{width_mm, length_mm, price_usd, "
                            "strip_width_mm (default 20)}. Board thickness "
                            "IS edge_band_thickness_mm. When set, the BOM "
                            "band line is priced in boards-to-order via "
                            "real piece-into-strip packing (flags edges "
                            "with no flush-trim overhang or longer than "
                            "the stock). Omit for the unpriced "
                            "rip-from-offcuts line. SharedDesign token."
                        ),
                    },
                    "face_gap_mm": {
                        "type": "number", "default": 4.0,
                        "description": (
                            "Vertical reveal between stacked show faces (mm, "
                            "total per joint) — sets false-front heights on "
                            "cutlists and renders alike. SharedDesign token."
                        ),
                    },
                    "carcass_corner_style": {
                        "type": "string",
                        "enum": ["butt", "miter"],
                        "default": "butt",
                        "description": (
                            "'miter': the four exterior corners are 45° "
                            "miters — top/bottom cut to FULL exterior "
                            "width (long-point dims), all four panels "
                            "beveled; divider/shelf joints stay butt "
                            "tenons. Floating-tenon carcasses only. Also "
                            "a SharedDesign token."
                        ),
                    },
                    "back_style": {
                        "type": "string",
                        "enum": ["full_height", "under_top"],
                        "default": "full_height",
                        "description": (
                            "'under_top': the top panel is cut to FULL "
                            "depth (rear edge flush with the sides) and "
                            "the back stops at its underside — no back "
                            "edge visible from above or the sides. Butt "
                            "corners + tenon/screw/biscuit/dowel joinery "
                            "only. Also a SharedDesign token."
                        ),
                    },
                    "back_capture": {
                        "type": "string",
                        "enum": ["pocket", "rabbet", "half_lap", "dado"],
                        "default": "pocket",
                        "description": (
                            "How the back is HELD — independent of "
                            "back_style and carcass_joinery. 'pocket': the "
                            "legacy let-in back, glued onto the rear edges "
                            "of the interior panels, nothing machined. "
                            "'rabbet': a rabbet in the rear inner edge of "
                            "the sides, top and bottom; the back drops in "
                            "from behind after glue-up and finishes flush. "
                            "'half_lap': both halves rabbeted so they lap — "
                            "twice the glue area and the panel is trapped "
                            "fore-and-aft by the step (needs a back at least "
                            "9 mm thick). 'dado': a groove in all four "
                            "members; the back SLIDES IN during glue-up and "
                            "is trapped on four edges, so nothing shows from "
                            "any angle and a solid-wood back can float and "
                            "move. The three machined captures seat the back "
                            "inside the case perimeter, so back_style only "
                            "changes what you see under 'pocket'."
                        ),
                    },
                    "back_groove_setback": {
                        "type": "number",
                        "default": 12.0,
                        "description": (
                            "back_capture 'dado' only: material left "
                            "standing behind the groove, i.e. how far the "
                            "back sits forward of the case rear (mm)."
                        ),
                    },
                    "drawer_config": {
                        "type": "array",
                        "items": {"type": "array", "minItems": 2, "maxItems": 3},
                        "default": [],
                    },
                    "fixed_shelf_positions": {
                        "type": "array",
                        "items": {"type": "number"},
                        "default": [],
                    },
                    "adj_shelf_holes": {"type": "boolean", "default": False},
                    "num_bays": {
                        "type": "integer",
                        "description": "Number of identical side-by-side bays to render (default 1).",
                        "default": 1,
                    },
                    "columns": {
                        "type": "array",
                        "description": "Multi-column layout from design_multi_column_cabinet. Each entry has width_mm and drawer_config.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "width_mm": {"type": "number"},
                                "drawer_config": {
                                    "type": "array",
                                    "items": {"type": "array", "minItems": 2, "maxItems": 3},
                                },
                            },
                            "required": ["width_mm"],
                        },
                    },
                    "name": {
                        "type": "string",
                        "description": "Base filename stem for output files.",
                        "default": "cabinet",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Directory for output files. Default: ~/.cabineteer/visualizations",
                        "default": "~/.cabineteer/visualizations",
                    },
                    "open_browser": {
                        "type": "boolean",
                        "description": "Open the HTML viewer in the default browser.",
                        "default": True,
                    },
                    "tolerance": {
                        "type": "number",
                        "description": "Mesh tessellation tolerance in mm. Lower = finer mesh, bigger file. Default: 0.1",
                        "default": 0.1,
                    },
                    "finish": {
                        "type": "string",
                        "enum": sorted(_WOOD_FINISHES),
                        "description": (
                            "Wood finish rendered in the viewer as a procedural "
                            "grain texture on the carcass, drawer faces, and "
                            "doors (pull hardware keeps its metal look). "
                            "Omit for flat panel colors."
                        ),
                    },
                    "drawer_box_finish": {
                        "type": "string",
                        "enum": sorted(_WOOD_FINISHES),
                        "description": (
                            "Finish for drawer-box meshes. Defaults to "
                            "baltic_birch whenever 'finish' is set; pass the "
                            "same key as 'finish' for a uniform look."
                        ),
                    },
                    "grain_direction": {
                        "type": "string",
                        "enum": ["vertical", "horizontal"],
                        "default": "vertical",
                        "description": (
                            "Grain orientation on show surfaces (carcass, "
                            "drawer faces, doors). Drawer boxes are always "
                            "horizontal — box sides are cut with the grain "
                            "along their length."
                        ),
                    },
                    "drawer_joinery": {
                        "type": "string",
                        "enum": ["butt", "qqq", "half_lap", "drawer_lock"],
                        "description": "Drawer box corner joint style.",
                        "default": "half_lap",
                    },
                    "drawer_corner_lip_mm": {
                        "type": "number",
                        "description": (
                            "drawer_lock only: the wall the router bit leaves "
                            "outboard of the socket, per corner (mm). Box "
                            "sides are cut 2x this short of the box depth. "
                            "Measure it on a test corner; omitting it takes "
                            "the catalogue nominal (1/8 in)."
                        ),
                    },
                    "drawer_pull": {
                        "type": "string",
                        "description": "Pull catalog key from list_hardware (category='pulls'). Omit for no pull hardware in render.",
                    },
                    "door_pull": {
                        "type": "string",
                        "description": "Pull catalog key applied to every door / door_pair slot in the render.",
                    },
                    "pull_preset": {
                        "type": "string",
                        "description": "Named pull preset key (see list_pull_presets). Populates drawer_pull, door_pull, and orientation. Explicit drawer_pull/door_pull fields override the preset.",
                    },
                    "furniture_top": {
                        "type": "boolean",
                        "description": (
                            "'Furniture top' style: a front cap strip extends the top "
                            "panel flush to the drawer-face plane, and the bottom of the "
                            "lowest drawer face drops to the carcass underside. Omit to "
                            "use the cabinet's stored furniture_top flag (a SharedDesign "
                            "token; also emits the cap strip on cutlists)."
                        ),
                    },
                    "divider_full_height": {
                        "type": "boolean",
                        "description": (
                            "Controls the center divider in mixed drawer+door columns "
                            "(e.g. armoire). Default true: divider extends the full "
                            "cabinet height, separating the upper bay into independent "
                            "compartments. Set false to clip the divider to the drawer "
                            "zone so the upper door/open section stays open."
                        ),
                        "default": True,
                    },
                    "manga": _manga_schema(),
                },
                "required": ["width", "height", "depth"],
            },
        ),
        types.Tool(
            name="list_presets",
            description=textwrap.dedent("""\
                Return the catalogue of named cabinet presets — validated starting
                configurations for common cabinet types (kitchen base, dresser,
                tool chest, bathroom vanity, wall cabinet, pantry, credenza,
                sideboard, console table, media console, etc.).

                Use this to discover preset names, then call apply_preset to load
                one as a full design_cabinet-compatible config dict.

                Optionally filter by category ("kitchen", "workshop", "bedroom",
                "bathroom", "storage", "living_room") or by tag (e.g. "drawer",
                "soft_close", "heavy_duty", "console", "credenza").
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["kitchen", "workshop", "bedroom", "bathroom", "storage",
                                 "living_room", "entryway", "office"],
                        "description": "Filter by cabinet category.",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Filter by tag (e.g. 'drawer', 'soft_close', 'wide').",
                    },
                },
            },
        ),
        types.Tool(
            name="apply_preset",
            description=textwrap.dedent("""\
                Load a named preset and return its full configuration dict, ready
                to pass directly to evaluate_cabinet.

                Optionally supply an overrides dict to tweak individual fields
                (e.g. change width, swap the drawer_slide, adjust depth) without
                rebuilding from scratch.

                Returns:
                  - preset_name, display_name, description, category
                  - config: the merged CabinetConfig fields as a flat dict
                  - interior_height_mm: computed from the merged config
                  - opening_stack_total_mm: sum of drawer_config heights
                  - opening_stack_matches_interior: whether they are equal
                    (mismatches indicate the overrides changed height but not
                    the opening stack — you should update drawer_config too)

                Use list_presets to discover valid preset names.

                ── WORKFLOW ──
                After calling this tool you MUST immediately call
                evaluate_cabinet on the returned config.  If errors are found,
                call auto_fix_cabinet, then re-evaluate.  Once clean, call
                describe_design and present the summary to the user.  If
                describe_design returns pull_selection_required=true, call
                list_pull_presets and ask the user to choose a pull style
                before visualizing.  Do NOT call visualize_cabinet until the
                user has approved the design and pull hardware.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Preset slug from list_presets (e.g. 'kitchen_base_3_drawer').",
                    },
                    "overrides": {
                        "type": "object",
                        "description": (
                            "Optional dict of CabinetConfig fields to override after loading "
                            "the preset (e.g. {\"width\": 750, \"drawer_slide\": \"blum_movento_760h\"})."
                        ),
                        "default": {},
                    },
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="identify_furniture_type",
            description=textwrap.dedent("""\
                Look up a furniture piece by common name or synonym and return
                its canonical type, category, typical dimensions, related names,
                and any matching preset slugs.

                Accepts plain English names including historical, regional, and
                foreign-language terms: "chifforobe", "semainier", "tallboy",
                "credenza", "tansu", "armadio", "chevet", etc.

                Use this tool when the user names a furniture type you want to
                confirm, or to discover which preset best matches what they want.
                Returns up to 5 candidates when the name is ambiguous.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Furniture piece name or synonym to look up.",
                    },
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="auto_fix_cabinet",
            description=textwrap.dedent("""\
                Attempt a single round of deterministic fixes on a cabinet
                configuration.  Evaluates the config, applies known-safe
                corrections (e.g. rebalancing an opening stack that overshoots
                interior_height, aligning a back-panel rabbet with the back
                thickness), then re-evaluates.

                Returns:
                  - config: the (possibly modified) config dict
                  - changes: human-readable list of what was adjusted
                  - initial_issues / final_issues: before and after
                  - fixed: whether at least one error was resolved
                  - clean: whether zero errors remain

                ── WORKFLOW ──
                Call this ONLY after evaluate_cabinet has returned one or more
                severity=error issues.  After auto-fix completes, call
                evaluate_cabinet again to confirm the result is clean.  If
                errors remain, describe them to the user and ask for guidance.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "width":  {"type": "number"},
                    "height": {"type": "number"},
                    "depth":  {"type": "number"},
                    "side_thickness":   {"type": "number", "default": 18.0},
                    "bottom_thickness": {"type": "number", "default": 18.0},
                    "top_thickness":    {"type": "number", "default": 18.0},
                    "shelf_thickness":  {"type": "number", "default": 18.0},
                    "back_thickness":   {"type": "number", "default": 6.0},
                    "drawer_box_thickness": {
                        "type": "number", "default": 15.0,
                        "description": "Drawer box stock (sides + sub-front/back) in mm; bottoms follow the per-drawer size rule.",
                    },
                    "drawer_box_prefinished": {
                        "type": "boolean", "default": False,
                        "description": "Build drawer boxes (incl. bottoms) from pre-finished Baltic birch — separate cutlist material and sheet pricing, no finishing step. Workshop presets default true.",
                    },
                    "face_material": {
                        "type": "string",
                        "default": "finished_wood",
                        "description": (
                            "Cutlist material for show faces (false fronts + door "
                            "leaves). 'baltic_birch' / 'baltic_birch_prefinished' "
                            "pool into sheet optimisation; any other string stays "
                            "a labeled order-out group (e.g. 'rift_white_oak_ply')."
                        ),
                    },
                    "carcass_material": {
                        "type": "string",
                        "default": "baltic_birch",
                        "description": (
                            "Cutlist material for carcass panels (sides, top, "
                            "bottom, shelves, dividers). Sheet materials (Baltic "
                            "birch stocks or names ending '_ply') pack per "
                            "(material, thickness) and price from PRICE_LIST when "
                            "listed. Backs and drawer boxes keep their own stock."
                        ),
                    },
                    "edge_band_mode": {
                        "type": "string",
                        "enum": ["none", "hot_melt", "hardwood"],
                        "default": "none",
                        "description": (
                            "Edge banding for exposed ply edges (fronts of "
                            "carcass panels + all four edges of faces/doors). "
                            "'hot_melt' = iron-on veneer rolls, dims "
                            "unchanged; 'hardwood' = solid strips, panel "
                            "CORES cut smaller so finished dims hold. "
                            "Also a SharedDesign token."
                        ),
                    },
                    "edge_band_thickness_mm": {
                        "type": "number",
                        "default": 0.6,
                        "description": (
                            "Band thickness in mm (hardwood: 3.2–6.4 "
                            "typical; hot-melt ~0.6)."
                        ),
                    },
                    "edge_band_material": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Banding species; empty derives from each "
                            "panel's material (rift white oak ply → "
                            "white_oak rolls, Baltic birch → white_birch)."
                        ),
                    },
                    "edge_band_stock": {
                        "type": ["object", "null"],
                        "default": None,
                        "description": (
                            "Purchasable hardwood banding stock "
                            "{width_mm, length_mm, price_usd, "
                            "strip_width_mm (default 20)}. Board thickness "
                            "IS edge_band_thickness_mm. When set, the BOM "
                            "band line is priced in boards-to-order via "
                            "real piece-into-strip packing (flags edges "
                            "with no flush-trim overhang or longer than "
                            "the stock). Omit for the unpriced "
                            "rip-from-offcuts line. SharedDesign token."
                        ),
                    },
                    "face_gap_mm": {
                        "type": "number", "default": 4.0,
                        "description": (
                            "Vertical reveal between stacked show faces (mm, "
                            "total per joint) — sets false-front heights on "
                            "cutlists and renders alike. SharedDesign token."
                        ),
                    },
                    "furniture_top": {
                        "type": "boolean", "default": False,
                        "description": (
                            "Stored 'furniture top' style: the top panel "
                            "gains a front cap strip flush with the face "
                            "plane (emitted on cutlists) and the lowest face "
                            "drops to the carcass underside. SharedDesign "
                            "token."
                        ),
                    },
                    "carcass_corner_style": {
                        "type": "string",
                        "enum": ["butt", "miter"],
                        "default": "butt",
                        "description": (
                            "'miter': the four exterior corners are 45° "
                            "miters — top/bottom cut to FULL exterior "
                            "width (long-point dims), all four panels "
                            "beveled; divider/shelf joints stay butt "
                            "tenons. Floating-tenon carcasses only. Also "
                            "a SharedDesign token."
                        ),
                    },
                    "back_style": {
                        "type": "string",
                        "enum": ["full_height", "under_top"],
                        "default": "full_height",
                        "description": (
                            "'under_top': the top panel is cut to FULL "
                            "depth (rear edge flush with the sides) and "
                            "the back stops at its underside — no back "
                            "edge visible from above or the sides. Butt "
                            "corners + tenon/screw/biscuit/dowel joinery "
                            "only. Also a SharedDesign token."
                        ),
                    },
                    "back_capture": {
                        "type": "string",
                        "enum": ["pocket", "rabbet", "half_lap", "dado"],
                        "default": "pocket",
                        "description": (
                            "How the back is HELD — independent of "
                            "back_style and carcass_joinery. 'pocket': the "
                            "legacy let-in back, glued onto the rear edges "
                            "of the interior panels, nothing machined. "
                            "'rabbet': a rabbet in the rear inner edge of "
                            "the sides, top and bottom; the back drops in "
                            "from behind after glue-up and finishes flush. "
                            "'half_lap': both halves rabbeted so they lap — "
                            "twice the glue area and the panel is trapped "
                            "fore-and-aft by the step (needs a back at least "
                            "9 mm thick). 'dado': a groove in all four "
                            "members; the back SLIDES IN during glue-up and "
                            "is trapped on four edges, so nothing shows from "
                            "any angle and a solid-wood back can float and "
                            "move. The three machined captures seat the back "
                            "inside the case perimeter, so back_style only "
                            "changes what you see under 'pocket'."
                        ),
                    },
                    "back_groove_setback": {
                        "type": "number",
                        "default": 12.0,
                        "description": (
                            "back_capture 'dado' only: material left "
                            "standing behind the groove, i.e. how far the "
                            "back sits forward of the case rear (mm)."
                        ),
                    },
                    "drawer_config": {
                        "type": "array",
                        "items": {"type": "array", "minItems": 2, "maxItems": 3},
                        "default": [],
                    },
                    "carcass_joinery": {
                        "type": "string",
                        "enum": ["dado_rabbet", "floating_tenon", "pocket_screw", "biscuit", "dowel"],
                        "default": "floating_tenon",
                    },
                    "drawer_joinery": {
                        "type": "string",
                        "enum": ["butt", "qqq", "half_lap", "drawer_lock"],
                        "default": "half_lap",
                        "description": "Drawer box corner joint style.",
                    },
                    "drawer_corner_lip_mm": {
                        "type": "number",
                        "description": (
                            "drawer_lock only: the wall the router bit leaves "
                            "outboard of the socket, per corner (mm). Box "
                            "sides are cut 2x this short of the box depth. "
                            "Measure it on a test corner; omitting it takes "
                            "the catalogue nominal (1/8 in)."
                        ),
                    },
                    "door_hinge": {"type": "string", "default": "blum_clip_top_blumotion_110_full"},
                    "adj_shelf_holes": {"type": "boolean", "default": False},
                    "drawer_slide": {"type": "string", "default": "blum_tandem_550h"},
                },
                "required": ["width", "height", "depth"],
            },
        ),
        types.Tool(
            name="describe_design",
            description=textwrap.dedent("""\
                Generate a human-readable prose description of a cabinet
                configuration — dimensions in both metric and imperial, opening
                layout, hardware names, joinery methods, and materials — suitable
                for presenting to the user during design review.

                Returns:
                  - prose: a short paragraph summarising the design
                  - dimensions, openings, hardware, materials: structured dicts
                  - materials.carcass_joinery: the carcass joinery method
                  - materials.drawer_box_joinery: the drawer-box corner joint

                Returns:
                  - pull_selection_required: true when drawers or doors have no
                    pull assigned — ask the user to pick one before visualizing.

                ── WORKFLOW ──
                Call this after evaluate_cabinet returns zero errors (or after
                auto_fix_cabinet has cleaned them).  Present the prose to the
                user, then EXPLICITLY ask them to confirm or change:
                  1. Carcass joinery (materials.carcass_joinery)
                  2. Drawer-box joinery (materials.drawer_box_joinery)
                  3. Pull style — if pull_selection_required is true, call
                     list_pull_presets and ask the user to choose before
                     calling visualize_cabinet.
                Do not proceed to visualization until all three are confirmed.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "width":  {"type": "number"},
                    "height": {"type": "number"},
                    "depth":  {"type": "number"},
                    "side_thickness":   {"type": "number", "default": 18.0},
                    "bottom_thickness": {"type": "number", "default": 18.0},
                    "top_thickness":    {"type": "number", "default": 18.0},
                    "shelf_thickness":  {"type": "number", "default": 18.0},
                    "back_thickness":   {"type": "number", "default": 6.0},
                    "drawer_box_thickness": {
                        "type": "number", "default": 15.0,
                        "description": "Drawer box stock (sides + sub-front/back) in mm; bottoms follow the per-drawer size rule.",
                    },
                    "drawer_box_prefinished": {
                        "type": "boolean", "default": False,
                        "description": "Build drawer boxes (incl. bottoms) from pre-finished Baltic birch — separate cutlist material and sheet pricing, no finishing step. Workshop presets default true.",
                    },
                    "face_material": {
                        "type": "string",
                        "default": "finished_wood",
                        "description": (
                            "Cutlist material for show faces (false fronts + door "
                            "leaves). 'baltic_birch' / 'baltic_birch_prefinished' "
                            "pool into sheet optimisation; any other string stays "
                            "a labeled order-out group (e.g. 'rift_white_oak_ply')."
                        ),
                    },
                    "carcass_material": {
                        "type": "string",
                        "default": "baltic_birch",
                        "description": (
                            "Cutlist material for carcass panels (sides, top, "
                            "bottom, shelves, dividers). Sheet materials (Baltic "
                            "birch stocks or names ending '_ply') pack per "
                            "(material, thickness) and price from PRICE_LIST when "
                            "listed. Backs and drawer boxes keep their own stock."
                        ),
                    },
                    "edge_band_mode": {
                        "type": "string",
                        "enum": ["none", "hot_melt", "hardwood"],
                        "default": "none",
                        "description": (
                            "Edge banding for exposed ply edges (fronts of "
                            "carcass panels + all four edges of faces/doors). "
                            "'hot_melt' = iron-on veneer rolls, dims "
                            "unchanged; 'hardwood' = solid strips, panel "
                            "CORES cut smaller so finished dims hold. "
                            "Also a SharedDesign token."
                        ),
                    },
                    "edge_band_thickness_mm": {
                        "type": "number",
                        "default": 0.6,
                        "description": (
                            "Band thickness in mm (hardwood: 3.2–6.4 "
                            "typical; hot-melt ~0.6)."
                        ),
                    },
                    "edge_band_material": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Banding species; empty derives from each "
                            "panel's material (rift white oak ply → "
                            "white_oak rolls, Baltic birch → white_birch)."
                        ),
                    },
                    "edge_band_stock": {
                        "type": ["object", "null"],
                        "default": None,
                        "description": (
                            "Purchasable hardwood banding stock "
                            "{width_mm, length_mm, price_usd, "
                            "strip_width_mm (default 20)}. Board thickness "
                            "IS edge_band_thickness_mm. When set, the BOM "
                            "band line is priced in boards-to-order via "
                            "real piece-into-strip packing (flags edges "
                            "with no flush-trim overhang or longer than "
                            "the stock). Omit for the unpriced "
                            "rip-from-offcuts line. SharedDesign token."
                        ),
                    },
                    "face_gap_mm": {
                        "type": "number", "default": 4.0,
                        "description": (
                            "Vertical reveal between stacked show faces (mm, "
                            "total per joint) — sets false-front heights on "
                            "cutlists and renders alike. SharedDesign token."
                        ),
                    },
                    "furniture_top": {
                        "type": "boolean", "default": False,
                        "description": (
                            "Stored 'furniture top' style: the top panel "
                            "gains a front cap strip flush with the face "
                            "plane (emitted on cutlists) and the lowest face "
                            "drops to the carcass underside. SharedDesign "
                            "token."
                        ),
                    },
                    "carcass_corner_style": {
                        "type": "string",
                        "enum": ["butt", "miter"],
                        "default": "butt",
                        "description": (
                            "'miter': the four exterior corners are 45° "
                            "miters — top/bottom cut to FULL exterior "
                            "width (long-point dims), all four panels "
                            "beveled; divider/shelf joints stay butt "
                            "tenons. Floating-tenon carcasses only. Also "
                            "a SharedDesign token."
                        ),
                    },
                    "back_style": {
                        "type": "string",
                        "enum": ["full_height", "under_top"],
                        "default": "full_height",
                        "description": (
                            "'under_top': the top panel is cut to FULL "
                            "depth (rear edge flush with the sides) and "
                            "the back stops at its underside — no back "
                            "edge visible from above or the sides. Butt "
                            "corners + tenon/screw/biscuit/dowel joinery "
                            "only. Also a SharedDesign token."
                        ),
                    },
                    "back_capture": {
                        "type": "string",
                        "enum": ["pocket", "rabbet", "half_lap", "dado"],
                        "default": "pocket",
                        "description": (
                            "How the back is HELD — independent of "
                            "back_style and carcass_joinery. 'pocket': the "
                            "legacy let-in back, glued onto the rear edges "
                            "of the interior panels, nothing machined. "
                            "'rabbet': a rabbet in the rear inner edge of "
                            "the sides, top and bottom; the back drops in "
                            "from behind after glue-up and finishes flush. "
                            "'half_lap': both halves rabbeted so they lap — "
                            "twice the glue area and the panel is trapped "
                            "fore-and-aft by the step (needs a back at least "
                            "9 mm thick). 'dado': a groove in all four "
                            "members; the back SLIDES IN during glue-up and "
                            "is trapped on four edges, so nothing shows from "
                            "any angle and a solid-wood back can float and "
                            "move. The three machined captures seat the back "
                            "inside the case perimeter, so back_style only "
                            "changes what you see under 'pocket'."
                        ),
                    },
                    "back_groove_setback": {
                        "type": "number",
                        "default": 12.0,
                        "description": (
                            "back_capture 'dado' only: material left "
                            "standing behind the groove, i.e. how far the "
                            "back sits forward of the case rear (mm)."
                        ),
                    },
                    "drawer_config": {
                        "type": "array",
                        "items": {"type": "array", "minItems": 2, "maxItems": 3},
                        "default": [],
                    },
                    "carcass_joinery": {
                        "type": "string",
                        "enum": ["dado_rabbet", "floating_tenon", "pocket_screw", "biscuit", "dowel"],
                        "default": "floating_tenon",
                    },
                    "drawer_joinery": {
                        "type": "string",
                        "enum": ["butt", "qqq", "half_lap", "drawer_lock"],
                        "default": "half_lap",
                        "description": "Drawer box corner joint style.",
                    },
                    "drawer_corner_lip_mm": {
                        "type": "number",
                        "description": (
                            "drawer_lock only: the wall the router bit leaves "
                            "outboard of the socket, per corner (mm). Box "
                            "sides are cut 2x this short of the box depth. "
                            "Measure it on a test corner; omitting it takes "
                            "the catalogue nominal (1/8 in)."
                        ),
                    },
                    "door_hinge":    {"type": "string", "default": "blum_clip_top_blumotion_110_full"},
                    "drawer_slide":  {"type": "string", "default": "blum_tandem_550h"},
                    "adj_shelf_holes": {"type": "boolean", "default": False},
                    "drawer_pull": {
                        "type": "string",
                        "description": "Pull catalog key from list_hardware (category='pulls'). Include when a pull has been selected.",
                    },
                    "door_pull": {
                        "type": "string",
                        "description": "Pull catalog key for door openings.",
                    },
                },
                "required": ["width", "height", "depth"],
            },
        ),
        types.Tool(
            name="design_legs",
            description=textwrap.dedent("""\
                Configure the legs / feet for a cabinet and return placement
                coordinates, hardware specs, and load-per-leg.

                Default: 4 corner feet using Richelieu 176138106 (100 mm brushed
                nickel contemporary square leg, 50 kg per leg).

                NOTE: the Richelieu 176138106 is 100 mm (3-15/16\"), not a true
                4\" leg.  If you need exactly 4\" / 102 mm, choose a different SKU
                or use an adjustable leg.

                leg_pattern options:
                  "corners"              — one foot at each corner (default, 4 legs)
                  "corners_and_midspan"  — corners + one centred on each long side
                  "along_front_back"     — count/2 evenly spaced across front and back

                Use list_hardware (with category="legs") to see available leg keys.

                Returns:
                  - leg: hardware spec (name, height_mm, load_capacity_kg, part_number)
                  - count: total number of legs
                  - placement_mm: list of {x, y} positions (origin = front-left corner)
                  - inset_mm: how far each foot is set in from the cabinet edge
                  - load_per_leg_kg: cabinet_weight_kg / count (if weight provided)
                  - total_height_mm: leg height (pass to cabinet design as base clearance)
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "cabinet_width":  {"type": "number", "description": "Cabinet exterior width in mm."},
                    "cabinet_depth":  {"type": "number", "description": "Cabinet exterior depth in mm."},
                    "leg_key": {
                        "type": "string",
                        "description": (
                            "Leg key from list_hardware (category=legs). "
                            "Default: richelieu_176138106."
                        ),
                        "default": "richelieu_176138106",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of legs. Default: 4.",
                        "default": 4,
                    },
                    "leg_pattern": {
                        "type": "string",
                        "enum": ["corners", "corners_and_midspan", "along_front_back"],
                        "description": "Foot placement pattern. Default: corners.",
                        "default": "corners",
                    },
                    "inset_mm": {
                        "type": "number",
                        "description": "How far each foot centre is set in from the cabinet edge. Default: 30 mm.",
                        "default": 30.0,
                    },
                    "cabinet_weight_kg": {
                        "type": "number",
                        "description": "Estimated cabinet + contents weight for load-per-leg calc. Default: 0 (skipped).",
                        "default": 0.0,
                    },
                },
                "required": ["cabinet_width", "cabinet_depth"],
            },
        ),
        types.Tool(
            name="design_pulls",
            description=textwrap.dedent("""\
                Compute pull-hardware placements, fit checks, and consolidated
                procurement BOM for an entire cabinet in one call.

                Pass the cabinet footprint (width/height/depth) plus a
                drawer_config (or columns array, for multi-column cabinets)
                matching the shape used by design_cabinet / design_multi_column_cabinet,
                together with drawer_pull and/or door_pull keys from
                list_hardware (category="pulls").

                For each drawer / door / door_pair slot the tool returns:
                  - slot index, slot type, face dimensions
                  - list of placements (centre + hole coordinates, face-local mm)
                  - per-slot issues (e.g. pull_fit, pull_projection)

                Plus:
                  - cabinet_issues: style-consistency check across drawer/door pulls
                  - hardware_bom: consolidated HardwareLine per SKU, including
                    pack-quantity math (packs_to_order / pieces_ordered / leftover)

                Omit both drawer_pull and door_pull to get an empty BOM and no
                slot placements (useful for confirming the tool accepts a layout).

                ── WORKFLOW ──
                Call this AFTER design_cabinet / design_multi_column_cabinet and
                AFTER evaluate_cabinet returns clean; the pull-consistency warning
                is reported here as well so the user sees it alongside the BOM.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "width":  {"type": "number", "description": "Exterior width in mm."},
                    "height": {"type": "number", "description": "Exterior height in mm."},
                    "depth":  {"type": "number", "description": "Exterior depth in mm."},
                    "side_thickness":   {"type": "number", "default": 18.0},
                    "bottom_thickness": {"type": "number", "default": 18.0},
                    "top_thickness":    {"type": "number", "default": 18.0},
                    "back_thickness":   {"type": "number", "default": 6.0},
                    "drawer_box_thickness": {
                        "type": "number", "default": 15.0,
                        "description": "Drawer box stock (sides + sub-front/back) in mm; bottoms follow the per-drawer size rule.",
                    },
                    "drawer_box_prefinished": {
                        "type": "boolean", "default": False,
                        "description": "Build drawer boxes (incl. bottoms) from pre-finished Baltic birch — separate cutlist material and sheet pricing, no finishing step. Workshop presets default true.",
                    },
                    "face_material": {
                        "type": "string",
                        "default": "finished_wood",
                        "description": (
                            "Cutlist material for show faces (false fronts + door "
                            "leaves). 'baltic_birch' / 'baltic_birch_prefinished' "
                            "pool into sheet optimisation; any other string stays "
                            "a labeled order-out group (e.g. 'rift_white_oak_ply')."
                        ),
                    },
                    "carcass_material": {
                        "type": "string",
                        "default": "baltic_birch",
                        "description": (
                            "Cutlist material for carcass panels (sides, top, "
                            "bottom, shelves, dividers). Sheet materials (Baltic "
                            "birch stocks or names ending '_ply') pack per "
                            "(material, thickness) and price from PRICE_LIST when "
                            "listed. Backs and drawer boxes keep their own stock."
                        ),
                    },
                    "edge_band_mode": {
                        "type": "string",
                        "enum": ["none", "hot_melt", "hardwood"],
                        "default": "none",
                        "description": (
                            "Edge banding for exposed ply edges (fronts of "
                            "carcass panels + all four edges of faces/doors). "
                            "'hot_melt' = iron-on veneer rolls, dims "
                            "unchanged; 'hardwood' = solid strips, panel "
                            "CORES cut smaller so finished dims hold. "
                            "Also a SharedDesign token."
                        ),
                    },
                    "edge_band_thickness_mm": {
                        "type": "number",
                        "default": 0.6,
                        "description": (
                            "Band thickness in mm (hardwood: 3.2–6.4 "
                            "typical; hot-melt ~0.6)."
                        ),
                    },
                    "edge_band_material": {
                        "type": "string",
                        "default": "",
                        "description": (
                            "Banding species; empty derives from each "
                            "panel's material (rift white oak ply → "
                            "white_oak rolls, Baltic birch → white_birch)."
                        ),
                    },
                    "edge_band_stock": {
                        "type": ["object", "null"],
                        "default": None,
                        "description": (
                            "Purchasable hardwood banding stock "
                            "{width_mm, length_mm, price_usd, "
                            "strip_width_mm (default 20)}. Board thickness "
                            "IS edge_band_thickness_mm. When set, the BOM "
                            "band line is priced in boards-to-order via "
                            "real piece-into-strip packing (flags edges "
                            "with no flush-trim overhang or longer than "
                            "the stock). Omit for the unpriced "
                            "rip-from-offcuts line. SharedDesign token."
                        ),
                    },
                    "face_gap_mm": {
                        "type": "number", "default": 4.0,
                        "description": (
                            "Vertical reveal between stacked show faces (mm, "
                            "total per joint) — sets false-front heights on "
                            "cutlists and renders alike. SharedDesign token."
                        ),
                    },
                    "furniture_top": {
                        "type": "boolean", "default": False,
                        "description": (
                            "Stored 'furniture top' style: the top panel "
                            "gains a front cap strip flush with the face "
                            "plane (emitted on cutlists) and the lowest face "
                            "drops to the carcass underside. SharedDesign "
                            "token."
                        ),
                    },
                    "carcass_corner_style": {
                        "type": "string",
                        "enum": ["butt", "miter"],
                        "default": "butt",
                        "description": (
                            "'miter': the four exterior corners are 45° "
                            "miters — top/bottom cut to FULL exterior "
                            "width (long-point dims), all four panels "
                            "beveled; divider/shelf joints stay butt "
                            "tenons. Floating-tenon carcasses only. Also "
                            "a SharedDesign token."
                        ),
                    },
                    "back_style": {
                        "type": "string",
                        "enum": ["full_height", "under_top"],
                        "default": "full_height",
                        "description": (
                            "'under_top': the top panel is cut to FULL "
                            "depth (rear edge flush with the sides) and "
                            "the back stops at its underside — no back "
                            "edge visible from above or the sides. Butt "
                            "corners + tenon/screw/biscuit/dowel joinery "
                            "only. Also a SharedDesign token."
                        ),
                    },
                    "back_capture": {
                        "type": "string",
                        "enum": ["pocket", "rabbet", "half_lap", "dado"],
                        "default": "pocket",
                        "description": (
                            "How the back is HELD — independent of "
                            "back_style and carcass_joinery. 'pocket': the "
                            "legacy let-in back, glued onto the rear edges "
                            "of the interior panels, nothing machined. "
                            "'rabbet': a rabbet in the rear inner edge of "
                            "the sides, top and bottom; the back drops in "
                            "from behind after glue-up and finishes flush. "
                            "'half_lap': both halves rabbeted so they lap — "
                            "twice the glue area and the panel is trapped "
                            "fore-and-aft by the step (needs a back at least "
                            "9 mm thick). 'dado': a groove in all four "
                            "members; the back SLIDES IN during glue-up and "
                            "is trapped on four edges, so nothing shows from "
                            "any angle and a solid-wood back can float and "
                            "move. The three machined captures seat the back "
                            "inside the case perimeter, so back_style only "
                            "changes what you see under 'pocket'."
                        ),
                    },
                    "back_groove_setback": {
                        "type": "number",
                        "default": 12.0,
                        "description": (
                            "back_capture 'dado' only: material left "
                            "standing behind the groove, i.e. how far the "
                            "back sits forward of the case rear (mm)."
                        ),
                    },
                    "drawer_config": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "prefixItems": [
                                {"type": "number"},
                                {"type": "string"},
                                {"type": "object"},
                            ],
                            "minItems": 2,
                            "maxItems": 3,
                        },
                        "description": (
                            "Flat stack of [height_mm, slot_type] pairs bottom-to-top; "
                            "optional third element per row is a per-opening options dict."
                        ),
                        "default": [],
                    },
                    "columns": {
                        "type": "array",
                        "description": (
                            "Multi-column layout; mirrors design_multi_column_cabinet. "
                            "If provided, drawer_config is ignored."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "width_mm": {"type": "number"},
                                "drawer_config": {
                                    "type": "array",
                                    "items": {
                                        "type": "array",
                                        "prefixItems": [
                                            {"type": "number"},
                                            {"type": "string"},
                                            {"type": "object"},
                                        ],
                                        "minItems": 2,
                                        "maxItems": 3,
                                    },
                                    "default": [],
                                },
                            },
                            "required": ["width_mm"],
                        },
                    },
                    "drawer_slide": {"type": "string", "default": "blum_tandem_550h"},
                    "door_hinge":   {"type": "string", "default": "blum_clip_top_blumotion_110_full"},
                    "drawer_pull": {
                        "type": "string",
                        "description": "Pull catalog key applied to every drawer slot.",
                    },
                    "door_pull": {
                        "type": "string",
                        "description": "Pull catalog key applied to every door / door_pair slot.",
                    },
                    "drawer_pull_vertical": {
                        "type": "string",
                        "enum": ["center", "upper_third", "lower_third"],
                        "default": "center",
                    },
                    "door_pull_vertical": {
                        "type": "string",
                        "enum": ["center", "upper_third", "lower_third"],
                        "default": "center",
                    },
                    "pull_preset": {
                        "type": "string",
                        "description": "Named pull preset key (see list_pull_presets). Populates drawer_pull, door_pull, and orientation. Explicit drawer_pull/door_pull fields override the preset.",
                    },
                },
                "required": ["width", "height", "depth"],
            },
        ),
        types.Tool(
            name="suggest_proportions",
            description=textwrap.dedent("""\
                Compare all four proportion presets (equal / subtle / classic / golden)
                side-by-side for a given cabinet's interior dimensions.

                For drawers: returns per-preset heights (mm + fractional inches) and a
                viability flag — if a preset would produce a top drawer below the 75 mm
                minimum it is marked not viable with a reason rather than raising an error.

                For columns: returns per-preset column widths (mm). Column widths are
                always viable for standard cabinet dimensions.

                Omit num_drawers to skip drawer suggestions; omit num_columns to skip
                column suggestions. At least one must be provided.

                Use this before committing to a proportion preset in design_cabinet or
                design_multi_column_cabinet.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "width":  {"type": "number", "description": "Exterior width in mm."},
                    "height": {"type": "number", "description": "Exterior height in mm."},
                    "depth":  {"type": "number", "description": "Exterior depth in mm (accepted but not used in proportion math)."},
                    "side_thickness":   {"type": "number", "default": 18},
                    "bottom_thickness": {"type": "number", "default": 18},
                    "top_thickness":    {"type": "number", "default": 18},
                    "num_drawers": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Number of drawer openings to compare across all presets.",
                    },
                    "num_columns": {
                        "type": "integer",
                        "minimum": 2,
                        "description": "Number of columns to compare across all presets.",
                    },
                    "wide_index": {
                        "type": "integer",
                        "description": "0-based index of the accent (wide) column (used with num_columns).",
                    },
                },
                "required": ["width", "height", "depth"],
            },
        ),
        types.Tool(
            name="list_pull_presets",
            description=textwrap.dedent("""\
                List available pull presets. Each preset bundles a drawer pull,
                door pull, and orientation/inset settings under a single key.

                Pass the preset key to design_cabinet, design_multi_column_cabinet,
                or visualize_cabinet as ``pull_preset`` to apply all settings at once.
                Individual ``drawer_pull`` / ``door_pull`` fields still override the
                preset when supplied alongside it.
            """),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="design_project",
            description=textwrap.dedent("""\
                Build a multi-cabinet project: several cabinets designed to live
                together (e.g. three matching sideboards, a kitchen run).

                The 'shared' block carries design tokens applied to every child
                cabinet at construction time — material thicknesses, joinery,
                hardware brand, pull preset, leg key. A child's 'config' block
                accepts the same parameters as design_cabinet / design_multi_column_cabinet;
                any field set there overrides the shared value for that child.

                The resolved project is persisted to
                ~/.cabineteer/projects/<name>.json so evaluate_project and
                generate_project_cutlist can be called by project name later.

                If the name is already taken, the call is refused unless
                overwrite=true — use update_project for a delta edit of the
                existing design, or duplicate_project to fork it first.

                Returns per-cabinet resolved configs plus a divergence note for
                any shared token a child overrode.

                An optional 'worktop' block models a desk/counter slab spanning
                part of the run (e.g. between two flanking drawer towers). It
                is rendered by visualize_project, included in the project
                cutlist as a finished-stock panel, and persists with the
                project.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Project name; used as the filename stem."},
                    "overwrite": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Replace an existing saved project of the same "
                            "name. Without it, a name collision is an error."
                        ),
                    },
                    "notes": {"type": "string", "description": "Optional human-readable notes."},
                    "wall_width_mm": {
                        "type": "number",
                        "description": "Available wall run in mm. When set, consistency checks flag a run wider than the wall (error) and report leftover gap (info).",
                    },
                    "shared": {
                        "type": "object",
                        "description": "Design tokens to apply to every child cabinet. All fields optional.",
                        "properties": {
                            "side_thickness":   {"type": "number"},
                            "bottom_thickness": {"type": "number"},
                            "top_thickness":    {"type": "number"},
                            "shelf_thickness":  {"type": "number"},
                            "back_thickness":   {"type": "number"},
                            "drawer_box_thickness": {"type": "number"},
                            "drawer_box_prefinished": {"type": "boolean"},
                            "face_material": {"type": "string"},
                            "carcass_material": {"type": "string"},
                            "carcass_joinery":  {"type": "string"},
                            "drawer_joinery":   {"type": "string"},
                            "drawer_corner_lip_mm": {"type": "number"},
                            "drawer_slide":     {"type": "string"},
                            "door_hinge":       {"type": "string"},
                            "drawer_pull":      {"type": "string"},
                            "door_pull":        {"type": "string"},
                            "leg_key":          {"type": "string"},
                            "pull_preset":      {"type": "string"},
                        },
                    },
                    "cabinets": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "config": {
                                    "type": "object",
                                    "description": "Per-cabinet config — same shape as design_cabinet / design_multi_column_cabinet args.",
                                },
                            },
                            "required": ["name", "config"],
                        },
                    },
                    "worktop": _worktop_schema(),
                },
                "required": ["name", "cabinets"],
            },
        ),
        types.Tool(
            name="update_project",
            description=textwrap.dedent("""\
                Delta-edit a saved project without re-submitting the whole
                payload. Loads the snapshot, applies the patch, validates the
                result exactly like a design_project submission, and saves.

                Patch fields (all optional except 'name'):
                - notes: replaces the notes string.
                - wall_width_mm: replaces the wall constraint; null clears it.
                - worktop: shallow-merged into the stored worktop spec
                  (creating one if absent — width_mm/depth_mm required then);
                  null removes the worktop entirely.
                - shared: shallow-merged into the shared token block; a null
                  value removes that token.
                - cabinets: per-cabinet patches matched by 'name'. 'config'
                  is shallow-merged into that cabinet's stored config (null
                  removes a key, reverting it to the shared token / default);
                  patched keys that collide with an active shared token are
                  auto-pinned as overrides so the patch sticks. 'new_name'
                  renames the cabinet, 'remove': true drops it, 'add': true
                  appends a new cabinet (config required).

                Note config merging is SHALLOW: to change one drawer, pass
                that cabinet's full 'columns' (or 'openings' / 'drawer_config')
                value back with the row edited — load_project returns the
                current value to edit.

                Returns the same per-cabinet summary as design_project plus a
                change log.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Saved project to update (see list_projects)."},
                    "notes": {"type": "string"},
                    "wall_width_mm": {"type": ["number", "null"]},
                    "worktop": {
                        "type": ["object", "null"],
                        "description": (
                            "Worktop deltas, shallow-merged into the stored "
                            "spec (see design_project's worktop block for the "
                            "fields); null removes the worktop."
                        ),
                    },
                    "shared": {
                        "type": "object",
                        "description": "Shared-token deltas; null values clear a token.",
                    },
                    "cabinets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name":     {"type": "string"},
                                "config":   {"type": "object"},
                                "overrides": {
                                    "type": "array", "items": {"type": "string"},
                                    "description": "Full replacement of the cabinet's override list (advanced).",
                                },
                                "new_name": {"type": "string"},
                                "remove":   {"type": "boolean"},
                                "add":      {"type": "boolean"},
                            },
                            "required": ["name"],
                        },
                    },
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="list_projects",
            description=textwrap.dedent("""\
                List every design saved under ~/.cabineteer/projects/ —
                name, cabinet count and names, total run width, notes, and
                last-modified time. Use this to discover what can be loaded
                with load_project or batched with generate_project_cutlist.

                Pass 'query' to filter: case-insensitive substring match
                over project name, notes, and cabinet names — e.g.
                query="shop" finds shop-bench projects via their notes.

                Sorted newest-first by default (sort="name" for
                alphabetical). Dev artifacts (names starting with eval_/
                test_/smoke_/_) are hidden unless include_all=true — a
                'query' always searches everything.

                A single cabinet is saved as a one-cabinet project via
                design_project, so this is the catalogue of all durable
                designs, not just multi-cabinet runs.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Case-insensitive substring filter over name, "
                            "notes, and cabinet names. Omit for all projects."
                        ),
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["recent", "name"],
                        "default": "recent",
                        "description": "recent = newest modified first.",
                    },
                    "include_all": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Include dev artifacts (eval_/test_/smoke_/_ "
                            "prefixed names) in the listing."
                        ),
                    },
                },
            },
        ),
        types.Tool(
            name="rename_project",
            description=textwrap.dedent("""\
                Rename a saved project — updates both the snapshot filename
                and the embedded project name. Refuses to overwrite an
                existing project. Previously generated cutlist/visualization
                files keep their old stems (they are output artifacts).
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Current project name."},
                    "new_name": {"type": "string", "description": "New project name."},
                },
                "required": ["name", "new_name"],
            },
        ),
        types.Tool(
            name="duplicate_project",
            description=textwrap.dedent("""\
                Fork a saved project: copy the snapshot under a new name so
                design changes can be explored without touching the original
                (like branching in git). The copy records its lineage —
                'forked_from' + 'forked_at' appear in load_project and
                list_projects output. Refuses to overwrite an existing
                project. Pass 'notes' to replace the copied notes (the
                original's notes often describe that build's final decisions).
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "name":     {"type": "string", "description": "Source project (see list_projects)."},
                    "new_name": {"type": "string", "description": "Name for the fork; must not already exist."},
                    "notes":    {"type": "string", "description": "Optional replacement notes for the fork."},
                },
                "required": ["name", "new_name"],
            },
        ),
        types.Tool(
            name="delete_project",
            description=textwrap.dedent("""\
                PERMANENTLY delete a saved project snapshot from
                ~/.cabineteer/projects/. There is no undo — confirm with
                the user before deleting anything they might still want.
                Generated cutlist/visualization files are not touched.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Project name to delete."},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="load_project",
            description=textwrap.dedent("""\
                Load a saved project's full payload back from
                ~/.cabineteer/projects/<name>.json.

                Returns the durable 'project' payload (shared design tokens,
                per-cabinet configs with any per-opening options, notes,
                wall width) plus a resolved per-cabinet summary. The payload
                is exactly the shape design_project accepts — pass it back
                (same name) to continue editing, or reference the project by
                name in evaluate_project / visualize_project /
                generate_project_cutlist.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Saved project name (see list_projects).",
                    },
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="evaluate_project",
            description=textwrap.dedent("""\
                Run evaluate_cabinet against every child cabinet in a project,
                plus cross-cabinet sanity checks (matching depth, matching
                exterior height).

                Pass either a 'project_name' to load a previously persisted
                project, or an inline 'project' payload (same shape as
                design_project input).
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_name": {
                        "type": "string",
                        "description": "Name of a previously persisted project (see design_project).",
                    },
                    "project": {
                        "type": "object",
                        "description": "Inline project payload — same shape as design_project input.",
                    },
                },
            },
        ),
        types.Tool(
            name="generate_project_cutlist",
            description=textwrap.dedent("""\
                Generate a combined cutlist, sheet-layout, and hardware BOM for
                every cabinet in a project. Identical panels (same material,
                thickness, dimensions, grain) are merged across cabinets so the
                sheet optimizer packs everyone together.

                Pass either 'project_name' to load a persisted project, an
                inline 'project' payload, or 'project_names' (a list of saved
                projects — see list_projects) to batch several designs into
                ONE merged cutlist, sheet optimization, and hardware BOM.
                Output files land in ~/.cabineteer/cutlists/<name>/ (for a
                batch, <name> is 'batch_name' or the joined project names).

                Batches keep per-project identity: identical panels from
                different projects stay separate rows tagged with their
                project (sheet optimization still pools everything, so no
                material is wasted), the layout HTML/PDF colours panels by
                project with a legend, CSV/parts tables gain a Project
                column, and hardware BOM lines carry a by_project piece
                breakdown.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_name": {"type": "string"},
                    "project": {"type": "object"},
                    "project_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": (
                            "Batch mode: names of saved projects to combine "
                            "into one merged cutlist. Takes precedence over "
                            "project_name/project."
                        ),
                    },
                    "batch_name": {
                        "type": "string",
                        "description": (
                            "Output name for a project_names batch (file stem "
                            "and cutlist directory). Defaults to the project "
                            "names joined with '-'."
                        ),
                    },
                    "sheet_length": {"type": "number", "default": 2440},
                    "sheet_width":  {"type": "number", "default": 1220},
                    "optimizer_overrides": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "string",
                            "enum": ["auto", "opcut", "rectpack", "strip",
                                     "rips_first"],
                        },
                        "description": (
                            "Per-group optimizer overrides. Keys match "
                            "(material, thickness) groups by precedence "
                            "'material@thickness' > 'material' > "
                            "'@thickness' (e.g. {'@6': 'opcut'} keeps 6 mm "
                            "stock on opcut while the run-level optimizer "
                            "handles the rest). Non-default; omit for a "
                            "single optimizer."
                        ),
                    },
                    "sheet_size_overrides": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                        "description": (
                            "Per-material sheet size overrides, "
                            "{material: [length_mm, width_mm]} — e.g. "
                            "{'rift_white_oak_ply': [2453, 1234]} when one "
                            "stock runs oversize while the rest stay at "
                            "sheet_length x sheet_width."
                        ),
                    },
                    "kerf":         {"type": "number", "default": 3.2},
                    "format":       {"type": "string", "enum": ["json", "csv", "both"], "default": "both"},
                    "optimizer":    {"type": "string", "enum": ["auto", "opcut", "rectpack", "strip", "rips_first"], "default": "auto"},
                    "paper":        {"type": "string", "enum": ["letter", "a4"], "default": "letter", "description": "PDF paper size — US Letter by default; 'a4' for A4."},
                },
            },
        ),
        types.Tool(
            name="generate_assembly_instructions",
            description=textwrap.dedent("""\
                Generate step-by-step CARCASS ASSEMBLY instructions for every
                cabinet in a project — floating-tenon (Festool Domino)
                construction only.

                For each structurally distinct cabinet design (identical
                cabinets collapse into one section with a ×N count) the
                document contains: DF 500 machine settings (cutter, plunge
                depth, fence height, tight/slotted width strategy), a joint
                schedule with tenon counts and mortise centres measured from
                the FRONT edge, per-panel mortise maps (SVG/PDF drawings —
                red = face mortises, blue = edge mortises), a consumables
                count (beech tenons plus how many PETG dry-fit tenons to
                print), and an ordered assembly sequence in which a full
                no-glue DRY FIT with 3D-printed reduced-size tenons
                (printables.com model 689403) always precedes glue-up.

                Tenon size follows carcass stock thickness: 5×30 for panels
                up to 19 mm (3/4" ply), 8×40 above — matching the hardware
                BOM. Part IDs match the project's cutlist. Files land in
                ~/.cabineteer/assembly/<project>/.
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_name": {
                        "type": "string",
                        "description": "Name of a previously persisted project (see design_project).",
                    },
                    "project": {
                        "type": "object",
                        "description": "Inline project payload — same shape as design_project input.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["html", "pdf", "both"],
                        "default": "both",
                    },
                    "paper": {
                        "type": "string",
                        "enum": ["letter", "a4"],
                        "default": "letter",
                        "description": "PDF paper size — US Letter by default; 'a4' for A4.",
                    },
                },
            },
        ),
        types.Tool(
            name="visualize_project",
            description=textwrap.dedent("""\
                Render every cabinet in a project as one 3D scene: cabinets are
                placed left-to-right at their run offsets (optionally separated
                by 'gap_mm'), exported to GLB, and wrapped in a self-contained
                HTML viewer.

                Pass either 'project_name' to load a persisted project or an
                inline 'project' payload (same shape as design_project input).
                A project 'worktop' block renders as a slab (plus optional
                support legs) at its stored run position — make sure gap_mm
                matches the layout the worktop was measured against.
                Requires the full install (CadQuery).
            """),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_name": {"type": "string"},
                    "project": {"type": "object"},
                    "gap_mm": {
                        "type": "number", "default": 0,
                        "description": "Gap between adjacent cabinets in mm (0 = butted).",
                    },
                    "furniture_top": {
                        "type": "boolean",
                        "description": (
                            "Force every cabinet in the run to (or out of) the "
                            "'furniture top, flush bottom' style. Omit to let "
                            "each cabinet's stored furniture_top flag decide."
                        ),
                    },
                    "manga": _manga_schema(),
                    "shared_junction_feet": {
                        "type": "boolean", "default": False,
                        "description": (
                            "Butted runs (gap_mm=0, 2+ cabinets) only: render "
                            "ONE pair of feet centered on each cabinet "
                            "junction instead of each cabinet carrying its "
                            "own inner pair; outer corners keep theirs. Foot "
                            "count becomes 2×(cabinet_count+1). Reflect the "
                            "reduced count in the hardware BOM by setting "
                            "per-cabinet leg_count (e.g. 4/2/2 for a trio)."
                        ),
                    },
                    "output_dir":   {"type": "string", "default": "~/.cabineteer/visualizations"},
                    "open_browser": {"type": "boolean", "default": True},
                    "tolerance":    {"type": "number", "default": 0.1},
                    "finish": {
                        "type": "string",
                        "enum": sorted(_WOOD_FINISHES),
                        "description": (
                            "Wood finish rendered in the viewer as a procedural "
                            "grain texture on the carcass, drawer faces, and "
                            "doors (pull hardware keeps its metal look). "
                            "Omit for flat panel colors."
                        ),
                    },
                    "drawer_box_finish": {
                        "type": "string",
                        "enum": sorted(_WOOD_FINISHES),
                        "description": (
                            "Finish for drawer-box meshes. Defaults to "
                            "baltic_birch whenever 'finish' is set; pass the "
                            "same key as 'finish' for a uniform look."
                        ),
                    },
                    "grain_direction": {
                        "type": "string",
                        "enum": ["vertical", "horizontal"],
                        "default": "vertical",
                        "description": (
                            "Grain orientation on show surfaces (carcass, "
                            "drawer faces, doors). Drawer boxes are always "
                            "horizontal — box sides are cut with the grain "
                            "along their length."
                        ),
                    },
                },
            },
        ),
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Tool handlers
# ──────────────────────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    handler = TOOL_DISPATCH.get(name)
    if handler is None:
        return _err(f"Unknown tool: {name}")
    try:
        return await handler(arguments)
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}")


# ── list_pull_presets ─────────────────────────────────────────────────────────


async def _tool_list_pull_presets(args: dict) -> list[types.TextContent]:
    from .hardware import PULL_PRESETS
    presets = [
        {
            "key": p.key,
            "style_name": p.style_name,
            "description": p.description,
            "drawer_pull": p.drawer_pull,
            "door_pull": p.door_pull,
            "door_pull_inset_mm": p.door_pull_inset_mm,
        }
        for p in PULL_PRESETS.values()
    ]
    return _ok({"presets": presets, "count": len(presets)})


# ── list_hardware ─────────────────────────────────────────────────────────────

async def _tool_list_hardware(args: dict) -> list[types.TextContent]:
    category = args.get("category", "all")
    result: dict[str, Any] = {}

    if category in ("slides", "all"):
        result["slides"] = {
            key: {
                "name": s.name,
                "slide_type": s.slide_type.value,
                "available_lengths_mm": list(s.available_lengths),
                "extension": s.extension,
                "nominal_side_clearance_mm": s.nominal_side_clearance,
                "clearance_reference": s.clearance_reference.value,
                "min_bottom_clearance_mm": s.min_bottom_clearance,
                "max_load_kg": s.max_load_kg,
                "min_drawer_height_mm": s.min_drawer_height,
            }
            for key, s in SLIDES.items()
        }

    if category in ("hinges", "all"):
        result["hinges"] = {
            key: {
                "name": h.name,
                "overlay_type": h.overlay_type.value,
                "overlay_mm": h.overlay,
                "cup_diameter_mm": h.cup_diameter,
                "cup_boring_distance_mm": h.cup_boring_distance,
                "opening_angle_deg": h.opening_angle,
                "soft_close": h.soft_close,
                "max_door_weight_kg": h.max_door_weight_kg,
                "part_number": h.part_number,
            }
            for key, h in HINGES.items()
        }

    if category in ("legs", "all"):
        result["legs"] = {
            key: {
                "name": l.name,
                "height_mm": l.height_mm,
                "base_diameter_mm": l.base_diameter_mm,
                "is_adjustable": l.is_adjustable,
                "adjustment_range_mm": l.adjustment_range_mm,
                "load_capacity_kg": l.load_capacity_kg,
                "finish": l.finish,
                "part_number": l.part_number,
                "notes": l.notes,
            }
            for key, l in LEGS.items()
        }

    if category in ("pulls", "all"):
        brand_filter = (args.get("brand") or "").strip().lower()
        mount_filter = (args.get("mount_style") or "").strip().lower()
        pulls_out: dict[str, dict[str, Any]] = {}
        for key, p in PULLS.items():
            if brand_filter and brand_filter not in p.brand.lower():
                continue
            if mount_filter and p.mount_style.value != mount_filter:
                continue
            pulls_out[key] = {
                "name":           p.name,
                "brand":          p.brand,
                "model_number":   p.model_number,
                "url":            p.url,
                "style":          p.style,
                "material":       p.material,
                "finish":         p.finish,
                "mount_style":    p.mount_style.value,
                "pack_quantity":  p.pack_quantity,
                "cc_mm":          p.cc_mm,
                "length_mm":      p.length_mm,
                "projection_mm":  p.projection_mm,
                "is_knob":        p.is_knob,
                "tags":           list(p.tags),
            }
        result["pulls"] = pulls_out
        # Provide a handy count so clients don't have to walk the dict.
        result["pulls_count"] = len(pulls_out)

    return _ok(result)


# ── list_joinery_options ──────────────────────────────────────────────────────

async def _tool_list_joinery(args: dict) -> list[types.TextContent]:
    from .joinery import DrawerJoinerySpec

    drawer_styles = {
        "butt":        "Simple butt joint — glue and fasteners only, no interlocking cuts.",
        "qqq":         "Quarter-Quarter-Quarter locking rabbet (Stephen Phipps). "
                       "All cuts = material_thickness ÷ 2. Stronger than dovetail, table-saw only.",
        "half_lap":    "Half-lap: side dado depth = side_thickness ÷ 2; "
                       "front/back channel matches.",
        "drawer_lock": "Drawer-lock (router bit required). Two-step interlock — "
                       "side dado + inner step for positive mechanical lock.",
    }

    carcass_methods = {
        "dado_rabbet":     "Dadoes for shelves/bottom, rabbet for back. No additional hardware.",
        "floating_tenon":  "Festool Domino oval mortise/tenon. DF 500 (≤8 mm) or DF 700 (10/14 mm).",
        "pocket_screw":    "Kreg-style 15° angled pocket. Fast, tool-accessible, no mortise needed.",
        "biscuit":         "Plate-joinery biscuits (#0, #10, #20). Alignment aid + glue surface.",
        "dowel":           "Round dowels — compatible with 32 mm European shelf-pin grid.",
    }

    domino_sizes = {
        key: {
            "tenon_length_mm":          d.tenon_length,
            "tenon_thickness_mm":       d.tenon_thickness,
            "mortise_length_mm":        d.mortise_length,
            "mortise_width_mm":         d.mortise_width,
            "mortise_depth_per_side_mm": d.mortise_depth_per_side,
            "machine": d.machine,
        }
        for key, d in DOMINO_SIZES.items()
    }

    return _ok({
        "drawer_joinery_styles": drawer_styles,
        "carcass_joinery_methods": carcass_methods,
        "domino_sizes": domino_sizes,
    })


# ── design_cabinet ────────────────────────────────────────────────────────────

#: CarcassPanel.kind → (payload key, the two axis names it reports).
#: A side and a divider stand up, so their long axis is a HEIGHT; a top,
#: bottom or shelf lies down, so it is a WIDTH. The front-to-back dimension
#: of anything standing is a DEPTH — the two design tools used to disagree
#: about that (``design_cabinet`` called a side panel's 457 mm a "width",
#: ``design_multi_column_cabinet`` called the same number a "depth"), which
#: is the same class of drift this function exists to end.
_PANEL_AXES = {
    "side":    ("side_panel",     "height_mm", "depth_mm"),
    "bottom":  ("bottom_panel",   "width_mm",  "depth_mm"),
    "top":     ("top_panel",      "width_mm",  "depth_mm"),
    "divider": ("column_divider", "height_mm", "depth_mm"),
    "back":    ("back_panel",     "height_mm", "width_mm"),
    "shelf":   ("fixed_shelf",    "width_mm",  "depth_mm"),
    "floor":   ("door_floor",     "width_mm",  "depth_mm"),
}


def _payload_carcass_panels(
    cfg: CabinetConfig,
    columns_raw: list | None = None,
) -> dict:
    """The design tools' ``panels`` block, read off ``carcass_panel_dims``.

    The first document anyone reads is a design payload, at approval time,
    and it used to re-derive its own numbers: it reported the column divider
    at ``cfg.height`` while every other document cut it to the interior — a
    720 mm divider standing in its own 684 mm interior.

    Dimensions are FINISHED, as they are everywhere ``carcass_panel_dims``
    is read. Under hardwood banding the cutlist cuts a core one band
    thickness shorter on the front edge; that gap is a separate, still-open
    defect (the payload does not yet say which face its number means), and
    reporting the finished size here keeps this function honest about what
    it is quoting rather than silently changing which one it means.
    """
    out: dict = {}
    shelf_n = 0
    floor_n = 0
    for p in _carcass_panel_dims(cfg, columns_raw):
        key, long_axis, short_axis = _PANEL_AXES[p.kind]
        if p.kind == "shelf":
            shelf_n += 1
            key = f"fixed_shelf_{shelf_n}"
        elif p.kind == "floor":
            floor_n += 1
            key = f"door_floor_{floor_n}"
        block = {"qty": p.quantity, long_axis: p.length,
                 short_axis: p.width, "thickness_mm": p.thickness}
        if p.z is not None:
            block["height_from_bottom_mm"] = p.z
        if p.column is not None:
            # 0-based, matching the `index` this same payload reports in its
            # `columns` list forty lines below. They disagreed on the first
            # draft (column 1 beside index 0, same object, same JSON).
            block["column_index"] = p.column
        if p.bevel_ends:
            # The number is the LONG POINT of a 45° end, not a face
            # dimension: a reader who measures the finished panel across its
            # show face gets 2 x thickness less. Saying which face a number
            # means is the whole of D19; this is the same on another axis.
            block["ends"] = "45° bevel — dimension is the long point"
        out[key] = block
    return out


async def _tool_design_cabinet(args: dict) -> list[types.TextContent]:
    num_drawers       = args.pop("num_drawers", None)
    drawer_proportion = args.pop("drawer_proportion", None)
    proportions_used: dict = {}

    if num_drawers and not args.get("drawer_config"):
        preset = drawer_proportion or "classic"
        side_t   = float(args.get("side_thickness",   18))
        bottom_t = float(args.get("bottom_thickness", 18))
        top_t    = float(args.get("top_thickness",    18))
        interior_h = float(args["height"]) - bottom_t - top_t
        heights = _grad_heights(interior_h, int(num_drawers), preset)
        args["drawer_config"] = [[h, "drawer"] for h in heights]
        proportions_used["drawer_proportion"] = preset

    if args.get("drawer_config"):
        args["drawer_config"] = _sort_drawer_config(args["drawer_config"])

    cfg = _build_cabinet_config(args)

    # Interior dimensions.  Use the canonical ``interior_depth`` property so
    # the reported interior matches describe_design and the evaluator; panel
    # cut depths below come from back_capture_geometry, which is the same
    # source the cutlist and the 3D model read.
    interior_width  = cfg.width  - 2 * cfg.side_thickness
    interior_height = cfg.interior_height
    interior_depth  = cfg.interior_depth
    _geo = back_capture_geometry(cfg)

    # Panel sizes (parametric only, no CQ needed). Every number comes from
    # cabinet.carcass_panel_dims — the same source the cutlist cuts from —
    # so the first document anyone reads cannot contradict the paper.
    panels = _payload_carcass_panels(cfg)

    opening_stack = [
        {"height_mm": op.height_mm, "type": op.opening_type} for op in cfg.openings
    ]

    result = {
        "exterior": {"width_mm": cfg.width, "height_mm": cfg.height, "depth_mm": cfg.depth},
        "interior": {
            "width_mm":  interior_width,
            "height_mm": interior_height,
            "depth_mm":  interior_depth,
        },
        "joinery":  cfg.carcass_joinery.value,
        "back_style": cfg.back_style,
        "back_capture": cfg.back_capture,
        # What the capture actually does to the case, so the caller never
        # has to infer it from the panel dims.
        "back_seat": {
            "engagement_mm": _geo.engagement,
            "case_cut": (None if not _geo.machined else {
                "depth_mm": _geo.cut_depth,
                "run_mm": _geo.cut_run,
                "offset_from_rear_mm": _geo.cut_offset,
            }),
            "setback_mm": _geo.setback,
            "clear_depth_mm": _geo.clear_depth,
            "captive": _geo.captive,
        },
        "panels":   panels,
        "opening_stack": opening_stack,
        "adj_shelf_holes": cfg.adj_shelf_holes,
        "door_hinge": cfg.door_hinge,
        "drawer_pull": cfg.drawer_pull,
        "door_pull": cfg.door_pull,
    }
    if proportions_used:
        result["proportions_used"] = proportions_used
    return _ok(result)


# ── design_multi_column_cabinet ───────────────────────────────────────────────

async def _tool_design_multi_column_cabinet(args: dict) -> list[types.TextContent]:
    """Handler for the design_multi_column_cabinet tool."""
    num_columns       = args.pop("num_columns",       None)
    wide_index        = args.pop("wide_index",         None)
    column_proportion = args.pop("column_proportion",  None)
    num_drawers       = args.pop("num_drawers",        None)
    drawer_proportion = args.pop("drawer_proportion",  None)
    proportions_used: dict = {}

    # ── Resolve column widths ──────────────────────────────────────────────────
    if not args.get("columns"):
        if not num_columns:
            return _err("Provide either 'columns' or 'num_columns'.")
        side_t     = float(args.get("side_thickness", 18))
        n_cols     = int(num_columns)
        interior_w = float(args["width"]) - 2 * side_t
        available_w = interior_w - (n_cols - 1) * side_t  # subtract internal divider space
        col_preset = column_proportion or ("golden" if wide_index is not None else "equal")
        widths = _col_widths(available_w, n_cols, wide_index, col_preset)
        proportions_used["column_proportion"] = col_preset
        if wide_index is not None:
            proportions_used["wide_index"] = wide_index
        # Build placeholder column dicts (drawer_config filled below)
        args["columns"] = [{"width_mm": w} for w in widths]

    # ── Resolve drawer heights ─────────────────────────────────────────────────
    if num_drawers:
        bottom_t   = float(args.get("bottom_thickness", 18))
        top_t      = float(args.get("top_thickness",    18))
        interior_h = float(args["height"]) - bottom_t - top_t
        drw_preset = drawer_proportion or "classic"
        heights    = _grad_heights(interior_h, int(num_drawers), drw_preset)
        proportions_used["drawer_proportion"] = drw_preset
        # Fill in any column that has no explicit drawer_config
        for col in args["columns"]:
            if not col.get("drawer_config"):
                col["drawer_config"] = [[h, "drawer"] for h in heights]

    for col in args.get("columns", []):
        if col.get("drawer_config"):
            col["drawer_config"] = _sort_drawer_config(col["drawer_config"])

    cfg = _build_cabinet_config(args)

    interior_width  = cfg.interior_width
    interior_height = cfg.interior_height
    # Panel depths follow back_capture, the same source generate_cutlist
    # reads — the two tools must never report different cut sizes for one
    # design.
    _geo = back_capture_geometry(cfg)
    interior_depth  = cfg.interior_depth   # the datum, not a second derivation

    # Build per-column breakdown
    col_x = 0.0  # running interior x from left column
    col_details = []
    for i, col in enumerate(cfg.columns):
        stack = [{"height_mm": op.height_mm, "type": op.opening_type} for op in col.openings]
        stack_total = sum(op.height_mm for op in col.openings)
        col_details.append({
            "index":             i,
            "interior_width_mm": col.width_mm,
            "divider_left_x_mm": col_x - cfg.side_thickness if i > 0 else 0.0,
            "stack_total_mm":    stack_total,
            # Openings share the interior with their door floors, the same
            # way columns share the interior width with their dividers.
            "stack_fills_interior": abs(
                stack_total - _openings_span(
                    _bays_from_config(cfg, None)[i])) < 0.5,
            "opening_stack":     stack,
        })
        col_x += col.width_mm + cfg.side_thickness  # account for divider between columns

    col_sum = sum(c.width_mm for c in cfg.columns)
    n_dividers = max(len(cfg.columns) - 1, 0)

    # Every panel number comes from cabinet.carcass_panel_dims, the source
    # the cutlist cuts from. Before this, the divider was reported at
    # cfg.height here and cut to the interior height there.
    panels = _payload_carcass_panels(cfg)   # cfg.columns carries the split

    result = {
        "exterior":   {"width_mm": cfg.width, "height_mm": cfg.height, "depth_mm": cfg.depth},
        "interior":   {"width_mm": interior_width, "height_mm": interior_height, "depth_mm": interior_depth},
        "joinery":    cfg.carcass_joinery.value,
        "panels":     panels,
        "column_count":          len(cfg.columns),
        "column_widths_sum_mm":  col_sum,
        "interior_width_mm":     interior_width,
        "columns_fill_interior": abs(col_sum - (interior_width - n_dividers * cfg.side_thickness)) < 0.5,
        "columns":               col_details,
        "adj_shelf_holes":       cfg.adj_shelf_holes,
        "door_hinge":            cfg.door_hinge,
        "drawer_pull":           cfg.drawer_pull,
        "door_pull":             cfg.door_pull,
    }
    if proportions_used:
        result["proportions_used"] = proportions_used
    return _ok(result)


# ── evaluate_cabinet ──────────────────────────────────────────────────────────

async def _tool_evaluate_cabinet(args: dict) -> list[types.TextContent]:
    door_config_dicts = args.pop("door_configs", []) or []
    cfg = _build_cabinet_config(args)

    door_configs = [_build_door_config(d) for d in door_config_dicts]

    issues = evaluate_cabinet(
        cab_cfg=cfg,
        door_configs=door_configs if door_configs else None,
    )

    errors   = [i for i in issues if i.severity == Severity.ERROR]
    warnings = [i for i in issues if i.severity == Severity.WARNING]
    infos    = [i for i in issues if i.severity == Severity.INFO]

    return _ok({
        "summary": {
            "errors":   len(errors),
            "warnings": len(warnings),
            "info":     len(infos),
            "pass":     len(errors) == 0,
        },
        "issues": _issues_to_dicts(issues),
    })


# ── design_door ───────────────────────────────────────────────────────────────

async def _tool_design_door(args: dict) -> list[types.TextContent]:
    cfg = _build_door_config(args)
    hinge = cfg.hinge

    result: dict[str, Any] = {
        "door_width_mm":      cfg.door_width,
        "door_height_mm":     cfg.door_height,
        "door_thickness_mm":  cfg.door_thickness,
        "num_doors":          cfg.num_doors,
        "overlay_type":       hinge.overlay_type.value,
        "overlay_mm":         hinge.overlay,
        "hinges_per_door":    cfg.hinge_count,
        "total_hinges":       cfg.total_hinge_count,
        "hinge_positions_z_mm": cfg.hinge_positions_z,
        "hinge": {
            "key":             args.get("hinge_key", "blum_clip_top_blumotion_110_full"),
            "name":            hinge.name,
            "cup_diameter_mm": hinge.cup_diameter,
            "cup_boring_distance_mm": hinge.cup_boring_distance,
            "soft_close":      hinge.soft_close,
            "part_number":     hinge.part_number,
        },
        "gaps": {
            "top_mm":     cfg.gap_top,
            "bottom_mm":  cfg.gap_bottom,
            "side_mm":    cfg.gap_side,
            "between_mm": cfg.gap_between,
        },
    }

    if cfg.pull_key is not None:
        from .cutlist import pull_line_from_door
        from .evaluation import check_door_pull

        placements = cfg.pull_placements  # per-leaf placements
        pull_issues = check_door_pull(cfg)
        bom_line = pull_line_from_door(cfg)
        result["pull"] = {
            "key":                 cfg.pull_key,
            "placements_per_leaf": _pull_placements_to_dicts(placements),
            "pulls_per_leaf":      len(placements),
            "total_pulls":         cfg.total_pull_count,
            "vertical_policy":     cfg.pull_vertical,
            "issues":              _issues_to_dicts(pull_issues),
            "bom":                 _hardware_line_to_dict(bom_line) if bom_line else None,
        }

    return _ok(result)


# ── design_drawer ─────────────────────────────────────────────────────────────

async def _tool_design_drawer(args: dict) -> list[types.TextContent]:
    cfg = _build_drawer_config(args)
    joinery = cfg.joinery
    slide   = cfg.slide

    joinery_info: dict[str, Any] = {
        "style": joinery.style.value,
        "requires_router_bit":     joinery.requires_router_bit,
        "requires_true_thickness": joinery.requires_true_thickness,
    }
    if joinery.style.value != "butt":
        joinery_info["side_dado_depth_x_mm"]  = joinery.side_dado_depth_x
        joinery_info["side_dado_depth_y_mm"]  = joinery.side_dado_depth_y
        joinery_info["fb_channel_depth_x_mm"] = joinery.fb_channel_depth_x
        joinery_info["fb_channel_depth_y_mm"] = joinery.fb_channel_depth_y
        joinery_info["side_tongue_width_mm"]  = joinery.side_tongue_width
    # Which piece wraps the corner — the fact that decides both part lengths.
    joinery_info["corner_lap"] = joinery.corner_lap
    if joinery.laps_front:
        joinery_info["lip_mm"] = joinery.lip
        joinery_info["socket_depth_mm"] = joinery.socket_depth
        joinery_info["lip_is_measured"] = cfg.corner_lip_mm is not None
    else:
        joinery_info["engagement_x_mm"] = joinery.engagement_x

    from .drawer import snap_to_standard_box_height, STANDARD_BOX_HEIGHTS
    slide_length = slide.slide_length_for_depth(cfg.opening_depth)
    raw_height   = _raw_box_height(cfg)
    std_height   = snap_to_standard_box_height(raw_height)

    result: dict[str, Any] = {
        # Width comes in three parts because one number cannot be checked:
        # the OUTSIDE is what you build, the INSIDE is what the slide maker
        # constrains (Blum: opening − 42 mm, "must equal"), and the GAP is
        # where the box sits in the opening. Print all three so a box can be
        # measured against the rule instead of against the drawing.
        "box_width_mm":               cfg.box_width,          # outside
        "box_inside_width_mm":        cfg.box_inside_width,
        "box_side_gap_mm":            cfg.side_gap,           # per side, air
        "box_width_basis": (
            f"outside = opening {cfg.opening_width:g} − "
            f"{2 * slide.nominal_side_clearance:g} + 2 × {cfg.side_thickness:g} mm side"
            if slide.clearance_reference is ClearanceReference.INSIDE else
            f"outside = opening {cfg.opening_width:g} − "
            f"{2 * slide.nominal_side_clearance:g}"
        ),
        "box_height_mm":              cfg.box_height,   # snapped when use_standard_height=True
        "box_height_raw_mm":          raw_height,       # clearance-adjusted, before snapping
        "standard_box_height_mm":     std_height,       # always the snapped value for reference
        "use_standard_height":        cfg.use_standard_height,
        "standard_heights_available": list(STANDARD_BOX_HEIGHTS),
        "box_depth_mm":               cfg.box_depth,
        # Cut lengths, from the joint. The box's outside dimensions are NOT
        # the part lengths: whichever piece wraps the corner runs full
        # length and the other is cut short by what the joint eats.
        "side_panel_length_mm":       cfg.side_panel_length,
        "front_back_panel_length_mm": cfg.front_back_panel_length,
        "bottom_panel_width_mm":      cfg.bottom_panel_width,
        "bottom_panel_depth_mm":      cfg.bottom_panel_depth,
        "side_thickness_mm":       cfg.side_thickness,
        "front_back_thickness_mm": cfg.front_back_thickness,
        "bottom_thickness_mm":     cfg.bottom_thickness,
        "bottom_thickness_auto":   "bottom_thickness" not in args,
        "slide": {
            "name":                     slide.name,
            "selected_length_mm":       slide_length,
            "nominal_side_clearance_mm": slide.nominal_side_clearance,
            # Which drawer-box face that clearance is measured to. Undermount
            # runners quote it to the INSIDE face, side-mount slides to the
            # OUTSIDE — the difference is 2 × side thickness of box width.
            "clearance_reference":      slide.clearance_reference.value,
            "min_bottom_clearance_mm":  slide.min_bottom_clearance,
            "max_load_kg":              slide.max_load_kg,
        },
        "joinery": joinery_info,
    }

    if cfg.pull_key is not None:
        from .cutlist import pull_line_from_drawer
        from .evaluation import check_drawer_pull

        placements = cfg.pull_placements
        pull_issues = check_drawer_pull(cfg)
        bom_line = pull_line_from_drawer(cfg)
        result["pull"] = {
            "key":               cfg.pull_key,
            "placements":        _pull_placements_to_dicts(placements),
            "count":             len(placements),
            "vertical_policy":   cfg.pull_vertical,
            "face_width_mm":     cfg.face_width,
            "face_height_mm":    cfg.face_height,
            "issues":            _issues_to_dicts(pull_issues),
            "bom":               _hardware_line_to_dict(bom_line) if bom_line else None,
        }

    return _ok(result)


def _build_cost_estimate(
    sheet_goods: list[dict],
    hw_lines: list,
) -> dict:
    """Summarise list-price cost by category with a grand total."""
    sheets_total = sum(e.get("line_total_usd", 0.0) for e in sheet_goods)
    hw_by_cat: dict[str, float] = {}
    for h in hw_lines:
        cat = h.category
        hw_by_cat[cat] = hw_by_cat.get(cat, 0.0) + round(h.packs_to_order * h.unit_price, 2)
    hw_total = sum(hw_by_cat.values())
    return {
        "sheet_goods_usd": round(sheets_total, 2),
        "hardware_by_category_usd": {k: round(v, 2) for k, v in hw_by_cat.items()},
        "hardware_total_usd": round(hw_total, 2),
        "grand_total_usd": round(sheets_total + hw_total, 2),
        "note": "List/MSRP prices — actual cost varies by supplier and region.",
    }


# ── generate_cutlist ──────────────────────────────────────────────────────────

def _is_sheet_material(material: str) -> bool:
    """True when the material is sheet stock the optimiser should pack.

    The Baltic birch stocks always are; any name ending in ``_ply``
    (``rift_white_oak_ply``, ``walnut_ply``, …) is treated as sheet goods
    too — packed per (material, thickness) and priced from PRICE_LIST when
    a matching ``sheet_<material>_<t>mm`` entry exists (price TBD flag
    otherwise).  Everything else (``finished_wood``, solid stock names)
    stays an order-out line excluded from packing.
    """
    return material in ("baltic_birch", "baltic_birch_prefinished") \
        or material.endswith("_ply")


def _door_hinge_overlay(cfg, panel) -> float:
    """The hinge's overlay for this leaf, or 0.0 for an inset hinge."""
    from .hardware import get_hinge
    try:
        return float(get_hinge(cfg.door_hinge).overlay or 0.0)
    except Exception:              # unknown key — say nothing rather than lie
        return 0.0


def _door_overlay_style(cfg) -> str:
    """"full" | "half" | "inset", from the hinge the door actually uses."""
    from .hardware import get_hinge
    try:
        return str(get_hinge(cfg.door_hinge).overlay_type.value).lower()
    except Exception:
        return ""


def _face_note(material: str, detail: str) -> str:
    """Cutlist note for a show-face panel (false front or door leaf)."""
    if material == "finished_wood":
        return f"species TBD; {detail}"
    if material in ("baltic_birch", "baltic_birch_prefinished"):
        return detail
    return f"{material.replace('_', ' ')}; {detail}"


def _raw_panels_for_cabinet(
    cfg: CabinetConfig,
    columns_raw: list | None,
) -> tuple[list[CutlistPanel], list[CutlistPanel], list[CutlistPanel], list[CutlistPanel]]:
    """Build the four raw panel lists (carcass, 6mm, drawer-box, false-front)
    for a single cabinet config.

    Shared by ``_tool_generate_cutlist`` and ``_tool_generate_project_cutlist``
    so identical-panel consolidation across a multi-cabinet project works
    against the same panel-shape definitions used in single-cabinet output.
    """
    # ONE column source for all three axes of this function — the carcass
    # panels, the drawer boxes and the show faces. carcass_panel_dims and
    # bays_from_config both read `columns=None` as "use cfg.columns"; the box
    # loop below used to read it as "there are no columns", so a config
    # carrying columns but called without them produced dividers and faces
    # for bays whose drawer boxes were silently absent — a cutlist that LOOKS
    # finished and cannot build the cabinet, which is worse than the bare box
    # it printed before. No shipping caller reaches it either way
    # (generate_cutlist pops `columns` out of args before building the config,
    # and both project paths pass _columns_dict_from_cfg), but one contract is
    # the whole point of this change.
    if not columns_raw and cfg.columns:
        columns_raw = _columns_dict_from_cfg(cfg)

    interior_width = cfg.width - 2 * cfg.side_thickness

    # Hardwood edge banding shrinks the CORE cut size by the band thickness
    # per banded edge so finished dims (and face reveals) hold; hot-melt
    # (~0.6 mm veneer) leaves cut dims alone.
    band_t = (float(getattr(cfg, "edge_band_thickness_mm", 0.6))
              if getattr(cfg, "edge_band_mode", "none") == "hardwood" else 0.0)
    miter = getattr(cfg, "carcass_corner_style", "butt") == "miter"
    # back_capture resolves every back dimension — where the top and bottom
    # stop, how big the back is cut, and how far interior panels are held
    # off the rear. "pocket" reproduces the legacy let-in geometry exactly,
    # including back_style's full-depth top cap.
    geo = back_capture_geometry(cfg)
    # Interior panels (shelves, dividers) stop at the back's front face.
    interior_depth = cfg.interior_depth    # the datum, not a second derivation
    under_top = getattr(cfg, "back_style", "full_height") == "under_top" and not miter
    # Every carcass CUT SIZE comes from cabinet.carcass_panel_dims — the one
    # source the design payloads, the 3D makers and the assembly-doc mortise
    # maps also read. This layer owns only what is a cutlist concern: the
    # material, the grain, the edge-band markers and the note prose. Panels
    # arrive at FINISHED size; ``.core(band_t)`` is the size the saw cuts
    # (hardwood banding replaces stock, hot-melt does not).
    carcass_dims = _carcass_panel_dims(cfg, columns_raw)

    def _core_note(finished_l: float, finished_w: float, edges: str) -> str:
        if not band_t:
            return ""
        return (f"core — band {edges} to finished "
                f"{finished_l:g}×{finished_w:g}")

    def _notes(*parts: str) -> str:
        return "; ".join(p for p in parts if p)

    def _capture_note(panel: str) -> str:
        """What this panel's rear edge needs machined for the back capture.

        Every machined capture cuts the same profile into the inner face of
        the sides, the top and the bottom — the back is seated inside the
        case perimeter, not lapped onto the panels' rear edges.
        """
        if not geo.machined:
            return ""
        where = {"side": "inner face", "top": "underside",
                 "bottom": "top face"}[panel]
        run = f"{geo.cut_run:g} mm wide × {geo.cut_depth:g} mm deep"
        # A side's ends show on the finished top and bottom surfaces (butt
        # corners seat the top and bottom between the sides), so its cut is
        # STOPPED; the top and bottom cuts run through into the sides.
        stop = ("; STOPPED — from the bottom panel's top face to the top "
                f"panel's underside, {geo.engagement:g} mm past each"
                if panel == "side" else "; runs through")
        if geo.capture == "dado":
            return (f"back groove: {run} in the {where}, "
                    f"{geo.setback:g} mm in from the rear edge{stop}")
        if geo.capture == "half_lap":
            return (f"back lap: {run} rabbet at the rear edge of the "
                    f"{where} (back is rabbeted to match){stop}")
        return f"back rabbet: {run} at the rear edge of the {where}{stop}"

    side_bevel = "45° bevels top+bottom ends (long-point dims)" if miter else ""
    tb_bevel = "45° bevels both ends (long-point dims)" if miter else ""

    def _carcass_note(p) -> str:
        """The note prose for one carcass panel — a cutlist concern.

        The dimensions come from ``carcass_panel_dims``; only the words are
        built here, and every number in them is read off the panel that is
        being described rather than recomputed from the config.
        """
        if p.kind == "side":
            return _notes(side_bevel, _capture_note("side"),
                          _core_note(p.length, p.width, "front edge"))
        if p.kind == "bottom":
            return _notes(tb_bevel, _capture_note("bottom"),
                          _core_note(p.length, p.width, "front edge"))
        if p.kind == "top":
            return _notes(
                tb_bevel,
                "full depth — rear edge flush with sides, caps the back"
                if under_top and not geo.machined else "",
                # A furniture-top cap strip is glued onto this front edge
                # (see the top_front_cap row), so it is NEVER banded and the
                # core is not shrunk for it — pre-fix the row demanded
                # banding on the same edge the cap covers, two contradictory
                # instructions at once.
                "front edge covered by the top_front_cap strip — no banding"
                if cfg.furniture_top else "",
                _capture_note("top"),
                "" if cfg.furniture_top
                else _core_note(p.length, p.width, "front edge"))
        return _core_note(p.length, p.width, "front edge")

    raw_carcass: list[CutlistPanel] = []
    for p in carcass_dims:
        if p.kind == "back":
            continue          # 6 mm group, emitted with its own note below
        cut_l, cut_w = p.core(band_t)
        raw_carcass.append(CutlistPanel(
            name=p.name, length=cut_l, width=cut_w, thickness=p.thickness,
            quantity=p.quantity, grain_direction="length",
            material=cfg.carcass_material,
            edge_band=list(p.banded_edges),
            notes=_carcass_note(p),
        ))

    _stock_note = ("1/4 in plywood" if cfg.back_thickness <= 6.0
                   else f"{cfg.back_thickness:g} mm plywood")
    if geo.capture == "pocket":
        _back_note = _notes(_stock_note,
                            "stops under the full-depth top" if under_top
                            else "")
    elif geo.capture == "half_lap":
        _back_note = _notes(
            _stock_note,
            f"rabbet all four edges {geo.lap_run:g} mm wide × "
            f"{geo.lap_depth:g} mm deep on the FRONT face — laps into the "
            "matching rabbets in the sides, top and bottom",
            f"cut size includes {geo.engagement:g} mm of lap per edge")
    else:
        seat = ("grooves" if geo.capture == "dado" else "rabbets")
        _back_note = _notes(
            _stock_note,
            f"seats {geo.engagement:g} mm into the {seat} on all four "
            "edges — cut size includes that engagement",
            "slides in during glue-up; it cannot go in afterwards"
            if geo.captive else "drops in from behind after glue-up")
    _back = next(p for p in carcass_dims if p.kind == "back")
    raw_6mm: list[CutlistPanel] = [
        CutlistPanel(name="back", length=_back.length, width=_back.width,
                     thickness=_back.thickness, quantity=_back.quantity,
                     grain_direction="", material="baltic_birch",
                     notes=_back_note),
    ]

    raw_box: list[CutlistPanel] = []
    raw_false_fronts: list[CutlistPanel] = []

    # Dividers and per-column fixed shelves were emitted by the loop above —
    # carcass_panel_dims orders them exactly where the hand-rolled rows used
    # to sit. What is left here is the opening stack the drawer boxes and
    # show faces are built from.
    if columns_raw:
        norm_cols: list[dict] = columns_raw
    elif cfg.openings:
        # Single-column cabinet: treat the opening stack as one full-width
        # column so drawer boxes and false fronts are generated for its
        # drawers too — the hardware BOM already orders slides for them, so
        # the panel lists must match. cfg-level fixed shelves were added
        # above; the synthetic column carries none.
        norm_cols = [{"width_mm": interior_width, "openings": cfg.openings}]
    else:
        norm_cols = []

    if norm_cols:
        for col in norm_cols:
            col_width = float(col["width_mm"])
            col_drawers = _stack_from_column(col)
            for row in col_drawers:
                op = _to_opening(row)
                opening_h, slot_type = op.height_mm, op.opening_type
                if slot_type != "drawer":
                    # Doors and open slots have no drawer box; their show
                    # faces come from face_layout() below.
                    continue
                dcfg = _box_config_for_opening(
                    cfg, col_width, opening_h, interior_depth, op)
                bw = round(dcfg.box_width, 1)
                bh = round(dcfg.box_height, 1)
                bd = round(dcfg.box_depth, 1)
                bt = dcfg.side_thickness
                bottom_w = round(dcfg.bottom_panel_width, 1)
                bottom_d = round(dcfg.bottom_panel_depth, 1)
                box_material = ("baltic_birch_prefinished"
                                if cfg.drawer_box_prefinished else "baltic_birch")
                # Part lengths come from the JOINT, never from the box's
                # outside dimensions: whichever piece wraps the corner runs
                # full length and the other is cut short. Listing both at
                # full size double-counts the corners and the box comes out
                # oversize in both directions (the pre-2026-08 bug).
                jspec = dcfg.joinery
                side_len = round(dcfg.side_panel_length, 1)
                fb_len = round(dcfg.front_back_panel_length, 1)
                # Where the box's outside width came from, in one clause, so
                # the row can be checked against the slide maker's rule
                # instead of trusted. Undermount runners constrain the
                # drawer's INSIDE width; the outside grows with the sides.
                _sl = dcfg.slide
                if _sl.clearance_reference is ClearanceReference.INSIDE:
                    width_basis = (
                        f"box {bw:g} outside = opening {col_width:g} − "
                        f"{2 * _sl.nominal_side_clearance:g} + 2 × {bt:g} sides; "
                        f"{dcfg.box_inside_width:g} inside, "
                        f"{dcfg.side_gap:g} air per side")
                else:
                    width_basis = (
                        f"box {bw:g} outside = opening {col_width:g} − "
                        f"{2 * _sl.nominal_side_clearance:g}; "
                        f"{dcfg.side_gap:g} air per side")
                if jspec.laps_front:
                    side_note = (
                        f"cut {2 * jspec.lip:g} mm short of the {bd:g} mm box "
                        f"depth — {jspec.lip:g} mm lock lip per corner; "
                        f"confirm the lip on a test corner before batching")
                    fb_note = (f"full box width — laps the ends of the sides "
                               f"({width_basis})")
                else:
                    side_note = "full box depth — the sides wrap the corners"
                    fb_note = (
                        f"cut {2 * (bt - jspec.engagement_x):g} mm short of the "
                        f"{bw:g} mm box width"
                        + (f" — seats {jspec.engagement_x:g} mm into each side"
                           if jspec.engagement_x else " — butts between the sides")
                        + f" ({width_basis})")

                raw_box += [
                    CutlistPanel(name="drawer_box_side", length=side_len,
                                 width=bh, thickness=bt, quantity=2,
                                 grain_direction="", material=box_material,
                                 notes=side_note),
                    CutlistPanel(name="drawer_box_front", length=fb_len,
                                 width=bh, thickness=bt, quantity=1,
                                 grain_direction="", material=box_material,
                                 notes=fb_note),
                    CutlistPanel(name="drawer_box_back", length=fb_len,
                                 width=bh, thickness=bt, quantity=1,
                                 grain_direction="", material=box_material,
                                 notes=fb_note),
                ]
                bottom_label = {6: "1/4 in", 9: "3/8 in", 12: "1/2 in"}.get(
                    int(round(dcfg.bottom_thickness)),
                    f"{dcfg.bottom_thickness:.0f} mm",
                )
                raw_6mm.append(CutlistPanel(
                    name="drawer_box_bottom",
                    length=bottom_w,
                    width=bottom_d,
                    thickness=dcfg.bottom_thickness,
                    quantity=1,
                    grain_direction="",
                    material=box_material,
                    notes=(f"{bottom_label}, dado-captured — sized to the "
                           f"groove floors, {dcfg.bottom_dado_depth:g} mm "
                           f"into all four parts "
                           f"({dcfg.box_inside_width:g} inside the box + "
                           f"{2 * dcfg.bottom_dado_depth:g} of groove)"),
                ))

    # ── Show faces — single source of truth ───────────────────────────────
    # All false-front and door-leaf dimensions come from cabinet.face_layout,
    # the same function the 3D builder extrudes. Widths run flush to the
    # cabinet exterior at outermost edges and split interior dividers
    # (INNER_FACE_OVERLAY_MM per neighbour); heights tile the opening span
    # with cfg.face_gap_mm at every internal boundary. Before 2026-08 this
    # block sized faces from DrawerConfig's flat 10/3/3 mm overlays — 16 mm
    # narrow on a flush build, and the height stack physically couldn't fit
    # (the kids'-desk fronts were cut from that paper).
    # Gate on furniture_top too — an opening-less furniture_top cabinet
    # still has its cap strip (the layout emits it regardless of openings;
    # gating on norm_cols alone recreated the render-only-cap divergence).
    if norm_cols or cfg.furniture_top:
        bays = _bays_from_config(cfg, columns_raw if columns_raw else None)
        gap = cfg.face_gap_mm
        n_bays = len(bays)

        def _guard(kind: str, p) -> None:
            # A gap-trimmed face can go ≤ 0 where the old flat overlays never
            # could — refuse to print garbage rows onto shop paper.
            if p.width <= 0 or p.height <= 0:
                raise ValueError(
                    f"{kind} for opening {p.slot} in column {p.bay} computes "
                    f"{p.width:.1f} × {p.height:.1f} mm — the opening is too "
                    f"small for face_gap_mm {gap:g}. Fix the opening stack "
                    "(evaluate_cabinet reports this as face_clearance).")

        layout = _face_layout(bays)
        # Keyed by the panel's identity in the layout, not by id(): the
        # placement pass builds its own FacePanel objects, so object identity
        # does not survive the call.
        def _key(fp):
            return (fp.kind, fp.bay, fp.slot, fp.leaf)

        placements = {_key(fp.panel): fp for fp in _face_placements(bays)}
        # Where each face sits in its own stack — the note's numbers come
        # from the panel and its neighbours, never from face_gap_mm. The
        # hardcoded "{gap} mm gaps above/below" was right at the three
        # internal boundaries of his sideboard and wrong at BOTH anchored
        # ends; obeyed literally it wants 647.6 mm of face in a 627.6 mm
        # span. It is also the only positioning statement on any document —
        # describe.py suppresses its reveal sentence at the default gap and
        # the assembly doc says nothing about hanging faces — so it now
        # carries the datum too.
        def _edge_txt(v: float, member: str) -> str:
            """One formula per end; the SIGN is what makes it one formula.

            A reveal, flush, or a lap over the member. Writing "0 mm reveal"
            at an anchored end was a second hardcoded constant standing where
            the first one had just been removed: a furniture_top bottom face
            hangs 18 mm OVER the bottom panel, and an inset door has a real
            2 mm reveal there.
            """
            if abs(v) < 0.005:
                return f"flush with the {member}"
            if v > 0:
                return f"{v:g} mm to the {member}"
            return f"laps the {member} by {-v:g} mm"

        def _position_note(fp) -> str:
            pl = placements[_key(fp)]
            datum = (f"{pl.datum:g} mm above the bottom panel's top face"
                     if pl.datum >= 0 else
                     f"{-pl.datum:g} mm BELOW the bottom panel's top face")
            # ONE clause, deliberately. Consolidation dedups clause by
            # clause, so splitting this into three let a qty-2 row keep two
            # "below" answers and a single "above" (the second face's
            # matched the first's and was deduped) — a part with two answers
            # to one question and no way to re-pair them.
            return (f"hangs {datum} — below: "
                    f"{_edge_txt(pl.reveal_below, pl.below_member)}, above: "
                    f"{_edge_txt(pl.reveal_above, pl.above_member)}")

        def _overlay_note(fp) -> str:
            """What each edge meets, and by how much.

            Stated by the MEMBER each edge meets and ordered by that member's
            ROLE, not left-then-right. A mirrored pair of bays cuts the same
            panel and consolidates into one row, so a left-then-right
            sentence handed that row two orderings of one fact — which is
            what this note was rewritten to stop, and an earlier version of
            this docstring claimed it had while the code still emitted
            left-then-right.
            """
            pl = placements[_key(fp)]

            def _lap(v: float, member: str) -> str:
                # An INSET leaf laps nothing — it sits inside the opening,
                # and its lap is negative. "full overlay — -2 mm over the
                # cabinet side" is not a sentence anyone can act on. Against
                # the partner leaf of a pair the same negative is a GAP, and
                # that is the number a person setting the pair needs.
                if member == "meeting leaf":
                    return f"{-v:g} mm gap to the meeting leaf"
                if v < -0.005:
                    return f"{-v:g} mm inside the {member}"
                if abs(v) < 0.005:
                    return f"flush with the {member}"
                return f"{v:g} mm over the {member}"

            # A drawer face is always full overlay; a door leaf's style is
            # the HINGE's, and "full overlay — 9.5 mm" was printed for a
            # half-overlay hinge.
            if fp.kind == "door":
                lead = {"full": "full overlay", "half": "half overlay",
                        "inset": "inset"}.get(
                            _door_overlay_style(cfg), "overlay")
            else:
                lead = "inset" if pl.left_lap < -0.005 else "full overlay"

            rank = {"cabinet side": 0, "meeting leaf": 1, "divider": 2}
            edges = sorted(((pl.left_lap, pl.left_member),
                            (pl.right_lap, pl.right_member)),
                           key=lambda e: (rank.get(e[1], 3), e[0]))
            if (edges[0][1] == edges[1][1]
                    and abs(edges[0][0] - edges[1][0]) < 0.005):
                return f"{lead} — {_lap(*edges[0])} at each edge"
            return f"{lead} — {_lap(*edges[0])}, {_lap(*edges[1])}"

        door_groups: dict[tuple[int, int], list] = {}
        for p in layout:
            if p.kind == "drawer_face":
                _guard("false front", p)
                # Position LAST. A consolidated row covers several physical
                # faces, so its position clauses multiply while the others
                # collapse; keeping them at the end clusters them instead of
                # interleaving them with the banding note.
                ff_note = _face_note(cfg.face_material, _overlay_note(p))
                if band_t:
                    ff_note += "; " + _core_note(
                        round(p.width, 1), round(p.height, 1), "4 edges")
                ff_note += "; " + _position_note(p)
                raw_false_fronts.append(CutlistPanel(
                    name="false_front",
                    length=round(p.width - 2 * band_t, 1),
                    width=round(p.height - 2 * band_t, 1),
                    thickness=p.thickness,
                    quantity=1,
                    grain_direction="length",
                    material=cfg.face_material,
                    edge_band=["all"],
                    notes=ff_note,
                ))
            elif p.kind == "door":
                door_groups.setdefault((p.bay, p.slot), []).append(p)
            elif p.kind == "top_cap":
                raw_false_fronts.append(CutlistPanel(
                    name="top_front_cap",
                    length=round(p.width, 1),
                    width=round(p.height, 1),
                    thickness=p.thickness,
                    quantity=1,
                    grain_direction="length",
                    material=cfg.face_material,
                    edge_band=[],
                    notes=_face_note(
                        cfg.face_material,
                        "furniture top front cap — glue to the top panel "
                        "front edge, flush with the face plane"),
                ))

        for (_bay, _slot), leaves in sorted(door_groups.items()):
            p0 = leaves[0]
            _guard("door leaf", p0)
            n_leaves = len(leaves)
            door_note = _face_note(
                cfg.face_material,
                (f"{n_leaves} leaf" if n_leaves == 1 else f"{n_leaves} leaves")
                + (" — width set by the hinge overlay; "
                   if _door_hinge_overlay(cfg, p0) else
                   # An inset hinge has no overlay: the leaf is sized to the
                   # opening less its gaps, so "width set by the hinge
                   # overlay" names a quantity that is zero.
                   " — sized to the opening less its gaps (inset hinge); ")
                # A door leaf's WIDTH comes from the hinge, so the row does
                # not claim to have chosen it — but whether the leaf sits
                # OVER the carcass or INSIDE the opening is the first thing
                # a person needs, and the row said nothing at all.
                + _overlay_note(p0) + "; "
                + _position_note(p0))
            if band_t:
                door_note += "; " + _core_note(
                    round(p0.height, 1), round(p0.width, 1), "4 edges")
            raw_false_fronts.append(CutlistPanel(
                name="door",
                length=round(p0.height - 2 * band_t, 1),
                width=round(p0.width - 2 * band_t, 1),
                thickness=p0.thickness,
                quantity=n_leaves,
                grain_direction="length",
                material=cfg.face_material,
                edge_band=["all"],
                notes=door_note,
            ))

    # edge_band markers mean "this edge WILL be banded" — strip them when
    # the cabinet has no banding so consumers (band BOM lines, the banding
    # cutlist doc, CSV markers) never claim banding on an unbanded build.
    # Found via the first mixed batch: the banding doc swept in kapex/kid
    # panels because markers were attached unconditionally.
    if getattr(cfg, "edge_band_mode", "none") == "none":
        for p in (*raw_carcass, *raw_6mm, *raw_box, *raw_false_fronts):
            p.edge_band = []
    return raw_carcass, raw_6mm, raw_box, raw_false_fronts


_IMPERIAL_SHEET_LABELS = {18: '3/4"', 15: '5/8"', 12: '1/2"', 9: '3/8"', 6: '1/4"'}

_SHEET_MATERIAL_LABELS = {
    "baltic_birch": "Baltic Birch",
    "baltic_birch_prefinished": "Pre-finished Baltic Birch",
}



_OPTIMIZER_CHOICES = ("auto", "opcut", "rectpack", "strip", "rips_first")


def _parse_optimizer_overrides(raw) -> dict:
    """Validate {key: algorithm} per-group optimizer overrides.

    Keys match a (material, thickness) group by precedence:
    "material@thickness" (e.g. "baltic_birch@6") > "material" >
    "@thickness" (any material at that thickness) > the run-level
    optimizer.
    """
    out: dict = {}
    for key, alg in (raw or {}).items():
        if alg not in _OPTIMIZER_CHOICES:
            raise ValueError(
                f"optimizer_overrides[{key!r}] must be one of "
                f"{list(_OPTIMIZER_CHOICES)}, got {alg!r}.")
        out[str(key)] = str(alg)
    return out


def _resolve_group_algorithm(
    material: str, thickness: float, overrides: dict, default: str,
) -> str:
    """Pick the optimizer for one (material, thickness) sheet group."""
    if not overrides:
        return default
    t = f"{thickness:g}"
    for key in (f"{material}@{t}", material, f"@{t}"):
        if key in overrides:
            return overrides[key]
    return default


def _parse_sheet_size_overrides(raw) -> dict:
    """Validate {material: [length_mm, width_mm]} sheet-size overrides."""
    out: dict = {}
    for mat, dims in (raw or {}).items():
        # Explicit shape check — a string like "2453x1234" would otherwise
        # index its characters into a 2×4 mm sheet (review 2026-07-29).
        if not isinstance(dims, (list, tuple)) or len(dims) != 2:
            raise ValueError(
                f"sheet_size_overrides[{mat!r}] must be [length_mm, "
                f"width_mm], got {dims!r}.")
        try:
            L, W = float(dims[0]), float(dims[1])
        except (TypeError, ValueError):
            raise ValueError(
                f"sheet_size_overrides[{mat!r}] must be [length_mm, "
                f"width_mm], got {dims!r}.")
        if L <= 0 or W <= 0:
            raise ValueError(
                f"sheet_size_overrides[{mat!r}] must be positive, "
                f"got {dims!r}.")
        out[str(mat)] = (L, W)
    return out

def _cutlist_pipeline(
    *,
    name: str,
    out_dir: Path,
    carcass_panels: list[CutlistPanel],
    box_panels: list[CutlistPanel],
    panels_6mm: list[CutlistPanel],
    false_fronts: list[CutlistPanel],
    hw_lines: list,
    sheet_length: float,
    sheet_width: float,
    kerf: float,
    optimizer: str,
    fmt: str,
    sheet_size_overrides: dict | None = None,
    optimizer_overrides: dict | None = None,
    band_cfg=None,
    band_panels: list | None = None,
    paper: str = "letter",
) -> dict[str, Any]:
    """Shared post-panel cutlist pipeline: per-thickness sheet optimisation,
    sheet-goods pricing, file output (CSV/JSON/hardware BOM/layout HTML/PDF),
    and the common result payload.

    Used by ``_tool_generate_cutlist`` (files land in ``out_dir`` with
    ``name`` as the stem) and ``_tool_generate_project_cutlist`` (same, with
    a per-project subdirectory) — one implementation so optimisation,
    pricing, and layout output can never drift between the two tools.

    Carcass panels are grouped by their actual thickness: each group is
    packed onto, and priced as, the matching sheet stock
    (``sheet_baltic_birch_{t}mm``).

    Show-face panels (false fronts, door leaves, worktops) whose material is
    real sheet stock join the optimisation: raw Baltic birch pools with the
    carcass thickness groups, pre-finished with the pre-finished parts pool.
    Faces in any other material (default "finished_wood", or a named species
    like "rift_white_oak_ply") remain a labeled group excluded from packing.
    """
    pooled_faces_parts = [p for p in false_fronts
                          if p.material == "baltic_birch_prefinished"]
    pooled_faces_carcass = [p for p in false_fronts
                            if p.material != "baltic_birch_prefinished"
                            and _is_sheet_material(p.material)]
    false_fronts = [p for p in false_fronts if not _is_sheet_material(p.material)]
    carcass_panels = carcass_panels + pooled_faces_carcass
    box_panels = box_panels + pooled_faces_parts

    all_panels = carcass_panels + box_panels + panels_6mm + false_fronts
    # Row IDs (A-DB1 …) — the same panel objects flow into the layout
    # groups, so graphics and tables always agree.
    assign_part_ids(all_panels)

    def _make_sheet(t: float, material: str = "") -> SheetStock:
        # Per-material stock-size override (e.g. Charlie's rift oak runs
        # oversize at 2453x1234 while the Baltic birch is nominal 2440x1220).
        L, W = sheet_length, sheet_width
        ov = (sheet_size_overrides or {}).get(material)
        if ov:
            L, W = float(ov[0]), float(ov[1])
        return SheetStock(
            name=f"{int(L)}x{int(W)} {t:.0f}mm",
            length=L, width=W, thickness=t,
        )

    def _opt_group(panels: list[CutlistPanel], thickness: float,
                   material: str = ""):
        # Returns (summary_dict, OptimizationResult | None)
        if not panels:
            return {}, None
        sheet = _make_sheet(thickness, material)
        alg = _resolve_group_algorithm(
            material, thickness, optimizer_overrides or {}, optimizer)
        opt = optimize_cutlist(panels, stock_sheet=sheet, kerf=kerf,
                               algorithm=alg)
        return ({"sheets_used": opt.sheets_used, "waste_pct": opt.waste_pct,
                 "unplaced": opt.unplaced,
                 "algorithm": opt.algorithm_used}, opt)

    # Carcass panels can span multiple thicknesses (side vs top/bottom
    # overrides, mixed-thickness projects) AND multiple materials
    # (carcass_material, sheet-stock show faces) — group by both.
    carcass_by_mt: dict[tuple[str, float], list[CutlistPanel]] = {}
    for p in carcass_panels:
        carcass_by_mt.setdefault((p.material, p.thickness), []).append(p)
    carcass_mts = sorted(carcass_by_mt, key=lambda mt: (mt[0], -mt[1]))

    # Drawer-box parts and thin panels (backs + bottoms) pool by
    # (material, thickness): box stock is configurable (drawer_box_thickness,
    # drawer_box_prefinished) and bottoms follow the per-drawer size rule /
    # overrides, so parts sharing both stock and thickness pack — and are
    # priced — on shared sheets (e.g. 12 mm box sides alongside 12 mm heavy
    # drawer bottoms), while pre-finished stock never mixes with raw.
    parts_by_mt: dict[tuple[str, float], list[CutlistPanel]] = {}
    for p in box_panels + panels_6mm:
        parts_by_mt.setdefault((p.material, p.thickness), []).append(p)
    parts_mts = sorted(parts_by_mt, key=lambda mt: (mt[0], -mt[1]))

    opt_carcass_by_mt = {mt: _opt_group(carcass_by_mt[mt], mt[1], mt[0])
                         for mt in carcass_mts}
    opt_parts_by_mt = {mt: _opt_group(parts_by_mt[mt], mt[1], mt[0])
                       for mt in parts_mts}

    # ── Sheet goods summary ────────────────────────────────────────────────
    sheet_goods = []
    for mt in carcass_mts:
        mat, t = mt
        opt_info, _ = opt_carcass_by_mt[mt]
        sheets = opt_info.get("sheets_used", 0)
        unit_p = price_for(f"sheet_{mat}_{int(round(t))}mm")
        frac   = _IMPERIAL_SHEET_LABELS.get(int(round(t)))
        mat_label = _SHEET_MATERIAL_LABELS.get(mat, mat.replace("_", " ").title())
        label  = (f"{mat_label} {frac} ({t:.0f} mm)" if frac
                  else f"{mat_label} {t:.0f} mm")
        entry  = {"material": label,
                  "thickness_mm": t,
                  "panel_count": sum(p.quantity for p in carcass_by_mt[mt]),
                  "price_per_sheet_usd": unit_p,
                  "line_total_usd": round(sheets * unit_p, 2)}
        if not unit_p:
            entry["price_missing"] = True  # no PRICE_LIST entry for this stock
        entry.update(opt_info)
        sheet_goods.append(entry)
    for mt in parts_mts:
        mat, t = mt
        opt_info, _ = opt_parts_by_mt[mt]
        sheets = opt_info.get("sheets_used", 0)
        unit_p = price_for(f"sheet_{mat}_{int(round(t))}mm")
        frac   = _IMPERIAL_SHEET_LABELS.get(int(round(t)))
        mat_label = _SHEET_MATERIAL_LABELS.get(mat, mat.replace("_", " ").title())
        label  = (f"{mat_label} {frac} ({t:.0f} mm)" if frac
                  else f"{mat_label} {t:.0f} mm")
        entry = {"material": label,
                 "thickness_mm": t,
                 "panel_count": sum(p.quantity for p in parts_by_mt[mt]),
                 "price_per_sheet_usd": unit_p,
                 "line_total_usd": round(sheets * unit_p, 2)}
        if not unit_p:
            entry["price_missing"] = True  # no PRICE_LIST entry for this stock
        entry.update(opt_info)
        sheet_goods.append(entry)
    if false_fronts:
        # Group the order-out faces by material so a named species reads as
        # its own shopping line while unspecified faces stay "species TBD".
        faces_by_mat: dict[str, list[CutlistPanel]] = {}
        for p in false_fronts:
            faces_by_mat.setdefault(p.material, []).append(p)
        for mat in sorted(faces_by_mat):
            group = faces_by_mat[mat]
            if mat == "finished_wood":
                label = "Finished wood — show faces (species TBD)"
            else:
                label = f"Show faces — {mat.replace('_', ' ')} (price TBD)"
            sheet_goods.append({
                "material": label,
                "thickness_mm": max(p.thickness for p in group),
                "panel_count": sum(p.quantity for p in group),
                "note": ("Order solid stock or veneered panel; "
                         "not included in sheet optimisation."),
            })

    # ── File output ────────────────────────────────────────────────────────
    pipeline_notes: list[str] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path  = out_dir / f"{name}_cutlist.csv"
    json_path = out_dir / f"{name}_cutlist.json"
    # Per (material, thickness) so sheet_size_overrides show up in the
    # exported JSON's stock declarations too (review 2026-07-29 minor 6);
    # deduped on resulting dims so same-size stocks list once.
    carcass_sheets, _seen_sheets = [], set()
    for mat, t in sorted(carcass_mts, key=lambda mt: -mt[1]):
        sh = _make_sheet(t, mat)
        skey = (sh.length, sh.width, sh.thickness)
        if skey not in _seen_sheets:
            _seen_sheets.add(skey)
            carcass_sheets.append(sh)
    if not carcass_sheets:
        carcass_sheets = [_make_sheet(18.0)]
    csv_path.write_text(to_csv(all_panels), encoding="utf-8")
    json_path.write_text(to_json(all_panels, carcass_sheets), encoding="utf-8")

    files: dict[str, str] = {"csv": str(csv_path), "json": str(json_path)}

    if hw_lines:
        hw_json_path = out_dir / f"{name}_hardware_bom.json"
        hw_json_path.write_text(to_hardware_json(hw_lines), encoding="utf-8")
        files["hardware_bom_json"] = str(hw_json_path)

    layout_groups = []
    for mt in carcass_mts:
        mat, t = mt
        _, opt_res = opt_carcass_by_mt[mt]
        if opt_res:
            frac = _IMPERIAL_SHEET_LABELS.get(int(round(t)))
            suffix = f" ({frac})" if frac else ""
            mat_label = ("" if mat == "baltic_birch" else
                         _SHEET_MATERIAL_LABELS.get(
                             mat, mat.replace("_", " ").title()) + " ")
            layout_groups.append((
                f'{t:.0f}mm {mat_label}Carcass{suffix} — {opt_res.sheets_used} sheets',
                carcass_by_mt[mt], opt_res,
            ))
    for mt in parts_mts:
        mat, t = mt
        _, opt_res = opt_parts_by_mt[mt]
        if opt_res:
            frac = _IMPERIAL_SHEET_LABELS.get(int(round(t)))
            suffix = f" ({frac})" if frac else ""
            box_ids  = {id(p) for p in box_panels}
            has_box  = any(id(p) in box_ids for p in parts_by_mt[mt])
            has_thin = any(id(p) not in box_ids for p in parts_by_mt[mt])
            what = ("Drawer Boxes, Backs & Bottoms" if has_box and has_thin
                    else "Drawer Boxes" if has_box else "Backs & Bottoms")
            if mat == "baltic_birch_prefinished":
                what = f"Pre-finished {what}"
            layout_groups.append((
                f'{t:.0f}mm {what}{suffix} — {opt_res.sheets_used} sheets',
                parts_by_mt[mt], opt_res,
            ))
    if layout_groups:
        html = generate_sheet_layout_html(
            layout_groups, cabinet_name=name, kerf=kerf,
            hardware_lines=hw_lines or None,
        )
        layout_path = out_dir / f"{name}_layout.html"
        layout_path.write_text(html, encoding="utf-8")
        files["layout"] = str(layout_path)

        try:
            pdf_bytes = generate_sheet_layout_pdf(
                layout_groups, cabinet_name=name, kerf=kerf,
                hardware_lines=hw_lines or None, paper=paper,
            )
            pdf_path = out_dir / f"{name}_layout.pdf"
            pdf_path.write_bytes(pdf_bytes)
            files["pdf"] = str(pdf_path)
        except ImportError:
            pipeline_notes.append(
                "Layout PDF skipped — reportlab not installed (lite mode).")

        # Standalone portrait cut-parts documents — the pages taped to the
        # saw (Charlie, 2026-08-02). One combined doc always; per-project
        # docs additionally on multi-project batches.
        try:
            from .cutlist import (
                generate_parts_list_pdf, _source_letters_from_groups,
            )
            all_parts = [p for _, pnls, _ in layout_groups for p in pnls]
            all_parts.sort(
                key=lambda p: (p.source, p.thickness, p.material, p.name))
            letters = _source_letters_from_groups(layout_groups)
            p_path = out_dir / f"{name}_parts.pdf"
            p_path.write_bytes(generate_parts_list_pdf(
                all_parts, cabinet_name=name, paper=paper,
                source_letters=letters or None))
            files["parts_pdf"] = str(p_path)
            if letters:
                for src, letter in letters.items():
                    sub = [p for p in all_parts if p.source == src]
                    if not sub:
                        continue
                    p_path = out_dir / f"{_safe_stem(src, kind='project name')}_parts.pdf"
                    p_path.write_bytes(generate_parts_list_pdf(
                        sub, cabinet_name=src, paper=paper,
                        subtitle=(f"Project {letter} in batch {name} — "
                                  "part IDs match the batch sheet "
                                  "layouts.")))
                    files[f"parts_pdf_{src}"] = str(p_path)
        except ImportError:
            pipeline_notes.append(
                "Parts-list PDF skipped — reportlab not installed "
                "(lite mode).")

    # ── Banding cutlist (board→strip→piece) ────────────────────────────────
    # band_cfg is the run's hardwood band config with a purchase spec; the
    # doc packs every banded panel in the run with that one spec (mixed
    # per-cabinet band tokens aren't split into separate documents).
    if (band_cfg is not None
            and getattr(band_cfg, "edge_band_mode", "none") == "hardwood"
            and getattr(band_cfg, "edge_band_stock", None)):
        if band_panels is not None:
            # The caller scoped the doc to the hardwood band group's own
            # panels — the edge_band-marker filter would also sweep in
            # hot-melt cabinets' panels, chop-planning (and double-
            # provisioning) edges the rolls already cover (review
            # 2026-07-29 M3). Copy the layout part IDs onto them so the
            # doc's labels match the drawings.
            id_by_key = {
                (p.name, round(p.length, 1), round(p.width, 1),
                 round(p.thickness, 1), p.grain_direction, p.material,
                 tuple(p.edge_band), p.source): p.part_id
                for p in all_panels}
            for bp in band_panels:
                bp.part_id = bp.part_id or id_by_key.get(
                    (bp.name, round(bp.length, 1), round(bp.width, 1),
                     round(bp.thickness, 1), bp.grain_direction,
                     bp.material, tuple(bp.edge_band), bp.source), "")
            banded = [p for p in band_panels if p.edge_band]
        else:
            banded = [p for p in all_panels if p.edge_band]
        if banded:
            from .cutlist import (generate_banding_cutlist_html,
                                  generate_banding_cutlist_pdf,
                                  to_banding_csv)
            p = out_dir / f"{name}_banding_cutlist.html"
            p.write_text(generate_banding_cutlist_html(banded, band_cfg, name), encoding="utf-8")
            files["banding_cutlist_html"] = str(p)
            p = out_dir / f"{name}_banding_cutlist.csv"
            p.write_text(to_banding_csv(banded, band_cfg), encoding="utf-8")
            files["banding_cutlist_csv"] = str(p)
            try:
                pdf_bytes = generate_banding_cutlist_pdf(
                    banded, band_cfg, name, paper=paper)
                p = out_dir / f"{name}_banding_cutlist.pdf"
                p.write_bytes(pdf_bytes)
                files["banding_cutlist_pdf"] = str(p)
            except ImportError:
                pipeline_notes.append(
                    "Banding PDF skipped — reportlab not installed "
                    "(lite mode).")

    # ── Build result ───────────────────────────────────────────────────────
    result: dict[str, Any] = {
        **({"notes": pipeline_notes} if pipeline_notes else {}),
        "panel_count": len(all_panels),
        "sheet_goods": sheet_goods,
        "panels_summary": [
            {"id": p.part_id, "name": p.name, "length_mm": p.length,
             "width_mm": p.width,
             "thickness_mm": p.thickness, "qty": p.quantity, "material": p.material,
             **({"project": p.source} if p.source else {})}
            for p in all_panels
        ],
        "hardware_bom": [
            {
                "category": h.category,
                "name": h.name,
                "brand": h.brand,
                "model_number": h.model_number,
                "pieces_needed": h.pieces_needed,
                **({"by_project": dict(h.source_counts)} if h.source_counts else {}),
                "pack_quantity": h.pack_quantity,
                "packs_to_order": h.packs_to_order,
                "leftover": h.leftover,
                "notes": h.notes,
                "unit_price_usd": h.unit_price,
                "line_total_usd": round(h.packs_to_order * h.unit_price, 2),
            }
            for h in hw_lines
        ],
        "cost_estimate": _build_cost_estimate(sheet_goods, hw_lines),
        "files": files,
    }

    if fmt in ("json", "both"):
        result["cutlist_json"] = json.loads(to_json(all_panels, carcass_sheets))
    if fmt in ("csv", "both"):
        result["cutlist_csv"] = to_csv(all_panels)

    # Aggregate carcass optimisation stats (single group for a uniform-
    # thickness cabinet — identical to the historical single-group values).
    carcass_summaries = [s for s, _ in opt_carcass_by_mt.values() if s]
    if carcass_summaries:
        result["sheets_used"] = sum(s["sheets_used"] for s in carcass_summaries)
        # Sheet-weighted average — a single group's waste_pct misrepresents
        # mixed-thickness carcass runs.
        total_sheets = sum(s["sheets_used"] for s in carcass_summaries) or 1
        result["waste_pct"] = round(
            sum(s["waste_pct"] * s["sheets_used"] for s in carcass_summaries)
            / total_sheets, 1)
        result["unplaced_panels"] = [u for s in carcass_summaries for u in s["unplaced"]]

    # Derive the note from what actually ran — the requested optimizer can
    # be remapped per group by optimizer_overrides, and "rips_first" used
    # to fall through to the opcut wording (review 2026-07-29 minor 5).
    _ALGO_LABELS = {
        "opcut": "opcut FORWARD_GREEDY (guillotine)",
        "rectpack": "rectpack GuillotineBssfSas",
        "strip": "strip-cutting (fallback)",
        "rips_first": ("rips_first (track-saw strip rips → cross-cuts → "
                       "table-saw secondary rips)"),
    }
    used = sorted({sg.get("algorithm", "") for sg in result["sheet_goods"]
                   if sg.get("algorithm")})
    if not used:
        used = ["opcut" if _OPCUT_AVAILABLE else
                "rectpack" if _RECTPACK_AVAILABLE else "strip"]
    algo = " + ".join(_ALGO_LABELS.get(a, a) for a in used)
    result["optimization_note"] = (
        f"Sheet layout via {algo}"
        + (" (mixed per material/thickness via optimizer_overrides)"
           if len(used) > 1 else "")
        + ". Every cut is a straight line across the remaining panel — "
        "the layout is directly executable at the saw."
    )

    return result


async def _tool_generate_cutlist(args: dict) -> list[types.TextContent]:
    fmt          = args.pop("format", "both")
    sheet_length = float(args.pop("sheet_length", 2440))
    sheet_width  = float(args.pop("sheet_width",  1220))
    sheet_size_overrides = _parse_sheet_size_overrides(
        args.pop("sheet_size_overrides", None))
    optimizer_overrides = _parse_optimizer_overrides(
        args.pop("optimizer_overrides", None))
    kerf         = float(args.pop("kerf", 3.2))
    optimizer    = str(args.pop("optimizer", "auto"))
    paper        = str(args.pop("paper", "letter")).lower()
    if paper not in ("letter", "a4"):
        raise ValueError(f"paper must be 'letter' or 'a4', got {paper!r}.")
    name         = _safe_stem(args.pop("name", "cabinet"), kind="cutlist name")
    columns_raw  = args.pop("columns", None)

    cfg = _build_cabinet_config(args)

    raw_carcass, raw_6mm, raw_box, raw_false_fronts = _raw_panels_for_cabinet(cfg, columns_raw)

    # Consolidate each material group
    carcass_panels  = consolidate_bom(raw_carcass)
    panels_6mm      = consolidate_bom(raw_6mm)
    box_panels      = consolidate_bom(raw_box)
    false_fronts    = consolidate_bom(raw_false_fronts)

    hw_lines = hardware_bom_for_cabinet_config(cfg, columns_raw)
    from .cutlist import edge_band_lines_for_panels
    hw_lines += edge_band_lines_for_panels(
        raw_carcass + raw_false_fronts, cfg)

    result = _cutlist_pipeline(
        name=name,
        out_dir=data_dir() / "cutlists",
        carcass_panels=carcass_panels, box_panels=box_panels,
        panels_6mm=panels_6mm, false_fronts=false_fronts,
        hw_lines=hw_lines,
        sheet_length=sheet_length, sheet_width=sheet_width,
        kerf=kerf, optimizer=optimizer, fmt=fmt,
        sheet_size_overrides=sheet_size_overrides,
        optimizer_overrides=optimizer_overrides,
        band_cfg=cfg,
        paper=paper,
    )
    return _ok(result)


# ── compare_joinery ───────────────────────────────────────────────────────────

async def _tool_compare_joinery(args: dict) -> list[types.TextContent]:
    from .joinery import DrawerJoinerySpec

    t_s  = float(args.get("side_thickness", 12.0))
    t_fb = float(args.get("front_back_thickness", 12.0))

    comparison = {}
    for style in DrawerJoineryStyle:
        spec = DrawerJoinerySpec.from_stock(style, t_s, t_fb)
        entry: dict[str, Any] = {
            "style": style.value,
            "requires_router_bit":    spec.requires_router_bit,
            "requires_true_thickness": spec.requires_true_thickness,
            "side_dado_depth_x_mm":  spec.side_dado_depth_x,
            "side_dado_depth_y_mm":  spec.side_dado_depth_y,
            "fb_channel_depth_x_mm": spec.fb_channel_depth_x,
            "fb_channel_depth_y_mm": spec.fb_channel_depth_y,
        }
        if style == DrawerJoineryStyle.QQQ:
            entry["note"] = (
                f"All cuts = side_thickness ÷ 2 = {t_s / 2:.1f} mm. "
                f"True {t_s:.0f} mm stock required for nominal "
                f"{t_s:.0f} mm settings."
            )
        entry["corner_lap"] = spec.corner_lap
        if style == DrawerJoineryStyle.DRAWER_LOCK:
            entry["lip_mm"] = spec.lip
            entry["socket_depth_mm"] = spec.socket_depth
            entry["note"] = (
                "Requires dedicated drawer-lock router bit. Front-lapping: "
                f"the front/back runs the full box width and the sides are "
                f"cut 2 x {spec.lip:g} mm short of the box depth. The lip is "
                "set by the bit and fence — measure a test corner and pass "
                "corner_lip_mm rather than trusting the nominal."
            )
        comparison[style.value] = entry

    return _ok({
        "side_thickness_mm":       t_s,
        "front_back_thickness_mm": t_fb,
        "styles": comparison,
    })


# ── suggest_proportions ───────────────────────────────────────────────────────

async def _tool_suggest_proportions(args: dict) -> list[types.TextContent]:
    width    = float(args["width"])
    height   = float(args["height"])
    side_t   = float(args.get("side_thickness",   18))
    bottom_t = float(args.get("bottom_thickness", 18))
    top_t    = float(args.get("top_thickness",    18))
    num_drawers = args.get("num_drawers")
    num_columns = args.get("num_columns")
    wide_index  = args.get("wide_index")

    if not num_drawers and not num_columns:
        return _err("Provide at least one of num_drawers or num_columns.")

    interior_h = height - bottom_t - top_t
    interior_w = width - 2 * side_t

    result: dict[str, Any] = {
        "interior_height_mm": interior_h,
        "interior_width_mm":  interior_w,
    }

    if num_drawers:
        suggestions = []
        for preset, ratio in _RATIO_PRESETS.items():
            try:
                heights = _grad_heights(interior_h, int(num_drawers), preset)
                suggestions.append({
                    "preset":              preset,
                    "ratio":               ratio,
                    "character":           _PRESET_DESCRIPTIONS[preset],
                    "viable":              True,
                    "heights_mm":          heights,
                    "heights_in":          [_mm_to_inches_str(h) for h in heights],
                    "bottom_mm":           heights[0],
                    "top_mm":              heights[-1],
                    "bottom_to_top_ratio": round(heights[0] / heights[-1], 3),
                })
            except ValueError as exc:
                suggestions.append({
                    "preset":    preset,
                    "ratio":     ratio,
                    "character": _PRESET_DESCRIPTIONS[preset],
                    "viable":    False,
                    "reason":    str(exc),
                })
        result["drawer_suggestions"] = suggestions

    if num_columns:
        suggestions = []
        n_cols_suggest = int(num_columns)
        available_w_suggest = interior_w - (n_cols_suggest - 1) * side_t
        for preset, ratio in _RATIO_PRESETS.items():
            widths = _col_widths(available_w_suggest, n_cols_suggest, wide_index, preset)
            wide_w   = widths[wide_index] if wide_index is not None else None
            narrow_w = widths[0] if wide_index != 0 else widths[1]
            suggestions.append({
                "preset":           preset,
                "ratio":            ratio,
                "character":        _PRESET_DESCRIPTIONS[preset],
                "widths_mm":        widths,
                "wide_column_mm":   wide_w,
                "narrow_column_mm": narrow_w,
            })
        result["column_suggestions"] = suggestions

    return _ok(result)


# ── visualize_cabinet ─────────────────────────────────────────────────────────

def _cabinet_assembly(
    cfg: CabinetConfig,
    columns_raw: list | None,
    *,
    num_bays: int = 1,
    furniture_top: bool | None = None,
    divider_full_height: bool = True,
    include_manga: bool = False,
    include_feet: bool = True,
):
    """Build the CadQuery assembly for one cabinet config (column-aware).

    Shared by ``visualize_cabinet`` (single cabinet) and
    ``visualize_project`` (one assembly per project cabinet, composed at
    run offsets). Returns ``(assembly, parts, info)``.
    """
    divider_top_z: float | None = None

    if columns_raw:
        # Build one bay config per column so build_multi_bay_cabinet renders
        # the correct dividers.  Bay exterior width = column interior width +
        # 2×side_thickness; the multi-bay function handles shared dividers.
        # Determine which column indices have door slots, then assign hinge sides:
        # leftmost door column → "left", rightmost → "right" (French-door style).
        _has_door = [
            any(_to_opening(r).opening_type in ("door", "door_pair")
                for r in _stack_from_column(col))
            for col in columns_raw
        ]
        _door_col_indices = [i for i, has in enumerate(_has_door) if has]
        _rightmost_door_col = _door_col_indices[-1] if _door_col_indices else -1

        # The column→bay split is cabinet.bays_from_config — the same
        # transform the cutlist, evaluator, and design_pulls use, so every
        # consumer of face_layout agrees on what a bay is. Only the render
        # layers hinge sides on top.
        bay_configs = [
            dataclasses.replace(
                bay,
                door_hinge_side=(
                    "right" if col_idx == _rightmost_door_col
                    and len(_door_col_indices) > 1 else cfg.door_hinge_side),
            )
            for col_idx, bay in enumerate(_bays_from_config(cfg, columns_raw))
        ]
        total_width = cfg.width
        info = {"width": total_width, "height": cfg.height, "depth": cfg.depth,
                "columns": len(bay_configs)}

        # Detect drawer-to-door transitions per column; use lowest transition z.
        # Door floors are real carcass parts now, emitted by
        # build_multi_bay_cabinet from cabinet.opening_stack — the same walk
        # the cutlist reads. Nothing is derived here any more.
        #
        # divider_full_height=False still clips the divider to the top of the
        # LOWEST door floor, which is the only height that ever meant
        # anything for it.
        if not divider_full_height:
            from .cabinet import opening_stack as _ostack
            tops = [sl.floor_z + bc.shelf_thickness
                    for bc in bay_configs for sl in _ostack(bc)
                    if sl.has_floor]
            if tops:
                divider_top_z = min(tops)
    else:
        bay_configs = [cfg] * num_bays
        info = {"width": cfg.width * num_bays, "height": cfg.height, "depth": cfg.depth}

    # face_top_overhang is no longer computed here — cabinet.face_layout owns
    # the door-transition rule (doors above drawers extend the face stack to
    # the carcass exterior top) so the cutlist applies the identical geometry.
    assy, parts = _build_multi_bay_cabinet(
        bay_configs,
        feet_at_dividers=(columns_raw is None),
        furniture_top=furniture_top,
        divider_top_z=divider_top_z,
        include_manga=include_manga,
        include_feet=include_feet,
    )
    return assy, parts, info


async def _tool_visualize_cabinet(args: dict) -> list[types.TextContent]:
    name          = _safe_stem(args.pop("name", "cabinet"), kind="visualization name")
    output_dir    = str(args.pop("output_dir", "~/.cabineteer/visualizations"))
    open_browser  = bool(args.pop("open_browser", True))
    tolerance     = float(args.pop("tolerance", 0.1))
    num_bays      = int(args.pop("num_bays", 1))
    finish        = args.pop("finish", None)
    drawer_box_finish = args.pop("drawer_box_finish", None)
    grain_direction   = str(args.pop("grain_direction", "vertical"))
    columns_raw        = args.pop("columns", None)
    _ft                = args.pop("furniture_top", None)
    furniture_top      = None if _ft is None else bool(_ft)
    divider_full_height = bool(args.pop("divider_full_height", True))
    include_manga      = bool(args.pop("manga", False))
    cfg = _build_cabinet_config(args)

    assy, parts, info = _cabinet_assembly(
        cfg, columns_raw,
        num_bays=num_bays,
        furniture_top=furniture_top,
        divider_full_height=divider_full_height,
        include_manga=include_manga,
    )
    caveats = _render_caveats(cfg).get("render_caveats") or []
    if caveats:
        info = {**info, "render_caveats": caveats}
    result = _visualize_assembly(
        assy,
        parts,
        output_dir=output_dir,
        name=name,
        open_browser=open_browser,
        tolerance=tolerance,
        info=info,
        finish=finish,
        drawer_box_finish=drawer_box_finish,
        grain_direction=grain_direction,
        cutlist_prompt=(
            f"Generate the cutlist for cabinet '{name}' "
            "(generate_cutlist with the same design parameters)."
        ),
    )

    return _ok({
        "html":        result["html"],
        "glb":         result["glb"],
        "parts":       result["parts"],
        "glb_size_kb": result["glb_size_kb"],
        "note": (
            "HTML viewer written. Open the 'html' path in a browser to inspect "
            "the 3D model (orbit with left-drag, pan with right-drag, scroll to "
            "zoom). The side panel has a live finish dropdown, a grain-direction "
            "toggle, and a Generate-cutlist button that copies a request to "
            "paste back to the assistant."
        ),
        **_render_caveats(cfg),
    })


#: Things the 3D deliberately does not model. Stated in the tool result so a
#: render is never read as a picture of the cut parts: the "furniture_top" cap
#: once existed in approved renders and on no cutlist, and cost a batch of
#: paper. A caveat is cheaper than a silent divergence.
def _render_caveats(cfg: CabinetConfig, keyed: bool = False):
    """What the 3D deliberately does not model, in the tool result AND on the
    page (``visualize`` puts them in the viewer's info panel).

    ``keyed`` returns ``[(kind, sentence)]`` so a multi-cabinet run can
    collapse by kind — the sentences interpolate per-cabinet numbers, so
    deduping the sentences themselves does not work.
    """
    out: list[tuple[str, str]] = []
    if getattr(cfg, "carcass_corner_style", "butt") == "miter":
        out.append(("miter",
            "Mitered corners are NOT modelled: the render draws butt corners, "
            "with the top and bottom between the sides. The cutlist is the "
            "correct document — it cuts them to the full exterior width "
            f"({cfg.width:g} mm long-point) with 45° bevels on both ends."))
    if getattr(cfg, "edge_band_mode", "none") == "hardwood":
        out.append(("hardwood_band",
            "Hardwood edge banding is not drawn. The render shows finished "
            "sizes; the cutlist cuts cores "
            f"{float(getattr(cfg, 'edge_band_thickness_mm', 0.6)):g} mm "
            "smaller per banded edge."))
    if keyed:
        return out
    return {"render_caveats": [s for _k, s in out]} if out else {}


# ── list_presets ──────────────────────────────────────────────────────────────

async def _tool_list_presets(args: dict) -> list[types.TextContent]:
    category = args.get("category")
    tag      = args.get("tag")

    presets = _list_presets(category=category, tag=tag)

    return _ok({
        "count": len(presets),
        "presets": [p.summary() for p in presets],
    })


# ── apply_preset ──────────────────────────────────────────────────────────────

async def _tool_apply_preset(args: dict) -> list[types.TextContent]:
    name      = args.get("name", "")
    overrides = args.get("overrides") or {}

    synonym_redirect: str | None = None
    try:
        preset = get_preset(name)
    except KeyError:
        # Try resolving via furniture-type synonym (e.g. "dresser" → "bedroom_dresser").
        synonym_slugs = SYNONYM_TO_PRESETS.get(name.lower().strip(), ())
        resolved = next(
            (get_preset(slug) for slug in synonym_slugs if slug in PRESETS),
            None,
        )
        if resolved is not None:
            preset = resolved
            synonym_redirect = name
        else:
            ref = get_furniture(name)
            note = (
                f"No preset named {name!r}. "
                + (
                    f"Closest furniture type: '{ref.piece}' ({ref.category}). "
                    f"Suggested presets: {list(ref.preset_keys) or 'none yet'}."
                    if ref else
                    "No matching furniture type found either."
                )
            )
            return _ok({"error": note, "available": sorted(PRESETS.keys())})

    # Merge: start from preset's config dict, apply caller overrides on top.
    merged = preset.config_dict()
    for key, value in overrides.items():
        if key == "carcass_joinery" and isinstance(value, str):
            merged[key] = value          # kept as string; _build_cabinet_config handles enum
        else:
            merged[key] = value

    # Validate merged config builds without error.
    try:
        cfg = _build_cabinet_config(dict(merged))
    except Exception as exc:
        return _err(f"Override produced an invalid config: {type(exc).__name__}: {exc}")

    # The openings share the interior with any door floors, exactly as the
    # columns share the interior width with their dividers.
    interior_h = _openings_span(cfg)
    stack_total = sum(op.height_mm for op in cfg.openings)
    stack_matches = abs(stack_total - interior_h) < 0.01

    result: dict[str, Any] = {
        "preset_name":   preset.name,
        "display_name":  preset.display_name,
        "description":   preset.description,
        "category":      preset.category,
        "config":        merged,
        "interior_height_mm":          interior_h,
        "opening_stack_total_mm":      stack_total,
        "opening_stack_matches_interior": stack_matches,
    }
    if synonym_redirect:
        result["resolved_from"] = synonym_redirect

    if not stack_matches and cfg.openings:
        diff = interior_h - stack_total
        result["opening_stack_warning"] = (
            f"Opening stack ({stack_total:.0f} mm) does not fill interior height "
            f"({interior_h:.0f} mm) — {abs(diff):.0f} mm "
            f"{'unaccounted for' if diff > 0 else 'over by'}. "
            "Update drawer_config heights to match."
        )

    return _ok(result)


# ── identify_furniture_type ───────────────────────────────────────────────────

async def _tool_identify_furniture_type(args: dict) -> list[types.TextContent]:
    query = str(args.get("name", "")).strip()
    if not query:
        return _ok({"error": "'name' is required."})

    matches = identify_furniture(query)
    if not matches:
        return _ok({
            "error": f"No furniture type found matching {query!r}.",
            "suggestion": "Try a common English name or synonym (e.g. 'dresser', 'armoire', 'credenza').",
        })

    def _enrich(ref) -> dict:
        d = ref.to_dict()
        d["presets"] = [
            PRESETS[slug].summary()
            for slug in ref.preset_keys
            if slug in PRESETS
        ]
        return d

    if len(matches) == 1:
        return _ok({"match": _enrich(matches[0])})

    return _ok({
        "candidates": [_enrich(r) for r in matches],
        "note": f"Multiple furniture types match {query!r}. Narrow your query for an exact match.",
    })


# ── auto_fix_cabinet ─────────────────────────────────────────────────────────

async def _tool_auto_fix_cabinet(args: dict) -> list[types.TextContent]:
    cfg = _build_cabinet_config(args)
    result: AutoFixResult = _auto_fix(cfg)

    errors_before = sum(1 for i in result.initial_issues if i.severity == Severity.ERROR)
    errors_after  = sum(1 for i in result.final_issues   if i.severity == Severity.ERROR)

    # Re-serialise the (possibly modified) config so it can be passed back
    # to evaluate_cabinet or describe_design directly.  Use the project layer's
    # full, round-trippable serializer so EVERY non-default field survives
    # (back_rabbet_*, dado_depth, columns, pulls, fixed shelves, drawer_joinery,
    # joinery specs) — a hand-picked subset silently drops fields the auto-fixer
    # may have touched, so a round-tripped config could re-evaluate dirty.
    from .project import _config_to_dict
    fixed_cfg = result.config
    config_dict: dict[str, Any] = _config_to_dict(fixed_cfg)
    # `_config_to_dict` emits the canonical `openings` list of dicts.  Also echo a
    # `drawer_config` in [[height_mm, opening_type], ...] form (matching every
    # other config-echoing tool, e.g. apply_preset) so the repaired config can be
    # piped straight back into a tool's `drawer_config` arg without conversion.
    if not fixed_cfg.columns:
        config_dict["drawer_config"] = [
            [op.height_mm, op.opening_type] for op in fixed_cfg.openings
        ]

    return _ok({
        "config":         config_dict,
        "changes":        result.changes,
        "errors_before":  errors_before,
        "errors_after":   errors_after,
        "fixed":          result.fixed,
        "clean":          result.clean,
        "initial_issues": _issues_to_dicts(result.initial_issues),
        "final_issues":   _issues_to_dicts(result.final_issues),
        "fixable_checks": fixable_checks(),
    })


# ── describe_design ──────────────────────────────────────────────────────────

async def _tool_describe_design(args: dict) -> list[types.TextContent]:
    cfg = _build_cabinet_config(args)
    return _ok(_describe_design(cfg))


# ── design_legs ───────────────────────────────────────────────────────────────

def _compute_leg_positions(
    width: float,
    depth: float,
    count: int,
    pattern: str,
    inset: float,
) -> list[dict[str, float]]:
    """Return a list of {x, y} foot-centre coordinates.

    Origin is the front-left corner of the cabinet.  X runs left→right,
    Y runs front→back.
    """
    positions: list[dict[str, float]] = []

    if pattern == "corners":
        corners = [
            {"x": inset,         "y": inset},
            {"x": width - inset, "y": inset},
            {"x": inset,         "y": depth - inset},
            {"x": width - inset, "y": depth - inset},
        ]
        if count <= 0:
            positions = []
        elif count < 4:
            # Fewer than four feet: pick diagonally-opposite corners so the
            # cabinet is supported front-and-back, not both on the same edge.
            order = [corners[0], corners[3], corners[1], corners[2]]
            positions = order[:count]
        else:
            positions = list(corners)
            # More than 4 feet: add evenly-spaced extras along front/back.
            extra = count - 4
            if extra > 0:
                spacing = width / (extra + 1)
                for i in range(extra):
                    x = spacing * (i + 1)
                    positions.append({"x": x, "y": inset})
                    positions.append({"x": x, "y": depth - inset})

    elif pattern == "corners_and_midspan":
        positions = [
            {"x": inset,         "y": inset},
            {"x": width - inset, "y": inset},
            {"x": inset,         "y": depth - inset},
            {"x": width - inset, "y": depth - inset},
            {"x": width / 2,     "y": inset},
            {"x": width / 2,     "y": depth - inset},
        ]

    elif pattern == "along_front_back":
        # Split count evenly front/back; an odd count rounds the front row up so
        # every requested foot is placed (the trailing slice never drops one).
        front_n = -(-count // 2)   # ceil(count / 2)
        back_n  = count // 2       # floor(count / 2)

        def _row(n: int, y: float) -> list[dict[str, float]]:
            if n <= 0:
                return []
            if n == 1:
                return [{"x": width / 2, "y": y}]
            sp = (width - 2 * inset) / (n - 1)
            return [{"x": inset + sp * i, "y": y} for i in range(n)]

        positions = _row(front_n, inset) + _row(back_n, depth - inset)

    # Trim or pad to exactly count feet
    return positions[:count]


async def _tool_design_legs(args: dict) -> list[types.TextContent]:
    cab_width  = float(args.get("cabinet_width",  600.0))
    cab_depth  = float(args.get("cabinet_depth",  550.0))
    leg_key    = str(args.get("leg_key",   "richelieu_176138106"))
    count      = int(args.get("count",     4))
    pattern    = str(args.get("leg_pattern", "corners"))
    inset      = float(args.get("inset_mm", 30.0))
    cab_weight = float(args.get("cabinet_weight_kg", 0.0))

    try:
        leg = get_leg(leg_key)
    except KeyError as exc:
        return _err(str(exc))

    positions = _compute_leg_positions(cab_width, cab_depth, count, pattern, inset)
    actual_count = len(positions)

    load_per_leg: float | None = None
    load_note: str = ""
    if cab_weight > 0 and actual_count > 0:
        load_per_leg = cab_weight / actual_count
        capacity = leg.load_capacity_kg
        if load_per_leg > capacity:
            load_note = (
                f"WARNING: load per leg ({load_per_leg:.1f} kg) exceeds "
                f"rated capacity ({capacity:.1f} kg). Add more legs or choose a heavier spec."
            )
        else:
            load_note = (
                f"OK — {load_per_leg:.1f} kg per leg vs {capacity:.1f} kg rated "
                f"({(load_per_leg / capacity * 100):.0f}% capacity)."
            )

    pack_qty = max(getattr(leg, "pack_quantity", 1), 1)
    packs_needed = -(-actual_count // pack_qty)  # ceiling div
    ordering_note = (
        f"Sold in {pack_qty}-packs — order {packs_needed} pack(s) for {actual_count} legs."
        if pack_qty > 1
        else f"Order {actual_count} individual legs."
    )

    result: dict[str, Any] = {
        "leg": {
            "key":               leg_key,
            "name":              leg.name,
            "height_mm":         leg.height_mm,
            "base_diameter_mm":  leg.base_diameter_mm,
            "is_adjustable":     leg.is_adjustable,
            "load_capacity_kg":  leg.load_capacity_kg,
            "finish":            leg.finish,
            "part_number":       leg.part_number,
            "notes":             leg.notes,
        },
        "count":           actual_count,
        "pattern":         pattern,
        "inset_mm":        inset,
        "total_height_mm": leg.height_mm,
        "placement_mm":    positions,
        "ordering":        ordering_note,
    }
    if load_per_leg is not None:
        result["load_per_leg_kg"] = round(load_per_leg, 2)
        result["load_check"]      = load_note

    return _ok(result)


# ── design_pulls ──────────────────────────────────────────────────────────────

async def _tool_design_pulls(args: dict) -> list[types.TextContent]:
    """Compute per-slot pull placements + cabinet-level pull issues + BOM.

    Walks the cabinet's drawer_config (or columns) once, building a parametric
    DrawerConfig / DoorConfig per slot, and reports placements + per-slot fit
    issues. Cabinet-level style consistency is checked separately.
    """
    from .drawer import DrawerConfig, box_config_for_opening as _box_config_for_opening
    from .door import DoorConfig
    from .cutlist import pull_lines_for_cabinet_config
    from .evaluation import (
        check_drawer_pull,
        check_door_pull,
        check_cabinet_pull_consistency,
    )

    # Tool-only knobs that don't live on CabinetConfig — strip before building.
    drawer_pull_vertical = args.pop("drawer_pull_vertical", "center")
    door_pull_vertical   = args.pop("door_pull_vertical",   "center")

    try:
        cab_cfg = _build_cabinet_config(args)
    except (TypeError, ValueError, KeyError) as exc:
        return _err(f"Could not build cabinet config: {exc}")

    drawer_slots: list[dict[str, Any]] = []
    door_slots: list[dict[str, Any]] = []

    # Real face/leaf rectangles from the single geometry source — keyed by
    # (bay, slot). DrawerConfig/DoorConfig's own face dims are the legacy
    # bases and do NOT match the assembled front.
    _pull_panels = _face_layout(_bays_from_config(cab_cfg))
    _face_rects = {(q.bay, q.slot): q for q in _pull_panels
                   if q.kind == "drawer_face"}
    _door_rects = {(q.bay, q.slot): q for q in _pull_panels
                   if q.kind == "door" and q.leaf == 0}
    _door_leaf_count: dict[tuple[int, int], int] = {}
    for q in _pull_panels:
        if q.kind == "door":
            k = (q.bay, q.slot)
            _door_leaf_count[k] = _door_leaf_count.get(k, 0) + 1

    def _walk_stack(stack, interior_width: float, interior_depth: float,
                    column_index: int | None) -> None:
        for slot_idx, item in enumerate(stack):
            op = _to_opening(item)
            opening_h  = op.height_mm
            slot_type  = op.opening_type
            base: dict[str, Any] = {
                "slot_index":        slot_idx,
                "opening_height_mm": opening_h,
                "slot_type":         slot_type,
            }
            if column_index is not None:
                base["column_index"] = column_index

            if slot_type == "drawer":
                pull_key = op.pull_key or cab_cfg.drawer_pull
                if pull_key is None:
                    continue  # nothing to place
                fp = _face_rects.get((column_index or 0, slot_idx))
                # Feed the real panel dims through the face_overlay knobs so
                # pull_placements / fit checks operate on the face that will
                # actually be drilled.
                dcfg = DrawerConfig(
                    opening_width=interior_width,
                    opening_height=opening_h,
                    opening_depth=interior_depth,
                    slide_key=op.slide_key or cab_cfg.drawer_slide,
                    pull_key=pull_key,
                    pull_vertical=drawer_pull_vertical,
                    face_overlay_sides=((fp.width - interior_width) / 2
                                        if fp else 10.0),
                    face_overlay_top=((fp.height - opening_h) / 2
                                      if fp else 3.0),
                    face_overlay_bottom=((fp.height - opening_h) / 2
                                         if fp else 3.0),
                )
                try:
                    placements = dcfg.pull_placements
                except KeyError:
                    placements = []
                issues = check_drawer_pull(dcfg)
                drawer_slots.append({
                    **base,
                    "face_width_mm":   dcfg.face_width,
                    "face_height_mm":  dcfg.face_height,
                    "pull_key":        pull_key,
                    "vertical_policy": drawer_pull_vertical,
                    "placements":      _pull_placements_to_dicts(placements),
                    "count":           len(placements),
                    "issues":          _issues_to_dicts(issues),
                })

            elif slot_type in ("door", "door_pair"):
                pull_key = op.pull_key or cab_cfg.door_pull
                hinge_key = op.hinge_key or cab_cfg.door_hinge
                if pull_key is None:
                    continue
                dk = (column_index or 0, slot_idx)
                # Honor per-opening num_doors (the layout and hinge BOM do)
                # and feed the real stack-anchored leaf height through the
                # reveal knobs so door_height — and therefore every pull
                # placement — matches the leaf that actually gets drilled.
                num_doors = _door_leaf_count.get(
                    dk, op.num_doors or (2 if slot_type == "door_pair" else 1))
                _leaf = _door_rects.get(dk)
                _gap_tb = ((opening_h - _leaf.height) / 2
                           if _leaf is not None else 2.0)
                dcfg = DoorConfig(
                    opening_width=interior_width,
                    opening_height=opening_h,
                    num_doors=num_doors,
                    hinge_key=hinge_key,
                    pull_key=pull_key,
                    pull_vertical=door_pull_vertical,
                    gap_top=_gap_tb,
                    gap_bottom=_gap_tb,
                )
                try:
                    placements = dcfg.pull_placements
                    total = dcfg.total_pull_count
                except KeyError:
                    placements = []
                    total = 0
                issues = check_door_pull(dcfg)
                door_slots.append({
                    **base,
                    "num_doors":           num_doors,
                    "leaf_width_mm":       dcfg.door_width,
                    "leaf_height_mm":      dcfg.door_height,
                    "pull_key":            pull_key,
                    "vertical_policy":     door_pull_vertical,
                    "placements_per_leaf": _pull_placements_to_dicts(placements),
                    "pulls_per_leaf":      len(placements),
                    "total_pulls":         total,
                    "issues":              _issues_to_dicts(issues),
                })
            # shelf / open slots contribute nothing

    if getattr(cab_cfg, "columns", None):
        for ci, col in enumerate(cab_cfg.columns):
            _walk_stack(col.openings, col.width_mm, cab_cfg.interior_depth, ci)
    else:
        _walk_stack(cab_cfg.openings, cab_cfg.interior_width,
                    cab_cfg.interior_depth, None)

    cabinet_issues = check_cabinet_pull_consistency(cab_cfg)
    bom_lines = pull_lines_for_cabinet_config(cab_cfg)

    result: dict[str, Any] = {
        "exterior": {
            "width_mm":  cab_cfg.width,
            "height_mm": cab_cfg.height,
            "depth_mm":  cab_cfg.depth,
        },
        "drawer_pull":          cab_cfg.drawer_pull,
        "door_pull":            cab_cfg.door_pull,
        "drawer_pull_vertical": drawer_pull_vertical,
        "door_pull_vertical":   door_pull_vertical,
        "drawer_slots":         drawer_slots,
        "door_slots":           door_slots,
        "cabinet_issues":       _issues_to_dicts(cabinet_issues),
        "hardware_bom":         [_hardware_line_to_dict(l) for l in bom_lines],
        "bom_totals": {
            "line_count":     len(bom_lines),
            "pieces_needed":  sum(l.pieces_needed for l in bom_lines),
            "packs_to_order": sum(l.packs_to_order for l in bom_lines),
        },
    }
    return _ok(result)


# ─── Project tools ────────────────────────────────────────────────────────────


def _project_from_args(args: dict):
    """Resolve a CabinetProject from either ``project_name`` (load from disk)
    or ``project`` (inline payload). Returns the project object.
    """
    from .project import build_project, load_project

    inline = args.get("project")
    name   = args.get("project_name")
    if inline:
        return build_project(inline)
    if name:
        return load_project(str(name))
    raise ValueError("Provide either 'project_name' or 'project'.")


def _columns_dict_from_cfg(cfg: CabinetConfig) -> list | None:
    """Re-derive the ``columns`` list-of-dicts shape from a resolved
    CabinetConfig, using the dict form for openings so per-opening overrides
    (hinge_key, pull_key, num_doors, hinge_side, door_thickness) survive —
    every downstream consumer normalizes rows via ``to_opening``, which
    accepts dicts. Returns None for single-column cabinets."""
    from .project import _opening_to_dict

    if not cfg.columns:
        return None
    out = []
    for col in cfg.columns:
        d: dict = {
            "width_mm": col.width_mm,
            "openings": [_opening_to_dict(op) for op in col.openings],
        }
        if col.fixed_shelf_positions:
            d["fixed_shelf_positions"] = list(col.fixed_shelf_positions)
        out.append(d)
    return out


def _project_summary(project, path) -> dict:
    """Per-cabinet resolved summary shared by design/update project handlers."""
    from .project import check_project_consistency

    resolved = project.resolved()
    per_cabinet = []
    for (cname, cfg), pc in zip(resolved, project.cabinets):
        per_cabinet.append({
            "name": cname,
            "exterior_mm": {"width": cfg.width, "height": cfg.height, "depth": cfg.depth},
            "drawer_slide": cfg.drawer_slide,
            "door_hinge":   cfg.door_hinge,
            "drawer_pull":  cfg.drawer_pull,
            "door_pull":    cfg.door_pull,
            "carcass_joinery": cfg.carcass_joinery.value,
            "drawer_joinery":  cfg.drawer_joinery.value,
            "drawer_corner_lip_mm": cfg.drawer_corner_lip_mm,
            "overrides":       sorted(pc.overrides),
        })

    out = {
        "name": project.name,
        "cabinet_count": len(resolved),
        "total_run_width_mm": round(sum(cfg.width for _, cfg in resolved), 1),
        "cabinets": per_cabinet,
        "consistency_issues": check_project_consistency(project),
        "saved_to": str(path),
    }
    if project.forked_from is not None:
        out["forked_from"] = project.forked_from
    if project.worktop is not None:
        from .project import _worktop_to_dict
        out["worktop"] = _worktop_to_dict(project.worktop)
    return out


async def _tool_design_project(args: dict) -> list[types.TextContent]:
    from .project import build_project, save_project, project_path

    name = str(args.get("name") or "")
    if name and not args.get("overwrite", False) and project_path(name).exists():
        return _err(
            f"Project '{name}' already exists. Pass overwrite=true to replace "
            "it, use update_project for a delta edit, or duplicate_project to "
            "fork it under a new name."
        )

    project = build_project(args)
    path = save_project(project)
    return _ok(_project_summary(project, path))


async def _tool_update_project(args: dict) -> list[types.TextContent]:
    from .project import update_saved_project, project_path

    project, changes = update_saved_project(args)
    if not changes:
        return _ok({
            "name": project.name,
            "changes": [],
            "note": "Patch was empty — nothing to change; project untouched.",
        })
    result = _project_summary(project, project_path(project.name))
    result["changes"] = changes
    return _ok(result)


async def _tool_duplicate_project(args: dict) -> list[types.TextContent]:
    from .project import duplicate_project, load_project

    name = str(args.get("name") or "")
    new_name = str(args.get("new_name") or "")
    if not name or not new_name:
        return _err("Provide 'name' (source) and 'new_name' (the fork).")
    path = duplicate_project(name, new_name, notes=args.get("notes"))
    project = load_project(new_name)
    result = _project_summary(project, path)
    result["forked_at"] = project.forked_at
    result["note"] = (
        f"'{new_name}' is an independent copy of '{name}' — edit it with "
        "update_project (or design_project overwrite=true); the original is untouched."
    )
    return _ok(result)


async def _tool_list_projects(args: dict) -> list[types.TextContent]:
    from .project import list_saved_projects, project_dir

    query = args.get("query")
    include_all = bool(args.get("include_all", False))
    sort = str(args.get("sort", "recent"))
    entries = list_saved_projects(
        query=str(query) if query else None,
        include_all=include_all,
        sort=sort,
    )
    names = [e["name"] for e in entries if "error" not in e]
    result = {
        "count": len(names),
        "unreadable": len(entries) - len(names),
        "names": names,
        "projects": entries,
        "directory": str(project_dir()),
        "sort": sort,
    }
    if query:
        result["query"] = str(query)
    elif not include_all:
        result["note"] = (
            "Dev artifacts (eval_/test_/smoke_/_ names) hidden; pass "
            "include_all=true or a query to see them."
        )
    return _ok(result)


async def _tool_rename_project(args: dict) -> list[types.TextContent]:
    from .project import rename_project

    name = args.get("name")
    new_name = args.get("new_name")
    if not name or not new_name:
        return _err("Provide both 'name' and 'new_name'.")
    path = rename_project(str(name), str(new_name))
    return _ok({
        "renamed": str(name),
        "to": str(new_name),
        "path": str(path),
        "note": (
            "Previously generated cutlists/visualizations keep the old "
            "stem; regenerate to pick up the new name."
        ),
    })


async def _tool_delete_project(args: dict) -> list[types.TextContent]:
    from .project import delete_project

    name = args.get("name")
    if not name:
        return _err("Provide 'name' (see list_projects).")
    path = delete_project(str(name))
    return _ok({
        "deleted": str(name),
        "path": str(path),
        "note": "Snapshot removed permanently; output files were not touched.",
    })


async def _tool_load_project(args: dict) -> list[types.TextContent]:
    from .project import load_project, project_to_dict

    name = args.get("name") or args.get("project_name")
    if not name:
        return _err("Provide 'name' (see list_projects for saved names).")
    project = load_project(str(name))

    per_cabinet = []
    for (cname, cfg), pc in zip(project.resolved(), project.cabinets):
        per_cabinet.append({
            "name": cname,
            "exterior_mm": {"width": cfg.width, "height": cfg.height, "depth": cfg.depth},
            "drawer_slide": cfg.drawer_slide,
            "door_hinge":   cfg.door_hinge,
            "carcass_joinery": cfg.carcass_joinery.value,
            "drawer_joinery":  cfg.drawer_joinery.value,
            "drawer_corner_lip_mm": cfg.drawer_corner_lip_mm,
            "overrides":       sorted(pc.overrides),
        })

    return _ok({
        "name": project.name,
        "cabinet_count": len(project.cabinets),
        "project": project_to_dict(project),
        "resolved": per_cabinet,
        "note": (
            "'project' is the durable payload — pass it back to design_project "
            "(same name) to continue editing, or reference the project by name "
            "in evaluate_project / visualize_project / generate_project_cutlist."
        ),
    })


async def _tool_evaluate_project(args: dict) -> list[types.TextContent]:
    from .project import check_project_consistency

    project = _project_from_args(args)

    by_cabinet: dict[str, dict] = {}
    total_errors = 0
    total_warnings = 0
    for cname, cfg in project.resolved():
        issues = evaluate_cabinet(cab_cfg=cfg)
        errors   = [i for i in issues if i.severity == Severity.ERROR]
        warnings = [i for i in issues if i.severity == Severity.WARNING]
        infos    = [i for i in issues if i.severity == Severity.INFO]
        total_errors   += len(errors)
        total_warnings += len(warnings)
        by_cabinet[cname] = {
            "summary": {
                "errors":   len(errors),
                "warnings": len(warnings),
                "info":     len(infos),
                "pass":     len(errors) == 0,
            },
            "issues": _issues_to_dicts(issues),
        }

    project_issues = check_project_consistency(project)
    total_errors   += sum(1 for i in project_issues if i["severity"] == "error")
    total_warnings += sum(1 for i in project_issues if i["severity"] == "warning")

    return _ok({
        "project": project.name,
        "summary": {
            "cabinet_count":  len(project.cabinets),
            "error_count":    total_errors,
            "warning_count":  total_warnings,
            "pass":           total_errors == 0,
        },
        "by_cabinet":     by_cabinet,
        "project_issues": project_issues,
    })


async def _tool_generate_project_cutlist(args: dict) -> list[types.TextContent]:
    # Batch mode: 'project_names' loads several saved projects and merges
    # them into one cutlist run; otherwise resolve the single project from
    # 'project_name' / inline 'project' as before.
    raw_names = args.get("project_names") or []
    if isinstance(raw_names, str):
        # A bare string would iterate per character ("abc" → load "a",
        # "b", "c") and die on a baffling FileNotFoundError.
        raise ValueError(
            "project_names must be a list of saved project names, "
            f"got the string {raw_names!r}.")
    batch_names = list(dict.fromkeys(
        str(n) for n in raw_names
    ))  # de-dupe, order-preserving — a repeated name would double-count panels
    result_notes: list[str] = []
    if batch_names:
        from .project import load_project, project_path
        projects = [load_project(n) for n in batch_names]
        explicit_batch_name = args.get("batch_name")
        if explicit_batch_name:
            out_name = str(explicit_batch_name)
            # Reusing a saved project's name would silently overwrite that
            # project's own cutlist files under ~/.cabineteer/cutlists/.
            if project_path(out_name).exists() and out_name not in batch_names:
                result_notes.append(
                    f"batch_name {out_name!r} matches a saved project — its "
                    f"cutlist files in ~/.cabineteer/cutlists/{out_name}/ "
                    f"are overwritten by this batch."
                )
        else:
            out_name = "-".join(p.name for p in projects)
            if len(out_name) > _MAX_STEM_LEN:
                out_name = out_name[:_MAX_STEM_LEN].rstrip(".- ")
    else:
        if args.get("batch_name"):
            return _err(
                "'batch_name' only applies to batch mode — pass 'project_names' "
                "with it, or omit it for a single project."
            )
        projects = [_project_from_args(args)]
        out_name = projects[0].name
    # Inline project payloads and batch names skip save_project's validation,
    # so re-check before the name becomes an output directory / file stem.
    _safe_stem(out_name, kind="batch name" if batch_names else "project name")

    fmt          = args.get("format", "both")
    sheet_length = float(args.get("sheet_length", 2440))
    sheet_width  = float(args.get("sheet_width",  1220))
    sheet_size_overrides = _parse_sheet_size_overrides(
        args.get("sheet_size_overrides"))
    optimizer_overrides = _parse_optimizer_overrides(
        args.get("optimizer_overrides"))
    kerf         = float(args.get("kerf", 3.2))
    optimizer    = str(args.get("optimizer", "auto"))
    paper        = str(args.get("paper", "letter")).lower()
    if paper not in ("letter", "a4"):
        raise ValueError(f"paper must be 'letter' or 'a4', got {paper!r}.")

    # Accumulate raw panels and hardware lines across every child cabinet.
    raw_carcass:    list[CutlistPanel] = []
    raw_6mm:        list[CutlistPanel] = []
    raw_box:        list[CutlistPanel] = []
    raw_false:      list[CutlistPanel] = []
    hw_lines_all:   list = []

    per_cabinet_summary = []
    total_cabinets = 0
    band_doc_cfg = None  # first hardwood cfg with a stock spec → banding doc
    band_doc_key = None  # that cfg's band-token key
    band_doc_panels: list = []  # panels of every group matching that key
    from .cutlist import edge_band_lines_for_panels
    for project in projects:
        # Band lines aggregate per PROJECT, not per cabinet: one line per
        # material with a single waste ceiling, instead of N per-cabinet
        # lines whose merged notes concatenate ("16.0 m…, 16.5 m…, 16.0 m")
        # and whose per-cabinet ceils inflate the footage.
        band_groups: dict[tuple, tuple] = {}
        for cname, cfg in project.resolved():
            total_cabinets += 1
            columns_raw = _columns_dict_from_cfg(cfg)
            c, b, x, f = _raw_panels_for_cabinet(cfg, columns_raw)
            hw = hardware_bom_for_cabinet_config(cfg, columns_raw)
            if getattr(cfg, "edge_band_mode", "none") != "none":
                bkey = (cfg.edge_band_mode, cfg.edge_band_thickness_mm,
                        cfg.edge_band_material,
                        tuple(sorted((cfg.edge_band_stock or {}).items())))
                band_groups.setdefault(bkey, (cfg, []))[1].extend(c + f)
                if (band_doc_cfg is None
                        and cfg.edge_band_mode == "hardwood"
                        and cfg.edge_band_stock):
                    band_doc_cfg = cfg
                    band_doc_key = bkey
            if batch_names:
                # Tag provenance so panels stay project-distinct rows through
                # consolidation and the layout colours/labels by project.
                for panel in (*c, *b, *x, *f):
                    panel.source = project.name
                for line in hw:
                    line.source = project.name
            raw_carcass.extend(c)
            raw_6mm.extend(b)
            raw_box.extend(x)
            raw_false.extend(f)
            hw_lines_all.extend(hw)

            per_cabinet_summary.append({
                # Cabinet names repeat across projects ("left", "a", …) — in a
                # batch, qualify each with its project so rows stay unambiguous.
                "name": f"{project.name}/{cname}" if batch_names else cname,
                "exterior_mm": {"width": cfg.width, "height": cfg.height, "depth": cfg.depth},
                "panel_count_raw": sum(len(lst) for lst in (c, b, x, f)),
            })

        for bkey, (bcfg, bpanels) in band_groups.items():
            band_lines = edge_band_lines_for_panels(bpanels, bcfg)
            if batch_names:
                for line in band_lines:
                    line.source = project.name
            hw_lines_all.extend(band_lines)
            # The banding doc covers exactly the groups sharing the doc
            # cfg's band token — never hot-melt (or other-stock) groups.
            if bkey == band_doc_key:
                band_doc_panels.extend(bpanels)

        if project.worktop is not None:
            wt = project.worktop
            panel = CutlistPanel(
                name="worktop",
                length=wt.width_mm,
                width=wt.depth_mm,
                thickness=wt.thickness_mm,
                material=wt.material,
                notes=(
                    f"desk/counter slab, top at {wt.surface_height_mm:g} mm"
                    + (f"; {len(wt.leg_points())} support legs (buy-out, "
                       f"~{wt.leg_height_mm:g} mm)" if wt.leg_points() else "")
                ),
            )
            if batch_names:
                panel.source = project.name
            raw_false.append(panel)

    # Consolidate identical panels across all cabinets — this is the merge
    # behavior the user picked. Six matching sides across three cabinets
    # become one row with quantity=6.
    from .cutlist import consolidate_hardware_lines
    carcass_panels = consolidate_bom(raw_carcass)
    panels_6mm     = consolidate_bom(raw_6mm)
    box_panels     = consolidate_bom(raw_box)
    false_fronts   = consolidate_bom(raw_false)
    hw_lines       = consolidate_hardware_lines(hw_lines_all)

    result = _cutlist_pipeline(
        name=out_name,
        out_dir=data_dir() / "cutlists" / out_name,
        carcass_panels=carcass_panels, box_panels=box_panels,
        panels_6mm=panels_6mm, false_fronts=false_fronts,
        hw_lines=hw_lines,
        sheet_length=sheet_length, sheet_width=sheet_width,
        kerf=kerf, optimizer=optimizer, fmt=fmt,
        sheet_size_overrides=sheet_size_overrides,
        optimizer_overrides=optimizer_overrides,
        band_cfg=band_doc_cfg,
        band_panels=band_doc_panels if band_doc_cfg is not None else None,
        paper=paper,
    )
    result = {
        "project": out_name,
        "cabinet_count": total_cabinets,
        "per_cabinet": per_cabinet_summary,
        **result,
    }
    if batch_names:
        result = {"projects": [p.name for p in projects], **result}
    if result_notes:
        # Append to any pipeline-emitted notes (e.g. lite-mode PDF skips)
        # instead of clobbering them.
        result["notes"] = list(result.get("notes") or []) + result_notes
    return _ok(result)


async def _tool_generate_assembly_instructions(args: dict) -> list[types.TextContent]:
    from .assembly import (
        build_assembly_plan, generate_assembly_html, generate_assembly_pdf,
        DRY_FIT_TENON_URL,
    )
    from .cutlist import assign_part_ids, consolidate_bom

    project = _project_from_args(args)
    _safe_stem(project.name, kind="project name")
    fmt = str(args.get("format", "both"))
    if fmt not in ("html", "pdf", "both"):
        # An unknown format used to return success with an empty files
        # dict and no explanation.
        raise ValueError(
            f"format must be 'html', 'pdf', or 'both', got {fmt!r}.")
    paper = str(args.get("paper", "letter")).lower()
    if paper not in ("letter", "a4"):
        raise ValueError(f"paper must be 'letter' or 'a4', got {paper!r}.")

    # Part IDs must match the project's own cutlist. Mirror the single-project
    # cutlist path: raw panels per cabinet → consolidate_bom per list →
    # assign_part_ids over the same concatenation order. (The pipeline's
    # face-pooling appends show-face panels AFTER the carcass list, and no
    # carcass part family (S/B/T/CD/SH/BK) occurs outside it, so carcass IDs
    # are unaffected by pooling.)
    raw_carcass: list[CutlistPanel] = []
    raw_6mm: list[CutlistPanel] = []
    raw_box: list[CutlistPanel] = []
    raw_false: list[CutlistPanel] = []
    per_cab: list[tuple[str, object, list[CutlistPanel]]] = []
    for cname, cfg in project.resolved():
        columns_raw = _columns_dict_from_cfg(cfg)
        c, b, x, f = _raw_panels_for_cabinet(cfg, columns_raw)
        per_cab.append((cname, cfg, c))
        raw_carcass.extend(c)
        raw_6mm.extend(b)
        raw_box.extend(x)
        raw_false.extend(f)
    carcass_panels = consolidate_bom(raw_carcass)
    assign_part_ids(
        carcass_panels + consolidate_bom(raw_box)
        + consolidate_bom(raw_6mm) + consolidate_bom(raw_false))

    def _dims_key(pl: CutlistPanel) -> tuple:
        # Thickness + material are part of the consolidation identity —
        # without them, same-outline panels in different stock collide and
        # the assembly doc labels the wrong stack (review 2026-07-29 M4).
        return (pl.name, round(min(pl.length, pl.width), 1),
                round(max(pl.length, pl.width), 1),
                round(pl.thickness, 1), pl.material)

    id_lookup = {_dims_key(pl): pl.part_id for pl in carcass_panels}

    # One plan per structurally identical cabinet design; identical cabinets
    # collapse into a single section with copies × N.
    plans = []
    by_signature: dict = {}
    skipped: list[str] = []
    for cname, cfg, craw in per_cab:
        id_map: dict[str, str] = {}
        for pl in craw:
            pid = id_lookup.get(_dims_key(pl))
            if pid and pl.name not in id_map:
                id_map[pl.name] = pid
            # Length-qualified key so shelf families with the same panel
            # name but different lengths (global vs column shelves) can be
            # told apart by the assembly maps.
            lkey = f"{pl.name}@{round(pl.length, 1)}"
            if pid and lkey not in id_map:
                id_map[lkey] = pid
        try:
            plan = build_assembly_plan(
                cfg, cabinet_name=cname, copies=1, id_map=id_map)
        except ValueError as exc:
            skipped.append(f"{cname}: {exc}")
            continue
        sig = (
            plan.size_key, round(plan.span, 1),
            tuple(j.name for j in plan.joints),
            tuple((pm.panel, round(pm.draw_width, 1),
                   round(pm.draw_height, 1), pm.rows) for pm in plan.panels),
        )
        if sig in by_signature:
            prev = by_signature[sig]
            prev.copies += 1
            prev.cabinet_name = f"{prev.cabinet_name}, {cname}"
        else:
            by_signature[sig] = plan
            plans.append(plan)

    if not plans:
        return _err(
            "No floating-tenon cabinets in this project — "
            + "; ".join(skipped))

    out_dir = data_dir() / "assembly" / project.name
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    if fmt in ("html", "both"):
        p = out_dir / f"{project.name}_assembly.html"
        p.write_text(generate_assembly_html(plans, project.name),
                     encoding="utf-8")
        files["html"] = str(p)
    notes: list[str] = []
    if fmt in ("pdf", "both"):
        try:
            p = out_dir / f"{project.name}_assembly.pdf"
            p.write_bytes(
                generate_assembly_pdf(plans, project.name, paper=paper))
            files["pdf"] = str(p)
        except ImportError:
            notes.append(
                "PDF skipped — reportlab not installed (lite mode).")

    result = {
        "project": project.name,
        "designs": [
            {
                "cabinets": plan.cabinet_name,
                "copies": plan.copies,
                "domino_size": plan.size_key,
                "festool_part_number": plan.size.part_number,
                "joints": len(plan.joints),
                "tenons_per_joint": plan.per_joint,
                "mortise_centers_from_front_mm": list(plan.positions),
                "tenons_per_cabinet": plan.tenons_per_cabinet,
                "tenons_total": plan.tenons_total,
                "corner_style": plan.corner_style,
                "edge_band_mode": plan.edge_band_mode,
                **({"miter_mortise_from_heel_mm":
                        plan.miter_placement.from_heel,
                    "miter_show_face_wall_mm":
                        plan.miter_placement.show_face_wall}
                   if plan.miter_placement is not None else {}),
            }
            for plan in plans
        ],
        "beech_tenons_total": sum(p.tenons_total for p in plans),
        "dry_fit_tenons_to_print": max(
            p.dry_fit_tenons_needed for p in plans),
        "dry_fit_tenon_model": DRY_FIT_TENON_URL,
        "files": files,
    }
    if skipped:
        result["skipped"] = skipped
    if notes:
        result["notes"] = notes
    return _ok(result)


async def _tool_visualize_project(args: dict) -> list[types.TextContent]:
    import cadquery as cq  # raises in lite mode; call_tool wraps into an error

    project = _project_from_args(args)
    # Inline project payloads skip save_project's validation, so re-check the
    # name before it becomes an output GLB/HTML file stem.
    _safe_stem(project.name, kind="project name")
    output_dir   = str(args.get("output_dir", "~/.cabineteer/visualizations"))
    open_browser = bool(args.get("open_browser", True))
    tolerance    = float(args.get("tolerance", 0.1))
    gap_mm       = float(args.get("gap_mm", 0.0))
    _ft           = args.get("furniture_top")
    furniture_top = None if _ft is None else bool(_ft)
    include_manga = bool(args.get("manga", False))
    shared_feet  = bool(args.get("shared_junction_feet", False))
    finish       = args.get("finish")
    drawer_box_finish = args.get("drawer_box_finish")
    grain_direction   = str(args.get("grain_direction", "vertical"))

    # A butted run (gap 0) can share one pair of feet per cabinet junction
    # instead of each cabinet carrying its own inner pair. Cabinet feet are
    # then suppressed and run-level feet added below.
    use_shared_feet = (
        shared_feet and gap_mm == 0 and len(project.cabinets) > 1
    )

    run_assy = cq.Assembly(name=project.name)
    all_parts: list = []
    per_cabinet = []
    x_off = 0.0
    for cname, cfg in project.resolved():
        columns_raw = _columns_dict_from_cfg(cfg)
        try:
            assy, parts, _info = _cabinet_assembly(
                cfg, columns_raw, furniture_top=furniture_top,
                include_manga=include_manga,
                include_feet=not use_shared_feet,
            )
        except ValueError as e:
            raise ValueError(f"cabinet '{cname}': {e}") from None
        run_assy.add(assy, name=cname, loc=cq.Location(cq.Vector(x_off, 0, 0)))
        all_parts.extend(parts)
        per_cabinet.append({
            "name": cname,
            "x_offset_mm": round(x_off, 1),
            "width_mm": cfg.width,
        })
        x_off += cfg.width + gap_mm

    run_width = x_off - gap_mm if project.cabinets else 0.0

    if use_shared_feet:
        resolved = project.resolved()
        cfg0 = resolved[0][1]
        try:
            leg_spec = get_leg(cfg0.leg_key)
            foot_h, foot_d = leg_spec.height_mm, leg_spec.base_diameter_mm
        except KeyError:
            foot_h, foot_d = 102.0, 50.0
        inset = cfg0.leg_inset
        # (x, depth) stations: outer ends keep their own inset pair; each
        # junction gets ONE pair centered on the seam. Feet run front-to-back
        # at the shallower neighbour's depth so they never poke out the back.
        stations: list[tuple[float, float]] = [(inset, resolved[0][1].depth)]
        acc = 0.0
        for (_, left_cfg), (_, right_cfg) in zip(resolved, resolved[1:]):
            acc += left_cfg.width
            stations.append((acc, min(left_cfg.depth, right_cfg.depth)))
        stations.append((run_width - inset, resolved[-1][1].depth))
        foot_shape = (
            cq.Workplane("XY")
            .cylinder(foot_h, foot_d / 2, centered=(True, True, False))
        )
        fi = 0
        for fx, depth in stations:
            for fy in (inset, depth - inset):
                run_assy.add(
                    foot_shape, name=f"foot_{fi}",
                    loc=cq.Location(cq.Vector(fx, fy, -foot_h)),
                    color=cq.Color(0.25, 0.25, 0.28, 1.0),
                )
                fi += 1

    worktop = project.worktop
    if worktop is not None:
        # The floor plane sits one foot-height below the carcass origin —
        # resolve it the same way build_multi_bay_cabinet resolves feet.
        floor_z = -102.0
        if project.cabinets:
            try:
                floor_z = -get_leg(project.resolved()[0][1].leg_key).height_mm
            except KeyError:
                pass
        slab_top_z = floor_z + worktop.surface_height_mm
        slab = cq.Workplane("XY").box(
            worktop.width_mm, worktop.depth_mm, worktop.thickness_mm,
            centered=False,
        )
        run_assy.add(
            slab, name="worktop",
            loc=cq.Location(cq.Vector(
                worktop.x_offset_mm,
                worktop.y_offset_mm,
                slab_top_z - worktop.thickness_mm,
            )),
            color=cq.Color(0.87, 0.72, 0.53, 1.0),
        )
        all_parts.append(PartInfo(
            name="worktop",
            shape=slab,
            material_thickness=worktop.thickness_mm,
            grain_direction="length",
        ))
        leg_pts = worktop.leg_points()
        if leg_pts:
            leg_shape = (
                cq.Workplane("XY")
                .cylinder(
                    worktop.leg_height_mm, worktop.leg_diameter_mm / 2,
                    centered=(True, True, False),
                )
            )
            for li, (lx, ly) in enumerate(leg_pts):
                run_assy.add(
                    leg_shape, name=f"worktop_leg{li}",
                    loc=cq.Location(cq.Vector(lx, ly, floor_z)),
                    color=cq.Color(0.25, 0.25, 0.28, 1.0),
                )

    info = {
        "cabinets":     len(project.cabinets),
        "run_width":    round(run_width, 1),
        "parts":        len(all_parts),
    }
    if worktop is not None:
        info["worktop"] = (
            f"{worktop.width_mm:g}×{worktop.depth_mm:g}×"
            f"{worktop.thickness_mm:g} mm @ {worktop.surface_height_mm:g} mm"
        )
    if project.wall_width_mm:
        info["wall_width"] = project.wall_width_mm

    # One caveat list for the run — the picture is one file. Grouped by the
    # caveat's KIND, not its finished sentence: the miter text interpolates
    # each cabinet's own width, so exact-string dedup emitted two nearly
    # identical lines for a 600 and a 1000 cabinet, and neither said which
    # cabinet it was about. One line per kind, naming the cabinets it covers.
    _cabs = project.resolved()
    _seen: dict[str, tuple[str, list[str]]] = {}
    for _cname, _ccfg in _cabs:
        for kind, line in _render_caveats(_ccfg, keyed=True):
            _seen.setdefault(kind, (line, []))[1].append(_cname)
    run_caveats = [
        line + (" (applies to every cabinet in the run)"
                if len(names) == len(_cabs)
                else f" (applies to: {', '.join(names)})")
        for kind, (line, names) in _seen.items()
    ]
    if run_caveats:
        info = {**info, "render_caveats": run_caveats}
    result = _visualize_assembly(
        run_assy,
        all_parts,
        output_dir=output_dir,
        name=project.name,
        open_browser=open_browser,
        tolerance=tolerance,
        info=info,
        finish=finish,
        drawer_box_finish=drawer_box_finish,
        grain_direction=grain_direction,
        cutlist_prompt=(
            f"Generate the project cutlist for '{project.name}' "
            "(generate_project_cutlist)."
        ),
    )

    out = {
        "project":      project.name,
        "cabinet_count": len(project.cabinets),
        "total_run_width_mm": round(run_width, 1),
        "per_cabinet":  per_cabinet,
        "html":         result["html"],
        "glb":          result["glb"],
        "parts":        result["parts"],
        "glb_size_kb":  result["glb_size_kb"],
        "note": (
            "HTML viewer written — cabinets are placed left-to-right at their "
            "run offsets. Viewer shortcuts (X-ray, open drawers, clip plane) "
            "work across all cabinets in the composed view. The side panel has "
            "a live finish dropdown, a grain-direction toggle, and a "
            "Generate-cutlist button that copies a request to paste back to "
            "the assistant."
        ),
    }
    if worktop is not None:
        from .project import _worktop_to_dict
        out["worktop"] = _worktop_to_dict(worktop)
    if run_caveats:
        out["render_caveats"] = run_caveats
    return _ok(out)


# ─── Port management ──────────────────────────────────────────────────────────

#: Default port for HTTP/SSE mode — chosen to be distinctive and avoid
#: accidental collision with common dev servers (3000, 3001, 8000, 8080 …).
DEFAULT_PORT: int = 3749

#: File written when the server binds in HTTP mode so other processes can
#: discover the actual port without parsing log output.  Kept under the
#: per-user ``~/.cabineteer`` directory (not a world-writable /tmp path) so a
#: predictable name can't be pre-created as a symlink by another user.
PORT_FILE: Path = Path.home() / ".cabineteer" / "cabineteer.port"


def find_free_port(start: int = DEFAULT_PORT, max_attempts: int = 20) -> int:
    """Return the first unused TCP port in ``[start, start + max_attempts)``.

    Tries each candidate port in order.  Raises ``RuntimeError`` if the entire
    range is occupied.
    """
    for port in range(start, start + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            # Do NOT set SO_REUSEADDR here — we need a true "is this port free"
            # check, not a "can I eventually take it" check.
            try:
                sock.bind(("", port))
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"No free port found in range {start}–{start + max_attempts - 1}. "
        "Use --port to choose a different starting point or --max-port-attempts "
        "to widen the search window."
    )


def write_port_file(port: int, path: Path = PORT_FILE) -> None:
    """Write the resolved port to a well-known file so other tools can read it.

    Written with ``O_NOFOLLOW`` (never follow a symlink at the final path
    component) so a pre-planted symlink at the well-known name can't redirect
    the write elsewhere.  A stale file from a previous run is truncated in
    place.
    """
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, str(port).encode("ascii"))
    finally:
        os.close(fd)


def clear_port_file(path: Path = PORT_FILE) -> None:
    """Remove the port file on clean exit."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ─── Transport runners ────────────────────────────────────────────────────────

def _init_options() -> InitializationOptions:
    return InitializationOptions(
        server_name="cabineteer",
        server_version="0.1.0",
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )


async def _run_stdio() -> None:
    """Run the server over stdin/stdout (default, for Claude Desktop / Gemini CLI)."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, _init_options())


async def _run_http(host: str, port: int) -> None:
    """Run the server over HTTP/SSE (Starlette + uvicorn)."""
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request):  # type: ignore[no-untyped-def]
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, _init_options())

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse_transport.handle_post_message),
        ],
    )

    print(
        f"cabineteer  HTTP/SSE  http://{host}:{port}/sse",
        file=sys.stderr,
        flush=True,
    )

    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="warning",   # suppress uvicorn access logs; our own startup line is enough
    )
    uv_server = uvicorn.Server(config)
    await uv_server.serve()


# ─── Canonical tool dispatch ──────────────────────────────────────────────────
# Single source of truth mapping tool name → async handler. `call_tool` (the
# MCP path), the eval harness, and the headless CLI all route through this dict,
# so there is exactly one place a new tool has to be registered. Defined here,
# after every _tool_* handler exists at module load.

TOOL_DISPATCH: dict[str, Any] = {
    "list_hardware":                 _tool_list_hardware,
    "list_joinery_options":          _tool_list_joinery,
    "design_cabinet":                _tool_design_cabinet,
    "design_multi_column_cabinet":   _tool_design_multi_column_cabinet,
    "evaluate_cabinet":              _tool_evaluate_cabinet,
    "design_door":                   _tool_design_door,
    "design_drawer":                 _tool_design_drawer,
    "generate_cutlist":              _tool_generate_cutlist,
    "compare_joinery":               _tool_compare_joinery,
    "visualize_cabinet":             _tool_visualize_cabinet,
    "list_presets":                  _tool_list_presets,
    "apply_preset":                  _tool_apply_preset,
    "identify_furniture_type":       _tool_identify_furniture_type,
    "auto_fix_cabinet":              _tool_auto_fix_cabinet,
    "describe_design":               _tool_describe_design,
    "design_legs":                   _tool_design_legs,
    "design_pulls":                  _tool_design_pulls,
    "suggest_proportions":           _tool_suggest_proportions,
    "list_pull_presets":             _tool_list_pull_presets,
    "design_project":                _tool_design_project,
    "update_project":                _tool_update_project,
    "list_projects":                 _tool_list_projects,
    "rename_project":                _tool_rename_project,
    "duplicate_project":             _tool_duplicate_project,
    "delete_project":                _tool_delete_project,
    "load_project":                  _tool_load_project,
    "evaluate_project":              _tool_evaluate_project,
    "generate_project_cutlist":      _tool_generate_project_cutlist,
    "generate_assembly_instructions": _tool_generate_assembly_instructions,
    "visualize_project":             _tool_visualize_project,
}


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        prog="cabineteer",
        description="Cabinet-design MCP server (stdio by default, HTTP/SSE with --http).",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Use HTTP/SSE transport instead of stdio.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        metavar="PORT",
        help=f"Starting port for HTTP mode (auto-increments if in use). Default: {DEFAULT_PORT}.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        metavar="HOST",
        help="Bind address for HTTP mode. Default: 127.0.0.1.",
    )
    parser.add_argument(
        "--max-port-attempts",
        type=int,
        default=20,
        metavar="N",
        dest="max_port_attempts",
        help="Number of consecutive ports to try before giving up. Default: 20.",
    )
    parser.add_argument(
        "--port-file",
        type=Path,
        default=PORT_FILE,
        metavar="PATH",
        dest="port_file",
        help=f"Where to write the resolved port in HTTP mode. Default: {PORT_FILE}.",
    )

    args = parser.parse_args()

    if args.http:
        port = find_free_port(args.port, args.max_port_attempts)
        write_port_file(port, args.port_file)
        try:
            asyncio.run(_run_http(args.host, port))
        finally:
            clear_port_file(args.port_file)
    else:
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
