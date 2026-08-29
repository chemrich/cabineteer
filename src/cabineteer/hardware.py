"""
Hardware specifications for drawer slides, hinges, and other cabinet hardware.

All dimensions in millimeters unless otherwise noted.

Sources
-------
Blum Tandem 550H   : Blum Inc. 550H datasheet + distributor catalog cross-reference
                     (mcfaddens.com, interfitco.com, search-confirmed part numbers)
Blum Tandem+ 563H  : Blum 563H official datasheet (© 2016 Blum Inc.) as indexed by
                     cabinetdoor.store and d2.blum.com; CabinetParts.com SKU listings
Blum Movento 760H  : Blum Movento brochure "The Evolution of Motion" (2016/2024);
                     distributor SKU tables (mcfaddens.com, hwt-pro.com, Amazon)
Blum Movento 769   : Blum 769 catalog page © 2019; CabinetParts / Indian River
                     Cabinet Supply SKU listings; rokhardware.com spec page
Accuride 3832      : Accuride product page and distributor listings
Salice Futura      : Salice Futura catalog D0CASG010ENG
Salice Progressa+  : Salice PROGRESSA catalog D0CASAA36USA; cabinetparts.com specs

Blum Clip Top hinge family:
  Hinge arm suffix: B = full overlay, H = half overlay, N = inset (cranked)
  BLUMOTION variants: 71B3590 / 71B3690 / 71B3790 (full/half/inset,
  integrated soft-close); plain = 71T35/36/3790. Suffix ..50 = screw-on
  cup, ..90 = INSERTA (tool-free). Numbers corrected 2026-07-28 — the
  old 71H/71N scheme was not real Blum numbering (caught at order time).
  Source: Blum CLIP top datasheet (d2.blum.com/en/HingeDataSheet_cliptop.pdf);
          Blum catalog "Kitchen & Bedroom" © 2023; hardware.com / hafele.com SKUs.

  Standard cup boring (Blum 35 mm system):
    Cup diameter : 35 mm
    Cup depth    : 13 mm
    Cup centre from door edge (boring centre): 22.5 mm
    This leaves 5 mm of door material beyond the cup edge — do not reduce below 3 mm.

  Standard hinge placement (from door edge):
    Top hinge    : 100 mm from door top
    Bottom hinge : 100 mm from door bottom
    3rd/4th hinge: evenly distributed in remaining span

  Hinge count by door height (Blum chart, ea.blum.com "Number of hinges"):
    Up to 900 mm    → 2 hinges
    901–1 600 mm    → 3 hinges
    1 601–2 000 mm  → 4 hinges
    > 2 000 mm      → 5 hinges
  The count is additionally raised until adjacent-hinge spacing ≤
  ``max_hinge_spacing`` (700 mm default) — the two rules agree within a
  hinge across the practical range.
  (Blum also recommends an extra hinge per 25 kg of door weight above 20 kg.)

IMPORTANT: Always verify part numbers and dimensions against the official Blum or
Salice datasheet for the specific revision you are purchasing before cutting.
Minor changes between catalog years are possible.
"""

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from importlib import resources


class SlideType(Enum):
    UNDERMOUNT = "undermount"
    SIDE_MOUNT = "side_mount"
    CENTER_MOUNT = "center_mount"


class SlideMountLocation(Enum):
    """Where the slide attaches relative to the drawer box."""
    BOTTOM = "bottom"  # undermount slides attach under the drawer
    SIDE = "side"      # side-mount slides attach to drawer sides


class ClearanceReference(Enum):
    """Which face of the drawer box a slide's side clearance is measured to.

    This is the single most consequential fact about a slide's clearance
    number, and getting it wrong is silent: both readings produce a
    plausible box that is exactly ``2 x side_thickness`` apart.

    ``INSIDE`` — undermount runners (Blum Tandem / Tandem plus / Movento,
        Salice Futura / Progressa+).  The runner sits UNDER the box, so
        nothing has to fit beside the drawer side; what the manufacturer
        constrains is the drawer's INSIDE width, because that is what the
        runner body, the rear brackets and the front locking devices have
        to reach across.  Blum states it as one sentence: "inside drawer
        width must equal opening width minus 42 mm".  The 21 mm per side
        therefore runs cabinet side -> drawer INSIDE face, and it stays 21
        at any side thickness (9 mm air + 12 mm side, or 5 mm air + 16 mm
        side).  The box's outside width GROWS with thicker sides.

    ``OUTSIDE`` — side-mount ball-bearing slides (Accuride and friends).
        The slide body lives in the gap BESIDE the drawer side, so the
        clearance is the body thickness and it is measured to the box's
        OUTSIDE face.  The box's outside width is independent of side
        thickness.

    Resolved automatically from ``mount_location`` when a spec does not
    state it, so a newly added undermount cannot silently inherit
    side-mount arithmetic.
    """
    INSIDE = "inside"
    OUTSIDE = "outside"


#: Blum's own "Calculating outside drawer width" table for the TANDEM
#: undermount family: millimetres to DEDUCT from the cabinet opening width
#: to get the drawer box's OUTSIDE width, keyed by drawer side thickness.
#: Source: Blum "TANDEM plus BLUMOTION 563H/563 Installation Instructions",
#: INST-TDM563H-563 05.16, page 2 (c) 2016 Blum Inc.
#:
#: Every entry is ``42 - 2 x side_thickness`` — the arithmetic proof that
#: the 42 mm is an INSIDE-width rule, kept here as literal published data
#: so the relation is anchored to something outside this codebase.  A test
#: asserts ``drawer_box_width`` reproduces it exactly; do not "simplify"
#: this table into the formula it validates.
BLUM_UNDERMOUNT_WIDTH_DEDUCTION: dict[float, float] = {
    12.0: 18.0,
    13.0: 16.0,
    14.0: 14.0,
    15.0: 12.0,
    16.0: 10.0,
}


