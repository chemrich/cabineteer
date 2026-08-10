"""Tests for the pulls catalog loader (hardware.PullSpec) and placement policy (pulls.py)."""

import json
from pathlib import Path

import pytest

from cabineteer.hardware import (
    MountStyle,
    PullSpec,
    PULLS,
    get_pull,
    _load_pulls_from_catalog,
)
from cabineteer.pulls import (
    DUAL_PULL_THRESHOLD_MM,
    END_MARGIN_MM,
    PullPlacement,
    compatible_pulls,
    pull_fits_face,
    pull_positions,
    recommend_pull_count,
)


# ─── Catalog loader ──────────────────────────────────────────────────────────


class TestCatalogLoad:
    def test_registry_populated(self):
        assert len(PULLS) == 45

    def test_all_ids_unique(self):
        ids = [p.id for p in PULLS.values()]
        assert len(ids) == len(set(ids))

    def test_key_equals_id(self):
        for key, pull in PULLS.items():
            assert key == pull.id

    def test_all_mount_styles_present(self):
        styles = {p.mount_style for p in PULLS.values()}
        # No knobs in current catalog; all other styles present
        assert MountStyle.SURFACE in styles
        assert MountStyle.EDGE    in styles
        assert MountStyle.FLUSH   in styles

    def test_mount_style_counts(self):
        counts = {}
        for p in PULLS.values():
            counts[p.mount_style] = counts.get(p.mount_style, 0) + 1
        assert counts[MountStyle.SURFACE] == 31
        assert counts[MountStyle.EDGE]    == 13
        assert counts[MountStyle.FLUSH]   == 1

    def test_get_pull_success(self):
        p = get_pull("topknobs-hb-128")
        assert p.brand == "Top Knobs"
        assert p.cc_mm == 128
        assert p.mount_style is MountStyle.SURFACE

    def test_get_pull_unknown(self):
        with pytest.raises(KeyError, match="Unknown pull"):
            get_pull("nope")

    def test_pulls_are_frozen(self):
        p = get_pull("topknobs-hb-128")
        with pytest.raises(Exception):
            p.cc_mm = 999  # type: ignore[misc]


class TestPullSpecDerivedFields:
    def test_surface_has_two_holes_at_cc(self):
        p = get_pull("topknobs-hb-128")
        assert p.hole_count == 2
        assert p.hole_offsets_from_center == (-64.0, 64.0)

    def test_edge_has_two_holes_at_cc(self):
        p = get_pull("richelieu-chbrz-96")
        assert p.mount_style is MountStyle.EDGE
        assert p.hole_count == 2
        assert p.hole_offsets_from_center == (-48.0, 48.0)

    def test_flush_has_zero_holes(self):
        p = get_pull("hafele-151.35.665")
        assert p.mount_style is MountStyle.FLUSH
        assert p.hole_count == 0
        assert p.hole_offsets_from_center == ()

    def test_knob_spec_has_one_hole(self, tmp_path):
        # Build a catalog with one synthetic knob and load it
        catalog = {
            "version": "test",
            "pulls": [{
                "id": "test-knob-1",
                "name": "Test Knob",
                "brand": "Test",
                "model_number": "TK-1",
                "url": "",
                "style": "Classic",
                "material": "Brass",
                "finish": "Polished",
                "mount_style": "knob",
                "pack_quantity": 1,
                "dimensions": {"cc_mm": 0, "length_mm": 30, "projection_mm": 25},
                "tags": [],
            }],
        }
        fp = tmp_path / "knobs.json"
        fp.write_text(json.dumps(catalog), encoding="utf-8")
        loaded = _load_pulls_from_catalog(fp)
        k = loaded["test-knob-1"]
        assert k.is_knob
        assert k.hole_count == 1
        assert k.hole_offsets_from_center == (0.0,)


