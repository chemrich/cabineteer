"""edge_band_stock: strip packing, priced band BOM lines, interference checks.

Feature request (Charlie, Jul 2026): account for edge banding — hot-melt or
1/8" / 1/4" hardwood strips — with a realistic view of how piece lengths pack
into purchasable banding stock (1/8" or 1/4" boards, 3–5.5" wide, 48" long),
and make extra sure banding never causes drawer/door-to-carcass interference.
"""

import asyncio
import json

import pytest

from cabineteer.cabinet import (
    CabinetConfig,
    build_cabinet_config,
    normalize_band_stock,
)
from cabineteer.cutlist import (
    BAND_PROUD_ALLOWANCE_MM,
    CutlistPanel,
    HardwareLine,
    band_segments_for_panels,
    edge_band_lines_for_panels,
    pack_band_strips,
)
from cabineteer.evaluation import (
    Severity,
    check_door_overlay_collisions,
    check_edge_band_face_gap,
    check_edge_banding,
)


def _await(coro):
    # Match the repo's loop convention (asyncio.run() would close the thread
    # loop and break later test files).
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


STOCK_55 = {"width_mm": 139.7, "length_mm": 1219.2, "price_usd": 52}


def _cfg(**kw):
    base = dict(width=600, height=720, depth=500,
                edge_band_mode="hardwood", edge_band_thickness_mm=3.2,
                edge_band_material="white_oak")
    base.update(kw)
    return CabinetConfig(**base)


class TestNormalizeBandStock:
    def test_defaults_and_coercion(self):
        out = normalize_band_stock({"width_mm": "139.7", "length_mm": 1219.2,
                                    "price_usd": 52})
        assert out == {"width_mm": 139.7, "length_mm": 1219.2,
                       "price_usd": 52.0, "strip_width_mm": 20.0}

    def test_none_and_empty_stay_none(self):
        assert normalize_band_stock(None) is None
        assert normalize_band_stock({}) is None

    def test_unknown_key_rejected(self):
        with pytest.raises(ValueError, match="unknown key"):
            normalize_band_stock({"width_mm": 100, "length_mm": 1219,
                                  "price_usd": 52, "thickness_mm": 6.4})

    def test_missing_and_non_numeric_rejected(self):
        with pytest.raises(ValueError, match="numeric"):
            normalize_band_stock({"width_mm": 100})
        with pytest.raises(ValueError, match="numeric"):
            normalize_band_stock({"width_mm": "wide", "length_mm": 1219,
                                  "price_usd": 52})

    def test_non_positive_rejected(self):
        with pytest.raises(ValueError, match="positive"):
            normalize_band_stock({"width_mm": 0, "length_mm": 1219,
                                  "price_usd": 52})

    def test_config_normalizes_at_construction(self):
        cfg = _cfg(edge_band_stock={"width_mm": 139.7, "length_mm": 1219.2,
                                    "price_usd": 52})
        assert cfg.edge_band_stock["strip_width_mm"] == 20.0
        with pytest.raises(ValueError):
            _cfg(edge_band_stock={"width_mm": -1, "length_mm": 1,
                                  "price_usd": 1})


