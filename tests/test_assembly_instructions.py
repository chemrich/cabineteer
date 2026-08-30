"""Tests for the expanded bench instructions.

Charlie's 2026-08-16 ask: the assembly doc was "woefully thin" on back
construction, drawer-box construction, picking front and outward faces,
marking Domino lines, the dry fit, and the glue-up. The document was 12
steps / 905 words, with the entire glue-up — the irreversible part — in
about 150 of them.

Two things these tests pin down. First, that the thin areas actually
carry content and that the safety-critical facts survive edits. Second,
and more important, that the drawer-box plan quotes the SAME dimensions
as the cutlist: instructions that disagree with the parts list are worse
than no instructions.
"""

import pytest

from cabineteer.assembly import (build_assembly_plan, build_drawer_box_plans,
                                 generate_assembly_html)
from cabineteer.cabinet import CabinetConfig, ColumnConfig, OpeningConfig
from cabineteer.joinery import CarcassJoinery, DrawerJoineryStyle
from cabineteer.server import _raw_panels_for_cabinet


def _cfg(**kw) -> CabinetConfig:
    kw.setdefault("carcass_joinery", CarcassJoinery.FLOATING_TENON)
    kw.setdefault("drawer_box_thickness", 12.0)
    kw.setdefault("drawer_joinery", DrawerJoineryStyle.DRAWER_LOCK)
    kw.setdefault("width", 900.0)
    kw.setdefault("height", 760.0)
    kw.setdefault("depth", 560.0)
    return CabinetConfig(**kw)


def _drawer_cfg(**kw) -> CabinetConfig:
    """Two columns of drawers — the shape A and E actually are."""
    col = lambda w: ColumnConfig(  # noqa: E731
        width_mm=w,
        openings=(OpeningConfig(height_mm=254.0, opening_type="drawer"),
                  OpeningConfig(height_mm=228.0, opening_type="drawer"),
                  OpeningConfig(height_mm=242.0, opening_type="drawer")))
    return _cfg(columns=[col(432.0), col(432.0)], **kw)


def _step(plan, fragment):
    return next(s for s in plan.steps if fragment.lower() in s.title.lower())


def _text(step):
    """Everything the step PRINTS, title included.

    The title was excluded, so every text assertion in this file was blind
    to headings — and the sentence that contradicted the mixed-bottom
    warning was a heading ("Groove for the bottoms — one saw setup, every
    part"). A guard that cannot see half the page is half a guard.
    """
    return " ".join((step.title, step.body) + tuple(step.checklist))


class TestShowFaces:
    """Has to come first: a reference face marked on the wrong side puts
    every mortise on the side meant to be seen."""

    def test_is_the_very_first_step(self):
        plan = build_assembly_plan(_cfg())
        assert "show faces" in plan.steps[0].title
        assert "before any other cut" in plan.steps[0].title

    def test_covers_both_face_and_grain(self):
        text = _text(build_assembly_plan(_cfg()).steps[0])
        assert "GRAIN runs along each panel's LENGTH" in text
        assert "HIDDEN face" in text

    def test_calls_out_the_legs_case_for_the_bottom(self):
        on_legs = _text(build_assembly_plan(_cfg(leg_count=4)).steps[0])
        assert "UNDERSIDE shows" in on_legs
        flat = _text(build_assembly_plan(_cfg(leg_count=0)).steps[0])
        assert "never seen" in flat

    def test_names_dividers_as_two_sided(self):
        text = _text(build_assembly_plan(_drawer_cfg()).steps[0])
        assert "BOTH faces are seen" in text

    def test_silent_about_parts_the_cabinet_does_not_have(self):
        """A plain box has no dividers or shelves to talk about."""
        text = _text(build_assembly_plan(_cfg(leg_count=0)).steps[0])
        assert "DIVIDERS" not in text
        assert "FIXED SHELVES" not in text


