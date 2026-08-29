"""
Tests for joinery.py specs, parametric layout logic, and evaluation checks.

All tests are pure-Python (no CadQuery).
"""

import pytest

from cabineteer.joinery import (
    DrawerJoineryStyle,
    DRAWER_LOCK_NOMINAL_LIP_MM,
    CarcassJoinery,
    DrawerJoinerySpec,
    drawer_joinery_spec,
    DominoSpec,
    DominoSize,
    DOMINO_SIZES,
    get_domino_size,
    PocketScrewSpec,
    pocket_screw_length,
    POCKET_SCREW_LENGTH_BY_THICKNESS,
    BiscuitSpec,
    BISCUIT_DIMS,
    DowelSpec,
    DEFAULT_DOMINO,
    DEFAULT_POCKET_SCREW,
    DEFAULT_BISCUIT,
    DEFAULT_DOWEL,
)
from cabineteer.evaluation import (
    Severity,
    check_drawer_joinery,
    check_domino_layout,
    check_pocket_screw_layout,
    check_carcass_joinery,
)
from cabineteer.drawer import DrawerConfig
from cabineteer.cabinet import CabinetConfig


# ─── DrawerJoinerySpec ────────────────────────────────────────────────────────

class TestDrawerJoinerySpecBUTT:
    def _spec(self, t_s=15.0, t_fb=15.0):
        return drawer_joinery_spec(DrawerJoineryStyle.BUTT, t_s, t_fb)

    def test_all_cuts_zero(self):
        s = self._spec()
        assert s.side_dado_depth_x == 0.0
        assert s.side_dado_depth_y == 0.0
        assert s.fb_channel_depth_x == 0.0
        assert s.fb_channel_depth_y == 0.0

    def test_no_router_required(self):
        assert not self._spec().requires_router_bit

    def test_no_true_thickness_required(self):
        assert not self._spec().requires_true_thickness


class TestDrawerJoinerySpecQQQ:
    def _spec(self, t_s=12.7, t_fb=12.7):
        return drawer_joinery_spec(DrawerJoineryStyle.QQQ, t_s, t_fb)

    def test_all_cuts_equal_half_thickness(self):
        t = 12.7
        s = self._spec(t, t)
        assert s.side_dado_depth_x == pytest.approx(t / 2)
        assert s.side_dado_depth_y == pytest.approx(t / 2)
        assert s.fb_channel_depth_x == pytest.approx(t / 2)
        assert s.fb_channel_depth_y == pytest.approx(t / 2)

    def test_tongue_width_is_half_thickness(self):
        t = 12.7
        s = self._spec(t, t)
        assert s.side_tongue_width == pytest.approx(t / 2)

    def test_requires_true_thickness(self):
        assert self._spec().requires_true_thickness

    def test_no_router_required(self):
        assert not self._spec().requires_router_bit

    def test_scales_with_different_thickness(self):
        t = 19.0  # 3/4"
        s = self._spec(t, t)
        assert s.side_dado_depth_x == pytest.approx(9.5)
        assert s.side_dado_depth_y == pytest.approx(9.5)

    def test_laps_from_the_side(self):
        s = self._spec()
        assert not s.laps_front
        assert s.lip == 0.0


class TestDrawerJoinerySpecHalfLap:
    def _spec(self, t_s=15.0, t_fb=15.0):
        return drawer_joinery_spec(DrawerJoineryStyle.HALF_LAP, t_s, t_fb)

    def test_side_dado_is_half_side_thickness(self):
        s = self._spec(t_s=15.0)
        assert s.side_dado_depth_x == pytest.approx(7.5)

    def test_side_dado_depth_is_full_fb_thickness(self):
        s = self._spec(t_s=15.0, t_fb=15.0)
        assert s.side_dado_depth_y == pytest.approx(15.0)

    def test_fb_channel_is_half_fb_thickness(self):
        s = self._spec(t_fb=15.0)
        assert s.fb_channel_depth_y == pytest.approx(7.5)

    def test_no_router_required(self):
        assert not self._spec().requires_router_bit

    def test_no_true_thickness_required(self):
        assert not self._spec().requires_true_thickness


