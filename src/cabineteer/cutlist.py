"""
BOM extraction and cutlist optimiser.

Extracts a bill of materials from cabinet assemblies and formats it for
cutlist optimisation. Supports output to:
- JSON (panel list for external tools or further processing)
- CSV (for manual reference / spreadsheet import)
- Console table

Also produces hardware BOMs (pulls, hinges, slides, legs) as
``HardwareLine`` records with pack-quantity procurement math.

In-process sheet optimisation is available via :func:`optimize_cutlist` when
``rectpack`` is installed (``uv pip install -e '.[cutlist]'``).  The
optimiser uses a **guillotine algorithm** (GuillotineBssfSas) which models
real table-saw and track-saw cuts: every cut runs straight across the full
remaining width or height of the sheet, so the resulting layout is always
physically executable at the saw.

All dimensions in millimeters.
"""

import csv
import json
import io
import math
import zlib
from dataclasses import dataclass, field, asdict
from typing import Optional

try:
    import rectpack as _rectpack
    _RECTPACK_AVAILABLE = True
except ImportError:
    _rectpack = None  # type: ignore[assignment]
    _RECTPACK_AVAILABLE = False

try:
    from opcut import common as _opcut_common, csp as _opcut_csp
    _OPCUT_AVAILABLE = True
except ImportError:
    _opcut_common = None  # type: ignore[assignment]
    _opcut_csp = None     # type: ignore[assignment]
    _OPCUT_AVAILABLE = False

try:
    from reportlab.lib.pagesizes import (
        A4, letter as _rl_letter, landscape as _rl_landscape,
    )
    from reportlab.lib.units import mm as _rl_mm
    from reportlab.lib.colors import HexColor as _HexColor
    from reportlab.platypus import (
        SimpleDocTemplate as _SimpleDocTemplate,
        Table as _Table,
        TableStyle as _TableStyle,
        Paragraph as _Paragraph,
        Spacer as _Spacer,
        PageBreak as _PageBreak,
        KeepTogether as _KeepTogether,
    )
    from reportlab.platypus.flowables import Flowable as _Flowable
    from reportlab.lib.styles import getSampleStyleSheet as _getSampleStyleSheet, ParagraphStyle as _ParagraphStyle
    _REPORTLAB_AVAILABLE = True
except ImportError:
    _REPORTLAB_AVAILABLE = False

from .cabinet import PartInfo, stack_from_column, to_opening


# ── Shared colour helpers (used by both HTML and PDF renderers) ───────────────

_PALETTE = [
    "#C8DFA8", "#A8C8DF", "#DFC8A8", "#A8DFC8",
    "#DFA8C8", "#C8A8DF", "#DFD8A8", "#A8D8DF",
    "#DFA8A8", "#A8A8DF", "#D8DFA8", "#A8DFD8",
    "#DFC0A8", "#B8A8DF", "#A8DFB8", "#DFA8D8",
]


# Per-project colours for multi-project batch layouts — Okabe–Ito palette
# (colour-blind safe, mutually distinguishable). Assigned by order of first
# appearance in the batch, so colours are stable for a given project order.
# Pastel tints (base + 60% toward white) of the Okabe–Ito hues: the full-
# saturation originals were too dark for the black panel labels once printed
# (Charlie, Jul 2026 — "the green" #009E73 especially). Hue identity and
# ordering preserved; the red dashed cut lines are a separate fixed colour
# (#c0392b) and are deliberately NOT pastel.
_PROJECT_PALETTE = (
    "#F5D999",  # orange       (base #E69F00)
    "#BBE1F6",  # sky blue     (base #56B4E9)
    "#99D8C7",  # bluish green (base #009E73)
    "#F9F4B3",  # yellow       (base #F0E442)
    "#99C7E0",  # blue         (base #0072B2)
    "#EEBF99",  # vermillion   (base #D55E00)
    "#EBC9DC",  # reddish purple (base #CC79A7)
    "#D6D6D6",  # grey         (base #999999)
)


def _paper_size(paper: str):
    """Resolve a paper name to a reportlab page size (portrait tuple).

    US Letter is the default for every generated PDF — Charlie prints on
    letter, not A4 (2026-08-02); pass paper="a4" for A4. Only call from
    PDF paths that have already checked _REPORTLAB_AVAILABLE.
    """
    sizes = {"letter": _rl_letter, "a4": A4}
    try:
        return sizes[paper.lower()]
    except KeyError:
        raise ValueError(
            f"Unknown paper size {paper!r} — use 'letter' or 'a4'")


def _inch_frac(mm: float) -> str:
    """mm as fractional inches to the nearest 1/32, reduced (e.g. '12 3/4').

    Charlie's shop annotation (Jul 2026): cut sheets carry metric AND
    fractional imperial. Whole inches drop the fraction ('48'); values
    under 1 in drop the whole part ('23/32').
    """
    from math import gcd
    n32 = round(mm / 25.4 * 32)
    whole, num = divmod(n32, 32)
    if num == 0:
        return str(whole)
    g = gcd(num, 32)
    frac = f"{num // g}/{32 // g}"
    return f"{whole} {frac}" if whole else frac


#: Nominal imperial names for common sheet thicknesses (trade names, not
#: exact conversions — 18 mm ply is "3/4 inch" at the yard).
_NOMINAL_SHEET_INCHES = {18: '3/4"', 15: '5/8"', 12: '1/2"', 9: '3/8"', 6: '1/4"'}


def _thickness_imperial(t: float) -> str:
    """Nominal imperial label for common sheet thicknesses, else exact frac."""
    frac = _NOMINAL_SHEET_INCHES.get(int(round(t)))
    return frac if frac else _inch_frac(t) + '\"'


#: Panel-name prefix → identifier code. Order matters: longest/most
#: specific first ("drawer_box_side" must map to DB, not S).
_PART_ID_CODES = (
    ("drawer_box", "DB"),
    ("false_front", "FF"),
    ("door", "DR"),
    ("column_divider", "CD"),
    ("shelf", "SH"),
    ("worktop", "WT"),
    ("back", "BK"),
    # Before the carcass "top" family — startswith would otherwise fold the
    # furniture-top cap strip into the top panel's T-sequence.
    ("top_front_cap", "TC"),
    ("side", "S"),
    ("bottom", "B"),
    ("top", "T"),
)


def _part_code(name: str) -> str:
    for prefix, code in _PART_ID_CODES:
        if name.startswith(prefix):
            return code
    return "P"


def assign_part_ids(panels: list["CutlistPanel"]) -> dict[str, str]:
    """Assign matchable row IDs like 'A-DB1' in place; return {source: letter}.

    Charlie's convention (Jul 2026): letter = project (A, B, … by first
    appearance), code = part family, number = row sequence within that
    project+family. Single-project rows (no source) drop the letter.
    The same panel objects flow into CSV, tables, and the layout groups,
    so every output shows the same ID.
    """
    letters: dict[str, str] = {}
    for pl in panels:
        if pl.source and pl.source not in letters:
            letters[pl.source] = chr(ord("A") + len(letters) % 26)
    counters: dict[tuple[str, str], int] = {}
    for pl in panels:
        letter = letters.get(pl.source, "")
        code = _part_code(pl.name)
        counters[(letter, code)] = counters.get((letter, code), 0) + 1
        seq = counters[(letter, code)]
        pl.part_id = (f"{letter}-" if letter else "") + f"{code}{seq}"
    return letters


def _source_letters_from_groups(groups) -> dict:
    """{source: letter} recovered from the assigned part IDs in the groups."""
    letters: dict[str, str] = {}
    for _, panels, _opt in groups:
        for pl in panels:
            if pl.source and pl.source not in letters and "-" in pl.part_id:
                letters[pl.source] = pl.part_id.split("-", 1)[0]
    return letters


def _group_id_map(panels: list["CutlistPanel"]) -> dict:
    """(source, name, sorted-dims) → part_id, for placement lookup within a
    single layout group (uniform thickness/material, so the key is unique)."""
    out = {}
    for pl in panels:
        key = (pl.source, pl.name,
               round(min(pl.length, pl.width), 1),
               round(max(pl.length, pl.width), 1))
        out[key] = pl.part_id
    return out


def _placement_id(id_map: dict, pl: "Placement") -> str:
    key = (pl.source, pl.panel_name,
           round(min(pl.placed_length, pl.placed_width), 1),
           round(max(pl.placed_length, pl.placed_width), 1))
    return id_map.get(key, "")


def _source_colour(source: str, source_order: list[str]) -> str:
    """Fill colour for a project in a batch layout (stable by batch order)."""
    try:
        return _PROJECT_PALETTE[source_order.index(source) % len(_PROJECT_PALETTE)]
    except ValueError:
        return _PROJECT_PALETTE[-1]


def _panel_colour(name: str) -> str:
    """Return a fill colour hex string for a panel name.

    crc32, not ``hash()`` — Python string hashing is salted per process, so
    colors would change on every regeneration and HTML/PDF from different
    runs would disagree.
    """
    return _PALETTE[zlib.crc32(name.encode("utf-8")) % len(_PALETTE)]


def _panel_colour_dark(hex_col: str, factor: float = 0.65) -> str:
    """Darken a hex colour for panel stroke / text."""
    h = hex_col.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return "#{:02x}{:02x}{:02x}".format(int(r * factor), int(g * factor), int(b * factor))


@dataclass
class CutlistPanel:
    """A single panel to be cut from sheet stock."""
    name: str
    length: float  # along grain
    width: float  # across grain
    thickness: float
    quantity: int = 1
    grain_direction: str = "length"
    material: str = "baltic_birch"
    edge_band: list[str] = field(default_factory=list)
    notes: str = ""
    # Which project this panel belongs to in a multi-project batch cutlist.
    # Empty for single-project runs. Part of the consolidation key, so
    # identical panels from different projects stay distinct, labeled rows;
    # sheet optimization still pools everything, so no material is wasted.
    source: str = ""
    # Row identifier for matching list rows to cut-sheet graphics, e.g.
    # "A-DB1" (project A, drawer-box row 1). Assigned by assign_part_ids()
    # after consolidation; single-project runs drop the letter ("DB1").
    part_id: str = ""


@dataclass
class SheetStock:
    """Available sheet stock for optimization."""
    name: str
    length: float
    width: float
    thickness: float
    quantity: int = 1  # number of sheets available
    material: str = "baltic_birch"
    cost: float = 0.0  # cost per sheet


# ─── Standard Sheet Sizes ─────────────────────────────────────────────────────

SHEET_4x8_3_4 = SheetStock(
    name="4x8 3/4 Baltic Birch",
    length=2440,
    width=1220,
    thickness=18,
    material="baltic_birch",
)

SHEET_4x8_1_2 = SheetStock(
    name="4x8 1/2 Baltic Birch",
    length=2440,
    width=1220,
    thickness=12,
    material="baltic_birch",
)

SHEET_4x8_1_4 = SheetStock(
    name="4x8 1/4 Baltic Birch",
    length=2440,
    width=1220,
    thickness=6,
    material="baltic_birch",
)

SHEET_5x5_3_4 = SheetStock(
    name="5x5 3/4 Baltic Birch",
    length=1525,
    width=1525,
    thickness=18,
    material="baltic_birch",
)


# ─── BOM Extraction ──────────────────────────────────────────────────────────


def extract_bom(parts: list[PartInfo]) -> list[CutlistPanel]:
    """Extract a cutlist-ready BOM from PartInfo list.

    Determines panel length/width from the CadQuery shape bounding box,
    orienting based on grain_direction metadata.
    """
    panels = []

    for part in parts:
        try:
            bb = part.shape.val().BoundingBox()
        except AttributeError:
            try:
                bb = part.shape.BoundingBox()
            except Exception:
                # Fallback: skip if we can't get dimensions
                continue

        # Get the three dimensions, sorted largest to smallest
        dims = sorted([bb.xlen, bb.ylen, bb.zlen], reverse=True)

        # For sheet goods, the two largest dimensions are length and width,
        # the smallest is thickness (should match material_thickness)
        if part.grain_direction == "length":
            panel_length = dims[0]
            panel_width = dims[1]
        else:  # "width" — grain runs along the shorter dimension
            panel_length = dims[1]
            panel_width = dims[0]

        panels.append(CutlistPanel(
            name=part.name,
            length=round(panel_length, 1),
            width=round(panel_width, 1),
            thickness=round(part.material_thickness, 1),
            grain_direction=part.grain_direction,
            edge_band=part.edge_band,
            notes=part.notes,
        ))

    return panels


def extract_bom_parametric(parts: list[PartInfo]) -> list[CutlistPanel]:
    """Extract BOM from PartInfo without requiring CadQuery geometry.

    Attempts full bounding-box extraction first (requires CadQuery shapes).
    Falls back to zero-dimension placeholder panels if geometry is unavailable
    (e.g. CadQuery not installed, or shapes are None), so the caller always
    receives exactly one CutlistPanel per input PartInfo.
    """
    try:
        result = extract_bom(parts)
        # extract_bom silently skips parts whose shapes are unavailable rather
        # than raising, so check that we got a complete result before returning.
        if len(result) == len(parts):
            return result
    except Exception:
        pass

    # Geometry not available for all parts — return zero-dimension fallback panels.
    return [
        CutlistPanel(
            name=part.name,
            length=0,
            width=0,
            thickness=part.material_thickness,
            grain_direction=part.grain_direction,
            edge_band=part.edge_band,
            notes=(part.notes + " " if part.notes else "") +
                  "[dimensions not computed — CadQuery not available]",
        )
        for part in parts
    ]


def consolidate_bom(panels: list[CutlistPanel]) -> list[CutlistPanel]:
    """Merge identical panels into single entries with quantity > 1."""
    consolidated: dict[tuple, CutlistPanel] = {}

    for panel in panels:
        key = (
            panel.name,
            round(panel.length, 1),
            round(panel.width, 1),
            round(panel.thickness, 1),
            panel.grain_direction,
            panel.material,
            tuple(panel.edge_band),
            panel.source,
        )
        if key in consolidated:
            consolidated[key].quantity += panel.quantity
            # Merge any distinct notes from the incoming panel. The panel
            # name is part of the consolidation key, so merged panels always
            # share a name — appending it added no information (and produced a
            # leading ", ") so it is intentionally dropped.
            existing = consolidated[key]
            if panel.notes and panel.notes not in existing.notes:
                # Merge CLAUSE by clause, not whole string by whole string.
                # Appending the entire incoming note repeated every clause
                # the two already shared — a merged pair of mirrored bay
                # faces printed "species TBD" twice and, worse, carried both
                # "18 mm left / 8 mm right" and "8 mm left / 18 mm right" for
                # one panel. Only what is genuinely new is added.
                have = [c.strip() for c in existing.notes.split(";")]
                add = [c.strip() for c in panel.notes.split(";")
                       if c.strip() and c.strip() not in have]
                if add:
                    existing.notes = "; ".join(
                        [existing.notes] + add) if existing.notes \
                        else "; ".join(add)
        else:
            # Preserve original notes; track part names separately in a leading tag
            new_panel = CutlistPanel(
                name=panel.name,
                length=panel.length,
                width=panel.width,
                thickness=panel.thickness,
                quantity=panel.quantity,
                grain_direction=panel.grain_direction,
                material=panel.material,
                edge_band=list(panel.edge_band),
                notes=panel.notes,
                source=panel.source,
            )
            consolidated[key] = new_panel

    return list(consolidated.values())


# ─── Output Formats ──────────────────────────────────────────────────────────


def to_json(
    panels: list[CutlistPanel],
    stock: list[SheetStock] | None = None,
    kerf: float = 3.2,  # table saw blade kerf
) -> str:
    """Export cutlist as JSON.

    The output structure mirrors the cut-optimizer-2d crate's input schema
    (panels + optional stock array with cut_width) and is suitable as a
    record of the panel list or for import into external tools.

    In-process optimisation is handled by :func:`optimize_cutlist` — this
    function is purely a serialisation step.
    """
    output = {
        "cut_width": kerf,
        "panels": [
            {
                "name": p.name,
                **({"project": p.source} if p.source else {}),
                "length": p.length,
                "width": p.width,
                "quantity": p.quantity,
                "can_rotate": p.grain_direction in ("", None),  # matches optimizers
            }
            for p in panels
        ],
    }

    if stock:
        output["stock"] = [
            {
                "name": s.name,
                "length": s.length,
                "width": s.width,
                "quantity": s.quantity,
            }
            for s in stock
        ]

    return json.dumps(output, indent=2)


