"""Tests for back_capture — how the back is HELD in the carcass.

An axis of its own, independent of ``carcass_joinery`` (how the corners go
together) and ``back_style`` (where the back's top edge lands):

  pocket   — the legacy let-in back. Nothing machined; the back laps onto
             the rear edges of the interior panels. The only capture that
             seats OUTSIDE the top and bottom, so the only one where
             back_style changes what you see.
  rabbet   — a rabbet in the rear inner edge of sides, top and bottom. The
             back drops in from behind after glue-up, flush with the rear.
  half_lap — both halves rabbeted to half the back's thickness so they lap:
             two glue planes, and the step traps the panel fore-and-aft.
  dado     — a groove in all four members. The back slides in during
             glue-up and is trapped on four edges.

The pocket cases here are regression locks: this feature must not move a
single dimension on anything already in the shop.
"""

import pytest

from cabineteer.assembly import build_assembly_plan
from cabineteer.cabinet import (BACK_CAPTURES, CabinetConfig,
                                HALF_LAP_MIN_BACK_MM, back_capture_geometry,
                                build_cabinet_config)
from cabineteer.evaluation import Severity, check_back_capture
from cabineteer.joinery import CarcassJoinery
from cabineteer.project import (CabinetProject, ProjectCabinet, SharedDesign,
                                project_from_dict, project_to_dict)
from cabineteer.server import _raw_panels_for_cabinet

try:
    import cadquery as cq
except ImportError:  # pragma: no cover - lite install
    cq = None

requires_cq = pytest.mark.skipif(cq is None, reason="cadquery not installed")

# 600 wide × 720 high × 550 deep, 18 mm stock: interior 564 × 684, and a
# 6 mm engagement puts the back at 576 × 696 for every machined capture.
W, H, D = 600.0, 720.0, 550.0
INTERIOR_W, INTERIOR_H = 564.0, 684.0
ENGAGED_W, ENGAGED_H = 576.0, 696.0


def _cfg(**kw) -> CabinetConfig:
    kw.setdefault("carcass_joinery", CarcassJoinery.FLOATING_TENON)
    return CabinetConfig(width=W, height=H, depth=D, **kw)


def _panel(panels, name):
    return next(p for p in panels if p.name == name)


class TestGeometry:
    def test_pocket_is_the_default(self):
        assert _cfg().back_capture == "pocket"

    def test_pocket_geometry_unchanged(self):
        geo = back_capture_geometry(_cfg())
        assert geo.width == pytest.approx(INTERIOR_W)
        assert geo.height == pytest.approx(H)          # runs to the top plane
        assert geo.engagement == 0
        assert geo.clear_depth == pytest.approx(6.0)
        assert geo.top_depth == pytest.approx(D - 6)
        assert not geo.machined
        assert not geo.captive

    def test_pocket_still_honours_back_style(self):
        geo = back_capture_geometry(_cfg(back_style="under_top"))
        assert geo.height == pytest.approx(H - 18)     # stops under the top
        assert geo.top_depth == pytest.approx(D)       # top runs full depth
        assert geo.captive                             # goes in at glue-up

    @pytest.mark.parametrize("capture", ["rabbet", "half_lap", "dado"])
    def test_machined_captures_seat_inside_the_perimeter(self, capture):
        """Back is cut oversize by the engagement on all four edges, and the
        top and bottom both run full depth to carry it."""
        geo = back_capture_geometry(_cfg(back_capture=capture,
                                         back_thickness=12.0))
        assert geo.width == pytest.approx(ENGAGED_W)
        assert geo.height == pytest.approx(ENGAGED_H)
        assert geo.engagement == pytest.approx(6.0)
        assert geo.top_depth == pytest.approx(D)
        assert geo.bottom_depth == pytest.approx(D)
        assert geo.machined

    @pytest.mark.parametrize("capture", ["rabbet", "half_lap", "dado"])
    def test_back_style_cannot_change_a_machined_capture(self, capture):
        """The back is inside the perimeter either way, so the cap is moot."""
        plain = back_capture_geometry(_cfg(back_capture=capture))
        capped = back_capture_geometry(
            _cfg(back_capture=capture, back_style="under_top"))
        assert plain == capped

    def test_rabbet_cut_takes_the_backs_full_thickness(self):
        geo = back_capture_geometry(_cfg(back_capture="rabbet",
                                         back_thickness=12.0))
        assert geo.cut_depth == pytest.approx(6.0)     # into the inner face
        assert geo.cut_run == pytest.approx(12.0)      # the whole back
        assert geo.cut_offset == 0                     # opens at the rear
        assert geo.lap_depth == 0                      # back stays a rectangle
        assert not geo.captive                         # drops in from behind

    def test_half_lap_splits_the_thickness_between_both_halves(self):
        geo = back_capture_geometry(_cfg(back_capture="half_lap",
                                         back_thickness=12.0))
        assert geo.cut_run == pytest.approx(6.0)       # case takes half
        assert geo.lap_depth == pytest.approx(6.0)     # back takes the other
        assert geo.lap_run == pytest.approx(geo.engagement)
        assert geo.cut_run + geo.lap_depth == pytest.approx(12.0)
        assert not geo.captive

    def test_dado_holds_the_back_forward_and_traps_it(self):
        geo = back_capture_geometry(_cfg(back_capture="dado"))
        assert geo.setback == pytest.approx(12.0)
        assert geo.cut_offset == pytest.approx(12.0)
        assert geo.clear_depth == pytest.approx(18.0)  # setback + thickness
        assert geo.captive                             # cannot go in later

    def test_dado_costs_interior_depth_the_pocket_does_not(self):
        """Drawer boxes size off interior_depth, so the capture has to move
        it — a groove holds the back 12 mm further forward."""
        assert _cfg().interior_depth == pytest.approx(D - 9)
        assert _cfg(back_capture="dado").interior_depth == pytest.approx(D - 18)

    def test_interior_depth_never_grows(self):
        """A shallower capture must not silently deepen boxes already cut."""
        for capture in BACK_CAPTURES:
            cfg = _cfg(back_capture=capture)
            assert cfg.interior_depth <= _cfg().interior_depth