class TestDryFitAndGlueUp:
    def test_dry_fit_is_a_checklist_not_a_paragraph(self):
        step = _step(build_assembly_plan(_cfg()), "DRY FIT")
        assert len(step.checklist) >= 6

    def test_dry_fit_checks_flat_and_square_separately(self):
        text = _text(_step(build_assembly_plan(_cfg()), "DRY FIT"))
        assert "rock" in text                       # twist
        assert "diagonals" in text                  # square
        assert "1 mm" in text

    def test_dry_fit_measures_openings_front_and_back(self):
        """A divider square at the front and leaning at the back still
        measures right where you first checked."""
        text = _text(_step(build_assembly_plan(_drawer_cfg()), "DRY FIT"))
        assert "FRONT" in text and "BACK" in text

    def test_glue_goes_in_the_mortise_never_on_the_tenon(self):
        text = _text(_step(build_assembly_plan(_cfg()), "Glue up"))
        assert "not onto the tenon" in text or "never onto the tenon" in text
        assert "swells" in text

    def test_glue_up_is_staged_when_there_is_inner_structure(self):
        staged = _text(_step(build_assembly_plan(_drawer_cfg()), "Glue up"))
        assert "STAGE 1" in staged and "STAGE 2" in staged

    def test_plain_box_is_told_to_glue_in_one_stage(self):
        text = _text(_step(build_assembly_plan(_cfg()), "Glue up"))
        assert "ONE stage" in text
        assert "STAGE 1" not in text

    def test_cure_step_distinguishes_clamp_time_from_full_strength(self):
        text = _text(_step(build_assembly_plan(_cfg()), "Square with the back"))
        assert "30–60" in text
        assert "24 h" in text or "24 hours" in text

    def test_rabbet_back_offers_the_choice_of_when_to_fit_it(self):
        """A rabbeted back drops in from behind, so it can either square the
        case during glue-up or stay out of the clamp race entirely."""
        text = _text(_step(build_assembly_plan(_cfg(back_capture="rabbet")),
                           "Square with the back"))
        assert "choice" in text
        assert "after the cure" in text

    def test_captive_back_offers_no_such_choice(self):
        text = _text(_step(build_assembly_plan(_cfg(back_capture="dado")),
                           "Square with the back"))
        assert "cannot go in afterwards" in text
        assert "choice" not in text


class TestDrawerBoxPlan:
    def test_boxes_are_found_bottom_up_per_column(self):
        boxes = build_drawer_box_plans(_drawer_cfg())
        assert len(boxes) == 6
        assert [b.position for b in boxes] == [1, 2, 3, 1, 2, 3]
        assert [b.column for b in boxes] == [1, 1, 1, 2, 2, 2]

    def test_single_column_labels_omit_the_column(self):
        cfg = _cfg(openings=[OpeningConfig(height_mm=254.0,
                                           opening_type="drawer")])
        assert build_drawer_box_plans(cfg)[0].label == "drawer 1 from the bottom"

    def test_no_drawers_no_boxes(self):
        assert build_drawer_box_plans(_cfg()) == []

    def test_doors_are_not_mistaken_for_drawers(self):
        cfg = _cfg(openings=[OpeningConfig(height_mm=600.0,
                                           opening_type="door")])
        assert build_drawer_box_plans(cfg) == []

    @pytest.mark.parametrize("capture", ["pocket", "rabbet", "dado"])
    def test_box_parts_match_the_cutlist_exactly(self, capture):
        """The whole point of routing both through box_config_for_opening:
        instructions that quote different numbers from the parts list are
        worse than no instructions."""
        from collections import Counter
        cfg = _drawer_cfg(back_capture=capture)
        cols = [{"width_mm": c.width_mm,
                 "openings": [{"height_mm": o.height_mm,
                               "opening_type": o.opening_type}
                              for o in c.openings]}
                for c in cfg.columns]
        _, six, box, _ = _raw_panels_for_cabinet(cfg, cols)

        cut = Counter()
        for p in list(box) + [p for p in six
                              if p.name == "drawer_box_bottom"]:
            cut[(p.name, p.length, p.width, p.thickness)] += p.quantity

        plan = Counter()
        for b in build_drawer_box_plans(cfg):
            plan[("drawer_box_side", b.side_length, b.side_height,
                  b.stock_thickness)] += 2
            plan[("drawer_box_front", b.front_back_length,
                  b.front_back_height, b.stock_thickness)] += 1
            plan[("drawer_box_back", b.front_back_length,
                  b.front_back_height, b.stock_thickness)] += 1
            plan[("drawer_box_bottom", b.bottom_length, b.bottom_width,
                  b.bottom_thickness)] += 1
        assert cut == plan

    def test_box_depth_follows_the_back_capture(self):
        """A dado holds the back further forward, so the boxes see a
        shallower interior."""
        pocket = build_drawer_box_plans(_drawer_cfg())[0]
        dado = build_drawer_box_plans(_drawer_cfg(back_capture="dado"))[0]
        assert dado.side_length <= pocket.side_length


