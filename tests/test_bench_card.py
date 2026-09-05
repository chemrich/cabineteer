"""Tests for the P1 face-height closure check and the P2 bench-card generator.

The P1 regression (``TestAssertFaceHeightsClose.test_b_tower_furniture_top_
mismatch_raises``) reproduces the exact incident from
PLAN-bench-card-reliability-2026-09-04.md: the kid1-desk ``tower-left``
tower, with the bottom row's claim computed under the NO-CAP assumption
while the rest of the claims (and the ``furniture_top=True`` argument
actually passed) assume the WITH-CAP stack. Before the fix this kind of
mismatch could only be caught by hand; the check must raise on the very
first bad row before any document is produced.
"""

import asyncio
import dataclasses
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cabineteer import bench_card as bench_card_module
from cabineteer.cabinet import (
    CabinetConfig,
    FaceHeightClaim,
    DEFAULT_HEIGHT_TOLERANCE_MM,
    assert_face_heights_close,
    bays_from_config,
    face_layout,
    face_row_bands,
)
from cabineteer.bench_card import (
    DonorPiece,
    GRAIN_FREE_ROTATION,
    GRAIN_LOCKED_VERTICAL,
    generate_bench_card,
)
from cabineteer.project import build_project


def _tower_left_cfg(**overrides) -> CabinetConfig:
    """The real B-tower (kid1-desk ``tower-left``) from the incident report:
    height=1168, bottom/top=18, 5 drawer openings summing to interior_height
    (1132), single column."""
    kwargs = dict(
        width=600.0, height=1168.0, depth=550.0,
        bottom_thickness=18.0, top_thickness=18.0,
        face_gap_mm=2.5,
        openings=[
            [296, "drawer"], [296, "drawer"], [180, "drawer"],
            [180, "drawer"], [180, "drawer"],
        ],
    )
    kwargs.update(overrides)
    return CabinetConfig(**kwargs)


def _tower_project_payload(name="bench_card_test_project"):
    return {
        "name": name,
        "cabinets": [{"name": "tower-left", "config": {
            "width": 600, "height": 1168, "depth": 550,
            "bottom_thickness": 18, "top_thickness": 18,
            "face_gap_mm": 2.5,
            "openings": [
                [296, "drawer"], [296, "drawer"], [180, "drawer"],
                [180, "drawer"], [180, "drawer"],
            ],
        }}],
    }


def _door_pair_project_payload(name="bench_card_door_pair_project"):
    """A single door_pair opening — its two leaves share one z-band (the
    convention ``bench_card._row_bands``/``cabinet.face_row_bands``
    document), so they must land in the SAME row/claim, not be split into
    two independent single-leaf rows."""
    return {
        "name": name,
        "cabinets": [{"name": "doorcab", "config": {
            "width": 600, "height": 720, "depth": 500,
            "bottom_thickness": 18, "top_thickness": 18,
            "face_gap_mm": 2.5,
            "openings": [[684, "door_pair"]],
        }}],
    }


def _two_column_project_payload(name="bench_card_multicol_project"):
    """An asymmetric 2-column cabinet — bay 0 and bay 1 have different
    opening counts/heights, so their real row stacks never coincide. Used
    to prove ``face_height_overrides`` is isolated per bay, not broadcast
    positionally across every column of the cabinet."""
    return {
        "name": name,
        "cabinets": [{"name": "twocol", "config": {
            "width": 900, "height": 700, "depth": 500,
            "bottom_thickness": 18, "top_thickness": 18,
            "face_gap_mm": 2.5,
            "columns": [
                {"width_mm": 400, "openings": [[300, "drawer"], [300, "drawer"]]},
                {"width_mm": 400, "openings": [[200, "drawer"], [200, "drawer"], [200, "drawer"]]},
            ],
        }}],
    }


# ─── P1: assert_face_heights_close ─────────────────────────────────────────


