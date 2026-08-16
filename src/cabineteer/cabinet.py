"""
Parametric base cabinet model.

Generates a frameless (Euro-style) base cabinet with:
- Two side panels with dados for shelves and rabbet for back
- Bottom panel in dados
- Fixed shelves in dados
- Back panel in rabbets
- Optional adjustable shelf pin holes (32mm system)

All dimensions in millimeters. The cabinet is oriented with:
- X axis: width (left to right)
- Y axis: depth (front to back)
- Z axis: height (bottom to top)
- Origin at front-bottom-left exterior corner
"""

import math
from dataclasses import dataclass, field
from typing import Optional

try:
    import cadquery as cq
except ImportError:
    cq = None  # allow import for type checking / planning without cadquery installed

from .hardware import DrawerSlideSpec, get_slide, LegSpec, get_leg, get_pull, MountStyle
from .pulls import HingeSide, door_pull_x_center
from .joinery import (
    CarcassJoinery,
    DrawerJoineryStyle,
    DominoSpec,
    PocketScrewSpec,
    BiscuitSpec,
    DowelSpec,
    DEFAULT_DOMINO,
    DEFAULT_POCKET_SCREW,
    DEFAULT_BISCUIT,
    DEFAULT_DOWEL,
)


#: Face overhang onto an interior column divider, per side.  Used as the
#: build default for ``build_multi_bay_cabinet(inner_overlay=...)`` AND by
#: evaluation.check_door_overlay_collisions as the share of a divider the
#: neighbouring column's faces claim — keep the two in sync via this constant
#: (an 18 mm divider splits 8 + 8 with a 2 mm reveal).
INNER_FACE_OVERLAY_MM: float = 8.0

#: build default for ``build_multi_bay_cabinet(face_gap=...)`` AND by
#: evaluation.check_edge_band_face_gap as the total vertical clearance
#: between adjacent faces that hot-melt banding growth eats into.
DEFAULT_FACE_GAP_MM: float = 4.0

#: How the back panel is held in the carcass — an axis independent of both
#: ``carcass_joinery`` (how the corners go together) and ``back_style``
#: (where the back's top edge lands).
#:
#:   "pocket"   — the legacy let-in back. Sides run full depth, everything
#:                else stops short, and the back is glued onto the rear edges
#:                it finds there. Nothing is machined. The only capture whose
#:                back sits OUTSIDE the top/bottom panels, so the only one
#:                where ``back_style`` changes what you see.
#:   "rabbet"   — a rabbet in the rear inner edge of the sides, top and
#:                bottom. The back drops in from BEHIND after glue-up and
#:                finishes flush with the case rear. Screw it instead of
#:                gluing it and the back comes out again.
#:   "half_lap" — both halves rabbeted to half the back's thickness so they
#:                lap. Twice the glue area of a plain rabbet, the panel is
#:                trapped fore-and-aft by the step rather than by glue alone,
#:                and the lap self-registers the panel square during glue-up.
#:                Still drops in from behind.
#:   "dado"     — a groove plowed in the inner faces of all four members,
#:                set in from the rear edge. The back SLIDES IN during
#:                glue-up and is trapped on four edges: nothing shows from
#:                any angle, no fasteners, and an unglued solid-wood back can
#:                float in the groove and move seasonally.
BACK_CAPTURES: tuple[str, ...] = ("pocket", "rabbet", "half_lap", "dado")

#: A half lap splits the back's thickness in two, so a thin back leaves
#: nothing to lap. Below this the evaluator rejects "half_lap".
HALF_LAP_MIN_BACK_MM: float = 9.0

#: Material a machined capture must leave standing: ``side_thickness −
#: back_rabbet_depth`` for the case member's wall, and the wall behind a
#: dado groove (``back_groove_setback``).
MIN_CAPTURE_WALL_MM: float = 6.0


@dataclass(frozen=True)
class OpeningConfig:
    """One opening (a single face-height zone) within a column stack.

    ``height_mm`` is the vertical space allocated to this opening.
    ``opening_type`` describes what fills it:
      "drawer"    — a drawer box on slides
      "door"      — a single swinging door
      "door_pair" — a matched pair of doors side-by-side
      "shelf"     — a fixed shelf
      "open"      — open compartment (no door/drawer)

    The optional override fields (all default to ``None``) let individual
    openings deviate from the cabinet-level defaults.  ``None`` means
    "inherit from the column or cabinet config".
    """
    height_mm: float
    opening_type: str
    hinge_key:      Optional[str]   = None
    hinge_side:     Optional[str]   = None   # "left" | "right"
    pull_key:       Optional[str]   = None
    num_doors:      Optional[int]   = None   # 1 or 2; only for door types
    door_thickness: Optional[float] = None
    bottom_thickness: Optional[float] = None  # drawer box bottom; only for drawers
    slide_key: Optional[str] = None  # drawer slide override; only for drawers

    def __post_init__(self) -> None:
        valid_types = {"drawer", "door", "door_pair", "shelf", "open"}
        if self.opening_type not in valid_types:
            raise ValueError(
                f"opening_type must be one of {sorted(valid_types)}, "
                f"got {self.opening_type!r}."
            )
        if not math.isfinite(self.height_mm) or self.height_mm <= 0:
            raise ValueError(
                f"height_mm must be a positive finite number, "
                f"got {self.height_mm!r}."
            )
        # Explicit per-opening overrides must be physically meaningful; a NaN
        # or non-positive thickness silently disables downstream checks.
        for fname in ("bottom_thickness", "door_thickness"):
            v = getattr(self, fname)
            if v is not None and (not math.isfinite(v) or v <= 0):
                raise ValueError(
                    f"{fname} must be a positive finite number, got {v!r}."
                )
        if self.num_doors is not None and self.num_doors not in (1, 2):
            raise ValueError(f"num_doors must be 1 or 2, got {self.num_doors!r}.")


@dataclass(frozen=True)
class ColumnConfig:
    """One vertical column within a single cabinet carcass.

    A cabinet may have multiple side-by-side columns separated by interior
    vertical dividers — e.g. a left column of three drawers next to a right
    column with a single door.

    ``width_mm`` is the **interior** width of the column opening (not
    including adjacent divider panel thickness).  The sum of all column
    widths plus ``(n_columns − 1) × side_thickness`` must equal the
    cabinet's ``interior_width``; the evaluator enforces this.

    ``fixed_shelf_positions`` holds heights (from the cabinet's exterior
    bottom to shelf bottom) for fixed shelves local to this column.
    """
    width_mm: float
    openings: tuple[OpeningConfig, ...]  # stacked bottom-to-top
    fixed_shelf_positions: tuple[float, ...] = ()


_BAND_STOCK_KEYS = {"width_mm", "length_mm", "price_usd", "strip_width_mm"}


def normalize_band_stock(spec: dict | None) -> dict | None:
    """Validated copy of an ``edge_band_stock`` spec with defaults applied.

    ``None``/empty stays ``None`` (unpriced rip-from-offcuts). Board
    thickness is NOT a key — it is ``edge_band_thickness_mm``, so the stock
    and the band can never disagree. Raises ``ValueError`` on unknown keys
    or non-positive values so bad specs fail at design time, not in the BOM.
    """
    if not spec:
        return None
    unknown = set(spec) - _BAND_STOCK_KEYS
    if unknown:
        raise ValueError(
            f"edge_band_stock: unknown key(s) {sorted(unknown)}. "
            f"Valid keys: {sorted(_BAND_STOCK_KEYS)}."
        )
    try:
        out = {
            "width_mm": float(spec["width_mm"]),
            "length_mm": float(spec["length_mm"]),
            "price_usd": float(spec["price_usd"]),
            "strip_width_mm": float(spec.get("strip_width_mm", 20.0)),
        }
    except (KeyError, TypeError, ValueError):
        raise ValueError(
            "edge_band_stock needs numeric width_mm, length_mm and "
            "price_usd (strip_width_mm optional, default 20)."
        ) from None
    bad = [k for k, v in out.items() if not math.isfinite(v) or v <= 0]
    if bad:
        raise ValueError(f"edge_band_stock: {', '.join(sorted(bad))} must be "
                         "positive finite numbers.")
    return out