class TestCatalogLoaderErrors:
    def _write(self, tmp_path, entry: dict) -> Path:
        fp = tmp_path / "catalog.json"
        fp.write_text(json.dumps({"pulls": [entry]}), encoding="utf-8")
        return fp

    def test_missing_required_field(self, tmp_path):
        bad = {
            "id": "x", "name": "X", "brand": "B", "model_number": "M",
            # missing mount_style
            "dimensions": {"cc_mm": 0, "length_mm": 10, "projection_mm": 5},
        }
        with pytest.raises(ValueError, match="missing fields"):
            _load_pulls_from_catalog(self._write(tmp_path, bad))

    def test_unknown_mount_style(self, tmp_path):
        bad = {
            "id": "x", "name": "X", "brand": "B", "model_number": "M",
            "mount_style": "pretzel",
            "dimensions": {"cc_mm": 0, "length_mm": 10, "projection_mm": 5},
        }
        with pytest.raises(ValueError, match="unknown mount_style"):
            _load_pulls_from_catalog(self._write(tmp_path, bad))

    def test_missing_dimension(self, tmp_path):
        bad = {
            "id": "x", "name": "X", "brand": "B", "model_number": "M",
            "mount_style": "surface",
            "dimensions": {"cc_mm": 0, "length_mm": 10},  # no projection_mm
        }
        with pytest.raises(ValueError, match="dimensions missing"):
            _load_pulls_from_catalog(self._write(tmp_path, bad))

    def test_duplicate_id(self, tmp_path):
        entry = {
            "id": "dup", "name": "X", "brand": "B", "model_number": "M",
            "mount_style": "surface",
            "dimensions": {"cc_mm": 0, "length_mm": 10, "projection_mm": 5},
        }
        fp = tmp_path / "c.json"
        fp.write_text(json.dumps({"pulls": [entry, entry]}), encoding="utf-8")
        with pytest.raises(ValueError, match="Duplicate pull id"):
            _load_pulls_from_catalog(fp)


# ─── Placement policy ────────────────────────────────────────────────────────


class TestRecommendCount:
    def test_single_pull_under_threshold(self):
        p = get_pull("topknobs-hb-128")
        assert recommend_pull_count(DUAL_PULL_THRESHOLD_MM, p) == 1
        assert recommend_pull_count(DUAL_PULL_THRESHOLD_MM - 1, p) == 1

    def test_dual_pull_above_threshold(self):
        p = get_pull("topknobs-hb-128")
        assert recommend_pull_count(DUAL_PULL_THRESHOLD_MM + 0.01, p) == 2
        assert recommend_pull_count(900, p) == 2

    def test_flush_always_single(self):
        p = get_pull("hafele-151.35.665")
        assert recommend_pull_count(2000, p) == 1


class TestFitCheck:
    def test_fits_centred_pull(self):
        p = get_pull("topknobs-hb-128")  # length 144
        # needs 144 + 2·40 = 224 mm
        assert not pull_fits_face(223, p, count=1)
        assert     pull_fits_face(224, p, count=1)

    def test_dual_fit_tighter(self):
        # 305 mm CC pull: length 316.  A dual layout must satisfy BOTH the
        # edge-clearance bound 3·(316/2 + 40) = 594 AND the non-overlap bound
        # 3·(316 + 20) = 1008.  The overlap bound is the binding one here, so
        # the pull does NOT fit at 594 (bodies would overlap at centre) and
        # only fits once the face reaches 1008 mm.
        #
        # NOTE: this test previously asserted a fit at 594 mm — that encoded
        # the old edge-only check, which approved physically overlapping
        # dual pulls.  Updated for the added non-overlap constraint.
        p = get_pull("topknobs-hb-305")
        assert not pull_fits_face(1007, p, count=2)
        assert     pull_fits_face(1008, p, count=2)

    def test_dual_no_overlap_regression(self):
        # Regression: a 316 mm pull on an 800 mm face passes the edge check
        # (3·(316/2+40)=594 ≤ 800) but its two bodies overlap at the centre
        # (spacing 800/3 ≈ 267 mm < 316 mm body).  Must be rejected.
        p = get_pull("topknobs-hb-305")  # length 316
        assert not pull_fits_face(800, p, count=2)

    def test_flush_small_footprint(self):
        p = get_pull("hafele-151.35.665")  # length 110
        assert pull_fits_face(190, p, count=1)
        assert not pull_fits_face(189, p, count=1)