def to_csv(panels: list[CutlistPanel]) -> str:
    """Export cutlist as CSV.

    Multi-project batches (any panel carrying a ``source``) get a leading
    Project column; single-project output keeps the historical header.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    with_project = any(p.source for p in panels)
    header = [
        "ID", "Name", "Length (mm)", "Width (mm)", "Thickness (mm)",
        "Quantity", "Grain", "Material", "Edge Band", "Notes",
    ]
    if with_project:
        header = ["Project"] + header
    writer.writerow(header)
    for p in panels:
        # Format the way the PDF and the HTML do. Raw floats put "800.0"
        # beside "457.0" beside "18" in the same row — the CSV was the only
        # one of the three shop documents that rendered a dimension by
        # repr(), so whether a number showed a decimal depended on whether
        # some upstream arithmetic had happened to it.
        row = [
            p.part_id, p.name, f"{p.length:g}", f"{p.width:g}",
            f"{p.thickness:g}",
            p.quantity, p.grain_direction, p.material,
            ", ".join(p.edge_band), p.notes,
        ]
        if with_project:
            row = [p.source] + row
        writer.writerow(row)
    return output.getvalue()


def print_bom(panels: list[CutlistPanel]) -> None:
    """Print a formatted BOM table to console."""
    print()
    print(f"{'Name':<25} {'L (mm)':>8} {'W (mm)':>8} {'T (mm)':>8} {'Qty':>4} {'Grain':<8} {'Edge Band':<12}")
    print("-" * 85)
    for p in panels:
        eb = ", ".join(p.edge_band) if p.edge_band else "—"
        print(f"{p.name:<25} {p.length:>8.1f} {p.width:>8.1f} {p.thickness:>8.1f} {p.quantity:>4} {p.grain_direction:<8} {eb:<12}")
    print()

    # Summary by thickness
    thickness_groups: dict[float, list[CutlistPanel]] = {}
    for p in panels:
        thickness_groups.setdefault(p.thickness, []).append(p)

    print("Sheet stock needed:")
    for t, group in sorted(thickness_groups.items()):
        total_area = sum(p.length * p.width * p.quantity for p in group)
        sheet_area = 2440 * 1220  # 4x8 sheet
        sheets_needed = total_area / sheet_area
        print(f"  {t:.0f}mm: {len(group)} parts, ~{total_area/1e6:.2f}m² total, ~{sheets_needed:.1f} sheets (4×8)")
    print()


# ─── Sheet-goods optimisation ────────────────────────────────────────────────


@dataclass
class Placement:
    """Position of one panel piece on a specific sheet."""
    panel_name: str
    sheet_index: int    # 0-based sheet number
    x: float            # mm from bottom-left corner of sheet
    y: float
    placed_length: float  # dimension along x-axis as placed (may differ from
    placed_width: float   #   nominal if rotated — see ``rotated`` flag)
    rotated: bool         # True when piece was rotated 90° from nominal orientation
    cut_sequence: int = 0  # 1-based cut order within the sheet (0 = unset)
    source: str = ""       # originating project in a multi-project batch
    #: Cutlist row ID stamped at expansion time. Authoritative for labels —
    #: the dims-based _placement_id fallback collides when rows differ only
    #: by edge_band/grain/thickness (review 2026-07-29 M5).
    part_id: str = ""


@dataclass
class OptimizationResult:
    """Sheet-goods bin-packing result produced by :func:`optimize_cutlist`.

    Attributes
    ----------
    sheets_used:
        Number of stock sheets that contain at least one placed piece.
    waste_pct:
        Percentage of consumed sheet area that is unused (off-cuts + gaps).
        Computed as ``(sheet_area - panel_area) / sheet_area * 100``.
    placements:
        One entry per placed piece (a panel with quantity=3 produces 3 entries).
    unplaced:
        Panel *names* whose pieces could not be placed — either because they
        are larger than the stock sheet, or because the packer ran out of bins.
        Empty list means everything fits.
    stock_sheet:
        The :class:`SheetStock` used for this optimisation run.
    algorithm_used:
        The optimiser that actually produced this result — one of
        ``"opcut"``, ``"rectpack"``, ``"strip"``, or ``""`` (empty panel
        list).  This reflects the real path taken, including fallbacks (e.g.
        ``algorithm="opcut"`` falling back to ``"strip"`` when opcut fails).
    """
    sheets_used: int
    waste_pct: float
    placements: list[Placement]
    unplaced: list[str]
    stock_sheet: SheetStock
    grain_mismatched: list[str] = field(default_factory=list)
    algorithm_used: str = ""
    #: Optimizer-declared cut plan per sheet index (same tuple format as
    #: _guillotine_cuts). When present, renderers use it INSTEAD of deriving
    #: cuts from geometry — rips_first fills it so bundled strips read as one
    #: wide track-saw rip + table-saw splits, which pure geometry cannot
    #: distinguish from many thin rips when stacks align.
    cuts: "dict[int, list] | None" = None

    @property
    def is_complete(self) -> bool:
        """True when every requested piece was successfully placed."""
        return len(self.unplaced) == 0


def optimize_cutlist(
    panels: list[CutlistPanel],
    stock_sheet: SheetStock | None = None,
    kerf: float = 3.2,
    algorithm: str = "auto",
) -> OptimizationResult:
    """Lay out *panels* onto sheets and return placement results.

    Parameters
    ----------
    panels:
        Consolidated (or raw) list of :class:`CutlistPanel` objects.  Panels
        with ``quantity > 1`` are expanded internally.
    stock_sheet:
        Sheet to lay out onto.  Defaults to :data:`SHEET_4x8_3_4`.
    kerf:
        Saw-blade kerf in mm.
    algorithm:
        Which optimizer to use.  One of:

        ``"auto"`` (default)
            Use opcut if installed, then rectpack if installed, then strip.
        ``"opcut"``
            opcut FORWARD_GREEDY guillotine (requires ``opcut``).
        ``"rectpack"``
            rectpack GuillotineBssfSas (requires ``rectpack``).
        ``"strip"``
            Pure-Python strip-cutting fallback (always available).

    Raises
    ------
    ValueError
        If *algorithm* is not one of the recognised names.
    ImportError
        If an explicitly requested optimiser is not installed.
    """
    if algorithm not in ("auto", "opcut", "rectpack", "strip", "rips_first"):
        raise ValueError(
            f"Unknown algorithm {algorithm!r}; "
            "expected one of 'auto', 'opcut', 'rectpack', 'strip', "
            "'rips_first'."
        )

    if stock_sheet is None:
        stock_sheet = SHEET_4x8_3_4

    # A panel of the wrong thickness on this sheet is always an upstream
    # grouping bug (the server pools by material+thickness before calling) —
    # packing it silently would produce a cutting plan for the wrong stock.
    mismatched = [
        p.name for p in panels
        if abs(p.thickness - stock_sheet.thickness) > 0.01
    ]
    if mismatched:
        raise ValueError(
            f"Panel thickness mismatch for {stock_sheet.thickness:.0f} mm "
            f"stock sheet {stock_sheet.name!r}: {sorted(set(mismatched))}. "
            f"Group panels by thickness before optimizing."
        )

    if not panels:
        return OptimizationResult(
            sheets_used=0, waste_pct=0.0, placements=[],
            unplaced=[], stock_sheet=stock_sheet, grain_mismatched=[],
            algorithm_used="",
        )

    result = _dispatch_optimizer(panels, stock_sheet, kerf, algorithm)
    # Post-condition, not a test: a layout that puts two parts in the same
    # place, or a part off the sheet, is drawn on the SVG and cut from real
    # plywood. The 2026-08 review found this surface has no closure coverage
    # at all — a one-character kerf-sign flip produced 12 overlapping pairs
    # with the whole suite green. Checking here covers optimizers not yet
    # written, which a test over today's four cannot.
    _assert_placements_valid(result)
    return result


def _dispatch_optimizer(panels, stock_sheet, kerf, algorithm):
    """Pick the packer. Split out so every path shares one post-condition."""
    if algorithm == "rips_first":
        # Opt-in shop-sequence layout (rips first, then cross-cuts);
        # deliberately NOT part of "auto" while Charlie evaluates it.
        return _optimize_rips_first(panels, stock_sheet, kerf)

    if algorithm == "opcut":
        if not _OPCUT_AVAILABLE:
            raise ImportError("opcut is not installed. Install with: uv pip install opcut")
        result = _optimize_with_opcut(panels, stock_sheet, kerf)
        return result if result is not None else _optimize_strip(panels, stock_sheet, kerf)

    if algorithm == "rectpack":
        if not _RECTPACK_AVAILABLE:
            raise ImportError(
                "rectpack is not installed. Install with: uv pip install -e '.[cutlist]'"
            )
        return _optimize_with_rectpack(panels, stock_sheet, kerf)

    if algorithm == "strip":
        return _optimize_strip(panels, stock_sheet, kerf)

    # "auto": opcut → rectpack → strip
    if _OPCUT_AVAILABLE:
        result = _optimize_with_opcut(panels, stock_sheet, kerf)
        if result is not None:
            return result
    if _RECTPACK_AVAILABLE:
        return _optimize_with_rectpack(panels, stock_sheet, kerf)
    return _optimize_strip(panels, stock_sheet, kerf)


#: Placement geometry tolerance, mm. Sheet dimensions and kerf arithmetic
#: accumulate float error across a packing run; a real overlap is at least a
#: kerf wide, three orders of magnitude above this.
PLACEMENT_TOL_MM = 0.01


def _assert_placements_valid(result: "OptimizationResult") -> None:
    """Every piece on the sheet, and no two pieces in the same place.

    Raises rather than warns: a bad layout is not a degraded layout, it is
    a drawing that will be cut. Cost measured at 2.45 ms for 240 placements,
    so this runs on every optimisation rather than under a flag.
    """
    sheet = result.stock_sheet
    if sheet is None:
        return
    by_sheet: dict[int, list] = {}
    for pl in result.placements:
        if (pl.x < -PLACEMENT_TOL_MM or pl.y < -PLACEMENT_TOL_MM
                or pl.x + pl.placed_length > sheet.length + PLACEMENT_TOL_MM
                or pl.y + pl.placed_width > sheet.width + PLACEMENT_TOL_MM):
            raise ValueError(
                f"{result.algorithm_used or 'optimizer'} placed "
                f"{pl.panel_name!r} ({pl.placed_length:g} x {pl.placed_width:g}) "
                f"at ({pl.x:g}, {pl.y:g}) — off a "
                f"{sheet.length:g} x {sheet.width:g} sheet.")
        by_sheet.setdefault(pl.sheet_index, []).append(pl)

    for idx, pieces in by_sheet.items():
        for i in range(len(pieces)):
            a = pieces[i]
            for j in range(i + 1, len(pieces)):
                b = pieces[j]
                if (a.x + a.placed_length <= b.x + PLACEMENT_TOL_MM
                        or b.x + b.placed_length <= a.x + PLACEMENT_TOL_MM
                        or a.y + a.placed_width <= b.y + PLACEMENT_TOL_MM
                        or b.y + b.placed_width <= a.y + PLACEMENT_TOL_MM):
                    continue
                ox = min(a.x + a.placed_length, b.x + b.placed_length) - max(a.x, b.x)
                oy = min(a.y + a.placed_width, b.y + b.placed_width) - max(a.y, b.y)
                raise ValueError(
                    f"{result.algorithm_used or 'optimizer'} overlapped "
                    f"{a.panel_name!r} at ({a.x:g}, {a.y:g}) and "
                    f"{b.panel_name!r} at ({b.x:g}, {b.y:g}) on sheet {idx} "
                    f"by {ox:g} x {oy:g} mm.")


def _optimize_with_rectpack(
    panels: list[CutlistPanel],
    stock_sheet: SheetStock,
    kerf: float,
) -> OptimizationResult:
    """Guillotine layout via rectpack GuillotineBssfSas.

    Each piece is packed at its net dimensions plus one kerf on each axis
    (the inter-piece / trailing saw cut), into a bin one kerf smaller than
    the nominal sheet on each axis (the leading edge trim) — matching the
    single-edge-trim convention used by :func:`_optimize_with_opcut`.

    Grain-free panels (``grain_direction`` empty/None) may rotate; grain-
    constrained panels keep their nominal orientation.  rectpack's rotation
    flag is global, so grain is respected by keeping ``rotation=False`` and
    *pre-rotating* only those grain-free pieces that fit rotated but not
    nominally (the same approach as :func:`_optimize_strip`).  Each expanded
    piece carries a globally-unique index so same-named panels never share a
    packer id.
    """
    eff_l = stock_sheet.length - kerf
    eff_w = stock_sheet.width - kerf
    EPS = 0.05

    grain_constrained: set[str] = {
        p.name for p in panels if p.grain_direction not in ("", None)
    }

    # Expand with a globally-unique piece index so two distinct CutlistPanel
    # objects that share a name (e.g. "side" across cabinets) never collide.
    # ``dims`` are the orientation the piece is added to the packer in, and
    # ``pre_rotated`` records whether that differs from the nominal L×W.
    oversized: list[str] = []
    # (add_len, add_wid, name, uid, pre_rotated)
    packable: list[tuple[float, float, str, int, bool]] = []
    piece_dims: dict[int, tuple[float, float]] = {}  # net add_len × add_wid
    piece_name: dict[int, str] = {}
    piece_source: dict[int, str] = {}
    piece_part_id: dict[int, str] = {}

    counter = 0
    for p in panels:
        can_rotate = p.name not in grain_constrained
        for _ in range(p.quantity):
            uid = counter
            counter += 1
            add_l, add_w, pre_rot = p.length, p.width, False
            fits = add_l <= eff_l + EPS and add_w <= eff_w + EPS
            if not fits and can_rotate and p.width <= eff_l + EPS and p.length <= eff_w + EPS:
                add_l, add_w, pre_rot = p.width, p.length, True
                fits = True
            if not fits:
                if p.name not in oversized:
                    oversized.append(p.name)
                continue
            packable.append((add_l, add_w, p.name, uid, pre_rot))
            piece_dims[uid] = (add_l, add_w)
            piece_name[uid] = p.name
            piece_source[uid] = p.source
            piece_part_id[uid] = p.part_id

    packable.sort(key=lambda e: e[0] * e[1], reverse=True)

    # rotation=False so the packer never rotates a piece — grain-constrained
    # pieces are always placed in their nominal orientation, and grain-free
    # pieces that needed rotating were already pre-rotated above.
    packer = _rectpack.newPacker(
        pack_algo=_rectpack.GuillotineBssfSas,
        rotation=False,
    )
    # Each piece is padded by one kerf on each axis; the bin is the *nominal*
    # sheet, so the trailing kerf of the last piece in a row/column falls into
    # the one-kerf margin rather than off the sheet. Net-fit boundary is then
    # exactly ``sheet − kerf`` on each axis, matching opcut / strip.
    packer.add_bin(stock_sheet.length, stock_sheet.width, count=max(1, len(packable)))
    for add_l, add_w, name, uid, pre_rot in packable:
        packer.add_rect(add_l + kerf, add_w + kerf, rid=uid)
    packer.pack()

    pre_rotated: dict[int, bool] = {uid: pr for _, _, _, uid, pr in packable}

    placements: list[Placement] = []
    placed_uids: set[int] = set()

    for bin_idx, abin in enumerate(packer):
        for rect in abin:
            uid: int = rect.rid
            placed_uids.add(uid)
            name = piece_name[uid]
            placements.append(Placement(
                panel_name=name,
                sheet_index=bin_idx,
                x=round(rect.x, 1),
                y=round(rect.y, 1),
                placed_length=round(rect.width - kerf, 1),
                placed_width=round(rect.height - kerf, 1),
                rotated=pre_rotated[uid],
                source=piece_source[uid],
                part_id=piece_part_id[uid],
            ))

    unplaced: list[str] = list(oversized)
    for _, _, name, uid, _ in packable:
        if uid not in placed_uids and name not in unplaced:
            unplaced.append(name)

    # Assign per-sheet cut sequence in placement order.
    sheet_counters: dict[int, int] = {}
    for p in placements:
        sheet_counters[p.sheet_index] = sheet_counters.get(p.sheet_index, 0) + 1
        p.cut_sequence = sheet_counters[p.sheet_index]

    sheets_used = len({p.sheet_index for p in placements})
    if sheets_used == 0:
        waste_pct = 0.0
    else:
        total_area = sheets_used * stock_sheet.length * stock_sheet.width
        placed_area = sum(piece_dims[uid][0] * piece_dims[uid][1] for uid in placed_uids)
        waste_pct = max(0.0, (total_area - placed_area) / total_area * 100)

    return OptimizationResult(
        sheets_used=sheets_used,
        waste_pct=round(waste_pct, 1),
        placements=placements,
        unplaced=unplaced,
        stock_sheet=stock_sheet,
        # rotation=False + pre-rotation of grain-free pieces means the packer
        # never rotates a grain-constrained piece, so there is never a mismatch.
        grain_mismatched=[],
        algorithm_used="rectpack",
    )


def _optimize_with_opcut(
    panels: list[CutlistPanel],
    stock_sheet: SheetStock,
    kerf: float,
) -> OptimizationResult | None:
    """Guillotine layout via opcut FORWARD_GREEDY.

    Returns None if opcut cannot place all valid items even after several
    retries (caller falls back to strip cutting).

    One kerf is subtracted from each sheet dimension so opcut models edge
    waste correctly; inter-piece kerfs are handled by opcut's cut_width.
    """
    eff_l = stock_sheet.length - kerf
    eff_w = stock_sheet.width  - kerf
    EPS = 0.05

    grain_constrained: set[str] = {
        p.name for p in panels if p.grain_direction not in ("", None)
    }

    oversized: list[str] = []
    valid: list[CutlistPanel] = []
    for p in panels:
        can_rotate = p.name not in grain_constrained
        fits = p.length <= eff_l + EPS and p.width <= eff_w + EPS
        fits_rot = can_rotate and p.width <= eff_l + EPS and p.length <= eff_w + EPS
        if not fits and not fits_rot:
            if p.name not in oversized:
                oversized.append(p.name)
        else:
            valid.append(p)

    if not valid:
        return OptimizationResult(
            sheets_used=0, waste_pct=0.0, placements=[],
            unplaced=oversized, stock_sheet=stock_sheet, grain_mismatched=[],
            algorithm_used="opcut",
        )

    items: list = []
    id_to_name: dict[str, str] = {}
    id_to_source: dict[str, str] = {}
    id_to_part_id: dict[str, str] = {}
    counter = 0
    for p in valid:
        for _ in range(p.quantity):
            iid = f"{p.name}__{counter}"
            counter += 1
            items.append(_opcut_common.Item(
                id=iid,
                width=p.length,
                height=p.width,
                can_rotate=p.name not in grain_constrained,
            ))
            id_to_name[iid] = p.name
            id_to_source[iid] = p.source
            id_to_part_id[iid] = p.part_id

    total_area = sum(p.length * p.width * p.quantity for p in valid)
    base = max(1, math.ceil(total_area / (eff_l * eff_w)))

    opcut_panels: list = []
    result = None
    for n in [base, base + 1, base + 2, base + 4]:
        opcut_panels = [
            _opcut_common.Panel(id=f"s{i}", width=eff_l, height=eff_w)
            for i in range(n)
        ]
        params = _opcut_common.Params(
            cut_width=kerf, panels=opcut_panels, items=items,
        )
        try:
            result = _opcut_csp.calculate(params, _opcut_common.Method.FORWARD_GREEDY)
            break
        except _opcut_common.UnresolvableError:
            continue

    if result is None:
        return None

    idx_map = {f"s{i}": i for i in range(len(opcut_panels))}
    placements: list[Placement] = []
    grain_mismatched: list[str] = []

    for used in result.used:
        name = id_to_name[used.item.id]
        if used.rotate:
            placed_l, placed_w = used.item.height, used.item.width
        else:
            placed_l, placed_w = used.item.width, used.item.height
        if used.rotate and name in grain_constrained and name not in grain_mismatched:
            grain_mismatched.append(name)
        placements.append(Placement(
            panel_name=name,
            sheet_index=idx_map[used.panel.id],
            x=round(used.x, 1),
            y=round(used.y, 1),
            placed_length=round(placed_l, 1),
            placed_width=round(placed_w, 1),
            rotated=used.rotate,
            source=id_to_source[used.item.id],
            part_id=id_to_part_id[used.item.id],
        ))

    used_indices = sorted({p.sheet_index for p in placements})
    remap = {old: new for new, old in enumerate(used_indices)}
    for p in placements:
        p.sheet_index = remap[p.sheet_index]

    # Assign per-sheet cut sequence in the order opcut placed each piece.
    sheet_counters: dict[int, int] = {}
    for p in placements:
        sheet_counters[p.sheet_index] = sheet_counters.get(p.sheet_index, 0) + 1
        p.cut_sequence = sheet_counters[p.sheet_index]

    sheets_used = len(used_indices)
    placed_area = sum(p.placed_length * p.placed_width for p in placements)
    total_used = sheets_used * stock_sheet.length * stock_sheet.width
    waste_pct = max(0.0, (total_used - placed_area) / total_used * 100)

    return OptimizationResult(
        sheets_used=sheets_used,
        waste_pct=round(waste_pct, 1),
        placements=placements,
        unplaced=oversized,
        stock_sheet=stock_sheet,
        grain_mismatched=grain_mismatched,
        algorithm_used="opcut",
    )


#: Table-saw fence capacity for the rips_first layout (Charlie: standard
#: fence 20" = 508 mm; 24" possible but avoid). Secondary (stacked) rips are
#: table-saw cuts and must keep within this; level-1 rips and level-2
#: cross-cuts are track-saw breakdown cuts with no fence constraint.
RIPS_FIRST_FENCE_LIMIT_MM = 508.0

#: Minimum comfortable track-saw rip width (Charlie: 76 mm strips are much
#: thinner than he'd run under the track). Pieces narrower than this open a
#: BUNDLED strip — k pieces + kerfs wide, ≥ this minimum — so the track saw
#: makes one wide rip and the table saw splits each cross-cut segment to
#: final width (easy repeated fence cuts).
RIPS_FIRST_MIN_STRIP_MM = 150.0


def _optimize_rips_first(
    panels: list[CutlistPanel],
    stock_sheet: SheetStock,
    kerf: float,
) -> OptimizationResult:
    """Shop-sequence layout for Charlie's track-saw + small-table-saw setup.

    Level 1: full-length RIPS break the sheet into strips (track saw).
    Level 2: cross-cuts chop each strip into segments (track saw / sled).
    Level 3: short secondary rips stack pieces within a segment — these are
    the TABLE-SAW cuts, so the kept width is capped at
    :data:`RIPS_FIRST_FENCE_LIMIT_MM`.

    Pieces place best-fit into strips-of-columns (stack in an existing
    column at the tightest width → new column in an exact-width strip →
    wider strip → new strip); grain-free pieces try both orientations.
    Same net-dimension / single-edge-trim conventions as the other
    optimizers.
    """
    eff_l = stock_sheet.length - kerf
    eff_w = stock_sheet.width - kerf
    EPS = 0.05

    # Each unit lists its allowed (along, across, rotated) orientations —
    # one for grain-constrained pieces, both for grain-free.
    units: list[list[tuple[float, float, bool]]] = []
    unit_meta: list[tuple[str, str]] = []   # (name, source)
    oversized: list[str] = []
    for p in panels:
        for _ in range(p.quantity):
            if p.grain_direction not in ("", None):
                cands = [(p.length, p.width, False)]
            else:
                cands = [(p.length, p.width, False),
                         (p.width, p.length, True)]
            cands = [c for c in cands
                     if c[0] <= eff_l + EPS and c[1] <= eff_w + EPS]
            if not cands:
                if p.name not in oversized:
                    oversized.append(p.name)
                continue
            units.append(cands)
            unit_meta.append((p.name, p.source, p.part_id))

    # 2) Best-fit units into strips of COLUMNS (shop 3-stage cutting):
    #    a strip is one full-length rip of width W (track saw); each column
    #    is one cross-cut segment (track saw); pieces stack within a column
    #    via short secondary rips (TABLE SAW — kept width must fit the
    #    fence). Preference order per unit/orientation: stack into an
    #    existing column (tightest width) → new column in an exact-width
    #    strip → new column in a wider strip → open a new strip.
    #    strip = [W, used_len, cols]; col = [seg_len, used_w, [(entry, y_off)]]
    FENCE = RIPS_FIRST_FENCE_LIMIT_MM
    MIN_STRIP = RIPS_FIRST_MIN_STRIP_MM
    strips: list[list] = []
    order = sorted(
        range(len(units)),
        key=lambda i: (-min(c[1] for c in units[i]),
                       -max(c[0] for c in units[i])),
    )
    # Same-width unit counts (preferred orientation) — sizes the bundle when
    # a narrow class opens a new strip, so a lone odd piece doesn't get an
    # empty slot's worth of waste.
    width_counts: dict[float, int] = {}
    for cands in units:
        w = round(min(cands, key=lambda c: c[1])[1], 1)
        width_counts[w] = width_counts.get(w, 0) + 1
    for ui in order:
        name, src, piece_pid = unit_meta[ui]
        best = None   # (kind, width_slack, strip, col, orientation)
        for cand in units[ui]:
            along, across = cand[0], cand[1]
            for st in strips:
                W = st[0]
                if across > W + EPS:
                    continue
                if across <= FENCE + EPS:
                    # Stacking = a fence-referenced table-saw rip.
                    for col in st[2]:
                        if (along <= col[0] + EPS
                                and col[1] + kerf + across <= W + EPS):
                            c = (0, W - across, st, col, cand)
                            if best is None or c[:2] < best[:2]:
                                best = c
                if st[1] + kerf + along <= eff_l + EPS:
                    kind = 1 if W - across <= EPS else 2
                    c = (kind, W - across, st, None, cand)
                    if best is None or c[:2] < best[:2]:
                        best = c
        if best is None:
            # New strip: prefer the orientation with the smaller across
            # (narrow strips keep more of the sheet rippable). Narrow
            # classes open a BUNDLED strip sized for k same-width pieces
            # so the track-saw rip stays comfortably wide.
            along, across, rot = min(units[ui], key=lambda c: c[1])
            W = across
            if across < MIN_STRIP and across <= FENCE + EPS:
                k_need = math.ceil((MIN_STRIP + kerf) / (across + kerf))
                avail = max(1, width_counts.get(round(across, 1), 1))
                k = min(k_need, avail)
                while k > 1 and k * across + (k - 1) * kerf > eff_w + EPS:
                    k -= 1
                W = k * across + (k - 1) * kerf
            strips.append([W, along,
                           [[along, across, [((along, across, name, src,
                                               rot, piece_pid), 0.0)]]]])
            wkey = round(across, 1)
            if wkey in width_counts:
                width_counts[wkey] -= 1
            continue
        _, _, st, col, (along, across, rot) = best
        entry = (along, across, name, src, rot, piece_pid)
        if col is not None:
            col[2].append((entry, col[1] + kerf))
            col[1] += kerf + across
        else:
            st[2].append([along, across, [(entry, 0.0)]])
            st[1] += kerf + along
        wkey = round(across, 1)
        if wkey in width_counts:
            width_counts[wkey] -= 1

    # 3) FFD the strip widths into sheets.
    order = sorted(range(len(strips)), key=lambda si: -strips[si][0])
    sheets: list[list] = []   # [used_width, [strip indices]]
    for si in order:
        w = strips[si][0]
        for sh in sheets:
            add = w + kerf if sh[1] else w
            if sh[0] + add <= eff_w + EPS:
                sh[0] += add
                sh[1].append(si)
                break
        else:
            sheets.append([w, [si]])

    # 4) Emit placements AND the declared cut plan: strips stack in y,
    #    columns run in x, stacked pieces offset in y within their column.
    #    Strip rips are the numbered breakdown (track-saw) cuts; column
    #    cross-cuts and in-column stack rips are non-breakdown (thin lines,
    #    table-saw work).
    sl, sw = stock_sheet.length, stock_sheet.width
    placements: list[Placement] = []
    cuts_by_sheet: dict[int, list] = {}
    for shi, (_, slist) in enumerate(sheets):
        entries = cuts_by_sheet.setdefault(shi, [])
        y = 0.0
        for si in slist:
            W, _, cols = strips[si]
            y_next = y + W
            if sw - y_next > 5.0:
                entries.append((0, round(y_next, 1), 'h',
                                0.0, round(y_next, 1), sl, round(y_next, 1),
                                True, round(W), round(sw - y_next)))
            x = 0.0
            for seg_len, _, members in cols:
                for (along, across, name, src, rot, ppid), y_off in members:
                    placements.append(Placement(
                        panel_name=name, sheet_index=shi,
                        x=round(x, 1), y=round(y + y_off, 1),
                        placed_length=round(along, 1),
                        placed_width=round(across, 1),
                        rotated=rot, source=src, part_id=ppid,
                    ))
                for mi in range(len(members) - 1):
                    (_, ac, *_rest), y_off = members[mi]
                    yy = y + y_off + ac
                    entries.append((2, round(yy, 1), 'h',
                                    round(x, 1), round(yy, 1),
                                    round(x + seg_len, 1), round(yy, 1),
                                    False, round(ac), round(W - y_off - ac)))
                x_next = x + seg_len
                if sl - x_next > 5.0:
                    entries.append((1, round(x_next, 1), 'v',
                                    round(x_next, 1), round(y, 1),
                                    round(x_next, 1), round(y_next, 1),
                                    False, round(seg_len),
                                    round(sl - x_next)))
                x = x_next + kerf
            y = y_next + kerf

    sheet_counters: dict[int, int] = {}
    for pl in placements:
        sheet_counters[pl.sheet_index] = sheet_counters.get(pl.sheet_index, 0) + 1
        pl.cut_sequence = sheet_counters[pl.sheet_index]

    sheets_used = len({pl.sheet_index for pl in placements})
    placed_area = sum(pl.placed_length * pl.placed_width for pl in placements)
    total_area = sheets_used * stock_sheet.length * stock_sheet.width
    waste_pct = (max(0.0, (total_area - placed_area) / total_area * 100)
                 if sheets_used else 0.0)

    return OptimizationResult(
        sheets_used=sheets_used,
        waste_pct=round(waste_pct, 1),
        placements=placements,
        unplaced=oversized,
        stock_sheet=stock_sheet,
        grain_mismatched=[],
        algorithm_used="rips_first",
        cuts=cuts_by_sheet,
    )


def _optimize_strip(
    panels: list[CutlistPanel],
    stock_sheet: SheetStock,
    kerf: float,
) -> OptimizationResult:
    """Strip-cutting fallback layout (pure Python, no extra dependencies).

    Groups panels into horizontal strips by across-grain dimension, sorted
    widest first.  Within each strip, pieces are arranged left-to-right.
    ``placed_length`` / ``placed_width`` are NET dimensions (no kerf added).

    One kerf is trimmed off each sheet dimension (the leading edge cut);
    inter-piece kerfs advance the cursor between pieces, but the last piece
    in a strip / row may extend to the trimmed edge — matching the single-
    edge-trim convention of :func:`_optimize_with_opcut`.
    """
    eff_l = stock_sheet.length - kerf
    eff_w = stock_sheet.width  - kerf
    EPS = 0.05

    grain_constrained: set[str] = {
        p.name for p in panels if p.grain_direction not in ("", None)
    }

    oversized: list[str] = []
    oriented: list[tuple[float, float, str, str, bool]] = []

    for p in panels:
        for _ in range(p.quantity):
            if p.name in grain_constrained:
                plen, pwid, rot = p.length, p.width, False
                if plen > eff_l + EPS or pwid > eff_w + EPS:
                    if p.name not in oversized:
                        oversized.append(p.name)
                else:
                    oriented.append((plen, pwid, p.name, p.source, rot,
                                     p.part_id))
            else:
                if p.length >= p.width:
                    plen, pwid, rot = p.length, p.width, False
                else:
                    plen, pwid, rot = p.width, p.length, True
                if plen > eff_l + EPS or pwid > eff_w + EPS:
                    plen, pwid, rot = pwid, plen, not rot
                    if plen > eff_l + EPS or pwid > eff_w + EPS:
                        if p.name not in oversized:
                            oversized.append(p.name)
                        continue
                oriented.append((plen, pwid, p.name, p.source, rot,
                                 p.part_id))

    oriented.sort(key=lambda e: (-e[1], -e[0]))

    placements: list[Placement] = []
    sheet_index = 0
    y = 0.0
    x = 0.0
    current_h: float | None = None

    for plen, pwid, name, src, rotated, ppid in oriented:
        pk = plen + kerf

        if current_h is None or abs(pwid - current_h) > EPS:
            if current_h is not None:
                y += current_h + kerf
            current_h = pwid
            x = 0.0
            if y + pwid > eff_w + EPS:
                sheet_index += 1
                y = 0.0

        if x + plen > eff_l + EPS:
            y += current_h + kerf
            x = 0.0
            if y + pwid > eff_w + EPS:
                sheet_index += 1
                y = 0.0

        placements.append(Placement(
            panel_name=name,
            sheet_index=sheet_index,
            x=round(x, 1),
            y=round(y, 1),
            placed_length=round(plen, 1),
            placed_width=round(pwid, 1),
            rotated=rotated,
            source=src,
            part_id=ppid,
        ))
        x += pk

    sheet_counters: dict[int, int] = {}
    for p in placements:
        sheet_counters[p.sheet_index] = sheet_counters.get(p.sheet_index, 0) + 1
        p.cut_sequence = sheet_counters[p.sheet_index]

    sheets_used = len({p.sheet_index for p in placements})
    placed_area = sum(p.placed_length * p.placed_width for p in placements)
    total_area = sheets_used * stock_sheet.length * stock_sheet.width
    waste_pct = max(0.0, (total_area - placed_area) / total_area * 100) if sheets_used else 0.0

    return OptimizationResult(
        sheets_used=sheets_used,
        waste_pct=round(waste_pct, 1),
        placements=placements,
        unplaced=oversized,
        stock_sheet=stock_sheet,
        grain_mismatched=[],
        algorithm_used="strip",
    )


# ─── Hardware BOM ────────────────────────────────────────────────────────────
#
# Hardware lines track the procurement side of the bill of materials: how many
# *pieces* of a given SKU are needed, what the pack size is, and therefore how
# many packs to order. They are produced alongside the panel cutlist but do
# not flow through the sheet-goods optimizer.


@dataclass
class HardwareLine:
    """A single hardware SKU with procurement math.

    ``pieces_needed`` is the actual quantity required by the design.
    ``pack_quantity`` is how many pieces ship per SKU pack (e.g. IKEA HACKÅS
    pulls sell in 2-packs, so pack_quantity=2). The derived properties turn
    that into the number of packs to order and the resulting leftover pieces.
    """
    sku: str               # stable key, e.g. "topknobs-hb-128"
    category: str          # "pull" | "hinge" | "slide" | "leg"
    name: str
    brand: str
    model_number: str
    pieces_needed: int
    pack_quantity: int = 1
    notes: str = ""
    # Price override for lines whose cost comes from a user-supplied spec
    # (e.g. edge_band_stock boards) instead of the PRICE_LIST catalog.
    unit_price_usd: Optional[float] = None
    # Which project this line came from in a multi-project batch. Hardware
    # consolidates GLOBALLY (one purchase per SKU) — consolidation merges
    # across sources but accumulates the per-project breakdown here.
    source: str = ""
    source_counts: dict = field(default_factory=dict)  # project → pieces

    @property
    def packs_to_order(self) -> int:
        """Smallest pack count that covers pieces_needed."""
        if self.pieces_needed <= 0:
            return 0
        pq = max(1, int(self.pack_quantity))
        return math.ceil(self.pieces_needed / pq)

    @property
    def pieces_ordered(self) -> int:
        """Total pieces received given packs_to_order × pack_quantity."""
        return self.packs_to_order * max(1, int(self.pack_quantity))

    @property
    def leftover(self) -> int:
        """Pieces remaining after installation (always ≥ 0)."""
        return self.pieces_ordered - self.pieces_needed

    @property
    def unit_price(self) -> float:
        """Line unit price: the spec override when set, else the catalog."""
        if self.unit_price_usd is not None:
            return self.unit_price_usd
        from .hardware import price_for
        return price_for(self.sku)


# ─── Pull BOM extractors ─────────────────────────────────────────────────────
#
# These functions inspect a DrawerConfig / DoorConfig / CabinetConfig and
# return ``HardwareLine`` objects describing the pulls needed.  They rely on
# the per-config ``pull_placements`` machinery added in Phase 3, so placement
# rules (single vs dual, applied_face=False suppression, door-pair doubling)
# stay in one place.


def _pull_line(sku: str, pieces: int, notes: str = "") -> Optional[HardwareLine]:
    """Build a HardwareLine from a pull catalog key.

    Returns ``None`` for zero pieces or unknown keys — unknown keys are the
    responsibility of the evaluator, not the BOM extractor.
    """
    if pieces <= 0 or not sku:
        return None
    # Import here to avoid a hard dependency at module-import time if the
    # catalog somehow failed to load (tests exercise that path via monkey-
    # patching PULLS).
    from .hardware import get_pull
    try:
        spec = get_pull(sku)
    except KeyError:
        return None
    return HardwareLine(
        sku=sku,
        category="pull",
        name=spec.name,
        brand=spec.brand,
        model_number=spec.model_number,
        pieces_needed=pieces,
        pack_quantity=spec.pack_quantity,
        notes=notes,
    )


def pull_line_from_drawer(drawer_cfg) -> Optional[HardwareLine]:
    """Return the HardwareLine for this drawer's pulls, or None.

    Returns None when the drawer has no pull_key, no applied face, or the
    key refers to a pull missing from the catalog.
    """
    if drawer_cfg.pull_key is None:
        return None
    try:
        placements = drawer_cfg.pull_placements
    except KeyError:
        return None
    n = len(placements)
    return _pull_line(drawer_cfg.pull_key, n)


def pull_line_from_door(door_cfg) -> Optional[HardwareLine]:
    """Return the HardwareLine for this door config's pulls, or None.

    Uses ``total_pull_count``, which already accounts for door pairs.
    """
    if door_cfg.pull_key is None:
        return None
    try:
        _ = door_cfg.pull_placements  # force resolve so unknown keys raise
    except KeyError:
        return None
    n = door_cfg.total_pull_count
    return _pull_line(door_cfg.pull_key, n)


def _interior_depth(cab_cfg) -> float:
    """The depth datum: front edge to the back panel's front face.

    A one-line indirection with a purpose. Callers here take duck-typed
    config objects (tests pass stand-ins), so a bare attribute access would
    raise on those while a hand-rolled fallback would reintroduce the second
    derivation this exists to remove. Fall back to the same arithmetic
    ``CabinetConfig`` uses, never to a different one.
    """
    depth = getattr(cab_cfg, "interior_depth", None)
    if depth is not None:
        return float(depth)
    from .cabinet import back_capture_geometry
    return float(cab_cfg.depth - back_capture_geometry(cab_cfg).clear_depth)


def _drawer_opening_depth(cab_cfg) -> float:
    """The space a drawer box and its runner must fit into."""
    depth = getattr(cab_cfg, "drawer_opening_depth", None)
    if depth is not None:
        return float(depth)
    return _interior_depth(cab_cfg)


def pull_lines_for_cabinet_config(
    cab_cfg, columns_raw: list | None = None
) -> list[HardwareLine]:
    """Walk a CabinetConfig's drawer_config and return a consolidated list of
    pull ``HardwareLine`` entries.

    Mirrors ``drawers_from_cabinet_config`` / ``doors_from_cabinet_config``:
    one drawer pull per "drawer" slot, one door pull per "door" slot, two
    door pulls per "door_pair" slot. Multi-column layouts (``cab_cfg.columns``)
    are walked per column.

    ``columns_raw`` (list of dicts with ``width_mm`` / ``drawer_config`` keys)
    takes priority over ``cab_cfg.columns`` when supplied — used by the MCP
    cutlist tool which pops ``columns`` from args before building the config.
    """
    from .drawer import DrawerConfig
    from .door import DoorConfig
    from .cabinet import bays_from_config, face_layout

    lines: list[HardwareLine] = []
    # The depth datum, not a hand-rolled `depth − back_thickness`: that
    # expression ignores back_groove_setback entirely, so on a dado carcass
    # it reported a deeper interior than the back leaves.
    interior_depth = _interior_depth(cab_cfg)

    # Pull counts depend on face width (dual-pull threshold, fit checks) —
    # bill against the REAL face from face_layout, not DrawerConfig's
    # legacy flat overlays, or the BOM disagrees with the placements
    # design_pulls reports and the pulls the render draws.
    _face_rects = {
        (q.bay, q.slot): q
        for q in face_layout(bays_from_config(cab_cfg, columns_raw))
        if q.kind == "drawer_face"
    }

    def _walk_stack(stack, interior_width: float, bay_idx: int = 0) -> None:
        for slot_idx, item in enumerate(stack):
            # Normalize OpeningConfig objects, dicts, and raw [height, type]
            # rows the same way — per-opening overrides survive all shapes.
            op = to_opening(item)
            opening_h, slot_type = op.height_mm, op.opening_type
            pull_key_override = op.pull_key
            hinge_key_override = op.hinge_key

            if slot_type == "drawer":
                fp = _face_rects.get((bay_idx, slot_idx))
                dcfg = DrawerConfig(
                    opening_width=interior_width,
                    opening_height=opening_h,
                    opening_depth=interior_depth,
                    slide_key=op.slide_key or cab_cfg.drawer_slide,
                    pull_key=pull_key_override or cab_cfg.drawer_pull,
                    face_overlay_sides=((fp.width - interior_width) / 2
                                        if fp else 10.0),
                    face_overlay_top=((fp.height - opening_h) / 2
                                      if fp else 3.0),
                    face_overlay_bottom=((fp.height - opening_h) / 2
                                         if fp else 3.0),
                )
                line = pull_line_from_drawer(dcfg)
                if line is not None:
                    lines.append(line)
            elif slot_type in ("door", "door_pair"):
                # Honor a per-opening num_doors override (matches the hinge
                # extractor); fall back to the slot-type default otherwise.
                num_doors = op.num_doors or (2 if slot_type == "door_pair" else 1)
                dcfg = DoorConfig(
                    opening_width=interior_width,
                    opening_height=opening_h,
                    num_doors=num_doors,
                    hinge_key=hinge_key_override or cab_cfg.door_hinge,
                    pull_key=pull_key_override or cab_cfg.door_pull,
                )
                line = pull_line_from_door(dcfg)
                if line is not None:
                    lines.append(line)

    if columns_raw:
        for bay_idx, col in enumerate(columns_raw):
            col_w = float(col["width_mm"])
            stack = stack_from_column(col)
            _walk_stack(stack, col_w, bay_idx)
    elif getattr(cab_cfg, "columns", None):
        for bay_idx, col in enumerate(cab_cfg.columns):
            _walk_stack(col.openings, col.width_mm, bay_idx)
    else:
        _walk_stack(cab_cfg.openings, cab_cfg.interior_width)

    return consolidate_hardware_lines(lines)


# ─── Consolidation + output ──────────────────────────────────────────────────


#: Blum undermount runners are useless without their front locking devices —
#: one LEFT and one RIGHT clip per slide pair, sold as separate SKUs.
#: Tandem family (550H, Tandem Plus 563H/563F): T51.1901 L / R.
#: Movento family (760H, 769): T51.7601 LI / RE.
#: (Charlie's supplier, Jul 2026: $2.25 ea Tandem, $2.50 ea Movento.)
_BLUM_LOCKING_DEVICES: dict[str, tuple[tuple[str, str], ...]] = {
    "blum_tandem": (
        ("blum_t51_1901_l", "T51.1901 L"),
        ("blum_t51_1901_r", "T51.1901 R"),
    ),
    "blum_movento": (
        ("blum_t51_7601_li", "T51.7601 LI"),
        ("blum_t51_7601_re", "T51.7601 RE"),
    ),
}


def slide_lines_for_cabinet_config(cab_cfg, columns_raw: list | None = None) -> list[HardwareLine]:
    """Return HardwareLines for drawer slides required by the cabinet.

    Each drawer needs one slide pair (left + right = 2 pieces). Whether the
    pair is one purchasable unit depends on the model: undermount runners
    (Blum Tandem/Movento, Salice) are sold as pairs, side-mount slides
    (Accuride) as singles — ``DrawerSlideSpec.sold_as_pair`` decides, and
    PRICE_LIST entries use the matching basis (per pair vs per single).
    The SKU is keyed by slide key + length so different-length slides on
    the same model stay separate.

    Blum runners additionally get their front locking devices — one left
    and one right clip per drawer (see ``_BLUM_LOCKING_DEVICES``); both
    Tandem models share the same clip SKUs, so they consolidate across
    mixed-slide projects.
    """
    from .hardware import get_slide
    from .drawer import DrawerConfig

    # A runner's length is chosen from the space it must live in, so this
    # asks drawer_opening_depth by name. Reading `depth − back_thickness`
    # here is what ordered a 457 mm runner into 452 mm of clear depth.
    interior_depth = _drawer_opening_depth(cab_cfg)

    def _slides_from_stack(stack, interior_width: float) -> list[HardwareLine]:
        lines: list[HardwareLine] = []
        for item in stack:
            op = to_opening(item)
            opening_h, slot_type = op.height_mm, op.opening_type
            if slot_type != "drawer":
                continue
            # Each drawer resolves its own slide (op.slide_key overriding
            # cab_cfg.drawer_slide) so a per-opening override is billed
            # under its own SKU; a drawer whose slide is unknown or doesn't
            # fit the depth is skipped alone — evaluation reports the
            # problem, and one bad drawer must not sink the whole BOM.
            slide_key = op.slide_key or cab_cfg.drawer_slide
            try:
                slide_spec = get_slide(slide_key)
                length = slide_spec.slide_length_for_depth(interior_depth)
            except (KeyError, ValueError):
                continue
            pn = slide_spec.part_numbers.get(length, "")
            sku = f"{slide_key}-{length}mm"
            lines.append(HardwareLine(
                sku=sku,
                category="slide",
                name=slide_spec.name,
                brand=slide_spec.manufacturer,
                model_number=pn or slide_key,
                pieces_needed=2,           # one pair (left + right) per drawer
                pack_quantity=2 if slide_spec.sold_as_pair else 1,
                # Parenthesised, not comma-separated: consolidation dedupes
                # notes by splitting on ", ".
                notes=f"{length} mm ({slide_spec.extension} extension)",
            ))
            family = next((f for f in _BLUM_LOCKING_DEVICES
                           if slide_key.startswith(f)), None)
            if family:
                for dev_sku, dev_pn in _BLUM_LOCKING_DEVICES[family]:
                    lines.append(HardwareLine(
                        sku=dev_sku,
                        category="slide_accessory",
                        name=f"Blum Front Locking Device {dev_pn}",
                        brand="Blum",
                        model_number=dev_pn,
                        pieces_needed=1,   # one of each hand per drawer
                        pack_quantity=1,
                        notes=f"for {slide_spec.name}",
                    ))
                # Runner mounting screws: 4 per runner × 2 runners. The
                # locking devices ship WITH their own screws (Charlie's
                # order, Jul 2026), so these cover the runners only.
                lines.append(HardwareLine(
                    sku="blum_606n",
                    category="fastener",
                    name='Blum 606N #6 × 5/8" Flat-Head Mounting Screw',
                    brand="Blum",
                    model_number="606N",
                    pieces_needed=8,
                    pack_quantity=100,  # sold as 100-packs (606N100)
                    notes="runner mounting (4/runner); locking devices "
                          "include their own screws",
                ))
        return lines

    raw: list[HardwareLine] = []
    if columns_raw:
        for col in columns_raw:
            col_w = float(col["width_mm"])
            stack = stack_from_column(col)
            raw.extend(_slides_from_stack(stack, col_w))
    elif getattr(cab_cfg, "columns", None):
        for col in cab_cfg.columns:
            raw.extend(_slides_from_stack(col.openings, col.width_mm))
    else:
        raw.extend(_slides_from_stack(cab_cfg.openings, cab_cfg.interior_width))

    return consolidate_hardware_lines(raw)


def hinge_lines_for_cabinet_config(cab_cfg, columns_raw: list | None = None) -> list[HardwareLine]:
    """Return HardwareLines for door hinges required by the cabinet.

    Uses ``HingeSpec.hinges_for_height()`` to count hinges per door.
    Hinges are sold individually (pack_quantity=1).

    Each door opening resolves its own hinge model (``op.hinge_key`` overriding
    ``cab_cfg.door_hinge``), so a per-opening override is billed under its own
    SKU — not the cabinet default — and openings whose resolved key is unknown
    are skipped without suppressing the rest.
    """
    from .hardware import get_hinge
    from .door import DoorConfig
    from .cabinet import bays_from_config, face_layout

    # Hinge counts bill by the REAL leaf height — face_layout's
    # stack-anchored panel, the one that gets cut and hung — not
    # DoorConfig's opening−reveal approximation. At the manufacturer
    # thresholds (900/1600/2000) the 4+ mm difference under-ordered
    # hinges for the taller actual leaf.
    _door_leaf_h = {
        (q.bay, q.slot): q.height
        for q in face_layout(bays_from_config(cab_cfg, columns_raw))
        if q.kind == "door" and q.leaf == 0
    }

    # One HardwareLine per resolved hinge SKU, in first-seen order.
    lines: list[HardwareLine] = []

    def _hinges_from_stack(stack, interior_width: float,
                           bay_idx: int) -> None:
        for slot_idx, item in enumerate(stack):
            op = to_opening(item)
            opening_h, slot_type = op.height_mm, op.opening_type
            if slot_type not in ("door", "door_pair"):
                continue
            hinge_key = op.hinge_key or cab_cfg.door_hinge
            try:
                hinge_spec = get_hinge(hinge_key)
            except KeyError:
                continue
            num_doors = op.num_doors or (2 if slot_type == "door_pair" else 1)
            dcfg = DoorConfig(
                opening_width=interior_width,
                opening_height=opening_h,
                num_doors=num_doors,
                hinge_key=hinge_key,
            )
            leaf_h = _door_leaf_h.get((bay_idx, slot_idx), dcfg.door_height)
            per_leaf = hinge_spec.hinges_for_height(
                leaf_h, dcfg.door_weight_kg)
            count = per_leaf * num_doors
            if count <= 0:
                continue
            sku = hinge_spec.part_number or hinge_key
            # State the leaf census so the count is checkable against the
            # physical build — "12 hinges" alone read as under-bought to
            # Charlie (2026-07-28); hinges are sold EACH, never in pairs.
            cup = ("INSERTA tool-free cup — no screws"
                   if hinge_spec.mounting_plate_part
                   and not hinge_spec.cup_screws else "screw-on cup")
            lines.append(HardwareLine(
                sku=sku,
                category="hinge",
                name=hinge_spec.name,
                brand=hinge_spec.manufacturer,
                model_number=sku,
                pieces_needed=count,
                pack_quantity=1,
                notes=(f"(sold each; {per_leaf} per leaf × {num_doors} "
                       f"leaf/leaves @ {leaf_h:.0f} mm; {cup})"),
            ))
            # The hinge SKU is the cup/arm only — the CLIP mounting plate
            # is a separate purchase, one per hinge (caught at order time,
            # 2026-07-28). 173L8100 ships with pre-mounted 5 mm Euro
            # system screws, so no plate-screw line is needed.
            if hinge_spec.mounting_plate_part:
                plate = hinge_spec.mounting_plate_part
                lines.append(HardwareLine(
                    sku=f"blum_{plate.lower()}",
                    category="hinge_accessory",
                    name='Blum CLIP 0mm wing mounting plate',
                    brand=hinge_spec.manufacturer,
                    model_number=plate,
                    pieces_needed=count,
                    pack_quantity=1,
                    notes=("(1 per hinge; pre-mounted 5 mm Euro system "
                           "screws — drill 5 mm pilots 37 mm from the "
                           "front edge)"),
                ))
            if hinge_spec.cup_screws:
                lines.append(HardwareLine(
                    sku="blum_606n",
                    category="fastener",
                    name='Blum #6 x 5/8" flat head screws (100-pack)',
                    brand="Blum",
                    model_number="606N100",
                    pieces_needed=count * hinge_spec.cup_screws,
                    pack_quantity=100,
                    notes=(f"({hinge_spec.cup_screws} per screw-on hinge "
                           f"cup — {sku})"),
                ))

    if columns_raw:
        for bay_idx, col in enumerate(columns_raw):
            stack = stack_from_column(col)
            _hinges_from_stack(stack, float(col["width_mm"]), bay_idx)
    elif getattr(cab_cfg, "columns", None):
        for bay_idx, col in enumerate(cab_cfg.columns):
            _hinges_from_stack(col.openings, col.width_mm, bay_idx)
    else:
        _hinges_from_stack(cab_cfg.openings, cab_cfg.interior_width, 0)

    return consolidate_hardware_lines(lines)


def leg_lines_for_cabinet_config(cab_cfg) -> list[HardwareLine]:
    """Return a HardwareLine for the cabinet's legs/feet, or an empty list."""
    from .hardware import get_leg

    try:
        leg_spec = get_leg(cab_cfg.leg_key)
    except KeyError:
        return []

    pieces = getattr(cab_cfg, "leg_count", 4)
    if pieces <= 0:
        return []
    sku = leg_spec.part_number or cab_cfg.leg_key
    return [HardwareLine(
        sku=sku,
        category="leg",
        name=leg_spec.name,
        brand=leg_spec.manufacturer,
        model_number=sku,
        pieces_needed=pieces,
        pack_quantity=1,
        notes=f"{leg_spec.height_mm:.0f} mm",
    )]