@dataclass
class CabinetConfig:
    """Configuration for a base cabinet."""

    # Overall exterior dimensions
    width: float = 600.0
    height: float = 720.0
    depth: float = 550.0

    # Materials
    side_thickness: float = 18.0  # 3/4" Baltic birch
    bottom_thickness: float = 18.0
    top_thickness: float = 18.0
    shelf_thickness: float = 18.0
    back_thickness: float = 6.0  # 1/4" plywood
    # Drawer box stock (sides + sub-front/back).  Bottoms are governed by the
    # per-drawer bottom_thickness option / size rule in DrawerConfig.
    drawer_box_thickness: float = 15.0  # 5/8" Baltic birch
    # Build drawer boxes (incl. bottoms) from pre-finished Baltic birch —
    # UV-coated both faces, no finishing step after assembly.  Affects the
    # cutlist material designation and sheet pricing only; geometry is
    # identical.  Workshop presets default this on.
    drawer_box_prefinished: bool = False
    # Cutlist material for the show faces — applied false fronts AND door
    # panels.  Sheet materials (the Baltic birch stocks, or any name ending
    # in "_ply") pool into the sheet optimisation; any other string (default
    # "finished_wood") keeps the faces as a labeled order-out group excluded
    # from sheet packing.  Geometry unaffected.
    face_material: str = "finished_wood"
    # Cutlist material for the carcass panels — sides, top, bottom, shelves,
    # and column dividers.  Backs and drawer boxes keep their own stock
    # (back is 1/4" ply, boxes follow drawer_box_thickness/_prefinished).
    # Sheet groups are packed per (material, thickness) and priced from
    # PRICE_LIST when a matching sheet entry exists (price TBD otherwise).
    carcass_material: str = "baltic_birch"
    # Edge banding for exposed ply edges (cutlist/BOM only — no 3D effect).
    # "hot_melt": pre-glued iron-on veneer applied after assembly; cut dims
    # unchanged. "hardwood": solid strips (typ. 3.2–6.4 mm) glued before
    # joinery; panel CORES are cut smaller by the band thickness per banded
    # edge so FINISHED dims (and face reveals) hold. Banded edges: fronts of
    # sides/top/bottom/shelves/dividers + all four edges of false fronts and
    # door leaves. All three are SharedDesign tokens.
    edge_band_mode: str = "none"          # none | hot_melt | hardwood
    edge_band_thickness_mm: float = 0.6   # hardwood: 3.2–6.4 typical
    edge_band_material: str = ""          # "" → derive from panel material
    # Purchasable strip stock for hardwood mode (None → unpriced rip-from-
    # offcuts line). Keys: width_mm (board width the strips rip from),
    # length_mm (board length = strip length), price_usd (per board),
    # strip_width_mm (optional, default 20 — covers an 18 mm edge proud).
    # Board thickness IS edge_band_thickness_mm, so the two can't disagree.
    # SharedDesign token; the cutlist packs actual edge lengths into strips
    # and prices boards-to-order from it.
    edge_band_stock: dict | None = None

    # Joinery
    dado_depth: float = 9.0  # half thickness dado for shelves/bottom
    back_rabbet_width: float = 9.0  # rabbet width for back panel
    back_rabbet_depth: float = 6.0  # matches back_thickness

    # Shelves
    fixed_shelf_positions: list[float] = field(default_factory=list)
    # Heights from cabinet bottom (exterior) to shelf bottom

    # Adjustable shelves (32mm system)
    adj_shelf_holes: bool = False
    shelf_pin_diameter: float = 5.0
    shelf_pin_depth: float = 10.0
    shelf_pin_row_inset: float = 37.0  # from front and back edges
    shelf_pin_start_z: float = 80.0  # first hole height from bottom
    shelf_pin_end_z: float = 640.0  # last hole height from bottom
    shelf_pin_spacing: float = 32.0  # 32mm system

    # Opening stack (from bottom up).  Used in single-column mode.
    # When ``columns`` is non-empty this field is ignored; each ColumnConfig
    # carries its own stack.
    openings: list[OpeningConfig] = field(default_factory=list)

    # Multi-column layout.  When non-empty, the cabinet interior is divided into
    # side-by-side vertical columns by interior dividers.
    # Column widths must sum to ``interior_width``; the evaluator checks this.
    columns: list[ColumnConfig] = field(default_factory=list)

    drawer_slide: str = "blum_tandem_550h"

    # Door hardware
    door_hinge: str = "blum_clip_top_blumotion_110_full"

    # Pull hardware (optional defaults).  These keys propagate down to drawers
    # and doors generated from this cabinet via ``drawers_from_cabinet_config``
    # and ``doors_from_cabinet_config`` — i.e. every drawer in this carcass
    # gets ``drawer_pull`` and every door gets ``door_pull`` unless the per-
    # drawer / per-door config overrides it.  ``None`` means no pull.
    drawer_pull: Optional[str] = None
    door_pull: Optional[str] = None
    door_hinge_side: HingeSide = "left"    # hinge side for single doors
    door_pull_inset_mm: float = 50.0       # gap from latch edge to pull body near-end

    # Leg / foot hardware (used by build_multi_bay_cabinet and design_legs)
    leg_key: str = "richelieu_176138106"
    leg_count: int = 4
    leg_inset: float = 30.0  # foot centre inset from cabinet edge (mm)

    # Carcass joinery method
    carcass_joinery: CarcassJoinery = CarcassJoinery.FLOATING_TENON
    # Exterior corner construction. "butt": top/bottom seat between the
    # sides (the default). "miter": the four exterior corners are 45°
    # miters — top/bottom are cut to FULL exterior width (long-point dims)
    # and all four panels get beveled ends; divider and shelf joints stay
    # butt tenons. Floating-tenon carcasses only. Also a SharedDesign token.
    carcass_corner_style: str = "butt"    # butt | miter
    # Back panel treatment (butt corners, non-dado joinery only).
    # "full_height": the back runs the full carcass height and its top edge
    # shows on the top plane (legacy cutlist convention). "under_top": the
    # top panel is cut to FULL depth (rear edge flush with the sides) and
    # the back stops at its underside — no back edge visible from above or
    # from the sides; the back slides into its pocket from the carcass's
    # bottom end during glue-up. Cutlist/assembly-doc effect; the 3D
    # viewer has always drawn under_top geometry. Also a SharedDesign token.
    back_style: str = "full_height"       # full_height | under_top
    # How the back is HELD — see BACK_CAPTURES. Independent of both
    # carcass_joinery and back_style; any combination is legal except the
    # ones check_back_capture rejects. Also a SharedDesign token.
    #
    # NOTE the three machined captures seat the back INSIDE the case
    # perimeter (rabbeted or grooved into the top and bottom as well as the
    # sides), so its top edge is covered whatever ``back_style`` says, and
    # the top and bottom both run full depth. back_style only changes what
    # you see under "pocket".
    back_capture: str = "pocket"          # pocket | rabbet | half_lap | dado
    # "dado" only: material left standing BEHIND the groove, i.e. how far
    # the back's rear face sits forward of the case's rear edge. Wants
    # enough meat that the wall doesn't blow out when the groove is plowed.
    back_groove_setback: float = 12.0

    # Drawer box corner joinery
    drawer_joinery: DrawerJoineryStyle = DrawerJoineryStyle.HALF_LAP

    # Per-method joinery specs (used when the matching joinery method is selected)
    domino_spec: DominoSpec = field(default_factory=lambda: DEFAULT_DOMINO)
    pocket_screw_spec: PocketScrewSpec = field(default_factory=lambda: DEFAULT_POCKET_SCREW)
    biscuit_spec: BiscuitSpec = field(default_factory=lambda: DEFAULT_BISCUIT)
    dowel_spec: DowelSpec = field(default_factory=lambda: DEFAULT_DOWEL)

    def __post_init__(self) -> None:
        """Normalize openings and column openings to OpeningConfig objects."""
        self.edge_band_stock = normalize_band_stock(self.edge_band_stock)
        self.openings = [to_opening(op) for op in self.openings]
        # Normalize per element — to_opening is a no-op for OpeningConfig, so
        # mixed tuples (OpeningConfig first, raw rows after) normalize too.
        self.columns = [
            ColumnConfig(
                width_mm=col.width_mm,
                openings=tuple(to_opening(op) for op in col.openings),
                fixed_shelf_positions=tuple(col.fixed_shelf_positions),
            )
            for col in self.columns
        ]

    # Derived / computed
    @property
    def interior_width(self) -> float:
        """Width between side panels."""
        return self.width - (self.side_thickness * 2)

    @property
    def interior_depth(self) -> float:
        """Depth from front edge to back panel face.

        A machined capture can sit the back further forward than the legacy
        allowance does — a dado holds it ``back_groove_setback`` in from the
        rear — so the usable depth follows the capture. Never returns MORE
        than the legacy allowance: drawer boxes and slides size off this,
        and a capture must not silently deepen boxes already in the shop.
        """
        legacy = self.depth - self.back_rabbet_width
        if getattr(self, "back_capture", "pocket") == "pocket":
            return legacy
        return min(legacy, self.depth - back_capture_geometry(self).clear_depth)

    @property
    def interior_height(self) -> float:
        """Height from top of bottom panel to underside of top panel."""
        return self.height - self.bottom_thickness - self.top_thickness

    @property
    def back_panel_width(self) -> float:
        """Back panel fits in rabbets on both sides."""
        return self.width - (self.side_thickness - self.back_rabbet_depth) * 2

    @property
    def back_panel_height(self) -> float:
        """Back panel height — spans from carcass floor to underside of top panel."""
        return self.height - self.top_thickness


# ─── Back capture geometry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class BackCapture:
    """Every dimension that follows from ``cfg.back_capture``.

    Single source of truth: the cutlist, the 3D panel makers, the evaluator
    and the assembly doc all read this rather than each re-deriving the
    back's geometry from the config. Distances are mm, measured from the
    carcass's REAR face unless stated.
    """
    capture: str
    thickness: float
    #: Cut size of the back panel itself.
    width: float
    height: float
    #: How far the back's edge runs into each case member (0 for "pocket",
    #: which laps onto their rear edges instead of into them).
    engagement: float
    #: What to machine in the sides / top / bottom: ``cut_depth`` into the
    #: inner FACE, ``cut_run`` along the depth, starting ``cut_offset`` in
    #: from the rear edge. All zero for "pocket".
    cut_depth: float
    cut_run: float
    cut_offset: float
    #: The back's OWN perimeter rabbet ("half_lap" only) — ``lap_depth``
    #: off its front face, ``lap_run`` in from each edge.
    lap_depth: float
    lap_run: float
    #: Where the back's rear face sits, forward of the carcass rear edge.
    setback: float
    #: Total depth consumed: carcass rear edge → the back's front face.
    #: Bottom, shelves and dividers stop at ``depth − clear_depth``.
    clear_depth: float
    #: Cut depth of the top and bottom panels.
    top_depth: float
    bottom_depth: float
    #: True when the back cannot be fitted after glue-up and must go in
    #: with the case — a grooved back slides in, and a capped pocket back
    #: comes up from the carcass's bottom end.
    captive: bool

    @property
    def machined(self) -> bool:
        """True when the capture cuts into the case members."""
        return self.capture != "pocket"


def back_capture_geometry(cfg: CabinetConfig) -> BackCapture:
    """Resolve ``cfg.back_capture`` into concrete dimensions.

    "pocket" keeps the legacy let-in geometry exactly: the back laps over
    the rear edges of the top and bottom, so it is the only capture where
    ``back_style`` changes anything. The three machined captures seat the
    back INSIDE the case perimeter — rabbeted or grooved into the top and
    bottom as well as the sides — so the top and bottom both run full depth
    and no back edge is visible from any angle.
    """
    capture = getattr(cfg, "back_capture", "pocket")
    t = float(cfg.back_thickness)
    depth = float(cfg.depth)

    if capture == "pocket":
        # Mitered tops are cut long-point on their own convention, so the
        # full-depth cap never applies to them — the evaluator errors on the
        # combination and the geometry must not half-apply it meanwhile.
        under_top = (cfg.back_style == "under_top"
                     and getattr(cfg, "carcass_corner_style", "butt") != "miter")
        return BackCapture(
            capture="pocket", thickness=t,
            width=cfg.interior_width,
            height=cfg.height - (cfg.top_thickness if under_top else 0.0),
            engagement=0.0,
            cut_depth=0.0, cut_run=0.0, cut_offset=0.0,
            lap_depth=0.0, lap_run=0.0,
            setback=0.0, clear_depth=t,
            top_depth=depth if under_top else depth - t,
            bottom_depth=depth - t,
            captive=under_top,
        )

    eng = float(cfg.back_rabbet_depth)
    setback = float(cfg.back_groove_setback) if capture == "dado" else 0.0
    # A half lap splits the back's thickness: the case member is only cut
    # halfway through it, and the back's own perimeter rabbet takes the
    # other half. A plain rabbet takes the back's full thickness.
    cut_run = t / 2.0 if capture == "half_lap" else t
    return BackCapture(
        capture=capture, thickness=t,
        width=cfg.interior_width + 2 * eng,
        height=cfg.interior_height + 2 * eng,
        engagement=eng,
        cut_depth=eng, cut_run=cut_run, cut_offset=setback,
        lap_depth=t / 2.0 if capture == "half_lap" else 0.0,
        lap_run=eng if capture == "half_lap" else 0.0,
        setback=setback, clear_depth=setback + t,
        top_depth=depth, bottom_depth=depth,
        captive=capture == "dado",
    )