@dataclass(frozen=True)
class DrawerSlideSpec:
    """Specifications for a drawer slide system.

    All clearance values are PER SIDE unless noted otherwise, and each is
    measured to the drawer-box face named by ``clearance_reference`` — see
    :class:`ClearanceReference`, which is the whole ballgame.

    For the Blum Tandem and Movento undermount families the nominal
    clearance is 21 mm per side measured to the drawer's INSIDE face, i.e.

        drawer INSIDE  width = opening width − 42 mm      (Blum: "must equal")
        drawer OUTSIDE width = opening width − 42 mm + 2 × side_thickness

    (Blum TANDEM plus 563H/563 installation instructions, page 2: the NOTE
    and the "Calculating outside drawer width" deduction table say the same
    thing two ways — see ``BLUM_UNDERMOUNT_WIDTH_DEDUCTION``.)

    Reading the 42 as an outside-width rule builds every box exactly
    ``2 × side_thickness`` too narrow, and no gap check can catch it,
    because the gap is then derived from the same constant it is compared
    against.  Ask ``drawer_box_width``/``drawer_inside_width`` for the
    numbers rather than doing the arithmetic at the call site.
    """
    name: str
    manufacturer: str
    slide_type: SlideType
    mount_location: SlideMountLocation

    # Clearance requirements (per side, cabinet side → the drawer-box face
    # named by ``clearance_reference``).  All three are on the SAME face.
    min_side_clearance: float   # absolute minimum; slide may not engage below this
    max_side_clearance: float   # maximum; coupling won't reach above this
    nominal_side_clearance: float  # recommended / spec clearance

    # Vertical clearances
    min_top_clearance: float    # minimum gap above drawer box top
    min_bottom_clearance: float # minimum gap below drawer box (undermount body height)

    # Slide dimensions
    available_lengths: tuple[int, ...]  # nominal slide lengths in mm
    max_load_kg: float                  # maximum rated dynamic load

    # Drawer box constraints
    min_drawer_height: float   # minimum drawer side height
    max_drawer_width: float    # maximum drawer box width (0 = no hard limit)

    # Mounting geometry (how far mounting points sit from cabinet extremes)
    rear_bracket_inset: float  # distance of rear mount from back of cabinet interior
    front_bracket_inset: float # distance of front clip/mount from cabinet face

    # Part numbers keyed by nominal length in mm.
    # Format follows Blum conventions: 550H4500B = Tandem 550H, 450 mm length.
    part_numbers: dict = field(default_factory=dict)

    # How the slide is sold. Undermount runners (Blum Tandem/Movento, Salice
    # Futura/Progressa) are sold as left+right pairs and priced per pair;
    # side-mount ball-bearing slides (Accuride) are commonly sold as singles.
    # Drives HardwareLine.pack_quantity and must match the PRICE_LIST basis.
    sold_as_pair: bool = True

    #: Which drawer-box face the three side-clearance numbers above are
    #: measured to.  ``None`` (the default) resolves from ``mount_location``
    #: in ``__post_init__``: a BOTTOM-mounted runner sits under the box and
    #: constrains the INSIDE width; anything mounted beside the box
    #: constrains the OUTSIDE.  State it explicitly to override.
    clearance_reference: "ClearanceReference | None" = None

    #: Drawer travel: "full" (box comes fully out of the cabinet) or "3/4"
    #: (partial extension). Shown in list_hardware and on every slide BOM
    #: line so the paperwork states it — added after the 563H swap left
    #: Charlie unable to verify extension from the BOM (2026-07-23).
    extension: str = "full"

    def slide_length_for_depth(self, cabinet_depth: float) -> int:
        """Return the longest slide that fits the given cabinet interior depth."""
        usable = cabinet_depth - self.rear_bracket_inset - self.front_bracket_inset
        candidates = [l for l in self.available_lengths if l <= usable]
        if not candidates:
            raise ValueError(
                f"No {self.name} slide fits cabinet depth {cabinet_depth}mm. "
                f"Minimum needed: {min(self.available_lengths) + self.rear_bracket_inset + self.front_bracket_inset}mm"
            )
        return max(candidates)

    def __post_init__(self) -> None:
        """Resolve ``clearance_reference`` from the mounting location.

        Deliberately a derivation rather than a per-spec default: a slide
        added later without thinking about it gets the reading that matches
        how it physically mounts, instead of inheriting whatever the last
        author typed.
        """
        if self.clearance_reference is None:
            object.__setattr__(
                self, "clearance_reference",
                ClearanceReference.INSIDE
                if self.mount_location is SlideMountLocation.BOTTOM
                else ClearanceReference.OUTSIDE,
            )

    def drawer_inside_width(self, opening_width: float,
                            side_thickness: float) -> float:
        """INSIDE width of the drawer box for this opening (box wall to wall).

        For an undermount runner this is the dimension the manufacturer
        constrains (Blum: "must equal opening width minus 42 mm"), so it is
        the number to check a built box against.
        """
        return self.drawer_box_width(opening_width, side_thickness) - 2 * side_thickness

    def drawer_box_width(self, opening_width: float,
                         side_thickness: float) -> float:
        """OUTSIDE width of the drawer box for this opening.

        ``side_thickness`` is the drawer-box side stock.  It is REQUIRED,
        not defaulted: for an inside-referenced slide the outside width is
        a function of it, and a defaulted 0 would silently reproduce the
        pre-2026-08 bug (every undermount box ``2 x side_thickness`` too
        narrow).  It is genuinely unused for side-mount slides.
        """
        inner_face = opening_width - 2 * self.nominal_side_clearance
        if self.clearance_reference is ClearanceReference.INSIDE:
            return inner_face + 2 * side_thickness
        return inner_face

    def side_gap(self, opening_width: float, side_thickness: float) -> float:
        """Air gap per side between the cabinet side and the box's OUTSIDE face.

        This is the placement number — where the box actually sits in the
        opening — and it equals ``nominal_side_clearance`` only for
        side-mount slides.  For an undermount it is
        ``nominal_side_clearance − side_thickness`` (9 mm for Blum's 21 mm
        with 12 mm sides).
        """
        return (opening_width
                - self.drawer_box_width(opening_width, side_thickness)) / 2

    def validate_drawer_dims(
        self, drawer_width: float, drawer_height: float, drawer_depth: float,
        opening_width: float, side_thickness: float = 0.0,
    ) -> list[str]:
        """Check drawer dimensions against slide constraints. Returns list of issues.

        ``drawer_width`` is the box's OUTSIDE width; ``side_thickness`` is
        the box side stock, needed to step in to the inside face for a
        slide whose clearance is referenced there.
        """
        issues = []
        # Measure the clearance to the face this slide's numbers describe.
        if self.clearance_reference is ClearanceReference.INSIDE:
            measured_face_width = drawer_width - 2 * side_thickness
            face = "inside"
        else:
            measured_face_width = drawer_width
            face = "outside"
        actual_clearance = (opening_width - measured_face_width) / 2

        if actual_clearance < self.min_side_clearance:
            issues.append(
                f"Side clearance {actual_clearance:.1f}mm (cabinet side to drawer "
                f"{face} face) < minimum {self.min_side_clearance}mm"
            )
        if actual_clearance > self.max_side_clearance:
            issues.append(
                f"Side clearance {actual_clearance:.1f}mm (cabinet side to drawer "
                f"{face} face) > maximum {self.max_side_clearance}mm — "
                f"slides won't engage"
            )
        if (self.clearance_reference is ClearanceReference.INSIDE
                and side_thickness >= self.nominal_side_clearance):
            issues.append(
                f"Drawer side stock {side_thickness:.1f}mm is at or beyond the "
                f"{self.nominal_side_clearance:.1f}mm per-side clearance for "
                f"{self.name} — the box would be as wide as the opening or wider "
                f"(outside width = opening − "
                f"{2 * self.nominal_side_clearance:.0f} + 2 × side)."
            )
        if drawer_height < self.min_drawer_height:
            issues.append(
                f"Drawer height {drawer_height:.1f}mm < minimum {self.min_drawer_height}mm for {self.name}"
            )
        if self.max_drawer_width > 0 and drawer_width > self.max_drawer_width:
            issues.append(
                f"Drawer width {drawer_width:.1f}mm > maximum {self.max_drawer_width}mm for {self.name}"
            )
        shortest = min(self.available_lengths)
        if drawer_depth < shortest:
            issues.append(
                f"Drawer depth {drawer_depth:.1f}mm < shortest {self.name} slide "
                f"({shortest}mm) — box must be at least as deep as the slide"
            )
        return issues


class OverlayType(Enum):
    """Door overlay relative to the cabinet carcase."""
    FULL = "full"         # door overlaps the cabinet side fully (16 mm per edge)
    HALF = "half"         # door overlaps half the side (9.5 mm) — shared partition
    INSET = "inset"       # door sits inside the opening with a reveal gap


