"""Drawer-box parts must close into a box.

The bug this file exists to prevent (found 2026-08-26 at B/C drawer-box
assembly, introduced 3f7ac68 on 2026-04-23): the cutlist listed the sides at
the box's full DEPTH *and* the fronts at its full WIDTH, which double-counts
both corners — no set of those parts assembles into the box they came from.
It survived two years because the only test in the area asserted that the
assembly doc and the cutlist agreed with EACH OTHER, and they agreed on the
same wrong numbers.

So these tests never compare one document to another. They take the parts and
close them into a box.
"""

import pytest

from cabineteer.cabinet import CabinetConfig, OpeningConfig
from cabineteer.drawer import (
    DrawerConfig,
    box_config_for_opening,
    drawer_part_offsets,
)
from cabineteer.joinery import DrawerJoineryStyle
from cabineteer.server import _raw_panels_for_cabinet
from cabineteer.assembly import build_drawer_box_plans


ALL_STYLES = list(DrawerJoineryStyle)
BOXES = [
    dict(opening_width=345, opening_height=133, opening_depth=451),
    dict(opening_width=600, opening_height=180, opening_depth=500),
    dict(opening_width=1219.2, opening_height=305, opening_depth=600),
]


def _cfg(style, box, **kw):
    return DrawerConfig(
        side_thickness=12.0, front_back_thickness=12.0,
        slide_key="blum_tandem_plus_563h", joinery_style=style, **box, **kw)


def _closes_to(cfg, side_len, fb_len):
    """The box those two part lengths actually assemble into.

    Which piece wraps the corner decides which one spans the finished
    dimension and which one is buried, so the closure is lap-aware — the
    single thing the old code got wrong.
    """
    j = cfg.joinery
    if j.laps_front:
        return fb_len, side_len + 2 * j.lip
    return fb_len + 2 * (cfg.side_thickness - j.engagement_x), side_len


# ─── The parts close ──────────────────────────────────────────────────────

@pytest.mark.parametrize("style", ALL_STYLES, ids=lambda s: s.value)
@pytest.mark.parametrize("box", BOXES, ids=lambda b: str(b["opening_width"]))
def test_part_lengths_close_into_the_box(style, box):
    cfg = _cfg(style, box, corner_lip_mm=2.0)
    w, d = _closes_to(cfg, cfg.side_panel_length, cfg.front_back_panel_length)
    assert w == pytest.approx(cfg.box_width)
    assert d == pytest.approx(cfg.box_depth)


@pytest.mark.parametrize("style", ALL_STYLES, ids=lambda s: s.value)
def test_exactly_one_piece_runs_full_length_per_axis(style):
    """The shape of the old bug, stated directly: the sides cannot run the
    full depth while the fronts run the full width."""
    cfg = _cfg(style, BOXES[0], corner_lip_mm=2.0)
    side_full = cfg.side_panel_length == pytest.approx(cfg.box_depth)
    fb_full = cfg.front_back_panel_length == pytest.approx(cfg.box_width)
    assert side_full != fb_full, (
        f"{style.value}: side={cfg.side_panel_length} (box depth "
        f"{cfg.box_depth}), front/back={cfg.front_back_panel_length} (box "
        f"width {cfg.box_width}) — both cannot span the box")


@pytest.mark.parametrize("style", ALL_STYLES, ids=lambda s: s.value)
@pytest.mark.parametrize("box", BOXES, ids=lambda b: str(b["opening_width"]))
def test_bottom_reaches_every_groove_floor(style, box):
    cfg = _cfg(style, box, corner_lip_mm=2.0)
    g = cfg.bottom_dado_depth
    assert cfg.bottom_panel_width + 2 * (cfg.side_thickness - g) == pytest.approx(
        cfg.box_width)
    assert cfg.bottom_panel_depth + 2 * (cfg.front_back_thickness - g) == pytest.approx(
        cfg.box_depth)


@pytest.mark.parametrize("style", ALL_STYLES, ids=lambda s: s.value)
def test_sides_span_the_whole_bottom(style):
    """A front-lapping corner shortens the sides, and the bottom's groove
    runs along them: if the lip ever grew past the groove's own setback the
    bottom's corners would reach past the end of the side and be held by
    nothing. (The x edges need no such check — the sides are full thickness
    there, so the bottom's corners sit in their grooves even where they run
    past the end of a butted front.)"""
    cfg = _cfg(style, BOXES[0], corner_lip_mm=2.0)
    off = drawer_part_offsets(cfg)
    side_y0 = off["side_L"][1]
    side_y1 = side_y0 + cfg.side_panel_length
    bot_y0 = off["bottom"][1]
    bot_y1 = bot_y0 + cfg.bottom_panel_depth
    assert side_y0 <= bot_y0 + 1e-9
    assert bot_y1 <= side_y1 + 1e-9


