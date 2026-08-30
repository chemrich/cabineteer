"""
Parametric drawer box generator.

Builds drawer boxes sized to fit cabinet openings with proper hardware clearances.
Supports dovetail-style (sides overlap front/back) and butt-joint construction.

All dimensions in millimeters. Drawer orientation:
- X axis: width (left to right)
- Y axis: depth (front to back)
- Z axis: height (bottom to top)
- Origin at front-bottom-left exterior corner
"""

from dataclasses import dataclass, field
from typing import Optional

try:
    import cadquery as cq
except ImportError:
    cq = None

from .hardware import DrawerSlideSpec, get_slide, get_pull
from .cabinet import CabinetConfig, PartInfo
from .joinery import (
    DrawerJoineryStyle,
    DrawerJoinerySpec,
    drawer_joinery_spec,
    apply_drawer_joinery_to_side,
    apply_drawer_joinery_to_front_back,
)
from .pulls import PullPlacement, VerticalPolicy, pull_positions


# ─── Standard drawer box heights ──────────────────────────────────────────────
# Industry-standard box heights in mm (3"–12" in 1" increments).
# Manufacturers (Eagle Woodworking, Drawer Connection, etc.) stock these sizes
# natively, making batch ordering and interchangeable spares straightforward.
STANDARD_BOX_HEIGHTS: tuple[float, ...] = (
    76.0,   # 3"
    102.0,  # 4"
    127.0,  # 5"
    152.0,  # 6"
    178.0,  # 7"
    203.0,  # 8"
    229.0,  # 9"
    254.0,  # 10"
    279.0,  # 11"
    305.0,  # 12"
)


# ─── Manga scale reference ────────────────────────────────────────────────────
# A standard tankōbon volume, used as a visual scale item in the viewer.
# Volumes sit in the front-left corner of the drawer interior and stack up to
# MANGA_MAX_STACK high; the viewer's M toggle controls how many are shown.
MANGA_WIDTH_MM:  float = 112.5   # cover width  (x, across the drawer)
MANGA_DEPTH_MM:  float = 176.0   # cover height (y, into the drawer)
MANGA_THICK_MM:  float = 15.0    # one volume's thickness (z)
MANGA_MAX_STACK: int   = 5
MANGA_CLEARANCE_MM: float = 5.0  # gap kept to the interior walls


def snap_to_standard_box_height(raw_mm: float) -> float:
    """Return the largest standard box height that fits within *raw_mm*.

    If *raw_mm* is smaller than the smallest standard height (76 mm / 3"),
    return *raw_mm* unchanged so callers never get a negative or zero result.

    Examples
    --------
    >>> snap_to_standard_box_height(135)   # fits a 5" (127 mm) box
    127.0
    >>> snap_to_standard_box_height(102)   # exactly 4" — stays 4"
    102.0
    >>> snap_to_standard_box_height(60)    # below minimum — pass through
    60.0
    """
    best = None
    for h in STANDARD_BOX_HEIGHTS:
        if h <= raw_mm:
            best = h
    return best if best is not None else raw_mm


# ─── Bottom panel thickness defaults ──────────────────────────────────────────
# Shop practice: a 1/4" bottom is fine for small drawers, but a box both
# deeper than 5" and 16"+ wide will carry real weight over a wide span, so
# it defaults to a 1/2" bottom (still dado-captured).  Thresholds compare
# against *box* dimensions (what physically spans), not the opening.
#: Drawer-bottom recess used when the slide does not state one, in mm.
#: 13 is Blum's figure for the whole TANDEM and MOVENTO wood-drawer range,
#: and Charlie confirmed it at the bench on 2026-08-29 — dadoed at 13, boxes
#: run. It is a sensible default rather than a universal truth: a slide that
#: really does impose a different recess should carry it on its own spec as
#: ``DrawerSlideSpec.bottom_recess``, which is what this falls back FROM.
DEFAULT_BOTTOM_DADO_INSET = 13.0

DEFAULT_BOTTOM_THICKNESS: float = 6.0     # 1/4" plywood
HEAVY_BOTTOM_THICKNESS: float = 12.0      # 1/2" plywood
HEAVY_BOTTOM_MIN_BOX_HEIGHT: float = 127.0  # > 5" deep (box height)
HEAVY_BOTTOM_MIN_BOX_WIDTH: float = 406.4   # ≥ 16" wide (box width)