# Band-material aliases: species-prefixed plys collapse to the veneer roll /
# strip species actually purchased.
_BAND_MATERIAL_ALIASES = {
    "rift_white_oak": "white_oak",
    "flat_sawn_white_oak": "white_oak",
    "baltic_birch": "white_birch",
    "baltic_birch_prefinished": "white_birch",
}


def _band_material_for(panel_material: str, cfg) -> str:
    explicit = getattr(cfg, "edge_band_material", "") or ""
    if explicit:
        return explicit
    base = panel_material.removesuffix("_ply")
    return _BAND_MATERIAL_ALIASES.get(base, base)


BAND_PROUD_ALLOWANCE_MM = 10.0  # per-piece length for flush-trim + crosscut kerf
BAND_RIP_KERF_MM = 3.2


def band_segments_for_panels(
    panels: list["CutlistPanel"], cfg,
) -> dict[str, list[float]]:
    """Per-material band piece lengths (mm) from ``edge_band`` markers.

    One entry per physical piece × quantity, grouped by band species.

    A thin view over :func:`band_pieces_for_panels` rather than a second
    implementation. It WAS a second implementation, with its own copy of
    which edge runs along which axis — and when the perimeter arithmetic was
    corrected in one of them, the ordering BOM and the banding document
    would have quoted different lengths for the same strip. Two functions
    deriving one quantity is the whole defect class this file is being swept
    for; there is now one derivation and this is a projection of it.
    """
    per_material: dict[str, list[float]] = {}
    for piece in band_pieces_for_panels(panels, cfg):
        per_material.setdefault(piece["material"], []).append(piece["length"])
    return per_material