@dataclass(frozen=True)
class HingeSpec:
    """Specifications for a cabinet door hinge.

    All dimensions in millimeters.

    Cup boring layout (Blum 35 mm system)
    --------------------------------------
    The cup is bored from the *interior* face of the door.
    ``cup_boring_distance`` is the distance from the door edge to the cup
    centre along the door face.  The Blum standard is 22.5 mm, which leaves
    5 mm of material beyond the 35 mm cup edge — never go below 3 mm.

    Hinge count guidance
    --------------------
    Use ``hinges_for_height()`` to get the recommended count.  The formula
    is derived from Blum's published door-height / weight tables.
    """
    name: str
    manufacturer: str
    overlay_type: OverlayType    # full / half / inset
    overlay: float               # mm the door overlaps the carcase edge (0 for inset)
    cup_diameter: float          # boring diameter (35 mm for Blum 35-mm system)
    cup_depth: float             # boring depth (13 mm standard)
    cup_boring_distance: float   # cup centre → door edge (22.5 mm standard)
    min_door_thickness: float
    max_door_thickness: float
    opening_angle: int           # maximum opening angle in degrees
    soft_close: bool             # integrated soft-close / BLUMOTION
    max_door_weight_kg: float    # max door weight per *pair* of hinges
    part_number: str = ""        # manufacturer part number
    # The hinge SKU is the cup/arm ONLY — CLIP mounting plates are a
    # separate purchase (found when Charlie went to order, 2026-07-28).
    mounting_plate_part: str = ""  # e.g. "173L8100" (one plate per hinge)
    # Cup attachment screws: 0 for INSERTA (tool-free expanding cup),
    # 2 for screw-on cups. Plates need no line — 173L8100 ships with
    # pre-mounted 5 mm Euro system screws.
    cup_screws: int = 0

    # Hinge placement constants (from door top / bottom edge)
    hinge_inset_top: float = 100.0     # distance of top hinge from door top
    hinge_inset_bottom: float = 100.0  # distance of bottom hinge from door bottom
    max_hinge_spacing: float = 700.0   # max on-centre spacing between any two hinges

    def hinges_for_height(self, door_height: float, door_weight_kg: float = 0.0) -> int:
        """Return the recommended number of hinges for a given door height and weight.

        Rules (Blum's published chart, ea.blum.com "Number of hinges"):
          ≤ 900 mm    → 2 hinges (base)
          ≤ 1 600 mm  → 3 hinges (base)
          ≤ 2 000 mm  → 4 hinges (base)
          > 2 000 mm  → 5 hinges (base)
        The base count is then raised, if necessary, until the on-centre
        spacing between adjacent hinges is ≤ ``max_hinge_spacing`` — so the
        spec never recommends a layout that ``check_door_hinge_count`` would
        flag as over-spaced.
        One extra hinge is added for every 25 kg above ``max_door_weight_kg``.
        """
        # Blum's published chart (ea.blum.com "Number of hinges"): ≤900 → 2,
        # ≤1600 → 3, ≤2000 → 4, above → 5.  The 700 mm spacing raise below
        # reproduces this chart almost exactly — the old 1200/1800 table was
        # the outlier (2026-07-17 review, resolved against Blum guidance).
        if door_height <= 900:
            count = 2
        elif door_height <= 1600:
            count = 3
        elif door_height <= 2000:
            count = 4
        else:
            count = 5
        # Raise the count until adjacent hinges fall within max_hinge_spacing.
        # Hinges span hinge_inset_bottom … (door_height − hinge_inset_top);
        # with ``count`` hinges the largest gap is span / (count − 1).
        span = (door_height - self.hinge_inset_top) - self.hinge_inset_bottom
        if span > 0 and self.max_hinge_spacing > 0:
            needed = math.ceil(span / self.max_hinge_spacing) + 1
            count = max(count, needed)
        # Additional hinge for excess weight
        if door_weight_kg > self.max_door_weight_kg:
            extra = math.ceil((door_weight_kg - self.max_door_weight_kg) / 25)
            count += extra
        return count

    def hinge_positions(self, door_height: float, door_weight_kg: float = 0.0) -> list[float]:
        """Return z-positions (from door bottom) for each hinge centre.

        The first hinge is ``hinge_inset_bottom`` from the door bottom; the
        last is ``hinge_inset_top`` from the door top.  Middle hinges are
        evenly distributed across the remaining span.

        For doors too short to hold the nominal insets (``top_z <= bottom_z``)
        the cup centres are clamped to the door interior — keeping every
        position inside ``[cup_radius, door_height − cup_radius]`` and strictly
        ordered rather than the old behaviour of returning negative or
        reversed z-values.  ``validate_door`` flags such doors as too short.
        """
        count = self.hinges_for_height(door_height, door_weight_kg)
        bottom_z = self.hinge_inset_bottom
        top_z = door_height - self.hinge_inset_top
        # Clamp for short doors: keep cup centres inside the door and ordered.
        if top_z <= bottom_z:
            cup_r = self.cup_diameter / 2.0
            lo = min(cup_r, door_height / 2.0)
            hi = max(door_height - cup_r, door_height / 2.0)
            if hi <= lo:
                return [door_height / 2.0] * count
            bottom_z, top_z = lo, hi
        if count == 1:
            return [door_height / 2]
        if count == 2:
            return [bottom_z, top_z]
        # 3+ hinges: bottom, evenly-spaced middles, top
        positions = [bottom_z]
        span = top_z - bottom_z
        for i in range(1, count - 1):
            positions.append(bottom_z + span * i / (count - 1))
        positions.append(top_z)
        return positions

    def validate_door(
        self,
        door_thickness: float,
        door_height: float,
        door_width: float = 0.0,
    ) -> list[str]:
        """Check door dimensions against hinge spec. Returns list of issue strings."""
        issues = []
        if door_thickness < self.min_door_thickness:
            issues.append(
                f"Door thickness {door_thickness:.1f} mm < minimum {self.min_door_thickness} mm"
            )
        if door_thickness > self.max_door_thickness:
            issues.append(
                f"Door thickness {door_thickness:.1f} mm > maximum {self.max_door_thickness} mm"
            )
        # Check minimum edge-to-cup edge clearance (≥ 3 mm)
        edge_to_cup_edge = self.cup_boring_distance - (self.cup_diameter / 2)
        if edge_to_cup_edge < 3.0:
            issues.append(
                f"Cup boring too close to door edge: only {edge_to_cup_edge:.1f} mm margin "
                f"(minimum 3 mm). Increase cup_boring_distance."
            )
        # Door must be tall enough to seat top and bottom hinges at their
        # nominal insets without the cups overlapping.
        min_height = self.hinge_inset_top + self.hinge_inset_bottom + self.cup_diameter
        if door_height > 0 and door_height < min_height:
            issues.append(
                f"Door height {door_height:.1f} mm is too short for {self.name}: "
                f"needs at least {min_height:.1f} mm "
                f"(top inset {self.hinge_inset_top:.0f} + bottom inset "
                f"{self.hinge_inset_bottom:.0f} + cup Ø {self.cup_diameter:.0f})."
            )
        return issues


# ─── Hardware Database ────────────────────────────────────────────────────────
#
# Side clearance note (Blum undermount family):
#   The Blum installation docs specify:
#       INSIDE drawer width = inside cabinet opening − 42 mm
#   i.e. 21 mm per side measured from the cabinet side to the drawer's
#   INSIDE face — NOT to the box's outside.  The box's outside width is
#       outside = opening − 42 + 2 × drawer side thickness
#   which is why Blum's own table deducts 18 mm for 12 mm sides and only
#   10 mm for 16 mm sides (see BLUM_UNDERMOUNT_WIDTH_DEDUCTION).  The air
#   gap beside the box is the leftover, 21 − side thickness.
#   This applies to both the Tandem and Movento families in frameless
#   (Euro-style) cabinets.
#   Adjustment range of the front locking device is ±1.5 mm laterally, giving
#   a workable window of roughly 19.5–22.5 mm per side ON THE INSIDE FACE.
#
# Part number conventions:
#   Tandem 550H  : 550H{length×10}B  e.g. 550H4500B = 450 mm
#   Tandem+ 563H : 563H{length×10}B  e.g. 563H5330B = 533 mm
#   Movento 760H : 760H{length×10}S  e.g. 760H4500S = 450 mm  (S = Blumotion)
#                  760H{length×10}T  for TIP-ON variant
#   Movento 769  : 769.{length×10}S  e.g. 769.4570S = 457 mm


# ── Blum Tandem 550H (partial extension, 30 kg) ───────────────────────────────

BLUM_TANDEM_550H = DrawerSlideSpec(
    # Concealed single-extension runner with integrated Blumotion soft-close.
    # For wooden drawer sides 11–19 mm thick, frameless cabinets.
    # Available lengths: 270–600 mm (metric series; no 250 mm variant exists).
    # Source: Blum 550H datasheet; distributor cross-reference (mcfaddens.com,
    #   interfitco.com); CabinetParts catalog confirmed 450 mm = 550H4500B,
    #   550 mm = 550H5500B.
    name="Blum Tandem 550H",
    extension="3/4",
    manufacturer="Blum",
    slide_type=SlideType.UNDERMOUNT,
    mount_location=SlideMountLocation.BOTTOM,
    # Blum formula: INSIDE drawer width = opening − 42 mm → 21 mm per side,
    # measured to the drawer's inside face (outside = opening − 42 + 2 × side).
    min_side_clearance=19.5,
    max_side_clearance=22.5,
    nominal_side_clearance=21.0,
    min_top_clearance=7.0,          # 9/32" — keep this gap above drawer box
    min_bottom_clearance=14.0,      # 9/16" — slide body height below drawer
    available_lengths=(270, 300, 350, 400, 450, 500, 550, 600),
    max_load_kg=30,
    min_drawer_height=68,
    max_drawer_width=0,
    rear_bracket_inset=2.0,
    front_bracket_inset=2.0,
    part_numbers={
        270: "550H2700B",
        300: "550H3000B",
        350: "550H3500B",
        400: "550H4000B",
        450: "550H4500B",  # confirmed
        500: "550H5000B",
        550: "550H5500B",  # confirmed
        600: "550H6000B",
    },
)


# ── Blum Tandem Plus 563H (full extension, 45 kg) ─────────────────────────────

BLUM_TANDEM_PLUS_563H = DrawerSlideSpec(
    # Full-extension Blumotion runner for frameless cabinets, ½"–⅝" drawer
    # sides. Inch-series lengths 9"–21". 90 lb / 41 kg load rating.
    # Source: Blum 563H datasheet © 2016; Woodworker Express listings
    #   (slides.html, May 2026 pricing) confirm SKUs 563H2290B10, 563H3050B,
    #   563H3810B, 563H4570B, 563H5330B at $27.00 / $22.26 / $22.26 / $21.33 /
    #   $21.99 per pair.
    # The 9" price premium over longer lengths is CONFIRMED REAL (Charlie,
    #   Jul 2026: WWE + multiple vendors agree; part number correct, not a
    #   kit — low-volume specialty length). Not a data error; do not "fix".
    name="Blum Tandem Plus 563H",
    manufacturer="Blum",
    slide_type=SlideType.UNDERMOUNT,
    mount_location=SlideMountLocation.BOTTOM,
    min_side_clearance=19.5,
    max_side_clearance=22.5,
    nominal_side_clearance=21.0,
    min_top_clearance=6.0,          # ¼" — slightly tighter than 550H
    min_bottom_clearance=14.0,      # 9/16"
    available_lengths=(229, 305, 381, 457, 533),  # 9", 12", 15", 18", 21"
    max_load_kg=41,                 # 90 lb (Woodworker Express listing)
    min_drawer_height=68,
    max_drawer_width=0,
    rear_bracket_inset=2.0,
    front_bracket_inset=2.0,
    part_numbers={
        229: "563H2290B10",  # 9"  — confirmed (Woodworker Express)
        305: "563H3050B",    # 12" — confirmed (Woodworker Express)
        381: "563H3810B",    # 15" — confirmed (Woodworker Express)
        457: "563H4570B",    # 18" — confirmed (Woodworker Express, CabinetParts)
        533: "563H5330B",    # 21" — confirmed (Woodworker Express, CabinetParts)
    },
)


