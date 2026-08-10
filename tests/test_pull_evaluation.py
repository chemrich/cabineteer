"""Tests for pull-hardware evaluation checks (Phase 4).

Covers check_drawer_pull, check_door_pull, and check_cabinet_pull_consistency,
plus their integration via evaluate_cabinet.
"""

import json
from pathlib import Path

import pytest

from cabineteer.cabinet import CabinetConfig
from cabineteer.door import DoorConfig
from cabineteer.drawer import DrawerConfig
from cabineteer.evaluation import (
    Severity,
    check_drawer_pull,
    check_door_pull,
    check_cabinet_pull_consistency,
    evaluate_cabinet,
)
from cabineteer.hardware import (
    PULLS,
    MountStyle,
    _load_pulls_from_catalog,
    get_pull,
)


def _only_severity(issues, sev: Severity):
    return [i for i in issues if i.severity is sev]


def _has_check(issues, name: str) -> bool:
    return any(i.check == name for i in issues)


# ─── check_drawer_pull ───────────────────────────────────────────────────────


class TestCheckDrawerPull:
    def test_no_pull_no_issues(self):
        d = DrawerConfig(opening_width=500, opening_height=150, opening_depth=500)
        assert check_drawer_pull(d) == []

    def test_fitting_pull_no_issues(self):
        d = DrawerConfig(
            opening_width=500, opening_height=150, opening_depth=500,
            pull_key="topknobs-hb-128",
        )
        assert check_drawer_pull(d) == []

    def test_unknown_pull_key_errors(self):
        d = DrawerConfig(
            opening_width=500, opening_height=150, opening_depth=500,
            pull_key="definitely-not-real",
        )
        issues = check_drawer_pull(d)
        assert len(issues) == 1
        assert issues[0].severity is Severity.ERROR
        assert issues[0].check == "pull_unknown"

    def test_no_applied_face_warns(self):
        d = DrawerConfig(
            opening_width=500, opening_height=150, opening_depth=500,
            pull_key="topknobs-hb-128",
            applied_face=False,
        )
        issues = check_drawer_pull(d)
        assert _has_check(issues, "pull_no_face")
        # No fit check when there's no face
        assert not _has_check(issues, "pull_fit")

    def test_pull_too_long_errors(self):
        # 120 mm face — a 305 mm cc pull is 316 mm long, cannot fit.
        d = DrawerConfig(
            opening_width=120, opening_height=150, opening_depth=500,
            pull_key="topknobs-hb-305",
        )
        issues = check_drawer_pull(d)
        errors = _only_severity(issues, Severity.ERROR)
        assert any(i.check == "pull_fit" for i in errors)

    def test_large_projection_warns(self, tmp_path):
        """Synthesize a pull with 60 mm projection to exercise the warn path.

        Real catalog entries cap at 45 mm, so we have to build a synthetic
        pull and place it in a throwaway catalog.
        """
        catalog = {
            "pulls": [{
                "id": "tall-bar", "name": "Tall Bar", "brand": "T",
                "model_number": "TB-1", "url": "", "style": "Contemporary",
                "material": "Steel", "finish": "Black",
                "mount_style": "surface", "pack_quantity": 1,
                "dimensions": {"cc_mm": 128, "length_mm": 144,
                               "projection_mm": 60},
                "tags": [],
            }],
        }
        fp = tmp_path / "c.json"
        fp.write_text(json.dumps(catalog), encoding="utf-8")
        loaded = _load_pulls_from_catalog(fp)
        # Inject into global PULLS so get_pull finds it
        PULLS["tall-bar"] = loaded["tall-bar"]
        try:
            d = DrawerConfig(
                opening_width=500, opening_height=150, opening_depth=500,
                pull_key="tall-bar",
            )
            issues = check_drawer_pull(d)
            warnings = _only_severity(issues, Severity.WARNING)
            assert any(i.check == "pull_projection" for i in warnings)
        finally:
            del PULLS["tall-bar"]

    def test_knob_on_wide_face_warns(self, tmp_path):
        """Knobs on faces > 600 mm get a 'use a handle pull' warning."""
        catalog = {
            "pulls": [{
                "id": "test-knob", "name": "Knob", "brand": "T",
                "model_number": "K-1", "url": "", "style": "Traditional",
                "material": "Brass", "finish": "Polished",
                "mount_style": "knob", "pack_quantity": 1,
                "dimensions": {"cc_mm": 0, "length_mm": 30,
                               "projection_mm": 25},
                "tags": [],
            }],
        }
        fp = tmp_path / "c.json"
        fp.write_text(json.dumps(catalog), encoding="utf-8")
        loaded = _load_pulls_from_catalog(fp)
        PULLS["test-knob"] = loaded["test-knob"]
        try:
            # Face width: opening 750 + 2·10 = 770 mm — above the 762 mm threshold.
            d = DrawerConfig(
                opening_width=750, opening_height=150, opening_depth=500,
                pull_key="test-knob",
            )
            issues = check_drawer_pull(d)
            assert _has_check(issues, "pull_knob_on_wide_face")
        finally:
            del PULLS["test-knob"]

    def test_knob_explicit_count_2_warns(self, tmp_path):
        """Knobs ignore pull_count > 1; warn so the user knows."""
        catalog = {
            "pulls": [{
                "id": "test-knob-2", "name": "Knob2", "brand": "T",
                "model_number": "K-2", "url": "", "style": "Traditional",
                "material": "Brass", "finish": "Polished",
                "mount_style": "knob", "pack_quantity": 1,
                "dimensions": {"cc_mm": 0, "length_mm": 30,
                               "projection_mm": 25},
                "tags": [],
            }],
        }
        fp = tmp_path / "c.json"
        fp.write_text(json.dumps(catalog), encoding="utf-8")
        loaded = _load_pulls_from_catalog(fp)
        PULLS["test-knob-2"] = loaded["test-knob-2"]
        try:
            d = DrawerConfig(
                opening_width=400, opening_height=150, opening_depth=500,
                pull_key="test-knob-2",
                pull_count=2,
            )
            issues = check_drawer_pull(d)
            assert _has_check(issues, "pull_count_knob_coerced")
        finally:
            del PULLS["test-knob-2"]


