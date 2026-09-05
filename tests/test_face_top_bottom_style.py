"""Tests for the top/bottom show-face style split.

``furniture_top`` used to be one boolean controlling two independent facts:
whether the top panel gets a front cap strip, and whether the lowest face
drops flush with the carcass underside. Charlie's ask (2026-09): make them
two real axes — ``face_top_style`` ("plain" | "cap" | "flush") and
``face_bottom_style`` ("plain" | "flush") — so a build can want either alone
(e.g. a flush top with no cap strip, over a plain bottom).

``furniture_top`` survives as a DERIVED, read-only property
(``face_top_style == "cap" and face_bottom_style == "flush"``) — never a
second stored boolean, which is exactly the "two documents agreeing with
each other while both are wrong" failure class the rest of the dimensioning
arc (#88, #89, the depth-datum arc) exists to eliminate. This file pins:

- the new fields' defaults and the derived property's six style combos,
- ``build_cabinet_config``'s legacy-boolean translation and its precedence
  against an explicit new-style key on the SAME call,
- ``face_layout``'s three-way top branch / two-way bottom branch, and the
  full override precedence (explicit overhang > style > furniture_top >
  door-transition rule > flush-to-panels),
- that ``carcass_panel_dims`` and ``face_layout`` agree on which top styles
  carry no separate cap panel (both "cap" and "flush" hide the top panel's
  front edge; only "cap" emits an actual top_cap row),
- the new ``evaluation.check_face_top_bottom_style`` validator,
- ``project.py``'s SharedDesign / shared_from_dict / config round-trip
  handling of the split (including the legacy ``furniture_top`` key), and
- the "flush" top style reproduces kid1-desk's real B-tower numbers
  (verified ad hoc this session: 196.75 mm top row at a 2.5 mm face gap).
"""

from __future__ import annotations

import pytest

from cabineteer.cabinet import (
    CabinetConfig,
    FaceHeightClaim,
    assert_face_heights_close,
    bays_from_config,
    build_cabinet_config,
    carcass_panel_dims,
    face_layout,
)
from cabineteer.evaluation import (
    Severity,
    check_face_top_bottom_style,
    evaluate_cabinet,
)
from cabineteer.project import (
    SharedDesign,
    _config_to_dict,
    _shared_to_dict,
    build_project,
    config_from_dict,
    shared_from_dict,
)
from cabineteer.server import _raw_panels_for_cabinet

TOL = 0.05


def _cfg(**kw) -> CabinetConfig:
    base = dict(
        width=381, height=389, depth=457,
        drawer_config=[[133, "drawer"], [110, "drawer"], [110, "drawer"]],
    )
    base.update(kw)
    return build_cabinet_config(base)


def _drawer_faces(panels):
    return [p for p in panels if p.kind == "drawer_face"]


# ─── CabinetConfig defaults and the derived furniture_top property ─────────


class TestCabinetConfigDefaultsAndProperty:
    def test_raw_default_is_cap_flush(self):
        # Charlie's ask: "always default to some sort of flushness".
        cfg = CabinetConfig(width=600, height=720, depth=500)
        assert cfg.face_top_style == "cap"
        assert cfg.face_bottom_style == "flush"
        assert cfg.furniture_top is True

    @pytest.mark.parametrize("top,bottom,expected", [
        ("cap", "flush", True),
        ("cap", "plain", False),
        ("flush", "flush", False),
        ("flush", "plain", False),
        ("plain", "flush", False),
        ("plain", "plain", False),
    ])
    def test_furniture_top_is_true_only_for_cap_and_flush(self, top, bottom, expected):
        cfg = CabinetConfig(width=600, height=720, depth=500,
                            face_top_style=top, face_bottom_style=bottom)
        assert cfg.furniture_top is expected

    def test_furniture_top_is_not_a_constructor_kwarg(self):
        # It's a property now — passing it directly to the dataclass
        # constructor must fail loudly, not silently swallow the value.
        with pytest.raises(TypeError):
            CabinetConfig(width=600, height=720, depth=500, furniture_top=True)


# ─── build_cabinet_config: the legacy translation chokepoint ───────────────


