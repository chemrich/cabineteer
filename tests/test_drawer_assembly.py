"""Drawer-box assembly correctness.

For every DrawerJoineryStyle and a sweep of opening sizes, verifies:

  1. Assembled bbox = cfg.box_width × box_depth × box_height.
  2. Side clearance to the cabinet opening = cfg.side_gap — the AIR beside
     the box, which is the slide's nominal clearance only for a side-mount
     slide. An undermount quotes its clearance to the drawer's inside face,
     so the air gap is that minus one side thickness.
  3. No material interference between any pair of wall panels.
  4. The bottom panel sits in each wall's dado at the expected engagement
     volume (intersection with the wall's *uncut envelope*, not the cut wall —
     the dado removes material exactly where the bottom sits).
  5. The bottom panel is contained within the carcass exterior.
  6. For non-BUTT styles, the sub-front actually engages the side rabbet
     (regression check for the 'decorative joinery' bug).
"""

import importlib.util

import pytest


cq_missing = importlib.util.find_spec("cadquery") is None
skipif_no_cq = pytest.mark.skipif(cq_missing, reason="cadquery not installed")

if not cq_missing:
    import cadquery as cq

from cabineteer.drawer import (
    drawer_part_offsets,
    DrawerConfig,
    make_drawer_bottom,
    make_drawer_front_back,
    make_drawer_side,
)
from cabineteer.joinery import DrawerJoineryStyle


OPENING_SIZES = [
    (600, 200, 500),
    (900, 305, 600),
    (400, 125, 400),
]


def _placed(panel_wp, x, y, z):
    return panel_wp.val().translate(cq.Vector(x, y, z))


def _envelope(x0, y0, z0, dx, dy, dz):
    return (cq.Workplane("XY").transformed(offset=(x0, y0, z0))
            .box(dx, dy, dz, centered=False).val())


def _vol(shape):
    try:
        return shape.Volume()
    except Exception:
        return float("nan")


def _intersect_vol(a, b):
    try:
        return _vol(a.intersect(b))
    except Exception:
        return float("nan")


def _build_placed(cfg):
    """Return (LS, RS, SF, BK, BT) at their world-coordinate positions inside
    a cabinet opening with x_offset = cfg.side_gap.

    Placement comes from ``drawer_part_offsets`` — the same rule the assembly
    uses. Re-deriving it here would let the probe agree with a stale idea of
    the joint instead of with the box that gets built.
    """
    x0 = cfg.side_gap
    off = drawer_part_offsets(cfg)

    def at(shape, key):
        x, y, z = off[key]
        return _placed(shape, x0 + x, y, z)

    ls = at(make_drawer_side(cfg, side="left"), "side_L")
    rs = at(make_drawer_side(cfg, side="right"), "side_R")
    sf = at(make_drawer_front_back(cfg, position="front"), "sub_front")
    bk = at(make_drawer_front_back(cfg, position="back"), "back")
    bt = at(make_drawer_bottom(cfg), "bottom")
    return ls, rs, sf, bk, bt


@pytest.fixture(params=[s for s in DrawerJoineryStyle], ids=lambda s: s.value)
def style(request):
    return request.param


@pytest.fixture(params=OPENING_SIZES, ids=lambda s: f"{s[0]}x{s[1]}x{s[2]}")
def opening(request):
    return request.param


@pytest.fixture
def cfg(style, opening):
    w, h, d = opening
    return DrawerConfig(
        opening_width=w, opening_height=h, opening_depth=d,
        joinery_style=style,
    )


@skipif_no_cq
class TestAssembledSize:
    def test_bbox_matches_box_dims(self, cfg):
        ls, rs, sf, bk, _ = _build_placed(cfg)
        union = ls.fuse(rs).fuse(sf).fuse(bk)
        bb = union.BoundingBox()
        assert bb.xlen == pytest.approx(cfg.box_width, abs=0.5)
        assert bb.ylen == pytest.approx(cfg.box_depth, abs=0.5)
        assert bb.zlen == pytest.approx(cfg.box_height, abs=0.5)