@dataclass
class DrawerConfig:
    """Configuration for a single drawer box."""

    # Opening dimensions (from cabinet)
    opening_width: float  # between cabinet sides
    opening_height: float  # vertical space for this drawer
    opening_depth: float  # from cabinet front to back panel

    # Materials
    side_thickness: float = 15.0  # 5/8" for drawer sides
    front_back_thickness: float = 15.0  # 5/8" for sub-front and back
    # Bottom panel thickness.  ``None`` (the default) resolves by size: boxes
    # taller than HEAVY_BOTTOM_MIN_BOX_HEIGHT (5") *and* at least
    # HEAVY_BOTTOM_MIN_BOX_WIDTH (16") wide get HEAVY_BOTTOM_THICKNESS
    # (12 mm / 1/2") — a 1/4" bottom sags across a wide span under shop
    # loads even dado-captured on four sides.  Everything else gets
    # DEFAULT_BOTTOM_THICKNESS (6 mm / 1/4").  Pass an explicit value to
    # override in either direction.
    bottom_thickness: Optional[float] = None

    # Joinery for bottom panel
    bottom_dado_depth: float = 6.0  # how deep the dado is cut
    # Distance from the box side's bottom edge up to the UNDERSIDE of the
    # bottom panel — the lower shoulder of the groove. ``None`` (the default)
    # resolves from the slide: an undermount runner carries the box under its
    # bottom and its locking devices engage that panel, so the RUNNER decides
    # where the bottom sits. It is an interface dimension, not a preference,
    # and it was a bare 12.0 here until 2026-08-29 — unreachable from every
    # tool argument, checked by nothing, and 1 mm off what Blum specifies.
    # Pass a number to override, e.g. for a slide whose recess this repo does
    # not carry.
    bottom_dado_inset: Optional[float] = None

    # Gaps / reveals
    front_gap: float = 2.0  # gap between drawer box front and cabinet face
    vertical_gap: float = 12.0  # clearance above drawer box

    # Hardware
    slide_key: str = "blum_tandem_550h"

    # Corner joinery style
    joinery_style: DrawerJoineryStyle = DrawerJoineryStyle.HALF_LAP

    # Front-lapping joints (drawer lock) only: the wall the router bit leaves
    # outboard of the socket, per corner, in mm.  It sets the side length
    # (``box_depth - 2 x lip``), it is a property of the BIT AND FENCE rather
    # than of the design, and it is measured on a test corner — not derived.
    # ``None`` falls back to the catalogue nominal for the style.  Ignored by
    # the side-lapping styles, which have no such wall.
    corner_lip_mm: Optional[float] = None

    # Height snapping: when True, box_height snaps down to the nearest standard
    # size (see STANDARD_BOX_HEIGHTS) so orders can be batched by common heights.
    # Set to False to use the full computed clearance-adjusted height instead.
    use_standard_height: bool = True

    # Drawer face (applied face, not the sub-front)
    applied_face: bool = True
    face_overlay_sides: float = 10.0  # how much face overlaps opening per side
    face_overlay_top: float = 3.0
    face_overlay_bottom: float = 3.0
    face_thickness: float = 18.0  # 3/4"

    # Pull hardware (optional).  ``pull_key`` is a key into the PULLS registry
    # (see ``hardware.PULLS`` / ``cabineteer/data/pulls_catalog.json``).
    # When ``None``, no pull is placed on the drawer face and the BOM omits it.
    # ``pull_count`` of 0 defers to :func:`pulls.recommend_pull_count` (1 for
    # knobs/flush; 1 if face_width ≤ 762 mm (30″), else 2 for surface/edge pulls).
    # ``pull_vertical`` controls the height at which the pull centres sit —
    # ``"center"`` (default), ``"upper_third"``, or ``"lower_third"``.
    pull_key: Optional[str] = None
    pull_count: int = 0
    pull_vertical: VerticalPolicy = "center"

    def __post_init__(self) -> None:
        """Resolve the size-based bottom-thickness default.

        Runs the heavy-bottom rule only when ``bottom_thickness`` was not
        given.  A bad ``slide_key`` is deliberately swallowed here (falling
        back to the thin default) so it keeps surfacing where it always has —
        on first property access — rather than turning construction into the
        failure point.
        """
        if self.bottom_dado_inset is None:
            try:
                recess = self.slide.bottom_recess
            except KeyError:
                recess = None      # bad slide key surfaces on property access
            self.bottom_dado_inset = (
                recess if recess is not None else DEFAULT_BOTTOM_DADO_INSET)

        if self.bottom_thickness is None:
            try:
                heavy = (
                    self.box_height > HEAVY_BOTTOM_MIN_BOX_HEIGHT
                    and self.box_width >= HEAVY_BOTTOM_MIN_BOX_WIDTH
                )
            except KeyError:
                heavy = False
            self.bottom_thickness = (
                HEAVY_BOTTOM_THICKNESS if heavy else DEFAULT_BOTTOM_THICKNESS
            )

    @property
    def slide(self) -> DrawerSlideSpec:
        return get_slide(self.slide_key)

    @property
    def joinery(self) -> DrawerJoinerySpec:
        """Computed corner-joint dimensions for the selected joinery style."""
        return drawer_joinery_spec(
            self.joinery_style, self.side_thickness, self.front_back_thickness,
            lip=self.corner_lip_mm,
        )

    @property
    def box_width(self) -> float:
        """Drawer box OUTSIDE width — the finished box, side face to side face.

        Delegated to the slide, because what the clearance is measured to
        differs by mounting style: an undermount runner constrains the
        drawer's INSIDE width (Blum: opening − 42 mm), so the outside grows
        with the side stock; a side-mount slide occupies the gap beside the
        box and constrains the outside directly.  See
        :class:`hardware.ClearanceReference`.
        """
        return self.slide.drawer_box_width(self.opening_width, self.side_thickness)

    @property
    def box_inside_width(self) -> float:
        """Drawer box INSIDE width — wall to wall, what goes in the drawer.

        For an undermount slide this is the dimension the manufacturer
        actually constrains, so it is the one to measure a built box
        against: Blum wants it dead on ``opening − 42``.
        """
        return self.slide.drawer_inside_width(self.opening_width, self.side_thickness)

    @property
    def side_gap(self) -> float:
        """Air gap per side, cabinet side to the box's OUTSIDE face.

        The placement number — where the box sits in the opening.  Equals
        the slide's ``nominal_side_clearance`` only for side-mount slides;
        for an undermount it is that minus the side stock (9 mm for Blum's
        21 mm on 12 mm sides).  Every X-offset that positions a box in a
        carcass reads this, never the raw clearance.
        """
        return (self.opening_width - self.box_width) / 2

    @property
    def box_height(self) -> float:
        """Drawer box height (exterior).

        When ``use_standard_height`` is True (default), the raw computed height
        is snapped *down* to the nearest value in ``STANDARD_BOX_HEIGHTS`` so
        that box orders can be batched by a small set of common sizes.  The
        remaining clearance is absorbed into the vertical gap above the box.
        """
        raw = self.opening_height - self.slide.min_bottom_clearance - self.vertical_gap
        if self.use_standard_height:
            return snap_to_standard_box_height(raw)
        return raw

    @property
    def standard_box_height(self) -> float:
        """Always returns the snapped standard height regardless of use_standard_height."""
        raw = self.opening_height - self.slide.min_bottom_clearance - self.vertical_gap
        return snap_to_standard_box_height(raw)

    @property
    def box_depth(self) -> float:
        """Drawer box depth (front to back, exterior)."""
        slide_length = self.slide.slide_length_for_depth(self.opening_depth)
        return min(
            self.opening_depth - self.front_gap,
            slide_length,
        )

    @property
    def side_panel_length(self) -> float:
        """Cut length of a box side (front to back).

        Full box depth for a side-lapping joint; ``2 x lip`` short of it for
        a front-lapping one, where the front and back wrap the side's ends.
        """
        return self.joinery.part_lengths(self.box_width, self.box_depth)[0]

    @property
    def front_back_panel_length(self) -> float:
        """Cut length of the sub-front / back (side to side).

        Full box width for a front-lapping joint; for a side-lapping one it
        is short by the material of both sides, less whatever it seats into
        them (``engagement_x`` per corner).
        """
        return self.joinery.part_lengths(self.box_width, self.box_depth)[1]

    @property
    def bottom_panel_width(self) -> float:
        """Bottom panel width — reaches the groove floors in both sides.

        ``box_inside_width`` plus the groove depth at each side, so this
        tracks the corrected outside width automatically.
        """
        return self.box_inside_width + (self.bottom_dado_depth * 2)

    @property
    def bottom_panel_depth(self) -> float:
        """Bottom panel depth — fits in dados in front and back."""
        return self.box_depth - (self.front_back_thickness * 2) + (self.bottom_dado_depth * 2)

    @property
    def face_width(self) -> float:
        """Applied drawer face width."""
        return self.opening_width + (self.face_overlay_sides * 2)

    @property
    def face_height(self) -> float:
        """Applied drawer face height."""
        return self.opening_height + self.face_overlay_top + self.face_overlay_bottom

    @property
    def pull_placements(self) -> list[PullPlacement]:
        """Pull placements on the applied drawer face, in face-local coords.

        Returns an empty list when ``pull_key`` is ``None`` or the drawer has
        no applied face (``applied_face=False``) — there is nowhere to mount
        a pull in either case.  Otherwise resolves the catalog entry and
        delegates to :func:`pulls.pull_positions`.
        """
        if self.pull_key is None or not self.applied_face:
            return []
        pull = get_pull(self.pull_key)
        return pull_positions(
            self.face_width,
            self.face_height,
            pull,
            self.pull_key,
            count=self.pull_count,
            vertical=self.pull_vertical,
        )