class TestBuildCabinetConfigTranslation:
    def test_legacy_true_maps_to_cap_flush(self):
        cfg = build_cabinet_config(dict(width=600, height=720, depth=500,
                                        furniture_top=True))
        assert (cfg.face_top_style, cfg.face_bottom_style) == ("cap", "flush")
        assert cfg.furniture_top is True

    def test_legacy_false_maps_to_plain_plain_not_the_new_default(self):
        # The load-bearing case: an already-approved furniture_top=False
        # design must render byte-identical after the class default
        # flipped to ("cap", "flush") — it must NOT fall through to that
        # new default.
        cfg = build_cabinet_config(dict(width=600, height=720, depth=500,
                                        furniture_top=False))
        assert (cfg.face_top_style, cfg.face_bottom_style) == ("plain", "plain")
        assert cfg.furniture_top is False

    def test_explicit_new_style_beats_legacy_boolean_same_call(self):
        # Per-axis: an explicit face_top_style wins even though
        # furniture_top=True is also given in the same call; the
        # untouched axis (bottom) still takes the legacy translation.
        cfg = build_cabinet_config(dict(
            width=600, height=720, depth=500,
            furniture_top=True, face_top_style="plain"))
        assert cfg.face_top_style == "plain"
        assert cfg.face_bottom_style == "flush"   # untouched axis, from legacy True

    def test_explicit_both_new_fields_ignore_legacy_boolean_entirely(self):
        cfg = build_cabinet_config(dict(
            width=600, height=720, depth=500, furniture_top=False,
            face_top_style="flush", face_bottom_style="flush"))
        assert cfg.face_top_style == "flush"
        assert cfg.face_bottom_style == "flush"

    def test_omitting_everything_takes_the_class_default(self):
        cfg = build_cabinet_config(dict(width=600, height=720, depth=500))
        assert (cfg.face_top_style, cfg.face_bottom_style) == ("cap", "flush")

    def test_unknown_key_message_still_names_furniture_top(self):
        # furniture_top is popped before the unknown-key check ever sees
        # it, so it's still accepted — but the documented "valid params"
        # set must keep naming it, or the error message misleads a caller
        # about what's actually supported.
        with pytest.raises(ValueError, match="furniture_top"):
            build_cabinet_config(dict(width=600, height=720, depth=500,
                                      bogus_field=True))


# ─── face_layout: the three-way top branch / two-way bottom branch ─────────


class TestFaceLayoutTopStyles:
    """One opening, so the single row is both first and last — isolates
    the top-anchor formula from any interior-boundary gap math."""

    def _cfg(self, **kw):
        return _cfg(width=381, height=200, depth=457,
                    drawer_config=[[164, "drawer"]], face_gap_mm=4.0, **kw)

    def test_plain_stops_at_panel_underside(self):
        cfg = self._cfg(face_top_style="plain", face_bottom_style="plain")
        panels = face_layout([cfg])
        face = _drawer_faces(panels)[0]
        assert abs((face.z + face.height) - (cfg.height - cfg.top_thickness)) < TOL
        assert not any(p.kind == "top_cap" for p in panels)

    def test_cap_trims_one_gap_and_emits_the_cap_panel(self):
        cfg = self._cfg(face_top_style="cap", face_bottom_style="plain")
        panels = face_layout([cfg])
        face = _drawer_faces(panels)[0]
        expected_top = cfg.height - cfg.top_thickness - cfg.face_gap_mm
        assert abs((face.z + face.height) - expected_top) < TOL
        cap = next(p for p in panels if p.kind == "top_cap")
        assert abs(cap.z - (cfg.height - cfg.top_thickness)) < TOL
        assert abs(cap.height - cfg.top_thickness) < TOL
        assert abs(cap.width - cfg.width) < TOL

    def test_flush_rises_to_true_underside_and_emits_no_cap_panel(self):
        cfg = self._cfg(face_top_style="flush", face_bottom_style="plain")
        panels = face_layout([cfg])
        face = _drawer_faces(panels)[0]
        # No -gap trim: the face itself covers the plane a cap strip would.
        assert abs((face.z + face.height) - cfg.height) < TOL
        assert not any(p.kind == "top_cap" for p in panels)

    def test_flush_top_is_taller_than_cap_top_by_exactly_top_thickness_plus_gap(self):
        cap_cfg = self._cfg(face_top_style="cap", face_bottom_style="plain")
        flush_cfg = self._cfg(face_top_style="flush", face_bottom_style="plain")
        cap_face = _drawer_faces(face_layout([cap_cfg]))[0]
        flush_face = _drawer_faces(face_layout([flush_cfg]))[0]
        assert flush_face.z == pytest.approx(cap_face.z)
        assert (flush_face.height - cap_face.height) == pytest.approx(
            cap_cfg.top_thickness + cap_cfg.face_gap_mm)