def pack_band_pieces(
    pieces: list[dict], stock: dict, kerf: float = BAND_RIP_KERF_MM,
) -> dict:
    """FFD-pack band piece dicts (each with ``"length"``) into strips.

    First-fit-decreasing with ``BAND_PROUD_ALLOWANCE_MM`` extra per piece
    (capped at the strip length). Strips-per-board comes from the board
    width, the strip width, and the rip kerf (the last strip needs no
    kerf). Each piece dict gains ``"cut"`` (the chop length — dead-length
    pieces are cut AT finished size; the strip's sliver is offal) and
    ``"dead_length"`` (fits, but with no flush-trim overhang).

    Returns ``strips`` (list of ``{"pieces": [...], "rem": float}`` — the
    per-strip assignment renderers use), ``strips_per_board`` / ``boards``
    / ``spare_strips``, ``flush_pieces`` and ``over_length_pieces`` (the
    latter longer than the stock — excluded from packing; splice or buy
    longer boards).
    """
    L = stock["length_mm"]
    strip_w = stock["strip_width_mm"]
    per_board = int((stock["width_mm"] + kerf) // (strip_w + kerf))
    if per_board < 1 and pieces:
        # Without this, the BOM silently orders 0 boards while pieces
        # remain queued, and to_banding_csv divides by zero
        # (review 2026-07-29 minor 1).
        raise ValueError(
            f"Band stock {stock['width_mm']:g} mm wide cannot yield a "
            f"single {strip_w:g} mm strip — check edge_band_stock "
            "width_mm / strip_width_mm."
        )
    over, flush, packable = [], [], []
    for pc in pieces:
        s = pc["length"]
        pc["dead_length"] = L - BAND_PROUD_ALLOWANCE_MM < s <= L
        pc["cut"] = s if pc["dead_length"] else min(s + BAND_PROUD_ALLOWANCE_MM, L)
        if s > L:
            over.append(pc)
        else:
            packable.append(pc)
            if pc["dead_length"]:
                flush.append(pc)
    strips: list[dict] = []
    for pc in sorted(packable, key=lambda d: -d["length"]):
        # Reserve the full-proud footprint even for dead-length pieces so
        # a strip never promises length it doesn't have.
        need = min(pc["length"] + BAND_PROUD_ALLOWANCE_MM, L)
        for st in strips:
            if st["rem"] >= need:
                st["rem"] -= need
                st["pieces"].append(pc)
                break
        else:
            strips.append({"rem": L - need, "pieces": [pc]})
    boards = math.ceil(len(strips) / per_board) if per_board else 0
    return {
        "strips": strips,
        "strips_per_board": per_board,
        "boards": boards,
        "spare_strips": max(0, boards * per_board - len(strips)),
        "flush_pieces": sorted(flush, key=lambda d: -d["length"]),
        "over_length_pieces": sorted(over, key=lambda d: -d["length"]),
    }


def pack_band_strips(
    segments: list[float], stock: dict, kerf: float = BAND_RIP_KERF_MM,
) -> dict:
    """Summary-shape wrapper over :func:`pack_band_pieces` for plain lengths.

    Same packing; returns counts (``strips`` is an int) with the flag lists
    as raw lengths — what :func:`edge_band_lines_for_panels` prices from.
    """
    pack = pack_band_pieces([{"length": s} for s in segments], stock, kerf)
    return {
        "strips": len(pack["strips"]),
        "strips_per_board": pack["strips_per_board"],
        "boards": pack["boards"],
        "spare_strips": pack["spare_strips"],
        "flush_pieces": [p["length"] for p in pack["flush_pieces"]],
        "over_length_pieces": [p["length"] for p in pack["over_length_pieces"]],
    }


def band_pieces_for_panels(
    panels: list["CutlistPanel"], cfg,
) -> list[dict]:
    """Band piece dicts with provenance for the banding-cutlist renderers.

    One dict per physical piece: ``part`` (the panel's assigned part ID —
    call after ``assign_part_ids`` so the banding doc matches the main
    cutlist), ``panel``, ``edge`` (front/left/right edge, or long/short
    edge for full-perimeter faces), ``length`` and ``material`` (band
    species via the same resolution the BOM line uses).
    """
    band_t = (float(getattr(cfg, "edge_band_thickness_mm", 0.0))
              if getattr(cfg, "edge_band_mode", "none") != "none" else 0.0)
    pieces: list[dict] = []
    for p in panels:
        if not p.edge_band:
            continue
        mat = _band_material_for(p.material, cfg)
        per: list[tuple[str, float]] = []
        for edge in p.edge_band:
            if edge == "all":
                # A full perimeter goes on in two passes: the SHORT pair
                # first, trimmed flush, then the LONG pair over the top of
                # them (see _band_corner_notes). So the long pieces do not
                # span the panel — they span the panel PLUS the two short
                # bands already stuck to its ends, which is 2 x band
                # thickness more.
                #
                # This was emitting the bare panel length under a "Finished
                # length" header, and the corner note claimed the 10 mm
                # proud allowance absorbed the difference. It does not: at
                # 1/8" it leaves 3.6 mm to flush-trim BOTH ends, and at 1/4"
                # — which the evaluator accepts without a word — 2 x 6.35 is
                # 12.7, so the piece comes out 2.7 mm SHORTER than the edge
                # it has to cover. 56 strips on the sideboards.
                #
                # Which pair is "short" is geometry, not which field it
                # happens to live in: a wide, low door has width > length.
                long_edge = max(p.length, p.width)
                short_edge = min(p.length, p.width)
                per += [("long edge", long_edge + 2 * band_t),
                        ("long edge", long_edge + 2 * band_t),
                        ("short edge", short_edge),
                        ("short edge", short_edge)]
            elif edge in ("left", "right"):
                per.append((f"{edge} edge", p.width))
            else:
                # A single banded edge laps nothing — it runs the edge.
                per.append((f"{edge} edge", p.length))
        for _ in range(p.quantity):
            for label, ln in per:
                pieces.append({"part": getattr(p, "part_id", "") or "",
                               "panel": p.name, "edge": label,
                               "length": ln, "material": mat})
    return pieces


def edge_band_lines_for_panels(
    panels: list["CutlistPanel"], cfg,
) -> list[HardwareLine]:
    """Edge-banding consumable lines from the panels' ``edge_band`` markers.

    Footage carries a 15% trim/waste factor. Hot-melt orders 7/8" × 50-ft
    pre-glued rolls. Hardwood mode: with an ``edge_band_stock`` spec on the
    config the line is priced in boards-to-order (real piece-into-strip
    packing, see :func:`pack_band_strips`); without one it stays the
    unpriced rip-from-offcuts line (strips cut proud for flush trimming).
    """
    mode = getattr(cfg, "edge_band_mode", "none")
    if mode == "none":
        return []
    thk = float(getattr(cfg, "edge_band_thickness_mm", 0.6))
    stock = getattr(cfg, "edge_band_stock", None) if mode == "hardwood" else None

    MM_PER_FT = 304.8
    HOT_MELT_ROLL_MM = 22.2      # 7/8" pre-glued roll
    DEFAULT_RIP_WIDTH_MM = 20.0  # when no stock spec names one
    # The thickest edge any of these panels actually bands. Both notes below
    # used to assert 18 mm and ~20 mm outright, so a 25 mm carcass was told
    # a 7/8" roll covers its edges — it does not, and that note is where the
    # roll gets ordered from.
    banded_t = sorted({float(p.thickness) for p in panels if p.edge_band})
    max_edge = banded_t[-1] if banded_t else 0.0
    rip_w = float((stock or {}).get("strip_width_mm", DEFAULT_RIP_WIDTH_MM))
    lines: list[HardwareLine] = []
    for mat, segs in sorted(band_segments_for_panels(panels, cfg).items()):
        mm = sum(segs)
        ft = math.ceil(mm / MM_PER_FT * 1.15)
        pretty = mat.replace("_", " ")
        if mode == "hot_melt":
            lines.append(HardwareLine(
                sku=f"edgeband-hotmelt-{mat}",
                category="edge_band",
                name=f'Iron-on edge banding, {pretty} 7/8" pre-glued',
                brand="",
                model_number="",
                pieces_needed=ft,
                pack_quantity=50,
                notes=(f"{mm / 1000:.1f} m of edges (+15% waste); "
                       + (f'7/8" ({HOT_MELT_ROLL_MM:g} mm) width covers the '
                          f'{max_edge:g} mm edges here (trim flush)'
                          if max_edge <= HOT_MELT_ROLL_MM else
                          f'WARNING: a 7/8" ({HOT_MELT_ROLL_MM:g} mm) roll '
                          f'does NOT cover the {max_edge:g} mm edges here '
                          '— order a wider roll')),
            ))
        elif stock is None:
            lines.append(HardwareLine(
                sku=f"edgeband-hardwood-{mat}",
                category="edge_band",
                name=f"Hardwood edge banding, {pretty} {thk:g} mm strips",
                brand="",
                model_number="",
                pieces_needed=ft,
                pack_quantity=1,
                notes=(f"{mm / 1000:.1f} m of edges (+15% waste); rip "
                       f"{thk:g} mm × {rip_w:g} mm strips from solid "
                       f"stock/offcuts — {rip_w - max_edge:g} mm proud of "
                       f"the {max_edge:g} mm edges, flush-trim after "
                       "glue-up"),
            ))
        else:
            pack = pack_band_strips(segs, stock)
            # Note chunks avoid ", " so the merge-time dedup keeps each whole.
            note_bits = [
                f"{mm / 1000:.1f} m of edges in {len(segs)} pieces → "
                f"{pack['strips']} strips of {stock['strip_width_mm']:g} mm "
                f"({pack['strips_per_board']}/board; "
                f"{pack['spare_strips']} spare)",
            ]
            if pack["flush_pieces"]:
                note_bits.append(
                    f"⚠ {len(pack['flush_pieces'])} piece(s) at ≈ full strip "
                    f"length (longest {max(pack['flush_pieces']):.0f} mm vs "
                    f"{stock['length_mm']:g} mm stock — no flush-trim "
                    "overhang; cut dead-length or splice)"
                )
            if pack["over_length_pieces"]:
                note_bits.append(
                    f"⚠ {len(pack['over_length_pieces'])} piece(s) LONGER "
                    f"than the stock (longest "
                    f"{max(pack['over_length_pieces']):.0f} mm — splice or "
                    "buy longer boards; not included in the board count)"
                )
            lines.append(HardwareLine(
                sku=f"edgeband-hardwood-{mat}",
                category="edge_band",
                name=(f"Hardwood banding stock, {pretty} — {thk:g} mm × "
                      f"{stock['width_mm']:g} mm × {stock['length_mm']:g} mm "
                      "boards"),
                brand="",
                model_number="",
                pieces_needed=pack["boards"],
                pack_quantity=1,
                unit_price_usd=stock["price_usd"],
                notes="; ".join(note_bits),
            ))
    return lines


def _band_packs_by_material(panels: list["CutlistPanel"], cfg) -> list[tuple]:
    """(material, pieces-pack) per band species, packed with the cfg's stock."""
    stock = getattr(cfg, "edge_band_stock", None)
    by_mat: dict[str, list[dict]] = {}
    for pc in band_pieces_for_panels(panels, cfg):
        by_mat.setdefault(pc["material"], []).append(pc)
    return [(mat, pack_band_pieces(pcs, stock))
            for mat, pcs in sorted(by_mat.items())]


def to_banding_csv(panels: list["CutlistPanel"], cfg) -> str:
    """Board/strip/piece CSV for hardwood banding cut from purchased stock.

    Boards are numbered ``#1..#N`` globally (never per material, and ``#``
    so the label can't collide with part IDs — B1 is a bottom panel).
    Dead-length pieces are cut AT finished size; everything else is cut
    ``BAND_PROUD_ALLOWANCE_MM`` proud for flush trimming.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Board", "Strip", "Piece", "Material", "Part", "Panel",
                "Edge", "Finished mm", "Cut mm", "Note"])
    board_no = strip_no = 0
    for mat, pack in _band_packs_by_material(panels, cfg):
        per_board = pack["strips_per_board"]
        for si, st in enumerate(pack["strips"]):
            if si % per_board == 0:
                board_no += 1
            strip_no += 1
            for pi, pc in enumerate(st["pieces"], 1):
                w.writerow([f"#{board_no}", f"S{strip_no}", pi, mat,
                            pc["part"], pc["panel"], pc["edge"],
                            f"{pc['length']:.1f}", f"{pc['cut']:.1f}",
                            "DEAD LENGTH — no trim overhang"
                            if pc["dead_length"] else ""])
        for pc in pack["over_length_pieces"]:
            w.writerow(["—", "—", "—", mat, pc["part"], pc["panel"],
                        pc["edge"], f"{pc['length']:.1f}", "—",
                        "LONGER THAN STOCK — splice or longer boards"])
    return buf.getvalue()


def band_length_schedule(packs: list[tuple]) -> list[dict]:
    """Aggregate packed band pieces into a qty-at-each-length schedule.

    The strips all rip to one width, so the shop-facing question is just
    "how many pieces at each length" — this is the table the banding doc
    leads with. Rows sorted longest-first per material:
    ``{material, length, cut, qty, parts, edges, dead, over}`` where
    ``parts`` / ``edges`` are the sorted unique part IDs and edge kinds.
    """
    rows: dict[tuple, dict] = {}
    for mat, pack in packs:
        placed = [pc for st in pack["strips"] for pc in st["pieces"]]
        for pc, over in ([(p, False) for p in placed]
                         + [(p, True) for p in pack["over_length_pieces"]]):
            key = (mat, round(pc["length"], 1), pc["dead_length"], over)
            row = rows.setdefault(key, {
                "material": mat, "length": pc["length"], "cut": pc["cut"],
                "qty": 0, "parts": set(), "edges": set(),
                "dead": pc["dead_length"], "over": over,
            })
            row["qty"] += 1
            row["parts"].add(pc["part"] or pc["panel"])
            row["edges"].add(pc["edge"])
    out = sorted(rows.values(),
                 key=lambda r: (r["material"], -r["length"]))
    for r in out:
        r["parts"] = sorted(r["parts"])
        r["edges"] = sorted(r["edges"])
    return out


def _band_corner_notes(cfg, schedule: list[dict]) -> list[str]:
    """Shop-facing statements on how band pieces meet at corners.

    Banding is applied per flat panel BEFORE assembly (the assembly plan's
    hardwood sequence), so corner treatment follows from panel geometry —
    these notes state it so nobody has to reverse-engineer it at the bench.
    """
    notes: list[str] = []
    has_fronts = any("front edge" in r["edges"] for r in schedule)
    has_faces = any("long edge" in r["edges"] or "short edge" in r["edges"]
                    for r in schedule)
    miter = getattr(cfg, "carcass_corner_style", "butt") == "miter"
    if has_fronts and miter:
        notes.append(
            "Carcass front bands (mitered corners): each band runs the FULL "
            "panel edge and is trimmed flush to the panel's 45° ends before "
            "assembly — at every corner the two bands meet in a 45° seam "
            "that continues the waterfall. No overlap, no length changes.")
    elif has_fronts:
        notes.append(
            "Carcass front bands (butt corners): side bands run THROUGH "
            "full height; top/bottom bands butt between them (their length "
            "is the interior span, already what the schedule lists).")
    if has_faces:
        thk = float(getattr(cfg, "edge_band_thickness_mm", 0.0))
        notes.append(
            "Door / false-front perimeters: band the SHORT edges first and "
            "trim flush, then the LONG edges — the long bands OVERLAP and "
            "hide the short bands' end grain. The long pieces in the "
            f"schedule are already {2 * thk:g} mm longer than the panel "
            f"({thk:g} mm of band at each end) — that is the panel's own "
            "geometry, not trim allowance, and the proud allowance is on "
            "top of it.")
    return notes


def generate_banding_cutlist_html(
    panels: list["CutlistPanel"], cfg, cabinet_name: str,
) -> str:
    """Printable banding cutlist (self-contained HTML).

    Leads with what the bench needs: rip width + kerf, the qty-at-each-
    length schedule, and corner treatment; the board-by-board chop plan
    follows as an appendix. Part IDs match the layout drawings (call after
    ``assign_part_ids``), bold metric with fractional imperial beside.
    """
    stock = getattr(cfg, "edge_band_stock", None)
    thk = float(getattr(cfg, "edge_band_thickness_mm", 0.6))

    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    packs = _band_packs_by_material(panels, cfg)
    n_pieces = sum(len(st["pieces"]) for _, p in packs for st in p["strips"])
    n_strips = sum(len(p["strips"]) for _, p in packs)
    n_boards = sum(p["boards"] for _, p in packs)
    n_spare = sum(p["spare_strips"] for _, p in packs)
    n_dead = sum(len(p["flush_pieces"]) for _, p in packs)
    total_m = sum(pc["length"] for _, p in packs for st in p["strips"]
                  for pc in st["pieces"]) / 1000
    over = [pc for _, p in packs for pc in p["over_length_pieces"]]
    schedule = band_length_schedule(packs)
    multi_mat = len(packs) > 1

    # Length schedule — the table the bench works from.
    sched_html: list[str] = [
        "<h2>Length schedule</h2>",
        "<table><tr><th>Qty</th>"
        + ("<th>Material</th>" if multi_mat else "")
        + "<th>Finished length</th><th>Cut at</th><th>Parts</th>"
          "<th>Edge</th><th>Note</th></tr>"]
    for r in schedule:
        if r["over"]:
            note = ("<span class='warn'>LONGER THAN STOCK — splice or "
                    "longer boards</span>")
            cut = "—"
        elif r["dead"]:
            note = ("<span class='warn'>DEAD LENGTH — cut at finished "
                    "size, no trim overhang</span>")
            cut = f"{r['cut']:.1f} mm"
        else:
            note = ""
            cut = f"{r['cut']:.1f} mm"
        sched_html.append(
            f"<tr><td><b>{r['qty']}×</b></td>"
            + (f"<td>{esc(r['material'].replace('_', ' '))}</td>"
               if multi_mat else "")
            + f"<td><b>{r['length']:.1f} mm</b> <span class='imp'>"
              f"{_inch_frac(r['length'])}</span></td><td>{cut}</td>"
              f"<td>{esc(', '.join(r['parts']))}</td>"
              f"<td>{esc(' / '.join(r['edges']))}</td><td>{note}</td></tr>")
    sched_html.append("</table>")

    corner_html = ""
    corners = _band_corner_notes(cfg, schedule)
    if corners:
        corner_html = ("<h2>Corners</h2>"
                       + "".join(f"<p>{esc(c)}</p>" for c in corners))

    body: list[str] = ["<h2 class='appendix'>Appendix — board-by-board "
                       "chop plan <span class='sub'>(one workable packing; "
                       "any chop order that satisfies the schedule works)"
                       "</span></h2>"]
    board_no = strip_no = 0
    for mat, pack in packs:
        if multi_mat:
            body.append(f"<h2 class='mat'>{esc(mat.replace('_', ' '))}</h2>")
        per_board = pack["strips_per_board"]
        # n strips leaving an offcut consume n kerfs (each strip is severed
        # from the remainder); only an exact-fit last strip uses the board
        # edge and saves one (review 2026-07-29 minor 2).
        raw_left = (stock["width_mm"] - per_board * stock["strip_width_mm"]
                    - (per_board - 1) * BAND_RIP_KERF_MM)
        offal = max(0.0, raw_left - BAND_RIP_KERF_MM) if raw_left > 0 else 0.0
        for bi in range(pack["boards"]):
            board_no += 1
            chunk = pack["strips"][bi * per_board:(bi + 1) * per_board]
            body.append(
                f"<h2 class='board'>Board #{board_no} <span class='sub'>— rip "
                f"{len(chunk)} × {stock['strip_width_mm']:g} mm strips "
                f"({per_board} max/board, {BAND_RIP_KERF_MM:g} mm kerf, "
                f"{offal:.1f} mm offal)</span></h2>")
            for st in chunk:
                strip_no += 1
                used = stock["length_mm"] - st["rem"]
                body.append(
                    f"<h3>Strip S{strip_no} <span class='sub'>— "
                    f"{len(st['pieces'])} piece(s), {used:.0f} mm used, "
                    f"{st['rem']:.0f} mm offcut</span></h3>")
                body.append("<table><tr><th>#</th><th>Part</th>"
                            "<th>Panel · edge</th><th>Finished</th>"
                            "<th>Cut at</th></tr>")
                for pi, pc in enumerate(st["pieces"], 1):
                    warn = (" <span class='warn'>DEAD LENGTH — cut at "
                            "exactly finished size, no trim overhang</span>"
                            if pc["dead_length"] else "")
                    body.append(
                        f"<tr><td>{pi}</td><td><b>{esc(pc['part'])}</b></td>"
                        f"<td>{esc(pc['panel'])} · {esc(pc['edge'])}</td>"
                        f"<td><b>{pc['length']:.1f} mm</b> "
                        f"<span class='imp'>{_inch_frac(pc['length'])}"
                        f"</span></td><td>{pc['cut']:.1f} mm{warn}</td></tr>")
                body.append("</table>")

    over_html = ""
    if over:
        rows = "".join(
            f"<li><b>{esc(pc['part'])}</b> {esc(pc['panel'])} · "
            f"{esc(pc['edge'])} — {pc['length']:.1f} mm</li>" for pc in over)
        over_html = (f"<div class='box warnbox'><b>⚠ {len(over)} piece(s) "
                     f"LONGER than the stock</b> — splice or buy longer "
                     f"boards; not in the board count:<ul>{rows}</ul></div>")

    dead_note = (f" All pieces are cut {BAND_PROUD_ALLOWANCE_MM:g} mm proud "
                 f"for flush trimming <b>except {n_dead} DEAD-LENGTH "
                 "piece(s)</b> — cut those at exactly finished size from "
                 "the straightest strips." if n_dead else
                 f" All pieces are cut {BAND_PROUD_ALLOWANCE_MM:g} mm proud "
                 "for flush trimming.")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Banding cutlist — {esc(cabinet_name)}</title><style>
body{{font-family:system-ui,sans-serif;max-width:900px;margin:24px auto;
padding:0 16px;color:#222}}
h1{{border-bottom:2px solid #2c3e50}} h2.mat{{color:#2c3e50}}
.sub{{font-weight:400;color:#666;font-size:.75em}}
.imp{{color:#666;font-size:.85em}}
.warn{{color:#c0392b;font-weight:600;font-size:.85em}}
table{{border-collapse:collapse;width:100%;margin:4px 0 14px}}
th,td{{border:1px solid #ccc;padding:4px 8px;text-align:left;
font-size:.9em}} th{{background:#2c3e50;color:#fff}}
.box{{background:#f6f3ee;border:1px solid #ccc;border-radius:6px;
padding:10px 14px;margin:12px 0}}
.warnbox{{background:#fbeeec;border-color:#c0392b}}
@media print{{h2.board{{page-break-before:always}}}}</style></head><body>
<h1>Edge-banding cutlist — {esc(cabinet_name)}</h1>
<div class="box"><b>Stock:</b> {n_boards} board(s), {thk:g} mm
({_inch_frac(thk)}) × {stock['width_mm']:g} mm
({_inch_frac(stock['width_mm'])}) × {stock['length_mm']:g} mm
({_inch_frac(stock['length_mm'])}) @ ${stock['price_usd']:g} =
<b>${n_boards * stock['price_usd']:.2f}</b><br>
<b>Rip:</b> ALL strips are the same width — fence at
<b>{stock['strip_width_mm']:g} mm</b> ({_inch_frac(stock['strip_width_mm'])}),
{BAND_RIP_KERF_MM:g} mm kerf assumed → {n_strips} strips needed
({packs[0][1]['strips_per_board'] if packs else 0}/board), {n_spare}
spare.<br>
<b>Pieces:</b> {n_pieces} band pieces, {total_m:.1f} m of
edges.{dead_note}<br>
<b>Order of work:</b> rip all strips first, then chop to the length
schedule below — longest first.</div>
{''.join(sched_html)}{corner_html}{over_html}{''.join(body)}
</body></html>"""


def generate_banding_cutlist_pdf(
    panels: list["CutlistPanel"], cfg, cabinet_name: str,
    paper: str = "letter",
) -> bytes:
    """Printable banding cutlist as PDF (same content as the HTML).

    Leads with rip width + kerf, the qty-at-each-length schedule, and the
    corner treatment; the board-by-board chop plan follows. Free-text cells
    are Paragraphs (plain strings never wrap — the #43 lesson). Raises
    ``ImportError`` when reportlab is unavailable (callers degrade like the
    layout PDF). ``paper``: "letter" (default) or "a4".
    """
    if not _REPORTLAB_AVAILABLE:
        raise ImportError(
            "reportlab is required for PDF export. "
            "Install with: uv pip install reportlab"
        )
    from xml.sax.saxutils import escape as _xml
    from reportlab.lib import colors as _colors
    from reportlab.lib.styles import getSampleStyleSheet as _styles
    from reportlab.lib.units import mm as _MM
    from reportlab.platypus import (
        Paragraph as _P, SimpleDocTemplate as _Doc, Spacer as _Spacer,
        Table as _Table, TableStyle as _TS)

    stock = getattr(cfg, "edge_band_stock", None)
    thk = float(getattr(cfg, "edge_band_thickness_mm", 0.6))
    packs = _band_packs_by_material(panels, cfg)
    schedule = band_length_schedule(packs)
    multi_mat = len(packs) > 1
    n_strips = sum(len(p["strips"]) for _, p in packs)
    n_boards = sum(p["boards"] for _, p in packs)
    n_spare = sum(p["spare_strips"] for _, p in packs)
    n_dead = sum(len(p["flush_pieces"]) for _, p in packs)
    per_board = packs[0][1]["strips_per_board"] if packs else 0

    ss = _styles()
    h1, h2, body_st = ss["Title"], ss["Heading2"], ss["BodyText"]
    small = ss["BodyText"].clone("small", fontSize=8, leading=10)

    head_style = _TS([
        ("BACKGROUND", (0, 0), (-1, 0), _colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), _colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, _colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ])

    story = [
        _P(f"Edge-banding cutlist — {_xml(cabinet_name)}", h1),
        _P(f"<b>Stock:</b> {n_boards} board(s), {thk:g} mm "
           f"({_inch_frac(thk)}) × {stock['width_mm']:g} mm × "
           f"{stock['length_mm']:g} mm @ ${stock['price_usd']:g} = "
           f"<b>${n_boards * stock['price_usd']:.2f}</b>", body_st),
        _P(f"<b>Rip:</b> ALL strips are the same width — fence at "
           f"<b>{stock['strip_width_mm']:g} mm</b> "
           f"({_inch_frac(stock['strip_width_mm'])}), "
           f"{BAND_RIP_KERF_MM:g} mm kerf assumed → {n_strips} strips "
           f"({per_board}/board), {n_spare} spare.", body_st),
        _P(f"All pieces cut {BAND_PROUD_ALLOWANCE_MM:g} mm proud for "
           f"flush trimming"
           + (f" except {n_dead} DEAD-LENGTH piece(s) — cut those at "
              "exactly finished size." if n_dead else "."), body_st),
        _Spacer(0, 4 * _MM),
        _P("Length schedule", h2),
    ]

    hdr = ["Qty"] + (["Material"] if multi_mat else []) + \
        ["Finished", "Cut at", "Parts", "Edge", "Note"]
    rows: list[list] = [hdr]
    for r in schedule:
        if r["over"]:
            note, cut = "LONGER THAN STOCK — splice", "—"
        elif r["dead"]:
            note, cut = "DEAD LENGTH — no overhang", f"{r['cut']:.1f} mm"
        else:
            note, cut = "", f"{r['cut']:.1f} mm"
        rows.append(
            [f"{r['qty']}×"]
            + ([_P(_xml(r["material"].replace("_", " ")), small)]
               if multi_mat else [])
            + [f"{r['length']:.1f} mm\n{_inch_frac(r['length'])}", cut,
               _P(_xml(", ".join(r["parts"])), small),
               _P(_xml(" / ".join(r["edges"])), small),
               _P(f'<font color="#c0392b">{_xml(note)}</font>'
                  if note else "", small)])
    story.append(_Table(rows, style=head_style, repeatRows=1))

    corners = _band_corner_notes(cfg, schedule)
    if corners:
        story.append(_P("Corners", h2))
        story += [_P(_xml(c), body_st) for c in corners]

    story.append(_P("Appendix — board-by-board chop plan", h2))
    story.append(_P("One workable packing; any chop order that satisfies "
                    "the schedule works.", small))
    board_no = strip_no = 0
    for mat, pack in packs:
        pb = pack["strips_per_board"]
        for bi in range(pack["boards"]):
            board_no += 1
            chunk = pack["strips"][bi * pb:(bi + 1) * pb]
            rows = [["Strip", "Pieces (cut lengths, mm)", "Offcut"]]
            for st in chunk:
                strip_no += 1
                rows.append([
                    f"S{strip_no}",
                    _P(_xml("  |  ".join(
                        f"{pc['cut']:.0f} ({pc['part'] or pc['panel']})"
                        for pc in st["pieces"])), small),
                    f"{st['rem']:.0f} mm"])
            story.append(_Spacer(0, 3 * _MM))
            story.append(_P(f"Board #{board_no}"
                            + (f" — {mat.replace('_', ' ')}"
                               if multi_mat else ""), ss["Heading3"]))
            story.append(_Table(rows, style=head_style, repeatRows=1,
                                colWidths=[16 * _MM, 144 * _MM, 20 * _MM]))

    import io as _io
    buf = _io.BytesIO()
    # Margins sized so the 180 mm chop-plan table fits the frame — the
    # default 1" margins left a 159 mm frame under a 176 mm table and the
    # Offcut column ran into the right margin (review 2026-07-29 minor 6).
    _Doc(buf, pagesize=_paper_size(paper),
         leftMargin=15 * _MM, rightMargin=15 * _MM,
         title=f"Banding cutlist — {cabinet_name}").build(story)
    return buf.getvalue()


def joinery_lines_for_cabinet_config(
    cab_cfg, columns_raw: list | None = None
) -> list[HardwareLine]:
    """Return HardwareLines for carcass joinery consumables.

    Counts every panel-to-panel edge joint in the carcass and looks up the
    fastener count using the corresponding joinery spec's ``count_for_span``.
    Returns an empty list for ``dado_rabbet`` (the dado/rabbet itself holds
    the panel — no additional fasteners are needed).

    Joints counted:
      - Top panel to each side (×2)
      - Bottom panel to each side (×2)
      - Each column divider top edge to top panel (×N dividers)
      - Each column divider bottom edge to bottom panel (×N dividers)
      - Each fixed shelf to its two bearing surfaces (×2 per shelf)

    The "span" for each joint is the panel depth (``interior_depth``), since
    fasteners run along the depth direction of the joint edge.
    """
    from .cabinet import CarcassJoinery
    from .joinery import (
        DominoSpec, DominoSize, get_domino_size,
        PocketScrewSpec, pocket_screw_length,
        BiscuitSpec,
        DowelSpec,
    )

    joinery = getattr(cab_cfg, "carcass_joinery", CarcassJoinery.DADO_RABBET)
    if joinery == CarcassJoinery.DADO_RABBET:
        return []

    # Span must match build_assembly_plan exactly — the plan's per-joint
    # mortise count and this BOM's tenon count are the same census, so both
    # read the depth datum rather than restating it.
    interior_depth = _interior_depth(cab_cfg)
    side_t = getattr(cab_cfg, "side_thickness", 18.0)

    # Count joints: top+bottom = 4, each divider adds 2, each shelf adds 2.
    # Divider and per-column shelf counts come from columns_raw (dicts, when
    # supplied) or fall back to cab_cfg.columns (ColumnConfig objects) — the
    # same precedence the pull / slide / hinge extractors use.
    global_shelves = len(getattr(cab_cfg, "fixed_shelf_positions", []))
    col_shelves = 0
    if columns_raw:
        n_dividers = max(0, len(columns_raw) - 1)
        for col in columns_raw:
            col_shelves += len(col.get("fixed_shelf_positions", []))
    elif getattr(cab_cfg, "columns", None):
        n_dividers = max(0, len(cab_cfg.columns) - 1)
        for col in cab_cfg.columns:
            col_shelves += len(getattr(col, "fixed_shelf_positions", ()) or ())
    else:
        n_dividers = 0
    n_joints = 4 + 2 * n_dividers + 2 * global_shelves + 2 * col_shelves

    if joinery == CarcassJoinery.FLOATING_TENON:
        from .joinery import (
            DOMINO_PACK_QUANTITIES, carcass_domino_size_for_thickness,
        )
        # Tenon size follows carcass stock thickness: 5×30 for ≤19 mm
        # (3/4" ply), 8×40 above that.
        size_key = carcass_domino_size_for_thickness(side_t)
        size = get_domino_size(size_key)
        spec = DominoSpec(size_key=size_key, max_spacing=150.0)
        per_joint = spec.count_for_span(interior_depth)
        total = n_joints * per_joint
        return consolidate_hardware_lines([HardwareLine(
            sku=f"festool-{size.part_number}",
            category="joinery",
            name=f"Festool Domino {size_key.replace('x', '×')} mm",
            brand="Festool",
            model_number=size.part_number,
            pieces_needed=total,
            pack_quantity=DOMINO_PACK_QUANTITIES[size_key],
            notes=f"{per_joint} per joint × {n_joints} joints",
        )])

    if joinery == CarcassJoinery.POCKET_SCREW:
        _SCREW_FRACTIONS = {19: '3/4"', 25: '1"', 32: '1-1/4"', 38: '1-1/2"', 51: '2"', 64: '2-1/2"'}
        spec = PocketScrewSpec()
        per_joint = spec.count_for_span(interior_depth)
        total = n_joints * per_joint
        screw_len_mm = int(pocket_screw_length(side_t))
        screw_len_str = _SCREW_FRACTIONS.get(screw_len_mm, f"{screw_len_mm}mm")
        return consolidate_hardware_lines([HardwareLine(
            sku=f"kreg-sml-c{screw_len_mm}-100",
            category="joinery",
            name=f"Pocket Screw {screw_len_str} coarse thread",
            brand="Kreg",
            model_number=f"SML-C{int(screw_len_mm)}-100",
            pieces_needed=total,
            pack_quantity=100,
            notes=f"{per_joint} per joint × {n_joints} joints",
        )])

    if joinery == CarcassJoinery.BISCUIT:
        spec = BiscuitSpec(size="#10", max_spacing=100.0)
        per_joint = spec.count_for_span(interior_depth)
        total = n_joints * per_joint
        return consolidate_hardware_lines([HardwareLine(
            sku="biscuit-10-100pk",
            category="joinery",
            name="Biscuit #10",
            brand="",
            model_number="",
            pieces_needed=total,
            pack_quantity=100,
            notes=f"{per_joint} per joint × {n_joints} joints",
        )])

    if joinery == CarcassJoinery.DOWEL:
        spec = DowelSpec(diameter=8.0, max_spacing=96.0)
        per_joint = spec.count_for_span(interior_depth)
        total = n_joints * per_joint
        return consolidate_hardware_lines([HardwareLine(
            sku="dowel-8x30-50pk",
            category="joinery",
            name="Hardwood Dowel 8×30 mm",
            brand="",
            model_number="",
            pieces_needed=total,
            pack_quantity=50,
            notes=f"{per_joint} per joint × {n_joints} joints",
        )])

    return []


def drawer_front_screw_lines_for_cabinet_config(
    cab_cfg, columns_raw: list | None = None
) -> list[HardwareLine]:
    """Return HardwareLines for screws that attach false fronts to drawer boxes.

    Standard practice: 2 × #8 × 1-1/4" (32 mm) pan-head screws per false
    front, driven from inside the drawer box face into the false front.
    Screws are sold in boxes of 100.
    """
    n_drawers = 0

    def _count_drawers(stack) -> int:
        return sum(
            1 for item in stack
            if to_opening(item).opening_type == "drawer"
        )

    if columns_raw:
        for col in columns_raw:
            stack = stack_from_column(col)
            n_drawers += _count_drawers(stack)
    elif getattr(cab_cfg, "columns", None):
        for col in cab_cfg.columns:
            n_drawers += _count_drawers(col.openings)
    else:
        n_drawers = _count_drawers(getattr(cab_cfg, "openings", []))

    if n_drawers == 0:
        return []

    total = n_drawers * 2  # 2 screws per false front
    return [HardwareLine(
        sku="screw-8x32-panhead-100pk",
        category="fastener",
        name='#8 × 1-1/4" Pan Head Screw (false front)',
        brand="",
        model_number="",
        pieces_needed=total,
        pack_quantity=100,
        notes=f"2 per drawer false front × {n_drawers} drawers",
    )]


def hardware_bom_for_cabinet_config(cab_cfg, columns_raw: list | None = None) -> list[HardwareLine]:
    """Return a consolidated hardware BOM for the full cabinet.

    Aggregates pulls, slides, hinges, legs, joinery, and fasteners.
    Categories are ordered: pull → slide → hinge → leg → joinery → fastener.
    """
    lines: list[HardwareLine] = []
    lines.extend(pull_lines_for_cabinet_config(cab_cfg, columns_raw))
    lines.extend(slide_lines_for_cabinet_config(cab_cfg, columns_raw))
    lines.extend(hinge_lines_for_cabinet_config(cab_cfg, columns_raw))
    lines.extend(leg_lines_for_cabinet_config(cab_cfg))
    lines.extend(joinery_lines_for_cabinet_config(cab_cfg, columns_raw))
    lines.extend(drawer_front_screw_lines_for_cabinet_config(cab_cfg, columns_raw))
    return consolidate_hardware_lines(lines)


def consolidate_hardware_lines(lines: list[HardwareLine]) -> list[HardwareLine]:
    """Merge HardwareLines that share the same SKU, summing pieces_needed.

    Notes are concatenated (comma-separated) for traceability. Input order
    is preserved for the first occurrence of each SKU.
    """
    def _line_sources(line: HardwareLine) -> dict:
        # A line re-entering consolidation may already carry a per-project
        # breakdown (batch mode consolidates per-cabinet output again);
        # otherwise derive it from the line's own source tag.
        if line.source_counts:
            return dict(line.source_counts)
        if line.source:
            return {line.source: line.pieces_needed}
        return {}

    out: dict[str, HardwareLine] = {}
    order: list[str] = []
    for line in lines:
        if line.sku in out:
            merged = out[line.sku]
            merged.pieces_needed += line.pieces_needed
            # Dedup identical notes — 21 drawers on one slide model should
            # read "533 mm" once, not 21 times.
            if line.notes and line.notes not in merged.notes.split(", "):
                merged.notes = (
                    f"{merged.notes}, {line.notes}" if merged.notes else line.notes
                )
            for src, n in _line_sources(line).items():
                merged.source_counts[src] = merged.source_counts.get(src, 0) + n
            if merged.unit_price_usd is None:
                merged.unit_price_usd = line.unit_price_usd
        else:
            out[line.sku] = HardwareLine(
                sku=line.sku,
                category=line.category,
                name=line.name,
                brand=line.brand,
                model_number=line.model_number,
                pieces_needed=line.pieces_needed,
                pack_quantity=line.pack_quantity,
                notes=line.notes,
                unit_price_usd=line.unit_price_usd,
                source_counts=_line_sources(line),
            )
            order.append(line.sku)
    return [out[sku] for sku in order]


def to_hardware_csv(lines: list[HardwareLine]) -> str:
    """Export a hardware BOM as CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "SKU", "Category", "Name", "Brand", "Model #",
        "Pieces Needed", "Pack Qty", "Packs to Order",
        "Pieces Ordered", "Leftover", "Notes",
    ])
    for line in lines:
        writer.writerow([
            line.sku, line.category, line.name, line.brand, line.model_number,
            line.pieces_needed, line.pack_quantity, line.packs_to_order,
            line.pieces_ordered, line.leftover, line.notes,
        ])
    return buf.getvalue()


def to_hardware_json(lines: list[HardwareLine]) -> str:
    """Export a hardware BOM as JSON.

    Each line includes the derived procurement fields so downstream consumers
    (MCP clients, spreadsheets) don't have to replicate the math.
    """
    payload = {
        "lines": [
            {
                "sku": l.sku,
                "category": l.category,
                "name": l.name,
                "brand": l.brand,
                "model_number": l.model_number,
                "pieces_needed": l.pieces_needed,
                "pack_quantity": l.pack_quantity,
                "packs_to_order": l.packs_to_order,
                "pieces_ordered": l.pieces_ordered,
                "leftover": l.leftover,
                "notes": l.notes,
                "unit_price_usd": l.unit_price,
                "line_total_usd": round(l.packs_to_order * l.unit_price, 2),
            }
            for l in lines
        ],
        "totals": {
            "line_count": len(lines),
            "pieces_needed": sum(l.pieces_needed for l in lines),
            "packs_to_order": sum(l.packs_to_order for l in lines),
        },
    }
    return json.dumps(payload, indent=2)


def _guillotine_cuts(
    placements: list[Placement],
    rect_x: float, rect_y: float, rect_w: float, rect_h: float,
    depth: int,
    out: list,
    EPS: float = 2.0,
) -> None:
    """Recursively find guillotine cut lines within a rectangle.

    Each entry appended to *out* is
    ``(depth, pos, orient, x0, y0, x1, y1, is_breakdown, dim_a, dim_b)`` where:

    - ``orient`` is ``'h'`` (horizontal) or ``'v'`` (vertical)
    - coordinates describe the full extent of the cut within its sub-rectangle
    - ``is_breakdown`` is True when both halves still contain multiple pieces
    - ``dim_a`` / ``dim_b`` are the resulting sub-board sizes on each side of
      the cut (in mm), useful for setting the fence
    """
    if len(placements) <= 1:
        return

    # Try horizontal cuts first (rip cuts along the sheet width).
    for cy in sorted({p.y + p.placed_width for p in placements}):
        if cy <= rect_y + EPS or cy >= rect_y + rect_h - EPS:
            continue
        above = [p for p in placements if p.y + p.placed_width <= cy + EPS]
        below = [p for p in placements if p.y >= cy - EPS]
        if above and below and len(above) + len(below) == len(placements):
            is_breakdown = len(above) > 1 and len(below) > 1
            dim_a = round(max(p.y + p.placed_width for p in above) - min(p.y for p in above))
            dim_b = round(max(p.y + p.placed_width for p in below) - min(p.y for p in below))
            out.append((depth, cy, 'h', rect_x, cy, rect_x + rect_w, cy, is_breakdown, dim_a, dim_b))
            _guillotine_cuts(above, rect_x, rect_y, rect_w, cy - rect_y, depth + 1, out, EPS)
            _guillotine_cuts(below, rect_x, cy, rect_w, rect_y + rect_h - cy, depth + 1, out, EPS)
            return

    # Try vertical cuts (crosscuts along the sheet height).
    for cx in sorted({p.x + p.placed_length for p in placements}):
        if cx <= rect_x + EPS or cx >= rect_x + rect_w - EPS:
            continue
        left  = [p for p in placements if p.x + p.placed_length <= cx + EPS]
        right = [p for p in placements if p.x >= cx - EPS]
        if left and right and len(left) + len(right) == len(placements):
            is_breakdown = len(left) > 1 and len(right) > 1
            dim_a = round(max(p.x + p.placed_length for p in left)  - min(p.x for p in left))
            dim_b = round(max(p.x + p.placed_length for p in right) - min(p.x for p in right))
            out.append((depth, cx, 'v', cx, rect_y, cx, rect_y + rect_h, is_breakdown, dim_a, dim_b))
            _guillotine_cuts(left,  rect_x, rect_y, cx - rect_x,          rect_h, depth + 1, out, EPS)
            _guillotine_cuts(right, cx,     rect_y, rect_x + rect_w - cx, rect_h, depth + 1, out, EPS)
            return


def generate_sheet_layout_html(
    groups: list[tuple[str, list["CutlistPanel"], "OptimizationResult"]],
    cabinet_name: str = "cabinet",
    kerf: float = 3.2,
    hardware_lines: "list[HardwareLine] | None" = None,
) -> str:
    """Generate a self-contained HTML page with per-sheet SVG cut layouts.

    Parameters
    ----------
    groups:
        List of ``(label, panels, opt_result)`` tuples — one per thickness
        group.  Label is the display name shown on the tab.
    cabinet_name:
        Used in the page title and ``<h1>``.

    Returns
    -------
    str
        Complete HTML document (self-contained, no external dependencies).
    """
    # ── Multi-project batches colour panels by originating project ────────────
    source_order: list[str] = []
    for _, _panels, _opt in groups:
        for _p in _panels:
            if _p.source and _p.source not in source_order:
                source_order.append(_p.source)
    project_mode = bool(source_order)

    def _fill_for(pl: Placement) -> str:
        if project_mode:
            return _source_colour(pl.source, source_order)
        return _panel_colour(pl.panel_name)

    # ── SVG builder ────────────────────────────────────────────────────────────
    def _sheet_svg(sheet: SheetStock, placements: list[Placement],
                   id_map: dict, preset_cuts: list | None = None) -> str:
        sl, sw = sheet.length, sheet.width
        # Display ~760 px wide; height scaled proportionally.
        disp_w = 760
        disp_h = sw / sl * disp_w

        out: list[str] = []
        pw_stroke = max(0.5, sl * 0.001)
        rx_val = sl * 0.003

        # Placements use top-left origin with y increasing downward, matching SVG.
        # placed_length/placed_width are net panel dimensions (no kerf padding).

        # Sheet background.
        out.append(
            f'<rect x="0" y="0" width="{sl:.1f}" height="{sw:.1f}" '
            f'fill="#F5EED8" stroke="#888" stroke-width="{sl * 0.002:.1f}"/>'
        )

        # Panels.
        for p in placements:
            fill = _fill_for(p)
            stroke = _panel_colour_dark(fill)

            out.append(
                f'<rect x="{p.x:.1f}" y="{p.y:.1f}" '
                f'width="{p.placed_length:.1f}" height="{p.placed_width:.1f}" '
                f'fill="{fill}" stroke="{stroke}" '
                f'stroke-width="{pw_stroke:.1f}" rx="{rx_val:.1f}"/>'
            )

            label = p.panel_name[:24] + ("…" if len(p.panel_name) > 24 else "")
            if p.rotated:
                label += " ↺"
            dim_text = f"{p.placed_length:.0f}×{p.placed_width:.0f} mm"
            pid = p.part_id or _placement_id(id_map, p)
            if pid:
                label = f"{pid} · {label}"

            min_dim = min(p.placed_length, p.placed_width)
            font_mm = max(min_dim * 0.10, 12)
            dim_font = max(min_dim * 0.07, 9)

            cx = p.x + p.placed_length / 2
            cy = p.y + p.placed_width / 2
            cy_label = cy - font_mm * 0.4
            cy_dim   = cy_label + font_mm * 1.1

            tall = p.placed_width > p.placed_length
            # Rotate BOTH lines as one group about the panel centre (the PDF's
            # translate-rotate-draw, in SVG). Rotating each line about its own
            # anchor leaves the line spacing along the reading direction, so
            # the two lines overprint on tall panels.
            if tall:
                out.append(f'<g transform="rotate(-90,{cx:.1f},{cy:.1f})">')
            out.append(
                f'<text x="{cx:.1f}" y="{cy_label:.1f}" '
                f'text-anchor="middle" dominant-baseline="middle" '
                f'font-family="monospace" font-size="{font_mm:.1f}" '
                # Solid black labels — the tinted text read as washed-out on
                # the pastel fills in print (Charlie, Jul 2026).
                f'fill="#000" pointer-events="none">'
                f'{_esc(label)}</text>'
            )
            out.append(
                f'<text x="{cx:.1f}" y="{cy_dim:.1f}" '
                f'text-anchor="middle" dominant-baseline="middle" '
                f'font-family="monospace" font-size="{dim_font:.1f}" '
                f'fill="#000" pointer-events="none">'
                f'{_esc(dim_text)}</text>'
            )
            if tall:
                out.append('</g>')


        # Guillotine cut lines — the optimizer's declared plan when present
        # (rips_first), else derived from geometry.
        raw_cuts: list = list(preset_cuts) if preset_cuts is not None else []
        if preset_cuts is None:
            _guillotine_cuts(placements, 0, 0, sl, sw, depth=0, out=raw_cuts)
        raw_cuts.sort(key=lambda c: (c[0], c[1]))  # BFS: shallower first

        breakdown_stroke = sl * 0.005
        atomic_stroke    = sl * 0.002
        label_r   = sl * 0.018
        label_font = label_r * 1.0
        seq = 0

        for entry in raw_cuts:
            depth, pos, orient, x0, y0, x1, y1, is_breakdown, dim_a, dim_b = entry
            dash = sl * 0.018
            if is_breakdown:
                seq += 1
                colour  = "#c0392b"
                opacity = "0.80"
                sw_line = breakdown_stroke
            else:
                colour  = "#555"
                opacity = "0.35"
                sw_line = atomic_stroke

            out.append(
                f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
                f'stroke="{colour}" stroke-width="{sw_line:.1f}" '
                f'stroke-dasharray="{dash:.0f},{dash*0.6:.0f}" opacity="{opacity}"/>'
            )

            if is_breakdown:
                lx = (x0 + x1) / 2 if orient == 'h' else x0
                ly = y0             if orient == 'h' else (y0 + y1) / 2

                # Numbered circle badge.
                out.append(
                    f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="{label_r:.1f}" '
                    f'fill="{colour}" opacity="0.9"/>'
                )
                out.append(
                    f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="middle" '
                    f'dominant-baseline="middle" font-family="monospace" '
                    f'font-size="{label_font:.1f}" font-weight="bold" fill="#fff" '
                    f'pointer-events="none">{seq}</text>'
                )

                # Dimension label — short side only, placed on that side of the cut.
                dim_font = label_r * 0.9
                pad = label_r * 1.6
                if orient == 'h':
                    if dim_a <= dim_b:
                        tx, ty, anchor = lx + label_r * 1.4, ly - pad, "start"
                        dim_label = f"{dim_a} mm"
                    else:
                        tx, ty, anchor = lx + label_r * 1.4, ly + pad, "start"
                        dim_label = f"{dim_b} mm"
                else:
                    if dim_a <= dim_b:
                        tx, ty, anchor = lx - pad, ly - label_r * 1.4, "end"
                        dim_label = f"{dim_a} mm"
                    else:
                        tx, ty, anchor = lx + pad, ly - label_r * 1.4, "start"
                        dim_label = f"{dim_b} mm"
                rotate = f' transform="rotate(-90,{tx:.1f},{ty:.1f})"' if orient == 'v' else ''
                out.append(
                    f'<text x="{tx:.1f}" y="{ty:.1f}" '
                    f'text-anchor="{anchor}" dominant-baseline="middle" '
                    f'font-family="monospace" font-size="{dim_font:.1f}" '
                    f'fill="{colour}" opacity="0.9" pointer-events="none"{rotate}>'
                    f'{dim_label}</text>'
                )

        # Ruler along the bottom edge.
        tick_font = sl * 0.018
        tick_y_top = sw + sl * 0.005
        tick_y_bot = tick_y_top + sl * 0.010
        out.append(
            f'<line x1="0" y1="{sw:.1f}" x2="{sl:.1f}" y2="{sw:.1f}" '
            f'stroke="#888" stroke-width="{pw_stroke:.1f}"/>'
        )
        for mm in range(0, int(sl) + 1, 200):
            out.append(
                f'<line x1="{mm}" y1="{tick_y_top:.1f}" '
                f'x2="{mm}" y2="{tick_y_bot:.1f}" '
                f'stroke="#666" stroke-width="{pw_stroke:.1f}"/>'
            )
            if mm % 400 == 0:
                out.append(
                    f'<text x="{mm}" y="{tick_y_bot + tick_font:.1f}" '
                    f'text-anchor="middle" font-family="monospace" '
                    f'font-size="{tick_font:.1f}" fill="#666">{mm}</text>'
                )

        vb_h = sw + sl * 0.06
        body = "\n".join(out)
        return (
            f'<svg viewBox="0 0 {sl:.1f} {vb_h:.1f}" '
            f'width="{disp_w}" height="{disp_h:.0f}" '
            f'xmlns="http://www.w3.org/2000/svg" '
            f'style="border:1px solid #ccc;border-radius:4px;background:#fff;">'
            f'{body}</svg>'
        )

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    src_letters = _source_letters_from_groups(groups)

    # ── Build tab HTML ─────────────────────────────────────────────────────────
    tab_buttons: list[str] = []
    tab_panes: list[str] = []
    # Globally unique sheet number across ALL groups, in generation order —
    # Charlie pencils it on each physical sheet's edge (Jul 2026). The PDF
    # renderer iterates groups/sheets in the same order, so numbers agree.
    global_sheet_no = 0

    for tab_idx, (label, _panels, opt) in enumerate(groups):
        active = "active" if tab_idx == 0 else ""
        tab_buttons.append(
            f'<button class="tab-btn {active}" '
            f'onclick="showTab({tab_idx})" id="btn-{tab_idx}">'
            f'{_esc(label)}</button>'
        )

        sheets_count = opt.sheets_used
        id_map = _group_id_map(_panels)
        by_sheet: dict[int, list[Placement]] = {}
        for p in opt.placements:
            by_sheet.setdefault(p.sheet_index, []).append(p)

        sheet_svgs: list[str] = []
        group_first_no = global_sheet_no + 1
        for si in sorted(by_sheet.keys()):
            global_sheet_no += 1
            # Per-sheet project key — only the projects cut from THIS sheet.
            sheet_key = ""
            if project_mode:
                on_sheet: list[str] = []
                for pl_ in by_sheet[si]:
                    if pl_.source and pl_.source not in on_sheet:
                        on_sheet.append(pl_.source)
                if on_sheet:
                    items = "".join(
                        f'<span class="legend-item">'
                        f'<span class="legend-swatch" '
                        f'style="background:{_source_colour(src, source_order)};"></span>'
                        f'{_esc((src_letters.get(src, "") + " · " if src_letters.get(src) else "") + src)}'
                        f'</span>'
                        for src in on_sheet
                    )
                    sheet_key = f'<div class="sheet-key">{items}</div>'
            sheet_svgs.append(
                f'<div class="sheet-card">'
                f'<h3>Sheet #{global_sheet_no} '
                f'<span class="dim">'
                f'({si + 1} of {sheets_count} in group) · '
                f'{opt.stock_sheet.length:.0f} × {opt.stock_sheet.width:.0f} mm '
                f'— {_esc(opt.stock_sheet.name)}</span></h3>'
                f'{sheet_key}'
                f'{_sheet_svg(opt.stock_sheet, by_sheet[si], id_map, (opt.cuts or {}).get(si))}'
                f'</div>'
            )

        notes_html = ""
        if opt.unplaced:
            names = ", ".join(opt.unplaced[:5])
            extra = f" + {len(opt.unplaced) - 5} more" if len(opt.unplaced) > 5 else ""
            notes_html += f'<p class="warn">⚠ Unplaced panels: {_esc(names)}{extra}</p>'
        if opt.grain_mismatched:
            names = ", ".join(opt.grain_mismatched[:5])
            notes_html += (
                f'<p class="warn">⚠ Grain-constrained panels rotated by optimizer '
                f'(verify orientation at saw): {_esc(names)}</p>'
            )

        tab_panes.append(
            f'<div class="tab-pane {active}" id="pane-{tab_idx}">'
            f'<div class="group-stats">'
            f'{sheets_count} sheet{"s" if sheets_count != 1 else ""} '
            + (f'(#{group_first_no}–#{global_sheet_no}) · '
               if sheets_count else '(—) · ')
            + f'{opt.waste_pct:.1f}% waste'
            f'</div>'
            f'{notes_html}'
            f'<div class="sheet-grid">{"".join(sheet_svgs)}</div>'
            f'</div>'
        )

    # ── Hardware BOM tab (optional) ────────────────────────────────────────────
    if hardware_lines:
        from .hardware import price_for
        bom_idx = len(tab_buttons)
        tab_buttons.append(
            f'<button class="tab-btn" onclick="showTab({bom_idx})" id="btn-{bom_idx}">'
            f'Hardware BOM</button>'
        )
        cat_order = {"pull": 0, "slide": 1, "hinge": 2, "leg": 3}
        sorted_hw = sorted(hardware_lines, key=lambda h: (cat_order.get(h.category, 9), h.name))
        hw_project_mode = any(h.source_counts for h in hardware_lines)
        hw_total = 0.0
        rows_list = []
        for h in sorted_hw:
            unit = h.unit_price
            line_total = round(h.packs_to_order * unit, 2)
            hw_total += line_total
            if hw_project_mode:
                breakdown = ", ".join(
                    f"{_esc(src)} ×{n}" for src, n in h.source_counts.items()
                ) or "—"
                proj_td = f'<td>{breakdown}</td>'
            else:
                proj_td = ""
            rows_list.append(
                f'<tr>'
                f'<td>{_esc(h.category.title())}</td>'
                f'<td>{_esc(h.name)}</td>'
                f'<td>{_esc(h.brand)}</td>'
                f'<td>{_esc(h.model_number)}</td>'
                f'{proj_td}'
                f'<td style="text-align:center">{h.pieces_needed}</td>'
                f'<td style="text-align:center">{h.pack_quantity}</td>'
                f'<td style="text-align:center;font-weight:600">{h.packs_to_order}</td>'
                f'<td style="text-align:right">{("$%.2f" % unit) if unit else "—"}</td>'
                f'<td style="text-align:right;font-weight:600">{("$%.2f" % line_total) if line_total else "—"}</td>'
                f'<td style="text-align:center">{h.leftover if h.leftover else "—"}</td>'
                f'<td>{_esc(h.notes)}</td>'
                f'</tr>'
            )
        rows = "".join(rows_list)
        total_span = 9 if hw_project_mode else 8
        proj_th = '<th>Project</th>' if hw_project_mode else ''
        total_row = (
            f'<tr style="border-top:2px solid #888;font-weight:600">'
            f'<td colspan="{total_span}" style="text-align:right">Hardware total (list prices):</td>'
            f'<td style="text-align:right">${hw_total:.2f}</td>'
            f'<td colspan="2"></td>'
            f'</tr>'
        )
        bom_table = (
            f'<table class="bom-tbl">'
            f'<thead><tr>'
            f'<th>Category</th><th>Name</th><th>Brand</th><th>Model #</th>'
            f'{proj_th}'
            f'<th>Needed</th><th>Pack&nbsp;Qty</th><th>Packs&nbsp;to&nbsp;Order</th>'
            f'<th>Unit&nbsp;Price</th><th>Line&nbsp;Total</th>'
            f'<th>Leftover</th><th>Notes</th>'
            f'</tr></thead>'
            f'<tbody>{rows}{total_row}</tbody>'
            f'</table>'
        )
        tab_panes.append(
            f'<div class="tab-pane" id="pane-{bom_idx}">{bom_table}</div>'
        )

    tabs_html = "\n".join(tab_buttons)
    panes_html = "\n".join(tab_panes)

    # ── Legend ─────────────────────────────────────────────────────────────────
    # Single project: panel name → colour. Batch: PROJECT → colour, so the
    # visual grouping on every sheet reads at a glance.
    seen: dict[str, str] = {}
    if project_mode:
        for src in source_order:
            letter = src_letters.get(src, "")
            label = f"{letter} · {src}" if letter else src
            seen[label] = _source_colour(src, source_order)
    else:
        for _, panels, opt in groups:
            for p in opt.placements:
                if p.panel_name not in seen:
                    seen[p.panel_name] = _panel_colour(p.panel_name)
    legend_title = "Projects: " if project_mode else ""
    legend_items = legend_title + "".join(
        f'<span class="legend-item">'
        f'<span class="legend-swatch" style="background:{col};"></span>'
        f'{_esc(name)}</span>'
        for name, col in seen.items()
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_esc(cabinet_name)} — Sheet Layout</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#f0ede8;color:#222;padding:16px}}
h1{{font-size:1.3rem;font-weight:600;margin-bottom:12px}}
.tabs{{display:flex;gap:6px;margin-bottom:0;flex-wrap:wrap}}
.tab-btn{{
  padding:7px 16px;border:1px solid #bbb;border-bottom:none;
  background:#e0dbd4;border-radius:6px 6px 0 0;cursor:pointer;
  font-size:.85rem;color:#444;
}}
.tab-btn.active{{background:#fff;border-color:#888;color:#111;font-weight:600}}
.tab-pane{{display:none;background:#fff;border:1px solid #888;
  border-radius:0 6px 6px 6px;padding:16px}}
.tab-pane.active{{display:block}}
.group-stats{{font-size:.85rem;color:#555;margin-bottom:10px}}
.sheet-grid{{display:flex;flex-direction:column;gap:24px}}
.sheet-card h3{{font-size:.9rem;font-weight:600;margin-bottom:6px;color:#333}}
.dim{{font-weight:400;color:#777;font-size:.8rem}}
.warn{{color:#b55;font-size:.85rem;margin-bottom:8px}}
.legend{{margin-top:20px;padding-top:12px;border-top:1px solid #ddd}}
.legend h2{{font-size:.85rem;font-weight:600;color:#555;margin-bottom:6px}}
.sheet-key{{margin:2px 0 8px;font-size:.78rem;color:#333}}
.sheet-key .legend-swatch{{width:11px;height:11px}}
.legend-item{{display:inline-flex;align-items:center;gap:5px;
  margin:3px 8px 3px 0;font-size:.78rem;color:#333}}
.legend-swatch{{width:14px;height:14px;border-radius:2px;
  border:1px solid rgba(0,0,0,.15);flex-shrink:0}}
.bom-tbl{{width:100%;border-collapse:collapse;font-size:.82rem}}
.bom-tbl th{{background:#2c3e50;color:#fff;padding:6px 8px;text-align:left;font-weight:600}}
.bom-tbl td{{padding:5px 8px;border-bottom:1px solid #e0e0e0}}
.bom-tbl tr:nth-child(even) td{{background:#f7f7f7}}
.bom-tbl tr:hover td{{background:#eef4fb}}
</style>
</head>
<body>
<h1>{_esc(cabinet_name)} — Cut Sheet Layout</h1>
<div class="tabs">{tabs_html}</div>
{panes_html}
<div class="legend">
<h2>Panel legend</h2>
{legend_items}
</div>
<script>
function showTab(n){{
  document.querySelectorAll('.tab-btn').forEach((b,i)=>b.classList.toggle('active',i===n));
  document.querySelectorAll('.tab-pane').forEach((p,i)=>p.classList.toggle('active',i===n));
}}
</script>
</body>
</html>"""


def _parts_table(panels: list["CutlistPanel"], content_width: float,
                 source_letters: dict | None = None):
    """Cut-parts table in Charlie's approved bench format (2026-08-02).

    Per part: a bold METRIC row with scannable L/W/T columns, a grey
    imperial sub-row beneath it, and — when banding markers or notes
    exist — a spanning grey note row. Multi-project batches get spanning
    "Project X — name" section header rows instead of a Project column
    (part IDs carry the letter anyway). Requires reportlab; only call
    behind a ``_REPORTLAB_AVAILABLE`` check.
    """
    from xml.sax.saxutils import escape as _esc

    styles = _getSampleStyleSheet()
    name_sty = _ParagraphStyle("pt_name", parent=styles["Normal"],
                               fontSize=10, leading=12.5)
    dim_sty = _ParagraphStyle("pt_dim", parent=styles["Normal"],
                              fontSize=10.5, leading=13, alignment=1)
    sub_sty = _ParagraphStyle("pt_sub", parent=styles["Normal"],
                              fontSize=8.5, leading=10, alignment=1,
                              textColor=_HexColor("#666666"))
    sub_l_sty = _ParagraphStyle("pt_subl", parent=sub_sty, alignment=0)
    note_sty = _ParagraphStyle("pt_note", parent=styles["Normal"],
                               fontSize=8.5, leading=10.5,
                               textColor=_HexColor("#555555"))

    def _m(text: str):
        return _Paragraph(f"<b>{_esc(text)}</b>", dim_sty)

    def _i(text: str):
        return _Paragraph(_esc(text), sub_sty)

    data: list[list] = [["ID", "Part", "L (mm)", "W (mm)", "T (mm)",
                         "Qty", "Material"]]
    cmds: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), _HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), _HexColor("#ffffff")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("ALIGN", (2, 0), (5, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, _HexColor("#2c3e50")),
    ]

    current_source = object()   # sentinel ≠ any source incl. ""
    shade = False
    for p in panels:
        if source_letters and p.source != current_source:
            current_source = p.source
            r = len(data)
            letter = source_letters.get(p.source, "")
            label = (f"Project {letter} — {p.source}" if letter
                     else (p.source or "Unassigned"))
            data.append([_Paragraph(f"<b>{_esc(label)}</b>", name_sty),
                         "", "", "", "", "", ""])
            cmds += [("SPAN", (0, r), (-1, r)),
                     ("BACKGROUND", (0, r), (-1, r), _HexColor("#dfe6ec")),
                     ("TOPPADDING", (0, r), (-1, r), 5),
                     ("BOTTOMPADDING", (0, r), (-1, r), 5)]
            shade = False

        r0 = len(data)
        data.append([
            p.part_id or "—",
            _Paragraph(_esc(p.name), name_sty),
            _m(f"{p.length:.0f}"), _m(f"{p.width:.0f}"),
            _m(f"{p.thickness:g}"),
            p.quantity, _Paragraph(
                _esc(p.material.replace("_", " ").title()), name_sty),
        ])
        data.append([
            "", _Paragraph("in", sub_l_sty),
            _i(_inch_frac(p.length)), _i(_inch_frac(p.width)),
            _i(_thickness_imperial(p.thickness).replace('"', "")),
            "", "",
        ])
        note_bits = []
        if p.edge_band:
            note_bits.append("band: " + ", ".join(p.edge_band))
        if p.notes:
            note_bits.append(p.notes)
        if note_bits:
            r = len(data)
            data.append(["", _Paragraph(_esc(" — ".join(note_bits)),
                                        note_sty), "", "", "", "", ""])
            cmds.append(("SPAN", (1, r), (-1, r)))
        r1 = len(data) - 1
        if shade:
            cmds.append(("BACKGROUND", (0, r0), (-1, r1),
                         _HexColor("#f5f5f5")))
        shade = not shade
        cmds.append(("LINEBELOW", (0, r1), (-1, r1), 0.5,
                     _HexColor("#cccccc")))

    col_w = [content_width * x
             for x in (0.11, 0.30, 0.12, 0.12, 0.09, 0.06, 0.20)]
    tbl = _Table(data, colWidths=col_w, repeatRows=1)
    tbl.setStyle(_TableStyle(cmds))
    return tbl