class TestDrawerBoxSteps:
    def test_no_steps_without_boxes(self):
        assert build_assembly_plan(_cfg()).box_steps == []

    def test_steps_cover_the_whole_sequence(self):
        plan = build_assembly_plan(_drawer_cfg())
        titles = " ".join(s.title.lower() for s in plan.box_steps)
        for expected in ("read before cutting", "groove", "corners",
                         "assemble", "slides"):
            assert expected in titles

    def test_inside_face_is_called_out_before_any_cut(self):
        plan = build_assembly_plan(_drawer_cfg())
        assert "INSIDE face" in _text(plan.box_steps[0])

    def test_bottom_must_not_be_glued(self):
        """A glued-in bottom turns seasonal movement into a split panel."""
        text = " ".join(_text(s) for s in
                        build_assembly_plan(_drawer_cfg()).box_steps)
        assert "FLOATS" in text
        assert "Glue the corners only" in text

    def test_mixed_bottom_thicknesses_are_flagged(self):
        """The size rule gives big boxes 12 mm bottoms and small ones 6 mm —
        the doc must not imply one setting covers them all."""
        col = ColumnConfig(
            width_mm=560.0,   # box clears the 406.4 mm heavy-bottom width
            openings=(OpeningConfig(height_mm=300.0, opening_type="drawer"),
                      OpeningConfig(height_mm=104.0, opening_type="drawer")))
        plan = build_assembly_plan(_cfg(width=1000.0, columns=[col]))
        bottoms = {b.bottom_thickness
                   for b in build_drawer_box_plans(_cfg(width=1000.0,
                                                        columns=[col]))}
        assert len(bottoms) == 2, f"test config did not mix bottoms: {bottoms}"
        assert "NOT all the same" in _text(plan.box_steps[0])

        # Scan the WHOLE run of steps, not step 0. This assertion used to
        # stop at step 0 and pass while step 1 — the next thing on the same
        # printed page — was titled "one saw setup, every part" and said
        # "Every part of every box takes the same groove". The docstring
        # above forbids exactly that, and the test could not see it.
        text = " ".join(_text(st) for st in plan.box_steps)
        assert "one saw setup" not in text
        assert "the same groove" not in text
        # And the dimension that actually differs has to be named. A groove
        # has three; this step printed two, and the missing one was the
        # width — which IS the bottom's thickness, and the only one that
        # varies. Cutting the run at one width scraps every box on the
        # other setting.
        for b in sorted(bottoms):
            assert f"{b:g} mm" in text, (
                f"the doc never names the {b:g} mm groove width")
        assert "width" in text

    def test_uniform_bottoms_still_get_one_setup(self):
        """The other half: do not cry wolf on a run that IS uniform."""
        plan = build_assembly_plan(_drawer_cfg())
        bottoms = {b.bottom_thickness for b in plan.drawer_boxes}
        assert len(bottoms) == 1, "test config is not uniform"
        text = " ".join(_text(st) for st in plan.box_steps)
        assert "one saw setup" in text
        assert "NOT all the same" not in text
        # Uniform or not, the width is stated.
        assert f"{sorted(bottoms)[0]:g} mm wide" in text

    def test_no_box_step_number_comes_from_box_zero(self):
        """A run's steps must describe the run, not its first box.

        Every number in these steps used to be read off ``boxes[0]`` and
        asserted of all of them. Reversing the box order must not change a
        single sentence — if it does, some step is quoting one box.
        """
        import dataclasses

        col = ColumnConfig(
            width_mm=560.0,
            openings=(OpeningConfig(height_mm=300.0, opening_type="drawer"),
                      OpeningConfig(height_mm=104.0, opening_type="drawer")))
        cfg = _cfg(width=1000.0, columns=[col])
        boxes = build_drawer_box_plans(cfg)
        assert len({b.bottom_thickness for b in boxes}) > 1
        from cabineteer.assembly import _build_box_steps
        forward = [_text(st) for st in _build_box_steps(boxes, cfg)]
        reversed_ = [_text(st) for st in
                     _build_box_steps(list(reversed(boxes)), cfg)]
        assert forward == reversed_