def _require_cq():
    if cq is None:
        raise ImportError("cadquery is required. Install with: pip install cadquery")


def make_drawer_side(cfg: DrawerConfig, side: str = "left") -> "cq.Workplane":
    """Create a drawer side panel with bottom dado and corner joinery cuts.

    ``side`` is ``"left"`` or ``"right"`` and determines which face the bottom
    dado and corner joinery are cut into so they end up on the *inside* face
    once the panel is placed in the assembly.
    """
    # Validate args before requiring CadQuery — a bad argument is the
    # caller's bug regardless of which extras are installed.
    if side not in ("left", "right"):
        raise ValueError(f"side must be 'left' or 'right', got {side!r}")

    _require_cq()

    length = cfg.side_panel_length
    panel = (
        cq.Workplane("XY")
        .box(cfg.side_thickness, length, cfg.box_height, centered=False)
    )

    dado_x = (cfg.side_thickness - cfg.bottom_dado_depth) if side == "left" else 0.0
    dado = (
        cq.Workplane("XY")
        .transformed(offset=(dado_x, 0, cfg.bottom_dado_inset))
        .box(cfg.bottom_dado_depth, length, cfg.bottom_thickness, centered=False)
    )
    panel = panel.cut(dado)

    panel = apply_drawer_joinery_to_side(
        panel, cfg.joinery, length, cfg.box_height, side=side
    )

    return panel