# ── Blum Tandem Plus 563F (3/4" drawer-side variant of 563H) ─────────────────

BLUM_TANDEM_PLUS_563F = DrawerSlideSpec(
    # Same full-extension BLUMOTION runner as the 563H; the F suffix is the
    # DRAWER-SIDE THICKNESS variant (H = 1/2"-5/8" sides, F = 5/8"-3/4"),
    # NOT face-frame and NOT extension — verified against Woodworker Express
    # Q&A + CabinetParts listings, Jul 2026, after Charlie challenged the
    # suffix meaning. (Either model mounts in face-frame cabinets with the
    # usual rear brackets.)
    # Source: Woodworker Express listings (slides.html, May 2026 pricing)
    #   SKUs 563F2290B10, 563F3050B, 563F3810B, 563F4570B, 563F5330B at
    #   $28.27 / $23.75 / $23.75 / $24.11 / $24.81 per pair.
    # 9" premium confirmed real across vendors (see 563H note) — not a kit,
    #   not a data error.
    name="Blum Tandem Plus 563F (3/4\" drawer sides)",
    manufacturer="Blum",
    slide_type=SlideType.UNDERMOUNT,
    mount_location=SlideMountLocation.BOTTOM,
    min_side_clearance=19.5,
    max_side_clearance=22.5,
    nominal_side_clearance=21.0,
    min_top_clearance=6.0,
    min_bottom_clearance=14.0,
    available_lengths=(229, 305, 381, 457, 533),
    max_load_kg=41,
    min_drawer_height=68,
    max_drawer_width=0,
    rear_bracket_inset=2.0,
    front_bracket_inset=2.0,
    part_numbers={
        229: "563F2290B10",
        305: "563F3050B",
        381: "563F3810B",
        457: "563F4570B",
        533: "563F5330B",
    },
)


# ── Blum Movento 760H (full extension, 40 kg) ─────────────────────────────────

BLUM_MOVENTO_760H = DrawerSlideSpec(
    # Full-extension concealed runner with Blumotion. 40 kg load.
    # Available in metric series 250–600 mm plus 270 mm.
    # The "S" suffix in part numbers = Blumotion soft-close.
    # "T" suffix = TIP-ON (push-to-open) variant; same lengths available.
    # Source: Blum Movento brochure "The Evolution of Motion" (2024);
    #   distributor SKUs confirmed for 250 mm (760H2500S), 300 mm (760H3000S),
    #   350 mm (760H3500S), 450 mm (760H4500S), 500 mm (760H5000S),
    #   550 mm (760H5500S), 600 mm (760H6000S) via mcfaddens.com / hwt-pro.com.
    name="Blum Movento 760H",
    manufacturer="Blum",
    slide_type=SlideType.UNDERMOUNT,
    mount_location=SlideMountLocation.BOTTOM,
    # Blum formula: INSIDE drawer width = opening − 42 mm → 21 mm per side,
    # measured to the drawer's inside face (outside = opening − 42 + 2 × side).
    min_side_clearance=19.5,
    max_side_clearance=22.5,
    nominal_side_clearance=21.0,
    min_top_clearance=3.0,
    min_bottom_clearance=15.0,      # Movento body is slightly taller than Tandem
    available_lengths=(250, 270, 300, 350, 400, 450, 500, 550, 600),
    max_load_kg=40,
    min_drawer_height=68,
    max_drawer_width=1200,
    rear_bracket_inset=2.0,
    front_bracket_inset=2.0,
    part_numbers={
        250: "760H2500S",  # 10" — confirmed
        270: "760H2700S",  # 270 mm — confirmed (also as 760H2700T TIP-ON)
        300: "760H3000S",  # 12" — confirmed
        350: "760H3500S",  # 14" — confirmed
        400: "760H4000S",  # 16" — pattern-derived
        450: "760H4500S",  # 18" — confirmed
        500: "760H5000S",  # 20" — confirmed
        550: "760H5500S",  # 22" — confirmed
        600: "760H6000S",  # 24" — confirmed
    },
)


# ── Blum Movento 769 (full extension, heavy duty, 70 kg dynamic) ─────────────

BLUM_MOVENTO_769 = DrawerSlideSpec(
    # Heavy-duty Movento. 170 lb static / 155 lb dynamic load (~77/70 kg).
    # Inch-series lengths 18"–30" (457–762 mm). Requires front locking devices
    # ordered separately. For ½"–⅝" drawer sides.
    # Source: Blum 769 catalog page © 2019; confirmed SKUs:
    #   769.4570S = 18" (457 mm), 769.4570M = 18" alternate finish,
    #   769.5330S / 769.5330M = 21" (533 mm),
    #   769.6100S = 24" (610 mm) via Indian River Cabinet Supply / siggia.
    #   686 mm (27") and 762 mm (30") part numbers pattern-derived.
    name="Blum Movento 769",
    manufacturer="Blum",
    slide_type=SlideType.UNDERMOUNT,
    mount_location=SlideMountLocation.BOTTOM,
    min_side_clearance=19.5,
    max_side_clearance=22.5,
    nominal_side_clearance=21.0,
    min_top_clearance=3.0,
    min_bottom_clearance=15.0,
    available_lengths=(457, 533, 610, 686, 762),  # 18"–30"
    max_load_kg=70,  # dynamic rating; 77 kg is the static figure
    min_drawer_height=68,
    max_drawer_width=1200,
    rear_bracket_inset=2.0,
    front_bracket_inset=2.0,
    part_numbers={
        457: "769.4570S",  # 18" — confirmed
        533: "769.5330S",  # 21" — confirmed
        610: "769.6100S",  # 24" — confirmed
        686: "769.6860S",  # 27" — pattern-derived
        762: "769.7620S",  # 30" — pattern-derived
    },
)


# ── Accuride 3832 ─────────────────────────────────────────────────────────────

ACCURIDE_3832 = DrawerSlideSpec(
    # Classic heavy-duty side-mount ball-bearing slide. 45 kg load.
    # Full extension, up to 700 mm. Common in commercial and utility cabinets.
    # Side-mount slides use a different clearance model: the slide body mounts
    # on the drawer side, so clearance per side = slide body thickness (~12.7 mm).
    name="Accuride 3832",
    manufacturer="Accuride",
    slide_type=SlideType.SIDE_MOUNT,
    mount_location=SlideMountLocation.SIDE,
    min_side_clearance=12.5,        # ½" per side — slide body thickness
    max_side_clearance=13.5,
    nominal_side_clearance=12.7,
    min_top_clearance=2.0,
    min_bottom_clearance=0.0,       # side-mount — no bottom clearance needed
    available_lengths=(250, 300, 350, 400, 450, 500, 550, 600, 650, 700),
    max_load_kg=45,
    min_drawer_height=40,
    max_drawer_width=0,
    rear_bracket_inset=0.0,
    front_bracket_inset=0.0,
    part_numbers={},                # Accuride uses length-coded SKUs; omitted here
    sold_as_pair=False,             # side-mount slides are sold as singles
)


# ── Salice Futura ─────────────────────────────────────────────────────────────

SALICE_FUTURA = DrawerSlideSpec(
    # Salice Futura undermount soft-close. 34 kg dynamic / 45 kg static.
    # For ½"–⅝" drawer sides. Lengths 12"–21" (305–533 mm).
    # Source: Salice Futura catalog D0CASG010ENG; wwhardware.com specs.
    name="Salice Futura",
    manufacturer="Salice",
    slide_type=SlideType.UNDERMOUNT,
    mount_location=SlideMountLocation.BOTTOM,
    min_side_clearance=19.5,
    max_side_clearance=22.5,
    nominal_side_clearance=21.0,
    min_top_clearance=3.0,
    min_bottom_clearance=13.0,
    available_lengths=(305, 381, 457, 533),
    max_load_kg=34,  # dynamic rating; 45 kg is the static figure
    min_drawer_height=79,           # taller slide body than Blum Tandem
    max_drawer_width=0,
    rear_bracket_inset=0.0,         # Salice clips mount flush to back
    front_bracket_inset=0.0,
    part_numbers={
        305: "A7555/305",   # 12" — confirmed (CabinetParts / woodworkerexpress)
        381: "A7555/381",   # 15" — pattern-derived
        457: "A7555/457",   # 18" — pattern-derived
        533: "A7555/533",   # 21" — confirmed (CabinetParts / woodworkerexpress)
    },
)