# ─── Dict → config builders ───────────────────────────────────────────────────
# These accept the flat JSON-ish shapes used by the MCP tool inputs and the
# project persistence layer. They live here (not in server.py) so that
# pure-data modules like project.py can build configs without importing the
# MCP layer.

# Joinery-spec fields that may arrive as serialized field dicts (e.g. from a
# persisted project) and need reconstructing into their dataclass.
_JOINERY_SPEC_CLASSES = {
    "domino_spec":       DominoSpec,
    "pocket_screw_spec": PocketScrewSpec,
    "biscuit_spec":      BiscuitSpec,
    "dowel_spec":        DowelSpec,
}


def stack_from_column(col: dict) -> list:
    """Return the opening stack for a raw column dict.

    Accepts both the canonical ``openings`` key and the backward-compat
    ``drawer_config`` alias. ``openings`` wins when both are present — the
    single source of truth for this precedence; every consumer of raw column
    dicts (cutlist hardware helpers, panel generation, config building) must
    resolve the stack through here so panels and hardware never disagree.
    """
    return col.get("openings", col.get("drawer_config", []))


# Per-opening override keys accepted in the optional third element of a
# raw ``[height, type, {options}]`` row (and in dict-shaped rows).
_OPENING_OPTION_KEYS = frozenset({
    "hinge_key", "hinge_side", "pull_key", "num_doors",
    "door_thickness", "bottom_thickness", "slide_key",
})


def to_opening(raw) -> OpeningConfig:
    """Normalize a raw row, dict, or OpeningConfig → OpeningConfig.

    Raw rows are ``[height, type]`` or ``[height, type, {options}]`` — the
    optional third element is a dict of per-opening overrides (any of
    ``_OPENING_OPTION_KEYS``, e.g. ``{"bottom_thickness": 12}`` on a drawer).
    """
    if isinstance(raw, OpeningConfig):
        return raw
    if isinstance(raw, dict):
        options = _coerce_opening_options(
            {k: raw.get(k) for k in _OPENING_OPTION_KEYS}
        )
        return OpeningConfig(
            height_mm=float(raw["height_mm"]),
            opening_type=str(raw.get("opening_type", raw.get("slot_type", "open"))),
            **options,
        )
    options: dict = {}
    if len(raw) > 2 and raw[2] is not None:
        if not isinstance(raw[2], dict):
            raise ValueError(
                f"Third element of an opening row must be an options dict, "
                f"got {raw[2]!r}."
            )
        unknown = set(raw[2]) - _OPENING_OPTION_KEYS
        if unknown:
            raise ValueError(
                f"Unknown per-opening option(s) {sorted(unknown)}; "
                f"valid options: {sorted(_OPENING_OPTION_KEYS)}."
            )
        options = _coerce_opening_options(raw[2])
    return OpeningConfig(
        height_mm=float(raw[0]), opening_type=str(raw[1]), **options
    )


def _coerce_opening_options(options: dict) -> dict:
    """Coerce per-opening option values to their expected types.

    MCP clients send JSON, where a numeric option can arrive as a string
    ("12" for bottom_thickness); without coercion that crashes deep in the
    cutlist instead of failing at input validation.
    """
    out = dict(options)
    for k in ("bottom_thickness", "door_thickness"):
        if out.get(k) is not None:
            out[k] = float(out[k])
    if out.get("num_doors") is not None:
        out["num_doors"] = int(out["num_doors"])
    for k in ("hinge_key", "hinge_side", "pull_key", "slide_key"):
        if out.get(k) is not None:
            out[k] = str(out[k])
    return out


def build_cabinet_config(args: dict) -> CabinetConfig:
    """Build a CabinetConfig from a flat dict of keyword arguments.

    Accepts ``drawer_config`` (backward-compat API name) as an alias for
    ``openings``. Each entry may be a ``[height_mm, opening_type]`` list,
    a dict, or an ``OpeningConfig`` object — all are normalised by
    ``to_opening``.

    Also accepts the ``design_cabinet`` convenience parameters:
    ``num_drawers`` (+ optional ``drawer_proportion``) auto-computes a
    graduated drawer stack when no explicit stack is given, and
    ``furniture_top`` (a build/visualization flag with no CabinetConfig
    counterpart) is ignored — so any config shape a design tool accepted
    can be rebuilt here.
    """
    num_drawers       = args.pop("num_drawers", None)
    drawer_proportion = args.pop("drawer_proportion", None)
    args.pop("furniture_top", None)
    if num_drawers and not args.get("drawer_config") and not args.get("openings"):
        from .proportions import graduated_drawer_heights
        bottom_t   = float(args.get("bottom_thickness", 18))
        top_t      = float(args.get("top_thickness",    18))
        interior_h = float(args["height"]) - bottom_t - top_t
        heights = graduated_drawer_heights(
            interior_h, int(num_drawers), drawer_proportion or "classic"
        )
        # Largest drawer at the bottom — same order design_cabinet produces.
        args["drawer_config"] = [[h, "drawer"] for h in sorted(heights, reverse=True)]

    preset_key = args.pop("pull_preset", None)
    if preset_key:
        from .hardware import get_pull_preset
        preset = get_pull_preset(preset_key)
        args.setdefault("drawer_pull", preset.drawer_pull)
        args.setdefault("door_pull", preset.door_pull)
        args.setdefault("door_pull_inset_mm", preset.door_pull_inset_mm)

    # Accept drawer_config as a backward-compat alias for openings.
    if "drawer_config" in args and "openings" not in args:
        args["openings"] = args.pop("drawer_config")
    else:
        args.pop("drawer_config", None)

    kwargs: dict = {}
    for key, value in args.items():
        if key == "carcass_joinery" and isinstance(value, str):
            kwargs[key] = CarcassJoinery(value)
        elif key == "drawer_joinery" and isinstance(value, str):
            kwargs[key] = DrawerJoineryStyle(value)
        elif key in _JOINERY_SPEC_CLASSES and isinstance(value, dict):
            kwargs[key] = _JOINERY_SPEC_CLASSES[key](**value)
        elif key == "openings" and isinstance(value, list):
            kwargs[key] = [to_opening(r) for r in value]
        elif key == "columns" and isinstance(value, list):
            kwargs[key] = [
                ColumnConfig(
                    width_mm=float(c["width_mm"]),
                    openings=tuple(to_opening(r) for r in stack_from_column(c)),
                    fixed_shelf_positions=tuple(
                        float(z) for z in c.get("fixed_shelf_positions", [])
                    ),
                )
                for c in value
            ]
        else:
            kwargs[key] = value

    # Reject unknown keys with an actionable message instead of letting
    # CabinetConfig(**kwargs) raise a bare "unexpected keyword argument" —
    # these dicts arrive from MCP clients, where a typo'd parameter name
    # should read as input validation, not a Python traceback.
    valid = set(CabinetConfig.__dataclass_fields__)
    unknown = set(kwargs) - valid
    if unknown:
        raise ValueError(
            f"Unknown cabinet parameter(s): {', '.join(sorted(unknown))}. "
            f"Valid parameters: {', '.join(sorted(valid | {'drawer_config', 'pull_preset', 'num_drawers', 'drawer_proportion'}))}."
        )
    return CabinetConfig(**kwargs)


def _require_cq():
    if cq is None:
        raise ImportError(
            "cadquery is required for 3D modeling. Install with: pip install cadquery"
        )


def _cut_back_capture(
    panel: "cq.Workplane",
    cfg: CabinetConfig,
    face: str,
    length: float,
    mirror: bool = False,
) -> "cq.Workplane":
    """Machine the back's groove or rabbet into one case panel.

    ``face`` names which face of the panel the back seats against, in that
    panel's own coordinates: "inner_x" for a side (thickness on X), "top_z"
    for a bottom panel, "under_z" for a top panel. ``length`` is the panel's
    extent along the axis the cut runs. No-op for the "pocket" capture,
    which machines nothing.
    """
    geo = back_capture_geometry(cfg)
    if not geo.machined:
        return panel
    # Measured from the panel's REAR edge forward: a rabbet opens at the
    # rear (offset 0), a dado groove is held back by its setback.
    y0 = cfg.depth - geo.cut_offset - geo.cut_run
    if face == "inner_x":
        x0 = 0.0 if mirror else cfg.side_thickness - geo.cut_depth
        cutter = (cq.Workplane("XY")
                  .transformed(offset=(x0, y0, 0))
                  .box(geo.cut_depth, geo.cut_run, length, centered=False))
    else:
        z0 = 0.0 if face == "under_z" else length - geo.cut_depth
        cutter = (cq.Workplane("XY")
                  .transformed(offset=(0, y0, z0))
                  .box(_CAPTURE_CUT_SPAN, geo.cut_run, geo.cut_depth,
                       centered=False))
    return panel.cut(cutter)


#: Width of the cutter used for top/bottom back grooves. The groove runs
#: right through the panel's length (its ends are covered by the sides), so
#: the cutter only has to be at least as long as any panel — it is trimmed
#: by the boolean.
_CAPTURE_CUT_SPAN: float = 10_000.0