def make_drawer_front_back(cfg: DrawerConfig, position: str = "back") -> "cq.Workplane":
    """Create a drawer sub-front or back panel with the bottom dado.

    ``position`` is ``"front"`` (sub-front) or ``"back"``; it controls which
    face the bottom dado is cut into so it ends up on the *inside* of the
    assembled drawer.  For QQQ the same parameter also routes the inside-face
    corner rabbets cut by ``apply_drawer_joinery_to_front_back``.

    The panel length comes from ``DrawerConfig.front_back_panel_length``: for
    a side-lapping joint it is ``box_width − 2 × (side_thickness −
    engagement_x)``, so each end overhangs the carcass interior by
    ``engagement_x`` and seats in the side panel's rabbet (zero overhang for
    BUTT); for a front-lapping one (drawer lock) it is the FULL box width,
    and each end carries a socket that swallows a side.
    """
    if position not in ("front", "back"):
        raise ValueError(f"position must be 'front' or 'back', got {position!r}")

    _require_cq()

    length = cfg.front_back_panel_length

    panel = (
        cq.Workplane("XY")
        .box(length, cfg.front_back_thickness, cfg.box_height, centered=False)
    )

    dado_y = 0.0 if position == "back" else (cfg.front_back_thickness - cfg.bottom_dado_depth)
    dado = (
        cq.Workplane("XY")
        .transformed(offset=(0, dado_y, cfg.bottom_dado_inset))
        .box(length, cfg.bottom_dado_depth, cfg.bottom_thickness, centered=False)
    )
    panel = panel.cut(dado)

    panel = apply_drawer_joinery_to_front_back(
        panel, cfg.joinery, length, cfg.box_height, position=position
    )

    return panel