SALICE_FUTURA_SMOVE = DrawerSlideSpec(
    # Futura with SMOVE progressive soft-close (load-adaptive damping).
    # Same mounting footprint and lengths as standard Futura.
    # Source: Salice Futura SMOVE page; rokhardware.com.
    name="Salice Futura Smove",
    manufacturer="Salice",
    slide_type=SlideType.UNDERMOUNT,
    mount_location=SlideMountLocation.BOTTOM,
    min_side_clearance=19.5,
    max_side_clearance=22.5,
    nominal_side_clearance=21.0,
    min_top_clearance=3.0,
    min_bottom_clearance=13.0,
    available_lengths=(305, 381, 457, 533),
    max_load_kg=34,  # dynamic rating; 45 kg is the static figure
    min_drawer_height=79,
    max_drawer_width=0,
    rear_bracket_inset=0.0,
    front_bracket_inset=0.0,
    part_numbers={},                # SMOVE part numbers vary by clip type; omitted
)


# ── Salice Progressa / Progressa+ ─────────────────────────────────────────────

SALICE_PROGRESSA_PLUS = DrawerSlideSpec(
    # Salice Progressa+ undermount soft-close. 54 kg (120 lb) load.
    # Widest length range: 229–762 mm (9"–30").
    # For ½"–⅝" drawer sides. Face-frame and frameless compatible.
    # Source: Salice PROGRESSA catalog D0CASAA36USA; cabinetparts.com SKUs:
    #   SHG5U6S533XXF6 = 21" confirmed.
    name="Salice Progressa+",
    manufacturer="Salice",
    slide_type=SlideType.UNDERMOUNT,
    mount_location=SlideMountLocation.BOTTOM,
    min_side_clearance=19.5,
    max_side_clearance=22.5,
    nominal_side_clearance=21.0,
    min_top_clearance=3.0,
    min_bottom_clearance=13.0,
    available_lengths=(229, 305, 381, 457, 533, 610, 686, 762),
    max_load_kg=54,
    min_drawer_height=79,
    max_drawer_width=0,
    rear_bracket_inset=2.0,
    front_bracket_inset=2.0,
    part_numbers={
        229: "G5U6S229",    # 9"  — pattern-derived
        305: "G5U6S305",    # 12" — pattern-derived
        381: "G5U6S381",    # 15" — pattern-derived
        457: "G5U6S457",    # 18" — pattern-derived
        533: "G5U6S533",    # 21" — confirmed (cabinetparts SHG5U6S533XXF6 base)
        610: "G5U6S610",    # 24" — pattern-derived
        686: "G5U6S686",    # 27" — length confirmed orderable (US inch series;
                            # cabinetparts.com lists 27" Progressa+ Smove), part
                            # number pattern-derived
        762: "G5U6S762",    # 30" — pattern-derived (marathonhardware SG7E6S700XXB ≈ 700 mm is a nearby length, not this SKU)
    },
)

SALICE_PROGRESSA_PLUS_SMOVE = DrawerSlideSpec(
    # Progressa+ with SMOVE progressive soft-close.
    # Same specs as Progressa+; stronger, load-adaptive damping at end of travel.
    # Source: Salice Progressa+ SMOVE page; hardwarehut.com.
    name="Salice Progressa+ Smove",
    manufacturer="Salice",
    slide_type=SlideType.UNDERMOUNT,
    mount_location=SlideMountLocation.BOTTOM,
    min_side_clearance=19.5,
    max_side_clearance=22.5,
    nominal_side_clearance=21.0,
    min_top_clearance=3.0,
    min_bottom_clearance=13.0,
    available_lengths=(229, 305, 381, 457, 533, 610, 686, 762),
    max_load_kg=54,
    min_drawer_height=79,
    max_drawer_width=0,
    rear_bracket_inset=2.0,
    front_bracket_inset=2.0,
    part_numbers={},                # SMOVE part numbers vary by clip/finish; omitted
)


# ── Hinges ────────────────────────────────────────────────────────────────────
#
# Blum CLIP top 110° family — three arm types:
#   Full overlay (B arm)  : door overlaps cabinet side 16 mm, part 71B35xx
#   Half overlay (H arm)  : door overlaps 9.5 mm for shared partitions, 71H35xx
#   Inset (N arm, cranked): door sits inside opening, 0 mm overlay, 71N35xx
#
# Standard vs BLUMOTION (soft-close):
#   Standard    : 71x3550  (no integrated damper)
#   BLUMOTION   : 71x3590  (integrated progressive soft-close)
#
# All Clip Top hinges share the same cup (35 mm Ø × 13 mm deep at 22.5 mm from
# door edge) and mounting plate.  Max door weight per *hinge pair*: 20 kg
# standard; 25 kg BLUMOTION (Blum 2023 catalog).
#
# Clip Top 170° is the wide-angle variant for corner/pie-cut doors; same cup
# geometry, same arm types but limited to full overlay only in standard catalog.

# ── Blum Clip Top 110° — Full Overlay ─────────────────────────────────────────

BLUM_CLIP_TOP_110_FULL = HingeSpec(
    # Standard (no soft-close), full overlay, straight arm.
    # Source: Blum CLIP top datasheet. 71T3590 = plain 110° full INSERTA
    # (71B3550 is the BLUMOTION screw-on, NOT plain — fixed 2026-07-28).
    name="Blum Clip Top 110° Full Overlay",
    manufacturer="Blum",
    overlay_type=OverlayType.FULL,
    overlay=16.0,
    cup_diameter=35.0,
    cup_depth=13.0,
    cup_boring_distance=22.5,
    min_door_thickness=16.0,
    max_door_thickness=25.0,
    opening_angle=110,
    soft_close=False,
    max_door_weight_kg=20.0,
    part_number="71T3590",
    mounting_plate_part="173L8100",
)

BLUM_CLIP_TOP_BLUMOTION_110_FULL = HingeSpec(
    # Integrated BLUMOTION soft-close, full overlay.
    # Source: Blum CLIP top BLUMOTION datasheet; cabinetparts BH71B3590.
    name="Blum Clip Top BLUMOTION 110° Full Overlay",
    manufacturer="Blum",
    overlay_type=OverlayType.FULL,
    overlay=16.0,
    cup_diameter=35.0,
    cup_depth=13.0,
    cup_boring_distance=22.5,
    min_door_thickness=16.0,
    max_door_thickness=25.0,
    opening_angle=110,
    soft_close=True,
    max_door_weight_kg=25.0,
    part_number="71B3590",
    mounting_plate_part="173L8100",
)

# ── Blum Clip Top 110° — Half Overlay ─────────────────────────────────────────

BLUM_CLIP_TOP_110_HALF = HingeSpec(
    # Half overlay (9.5 mm) — for shared partition between two adjacent cabinets.
    # Source: Blum catalog; 71T3690 = plain half-overlay INSERTA.
    name="Blum Clip Top 110° Half Overlay",
    manufacturer="Blum",
    overlay_type=OverlayType.HALF,
    overlay=9.5,
    cup_diameter=35.0,
    cup_depth=13.0,
    cup_boring_distance=22.5,
    min_door_thickness=16.0,
    max_door_thickness=25.0,
    opening_angle=110,
    soft_close=False,
    max_door_weight_kg=20.0,
    part_number="71T3690",
    mounting_plate_part="173L8100",
)

BLUM_CLIP_TOP_BLUMOTION_110_HALF = HingeSpec(
    # BLUMOTION soft-close, half overlay.
    # Source: Blum catalog; part number pattern from 71H35xx family.
    name="Blum Clip Top BLUMOTION 110° Half Overlay",
    manufacturer="Blum",
    overlay_type=OverlayType.HALF,
    overlay=9.5,
    cup_diameter=35.0,
    cup_depth=13.0,
    cup_boring_distance=22.5,
    min_door_thickness=16.0,
    max_door_thickness=25.0,
    opening_angle=110,
    soft_close=True,
    max_door_weight_kg=25.0,
    part_number="71B3690",
    mounting_plate_part="173L8100",
)

# ── Blum Clip Top 110° — Inset ────────────────────────────────────────────────

BLUM_CLIP_TOP_110_INSET = HingeSpec(
    # Inset / cranked arm (N arm) — door sits flush inside opening.
    # Overlay = 0; door is narrower than opening by the reveal gap on each side.
    # Source: Blum catalog; 71T3790 = plain inset INSERTA (cranked arm).
    name="Blum Clip Top 110° Inset",
    manufacturer="Blum",
    overlay_type=OverlayType.INSET,
    overlay=0.0,
    cup_diameter=35.0,
    cup_depth=13.0,
    cup_boring_distance=22.5,
    min_door_thickness=16.0,
    max_door_thickness=25.0,
    opening_angle=110,
    soft_close=False,
    max_door_weight_kg=20.0,
    part_number="71T3790",
    mounting_plate_part="173L8100",
)