def generate_parts_list_pdf(
    panels: list["CutlistPanel"],
    cabinet_name: str = "Cabinet",
    paper: str = "letter",
    subtitle: str = "",
    source_letters: dict | None = None,
) -> bytes:
    """Standalone portrait cut-parts document — the page taped to the saw.

    Same rows as the layout PDF's Cut Parts List section (bold metric row,
    grey imperial sub-row, spanning note rows), portrait ``paper`` (US
    Letter default), one table, no sheet drawings. ``subtitle`` prints
    under the title (batch provenance, part-ID notes). Raises
    ``ImportError`` without reportlab.
    """
    if not _REPORTLAB_AVAILABLE:
        raise ImportError(
            "reportlab is required for PDF export. "
            "Install with: uv pip install reportlab"
        )
    import io
    from datetime import date
    from xml.sax.saxutils import escape as _esc

    PAGE = _paper_size(paper)
    MARGIN = 14 * _rl_mm
    CW = PAGE[0] - 2 * MARGIN

    styles = _getSampleStyleSheet()
    title_sty = _ParagraphStyle("plt", parent=styles["Title"], fontSize=18,
                                leading=22, spaceAfter=2 * _rl_mm)
    norm_sty = _ParagraphStyle("pln", parent=styles["Normal"], fontSize=10,
                               leading=13, spaceAfter=1 * _rl_mm)

    total_pieces = sum(p.quantity for p in panels)
    head = (f"{len(panels)} part rows / {total_pieces} pieces · metric bold, "
            f"imperial beneath · generated {date.today().isoformat()}.")

    buf = io.BytesIO()
    doc = _SimpleDocTemplate(
        buf, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title=f"Cut Parts — {cabinet_name}")
    story = [_Paragraph(f"{_esc(cabinet_name)} — Cut Parts List",
                        title_sty)]
    if subtitle:
        story.append(_Paragraph(_esc(subtitle), norm_sty))
    story.append(_Paragraph(head, norm_sty))
    story.append(_Spacer(1, 3 * _rl_mm))
    story.append(_parts_table(panels, CW, source_letters=source_letters))
    doc.build(story)
    return buf.getvalue()