def drawer_part_offsets(cfg: DrawerConfig) -> dict[str, tuple[float, float, float]]:
    """Where each box part sits, in box-local coordinates (origin = the box's
    front-left-bottom outside corner).

    SINGLE SOURCE for box part placement, so the assembly, the tests and any
    probe all agree on where a part goes. Which piece wraps the corner sets
    both the sides' Y offset and the front/back's X offset:

    LAP_FRONT (drawer lock)
        the front/back runs the full width at ``x = 0``; each side is held
        off the ends of the box by one ``lip``.
    LAP_SIDE (butt, QQQ, half lap)
        the sides run the full depth at ``y = 0``; the front/back seats
        ``engagement_x`` into each side's rabbet.

    No CadQuery required — this is arithmetic, and the geometry-free paths
    (parts lists, checks) are entitled to it too.
    """
    j = cfg.joinery
    side_y = j.lip if j.laps_front else 0.0
    fb_x = 0.0 if j.laps_front else (cfg.side_thickness - j.engagement_x)
    return {
        "side_L":    (0.0, side_y, 0.0),
        "side_R":    (cfg.box_width - cfg.side_thickness, side_y, 0.0),
        "sub_front": (fb_x, 0.0, 0.0),
        "back":      (fb_x, cfg.box_depth - cfg.front_back_thickness, 0.0),
        "bottom":    (cfg.side_thickness - cfg.bottom_dado_depth,
                      cfg.front_back_thickness - cfg.bottom_dado_depth,
                      cfg.bottom_dado_inset),
    }


def make_drawer_bottom(cfg: DrawerConfig) -> "cq.Workplane":
    """Create the drawer bottom panel."""
    _require_cq()
    return (
        cq.Workplane("XY")
        .box(cfg.bottom_panel_width, cfg.bottom_panel_depth, cfg.bottom_thickness, centered=False)
    )


def make_drawer_face(cfg: DrawerConfig) -> "cq.Workplane":
    """Create an applied drawer face."""
    _require_cq()
    return (
        cq.Workplane("XY")
        .box(cfg.face_width, cfg.face_thickness, cfg.face_height, centered=False)
    )