class TestRendering:
    def test_checklists_render_as_lists_in_html(self):
        html = generate_assembly_html(
            [build_assembly_plan(_drawer_cfg())], "t")
        assert "<ol class='checklist'>" in html
        assert "<li>" in html

    def test_box_section_renders_with_its_table(self):
        html = generate_assembly_html(
            [build_assembly_plan(_drawer_cfg())], "t")
        assert "Drawer boxes" in html
        assert "Front + back" in html
        assert "Box step 1" in html

    def test_no_box_section_when_there_are_no_boxes(self):
        html = generate_assembly_html([build_assembly_plan(_cfg())], "t")
        assert "Drawer boxes" not in html

    def test_pdf_renders_with_boxes(self):
        pytest.importorskip("reportlab")
        from cabineteer.assembly import generate_assembly_pdf
        pdf = generate_assembly_pdf(
            [build_assembly_plan(_drawer_cfg())], "t")
        assert pdf.startswith(b"%PDF")


class TestSlideNaming:
    """The slide line is bench-facing: it must name the hardware the way the
    BOM does, and must not quote one slide as if it covered every box."""

    def test_single_slide_named_not_keyed(self):
        plan = build_assembly_plan(_drawer_cfg())
        step = next(s for s in plan.box_steps if s.title == "Fit the slides")
        assert "Blum" in step.body
        assert "blum_tandem" not in step.body      # no raw config key
        assert "throughout" in step.body

    def test_mixed_slides_are_flagged_not_averaged(self):
        """Charlie puts heavier slides under the heavy drawers only — quoting
        the first box's slide would be wrong for most of the run."""
        col = ColumnConfig(
            width_mm=432.0,
            openings=(OpeningConfig(height_mm=254.0, opening_type="drawer",
                                    slide_key="blum_movento_769"),
                      OpeningConfig(height_mm=104.0, opening_type="drawer")))
        plan = build_assembly_plan(_cfg(columns=[col]))
        step = next(s for s in plan.box_steps if s.title == "Fit the slides")
        assert "MIXED slides" in step.body
        assert "Movento" in step.body
        assert "throughout" not in step.body

    def test_unknown_slide_key_does_not_crash_the_doc(self):
        col = ColumnConfig(
            width_mm=432.0,
            openings=(OpeningConfig(height_mm=254.0, opening_type="drawer"),))
        plan = build_assembly_plan(_cfg(columns=[col],
                                        drawer_slide="blum_tandem_550h"))
        step = next(s for s in plan.box_steps if s.title == "Fit the slides")
        assert step.body
