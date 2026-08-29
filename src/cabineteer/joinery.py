"""
Joinery specifications and geometry for drawer boxes and cabinet carcasses.

Drawer corner joints
--------------------
Four styles are supported, selectable via ``DrawerJoineryStyle``:

  BUTT         — plain butt joint (current default); sides butt against
                 the front and back sub-panels, fastened with glue + staples
                 or pocket screws.  No interlocking geometry.

  QQQ          — Quarter-Quarter-Quarter locking rabbet, coined by Stephen
                 Phipps (thisiscarpentry.com, 2014).  A dado is crosscut near
                 each end of the side piece (inside face down), creating a
                 tongue equal to half the stock thickness.  A matching channel
                 (rabbet) is cut on the outside edges of the front/back pieces.
                 All three table-saw settings equal material_thickness ÷ 2:
                 blade width, blade height, fence-to-blade distance.
                 Wood Magazine torture tests found this stronger than dovetail.
                 Requires true ½″ (12.7 mm) stock; works with any thickness
                 using the ½-stock rule throughout.

  HALF_LAP     — Half-lap at each corner.  Each mating face loses half its
                 thickness; the overlapping glue area doubles vs. a butt joint.
                 No mechanical interlock, but simple to cut (one rabbet per
                 piece end on the table saw).  No change to box exterior dims.

  DRAWER_LOCK  — Stepped router-bit joint (single bit, one fence setting per
                 piece type).  Unlike the other three, this one is
                 FRONT-LAPPING: the front and back span the full box width and
                 their end sockets swallow the ends of the sides, which is what
                 makes the front resist being pulled off.  The number the shop
                 actually sets is the LIP — the wall left outboard of the
                 socket — and the sides are cut short by two of them.  The lip
                 is a property of the bit and the fence, not of the drawing:
                 measure a test corner (see ``DrawerConfig.corner_lip_mm``).

Carcass joinery
---------------
Five methods are supported via ``CarcassJoinery``:

  DADO_RABBET    — current default (dados for shelves/bottom, rabbet for back)
  FLOATING_TENON — Festool Domino oval loose tenon; parametric mortise layout
  POCKET_SCREW   — Kreg-style angled pocket; parametric count and positioning
  BISCUIT        — #0 / #10 / #20 biscuit; primarily for alignment in plywood
  DOWEL          — 8 mm or 10 mm dowels; compatible with the 32 mm grid system

All dimensions in millimeters.  CadQuery geometry functions are gated behind
``_require_cq()`` so pure-parametric planning works without the CAD kernel.

Sources
-------
QQQ system  : Stephen Phipps, "The Quarter-Quarter-Quarter Drawer System",
              thisiscarpentry.com, 2014-09-19.
Domino tenon sizes : Festool catalog (DF 500 and DF 700 machines), 2023.
              Mortise dimensions confirmed via Festool technical datasheet
              "Domino Joining System" (EN, Rev. 2022).
Pocket screw : Kreg Tool Company, "Pocket-Hole Joinery Guide", 2023.
              Drill angle 15°; pocket and screw dimensions from Kreg Jig
              settings chart for wood thickness 12–38 mm.
Biscuit sizes: Porter-Cable / DeWalt biscuit dimension standard (ANSI 1986);
              #0, #10, #20 are the three standard sizes in universal use.
Dowel system : 32 mm European cabinet standard (Hettich/Grass technical
              docs); 8 mm is the most common diameter for carcass alignment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

try:
    import cadquery as cq
except ImportError:
    cq = None


# ─── Enumerations ─────────────────────────────────────────────────────────────


class DrawerJoineryStyle(Enum):
    """Corner-joint style for drawer boxes."""
    BUTT        = "butt"         # plain butt joint
    QQQ         = "qqq"          # quarter-quarter-quarter locking rabbet
    HALF_LAP    = "half_lap"     # half-lap overlap
    DRAWER_LOCK = "drawer_lock"  # stepped router-bit lock joint


#: Corner lap directions — which piece owns the box's outside faces at the
#: corner, and therefore which piece gets cut short.
#:
#: ``LAP_SIDE``  the sides run the full box depth and the front/back is buried
#:               between them (butt, QQQ, half lap).
#: ``LAP_FRONT`` the front/back runs the full box width and wraps the ends of
#:               the sides (drawer lock, and how a dedicated drawer-lock bit is
#:               cut at the router table).
LAP_SIDE = "side"
LAP_FRONT = "front"

#: Nominal wall a standard drawer-lock bit leaves outboard of the socket, per
#: corner (1/8").  A DEFAULT, not a measurement: the real value comes off a
#: test corner and belongs in ``DrawerConfig.corner_lip_mm``.  Charlie's setup
#: on 12 mm Baltic birch measured 2.0 mm (2026-08-26).
DRAWER_LOCK_NOMINAL_LIP_MM = 3.2


class CarcassJoinery(Enum):
    """Method for joining cabinet carcass panels."""
    DADO_RABBET    = "dado_rabbet"
    FLOATING_TENON = "floating_tenon"
    POCKET_SCREW   = "pocket_screw"
    BISCUIT        = "biscuit"
    DOWEL          = "dowel"


# ─── Drawer joint geometry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class DrawerJoinerySpec:
    """Computed dimensions for one drawer corner joint style.

    All dimensions in mm.  The spec is derived from stock thicknesses via
    ``from_stock()``.  Do not instantiate directly.

    Coordinate conventions (follows drawer.py orientation):
      X = width  (left → right)
      Y = depth  (front → back)
      Z = height (bottom → top)

    The LEFT SIDE panel occupies x = 0 … side_thickness, spanning full depth.
    The SUB-FRONT occupies y = 0 … front_back_thickness, spanning interior_width.

    Joint cuts on the SIDE panel (at its front end, y = 0):
      ``side_dado_x``   — start x of the dado cut from the INSIDE face
      ``side_dado_y``   — how far the dado penetrates into the side end (y direction)
      ``side_dado_z``   — always 0 (dado runs full height)
      The tongue that remains is the outer portion: x = 0 … side_tongue_width

    Joint cuts on the FRONT/BACK panel (at its left end, x = 0):
      ``fb_channel_x``  — depth of channel from outside edge (in x direction)
      ``fb_channel_y``  — width of channel from front face (in y direction)

    For BUTT: all cut dimensions are 0 (no joinery geometry, just glue face).
    For HALF_LAP: overlapping rabbet on each piece; no mechanical interlock.
    For DRAWER_LOCK: L-shaped tongue/socket; see attribute comments.
    """
    style: DrawerJoineryStyle

    side_thickness: float       # thickness of side panels (mm)
    front_back_thickness: float # thickness of front/back sub-panels (mm)

    # SIDE panel cuts (at each end, Y direction)
    side_dado_depth_x: float    # how deep the dado cuts into side (x direction, from inside face)
    side_dado_depth_y: float    # how far into the end of the side (y direction)

    # FRONT/BACK panel cuts (at each end, X direction)
    fb_channel_depth_x: float   # how deep the channel is from the outer edge (x)
    fb_channel_depth_y: float   # how wide the channel is from the front face (y)

    # Which piece owns the box's outside faces at the corner (LAP_SIDE /
    # LAP_FRONT).  This decides which part is cut short, so it decides every
    # box part length — see ``part_lengths``.
    corner_lap: str = LAP_SIDE

    # LAP_FRONT only: the wall left outboard of the socket on the front/back,
    # per corner.  An assembled box runs 2 x lip longer than its sides.
    lip: float = 0.0

    # Does the joint require a router bit (True) or a saw blade setup (False)?
    requires_router_bit: bool = False

    # For QQQ: is exact-thickness stock required?
    requires_true_thickness: bool = False
    nominal_thickness: float = 0.0  # mm — 0 means "any thickness works"

    @property
    def side_tongue_width(self) -> float:
        """Width of the tongue left on the side after the dado cut (x direction)."""
        return self.side_thickness - self.side_dado_depth_x

    @property
    def engagement_x(self) -> float:
        """How far the sub-front/back must extend past the carcass interior edge
        to engage the side panel.

        For BUTT this is 0 — the sub-front sits flush between the sides.
        For HALF_LAP / DRAWER_LOCK the sub-front fills a full-thickness rabbet
        (``side_dado_depth_x`` deep × ``front_back_thickness`` wide) on the
        side, edge-to-edge.  For QQQ the same value is the depth into the
        side that the front piece's inside-face tongue protrudes — into a
        set-in dado pocket on the side's inner face.  The side carries a
        full-thickness lip at the very end (Y `0…t_s/2`) that wraps around
        the corner and hides the joint from outside the box.
        """
        if self.style == DrawerJoineryStyle.BUTT or self.laps_front:
            return 0.0
        return self.side_dado_depth_x

    @property
    def laps_front(self) -> bool:
        """True when the front/back wraps the ends of the sides."""
        return self.corner_lap == LAP_FRONT

    @property
    def socket_depth(self) -> float:
        """LAP_FRONT: how deep the side's end buries into the front/back.

        The rest of the front/back's thickness is the ``lip`` that shows
        outboard of the side.
        """
        return (self.front_back_thickness - self.lip) if self.laps_front else 0.0

    def part_lengths(self, box_width: float, box_depth: float) -> tuple[float, float]:
        """``(side length, front/back length)`` for a finished box.

        THE single source for box part lengths.  The cutlist, the assembly
        doc and the 3D model all size from here, so a set of parts always
        closes into a ``box_width`` x ``box_depth`` box — which is the one
        thing a drawer-box parts list has to get right and the thing that
        went wrong before 2026-08: sides were listed at full depth AND
        fronts at full width, double-counting both corners.

        Which part is cut short follows the lap direction:

        LAP_SIDE   sides run the full depth; the front/back loses
                   ``2 x (side_thickness - engagement_x)`` and seats
                   ``engagement_x`` into each side.
        LAP_FRONT  the front/back runs the full width; the side loses
                   ``2 x lip``.
        """
        if self.laps_front:
            return box_depth - 2 * self.lip, box_width
        return box_depth, box_width - 2 * (self.side_thickness - self.engagement_x)

    @property
    def glue_area_corner(self) -> float:
        """Approximate glue contact width at one corner, per mm of box height
        (mm² per mm).  Multiply by drawer height for total corner glue area.

        Every style returns the same unit so the values are comparable:
        butt is the bare end-grain face width; the interlocking styles add
        their extra mating faces.
        """
        if self.style == DrawerJoineryStyle.BUTT:
            # End face of front/back against side inside face
            return self.front_back_thickness
        elif self.style == DrawerJoineryStyle.QQQ:
            # Tongue face (long grain) + shoulder face (cross grain)
            return self.side_dado_depth_x + self.side_dado_depth_y
        elif self.style == DrawerJoineryStyle.HALF_LAP:
            # Lap face + shoulder
            return self.side_dado_depth_x + self.front_back_thickness / 2
        elif self.style == DrawerJoineryStyle.DRAWER_LOCK:
            # The side's end is swallowed by the front's socket: both faces
            # of the side glue to the socket walls over its full depth, plus
            # the end grain on the socket floor.
            return 2 * self.socket_depth + self.side_thickness
        return 0.0

    @classmethod
    def from_stock(
        cls,
        style: DrawerJoineryStyle,
        side_thickness: float,
        front_back_thickness: float,
        lip: float | None = None,
    ) -> "DrawerJoinerySpec":
        """Create a spec with dimensions computed from stock thicknesses.

        QQQ:
          All cut depths = side_thickness / 2  (the ¼-¼-¼ rule scaled to stock).
        HALF_LAP:
          Each piece loses half its own thickness at the corner.
        DRAWER_LOCK:
          Front-lapping.  ``lip`` is the wall the bit leaves outboard of the
          socket, per corner — the number that sets the side length.  Pass the
          value measured off a test corner; omitting it falls back to
          ``DRAWER_LOCK_NOMINAL_LIP_MM`` (clamped to a third of the stock),
          which is a catalogue figure, not a measurement.
        BUTT:
          No cuts; all zero.

        ``lip`` is ignored by the side-lapping styles, which have no such
        wall — a project-wide lip token stays harmless on a half-lap box.
        """
        t_s = side_thickness
        t_fb = front_back_thickness

        if t_s <= 0 or t_fb <= 0:
            raise ValueError(
                f"Stock thicknesses must be positive, got side={t_s}, "
                f"front/back={t_fb}."
            )
        # QQQ's tongue/pocket is sized from the side stock (t_s/2 both ways);
        # the sub-front must be thick enough to carry that channel and still
        # leave a positive outer-face rabbet (t_fb − t_s/2 > 0).
        if style == DrawerJoineryStyle.QQQ and t_fb <= t_s / 2:
            raise ValueError(
                f"QQQ joinery needs front/back stock thicker than half the "
                f"side stock: side={t_s} requires front/back > {t_s / 2}, "
                f"got {t_fb}. Use thicker sub-front stock or a different "
                f"joinery style."
            )

        if style == DrawerJoineryStyle.BUTT:
            return cls(
                style=style,
                side_thickness=t_s,
                front_back_thickness=t_fb,
                side_dado_depth_x=0.0,
                side_dado_depth_y=0.0,
                fb_channel_depth_x=0.0,
                fb_channel_depth_y=0.0,
                requires_router_bit=False,
                requires_true_thickness=False,
            )

        elif style == DrawerJoineryStyle.QQQ:
            half = t_s / 2
            return cls(
                style=style,
                side_thickness=t_s,
                front_back_thickness=t_fb,
                # Dado on side end: inner half of thickness, half deep into end
                side_dado_depth_x=half,      # cuts from inside face inward
                side_dado_depth_y=half,      # penetrates half-thickness into the end
                # Channel on front/back end: matching the side tongue
                fb_channel_depth_x=half,    # channel depth from outer edge
                fb_channel_depth_y=half,    # channel height from front face
                requires_router_bit=False,
                requires_true_thickness=True,
                nominal_thickness=t_s,
            )

        elif style == DrawerJoineryStyle.HALF_LAP:
            return cls(
                style=style,
                side_thickness=t_s,
                front_back_thickness=t_fb,
                # Side: rabbet from inside face, full front_back_thickness wide
                side_dado_depth_x=t_s / 2,
                side_dado_depth_y=t_fb,
                # Front/back: rabbet from outside face, full side_thickness wide
                fb_channel_depth_x=t_s,
                fb_channel_depth_y=t_fb / 2,
                requires_router_bit=False,
                requires_true_thickness=False,
            )

        elif style == DrawerJoineryStyle.DRAWER_LOCK:
            # Front-lapping.  Nothing is cut into the side but its length:
            # the front/back carries the socket, and the wall left outboard
            # of that socket — the lip — is what the box grows by.
            eff_lip = (min(DRAWER_LOCK_NOMINAL_LIP_MM, t_fb / 3)
                       if lip is None else float(lip))
            if not 0 < eff_lip < t_fb:
                raise ValueError(
                    f"Drawer-lock lip must sit between 0 and the front/back "
                    f"thickness ({t_fb} mm), got {eff_lip}. The lip is the "
                    f"wall left outboard of the socket — measure it on a "
                    f"test corner."
                )
            return cls(
                style=style,
                side_thickness=t_s,
                front_back_thickness=t_fb,
                side_dado_depth_x=0.0,   # the side is cut to length, not cut into
                side_dado_depth_y=0.0,
                fb_channel_depth_x=t_s,           # socket is as wide as the side
                fb_channel_depth_y=t_fb - eff_lip,  # ...and this deep into the end
                corner_lap=LAP_FRONT,
                lip=eff_lip,
                requires_router_bit=True,
                requires_true_thickness=False,
            )

        raise ValueError(f"Unknown DrawerJoineryStyle: {style}")


def drawer_joinery_spec(
    style: DrawerJoineryStyle,
    side_thickness: float,
    front_back_thickness: float,
    lip: float | None = None,
) -> DrawerJoinerySpec:
    """Convenience wrapper for DrawerJoinerySpec.from_stock()."""
    return DrawerJoinerySpec.from_stock(
        style, side_thickness, front_back_thickness, lip)


# ─── Festool Domino floating tenon ────────────────────────────────────────────


@dataclass(frozen=True)
class DominoSize:
    """Dimensions for a single Domino tenon size.

    The tenon is nominally tenon_length × tenon_thickness (oval cross-section).
    The machine cuts an oval mortise slightly larger than the tenon for fit.

    Source: Festool "Domino Joining System" technical datasheet, 2022.
    Mortise dims are for the "fixed" (tight) fit setting on the DF 500/700.
    The DF 500 machine handles tenons up to 8 mm thick;
    the DF 700 handles 10 mm and 14 mm tenons.
    """
    tenon_length: float          # longer dimension (mm) — runs along the panel face
    tenon_thickness: float       # shorter dimension (mm) — penetrates into each piece
    mortise_length: float        # slot length cut by machine (tenon_length + 0.5 mm)
    mortise_width: float         # slot width (tenon_thickness + 0.5 mm)
    mortise_depth_per_side: float  # how deep the mortise goes into each piece
    min_edge_distance: float     # centre of mortise to nearest panel edge
    machine: str                 # "DF 500" or "DF 700"
    part_number: str             # Festool catalog number for the tenon pack


# All sizes from Festool catalog 2023; mortise depths are at the "fixed" fit setting.
DOMINO_SIZES: dict[str, DominoSize] = {
    "4x17": DominoSize(
        tenon_length=17, tenon_thickness=4,
        mortise_length=17.5, mortise_width=4.5, mortise_depth_per_side=12,
        min_edge_distance=8, machine="DF 500", part_number="498879",
    ),
    "5x19": DominoSize(
        tenon_length=19, tenon_thickness=5,
        mortise_length=19.5, mortise_width=5.5, mortise_depth_per_side=15,
        min_edge_distance=9, machine="DF 500", part_number="498880",
    ),
    "5x30": DominoSize(
        tenon_length=30, tenon_thickness=5,
        mortise_length=30.5, mortise_width=5.5, mortise_depth_per_side=15,
        # 494938 = "D 5x30/300 BU" — verified against US Tool & Fastener and
        # Taco Tools listings, Jul 2026 (the previous 498889 was wrong).
        min_edge_distance=9, machine="DF 500", part_number="494938",
    ),
    "6x40": DominoSize(
        tenon_length=40, tenon_thickness=6,
        mortise_length=40.5, mortise_width=6.5, mortise_depth_per_side=18,
        min_edge_distance=10, machine="DF 500", part_number="498881",
    ),
    "8x40": DominoSize(
        # Mortise depth set to 15 mm per side — the recommended depth for
        # 18–19 mm (3/4″) plywood per Festool DF 500 settings chart.
        # The machine maximum is 20 mm; 15 mm leaves a safe 3 mm wall in
        # 18 mm stock.  Use the deeper setting only in panels ≥ 23 mm thick.
        tenon_length=40, tenon_thickness=8,
        mortise_length=40.5, mortise_width=8.5, mortise_depth_per_side=15,
        min_edge_distance=11, machine="DF 500", part_number="493298",
    ),
    "8x50": DominoSize(
        tenon_length=50, tenon_thickness=8,
        mortise_length=50.5, mortise_width=8.5, mortise_depth_per_side=15,
        min_edge_distance=11, machine="DF 500", part_number="498883",
    ),
    "10x24": DominoSize(
        tenon_length=24, tenon_thickness=10,
        mortise_length=24.5, mortise_width=10.5, mortise_depth_per_side=22,
        min_edge_distance=12, machine="DF 700", part_number="498884",
    ),
    "10x50": DominoSize(
        tenon_length=50, tenon_thickness=10,
        mortise_length=50.5, mortise_width=10.5, mortise_depth_per_side=22,
        min_edge_distance=12, machine="DF 700", part_number="498885",
    ),
    "14x28": DominoSize(
        tenon_length=28, tenon_thickness=14,
        mortise_length=28.5, mortise_width=14.5, mortise_depth_per_side=27,
        min_edge_distance=15, machine="DF 700", part_number="498886",
    ),
    "14x56": DominoSize(
        tenon_length=56, tenon_thickness=14,
        mortise_length=56.5, mortise_width=14.5, mortise_depth_per_side=27,
        min_edge_distance=15, machine="DF 700", part_number="498887",
    ),
}


def get_domino_size(key: str) -> DominoSize:
    """Look up a DominoSize by key. Raises KeyError on unknown key."""
    if key not in DOMINO_SIZES:
        raise KeyError(f"Unknown Domino size '{key}'. Available: {list(DOMINO_SIZES)}")
    return DOMINO_SIZES[key]


@dataclass(frozen=True)
class DominoSpec:
    """Layout specification for Domino floating tenons along a panel joint.

    Parameters
    ----------
    size_key :
        Key into DOMINO_SIZES (e.g. ``"8x40"``).
    max_spacing :
        Maximum on-centre spacing between adjacent tenons (mm).
        Use 150 mm for structural joints (shelf-to-side, bottom-to-side).
        Use 250 mm for alignment-only joints.
    """
    size_key: str = "8x40"
    max_spacing: float = 150.0   # structural; use 250.0 for alignment only

    @property
    def size(self) -> DominoSize:
        return get_domino_size(self.size_key)

    def count_for_span(self, span: float) -> int:
        """Minimum number of tenons needed for a panel edge of ``span`` mm.

        Two tenons (one near each end) are the norm.  Beyond that, one tenon is
        added for every ``max_spacing`` mm of span.  A span too small to seat
        two end mortises without their slots overlapping (the end-tenon centres,
        placed at ``min_edge_distance + mortise_length / 2`` from each end,
        would cross) uses a single centred tenon — kept consistent with
        ``positions_for_span``.
        """
        if span <= 0:
            return 0
        s = self.size
        # End-tenon centres, placed so the mortise slot clears the panel end.
        start = s.min_edge_distance + s.mortise_length / 2
        end = span - s.min_edge_distance - s.mortise_length / 2
        if end <= start:
            return 1
        # Usable span between the two end tenons
        extra = math.ceil((end - start) / self.max_spacing) - 1
        return 2 + max(0, extra)

    def positions_for_span(self, span: float) -> list[float]:
        """Centred positions (from panel edge) for each tenon along the span.

        ``min_edge_distance`` is the clearance from the slot *edge* to the panel
        end, so the end-tenon *centres* sit at ``min_edge_distance +
        mortise_length / 2`` — this keeps the cut mortise fully inside
        ``[0, span]``.  Remaining tenons are evenly distributed between them.

        When the span is too small to hold two end fasteners without their
        slots overlapping (``span < 2 * min_edge_distance``) a single centred
        tenon is returned instead of a crossed/overrunning pair.
        """
        n = self.count_for_span(span)
        s = self.size
        if n == 0:
            return []
        if n == 1 or span < 2 * s.min_edge_distance:
            return [span / 2]
        start = s.min_edge_distance + s.mortise_length / 2
        end = span - s.min_edge_distance - s.mortise_length / 2
        if end <= start:
            return [span / 2]
        if n == 2:
            return [start, end]
        step = (end - start) / (n - 1)
        return [start + i * step for i in range(n)]


# ─── Pocket screw (Kreg-style) ────────────────────────────────────────────────


# Screw length by stock thickness (mm → mm).
# Source: Kreg Tool "Pocket-Hole Joinery Guide", 2023 edition.
POCKET_SCREW_LENGTH_BY_THICKNESS: dict[float, float] = {
    10: 19,   # 3/8" stock → 3/4" screw
    12: 19,   # 1/2" stock → 3/4" screw
    16: 25,   # 5/8" stock → 1" screw
    18: 32,   # 3/4" stock → 1-1/4" screw
    22: 38,   # 7/8" stock → 1-1/2" screw
    25: 38,   # 1" stock   → 1-1/2" screw
    32: 51,   # 1-1/4" stock → 2" screw
    38: 64,   # 1-1/2" stock → 2-1/2" screw
}


def pocket_screw_length(thickness_mm: float) -> float:
    """Return the recommended screw length for the given stock thickness.

    Looks up the nearest thickness in the Kreg chart and returns the
    corresponding screw length in mm.
    """
    if not POCKET_SCREW_LENGTH_BY_THICKNESS:
        return 32.0
    nearest = min(POCKET_SCREW_LENGTH_BY_THICKNESS, key=lambda t: abs(t - thickness_mm))
    return POCKET_SCREW_LENGTH_BY_THICKNESS[nearest]


@dataclass(frozen=True)
class PocketScrewSpec:
    """Layout spec for Kreg-style pocket-screw joints.

    The pocket is drilled at 15° through the thinner (or weaker) panel into
    the face of the mating panel.  No mortise is cut in the mating panel.

    Source: Kreg Tool Co., pocket-hole joinery guide (2023);
            drill angle and pocket dimensions from the Kreg Jig K5 settings.
    """
    drill_angle_deg: float = 15.0     # standard Kreg jig angle
    pocket_diameter: float = 9.5      # 3/8" pocket hole
    min_edge_distance: float = 19.0   # pocket centre → panel edge (Kreg minimum)
    max_spacing: float = 200.0        # on-centre spacing between pockets

    def screw_length(self, stock_thickness: float) -> float:
        """Return recommended screw length for the given stock thickness (mm)."""
        return pocket_screw_length(stock_thickness)

    def count_for_span(self, span: float) -> int:
        """Minimum pockets for a panel edge of ``span`` mm.

        Two pockets (one near each end) are the norm; one more per
        ``max_spacing`` mm.  A span too small to hold two pockets without their
        edge-distances overlapping (``span <= 2 * min_edge_distance``) uses a
        single centred pocket — kept consistent with ``positions_for_span``.
        """
        if span <= 0:
            return 0
        usable = span - 2 * self.min_edge_distance
        if usable <= 0:
            return 1
        extra = math.ceil(usable / self.max_spacing) - 1
        return 2 + max(0, extra)

    def positions_for_span(self, span: float) -> list[float]:
        """Pocket-centre positions (from panel edge) along ``span`` mm.

        When ``span < 2 * min_edge_distance`` a single centred pocket is
        returned rather than a crossed/overrunning pair.
        """
        n = self.count_for_span(span)
        if n == 0:
            return []
        if n == 1 or span < 2 * self.min_edge_distance:
            return [span / 2]
        start = self.min_edge_distance
        end = span - self.min_edge_distance
        if n == 2:
            return [start, end]
        step = (end - start) / (n - 1)
        return [start + i * step for i in range(n)]


# ─── Biscuit joinery ──────────────────────────────────────────────────────────


# ANSI standard biscuit dimensions: (slot_length, slot_width, slot_depth_per_side)
# Source: Porter-Cable / DeWalt biscuit dimension standard (ANSI 1986).
# slot_depth_per_side must be ≥ half the biscuit width so the two mating
# slots (one per panel) together seat the full biscuit (2×depth ≥ width).
BISCUIT_DIMS: dict[str, tuple[float, float, float]] = {
    "#0":  (47.0, 15.0, 8.0),   # 2×8.0  = 16 ≥ 15
    "#10": (53.0, 19.0, 10.0),  # 2×10.0 = 20 ≥ 19
    "#20": (56.0, 23.0, 12.5),  # 2×12.5 = 25 ≥ 23
}


@dataclass(frozen=True)
class BiscuitSpec:
    """Layout spec for biscuit joints.

    Biscuits are primarily used for alignment in plywood carcasses.
    They add relatively little structural strength across the panel face.

    Parameters
    ----------
    size :
        ``"#0"``, ``"#10"``, or ``"#20"``.
    max_spacing :
        Maximum on-centre spacing (mm).  100 mm is typical for alignment;
        75 mm for locations where some shear strength is needed.
    """
    size: str = "#10"
    max_spacing: float = 100.0
    min_edge_distance: float = 50.0   # biscuit centre → panel end

    @property
    def dims(self) -> tuple[float, float, float]:
        """(slot_length, slot_width, slot_depth_per_side)"""
        if self.size not in BISCUIT_DIMS:
            raise KeyError(f"Unknown biscuit size '{self.size}'. Use #0, #10, or #20.")
        return BISCUIT_DIMS[self.size]

    @property
    def slot_length(self) -> float:
        return self.dims[0]

    @property
    def slot_width(self) -> float:
        return self.dims[1]

    @property
    def slot_depth_per_side(self) -> float:
        return self.dims[2]

    def count_for_span(self, span: float) -> int:
        """Minimum biscuits for a panel edge of ``span`` mm.

        A span too small to hold two biscuits without their edge-distances
        overlapping (``span <= 2 * min_edge_distance``) uses a single centred
        biscuit — kept consistent with ``positions_for_span``.
        """
        if span <= 0:
            return 0
        usable = span - 2 * self.min_edge_distance
        if usable <= 0:
            return 1
        extra = math.ceil(usable / self.max_spacing) - 1
        return 2 + max(0, extra)

    def positions_for_span(self, span: float) -> list[float]:
        """Biscuit-centre positions (from panel edge) along ``span`` mm.

        When ``span < 2 * min_edge_distance`` a single centred biscuit is
        returned rather than a crossed/overrunning pair.
        """
        n = self.count_for_span(span)
        if n == 0:
            return []
        if n == 1 or span < 2 * self.min_edge_distance:
            return [span / 2]
        start = self.min_edge_distance
        end = span - self.min_edge_distance
        if n == 2:
            return [start, end]
        step = (end - start) / (n - 1)
        return [start + i * step for i in range(n)]


# ─── Dowel joinery ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DowelSpec:
    """Layout spec for round wood dowels.

    Dowels are compatible with the 32 mm European cabinet system — the same
    5 mm shelf-pin holes drilled on the 32 mm grid can serve as alignment
    dowels.  For structural joints, 8 mm or 10 mm dowels are standard.

    Source: 32 mm European cabinet standard; Hettich/Grass technical guides.
    """
    diameter: float = 8.0         # dowel diameter (mm); 8 or 10 for carcass
    depth_per_side: float = 15.0  # how deep each hole goes into the panel
    max_spacing: float = 96.0     # on-centre spacing (3 × 32 mm = 96 mm typical)
    min_edge_distance: float = 16.0  # dowel centre → panel end

    def count_for_span(self, span: float) -> int:
        """Minimum dowels for a panel edge of ``span`` mm.

        A span too small to hold two dowels without their edge-distances
        overlapping (``span <= 2 * min_edge_distance``) uses a single centred
        dowel — kept consistent with ``positions_for_span``.
        """
        if span <= 0:
            return 0
        usable = span - 2 * self.min_edge_distance
        if usable <= 0:
            return 1
        extra = math.ceil(usable / self.max_spacing) - 1
        return 2 + max(0, extra)

    def positions_for_span(self, span: float) -> list[float]:
        """Dowel-centre positions (from panel edge) along ``span`` mm.

        Positions are snapped to the nearest 32 mm grid increment when
        ``snap_to_32mm`` would be True, but the raw version just distributes
        evenly between the two end positions.

        When ``span < 2 * min_edge_distance`` a single centred dowel is
        returned rather than a crossed/overrunning pair.
        """
        n = self.count_for_span(span)
        if n == 0:
            return []
        if n == 1 or span < 2 * self.min_edge_distance:
            return [span / 2]
        start = self.min_edge_distance
        end = span - self.min_edge_distance
        if n == 2:
            return [start, end]
        step = (end - start) / (n - 1)
        return [start + i * step for i in range(n)]


# Bulk-pack quantities for the carcass tenon sizes (Festool catalog packs
# matching the part_number on each DominoSize).
DOMINO_PACK_QUANTITIES: dict[str, int] = {"5x30": 300, "8x40": 780}


def carcass_domino_size_for_thickness(panel_thickness: float) -> str:
    """DOMINO_SIZES key for carcass butt joints in stock of the given thickness.

    5×30 for panels up to 19 mm (3/4" plywood — Charlie's shop standard,
    Jul 2026: ~1/3-of-stock tenon thickness, 15 mm bite each side); 8×40
    for anything thicker.
    """
    return "5x30" if panel_thickness <= 19.0 else "8x40"


@dataclass(frozen=True)
class MiterMortisePlacement:
    """Solved Domino placement in a 45° miter face.

    All distances in mm. ``from_heel`` / ``from_long_point`` locate the
    mortise CENTRELINE along the miter face (face width = t·√2), measured
    from the inside corner (heel) and the outside tip respectively.
    ``show_face_wall`` is the remaining material between the mortise and
    the panel's outside (show) face at full plunge depth.
    """
    face_width: float
    from_heel: float
    from_long_point: float
    show_face_wall: float
    depth: float               # plunge depth per side (mm)


def miter_mortise_placement(
    size: DominoSize,
    stock_thickness: float,
    wall_mm: float = 2.0,
) -> MiterMortisePlacement:
    """Solve where a Domino mortise fits in a 45° miter of the given stock.

    The DF plunges perpendicular to the miter face; the plunge axis runs at
    45° to both panel faces, so depth eats toward the OUTSIDE (show) face
    at cos 45° per mm — the bevel's inward normal points from the heel
    toward the long point. The mortise must (a) keep ``wall_mm`` of
    material at the show face at full depth and (b) stay inside the face
    at entry. Feasible placements bias toward the HEEL; the solver returns
    the midpoint of the feasible window.

    Raises ``ValueError`` when the stock is too thin for the tenon's depth
    (e.g. 5×30 @ 15 mm needs ≥ 16.5 mm stock at a 2 mm wall).
    """
    c = math.cos(math.radians(45))
    t = float(stock_thickness)
    hw = (size.mortise_width / 2) * c          # slot half-extent across faces
    depth = size.mortise_depth_per_side
    # y = perpendicular distance of the mortise centreline entry point from
    # the inside face; the plunge drifts toward the show face, so the
    # feasible window is:
    low = hw                                   # slot stays inside the heel face
    high = t - wall_mm - hw - c * depth        # show-face wall survives at depth
    if low > high:
        min_t = wall_mm + 2 * hw + c * depth
        raise ValueError(
            f"Domino {size.tenon_thickness:g}×{size.tenon_length:g} at "
            f"{depth:g} mm depth does not fit a 45° miter in "
            f"{t:g} mm stock (needs ≥ {min_t:.1f} mm with a "
            f"{wall_mm:g} mm show-face wall)."
        )
    y0 = (low + high) / 2
    face_w = t / c
    from_heel = y0 / c
    return MiterMortisePlacement(
        face_width=round(face_w, 1),
        from_heel=round(from_heel, 1),
        from_long_point=round(face_w - from_heel, 1),
        show_face_wall=round(t - y0 - c * depth - hw, 1),
        depth=depth,
    )


# ─── Default spec instances ───────────────────────────────────────────────────

#: Default Domino spec for structural carcass joints (8×40, 150 mm spacing)
DEFAULT_DOMINO = DominoSpec(size_key="8x40", max_spacing=150.0)

#: Default pocket-screw spec
DEFAULT_POCKET_SCREW = PocketScrewSpec()

#: Default biscuit spec (#10, 100 mm spacing)
DEFAULT_BISCUIT = BiscuitSpec(size="#10", max_spacing=100.0)

#: Backward-compat alias — the class was originally (mis)spelled DownelSpec.
DownelSpec = DowelSpec

#: Default dowel spec (8 mm, 96 mm spacing)
DEFAULT_DOWEL = DowelSpec(diameter=8.0, max_spacing=96.0)


# ─── CadQuery geometry (gated behind _require_cq) ────────────────────────────


def _require_cq() -> None:
    if cq is None:
        raise ImportError("cadquery is required for 3D geometry. pip install cadquery")


def apply_drawer_joinery_to_side(
    panel: "cq.Workplane",
    spec: DrawerJoinerySpec,
    panel_length: float,
    box_height: float,
    side: str = "left",
) -> "cq.Workplane":
    """Cut the inner-face dado / rabbet that receives the sub-front / back panels.

    The panel is assumed to start at the origin (0, 0, 0) with:
      X = 0 … side_thickness
      Y = 0 … panel_length   (the SIDE's own length, which for a front-lapping
                              joint is shorter than the box depth)
      Z = 0 … box_height

    For BUTT: no cut.

    For a FRONT-LAPPING joint (drawer lock): no cut either — the socket that
    makes the corner is cut into the front/back, and the side is simply cut
    ``2 x lip`` short of the box depth.

    For HALF_LAP / DRAWER_LOCK: a uniform inner-face rabbet — ``engagement_x``
    deep in X, full ``front_back_thickness`` deep in Y — at the very end of the
    panel (Y = 0 / Y = box_depth).  The sub-front / back is widened by
    ``2 × engagement_x`` and seats into the rabbet edge-to-edge.

    For QQQ: a *set-in* dado pocket on the inner face at each end.  The pocket
    is ``side_dado_depth_x`` deep in X (= t_s/2) and ``side_dado_depth_y`` long
    in Y (= t_s/2), but its near edge sits ``side_dado_depth_y`` from the panel
    end.  This leaves a full-thickness **lip** at Y `0…t_s/2` (and the
    mirroring lip at the back end) that wraps around the front-corner of the
    box, hiding the joint from outside.  The sub-front's inside-face tongue —
    cut by ``apply_drawer_joinery_to_front_back`` — protrudes into the pocket.

    ``side="left"`` puts the inner face at panel-local X = side_thickness;
    ``side="right"`` puts it at X = 0.
    """
    _require_cq()

    if spec.style == DrawerJoineryStyle.BUTT or spec.laps_front:
        return panel

    if side not in ("left", "right"):
        raise ValueError(f"side must be 'left' or 'right', got {side!r}")

    t_s = spec.side_thickness
    dx = spec.engagement_x
    if spec.style == DrawerJoineryStyle.QQQ:
        dy = spec.side_dado_depth_y
        cut_y_inset = dy  # dado set in by t_s/2 from each end
    else:
        dy = spec.front_back_thickness
        cut_y_inset = 0.0

    cut_x_start = (t_s - dx) if side == "left" else 0.0

    front_cut = (
        cq.Workplane("XY")
        .transformed(offset=(cut_x_start, cut_y_inset, 0))
        .box(dx, dy, box_height, centered=False)
    )
    panel = panel.cut(front_cut)

    back_cut = (
        cq.Workplane("XY")
        .transformed(offset=(cut_x_start, panel_length - dy - cut_y_inset, 0))
        .box(dx, dy, box_height, centered=False)
    )
    panel = panel.cut(back_cut)

    return panel


def apply_drawer_joinery_to_front_back(
    panel: "cq.Workplane",
    spec: DrawerJoinerySpec,
    panel_length: float,
    box_height: float,
    position: str = "back",
) -> "cq.Workplane":
    """Cut the end sockets (front-lapping) or the QQQ outer-face rabbet.

    For a FRONT-LAPPING joint (drawer lock) the panel spans the full box
    width, and each end carries a socket that swallows the end of a side:
    ``side_thickness`` wide in X, ``socket_depth`` deep in Y measured from
    the INSIDE face, full height.  What is left outboard of it is the
    ``lip``.  The bottom groove dies into these sockets, which is why no
    groove shows on the outside of the finished box.

    For BUTT / HALF_LAP this is a no-op — the sub-front's solid body fills
    the side's rabbet directly.

    For QQQ each end of the front/back gets an outer-face rabbet that removes
    the corner (panel-local X = 0…fb_channel_depth_x, Y = 0…(t_fb − tongue_y),
    full Z, on the outer-face side).  What remains at each end is a
    ``tongue_y``-thick **inside-face tongue** that protrudes into the side
    panel's set-in dado pocket.  The matching cut on the side is in
    ``apply_drawer_joinery_to_side``.

    The "outer face" depends on ``position``: for a sub-front the outer face
    is panel-local Y = 0 (the front of the drawer faces the user), so the
    rabbet starts at Y = 0; for the back panel the outer face is Y = t_fb,
    so the rabbet starts at Y = tongue_y.  In both cases the tongue ends up
    on the inside-face half of the panel.
    """
    _require_cq()
    if position not in ("front", "back"):
        raise ValueError(f"position must be 'front' or 'back', got {position!r}")

    if spec.laps_front:
        # The inside face is Y = t_fb for a sub-front (its outer face looks
        # out of the box at Y = 0) and Y = 0 for the back.
        depth = spec.socket_depth
        y0 = spec.lip if position == "front" else 0.0
        for x0 in (0.0, panel_length - spec.side_thickness):
            panel = panel.cut(
                cq.Workplane("XY")
                .transformed(offset=(x0, y0, 0))
                .box(spec.side_thickness, depth, box_height, centered=False)
            )
        return panel

    if spec.style != DrawerJoineryStyle.QQQ:
        return panel

    t_fb = spec.front_back_thickness
    dx = spec.fb_channel_depth_x
    tongue_y = spec.fb_channel_depth_y
    rabbet_dy = t_fb - tongue_y

    rabbet_y_start = 0.0 if position == "front" else tongue_y

    left_cut = (
        cq.Workplane("XY")
        .transformed(offset=(0, rabbet_y_start, 0))
        .box(dx, rabbet_dy, box_height, centered=False)
    )
    panel = panel.cut(left_cut)

    right_cut = (
        cq.Workplane("XY")
        .transformed(offset=(panel_length - dx, rabbet_y_start, 0))
        .box(dx, rabbet_dy, box_height, centered=False)
    )
    panel = panel.cut(right_cut)

    return panel


def apply_domino_mortises(
    panel: "cq.Workplane",
    spec: DominoSpec,
    span: float,
    edge_y: float,
    panel_thickness_z: float,
) -> "cq.Workplane":
    """Cut Domino mortises into a panel face along an edge.

    Mortises are cut from the face at z = panel_thickness_z (top face for a
    horizontal panel) down to z = panel_thickness_z - mortise_depth_per_side.
    Positions are along the X axis starting at x = 0.

    Args:
        panel: CadQuery workplane of the panel.
        spec: DominoSpec with size and spacing configuration.
        span: Panel edge length (mm); mortises are distributed along this span.
        edge_y: Y-coordinate of the panel edge where the joint is made.
                Mortise centres are placed at this Y offset.
        panel_thickness_z: Z height of the panel face where mortises are cut.
    """
    _require_cq()
    s = spec.size
    positions = spec.positions_for_span(span)
    depth = s.mortise_depth_per_side

    for x_pos in positions:
        mortise = (
            cq.Workplane("XY")
            .transformed(offset=(
                x_pos - s.mortise_length / 2,
                edge_y - s.mortise_width / 2,
                panel_thickness_z - depth,
            ))
            .box(s.mortise_length, s.mortise_width, depth, centered=False)
        )
        panel = panel.cut(mortise)

    return panel


def apply_pocket_screw_pockets(
    panel: "cq.Workplane",
    spec: PocketScrewSpec,
    span: float,
    stock_thickness: float,
    pocket_face_y: float,
    panel_z: float = 0.0,
) -> "cq.Workplane":
    """Cut angled pocket-screw pockets into a panel face.

    The pocket is modelled as a simplified angled cylinder (approximated as an
    angled box cut for compatibility).  The drill enters at pocket_face_y on
    the back face of the panel and exits at an angle toward the mating panel.

    Note: The full angled geometry requires the panel to be thick enough to
    accommodate the pocket depth.  This implementation uses an approximation
    that is sufficient for interference detection; the actual jig setup governs
    the real cut path.
    """
    _require_cq()
    angle_rad = math.radians(spec.drill_angle_deg)
    pocket_len = stock_thickness / math.sin(angle_rad)  # approximate pocket length

    positions = spec.positions_for_span(span)

    for x_pos in positions:
        # Simplified angled pocket: a box cut at the drill angle
        pocket = (
            cq.Workplane("YZ")
            .transformed(offset=(pocket_face_y, panel_z + stock_thickness / 2, x_pos),
                          rotate=(spec.drill_angle_deg, 0, 0))
            .box(pocket_len, spec.pocket_diameter, spec.pocket_diameter, centered=True)
        )
        panel = panel.cut(pocket)

    return panel
