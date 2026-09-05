"""Tests for back_style — 'under_top' caps the back with a full-depth top.

full_height (legacy): back runs the full carcass height; its top edge is
exposed on the top plane. under_top: the top panel is cut to full depth
(rear edge flush with the sides) and the back stops at its underside, so
no back edge is visible from above or from the sides.
"""

import pytest

from cabineteer.assembly import build_assembly_plan
from cabineteer.cabinet import CabinetConfig, build_cabinet_config
from cabineteer.evaluation import check_back_style, evaluate_cabinet
from cabineteer.joinery import CarcassJoinery
from cabineteer.project import CabinetProject, ProjectCabinet, SharedDesign
from cabineteer.server import _raw_panels_for_cabinet


def _cfg(**kw) -> CabinetConfig:
    kw.setdefault("carcass_joinery", CarcassJoinery.FLOATING_TENON)
    # This module is about back_style, not the show-face top/bottom axis —
    # pin plain/plain so a "cap" top's banded_edges=() (unrelated to
    # back_style) doesn't change what these tests are measuring.
    kw.setdefault("face_top_style", "plain")
    kw.setdefault("face_bottom_style", "plain")
    return CabinetConfig(width=1219.2, height=663.6, depth=457, **kw)


def _doc_text(plan) -> str:
    """Everything the builder reads in the step sequence.

    Steps carry prose in ``body`` and actions in ``checklist``; both land in
    the HTML and the PDF, so a test asking "does the document say this?"
    has to look at both.
    """
    return " ".join(
        " ".join((s.body,) + tuple(s.checklist)) for s in plan.steps)


def _panel(panels, name):
    return next(p for p in panels if p.name == name)


class TestCutlistPanels:
    def test_full_height_default_dims_unchanged(self):
        cfg = _cfg()
        carcass, six_mm, _, _ = _raw_panels_for_cabinet(cfg, None)
        top = _panel(carcass, "top")
        back = _panel(six_mm, "back")
        assert cfg.back_style == "full_height"
        assert top.width == pytest.approx(457 - 6)      # depth − back_thickness
        assert back.length == pytest.approx(663.6)      # full height

    def test_under_top_full_depth_top_short_back(self):
        cfg = _cfg(back_style="under_top")
        carcass, six_mm, _, _ = _raw_panels_for_cabinet(cfg, None)
        top = _panel(carcass, "top")
        bottom = _panel(carcass, "bottom")
        back = _panel(six_mm, "back")
        assert top.width == pytest.approx(457)          # full depth
        assert "caps the back" in top.notes
        assert bottom.width == pytest.approx(457 - 6)   # bottom unchanged
        assert back.length == pytest.approx(663.6 - 18)  # height − top_t
        assert "stops under" in back.notes

    def test_under_top_respects_hardwood_band_core_shrink(self):
        cfg = _cfg(back_style="under_top", edge_band_mode="hardwood",
                   edge_band_thickness_mm=3.2)
        carcass, _, _, _ = _raw_panels_for_cabinet(cfg, None)
        top = _panel(carcass, "top")
        assert top.width == pytest.approx(457 - 3.2)    # full depth core

    def test_miter_ignores_under_top_panel_change(self):
        # Invalid combo — the evaluator errors; the panel builder must not
        # half-apply the cap on top of the long-point miter convention.
        cfg = _cfg(back_style="under_top", carcass_corner_style="miter")
        carcass, six_mm, _, _ = _raw_panels_for_cabinet(cfg, None)
        assert _panel(carcass, "top").width == pytest.approx(457 - 6)
        assert _panel(six_mm, "back").length == pytest.approx(663.6)


class TestEvaluator:
    def test_full_height_silent(self):
        assert check_back_style(_cfg()) == []

    def test_under_top_valid_combo_silent(self):
        assert check_back_style(_cfg(back_style="under_top")) == []

    def test_unknown_style_errors(self):
        issues = check_back_style(_cfg(back_style="floating"))
        assert len(issues) == 1
        assert "Unknown back_style" in issues[0].message

    def test_under_top_with_miter_errors(self):
        issues = check_back_style(
            _cfg(back_style="under_top", carcass_corner_style="miter"))
        assert any("butt corners" in i.message for i in issues)

    def test_under_top_with_dado_rabbet_errors(self):
        issues = check_back_style(
            _cfg(back_style="under_top",
                 carcass_joinery=CarcassJoinery.DADO_RABBET))
        assert any("side rabbets" in i.message for i in issues)

    def test_wired_into_evaluate_cabinet(self):
        issues = evaluate_cabinet(
            _cfg(back_style="under_top",
                 carcass_joinery=CarcassJoinery.DADO_RABBET))
        assert any(i.check == "back_style" for i in issues)


class TestSharedToken:
    def test_shared_back_style_propagates(self):
        project = CabinetProject(
            name="t",
            shared=SharedDesign(back_style="under_top"),
            cabinets=(ProjectCabinet(name="a", config=_cfg()),),
        )
        (_, resolved), = project.resolved()
        assert resolved.back_style == "under_top"

    def test_child_override_wins(self):
        project = CabinetProject(
            name="t",
            shared=SharedDesign(back_style="under_top"),
            cabinets=(ProjectCabinet(
                name="a", config=_cfg(back_style="full_height"),
                overrides=frozenset({"back_style"})),),
        )
        (_, resolved), = project.resolved()
        assert resolved.back_style == "full_height"

    def test_build_cabinet_config_accepts_flat_key(self):
        cfg = build_cabinet_config(
            {"width": 600, "height": 720, "depth": 550,
             "back_style": "under_top"})
        assert cfg.back_style == "under_top"


class TestAssemblyDoc:
    def test_under_top_map_and_steps(self):
        plan = build_assembly_plan(_cfg(back_style="under_top"))
        top_map = next(p for p in plan.panels if p.panel == "top")
        bottom_map = next(p for p in plan.panels if p.panel == "bottom")
        assert top_map.draw_height == pytest.approx(457)
        assert "caps the back" in top_map.note
        assert bottom_map.draw_height == pytest.approx(457 - 6)
        text = _doc_text(plan)
        assert "underside of the full-depth top" in text
        assert "rabbet" not in text

    def test_full_height_steps_describe_the_rear_pocket(self):
        plan = build_assembly_plan(_cfg())
        top_map = next(p for p in plan.panels if p.panel == "top")
        assert top_map.draw_height == pytest.approx(457 - 6)
        text = _doc_text(plan)
        assert "test-fit the back panel in its rear pocket" in text
        assert "drop the back panel into the rear pocket" in text
        # Assembly plans are floating-tenon (butt) only, so no carcass this
        # doc covers has a back rabbet to seat the panel in.
        assert "rabbet" not in text

    def test_back_glue_edges_name_only_the_parts_present(self):
        """A plain box has no dividers or fixed shelves to glue the back to."""
        plain = build_assembly_plan(_cfg())
        plain_text = _doc_text(plain)
        assert plain_text.count("rear edges of the top and bottom") == 2
        assert "rear edges of the top, bottom, and dividers" not in plain_text

        shelved = build_assembly_plan(_cfg(fixed_shelf_positions=(300.0,)))
        shelved_text = _doc_text(shelved)
        assert "rear edges of the top, bottom, and fixed shelves" in shelved_text

    def test_under_top_back_glue_edges_exclude_the_capping_top(self):
        """under_top: the top is the back's landing face, not a glue edge."""
        plan = build_assembly_plan(_cfg(back_style="under_top",
                                        fixed_shelf_positions=(300.0,)))
        text = _doc_text(plan)
        assert "rear edges of the bottom and fixed shelves" in text