class TestFaceLayoutBottomStyles:
    def _cfg(self, **kw):
        return _cfg(width=381, height=200, depth=457,
                    drawer_config=[[164, "drawer"]], **kw)

    def test_plain_starts_at_bottom_panel_top_face(self):
        cfg = self._cfg(face_top_style="plain", face_bottom_style="plain")
        face = _drawer_faces(face_layout([cfg]))[0]
        assert abs(face.z - cfg.bottom_thickness) < TOL

    def test_flush_drops_to_carcass_underside(self):
        cfg = self._cfg(face_top_style="plain", face_bottom_style="flush")
        face = _drawer_faces(face_layout([cfg]))[0]
        assert abs(face.z - 0.0) < TOL   # drops by bottom_thickness -> z=0

    def test_top_and_bottom_styles_are_independent(self):
        # flush top + plain bottom: only the TOP anchor moves.
        cfg = self._cfg(face_top_style="flush", face_bottom_style="plain")
        face = _drawer_faces(face_layout([cfg]))[0]
        assert abs(face.z - cfg.bottom_thickness) < TOL
        assert abs((face.z + face.height) - cfg.height) < TOL


# ─── Precedence: explicit overhang > style > furniture_top > cfg default ───


class TestFaceLayoutPrecedence:
    def _cfg(self, **kw):
        base = dict(width=381, height=200, depth=457,
                    drawer_config=[[164, "drawer"]],
                    face_top_style="cap", face_bottom_style="flush")
        base.update(kw)
        return _cfg(**base)

    def test_no_kwargs_reads_cfg0s_own_stored_fields(self):
        cfg = self._cfg()   # stored as face_top_style="cap", face_bottom_style="flush"
        panels = face_layout([cfg])
        face = _drawer_faces(panels)[0]
        assert abs(face.z - 0.0) < TOL                     # flush bottom
        assert any(p.kind == "top_cap" for p in panels)    # cap top

    def test_explicit_style_kwarg_beats_stored_furniture_top(self):
        # cfg0 is stored as cap/flush; calling with explicit plain/plain
        # must win, exactly like build_cabinet_config's own precedence.
        cfg = self._cfg()
        panels = face_layout([cfg], face_top_style="plain",
                             face_bottom_style="plain")
        face = _drawer_faces(panels)[0]
        assert abs(face.z - cfg.bottom_thickness) < TOL
        assert abs((face.z + face.height) - (cfg.height - cfg.top_thickness)) < TOL
        assert not any(p.kind == "top_cap" for p in panels)

    def test_legacy_furniture_top_kwarg_beats_stored_fields(self):
        cfg = self._cfg(face_top_style="plain", face_bottom_style="plain")
        panels = face_layout([cfg], furniture_top=True)
        assert any(p.kind == "top_cap" for p in panels)
        face = _drawer_faces(panels)[0]
        assert abs(face.z - 0.0) < TOL   # flush bottom, from the legacy True

    def test_explicit_style_kwarg_beats_legacy_furniture_top_same_call(self):
        # Both given on the SAME face_layout call: the real style kwarg
        # wins, matching build_cabinet_config's per-axis precedence.
        cfg = self._cfg()
        panels = face_layout([cfg], furniture_top=False, face_top_style="cap")
        assert any(p.kind == "top_cap" for p in panels)

    def test_explicit_overhang_beats_style_and_furniture_top(self):
        cfg = self._cfg()   # stored cap/flush
        panels = face_layout(
            [cfg], furniture_top=True,
            face_bottom_overhang=0.0, face_top_overhang=0.0)
        face = _drawer_faces(panels)[0]
        assert abs(face.z - cfg.bottom_thickness) < TOL
        assert abs((face.z + face.height) - (cfg.height - cfg.top_thickness)) < TOL
        # The overhang override doesn't touch cap-panel EMISSION — that's
        # governed by face_top_style alone, which is still "cap" here.
        assert any(p.kind == "top_cap" for p in panels)


