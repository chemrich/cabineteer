"""Regressions for the 2026-07-29 review's cutlist/banding findings
(M3, M5, M6 + minors — docs/code-review-2026-07-29.md)."""

import asyncio
import json
from pathlib import Path

import pytest

from cabineteer.cutlist import (
    CutlistPanel,
    assign_part_ids,
    consolidate_bom,
    generate_sheet_layout_html,
    optimize_cutlist,
    pack_band_pieces,
    SheetStock,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def home(tmp_path, monkeypatch):
    from cabineteer import project as pmod
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(pmod, "project_dir", lambda: tmp_path / "projects")
    return tmp_path


class TestPlacementPartIds:
    """M5: rows differing only by edge_band collapsed to one ID in the
    graphics — part_id is now stamped on every Placement at expansion."""

    def _panels(self):
        panels = consolidate_bom([
            CutlistPanel(name="side", length=720, width=450,
                         thickness=18, edge_band=["front"]),
            CutlistPanel(name="side", length=720, width=450,
                         thickness=18, edge_band=["front"]),
            CutlistPanel(name="side", length=720, width=450, thickness=18),
            CutlistPanel(name="side", length=720, width=450, thickness=18),
            CutlistPanel(name="top", length=764, width=450, thickness=18),
        ])
        assign_part_ids(panels)
        return panels

    @pytest.mark.parametrize("algorithm", ["strip", "opcut", "rips_first"])
    def test_placements_carry_row_ids(self, algorithm):
        if algorithm == "opcut":
            # Lite CI has no opcut — the pure-Python algorithms still cover
            # the part_id plumbing there (rectpack is covered implicitly by
            # the same expansion code path).
            pytest.importorskip("opcut")
        panels = self._panels()
        opt = optimize_cutlist(
            panels, stock_sheet=SheetStock("s", 2440, 1220, 18),
            algorithm=algorithm)
        ids = sorted(p.part_id for p in opt.placements)
        # 2 banded sides (S1), 2 unbanded (S2), 1 top (T1) — all present.
        assert ids.count("S1") == 2, ids
        assert ids.count("S2") == 2, ids
        assert ids.count("T1") == 1, ids

    def test_html_labels_show_both_side_ids(self):
        panels = self._panels()
        opt = optimize_cutlist(
            panels, stock_sheet=SheetStock("s", 2440, 1220, 18),
            algorithm="strip")
        html = generate_sheet_layout_html([("18mm", panels, opt)])
        assert html.count("S1 ·") == 2
        assert html.count("S2 ·") == 2


class TestBandStockGuards:
    def test_strip_wider_than_board_raises(self):
        # minor 1: used to order 0 boards and crash to_banding_csv with
        # ZeroDivisionError mid-write.
        with pytest.raises(ValueError, match="cannot yield"):
            pack_band_pieces(
                [{"length": 300.0, "part": "", "panel": "side"}],
                {"width_mm": 89.0, "length_mm": 1219.2,
                 "price_usd": 10.0, "strip_width_mm": 100.0})

    def test_offal_accounts_for_final_kerf(self):
        # minor 2: 89 mm board / 20 mm strips / 3.2 kerf → 3 strips consume
        # 3 kerfs when an offcut remains: 89 − 60 − 9.6 = 19.4, not 22.6.
        from cabineteer.cutlist import generate_banding_cutlist_html
        panels = [CutlistPanel(name="side", length=500, width=450,
                               thickness=18, edge_band=["front"])]

        class _Cfg:
            edge_band_mode = "hardwood"
            edge_band_thickness_mm = 3.2
            edge_band_material = "white_oak"
            edge_band_stock = {"width_mm": 89.0, "length_mm": 1219.2,
                               "price_usd": 10.0, "strip_width_mm": 20.0}

        html = generate_banding_cutlist_html(panels, _Cfg(), "t")
        assert "19.4 mm offal" in html
        assert "22.6" not in html


class TestBandingDocScope:
    """M3: a mixed project (hardwood-with-stock + hot-melt) must not chop-
    plan the hot-melt cabinet's edges as hardwood board stock."""

    def test_hot_melt_panels_excluded(self, home):
        from cabineteer.server import (
            _tool_design_project, _tool_generate_project_cutlist,
        )
        stock = {"width_mm": 89.0, "length_mm": 1219.2, "price_usd": 10.0,
                 "strip_width_mm": 20.0}
        base = {"height": 720, "depth": 450,
                "drawer_config": [[300, "drawer"], [384, "drawer"]]}
        _run(_tool_design_project({
            "name": "test_bandscope",
            "cabinets": [
                {"name": "hw", "config": dict(
                    base, width=600, edge_band_mode="hardwood",
                    edge_band_thickness_mm=3.2,
                    edge_band_material="white_oak",
                    edge_band_stock=stock)},
                {"name": "hm", "config": dict(
                    base, width=800, edge_band_mode="hot_melt",
                    edge_band_thickness_mm=0.6,
                    edge_band_material="white_oak")},
            ]}))
        res = _run(_tool_generate_project_cutlist(
            {"project_name": "test_bandscope"}))
        payload = json.loads(res[0].text)
        csv_path = payload["files"]["banding_cutlist_csv"]
        csv_text = Path(csv_path).read_text(encoding="utf-8")
        # The hot-melt cabinet is 800 wide → its top/bottom front edges are
        # 764 mm; the hardwood cabinet's are 564 mm. Only the latter may
        # appear in the chop plan.
        assert "564" in csv_text
        assert "764" not in csv_text
        # Both hardware band lines still exist (roll + boards).
        hw = payload["hardware_bom"]
        cats = [l for l in hw if l["category"] == "edge_band"]
        assert len(cats) == 2

    def test_banding_doc_ids_match_layout(self, home):
        from cabineteer.server import (
            _tool_design_project, _tool_generate_project_cutlist,
        )
        stock = {"width_mm": 89.0, "length_mm": 1219.2, "price_usd": 10.0,
                 "strip_width_mm": 20.0}
        _run(_tool_design_project({
            "name": "test_bandids",
            "cabinets": [{"name": "hw", "config": {
                "width": 600, "height": 720, "depth": 450,
                "drawer_config": [[300, "drawer"], [384, "drawer"]],
                "edge_band_mode": "hardwood",
                "edge_band_thickness_mm": 3.2,
                "edge_band_material": "white_oak",
                "edge_band_stock": stock}}]}))
        res = _run(_tool_generate_project_cutlist(
            {"project_name": "test_bandids"}))
        payload = json.loads(res[0].text)
        csv_text = Path(payload["files"]["banding_cutlist_csv"]).read_text(encoding="utf-8")
        layout_ids = {p["id"] for p in payload["panels_summary"] if p["id"]}
        assert any(pid in csv_text for pid in layout_ids)


class TestPipelineCosmetics:
    def test_optimization_note_names_rips_first(self, home):
        from cabineteer.server import _tool_generate_cutlist
        res = _run(_tool_generate_cutlist({
            "name": "test_note", "width": 800, "height": 720, "depth": 450,
            "drawer_config": [[300, "drawer"], [384, "drawer"]],
            "optimizer": "rips_first"}))
        payload = json.loads(res[0].text)
        assert "rips_first" in payload["optimization_note"]
        assert "opcut" not in payload["optimization_note"]

    def test_json_stock_reflects_override(self, home):
        from cabineteer.server import _tool_generate_cutlist
        res = _run(_tool_generate_cutlist({
            "name": "test_stockov", "width": 800, "height": 720,
            "depth": 450, "drawer_config": [[300, "drawer"]],
            "sheet_size_overrides": {"baltic_birch": [2453, 1234]}}))
        payload = json.loads(res[0].text)
        cutlist = json.loads(
            Path(payload["files"]["json"]).read_text(encoding="utf-8"))
        lengths = {s["length"] for s in cutlist["stock"]}
        assert 2453.0 in lengths
        assert 2440.0 not in lengths

    def test_string_override_rejected(self):
        from cabineteer.server import _parse_sheet_size_overrides
        with pytest.raises(ValueError, match="must be"):
            _parse_sheet_size_overrides({"baltic_birch": "2453x1234"})