def _is_butt(cfg: CabinetConfig) -> bool:
    """Butt-joint construction (floating tenon / pocket screw / biscuit /
    dowel): interior panels seat BETWEEN plain-slab sides at their cutlist
    dimensions — no dados, no back rabbet. Only dado/rabbet construction
    keeps the housed (dado-era) geometry. Mirrors the cutlist convention in
    server._raw_panels_for_cabinet."""
    return cfg.carcass_joinery != CarcassJoinery.DADO_RABBET


def make_side_panel(cfg: CabinetConfig, mirror: bool = False) -> "cq.Workplane":
    """Create a side panel.

    Dado/rabbet construction: dados for bottom/top/shelves and a rabbet for
    the back. Butt construction: a plain slab (panels butt against the
    interior face; the back seats against the setback of the interior
    panels, not a rabbet).

    Args:
        cfg: Cabinet configuration.
        mirror: If True, mirror joinery for the right side panel.
    """
    _require_cq()

    # Start with a solid panel
    panel = (
        cq.Workplane("XY")
        .box(cfg.side_thickness, cfg.depth, cfg.height, centered=False)
    )

    if _is_butt(cfg):
        # Plain slab; only the shelf-pin holes apply. The rear pin row
        # references the back's seat plane (depth − back_thickness).
        if cfg.adj_shelf_holes:
            x_start = (cfg.side_thickness - cfg.shelf_pin_depth) if not mirror else 0
            z = cfg.shelf_pin_start_z
            rear_ref = cfg.depth - cfg.back_thickness
            while z <= cfg.shelf_pin_end_z:
                for y_inset in [cfg.shelf_pin_row_inset,
                                rear_ref - cfg.shelf_pin_row_inset]:
                    pin_hole = (
                        cq.Workplane("YZ")
                        .transformed(offset=(y_inset, z, x_start))
                        .cylinder(
                            cfg.shelf_pin_depth,
                            cfg.shelf_pin_diameter / 2,
                            centered=(True, True, False),
                        )
                    )
                    panel = panel.cut(pin_hole)
                z += cfg.shelf_pin_spacing
        return _cut_back_capture(panel, cfg, "inner_x", cfg.height, mirror)

    # Cut rabbet for back panel along the back edge.
    # The rabbet runs the full height on the inside-back edge.
    # Left panel (mirror=False): interior face at x=side_thickness; rabbet cut from
    # x = side_thickness - back_rabbet_depth.
    # Right panel (mirror=True): interior face at x=0; rabbet cut starts at x=0.
    rabbet = (
        cq.Workplane("XY")
        .transformed(offset=(
            cfg.side_thickness - cfg.back_rabbet_depth if not mirror else 0,
            cfg.depth - cfg.back_rabbet_width,
            0,
        ))
        .box(cfg.back_rabbet_depth, cfg.back_rabbet_width, cfg.height, centered=False)
    )
    panel = panel.cut(rabbet)

    # Cut dado for bottom panel.
    # Left panel (mirror=False): interior face is at local x=side_thickness,
    # so the dado must start at x = side_thickness - dado_depth.
    # Right panel (mirror=True): interior face is at local x=0, dado starts at x=0.
    dado_x = cfg.side_thickness - cfg.dado_depth if not mirror else 0
    bottom_dado = (
        cq.Workplane("XY")
        .transformed(offset=(dado_x, 0, 0))
        .box(cfg.dado_depth, cfg.depth - cfg.back_rabbet_width, cfg.bottom_thickness, centered=False)
    )
    panel = panel.cut(bottom_dado)

    # Cut dado for top panel — extends full depth so the top panel (which runs
    # to the back exterior) seats flush in the dado along its entire length.
    top_dado = (
        cq.Workplane("XY")
        .transformed(offset=(dado_x, 0, cfg.height - cfg.top_thickness))
        .box(cfg.dado_depth, cfg.depth, cfg.top_thickness, centered=False)
    )
    panel = panel.cut(top_dado)

    # Cut dados for fixed shelves
    for shelf_z in cfg.fixed_shelf_positions:
        shelf_dado = (
            cq.Workplane("XY")
            .transformed(offset=(dado_x, 0, shelf_z))
            .box(cfg.dado_depth, cfg.depth - cfg.back_rabbet_width, cfg.shelf_thickness, centered=False)
        )
        panel = panel.cut(shelf_dado)

    # Drill shelf pin holes (32mm system).
    # Holes bore horizontally from the interior face (X direction), so the
    # workplane must be YZ (normal = X). x_start is the global X where the
    # bore begins; the cylinder extends shelf_pin_depth toward the exterior.
    if cfg.adj_shelf_holes:
        x_start = (cfg.side_thickness - cfg.shelf_pin_depth) if not mirror else 0
        z = cfg.shelf_pin_start_z
        while z <= cfg.shelf_pin_end_z:
            for y_inset in [cfg.shelf_pin_row_inset, cfg.depth - cfg.back_rabbet_width - cfg.shelf_pin_row_inset]:
                pin_hole = (
                    cq.Workplane("YZ")
                    .transformed(offset=(y_inset, z, x_start))
                    .cylinder(
                        cfg.shelf_pin_depth,
                        cfg.shelf_pin_diameter / 2,
                        centered=(True, True, False),
                    )
                )
                panel = panel.cut(pin_hole)
            z += cfg.shelf_pin_spacing

    return panel


def make_bottom_panel(cfg: CabinetConfig) -> "cq.Workplane":
    """Create the bottom panel.

    Dado/rabbet: extends into the side dados, stops at the back rabbet.
    Butt: cut to interior width × the depth ``back_capture`` calls for —
    the cutlist dims. Under "pocket" the panel stops short and its rear
    setback is the back's seat; the machined captures run it full depth and
    take the back in a groove or rabbet in its top face.
    """
    _require_cq()
    if _is_butt(cfg):
        panel_width = cfg.interior_width
        panel_depth = back_capture_geometry(cfg).bottom_depth
    else:
        # Width: interior width + dado depth on each side (extends into dados)
        panel_width = cfg.interior_width + (cfg.dado_depth * 2)
        panel_depth = cfg.depth - cfg.back_rabbet_width

    panel = (
        cq.Workplane("XY")
        .box(panel_width, panel_depth, cfg.bottom_thickness, centered=False)
    )
    if _is_butt(cfg):
        panel = _cut_back_capture(panel, cfg, "top_z", cfg.bottom_thickness)
    return panel


def make_top_panel(cfg: CabinetConfig) -> "cq.Workplane":
    """Create the top panel.

    Dado/rabbet: seats in side dados and extends to full depth (the back
    stops at its underside). Butt: interior width; depth follows
    ``back_style`` — "under_top" runs full depth (rear edge flush with the
    sides, capping the back), "full_height" stops at depth − back_thickness
    (the back runs past it to the top plane), matching the cutlist.
    """
    _require_cq()
    if _is_butt(cfg):
        panel_width = cfg.interior_width
        panel_depth = back_capture_geometry(cfg).top_depth
    else:
        panel_width = cfg.interior_width + (cfg.dado_depth * 2)
        panel_depth = cfg.depth

    panel = (
        cq.Workplane("XY")
        .box(panel_width, panel_depth, cfg.top_thickness, centered=False)
    )
    if _is_butt(cfg):
        panel = _cut_back_capture(panel, cfg, "under_z", cfg.top_thickness)
    return panel


def make_shelf(cfg: CabinetConfig) -> "cq.Workplane":
    """Create a fixed shelf panel. Same dimensions as bottom."""
    _require_cq()
    if _is_butt(cfg):
        panel_width = cfg.interior_width
        # Shelves stop at the back's FRONT face whatever the capture — only
        # the perimeter members (sides, top, bottom) hold the back.
        panel_depth = cfg.depth - back_capture_geometry(cfg).clear_depth
    else:
        panel_width = cfg.interior_width + (cfg.dado_depth * 2)
        panel_depth = cfg.depth - cfg.back_rabbet_width

    return (
        cq.Workplane("XY")
        .box(panel_width, panel_depth, cfg.shelf_thickness, centered=False)
    )


def make_back_panel(cfg: CabinetConfig) -> "cq.Workplane":
    """Create the back panel.

    Dado/rabbet: rabbet-width panel stopping under the full-depth top.
    Butt: sized by ``back_capture``. "pocket" laps onto the rear edges it
    finds, at interior width and a height following ``back_style``; the
    machined captures seat INSIDE the case perimeter, so the panel is cut
    oversize by the engagement on all four edges. "half_lap" additionally
    carries its own perimeter rabbet on the FRONT face, which is what laps
    into the matching rabbets in the case.
    """
    _require_cq()
    if _is_butt(cfg):
        geo = back_capture_geometry(cfg)
        width, height = geo.width, geo.height
    else:
        geo = None
        width, height = cfg.back_panel_width, cfg.back_panel_height
    panel = (
        cq.Workplane("XY")
        .box(width, cfg.back_thickness, height, centered=False)
    )
    if geo is not None and geo.lap_depth:
        # Panel-local axes: X across the back, Y its thickness (0 = front
        # face, since the back faces forward into the case), Z its height.
        # The lap comes off the FRONT face around all four edges.
        for x0, z0, dx, dz in (
            (0.0, 0.0, geo.lap_run, height),                    # left edge
            (width - geo.lap_run, 0.0, geo.lap_run, height),    # right edge
            (0.0, 0.0, width, geo.lap_run),                     # bottom edge
            (0.0, height - geo.lap_run, width, geo.lap_run),    # top edge
        ):
            panel = panel.cut(
                cq.Workplane("XY")
                .transformed(offset=(x0, 0.0, z0))
                .box(dx, geo.lap_depth, dz, centered=False)
            )
    return panel