class TestPackBandStrips:
    def test_strips_per_board_by_width(self):
        # 20 mm strips, 3.2 kerf: 3" board → 3, 4" → 4, 5.5" → 6.
        for width, expect in ((76.2, 3), (101.6, 4), (139.7, 6)):
            stock = {"width_mm": width, "length_mm": 1219.2,
                     "price_usd": 42, "strip_width_mm": 20.0}
            assert pack_band_strips([100.0], stock)["strips_per_board"] == expect

    def test_ffd_packs_short_pieces_together(self):
        stock = dict(STOCK_55, strip_width_mm=20.0)
        # 10 × 300 mm (+10 allowance each): 3 pieces fit a 1219.2 strip
        # (930 + 289.2 rem), so 10 pieces → 4 strips (3+3+3+1).
        pack = pack_band_strips([300.0] * 10, stock)
        assert pack["strips"] == 4
        assert pack["boards"] == 1
        assert pack["spare_strips"] == 2

    def test_board_count_and_spares(self):
        stock = dict(STOCK_55, strip_width_mm=20.0)
        # 43 one-per-strip pieces at 6 strips/board → 8 boards, 5 spare
        # (the dining-sideboards v2-hardwood numbers).
        pack = pack_band_strips([1200.0] * 43, stock)
        assert pack["strips"] == 43
        assert pack["boards"] == 8
        assert pack["spare_strips"] == 5

    def test_flush_and_over_length_flags(self):
        stock = dict(STOCK_55, strip_width_mm=20.0)
        pack = pack_band_strips([1219.0, 1219.0, 1400.0, 300.0], stock)
        assert pack["flush_pieces"] == [1219.0, 1219.0]
        assert pack["over_length_pieces"] == [1400.0]
        # over-length pieces are excluded from the strip count; each flush
        # piece consumes a whole strip, the 300 needs its own
        assert pack["strips"] == 3

    def test_exact_fit_piece_consumes_whole_strip(self):
        stock = dict(STOCK_55, strip_width_mm=20.0)
        pack = pack_band_strips([1219.2 - BAND_PROUD_ALLOWANCE_MM / 2], stock)
        assert pack["strips"] == 1
        assert pack["flush_pieces"]  # inside the no-overhang window


class TestBandSegments:
    def test_left_right_markers_use_width(self):
        # Regression: left/right edges run along the panel WIDTH; the old
        # footage loop charged p.length for every non-"all" marker.
        cfg = _cfg()
        p = CutlistPanel(name="divider", length=500.0, width=200.0,
                         thickness=18, material="baltic_birch",
                         edge_band=["front", "left", "right"])
        segs = band_segments_for_panels([p], cfg)
        (mat, lengths), = segs.items()
        assert sorted(lengths) == [200.0, 200.0, 500.0]

    def test_all_marker_is_perimeter_and_quantity_multiplies(self):
        cfg = _cfg()
        p = CutlistPanel(name="door", length=600.0, width=300.0, thickness=18,
                         quantity=2, material="baltic_birch",
                         edge_band=["all"])
        (_, lengths), = band_segments_for_panels([p], cfg).items()
        assert len(lengths) == 8
        assert sum(lengths) == 2 * 2 * (600.0 + 300.0)


class TestBandLine:
    def _panels(self):
        return [
            CutlistPanel(name="top", length=1000.0, width=400.0, thickness=18,
                         quantity=2, material="baltic_birch",
                         edge_band=["front"]),
            CutlistPanel(name="door", length=500.0, width=280.0, thickness=18,
                         material="baltic_birch", edge_band=["all"]),
        ]

    def test_priced_line_from_stock(self):
        cfg = _cfg(edge_band_stock=STOCK_55)
        line, = edge_band_lines_for_panels(self._panels(), cfg)
        assert line.category == "edge_band"
        assert line.unit_price == 52.0
        assert line.pieces_needed >= 1          # boards to order
        assert "boards" in line.name
        assert "strips" in line.notes
        # note chunks avoid ", " so merge-time dedup keeps them whole
        assert ", " not in line.notes

    def test_unpriced_line_without_stock(self):
        cfg = _cfg()
        line, = edge_band_lines_for_panels(self._panels(), cfg)
        assert line.unit_price_usd is None
        assert "rip" in line.notes
        assert line.unit_price == 0.0           # no PRICE_LIST entry

    def test_stock_ignored_for_hot_melt(self):
        cfg = _cfg(edge_band_mode="hot_melt", edge_band_thickness_mm=0.6,
                   edge_band_stock=STOCK_55)
        line, = edge_band_lines_for_panels(self._panels(), cfg)
        assert "Iron-on" in line.name
        assert line.unit_price_usd is None

    def test_over_length_flag_in_notes(self):
        cfg = _cfg(edge_band_stock=STOCK_55)
        panels = [CutlistPanel(name="long", length=1500.0, width=300.0,
                               thickness=18, material="baltic_birch",
                               edge_band=["front"])]
        line, = edge_band_lines_for_panels(panels, cfg)
        assert "LONGER" in line.notes and "splice" in line.notes

    def test_unit_price_falls_back_to_catalog(self):
        h = HardwareLine(sku="edgeband-hotmelt-white_oak",
                         category="edge_band", name="x", brand="",
                         model_number="", pieces_needed=1)
        assert h.unit_price == 14.80
        h2 = HardwareLine(sku="edgeband-hotmelt-white_oak",
                          category="edge_band", name="x", brand="",
                          model_number="", pieces_needed=1,
                          unit_price_usd=52.0)
        assert h2.unit_price == 52.0