class TestAssertFaceHeightsClose:
    def test_b_tower_furniture_top_mismatch_raises(self):
        """The exact incident: bottom row uses the NO-CAP number (294.5,
        which is really the furniture_top=False row) while the call site
        passes furniture_top=True (the WITH-CAP assumption the rest of the
        document — and the '2nd' through 'top' claims — actually used).
        The mismatch must be caught on row 0, before anything is printed.
        """
        cfg = _tower_left_cfg()
        claims = [
            FaceHeightClaim("bottom", 294.5),   # wrong: no-cap number
            FaceHeightClaim("2nd", 293.5),
            FaceHeightClaim("3rd", 177.5),
            FaceHeightClaim("4th", 177.5),
            FaceHeightClaim("top", 176.5),
        ]
        with pytest.raises(ValueError) as exc_info:
            assert_face_heights_close(
                [cfg], claims, bay_index=0,
                face_gap=2.5, furniture_top=True,
            )
        msg = str(exc_info.value)
        assert "row 0" in msg
        assert "'bottom'" in msg
        assert "294.5" in msg
        assert "312.75" in msg    # the real with-cap height for that row

    def test_with_cap_claims_close(self):
        """The correct with-cap claim set (furniture_top=True) passes."""
        cfg = _tower_left_cfg()
        claims = [
            FaceHeightClaim("bottom", 312.75),
            FaceHeightClaim("2nd", 293.5),
            FaceHeightClaim("3rd", 177.5),
            FaceHeightClaim("4th", 177.5),
            FaceHeightClaim("top", 176.25),
        ]
        assert assert_face_heights_close(
            [cfg], claims, bay_index=0,
            face_gap=2.5, furniture_top=True,
        ) is None

    def test_no_cap_claims_close_under_no_cap_assumption(self):
        """The SAME claim set that fails with furniture_top=True is exactly
        right for furniture_top=False — confirming the function checks the
        assumption actually in effect, not a hardcoded row shape."""
        cfg = _tower_left_cfg()
        claims = [
            FaceHeightClaim("bottom", 294.75),
            FaceHeightClaim("2nd", 293.5),
            FaceHeightClaim("3rd", 177.5),
            FaceHeightClaim("4th", 177.5),
            FaceHeightClaim("top", 178.75),
        ]
        assert assert_face_heights_close(
            [cfg], claims, bay_index=0,
            face_gap=2.5, furniture_top=False,
        ) is None

    def test_wrong_row_count_raises(self):
        cfg = _tower_left_cfg()
        claims = [FaceHeightClaim("only one", 500.0)]
        with pytest.raises(ValueError, match=r"row\(s\)"):
            assert_face_heights_close(
                [cfg], claims, bay_index=0, face_gap=2.5, furniture_top=True)

    def test_measure_to_remainder_allows_long_never_short(self):
        cfg = _tower_left_cfg()

        def _claims(bottom_height):
            return [
                FaceHeightClaim("bottom", bottom_height, measure_to_remainder=True),
                FaceHeightClaim("2nd", 293.5),
                FaceHeightClaim("3rd", 177.5),
                FaceHeightClaim("4th", 177.5),
                FaceHeightClaim("top", 176.25),
            ]

        # Oversize on purpose — allowed.
        assert assert_face_heights_close(
            [cfg], _claims(320.0), bay_index=0,
            face_gap=2.5, furniture_top=True) is None

        # Short — never allowed, even under measure_to_remainder.
        with pytest.raises(ValueError, match="SHORTER"):
            assert_face_heights_close(
                [cfg], _claims(300.0), bay_index=0,
                face_gap=2.5, furniture_top=True)

    def test_tolerance_is_submillimeter_by_default(self):
        assert DEFAULT_HEIGHT_TOLERANCE_MM == 0.5

    def test_precomputed_panels_agrees_with_recomputing_fresh(self):
        """``precomputed_panels`` is a pure efficiency escape hatch
        (generate_bench_card already ran face_layout under the identical
        kwargs a moment ago) — passing it must reach the exact same
        verdict as letting the function recompute face_layout itself,
        for both a passing and a failing claim set."""
        cfg = _tower_left_cfg()
        panels = face_layout([cfg], face_gap=2.5, furniture_top=True)
        good_claims = [
            FaceHeightClaim("bottom", 312.75),
            FaceHeightClaim("2nd", 293.5),
            FaceHeightClaim("3rd", 177.5),
            FaceHeightClaim("4th", 177.5),
            FaceHeightClaim("top", 176.25),
        ]
        assert assert_face_heights_close(
            [cfg], good_claims, bay_index=0,
            face_gap=2.5, furniture_top=True,
            precomputed_panels=panels,
        ) is None

        bad_claims = [
            FaceHeightClaim("bottom", 294.5),   # wrong: no-cap number
            FaceHeightClaim("2nd", 293.5),
            FaceHeightClaim("3rd", 177.5),
            FaceHeightClaim("4th", 177.5),
            FaceHeightClaim("top", 176.5),
        ]
        with pytest.raises(ValueError, match="312.75"):
            assert_face_heights_close(
                [cfg], bad_claims, bay_index=0,
                face_gap=2.5, furniture_top=True,
                precomputed_panels=panels,
            )


# ─── P2: DonorPiece ─────────────────────────────────────────────────────────