@skipif_no_cq
class TestCabinetClearance:
    def test_left_and_right_clearance(self, cfg):
        ls, rs, sf, bk, _ = _build_placed(cfg)
        union = ls.fuse(rs).fuse(sf).fuse(bk)
        bb = union.BoundingBox()
        opening_w = cfg.opening_width
        # Air beside the box, not the slide's quoted clearance: these are
        # undermount slides, whose 21 mm reaches the drawer's INSIDE face.
        gap = cfg.side_gap
        assert bb.xmin == pytest.approx(gap, abs=0.5)
        assert opening_w - bb.xmax == pytest.approx(gap, abs=0.5)

    def test_inside_width_matches_the_slide_rule(self, cfg):
        """The box's INSIDE width is what an undermount runner constrains.

        Measured off the built walls rather than read back off the config,
        so this fails if the geometry and the spec ever drift apart.
        """
        ls, rs, _sf, _bk, _ = _build_placed(cfg)
        inside = rs.BoundingBox().xmin - ls.BoundingBox().xmax
        slide = cfg.slide
        expected = cfg.opening_width - 2 * slide.nominal_side_clearance
        assert inside == pytest.approx(expected, abs=0.5)


@skipif_no_cq
class TestNoWallInterference:
    @pytest.mark.parametrize("pair", ["LS-SF", "LS-BK", "RS-SF", "RS-BK",
                                      "LS-RS", "SF-BK"])
    def test_pair_no_overlap(self, cfg, pair):
        ls, rs, sf, bk, _ = _build_placed(cfg)
        lookup = {"LS": ls, "RS": rs, "SF": sf, "BK": bk}
        a, b = pair.split("-")
        v = _intersect_vol(lookup[a], lookup[b])
        assert v == pytest.approx(0.0, abs=0.5), \
            f"{pair} interference {v:.1f} mm³ for joinery={cfg.joinery_style.value}"


@skipif_no_cq
class TestBottomDadoEngagement:
    """The bottom panel intersected with each wall's pre-cut *envelope* (not the
    cut wall — the dado removes wall material where the bottom sits) should
    equal the theoretical engagement volume = dado_depth × span × bt_thickness."""

    def test_engages_all_four_walls(self, cfg):
        x0 = cfg.side_gap  # air beside the box (see _build_placed)
        t_s = cfg.side_thickness
        t_fb = cfg.front_back_thickness
        bd = cfg.box_depth
        bw = cfg.box_width
        bh = cfg.box_height
        bdd = cfg.bottom_dado_depth
        bt_thk = cfg.bottom_thickness
        engagement_x = cfg.joinery.engagement_x
        interior_w = bw - 2 * (t_s - engagement_x)

        _, _, _, _, bt = _build_placed(cfg)

        ls_env = _envelope(x0, 0, 0, t_s, bd, bh)
        rs_env = _envelope(x0 + bw - t_s, 0, 0, t_s, bd, bh)
        sf_env = _envelope(x0 + t_s - engagement_x, 0, 0, interior_w, t_fb, bh)
        bk_env = _envelope(x0 + t_s - engagement_x, bd - t_fb, 0,
                           interior_w, t_fb, bh)

        # Sides: bottom span = bottom_panel_depth (front/back take the rest).
        bp_d = cfg.bottom_panel_depth
        # Front/back span: bottom is bottom_panel_width wide; the f/b dado
        # spans interior_w. The narrower of the two bounds engagement.
        bp_w = cfg.bottom_panel_width
        fb_span = min(bp_w, interior_w)

        exp_lr = bdd * bp_d * bt_thk
        exp_fb = bdd * fb_span * bt_thk

        assert _intersect_vol(bt, ls_env) == pytest.approx(exp_lr, abs=5.0)
        assert _intersect_vol(bt, rs_env) == pytest.approx(exp_lr, abs=5.0)
        assert _intersect_vol(bt, sf_env) == pytest.approx(exp_fb, abs=5.0)
        assert _intersect_vol(bt, bk_env) == pytest.approx(exp_fb, abs=5.0)