class TestStockSpecEvaluator:
    def test_stock_with_wrong_mode_warns(self):
        cfg = _cfg(edge_band_mode="hot_melt", edge_band_thickness_mm=0.6,
                   edge_band_stock=STOCK_55)
        assert any(i.check == "edge_band_stock" and "ignored" in i.message
                   for i in check_edge_banding(cfg))

    def test_odd_thickness_warns(self):
        cfg = _cfg(edge_band_thickness_mm=5.0, edge_band_stock=STOCK_55)
        assert any("1/8" in i.message and i.severity == Severity.WARNING
                   for i in check_edge_banding(cfg)
                   if i.check == "edge_band_stock")

    def test_eighth_and_quarter_thickness_clean(self):
        for thk in (3.2, 6.4):
            cfg = _cfg(edge_band_thickness_mm=thk, edge_band_stock=STOCK_55,
                       carcass_material="baltic_birch",
                       face_material="baltic_birch")
            assert not [i for i in check_edge_banding(cfg)
                        if i.check == "edge_band_stock"]

    def test_width_outside_envelope_warns(self):
        cfg = _cfg(edge_band_stock={"width_mm": 300, "length_mm": 1219.2,
                                    "price_usd": 52})
        assert any("envelope" in i.message for i in check_edge_banding(cfg)
                   if i.check == "edge_band_stock")

    def test_narrow_strip_errors(self):
        cfg = _cfg(edge_band_stock=dict(STOCK_55, strip_width_mm=15.0))
        issues = [i for i in check_edge_banding(cfg)
                  if i.check == "edge_band_stock"]
        assert any(i.severity == Severity.ERROR and "cover" in i.message
                   for i in issues)

    def test_no_proud_margin_warns(self):
        cfg = _cfg(edge_band_stock=dict(STOCK_55, strip_width_mm=18.5))
        assert any(i.severity == Severity.WARNING and "proud" in i.message
                   for i in check_edge_banding(cfg)
                   if i.check == "edge_band_stock")

    def test_long_miter_edge_vs_stock_length_warns(self):
        cfg = _cfg(width=1250, carcass_corner_style="miter",
                   carcass_joinery="floating_tenon",
                   edge_band_stock=STOCK_55)
        assert any("splice" in i.message for i in check_edge_banding(cfg)
                   if i.check == "edge_band_stock")
        # same width butt carcass: interior 1214 < 1219.2 but within the
        # no-overhang window → still warned, softer message
        cfg2 = _cfg(width=1250, edge_band_stock=STOCK_55)
        assert any("overhang" in i.message for i in check_edge_banding(cfg2)
                   if i.check == "edge_band_stock")