class TestPositions:
    def test_single_centered(self):
        p = get_pull("topknobs-hb-128")
        placements = pull_positions(500, 200, p, "topknobs-hb-128")
        assert len(placements) == 1
        cx, cz = placements[0].center
        assert cx == 250.0
        assert cz == 100.0
        assert placements[0].hole_coords == ((186.0, 100.0), (314.0, 100.0))
        assert placements[0].pull_key == "topknobs-hb-128"

    def test_dual_at_third_positions(self):
        p = get_pull("topknobs-hb-128")
        placements = pull_positions(900, 200, p, "topknobs-hb-128")
        assert len(placements) == 2
        xs = [pl.center[0] for pl in placements]
        assert xs[0] == pytest.approx(300.0)
        assert xs[1] == pytest.approx(600.0)

    def test_vertical_center(self):
        p = get_pull("topknobs-hb-128")
        placements = pull_positions(500, 300, p, "topknobs-hb-128", vertical="center")
        assert placements[0].center[1] == 150.0

    def test_vertical_upper_third(self):
        p = get_pull("topknobs-hb-128")
        placements = pull_positions(500, 300, p, "topknobs-hb-128", vertical="upper_third")
        assert placements[0].center[1] == pytest.approx(200.0)  # 2/3 of 300

    def test_vertical_lower_third(self):
        p = get_pull("topknobs-hb-128")
        placements = pull_positions(500, 300, p, "topknobs-hb-128", vertical="lower_third")
        assert placements[0].center[1] == pytest.approx(100.0)  # 1/3 of 300

    def test_unknown_vertical_raises(self):
        p = get_pull("topknobs-hb-128")
        with pytest.raises(ValueError, match="Unknown vertical policy"):
            pull_positions(500, 200, p, "topknobs-hb-128", vertical="middle")  # type: ignore[arg-type]

    def test_negative_face_raises(self):
        p = get_pull("topknobs-hb-128")
        with pytest.raises(ValueError, match="must be positive"):
            pull_positions(0, 200, p, "topknobs-hb-128")

    def test_flush_placement_has_no_holes(self):
        p = get_pull("hafele-151.35.665")
        placements = pull_positions(500, 200, p, "hafele-151.35.665")
        assert len(placements) == 1
        assert placements[0].hole_coords == ()

    def test_knob_coerced_to_single(self, tmp_path):
        # Synthetic knob; explicit count=2 should be coerced to 1.
        entry = {
            "id": "k1", "name": "K", "brand": "T", "model_number": "k1",
            "url": "", "style": "Classic", "material": "Brass", "finish": "Polished",
            "mount_style": "knob", "pack_quantity": 1,
            "dimensions": {"cc_mm": 0, "length_mm": 30, "projection_mm": 20},
            "tags": [],
        }
        fp = tmp_path / "c.json"
        fp.write_text(json.dumps({"pulls": [entry]}), encoding="utf-8")
        catalog = _load_pulls_from_catalog(fp)
        knob = catalog["k1"]
        placements = pull_positions(1000, 200, knob, "k1", count=2)
        assert len(placements) == 1
        assert placements[0].hole_coords == ((500.0, 100.0),)

    def test_explicit_count_honored(self):
        # 500 mm face would default to 1 pull; force 2 explicitly
        p = get_pull("topknobs-hb-76")  # length 92, so dual fits easily
        placements = pull_positions(500, 200, p, "topknobs-hb-76", count=2)
        assert len(placements) == 2


# ─── Selection helpers ───────────────────────────────────────────────────────


class TestCompatiblePulls:
    def test_face_fit_filter(self):
        # On a 200 mm face, only small-cc pulls fit (length ≤ 120 with 40 mm margin)
        tiny = compatible_pulls(200)
        # nothing with length > 120 should be in here
        for key, p in tiny:
            assert p.length_mm + 2 * END_MARGIN_MM <= 200

    def test_style_filter(self):
        matches = compatible_pulls(500, style="Transitional")
        assert len(matches) > 0
        assert all(p.style == "Transitional" for _, p in matches)

    def test_finish_filter(self):
        matches = compatible_pulls(500, finish="Flat Black")
        for _, p in matches:
            assert p.finish == "Flat Black"

    def test_mount_style_filter(self):
        matches = compatible_pulls(700, mount_style=MountStyle.EDGE)
        for _, p in matches:
            assert p.mount_style is MountStyle.EDGE

    def test_brand_filter(self):
        matches = compatible_pulls(400, brand="IKEA")
        for _, p in matches:
            assert p.brand == "IKEA"

    def test_combined_filters(self):
        matches = compatible_pulls(
            600, style="Transitional", finish="Honey Bronze",
            mount_style=MountStyle.SURFACE,
        )
        for _, p in matches:
            assert p.style == "Transitional"
            assert p.finish == "Honey Bronze"
            assert p.mount_style is MountStyle.SURFACE

    def test_custom_catalog(self):
        fake = {"x": get_pull("topknobs-hb-128")}
        matches = compatible_pulls(500, catalog=fake)
        assert len(matches) == 1
        assert matches[0][0] == "x"
