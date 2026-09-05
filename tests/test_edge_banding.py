"""Tests for edge banding — tokens, core-shrink cut math, BOM lines, checks."""

import pytest

from cabineteer.cabinet import CabinetConfig, ColumnConfig
from cabineteer.cutlist import edge_band_lines_for_panels
from cabineteer.evaluation import check_edge_banding
from cabineteer.server import _raw_panels_for_cabinet


def _cfg(**kw) -> CabinetConfig:
    # This module is about edge banding, not the show-face top/bottom
    # style — pin plain/plain (the CabinetConfig default is now "cap" /
    # "flush") so a top_front_cap row and an unbanded top panel don't
    # change the counts and footage these tests hand-check.
    kw.setdefault("face_top_style", "plain")
    kw.setdefault("face_bottom_style", "plain")
    return CabinetConfig(width=800, height=700, depth=457,
                         carcass_material="rift_white_oak_ply",
                         face_material="rift_white_oak_ply", **kw)


def _cols():
    return [{"width_mm": 373, "openings": [[300, "drawer"], [346, "drawer"]],
             "fixed_shelf_positions": []},
            {"width_mm": 373, "openings": [[646, "door"]],
             "fixed_shelf_positions": [320]}]


def _panels(cfg):
    c, b, x, f = _raw_panels_for_cabinet(cfg, _cols())
    return c, f


class TestCoreShrink:
    def test_none_mode_leaves_dims(self):
        c, f = _panels(_cfg())
        side = next(p for p in c if p.name == "side")
        assert side.width == 457

    def test_hot_melt_leaves_dims(self):
        c, f = _panels(_cfg(edge_band_mode="hot_melt"))
        side = next(p for p in c if p.name == "side")
        assert side.width == 457
        assert side.notes == ""

    def test_hardwood_shrinks_front_banded_cores(self):
        cfg = _cfg(edge_band_mode="hardwood", edge_band_thickness_mm=6.4)
        c, _ = _panels(cfg)
        side = next(p for p in c if p.name == "side")
        bottom = next(p for p in c if p.name == "bottom")
        divider = next(p for p in c if p.name == "column_divider")
        shelf = next(p for p in c if p.name == "shelf_1")
        assert side.width == pytest.approx(457 - 6.4)
        assert side.length == 700          # only the front edge is banded
        assert bottom.width == pytest.approx(457 - 6 - 6.4)
        assert divider.width == pytest.approx(457 - 6 - 6.4)
        assert shelf.width == pytest.approx(457 - 6 - 6.4)
        assert "finished" in side.notes

    def test_hardwood_shrinks_faces_both_axes(self):
        cfg = _cfg(edge_band_mode="hardwood", edge_band_thickness_mm=6.4)
        _, f = _panels(cfg)
        plain = _panels(_cfg())[1]
        for banded, orig in zip(f, plain):
            assert banded.length == pytest.approx(orig.length - 12.8, abs=0.1)
            assert banded.width == pytest.approx(orig.width - 12.8, abs=0.1)
            assert banded.edge_band == ["all"]
            assert "4 edges" in banded.notes

    def test_markers_stripped_without_banding(self):
        # Markers mean "this edge WILL be banded". With mode=none they are
        # stripped so band BOM lines, the banding cutlist doc, and CSV
        # markers can never claim banding on an unbanded build (the first
        # mixed batch swept kapex/kid panels into the banding doc).
        c, f = _panels(_cfg())
        assert all(p.edge_band == [] for p in c + f)

    def test_markers_present_with_banding(self):
        cfg = _cfg(edge_band_mode="hardwood", edge_band_thickness_mm=3.2)
        c, f = _panels(cfg)
        assert all(p.edge_band == ["front"] for p in c
                   if p.name in ("side", "bottom", "top", "column_divider",
                                 "shelf_1"))
        assert all(p.edge_band == ["all"] for p in f)