BLUM_CLIP_TOP_BLUMOTION_110_INSET = HingeSpec(
    # BLUMOTION soft-close, inset / cranked arm.
    # Source: Blum catalog; part number pattern from 71N35xx family.
    name="Blum Clip Top BLUMOTION 110° Inset",
    manufacturer="Blum",
    overlay_type=OverlayType.INSET,
    overlay=0.0,
    cup_diameter=35.0,
    cup_depth=13.0,
    cup_boring_distance=22.5,
    min_door_thickness=16.0,
    max_door_thickness=25.0,
    opening_angle=110,
    soft_close=True,
    max_door_weight_kg=25.0,
    part_number="71B3790",
    mounting_plate_part="173L8100",
)

# ── Blum Clip Top 170° — Full Overlay (wide-angle / corner) ───────────────────

BLUM_CLIP_TOP_170_FULL = HingeSpec(
    # Wide-angle hinge for corner cabinets and areas with restricted access.
    # Opens to 170°, allowing full access past the cabinet side.
    # Only available with full overlay arm in the standard catalog.
    # Source: Blum CLIP top 170° datasheet; hafele.com / cabinetparts.com.
    name="Blum Clip Top 170° Full Overlay",
    manufacturer="Blum",
    overlay_type=OverlayType.FULL,
    overlay=16.0,
    cup_diameter=35.0,
    cup_depth=13.0,
    cup_boring_distance=22.5,
    min_door_thickness=16.0,
    max_door_thickness=25.0,
    opening_angle=170,
    soft_close=False,
    max_door_weight_kg=20.0,
    part_number="71T6550",
    mounting_plate_part="173L8100",
    cup_screws=2,
)

# Legacy aliases kept for backward compatibility with existing code.
BLUM_CLIP_TOP_110 = BLUM_CLIP_TOP_110_FULL
BLUM_CLIP_TOP_170 = BLUM_CLIP_TOP_170_FULL


# ─── Lookup ───────────────────────────────────────────────────────────────────

SLIDES: dict[str, DrawerSlideSpec] = {
    # Blum Tandem
    "blum_tandem_550h":       BLUM_TANDEM_550H,
    "blum_tandem_plus_563h":  BLUM_TANDEM_PLUS_563H,
    "blum_tandem_plus_563f":  BLUM_TANDEM_PLUS_563F,
    # Blum Movento
    "blum_movento_760h":      BLUM_MOVENTO_760H,
    "blum_movento_769":       BLUM_MOVENTO_769,
    # Accuride
    "accuride_3832":          ACCURIDE_3832,
    # Salice Futura
    "salice_futura":          SALICE_FUTURA,
    "salice_futura_smove":    SALICE_FUTURA_SMOVE,
    # Salice Progressa+
    "salice_progressa_plus":          SALICE_PROGRESSA_PLUS,
    "salice_progressa_plus_smove":    SALICE_PROGRESSA_PLUS_SMOVE,
}

HINGES: dict[str, HingeSpec] = {
    # Blum Clip Top 110° — three overlay types, standard and BLUMOTION
    "blum_clip_top_110_full":             BLUM_CLIP_TOP_110_FULL,
    "blum_clip_top_blumotion_110_full":   BLUM_CLIP_TOP_BLUMOTION_110_FULL,
    "blum_clip_top_110_half":             BLUM_CLIP_TOP_110_HALF,
    "blum_clip_top_blumotion_110_half":   BLUM_CLIP_TOP_BLUMOTION_110_HALF,
    "blum_clip_top_110_inset":            BLUM_CLIP_TOP_110_INSET,
    "blum_clip_top_blumotion_110_inset":  BLUM_CLIP_TOP_BLUMOTION_110_INSET,
    # Blum Clip Top 170° wide-angle
    "blum_clip_top_170_full":             BLUM_CLIP_TOP_170_FULL,
    # Legacy keys (backward compatibility)
    "blum_clip_top_110":                  BLUM_CLIP_TOP_110,
    "blum_clip_top_170":                  BLUM_CLIP_TOP_170,
}


def get_slide(name: str) -> DrawerSlideSpec:
    """Look up a slide spec by key."""
    if name not in SLIDES:
        raise KeyError(f"Unknown slide '{name}'. Available: {list(SLIDES.keys())}")
    return SLIDES[name]


def get_hinge(name: str) -> HingeSpec:
    """Look up a hinge spec by key."""
    if name not in HINGES:
        raise KeyError(f"Unknown hinge '{name}'. Available: {list(HINGES.keys())}")
    return HINGES[name]


# ─── Legs / Feet ─────────────────────────────────────────────────────────────


class LegPattern(Enum):
    """Foot placement pattern for a cabinet base."""
    CORNERS              = "corners"               # one foot at each corner
    CORNERS_AND_MIDSPAN  = "corners_and_midspan"   # corners + one centred on each long side
    ALONG_FRONT_BACK     = "along_front_back"      # evenly spaced rows front & back


@dataclass(frozen=True)
class LegSpec:
    """Specifications for a cabinet leg / furniture foot.

    All dimensions in millimetres unless otherwise noted.

    Adjustable legs have a threaded stem; ``adjustment_range_mm`` is the total
    travel.  Fixed legs have ``is_adjustable=False`` and ``adjustment_range_mm``
    should be zero.

    ``base_diameter_mm`` is the load-bearing pad or flange diameter (not the
    stem).  ``stem_diameter_mm`` is the threaded section (0 for fixed legs).
    """
    name: str
    manufacturer: str
    height_mm: float                # nominal / mid-range height
    base_diameter_mm: float         # floor pad / flange diameter
    is_adjustable: bool
    adjustment_range_mm: float      # total travel for adjustable legs; 0 for fixed
    stem_diameter_mm: float         # threaded stem Ø; 0 for fixed legs
    load_capacity_kg: float         # rated load per leg
    finish: str                     # e.g. "brushed_nickel", "matte_black", "chrome"
    part_number: str = ""
    notes: str = ""
    pack_quantity: int = 1          # legs per retail pack (1 = sold individually)


# ── Richelieu 176138106 — Contemporary Square Leg, 100 mm, Brushed Nickel ────
#
# Fixed contemporary square metal leg from Richelieu's 1761 series.
# Sold in packs of 2; load 50 kg (110 lb) per leg.  Has an integrated felt pad
# to protect floors.  Height is 3-15/16" = ~100 mm, not a true 4".
# Source: thebuilderssupply.com, dspoutlet.com product pages; Richelieu catalog.

RICHELIEU_176138106 = LegSpec(
    name="Richelieu Contemporary Square Leg 100mm",
    manufacturer="Richelieu",
    height_mm=100.0,           # 3-15/16" ≈ 100 mm
    base_diameter_mm=38.0,     # square base ~38 mm × 38 mm; use diameter for cylinder approx
    is_adjustable=False,
    adjustment_range_mm=0.0,
    stem_diameter_mm=0.0,
    load_capacity_kg=50.0,
    finish="brushed_nickel",
    part_number="176138106",
    notes="Square contemporary leg, integrated floor pad. Sold 2/pack.",
    pack_quantity=2,
)

# ── Richelieu 17613B106 — Contemporary Square Leg, 100 mm, Matte Black ───────

RICHELIEU_17613B106 = LegSpec(
    name="Richelieu Contemporary Square Leg 100mm Matte Black",
    manufacturer="Richelieu",
    height_mm=100.0,
    base_diameter_mm=38.0,
    is_adjustable=False,
    adjustment_range_mm=0.0,
    stem_diameter_mm=0.0,
    load_capacity_kg=50.0,
    finish="matte_black",
    part_number="17613B106",
    notes="Square contemporary leg, integrated floor pad. Sold 2/pack.",
    pack_quantity=2,
)

# ── Richelieu Adjustable Leg, 40–65 mm, Aluminium ────────────────────────────
#
# Economy adjustable leveling leg.  Common in flat-pack / Euro-style cabinets.
# Threaded M8 stem; adjustment range ≈ 25 mm via threaded insert.
# Source: woodcraft.com product page; Richelieu catalog.

RICHELIEU_ADJUSTABLE_40MM = LegSpec(
    name="Richelieu Adjustable Furniture Leg 40–65mm",
    manufacturer="Richelieu",
    height_mm=52.5,            # midpoint of 40–65 mm range
    base_diameter_mm=50.0,     # round base flange
    is_adjustable=True,
    adjustment_range_mm=25.0,
    stem_diameter_mm=8.0,      # M8 thread
    load_capacity_kg=60.0,
    finish="aluminum",
    part_number="RICALEG40",   # generic / catalog-dependent
    notes="Threaded M8 adjustable leg, 40–65 mm travel. For Euro-style cabinet bases.",
)

# ── Generic Hairpin Leg, 200 mm, Matte Black ─────────────────────────────────
# Popular for media consoles, credenzas, and modern case pieces.

HAIRPIN_200MM = LegSpec(
    name="Hairpin Leg 200mm",
    manufacturer="Generic",
    height_mm=200.0,
    base_diameter_mm=10.0,     # rod diameter (3-rod hairpin; footprint wider)
    is_adjustable=False,
    adjustment_range_mm=0.0,
    stem_diameter_mm=0.0,
    load_capacity_kg=30.0,
    finish="matte_black",
    part_number="",
    notes="3-rod steel hairpin leg with mounting plate. Common in furniture stores.",
)


