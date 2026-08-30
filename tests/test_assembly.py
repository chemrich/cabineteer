"""Tests for assembly.py — carcass assembly instructions — and the Domino
thickness rule they share with the hardware BOM."""

import pytest

from cabineteer.assembly import (
    DRY_FIT_TENON_URL,
    build_assembly_plan,
    generate_assembly_html,
)
from cabineteer.cabinet import CabinetConfig, CarcassJoinery, ColumnConfig
from cabineteer.joinery import (
    DOMINO_SIZES,
    carcass_domino_size_for_thickness,
)


def _box(**kw) -> CabinetConfig:
    return CabinetConfig(width=800, height=700, depth=457, **kw)


def _two_col(**kw) -> CabinetConfig:
    return CabinetConfig(
        width=800, height=700, depth=457,
        columns=[
            ColumnConfig(width_mm=373, openings=(),
                         fixed_shelf_positions=(320,)),
            ColumnConfig(width_mm=373, openings=()),
        ], **kw)


# ─── Thickness rule + catalog data ───────────────────────────────────────────


class TestDominoThicknessRule:
    def test_18mm_ply_uses_5x30(self):
        assert carcass_domino_size_for_thickness(18.0) == "5x30"

    def test_19mm_boundary_still_5x30(self):
        assert carcass_domino_size_for_thickness(19.0) == "5x30"

    def test_thicker_stock_uses_8x40(self):
        assert carcass_domino_size_for_thickness(19.1) == "8x40"
        assert carcass_domino_size_for_thickness(25.0) == "8x40"

    def test_5x30_part_number_is_494938(self):
        # 498889 was wrong; D 5x30/300 BU is 494938 (verified Jul 2026).
        assert DOMINO_SIZES["5x30"].part_number == "494938"

    def test_5x30_pack_priced(self):
        from cabineteer.hardware import price_for
        assert price_for("festool-494938") == 25.00


class TestJoineryBomFollowsRule:
    def _domino_line(self, cfg):
        from cabineteer.cutlist import (
            joinery_lines_for_cabinet_config,
        )
        lines = joinery_lines_for_cabinet_config(cfg, None)
        assert len(lines) == 1
        return lines[0]

    def test_18mm_carcass_orders_5x30(self):
        line = self._domino_line(_box())
        assert line.model_number == "494938"
        assert line.pack_quantity == 300
        assert "5×30" in line.name

    def test_25mm_carcass_orders_8x40(self):
        line = self._domino_line(_box(side_thickness=25.0))
        assert line.model_number == "493298"
        assert line.pack_quantity == 780


# ─── Plan construction ───────────────────────────────────────────────────────


class TestAssemblyPlan:
    def test_simple_box_has_four_joints(self):
        plan = build_assembly_plan(_box())
        assert [j.name for j in plan.joints] == [
            "bottom ↔ left side", "bottom ↔ right side",
            "top ↔ left side", "top ↔ right side"]

    def test_edge_vs_face_parts(self):
        plan = build_assembly_plan(_box())
        j = plan.joints[0]
        assert j.edge_part == "bottom"
        assert j.face_part == "left side"

    def test_two_column_census_matches_bom(self):
        # 4 top/bottom↔side + 2 divider + 2 column-shelf = 8 — the same
        # count the hardware BOM uses (4 + 2·dividers + 2·shelves).
        plan = build_assembly_plan(_two_col())
        assert len(plan.joints) == 8

    def test_positions_measured_from_front(self):
        plan = build_assembly_plan(_box())
        s = plan.size
        first = s.min_edge_distance + s.mortise_length / 2
        assert plan.positions[0] == pytest.approx(first, abs=0.1)
        assert plan.positions[-1] == pytest.approx(plan.span - first, abs=0.1)
        assert list(plan.positions) == sorted(plan.positions)

    def test_span_is_the_depth_of_the_panel_the_mortises_go_into(self):
        """Closure, not a restatement.

        ``assembly.py`` sets ``plan.span = cab_cfg.interior_depth``, so
        asserting those two are equal cannot fail — it re-types the
        implementation. There were two copies of that assertion. This one
        checks the span against the panel the joint is actually cut into,
        which is a fact about the furniture: mortises spaced across a span
        longer than the panel would run off its end.
        """
        from cabineteer.server import _raw_panels_for_cabinet
        cfg = _box()
        plan = build_assembly_plan(cfg)
        carcass, _thin, _box_p, _faces = _raw_panels_for_cabinet(cfg, None)
        bottom = next(p for p in carcass if p.name == "bottom")
        assert plan.span <= bottom.width + 0.01, (
            f"joints span {plan.span:g} mm across a bottom panel only "
            f"{bottom.width:g} mm deep")
        assert plan.span == pytest.approx(cfg.interior_depth)

    def test_18mm_plan_uses_5x30(self):
        plan = build_assembly_plan(_box())
        assert plan.size_key == "5x30"
        assert plan.size.part_number == "494938"

    def test_consumables_math(self):
        plan = build_assembly_plan(_two_col(), copies=3)
        assert plan.tenons_per_cabinet == plan.per_joint * 8
        assert plan.tenons_total == plan.tenons_per_cabinet * 3
        # PETG dry-fit prints cover one cabinet at a time.
        assert plan.dry_fit_tenons_needed == plan.tenons_per_cabinet

    def test_non_tenon_carcass_rejected(self):
        with pytest.raises(ValueError, match="floating-tenon"):
            build_assembly_plan(
                _box(carcass_joinery=CarcassJoinery.DADO_RABBET))

    def test_divider_map_uses_interior_height(self):
        cfg = _two_col()
        plan = build_assembly_plan(cfg)
        div = next(p for p in plan.panels if "divider" in p.panel)
        assert div.draw_height == pytest.approx(cfg.interior_height)

    def test_part_ids_flow_to_maps(self):
        plan = build_assembly_plan(_box(), id_map={"side": "A-S1"})
        side = next(p for p in plan.panels if p.panel.startswith("side"))
        assert side.part_id == "A-S1"

    def test_dry_fit_precedes_glue_up(self):
        plan = build_assembly_plan(_box())
        titles = [s.title for s in plan.steps]
        dry = next(i for i, t in enumerate(titles) if "DRY FIT" in t)
        glue = next(i for i, t in enumerate(titles) if "Glue up" in t)
        assert dry < glue