def make_interior_divider(
    cfg: CabinetConfig,
    height_override: Optional[float] = None,
) -> "cq.Workplane":
    """Create an interior vertical divider for multi-bay assemblies.

    Unlike a standard side panel, the divider:
    - Stops at ``depth - back_rabbet_width`` (flush with the back panel's front
      face — does not extend into the back rabbet zone).
    - Has dados for the bottom panel on *both* interior faces so adjacent bay
      horizontal panels are properly supported.
    - Has no back rabbet (the continuous back panel covers the back).

    ``height_override`` clips the divider to a shorter height (e.g. the top of
    the drawer zone in an armoire so the upper door section stays open).  When
    clipped, only bottom dados are cut; no top dados are added.
    """
    _require_cq()
    if _is_butt(cfg):
        # Butt construction: the divider is a plain slab seated on the
        # bottom panel (caller places it at z = bottom_thickness) — interior
        # height up to the top, or up to height_override for clipped
        # armoire dividers. Matches the cutlist's column_divider row.
        panel_depth = cfg.depth - cfg.back_thickness
        top_z = cfg.height - cfg.top_thickness if height_override is None \
            else height_override
        height = top_z - cfg.bottom_thickness
        return cq.Workplane("XY").box(
            cfg.side_thickness, panel_depth, height, centered=False)

    panel_depth = cfg.depth - cfg.back_rabbet_width
    height      = height_override if height_override is not None else cfg.height

    panel = cq.Workplane("XY").box(cfg.side_thickness, panel_depth, height, centered=False)

    # Bottom dado — left face (x = 0 side, facing the bay to the left)
    panel = panel.cut(
        cq.Workplane("XY")
        .transformed(offset=(0, 0, 0))
        .box(cfg.dado_depth, panel_depth, cfg.bottom_thickness, centered=False)
    )
    # Bottom dado — right face (x = side_thickness side, facing the bay to the right)
    panel = panel.cut(
        cq.Workplane("XY")
        .transformed(offset=(cfg.side_thickness - cfg.dado_depth, 0, 0))
        .box(cfg.dado_depth, panel_depth, cfg.bottom_thickness, centered=False)
    )
    if height_override is None:
        # Top dado — left face (full panel depth to receive the full-depth top panel)
        panel = panel.cut(
            cq.Workplane("XY")
            .transformed(offset=(0, 0, height - cfg.top_thickness))
            .box(cfg.dado_depth, panel_depth, cfg.top_thickness, centered=False)
        )
        # Top dado — right face
        panel = panel.cut(
            cq.Workplane("XY")
            .transformed(offset=(cfg.side_thickness - cfg.dado_depth, 0, height - cfg.top_thickness))
            .box(cfg.dado_depth, panel_depth, cfg.top_thickness, centered=False)
        )
    # Note: divider depth = depth - back_rabbet_width; the top panel
    # extends further back but sits above the divider, so no conflict.

    return panel


@dataclass
class PartInfo:
    """Metadata for a part in the assembly."""
    name: str
    shape: object  # cq.Workplane
    material_thickness: float
    grain_direction: str  # "length" or "width" — which dimension follows grain
    edge_band: list[str] = field(default_factory=list)  # list of edges to band
    notes: str = ""


def build_cabinet(
    cfg: Optional[CabinetConfig] = None,
    suppress_left_side: bool = False,
    suppress_right_side: bool = False,
    suppress_back: bool = False,
    suppress_top: bool = False,
    suppress_bottom: bool = False,
) -> tuple["cq.Assembly", list[PartInfo]]:
    """Build a complete cabinet assembly from configuration.

    Args:
        cfg:                Cabinet configuration (defaults to CabinetConfig()).
        suppress_left_side:  When True, omit the left side panel.  Used when
                             a dedicated interior divider panel takes its place.
        suppress_right_side: When True, omit the right side panel.  Used when
                             a dedicated interior divider panel takes its place.
        suppress_back:       When True, omit the back panel.  Used when a
                             single continuous back spans all bays.
        suppress_top:        When True, omit the top panel.  Used when a
                             single continuous top spans all bays.
        suppress_bottom:     When True, omit the bottom panel.  Used when a
                             single continuous bottom spans all bays.

    Returns:
        Tuple of (cq.Assembly, list of PartInfo for BOM/cutlist).
    """
    _require_cq()
    if cfg is None:
        cfg = CabinetConfig()

    parts: list[PartInfo] = []

    # ── Side panels ──────────────────────────────────────────────────────
    left_side  = make_side_panel(cfg, mirror=False) if not suppress_left_side  else None
    right_side = make_side_panel(cfg, mirror=True)  if not suppress_right_side else None

    if left_side is not None:
        parts.append(PartInfo(
            name="left_side",
            shape=left_side,
            material_thickness=cfg.side_thickness,
            grain_direction="length",
            edge_band=["front"],
        ))
    if right_side is not None:
        parts.append(PartInfo(
            name="right_side",
            shape=right_side,
            material_thickness=cfg.side_thickness,
            grain_direction="length",
            edge_band=["front"],
        ))

    # ── Bottom panel ─────────────────────────────────────────────────────
    bottom = make_bottom_panel(cfg) if not suppress_bottom else None
    if bottom is not None:
        parts.append(PartInfo(
            name="bottom",
            shape=bottom,
            material_thickness=cfg.bottom_thickness,
            grain_direction="width",  # grain runs left-to-right
            edge_band=["front"],
        ))

    # ── Top panel ────────────────────────────────────────────────────────
    top = make_top_panel(cfg) if not suppress_top else None
    if top is not None:
        parts.append(PartInfo(
            name="top",
            shape=top,
            material_thickness=cfg.top_thickness,
            grain_direction="width",
            edge_band=["front"],
        ))

    # ── Fixed shelves ────────────────────────────────────────────────────
    shelves = []
    for i, shelf_z in enumerate(cfg.fixed_shelf_positions):
        shelf = make_shelf(cfg)
        shelves.append(shelf)
        parts.append(PartInfo(
            name=f"shelf_{i}",
            shape=shelf,
            material_thickness=cfg.shelf_thickness,
            grain_direction="width",
            edge_band=["front"],
        ))

    # ── Back panel ───────────────────────────────────────────────────────
    back = make_back_panel(cfg) if not suppress_back else None
    if back is not None:
        parts.append(PartInfo(
            name="back",
            shape=back,
            material_thickness=cfg.back_thickness,
            grain_direction="width",
            notes="1/4 inch plywood",
        ))

    # ── Assembly ─────────────────────────────────────────────────────────
    assy = cq.Assembly(name="base_cabinet")

    # Left side: sits at x=0 (omitted when suppress_left_side=True)
    if left_side is not None:
        assy.add(left_side, name="left_side", loc=cq.Location((0, 0, 0)),
                 color=cq.Color(0.87, 0.72, 0.53, 1.0))

    # Right side: sits at x = width - side_thickness (omitted when suppress_right_side=True)
    if right_side is not None:
        assy.add(right_side, name="right_side",
                 loc=cq.Location((cfg.width - cfg.side_thickness, 0, 0)),
                 color=cq.Color(0.87, 0.72, 0.53, 1.0))

    # Butt: panels seat against the sides' interior faces (x = side_thickness);
    # dado/rabbet: panels extend into the dados / rabbets.
    interior_x = (cfg.side_thickness if _is_butt(cfg)
                  else cfg.side_thickness - cfg.dado_depth)

    # Bottom: sits between sides
    if bottom is not None:
        assy.add(bottom, name="bottom", loc=cq.Location((interior_x, 0, 0)),
                 color=cq.Color(0.87, 0.72, 0.53, 1.0))

    # Shelves
    for i, (shelf, shelf_z) in enumerate(zip(shelves, cfg.fixed_shelf_positions)):
        assy.add(shelf, name=f"shelf_{i}", loc=cq.Location((interior_x, 0, shelf_z)),
                 color=cq.Color(0.80, 0.65, 0.45, 1.0))

    # Top panel
    if top is not None:
        top_z = cfg.height - cfg.top_thickness
        assy.add(top, name="top", loc=cq.Location((interior_x, 0, top_z)),
                 color=cq.Color(0.87, 0.72, 0.53, 1.0))

    # Back panel (omitted when suppress_back=True). Butt: seats between the
    # sides against the interior panels' rear setback; dado/rabbet: in the
    # side rabbets.
    if back is not None:
        if _is_butt(cfg):
            geo = back_capture_geometry(cfg)
            # A machined capture holds the back INSIDE the case: it runs
            # into each member by the engagement, and a groove holds its
            # rear face forward of the carcass rear by the setback.
            back_x = cfg.side_thickness - geo.engagement
            back_y = cfg.depth - geo.setback - cfg.back_thickness
            back_z = (0.0 if not geo.machined
                      else cfg.bottom_thickness - geo.engagement)
        else:
            back_x = cfg.side_thickness - cfg.back_rabbet_depth
            back_y = cfg.depth - cfg.back_rabbet_width
            back_z = 0.0
        assy.add(back, name="back", loc=cq.Location((back_x, back_y, back_z)),
                 color=cq.Color(0.75, 0.60, 0.40, 0.8))

    return assy, parts


def _make_pull_shape(pull_spec, vertical: bool = False) -> "Optional[cq.Workplane]":
    """Return a simple 3D body for a pull, centered at its geometric midpoint.

    The caller places this shape so its origin sits at:
        (face_center_x, face_front_y - projection/2, face_center_z)

    ``vertical=True`` rotates the bar so its long axis runs along Z (used for
    door pulls, which are mounted vertically).

    Returns None for flush/recessed pulls (nothing projects above the face).
    """
    _require_cq()
    proj = max(pull_spec.projection_mm, 4.0)
    if pull_spec.mount_style is MountStyle.FLUSH:
        return None
    if pull_spec.mount_style is MountStyle.KNOB:
        r = max(proj * 0.6, 8.0)
        return cq.Workplane("XY").sphere(r)
    # SURFACE or EDGE bar pull — a rounded rectangular bar
    bar_h = min(proj * 0.7, 12.0)
    if vertical:
        return cq.Workplane("XY").box(bar_h, proj, pull_spec.length_mm, centered=True)
    return cq.Workplane("XY").box(pull_spec.length_mm, proj, bar_h, centered=True)