class TestCutlistPanels:
    def test_pocket_panels_unchanged(self):
        carcass, six_mm, _, _ = _raw_panels_for_cabinet(_cfg(), None)
        assert _panel(carcass, "top").width == pytest.approx(D - 6)
        assert _panel(carcass, "bottom").width == pytest.approx(D - 6)
        back = _panel(six_mm, "back")
        assert (back.length, back.width) == pytest.approx((H, INTERIOR_W))
        assert "rabbet" not in _panel(carcass, "side").notes

    @pytest.mark.parametrize("capture", ["rabbet", "half_lap", "dado"])
    def test_machined_panels_carry_the_cut_and_the_oversize(self, capture):
        cfg = _cfg(back_capture=capture, back_thickness=12.0)
        carcass, six_mm, _, _ = _raw_panels_for_cabinet(cfg, None)
        # Top and bottom run full depth to carry the back.
        assert _panel(carcass, "top").width == pytest.approx(D)
        assert _panel(carcass, "bottom").width == pytest.approx(D)
        back = _panel(six_mm, "back")
        assert (back.length, back.width) == pytest.approx((ENGAGED_H, ENGAGED_W))
        # Every perimeter member tells the builder what to machine.
        for name in ("side", "top", "bottom"):
            notes = _panel(carcass, name).notes
            assert ("groove" if capture == "dado" else "rabbet") in notes
            assert "6 mm deep" in notes

    def test_shelves_stop_at_the_backs_front_face(self):
        cfg = _cfg(back_capture="dado", fixed_shelf_positions=[300.0])
        carcass, _, _, _ = _raw_panels_for_cabinet(cfg, None)
        # setback 12 + thickness 6 = 18 held off the rear, not 6.
        assert _panel(carcass, "shelf_1").width == pytest.approx(D - 18)

    def test_half_lap_back_row_states_its_own_rabbet(self):
        cfg = _cfg(back_capture="half_lap", back_thickness=12.0)
        _, six_mm, _, _ = _raw_panels_for_cabinet(cfg, None)
        notes = _panel(six_mm, "back").notes
        assert "FRONT face" in notes
        assert "6 mm deep" in notes

    def test_captive_back_says_so_on_the_cutlist(self):
        _, six_mm, _, _ = _raw_panels_for_cabinet(_cfg(back_capture="dado"), None)
        assert "cannot go in afterwards" in _panel(six_mm, "back").notes
        _, six_mm, _, _ = _raw_panels_for_cabinet(_cfg(back_capture="rabbet"), None)
        assert "from behind" in _panel(six_mm, "back").notes