# ─── Renderers ───────────────────────────────────────────────────────────────


class TestRenderers:
    def test_html_cites_dry_fit_model(self):
        plan = build_assembly_plan(_box())
        html = generate_assembly_html([plan], "proj")
        assert DRY_FIT_TENON_URL in html
        assert "paulengel" in html
        assert "DRY FIT" in html

    def test_html_declares_charset(self):
        # Without an explicit charset the browser sniffs windows-1252 and
        # every em-dash renders as "â€”" (Charlie's printout, 2026-07-28).
        plan = build_assembly_plan(_box())
        html = generate_assembly_html([plan], "proj")
        assert html.startswith("<!DOCTYPE html>")
        assert '<meta charset="utf-8">' in html
        assert html.rstrip().endswith("</html>")
        # the charset must come before any em-dash content
        assert html.index('charset="utf-8"') < html.index("—")

    def test_html_shows_positions_and_part_number(self):
        plan = build_assembly_plan(_box())
        html = generate_assembly_html([plan], "proj")
        assert "494938" in html
        for p in plan.positions:
            assert f"{p:.0f}" in html

    def test_pdf_renders(self):
        pytest.importorskip("reportlab")
        from cabineteer.assembly import generate_assembly_pdf
        plan = build_assembly_plan(_two_col())
        pdf = generate_assembly_pdf([plan], "proj")
        assert pdf.startswith(b"%PDF")
        assert len(pdf) > 5000


# ─── Cutlist divider construction ────────────────────────────────────────────


class TestDividerConstruction:
    def _divider(self, cfg):
        from cabineteer.server import _raw_panels_for_cabinet
        carcass, _, _, _ = _raw_panels_for_cabinet(
            cfg, [{"width_mm": 373, "openings": [],
                   "fixed_shelf_positions": []},
                  {"width_mm": 373, "openings": []}])
        return next(p for p in carcass if p.name == "column_divider")

    def test_tenon_divider_cut_to_interior_height(self):
        div = self._divider(_two_col())
        assert div.length == pytest.approx(700 - 18 - 18)

    def test_dado_divider_keeps_full_height(self):
        div = self._divider(
            _two_col(carcass_joinery=CarcassJoinery.DADO_RABBET))
        assert div.length == pytest.approx(700)


# ─── Review 2026-07-29 regressions (M1, M2, M4 + nits) ───────────────────────


