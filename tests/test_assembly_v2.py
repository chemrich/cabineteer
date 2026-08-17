"""Assembly-instruction v2: mitered corners + edge banding in the docs."""

import pytest

from cabineteer.assembly import (
    build_assembly_plan,
    generate_assembly_html,
)
from cabineteer.cabinet import CabinetConfig, ColumnConfig


def _miter_cfg(**kw) -> CabinetConfig:
    return CabinetConfig(
        width=1219.2, height=663.6, depth=457,
        carcass_corner_style="miter",
        columns=[
            ColumnConfig(width_mm=582.6, openings=()),
            ColumnConfig(width_mm=582.6, openings=()),
        ], **kw)


class TestMiterPlan:
    def test_corner_joints_marked_miter(self):
        plan = build_assembly_plan(_miter_cfg())
        kinds = {j.name: j.kind for j in plan.joints}
        assert kinds["bottom ↔ left side"] == "miter"
        assert kinds["top ↔ right side"] == "miter"
        assert kinds["divider 1 ↔ bottom"] == "butt"

    def test_miter_placement_solved(self):
        plan = build_assembly_plan(_miter_cfg())
        assert plan.corner_style == "miter"
        assert plan.miter_placement is not None
        assert plan.miter_placement.show_face_wall >= 2.0

    def test_thin_stock_raises(self):
        with pytest.raises(ValueError, match="45° miter"):
            build_assembly_plan(_miter_cfg(
                side_thickness=12, top_thickness=12, bottom_thickness=12))

    def test_side_map_rows_move_to_beveled_ends(self):
        plan = build_assembly_plan(_miter_cfg())
        side = next(p for p in plan.panels if p.panel.startswith("side"))
        miter_rows = [r for r in side.rows if r.kind == "miter"]
        assert len(miter_rows) == 2
        assert {r.offset for r in miter_rows} == {0.0, 663.6}
        assert not any(r.kind == "face" and "top" in r.label
                       for r in side.rows)

    def test_top_map_full_width_and_shifted_dividers(self):
        plan = build_assembly_plan(_miter_cfg())
        top = next(p for p in plan.panels if p.panel == "top")
        assert top.draw_width == pytest.approx(1219.2)
        assert all(r.kind == "miter" for r in top.rows if r.axis == "v"
                   and r.label == "")
        # Row = divider LEFT face + 10 mm base-height reference, shifted
        # right by side_thickness under the long-point panel origin:
        # 582.6 + 10 + 18 = 610.6.
        div_row = next(r for r in top.rows if "divider" in r.label)
        assert div_row.offset == pytest.approx(582.6 + 10 + 18)

    def test_miter_steps_and_ordering(self):
        plan = build_assembly_plan(_miter_cfg())
        titles = [s.title for s in plan.steps]
        miter_i = next(i for i, t in enumerate(titles)
                       if "miter mortises" in t)
        dry_i = next(i for i, t in enumerate(titles) if "DRY FIT" in t)
        assert miter_i < dry_i
        body = plan.steps[miter_i].body
        assert "45°" in body and "SCRAP" in body.upper()
        glue = next(s for s in plan.steps if s.title.startswith("Glue up"))
        # The staged actions live in the step's checklist, not its prose.
        glue_text = " ".join((glue.body,) + tuple(glue.checklist))
        assert "tape" in glue_text
        assert "band clamps" in glue_text

    def test_machine_rows_include_miter_placement(self):
        html = generate_assembly_html(
            [build_assembly_plan(_miter_cfg())], "p")
        assert "Miter placement" in html
        assert "45° miter face" in html
        assert "#8e44ad" in html   # purple rows present in the maps


class TestBandingSteps:
    def test_hardwood_band_precedes_mortising(self):
        cfg = CabinetConfig(width=800, height=700, depth=457,
                            edge_band_mode="hardwood",
                            edge_band_thickness_mm=6.4)
        plan = build_assembly_plan(cfg)
        titles = [s.title for s in plan.steps]
        band_i = next(i for i, t in enumerate(titles) if "Band the front" in t)
        setup_i = next(i for i, t in enumerate(titles)
                       if "Set up the Domino" in t)
        assert band_i < setup_i
        assert "BANDED front edge" in plan.steps[band_i].body

    def test_hot_melt_banding_after_glue_up(self):
        cfg = CabinetConfig(width=800, height=700, depth=457,
                            edge_band_mode="hot_melt")
        plan = build_assembly_plan(cfg)
        titles = [s.title for s in plan.steps]
        iron_i = next(i for i, t in enumerate(titles) if "Iron on" in t)
        glue_i = next(i for i, t in enumerate(titles)
                      if t.startswith("Square with the back"))
        assert iron_i > glue_i
        assert not any("Band the front" in t for t in titles)

    def test_no_banding_no_band_steps(self):
        plan = build_assembly_plan(
            CabinetConfig(width=800, height=700, depth=457))
        titles = " ".join(s.title for s in plan.steps)
        assert "Band the front" not in titles
        assert "Iron on" not in titles


class TestCombinedRender:
    def test_miter_plus_hardwood_pdf(self):
        pytest.importorskip("reportlab")
        from cabineteer.assembly import generate_assembly_pdf
        plan = build_assembly_plan(_miter_cfg(
            edge_band_mode="hardwood", edge_band_thickness_mm=6.4))
        pdf = generate_assembly_pdf([plan], "v2")
        assert pdf.startswith(b"%PDF")