class TestDonorPiece:
    def test_as_stock_grain_along_length(self):
        piece = DonorPiece(id="E1", name="offcut", length_mm=1200,
                           width_mm=600, thickness_mm=18, grain_along="length")
        stock = piece.as_stock()
        assert (stock.length, stock.width) == (1200, 600)

    def test_as_stock_grain_along_width_reorients(self):
        """A rough offcut whose grain runs along its WIDTH must come out of
        as_stock() with that axis as SheetStock.length — the axis a
        grain-locked panel is pinned to."""
        piece = DonorPiece(id="E1", name="offcut", length_mm=600,
                           width_mm=1200, thickness_mm=18, grain_along="width")
        stock = piece.as_stock()
        assert (stock.length, stock.width) == (1200, 600)

    def test_as_stock_carries_material_and_thickness(self):
        piece = DonorPiece(id="E1", name="offcut", length_mm=1200,
                           width_mm=600, thickness_mm=18,
                           material="white_oak")
        stock = piece.as_stock()
        assert stock.material == "white_oak"
        assert stock.thickness == 18
        assert stock.quantity == 1

    def test_grain_along_typo_raises_instead_of_silently_swapping_axes(self):
        """as_stock() branches on the EXACT string 'length'/'width'; a
        typo like 'Length' (capitalized) must raise at construction, not
        silently fall into the 'width' branch and swap length_mm/width_mm
        — defeating the grain-lock feature with a plausible-looking but
        wrong card."""
        with pytest.raises(ValueError, match="grain_along"):
            DonorPiece(id="E1", name="offcut", length_mm=1200,
                       width_mm=600, thickness_mm=18, grain_along="Length")

    @pytest.mark.parametrize("length_mm,width_mm,thickness_mm", [
        (0, 600, 18), (1200, 0, 18), (1200, 600, 0), (-100, 600, 18),
    ])
    def test_non_positive_dims_raise(self, length_mm, width_mm, thickness_mm):
        """A non-positive dimension is schema-valid but physically
        meaningless — as_stock() feeding it straight to a division in
        _donor_piece_drawing (content_width / stock.length) would raise a
        raw ZeroDivisionError deep inside PDF rendering instead of a
        clean error naming the bad piece. Caught at construction."""
        with pytest.raises(ValueError, match="E1"):
            DonorPiece(id="E1", name="offcut", length_mm=length_mm,
                       width_mm=width_mm, thickness_mm=thickness_mm)


# ─── P2: generate_bench_card (pure — no filesystem) ────────────────────────