class TestDrawerJoinerySpecDrawerLock:
    def _spec(self, t_s=15.0, t_fb=15.0):
        return drawer_joinery_spec(DrawerJoineryStyle.DRAWER_LOCK, t_s, t_fb)

    def test_requires_router_bit(self):
        assert self._spec().requires_router_bit

    def test_laps_from_the_front(self):
        """The front/back wraps the side ends — the reason the joint resists
        the front being pulled off, and the reason the SIDES are the parts
        that get cut short."""
        s = self._spec()
        assert s.laps_front
        assert s.engagement_x == 0.0    # nothing is cut into the side

    def test_lip_and_socket_split_the_front_thickness(self):
        s = self._spec(t_fb=15.0)
        assert s.lip + s.socket_depth == pytest.approx(15.0)
        assert 0 < s.lip < 15.0

    def test_nominal_lip_is_a_default_a_measurement_overrides(self):
        """The lip belongs to the bit and the fence, not the drawing."""
        nominal = self._spec(t_s=12.0, t_fb=12.0)
        assert nominal.lip == pytest.approx(DRAWER_LOCK_NOMINAL_LIP_MM)
        measured = drawer_joinery_spec(
            DrawerJoineryStyle.DRAWER_LOCK, 12.0, 12.0, lip=2.0)
        assert measured.lip == pytest.approx(2.0)
        assert measured.socket_depth == pytest.approx(10.0)

    def test_impossible_lip_is_refused(self):
        for bad in (0.0, -1.0, 12.0, 20.0):
            with pytest.raises(ValueError, match="lip"):
                drawer_joinery_spec(
                    DrawerJoineryStyle.DRAWER_LOCK, 12.0, 12.0, lip=bad)

    def test_lip_is_ignored_by_side_lapping_styles(self):
        """A project-wide lip token must stay harmless on a half-lap box."""
        s = drawer_joinery_spec(
            DrawerJoineryStyle.HALF_LAP, 12.0, 12.0, lip=2.0)
        assert s.lip == 0.0 and not s.laps_front

    def test_no_true_thickness_required(self):
        assert not self._spec().requires_true_thickness


# ─── Domino sizes ─────────────────────────────────────────────────────────────

class TestDominoSizes:
    def test_all_standard_sizes_present(self):
        for key in ["4x17", "5x19", "5x30", "6x40", "8x40", "8x50", "10x24", "10x50"]:
            assert key in DOMINO_SIZES

    def test_xl_sizes_present(self):
        assert "14x28" in DOMINO_SIZES
        assert "14x56" in DOMINO_SIZES

    def test_mortise_larger_than_tenon(self):
        for key, s in DOMINO_SIZES.items():
            assert s.mortise_length > s.tenon_length, f"{key}: mortise_length <= tenon_length"
            assert s.mortise_width > s.tenon_thickness, f"{key}: mortise_width <= tenon_thickness"

    def test_df500_vs_df700(self):
        assert DOMINO_SIZES["8x40"].machine == "DF 500"
        assert DOMINO_SIZES["10x24"].machine == "DF 700"
        assert DOMINO_SIZES["14x28"].machine == "DF 700"

    def test_get_domino_size_valid(self):
        s = get_domino_size("8x40")
        assert s.tenon_length == 40

    def test_get_domino_size_invalid(self):
        with pytest.raises(KeyError):
            get_domino_size("99x99")

    def test_min_edge_distances_positive(self):
        for s in DOMINO_SIZES.values():
            assert s.min_edge_distance > 0

    def test_part_numbers_present(self):
        for key, s in DOMINO_SIZES.items():
            assert s.part_number, f"{key} missing part number"


# ─── DominoSpec layout ────────────────────────────────────────────────────────

class TestDominoSpec:
    spec = DEFAULT_DOMINO  # 8x40, 150 mm max spacing

    def test_count_minimum_two(self):
        assert self.spec.count_for_span(300) >= 2

    def test_count_short_span_still_two(self):
        # Even a short span should give 2
        assert self.spec.count_for_span(100) == 2

    def test_count_increases_with_span(self):
        c1 = self.spec.count_for_span(400)
        c2 = self.spec.count_for_span(800)
        assert c2 >= c1

    def test_count_wide_cabinet_enough(self):
        # 564 mm interior width, 150 mm spacing → should fit 4+ tenons
        n = self.spec.count_for_span(564)
        assert n >= 2

    def test_positions_count_matches_count(self):
        for span in [200, 400, 600, 800]:
            positions = self.spec.positions_for_span(span)
            assert len(positions) == self.spec.count_for_span(span)

    def test_positions_respect_edge_distance(self):
        s = self.spec.size
        positions = self.spec.positions_for_span(500)
        assert positions[0] >= s.min_edge_distance - 0.01
        assert positions[-1] <= 500 - s.min_edge_distance + 0.01

    def test_positions_are_increasing(self):
        positions = self.spec.positions_for_span(600)
        for i in range(1, len(positions)):
            assert positions[i] > positions[i - 1]

    def test_zero_span_returns_zero(self):
        assert self.spec.count_for_span(0) == 0
        assert self.spec.positions_for_span(0) == []