# ─── carcass_panel_dims and face_layout must agree ──────────────────────────


class TestCarcassPanelDimsAgreesWithFaceLayout:
    """A 'flush' top emits no cap panel (only a taller face), same as
    'plain' in that one respect — but its carcass top panel is UNBANDED
    like 'cap's, because the tall face covers the same edge a cap strip
    would. carcass_panel_dims must never independently re-derive this."""

    @pytest.mark.parametrize("style,cap_panel_expected,top_banded", [
        ("plain", False, True),
        ("cap",   True,  False),
        ("flush", False, False),
    ])
    def test_agreement_across_all_three_styles(self, style, cap_panel_expected, top_banded):
        cfg = _cfg(face_top_style=style, face_bottom_style="flush")
        bays = bays_from_config(cfg, None)
        face_panels = face_layout(bays)
        has_cap = any(p.kind == "top_cap" for p in face_panels)
        assert has_cap is cap_panel_expected

        carcass = carcass_panel_dims(cfg)
        top = next(p for p in carcass if p.kind == "top")
        assert bool(top.banded_edges) is top_banded

    def test_flush_top_face_itself_covers_the_plane_a_cap_would(self):
        # The physical fact both functions must agree on without either
        # re-deriving it: under "flush" the tallest drawer face's top edge
        # reaches the true top-panel underside — the same z a cap strip's
        # underside would sit at under "cap".
        cap_cfg = _cfg(face_top_style="cap", face_bottom_style="flush")
        flush_cfg = _cfg(face_top_style="flush", face_bottom_style="flush")
        cap_top = next(p for p in face_layout(bays_from_config(cap_cfg, None))
                       if p.kind == "top_cap")
        flush_face = max(_drawer_faces(face_layout(bays_from_config(flush_cfg, None))),
                         key=lambda p: p.z)
        assert (flush_face.z + flush_face.height) == pytest.approx(
            cap_top.z + cap_top.height, abs=TOL)


class TestOpeninglessGate:
    """server._raw_panels_for_cabinet must print the cap row even with no
    openings ("cap" needs the gate); "flush" needs no gate — it produces
    no standalone panel of its own when there's no face to make taller."""

    def test_cap_reaches_paper_with_no_openings(self):
        cfg = _cfg(width=600, height=720, depth=500, openings=[],
                   face_top_style="cap", face_bottom_style="plain")
        _carcass, _thin, _box, faces = _raw_panels_for_cabinet(cfg, None)
        assert any(p.name == "top_front_cap" for p in faces)

    def test_flush_with_no_openings_prints_no_cap_row(self):
        cfg = _cfg(width=600, height=720, depth=500, openings=[],
                   face_top_style="flush", face_bottom_style="plain")
        _carcass, _thin, _box, faces = _raw_panels_for_cabinet(cfg, None)
        assert not any(p.name == "top_front_cap" for p in faces)


# ─── evaluation.check_face_top_bottom_style ─────────────────────────────────