class TestBandBom:
    def test_none_mode_no_lines(self):
        c, f = _panels(_cfg())
        assert edge_band_lines_for_panels(c + f, _cfg()) == []

    def test_hot_melt_rolls_and_footage(self):
        cfg = _cfg(edge_band_mode="hot_melt")
        c, f = _panels(cfg)
        lines = edge_band_lines_for_panels(c + f, cfg)
        assert len(lines) == 1
        line = lines[0]
        assert line.sku == "edgeband-hotmelt-white_oak"
        assert line.pack_quantity == 50
        # Hand-check the footage: fronts (2 sides ×700 + top + bottom ×764
        # + divider ×664 + shelf ×373) + face perimeters, ×1.15 waste.
        mm = (2 * 700 + 2 * 764 + 664 + 373
              + sum(2 * (p.length + p.width) * p.quantity for p in f))
        import math
        assert line.pieces_needed == math.ceil(mm / 304.8 * 1.15)

    def test_hot_melt_priced(self):
        from cabineteer.hardware import price_for
        assert price_for("edgeband-hotmelt-white_oak") == 14.80
        assert price_for("edgeband-hotmelt-white_birch") == 15.50

    def test_hardwood_line_unpriced_rip_stock(self):
        from cabineteer.hardware import price_for
        cfg = _cfg(edge_band_mode="hardwood", edge_band_thickness_mm=6.4)
        c, f = _panels(cfg)
        lines = edge_band_lines_for_panels(c + f, cfg)
        assert len(lines) == 1
        assert lines[0].sku == "edgeband-hardwood-white_oak"
        assert "rip" in lines[0].notes
        assert price_for(lines[0].sku) == 0.0

    def test_explicit_material_override(self):
        cfg = _cfg(edge_band_mode="hot_melt", edge_band_material="walnut")
        c, f = _panels(cfg)
        lines = edge_band_lines_for_panels(c + f, cfg)
        assert lines[0].sku == "edgeband-hotmelt-walnut"

    def test_baltic_birch_maps_to_white_birch_roll(self):
        cfg = CabinetConfig(width=800, height=700, depth=457,
                            edge_band_mode="hot_melt")
        c, b, x, f = _raw_panels_for_cabinet(cfg, None)
        lines = edge_band_lines_for_panels(c + f, cfg)
        assert lines[0].sku == "edgeband-hotmelt-white_birch"


class TestEdgeBandingCheck:
    def test_none_is_silent(self):
        assert check_edge_banding(_cfg()) == []

    def test_unknown_mode_errors(self):
        issues = check_edge_banding(_cfg(edge_band_mode="tape"))
        assert any(i.severity.value == "error" for i in issues)

    def test_thick_hot_melt_warns(self):
        issues = check_edge_banding(
            _cfg(edge_band_mode="hot_melt", edge_band_thickness_mm=6.4))
        assert any("too thick" in i.message for i in issues)

    def test_thin_hardwood_warns(self):
        issues = check_edge_banding(
            _cfg(edge_band_mode="hardwood", edge_band_thickness_mm=0.6))
        assert any("thin for solid" in i.message for i in issues)

    def test_order_out_faces_warn(self):
        issues = check_edge_banding(CabinetConfig(
            width=800, height=700, depth=457, edge_band_mode="hot_melt",
            face_material="finished_wood"))
        assert any("order-out" in i.message for i in issues)

    def test_sheet_faces_valid_hardwood_silent(self):
        issues = check_edge_banding(
            _cfg(edge_band_mode="hardwood", edge_band_thickness_mm=6.4))
        assert issues == []


class TestSharedToken:
    def test_token_round_trips_and_merges(self):
        from cabineteer.project import (
            SharedDesign, _merge, _shared_to_dict, shared_from_dict,
        )
        shared = shared_from_dict({
            "edge_band_mode": "hardwood",
            "edge_band_thickness_mm": 6.4,
            "edge_band_material": "white_oak",
        })
        merged = _merge(CabinetConfig(width=800, height=700, depth=457),
                        shared, frozenset())
        assert merged.edge_band_mode == "hardwood"
        assert merged.edge_band_thickness_mm == 6.4
        assert merged.edge_band_material == "white_oak"
        assert _shared_to_dict(shared)["edge_band_mode"] == "hardwood"

    def test_unknown_token_still_rejected(self):
        from cabineteer.project import shared_from_dict
        with pytest.raises(ValueError, match="Unknown shared design token"):
            shared_from_dict({"edge_band_moed": "hardwood"})