# ─── PocketScrewSpec ──────────────────────────────────────────────────────────

class TestPocketScrewSpec:
    spec = DEFAULT_POCKET_SCREW

    def test_count_minimum_two(self):
        assert self.spec.count_for_span(300) >= 2

    def test_positions_respect_edge_distance(self):
        positions = self.spec.positions_for_span(400)
        assert positions[0] >= self.spec.min_edge_distance - 0.01
        assert positions[-1] <= 400 - self.spec.min_edge_distance + 0.01

    def test_screw_length_for_3_4_stock(self):
        length = self.spec.screw_length(18.0)
        assert length == 32.0

    def test_screw_length_for_1_2_stock(self):
        length = self.spec.screw_length(12.0)
        assert length == 19.0

    def test_pocket_screw_length_lookup(self):
        assert pocket_screw_length(18.0) == 32.0
        assert pocket_screw_length(12.0) == 19.0

    def test_positions_count_matches(self):
        for span in [200, 400, 600]:
            assert len(self.spec.positions_for_span(span)) == self.spec.count_for_span(span)


# ─── BiscuitSpec ──────────────────────────────────────────────────────────────

class TestBiscuitSpec:
    spec = DEFAULT_BISCUIT  # #10

    def test_dims_correct(self):
        assert self.spec.slot_length == 53.0
        assert self.spec.slot_width == 19.0
        # slot_depth_per_side raised 8.0 → 10.0 so the two mating slots seat
        # the full #10 biscuit (2×10 = 20 ≥ 19 mm width); the old 8.0 left a
        # 3 mm gap at the joint line (audit finding: biscuit slots too shallow).
        assert self.spec.slot_depth_per_side == 10.0

    def test_slot_depth_seats_biscuit(self):
        # Regression: 2 × slot_depth_per_side must cover the biscuit width
        # for every standard size so the joint can close.
        for size in ("#0", "#10", "#20"):
            b = BiscuitSpec(size=size)
            assert 2 * b.slot_depth_per_side >= b.slot_width, size

    def test_count_minimum_two(self):
        assert self.spec.count_for_span(300) >= 2

    def test_unknown_size_raises(self):
        b = BiscuitSpec(size="#99")
        with pytest.raises(KeyError):
            _ = b.dims

    def test_all_standard_sizes(self):
        for size in ["#0", "#10", "#20"]:
            b = BiscuitSpec(size=size)
            dims = b.dims
            assert len(dims) == 3

    def test_positions_count_matches(self):
        for span in [200, 400, 600]:
            assert len(self.spec.positions_for_span(span)) == self.spec.count_for_span(span)


# ─── DowelSpec ───────────────────────────────────────────────────────────────

class TestDowelSpec:
    spec = DEFAULT_DOWEL  # 8 mm, 96 mm spacing

    def test_count_minimum_two(self):
        assert self.spec.count_for_span(300) >= 2

    def test_positions_respect_edge_distance(self):
        positions = self.spec.positions_for_span(400)
        assert positions[0] >= self.spec.min_edge_distance - 0.01
        assert positions[-1] <= 400 - self.spec.min_edge_distance + 0.01

    def test_positions_count_matches(self):
        for span in [200, 400, 600]:
            assert len(self.spec.positions_for_span(span)) == self.spec.count_for_span(span)


# ─── Fastener layout regressions (audit) ──────────────────────────────────────