# ─── check_door_pull ─────────────────────────────────────────────────────────


class TestCheckDoorPull:
    def test_no_pull_no_issues(self):
        d = DoorConfig(opening_width=400, opening_height=720)
        assert check_door_pull(d) == []

    def test_fitting_pull_no_issues(self):
        d = DoorConfig(
            opening_width=400, opening_height=720,
            pull_key="topknobs-hb-96",
        )
        assert check_door_pull(d) == []

    def test_unknown_pull_key_errors(self):
        d = DoorConfig(
            opening_width=400, opening_height=720,
            pull_key="not-real",
        )
        issues = check_door_pull(d)
        assert issues[0].check == "pull_unknown"
        assert issues[0].severity is Severity.ERROR

    def test_pull_on_narrow_door_errors(self):
        # Door pair with very narrow opening → each leaf is tiny, long pull won't fit
        d = DoorConfig(
            opening_width=300, opening_height=720, num_doors=2,
            pull_key="topknobs-hb-305",
        )
        issues = check_door_pull(d)
        assert any(i.check == "pull_fit" and i.severity is Severity.ERROR for i in issues)

    def test_door_pair_uses_per_leaf_width(self):
        """The fit check should operate on door_width (per leaf), not opening_width."""
        # Single door 600 mm opening, full overlay: door_width = 600 + 2·16 = 632 mm
        # Pair door 600 mm opening, full overlay: each leaf = 600/2 + 16 = 316 mm
        # Pick a pull that fits the single but not the pair:
        # topknobs-hb-305 has length 316 mm → needs 316 + 80 = 396 mm for single-fit.
        # 632 mm fits; 316 mm does not.
        pull_key = "topknobs-hb-305"
        # Single door — fits
        single = DoorConfig(opening_width=600, opening_height=720,
                             num_doors=1, pull_key=pull_key)
        assert check_door_pull(single) == []
        # Pair — each leaf 316 mm, pull needs 396 mm → does not fit
        pair = DoorConfig(opening_width=600, opening_height=720,
                           num_doors=2, pull_key=pull_key)
        issues = check_door_pull(pair)
        assert any(i.check == "pull_fit" for i in issues)