def generate_sheet_layout_pdf(
    groups: list[tuple[str, list["CutlistPanel"], "OptimizationResult"]],
    cabinet_name: str = "Cabinet",
    kerf: float = 3.2,
    hardware_lines: "list[HardwareLine] | None" = None,
    paper: str = "letter",
) -> bytes:
    """Generate a PDF cutlist document with sheet layouts and parts list.

    Parameters
    ----------
    groups:
        List of ``(label, panels, opt_result)`` tuples — one per thickness
        group.  Same format as :func:`generate_sheet_layout_html`.
    cabinet_name:
        Used in the document title.
    kerf:
        Saw kerf in mm (shown in the header).
    paper:
        "letter" (default) or "a4"; the document is landscape either way.

    Returns
    -------
    bytes
        Raw PDF bytes ready to write to a file.

    Raises
    ------
    ImportError
        If ``reportlab`` is not installed.
    """
    if not _REPORTLAB_AVAILABLE:
        raise ImportError(
            "reportlab is required for PDF export. "
            "Install with: uv pip install reportlab"
        )

    from datetime import date as _date
    from xml.sax.saxutils import escape as _xml_escape

    # cabinet_name / group_label are user-provided and flow into reportlab
    # Paragraphs, which parse their text as inline XML markup — an unescaped
    # "<" or a tag-like name (e.g. "Cab <b>bold") raises a paraparser error
    # and aborts the whole PDF. Escape before interpolation.
    safe_cabinet_name = _xml_escape(cabinet_name)

    PAGE = _rl_landscape(_paper_size(paper))
    MARGIN = 15 * _rl_mm
    CW = PAGE[0] - 2 * MARGIN   # usable content width

    styles = _getSampleStyleSheet()

    title_sty = _ParagraphStyle("ct", parent=styles["Title"],
                                fontSize=18, leading=22, spaceAfter=3 * _rl_mm)
    h1_sty    = _ParagraphStyle("ch1", parent=styles["Heading1"],
                                fontSize=12, leading=15, spaceBefore=4 * _rl_mm, spaceAfter=2 * _rl_mm)
    h2_sty    = _ParagraphStyle("ch2", parent=styles["Heading2"],
                                fontSize=9, leading=12, spaceBefore=2 * _rl_mm, spaceAfter=1.5 * _rl_mm)
    norm_sty  = _ParagraphStyle("cn", parent=styles["Normal"],
                                fontSize=8.5, leading=11)
    small_sty = _ParagraphStyle("cs", parent=styles["Normal"],
                                fontSize=7.5, leading=10)
    # Free-text table cells (material names, notes, per-project breakdowns)
    # must be Paragraphs: reportlab never wraps plain strings, so long text
    # overprints neighbouring columns or runs off the page.
    cell_sty  = _ParagraphStyle("ccell", parent=styles["Normal"],
                                fontSize=7.5, leading=9)
    cell_sm_sty = _ParagraphStyle("ccellsm", parent=styles["Normal"],
                                  fontSize=6.5, leading=8)

    def _wrap_cell(text: str, small: bool = False):
        return _Paragraph(_xml_escape(text), cell_sm_sty if small else cell_sty)

    def _tbl_style(small: bool = False, align_right_from: int = 1) -> _TableStyle:
        fs = 7.5 if small else 9
        return _TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0),  _HexColor("#2c3e50")),
            ("TEXTCOLOR",     (0, 0), (-1, 0),  _HexColor("#ffffff")),
            ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), fs),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [_HexColor("#f5f5f5"), _HexColor("#ffffff")]),
            ("GRID",          (0, 0), (-1, -1), 0.5, _HexColor("#cccccc")),
            ("ALIGN",         (0, 0), (0,  -1), "LEFT"),
            ("ALIGN",         (align_right_from, 0), (-1, -1), "CENTER"),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        ])

    buf = io.BytesIO()
    doc = _SimpleDocTemplate(
        buf,
        pagesize=PAGE,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title=f"Cutlist — {cabinet_name}",
    )

    # Multi-project batches colour panels by originating project and add a
    # Project column to the parts / hardware tables.
    source_order: list[str] = []
    for _, _pnls, _opt in groups:
        for _p in _pnls:
            if _p.source and _p.source not in source_order:
                source_order.append(_p.source)
    project_mode = bool(source_order)
    _fill_for = (
        (lambda pl: _source_colour(pl.source, source_order))
        if project_mode else None
    )

    story = []

    # ── Page 1: summary ───────────────────────────────────────────────────────
    story.append(_Paragraph(f"Cutlist — {safe_cabinet_name}", title_sty))
    story.append(_Paragraph(
        f"Generated {_date.today().isoformat()} · Kerf: {kerf} mm", norm_sty
    ))
    if project_mode:
        pdf_letters = _source_letters_from_groups(groups)
        legend = " &nbsp;·&nbsp; ".join(
            f'<font color="{_source_colour(s, source_order)}">◼</font> '
            f"{_xml_escape((pdf_letters.get(s, '') + ' · ' if pdf_letters.get(s) else '') + s)}"
            for s in source_order
        )
        story.append(_Paragraph(f"Projects: {legend}", norm_sty))
    story.append(_Spacer(1, 5 * _rl_mm))

    # Sheet goods table
    story.append(_Paragraph("Sheet Goods Required", h1_sty))
    sg_data = [["Material", "Thickness", "Sheets", "Sheet #s", "Waste", "Unplaced"]]
    _no = 0
    for label, _pnls, result in groups:
        mat = result.stock_sheet.material.replace("_", " ").title()
        first, _no = _no + 1, _no + result.sheets_used
        sg_data.append([
            _wrap_cell(f"{label}  ({mat})"),
            f"{result.stock_sheet.thickness:.0f} mm",
            str(result.sheets_used),
            ("—" if result.sheets_used == 0
             else f"#{first}–#{_no}" if _no > first else f"#{first}"),
            f"{result.waste_pct:.1f}%",
            str(len(result.unplaced)) if result.unplaced else "—",
        ])
    sg_col_w = [CW * x for x in (0.36, 0.14, 0.10, 0.14, 0.13, 0.13)]
    sg_tbl = _Table(sg_data, colWidths=sg_col_w)
    sg_tbl.setStyle(_tbl_style())
    story.append(sg_tbl)
    story.append(_Spacer(1, 5 * _rl_mm))

    # Cut parts table — same bench format as generate_parts_list_pdf
    # (bold metric row + grey imperial sub-row + spanning note rows;
    # Charlie's pick, 2026-08-02). On this landscape page the table just
    # gets wider columns.
    story.append(_Paragraph("Cut Parts List", h1_sty))
    all_panels: list[CutlistPanel] = []
    for _, pnls, _ in groups:
        all_panels.extend(pnls)
    all_panels.sort(key=lambda p: (p.source, p.thickness, p.material, p.name))
    story.append(_parts_table(
        all_panels, CW,
        source_letters=_source_letters_from_groups(groups)
        if project_mode else None))

    # ── Sheet layout pages ────────────────────────────────────────────────────
    HEADER_RESERVE = 28 * _rl_mm    # space for title + subtitle above drawing
    CUT_TABLE_RESERVE = 50 * _rl_mm # space below drawing for cut-sequence table (~8 rows)
    DRAW_H = PAGE[1] - 2 * MARGIN - HEADER_RESERVE - CUT_TABLE_RESERVE

    pdf_src_letters = _source_letters_from_groups(groups)
    pdf_sheet_no = 0

    for group_label, _pnls, result in groups:
        by_sheet: dict[int, list[Placement]] = {}
        for pl in result.placements:
            by_sheet.setdefault(pl.sheet_index, []).append(pl)

        for sheet_idx in sorted(by_sheet):
            pdf_sheet_no += 1
            story.append(_PageBreak())
            pls = by_sheet[sheet_idx]

            story.append(_Paragraph(
                f"Sheet #{pdf_sheet_no} — {_xml_escape(group_label)} "
                f"({sheet_idx + 1} of {result.sheets_used})",
                h1_sty,
            ))
            warn = ""
            if result.grain_mismatched:
                warn = f" · ⚠ {len(result.grain_mismatched)} grain mismatch(es)"
            story.append(_Paragraph(
                f"{result.stock_sheet.length:.0f} × {result.stock_sheet.width:.0f} mm "
                + "(" + _inch_frac(result.stock_sheet.length) + '\" × '
                + _inch_frac(result.stock_sheet.width) + '\") '
                f"· Waste: {result.waste_pct:.1f}%{warn}",
                norm_sty,
            ))
            if project_mode:
                on_sheet: list[str] = []
                for pl_ in pls:
                    if pl_.source and pl_.source not in on_sheet:
                        on_sheet.append(pl_.source)
                if on_sheet:
                    key = " &nbsp;·&nbsp; ".join(
                        f'<font color="{_source_colour(src, source_order)}">◼</font> '
                        + _xml_escape(
                            (pdf_src_letters.get(src, "") + " · "
                             if pdf_src_letters.get(src) else "") + src)
                        for src in on_sheet
                    )
                    story.append(_Paragraph(f"On this sheet: {key}", norm_sty))
            story.append(_Spacer(1, 2 * _rl_mm))

            story.append(_SheetDrawingFlowable(
                pls, result.stock_sheet, kerf, CW, DRAW_H, fill_for=_fill_for,
                id_map=_group_id_map(_pnls),
                preset_cuts=(result.cuts or {}).get(sheet_idx),
            ))

            # Cut-sequence table
            preset = (result.cuts or {}).get(sheet_idx)
            raw_cuts = list(preset) if preset is not None else []
            if preset is None:
                _guillotine_cuts(pls, 0, 0, result.stock_sheet.length,
                                 result.stock_sheet.width, depth=0,
                                 out=raw_cuts)
            # Same key as the HTML renderer — (depth, position) — so
            # "cut #N" names the same physical cut in every document
            # (review 2026-07-29 M6).
            raw_cuts.sort(key=lambda e: (e[0], e[1]))
            seq = 0
            cut_data = [["#", "Type", "Set fence to (shorter piece)"]]
            for entry in raw_cuts:
                if entry[7]:   # is_breakdown
                    seq += 1
                    orient = entry[2]
                    dim_a, dim_b = entry[8], entry[9]
                    cut_data.append([
                        str(seq),
                        "Rip" if orient == "h" else "Cross-cut",
                        _Paragraph(
                            f"<b>{min(dim_a, dim_b):.0f} mm</b> ("
                            + _xml_escape(_inch_frac(min(dim_a, dim_b)))
                            + '\")', norm_sty),
                    ])
            if len(cut_data) > 1:
                cut_col_w = [CW * x for x in (0.06, 0.20, 0.74)]
                cut_tbl = _Table(cut_data, colWidths=cut_col_w)
                cut_tbl.setStyle(_tbl_style(small=True))
                story.append(_KeepTogether([
                    _Spacer(1, 3 * _rl_mm),
                    _Paragraph("Cut Sequence", h2_sty),
                    cut_tbl,
                ]))

    # ── Hardware BOM page (optional) ──────────────────────────────────────────
    if hardware_lines:
        story.append(_PageBreak())
        story.append(_Paragraph("Hardware BOM", h1_sty))
        story.append(_Paragraph(
            "Quantities include procurement math based on pack size.", norm_sty
        ))
        story.append(_Spacer(1, 3 * _rl_mm))

        from .hardware import price_for
        cat_order = {"pull": 0, "slide": 1, "hinge": 2, "leg": 3}
        sorted_hw = sorted(hardware_lines, key=lambda h: (cat_order.get(h.category, 9), h.name))

        hw_project_mode = any(h.source_counts for h in hardware_lines)
        hw_header = ["Category", "Name", "Brand", "Model #",
                     "Needed", "Pack Qty", "Packs", "Unit $", "Line $",
                     "Leftover", "Notes"]
        if hw_project_mode:
            hw_header.insert(4, "Project")
        hw_data = [hw_header]
        hw_total = 0.0
        for h in sorted_hw:
            unit = h.unit_price
            line_total = round(h.packs_to_order * unit, 2)
            hw_total += line_total
            row = [
                _wrap_cell(h.category.title(), small=True),
                _wrap_cell(h.name, small=True),
                _wrap_cell(h.brand, small=True),
                _wrap_cell(h.model_number, small=True),
                str(h.pieces_needed),
                str(h.pack_quantity),
                str(h.packs_to_order),
                f"${unit:.2f}" if unit else "—",
                f"${line_total:.2f}" if line_total else "—",
                str(h.leftover) if h.leftover else "—",
                _wrap_cell(h.notes, small=True) if h.notes else "—",
            ]
            if hw_project_mode:
                breakdown = ", ".join(
                    f"{src} ×{n}" for src, n in h.source_counts.items()
                )
                row.insert(4, _wrap_cell(breakdown, small=True) if breakdown else "—")
            hw_data.append(row)
        total_pad = [""] * (5 if hw_project_mode else 4)
        hw_data.append(total_pad + ["", "", "", "Total:", f"${hw_total:.2f}", "", ""])
        if hw_project_mode:
            hw_col_w = [CW * x for x in
                        (0.07, 0.15, 0.08, 0.10, 0.13, 0.05, 0.05, 0.05, 0.06, 0.07, 0.06, 0.13)]
        else:
            hw_col_w = [CW * x for x in
                        (0.08, 0.18, 0.10, 0.11, 0.06, 0.06, 0.06, 0.07, 0.08, 0.07, 0.13)]
        hw_tbl = _Table(hw_data, colWidths=hw_col_w, repeatRows=1)
        hw_tbl.setStyle(_tbl_style(small=True))
        story.append(hw_tbl)

    doc.build(story)
    return buf.getvalue()