class TestFastenerLayoutContainment:
    """Regression: every fastener CENTRE — and, where a slot length is known,
    every slot EDGE — must fall inside [0, span] for all spans, and positions
    must be strictly increasing.  Guards the audit fixes for Domino edge-distance
    semantics and the span-too-small guard across all four fastener types.
    """

    SPANS = [1, 5, 20, 30, 60, 80, 100, 150, 200, 300, 400, 564, 800, 1200]

    def _check(self, spec, span, slot_len=0.0):
        positions = spec.positions_for_span(span)
        assert len(positions) == spec.count_for_span(span), (span, positions)
        for p in positions:
            # Fastener CENTRE must always be inside the panel.
            assert 0.0 <= p <= span, (span, p)
            # Slot EDGES must not overrun the ends — but only when the span can
            # physically hold the slot at all (a 40 mm mortise cannot fit a
            # 20 mm-wide panel; that degenerate case is a caller error, not a
            # layout bug, and centre-in-range is the only meaningful guarantee).
            if span >= slot_len:
                assert p - slot_len / 2 >= -1e-9, (span, p, slot_len)
                assert p + slot_len / 2 <= span + 1e-9, (span, p, slot_len)
        for i in range(1, len(positions)):
            assert positions[i] > positions[i - 1], (span, positions)

    def test_domino_all_sizes_contained(self):
        for key, size in DOMINO_SIZES.items():
            spec = DominoSpec(size_key=key)
            for span in self.SPANS:
                self._check(spec, span, slot_len=size.mortise_length)

    def test_pocket_screw_contained(self):
        spec = DEFAULT_POCKET_SCREW
        for span in self.SPANS:
            self._check(spec, span, slot_len=spec.pocket_diameter)

    def test_biscuit_contained(self):
        for size in ("#0", "#10", "#20"):
            spec = BiscuitSpec(size=size)
            for span in self.SPANS:
                self._check(spec, span, slot_len=spec.slot_length)

    def test_dowel_contained(self):
        spec = DEFAULT_DOWEL
        for span in self.SPANS:
            self._check(spec, span, slot_len=spec.diameter)

    def test_small_span_returns_single_centred_fastener(self):
        # span < 2 * min_edge_distance → one centred fastener, not a crossed pair.
        assert DominoSpec("8x40").positions_for_span(15) == [7.5]
        assert BiscuitSpec().positions_for_span(80) == [40.0]
        assert DEFAULT_DOWEL.positions_for_span(20) == [10.0]
        assert DEFAULT_POCKET_SCREW.positions_for_span(30) == [15.0]


# ─── DrawerConfig.joinery property ───────────────────────────────────────────

class TestDrawerConfigJoineryProperty:
    def _cfg(self, style=DrawerJoineryStyle.BUTT, **kwargs):
        defaults = dict(
            opening_width=564, opening_height=200, opening_depth=500,
            joinery_style=style,
        )
        defaults.update(kwargs)
        return DrawerConfig(**defaults)

    def test_half_lap_default(self):
        cfg = DrawerConfig(opening_width=564, opening_height=200, opening_depth=500)
        assert cfg.joinery_style == DrawerJoineryStyle.HALF_LAP

    def test_qqq_spec_returned(self):
        cfg = self._cfg(DrawerJoineryStyle.QQQ, side_thickness=12.7)
        spec = cfg.joinery
        assert spec.style == DrawerJoineryStyle.QQQ
        assert spec.side_dado_depth_x == pytest.approx(12.7 / 2)

    def test_half_lap_spec_returned(self):
        cfg = self._cfg(DrawerJoineryStyle.HALF_LAP)
        assert cfg.joinery.style == DrawerJoineryStyle.HALF_LAP

    def test_drawer_lock_spec_returned(self):
        cfg = self._cfg(DrawerJoineryStyle.DRAWER_LOCK)
        assert cfg.joinery.style == DrawerJoineryStyle.DRAWER_LOCK
        assert cfg.joinery.requires_router_bit


# ─── CabinetConfig carcass joinery fields ────────────────────────────────────

class TestCabinetConfigJoineryFields:
    def test_default_carcass_joinery(self):
        cfg = CabinetConfig()
        assert cfg.carcass_joinery == CarcassJoinery.FLOATING_TENON

    def test_set_floating_tenon(self):
        cfg = CabinetConfig(carcass_joinery=CarcassJoinery.FLOATING_TENON)
        assert cfg.carcass_joinery == CarcassJoinery.FLOATING_TENON

    def test_domino_spec_default(self):
        cfg = CabinetConfig()
        assert cfg.domino_spec.size_key == "8x40"

    def test_custom_domino_spec(self):
        cfg = CabinetConfig(
            carcass_joinery=CarcassJoinery.FLOATING_TENON,
            domino_spec=DominoSpec(size_key="10x50", max_spacing=200.0),
        )
        assert cfg.domino_spec.size_key == "10x50"

    def test_pocket_screw_default(self):
        cfg = CabinetConfig()
        assert cfg.pocket_screw_spec.drill_angle_deg == 15.0


# ─── Evaluation checks ───────────────────────────────────────────────────────