class TestBandingInterference:
    STACK = [[300, "drawer"], [384, "drawer"]]

    def test_hot_melt_default_thickness_clean(self):
        cfg = CabinetConfig(width=600, height=720, depth=500,
                            edge_band_mode="hot_melt",
                            openings=self.STACK)
        assert check_edge_band_face_gap(cfg) == []

    def test_hot_melt_thick_band_warns_then_errors(self):
        warn = CabinetConfig(width=600, height=720, depth=500,
                             edge_band_mode="hot_melt",
                             edge_band_thickness_mm=1.5,
                             openings=self.STACK)
        issues = check_edge_band_face_gap(warn)
        assert [i.severity for i in issues] == [Severity.WARNING]
        err = CabinetConfig(width=600, height=720, depth=500,
                            edge_band_mode="hot_melt",
                            edge_band_thickness_mm=2.5,
                            openings=self.STACK)
        issues = check_edge_band_face_gap(err)
        assert [i.severity for i in issues] == [Severity.ERROR]
        assert "COLLIDE" in issues[0].message

    def test_hardwood_is_dimension_neutral(self):
        cfg = CabinetConfig(width=600, height=720, depth=500,
                            edge_band_mode="hardwood",
                            edge_band_thickness_mm=6.4,
                            openings=self.STACK)
        assert check_edge_band_face_gap(cfg) == []

    def test_no_adjacent_faces_no_issue(self):
        cfg = CabinetConfig(width=600, height=720, depth=500,
                            edge_band_mode="hot_melt",
                            edge_band_thickness_mm=2.5,
                            openings=[[300, "drawer"], [384, "open"]])
        assert check_edge_band_face_gap(cfg) == []

    def _door_beside_drawers(self, **band):
        return build_cabinet_config(dict(
            width=614, height=720, depth=500,
            door_hinge="blum_clip_top_blumotion_110_half",
            columns=[
                {"width_mm": 280, "openings": [[646, "door"]]},
                {"width_mm": 280,
                 "openings": [[321, "drawer"], [321, "drawer"]]},
            ], **band))

    def test_hot_melt_growth_tips_overlay_into_collision(self):
        # Half-overlay door beside a drawer column: 0.5 mm reveal without
        # banding (warning). Hot-melt adds 0.6 mm on the door edge and
        # 0.6 mm on the neighbour faces → 1.2 mm over budget → ERROR.
        base = self._door_beside_drawers()
        no_band = check_door_overlay_collisions(base)
        assert [i.severity for i in no_band] == [Severity.WARNING]
        assert "banding" not in no_band[0].message

        banded = self._door_beside_drawers(edge_band_mode="hot_melt",
                                           edge_band_thickness_mm=0.6)
        with_band = check_door_overlay_collisions(banded)
        assert [i.severity for i in with_band] == [Severity.ERROR]
        assert "hot-melt banding growth" in with_band[0].message

    def test_hardwood_does_not_change_overlay_math(self):
        hard = self._door_beside_drawers(edge_band_mode="hardwood",
                                         edge_band_thickness_mm=6.4)
        issues = check_door_overlay_collisions(hard)
        assert [i.severity for i in issues] == [Severity.WARNING]
        assert "banding" not in issues[0].message


class TestBandingCutlistDoc:
    STOCK = dict(STOCK_55, strip_width_mm=20.0)

    def _pieces(self):
        return [
            {"part": "T1", "panel": "top", "edge": "front edge",
             "length": 1219.0, "material": "white_oak"},
            {"part": "D1", "panel": "door", "edge": "long edge",
             "length": 500.0, "material": "white_oak"},
            {"part": "D1", "panel": "door", "edge": "short edge",
             "length": 280.0, "material": "white_oak"},
        ]

    def test_pack_band_pieces_assignments(self):
        from cabineteer.cutlist import pack_band_pieces
        pack = pack_band_pieces(self._pieces(), self.STOCK)
        placed = [pc for st in pack["strips"] for pc in st["pieces"]]
        assert len(placed) == 3
        # dead-length piece owns its strip and is cut AT finished size
        dead = [pc for pc in placed if pc["dead_length"]]
        assert len(dead) == 1 and dead[0]["cut"] == 1219.0
        dead_strip, = [st for st in pack["strips"] if dead[0] in st["pieces"]]
        assert len(dead_strip["pieces"]) == 1
        # normal pieces cut proud
        norm = [pc for pc in placed if not pc["dead_length"]]
        assert all(pc["cut"] == pc["length"] + BAND_PROUD_ALLOWANCE_MM
                   for pc in norm)

    def test_band_pieces_provenance_uses_part_ids(self):
        from cabineteer.cutlist import (assign_part_ids,
                                                band_pieces_for_panels)
        cfg = _cfg(edge_band_stock=STOCK_55)
        panels = [CutlistPanel(name="top", length=800.0, width=400.0,
                               thickness=18, material="baltic_birch",
                               edge_band=["front"]),
                  CutlistPanel(name="door", length=500.0, width=280.0,
                               thickness=18, material="baltic_birch",
                               edge_band=["all"])]
        assign_part_ids(panels)
        pieces = band_pieces_for_panels(panels, cfg)
        assert len(pieces) == 5
        assert {pc["part"] for pc in pieces} == {"T1", "DR1"}
        door = [pc for pc in pieces if pc["panel"] == "door"]
        assert sorted(pc["edge"] for pc in door) == \
            ["long edge", "long edge", "short edge", "short edge"]

    def test_csv_and_html_render(self):
        from cabineteer.cutlist import (assign_part_ids,
                                                generate_banding_cutlist_html,
                                                to_banding_csv)
        cfg = _cfg(edge_band_stock=STOCK_55)
        panels = [CutlistPanel(name="top", length=1219.0, width=400.0,
                               thickness=18, quantity=2,
                               material="baltic_birch", edge_band=["front"]),
                  CutlistPanel(name="long", length=1400.0, width=300.0,
                               thickness=18, material="baltic_birch",
                               edge_band=["front"])]
        assign_part_ids(panels)
        csv_text = to_banding_csv(panels, cfg)
        assert "#1,S1,1" in csv_text
        assert "DEAD LENGTH" in csv_text
        assert "LONGER THAN STOCK" in csv_text
        html = generate_banding_cutlist_html(panels, cfg, "doc_test")
        assert "Board #1" in html and "Strip S1" in html
        assert "DEAD LENGTH" in html and "LONGER than the stock" in html
        assert "T1" in html
        # boards labeled '#N', never bare 'B1' (that's a part-ID family)
        assert "Board B1" not in html