class TestGenerateBenchCard:
    @pytest.fixture(autouse=True)
    def _require_reportlab(self):
        # generate_bench_card always renders a PDF once it gets past
        # validation (there's no PDF-less mode) — every test here that
        # reaches rendering needs reportlab, which lite installs lack.
        pytest.importorskip("reportlab")

    def _project(self):
        return build_project(_tower_project_payload())

    def test_packs_faces_onto_one_donor_piece(self):
        project = self._project()
        donors = [DonorPiece(id="E1", name="big offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0,
                             material="finished_wood")]
        result = generate_bench_card(
            project, ["tower-left"], donors,
            face_gap=2.5, furniture_top=True)
        assert len(result.assignments) == 5
        assert result.unassigned == []
        assert len(result.pdf_bytes) > 0

    def test_single_cabinet_card_leaves_source_empty(self):
        """Codebase-wide convention (assign_part_ids/consolidate_bom):
        ``source`` stays empty for a single-project/single-cabinet run —
        a nonempty value prints a 'Project A — <name>' section header and
        letter-prefixed part IDs that imply a multi-cabinet batch that
        isn't there. A single requested cabinet must produce bare part
        IDs (no letter prefix)."""
        project = self._project()
        donors = [DonorPiece(id="E1", name="big offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0,
                             material="finished_wood")]
        result = generate_bench_card(
            project, ["tower-left"], donors,
            face_gap=2.5, furniture_top=True)
        assert all(a.panel.source == "" for a in result.assignments)
        assert all("-" not in a.panel.part_id for a in result.assignments)

    def test_unassigned_when_donor_too_small(self):
        project = self._project()
        # Too short to hold even the tallest face (312.75 mm).
        donors = [DonorPiece(id="E1", name="tiny scrap", length_mm=200,
                             width_mm=200, thickness_mm=18.0,
                             material="finished_wood")]
        result = generate_bench_card(
            project, ["tower-left"], donors,
            face_gap=2.5, furniture_top=True)
        assert len(result.assignments) == 0
        assert len(result.unassigned) == 5

    def test_donor_overflow_reports_unassigned_not_phantom_sheet(self):
        """A DonorPiece is ONE physical board (as_stock() sets quantity=1),
        but optimize_cutlist's packers treat any stock_sheet as an
        unlimited-supply size to buy — they will happily invent a second
        copy of a one-off board at sheet_index=1 for whatever doesn't fit
        on the first. This 1000x650 board cannot physically hold all 5 of
        tower-left's stacked rows (~1148 mm needed); anything beyond the
        real board must come back unassigned, never silently placed on a
        board that doesn't exist."""
        project = self._project()
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1000,
                             width_mm=650, thickness_mm=18.0,
                             material="finished_wood")]
        result = generate_bench_card(
            project, ["tower-left"], donors,
            face_gap=2.5, furniture_top=True)
        assert 0 < len(result.assignments) < 5
        assert len(result.unassigned) == 5 - len(result.assignments)
        # No assignment may claim a sheet_index beyond the single physical
        # board this donor represents.
        assert all(a.placement.sheet_index == 0 for a in result.assignments)
        # No part is both assigned and unassigned.
        assigned_ids = {a.panel.part_id for a in result.assignments}
        unassigned_ids = {f.part_id for f in result.unassigned}
        assert assigned_ids.isdisjoint(unassigned_ids)
        assert len(assigned_ids | unassigned_ids) == 5

    def test_donor_results_agree_with_the_physical_overflow(self):
        """``BenchCardResult.donor_results`` must reflect the SAME
        physical reality as ``assignments``/``unassigned`` — not the raw,
        unfiltered ``OptimizationResult`` optimize_cutlist returned
        (which still counts the phantom overflow sheet as used/complete).
        A future caller reading ``donor_results[i][1]`` directly must see
        a board that could not hold everything, not one reporting
        'fully placed' or 'used 2 sheets'."""
        project = self._project()
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1000,
                             width_mm=650, thickness_mm=18.0,
                             material="finished_wood")]
        result = generate_bench_card(
            project, ["tower-left"], donors,
            face_gap=2.5, furniture_top=True)
        assert len(result.donor_results) == 1
        donor, opt_result = result.donor_results[0]
        assert donor.id == "E1"
        # Exactly one physical board — never a phantom second sheet.
        assert opt_result.sheets_used == 1
        assert all(pl.sheet_index == 0 for pl in opt_result.placements)
        # The board could not hold everything — must not report complete.
        assert opt_result.is_complete is False
        # unplaced is name-deduped (like every other optimizer backend);
        # every unassigned panel's name must be represented in it.
        assert set(opt_result.unplaced) == {f.name for f in result.unassigned}
        # waste_pct computed against the one real sheet, not two.
        assert 0.0 <= opt_result.waste_pct <= 100.0

    def test_multi_donor_allocation_no_piece_missing_or_double_cut(self):
        """Faces that don't fit the first donor spill into the pool for the
        next one (bench_card.py's own docstring: 'calls optimize_cutlist
        ONCE PER DonorPiece ... so faces spread across several boards').
        Two boards, together plenty big enough for all 5 tower-left rows,
        neither alone sufficient (mirrors the overflow case above) — every
        face must land on exactly one board, none dropped, none doubled."""
        project = self._project()
        donors = [
            DonorPiece(id="E1", name="offcut 1", length_mm=1000, width_mm=650,
                       thickness_mm=18.0, material="finished_wood"),
            DonorPiece(id="E2", name="offcut 2", length_mm=1000, width_mm=650,
                       thickness_mm=18.0, material="finished_wood"),
        ]
        result = generate_bench_card(
            project, ["tower-left"], donors,
            face_gap=2.5, furniture_top=True)
        assert len(result.assignments) == 5
        assert result.unassigned == []
        # every physical assignment lives on sheet 0 of its own donor
        assert all(a.placement.sheet_index == 0 for a in result.assignments)
        # each face assigned exactly once, split across the two donors
        part_ids = [a.panel.part_id for a in result.assignments]
        assert len(part_ids) == len(set(part_ids))
        donors_used = {a.donor_id for a in result.assignments}
        assert donors_used == {"E1", "E2"}

    def test_mismatched_material_never_assigned(self):
        project = self._project()
        # Donor material doesn't match cfg.face_material ("finished_wood"
        # default), so nothing should be eligible for it.
        donors = [DonorPiece(id="E1", name="wrong material", length_mm=1300,
                             width_mm=700, thickness_mm=18.0,
                             material="baltic_birch")]
        result = generate_bench_card(
            project, ["tower-left"], donors,
            face_gap=2.5, furniture_top=True)
        assert result.assignments == []
        assert len(result.unassigned) == 5

    def test_closure_mismatch_raises_and_produces_no_result(self):
        """The bad-override version of the exact incident, run through the
        full generator: a caller supplies the no-cap bottom height while
        the run is computed under furniture_top=True. Must raise before any
        PDF bytes exist."""
        project = self._project()
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0,
                             material="finished_wood")]
        overrides = {"tower-left": [[
            FaceHeightClaim("bottom", 294.5),
            None, None, None, None,
        ]]}
        with pytest.raises(ValueError, match="312.75"):
            generate_bench_card(
                project, ["tower-left"], donors,
                face_gap=2.5, furniture_top=True,
                face_height_overrides=overrides)

    def test_face_height_overrides_isolated_per_bay(self):
        """An override list for bay 0 of a multi-column cabinet must not be
        broadcast to bay 1's unrelated rows. Bay 0 has 2 rows, bay 1 has 3
        (different opening counts) — if the pre-fix positional broadcast
        were still in effect, supplying an override only for bay 0 would
        also apply it (by position) to bay 1 and immediately raise a
        'wrong row count' ValueError for bay 1 (2 claims given, 3 rows
        live). It must not raise."""
        project = build_project(_two_column_project_payload())
        cfg = dict(project.resolved())["twocol"]
        bays = bays_from_config(cfg)
        panels = face_layout(bays, face_gap=2.5)
        bay0_faces = [p for p in panels
                      if p.bay == 0 and p.kind in ("drawer_face", "door")]
        bands0 = face_row_bands(bay0_faces)
        real_h0 = round(bands0[0][1] - bands0[0][0], 3)

        overrides = {"twocol": [[
            FaceHeightClaim("bottom", real_h0),
            None,
        ]]}
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1500,
                             width_mm=900, thickness_mm=18.0,
                             material="finished_wood")]
        result = generate_bench_card(
            project, ["twocol"], donors, face_gap=2.5,
            face_height_overrides=overrides)
        # bay 0 (2 rows) + bay 1 (3 rows) = 5 faces total, all accounted for.
        assert len(result.assignments) + len(result.unassigned) == 5

    def test_door_pair_leaves_share_one_row_claim(self):
        """A door_pair's two leaves occupy the same z-band and must be
        checked/priced as ONE row — a regression that mis-grouped them
        into two independent rows (or gave leaf 1 a different claim
        height than leaf 0) would ship a card with mismatched door
        heights on the same opening."""
        project = build_project(_door_pair_project_payload())
        donors = [DonorPiece(id="E1", name="offcut", length_mm=750,
                             width_mm=650, thickness_mm=18.0,
                             material="finished_wood")]
        result = generate_bench_card(
            project, ["doorcab"], donors, face_gap=2.5)
        assert len(result.assignments) == 2
        assert result.unassigned == []
        leaf_heights = {round(a.panel.length, 3) for a in result.assignments}
        # Both leaves closed against the SAME single band -> identical
        # claimed height.
        assert len(leaf_heights) == 1
        assert all(a.panel.name == "door" for a in result.assignments)

    def test_unknown_cabinet_name_raises(self):
        project = self._project()
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0)]
        with pytest.raises(ValueError, match="Unknown cabinet"):
            generate_bench_card(project, ["nope"], donors)

    def test_unknown_grain_policy_raises(self):
        project = self._project()
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0)]
        with pytest.raises(ValueError, match="grain_policy"):
            generate_bench_card(project, ["tower-left"], donors,
                               grain_policy="sideways")

    def test_unknown_grain_overrides_value_raises(self):
        """A per-row override value must be validated against
        _GRAIN_POLICIES — a typo like 'locked' (for 'locked_vertical')
        must raise, not silently fall through to free rotation."""
        project = self._project()
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0,
                             material="finished_wood")]
        with pytest.raises(ValueError, match="grain_overrides"):
            generate_bench_card(
                project, ["tower-left"], donors, face_gap=2.5,
                furniture_top=True,
                grain_overrides={"tower-left:0:0:0": "locked"})

    def test_duplicate_donor_ids_raises(self):
        """Two DonorPiece entries sharing an id would have their
        assignments matched back by that free-text id — merging two
        distinct physical boards' cut pieces on the rendered card."""
        project = self._project()
        donors = [
            DonorPiece(id="E1", name="board one", length_mm=1300,
                       width_mm=700, thickness_mm=18.0,
                       material="finished_wood"),
            DonorPiece(id="E1", name="board two", length_mm=1300,
                       width_mm=700, thickness_mm=18.0,
                       material="finished_wood"),
        ]
        with pytest.raises(ValueError, match="donor_pieces"):
            generate_bench_card(project, ["tower-left"], donors,
                               face_gap=2.5, furniture_top=True)

    def test_duplicate_cabinet_names_in_project_raises(self):
        """Two cabinets sharing a name would have
        ``dict(project.resolved())`` silently keep only the last one's
        config — the wrong dimensions printed with no error."""
        payload = _tower_project_payload("bench_card_dup_cabinet_project")
        payload["cabinets"].append({
            "name": "tower-left",
            "config": {
                "width": 900, "height": 2000, "depth": 550,
                "bottom_thickness": 18, "top_thickness": 18,
                "face_gap_mm": 2.5,
                "openings": [[1964, "door"]],
            },
        })
        project = build_project(payload)
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0,
                             material="finished_wood")]
        with pytest.raises(ValueError, match="more than one cabinet named"):
            generate_bench_card(project, ["tower-left"], donors,
                               face_gap=2.5, furniture_top=True)

    def test_duplicate_requested_cabinet_names_raises(self):
        project = self._project()
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0,
                             material="finished_wood")]
        with pytest.raises(ValueError, match="cabinet_names"):
            generate_bench_card(
                project, ["tower-left", "tower-left"], donors,
                face_gap=2.5, furniture_top=True)

    def test_face_height_overrides_unknown_bay_index_raises(self):
        """An override list longer than the cabinet's real bay count must
        raise rather than silently discard the extra entry."""
        project = self._project()
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0,
                             material="finished_wood")]
        overrides = {"tower-left": [
            [None, None, None, None, None],   # bay 0 — real
            [None],                            # bay 1 — doesn't exist
        ]}
        with pytest.raises(ValueError, match="bay"):
            generate_bench_card(
                project, ["tower-left"], donors, face_gap=2.5,
                furniture_top=True, face_height_overrides=overrides)

    def test_face_height_overrides_extra_row_raises(self):
        """An override row list longer than the bay's real row count must
        raise rather than silently discard the extra rows."""
        project = self._project()
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0,
                             material="finished_wood")]
        # tower-left bay 0 has exactly 5 rows; a 6th entry is out of range.
        overrides = {"tower-left": [[None, None, None, None, None, None]]}
        with pytest.raises(ValueError, match="row"):
            generate_bench_card(
                project, ["tower-left"], donors, face_gap=2.5,
                furniture_top=True, face_height_overrides=overrides)

    def test_free_rotation_policy_allows_rotation(self):
        project = self._project()
        # Every face here is 600 mm wide (single-column tower, flush
        # overlay). A 620x320 donor is too NARROW for any face nominally
        # (width 600 > 320) but wide enough lengthwise to take one rotated
        # (600 <= 620) as long as its height fits the 320 mm width
        # (176.25/177.5 mm both do). Only reachable if rotation is allowed.
        donors = [DonorPiece(id="E1", name="narrow offcut", length_mm=620,
                             width_mm=320, thickness_mm=18.0,
                             material="finished_wood")]
        locked = generate_bench_card(
            project, ["tower-left"], donors,
            face_gap=2.5, furniture_top=True,
            grain_policy=GRAIN_LOCKED_VERTICAL)
        assert locked.assignments == []

        free = generate_bench_card(
            project, ["tower-left"], donors,
            face_gap=2.5, furniture_top=True,
            grain_policy=GRAIN_FREE_ROTATION)
        assert len(free.assignments) >= 1
        assert all(a.placement.rotated for a in free.assignments)

    def test_grain_override_frees_exactly_the_overridden_row_default_algorithm(self):
        """The headline regression: a grain_overrides entry freeing ONE
        row of a same-named 'false_front' stack while its siblings stay
        grain-locked (the module default) must actually free THAT row —
        run through algorithm='auto', the path every real bench card
        uses. Before the cutlist fix, every backend but rips_first
        grouped can_rotate by CutlistPanel.name across ALL rows sharing
        that name, so a locked sibling re-locked the freed row too (0
        assignments where 1 was expected)."""
        project = self._project()
        cfg = dict(project.resolved())["tower-left"]
        bays = bays_from_config(cfg)
        panels = face_layout(bays, face_gap=2.5, furniture_top=True)
        faces = [p for p in panels if p.kind in ("drawer_face", "door")]
        bottom = min(faces, key=lambda p: p.z)
        key = f"tower-left:{bottom.bay}:{bottom.slot}:{bottom.leaf}"

        # All 5 rows are 600 mm wide (single-column tower); this donor is
        # too NARROW (320 mm) for any row nominally (width 600 > 320) but
        # long enough to take one rotated (600 <= 620), and every row's
        # height fits the 320 mm width once rotated. Only the overridden
        # row may rotate under the default grain_policy
        # (GRAIN_LOCKED_VERTICAL), so exactly one placement is reachable.
        donors = [DonorPiece(id="E1", name="narrow offcut", length_mm=620,
                             width_mm=320, thickness_mm=18.0,
                             material="finished_wood")]
        result = generate_bench_card(
            project, ["tower-left"], donors,
            face_gap=2.5, furniture_top=True,
            grain_overrides={key: GRAIN_FREE_ROTATION},
            algorithm="auto")
        assert len(result.assignments) == 1
        assert result.assignments[0].placement.rotated is True
        assert len(result.unassigned) == 4

    def test_grain_mismatch_from_optimizer_surfaces_as_warning(self):
        """bench_card.py's own translation of a packer-reported
        ``OptimizationResult.grain_mismatched`` into a printed warning
        (the forced-rotation / cross-grain path). No packer today can
        actually violate a ``can_rotate=False`` request (opcut's
        ``csp.py`` honors it categorically; rectpack and the strip
        fallback never rotate a grain-constrained piece either) — so this
        drives the real generator against a canned optimizer result that
        reports the violation, the way a future packer (or a bug in one
        of today's) could. A regression that stops threading
        ``result.grain_mismatched`` into ``warnings`` should fail this."""
        project = self._project()
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0,
                             material="finished_wood")]
        real_optimize = bench_card_module.optimize_cutlist

        def fake_optimize(panels, stock_sheet, kerf, algorithm):
            result = real_optimize(panels, stock_sheet=stock_sheet,
                                    kerf=kerf, algorithm=algorithm)
            if not result.placements:
                return result
            forced = result.placements[0]
            forced.rotated = True
            return dataclasses.replace(
                result, grain_mismatched=[forced.panel_name])

        with patch.object(bench_card_module, "optimize_cutlist",
                          side_effect=fake_optimize):
            result = generate_bench_card(
                project, ["tower-left"], donors,
                face_gap=2.5, furniture_top=True,
                grain_policy=GRAIN_LOCKED_VERTICAL)

        assert len(result.warnings) == 1
        assert "E1" in result.warnings[0]
        assert "cross-grain" in result.warnings[0]

    def test_pdf_bytes_start_with_pdf_header(self):
        pytest.importorskip("reportlab")
        project = self._project()
        donors = [DonorPiece(id="E1", name="offcut", length_mm=1300,
                             width_mm=700, thickness_mm=18.0,
                             material="finished_wood")]
        result = generate_bench_card(
            project, ["tower-left"], donors,
            face_gap=2.5, furniture_top=True)
        assert result.pdf_bytes[:5] == b"%PDF-"