class TestPlanBomSpanParity:
    """M1: the plan and the hardware BOM must count tenons from the SAME
    span (interior_depth). Before the fix the plan used depth−back_rabbet
    while the BOM used depth−back_thickness, so 3 mm depth windows near
    every 150 mm spacing threshold diverged (depth 356: plan 3/joint, BOM
    4/joint)."""

    def _bom_pieces(self, cfg):
        from cabineteer.cutlist import joinery_lines_for_cabinet_config
        lines = joinery_lines_for_cabinet_config(cfg)
        (line,) = [l for l in lines if "Domino" in l.name]
        return line.pieces_needed

    @pytest.mark.parametrize("depth", [354, 355, 356, 357, 358, 505, 507])
    def test_tenon_counts_agree_across_spacing_windows(self, depth):
        cfg = CabinetConfig(width=800, height=700, depth=depth)
        plan = build_assembly_plan(cfg)
        assert plan.tenons_per_cabinet == self._bom_pieces(cfg)

    # (The span/interior_depth assertion lives once, in TestAssemblyPlan,
    # where it is checked against the panel the mortises go into. A second
    # copy of `plan.span == cfg.interior_depth` restated assembly.py:286 and
    # could not fail.)


class TestDividerShelfMaps:
    """M2: a divider bordering a column with fixed shelves must show face
    rows for those shelves (they were omitted, and the note said 'ENDS
    only')."""

    def _plan(self):
        return build_assembly_plan(_two_col())

    def test_divider_map_has_shelf_face_row(self):
        plan = self._plan()
        div_map = next(pm for pm in plan.panels
                       if pm.panel.startswith("column divider"))
        face_rows = [r for r in div_map.rows if r.kind == "face"]
        assert len(face_rows) == 1
        (row,) = face_rows
        assert "col 1 shelf 1" in row.label
        assert "left face" in row.label
        cfg = _two_col()
        # Offset re-based to the divider's bottom (= top of bottom panel):
        # shelf underside line + the 10 mm base-height reference.
        assert row.offset == pytest.approx(320 - cfg.bottom_thickness + 10.0)
        assert "ENDS only" not in div_map.note

    def test_divider_without_shelves_keeps_ends_only_note(self):
        cfg = CabinetConfig(
            width=800, height=700, depth=457,
            columns=[ColumnConfig(width_mm=373, openings=()),
                     ColumnConfig(width_mm=373, openings=())])
        plan = build_assembly_plan(cfg)
        div_map = next(pm for pm in plan.panels
                       if pm.panel.startswith("column divider"))
        assert all(r.kind == "edge" for r in div_map.rows)
        assert "ENDS only" in div_map.note

    def test_map_rows_match_joint_census(self):
        # Every face-mortised part in the joint schedule must have a face
        # row on its map (the map/census contract the review found broken).
        plan = self._plan()
        for j in plan.joints:
            if j.kind != "butt":
                continue
            face_map = next(
                (pm for pm in plan.panels
                 if pm.panel.split(" (")[0] in (
                     j.face_part, "side", "column divider")
                 or j.face_part in pm.panel
                 or (j.face_part in ("left side", "right side")
                     and pm.panel.startswith("side"))), None)
            assert face_map is not None, j.name
            assert any(r.kind == "face" for r in face_map.rows), j.name


class TestSingleColumnShelfLabel:
    def test_both_sides_label(self):
        cfg = CabinetConfig(
            width=600, height=700, depth=457,
            columns=[ColumnConfig(width_mm=564, openings=(),
                                  fixed_shelf_positions=(300,))])
        plan = build_assembly_plan(cfg)
        side_map = next(pm for pm in plan.panels
                        if pm.panel.startswith("side"))
        labels = [r.label for r in side_map.rows]
        assert any("both sides" in l for l in labels)
        assert not any("only" in l for l in labels)
        # And the census still has one joint per side.
        names = [j.name for j in plan.joints]
        assert "col 1 shelf 1 ↔ left side" in names
        assert "col 1 shelf 1 ↔ right side" in names


