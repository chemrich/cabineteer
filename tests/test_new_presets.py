"""
Stress tests for the new preset catalogue and the identify_furniture_type tool.

Validates (over every registered preset, derived from PRESETS):
- Every preset's opening stack sums to interior height
- Multi-column presets have correct column width sums
- evaluate_cabinet returns no errors for any preset
- identify_furniture_type tool handles valid, ambiguous, and invalid inputs
- apply_preset synonym resolution roundtrips for all furniture-type synonyms
  that have a preset mapping
"""

import asyncio
import json
import pytest

from cabineteer.presets import PRESETS, get_preset
from cabineteer.cabinet import CabinetConfig
from cabineteer.evaluation import evaluate_cabinet, Severity
from cabineteer.furniture_refs import (
    FURNITURE_REFS,
    SYNONYM_TO_PRESETS,
    identify_furniture,
    _norm,
)
from cabineteer.server import (
    _tool_identify_furniture_type,
    _tool_apply_preset,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _parse(result) -> dict:
    return json.loads(result[0].text)


# ─── Preset slugs under guard ─────────────────────────────────────────────────
#
# Every registered preset is guarded — derived from PRESETS rather than a
# hand-maintained list, so a newly added preset is covered automatically (no
# separate registration step). ``sorted`` keeps the parametrized test IDs stable.

ALL_PRESET_SLUGS = sorted(PRESETS)


# ─── Opening-stack integrity ──────────────────────────────────────────────────

class TestOpeningStackIntegrity:
    """Every preset's opening stack must exactly fill interior_height."""

    @pytest.mark.parametrize("slug", ALL_PRESET_SLUGS)
    def test_stack_fills_interior(self, slug):
        preset = get_preset(slug)
        cfg = preset.config

        # A stack shares its interior with any door floors — a door standing
        # on something needs one, and its thickness is the stack's, the same
        # way a divider's is the cabinet's and not a column's.
        from cabineteer.cabinet import bays_from_config, openings_span

        if cfg.columns:
            for i, bay in enumerate(bays_from_config(cfg, None)):
                interior_h = openings_span(bay)
                total = sum(op.height_mm for op in bay.openings)
                assert abs(total - interior_h) < 0.01, (
                    f"{slug} column {i}: stack {total} ≠ available {interior_h}"
                )
        else:
            interior_h = openings_span(cfg)
            total = sum(op.height_mm for op in cfg.openings)
            assert abs(total - interior_h) < 0.01, (
                f"{slug}: stack {total} ≠ interior {interior_h}"
            )

    @pytest.mark.parametrize("slug", ALL_PRESET_SLUGS)
    def test_stack_has_at_least_one_opening(self, slug):
        preset = get_preset(slug)
        cfg = preset.config
        if cfg.columns:
            for col in cfg.columns:
                assert len(col.openings) >= 1
        else:
            assert len(cfg.openings) >= 1


# ─── Multi-column width integrity ─────────────────────────────────────────────

class TestMultiColumnIntegrity:

    def test_gentleman_chest_column_widths_sum_to_interior(self):
        cfg = get_preset("bedroom_gentleman_chest").config
        assert cfg.columns, "Expected multi-column config"
        interior_w = cfg.interior_width
        side_t = cfg.side_thickness
        n_dividers = len(cfg.columns) - 1
        col_sum = sum(col.width_mm for col in cfg.columns)
        total = col_sum + n_dividers * side_t
        assert abs(total - interior_w) < 0.01, (
            f"Column widths {col_sum} + {n_dividers} dividers "
            f"= {total} ≠ interior_width {interior_w}"
        )

    def test_gentleman_chest_left_col_is_door_only(self):
        cfg = get_preset("bedroom_gentleman_chest").config
        left = cfg.columns[0]
        assert len(left.openings) == 1
        assert left.openings[0].opening_type == "door"

    def test_gentleman_chest_right_col_is_all_drawers(self):
        cfg = get_preset("bedroom_gentleman_chest").config
        right = cfg.columns[1]
        assert all(op.opening_type == "drawer" for op in right.openings)


# ─── evaluate_cabinet produces no errors ──────────────────────────────────────

class TestPresetEvaluationClean:
    """evaluate_cabinet must return zero ERROR-severity issues for every preset."""

    @pytest.mark.parametrize("slug", ALL_PRESET_SLUGS)
    def test_no_errors(self, slug):
        cfg = get_preset(slug).config
        issues = evaluate_cabinet(cfg)
        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert not errors, (
            f"{slug} has {len(errors)} error(s):\n"
            + "\n".join(f"  [{i.check}] {i.message}" for i in errors)
        )


# ─── identify_furniture_type tool ─────────────────────────────────────────────

class TestIdentifyFurnitureTypeTool:

    def test_exact_canonical_match(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": "sideboard"})))
        assert "match" in result
        assert result["match"]["piece"] == "Sideboard"

    def test_synonym_match_nightstand(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": "chevet"})))
        assert result["match"]["piece"] == "Chevet"
        assert "bedroom_nightstand" in result["match"]["preset_keys"]

    def test_synonym_match_tallboy(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": "tallboy"})))
        assert result["match"]["piece"] == "Tallboy"
        assert "bedroom_tall_chest" in result["match"]["preset_keys"]

    def test_armoire_returns_both_presets(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": "armoire"})))
        assert result["match"]["piece"] == "Armoire"
        keys = result["match"]["preset_keys"]
        assert "bedroom_armoire" in keys
        assert "armoire_2col" in keys
        assert len(result["match"]["presets"]) == 2

    def test_chiffonier_returns_chiffoniere_preset_first(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": "chiffonier"})))
        assert result["match"]["piece"] == "Chiffonier"
        keys = result["match"]["preset_keys"]
        assert keys[0] == "bedroom_chiffoniere"

    def test_ambiguous_chest_returns_candidates(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": "chest"})))
        assert "candidates" in result or "match" in result
        if "candidates" in result:
            assert len(result["candidates"]) >= 2

    def test_empty_name_returns_error(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": ""})))
        assert "error" in result

    def test_missing_name_returns_error(self):
        result = _parse(_run(_tool_identify_furniture_type({})))
        assert "error" in result

    def test_gibberish_returns_error(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": "xyzzy_zork_9000"})))
        assert "error" in result

    def test_foreign_synonym_armadio(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": "armadio"})))
        assert result["match"]["piece"] == "Armadio"
        assert "bedroom_armoire" in result["match"]["preset_keys"]

    def test_foreign_synonym_schrank(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": "schrank"})))
        assert result["match"]["piece"] == "Schrank"

    def test_pieces_with_no_preset_still_return_info(self):
        # Tansu has no preset
        result = _parse(_run(_tool_identify_furniture_type({"name": "tansu"})))
        assert "match" in result
        assert result["match"]["piece"] == "Tansu"
        assert result["match"]["preset_keys"] == []
        assert result["match"]["presets"] == []

    def test_result_includes_dimensions(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": "dresser"})))
        dims = result["match"]["example_dims_mm"]
        assert "h" in dims and "w" in dims and "d" in dims
        assert dims["w"] > 0

    def test_result_includes_synonyms(self):
        result = _parse(_run(_tool_identify_furniture_type({"name": "nightstand"})))
        syns = result["match"]["synonyms"]
        assert isinstance(syns, list)
        assert any("bedside" in s.lower() or "chevet" in s.lower() for s in syns)


# ─── apply_preset synonym resolution ──────────────────────────────────────────

class TestApplyPresetSynonymResolution:
    """All synonym→preset mappings that have slugs should resolve correctly."""

    SYNONYM_CASES = [
        # (query, expected_preset_slug)
        ("dresser",          "bedroom_dresser"),
        ("nightstand",       "bedroom_nightstand"),
        ("chiffonier",       "bedroom_chiffoniere"),
        ("armoire",          "bedroom_armoire"),
        ("wardrobe",         "bedroom_armoire"),
        ("tallboy",          "bedroom_tall_chest"),
        ("highboy",          "bedroom_tall_chest"),
        ("chest of drawers", "bedroom_tall_chest"),
        ("chest-on-chest",   "bedroom_tall_chest"),
        ("linen tower",      "bathroom_linen_tower"),
        ("linen cabinet",    "bathroom_linen_tower"),
        ("linen press",      "bathroom_linen_tower"),
        ("bar cabinet",      "living_room_bar_cabinet"),
        ("cocktail cabinet", "living_room_bar_cabinet"),
        ("filing cabinet",   "office_filing_cabinet"),
        ("hall tree",        "entryway_hall_tree"),
        ("media console",    "media_console"),
        ("credenza",         "living_room_credenza"),
        ("sideboard",        "living_room_sideboard"),
        ("buffet",           "living_room_sideboard"),
        ("tool chest",       "workshop_tool_chest"),
        ("pantry cupboard",  "kitchen_tall_pantry"),
        ("bathroom vanity",  "bathroom_vanity"),
    ]

    @pytest.mark.parametrize("query,expected_slug", SYNONYM_CASES)
    def test_synonym_resolves_to_expected_preset(self, query, expected_slug):
        result = _parse(_run(_tool_apply_preset({"name": query})))
        assert "error" not in result, (
            f"apply_preset({query!r}) returned error: {result.get('error')}"
        )
        assert result.get("preset_name") == expected_slug, (
            f"apply_preset({query!r}): expected {expected_slug!r}, "
            f"got {result.get('preset_name')!r}"
        )
        assert result.get("resolved_from") == query

    def test_exact_slug_still_works(self):
        # Regression: exact slug bypass synonym path entirely
        result = _parse(_run(_tool_apply_preset({"name": "bedroom_dresser"})))
        assert result["preset_name"] == "bedroom_dresser"
        assert "resolved_from" not in result

    def test_synonym_with_no_preset_returns_helpful_error(self):
        # "tansu" has no preset — should return error with furniture ref info
        result = _parse(_run(_tool_apply_preset({"name": "tansu"})))
        assert "error" in result
        err = result["error"].lower()
        assert "tansu" in err or "antique" in err or "no preset" in err

    def test_totally_unknown_name_returns_error(self):
        result = _parse(_run(_tool_apply_preset({"name": "xyzzy_cabinet_9000"})))
        assert "error" in result

    def test_resolved_from_field_present_on_synonym_hit(self):
        result = _parse(_run(_tool_apply_preset({"name": "tallboy"})))
        assert result.get("resolved_from") == "tallboy"

    def test_resolved_from_absent_on_exact_slug(self):
        result = _parse(_run(_tool_apply_preset({"name": "bedroom_tall_chest"})))
        assert "resolved_from" not in result

    def test_synonym_resolution_is_case_insensitive(self):
        lower = _parse(_run(_tool_apply_preset({"name": "ARMOIRE"})))
        assert lower.get("preset_name") == "bedroom_armoire"

    def test_synonym_resolution_trims_whitespace(self):
        result = _parse(_run(_tool_apply_preset({"name": "  dresser  "})))
        assert result.get("preset_name") == "bedroom_dresser"


# ─── describe_design regressions ──────────────────────────────────────────────

class TestDescribeMultiColumn:
    """Regression: describe_design must see multi-column openings rather than
    reporting them as an empty carcass (which also disabled hardware and the
    pull-selection gate)."""

    def test_multi_column_preset_reports_openings_and_hardware(self):
        from cabineteer.describe import describe_design

        d = describe_design(get_preset("armoire_2col").config)
        # Aggregated counts across both columns: 6 drawers + 2 doors.
        assert d["openings"]["counts"] == {"drawer": 6, "door": 2}
        # Hardware is no longer suppressed.
        assert "drawer_slide" in d["hardware"]
        assert "door_hinge" in d["hardware"]
        # Per-column structure is exposed.
        assert "columns" in d["openings"]
        assert len(d["openings"]["columns"]) == 2
        # Prose describes the columns, not an "open carcass".
        assert "open carcass with no fixed openings" not in d["prose"]
        assert "column" in d["prose"].lower()
        # armoire_2col ships with pulls set, so no selection prompt.
        assert d["pull_selection_required"] is False

    def test_multi_column_without_pulls_triggers_pull_gate(self):
        from cabineteer.describe import describe_design

        # bedroom_gentleman_chest is multi-column with no pull keys set.
        d = describe_design(get_preset("bedroom_gentleman_chest").config)
        assert d["pull_selection_required"] is True
        assert d["openings"]["counts"]  # non-empty

    def test_multi_column_stack_fills_interior_true_for_valid_preset(self):
        from cabineteer.describe import describe_design

        d = describe_design(get_preset("armoire_2col").config)
        assert d["openings"]["stack_fills_interior"] is True


class TestImperialFormatter:
    """Regression: the feet+inches formatter used to render 600 mm as
    '1 ft 12 in' because it rounded the inch remainder without carrying."""

    def test_600mm_renders_as_1ft_11_half_in(self):
        from cabineteer.describe import _mm_to_ft_in

        assert _mm_to_ft_in(600) == "1 ft 11½ in"

    def test_no_twelve_inch_remainder(self):
        from cabineteer.describe import _mm_to_ft_in

        # Sweep a range and assert we never emit "12 in" (must carry to feet).
        for mm in range(150, 3000, 3):
            out = _mm_to_ft_in(float(mm))
            assert "12 in" not in out, f"{mm} mm -> {out!r}"

    def test_exact_foot_boundaries(self):
        from cabineteer.describe import _mm_to_ft_in

        assert _mm_to_ft_in(304.8) == "1 ft"      # exactly 12 in
        assert _mm_to_ft_in(1828.8) == "6 ft"     # exactly 72 in
        assert _mm_to_ft_in(152.4) == "6 in"      # exactly 6 in, sub-foot
