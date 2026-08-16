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
    width_label: str           # axis meaning, e.g. "depth (front → back)"
    height_label: str
    rows: tuple[MortiseRow, ...]
    note: str = ""


@dataclass(frozen=True)
class AssemblyStep:
    title: str
    body: str                  # plain text; renderers wrap/escape


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
    edge_band_thickness_mm: float = 0.0
    #: Distinct carcass panel thicknesses (sorted). Panels below
    #: BASE_REF_MIN_THICKNESS_MM take the centred t/2 fallback; everything
    #: else shares the single 10 mm base-height fence setting.
    panel_thicknesses: tuple = ()

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

    # ── Panel mortise maps ────────────────────────────────────────────────
    panels: list[PanelMortiseMap] = []
    depth = float(cab_cfg.depth)
    # Interior panels (bottom/top/shelves/dividers) are CUT to
    # depth − back_thickness (the cutlist convention) — draw them that way
    # so map dims match the parts in hand. Mortise positions still span
    # interior_depth from the front edge, safely inside the panel.
    interior_panel_depth = depth - float(
        getattr(cab_cfg, "back_thickness", 6.0))
    # back_style "under_top": the top runs full depth and caps the back —
    # its map must draw the panel at the dims in hand (butt corners only).
    under_top = (getattr(cab_cfg, "back_style", "full_height") == "under_top"
                 and not miter)
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
    panels.append(PanelMortiseMap(
        panel="side (make 2, mirror-image)", part_id=pid("side"),
        draw_width=depth, draw_height=height,
        width_label="depth — front edge at left",
        height_label="height" + (" (long-point)" if miter else ""),
        rows=tuple(side_rows),
        note=(("Ends beveled 45° — mortise the MITER FACES top and bottom. "
               if miter else "Mortise the INNER face. ")
              + "The two sides are a mirrored pair — mark them L and R "
              "before machining."),
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
        panel_draw_depth = (depth if (pname == "top" and under_top)
                            else interior_panel_depth)
        cap_txt = (" Full-depth panel — rear edge flush with the sides; "
                   "it caps the back." if (pname == "top" and under_top)
                   else "")
        panels.append(PanelMortiseMap(
            panel=pname, part_id=pid(canonical),
            draw_width=tb_width, draw_height=panel_draw_depth,
            width_label=("length (= exterior width, long-point)" if miter
                         else "length (= interior width)"),
            height_label="depth — front edge at bottom",
            rows=tuple(rows),
            note=(f"{end_txt}; face mortises "
                  f"({'top face' if pname == 'bottom' else 'underside'}) "
                  f"{_ref_offset(side_t):g} mm past each divider's "
                  "LEFT-face line — mark left-face lines, not centrelines."
                  f"{cap_txt}"
                  if div_centres else f"{end_txt}.{cap_txt}"),
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
            panels.append(PanelMortiseMap(
                panel=label,
                part_id=pid("column_divider"),
                draw_width=interior_panel_depth,
                draw_height=float(cab_cfg.interior_height),
                width_label="depth — front edge at left",
                height_label="height (fits between bottom and top)",
                rows=rows_key,
                note=("Both faces show; edge mortises in the two ENDS "
                      "(ride the fence/plate on the LEFT face — mark it), "
                      "face mortises at each shelf row (face noted per "
                      "row, measured from the shelf's UNDERSIDE line)."
                      if has_faces else
                      "Both faces show; mortise the two ENDS only — ride "
                      "the fence/plate on the LEFT face (mark it)."),
            ))

    shelf_like = []
    if global_shelves:
        shelf_like.append(("fixed shelf", len(global_shelves), interior_w))
    for ci, col in enumerate(cols):
        ns = len(getattr(col, "fixed_shelf_positions", ()) or ())
        if ns:
            shelf_like.append(
                (f"col {ci + 1} fixed shelf", ns, float(cols[ci].width_mm)))
    for label, count, length in shelf_like:
        panels.append(PanelMortiseMap(
            panel=f"{label} (make {count})" if count > 1 else label,
            # Length-qualified lookup first: global and column shelf
            # families share the "shelf_1" panel name but are distinct
            # cutlist rows (review 2026-07-29).
            part_id=pid(f"shelf_1@{round(length, 1)}") or pid("shelf_1"),
            draw_width=length, draw_height=interior_panel_depth,
            width_label="length",
            height_label="depth — front edge at bottom",
            rows=(
                MortiseRow("", "v", 0.0, positions, "edge"),
                MortiseRow("", "v", length, positions, "edge"),
            ),
            note="Edge mortises in both ends — ride the fence/plate on "
                 "the UNDERSIDE.",
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
        panel_thicknesses=tuple(sorted(thicknesses)),
    )
    plan.steps = _build_steps(plan, cab_cfg)
    return plan


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
    return AssemblyStep(
        "Machine the back capture",
        f"On the INNER face of both sides, the underside of the top and the "
        f"top face of the bottom, cut {where}. Run it right through each "
        "panel — the ends are covered by the panels they meet. One fence "
        f"setting covers all four. {extra}")


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
        AssemblyStep(
            "Inventory and label the parts",
            "Pull every carcass panel from the cutlist and pencil its part ID "
            "on a face that ends up hidden (outside back corner). Sides are a "
            "mirrored pair — mark L / R now. Check each panel for the show "
            "face and orient the best face where it will be seen."
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
            f"Glue {bt:g} mm hardwood strips (ripped ~20 mm wide, proud "
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
        f"{s.mortise_depth_per_side:g} mm (this leaves a "
        f"{t - s.mortise_depth_per_side:g} mm wall behind face "
        f"mortises in {t:g} mm stock). Fence 90°, {_fence_text(plan)}. "
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
            "cross-section inserts and pulls by hand. Seat every joint, "
            f"then: (1) check both diagonals match within 1 mm; (2) {back_seat_txt}; "
            "(3) rehearse the clamp layout — "
            "set every clamp and caul you will use for real; (4) confirm "
            "drawer openings with a slide or spacer. Fix anything that "
            "fights you NOW — glue makes it permanent."),
        AssemblyStep(
            "Disassemble and stage",
            "Pull the dry-fit tenons and lay panels out in assembly order "
            "with clamps open to length, real beech tenons counted out "
            f"({plan.tenons_per_cabinet} per cabinet), glue, brush, and a "
            "damp rag in reach. Glue-up is a race; staging wins it."),
        AssemblyStep(
            "Glue up in stages",
            ("Stage 1 — inner structure: glue dividers and fixed shelves "
             "into the BOTTOM panel (tenons glued in both mortises, glue "
             "on the mating edge), then cap with the TOP panel. Stage 2 — "
             "fold the mitered sides on: run painter's tape across each "
             "miter's OUTSIDE corner as a hinge, glue the miter faces and "
             "tenons, fold closed, and pull the corners tight with band "
             "clamps (bar clamps + cauls across the case as backup). "
             "Check diagonals immediately and rack square before the "
             "glue tacks."
             if miter else
             "Stage 1 — inner structure: glue dividers and fixed shelves "
             "into the BOTTOM panel (tenons glued in both mortises, glue "
             "on the mating edge), then cap with the TOP panel. Stage 2 — "
             "add the sides. Clamp across every joint line with cauls, "
             "check diagonals immediately, and rack square before the "
             "glue tacks. For a simple box (no dividers/shelves) do it "
             "in one stage.")),
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
              "While the clamps are on, drop the back panel into its "
              "rabbets from behind and glue/pin it home — a square back "
              "holds the carcass square as it cures. Its edges are buried "
              "in the rabbets, so nothing shows from any angle. Re-check "
              "diagonals, wipe squeeze-out, leave clamped for the glue's "
              "clamp time (30–60 min PVA) and unstressed for 24 h.")
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
             "24 h.")),
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
                         f"({t - s.mortise_depth_per_side:.0f} mm wall left "
                         f"in {t:.0f} mm stock)"),
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
            out.append(
                f"<div class='{cls}'><b>Step {i} — {escape(st.title)}</b>"
                f"{body}</div>")

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

    doc.build(story)
    return buf.getvalue()