class TestFenceHeightText:
    """The fence height is 10 mm — the DF 500's fixed base height (= a
    0-offset Domiplate) — NOT t/2. Fence-cut edge mortises must land in the
    same plane as base-registered face mortises; the pre-2026-08 t/2 scheme
    mismatched them by |10 − t/2| and a tight joint wouldn't close
    (Charlie, 2026-08-02)."""

    def test_uniform_stock_single_10mm_setting(self):
        plan = build_assembly_plan(_box())
        setup = next(s for s in plan.steps
                     if s.title == "Set up the Domino machine")
        assert "height 10 mm" in setup.body
        assert "REFERENCE face" in setup.body
        assert "9 mm" not in setup.body           # the old t/2 for 18 mm
        assert "ONE setting" in setup.body

    def test_15mm_stock_still_takes_base_height(self):
        # 15 mm is the floor: 10 + 2.5 (half cutter) leaves a 2.5 mm wall.
        plan = build_assembly_plan(_box(
            side_thickness=15.0, top_thickness=15.0, bottom_thickness=15.0))
        setup = next(s for s in plan.steps
                     if s.title == "Set up the Domino machine")
        assert "height 10 mm" in setup.body
        assert "7.5 mm" not in setup.body

    def test_mixed_thick_stock_shares_one_setting(self):
        # 18 + 25 mm: both ≥ 15 → one 10 mm setting, no per-thickness reset.
        plan = build_assembly_plan(_box(
            bottom_thickness=25.0, top_thickness=25.0))
        assert plan.panel_thicknesses == (18.0, 25.0)
        setup = next(s for s in plan.steps
                     if s.title == "Set up the Domino machine")
        assert "height 10 mm" in setup.body
        assert "12.5 mm" not in setup.body
        assert "ONE setting" in setup.body

    def test_thin_stock_falls_back_to_centred_with_batten_offset(self):
        plan = build_assembly_plan(_box(
            side_thickness=12.0, top_thickness=12.0, bottom_thickness=12.0))
        setup = next(s for s in plan.steps
                     if s.title == "Set up the Domino machine")
        assert "fence 6 mm (centred)" in setup.body
        assert "4 mm SHORT" in setup.body


class TestReferenceFaceRegistration:
    """Every mortise row sits 10 mm from its panel's reference face —
    bottom/top: outside face, shelves: underside, dividers: left face —
    so map centres, fence rides, and batten lines all agree."""

    def test_side_map_rows_at_10mm_from_ends(self):
        plan = build_assembly_plan(_box())     # 18 mm stock, 700 tall
        side = next(p for p in plan.panels if p.panel.startswith("side"))
        rows = {r.label: r.offset for r in side.rows if r.kind == "face"}
        assert rows["bottom (J1/J2)"] == pytest.approx(10.0)
        assert rows["top (J3/J4)"] == pytest.approx(700.0 - 10.0)

    def test_shelf_row_10mm_above_underside_line(self):
        plan = build_assembly_plan(_two_col())   # col 1 shelf underside 320
        side = next(p for p in plan.panels if p.panel.startswith("side"))
        row = next(r for r in side.rows if "shelf" in r.label)
        assert row.offset == pytest.approx(320.0 + 10.0)
        shelf = next(p for p in plan.panels
                     if "shelf" in p.panel and "divider" not in p.panel)
        assert "UNDERSIDE" in shelf.note

    def test_divider_face_row_10mm_past_left_face(self):
        cfg = _two_col()
        plan = build_assembly_plan(cfg)
        top = next(p for p in plan.panels if p.panel == "top")
        div_row = next(r for r in top.rows if "divider" in r.label)
        # Divider left face = col 1 width; row = left face + 10.
        left_face = float(cfg.columns[0].width_mm)
        assert div_row.offset == pytest.approx(left_face + 10.0)
        assert "LEFT-face line" in top.note
        div_map = next(p for p in plan.panels if "divider" in p.panel)
        assert "LEFT face" in div_map.note

    def test_registration_explainer_prefers_dividers(self):
        from cabineteer.assembly import _registration_scenes
        reg = _registration_scenes(build_assembly_plan(_two_col()))
        assert reg["case"] == "divider"
        assert len(reg["scenes"]) == 3
        intro = " ".join(reg["intro"])
        assert "LEFT-face layout line" in intro
        assert "10 mm" in intro
        # Corner-only build falls back to the corner labels.
        reg2 = _registration_scenes(build_assembly_plan(_box()))
        assert reg2["case"] == "corner"

    def test_registration_section_renders_in_html(self):
        from cabineteer.assembly import generate_assembly_html
        html = generate_assembly_html(
            [build_assembly_plan(_two_col())], "p")
        assert "Registration — how the two halves" in html
        assert "DF 500 standing on its base," in html
        assert "flush, no math" in html

    def test_registration_section_renders_in_pdf(self):
        pytest.importorskip("reportlab")
        from cabineteer.assembly import generate_assembly_pdf
        pdf = generate_assembly_pdf(
            [build_assembly_plan(_two_col())], "p")
        assert pdf.startswith(b"%PDF")


