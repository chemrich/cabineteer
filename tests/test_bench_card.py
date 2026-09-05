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


# ─── P2: generate_bench_card (pure — no filesystem) ────────────────────────


class TestGenerateBenchCard:
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