class TestCheckFaceTopBottomStyle:
    def test_valid_combos_are_clean(self):
        for top in ("plain", "cap", "flush"):
            for bottom in ("plain", "flush"):
                cfg = _cfg(face_top_style=top, face_bottom_style=bottom)
                assert check_face_top_bottom_style(cfg) == []

    def test_unknown_top_style_errors(self):
        cfg = _cfg(face_top_style="bogus", face_bottom_style="flush")
        issues = check_face_top_bottom_style(cfg)
        assert any(i.severity == Severity.ERROR
                   and i.check == "face_top_bottom_style" for i in issues)

    def test_unknown_bottom_style_errors(self):
        cfg = _cfg(face_top_style="cap", face_bottom_style="bogus")
        issues = check_face_top_bottom_style(cfg)
        assert any(i.severity == Severity.ERROR
                   and i.check == "face_top_bottom_style" for i in issues)

    def test_wired_into_evaluate_cabinet(self):
        cfg = _cfg(face_top_style="nonsense")
        issues = evaluate_cabinet(cfg)
        assert any(i.check == "face_top_bottom_style" for i in issues)

    def test_flush_top_has_no_combinatorial_constraints(self):
        # "flush" only moves a face; it never cuts the carcass, so it must
        # be legal beside miter, dado/rabbet, or any back_capture.
        cfg = _cfg(face_top_style="flush", face_bottom_style="flush",
                   carcass_corner_style="miter")
        issues = check_face_top_bottom_style(cfg)
        assert issues == []


# ─── project.py: SharedDesign / round-trip / legacy key ────────────────────


class TestProjectPlumbing:
    def test_shared_design_has_the_two_new_fields_not_the_old_one(self):
        sd = SharedDesign(face_top_style="flush", face_bottom_style="plain")
        assert sd.face_top_style == "flush"
        assert sd.face_bottom_style == "plain"
        assert not hasattr(sd, "furniture_top")

    def test_shared_from_dict_translates_legacy_furniture_top(self):
        sd = shared_from_dict({"furniture_top": True})
        assert (sd.face_top_style, sd.face_bottom_style) == ("cap", "flush")
        sd2 = shared_from_dict({"furniture_top": False})
        assert (sd2.face_top_style, sd2.face_bottom_style) == ("plain", "plain")

    def test_shared_from_dict_explicit_style_beats_legacy_key(self):
        sd = shared_from_dict({"furniture_top": True, "face_top_style": "plain"})
        assert sd.face_top_style == "plain"
        assert sd.face_bottom_style == "flush"   # untouched axis still translates

    def test_shared_to_dict_round_trip(self):
        sd = shared_from_dict({"face_top_style": "flush",
                               "face_bottom_style": "plain"})
        assert _shared_to_dict(sd) == {"face_top_style": "flush",
                                       "face_bottom_style": "plain"}

    def test_config_round_trip_preserves_flush_without_going_through_the_lossy_bool(self):
        # The whole reason furniture_top can't be what's persisted: a
        # "flush" top's furniture_top property reads False, same as
        # "plain" — persisting that boolean would silently turn "flush"
        # into "plain" on the next load.
        cfg = _cfg(face_top_style="flush", face_bottom_style="plain")
        d = _config_to_dict(cfg)
        assert d["face_top_style"] == "flush"
        assert d["face_bottom_style"] == "plain"
        assert "furniture_top" not in d
        back = config_from_dict(d)
        assert back.face_top_style == "flush"
        assert back.face_bottom_style == "plain"
        assert back.furniture_top is False   # correctly NOT the cap/flush look

    def test_build_project_merges_shared_style_tokens(self):
        payload = {
            "name": "eval_face_style_merge_test",
            "shared": {"face_top_style": "flush", "face_bottom_style": "plain"},
            "cabinets": [{"name": "a", "config": {
                "width": 600, "height": 720, "depth": 500,
                "drawer_config": [[684, "drawer"]]}}],
        }
        proj = build_project(payload)
        cfg = proj.resolved()[0][1]
        assert cfg.face_top_style == "flush"
        assert cfg.face_bottom_style == "plain"

    def test_child_explicit_furniture_top_overrides_shared_style(self):
        # The legacy convenience key on a CHILD must be able to pin an
        # override against a shared style token, same as pull_preset.
        payload = {
            "name": "eval_face_style_override_test",
            "shared": {"face_top_style": "flush", "face_bottom_style": "flush"},
            "cabinets": [{"name": "a", "config": {
                "width": 600, "height": 720, "depth": 500,
                "furniture_top": False,
                "drawer_config": [[684, "drawer"]]}}],
        }
        proj = build_project(payload)
        cfg = proj.resolved()[0][1]
        assert (cfg.face_top_style, cfg.face_bottom_style) == ("plain", "plain")