@skipif_no_cq
class TestBottomContainedInCarcass:
    def test_no_overhang(self, cfg):
        x0 = cfg.side_gap  # air beside the box (see _build_placed)
        bw = cfg.box_width
        bd = cfg.box_depth
        _, _, _, _, bt = _build_placed(cfg)
        bb = bt.BoundingBox()
        # Bottom should sit inside the carcass exterior on all four edges.
        assert bb.xmin >= x0 - 0.1
        assert bb.xmax <= x0 + bw + 0.1
        assert bb.ymin >= -0.1
        assert bb.ymax <= bd + 0.1


@skipif_no_cq
class TestJointEngagement:
    """For non-BUTT styles the sub-front material must occupy the side
    panel's front-end rabbet — proves the joint actually engages."""

    def test_sub_front_fills_left_side_rabbet(self, cfg):
        if cfg.joinery_style == DrawerJoineryStyle.BUTT:
            pytest.skip("BUTT has no rabbet to fill")
        if cfg.joinery_style == DrawerJoineryStyle.QQQ:
            pytest.skip("QQQ uses a shallow Y rabbet — see TestQQQGeometry")
        if cfg.joinery.laps_front:
            pytest.skip("front-lapping inverts this — see TestFrontLapEngagement")
        x0 = cfg.side_gap  # air beside the box (see _build_placed)
        t_s = cfg.side_thickness
        t_fb = cfg.front_back_thickness
        bh = cfg.box_height
        engagement_x = cfg.joinery.engagement_x

        # The left-side rabbet zone in world coords:
        rabbet = _envelope(x0 + t_s - engagement_x, 0, 0,
                           engagement_x, t_fb, bh)

        _, _, sf, _, _ = _build_placed(cfg)
        # The sub-front fills the rabbet (less the bottom dado slot in z).
        # Expected fill: rabbet volume minus the slot the bottom dado removes.
        bdd = cfg.bottom_dado_depth
        bt_thk = cfg.bottom_thickness
        rabbet_vol = engagement_x * t_fb * bh
        # The sub-front bottom dado intersects the rabbet zone too:
        # in panel-local (sub-front placed at fb_x = x0 + t_s - engagement_x),
        # the rabbet's panel-local X is 0..engagement_x. The dado runs the full
        # interior_width in X (which includes 0..engagement_x). In Y the dado
        # at the inside face is t_fb-bdd..t_fb. In Z it's dz..dz+bt_thk.
        dado_in_rabbet = engagement_x * bdd * bt_thk
        expected = rabbet_vol - dado_in_rabbet

        actual = _intersect_vol(sf, rabbet)
        assert actual == pytest.approx(expected, abs=5.0), (
            f"joint engagement {actual:.1f} mm³ vs expected {expected:.1f} mm³ "
            f"for {cfg.joinery_style.value}"
        )