def add_manga_stack(assy: "cq.Assembly", cfg: DrawerConfig) -> None:
    """Add a MANGA_MAX_STACK-high manga pile to a drawer-box assembly.

    The stack sits on the drawer bottom in the front-left interior corner
    (viewer meshes ``manga0`` … ``manga4``; visibility is a viewer toggle).
    Raises ``ValueError`` when the drawer interior cannot hold the full
    stack — footprint plus MANGA_CLEARANCE_MM on the wall sides, and
    MANGA_MAX_STACK volumes of height under the box rim.
    """
    floor_z    = cfg.bottom_dado_inset + cfg.bottom_thickness
    interior_w = cfg.box_inside_width
    interior_d = cfg.box_depth  - 2 * cfg.front_back_thickness
    interior_h = cfg.box_height - floor_z
    need_w = MANGA_WIDTH_MM + MANGA_CLEARANCE_MM
    need_d = MANGA_DEPTH_MM + MANGA_CLEARANCE_MM
    need_h = MANGA_MAX_STACK * MANGA_THICK_MM
    if interior_w < need_w or interior_d < need_d or interior_h < need_h:
        raise ValueError(
            f"drawer interior {interior_w:.0f}×{interior_d:.0f}×"
            f"{interior_h:.0f} mm cannot hold a stack of {MANGA_MAX_STACK} "
            f"manga ({MANGA_WIDTH_MM:g}×{MANGA_DEPTH_MM:g} mm footprint "
            f"+ {MANGA_CLEARANCE_MM:g} mm wall clearance, "
            f"{need_h:g} mm tall). Render without the manga scale item."
        )

    # Flat pastel accents for the no-finish render; the viewer replaces these
    # with drawn tankōbon covers at load time.
    accents = [
        cq.Color(0.91, 0.26, 0.23, 1.0),
        cq.Color(0.17, 0.44, 0.83, 1.0),
        cq.Color(0.07, 0.63, 0.35, 1.0),
        cq.Color(0.95, 0.55, 0.11, 1.0),
        cq.Color(0.55, 0.27, 0.78, 1.0),
    ]
    base_x = cfg.side_thickness + MANGA_CLEARANCE_MM
    base_y = cfg.front_back_thickness + MANGA_CLEARANCE_MM
    volume = (
        cq.Workplane("XY")
        .box(MANGA_WIDTH_MM, MANGA_DEPTH_MM, MANGA_THICK_MM, centered=False)
    )
    # Jitter room left after the base footprint claims its clearance —
    # the offsets move volumes TOWARD the far walls, so they must be
    # clamped or tight-but-passing interiors clip the pile through the
    # right/back panels (review 2026-07-29).
    slack_x = max(0.0, interior_w - need_w)
    slack_y = max(0.0, interior_d - need_d)
    for k in range(MANGA_MAX_STACK):
        # Small deterministic jitter so the pile reads as stacked books
        # rather than one extruded block.
        jx = min(4.0 if k % 2 else 0.0, slack_x)
        jy = min(3.0 * (k % 3), slack_y)
        assy.add(
            volume, name=f"manga{k}",
            loc=cq.Location((base_x + jx, base_y + jy, floor_z + k * MANGA_THICK_MM)),
            color=accents[k % len(accents)],
        )


def build_drawer(
    cfg: DrawerConfig,
    include_manga: bool = False,
) -> tuple["cq.Assembly", list[PartInfo]]:
    """Build a complete drawer box assembly.

    Args:
        cfg:           Drawer configuration.
        include_manga: Add the manga scale-reference stack (viewer prop, not a
                       BOM part; see :func:`add_manga_stack`). Raises
                       ``ValueError`` if the interior can't hold the full stack.

    Returns:
        Tuple of (cq.Assembly, list of PartInfo for BOM/cutlist).
    """
    _require_cq()

    parts: list[PartInfo] = []

    # ── Build parts ──────────────────────────────────────────────────────
    left_side = make_drawer_side(cfg, side="left")
    right_side = make_drawer_side(cfg, side="right")
    sub_front = make_drawer_front_back(cfg, position="front")
    back = make_drawer_front_back(cfg, position="back")
    bottom = make_drawer_bottom(cfg)

    parts.append(PartInfo(
        name="drawer_side_L", shape=left_side,
        material_thickness=cfg.side_thickness,
        grain_direction="length",
    ))
    parts.append(PartInfo(
        name="drawer_side_R", shape=right_side,
        material_thickness=cfg.side_thickness,
        grain_direction="length",
    ))
    parts.append(PartInfo(
        name="drawer_sub_front", shape=sub_front,
        material_thickness=cfg.front_back_thickness,
        grain_direction="width",
    ))
    parts.append(PartInfo(
        name="drawer_back", shape=back,
        material_thickness=cfg.front_back_thickness,
        grain_direction="width",
    ))
    parts.append(PartInfo(
        name="drawer_bottom", shape=bottom,
        material_thickness=cfg.bottom_thickness,
        grain_direction="width",
        notes="1/4 inch plywood",
    ))

    if cfg.applied_face:
        face = make_drawer_face(cfg)
        parts.append(PartInfo(
            name="drawer_face", shape=face,
            material_thickness=cfg.face_thickness,
            grain_direction="width",
            edge_band=["all"],
        ))

    # ── Assembly ─────────────────────────────────────────────────────────
    assy = cq.Assembly(name="drawer_box")

    COL_SIDE   = cq.Color(0.90, 0.76, 0.50, 1.0)   # warm honey maple — sides
    COL_FB     = cq.Color(0.96, 0.91, 0.76, 1.0)   # light ash cream  — front/back
    COL_BOTTOM = cq.Color(0.60, 0.46, 0.28, 1.0)   # dark ply brown   — bottom

    # Placement is arithmetic, and it lives in drawer_part_offsets so the
    # assembly and every probe of it read the same rule.
    off = drawer_part_offsets(cfg)

    assy.add(left_side, name="side_L",
             loc=cq.Location(off["side_L"]), color=COL_SIDE)

    assy.add(right_side, name="side_R",
             loc=cq.Location(off["side_R"]), color=COL_SIDE)

    assy.add(sub_front, name="sub_front",
             loc=cq.Location(off["sub_front"]), color=COL_FB)

    assy.add(back, name="back", loc=cq.Location(off["back"]), color=COL_FB)

    # Bottom panel captured in dados
    assy.add(bottom, name="bottom",
             loc=cq.Location(off["bottom"]), color=COL_BOTTOM)

    if include_manga:
        add_manga_stack(assy, cfg)

    # Applied face
    if cfg.applied_face:
        face = make_drawer_face(cfg)
        face_x = -(cfg.face_overlay_sides + cfg.side_gap)
        face_y = -cfg.face_thickness
        face_z = -cfg.face_overlay_bottom
        assy.add(face, name="face",
                 loc=cq.Location((face_x, face_y, face_z)),
                 color=cq.Color(0.65, 0.45, 0.28, 1.0))

    return assy, parts