# ─── B-tower flush-top regression (kid1-desk, verified ad hoc this session) ─


class TestBTowerFlushRegression:
    """The real kid1-desk 'tower-left' B-tower: height=1168, bottom/top=18,
    5 drawer openings summing to interior_height (1132), face_gap_mm=2.5,
    single column. Verified ad hoc this session before the style split
    existed (as a manual face_top_overhang=cfg.top_thickness override);
    this pins the SAME numbers now that it's a named style."""

    def _tower_cfg(self, **overrides) -> CabinetConfig:
        kwargs = dict(
            width=600.0, height=1168.0, depth=550.0,
            bottom_thickness=18.0, top_thickness=18.0,
            face_gap_mm=2.5,
            face_top_style="flush", face_bottom_style="flush",
            openings=[
                [296, "drawer"], [296, "drawer"], [180, "drawer"],
                [180, "drawer"], [180, "drawer"],
            ],
        )
        kwargs.update(overrides)
        return CabinetConfig(**kwargs)

    def test_top_row_is_196_75_mm(self):
        cfg = self._tower_cfg()
        faces = sorted(_drawer_faces(face_layout([cfg])), key=lambda p: p.z)
        assert len(faces) == 5
        assert faces[-1].height == pytest.approx(196.75, abs=0.01)

    def test_no_cap_panel_emitted(self):
        cfg = self._tower_cfg()
        panels = face_layout([cfg])
        assert not any(p.kind == "top_cap" for p in panels)

    def test_full_row_set_matches_the_verified_stack(self):
        cfg = self._tower_cfg()
        faces = sorted(_drawer_faces(face_layout([cfg])), key=lambda p: p.z)
        heights = [round(p.height, 2) for p in faces]
        assert heights == [312.75, 293.5, 177.5, 177.5, 196.75]

    def test_stack_still_tiles_the_full_exterior_height(self):
        # flush top + flush bottom: faces + 4 gaps span the WHOLE height,
        # not just the interior — the physical predicate the closure net
        # insists on beside any agreement check.
        cfg = self._tower_cfg()
        faces = sorted(_drawer_faces(face_layout([cfg])), key=lambda p: p.z)
        span = (faces[-1].z + faces[-1].height) - faces[0].z
        total_gap = 4 * cfg.face_gap_mm
        assert span + 0 == pytest.approx(cfg.height, abs=TOL)
        assert sum(p.height for p in faces) + total_gap == pytest.approx(
            cfg.height, abs=TOL)

    def test_claims_close_via_assert_face_heights_close(self):
        cfg = self._tower_cfg()
        claims = [
            FaceHeightClaim("row0", 312.75), FaceHeightClaim("row1", 293.5),
            FaceHeightClaim("row2", 177.5), FaceHeightClaim("row3", 177.5),
            FaceHeightClaim("row4", 196.75),
        ]
        # Must not raise.
        assert_face_heights_close(
            [cfg], claims, face_gap=2.5,
            face_top_style="flush", face_bottom_style="flush")

    def test_a_cap_style_claim_set_is_rejected_for_this_flush_config(self):
        # The P1 mismatch-detection shape (test_bench_card.py's
        # test_b_tower_furniture_top_mismatch_raises), replayed on the new
        # axis: claims computed for the "cap" stack must NOT silently pass
        # against the real "flush" geometry.
        cfg = self._tower_cfg()
        cap_cfg = self._tower_cfg(face_top_style="cap", face_bottom_style="flush")
        cap_faces = sorted(_drawer_faces(face_layout([cap_cfg])), key=lambda p: p.z)
        wrong_claims = [FaceHeightClaim(f"row{i}", round(p.height, 2))
                        for i, p in enumerate(cap_faces)]
        with pytest.raises(ValueError):
            assert_face_heights_close(
                [cfg], wrong_claims, face_gap=2.5,
                face_top_style="flush", face_bottom_style="flush")