@skipif_no_cq
class TestFrontLapEngagement:
    """Front-lapping (drawer lock) inverts the corner: the SIDE's end is
    swallowed by a socket in the front, and the wall left outboard of that
    socket — the lip — is what wraps the side and makes the box longer than
    its sides. Both claims are checked in solid volume, because both were
    wrong on paper until 2026-08."""

    def test_side_end_fills_the_front_socket(self, cfg):
        if not cfg.joinery.laps_front:
            pytest.skip("side-lapping — see TestJointEngagement")
        x0 = cfg.side_gap  # air beside the box (see _build_placed)
        t_s = cfg.side_thickness
        bh = cfg.box_height
        depth = cfg.joinery.socket_depth
        lip = cfg.joinery.lip

        socket = _envelope(x0, lip, 0, t_s, depth, bh)
        ls, _, _, _, _ = _build_placed(cfg)

        # The side's own bottom groove takes a bite out of the buried end.
        expected = (t_s * depth * bh
                    - cfg.bottom_dado_depth * depth * cfg.bottom_thickness)
        actual = _intersect_vol(ls, socket)
        assert actual == pytest.approx(expected, abs=5.0), (
            f"side buried {actual:.1f} mm³ into the socket, expected "
            f"{expected:.1f} mm³")

    def test_lip_covers_the_side_end(self, cfg):
        if not cfg.joinery.laps_front:
            pytest.skip("side-lapping has no lip")
        x0 = cfg.side_gap  # air beside the box (see _build_placed)
        t_s = cfg.side_thickness
        bh = cfg.box_height
        lip = cfg.joinery.lip

        # The outermost slice of the box at the front, over the side's own
        # width, must be solid FRONT material: that is what the lip is.
        zone = _envelope(x0, 0, 0, t_s, lip, bh)
        _, _, sf, _, _ = _build_placed(cfg)
        assert _intersect_vol(sf, zone) == pytest.approx(t_s * lip * bh, abs=5.0)

    def test_no_side_material_outboard_of_the_lip(self, cfg):
        if not cfg.joinery.laps_front:
            pytest.skip("side-lapping has no lip")
        x0 = cfg.side_gap  # air beside the box (see _build_placed)
        zone = _envelope(x0, 0, 0, cfg.side_thickness,
                         cfg.joinery.lip, cfg.box_height)
        ls, _, _, _, _ = _build_placed(cfg)
        assert _intersect_vol(ls, zone) == pytest.approx(0.0, abs=1.0)