class TestPaperSize:
    """US Letter is the default page everywhere (Charlie prints letter,
    2026-08-02); A4 stays available via paper='a4'."""

    def test_paper_size_resolver(self):
        pytest.importorskip("reportlab")
        from cabineteer.cutlist import _paper_size
        assert _paper_size("letter") == (612.0, 792.0)
        assert round(_paper_size("a4")[1], 2) == 841.89
        assert _paper_size("A4") == _paper_size("a4")
        with pytest.raises(ValueError, match="letter"):
            _paper_size("legal")

    def test_assembly_pdf_defaults_to_letter(self):
        pytest.importorskip("reportlab")
        from cabineteer.assembly import generate_assembly_pdf
        plan = build_assembly_plan(_box())
        pdf = generate_assembly_pdf([plan], "p")
        assert b"612" in pdf and b"792" in pdf          # letter MediaBox
        pdf_a4 = generate_assembly_pdf([plan], "p", paper="a4")
        assert b"841.8" in pdf_a4                        # A4 MediaBox

    def test_layout_and_banding_pdfs_accept_paper(self):
        pytest.importorskip("reportlab")
        from cabineteer.cutlist import (
            CutlistPanel, SheetStock, optimize_cutlist,
            generate_sheet_layout_pdf,
        )
        panels = [CutlistPanel(name="side", length=700, width=400,
                               thickness=18, quantity=2)]
        result = optimize_cutlist(
            panels, SheetStock(name="s", length=2440, width=1220,
                               thickness=18))
        pdf = generate_sheet_layout_pdf([("18mm", panels, result)], "t")
        assert b"612" in pdf                             # letter default
        pdf_a4 = generate_sheet_layout_pdf(
            [("18mm", panels, result)], "t", paper="a4")
        assert b"841.8" in pdf_a4

    def test_steps_carry_the_reference_system(self):
        plan = build_assembly_plan(_box())
        titles = [s.title for s in plan.steps]
        assert "Mark the reference faces" in titles
        ref_i = titles.index("Mark the reference faces")
        edge_i = titles.index("Cut the edge mortises (butt joints)")
        face_i = titles.index("Cut the face mortises")
        assert ref_i < edge_i < face_i
        face = plan.steps[face_i].body
        assert "REFERENCE LINE" in face
        assert "10 mm" in face
        assert "batten" in face.lower()


class TestAssemblyPartIdCollisions:
    """M4: the assembly doc's part-ID lookup must key on thickness and
    material too — two same-outline cabinets in 18 mm vs 12 mm stock used
    to both label their maps with the last row's ID."""

    def _instructions(self, tmp_path, monkeypatch, cabinets):
        import asyncio
        import json
        from pathlib import Path
        from cabineteer import project as pmod
        from cabineteer.server import (
            _tool_design_project, _tool_generate_assembly_instructions,
        )
        monkeypatch.setattr(pmod, "project_dir", lambda: tmp_path / "proj")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(_tool_design_project({
            "name": "idcol", "cabinets": cabinets}))
        res = loop.run_until_complete(_tool_generate_assembly_instructions({
            "project_name": "idcol", "format": "html"}))
        payload = json.loads(res[0].text)
        html_path = payload["files"]["html"]
        return payload, Path(html_path).read_text(encoding="utf-8")

    def test_thickness_distinguishes_ids(self, tmp_path, monkeypatch):
        base = {"height": 700, "depth": 457, "width": 800,
                "drawer_config": [[300, "drawer"], [384, "drawer"]],
                "carcass_joinery": "floating_tenon"}
        cabinets = [
            {"name": "thick", "config": dict(base)},
            {"name": "thin", "config": dict(
                base, side_thickness=12.0, top_thickness=12.0,
                bottom_thickness=12.0, shelf_thickness=12.0)},
        ]
        _, html = self._instructions(tmp_path, monkeypatch, cabinets)
        # Both cabinets' side part IDs must appear in the doc — before the
        # fix the last-processed cabinet's ID labeled both.
        assert "S1" in html and "S2" in html

    def test_shelf_families_get_distinct_ids(self, tmp_path, monkeypatch):
        cabinets = [{
            "name": "shelves", "config": {
                "height": 760, "depth": 500, "width": 900,
                "carcass_joinery": "floating_tenon",
                "fixed_shelf_positions": [500],
                "columns": [
                    {"width_mm": 430, "openings": [[300, "door"], [384, "drawer"]],
                     "fixed_shelf_positions": [300]},
                    {"width_mm": 434, "openings": [[684, "door"]]},
                ]}}]
        payload, html = self._instructions(tmp_path, monkeypatch, cabinets)
        import re
        ids = {m for m in re.findall(r"SH\d+", html)}
        # Global 864 mm shelf and col-1 430 mm shelf are distinct cutlist
        # rows; the maps must show both IDs, not one twice.
        assert len(ids) >= 2, ids