class TestBandingSchedule:
    def _packs(self, cfg, panels):
        from cabineteer.cutlist import _band_packs_by_material
        return _band_packs_by_material(panels, cfg)

    def test_schedule_aggregates_qty_at_each_length(self):
        from cabineteer.cutlist import (assign_part_ids,
                                                band_length_schedule)
        cfg = _cfg(edge_band_stock=STOCK_55)
        panels = [CutlistPanel(name="top", length=1219.0, width=400.0,
                               thickness=18, quantity=3,
                               material="baltic_birch", edge_band=["front"]),
                  CutlistPanel(name="bottom", length=1219.0, width=400.0,
                               thickness=18, quantity=3,
                               material="baltic_birch", edge_band=["front"]),
                  CutlistPanel(name="side", length=663.6, width=450.0,
                               thickness=18, quantity=6,
                               material="baltic_birch", edge_band=["front"])]
        assign_part_ids(panels)
        sched = band_length_schedule(self._packs(cfg, panels))
        assert len(sched) == 2                       # two distinct lengths
        long_row, short_row = sched
        assert long_row["qty"] == 6 and long_row["dead"]
        assert long_row["parts"] == ["B1", "T1"]     # merged across parts
        assert short_row["qty"] == 6
        assert short_row["cut"] == 663.6 + BAND_PROUD_ALLOWANCE_MM
        # longest-first within a material
        assert long_row["length"] > short_row["length"]

    def test_schedule_includes_over_length_rows(self):
        from cabineteer.cutlist import band_length_schedule
        cfg = _cfg(edge_band_stock=STOCK_55)
        panels = [CutlistPanel(name="long", length=1400.0, width=300.0,
                               thickness=18, material="baltic_birch",
                               edge_band=["front"])]
        sched = band_length_schedule(self._packs(cfg, panels))
        assert len(sched) == 1 and sched[0]["over"]

    def test_corner_notes_by_style(self):
        from cabineteer.cutlist import (_band_corner_notes,
                                                band_length_schedule)
        panels = [CutlistPanel(name="top", length=800.0, width=400.0,
                               thickness=18, material="baltic_birch",
                               edge_band=["front"]),
                  CutlistPanel(name="door", length=500.0, width=280.0,
                               thickness=18, material="baltic_birch",
                               edge_band=["all"])]
        miter_cfg = _cfg(edge_band_stock=STOCK_55,
                         carcass_corner_style="miter",
                         carcass_joinery="floating_tenon")
        sched = band_length_schedule(self._packs(miter_cfg, panels))
        notes = _band_corner_notes(miter_cfg, sched)
        assert any("45° seam" in n for n in notes)
        assert any("OVERLAP" in n and "SHORT edges first" in n
                   for n in notes)
        butt_cfg = _cfg(edge_band_stock=STOCK_55)
        notes = _band_corner_notes(butt_cfg, sched)
        assert any("run THROUGH" in n for n in notes)

    def test_html_leads_with_schedule_and_corners(self):
        from cabineteer.cutlist import (assign_part_ids,
                                                generate_banding_cutlist_html)
        cfg = _cfg(edge_band_stock=STOCK_55, carcass_corner_style="miter",
                   carcass_joinery="floating_tenon")
        panels = [CutlistPanel(name="top", length=1219.0, width=400.0,
                               thickness=18, quantity=2,
                               material="baltic_birch", edge_band=["front"])]
        assign_part_ids(panels)
        html = generate_banding_cutlist_html(panels, cfg, "sched_test")
        assert "fence at" in html and "kerf assumed" in html
        assert "Length schedule" in html and "2×" in html
        assert "Corners" in html and "45° seam" in html
        # schedule and corners come BEFORE the chop-plan appendix
        assert html.index("Length schedule") < html.index("Appendix")
        assert html.index("Corners") < html.index("Appendix")

    def test_pdf_renders(self):
        pytest.importorskip("reportlab")
        from cabineteer.cutlist import (assign_part_ids,
                                                generate_banding_cutlist_pdf)
        cfg = _cfg(edge_band_stock=STOCK_55)
        panels = [CutlistPanel(name="top", length=1219.0, width=400.0,
                               thickness=18, quantity=2,
                               material="baltic_birch", edge_band=["front"]),
                  CutlistPanel(name="door", length=500.0, width=280.0,
                               thickness=18, material="baltic_birch",
                               edge_band=["all"])]
        assign_part_ids(panels)
        pdf = generate_banding_cutlist_pdf(panels, cfg, "pdf_test")
        assert pdf[:5] == b"%PDF-"
        assert len(pdf) > 1500


