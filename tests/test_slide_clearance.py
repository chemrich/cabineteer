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


class TestTheGuardsCannotBeBypassed:
    """P0: the datum discriminator only works if its companion is mandatory.

    #91 introduced ClearanceReference and then defaulted ``side_thickness``
    to 0.0 on both validators, which inverts their verdict — a box built to
    the old outside-width reading passed and a correct one failed. These
    tests are the reason the argument is positional and required.
    """

    def test_validate_drawer_dims_requires_the_side_stock(self):
        slide = get_slide("blum_tandem_plus_563h")
        with pytest.raises(TypeError):
            slide.validate_drawer_dims(
                drawer_width=552.0, drawer_height=120,
                drawer_depth=450, opening_width=564)

    def test_check_drawer_in_opening_requires_the_side_stock(self):
        import inspect
        from cabineteer.evaluation import check_drawer_in_opening
        sig = inspect.signature(check_drawer_in_opening)
        assert sig.parameters["side_thickness"].default is inspect.Parameter.empty

    def test_no_width_validator_defaults_the_side_stock(self):
        """Catches the same shape on any validator added later."""
        import inspect
        from cabineteer.hardware import DrawerSlideSpec
        for name in ("drawer_box_width", "drawer_inside_width", "side_gap",
                     "validate_drawer_dims"):
            sig = inspect.signature(getattr(DrawerSlideSpec, name))
            assert sig.parameters["side_thickness"].default is inspect.Parameter.empty, (
                f"{name} defaults side_thickness — a defaulted 0 reproduces the "
                f"pre-2026-08 undermount width bug")


class TestTheAdviceIsFollowable:
    """A remedy derived a second way is a remedy that can disagree with its check.

    The 2026-08 review found exactly that: the too-narrow error advised a
    width that still errored. This follows the advice and asserts it clears.
    """

    @pytest.mark.parametrize("slide_key,side_t", [
        ("blum_tandem_plus_563h", 12.0),
        ("blum_tandem_550h", 15.0),
        ("accuride_3832", 15.0),
    ])
    @pytest.mark.parametrize("start_width", [100.0, 140.0, 180.0])
    def test_widening_to_the_advised_interior_clears_the_error(
            self, slide_key, side_t, start_width):
        import re
        from cabineteer.cabinet import CabinetConfig
        from cabineteer.evaluation import check_drawer_carcass_clearances

        def issues_for(width):
            cfg = CabinetConfig(width=width, height=720, depth=550,
                                drawer_box_thickness=side_t,
                                drawer_slide=slide_key,
                                openings=[(150.0, "drawer")])
            return check_drawer_carcass_clearances(cfg)

        found = [i for i in issues_for(start_width)
                 if "Widen the opening to at least" in i.message]
        if not found:
            pytest.skip("already wide enough for this slide/stock")
        advised = float(re.search(r"at least (\d+(?:\.\d+)?) mm",
                                  found[0].message).group(1))
        # The advice names an INTERIOR width; the config takes an exterior.
        after = issues_for(advised + 2 * 18.0)
        assert not [i for i in after if "inside the drawer box" in i.message], (
            f"advised {advised} mm interior, which still reports: "
            f"{[i.message for i in after]}")