# ─── The cutlist ships those numbers ──────────────────────────────────────

def _cabinet(style, lip=2.0):
    return CabinetConfig(
        width=381, height=389, depth=457,
        side_thickness=18, bottom_thickness=18, top_thickness=18,
        drawer_box_thickness=12,
        drawer_slide="blum_tandem_plus_563h",
        drawer_joinery=style,
        drawer_corner_lip_mm=lip,
        openings=[OpeningConfig(height_mm=133.0, opening_type="drawer")],
        leg_key=None, leg_count=0,
    )


@pytest.mark.parametrize("style", ALL_STYLES, ids=lambda s: s.value)
def test_cutlist_rows_close_into_the_box(style):
    cfg = _cabinet(style)
    _, six, box, _ = _raw_panels_for_cabinet(cfg, None)
    rows = {p.name: p for p in list(box) + list(six)}
    dcfg = box_config_for_opening(
        cfg, cfg.interior_width, 133.0, cfg.interior_depth, cfg.openings[0])

    w, d = _closes_to(dcfg,
                      rows["drawer_box_side"].length,
                      rows["drawer_box_front"].length)
    assert w == pytest.approx(dcfg.box_width, abs=0.05)
    assert d == pytest.approx(dcfg.box_depth, abs=0.05)
    assert rows["drawer_box_back"].length == rows["drawer_box_front"].length
    assert rows["drawer_box_bottom"].length == pytest.approx(
        dcfg.bottom_panel_width, abs=0.05)
    assert rows["drawer_box_bottom"].width == pytest.approx(
        dcfg.bottom_panel_depth, abs=0.05)


@pytest.mark.parametrize("style", ALL_STYLES, ids=lambda s: s.value)
def test_every_box_row_says_what_the_joint_took(style):
    """Numbers a builder cannot check are how the last one survived."""
    cfg = _cabinet(style)
    _, _, box, _ = _raw_panels_for_cabinet(cfg, None)
    for p in box:
        assert p.notes, f"{p.name} ships no explanation of its length"


# ─── The bench numbers, locked ────────────────────────────────────────────

class TestCharliesBenchNumbers:
    """B/C kids' desks: 12 mm Baltic birch, drawer-lock corners measured at a
    2 mm lip, 563H 15" runners in a 381-wide carcass. These are the numbers
    on the 2026-08-26 bench card; the ones on the batch paper before it were
    303 fronts and 291 x 381 bottoms."""

    def _boxes(self):
        cfg = _cabinet(DrawerJoineryStyle.DRAWER_LOCK, lip=2.0)
        return box_config_for_opening(
            cfg, cfg.interior_width, 133.0, cfg.interior_depth,
            cfg.openings[0])

    def test_box_is_303_by_381(self):
        d = self._boxes()
        assert (d.box_width, d.box_depth) == (303.0, 381.0)

    def test_sides_are_377(self):
        assert self._boxes().side_panel_length == pytest.approx(377.0)

    def test_fronts_and_backs_are_303(self):
        assert self._boxes().front_back_panel_length == pytest.approx(303.0)

    def test_bottom_is_291_by_369(self):
        d = self._boxes()
        assert d.bottom_panel_width == pytest.approx(291.0)
        assert d.bottom_panel_depth == pytest.approx(369.0)


# ─── The instructions quote the same parts ────────────────────────────────

@pytest.mark.parametrize("style", ALL_STYLES, ids=lambda s: s.value)
def test_assembly_plan_closes_too(style):
    cfg = _cabinet(style)
    plan = build_drawer_box_plans(cfg)[0]
    dcfg = box_config_for_opening(
        cfg, cfg.interior_width, 133.0, cfg.interior_depth, cfg.openings[0])
    w, d = _closes_to(dcfg, plan.side_length, plan.front_back_length)
    assert w == pytest.approx(plan.box_width, abs=0.05)
    assert d == pytest.approx(plan.box_depth, abs=0.05)


def test_box_steps_do_not_claim_both_parts_run_full_size():
    """The old step text said 'sides run the box DEPTH; fronts and backs run
    the box WIDTH' — true of neither joint, and the sentence a builder would
    have trusted over the parts list."""
    from cabineteer.assembly import _build_box_steps
    for style in ALL_STYLES:
        cfg = _cabinet(style)
        boxes = build_drawer_box_plans(cfg)
        text = " ".join(s.body + " " + " ".join(s.checklist)
                        for s in _build_box_steps(boxes, cfg))
        assert not ("run the box DEPTH" in text and "run the box WIDTH" in text)
        if boxes[0].laps_front:
            assert "lip" in text.lower()