# ─── check_cabinet_pull_consistency ──────────────────────────────────────────


class TestCheckCabinetPullConsistency:
    def test_no_pulls_no_issues(self):
        cab = CabinetConfig()
        assert check_cabinet_pull_consistency(cab) == []

    def test_only_one_set_no_issues(self):
        cab_d = CabinetConfig(drawer_pull="topknobs-hb-128")
        cab_o = CabinetConfig(door_pull="topknobs-hb-96")
        assert check_cabinet_pull_consistency(cab_d) == []
        assert check_cabinet_pull_consistency(cab_o) == []

    def test_same_pull_both_no_issues(self):
        cab = CabinetConfig(
            drawer_pull="topknobs-hb-128",
            door_pull="topknobs-hb-128",
        )
        assert check_cabinet_pull_consistency(cab) == []

    def test_same_style_different_size_no_issues(self):
        # Two Transitional Honey Bronze pulls in different cc sizes — fine
        cab = CabinetConfig(
            drawer_pull="topknobs-hb-128",  # Transitional
            door_pull="topknobs-hb-96",     # Transitional
        )
        assert check_cabinet_pull_consistency(cab) == []

    def test_style_mismatch_warns(self):
        # Contemporary vs Transitional
        cab = CabinetConfig(
            drawer_pull="rockler-wnl-160",  # Contemporary
            door_pull="topknobs-hb-96",     # Transitional
        )
        issues = check_cabinet_pull_consistency(cab)
        assert len(issues) == 1
        assert issues[0].severity is Severity.WARNING
        assert issues[0].check == "pull_style_mismatch"

    def test_unknown_key_does_not_double_report(self):
        # Unknown keys are the responsibility of the per-config checks;
        # this check should stay silent rather than raise or warn.
        cab = CabinetConfig(
            drawer_pull="not-real",
            door_pull="topknobs-hb-96",
        )
        assert check_cabinet_pull_consistency(cab) == []


# ─── evaluate_cabinet integration ────────────────────────────────────────────


class TestEvaluateCabinetWithPulls:
    def test_pull_checks_flow_through_drawer_assemblies(self):
        cab = CabinetConfig(width=600, height=720, depth=550)
        # Build a DrawerConfig with a bad pull_key and pass it to evaluate_cabinet.
        # We don't actually need a cq.Assembly — evaluate_cabinet accepts
        # (drawer_assy, drawer_cfg) pairs; we pass None for the assembly.
        dcfg = DrawerConfig(
            opening_width=cab.interior_width, opening_height=150,
            opening_depth=cab.interior_depth,
            pull_key="does-not-exist",
        )
        issues = evaluate_cabinet(
            cab,
            drawer_assemblies=[(None, dcfg)],
        )
        assert any(i.check == "pull_unknown" for i in issues)

    def test_pull_checks_flow_through_door_configs(self):
        cab = CabinetConfig(width=600, height=720, depth=550)
        dcfg = DoorConfig(
            opening_width=cab.interior_width, opening_height=720,
            pull_key="also-not-real",
        )
        issues = evaluate_cabinet(cab, door_configs=[dcfg])
        assert any(i.check == "pull_unknown" for i in issues)

    def test_cabinet_consistency_runs_on_evaluate(self):
        cab = CabinetConfig(
            drawer_pull="rockler-wnl-160",  # Contemporary
            door_pull="topknobs-hb-96",     # Transitional
        )
        issues = evaluate_cabinet(cab)
        assert any(i.check == "pull_style_mismatch" for i in issues)

    def test_clean_cabinet_no_pull_issues(self):
        cab = CabinetConfig(
            width=600, height=720, depth=550,
            drawer_pull="topknobs-hb-128",
            door_pull="topknobs-hb-96",  # both Transitional
        )
        issues = evaluate_cabinet(cab)
        pull_issues = [i for i in issues if i.check.startswith("pull_")]
        assert pull_issues == []
