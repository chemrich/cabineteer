"""What a drawer slide's side clearance is measured TO.

This is the check that was missing when every undermount drawer box in the
corpus came out ``2 x side_thickness`` too narrow.  It could not have been
caught by any gap check, because the gap was computed from the same constant
it would have been compared against — a closed circle that can only pass.

So these tests are anchored OUTSIDE the codebase wherever possible:

  * Blum's own published "Calculating outside drawer width" deduction table
    (``BLUM_UNDERMOUNT_WIDTH_DEDUCTION``), five independent data points that
    the formula has to reproduce;
  * the physical invariant that a box's parts, its bottom panel and its
    placement in the opening all have to agree about the same two faces.

Do not rewrite an assertion here into the formula it is checking.
"""

import pytest

from cabineteer.drawer import DrawerConfig
from cabineteer.hardware import (
    BLUM_UNDERMOUNT_WIDTH_DEDUCTION,
    SLIDES,
    ClearanceReference,
    SlideMountLocation,
    get_slide,
)


UNDERMOUNTS = [k for k, s in SLIDES.items()
               if s.mount_location is SlideMountLocation.BOTTOM]
SIDE_MOUNTS = [k for k, s in SLIDES.items()
               if s.mount_location is not SlideMountLocation.BOTTOM]


class TestBlumPublishedTable:
    """Blum, TANDEM plus 563H/563 installation instructions, page 2."""

    @pytest.mark.parametrize(
        "side_t,deduction", sorted(BLUM_UNDERMOUNT_WIDTH_DEDUCTION.items()))
    @pytest.mark.parametrize("opening", [345.0, 564.0, 1000.0])
    def test_outside_width_matches_blums_deduction(self, opening, side_t,
                                                   deduction):
        slide = get_slide("blum_tandem_plus_563h")
        assert slide.drawer_box_width(opening, side_t) == pytest.approx(
            opening - deduction)

    @pytest.mark.parametrize(
        "side_t", sorted(BLUM_UNDERMOUNT_WIDTH_DEDUCTION))
    def test_inside_width_is_the_same_42_at_every_stock(self, side_t):
        """Blum's NOTE: "inside drawer width must equal opening − 42"."""
        slide = get_slide("blum_tandem_plus_563h")
        assert slide.drawer_inside_width(564.0, side_t) == pytest.approx(522.0)

    def test_the_table_is_not_a_restatement_of_the_formula(self):
        """Guard the anchor itself: 5 points, all 42 − 2t, none invented."""
        assert len(BLUM_UNDERMOUNT_WIDTH_DEDUCTION) == 5
        for t, ded in BLUM_UNDERMOUNT_WIDTH_DEDUCTION.items():
            assert t + t + ded == 42.0


class TestReferenceFaceByMounting:
    @pytest.mark.parametrize("key", UNDERMOUNTS)
    def test_undermount_clearance_is_to_the_inside_face(self, key):
        """The runner is under the box; nothing sits beside the drawer side."""
        assert get_slide(key).clearance_reference is ClearanceReference.INSIDE

    @pytest.mark.parametrize("key", SIDE_MOUNTS)
    def test_side_mount_clearance_is_to_the_outside_face(self, key):
        """The slide body lives in the gap, so the gap IS the clearance."""
        assert get_slide(key).clearance_reference is ClearanceReference.OUTSIDE

    @pytest.mark.parametrize("key", SIDE_MOUNTS)
    def test_side_mount_width_ignores_the_side_stock(self, key):
        slide = get_slide(key)
        assert (slide.drawer_box_width(600.0, 12.0)
                == slide.drawer_box_width(600.0, 19.0))

    @pytest.mark.parametrize("key", UNDERMOUNTS)
    def test_undermount_width_follows_the_side_stock(self, key):
        slide = get_slide(key)
        assert (slide.drawer_box_width(600.0, 19.0)
                - slide.drawer_box_width(600.0, 12.0)) == pytest.approx(14.0)

    def test_an_explicit_reference_overrides_the_mounting_default(self):
        from dataclasses import replace
        odd = replace(get_slide("blum_tandem_plus_563h"),
                      clearance_reference=ClearanceReference.OUTSIDE)
        assert odd.drawer_box_width(600.0, 12.0) == pytest.approx(558.0)