def build_multi_bay_cabinet(
    bay_configs: list["CabinetConfig"],
    foot_height: Optional[float] = None,
    foot_diameter: Optional[float] = None,
    face_thickness: float = 18.0,
    outer_overlay: float = 18.0,
    inner_overlay: float = INNER_FACE_OVERLAY_MM,
    face_gap: float = DEFAULT_FACE_GAP_MM,
    face_bottom_overhang: float = 0.0,
    face_top_overhang: float = 0.0,
    include_drawers: bool = True,
    include_faces: bool = True,
    include_feet: bool = True,
    feet_at_dividers: bool = True,
    furniture_top: bool = False,
    transition_shelf_zs: Optional[list[float]] = None,
    divider_top_z: Optional[float] = None,
    include_manga: bool = False,
) -> tuple["cq.Assembly", list["PartInfo"]]:
    """Build a multi-bay cabinet assembly with bays positioned side-by-side.

    Bay 0 is leftmost; the outer edges of bay 0 and bay[-1] are flush with
    the full cabinet exterior.  Drawer faces span the dividers using
    ``outer_overlay`` on the two outermost edges and ``inner_overlay`` on
    all interior bay joints, leaving a ``divider_thickness - 2 * inner_overlay``
    gap between adjacent bay faces.

    The face stack is anchored at top and bottom:
    - Bottom of lowest face = ``bottom_thickness - face_bottom_overhang``
      (0 = faces start at top of bottom panel; set to bottom_thickness for flush-to-carcass-exterior)
    - Top of highest face  = ``height - top_thickness + face_top_overhang``
      (0 = faces end at underside of top panel; set to top_thickness for flush-to-carcass-exterior)

    Between adjacent faces ``face_gap`` is the **total** clearance between the
    bottom edge of the upper face and the top edge of the lower face.  Half of
    ``face_gap`` is trimmed from each side of the opening boundary, so both
    faces share the gap symmetrically.

    Args:
        bay_configs:          Ordered list of CabinetConfig, left to right.
        foot_height:          Adjustable-foot height in mm (default 102 mm = 4″).
        foot_diameter:        Foot cylinder diameter in mm.
        face_thickness:       Drawer face panel thickness in mm.
        outer_overlay:        Face overhang on outermost cabinet edges (flush = side_thickness).
        inner_overlay:        Face overhang on interior bay dividers.
        face_gap:             Total vertical gap between adjacent faces (mm).  Half is
                              trimmed from the top of the lower face and half from the
                              bottom of the upper face.
        face_bottom_overhang: How far the bottom face extends below the top surface of
                              the bottom panel (default 0 = starts at top of bottom panel).
        face_top_overhang:    How far the top face extends above the underside of the top
                              panel (default 0 = ends at underside of top panel).
        include_drawers:      Build and add drawer box assemblies.
        include_faces:        Build and add drawer face panels.
        include_feet:         Build and add adjustable-foot cylinders.
        furniture_top:        When True, adds a "furniture top" style: a front cap
                              strip extends the top panel forward to the drawer-face
                              plane, and the bottom of the lowest drawer face drops
                              to the underside of the carcass bottom panel
                              (face_bottom_overhang is automatically set to
                              bottom_thickness; an explicit face_bottom_overhang
                              argument is ignored when furniture_top=True).
        include_manga:        Add a manga scale-reference stack to every drawer
                              box (viewer prop, excluded from the parts list).
                              Raises ValueError naming the drawer if any
                              interior can't hold the full 5-volume stack.

    Returns:
        (cq.Assembly, list[PartInfo]) — the full assembly and its parts list.
    """
    _require_cq()

    if not bay_configs:
        raise ValueError("bay_configs must be non-empty")

    # Lazy import to avoid circular dependency (drawer.py imports from cabinet.py)
    from .drawer import DrawerConfig, build_drawer

    all_parts: list[PartInfo] = []
    assy = cq.Assembly(name="multi_bay_cabinet")

    n_bays = len(bay_configs)

    # ── Bay X offsets ──────────────────────────────────────────────────────────
    # Adjacent bays share a single divider panel: the right panel of bay N serves
    # as the left wall of bay N+1.  Each non-leftmost bay is therefore shifted
    # one side_thickness to the left so its interior aligns with the shared panel.
    x_offsets: list[float] = []
    x = 0.0
    for i, cfg in enumerate(bay_configs):
        x_offsets.append(x)
        x += cfg.width - (cfg.side_thickness if i < n_bays - 1 else 0)
    total_width = x

    # ── furniture_top override ─────────────────────────────────────────────────
    # "Furniture top, flush bottom": the top panel cap extends forward to the face
    # plane; the lowest drawer face drops to the carcass underside.
    if furniture_top:
        face_bottom_overhang = bay_configs[0].bottom_thickness
        face_top_overhang    = -face_gap        # same reveal as between adjacent faces

    # Colours — alternate slightly between bays for clarity
    carcass_colours = [
        cq.Color(0.87, 0.72, 0.53, 1.0),
        cq.Color(0.80, 0.65, 0.45, 1.0),
        cq.Color(0.87, 0.72, 0.53, 1.0),
    ]
    drawer_colour = cq.Color(0.78, 0.65, 0.42, 1.0)
    face_colour   = cq.Color(0.55, 0.38, 0.22, 1.0)
    foot_colour   = cq.Color(0.25, 0.25, 0.28, 1.0)

    # ── Continuous top/bottom when non-stacked ─────────────────────────────────
    # When the layout has no transition shelves AND dividers run full height,
    # the cabinet has a single bottom and a single top spanning all bays
    # instead of one per bay.  Stacked layouts (armoires with a transition
    # shelf, or clipped dividers) keep per-bay top/bottom.
    non_stacked = not transition_shelf_zs and divider_top_z is None
    suppress_bay_tb = non_stacked

    # ── Carcass bays ───────────────────────────────────────────────────────────
    for bay_idx, (cfg, bx) in enumerate(zip(bay_configs, x_offsets)):
        bay_assy, bay_parts = build_cabinet(
            cfg,
            suppress_left_side=(bay_idx > 0),           # divider provides left wall
            suppress_right_side=(bay_idx < n_bays - 1), # divider provides right wall
            suppress_back=True,                          # single continuous back added below
            suppress_top=suppress_bay_tb,                # continuous top added below
            suppress_bottom=suppress_bay_tb,             # continuous bottom added below
        )
        col = carcass_colours[bay_idx % len(carcass_colours)]
        assy.add(bay_assy, name=f"bay_{bay_idx}",
                 loc=cq.Location((bx, 0, 0)),
                 color=col)
        for p in bay_parts:
            all_parts.append(PartInfo(
                name=f"bay{bay_idx}_{p.name}",
                shape=p.shape,
                material_thickness=p.material_thickness,
                grain_direction=p.grain_direction,
                edge_band=list(p.edge_band),
                notes=p.notes,
            ))

    # ── Continuous bottom + top panels (non-stacked only) ──────────────────────
    if suppress_bay_tb:
        cfg0 = bay_configs[0]
        butt = _is_butt(cfg0)
        if butt:
            # Butt: panels seat BETWEEN the outer sides at interior width —
            # the cutlist dims. Rear setback = back thickness (the back's
            # seat).
            cont_panel_width = total_width - 2 * cfg0.side_thickness
            cont_panel_x = cfg0.side_thickness
            cont_bottom_depth = back_capture_geometry(cfg0).bottom_depth
        else:
            # Dado/rabbet: spans from the inside-back of the left side
            # panel's bottom dado to the matching point on the right side —
            # interior width plus one dado_depth each side.
            cont_panel_width = (
                total_width
                - 2 * cfg0.side_thickness
                + 2 * cfg0.dado_depth
            )
            cont_panel_x = cfg0.side_thickness - cfg0.dado_depth
            cont_bottom_depth = cfg0.depth - cfg0.back_rabbet_width

        cont_bottom = (
            cq.Workplane("XY")
            .box(cont_panel_width, cont_bottom_depth, cfg0.bottom_thickness, centered=False)
        )
        if butt:
            cont_bottom = _cut_back_capture(cont_bottom, cfg0, "top_z",
                                            cfg0.bottom_thickness)
        assy.add(cont_bottom, name="bottom",
                 loc=cq.Location((cont_panel_x, 0, 0)),
                 color=cq.Color(0.87, 0.72, 0.53, 1.0))
        all_parts.append(PartInfo(
            name="bottom",
            shape=cont_bottom,
            material_thickness=cfg0.bottom_thickness,
            grain_direction="width",
            edge_band=["front"],
            notes="continuous bottom — single panel spanning all bays",
        ))

        # Continuous top — depth follows the construction (matches
        # make_top_panel): dado/rabbet always full depth; butt follows
        # back_capture, which folds in back_style's full-depth cap.
        cont_top_depth = (back_capture_geometry(cfg0).top_depth if butt
                          else cfg0.depth)
        cont_top = (
            cq.Workplane("XY")
            .box(cont_panel_width, cont_top_depth, cfg0.top_thickness, centered=False)
        )
        if butt:
            cont_top = _cut_back_capture(cont_top, cfg0, "under_z",
                                         cfg0.top_thickness)
        cont_top_z = cfg0.height - cfg0.top_thickness
        assy.add(cont_top, name="top",
                 loc=cq.Location((cont_panel_x, 0, cont_top_z)),
                 color=cq.Color(0.87, 0.72, 0.53, 1.0))
        all_parts.append(PartInfo(
            name="top",
            shape=cont_top,
            material_thickness=cfg0.top_thickness,
            grain_direction="width",
            edge_band=["front"],
            notes="continuous top — single panel spanning all bays",
        ))

    # ── Interior vertical dividers ─────────────────────────────────────────────
    # One purpose-built divider per bay boundary, placed at x_offsets[1:].
    # Depth = depth - back_rabbet_width so the back edge is flush with the
    # front face of the continuous back panel (no protrusion behind the back).
    divider_colour = cq.Color(0.87, 0.72, 0.53, 1.0)
    for div_idx, (div_x, cfg) in enumerate(zip(x_offsets[1:], bay_configs)):
        div_shape = make_interior_divider(cfg, height_override=divider_top_z)
        # Butt dividers seat ON the bottom panel (make_interior_divider cut
        # them short by bottom_thickness); dado dividers run to the floor.
        div_z = cfg.bottom_thickness if _is_butt(cfg) else 0.0
        assy.add(div_shape, name=f"divider_{div_idx}",
                 loc=cq.Location((div_x, 0, div_z)),
                 color=divider_colour)
        all_parts.append(PartInfo(
            name=f"divider_{div_idx}",
            shape=div_shape,
            material_thickness=cfg.side_thickness,
            grain_direction="length",
            edge_band=["front"],
        ))

    # ── Continuous back panel ──────────────────────────────────────────────────
    # A single panel spanning all bays, fitting into the outer side-panel rabbets
    # and running behind the shared interior dividers.
    cfg0 = bay_configs[0]
    cfg_last = bay_configs[-1]
    if _is_butt(cfg0):
        # Butt: sized and placed by back_capture. "pocket" seats the back
        # BETWEEN the outer sides against the interior panels' rear setback,
        # at a height following back_style; a machined capture runs it into
        # each member by the engagement and holds it forward by the setback.
        geo0 = back_capture_geometry(cfg0)
        cont_back_width = (total_width - cfg0.side_thickness
                           - cfg_last.side_thickness + 2 * geo0.engagement)
        cont_back_height = geo0.height
        back_x = cfg0.side_thickness - geo0.engagement
        back_y = cfg0.depth - geo0.setback - cfg0.back_thickness
        back_z = (0.0 if not geo0.machined
                  else cfg0.bottom_thickness - geo0.engagement)
    else:
        cont_back_width = (
            total_width
            - (cfg0.side_thickness - cfg0.back_rabbet_depth)   # left rabbet offset
            - (cfg_last.side_thickness - cfg_last.back_rabbet_depth)  # right rabbet offset
        )
        cont_back_height = cfg0.back_panel_height
        back_x = cfg0.side_thickness - cfg0.back_rabbet_depth
        back_y = cfg0.depth - cfg0.back_rabbet_width
        back_z = 0.0
    cont_back = (
        cq.Workplane("XY")
        .box(cont_back_width, cfg0.back_thickness, cont_back_height, centered=False)
    )
    if _is_butt(cfg0) and geo0.lap_depth:
        for x0, z0, dx, dz in (
            (0.0, 0.0, geo0.lap_run, cont_back_height),
            (cont_back_width - geo0.lap_run, 0.0, geo0.lap_run, cont_back_height),
            (0.0, 0.0, cont_back_width, geo0.lap_run),
            (0.0, cont_back_height - geo0.lap_run, cont_back_width, geo0.lap_run),
        ):
            cont_back = cont_back.cut(
                cq.Workplane("XY")
                .transformed(offset=(x0, 0.0, z0))
                .box(dx, geo0.lap_depth, dz, centered=False))
    assy.add(cont_back, name="back",
             loc=cq.Location((back_x, back_y, back_z)),
             color=cq.Color(0.75, 0.60, 0.40, 0.8))
    all_parts.append(PartInfo(
        name="back",
        shape=cont_back,
        material_thickness=cfg0.back_thickness,
        grain_direction="width",
        notes="1/4 inch plywood — single panel spanning all bays",
    ))

    # ── Transition shelves ─────────────────────────────────────────────────────
    # Full-width horizontal panels at drawer-to-door boundaries (e.g. armoire base).
    if transition_shelf_zs:
        shelf_colour_ts = cq.Color(0.87, 0.72, 0.53, 1.0)
        ts_cfg  = bay_configs[0]
        ts_w    = total_width - 2 * ts_cfg.side_thickness
        ts_dep  = ts_cfg.depth - (ts_cfg.back_thickness if _is_butt(ts_cfg)
                                  else ts_cfg.back_rabbet_width)
        ts_thk  = ts_cfg.shelf_thickness
        for ts_idx, ts_z in enumerate(transition_shelf_zs):
            ts_panel = (
                cq.Workplane("XY")
                .box(ts_w, ts_dep, ts_thk, centered=False)
            )
            assy.add(
                ts_panel,
                name=f"transition_shelf_{ts_idx}",
                loc=cq.Location((ts_cfg.side_thickness, 0.0, ts_z)),
                color=shelf_colour_ts,
            )
            all_parts.append(PartInfo(
                name=f"transition_shelf_{ts_idx}",
                shape=ts_panel,
                material_thickness=ts_thk,
                grain_direction="width",
                edge_band=["front"],
                notes="transition shelf — drawer-to-door boundary",
            ))

    # ── Furniture top cap ──────────────────────────────────────────────────────
    # A thin horizontal strip that extends the top panel forward to the drawer
    # face plane, creating a flush furniture-style top edge.
    if furniture_top:
        top_cap = (
            cq.Workplane("XY")
            .box(total_width, face_thickness, cfg0.top_thickness, centered=False)
        )
        cap_z = cfg0.height - cfg0.top_thickness
        assy.add(top_cap, name="top_front_cap",
                 loc=cq.Location((0.0, -face_thickness, cap_z)),
                 color=carcass_colours[0])
        all_parts.append(PartInfo(
            name="top_front_cap",
            shape=top_cap,
            material_thickness=cfg0.top_thickness,
            grain_direction="width",
            edge_band=["front", "left", "right"],
            notes="furniture top front cap — spans full cabinet width",
        ))

    # ── Drawer boxes ───────────────────────────────────────────────────────────
    if include_drawers:
        for bay_idx, (cfg, bx) in enumerate(zip(bay_configs, x_offsets)):
            if not cfg.openings:
                continue

            z = cfg.bottom_thickness  # drawers sit above the bottom panel

            for drw_idx, op in enumerate(cfg.openings):
                opening_h = op.height_mm
                if op.opening_type == "drawer":
                    dcfg = DrawerConfig(
                        opening_width=cfg.interior_width,
                        opening_height=opening_h,
                        opening_depth=cfg.interior_depth,
                        slide_key=op.slide_key or cfg.drawer_slide,
                        applied_face=False,  # faces handled below
                        joinery_style=cfg.drawer_joinery,
                        side_thickness=cfg.drawer_box_thickness,
                        front_back_thickness=cfg.drawer_box_thickness,
                        bottom_thickness=op.bottom_thickness,
                    )
                    try:
                        drw_assy, drw_parts = build_drawer(
                            dcfg, include_manga=include_manga)
                    except ValueError as e:
                        raise ValueError(
                            f"bay{bay_idx}_drawer{drw_idx}: {e}") from None

                    drw_x = bx + cfg.side_thickness + dcfg.slide.nominal_side_clearance
                    drw_y = dcfg.front_gap
                    drw_z = z + dcfg.slide.min_bottom_clearance

                    assy.add(drw_assy, name=f"bay{bay_idx}_drawer{drw_idx}",
                             loc=cq.Location((drw_x, drw_y, drw_z)),
                             color=drawer_colour)

                z += opening_h

    # ── Drawer faces ───────────────────────────────────────────────────────────
    if include_faces:
        for bay_idx, (cfg, bx) in enumerate(zip(bay_configs, x_offsets)):
            if not cfg.openings:
                continue

            is_leftmost  = bay_idx == 0
            is_rightmost = bay_idx == n_bays - 1

            left_ov  = outer_overlay if is_leftmost  else inner_overlay
            right_ov = outer_overlay if is_rightmost else inner_overlay

            face_w = left_ov + cfg.interior_width + right_ov

            # Global X of the face's left edge
            if is_leftmost:
                face_x = 0.0
            else:
                face_x = bx + cfg.side_thickness - inner_overlay

            # Anchor the face stack between the bottom and top panels.
            # z_face_start = bottom of the lowest face (in assembly Z coordinates)
            # z_face_end   = top of the highest face
            z_face_start = cfg.bottom_thickness - face_bottom_overhang
            z_face_end   = cfg.height - cfg.top_thickness + face_top_overhang

            # Collect drawer openings with their cumulative Z position within the
            # carcass interior (measured from the top of the bottom panel).
            drawer_slots: list[tuple[int, int, float]] = []  # (drw_idx, opening_h, opening_z)
            z_acc = cfg.bottom_thickness
            for drw_idx, op in enumerate(cfg.openings):
                if op.opening_type == "drawer":
                    drawer_slots.append((drw_idx, op.height_mm, z_acc))
                z_acc += op.height_mm

            n_faces = len(drawer_slots)
            for face_num, (drw_idx, opening_h, opening_z) in enumerate(drawer_slots):
                is_last  = face_num == n_faces - 1

                # Bottom edge of this face.
                # Anchor to z_face_start only when this drawer is the lowest
                # opening in the column; otherwise (a door/open opening sits
                # below it) start face_gap/2 above the opening boundary so the
                # gap straddles the boundary symmetrically.
                is_first_in_col = drw_idx == 0
                if is_first_in_col:
                    face_z_bot = z_face_start
                else:
                    face_z_bot = opening_z + face_gap / 2

                # Top edge of this face.
                # Anchor to z_face_end only when this drawer is also the last
                # opening in the column (i.e. no door/open openings above it).
                # If door openings follow, apply the same face_gap/2 trim so the
                # gap above the top drawer matches the gaps between drawers.
                is_last_in_col = (drw_idx == len(cfg.openings) - 1)
                if is_last and is_last_in_col:
                    face_z_top = z_face_end
                else:
                    face_z_top = opening_z + opening_h - face_gap / 2

                face_h = face_z_top - face_z_bot
                face_shape = (
                    cq.Workplane("XY")
                    .box(face_w, face_thickness, face_h, centered=False)
                )
                # y = -face_thickness so face sits proud of carcass front
                assy.add(face_shape,
                         name=f"bay{bay_idx}_face{drw_idx}",
                         loc=cq.Location((face_x, -face_thickness, face_z_bot)),
                         color=face_colour)
                all_parts.append(PartInfo(
                    name=f"bay{bay_idx}_face{drw_idx}",
                    shape=face_shape,
                    material_thickness=face_thickness,
                    grain_direction="width",
                    edge_band=["all"],
                ))

    # ── Door panels ────────────────────────────────────────────────────────────
    # Render a flat face panel for every "door" or "door_pair" slot.
    # "door_pair" splits into two panels with a 3 mm centre gap.
    # Uses the same z_face_start / z_face_end anchors as drawer faces so that
    # in mixed columns all face edges align at the top and bottom.
    if include_faces:
        door_gap_centre = 3.0
        for bay_idx, (cfg, bx) in enumerate(zip(bay_configs, x_offsets)):
            if not cfg.openings:
                continue

            is_leftmost  = bay_idx == 0
            is_rightmost = bay_idx == n_bays - 1
            left_ov  = outer_overlay if is_leftmost  else inner_overlay
            right_ov = outer_overlay if is_rightmost else inner_overlay
            face_w   = left_ov + cfg.interior_width + right_ov
            face_x   = 0.0 if is_leftmost else bx + cfg.side_thickness - inner_overlay

            z_face_start = cfg.bottom_thickness - face_bottom_overhang
            z_face_end   = cfg.height - cfg.top_thickness + face_top_overhang
            n_slots      = len(cfg.openings)

            z_acc = cfg.bottom_thickness
            for slot_idx, op in enumerate(cfg.openings):
                opening_h  = op.height_mm
                slot_type  = op.opening_type
                if slot_type in ("door", "door_pair"):
                    is_first = slot_idx == 0
                    is_last  = slot_idx == n_slots - 1
                    # Door face starts at z_acc + face_gap/2 — same rule as between
                    # adjacent drawers.  The transition shelf sits behind the face.
                    face_z_bot = z_face_start if is_first else z_acc + face_gap / 2
                    face_z_top = z_face_end   if is_last  else z_acc + opening_h - face_gap / 2
                    face_h = face_z_top - face_z_bot

                    # Honor a per-opening num_doors override — the hinge BOM
                    # already bills by it, so rendering must agree.
                    n_doors = op.num_doors or (2 if slot_type == "door_pair" else 1)
                    if n_doors == 2:
                        door_w = (face_w - door_gap_centre) / 2
                        for i, dx in enumerate(
                            [face_x, face_x + door_w + door_gap_centre]
                        ):
                            ds = (
                                cq.Workplane("XY")
                                .box(door_w, face_thickness, face_h, centered=False)
                            )
                            assy.add(
                                ds,
                                name=f"bay{bay_idx}_door{slot_idx}_{i}",
                                loc=cq.Location((dx, -face_thickness, face_z_bot)),
                                color=face_colour,
                            )
                            all_parts.append(PartInfo(
                                name=f"bay{bay_idx}_door{slot_idx}_{i}",
                                shape=ds,
                                material_thickness=face_thickness,
                                grain_direction="length",
                                edge_band=["all"],
                            ))
                    else:
                        ds = (
                            cq.Workplane("XY")
                            .box(face_w, face_thickness, face_h, centered=False)
                        )
                        assy.add(
                            ds,
                            name=f"bay{bay_idx}_door{slot_idx}",
                            loc=cq.Location((face_x, -face_thickness, face_z_bot)),
                            color=face_colour,
                        )
                        all_parts.append(PartInfo(
                            name=f"bay{bay_idx}_door{slot_idx}",
                            shape=ds,
                            material_thickness=face_thickness,
                            grain_direction="length",
                            edge_band=["all"],
                        ))
                z_acc += opening_h

    # ── Pull hardware ──────────────────────────────────────────────────────────
    # For each bay that has a drawer_pull configured, place the pull body on
    # every drawer face.  Pulls are named bay{i}_pull{j}_{k} so the visualizer
    # can animate them alongside the matching face (bay{i}_face{j}).
    if include_faces:
        from .pulls import pull_positions as _pull_positions
        pull_colour = cq.Color(0.40, 0.40, 0.45, 1.0)

        for bay_idx, (cfg, bx) in enumerate(zip(bay_configs, x_offsets)):
            if not cfg.drawer_pull or not cfg.openings:
                continue
            try:
                pull_spec = get_pull(cfg.drawer_pull)
            except KeyError:
                continue

            pull_body = _make_pull_shape(pull_spec)
            if pull_body is None:
                continue  # flush / recessed pulls have nothing to render

            is_leftmost  = bay_idx == 0
            is_rightmost = bay_idx == n_bays - 1
            left_ov  = outer_overlay if is_leftmost  else inner_overlay
            right_ov = outer_overlay if is_rightmost else inner_overlay
            face_w   = left_ov + cfg.interior_width + right_ov
            face_x   = 0.0 if is_leftmost else bx + cfg.side_thickness - inner_overlay

            z_face_start = cfg.bottom_thickness - face_bottom_overhang
            z_face_end   = cfg.height - cfg.top_thickness + face_top_overhang

            drawer_slots: list[tuple[int, float, float]] = []
            z_acc = cfg.bottom_thickness
            for drw_idx, op in enumerate(cfg.openings):
                if op.opening_type == "drawer":
                    drawer_slots.append((drw_idx, op.height_mm, z_acc))
                z_acc += op.height_mm

            n_faces = len(drawer_slots)
            pull_py = -face_thickness - pull_spec.projection_mm / 2.0

            for face_num, (drw_idx, opening_h, opening_z) in enumerate(drawer_slots):
                is_last  = face_num == n_faces - 1
                is_first_in_col = drw_idx == 0
                is_last_in_col = drw_idx == len(cfg.openings) - 1
                face_z_bot = z_face_start if is_first_in_col else opening_z + face_gap / 2
                if is_last and is_last_in_col:
                    face_z_top = z_face_end
                else:
                    face_z_top = opening_z + opening_h - face_gap / 2
                face_h = face_z_top - face_z_bot

                try:
                    placements = _pull_positions(face_w, face_h, pull_spec, cfg.drawer_pull)
                except ValueError:
                    continue

                for p_idx, placement in enumerate(placements):
                    cx, cz = placement.center
                    assy.add(
                        pull_body,
                        name=f"bay{bay_idx}_pull{drw_idx}_{p_idx}",
                        loc=cq.Location((face_x + cx, pull_py, face_z_bot + cz)),
                        color=pull_colour,
                    )

    # ── Door pull hardware ─────────────────────────────────────────────────────
    # Place a pull body on every door / door_pair face for bays with door_pull set.
    if include_faces:
        from .pulls import pull_positions as _pull_positions
        pull_colour = cq.Color(0.40, 0.40, 0.45, 1.0)
        door_gap_centre = 3.0

        for bay_idx, (cfg, bx) in enumerate(zip(bay_configs, x_offsets)):
            if not cfg.door_pull or not cfg.openings:
                continue
            try:
                pull_spec = get_pull(cfg.door_pull)
            except KeyError:
                continue

            pull_body = _make_pull_shape(pull_spec, vertical=True)
            if pull_body is None:
                continue

            is_leftmost  = bay_idx == 0
            is_rightmost = bay_idx == n_bays - 1
            left_ov  = outer_overlay if is_leftmost  else inner_overlay
            right_ov = outer_overlay if is_rightmost else inner_overlay
            face_w   = left_ov + cfg.interior_width + right_ov
            face_x   = 0.0 if is_leftmost else bx + cfg.side_thickness - inner_overlay

            z_face_start = cfg.bottom_thickness - face_bottom_overhang
            z_face_end   = cfg.height - cfg.top_thickness + face_top_overhang
            n_slots      = len(cfg.openings)
            pull_py      = -face_thickness - pull_spec.projection_mm / 2.0

            z_acc = cfg.bottom_thickness
            for slot_idx, op in enumerate(cfg.openings):
                opening_h = op.height_mm
                slot_type = op.opening_type
                if slot_type in ("door", "door_pair"):
                    is_first   = slot_idx == 0
                    is_last    = slot_idx == n_slots - 1
                    face_z_bot = z_face_start if is_first else z_acc + face_gap / 2
                    face_z_top = z_face_end   if is_last  else z_acc + opening_h - face_gap / 2
                    face_h     = face_z_top - face_z_bot

                    n_doors = op.num_doors or (2 if slot_type == "door_pair" else 1)
                    if n_doors == 2:
                        # Pair: left leaf hinges left (outer), right leaf hinges right (outer).
                        # Pulls go on the latch (inner) edges regardless of cfg.door_hinge_side.
                        door_w = (face_w - door_gap_centre) / 2
                        pair_hinge_sides: list[HingeSide] = ["left", "right"]
                        for door_i, door_x in enumerate(
                            [face_x, face_x + door_w + door_gap_centre]
                        ):
                            hs = pair_hinge_sides[door_i]
                            cx = door_pull_x_center(door_w, pull_spec, hs, cfg.door_pull_inset_mm, vertical=True)
                            try:
                                placements = _pull_positions(
                                    door_w, face_h, pull_spec, cfg.door_pull,
                                    x_override_mm=cx,
                                    vertical="upper_third",
                                )
                            except ValueError:
                                continue
                            for p_idx, placement in enumerate(placements):
                                _cx, cz = placement.center
                                assy.add(
                                    pull_body,
                                    name=f"bay{bay_idx}_doorpull{slot_idx}_{door_i}_{p_idx}",
                                    loc=cq.Location((door_x + _cx, pull_py, face_z_bot + cz)),
                                    color=pull_colour,
                                )
                    else:
                        cx = door_pull_x_center(
                            face_w, pull_spec, cfg.door_hinge_side, cfg.door_pull_inset_mm, vertical=True
                        )
                        try:
                            placements = _pull_positions(
                                face_w, face_h, pull_spec, cfg.door_pull,
                                x_override_mm=cx,
                                vertical="upper_third",
                            )
                        except ValueError:
                            continue
                        for p_idx, placement in enumerate(placements):
                            _cx, cz = placement.center
                            assy.add(
                                pull_body,
                                name=f"bay{bay_idx}_doorpull{slot_idx}_{p_idx}",
                                loc=cq.Location((face_x + _cx, pull_py, face_z_bot + cz)),
                                color=pull_colour,
                            )
                z_acc += opening_h

    # ── Feet ───────────────────────────────────────────────────────────────────
    if include_feet:
        cfg0       = bay_configs[0]
        depth      = cfg0.depth
        foot_inset = cfg0.leg_inset

        # Resolve leg spec from the first bay's config; fall back to caller overrides
        try:
            leg_spec = get_leg(cfg0.leg_key)
            _foot_height   = foot_height   if foot_height   is not None else leg_spec.height_mm
            _foot_diameter = foot_diameter if foot_diameter is not None else leg_spec.base_diameter_mm
        except KeyError:
            _foot_height   = foot_height   if foot_height   is not None else 102.0
            _foot_diameter = foot_diameter if foot_diameter is not None else 50.0

        # X positions: outer corners only, or also under each interior divider.
        # Divider feet sit on the divider centreline, not its left edge.
        foot_xs = [foot_inset, total_width - foot_inset]
        if feet_at_dividers:
            foot_xs += [dx + cfg0.side_thickness / 2 for dx in x_offsets[1:]]
        foot_ys = [foot_inset, depth - foot_inset]

        foot_shape = (
            cq.Workplane("XY")
            .cylinder(_foot_height, _foot_diameter / 2, centered=(True, True, False))
        )
        fi = 0
        for fx in foot_xs:
            for fy in foot_ys:
                assy.add(foot_shape, name=f"foot_{fi}",
                         loc=cq.Location((fx, fy, -_foot_height)),
                         color=foot_colour)
                fi += 1

    return assy, all_parts