class TestEvaluator:
    def test_pocket_silent(self):
        assert check_back_capture(_cfg()) == []

    @pytest.mark.parametrize("capture", ["rabbet", "half_lap", "dado"])
    def test_valid_machined_captures_silent(self, capture):
        assert check_back_capture(_cfg(back_capture=capture,
                                       back_thickness=12.0)) == []

    def test_unknown_capture_errors(self):
        issues = check_back_capture(_cfg(back_capture="biscuit_slot"))
        assert len(issues) == 1
        assert "Unknown back_capture" in issues[0].message

    def test_half_lap_rejects_a_back_too_thin_to_split(self):
        issues = check_back_capture(_cfg(back_capture="half_lap",
                                         back_thickness=6.0))
        assert [i.severity for i in issues] == [Severity.ERROR]
        assert "3 mm lap" in issues[0].message
        assert issues[0].limit == HALF_LAP_MIN_BACK_MM

    def test_half_lap_accepts_the_minimum(self):
        assert check_back_capture(_cfg(back_capture="half_lap",
                                       back_thickness=HALF_LAP_MIN_BACK_MM)) == []

    def test_cut_must_leave_a_wall_standing(self):
        issues = check_back_capture(_cfg(back_capture="dado",
                                         back_rabbet_depth=14.0))
        assert any("blow out" in i.message for i in issues)

    def test_dado_needs_meat_behind_the_groove(self):
        issues = check_back_capture(_cfg(back_capture="dado",
                                         back_groove_setback=3.0))
        assert any("break out" in i.message for i in issues)

    def test_machined_capture_rejects_dado_rabbet_joinery(self):
        issues = check_back_capture(
            _cfg(back_capture="dado", carcass_joinery=CarcassJoinery.DADO_RABBET))
        assert any("already houses the back" in i.message for i in issues)

    def test_machined_capture_rejects_mitered_corners(self):
        issues = check_back_capture(
            _cfg(back_capture="dado", carcass_corner_style="miter"))
        assert any("exits through the 45°" in i.message for i in issues)


class TestAssemblyDoc:
    def test_pocket_has_no_machining_step(self):
        plan = build_assembly_plan(_cfg())
        assert not any(s.title == "Machine the back capture" for s in plan.steps)

    @pytest.mark.parametrize("capture,word", [
        ("rabbet", "rabbet"), ("half_lap", "rabbet"), ("dado", "groove")])
    def test_machined_captures_get_a_machining_step(self, capture, word):
        plan = build_assembly_plan(_cfg(back_capture=capture,
                                        back_thickness=12.0))
        step = next(s for s in plan.steps
                    if s.title == "Machine the back capture")
        assert word in step.body
        # One setup covers all four members.
        assert "One fence setting" in step.body

    def test_half_lap_step_covers_the_backs_own_rabbet(self):
        plan = build_assembly_plan(_cfg(back_capture="half_lap",
                                        back_thickness=12.0))
        step = next(s for s in plan.steps
                    if s.title == "Machine the back capture")
        assert "rabbet the BACK panel itself" in step.body

    def test_captive_back_warns_it_goes_in_at_glue_up(self):
        plan = build_assembly_plan(_cfg(back_capture="dado"))
        text = " ".join(s.body for s in plan.steps)
        assert "cannot go in afterwards" in text
        assert "it must go in NOW" in text

    def test_dado_step_covers_solid_wood_movement(self):
        plan = build_assembly_plan(_cfg(back_capture="dado"))
        text = " ".join(s.body for s in plan.steps)
        assert "move in the grooves" in text

    def test_rabbet_back_drops_in_from_behind(self):
        plan = build_assembly_plan(_cfg(back_capture="rabbet"))
        text = " ".join(s.body for s in plan.steps)
        assert "from behind" in text
        assert "cannot go in afterwards" not in text