HAIRPIN_152MM = LegSpec(
    name="Hairpin Leg 152mm (6\")",
    manufacturer="Generic",
    height_mm=152.4,           # exact 6 in
    base_diameter_mm=10.0,
    is_adjustable=False,
    adjustment_range_mm=0.0,
    stem_diameter_mm=0.0,
    load_capacity_kg=30.0,
    finish="matte_black",
    part_number="",
    notes="3-rod steel hairpin leg, 6\". Generic furniture-grade.",
)


LEGS: dict[str, LegSpec] = {
    "richelieu_176138106":       RICHELIEU_176138106,
    "richelieu_17613b106":       RICHELIEU_17613B106,
    "richelieu_adjustable_40mm": RICHELIEU_ADJUSTABLE_40MM,
    "hairpin_152mm":             HAIRPIN_152MM,
    "hairpin_200mm":             HAIRPIN_200MM,
}


def get_leg(name: str) -> LegSpec:
    """Look up a leg spec by key."""
    if name not in LEGS:
        raise KeyError(f"Unknown leg '{name}'. Available: {list(LEGS.keys())}")
    return LEGS[name]


# ─── Pulls and Knobs ──────────────────────────────────────────────────────────
#
# Pulls live in an external JSON catalog (data/pulls_catalog.json) rather than
# being hand-written as module-level constants, because there are many variants
# (currently 45 across 5 brands) that differ only in size/finish and it's more
# convenient to treat the catalog as data.  The JSON is loaded once at import
# time and materialised into a dict of frozen PullSpec objects.
#
# Schema version 1.7.0 of the catalog requires a per-entry "mount_style" field.
#
# Mount styles
# ------------
#   surface  — standard bar pull with two through-holes into the face.  Most
#              Top Knobs, Rockler, Hafele, and IKEA bar-style pulls fall here.
#   edge     — edge pull gripped from above (drawer) or from behind the leading
#              edge (door); mounts with two screws through the face the same
#              way surface pulls do, but sits proud of the face by design.
#   flush    — recessed pull routed into the face; no mounting screws through
#              the face (hole_count = 0).  The cc_mm is the cutout length.
#   knob     — single-screw fastener; one mounting hole at the knob centre.
#              cc_mm is 0.
#
# Knob support is provided in the schema even though the current catalog has
# no knob entries — add them to the JSON and they work automatically.


class MountStyle(Enum):
    """How a pull or knob is fastened to a drawer front or door."""
    SURFACE = "surface"   # two through-holes in the face; bar pull style
    EDGE    = "edge"      # sits on the top/leading edge, fastens through face
    FLUSH   = "flush"     # routed recess, no through-holes
    KNOB    = "knob"      # single centre hole


@dataclass(frozen=True)
class PullSpec:
    """Specification for a cabinet pull or knob.

    All dimensions in millimetres.

    ``cc_mm`` is the centre-to-centre distance between mounting holes.  For
    knobs it is 0 (single hole).  For flush / inset pulls it describes the
    cutout opening length, not a hole spacing.

    ``length_mm`` is the overall length of the pull body.

    ``projection_mm`` is how far the pull stands proud of the face.  For flush
    pulls this is typically ≤ 2 mm (the lip of the recessed cup).
    """
    id: str                       # stable catalog identifier, e.g. "topknobs-hb-128"
    name: str                     # display name
    brand: str
    model_number: str
    url: str
    style: str                    # design style: "Transitional", "Minimalist", ...
    material: str
    finish: str
    mount_style: MountStyle
    pack_quantity: int            # units per SKU pack (IKEA sells in 2-packs)
    cc_mm: float                  # hole spacing (0 for knobs)
    length_mm: float              # overall pull length
    projection_mm: float          # stand-off from face
    tags: tuple[str, ...] = ()

    @property
    def hole_count(self) -> int:
        """Number of mounting screws through the face."""
        if self.mount_style is MountStyle.KNOB:
            return 1
        if self.mount_style is MountStyle.FLUSH:
            return 0
        # surface and edge pulls — both use two through-holes
        return 2

    @property
    def hole_offsets_from_center(self) -> tuple[float, ...]:
        """X-offsets (along the pull's long axis) of each mounting hole from
        the pull's centre point.  Empty tuple for flush pulls."""
        n = self.hole_count
        if n == 0:
            return ()
        if n == 1:
            return (0.0,)
        return (-self.cc_mm / 2.0, self.cc_mm / 2.0)

    @property
    def is_knob(self) -> bool:
        return self.mount_style is MountStyle.KNOB


# ─── Catalog loader ──────────────────────────────────────────────────────────
#
# Loaded once at import time.  The catalog file ships as package data — see
# pyproject.toml [tool.setuptools.package-data].  Use importlib.resources so
# it resolves correctly whether the package is installed, run from a wheel,
# or imported from a src-layout source tree.

_REQUIRED_PULL_FIELDS = {
    "id", "name", "brand", "model_number", "mount_style", "dimensions",
}
_REQUIRED_DIMENSION_FIELDS = {"cc_mm", "length_mm", "projection_mm"}