# ─── P2: _donor_piece_drawing label fit ────────────────────────────────────


class TestDonorPieceDrawingLabelFit:
    """CLAUDE.md documents the general renderer's label-overprint defect as
    already fixed (stringWidth-based shrink/truncate). ``_donor_piece_
    drawing`` drew part labels at a fixed fontSize with no such fit, which
    reintroduces that same defect for the bench card's own diagram — a
    long label on a narrow or rotated donor rectangle overruns the sheet
    border or a neighbouring label."""

    def _label_strings(self, drawing):
        from reportlab.graphics.shapes import Group, String
        out = []

        def walk(obj):
            for child in getattr(obj, "contents", []):
                if isinstance(child, String):
                    out.append(child)
                elif isinstance(child, Group):
                    walk(child)
        walk(drawing)
        return out

    def test_long_label_fits_within_its_own_narrow_rectangle(self):
        pytest.importorskip("reportlab")
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from cabineteer.bench_card import _donor_piece_drawing, BenchCardAssignment
        from cabineteer.cutlist import CutlistPanel, Placement

        # A long part_id + name on a narrow, short rectangle — at the
        # fixed fontSize=9 this label is far wider than the piece it sits
        # on, which is exactly the overprint the general renderer's
        # stringWidth fit was added to prevent.
        panel = CutlistPanel(name="false_front_extra_long_description",
                              length=80, width=40, thickness=18,
                              part_id="A-VERYLONGPARTNUMBER1")
        placement = Placement(panel_name=panel.name, sheet_index=0,
                               x=0, y=0, placed_length=80, placed_width=40,
                               rotated=False, part_id=panel.part_id)
        assignment = BenchCardAssignment(donor_id="E1", placement=placement,
                                          panel=panel)
        piece = DonorPiece(id="E1", name="offcut", length_mm=200,
                           width_mm=100, thickness_mm=18.0)

        content_width = 200.0
        drawing = _donor_piece_drawing(piece, [assignment], content_width)

        scale = content_width / piece.length_mm
        pw, ph = placement.placed_length * scale, placement.placed_width * scale
        tall = ph > pw
        along = (ph if tall else pw) - 4.0

        labels = [s for s in self._label_strings(drawing)
                  if s.text.startswith("A-VERYLONGPARTNUMBER1")]
        assert labels, "expected the part label to be rendered"
        label = labels[0]
        # The rendered label, AT ITS OWN reported fontSize, must fit
        # within the rectangle it's drawn on — either shrunk or
        # truncated, never printed at a fixed size regardless of fit.
        assert stringWidth(label.text, "Helvetica", label.fontSize) <= along + 0.05

    def test_short_label_on_a_roomy_rectangle_is_unshrunk(self):
        """A label that already fits keeps the original fontSize/text —
        the fit logic must not shrink or truncate when there's no need
        to."""
        pytest.importorskip("reportlab")
        from cabineteer.bench_card import _donor_piece_drawing, BenchCardAssignment
        from cabineteer.cutlist import CutlistPanel, Placement

        panel = CutlistPanel(name="ff", length=800, width=300, thickness=18,
                              part_id="DB1")
        placement = Placement(panel_name=panel.name, sheet_index=0,
                               x=0, y=0, placed_length=800, placed_width=300,
                               rotated=False, part_id=panel.part_id)
        assignment = BenchCardAssignment(donor_id="E1", placement=placement,
                                          panel=panel)
        piece = DonorPiece(id="E1", name="offcut", length_mm=1200,
                           width_mm=600, thickness_mm=18.0)
        drawing = _donor_piece_drawing(piece, [assignment], content_width=1200.0)

        labels = [s for s in self._label_strings(drawing)
                  if s.text.startswith("DB1")]
        assert labels
        assert labels[0].text == "DB1 ff"
        assert labels[0].fontSize == 9.0