@skipif_no_cq
class TestQQQGeometry:
    """QQQ has a hidden tongue-in-pocket joint: the side panel keeps a
    full-thickness lip at each end (Y `0…t_s/2`), and the dado that receives
    the front piece's inside-face tongue is set in by t_s/2 from the end.
    From outside the box the side wraps the corner and hides the joint.
    """

    @pytest.fixture
    def qqq_cfg(self, opening):
        w, h, d = opening
        return DrawerConfig(
            opening_width=w, opening_height=h, opening_depth=d,
            joinery_style=DrawerJoineryStyle.QQQ,
        )

    @pytest.mark.parametrize("end", ["front", "back"])
    def test_side_lip_intact(self, qqq_cfg, end):
        """The very-end strip of the side panel (Y `0…t_s/2`) is full
        thickness — both inner and outer face material survive, forming the
        lip that wraps the corner from outside.
        """
        ls, _, _, _, _ = _build_placed(qqq_cfg)
        x0 = qqq_cfg.side_gap  # air beside the box (see _build_placed)
        t_s = qqq_cfg.side_thickness
        bh = qqq_cfg.box_height
        bd = qqq_cfg.box_depth
        bdd = qqq_cfg.bottom_dado_depth
        bt_thk = qqq_cfg.bottom_thickness

        y0 = 0.0 if end == "front" else bd - t_s / 2
        lip_env = _envelope(x0, y0, 0, t_s, t_s / 2, bh)
        actual = _intersect_vol(ls, lip_env)
        # The bottom dado on the side runs full Y and spans panel-local
        # X = t_s − bdd … t_s.  It intersects the lip envelope across the
        # full t_s/2 of Y, removing a bdd × t_s/2 × bt_thk slot.
        dado_in_lip = bdd * (t_s / 2) * bt_thk
        expected = t_s * (t_s / 2) * bh - dado_in_lip
        assert actual == pytest.approx(expected, abs=10.0)

    @pytest.mark.parametrize("end", ["front", "back"])
    def test_side_dado_pocket_removed(self, qqq_cfg, end):
        """The set-in dado pocket (panel-local X `t_s/2…t_s`, Y `t_s/2…t_s`
        at the front; Y `bd−t_s…bd−t_s/2` at the back) is fully cut out."""
        ls, _, _, _, _ = _build_placed(qqq_cfg)
        x0 = qqq_cfg.side_gap  # air beside the box (see _build_placed)
        t_s = qqq_cfg.side_thickness
        bh = qqq_cfg.box_height
        bd = qqq_cfg.box_depth

        y0 = t_s / 2 if end == "front" else bd - t_s
        dado_env = _envelope(x0 + t_s / 2, y0, 0, t_s / 2, t_s / 2, bh)
        assert _intersect_vol(ls, dado_env) == pytest.approx(0.0, abs=1.0)

    def test_sub_front_outer_rabbet_removed(self, qqq_cfg):
        """The outer-face rabbet at each end of the sub-front (panel-local
        X `0…t_s/2`, Y `0…t_fb − t_s/2`) is fully cut out."""
        _, _, sf, _, _ = _build_placed(qqq_cfg)
        x0 = qqq_cfg.side_gap  # air beside the box (see _build_placed)
        t_s = qqq_cfg.side_thickness
        t_fb = qqq_cfg.front_back_thickness
        bh = qqq_cfg.box_height
        # Sub-front placed at world X = x0 + t_s/2; left rabbet zone at
        # world X `x0 + t_s/2 … x0 + t_s`, Y `0 … t_fb − t_s/2`.
        rabbet_env = _envelope(
            x0 + t_s / 2, 0, 0,
            t_s / 2, t_fb - t_s / 2, bh,
        )
        assert _intersect_vol(sf, rabbet_env) == pytest.approx(0.0, abs=1.0)

    def test_sub_front_tongue_fills_side_dado_pocket(self, qqq_cfg):
        """The sub-front's inside-face tongue at the left end fills the side's
        set-in dado pocket: zone = world (X `t_s/2…t_s`, Y `t_s/2…t_s`)."""
        _, _, sf, _, _ = _build_placed(qqq_cfg)
        x0 = qqq_cfg.side_gap  # air beside the box (see _build_placed)
        t_s = qqq_cfg.side_thickness
        t_fb = qqq_cfg.front_back_thickness
        bh = qqq_cfg.box_height
        bdd = qqq_cfg.bottom_dado_depth
        bt_thk = qqq_cfg.bottom_thickness

        # For QQQ Phipps' recipe t_fb = t_s; the tongue sits at world Y
        # `t_fb − t_s/2 … t_fb`.  Build the dado pocket envelope at the
        # matching world coordinates.
        zone = _envelope(x0 + t_s / 2, t_s / 2, 0, t_s / 2, t_s / 2, bh)
        actual = _intersect_vol(sf, zone)
        # The sub-front's bottom dado at panel-local Y `t_fb − bdd … t_fb`
        # overlaps the tongue zone (which is at panel-local Y `t_fb − t_s/2 …
        # t_fb`).  Compute the overlap volume removed from the tongue.
        y_overlap = min(t_fb, t_fb) - max(t_fb - bdd, t_fb - t_s / 2)
        y_overlap = max(0.0, y_overlap)
        dado_in_tongue = (t_s / 2) * y_overlap * bt_thk
        expected = (t_s / 2) * (t_s / 2) * bh - dado_in_tongue
        assert actual == pytest.approx(expected, abs=15.0)

    def test_outside_corner_owned_by_side(self, qqq_cfg):
        """The exterior corner zone (X `0…t_s`, Y `0…t_s/2`) is owned by the
        side panel alone — the sub-front does not appear at the outside corner
        because its outer-face rabbet has removed material there."""
        ls, _, sf, _, _ = _build_placed(qqq_cfg)
        x0 = qqq_cfg.side_gap  # air beside the box (see _build_placed)
        t_s = qqq_cfg.side_thickness
        bh = qqq_cfg.box_height
        corner = _envelope(x0, 0, 0, t_s, t_s / 2, bh)
        # Sub-front contributes no material to the outside corner.
        assert _intersect_vol(sf, corner) == pytest.approx(0.0, abs=1.0)
        # Side fills the corner (less the bottom-dado slot through the lip).
        bdd = qqq_cfg.bottom_dado_depth
        bt_thk = qqq_cfg.bottom_thickness
        dado_in_lip = bdd * (t_s / 2) * bt_thk
        expected = t_s * (t_s / 2) * bh - dado_in_lip
        assert _intersect_vol(ls, corner) == pytest.approx(expected, abs=15.0)