if _REPORTLAB_AVAILABLE:
    # Defined only when reportlab is importable — _Flowable does not
    # exist in lite mode, and this class is only reachable from
    # generate_sheet_layout_pdf (which raises ImportError without it).
    class _SheetDrawingFlowable(_Flowable):
        """Platypus Flowable that renders a single sheet layout using the canvas."""

        def __init__(
            self,
            placements: list["Placement"],
            stock: "SheetStock",
            kerf: float,
            avail_w: float,
            avail_h: float,
            fill_for=None,
            id_map=None,
            preset_cuts=None,
        ) -> None:
            super().__init__()
            self._pl = placements
            self._stock = stock
            self._kerf = kerf
            self.width = avail_w
            self.height = avail_h
            # Optional Placement → hex-colour override (batch mode colours
            # by project); default is the per-panel-name palette.
            self._fill_for = fill_for or (lambda p: _panel_colour(p.panel_name))
            self._id_map = id_map or {}
            self._preset_cuts = preset_cuts

        def draw(self) -> None:
            canvas = self.canv
            sl, sw = self._stock.length, self._stock.width

            scale = min(self.width / sl, self.height / sw)
            drawn_w = sl * scale
            drawn_h = sw * scale
            x_off = (self.width - drawn_w) / 2
            y_off = (self.height - drawn_h) / 2

            def sx(x_mm: float) -> float:
                return x_off + x_mm * scale

            def sy(y_mm: float, h_mm: float = 0.0) -> float:
                # SVG y-down → RL y-up
                return y_off + (sw - y_mm - h_mm) * scale

            # Sheet background
            canvas.setFillColor(_HexColor("#F5EED8"))
            canvas.setStrokeColor(_HexColor("#888888"))
            canvas.setLineWidth(0.5)
            canvas.rect(sx(0), sy(0, sw), drawn_w, drawn_h, fill=1, stroke=1)

            # Panels
            for p in self._pl:
                fc = self._fill_for(p)
                sc = _panel_colour_dark(fc)
                canvas.setFillColor(_HexColor(fc))
                canvas.setStrokeColor(_HexColor(sc))
                canvas.setLineWidth(0.4)

                px_pt = sx(p.x)
                py_pt = sy(p.y, p.placed_width)
                pw_pt = p.placed_length * scale
                ph_pt = p.placed_width * scale
                corner_pt = max(1.0, min(pw_pt, ph_pt) * 0.03)
                canvas.roundRect(px_pt, py_pt, pw_pt, ph_pt, corner_pt, fill=1, stroke=1)

                label = p.panel_name[:20] + ("…" if len(p.panel_name) > 20 else "")
                if p.rotated:
                    label += " ↺"
                pid = p.part_id or _placement_id(self._id_map, p)
                if pid:
                    label = f"{pid} · {label}"
                dim_text = f"{p.placed_length:.0f}×{p.placed_width:.0f}mm"

                min_dim_pt = min(pw_pt, ph_pt)
                font_pt = max(5.0, min(min_dim_pt * 0.12, 9.0))
                dim_pt  = max(4.0, min(min_dim_pt * 0.09, 7.0))

                cx_pt = px_pt + pw_pt / 2
                cy_pt = py_pt + ph_pt / 2
                tall  = p.placed_width > p.placed_length

                # Fit text to the panel's drawn extent: font size above is
                # derived from the short side only, so a long label on a
                # narrow panel spills into neighbours (or off the sheet)
                # unless measured and shrunk/truncated here.
                along_pt = (ph_pt if tall else pw_pt) - 4.0
                across_pt = (pw_pt if tall else ph_pt)
                while (font_pt > 4.0
                       and canvas.stringWidth(label, "Helvetica", font_pt) > along_pt):
                    font_pt -= 0.5
                if canvas.stringWidth(label, "Helvetica", font_pt) > along_pt:
                    while (len(label) > 1
                           and canvas.stringWidth(label + "…", "Helvetica",
                                                  font_pt) > along_pt):
                        label = label[:-1]
                    label += "…"
                show_label = along_pt > 0 and len(label.rstrip("…")) >= 2
                show_dims = (
                    show_label
                    and canvas.stringWidth(dim_text, "Helvetica", dim_pt) <= along_pt
                    and across_pt >= font_pt * 1.5 + dim_pt * 2.6
                )

                canvas.saveState()
                canvas.translate(cx_pt, cy_pt)
                if tall:
                    canvas.rotate(90)
                # Solid black labels (matches the HTML renderer).
                canvas.setFillColor(_HexColor("#000000"))
                if show_label:
                    canvas.setFont("Helvetica", font_pt)
                    if show_dims:
                        canvas.drawCentredString(0, font_pt * 0.25, label)
                        canvas.setFont("Helvetica", dim_pt)
                        canvas.drawCentredString(0, -dim_pt * 1.6, dim_text)
                    else:
                        canvas.drawCentredString(0, -font_pt * 0.35, label)
                canvas.restoreState()

            # Guillotine cut lines
            raw_cuts: list = (list(self._preset_cuts)
                              if self._preset_cuts is not None else [])
            if self._preset_cuts is None:
                _guillotine_cuts(self._pl, 0, 0, sl, sw, depth=0,
                                 out=raw_cuts)
            # Same key as the HTML renderer — (depth, position) — so
            # "cut #N" names the same physical cut in every document
            # (review 2026-07-29 M6).
            raw_cuts.sort(key=lambda e: (e[0], e[1]))

            label_r_pt = max(4.0, sl * 0.016 * scale)
            seq = 0

            for entry in raw_cuts:
                depth, pos, orient, x0, y0, x1, y1, is_breakdown, dim_a, dim_b = entry

                if is_breakdown:
                    seq += 1
                    lc = _HexColor("#c0392b")
                    lw = max(0.6, sl * 0.004 * scale)
                    dash = max(3.0, sl * 0.015 * scale)
                else:
                    lc = _HexColor("#aaaaaa")
                    lw = 0.3
                    dash = max(2.0, sl * 0.010 * scale)

                canvas.setStrokeColor(lc)
                canvas.setLineWidth(lw)
                canvas.setDash(dash, dash * 0.6)
                canvas.line(sx(x0), sy(y0), sx(x1), sy(y1))
                canvas.setDash()

                if is_breakdown:
                    if orient == "h":
                        bx = (sx(x0) + sx(x1)) / 2
                        by = sy(y0)
                    else:
                        bx = sx(x0)
                        by = (sy(y0) + sy(y1)) / 2

                    canvas.setFillColor(lc)
                    canvas.circle(bx, by, label_r_pt, fill=1, stroke=0)
                    canvas.setFillColor(_HexColor("#ffffff"))
                    canvas.setFont("Helvetica-Bold", max(4.0, label_r_pt * 1.1))
                    canvas.drawCentredString(bx, by - label_r_pt * 0.38, str(seq))

                    # Dimension label on the shorter side of the cut, on a
                    # solid white chip so it stays legible over panel fills
                    # and labels (Charlie, Jul 2026).
                    short_dim = min(dim_a, dim_b)
                    dim_label = f"{short_dim:.0f}mm"
                    dim_font_pt = max(4.0, label_r_pt * 0.85)
                    pad_pt = label_r_pt * 1.8
                    dim_w_pt = canvas.stringWidth(dim_label, "Helvetica", dim_font_pt)

                    def _chip_and_text(x_pt: float, y_pt: float) -> None:
                        # x_pt/y_pt = text baseline origin in the current frame
                        canvas.setFillColor(_HexColor("#ffffff"))
                        canvas.roundRect(x_pt - 1.5, y_pt - dim_font_pt * 0.30,
                                         dim_w_pt + 3.0, dim_font_pt * 1.35,
                                         1.5, fill=1, stroke=0)
                        canvas.setFillColor(lc)
                        canvas.setFont("Helvetica", dim_font_pt)
                        canvas.drawString(x_pt, y_pt, dim_label)

                    if orient == "h":
                        tx = bx + label_r_pt * 1.5
                        ty = by + (pad_pt if dim_a > dim_b else -pad_pt)
                        _chip_and_text(tx, ty)
                    else:
                        # Rotated label starts clear of the marker circle —
                        # centring it at the circle's edge hid the leading
                        # digits under the circle.
                        canvas.saveState()
                        canvas.translate(bx, by + label_r_pt * 1.5)
                        canvas.rotate(90)
                        _chip_and_text(0, -dim_font_pt * 0.35)
                        canvas.restoreState()

            # Bottom ruler
            canvas.setStrokeColor(_HexColor("#888888"))
            canvas.setLineWidth(0.4)
            ruler_y = sy(0, sw) - 1.0
            tick_font = max(4.0, min(sl * 0.014 * scale, 6.0))
            for tick_mm in range(0, int(sl) + 1, 200):
                tx = sx(tick_mm)
                canvas.line(tx, ruler_y, tx, ruler_y - 3.0)
                if tick_mm % 400 == 0:
                    canvas.setFillColor(_HexColor("#666666"))
                    canvas.setFont("Helvetica", tick_font)
                    canvas.drawCentredString(tx, ruler_y - 3.0 - tick_font, str(tick_mm))


def print_hardware_bom(lines: list[HardwareLine]) -> None:
    """Print a formatted hardware BOM table to console."""
    if not lines:
        print("(no hardware lines)")
        return
    print()
    print(f"{'SKU':<28} {'Cat':<6} {'Name':<32} "
          f"{'Need':>5} {'Pack':>5} {'Order':>6} {'Left':>5}")
    print("-" * 92)
    for l in lines:
        print(
            f"{l.sku:<28} {l.category:<6} {l.name[:32]:<32} "
            f"{l.pieces_needed:>5} {l.pack_quantity:>5} "
            f"{l.packs_to_order:>6} {l.leftover:>5}"
        )
    print()
    tot_pieces = sum(l.pieces_needed for l in lines)
    tot_packs  = sum(l.packs_to_order for l in lines)
    print(f"  {len(lines)} lines, {tot_pieces} pieces, {tot_packs} packs to order")
    print()