class TestProjectIntegration:
    def test_token_round_trip_and_aggregation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from cabineteer.server import (
            _tool_design_project, _tool_generate_project_cutlist,
            _tool_update_project)

        proj = {
            "name": "eval_band_stock", "overwrite": True,
            "shared": {
                "edge_band_mode": "hardwood",
                "edge_band_thickness_mm": 3.2,
                "edge_band_material": "white_oak",
                "edge_band_stock": STOCK_55,
                "face_material": "baltic_birch",
            },
            "cabinets": [
                {"name": n, "config": {
                    "width": 600, "height": 720, "depth": 500,
                    "openings": [[300, "drawer"], [384, "drawer"]]}}
                for n in ("left", "right")
            ],
        }
        d = json.loads(_await(_tool_design_project(proj))[0].text)
        assert d["cabinet_count"] == 2

        cut = json.loads(_await(_tool_generate_project_cutlist(
            {"project_name": "eval_band_stock"}))[0].text)
        # banding cutlist files emitted alongside the standard set
        assert "banding_cutlist_html" in cut["files"]
        assert "banding_cutlist_csv" in cut["files"]
        doc = open(cut["files"]["banding_cutlist_html"], encoding="utf-8").read()
        assert "Board #1" in doc and "Strip S1" in doc

        bom = json.load(open(cut["files"]["hardware_bom_json"], encoding="utf-8"))
        band = [l for l in bom["lines"] if l["category"] == "edge_band"]
        # ONE aggregated line across both cabinets, priced from the spec
        assert len(band) == 1
        assert band[0]["unit_price_usd"] == 52.0
        assert band[0]["pieces_needed"] >= 1
        assert band[0]["line_total_usd"] == \
            band[0]["pieces_needed"] * 52.0
        assert cut["cost_estimate"]["hardware_by_category_usd"][
            "edge_band"] == band[0]["line_total_usd"]

        # null clears the token → back to the unpriced line
        _await(_tool_update_project({
            "name": "eval_band_stock",
            "shared": {"edge_band_stock": None}}))
        cut2 = json.loads(_await(_tool_generate_project_cutlist(
            {"project_name": "eval_band_stock"}))[0].text)
        bom2 = json.load(open(cut2["files"]["hardware_bom_json"], encoding="utf-8"))
        band2 = [l for l in bom2["lines"] if l["category"] == "edge_band"]
        assert len(band2) == 1
        assert band2[0]["unit_price_usd"] == 0.0
        # no stock spec → no banding cutlist files
        assert "banding_cutlist_html" not in cut2["files"]