# ─── P2: generate_bench_card MCP tool handler ──────────────────────────────


class TestGenerateBenchCardTool:
    @pytest.fixture(autouse=True)
    def _isolated_home(self, tmp_path, monkeypatch):
        # The tool writes real files under Path.home()/.cabineteer — keep
        # every run out of the user's actual store, exactly like
        # TestGenerateCutlist (tests/test_server.py) does for its sibling
        # paperwork tool.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def _args(self, **overrides):
        args = {
            "project": _tower_project_payload("bench_card_tool_project"),
            "cabinet_names": ["tower-left"],
            "donor_pieces": [
                {"id": "E1", "name": "offcut", "length_mm": 1300,
                 "width_mm": 700, "thickness_mm": 18.0,
                 "material": "finished_wood"},
            ],
            "face_gap_mm": 2.5,
            "furniture_top": True,
        }
        args.update(overrides)
        return args

    def _run(self, args):
        from cabineteer.server import call_tool
        return asyncio.get_event_loop().run_until_complete(
            call_tool("generate_bench_card", args))

    def test_writes_pdf_under_data_dir(self, tmp_path):
        pytest.importorskip("reportlab")
        res = self._run(self._args())
        text = res[0].text
        assert not text.startswith("ERROR:"), text
        data = json.loads(text)
        assert data["assigned_count"] == 5
        assert data["unassigned"] == []
        out_path = Path(data["file"])
        assert out_path.exists()
        assert str(out_path).startswith(str(tmp_path))
        assert out_path.parent == (
            tmp_path / ".cabineteer" / "bench_cards" / "bench_card_tool_project")

    def test_missing_donor_pieces_errors(self):
        args = self._args()
        args["donor_pieces"] = []
        res = self._run(args)
        assert res[0].text.startswith("ERROR:")
        assert "donor_pieces" in res[0].text

    def test_unknown_cabinet_name_errors(self):
        args = self._args(cabinet_names=["not-a-cabinet"])
        res = self._run(args)
        assert res[0].text.startswith("ERROR:")
        assert "Unknown cabinet" in res[0].text

    def test_closure_failure_errors_and_writes_no_file(self, tmp_path):
        args = self._args(face_height_overrides={
            "tower-left": [[
                {"row_index": 0, "height_mm": 294.5},
                None, None, None, None,
            ]]
        })
        res = self._run(args)
        assert res[0].text.startswith("ERROR:")
        assert "312.75" in res[0].text
        bench_dir = tmp_path / ".cabineteer" / "bench_cards"
        assert not bench_dir.exists() or not any(bench_dir.rglob("*.pdf"))

    def test_bad_paper_size_errors(self):
        args = self._args(paper="tabloid")
        res = self._run(args)
        assert res[0].text.startswith("ERROR:")

    def test_row_index_mismatched_with_position_errors(self, tmp_path):
        """``row_index`` is a label-consistency check, not a reorder key —
        an override whose stated row_index doesn't match its actual
        position in the JSON array must be rejected outright rather than
        silently applied to the wrong physical row (two identically-sized
        rows would otherwise close cleanly against the wrong one, misfiling
        which drawer face a note belongs to)."""
        args = self._args(face_height_overrides={
            "tower-left": [[
                None,
                # Position 1, but claims to be row 2 — must error, not get
                # silently applied to position 1's real row.
                {"row_index": 2, "height_mm": 293.5},
                None, None, None,
            ]]
        })
        res = self._run(args)
        assert res[0].text.startswith("ERROR:")
        assert "row_index" in res[0].text
        bench_dir = tmp_path / ".cabineteer" / "bench_cards"
        assert not bench_dir.exists() or not any(bench_dir.rglob("*.pdf"))
