"""Tests for the strip-cutting sheet-goods optimiser in cutlist.py."""

import pytest
from cabineteer.cutlist import (
    CutlistPanel,
    OptimizationResult,
    Placement,
    SheetStock,
    SHEET_4x8_3_4,
    SHEET_4x8_1_2,
    optimize_cutlist,
    _optimize_with_rectpack,
    _RECTPACK_AVAILABLE,
    _OPCUT_AVAILABLE,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────


def panel(name: str, length: float, width: float, qty: int = 1, grain: str = "length") -> CutlistPanel:
    return CutlistPanel(name=name, length=length, width=width, thickness=18, quantity=qty, grain_direction=grain)


SMALL_SHEET = SheetStock(name="test_600x600", length=600, width=600, thickness=18)


# ─── Result structure ─────────────────────────────────────────────────────────


class TestOptimizationResultStructure:
    def test_returns_optimization_result(self):
        result = optimize_cutlist([panel("side", 400, 200)], stock_sheet=SMALL_SHEET)
        assert isinstance(result, OptimizationResult)

    def test_placements_are_placement_objects(self):
        result = optimize_cutlist([panel("side", 400, 200)], stock_sheet=SMALL_SHEET)
        assert all(isinstance(p, Placement) for p in result.placements)

    def test_stock_sheet_preserved(self):
        result = optimize_cutlist([panel("side", 400, 200)], stock_sheet=SMALL_SHEET)
        assert result.stock_sheet is SMALL_SHEET

    def test_is_complete_true_when_all_placed(self):
        result = optimize_cutlist([panel("side", 200, 100)], stock_sheet=SMALL_SHEET)
        assert result.is_complete is True

    def test_is_complete_false_when_oversized(self):
        result = optimize_cutlist([panel("giant", 700, 700)], stock_sheet=SMALL_SHEET)
        assert result.is_complete is False


# ─── Single-sheet fit ─────────────────────────────────────────────────────────


class TestSingleSheetFit:
    def test_two_panels_one_sheet(self):
        panels = [panel("left", 400, 200), panel("right", 400, 200)]
        result = optimize_cutlist(panels, stock_sheet=SMALL_SHEET)
        assert result.sheets_used == 1

    def test_one_piece_per_panel_placed(self):
        panels = [panel("left", 400, 200), panel("right", 400, 200)]
        result = optimize_cutlist(panels, stock_sheet=SMALL_SHEET)
        assert len(result.placements) == 2

    def test_panel_names_in_placements(self):
        panels = [panel("left", 400, 200), panel("right", 400, 200)]
        result = optimize_cutlist(panels, stock_sheet=SMALL_SHEET)
        names = {p.panel_name for p in result.placements}
        assert "left" in names
        assert "right" in names

    def test_waste_pct_is_float_in_range(self):
        result = optimize_cutlist([panel("p", 400, 200)], stock_sheet=SMALL_SHEET)
        assert 0.0 <= result.waste_pct <= 100.0

    def test_placement_coords_non_negative(self):
        result = optimize_cutlist([panel("p", 400, 200)], stock_sheet=SMALL_SHEET)
        for p in result.placements:
            assert p.x >= 0
            assert p.y >= 0

    def test_placement_sheet_index_zero(self):
        result = optimize_cutlist([panel("p", 400, 200)], stock_sheet=SMALL_SHEET)
        assert all(p.sheet_index == 0 for p in result.placements)

    def test_default_stock_sheet_is_4x8(self):
        # A small panel with no explicit sheet should land on the 4×8 default.
        result = optimize_cutlist([panel("p", 400, 200)])
        assert result.stock_sheet is SHEET_4x8_3_4


# ─── Quantity expansion ───────────────────────────────────────────────────────


class TestQuantityExpansion:
    def test_quantity_expands_to_multiple_placements(self):
        result = optimize_cutlist([panel("side", 200, 100, qty=4)], stock_sheet=SMALL_SHEET)
        assert len(result.placements) == 4

    def test_all_pieces_have_same_panel_name(self):
        result = optimize_cutlist([panel("side", 200, 100, qty=3)], stock_sheet=SMALL_SHEET)
        assert all(p.panel_name == "side" for p in result.placements)

    def test_mixed_quantities(self):
        panels = [panel("a", 200, 100, qty=2), panel("b", 150, 80, qty=3)]
        result = optimize_cutlist(panels, stock_sheet=SMALL_SHEET)
        assert len(result.placements) == 5


# ─── Multi-sheet layouts ──────────────────────────────────────────────────────


class TestMultipleSheets:
    def test_many_panels_require_multiple_sheets(self):
        # 8 × (500×500) panels can't all fit on one 600×600 sheet.
        panels = [panel(f"p{i}", 500, 500) for i in range(8)]
        result = optimize_cutlist(panels, stock_sheet=SMALL_SHEET)
        assert result.sheets_used > 1

    def test_sheet_indices_are_sequential(self):
        panels = [panel(f"p{i}", 500, 500) for i in range(4)]
        result = optimize_cutlist(panels, stock_sheet=SMALL_SHEET)
        indices = sorted({p.sheet_index for p in result.placements})
        assert indices == list(range(result.sheets_used))

    def test_all_placed_across_sheets(self):
        panels = [panel(f"p{i}", 500, 500) for i in range(4)]
        result = optimize_cutlist(panels, stock_sheet=SMALL_SHEET)
        assert result.is_complete

    def test_realistic_kitchen_base_cabinet(self):
        # 600 mm wide base: 2 sides, top, bottom, back (simplified — no CadQuery)
        panels = [
            panel("side",   720, 560, qty=2),
            panel("top",    564, 560),
            panel("bottom", 564, 560),
            panel("back",   600, 720, grain=""),   # no grain — plywood back
            panel("shelf",  564, 540, qty=2),
        ]
        result = optimize_cutlist(panels, stock_sheet=SHEET_4x8_3_4)
        assert result.is_complete
        assert result.sheets_used >= 1
        assert result.waste_pct < 80  # sanity: shouldn't be nearly all waste


# ─── Oversized panels ─────────────────────────────────────────────────────────


class TestOversizedPanels:
    def test_oversized_panel_is_unplaced(self):
        big = panel("wardrobe_side", 800, 700)  # too big for SMALL_SHEET (600×600)
        result = optimize_cutlist([big], stock_sheet=SMALL_SHEET)
        assert "wardrobe_side" in result.unplaced

    def test_oversized_does_not_consume_a_sheet(self):
        big = panel("wardrobe_side", 800, 700)
        result = optimize_cutlist([big], stock_sheet=SMALL_SHEET)
        assert result.sheets_used == 0

    def test_normal_panels_placed_alongside_oversized(self):
        panels = [
            panel("giant", 800, 700),    # oversized — unplaced
            panel("small", 200, 100),    # fits fine
        ]
        result = optimize_cutlist(panels, stock_sheet=SMALL_SHEET)
        assert "giant" in result.unplaced
        assert any(p.panel_name == "small" for p in result.placements)

    def test_oversized_name_appears_once(self):
        # qty=3 of an oversized panel: name should appear only once in unplaced
        big = panel("giant", 800, 700, qty=3)
        result = optimize_cutlist([big], stock_sheet=SMALL_SHEET)
        assert result.unplaced.count("giant") == 1


# ─── Kerf accounting ─────────────────────────────────────────────────────────


class TestKerfAccounting:
    def test_custom_kerf_accepted(self):
        result = optimize_cutlist(
            [panel("p", 400, 200)], stock_sheet=SMALL_SHEET, kerf=4.0
        )
        assert result.sheets_used >= 1  # just must not raise

    def test_zero_kerf_accepted(self):
        result = optimize_cutlist(
            [panel("p", 400, 200)], stock_sheet=SMALL_SHEET, kerf=0.0
        )
        assert result.is_complete

    def test_kerf_reduces_effective_sheet_area(self):
        # A panel that fits within the sheet minus kerf (with a 1 mm margin to
        # avoid floating-point boundary issues) should be placed successfully.
        # A panel equal to the full nominal sheet size cannot fit once kerf is added.
        kerf = 3.2
        just_fits = panel("ok", SMALL_SHEET.length - kerf * 2 - 1, SMALL_SHEET.width - kerf * 2 - 1)
        result_ok = optimize_cutlist([just_fits], stock_sheet=SMALL_SHEET, kerf=kerf)
        assert result_ok.is_complete

        too_big = panel("nope", SMALL_SHEET.length, SMALL_SHEET.width)
        result_nope = optimize_cutlist([too_big], stock_sheet=SMALL_SHEET, kerf=kerf)
        assert not result_nope.is_complete


# ─── Waste calculation ────────────────────────────────────────────────────────


class TestWasteCalculation:
    def test_waste_zero_for_empty_panels(self):
        result = optimize_cutlist([], stock_sheet=SMALL_SHEET)
        assert result.waste_pct == 0.0
        assert result.sheets_used == 0

    def test_waste_lower_when_panels_fill_sheet(self):
        # Two panels that together nearly fill a 600×600 sheet
        almost_full = [panel("a", 580, 290), panel("b", 580, 290)]
        result = optimize_cutlist(almost_full, stock_sheet=SMALL_SHEET)
        # These two panels cover ~(580×290)*2 = 336,400 mm² of a 360,000 mm² sheet → ~7% waste
        assert result.waste_pct < 30

    def test_waste_higher_for_small_panels_on_large_sheet(self):
        tiny = [panel("chip", 50, 30)]
        result = optimize_cutlist(tiny, stock_sheet=SHEET_4x8_3_4)
        assert result.waste_pct > 90  # one tiny piece on a 4×8 is mostly waste


# ─── No rectpack ─────────────────────────────────────────────────────────────


class TestNoRectpack:
    """Strip cutting is pure Python — neither opcut nor rectpack is required."""

    def test_works_without_rectpack(self, monkeypatch):
        import cabineteer.cutlist as cl
        # Disable BOTH optional solvers so "auto" actually reaches the strip
        # fallback — otherwise opcut (installed in full/dev) would answer and
        # this test would silently exercise opcut, not strip.
        monkeypatch.setattr(cl, "_OPCUT_AVAILABLE", False)
        monkeypatch.setattr(cl, "_RECTPACK_AVAILABLE", False)
        result = cl.optimize_cutlist([panel("p", 200, 100)])
        assert result.is_complete
        assert result.algorithm_used == "strip"


# ─── Algorithm selection & unknown-name guard ────────────────────────────────


class TestAlgorithmSelection:
    def test_unknown_algorithm_raises_value_error(self):
        with pytest.raises(ValueError):
            optimize_cutlist([panel("p", 200, 100)], algorithm="rect_pack")

    def test_empty_panels_algorithm_used_blank(self):
        result = optimize_cutlist([], stock_sheet=SMALL_SHEET)
        assert result.algorithm_used == ""

    @pytest.mark.parametrize(
        "algo",
        [
            pytest.param(
                "opcut",
                marks=pytest.mark.skipif(not _OPCUT_AVAILABLE, reason="opcut not installed"),
            ),
            pytest.param(
                "rectpack",
                marks=pytest.mark.skipif(not _RECTPACK_AVAILABLE, reason="rectpack not installed"),
            ),
            "strip",
        ],
    )
    def test_algorithm_used_reported(self, algo):
        result = optimize_cutlist([panel("p", 400, 200)], stock_sheet=SMALL_SHEET, algorithm=algo)
        assert result.algorithm_used == algo
        assert result.is_complete


# ─── Cross-algorithm behaviour parity ────────────────────────────────────────


_ALGOS = ["strip"]
if _OPCUT_AVAILABLE:
    _ALGOS.append("opcut")
if _RECTPACK_AVAILABLE:
    _ALGOS.append("rectpack")


class TestAlgorithmParity:
    """Behaviour that should hold identically across every optimizer."""

    @pytest.mark.parametrize("algo", _ALGOS)
    def test_basic_placement(self, algo):
        panels = [panel("left", 400, 200), panel("right", 400, 200)]
        result = optimize_cutlist(panels, stock_sheet=SMALL_SHEET, algorithm=algo)
        assert result.is_complete
        assert result.sheets_used == 1
        assert len(result.placements) == 2

    @pytest.mark.parametrize("algo", _ALGOS)
    def test_quantity_expansion(self, algo):
        result = optimize_cutlist([panel("s", 200, 100, qty=4)], stock_sheet=SMALL_SHEET, algorithm=algo)
        assert len(result.placements) == 4

    @pytest.mark.parametrize("algo", _ALGOS)
    def test_oversized_reported_once(self, algo):
        result = optimize_cutlist([panel("giant", 800, 700, qty=3)], stock_sheet=SMALL_SHEET, algorithm=algo)
        assert result.unplaced.count("giant") == 1
        assert result.sheets_used == 0

    @pytest.mark.parametrize("algo", _ALGOS)
    def test_single_edge_trim_convention(self, algo):
        # Under the unified single-edge-trim convention, a piece equal to the
        # sheet minus one kerf on each axis must fit; the full nominal sheet
        # must not (one kerf is trimmed for the leading edge cut).
        kerf = 3.2
        fits = panel("ok", SMALL_SHEET.length - kerf, SMALL_SHEET.width - kerf)
        result_ok = optimize_cutlist([fits], stock_sheet=SMALL_SHEET, kerf=kerf, algorithm=algo)
        assert result_ok.is_complete, f"{algo} should place a sheet-minus-kerf piece"

        too_big = panel("nope", SMALL_SHEET.length, SMALL_SHEET.width)
        result_nope = optimize_cutlist([too_big], stock_sheet=SMALL_SHEET, kerf=kerf, algorithm=algo)
        assert not result_nope.is_complete, f"{algo} should reject a full-sheet piece"


# ─── rectpack rid uniqueness (regression) ────────────────────────────────────


@pytest.mark.skipif(not _RECTPACK_AVAILABLE, reason="rectpack not installed")
class TestRectpackDuplicateNames:
    """Regression: distinct CutlistPanel objects sharing a name must not
    collide on the packer id (previously the per-panel index restarted at 0,
    so ("shelf", 0) was reused — corrupting waste %, rotation flags, and
    unplaced detection)."""

    def test_same_name_different_dims_both_placed_and_areas_distinct(self):
        big = SheetStock(name="4x8", length=2440, width=1220, thickness=18)
        panels = [
            CutlistPanel(name="shelf", length=800, width=400, thickness=18, grain_direction="length"),
            CutlistPanel(name="shelf", length=600, width=300, thickness=18, grain_direction="length"),
        ]
        result = _optimize_with_rectpack(panels, big, kerf=3.2)

        assert len(result.placements) == 2
        assert not result.unplaced

        # Both distinct areas must be represented — a rid collision would
        # double-count one piece's area and drop the other's.
        placed_areas = sorted(round(p.placed_length * p.placed_width) for p in result.placements)
        assert placed_areas == sorted([800 * 400, 600 * 300])

        # Waste computed from both real areas (not a doubled single area).
        sheet_area = 2440 * 1220
        expected = round((sheet_area - (800 * 400 + 600 * 300)) / sheet_area * 100, 1)
        assert abs(result.waste_pct - expected) < 0.2

        # Grain-constrained pieces are never rotated by the packer.
        assert all(not p.rotated for p in result.placements)
        assert result.grain_mismatched == []

    def test_many_same_name_pieces_all_placed(self):
        big = SheetStock(name="4x8", length=2440, width=1220, thickness=18)
        panels = [CutlistPanel(name="side", length=300, width=300, thickness=18, quantity=6)]
        result = _optimize_with_rectpack(panels, big, kerf=3.2)
        assert len(result.placements) == 6
        assert not result.unplaced


# ─── Per-instance grain constraint (regression) ──────────────────────────────


class TestPerInstanceGrainConstraint:
    """Regression: ``can_rotate`` must be read off each CutlistPanel
    INSTANCE's own ``grain_direction``, never aggregated into a
    name-keyed set first. bench_card.py's per-row ``grain_overrides``
    frees rotation on individual rows of a same-named stack (e.g. one
    'false_front' out of five) while leaving the sibling rows
    grain-locked; before the fix, every backend but ``rips_first`` built
    a ``{p.name for p in panels if grain_direction}`` set and tested
    ``p.name not in that_set`` — collapsing "this instance is free" back
    to "no instance of this name is locked", so the freed row silently
    stayed locked and failed to place at all whenever any same-named
    sibling was locked."""

    #: A 620x320 donor board: the nominal orientation (length=176,
    #: width=600) does not fit (width 600 > 320), but the ROTATED
    #: orientation (600x176) does. Placement is only reachable through
    #: rotation, so it proves can_rotate was actually True for this row.
    _STOCK = SheetStock(name="donor", length=620, width=320, thickness=18)

    def _mixed_grain_panels(self):
        # Row 0 is free-rotation (the override); rows 1-4 share the same
        # name and stay grain-locked (the un-overridden siblings).
        return [
            CutlistPanel(name="false_front", length=176, width=600,
                         thickness=18, quantity=1,
                         grain_direction="" if i == 0 else "length")
            for i in range(5)
        ]

    @pytest.mark.parametrize("algo", ["auto"] + _ALGOS)
    def test_freed_row_rotates_despite_locked_same_named_siblings(self, algo):
        result = optimize_cutlist(
            self._mixed_grain_panels(), stock_sheet=self._STOCK,
            kerf=3.2, algorithm=algo)
        # Only the free-rotation row can possibly fit this board (the
        # four locked siblings are 600 mm wide against a 320 mm board and
        # may never rotate) — exactly one placement, and it must be the
        # rotated one.
        assert len(result.placements) == 1
        assert result.placements[0].rotated is True
        # The four locked instances correctly remain unplaced.
        assert result.unplaced.count("false_front") == 1
