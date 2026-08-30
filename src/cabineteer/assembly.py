"""Carcass assembly instructions — floating-tenon (Domino) construction.

Pure Python (no CadQuery).  Builds an :class:`AssemblyPlan` from a
``CabinetConfig`` — the per-joint tenon layout, DF 500 machine settings,
per-panel mortise maps, and an ordered step list with a mandatory dry fit
before glue-up — then renders it as a self-contained HTML document or a
portrait PDF (US Letter default, A4 optional).

Conventions
-----------
* Every carcass joint runs along the cabinet depth; the fastener span is
  ``interior_depth`` (front edge to back-panel face), matching the hardware
  BOM census in cutlist.py.  All mortise positions are measured **from the
  front edge** — both mating parts register off the front, so any variance
  pushes to the back where the back panel hides it.
* The tenon size follows the carcass stock thickness via
  :func:`joinery.carcass_domino_size_for_thickness` (5×30 for 3/4" ply).
* Dry fitting uses 3D-printed PETG reduced-size dominos ("Festool Reduced
  Size Dominos for Dry Fit" by paulengel, printables.com model 689403) —
  slightly undersized for easy insertion and removal.  The plan reports how
  many to print.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .joinery import (
    DominoSize,
    DominoSpec,
    DOMINO_PACK_QUANTITIES,
    carcass_domino_size_for_thickness,
    get_domino_size,
)

DRY_FIT_TENON_URL = (
    "https://www.printables.com/model/689403-festool-reduced-size-dominos-"
    "for-dry-fit-4mm-5mm-6"
)
DRY_FIT_TENON_NAME = (
    'PETG dry-fit tenons — "Festool Reduced Size Dominos for Dry Fit" '
    "by paulengel (printables.com model 689403)"
)


# ─── Plan data model ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CarcassJoint:
    """One tenon-joined carcass joint.

    ``edge_part`` takes mortises in its panel *end* (plunge into end grain);
    ``face_part`` takes the matching mortises in its *face*.  ``positions``
    are mortise centres from the cabinet FRONT edge, identical on both parts.
    """

    index: int                 # 1-based, J1…
    name: str                  # "bottom ↔ left side"
    edge_part: str             # panel that takes EDGE mortises
    face_part: str             # panel that takes FACE mortises
    span: float                # joint length (mm) — interior_depth
    positions: tuple[float, ...]
    kind: str = "butt"         # "butt" | "miter" (both parts: 45° miter face)


@dataclass(frozen=True)
class MortiseRow:
    """A row of mortises on one panel of the mortise map.

    ``axis`` — "h": the row runs along the panel's drawn width (offset is a
    height from the panel's bottom edge); "v": the row runs along the drawn
    height (offset is a distance from the panel's left edge).
    ``kind`` — "face" (plunge into the face), "edge" (plunge into the
    panel end), or "miter" (plunge perpendicular to a 45° beveled end).
    ``positions`` are mortise centres measured from the FRONT
    edge of the panel (drawn left → right or bottom → top as noted per map).
    """

    label: str                 # "bottom (J1)" — what lands here
    axis: str                  # "h" | "v"
    offset: float              # mm, see class docstring
    positions: tuple[float, ...]
    kind: str                  # "face" | "edge"


@dataclass(frozen=True)
class PanelMortiseMap:
    """Flat drawing of one panel with every mortise row it carries."""

    panel: str                 # "left side"
    part_id: str               # cutlist part ID ("A-S1") or ""
    draw_width: float          # mm — horizontal extent of the drawing
    draw_height: float         # mm — vertical extent
    #: The cutlist row family this drawing is OF — "side", "bottom", "top",
    #: "column_divider", "shelf". ``panel`` is display prose ("side (make 2,
    #: mirror-image)", "col 2 fixed shelf") and cannot be joined on; without
    #: this, a closure test comparing the map to the paper could only match
    #: the two panels whose display name happened to equal their row name,
    #: and silently skipped the other four.
    canonical: str
    width_label: str           # axis meaning, e.g. "depth (front → back)"
    height_label: str
    rows: tuple[MortiseRow, ...]
    note: str = ""


@dataclass(frozen=True)
class AssemblyStep:
    """One step of the bench document.

    ``body`` is the prose lead-in. ``checklist`` carries the actions as
    discrete items — use it for anything performed under time pressure or
    tick-by-tick (the dry fit, the glue-up), where a paragraph forces the
    builder to re-read a wall of text with glue on their hands. Renderers
    lay checklist items out as a numbered list.
    """

    title: str
    body: str                  # plain text; renderers wrap/escape
    checklist: tuple[str, ...] = ()


@dataclass(frozen=True)
class DrawerBoxPlan:
    """One drawer box: its cut parts, its corner joint, and where it lands.

    Dimensions come from ``drawer.box_config_for_opening`` — the same
    function the cutlist resolves boxes through — so the numbers here and
    the numbers on the parts list are the same numbers, not two
    derivations that happen to agree today.
    """

    label: str                 # "column 1, drawer 2 (from the bottom)"
    column: int                # 1-based
    position: int              # 1-based from the BOTTOM of the stack
    #: Cut parts. Which one runs full length depends on the corner: see
    #: ``laps_front``. Never assume sides = box depth AND front/back = box
    #: width — that double-counts both corners and the box comes out
    #: oversize in both directions.
    side_length: float
    side_height: float
    front_back_length: float
    front_back_height: float
    stock_thickness: float
    bottom_length: float
    bottom_width: float
    bottom_thickness: float
    #: Bottom groove: ``dado_depth`` deep, ``dado_inset`` up from the
    #: bottom edge, cut on the INSIDE face of all four parts.
    dado_depth: float
    dado_inset: float
    joinery: str               # drawer-lock, half-lap, …
    slide_key: str
    slide_length: float
    opening_width: float
    opening_height: float
    part_ids: tuple = ()       # cutlist IDs for side/front/back/bottom
    #: True when the front and back span the full box width and wrap the
    #: ends of the sides (drawer lock). False when the sides run the full
    #: depth and the front/back is buried between them.
    laps_front: bool = False
    #: Front-lapping only: wall left outboard of the socket, per corner. The
    #: sides are cut 2 x this short of the box depth.
    lip: float = 0.0
    #: Side-lapping only: how far the front/back seats into each side.
    engagement: float = 0.0
    #: Finished box, for the check that matters at glue-up.
    box_width: float = 0.0
    box_depth: float = 0.0
    #: Box width wall to wall. For an undermount runner this is the number
    #: the slide maker constrains (Blum: opening − 42 mm, "must equal"), so
    #: it is what a built box gets measured against — the outside width is
    #: a consequence of it and the side stock, not the other way round.
    box_inside_width: float = 0.0
    #: Which box face the slide's side clearance is quoted to.
    clearance_reference: str = "outside"


@dataclass
class AssemblyPlan:
    cabinet_name: str
    copies: int                # identical cabinets built from this plan
    size_key: str              # "5x30"
    size: DominoSize
    stock_thickness: float
    span: float                # per-joint fastener span (interior_depth)
    per_joint: int             # tenons per joint
    positions: tuple[float, ...]
    joints: list[CarcassJoint] = field(default_factory=list)
    panels: list[PanelMortiseMap] = field(default_factory=list)
    steps: list[AssemblyStep] = field(default_factory=list)
    corner_style: str = "butt"          # butt | miter
    miter_placement: Optional[object] = None   # MiterMortisePlacement
    edge_band_mode: str = "none"        # none | hot_melt | hardwood
    #: Rip width of a hardwood band strip, from ``edge_band_stock``. The
    #: banding step said "ripped ~20 mm wide" as a literal while the cutlist
    #: BOM printed the config's real ``strip_width_mm`` — two numbers for
    #: one setting, on two documents at the same bench. The step had no
    #: object to read, which is why it invented one.
    edge_band_strip_width_mm: float = 0.0
    #: Thicknesses of the panels that actually take a FACE mortise. Computed
    #: once here so the step and the machine table cannot disagree — the
    #: table renderer has no config to derive from, which is how it ended up
    #: with its own copy of the wall arithmetic.
    face_mortised_thicknesses: tuple = ()
    edge_band_thickness_mm: float = 0.0
    #: Distinct carcass panel thicknesses (sorted). Panels below
    #: BASE_REF_MIN_THICKNESS_MM take the centred t/2 fallback; everything
    #: else shares the single 10 mm base-height fence setting.
    panel_thicknesses: tuple = ()
    #: Every drawer box this carcass carries, bottom-up per column.
    drawer_boxes: list = field(default_factory=list)
    #: Steps for building the boxes — a separate sequence from the carcass
    #: one, because they are a separate day's work at the bench.
    box_steps: list = field(default_factory=list)

    @property
    def tenons_per_cabinet(self) -> int:
        return self.per_joint * len(self.joints)

    @property
    def tenons_total(self) -> int:
        return self.tenons_per_cabinet * self.copies

    @property
    def dry_fit_tenons_needed(self) -> int:
        """PETG tenons to print — one full cabinet dry-fits at a time."""
        return self.tenons_per_cabinet


# ─── Registration system (DF 500) ─────────────────────────────────────────────
# The DF 500's cutter axis sits a FIXED 10 mm above its base plate, and face
# mortises mid-panel can only be base-registered (the fence has nothing to
# hook onto mid-panel). So every mortise — fence-cut edge mortises included —
# references 10 mm from ONE marked face per panel (the "reference face":
# top/bottom → outside face, fixed shelves → underside, dividers → left
# face), and the fence height is 10 mm to match the base, NOT t/2. A
# 0-offset base plate (e.g. Seneca 0" Domiplate) gives the identical 10 mm
# without fence drift. Slots land off-centre in the stock; that is
# intentional and harmless — flushness comes from both halves of a joint
# sharing one reference, not from centring.
# (Docs generated before 2026-08 said fence t/2, which mismatches
# base-registered face mortises by |10 − t/2| — 1 mm in 18 mm stock, enough
# that a tight-width joint will not close. Charlie caught it, 2026-08-02.)
DF500_BASE_HEIGHT_MM = 10.0
# Below ~15 mm a 10 mm reference would break out of the far face
# (10 + cutter/2 + 2 mm wall ≈ 14.5 mm for a 5 mm cutter): fall back to
# centred t/2 slots, with the mating face-row batten clamped (10 − t/2) mm
# short of the reference line to compensate.
BASE_REF_MIN_THICKNESS_MM = 15.0


def _ref_offset(t: float) -> float:
    """Mortise-centre distance from the panel's reference face."""
    return DF500_BASE_HEIGHT_MM if t >= BASE_REF_MIN_THICKNESS_MM else t / 2.0


def _and_list(items: list[str]) -> str:
    """Join part names for step prose: "a", "a and b", "a, b, and c"."""
    if len(items) < 3:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


# ─── Plan construction ────────────────────────────────────────────────────────


def _column_shelf_walls(col_index: int, n_cols: int) -> tuple[str, str]:
    left = "left side" if col_index == 0 else f"divider {col_index}"
    right = (
        "right side" if col_index == n_cols - 1 else f"divider {col_index + 1}"
    )
    return left, right


def build_assembly_plan(
    cab_cfg,
    cabinet_name: str = "Cabinet",
    copies: int = 1,
    id_map: Optional[dict] = None,
) -> AssemblyPlan:
    """Build the carcass assembly plan for one cabinet design.

    ``id_map`` optionally maps cutlist panel names ("side", "bottom",
    "column_divider", "shelf_1", …) to part IDs ("A-S1") so the doc and the
    cutlist agree on labels.
    """
    from .cabinet import CarcassJoinery

    joinery = getattr(cab_cfg, "carcass_joinery", CarcassJoinery.FLOATING_TENON)
    if joinery != CarcassJoinery.FLOATING_TENON:
        raise ValueError(
            "Assembly instructions currently cover floating-tenon carcasses "
            f"only; this cabinet uses {getattr(joinery, 'value', joinery)!r}."
        )

    side_t = float(getattr(cab_cfg, "side_thickness", 18.0))
    size_key = carcass_domino_size_for_thickness(side_t)
    size = get_domino_size(size_key)
    spec = DominoSpec(size_key=size_key, max_spacing=150.0)

    span = float(cab_cfg.interior_depth)
    positions = tuple(round(p, 1) for p in spec.positions_for_span(span))
    per_joint = len(positions)

    corner_style = getattr(cab_cfg, "carcass_corner_style", "butt")
    miter = corner_style == "miter"
    miter_placement = None
    if miter:
        from .joinery import miter_mortise_placement
        # Raises ValueError on infeasible stock — the caller reports it.
        miter_placement = miter_mortise_placement(size, side_t)

    band_mode = getattr(cab_cfg, "edge_band_mode", "none")
    band_t = float(getattr(cab_cfg, "edge_band_thickness_mm", 0.6))

    id_map = id_map or {}

    def pid(panel_name: str) -> str:
        return id_map.get(panel_name, "")

    # ── Joint census (mirrors the hardware-BOM count) ─────────────────────
    joints: list[CarcassJoint] = []

    def add(name: str, edge_part: str, face_part: str,
            kind: str = "butt") -> None:
        joints.append(CarcassJoint(
            index=len(joints) + 1, name=name,
            edge_part=edge_part, face_part=face_part,
            span=span, positions=positions, kind=kind,
        ))

    corner_kind = "miter" if miter else "butt"
    add("bottom ↔ left side", "bottom", "left side", corner_kind)
    add("bottom ↔ right side", "bottom", "right side", corner_kind)
    add("top ↔ left side", "top", "left side", corner_kind)
    add("top ↔ right side", "top", "right side", corner_kind)

    cols = list(getattr(cab_cfg, "columns", []) or [])
    n_cols = len(cols)
    n_dividers = max(0, n_cols - 1)
    for d in range(1, n_dividers + 1):
        add(f"divider {d} ↔ bottom", f"divider {d}", "bottom")
        add(f"divider {d} ↔ top", f"divider {d}", "top")

    global_shelves = list(getattr(cab_cfg, "fixed_shelf_positions", []) or [])
    for si, _z in enumerate(global_shelves, start=1):
        add(f"fixed shelf {si} ↔ left side", f"fixed shelf {si}", "left side")
        add(f"fixed shelf {si} ↔ right side", f"fixed shelf {si}", "right side")

    for ci, col in enumerate(cols):
        for si, _z in enumerate(
                getattr(col, "fixed_shelf_positions", ()) or (), start=1):
            left_wall, right_wall = _column_shelf_walls(ci, n_cols)
            shelf = f"col {ci + 1} shelf {si}"
            add(f"{shelf} ↔ {left_wall}", shelf, left_wall)
            add(f"{shelf} ↔ {right_wall}", shelf, right_wall)

    # Door floors — two joints each, into the same pair of walls a fixed
    # shelf in that column uses. The BOM counts them the same way; this
    # census and cutlist.joinery_lines_for_cabinet_config are one count and
    # a test holds them together.
    from .cabinet import bays_from_config as _bays, opening_stack as _ostack
    floor_labels: list[tuple[str, float]] = []
    for ci, bay in enumerate(_bays(cab_cfg, None)):
        for slot in _ostack(bay):
            if not slot.has_floor:
                continue
            left_wall, right_wall = _column_shelf_walls(ci, n_cols)
            label = ("door floor" if n_cols <= 1
                     else f"col {ci + 1} door floor")
            add(f"{label} ↔ {left_wall}", label, left_wall)
            add(f"{label} ↔ {right_wall}", label, right_wall)
            floor_labels.append((label, slot.floor_z))

    # ── Panel mortise maps ────────────────────────────────────────────────
    panels: list[PanelMortiseMap] = []
    depth = float(cab_cfg.depth)
    # Panel outlines come from cabinet.carcass_panel_dims — the same source
    # the cutlist and the 3D read — so a map can no longer draw a panel the
    # paper does not cut. It used to hardcode ``depth − back_thickness``
    # under a comment calling that "the cutlist convention", which it stopped
    # being when back_capture landed: a rabbet or a dado runs the top AND the
    # bottom full depth, and the map drew them 6 mm short on 6 of the 12
    # cabinets in the saved projects. Mortise positions still span
    # interior_depth from the front edge, safely inside the panel.
    from .cabinet import (back_capture_geometry, carcass_panel_dims,
                          divider_cut_length)
    _dims = carcass_panel_dims(cab_cfg)
    _by_kind: dict[str, list] = {}
    for _p in _dims:
        _by_kind.setdefault(_p.kind, []).append(_p)
    geo = back_capture_geometry(cab_cfg)
    # Interior panels (dividers, fixed shelves) stop at the back's front
    # face whatever the capture; only the perimeter members hold the back.
    interior_panel_depth = float(cab_cfg.interior_depth)
    # These are FINISHED dims. Under hardwood banding the cutlist cuts a core
    # one band thickness shorter on the front edge, but the band is glued and
    # flush-trimmed BEFORE any mortising (see the banding step), so the panel
    # in hand when this map is used measures the finished number. The note
    # says so rather than leaving the reader to guess which face is meant.
    #
    # Per PANEL, not per cabinet: a furniture_top top is never banded (the
    # cap strip covers that edge, and carcass_panel_dims says so with
    # banded_edges=()), so a cabinet-wide note would tell the builder to band
    # the one edge the cutlist row forbids banding — the same pair of
    # contradictory instructions the cutlist comment says it exists to remove,
    # re-created in the document taped to the other machine.
    def _band_note(panel) -> str:
        if band_mode != "hardwood" or not panel.banded_edges:
            return ""
        return (f" Dims are FINISHED — the front edge already carries its "
                f"{band_t:g} mm band (the cutlist cut the core that much "
                f"shorter).")
    # back_style "under_top": the top runs full depth and caps the back —
    # its map must draw the panel at the dims in hand (butt corners only).
    # Nothing caps the back under a machined capture: a rabbet or a dado
    # seats it inside the perimeter, with the top full depth either way.
    under_top = (getattr(cab_cfg, "back_style", "full_height") == "under_top"
                 and not miter)
    caps_back = under_top and not geo.machined
    height = float(cab_cfg.height)
    bottom_t = float(getattr(cab_cfg, "bottom_thickness", side_t))
    top_t = float(getattr(cab_cfg, "top_thickness", side_t))
    shelf_t = float(getattr(cab_cfg, "shelf_thickness", side_t))
    interior_w = float(cab_cfg.interior_width)

    # Side panels (inner face, front edge at LEFT of the drawing).
    if miter:
        side_rows: list[MortiseRow] = [
            MortiseRow("bottom miter (J1/J2)", "h", 0.0, positions, "miter"),
            MortiseRow("top miter (J3/J4)", "h", height, positions, "miter"),
        ]
    else:
        side_rows = [
            MortiseRow("bottom (J1/J2)", "h", _ref_offset(bottom_t),
                       positions, "face"),
            MortiseRow("top (J3/J4)", "h", height - _ref_offset(top_t),
                       positions, "face"),
        ]
    for si, z in enumerate(global_shelves, start=1):
        side_rows.append(MortiseRow(
            f"fixed shelf {si}", "h", float(z) + _ref_offset(shelf_t),
            positions, "face"))
    # Column shelves land on a side only when their column borders it.
    for ci, col in enumerate(cols):
        for si, z in enumerate(
                getattr(col, "fixed_shelf_positions", ()) or (), start=1):
            if n_cols == 1:
                # A single column's shelves join BOTH sides — one shared
                # row on the mirrored side map (review 2026-07-29).
                side_rows.append(MortiseRow(
                    f"col 1 shelf {si} (both sides)", "h",
                    float(z) + _ref_offset(shelf_t), positions, "face"))
                continue
            if ci == 0:
                side_rows.append(MortiseRow(
                    f"col 1 shelf {si} (left side only)", "h",
                    float(z) + _ref_offset(shelf_t), positions, "face"))
            if ci == n_cols - 1:
                side_rows.append(MortiseRow(
                    f"col {n_cols} shelf {si} (right side only)", "h",
                    float(z) + _ref_offset(shelf_t), positions, "face"))
    # A door floor mortises into whatever wall it meets, and on a
    # single-column cabinet — or the outer column of a multi-column one —
    # that wall is a SIDE. Without these rows the map shows a joint in the
    # census that has no hole drawn for it.
    for ci, bay in enumerate(_bays(cab_cfg, None)):
        for slot in _ostack(bay):
            if not slot.has_floor:
                continue
            lw, rw = _column_shelf_walls(ci, n_cols)
            # n_cols is 0 for a single-stack cabinet (it has no ColumnConfig
            # list at all), so "one column" is n_cols <= 1 — the same shape
            # the fixed-shelf rows above use.
            single = n_cols <= 1
            lbl = "door floor" if single else f"col {ci + 1} door floor"
            if single:
                side_rows.append(MortiseRow(
                    f"{lbl} (both sides)", "h",
                    float(slot.floor_z) + _ref_offset(shelf_t),
                    positions, "face"))
            else:
                if lw == "left side":
                    side_rows.append(MortiseRow(
                        f"{lbl} (left side only)", "h",
                        float(slot.floor_z) + _ref_offset(shelf_t),
                        positions, "face"))
                if rw == "right side":
                    side_rows.append(MortiseRow(
                        f"{lbl} (right side only)", "h",
                        float(slot.floor_z) + _ref_offset(shelf_t),
                        positions, "face"))

    _side = _by_kind["side"][0]
    panels.append(PanelMortiseMap(
        panel="side (make 2, mirror-image)", part_id=pid("side"),
        draw_width=_side.width, draw_height=_side.length,
        canonical="side",
        width_label="depth — front edge at left",
        height_label="height" + (" (long-point)" if miter else ""),
        rows=tuple(side_rows),
        note=(("Ends beveled 45° — mortise the MITER FACES top and bottom. "
               if miter else "Mortise the INNER face. ")
              + "The two sides are a mirrored pair — mark them L and R "
              "before machining." + _band_note(_side)),
    ))

    # Divider centrelines from the left END of the top/bottom panel.
    div_centres: list[float] = []
    if n_dividers:
        x = 0.0
        for ci in range(n_dividers):
            x += float(cols[ci].width_mm)
            div_centres.append(x + side_t / 2)
            x += side_t

    tb_width = float(cab_cfg.width) if miter else interior_w
    end_kind = "miter" if miter else "edge"
    # Divider centrelines were measured from the panel's left end; under
    # miter the panel end moves out by side_thickness (long point).
    div_offset = side_t if miter else 0.0
    for pname, canonical in (("bottom", "bottom"), ("top", "top")):
        # End rows carry no on-drawing label — the note text and the row
        # colour identify them, and a label at the panel end collides with
        # the divider labels on narrow drawings.
        rows = [MortiseRow("", "v", 0.0, positions, end_kind),
                MortiseRow("", "v", tb_width, positions, end_kind)]
        for di, cx in enumerate(div_centres, start=1):
            # Row sits _ref_offset past the divider's LEFT face (cx is the
            # centreline, left face = cx − side_t/2) — the same distance the
            # fence/plate puts the divider's edge slots from that face.
            rows.append(MortiseRow(
                f"divider {di} (face mortises)", "v",
                cx - side_t / 2 + _ref_offset(side_t) + div_offset,
                positions, "face"))
        end_txt = ("45° miter faces both ends"
                   if miter else "Edge mortises in both ends (ride the "
                   "fence/plate on the OUTSIDE face)")
        _tb = _by_kind[pname][0]
        panel_draw_depth = _tb.width
        if pname == "top" and caps_back:
            cap_txt = (" Full-depth panel — rear edge flush with the sides; "
                       "it caps the back.")
        elif geo.machined:
            # Both members run full depth under a machined capture, and the
            # back seats into them — say which, so a full-depth panel is
            # never read as a drawing error.
            cap_txt = (f" Full-depth panel — the {geo.capture.replace('_', ' ')} "
                       f"in its {'top face' if pname == 'bottom' else 'underside'} "
                       f"takes the back {geo.engagement:g} mm.")
        else:
            cap_txt = ""
        panels.append(PanelMortiseMap(
            panel=pname, part_id=pid(canonical),
            draw_width=_tb.length, draw_height=panel_draw_depth,
            canonical=canonical,
            width_label=("length (= exterior width, long-point)" if miter
                         else "length (= interior width)"),
            height_label="depth — front edge at bottom",
            rows=tuple(rows),
            note=(f"{end_txt}; face mortises "
                  f"({'top face' if pname == 'bottom' else 'underside'}) "
                  f"{_ref_offset(side_t):g} mm past each divider's "
                  "LEFT-face line — mark left-face lines, not centrelines."
                  f"{cap_txt}{_band_note(_tb)}"
                  if div_centres else f"{end_txt}.{cap_txt}{_band_note(_tb)}"),
        ))

    if n_dividers:
        # Each divider's rows: the two end (edge) mortises, plus a FACE
        # row for every column shelf that lands on it — col d's shelves
        # mortise the divider's LEFT face (divider d borders col d on its
        # left, 1-indexed), col d+1's its RIGHT face (review 2026-07-29
        # M2: these rows were missing and the note said "ENDS only").
        div_rows_by_d: dict[int, list[MortiseRow]] = {}
        for d in range(1, n_dividers + 1):
            rows_d: list[MortiseRow] = [
                MortiseRow("bottom end", "h", 0.0, positions, "edge"),
                MortiseRow("top end", "h", float(cab_cfg.interior_height),
                           positions, "edge"),
            ]
            for ci, side_label in ((d - 1, "left face"),
                                   (d, "right face")):
                for si, z in enumerate(
                        getattr(cols[ci], "fixed_shelf_positions", ())
                        or (), start=1):
                    rows_d.append(MortiseRow(
                        f"col {ci + 1} shelf {si} ({side_label})", "h",
                        float(z) - bottom_t + _ref_offset(shelf_t),
                        positions, "face"))
                # A door floor lands on the divider from whichever column
                # it belongs to, exactly like that column's shelves.
                for slot in _ostack(_bays(cab_cfg, None)[ci]):
                    if not slot.has_floor:
                        continue
                    rows_d.append(MortiseRow(
                        f"col {ci + 1} door floor ({side_label})", "h",
                        float(slot.floor_z) - bottom_t
                        + _ref_offset(shelf_t),
                        positions, "face"))
            div_rows_by_d[d] = rows_d

        # Dividers with identical rows collapse into one "make N" map;
        # ones that differ (different neighbouring shelves) get their own.
        grouped: dict[tuple, list[int]] = {}
        for d, rows_d in div_rows_by_d.items():
            grouped.setdefault(tuple(rows_d), []).append(d)
        for rows_key, ds in grouped.items():
            has_faces = any(r.kind == "face" for r in rows_key)
            if len(grouped) == 1:
                label = f"column divider (make {n_dividers})"
            elif len(ds) > 1:
                label = ("column divider "
                         f"{', '.join(str(d) for d in ds)} (make {len(ds)})")
            else:
                label = f"column divider {ds[0]}"
            _div_panel = (_by_kind.get("divider") or [_by_kind["side"][0]])[0]
            panels.append(PanelMortiseMap(
                panel=label,
                part_id=pid("column_divider"),
                draw_width=interior_panel_depth,
                draw_height=divider_cut_length(cab_cfg),
                canonical="column_divider",
                width_label="depth — front edge at left",
                height_label="height (fits between bottom and top)",
                rows=rows_key,
                note=("Both faces show; edge mortises in the two ENDS "
                      "(ride the fence/plate on the LEFT face — mark it), "
                      "face mortises at each shelf row (face noted per "
                      "row, measured from the shelf's UNDERSIDE line)."
                      if has_faces else
                      "Both faces show; mortise the two ENDS only — ride "
                      "the fence/plate on the LEFT face (mark it).")
                     + _band_note(_div_panel),
            ))

    shelf_like = []
    if global_shelves:
        shelf_like.append(("fixed shelf", len(global_shelves), interior_w))
    for ci, col in enumerate(cols):
        ns = len(getattr(col, "fixed_shelf_positions", ()) or ())
        if ns:
            shelf_like.append(
                (f"col {ci + 1} fixed shelf", ns, float(cols[ci].width_mm)))
    # A door floor is drawn like a shelf — same panel, same two edge
    # mortises — but it gets its own map so the part it names on the
    # drawing is the part on the cutlist, not a shelf the cabinet has not
    # got. Grouped by label so two columns' identical floors collapse.
    _floor_widths: dict[str, list[float]] = {}
    for _lbl, _z in floor_labels:
        _floor_widths.setdefault(_lbl, []).append(_z)
    for ci, bay in enumerate(_bays(cab_cfg, None)):
        lbl = "door floor" if n_cols <= 1 else f"col {ci + 1} door floor"
        if lbl in _floor_widths:
            shelf_like.append((lbl, len(_floor_widths[lbl]),
                               float(bay.interior_width)))
    for label, count, length in shelf_like:
        # A shelf's banding follows the shelf rows, which all band the front
        # edge; fall back to the side when a cfg somehow has no shelf panel.
        _shelf_panel = (_by_kind.get("shelf") or [_by_kind["side"][0]])[0]
        panels.append(PanelMortiseMap(
            panel=f"{label} (make {count})" if count > 1 else label,
            # Length-qualified lookup first: global and column shelf
            # families share the "shelf_1" panel name but are distinct
            # cutlist rows (review 2026-07-29).
            part_id=(pid(f"floor@{round(length, 1)}") or pid("floor")
                     if "door floor" in label else
                     pid(f"shelf_1@{round(length, 1)}") or pid("shelf_1")),
            draw_width=length, draw_height=interior_panel_depth,
            # The row name this drawing is OF. A floor is not a shelf on
            # the paper, so it must not join to one here either — that is
            # exactly the mis-join `canonical` was added to prevent.
            canonical="floor" if "door floor" in label else "shelf",
            width_label="length",
            height_label="depth — front edge at bottom",
            rows=(
                MortiseRow("", "v", 0.0, positions, "edge"),
                MortiseRow("", "v", length, positions, "edge"),
            ),
            note=("Edge mortises in both ends — ride the fence/plate on "
                  "the UNDERSIDE."
                  + (" This is the floor the door closes on, not an "
                     "adjustable shelf — it is glued in with the case."
                     if "door floor" in label else "")
                  ) + _band_note(_shelf_panel),
        ))

    thicknesses = {side_t, bottom_t, top_t}
    if global_shelves or any(
            getattr(c_, "fixed_shelf_positions", ()) or () for c_ in cols):
        thicknesses.add(shelf_t)
    plan = AssemblyPlan(
        cabinet_name=cabinet_name, copies=copies,
        size_key=size_key, size=size, stock_thickness=side_t,
        span=span, per_joint=per_joint, positions=positions,
        joints=joints, panels=panels,
        corner_style=corner_style, miter_placement=miter_placement,
        edge_band_mode=band_mode,
        edge_band_thickness_mm=band_t if band_mode != "none" else 0.0,
        edge_band_strip_width_mm=float(
            (getattr(cab_cfg, "edge_band_stock", None) or {})
            .get("strip_width_mm", 20.0)),
        face_mortised_thicknesses=(),
        panel_thicknesses=tuple(sorted(thicknesses)),
    )
    plan.face_mortised_thicknesses = tuple(
        _face_mortised_thicknesses(plan, cab_cfg))
    plan.steps = _build_steps(plan, cab_cfg)
    plan.drawer_boxes = build_drawer_box_plans(cab_cfg, id_map)
    plan.box_steps = _build_box_steps(plan.drawer_boxes, cab_cfg)
    return plan


def _face_mortised_thicknesses(plan: AssemblyPlan, cab_cfg) -> list[float]:
    """Thicknesses of the panels that actually take a FACE mortise.

    Not every panel does, and a wall claim is only about the ones that do:
    an edge mortise goes into the panel's END and has the whole panel behind
    it. Mirrors how the maps are built — the top and bottom get face rows
    only where there are dividers, the sides only where there are shelves,
    and a divider only where its columns carry shelves.

    Reducing over every thickness instead put a stop-work warning on a case
    whose 12 mm panels are edge-mortised only, and told the builder to
    shorten a plunge that was correct.
    """
    cols = list(getattr(cab_cfg, "columns", []) or [])
    n_div = max(0, len(cols) - 1)
    global_shelves = list(getattr(cab_cfg, "fixed_shelf_positions", []) or [])
    col_shelves = any(getattr(c, "fixed_shelf_positions", ()) or ()
                      for c in cols)
    side_t = float(getattr(cab_cfg, "side_thickness", plan.stock_thickness))
    out: list[float] = []
    if global_shelves or col_shelves:
        out.append(side_t)                       # side faces take shelf rows
    if n_div:
        out.append(float(getattr(cab_cfg, "bottom_thickness", side_t)))
        out.append(float(getattr(cab_cfg, "top_thickness", side_t)))
    if col_shelves and n_div:
        out.append(side_t)                       # divider faces take them too
    return sorted(set(out))


def _wall_text(plan: AssemblyPlan, spec) -> str:
    """What the plunge leaves behind a FACE mortise, on the thinnest panel.

    This read ``plan.stock_thickness`` — which is the SIDE thickness — and
    asserted it of every panel. The top and bottom carry face mortises too
    (their divider rows), so on a case with 18 mm sides and a 12 mm top a
    15 mm plunge does not leave a 3 mm wall: it comes out the far face by
    3 mm. The correct pattern was already two sentences away in the same
    paragraph — ``_fence_text`` reads ``plan.panel_thicknesses``.

    But it must reduce over the FACE-mortised panels only. Reducing over all
    of them cried wolf on a case whose thin panels are edge-mortised.
    """
    depth = spec.mortise_depth_per_side
    ts = sorted(plan.face_mortised_thicknesses)
    if not ts:
        return ("no face mortises in this carcass — every joint is cut into "
                "a panel END, which has the full panel behind it")
    thinnest = ts[0]
    wall = thinnest - depth
    if wall <= 0:
        return (f"this BREAKS THROUGH {abs(wall):g} mm on the "
                f"{thinnest:g} mm panels — reduce the plunge or use a "
                "shorter tenon before cutting any face mortise")
    if len(ts) == 1:
        return f"this leaves a {wall:g} mm wall behind face mortises in " \
               f"{thinnest:g} mm stock"
    return (f"this leaves {wall:g} mm behind a face mortise in the thinnest "
            f"stock here ({thinnest:g} mm); "
            + ", ".join(f"{t:g} mm → {t - depth:g} mm" for t in ts[1:])
            + " elsewhere")


def _fence_text(plan: AssemblyPlan) -> str:
    """Fence-height wording. One 10 mm setting (the DF 500's fixed base
    height, = a 0-offset Domiplate) covers every panel thick enough to take
    it; thin stock gets a centred fallback with an explicit batten offset."""
    ts = plan.panel_thicknesses or (plan.stock_thickness,)
    thin = sorted(t for t in ts if t < BASE_REF_MIN_THICKNESS_MM)
    base = (f"height {DF500_BASE_HEIGHT_MM:g} mm — matching the DF 500's "
            f"fixed {DF500_BASE_HEIGHT_MM:g} mm base height (a 0-offset "
            "Domiplate is the same setting), so fence-cut edge mortises "
            "land in the same plane as base-registered face mortises. "
            "Every slot sits 10 mm from its panel's REFERENCE face; "
            "off-centre in the stock is intentional — do NOT recentre to "
            "t/2")
    if not thin:
        return base + ". ONE setting covers every panel"
    per = " · ".join(
        f"{t:g} mm panels → fence {t / 2:g} mm (centred) and clamp their "
        f"face-row battens {DF500_BASE_HEIGHT_MM - t / 2:g} mm SHORT of "
        "the reference line" for t in thin)
    return (base + f" on panels ≥ {BASE_REF_MIN_THICKNESS_MM:g} mm. "
            f"THIN stock exception: {per}")


def _back_capture_step(geo) -> AssemblyStep:
    """The router work a machined back capture adds, with real numbers.

    Same profile in all three case members, so it is one setup: the fence
    and cutter never move between the sides, the top and the bottom.
    """
    cut = (f"{geo.cut_run:g} mm wide × {geo.cut_depth:g} mm deep")
    if geo.capture == "dado":
        where = (f"a groove {cut}, held {geo.setback:g} mm in from the rear "
                 "edge so there is meat behind it")
        extra = ("The back is trapped once the case closes, so this groove "
                 "is the one cut you cannot fix later — run a test groove "
                 "in scrap and check the back slides without forcing.")
    elif geo.capture == "half_lap":
        where = (f"a rabbet {cut} at the rear edge — half the back's "
                 "thickness, the back's own perimeter rabbet takes the "
                 "other half")
        extra = (f"Then rabbet the BACK panel itself: {geo.lap_run:g} mm in "
                 f"from each of its four edges × {geo.lap_depth:g} mm deep, "
                 "on its FRONT face. The two rabbets lap; test the fit on "
                 "scrap before you cut the real panel.")
    else:
        where = f"a rabbet {cut} at the rear edge"
        extra = ("Cut it a hair deep rather than shallow — the back sitting "
                 "a few tenths proud reads as a bump against the wall.")
    stop = ("STOP the two side cuts: start them level with the top face of "
            "the bottom panel and end them level with the underside of the "
            "top, running "
            f"{geo.engagement:g} mm past each so the back's corners seat. "
            "Run them through and they exit through the side's end grain "
            "as an open notch at the back corner — which is in plain sight "
            "on the finished top and bottom surfaces, since the top and "
            "bottom sit BETWEEN the sides. The top and bottom cuts DO run "
            "right through; their ends butt into the sides and are covered.")
    return AssemblyStep(
        "Machine the back capture",
        f"On the INNER face of both sides, the underside of the top and the "
        f"top face of the bottom, cut {where}. {stop} One fence setting "
        f"covers all four. {extra}")


def build_drawer_box_plans(cab_cfg, id_map=None) -> list[DrawerBoxPlan]:
    """Resolve every drawer box in a carcass, bottom-up per column.

    Boxes come from ``drawer.box_config_for_opening`` — the cutlist's own
    resolver — so a box described here is the box on the parts list.
    """
    from .cabinet import back_capture_geometry
    from .drawer import box_config_for_opening

    id_map = id_map or {}
    interior_depth = cab_cfg.interior_depth  # the datum, not a second derivation
    interior_width = cab_cfg.interior_width

    # A single-column cabinet carries its stack on the config itself; a
    # multi-column one carries one stack per column.
    if cab_cfg.columns:
        columns = [(i + 1, col.width_mm, list(col.openings))
                   for i, col in enumerate(cab_cfg.columns)]
    else:
        columns = [(1, interior_width, list(cab_cfg.openings or []))]

    plans: list[DrawerBoxPlan] = []
    for col_no, col_width, openings in columns:
        drawer_no = 0
        for op in openings:
            if op.opening_type != "drawer":
                continue
            drawer_no += 1
            d = box_config_for_opening(
                cab_cfg, col_width, op.height_mm, interior_depth, op)
            slide = d.slide
            plans.append(DrawerBoxPlan(
                label=(f"column {col_no}, drawer {drawer_no} from the bottom"
                       if len(columns) > 1 else
                       f"drawer {drawer_no} from the bottom"),
                column=col_no,
                position=drawer_no,
                side_length=round(d.side_panel_length, 1),
                side_height=round(d.box_height, 1),
                front_back_length=round(d.front_back_panel_length, 1),
                front_back_height=round(d.box_height, 1),
                stock_thickness=d.side_thickness,
                bottom_length=round(d.bottom_panel_width, 1),
                bottom_width=round(d.bottom_panel_depth, 1),
                bottom_thickness=d.bottom_thickness,
                dado_depth=d.bottom_dado_depth,
                dado_inset=d.bottom_dado_inset,
                joinery=d.joinery_style.value,
                laps_front=d.joinery.laps_front,
                lip=d.joinery.lip,
                engagement=d.joinery.engagement_x,
                box_width=round(d.box_width, 1),
                box_depth=round(d.box_depth, 1),
                box_inside_width=round(d.box_inside_width, 1),
                clearance_reference=d.slide.clearance_reference.value,
                slide_key=d.slide_key,
                slide_length=round(d.box_depth, 1),
                opening_width=round(col_width, 1),
                opening_height=round(op.height_mm, 1),
                part_ids=(id_map.get("drawer_box_side", ""),
                          id_map.get("drawer_box_front", ""),
                          id_map.get("drawer_box_back", ""),
                          id_map.get("drawer_box_bottom", "")),
            ))
    return plans


def _slide_name(boxes: list) -> str:
    """Name the slides the way the hardware BOM does, not by config key.

    A run can mix slides per opening (Charlie puts Movento under the heavy
    drawers only), so this says so rather than quoting the first one as if
    it covered every box.
    """
    from .hardware import get_slide

    def label(key: str) -> str:
        try:
            return get_slide(key).name
        except Exception:          # unknown key — show what was asked for
            return key

    keys = []
    for b in boxes:
        if b.slide_key not in keys:
            keys.append(b.slide_key)
    if len(keys) == 1:
        return f"{label(keys[0])} throughout."
    named = ", ".join(label(k) for k in keys)
    return (f"MIXED slides in this run — {named}. Check each box's row "
            "before you mount anything; they are not interchangeable.")


def _box_table_caption(boxes: list) -> str:
    """One line saying which piece wraps the corner, and what that costs.

    The parts table alone reads as four independent numbers; a builder who
    assumes both the sides and the fronts span the box builds it oversize in
    both directions. Say it once, above the numbers.
    """
    if not boxes:
        return ""
    b = boxes[0]
    joint = b.joinery.replace("_", "-")
    if b.laps_front:
        return (f"{joint} corners are front-lapping: the front and back run "
                f"the FULL box width and wrap the ends of the sides, and each "
                f"side is cut {2 * b.lip:g} mm short of the box depth "
                f"({b.lip:g} mm lip per corner). Confirm the lip on a test "
                f"corner — it belongs to the bit and fence, not to this "
                f"drawing. Every assembled box must measure its Box column.")
    seat = (f" and seats {b.engagement:g} mm into each side"
            if b.engagement else " and butts between the sides")
    return (f"{joint} corners are side-lapping: the sides run the FULL box "
            f"depth, and the front and back are cut short{seat}. Every "
            f"assembled box must measure its Box column.")


def _groove_step(bottoms: list, depths: list, insets: list) -> AssemblyStep:
    """The bottom-groove step, told by the run rather than by box zero.

    A groove has three dimensions and this step used to print two of them.
    The missing one is the WIDTH — which IS the bottom panel's thickness
    (``drawer.make_drawer_side`` cuts the dado ``bottom_thickness`` across)
    and the only one of the three that varies from box to box. So on a run
    mixing 6 mm and 12 mm bottoms the page said "Every part of every box
    takes the same groove", directly under a step warning that the bottoms
    are NOT all the same, and never named the setting that differs. Cutting
    the run at one width puts a 12 mm panel at a 6 mm groove.
    """
    uniform = len(bottoms) == 1 and len(depths) == 1 and len(insets) == 1

    def _mm(vals):
        return " and ".join(f"{v:g} mm" for v in vals)

    if uniform:
        return AssemblyStep(
            "Groove for the bottoms — one saw setup, every part",
            f"Every part of every box takes the same groove: "
            f"{bottoms[0]:g} mm wide (the bottom panel's own thickness) × "
            f"{depths[0]:g} mm deep, {insets[0]:g} mm up from the bottom "
            "edge, on the inside face, running the full length. Cut them "
            "all in one session — the setup is the work, the cutting is "
            "nothing.",
            checklist=(
                f"Set the fence so the groove sits {insets[0]:g} mm from the "
                "BOTTOM edge, and confirm which edge that is on each part "
                "before it goes through.",
                f"Set the cutter to {bottoms[0]:g} mm wide — the groove has "
                "to match the bottom it holds, and that is the dimension "
                "this run has only one value of.",
                "Cut a test groove in offcut and fit an actual bottom "
                "panel: snug enough to hold, loose enough to slide in dry. "
                "A bottom forced into a tight groove will bow the box and "
                "you will not get it square.",
                "Run all four parts of every box. A box missing one groove "
                "does not show up until glue-up, when it is too late.",
            ))

    varies = []
    if len(bottoms) > 1:
        varies.append(f"width ({_mm(bottoms)})")
    if len(depths) > 1:
        varies.append(f"depth ({_mm(depths)})")
    if len(insets) > 1:
        varies.append(f"height off the bottom edge ({_mm(insets)})")
    same = []
    if len(depths) == 1:
        same.append(f"{depths[0]:g} mm deep")
    if len(insets) == 1:
        same.append(f"{insets[0]:g} mm up from the bottom edge")
    same_txt = (" " + " and ".join(same) + " on every part."
                if same else "")
    n_setups = len(bottoms) if len(bottoms) > 1 else max(
        len(depths), len(insets))
    return AssemblyStep(
        f"Groove for the bottoms — {n_setups} setups, NOT one",
        "This run does not take a single groove setting: the "
        + ", ".join(varies) + " changes from box to box."
        + same_txt
        + " The groove's width is the thickness of the bottom it holds, so "
        "sort the boxes by bottom thickness and cut each group in its own "
        "pass. A 12 mm bottom will not enter a 6 mm groove, and a 6 mm "
        "bottom rattles in a 12 mm one.",
        checklist=(
            "Sort every part into groups by its box's bottom thickness "
            "BEFORE any of them goes near the saw — the parts do not say "
            "which group they belong to once they are mixed.",
            "For each group: set the cutter to that group's width ("
            + _mm(bottoms) + "), cut a test groove in offcut, and fit an "
            "actual bottom from that group before running the rest.",
            "Re-check the fence height after every cutter change. It is the "
            "setting that does NOT vary here, which is exactly the one that "
            "gets knocked without being noticed.",
            "Run all four parts of every box. A box missing one groove does "
            "not show up until glue-up, when it is too late.",
        ))


def _build_box_steps(boxes: list, cab_cfg) -> list[AssemblyStep]:
    """Bench sequence for the drawer boxes.

    Written as its own run of steps because boxes are batch work: every
    setup below is made once and every box in the project goes through it,
    which is the opposite of the carcass's one-case-at-a-time order.
    """
    if not boxes:
        return []

    def _set(attr):
        """Every distinct value of ``attr`` across the run, sorted.

        Box steps used to read their numbers off ``boxes[0]`` and assert
        them of every box — "Every part of every box takes the same
        groove" — while the very same function was already computing
        ``bottoms`` as a set in order to warn that they are NOT all the
        same. Two adjacent steps on one printed page said opposite things.
        Nothing here reads box zero any more.
        """
        return sorted({getattr(b, attr) for b in boxes})

    def _mm(vals) -> str:
        return " and ".join(f"{v:g} mm" for v in vals)

    stocks = _set("stock_thickness")
    depths = _set("dado_depth")
    insets = _set("dado_inset")
    joints = sorted({b.joinery.replace("_", "-") for b in boxes})
    lips = _set("lip")
    bottoms = _set("bottom_thickness")
    heights = _set("side_height")
    n = len(boxes)
    laps_front = any(b.laps_front for b in boxes)
    all_lap_front = all(b.laps_front for b in boxes)
    inside_ref = any(b.clearance_reference == "inside" for b in boxes)
    widths = sorted({(b.opening_width, b.box_width, b.box_inside_width)
                     for b in boxes})

    # The cutlist cuts pre-finished stock for a workshop build; this step
    # said "Baltic birch" whatever the config, so the doc named a different
    # material from the parts list it is printed next to.
    stock_txt = ("pre-finished Baltic birch"
                 if getattr(cab_cfg, "drawer_box_prefinished", False)
                 else "Baltic birch")
    t_txt = _mm(stocks)
    joint = " and ".join(joints)
    lip = lips[0]
    all_txt = "all " if len(stocks) == 1 and len(joints) == 1 else ""

    bottom_txt = (f"{bottoms[0]:g} mm" if len(bottoms) == 1 else
                  _mm(bottoms) +
                  " — check each box's row, they are NOT all the same")

    steps = [
        AssemblyStep(
            "Drawer boxes — read before cutting",
            f"{n} boxes, {all_txt}{t_txt} {stock_txt} with {joint} corners "
            f"and a captured bottom in {bottom_txt}. Box parts have no show "
            "face to "
            "choose — birch ply is the same both sides — but they DO have an "
            "inside and an outside, and every joint and groove below is cut "
            "on the INSIDE face. Mark the inside of all four parts of a box "
            "before you cut anything.",
            checklist=(
                "Sort the parts into per-box sets and keep each set together "
                f"— there are {len(heights)} different box heights here, and "
                "parts from two boxes are near-identical until they are not.",
                "Mark the INSIDE face of every part. All corner joinery and "
                "the bottom groove are cut on that face; a part machined on "
                "the wrong face is scrap.",
                (("Fronts and backs run the FULL box width and wrap the "
                  "ends of the sides; the sides are cut short of the box "
                  "depth to leave room for them. Both numbers are on the "
                  "parts list — do not re-derive either from the box size.")
                 if laps_front else
                 ("Sides run the box DEPTH; fronts and backs are cut SHORT "
                  "of the box width, because they sit between the sides. "
                  "Both numbers are on the parts list — do not re-derive "
                  "either from the box size.")),
            ) + ((
                "The width these parts add up to is set by the runners, and "
                "the runners constrain the INSIDE of the box, not the "
                "outside: " + "; ".join(
                    f"a {ow:g} mm opening takes a box {bw:g} outside / "
                    f"{iw:g} inside" for ow, bw, iw in widths) +
                ". Measure a finished box across the inside and check it "
                "against that number before you cut a batch.",
            ) if inside_ref else ())),
        _groove_step(bottoms, depths, insets),
        AssemblyStep(
            f"Cut the {joint} corners",
            (f"All four corners of each box are {joint}. Set the cut once and "
             "run every part through it — with the parts already marked "
             "inside-face, this is repetitive rather than delicate, which is "
             "exactly what you want before a batch of glue-ups."
             + (f" The parts list assumes a {lip:g} mm lip — the wall left "
                f"outboard of the socket on the front and back. That lip is "
                f"a property of your bit and fence, not of the drawing, and "
                f"the box runs two of them longer than its sides: CONFIRM IT "
                f"ON A TEST CORNER before cutting the sides to length."
                if laps_front else "")),
            checklist=(
                "Dial the setup in on offcut of the SAME stock, and assemble "
                "a test corner. Check it closes with hand pressure and sits "
                "at 90° with no gap at the shoulder.",
                "Keep the test corner on the bench as a reference for the "
                "rest of the run.",
                "Cut every part, keeping the box sets separate. Confirm as "
                "you go that the joint lands on the inside face.",
            )),
        AssemblyStep(
            "Assemble the boxes",
            "One box at a time, and dry-fit each before glue — the bottom is "
            "captured, so it goes in during assembly and there is no fixing "
            "a forgotten one afterwards.",
            checklist=(
                "Dry-assemble the box with its bottom in the grooves and "
                "measure it against the box size on its own row — the first "
                "box is where a corner setup that eats the wrong amount "
                "shows up, and it is cheap to fix there and expensive to "
                "fix after a batch of glue-ups.",
                "Check it is square by the diagonals and that it sits flat.",
                "Glue the corners only. The bottom FLOATS in its grooves — "
                "it is what keeps the box square, and gluing it in is how "
                "you turn a seasonal movement into a split panel.",
                "Clamp, check both diagonals, and correct before the glue "
                "tacks. A box out of square binds in its slides.",
                "Check the box sits flat on the bench, not just square. A "
                "twisted box runs rough no matter how good the slides are.",
                "Wipe the squeeze-out inside the box now — you will not get "
                "a chisel into those corners cleanly once it cures.",
            )),
        AssemblyStep(
            "Fit the slides",
            f"{_slide_name(boxes)} Mount to the box and the carcass only "
            "after the carcass is cured and standing on its final feet — a "
            "case fitted on its side rarely stays true.",
            checklist=(
                ("Check the box height and the INSIDE width against the "
                 "opening one last time before drilling anything — "
                 + "; ".join(f"{ow:g} mm opening → {iw:g} mm inside the box"
                             for ow, _bw, iw in widths)
                 + ". An undermount runner will not align to a box that "
                   "misses this."
                 if inside_ref else
                 "Check the box height and width against the opening one "
                 "last time before drilling anything."),
                "Mount, then run each box in and out fully before the faces "
                "go on. Adjust for bind now, while the box is still empty "
                "and light.",
            )),
    ]
    return steps


def _glue_up_step(plan: AssemblyPlan, miter: bool,
                  has_inner: bool) -> AssemblyStep:
    """The irreversible step, written as a sequence rather than a paragraph.

    Ordering matters and is not obvious: glue goes in the mortises and on
    the mating edge (not on the tenon, which swells and jams), the inner
    structure is assembled and squared before the sides trap it, and the
    diagonals are checked while the glue still moves.
    """
    if has_inner:
        stages = [
            "STAGE 1 — inner structure. Glue the dividers and fixed shelves "
            "into the BOTTOM panel. Glue goes into BOTH mortises and onto "
            "the mating edge — not onto the tenon itself, which swells on "
            "contact and can jam before it is seated.",
            "Seat each inner panel fully and check it stands square to the "
            "bottom. This is your last chance to correct one: the top panel "
            "traps them all.",
            "Cap with the TOP panel, working the mortises onto the tenons "
            "across the whole width at once rather than dropping one end "
            "first.",
        ]
    else:
        stages = [
            "Glue the case in ONE stage — with no dividers or shelves there "
            "is no inner structure to assemble first. Glue goes into BOTH "
            "mortises and onto the mating edge, never onto the tenon, which "
            "swells on contact and can jam before it is seated.",
        ]

    if miter:
        stages += [
            "STAGE 2 — fold the sides on. Run painter's tape across each "
            "miter's OUTSIDE corner as a hinge, glue the miter faces and "
            "tenons, and fold the case closed.",
            "Pull the corners tight with band clamps, with bar clamps and "
            "cauls across the case as backup. A mitered corner closes with "
            "pressure ACROSS the joint, which a bar clamp alone cannot give.",
        ]
    else:
        stages += [
            "STAGE 2 — add the sides. Bring each side on square to the "
            "panel ends rather than pivoting it in on one corner.",
            "Clamp across every joint line, with a caul under each clamp "
            "head so the pressure spreads instead of denting the show face. "
            "Snug, not crushed: a joint that needs heavy pressure to close "
            "is telling you something is wrong, and you have seconds to act "
            "on it.",
        ]

    stages += [
        "Measure both diagonals IMMEDIATELY and rack the case square before "
        "the glue tacks. This is the whole reason the previous steps were "
        "rehearsed — from here you have a couple of minutes at most.",
        "Check the case is not twisted as well as square: sight across the "
        "front edges, or set it on the reference surface and look for a "
        "rock. Square and twisted is still wrong.",
        "Wipe every trace of squeeze-out with the damp rag while it is wet, "
        "inside corners especially. Cured glue has to be chiselled off and "
        "any film left behind will refuse finish and show as a pale patch.",
    ]
    return AssemblyStep(
        "Glue up in stages",
        "Read this through before opening the glue. Work in the order "
        "below — the sequence is what keeps the case square, and there is "
        "no version of this step you can pause halfway through.",
        checklist=tuple(stages))


def _show_face_step(plan: AssemblyPlan, cab_cfg) -> AssemblyStep:
    """Decide show faces and grain BEFORE anything is cut or mortised.

    Sheet goods have a better face, and once a panel is mortised it is
    committed — a reference face on the wrong side puts every slot on the
    show side of the panel. This has to be the first thing that happens,
    which is why it precedes the inventory step rather than being a clause
    inside it.
    """
    on_legs = getattr(cab_cfg, "leg_count", 0) > 0
    has_doors = any(op.opening_type in ("door", "door_pair")
                    for col in (cab_cfg.columns or [])
                    for op in col.openings) or any(
        op.opening_type in ("door", "door_pair")
        for op in (cab_cfg.openings or []))
    n_shelf = len(cab_cfg.fixed_shelf_positions or ())
    n_div = max(len(cab_cfg.columns or []) - 1, 0)

    seen = [
        "SIDES — the OUTSIDE face. On a run of cabinets pushed together, "
        "the buried sides are the exception: use your worst faces there and "
        "save the good ones for the ends.",
        "TOP — the upper face. It is the most-looked-at surface on the "
        "piece; pick the best sheet face you have for it.",
    ]
    seen.append(
        "BOTTOM — the UNDERSIDE shows, because the piece stands on legs."
        if on_legs else
        "BOTTOM — the upper face (inside the cabinet); the underside is "
        "against the floor and never seen.")
    if n_div:
        seen.append(
            f"DIVIDERS ({n_div}) — BOTH faces are seen, one from each "
            "opening. There is no hidden side to hide a flaw on.")
    if n_shelf:
        seen.append(
            f"FIXED SHELVES ({n_shelf}) — the top face is seen looking down, "
            "the underside looking up. Treat both as show.")
    if has_doors:
        seen.append(
            "INTERIOR — this cabinet has doors, so everything inside is on "
            "display whenever they are open.")

    return AssemblyStep(
        "Choose show faces and grain direction — before any other cut",
        "Plywood has a better face and a worse one, and grain has a "
        "direction. Both are decided now, not at glue-up: once a panel is "
        "mortised it is committed, and a reference face marked on the wrong "
        "side puts every slot on the side you meant to show. Lay all the "
        "carcass panels out, good face up, and work through the list before "
        "picking up a router. Where the two rules fight, grain wins — a "
        "flipped board reads as a mistake from across the room, while a "
        "minor face flaw does not.",
        checklist=tuple(seen) + (
            "GRAIN runs along each panel's LENGTH — the first dimension on "
            "every cutlist row, and the direction the parts were laid out "
            "on the sheet. On the sides that is floor-to-ceiling; on the "
            "top, bottom and shelves it runs left to right across the "
            "front. Stand the panels up in their finished positions and "
            "check the grain flows the same way across the whole front.",
            "Mark each panel on its HIDDEN face: the part ID, an arrow for "
            "grain direction, and which edge is the FRONT. Everything "
            "downstream — the reference faces, the mortise rows, the "
            "banding — is measured from the front edge, so a panel that "
            "loses track of its front is a panel you will cut backwards.",
        ))


def _build_steps(plan: AssemblyPlan, cab_cfg) -> list[AssemblyStep]:
    s = plan.size
    t = plan.stock_thickness
    pos_txt = ", ".join(f"{p:.0f}" for p in plan.positions)
    n_joints = len(plan.joints)
    miter = plan.corner_style == "miter"
    n_butt = sum(1 for j in plan.joints if j.kind == "butt")
    under_top = (getattr(cab_cfg, "back_style", "full_height") == "under_top"
                 and not miter)
    # Every carcass this doc covers is floating-tenon (butt), so the back is
    # never rabbeted in: the sides run full depth while the top, bottom,
    # dividers, and shelves stop at depth − back_thickness, and that setback
    # IS the pocket the back seats in. Name only the parts this cabinet has —
    # a plain box has no dividers or fixed shelves to glue the back to.
    has_divider = any("divider" in pm.panel for pm in plan.panels)
    has_shelf = any("shelf" in pm.panel for pm in plan.panels)
    # "under_top" caps the back with the top panel, so the top is a landing
    # face there, not a glue edge.
    back_edges = _and_list(
        (["top"] if not under_top else []) + ["bottom"]
        + (["dividers"] if has_divider else [])
        + (["fixed shelves"] if has_shelf else []))
    # back_capture: a machined capture seats the back INSIDE the case
    # perimeter, in grooves or rabbets cut in the sides, top and bottom —
    # so it is held by those four members, not glued onto the rear edges of
    # the interior panels.
    from .cabinet import back_capture_geometry
    geo = back_capture_geometry(cab_cfg)
    if geo.machined:
        seat = "grooves" if geo.capture == "dado" else "rabbets"
        back_seat_txt = (
            f"test-fit the back panel in its {seat} — it seats "
            f"{geo.engagement:g} mm into all four members"
            + (", and it must go in NOW: once the case is closed it cannot "
               "be fitted" if geo.captive else
               ", dropping in from behind"))
    else:
        back_seat_txt = (
            "slide the back panel up into its pocket from the carcass's "
            f"bottom end (it runs behind the {back_edges}) until it seats "
            "against the underside of the full-depth top"
            if under_top else
            "test-fit the back panel in its rear pocket — it drops in from "
            f"behind against the rear edges of the {back_edges}, flush with "
            "the back edges of the sides")
    # Panels that actually carry face (red) rows — drives the face-mortise
    # step text so it never claims rows that don't exist (or vice versa).
    face_panels = [pm.panel for pm in plan.panels
                   if any(r.kind == "face" for r in pm.rows)]

    steps = [
        _show_face_step(plan, cab_cfg),
        AssemblyStep(
            "Inventory and label the parts",
            "Pull every carcass panel from the cutlist and pencil its part ID "
            "on the face you marked HIDDEN in step 1, near a back corner. "
            "Sides are a mirrored pair — mark L / R now, and note that their "
            "show faces point in opposite directions."
            + (" Beveled panels are cut LONG-POINT — verify each miter's "
               "long point lands on the OUTSIDE face before any mortising."
               if miter else "")),
        AssemblyStep(
            "Mark every joint",
            f"Dry-stack the carcass and mark all {n_joints} joints with a "
            "cabinetmaker's triangle across each seam so every mating pair "
            "and its orientation is unambiguous after the panels separate. "
            "Mark the FRONT edge of every panel — every mortise position "
            "below is measured from the front."),
    ]

    if plan.edge_band_mode == "hardwood":
        bt = plan.edge_band_thickness_mm
        steps.append(AssemblyStep(
            "Band the front edges (hardwood strips)",
            f"Glue {bt:g} mm hardwood strips (ripped {plan.edge_band_strip_width_mm:g} mm wide, proud "
            "both faces) to the FRONT edges of every carcass panel — "
            "sides, top, bottom, dividers, fixed shelves. Clamp, cure, "
            "then flush-trim both faces. Do this BEFORE any mortising: "
            "the panels were cut short by the band thickness, and every "
            "mortise position references the BANDED front edge. Face "
            "panels get their 4-edge banding during finishing, not here; "
            "footage is on the cutlist hardware BOM."))

    steps.append(AssemblyStep(
        "Set up the Domino machine",
        f"DF 500 with the {s.tenon_thickness:g} mm cutter. Plunge depth "
        f"{s.mortise_depth_per_side:g} mm ({_wall_text(plan, s)}). "
        f"Fence 90°, {_fence_text(plan)}. "
        "Width setting: TIGHT for the front mortise of every joint, "
        "middle (slotted) for all others — the front pair registers the "
        "joint flush; slotted mates absorb tolerance."))

    if n_butt:
        thin_t = [th for th in (plan.panel_thicknesses or (t,))
                  if th < BASE_REF_MIN_THICKNESS_MM]
        steps.append(AssemblyStep(
            "Mark the reference faces",
            "Pencil-mark ONE reference face on every interior panel: "
            "bottom and top — the OUTSIDE face; fixed shelves — the "
            "UNDERSIDE; dividers — the LEFT face (as drawn in the maps). "
            "Every mortise in this build sits "
            f"{DF500_BASE_HEIGHT_MM:g} mm from these faces — the fence "
            "rides them for edge mortises, and their layout lines take the "
            "batten for face mortises. Flushness comes from this one "
            "shared reference, not from centring the slots."))
        steps.append(AssemblyStep(
            "Cut the edge mortises (butt joints)",
            f"Batch all square panel-END mortises first (dividers, fixed "
            f"shelves{'' if miter else ', bottom, top'}): centres at "
            f"{pos_txt} mm from the front edge, "
            f"{plan.per_joint} per end. Ride the fence (or 0-offset plate) "
            "on each panel's REFERENCE face, machine base toward the end, "
            "and reference every row from the front edge. "
            + (f"The one {DF500_BASE_HEIGHT_MM:g} mm setting covers every "
               "edge mortise." if not thin_t else
               "Thin panels take the centred fallback — see the machine "
               "table for their fence heights and batten offsets.")))
        steps.append(AssemblyStep(
            "Cut the face mortises",
            "Strike each row's REFERENCE LINE with a square off the front "
            "edge: for a side's top/bottom row that line is the panel END "
            "itself; for a shelf row it is the shelf's UNDERSIDE height; "
            "for a divider row, the divider's LEFT-face position. Clamp a "
            "straight batten exactly ON the line, stand the DF 500 on its "
            "base INSIDE the panel's footprint, butt the base against the "
            "batten, and plunge at each centre mark — the fixed base "
            f"height drops the cutter axis {DF500_BASE_HEIGHT_MM:g} mm "
            "past the line, exactly where the fence put the mating edge "
            "slots. Never register face rows off the fence numbers or by "
            "eye against the mating panel. (Section drawings in the "
            "'Registration' pages show this joint in cross-section.) "
            + (f"Face rows land on: {', '.join(face_panels)} — the RED "
               "rows in the mortise maps. "
               if face_panels else "")
            + ("The corners are mitered — their mortises come in the next "
               "step, not here. " if miter else "")
            + "Same tight/slotted pattern: front mortise tight, the rest "
            "slotted."))

    if miter and plan.miter_placement is not None:
        mp = plan.miter_placement
        steps.append(AssemblyStep(
            "Cut the miter mortises (corners)",
            f"Tilt the DF 500 fence to 45° and stand it on each miter "
            f"face. Set the fence so the mortise centreline sits "
            f"{mp.from_heel:.1f} mm from the HEEL (inside corner; "
            f"{mp.from_long_point:.1f} mm from the long point) along the "
            f"{mp.face_width:.1f} mm face — the plunge drifts toward the "
            f"show face, leaving {mp.show_face_wall:.1f} mm of wall there "
            f"at the full {mp.depth:.0f} mm plunge. Same centres from the "
            f"front edge ({pos_txt} mm), same tight/slotted pattern. DIAL "
            "IN ON A SCRAP MITER FIRST — a misplaced miter mortise exits "
            "the show face."))

    steps.append(AssemblyStep(
        "Prep interior surfaces",
        "Vacuum every mortise. Sand interior faces now — flat panels "
        "sand and finish far easier than an assembled box. If the "
        "interiors get finish, mask the glue faces at each joint."))
    if geo.machined:
        steps.append(_back_capture_step(geo))
    steps.extend([
        AssemblyStep(
            "DRY FIT — full carcass, no glue",
            f"Assemble the complete carcass with {plan.dry_fit_tenons_needed} "
            f"{DRY_FIT_TENON_NAME} — print at least that many "
            f"({plan.size_key} size): {DRY_FIT_TENON_URL} . The reduced "
            "cross-section inserts and pulls out by hand, which is the whole "
            "point: you can take this apart. Work the list in order and fix "
            "anything that fights you NOW — every one of these problems is "
            "cheap here and permanent ten minutes after the glue is open.",
            checklist=(
                "Seat every joint fully, by hand. A tenon that needs a mallet "
                "dry will need a clamp wet, and a joint that needs a clamp to "
                "close is a joint that will spring back open. Pare the mortise "
                "rather than forcing it.",
                "Check the case sits FLAT. Put it on your reference surface "
                "and try to rock it — a case that rocks now is a case that "
                "cures twisted, and no amount of clamping at glue-up fixes a "
                "twist you did not find here.",
                "Measure both diagonals across the front opening. They must "
                "match within 1 mm. If they do not, the fault is a joint that "
                "is not fully seated or a panel that is out of square — find "
                "which before going on.",
                f"Back: {back_seat_txt}.",
                "Confirm the drawer openings with an actual slide, or a "
                "spacer cut to the slide's required clearance. Measure at the "
                "FRONT and again at the BACK of each opening — a divider that "
                "is square at the front and leaning at the back still "
                "measures right where you first checked.",
                "Rehearse the clamp layout completely: every clamp opened to "
                "length, every caul cut and placed, and the case pulled up "
                "the way you intend to do it for real. Count the clamps. "
                "Running out mid-glue-up is the classic way to lose a case.",
                "Take a photo of the assembled dry fit. It is the reference "
                "you will want when the parts are apart and covered in glue.",
            )),
        AssemblyStep(
            "Disassemble and stage",
            "Pull the dry-fit tenons and stage everything within arm's reach "
            "before opening the glue. Glue-up is a race against open time and "
            "staging is how you win it — this step is the difference between "
            "a calm assembly and a ruined case.",
            checklist=(
                f"Real beech tenons counted out: {plan.tenons_per_cabinet} per "
                "cabinet. Count them into a tray, do not pull them from the "
                "box as you go.",
                "Panels laid out in assembly order, show faces already "
                "oriented, so no panel has to be turned over or worked out "
                "mid-glue-up.",
                "Clamps opened to length and within reach; cauls stacked at "
                "the joints they serve.",
                "Glue, a brush or roller, a damp rag, and a bucket of water. "
                "Squeeze-out wipes off in seconds while wet and has to be "
                "chiselled off once cured — and any smear left behind will "
                "show as a pale patch under finish.",
                "Your square and a tape for the diagonals, already set down "
                "on the bench — not across the shop.",
                "Work out the open time of the glue you are using before you "
                "start (PVA is typically 5–10 minutes at shop temperature, "
                "less when it is warm). If the stages below will not fit "
                "inside it, plan to glue in two sessions rather than rushing "
                "one.",
            )),
        _glue_up_step(plan, miter, has_divider or has_shelf),
        AssemblyStep(
            "Square with the back, then cure",
            (("Set the back into its grooves as the case goes together — it "
              "cannot go in afterwards. Glue only the top and bottom edges "
              "if the back is solid wood, so it can move in the grooves; "
              "plywood can be glued all round. A square back holds the "
              "carcass square as it cures, and its edges are buried in the "
              "grooves, so nothing shows from any angle. Re-check "
              "diagonals, wipe squeeze-out, leave clamped for the glue's "
              "clamp time (30–60 min PVA) and unstressed for 24 h."
              if geo.captive else
              "You have a choice here, because a rabbeted back drops in "
              "from behind and is not trapped by the case. Fitting it while "
              "the clamps are on is the better job — a square panel pulls "
              "the whole carcass square and holds it there through the "
              "cure, which is more reliable than racking by diagonals "
              "alone. Fitting it after the cure keeps the glue-up to five "
              "panels and off the clock. Take the first if the case fought "
              "you at all on the diagonals; take the second if it went "
              "together sweetly and you would rather not add a step to the "
              "race. Either way its edges are buried in the rabbets, so "
              "nothing shows from any angle.")
             if geo.machined else
             "While the clamps are on, slide the back panel up into its "
             "pocket from the carcass's bottom end until it seats against "
             "the underside of the full-depth top, then glue/pin it into "
             f"the rear edges of the {back_edges} — "
             "a square back holds the carcass square as it cures. Its top "
             "edge is capped by the top panel: nothing shows from above. "
             "Re-check diagonals, wipe squeeze-out, leave clamped for the "
             "glue's clamp time (30–60 min PVA) and unstressed for 24 h."
             if under_top else
             "While the clamps are on, drop the back panel into the rear "
             "pocket from behind and glue/pin it to the rear edges of the "
             f"{back_edges}, flush with the back "
             "edges of the sides — a square back holds the carcass square as "
             "it cures. Its top edge lands in the top plane, so it IS visible "
             "from above. Re-check diagonals, wipe squeeze-out, leave clamped "
             "for the glue's clamp time (30–60 min PVA) and unstressed for "
             "24 h."),
            checklist=(
                "Glue and pin (or screw) the back home, checking the "
                "diagonals as you fix it — the back can only square the case "
                "if it is square itself and you fasten it that way."
                if not geo.captive and geo.machined else
                "Re-check both diagonals with the back in place.",
                "Wipe the squeeze-out one more time, inside corners "
                "included, before it starts to skin.",
                "Leave it CLAMPED for the glue's clamp time — 30–60 minutes "
                "for PVA at shop temperature.",
                "Then leave it UNSTRESSED for 24 hours before you machine, "
                "hang doors, or stand it up loaded. PVA reaches clamp "
                "strength quickly and full strength slowly; a case racked "
                "while it is green will hold that rack permanently.",
            )),
    ])

    if plan.edge_band_mode == "hot_melt":
        steps.append(AssemblyStep(
            "Iron on the edge banding",
            "With the carcass cured, iron pre-glued banding onto every "
            "exposed front edge (sides, top, bottom, dividers, fixed "
            "shelves), then trim flush and break the corners. Face panels "
            "get all four edges banded before hardware goes on. Roll "
            "footage is on the cutlist hardware BOM."))

    return steps


# ─── Rendering helpers ────────────────────────────────────────────────────────


def _inch(mm: float) -> str:
    from .cutlist import _inch_frac
    return _inch_frac(mm)


def _machine_rows(plan: AssemblyPlan) -> list[tuple[str, str]]:
    s = plan.size
    t = plan.stock_thickness
    return [
        ("Machine", s.machine),
        ("Cutter", f"{s.tenon_thickness:.0f} mm"),
        ("Tenon", f"Domino {plan.size_key.replace('x', '×')} beech — "
                  f"Festool {s.part_number} "
                  f"(pack of {DOMINO_PACK_QUANTITIES[plan.size_key]})"),
        ("Plunge depth", f"{s.mortise_depth_per_side:.0f} mm each part "
                         f"({_wall_text(plan, s)})"),
        ("Fence", f"90° · {_fence_text(plan)}"),
        ("Width setting", "TIGHT for the front mortise of each joint; "
                          "middle (slotted) for all others"),
        ("Registration", "Every mortise measured from the FRONT edge on "
                         "both mating parts; in the thickness direction, "
                         f"every slot sits {DF500_BASE_HEIGHT_MM:g} mm from "
                         "the panel's REFERENCE face (top/bottom: outside "
                         "· shelves: underside · dividers: left face)"),
        ("Mortise slot", f"{s.mortise_length:.1f} × {s.mortise_width:.1f} mm"),
    ] + ([
        ("Miter corners", "Fence tilted 45°, standing on the miter face — "
                          "dial in on a scrap miter first"),
        ("Miter placement",
         f"centreline {plan.miter_placement.from_heel:.1f} mm from "
         f"the HEEL ({plan.miter_placement.from_long_point:.1f} mm from "
         f"the long point) along the {plan.miter_placement.face_width:.1f} mm "
         f"face — {plan.miter_placement.show_face_wall:.1f} mm show-face "
         f"wall at the full {plan.miter_placement.depth:.0f} mm plunge"),
    ] if plan.corner_style == "miter" and plan.miter_placement is not None
       else [])


# ─── SVG mortise maps (HTML) ─────────────────────────────────────────────────

_SVG_W = 460.0   # px drawing width per panel

#: Mortise-row colours by kind (shared by the SVG and PDF renderers).
_ROW_COLOURS = {"face": "#c0392b", "edge": "#2471a3", "miter": "#8e44ad"}


# ─── Registration explainer (section diagrams) ──────────────────────────────
# Charlie couldn't picture how an internal divider's mortises line up from
# the flat maps alone (2026-08-02): the maps show WHERE the rows go, but not
# how the two halves of a joint register in the thickness direction. These
# three section views — cut the face slots, cut the edge slots, assembled —
# make the shared 10 mm reference visible. Scene geometry is built ONCE
# (primitive shapes in mm coordinates, y-up) and walked by a small SVG
# renderer for HTML and a small Flowable for the PDF.

_EXPLAIN_GREY = "#b9b1a3"      # battens / fence / machine bodies
_EXPLAIN_WOOD = "#f7efd8"      # panel fill (matches the mortise maps)
_EXPLAIN_WOOD_EDGE = "#7a6a4f"
_EXPLAIN_REF = "#c0392b"       # the reference line — same red as face rows
_EXPLAIN_TENON = "#6d4c2f"


def _registration_case(plan: AssemblyPlan) -> Optional[dict]:
    """Pick the most instructive butt joint for the explainer's labels.

    Dividers first (the case that confuses — mid-panel, nothing to hook a
    fence on), then fixed shelves (same geometry), then plain corners.
    Returns None when the plan has no butt joints (all-miter corners only).
    """
    if not any(j.kind == "butt" for j in plan.joints):
        return None
    names = " ".join(j.name for j in plan.joints)
    if "divider" in names:
        return {
            "case": "divider",
            "host": "bottom (or top) panel",
            "standing": "divider",
            "ref_line": "divider's LEFT-face layout line",
            "ride_face": "LEFT face",
            "footprint": "divider stands here",
        }
    if "shelf" in names:
        return {
            "case": "shelf",
            "host": "side panel (or divider), lying on the bench",
            "standing": "fixed shelf",
            "ref_line": "shelf's UNDERSIDE layout line",
            "ride_face": "UNDERSIDE",
            "footprint": "shelf lands here",
        }
    return {
        "case": "corner",
        "host": "side panel",
        "standing": "bottom / top panel",
        "ref_line": "panel END (= outside-face line)",
        "ride_face": "OUTSIDE face",
        "footprint": "top/bottom sits here",
    }


def _registration_scenes(plan: AssemblyPlan) -> Optional[dict]:
    """Build the explainer: intro prose + three primitive-shape scenes.

    Primitives (mm coordinates, y-up; renderers scale and flip as needed):
      ("rect",  x, y, w, h, fill, stroke, dashed)
      ("line",  x1, y1, x2, y2, colour, dashed)
      ("text",  x, y, string, anchor)          anchor: start|middle|end
      ("dim",   x1, x2, y, label)              horizontal dimension
      ("dimv",  y1, y2, x, label)              vertical dimension
    """
    labels = _registration_case(plan)
    if labels is None:
        return None
    t = plan.stock_thickness
    ref = _ref_offset(t)
    off = DF500_BASE_HEIGHT_MM
    thin = t < BASE_REF_MIN_THICKNESS_MM

    intro = [
        "Every butt joint here is two sets of slots: FACE slots plunged "
        "into one panel's face, and EDGE slots plunged into the mating "
        "panel's end. They line up because both are measured from the SAME "
        "reference — a marked reference face and its layout line: "
        "top/bottom → outside face · fixed shelves → underside · dividers "
        "→ left face.",
        f"Why {off:g} mm? The DF 500 standing on its base puts the cutter "
        f"axis a fixed {off:g} mm above the surface — that number is "
        "machined into the tool and registers every FACE slot. So the "
        f"fence is set to the same {off:g} mm for the EDGE slots (a "
        "0-offset Domiplate rides at exactly this height), and the two "
        "cuts meet. Setting the fence to t/2 to \"centre\" the slot breaks "
        "this — the halves miss by 1 mm in 18 mm stock and a tight joint "
        "will not close.",
        "The slots sit slightly off-centre in the stock. That is fine: "
        "flushness comes from sharing one reference, not from centring. "
        "Do not recentre.",
    ]
    if labels["case"] == "divider":
        # Count actual dividers by edge part ("divider N ↔ bottom/top") —
        # matching on joint NAME would also catch shelf ↔ divider joints.
        div_names = sorted({j.edge_part for j in plan.joints
                            if j.edge_part.startswith("divider")})
        n_div = len(div_names)
        intro.append(
            "For an internal divider there is no panel end to feel for — "
            "the LEFT-face layout line on the top/bottom panel does that "
            "job. Strike it where the divider's left face goes (the "
            "mortise maps mark these lines), clamp the batten ON it, and "
            "the base drops the face slots exactly where the divider's "
            f"edge slots expect them ({n_div} divider"
            f"{'s' if n_div != 1 else ''} in this build, all cut the same "
            "way).")
    if thin:
        intro.append(
            f"THIN-STOCK NOTE: this build has {t:g} mm carcass panels — "
            f"too thin for the {off:g} mm reference, so the machine table "
            f"uses centred slots (fence {t / 2:g} mm) and the batten "
            f"clamps {off - t / 2:g} mm SHORT of the line instead. The "
            "diagrams below show the standard ≥ 15 mm geometry.")

    # Diagrams always draw the standard ≥ 15 mm geometry (18 mm stock,
    # 10 mm reference) — the thin-stock intro note covers the exception.
    host_t = 18.0                        # drawn host thickness
    lx = 120.0                           # reference line x
    ax = lx + off                        # cutter axis x

    # Scene A — face slots on the host panel.
    a: list = [
        ("rect", 0, 0, 250, host_t, _EXPLAIN_WOOD, _EXPLAIN_WOOD_EDGE, False),
        ("text", 4, host_t / 2 - 3, labels["host"], "start"),
        # Footprint of the standing panel (ghost).
        ("rect", lx, host_t, host_t, 58, "none", _EXPLAIN_WOOD_EDGE, True),
        ("text", lx + host_t + 4, host_t + 46, labels["footprint"],
         "start"),
        # Reference line.
        ("line", lx, -6, lx, 92, _EXPLAIN_REF, True),
        ("text", lx - 4, 84, labels["ref_line"] + " — batten HERE", "end"),
        # Batten (outside the footprint) and machine (inside it).
        ("rect", lx - 26, host_t, 26, 24, _EXPLAIN_GREY,
         _EXPLAIN_WOOD_EDGE, False),
        ("text", lx - 13, host_t + 9, "batten", "middle"),
        ("rect", lx, host_t, 96, 42, "#e8e4dc", _EXPLAIN_WOOD_EDGE, False),
        ("text", lx + 48, host_t + 28, "DF 500 standing on its base,",
         "middle"),
        ("text", lx + 48, host_t + 17, "front butted against the batten",
         "middle"),
        # Cutter axis + face slot (15 mm plunge → 3 mm wall in 18 mm).
        ("line", ax, host_t + 42, ax, 1, _ROW_COLOURS["face"], True),
        ("rect", ax - 2.5, host_t - 15, 5, 15, "none",
         _ROW_COLOURS["face"], False),
        ("dim", lx, ax, -12, f"{(ax - lx):g} mm — fixed base height"),
    ]
    scene_a = {"title": "1 · FACE slots — host panel, machine on its base",
               "w": 250.0, "h": 100.0, "pad_b": 22.0, "prims": a}

    # Scene B — edge slots on the standing panel (lying flat, ref face up).
    end_x = 170.0
    slot_cy = host_t - (ax - lx)          # 10 mm down from the ref face
    b: list = [
        ("rect", 0, 0, end_x, host_t, _EXPLAIN_WOOD, _EXPLAIN_WOOD_EDGE,
         False),
        ("text", 4, host_t / 2 - 3, labels["standing"] + " (lying flat)",
         "start"),
        # Reference face on top, fence/plate riding it.
        ("line", 0, host_t, end_x, host_t, _EXPLAIN_REF, False),
        ("rect", 40, host_t, 130, 9, _EXPLAIN_GREY, _EXPLAIN_WOOD_EDGE,
         False),
        ("text", 105, host_t + 12,
         f"fence @ {(ax - lx):g} mm (or 0-offset plate) rides the "
         f"{labels['ride_face']} — the REFERENCE face", "middle"),
        # Edge slot plunged into the end (15 mm deep).
        ("rect", end_x - 15, slot_cy - 2.5, 15, 5, "none",
         _ROW_COLOURS["edge"], False),
        ("text", end_x - 16, slot_cy - 1.5, "plunge into the end", "end"),
        ("dimv", host_t, slot_cy, end_x + 10, f"{(ax - lx):g}"),
    ]
    scene_b = {"title": "2 · EDGE slots — standing panel, fence or plate "
                        "on the reference face",
               "w": 200.0, "h": 52.0, "pad_b": 8.0, "prims": b}

    # Scene C — assembled: one shared axis, flush by construction.
    c: list = [
        ("rect", 0, 0, 250, host_t, _EXPLAIN_WOOD, _EXPLAIN_WOOD_EDGE,
         False),
        ("text", 4, host_t / 2 - 3, labels["host"], "start"),
        ("rect", lx, host_t, host_t, 74, _EXPLAIN_WOOD, _EXPLAIN_WOOD_EDGE,
         False),
        ("text", lx + host_t / 2, host_t + 78, labels["standing"],
         "middle"),
        ("line", lx, -6, lx, 100, _EXPLAIN_REF, True),
        ("text", lx - 4, 92, "your layout line", "end"),
        # The 5×30 tenon: 15 mm in each part, on the shared axis.
        ("rect", ax - 2.5, host_t - 15, 5, 30, _EXPLAIN_TENON,
         _EXPLAIN_TENON, False),
        ("rect", ax - 2.5, host_t - 15, 5, 15, "none",
         _ROW_COLOURS["face"], False),
        ("rect", ax - 2.5, host_t, 5, 15, "none", _ROW_COLOURS["edge"],
         False),
        ("dim", lx, ax, -12, f"{(ax - lx):g} mm in BOTH parts"),
        ("text", lx + host_t + 6, host_t + 40,
         f"{labels['ride_face']} lands ON the line — flush, no math",
         "start"),
    ]
    scene_c = {"title": "3 · Assembled — the two cuts meet on one axis",
               "w": 250.0, "h": 118.0, "pad_b": 22.0, "prims": c}

    return {"intro": intro, "scenes": [scene_a, scene_b, scene_c],
            "case": labels["case"]}


def _scene_svg(scene: dict) -> str:
    """Render one explainer scene as an inline SVG (y flipped to y-down)."""
    from xml.sax.saxutils import escape

    S = 2.1                     # px per mm
    pad = 14.0
    pad_b = scene.get("pad_b", 8.0) * S
    W = scene["w"] * S + 2 * pad
    H = scene["h"] * S + pad + pad_b

    def X(mm: float) -> float:
        return pad + mm * S

    def Y(mm: float) -> float:
        return pad + scene["h"] * S - mm * S

    out = [f'<svg viewBox="0 0 {W:.0f} {H:.0f}" '
           f'style="max-width:{W:.0f}px" xmlns="http://www.w3.org/2000/svg">']
    for p in scene["prims"]:
        kind = p[0]
        if kind == "rect":
            _, x, y, w, h, fill, stroke, dashed = p
            dash = ' stroke-dasharray="5 3"' if dashed else ""
            out.append(
                f'<rect x="{X(x):.1f}" y="{Y(y + h):.1f}" '
                f'width="{w * S:.1f}" height="{h * S:.1f}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="1"{dash}/>')
        elif kind == "line":
            _, x1, y1, x2, y2, colour, dashed = p
            dash = ' stroke-dasharray="5 3"' if dashed else ""
            out.append(
                f'<line x1="{X(x1):.1f}" y1="{Y(y1):.1f}" x2="{X(x2):.1f}" '
                f'y2="{Y(y2):.1f}" stroke="{colour}" '
                f'stroke-width="1.2"{dash}/>')
        elif kind == "text":
            _, x, y, s, anchor = p
            out.append(
                f'<text x="{X(x):.1f}" y="{Y(y):.1f}" font-size="10" '
                f'fill="#333" text-anchor="{anchor}">{escape(s)}</text>')
        elif kind == "dim":
            _, x1, x2, y, label = p
            yy = Y(y)
            out.append(
                f'<line x1="{X(x1):.1f}" y1="{yy:.1f}" x2="{X(x2):.1f}" '
                f'y2="{yy:.1f}" stroke="#333" stroke-width="1"/>')
            for xx in (x1, x2):
                out.append(
                    f'<line x1="{X(xx):.1f}" y1="{yy - 4:.1f}" '
                    f'x2="{X(xx):.1f}" y2="{yy + 4:.1f}" stroke="#333" '
                    'stroke-width="1"/>')
            out.append(
                f'<text x="{X((x1 + x2) / 2):.1f}" y="{yy + 13:.1f}" '
                f'font-size="10" font-weight="bold" fill="#333" '
                f'text-anchor="middle">{escape(label)}</text>')
        elif kind == "dimv":
            _, y1, y2, x, label = p
            xx = X(x)
            out.append(
                f'<line x1="{xx:.1f}" y1="{Y(y1):.1f}" x2="{xx:.1f}" '
                f'y2="{Y(y2):.1f}" stroke="#333" stroke-width="1"/>')
            for yy in (y1, y2):
                out.append(
                    f'<line x1="{xx - 4:.1f}" y1="{Y(yy):.1f}" '
                    f'x2="{xx + 4:.1f}" y2="{Y(yy):.1f}" stroke="#333" '
                    'stroke-width="1"/>')
            out.append(
                f'<text x="{xx + 6:.1f}" y="{Y((y1 + y2) / 2) + 3:.1f}" '
                f'font-size="10" font-weight="bold" fill="#333" '
                f'text-anchor="start">{escape(label)}</text>')
    out.append("</svg>")
    return "".join(out)


def _panel_svg(pm: PanelMortiseMap) -> str:
    """Render one panel mortise map as an inline SVG string."""
    from xml.sax.saxutils import escape

    pad = 46.0
    scale = (_SVG_W - 2 * pad) / max(pm.draw_width, 1.0)
    ph = pm.draw_height * scale
    # Keep tall panels readable but bounded.
    max_h = 360.0
    if ph > max_h:
        scale *= max_h / ph
        ph = max_h
    pw = pm.draw_width * scale
    W = pw + 2 * pad
    H = ph + 2 * pad

    def X(mm: float) -> float:
        return pad + mm * scale

    def Y(mm: float) -> float:      # mm measured from panel BOTTOM edge
        return pad + ph - mm * scale

    parts: list[str] = [
        f'<svg viewBox="0 0 {W:.0f} {H:.0f}" '
        f'style="max-width:{W:.0f}px" xmlns="http://www.w3.org/2000/svg">',
        f'<rect x="{pad}" y="{pad}" width="{pw:.1f}" height="{ph:.1f}" '
        'fill="#f7efd8" stroke="#7a6a4f" stroke-width="1" rx="3"/>',
    ]
    for row in pm.rows:
        colour = _ROW_COLOURS.get(row.kind, "#2471a3")
        if row.axis == "h":
            y = Y(row.offset)
            y = min(max(y, pad + 3), pad + ph - 3)
            parts.append(
                f'<line x1="{pad}" y1="{y:.1f}" x2="{pad + pw:.1f}" '
                f'y2="{y:.1f}" stroke="{colour}" stroke-width="0.7" '
                'stroke-dasharray="4 3"/>')
            for p in row.positions:
                cx = X(p)
                parts.append(
                    f'<ellipse cx="{cx:.1f}" cy="{y:.1f}" rx="6" ry="2.6" '
                    f'fill="{colour}"/>')
            if row.label:
                parts.append(
                    f'<text x="{pad + pw + 4:.1f}" y="{y + 3:.1f}" '
                    f'font-size="9" fill="{colour}">{escape(row.label)}</text>')
        else:
            x = X(row.offset)
            x = min(max(x, pad + 3), pad + pw - 3)
            parts.append(
                f'<line x1="{x:.1f}" y1="{pad}" x2="{x:.1f}" '
                f'y2="{pad + ph:.1f}" stroke="{colour}" stroke-width="0.7" '
                'stroke-dasharray="4 3"/>')
            for p in row.positions:
                cy = Y(p)
                parts.append(
                    f'<ellipse cx="{x:.1f}" cy="{cy:.1f}" rx="2.6" ry="6" '
                    f'fill="{colour}"/>')
            if row.label:
                parts.append(
                    f'<text x="{x:.1f}" y="{pad - 6:.1f}" font-size="9" '
                    f'fill="{colour}" text-anchor="middle">'
                    f'{escape(row.label)}</text>')
    # Position ticks along the front edge (bottom of drawing for "v" rows,
    # left edge for "h" rows) — one dimension set serves every row.
    axis_h = any(r.axis == "h" for r in pm.rows)
    for p in pm.rows[0].positions:
        if axis_h:
            cx = X(p)
            parts.append(
                f'<text x="{cx:.1f}" y="{pad + ph + 14:.1f}" font-size="8.5" '
                f'fill="#333" text-anchor="middle">{p:.0f}</text>')
        else:
            cy = Y(p)
            parts.append(
                f'<text x="{pad - 5:.1f}" y="{cy + 3:.1f}" font-size="8.5" '
                f'fill="#333" text-anchor="end">{p:.0f}</text>')
    lbl = "mortise centres, mm from front edge"
    parts.append(
        f'<text x="{W / 2:.1f}" y="{H - 4:.1f}" font-size="8.5" '
        f'fill="#666" text-anchor="middle">{lbl}</text>')
    parts.append("</svg>")
    return "".join(parts)


# ─── HTML renderer ────────────────────────────────────────────────────────────


def generate_assembly_html(
    plans: list[AssemblyPlan],
    project_name: str = "Cabinet",
) -> str:
    from datetime import date
    from xml.sax.saxutils import escape

    css = """
    body{font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;
         margin:24px auto;max-width:1080px;color:#222;line-height:1.45}
    h1{font-size:26px} h2{font-size:20px;margin-top:38px;
       border-bottom:2px solid #2c3e50;padding-bottom:4px}
    h3{font-size:15px;margin-top:26px}
    table{border-collapse:collapse;margin:10px 0;width:100%}
    th{background:#2c3e50;color:#fff;padding:6px 9px;font-size:12.5px;
       text-align:left}
    td{border:1px solid #ccc;padding:5px 9px;font-size:12.5px}
    tr:nth-child(even) td{background:#f6f6f6}
    .mm{font-weight:700} .in{color:#555}
    .step{margin:14px 0;padding:10px 14px;border-left:4px solid #2c3e50;
          background:#f8f8f8;border-radius:0 6px 6px 0}
    .step.dryfit{border-left-color:#c0392b;background:#fdf3f2}
    .step b{display:block;margin-bottom:3px}
    .checklist{margin:7px 0 2px 0;padding-left:22px}
    .checklist li{margin:5px 0;line-height:1.45}
    .maps{display:flex;flex-wrap:wrap;gap:18px}
    .map{border:1px solid #ddd;border-radius:8px;padding:10px 12px}
    .map h4{margin:2px 0 6px;font-size:13px}
    .map .note{font-size:11px;color:#666;max-width:460px}
    .legend{font-size:12px;color:#444}
    .legend .f{color:#c0392b} .legend .e{color:#2471a3}
    a{color:#2471a3}
    """
    out: list[str] = [
        # Full document skeleton with an explicit charset — without it,
        # browsers sniff windows-1252 and every em-dash renders as "â€”"
        # (Charlie's printout, 2026-07-28). The other HTML generators
        # (layout, banding, viewer) already declare it.
        "<!DOCTYPE html><html><head>",
        '<meta charset="utf-8">',
        f"<title>Carcass Assembly — {escape(project_name)}</title>",
        f"<style>{css}</style>",
        "</head><body>",
        f"<h1>Carcass Assembly — {escape(project_name)}</h1>",
        f"<p>Generated {date.today().isoformat()} · floating-tenon "
        "(Festool Domino) construction · all positions measured from the "
        "<b>front edge</b>.</p>",
    ]

    for plan in plans:
        copies = f" × {plan.copies}" if plan.copies > 1 else ""
        out.append(f"<h2>{escape(plan.cabinet_name)}{copies}</h2>")

        # Machine setup
        out.append("<h3>Machine setup</h3><table>")
        out.append("<tr><th>Setting</th><th>Value</th></tr>")
        for k, v in _machine_rows(plan):
            out.append(f"<tr><td>{escape(k)}</td><td>{escape(v)}</td></tr>")
        out.append("</table>")

        # Joint schedule
        out.append("<h3>Joint schedule</h3>")
        out.append(
            f"<p>Every joint spans <span class='mm'>{plan.span:.0f} mm</span> "
            f"<span class='in'>({_inch(plan.span)}\")</span> front-to-back "
            f"and takes <b>{plan.per_joint}</b> tenons at the same centres — "
            "<span class='mm'>"
            + ", ".join(f"{p:.0f}" for p in plan.positions)
            + " mm</span> <span class='in'>("
            + ", ".join(f"{_inch(p)}" for p in plan.positions)
            + " in)</span> from the front edge.</p>")
        out.append("<table><tr><th>#</th><th>Joint</th>"
                   "<th>Edge mortises in</th><th>Face mortises in</th></tr>")
        for j in plan.joints:
            if j.kind == "miter":
                a = f"{j.edge_part} — 45° miter face"
                b = f"{j.face_part} — 45° miter face"
            else:
                a, b = j.edge_part, j.face_part
            out.append(
                f"<tr><td>J{j.index}</td><td>{escape(j.name)}</td>"
                f"<td>{escape(a)}</td>"
                f"<td>{escape(b)}</td></tr>")
        out.append("</table>")

        # Consumables
        out.append("<h3>Consumables</h3><table>")
        out.append("<tr><th>Item</th><th>Count</th><th>Notes</th></tr>")
        out.append(
            f"<tr><td>Beech Domino {plan.size_key.replace('x', '×')} "
            f"(Festool {plan.size.part_number})</td>"
            f"<td>{plan.tenons_total}</td>"
            f"<td>{plan.per_joint} per joint × {len(plan.joints)} joints"
            + (f" × {plan.copies} cabinets" if plan.copies > 1 else "")
            + "</td></tr>")
        out.append(
            f"<tr><td><a href='{DRY_FIT_TENON_URL}'>PETG dry-fit tenons "
            f"({plan.size_key})</a></td>"
            f"<td>{plan.dry_fit_tenons_needed}</td>"
            "<td>reduced-size prints by paulengel — one cabinet dry-fits "
            "at a time; reused across copies</td></tr>")
        out.append("</table>")

        # Registration explainer — three section views showing how the
        # face and edge slots share one reference (Charlie, 2026-08-02).
        reg = _registration_scenes(plan)
        if reg is not None:
            out.append("<h3>Registration — how the two halves of a joint "
                       "line up</h3>")
            for para in reg["intro"]:
                out.append(f"<p class='reg'>{escape(para)}</p>")
            out.append("<div class='maps'>")
            for scene in reg["scenes"]:
                out.append("<div class='map'>")
                out.append(f"<h4>{escape(scene['title'])}</h4>")
                out.append(_scene_svg(scene))
                out.append("</div>")
            out.append("</div>")

        # Mortise maps
        out.append("<h3>Mortise maps</h3>")
        legend = ("<p class='legend'><span class='f'>◗ red = face "
                  "mortises</span> · <span class='e'>◗ blue = edge "
                  "mortises (into the panel end)</span>")
        if plan.corner_style == "miter":
            legend += (" · <span style='color:#8e44ad'>◗ purple = miter "
                       "mortises (into the 45° beveled end)</span>")
        out.append(legend + "</p>")
        out.append("<div class='maps'>")
        for pm in plan.panels:
            pid_txt = f" · {escape(pm.part_id)}" if pm.part_id else ""
            out.append("<div class='map'>")
            out.append(f"<h4>{escape(pm.panel)}{pid_txt}</h4>")
            out.append(_panel_svg(pm))
            note = f"{pm.note} " if pm.note else ""
            out.append(
                f"<div class='note'>{escape(note)}Drawing: "
                f"{escape(pm.width_label)} × {escape(pm.height_label)} "
                f"({pm.draw_width:.0f} × {pm.draw_height:.0f} mm).</div>")
            out.append("</div>")
        out.append("</div>")

        # Steps
        out.append("<h3>Assembly sequence</h3>")
        for i, st in enumerate(plan.steps, start=1):
            cls = "step dryfit" if st.title.startswith("DRY FIT") else "step"
            body = escape(st.body).replace(
                escape(DRY_FIT_TENON_URL),
                f"<a href='{DRY_FIT_TENON_URL}'>printables.com/model/"
                "689403</a>")
            items = "".join(
                f"<li>{escape(item)}</li>" for item in st.checklist)
            listing = f"<ol class='checklist'>{items}</ol>" if items else ""
            out.append(
                f"<div class='{cls}'><b>Step {i} — {escape(st.title)}</b>"
                f"{body}{listing}</div>")

        # Drawer boxes — their own section: separate parts, separate setups,
        # and a different day's work from the carcass.
        if plan.drawer_boxes:
            out.append(f"<h3>Drawer boxes — {escape(plan.cabinet_name)} "
                       f"({len(plan.drawer_boxes)})</h3>")
            out.append(
                f"<p class='legend'>{escape(_box_table_caption(plan.drawer_boxes))}</p>")
            out.append("<table><tr><th>Box</th><th>Sides (×2)</th>"
                       "<th>Front + back</th><th>Bottom</th>"
                       "<th>Box (finished)</th><th>Opening</th></tr>")
            for b in plan.drawer_boxes:
                out.append(
                    "<tr>"
                    f"<td>{escape(b.label)}</td>"
                    f"<td class='mm'>{b.side_length:g} × {b.side_height:g}"
                    f" × {b.stock_thickness:g}</td>"
                    f"<td class='mm'>{b.front_back_length:g} × "
                    f"{b.front_back_height:g} × {b.stock_thickness:g}</td>"
                    f"<td class='mm'>{b.bottom_length:g} × {b.bottom_width:g}"
                    f" × {b.bottom_thickness:g}</td>"
                    f"<td class='mm'>{b.box_width:g} W × {b.box_depth:g} D</td>"
                    f"<td class='in'>{b.opening_width:g} × "
                    f"{b.opening_height:g}</td></tr>")
            out.append("</table>")
            for i, st in enumerate(plan.box_steps, start=1):
                items = "".join(
                    f"<li>{escape(item)}</li>" for item in st.checklist)
                listing = (f"<ol class='checklist'>{items}</ol>"
                           if items else "")
                out.append(
                    f"<div class='step'><b>Box step {i} — "
                    f"{escape(st.title)}</b>{escape(st.body)}{listing}</div>")

    out.append("</body></html>")
    return "\n".join(out)


# ─── PDF renderer ─────────────────────────────────────────────────────────────


def generate_assembly_pdf(
    plans: list[AssemblyPlan],
    project_name: str = "Cabinet",
    paper: str = "letter",
) -> bytes:
    """Portrait PDF mirroring the HTML content — US Letter by default,
    ``paper="a4"`` for A4."""
    from .cutlist import _REPORTLAB_AVAILABLE
    if not _REPORTLAB_AVAILABLE:
        raise ImportError(
            "reportlab is required for PDF export. "
            "Install with: uv pip install reportlab")

    import io
    from datetime import date
    from xml.sax.saxutils import escape as xesc

    from .cutlist import (
        _getSampleStyleSheet, _HexColor, _KeepTogether, _PageBreak,
        _Paragraph, _ParagraphStyle, _paper_size, _rl_mm,
        _SimpleDocTemplate, _Spacer, _Table, _TableStyle, _Flowable,
    )

    # Portrait with ~10 pt body text: these pages are read at the bench,
    # not at a desk — readability beats density (Charlie, 2026-08-02;
    # the old landscape layout ran 7.5–8.5 pt and was hard to read).
    PAGE = _paper_size(paper)
    MARGIN = 14 * _rl_mm
    CW = PAGE[0] - 2 * MARGIN

    styles = _getSampleStyleSheet()
    title_sty = _ParagraphStyle("at", parent=styles["Title"], fontSize=20,
                                leading=24, spaceAfter=3 * _rl_mm)
    h1 = _ParagraphStyle("ah1", parent=styles["Heading1"], fontSize=15,
                         leading=18, spaceBefore=4 * _rl_mm,
                         spaceAfter=2 * _rl_mm)
    h2 = _ParagraphStyle("ah2", parent=styles["Heading2"], fontSize=12.5,
                         leading=15, spaceBefore=3 * _rl_mm,
                         spaceAfter=1.5 * _rl_mm)
    norm = _ParagraphStyle("an", parent=styles["Normal"], fontSize=10,
                           leading=13)
    cell = _ParagraphStyle("acell", parent=styles["Normal"], fontSize=9,
                           leading=11.5)
    step_sty = _ParagraphStyle("astep", parent=styles["Normal"], fontSize=10,
                               leading=13.5, spaceAfter=2.5 * _rl_mm)
    # Checklist items: same 10 pt body (Charlie reads these at the bench and
    # 7.5 pt was unreadable), indented so the sequence is scannable.
    check_sty = _ParagraphStyle("acheck", parent=step_sty,
                                leftIndent=7 * _rl_mm,
                                spaceAfter=1.2 * _rl_mm)

    def tbl_style() -> _TableStyle:
        return _TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), _HexColor("#2c3e50")),
            ("TEXTCOLOR", (0, 0), (-1, 0), _HexColor("#ffffff")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [_HexColor("#f5f5f5"), _HexColor("#ffffff")]),
            ("GRID", (0, 0), (-1, -1), 0.5, _HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])

    def wc(text: str) -> "_Paragraph":
        return _Paragraph(xesc(text), cell)

    class _MapFlowable(_Flowable):
        """Draws one PanelMortiseMap on the canvas."""

        def __init__(self, pm: PanelMortiseMap, avail_w: float,
                     avail_h: float) -> None:
            super().__init__()
            self._pm = pm
            self.width = avail_w
            self.height = avail_h

        def draw(self) -> None:
            c = self.canv
            pm = self._pm
            pad = 40.0
            scale = min((self.width - 2 * pad) / max(pm.draw_width, 1.0),
                        (self.height - 2 * pad) / max(pm.draw_height, 1.0))
            pw = pm.draw_width * scale
            ph = pm.draw_height * scale
            x0 = (self.width - pw) / 2
            y0 = (self.height - ph) / 2

            c.setFillColor(_HexColor("#f7efd8"))
            c.setStrokeColor(_HexColor("#7a6a4f"))
            c.setLineWidth(0.8)
            c.roundRect(x0, y0, pw, ph, 2, fill=1, stroke=1)

            for row in pm.rows:
                col = _HexColor(_ROW_COLOURS.get(row.kind, "#2471a3"))
                c.setStrokeColor(col)
                c.setFillColor(col)
                c.setLineWidth(0.5)
                c.setDash(3, 2)
                if row.axis == "h":
                    y = min(max(y0 + row.offset * scale, y0 + 2),
                            y0 + ph - 2)
                    c.line(x0, y, x0 + pw, y)
                    c.setDash()
                    for p in row.positions:
                        c.ellipse(x0 + p * scale - 4, y - 1.8,
                                  x0 + p * scale + 4, y + 1.8,
                                  fill=1, stroke=0)
                    if row.label:
                        c.setFont("Helvetica", 8)
                        c.drawString(x0 + pw + 3, y - 2.5, row.label)
                else:
                    x = min(max(x0 + row.offset * scale, x0 + 2),
                            x0 + pw - 2)
                    c.line(x, y0, x, y0 + ph)
                    c.setDash()
                    for p in row.positions:
                        c.ellipse(x - 1.8, y0 + p * scale - 4,
                                  x + 1.8, y0 + p * scale + 4,
                                  fill=1, stroke=0)
                    if row.label:
                        c.setFont("Helvetica", 8)
                        c.drawCentredString(x, y0 + ph + 5, row.label)
                c.setDash()

            c.setFillColor(_HexColor("#333333"))
            c.setFont("Helvetica", 7.5)
            axis_h = any(r.axis == "h" for r in pm.rows)
            for p in pm.rows[0].positions:
                if axis_h:
                    c.drawCentredString(x0 + p * scale, y0 - 8, f"{p:.0f}")
                else:
                    c.drawRightString(x0 - 3, y0 + p * scale - 2, f"{p:.0f}")
            c.setFillColor(_HexColor("#666666"))
            c.setFont("Helvetica", 7.5)
            c.drawCentredString(self.width / 2, 2,
                                "mortise centres, mm from front edge")

    class _SceneFlowable(_Flowable):
        """Draws one registration-explainer scene (mm coords, y-up —
        matching the canvas, so no flip)."""

        def __init__(self, scene: dict, avail_w: float) -> None:
            super().__init__()
            self._sc = scene
            self._pad = 10.0
            # Fit the scene to the available width (portrait pages are
            # narrower than the scenes' natural 2 pt/mm).
            self._S = min(2.0, (avail_w - 2 * self._pad) / scene["w"])
            self._pad_b = scene.get("pad_b", 8.0) * self._S
            self.width = scene["w"] * self._S + 2 * self._pad
            self.height = (scene["h"] * self._S + self._pad
                           + self._pad_b)

        def draw(self) -> None:
            c = self.canv
            S, pad = self._S, self._pad

            def X(mm: float) -> float:
                return pad + mm * S

            def Y(mm: float) -> float:
                return self._pad_b + mm * S

            for p in self._sc["prims"]:
                kind = p[0]
                if kind == "rect":
                    _, x, y, w, h, fill, stroke, dashed = p
                    c.setDash(4, 3) if dashed else c.setDash()
                    c.setStrokeColor(_HexColor(stroke))
                    c.setLineWidth(0.8)
                    if fill == "none":
                        c.rect(X(x), Y(y), w * S, h * S, fill=0, stroke=1)
                    else:
                        c.setFillColor(_HexColor(fill))
                        c.rect(X(x), Y(y), w * S, h * S, fill=1, stroke=1)
                    c.setDash()
                elif kind == "line":
                    _, x1, y1, x2, y2, colour, dashed = p
                    c.setDash(4, 3) if dashed else c.setDash()
                    c.setStrokeColor(_HexColor(colour))
                    c.setLineWidth(0.9)
                    c.line(X(x1), Y(y1), X(x2), Y(y2))
                    c.setDash()
                elif kind == "text":
                    _, x, y, s, anchor = p
                    c.setFillColor(_HexColor("#333333"))
                    c.setFont("Helvetica", 8)
                    if anchor == "middle":
                        c.drawCentredString(X(x), Y(y), s)
                    elif anchor == "end":
                        c.drawRightString(X(x), Y(y), s)
                    else:
                        c.drawString(X(x), Y(y), s)
                elif kind == "dim":
                    _, x1, x2, y, label = p
                    yy = Y(y)
                    c.setStrokeColor(_HexColor("#333333"))
                    c.setLineWidth(0.8)
                    c.setDash()
                    c.line(X(x1), yy, X(x2), yy)
                    for xx in (x1, x2):
                        c.line(X(xx), yy - 3, X(xx), yy + 3)
                    c.setFillColor(_HexColor("#333333"))
                    c.setFont("Helvetica-Bold", 8)
                    c.drawCentredString(X((x1 + x2) / 2), yy - 10, label)
                elif kind == "dimv":
                    _, y1, y2, x, label = p
                    xx = X(x)
                    c.setStrokeColor(_HexColor("#333333"))
                    c.setLineWidth(0.8)
                    c.setDash()
                    c.line(xx, Y(y1), xx, Y(y2))
                    for yy in (y1, y2):
                        c.line(xx - 3, Y(yy), xx + 3, Y(yy))
                    c.setFillColor(_HexColor("#333333"))
                    c.setFont("Helvetica-Bold", 8)
                    c.drawString(xx + 4, Y((y1 + y2) / 2) - 2.5, label)

    buf = io.BytesIO()
    doc = _SimpleDocTemplate(
        buf, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title=f"Carcass Assembly — {project_name}")

    story: list = []
    story.append(_Paragraph(
        f"Carcass Assembly — {xesc(project_name)}", title_sty))
    story.append(_Paragraph(
        f"Generated {date.today().isoformat()} · floating-tenon (Festool "
        "Domino) construction · all positions measured from the "
        "<b>front edge</b>.", norm))

    for pi, plan in enumerate(plans):
        if pi:
            story.append(_PageBreak())
        copies = f"  ×{plan.copies}" if plan.copies > 1 else ""
        story.append(_Paragraph(
            f"{xesc(plan.cabinet_name)}{copies}", h1))

        story.append(_Paragraph("Machine setup", h2))
        data = [["Setting", "Value"]] + [
            [wc(k), wc(v)] for k, v in _machine_rows(plan)]
        t = _Table(data, colWidths=[CW * 0.18, CW * 0.82])
        t.setStyle(tbl_style())
        story.append(t)

        story.append(_Paragraph("Joint schedule", h2))
        story.append(_Paragraph(
            f"Every joint spans <b>{plan.span:.0f} mm</b> "
            f"({xesc(_inch(plan.span))}&quot;) front-to-back and takes "
            f"<b>{plan.per_joint}</b> tenons at the same centres — <b>"
            + ", ".join(f"{p:.0f}" for p in plan.positions)
            + " mm</b> ("
            + ", ".join(_inch(p) for p in plan.positions)
            + " in) from the front edge.", norm))
        data = [["#", "Joint", "Edge mortises in", "Face mortises in"]] + [
            [f"J{j.index}", wc(j.name),
             wc(f"{j.edge_part} — 45° miter face" if j.kind == "miter"
                else j.edge_part),
             wc(f"{j.face_part} — 45° miter face" if j.kind == "miter"
                else j.face_part)]
            for j in plan.joints]
        t = _Table(data, colWidths=[CW * 0.06, CW * 0.40, CW * 0.27,
                                    CW * 0.27], repeatRows=1)
        t.setStyle(tbl_style())
        story.append(t)

        story.append(_Paragraph("Consumables", h2))
        data = [["Item", "Count", "Notes"],
                [wc(f"Beech Domino {plan.size_key.replace('x', '×')} "
                    f"(Festool {plan.size.part_number}, pack of "
                    f"{DOMINO_PACK_QUANTITIES[plan.size_key]})"),
                 str(plan.tenons_total),
                 wc(f"{plan.per_joint} per joint × {len(plan.joints)} joints"
                    + (f" × {plan.copies} cabinets"
                       if plan.copies > 1 else ""))],
                [wc(f"{DRY_FIT_TENON_NAME} — print in {plan.size_key}: "
                    f"{DRY_FIT_TENON_URL}"),
                 str(plan.dry_fit_tenons_needed),
                 wc("one cabinet dry-fits at a time; reused across "
                    "copies")]]
        t = _Table(data, colWidths=[CW * 0.52, CW * 0.08, CW * 0.40])
        t.setStyle(tbl_style())
        story.append(t)

        # Registration explainer — section views of one joint showing the
        # shared 10 mm reference (Charlie, 2026-08-02).
        reg = _registration_scenes(plan)
        if reg is not None:
            story.append(_PageBreak())
            story.append(_Paragraph(
                "Registration — how the two halves of a joint line up",
                h1))
            for para in reg["intro"]:
                story.append(_Paragraph(xesc(para), norm))
                story.append(_Spacer(1, 1.5 * _rl_mm))
            for scene in reg["scenes"]:
                story.append(_KeepTogether([
                    _Spacer(1, 2 * _rl_mm),
                    _Paragraph(f"<b>{xesc(scene['title'])}</b>", norm),
                    _SceneFlowable(scene, CW),
                ]))

        story.append(_PageBreak())
        story.append(_Paragraph(
            f"Mortise maps — {xesc(plan.cabinet_name)}", h1))
        story.append(_Paragraph(
            "Red = face mortises · blue = edge mortises (into the panel "
            "end)"
            + (" · purple = miter mortises (into the 45° beveled end)"
               if plan.corner_style == "miter" else "")
            + ".", norm))
        map_h = 100 * _rl_mm
        for pm in plan.panels:
            pid_txt = f" · {pm.part_id}" if pm.part_id else ""
            note = f" — {pm.note}" if pm.note else ""
            story.append(_KeepTogether([
                _Paragraph(
                    f"<b>{xesc(pm.panel)}{xesc(pid_txt)}</b>"
                    f"{xesc(note)}  ({pm.draw_width:.0f} × "
                    f"{pm.draw_height:.0f} mm)", norm),
                _MapFlowable(pm, CW, map_h),
                _Spacer(1, 2 * _rl_mm),
            ]))

        story.append(_PageBreak())
        story.append(_Paragraph(
            f"Assembly sequence — {xesc(plan.cabinet_name)}", h1))
        for i, st in enumerate(plan.steps, start=1):
            story.append(_Paragraph(
                f"<b>Step {i} — {xesc(st.title)}.</b>  {xesc(st.body)}",
                step_sty))
            # Checklist items get their own indented, numbered paragraphs so
            # they stay tickable at the bench instead of collapsing into the
            # prose (reportlab has no list primitive in this style set).
            for n, item in enumerate(st.checklist, start=1):
                story.append(_Paragraph(
                    f"<b>{n}.</b>  {xesc(item)}", check_sty))

        if plan.drawer_boxes:
            story.append(_PageBreak())
            story.append(_Paragraph(
                f"Drawer boxes — {xesc(plan.cabinet_name)} "
                f"({len(plan.drawer_boxes)})", h1))
            story.append(_Paragraph(
                xesc(_box_table_caption(plan.drawer_boxes)), norm))
            story.append(_Spacer(1, 2 * _rl_mm))
            rows = [["Box", "Sides (×2)", "Front + back", "Bottom",
                     "Box (finished)", "Opening"]]
            for b in plan.drawer_boxes:
                rows.append([
                    _Paragraph(xesc(b.label), cell),
                    f"{b.side_length:g} × {b.side_height:g} × "
                    f"{b.stock_thickness:g}",
                    f"{b.front_back_length:g} × {b.front_back_height:g} × "
                    f"{b.stock_thickness:g}",
                    f"{b.bottom_length:g} × {b.bottom_width:g} × "
                    f"{b.bottom_thickness:g}",
                    f"{b.box_width:g} W × {b.box_depth:g} D",
                    f"{b.opening_width:g} × {b.opening_height:g}",
                ])
            box_tbl = _Table(rows, colWidths=[CW * f for f in
                                              (.2, .18, .18, .18, .14, .12)])
            box_tbl.setStyle(tbl_style())
            story.append(box_tbl)
            story.append(_Spacer(1, 3 * _rl_mm))
            for i, st in enumerate(plan.box_steps, start=1):
                story.append(_Paragraph(
                    f"<b>Box step {i} — {xesc(st.title)}.</b>  "
                    f"{xesc(st.body)}", step_sty))
                for n, item in enumerate(st.checklist, start=1):
                    story.append(_Paragraph(
                        f"<b>{n}.</b>  {xesc(item)}", check_sty))

    doc.build(story)
    return buf.getvalue()