class TestTheBottomRecessIsTheRunnersDimension:
    """Where the drawer bottom sits is decided by the runner, not the designer.

    An undermount carries the box under its bottom panel and its front
    locking devices engage that panel, so the recess — box side's bottom
    edge up to the underside of the bottom, i.e. the groove's lower shoulder
    — is an interface dimension. It sat on ``DrawerConfig`` as a bare 12.0
    default until 2026-08-29: unreachable from every tool argument, checked
    by nothing, and 1 mm off what Blum specifies. Same shape as #91's side
    clearance, one axis over.

    Charlie cut his C boxes at 13 on 2026-08-29 and they run.
    """

    #: Blum's published figure for the whole TANDEM and MOVENTO wood-drawer
    #: range. External anchor: TANDEM plus BLUMOTION 563H/563 Installation
    #: Instructions, INST-TDM563H-563 05.16, page 2, dimensioned on the
    #: drawer-box front view beside the 14 mm bottom clearance; and the
    #: locking devices are described as "designed for 13 (1/2") drawer
    #: bottom recess". Do not replace this with a reference to the code.
    BLUM_RECESS_MM = 13.0

    @pytest.mark.parametrize("key", [k for k in SLIDES if k.startswith("blum_")])
    def test_every_blum_undermount_carries_blums_figure(self, key):
        assert get_slide(key).bottom_recess == pytest.approx(self.BLUM_RECESS_MM)

    def test_a_side_mount_imposes_none(self):
        """It never touches the drawer bottom, so it constrains nothing."""
        slide = get_slide("accuride_3832")
        assert slide.bottom_recess is None
        assert slide.min_bottom_clearance == 0.0

    @pytest.mark.parametrize("key", sorted(SLIDES))
    def test_the_config_takes_the_recess_from_its_slide(self, key):
        cfg = DrawerConfig(opening_width=345, opening_height=133,
                           opening_depth=448, side_thickness=12,
                           front_back_thickness=12, slide_key=key)
        required = get_slide(key).bottom_recess
        if required is not None:
            assert cfg.bottom_dado_inset == pytest.approx(required)
        else:
            from cabineteer.drawer import DEFAULT_BOTTOM_DADO_INSET
            assert cfg.bottom_dado_inset == pytest.approx(DEFAULT_BOTTOM_DADO_INSET)

    def test_an_explicit_value_still_wins(self):
        """A slide this repo has no figure for still needs a way in."""
        cfg = DrawerConfig(opening_width=345, opening_height=133,
                           opening_depth=448, side_thickness=12,
                           front_back_thickness=12,
                           slide_key="blum_tandem_plus_563h",
                           bottom_dado_inset=11.0)
        assert cfg.bottom_dado_inset == pytest.approx(11.0)

    def test_a_groove_at_the_wrong_height_is_an_error(self):
        """The check the bare default never had.

        Non-circular: the inset is an independent input and the requirement
        comes from the slide catalogue, so this can genuinely fail — unlike
        the side-clearance branch deleted as dead code in 2026-08 because it
        compared a value against the constant it was derived from.
        """
        from cabineteer.evaluation import check_drawer_hardware_clearances
        cfg = DrawerConfig(opening_width=345, opening_height=133,
                           opening_depth=448, side_thickness=12,
                           front_back_thickness=12,
                           slide_key="blum_tandem_plus_563h",
                           bottom_dado_inset=12.0)   # the pre-2026-08-29 default
        issues = check_drawer_hardware_clearances(cfg)
        assert [i for i in issues if i.check == "drawer_bottom_recess"]

    def test_the_resolved_recess_evaluates_clean(self):
        from cabineteer.evaluation import check_drawer_hardware_clearances
        cfg = DrawerConfig(opening_width=345, opening_height=133,
                           opening_depth=448, side_thickness=12,
                           front_back_thickness=12,
                           slide_key="blum_tandem_plus_563h")
        assert not [i for i in check_drawer_hardware_clearances(cfg)
                    if i.check == "drawer_bottom_recess"]

    def test_the_recess_changes_no_cut_dimension(self):
        """It is a fence setting, not a size. Guards the blast radius claim.

        The bottom panel is sized to the groove FLOORS via bottom_dado_depth,
        and the box height comes from the slide's clearances — neither reads
        the inset. If that ever stops being true, the 12-to-13 change stops
        being free and this fails.
        """
        def box(inset):
            return DrawerConfig(opening_width=345, opening_height=133,
                                opening_depth=448, side_thickness=12,
                                front_back_thickness=12,
                                slide_key="blum_tandem_plus_563h",
                                bottom_dado_inset=inset)
        a, b = box(12.0), box(13.0)
        for attr in ("box_width", "box_height", "box_depth",
                     "side_panel_length", "front_back_panel_length",
                     "bottom_panel_width", "bottom_panel_depth"):
            assert getattr(a, attr) == pytest.approx(getattr(b, attr)), attr