def _load_pulls_from_catalog(catalog_path=None) -> dict[str, PullSpec]:
    """Read the JSON catalog and build the PULLS registry.

    ``catalog_path`` may be passed for testing; otherwise the packaged
    ``data/pulls_catalog.json`` is used.
    """
    if catalog_path is None:
        data_resource = resources.files("cabineteer") / "data" / "pulls_catalog.json"
        raw_text = data_resource.read_text(encoding="utf-8")
    else:
        with open(catalog_path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    doc = json.loads(raw_text)

    out: dict[str, PullSpec] = {}
    for entry in doc.get("pulls", []):
        missing = _REQUIRED_PULL_FIELDS - entry.keys()
        if missing:
            raise ValueError(f"Pull entry missing fields {missing}: {entry.get('id', '?')}")
        dims = entry["dimensions"]
        miss_dims = _REQUIRED_DIMENSION_FIELDS - dims.keys()
        if miss_dims:
            raise ValueError(f"Pull {entry['id']} dimensions missing {miss_dims}")
        try:
            ms = MountStyle(entry["mount_style"])
        except ValueError as exc:
            raise ValueError(
                f"Pull {entry['id']} has unknown mount_style {entry['mount_style']!r}"
            ) from exc

        pid = entry["id"]
        if pid in out:
            raise ValueError(f"Duplicate pull id in catalog: {pid}")
        out[pid] = PullSpec(
            id=pid,
            name=entry["name"],
            brand=entry["brand"],
            model_number=entry["model_number"],
            url=entry.get("url", ""),
            style=entry.get("style", ""),
            material=entry.get("material", ""),
            finish=entry.get("finish", ""),
            mount_style=ms,
            pack_quantity=int(entry.get("pack_quantity", 1)),
            cc_mm=float(dims.get("cc_mm", 0.0)),
            length_mm=float(dims["length_mm"]),
            projection_mm=float(dims["projection_mm"]),
            tags=tuple(entry.get("tags", ())),
        )
    return out


PULLS: dict[str, PullSpec] = _load_pulls_from_catalog()


def get_pull(name: str) -> PullSpec:
    """Look up a pull spec by id. Raises KeyError with the available keys on miss."""
    if name not in PULLS:
        raise KeyError(f"Unknown pull '{name}'. {len(PULLS)} available; first few: "
                       f"{list(PULLS.keys())[:5]}")
    return PULLS[name]


# ── Pull presets ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PullPreset:
    """A named bundle of pull settings for a complete cabinet."""
    key: str
    style_name: str
    description: str
    drawer_pull: str
    door_pull: str
    door_pull_inset_mm: float = 50.0


def _load_pull_presets() -> dict[str, "PullPreset"]:
    data_resource = resources.files("cabineteer") / "data" / "pull_presets.json"
    raw: dict = json.loads(data_resource.read_text(encoding="utf-8"))
    return {key: PullPreset(key=key, **vals) for key, vals in raw.items()}


PULL_PRESETS: dict[str, PullPreset] = _load_pull_presets()


def get_pull_preset(key: str) -> PullPreset:
    """Return a PullPreset by key. Raises KeyError with available options on miss."""
    try:
        return PULL_PRESETS[key]
    except KeyError:
        available = ", ".join(sorted(PULL_PRESETS))
        raise KeyError(
            f"Pull preset {key!r} not found. Available: {available}"
        ) from None


# ─── Price list ───────────────────────────────────────────────────────────────
# List / MSRP prices in USD.  Not market prices — use as rough estimates only.
# Keys match hardware catalog keys (SLIDES, HINGES, LEGS, PULLS) and joinery
# SKUs used in cutlist.py.  Sheet goods keys: "sheet_baltic_birch_{t}mm".

PRICE_LIST: dict[str, float] = {
    # ── Sheet goods — per 4×8 sheet ──────────────────────────────────────────
    "sheet_baltic_birch_18mm":  95.00,
    "sheet_baltic_birch_15mm":  82.00,
    "sheet_baltic_birch_12mm":  68.00,
    "sheet_baltic_birch_9mm":   56.00,  # 3/8" B/BB (Baker Lumber, Jul 2026)
    "sheet_baltic_birch_6mm":   46.00,
    # Pre-finished (UV-coated both faces) — drawer box stock
    "sheet_baltic_birch_prefinished_18mm": 128.00,
    "sheet_baltic_birch_prefinished_15mm": 110.00,
    "sheet_baltic_birch_prefinished_12mm":  94.00,
    "sheet_baltic_birch_prefinished_9mm":   78.00,
    "sheet_baltic_birch_prefinished_6mm":   64.00,
    # Show-wood plywood
    "sheet_rift_white_oak_ply_18mm": 209.00,  # 3/4" A1 rift-sawn white oak (Charlie's supplier, Jul 2026)

    # ── Blum front locking devices — per piece (Charlie's supplier, Jul 2026) ──
    # One left + one right per slide pair; Tandem family shares T51.1901,
    # Movento uses T51.7601.
    "blum_t51_1901_l":  2.25,
    "blum_t51_1901_r":  2.25,
    "blum_t51_7601_li": 2.50,
    "blum_t51_7601_re": 2.50,
    # Runner mounting screws — per 100-pack (606N100)
    "blum_606n": 4.19,

    # ── Drawer slides — per pair ──────────────────────────────────────────────
    # Length-suffixed keys (model-length-NNNmm) reflect actual distributor
    # pricing (Woodworker Express, slides.html, May 2026). price_for() falls
    # back to the bare model key if no length-specific entry is found.
    "blum_tandem_550h":             28.50,
    # Blum Tandem Plus 563H — frameless, 1/2"–5/8" sides (slides.html)
    "blum_tandem_plus_563h":             22.00,  # representative; ~$21–$27 by length
    "blum_tandem_plus_563h-229mm":       27.00,  # 9"
    "blum_tandem_plus_563h-305mm":       22.26,  # 12"
    "blum_tandem_plus_563h-381mm":       22.26,  # 15"
    "blum_tandem_plus_563h-457mm":       21.33,  # 18"
    "blum_tandem_plus_563h-533mm":       21.99,  # 21"
    # Blum Tandem Plus 563F — face-frame variant (slides.html)
    "blum_tandem_plus_563f":             24.00,  # representative; ~$23–$28 by length
    "blum_tandem_plus_563f-229mm":       28.27,  # 9"
    "blum_tandem_plus_563f-305mm":       23.75,  # 12"
    "blum_tandem_plus_563f-381mm":       23.75,  # 15"
    "blum_tandem_plus_563f-457mm":       24.11,  # 18"
    "blum_tandem_plus_563f-533mm":       24.81,  # 21"
    "blum_movento_760h":            36.00,
    "blum_movento_769":             58.00,
    "accuride_3832":                18.00,  # per single slide (sold_as_pair=False)
    "salice_futura":                32.00,
    "salice_futura_smove":          48.00,
    "salice_progressa_plus":        38.00,
    "salice_progressa_plus_smove":  54.00,

    # ── Hinges — each ─────────────────────────────────────────────────────────
    # Catalog-key form only. Part-number SKUs (the form the hardware-BOM
    # helpers actually emit) are mirrored programmatically below — see the
    # loop after this dict — so a spec's part_number can never drift out of
    # sync with a hand-maintained alias.
    "blum_clip_top_110_full":             9.50,
    "blum_clip_top_blumotion_110_full":  14.00,
    "blum_clip_top_110_half":             9.50,
    "blum_clip_top_blumotion_110_half":  14.00,
    "blum_clip_top_110_inset":            9.50,
    "blum_clip_top_blumotion_110_inset": 14.00,
    "blum_clip_top_170_full":            12.00,
    "blum_clip_top_110":                  9.50,
    "blum_clip_top_170":                 12.00,
    # CLIP 0mm wing mounting plate, one per hinge — sold separately from
    # the cup/arm; ships with pre-mounted 5 mm Euro system screws.
    # (rokhardware.com single-qty price, Jul 2026)
    "blum_173l8100":                      0.91,

    # ── Legs — each ───────────────────────────────────────────────────────────
    "richelieu_176138106":      18.00,
    "richelieu_17613b106":      18.00,
    "richelieu_adjustable_40mm": 8.00,
    "hairpin_152mm":            22.00,
    "hairpin_200mm":            25.00,

    # ── Pulls — each ─────────────────────────────────────────────────────────
    "topknobs-hb-76":    9.00,
    "topknobs-hb-96":   10.00,
    "topknobs-hb-128":  12.00,
    "topknobs-hb-160":  14.00,
    "topknobs-hb-305":  22.00,
    "topknobs-ag-76":   10.00,
    "topknobs-ag-96":   11.00,
    "topknobs-ag-128":  13.00,
    "topknobs-ag-160":  15.00,
    "topknobs-ag-305":  24.00,
    "topknobs-blk-76":  11.00,
    "topknobs-blk-96":  12.00,
    "topknobs-blk-128": 14.00,
    "topknobs-blk-160": 16.00,
    "topknobs-blk-305": 26.00,
    "topknobs-bsn-76":   9.00,
    "topknobs-bsn-96":  10.00,
    "topknobs-bsn-128": 12.00,
    "topknobs-bsn-160": 14.00,
    "topknobs-bsn-305": 22.00,
    "rockler-wnl-160":  18.00,
    "rockler-wnl-224":  22.00,
    "rockler-wnl-288":  26.00,
    "rockler-okl-160":  15.00,
    "rockler-okl-224":  18.00,
    "rockler-okl-288":  22.00,
    "richelieu-chbrz-32":   4.00,
    "richelieu-chbrz-96":   8.00,
    "richelieu-chbrz-128": 10.00,
    "richelieu-chbrz-416": 20.00,
    "richelieu-900-32":     4.00,
    "richelieu-900-96":     8.00,
    "richelieu-900-128":   10.00,
    "richelieu-900-416":   18.00,
    "richelieu-30-32":      3.00,
    "richelieu-30-96":      6.00,
    "richelieu-30-128":     8.00,
    "richelieu-30-416":    16.00,
    "hafele-193.18.766":   12.00,
    "hafele-151.35.665":    8.00,
    "rockler-42250":        6.00,
    # IKEA — sold in 2-packs (pack_quantity=2 in the catalog); prices below are
    # per 2-pack, matching the per-pack basis the BOM math uses.
    "ikea-bagganas-black-128":   5.00,
    "ikea-hackas-anthracite-128": 5.00,
    "ikea-borghamn-black-416":   8.00,
    "ikea-billsbro-white-120":   5.00,

    # ── Joinery consumables — per pack ────────────────────────────────────────
    "festool-493298":         129.00,   # Domino 8×40, 780-piece bulk pack
    "festool-494938":          25.00,   # Domino 5×30, 300-piece pack (US Tool & Fastener / Taco Tools, Jul 2026)
    # Iron-on pre-glued edge banding, 7/8" × 50' rolls (veneersupplies.com, Jul 2026)
    "edgeband-hotmelt-white_oak":   14.80,
    "edgeband-hotmelt-white_birch": 15.50,
    "kreg-sml-c32-100":        12.00,   # pocket screws 1-1/4", 100-pack
    "kreg-sml-c38-100":        12.00,
    "kreg-sml-c45-100":        12.00,
    "biscuit-10-100pk":         8.00,
    "dowel-8x30-50pk":          6.00,
    "screw-8x32-panhead-100pk": 8.00,   # false-front screws
}


# Mirror catalog-key prices onto manufacturer part numbers. The hardware-BOM
# helpers use ``spec.part_number`` as the HardwareLine SKU (falling back to
# the catalog key only when part_number is empty), so price_for() must
# resolve both forms. Deriving the mapping here — instead of hand-listing
# part-number aliases — keeps it correct when a spec's part_number changes.
for _catalog_key, _spec in {**HINGES, **LEGS}.items():
    _pn = getattr(_spec, "part_number", "")
    if _pn and _catalog_key in PRICE_LIST:
        PRICE_LIST.setdefault(_pn, PRICE_LIST[_catalog_key])
del _catalog_key, _spec, _pn


def price_for(key: str) -> float:
    """Return the list price for a hardware/sheet key, or 0.0 if not listed.

    Hardware-line SKUs from the cutlist module are often length-suffixed
    (``blum_tandem_550h-457mm``). Look up the exact key first; if missing,
    strip a trailing ``-NNNmm`` segment and try the base model.
    """
    if key in PRICE_LIST:
        return PRICE_LIST[key]
    # Fall back to the base model (everything before a trailing `-NNNmm`).
    import re as _re
    base = _re.sub(r"-\d+mm$", "", key)
    return PRICE_LIST.get(base, 0.0)