class TestPersistence:
    def test_back_capture_is_a_shared_token(self):
        project = CabinetProject(
            name="t",
            shared=SharedDesign(back_capture="dado", back_groove_setback=15.0),
            cabinets=(ProjectCabinet(name="a", config=_cfg()),),
        )
        (_, resolved), = project.resolved()
        assert resolved.back_capture == "dado"
        assert resolved.back_groove_setback == pytest.approx(15.0)

    def test_per_cabinet_override_beats_the_token(self):
        project = CabinetProject(
            name="t",
            shared=SharedDesign(back_capture="dado"),
            cabinets=(ProjectCabinet(
                name="a", config=_cfg(back_capture="rabbet"),
                overrides=frozenset({"back_capture"})),),
        )
        (_, resolved), = project.resolved()
        assert resolved.back_capture == "rabbet"

    def test_round_trips_through_the_snapshot(self):
        """back_style rode along in this fix — it was dropped on save."""
        project = CabinetProject(
            name="t",
            cabinets=(ProjectCabinet(
                name="a", config=_cfg(back_capture="half_lap",
                                      back_thickness=12.0,
                                      back_style="under_top")),),
        )
        again = project_from_dict(project_to_dict(project))
        (_, resolved), = again.resolved()
        assert resolved.back_capture == "half_lap"
        assert resolved.back_style == "under_top"

    def test_build_cabinet_config_accepts_the_flat_keys(self):
        cfg = build_cabinet_config(
            {"width": W, "height": H, "depth": D,
             "back_capture": "dado", "back_groove_setback": 15.0})
        assert cfg.back_capture == "dado"
        assert cfg.back_groove_setback == pytest.approx(15.0)


@requires_cq
class TestSolidGeometry:
    """The 3D model must match the paper — the back has to seat in the cut
    the cutlist told him to make, with no interference and no float."""

    @staticmethod
    def _solids(assy):
        return {ch.name: ch.obj.val().moved(ch.loc)
                for ch in assy.children if ch.obj is not None}

    @staticmethod
    def _build(**kw):
        from cabineteer.cabinet import build_cabinet
        assy, _ = build_cabinet(_cfg(**kw))
        return TestSolidGeometry._solids(assy)

    def test_pocket_machines_nothing(self):
        s = self._build()
        assert s["left_side"].Volume() == pytest.approx(18 * D * H)

    @pytest.mark.parametrize("capture", ["rabbet", "half_lap", "dado"])
    def test_back_does_not_interfere_with_the_case(self, capture):
        s = self._build(back_capture=capture, back_thickness=12.0)
        for member in ("left_side", "right_side", "top", "bottom"):
            common = s["back"].intersect(s[member])
            volume = common.Volume() if common.Solids() else 0.0
            assert volume == pytest.approx(0.0, abs=1e-6), (
                f"{capture}: back overlaps {member} by {volume:g} mm³")

    @pytest.mark.parametrize("capture", ["rabbet", "half_lap", "dado"])
    def test_back_engages_both_sides(self, capture):
        """It has to actually reach into the cut, not just touch the face."""
        s = self._build(back_capture=capture, back_thickness=12.0)
        bb = s["back"].BoundingBox()
        assert bb.xmin == pytest.approx(18 - 6)        # 6 mm into the left
        assert bb.xmax == pytest.approx(W - 18 + 6)    # and into the right

    def test_rabbet_and_half_lap_finish_flush_with_the_case_rear(self):
        for capture in ("rabbet", "half_lap"):
            s = self._build(back_capture=capture, back_thickness=12.0)
            assert s["back"].BoundingBox().ymax == pytest.approx(D)

    def test_dado_holds_the_back_forward_of_the_rear(self):
        s = self._build(back_capture="dado")
        assert s["back"].BoundingBox().ymax == pytest.approx(D - 12)

    def test_side_cut_matches_the_declared_profile(self):
        for capture, run in (("rabbet", 12.0), ("half_lap", 6.0),
                             ("dado", 12.0)):
            s = self._build(back_capture=capture, back_thickness=12.0)
            removed = 18 * D * H - s["left_side"].Volume()
            assert removed == pytest.approx(6.0 * run * H), capture

    def test_multi_bay_back_engages_the_outer_sides(self):
        from cabineteer.cabinet import OpeningConfig, build_multi_bay_cabinet
        bays = [_cfg(back_capture="dado",
                     openings=[OpeningConfig(height_mm=680.0,
                                             opening_type="drawer")])
                for _ in range(2)]
        assy, _ = build_multi_bay_cabinet(bays)
        solids = self._solids(assy)
        bb = solids["back"].BoundingBox()
        # Adjacent bays share one side, so measure the engagement against
        # the continuous top (which spans the interior) rather than
        # assuming the total width.
        top = solids["top"].BoundingBox()
        assert bb.xmin == pytest.approx(top.xmin - 6)
        assert bb.xmax == pytest.approx(top.xmax + 6)
        assert bb.ymax == pytest.approx(D - 12)