def drawers_from_cabinet_config(cab_cfg: CabinetConfig) -> list[tuple["cq.Assembly", list[PartInfo], float]]:
    """Generate drawer assemblies from a cabinet's drawer_config.

    Returns:
        List of (drawer_assembly, parts, z_position) tuples.
    """
    if not cab_cfg.openings:
        return []

    drawers = []
    # Start stacking from the bottom panel
    current_z = cab_cfg.bottom_thickness

    for op in cab_cfg.openings:
        opening_height = op.height_mm
        if op.opening_type == "drawer":
            dcfg = DrawerConfig(
                opening_width=cab_cfg.interior_width,
                opening_height=opening_height,
                opening_depth=cab_cfg.interior_depth,
                slide_key=op.slide_key or cab_cfg.drawer_slide,
                pull_key=op.pull_key or cab_cfg.drawer_pull,
                joinery_style=cab_cfg.drawer_joinery,
                side_thickness=cab_cfg.drawer_box_thickness,
                front_back_thickness=cab_cfg.drawer_box_thickness,
                bottom_thickness=op.bottom_thickness,
                applied_face=False,  # faces handled by the caller / face stack
            )
            drawer_assy, drawer_parts = build_drawer(dcfg)

            # Position within cabinet: centered in opening with slide clearance,
            # lifted by the slide's minimum bottom clearance (matches
            # build_multi_bay_cabinet's drawer placement).
            drawer_x = cab_cfg.side_thickness + dcfg.side_gap
            drawer_y = dcfg.front_gap
            drawer_z = current_z + dcfg.slide.min_bottom_clearance

            drawers.append((drawer_assy, drawer_parts, drawer_z))

        current_z += opening_height

    return drawers


def box_config_for_opening(
    cab_cfg: CabinetConfig,
    col_width: float,
    opening_height: float,
    interior_depth: float,
    opening=None,
) -> DrawerConfig:
    """Build the DrawerConfig for one drawer opening in a carcass.

    SINGLE SOURCE for the box that goes in an opening. The cutlist
    (``server._raw_panels_for_cabinet``) and the assembly doc
    (``assembly.build_drawer_box_plans``) both resolve boxes through here,
    so the parts list and the instructions can never quote different
    dimensions for the same drawer.

    ``opening`` is the OpeningConfig, whose per-opening options (slide key,
    bottom thickness) win over the cabinet-level defaults.
    """
    return DrawerConfig(
        opening_width=col_width,
        opening_height=opening_height,
        opening_depth=interior_depth,
        slide_key=(getattr(opening, "slide_key", None) or cab_cfg.drawer_slide),
        side_thickness=cab_cfg.drawer_box_thickness,
        front_back_thickness=cab_cfg.drawer_box_thickness,
        bottom_thickness=getattr(opening, "bottom_thickness", None),
        joinery_style=cab_cfg.drawer_joinery,
        corner_lip_mm=cab_cfg.drawer_corner_lip_mm,
    )