class TestTheThreeWidthsAgree:
    """Outside, inside and gap are one geometry stated three ways."""

    @pytest.mark.parametrize("key", sorted(SLIDES))
    @pytest.mark.parametrize("side_t", [12.0, 15.0, 16.0])
    def test_gap_plus_box_fills_the_opening(self, key, side_t):
        slide = get_slide(key)
        outside = slide.drawer_box_width(700.0, side_t)
        assert 2 * slide.side_gap(700.0, side_t) + outside == pytest.approx(700.0)

    @pytest.mark.parametrize("key", sorted(SLIDES))
    @pytest.mark.parametrize("side_t", [12.0, 15.0, 16.0])
    def test_walls_plus_inside_fill_the_box(self, key, side_t):
        slide = get_slide(key)
        assert (slide.drawer_inside_width(700.0, side_t) + 2 * side_t
                == pytest.approx(slide.drawer_box_width(700.0, side_t)))


class TestDrawerConfigAgreesWithTheSlide:
    @pytest.mark.parametrize("key", sorted(SLIDES))
    def test_config_widths_come_from_the_slide(self, key):
        cfg = DrawerConfig(opening_width=600, opening_height=180,
                           opening_depth=500, side_thickness=12,
                           front_back_thickness=12, slide_key=key)
        slide = get_slide(key)
        assert cfg.box_width == pytest.approx(slide.drawer_box_width(600, 12))
        assert cfg.box_inside_width == pytest.approx(
            slide.drawer_inside_width(600, 12))
        assert cfg.side_gap == pytest.approx(slide.side_gap(600, 12))

    def test_bottom_panel_spans_the_inside_plus_both_grooves(self):
        cfg = DrawerConfig(opening_width=345, opening_height=133,
                           opening_depth=448, side_thickness=12,
                           front_back_thickness=12,
                           slide_key="blum_tandem_plus_563h")
        assert cfg.bottom_panel_width == pytest.approx(
            cfg.box_inside_width + 2 * cfg.bottom_dado_depth)

    def test_charlies_pedestal_box_in_full(self):
        """345 mm opening, 12 mm Baltic birch, 563H: 327 outside / 303 in."""
        cfg = DrawerConfig(opening_width=345, opening_height=133,
                           opening_depth=448, side_thickness=12,
                           front_back_thickness=12,
                           slide_key="blum_tandem_plus_563h")
        assert cfg.box_width == pytest.approx(327.0)
        assert cfg.box_inside_width == pytest.approx(303.0)
        assert cfg.side_gap == pytest.approx(9.0)
        assert cfg.bottom_panel_width == pytest.approx(315.0)


class TestTheOldBugCannotComeBack:
    def test_reading_42_as_an_outside_rule_is_now_an_error(self):
        """A box built to opening − 42 outside must fail the fit check.

        This is the exact box the whole corpus was cut to before
        2026-08-29: correct on every axis except that it is one wall per
        side too narrow, so the runners cannot reach it.
        """
        slide = get_slide("blum_tandem_plus_563h")
        issues = slide.validate_drawer_dims(
            drawer_width=345.0 - 42.0,   # the old, wrong outside width
            drawer_height=102.0,
            drawer_depth=381.0,
            opening_width=345.0,
            side_thickness=12.0,
        )
        assert any("clearance" in i.lower() for i in issues)

    def test_the_corrected_box_passes_the_same_check(self):
        slide = get_slide("blum_tandem_plus_563h")
        assert slide.validate_drawer_dims(
            drawer_width=327.0,
            drawer_height=102.0,
            drawer_depth=381.0,
            opening_width=345.0,
            side_thickness=12.0,
        ) == []

    def test_omitting_the_side_stock_is_a_type_error_not_a_silent_wrong_answer(self):
        """The parameter is required precisely so this cannot regress."""
        slide = get_slide("blum_tandem_plus_563h")
        with pytest.raises(TypeError):
            slide.drawer_box_width(345.0)