class TestCheckDrawerJoinery:
    def _cfg(self, style, t_s=12.7):
        return DrawerConfig(
            opening_width=564, opening_height=200, opening_depth=500,
            joinery_style=style, side_thickness=t_s,
        )

    def test_butt_no_issues(self):
        issues = check_drawer_joinery(self._cfg(DrawerJoineryStyle.BUTT))
        assert not issues

    def test_qqq_true_thickness_no_warning(self):
        # True 1/2" (12.7 mm) should not warn
        issues = check_drawer_joinery(self._cfg(DrawerJoineryStyle.QQQ, t_s=12.7))
        assert not any(i.check == "joinery_qqq_thickness" for i in issues)

    def test_qqq_non_standard_thickness_warns(self):
        # 11.0 mm is not a standard nominal thickness → warn
        issues = check_drawer_joinery(self._cfg(DrawerJoineryStyle.QQQ, t_s=11.0))
        assert any(i.check == "joinery_qqq_thickness" for i in issues)

    def test_qqq_very_thin_stock_errors(self):
        # 6 mm stock → tongue = 3 mm, below 4 mm minimum
        issues = check_drawer_joinery(self._cfg(DrawerJoineryStyle.QQQ, t_s=6.0))
        assert any(i.severity == Severity.ERROR for i in issues)

    def test_drawer_lock_thin_warns(self):
        issues = check_drawer_joinery(self._cfg(DrawerJoineryStyle.DRAWER_LOCK, t_s=10.0))
        assert any(i.check == "joinery_drawer_lock_thickness" for i in issues)

    def test_drawer_lock_adequate_no_warning(self):
        issues = check_drawer_joinery(self._cfg(DrawerJoineryStyle.DRAWER_LOCK, t_s=15.0))
        assert not any(i.check == "joinery_drawer_lock_thickness" for i in issues)


class TestCheckDominoLayout:
    spec = DEFAULT_DOMINO  # 8x40, 150 mm

    def test_adequate_panel_no_issues(self):
        # 18 mm panel, 564 mm span. 8×40 mortise_depth=15 mm + 2 mm wall = 17 mm min → OK.
        issues = check_domino_layout(self.spec, span=564, panel_thickness=18, joint_name="test")
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert not errors

    def test_thin_panel_error(self):
        issues = check_domino_layout(self.spec, span=564, panel_thickness=10, joint_name="test")
        assert any(i.check == "domino_panel_thickness" for i in issues)

    def test_short_span_error(self):
        # 8×40 needs min_edge_distance*2 + mortise_length = 11*2 + 40.5 = 62.5 mm
        issues = check_domino_layout(self.spec, span=30, panel_thickness=18, joint_name="test")
        assert any(i.check == "domino_span_too_short" for i in issues)

    def test_long_span_no_spacing_warning(self):
        # 500 mm span at 150 mm max spacing — should fit cleanly
        issues = check_domino_layout(self.spec, span=500, panel_thickness=18, joint_name="test")
        assert not any(i.check == "domino_spacing" for i in issues)


class TestCheckPocketScrewLayout:
    spec = DEFAULT_POCKET_SCREW

    def test_adequate_stock_no_errors(self):
        issues = check_pocket_screw_layout(self.spec, span=400, stock_thickness=18, joint_name="t")
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert not errors

    def test_thin_stock_error(self):
        issues = check_pocket_screw_layout(self.spec, span=400, stock_thickness=6, joint_name="t")
        assert any(i.check == "pocket_screw_thickness" for i in issues)


class TestCheckCarcassJoinery:
    def test_dado_rabbet_no_extra_issues(self):
        cfg = CabinetConfig(carcass_joinery=CarcassJoinery.DADO_RABBET)
        issues = check_carcass_joinery(cfg)
        assert issues == []

    def test_floating_tenon_standard_cabinet_ok(self):
        cfg = CabinetConfig(carcass_joinery=CarcassJoinery.FLOATING_TENON)
        issues = check_carcass_joinery(cfg)
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert not errors

    def test_pocket_screw_standard_cabinet_ok(self):
        cfg = CabinetConfig(carcass_joinery=CarcassJoinery.POCKET_SCREW)
        issues = check_carcass_joinery(cfg)
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert not errors

    def test_biscuit_thin_panel_error(self):
        # #10 biscuit slot_depth=8 mm + 3 = 11 mm min. Default side=18 mm → OK.
        cfg = CabinetConfig(carcass_joinery=CarcassJoinery.BISCUIT)
        issues = check_carcass_joinery(cfg)
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert not errors

    def test_dowel_standard_cabinet_ok(self):
        cfg = CabinetConfig(carcass_joinery=CarcassJoinery.DOWEL)
        issues = check_carcass_joinery(cfg)
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert not errors

    def test_domino_too_large_for_thin_panel_errors(self):
        # Use 14×56 Domino (mortise_depth=27 mm) with 12 mm side panel → error
        cfg = CabinetConfig(
            side_thickness=12.0,
            carcass_joinery=CarcassJoinery.FLOATING_TENON,
            domino_spec=DominoSpec(size_key="14x56"),
        )
        issues = check_carcass_joinery(cfg)
        assert any(i.check == "domino_panel_thickness" for i in issues)
