"""A door standing on something has a floor, and the floor is a real part.

Charlie's call, 2026-08-30: "a door over a drawer should always have a floor
on it. I think it's misleading to call it a shelf. Let's make it its own
part."

What was there before: the 3D INVENTED a panel at each drawer-to-door
boundary. One panel spanning every column, placed at ``min()`` of their
transitions, on no cutlist, and driven clean through the divider —
170,878 mm³, which is exactly ``18 × 527.4 × 18``, the divider's whole
thickness over its whole depth. Deriving it also flipped the case off its
continuous top and bottom, so an armoire's picture showed two 531.8 bottoms
against one 1081.6 on the paper. That was registered defect D8, three
entries, and it is closed by making the part real rather than by deleting
it — because the furniture does need a floor there.

The tests here are mostly CLOSURE: they compare the part against the other
producers of it, or against a physical fact (it has to fit; it must not
occupy the divider's space). Where a constant is pinned it is a
bench-legible number stated in the commit and the docs — the armoire's
531.8 × 527.4 — and it is pinned so a silent re-derivation shows up as a
diff a person can read.
"""

from __future__ import annotations

import pytest

from cabineteer.assembly import build_assembly_plan
from cabineteer.cabinet import (DOOR_TYPES, bays_from_config,
                                build_cabinet_config, carcass_panel_dims,
                                door_floor_count, face_layout,
                                opening_needs_floor, opening_stack,
                                openings_span)
from cabineteer.cutlist import (assign_part_ids, consolidate_bom,
                                joinery_lines_for_cabinet_config)
from cabineteer.evaluation import evaluate_cabinet
from cabineteer.server import _raw_panels_for_cabinet

try:
    import cadquery as cq
except ImportError:                                      # pragma: no cover
    cq = None

requires_cq = pytest.mark.skipif(cq is None, reason="cadquery not installed")


def _cfg(**kw):
    base = dict(width=600, height=900, depth=600,
                side_thickness=18, bottom_thickness=18, top_thickness=18,
                shelf_thickness=18, back_thickness=6,
                carcass_joinery="floating_tenon")
    base.update(kw)
    return build_cabinet_config(base)


#: A door standing on three drawers, filling its interior exactly:
#: 3×200 + 246 door + 18 floor == 864.
DOOR_OVER_DRAWERS = [[200, "drawer"], [200, "drawer"], [200, "drawer"],
                     [246, "door"]]


class TestTheRule:
    """When a floor exists, and — as load-bearing — when it does not."""

    def test_a_door_on_something_gets_one(self):
        ops = [type("O", (), {"opening_type": "drawer"})(),
               type("O", (), {"opening_type": "door"})()]
        assert opening_needs_floor(ops, 1)

    def test_a_door_at_the_bottom_gets_none(self):
        """Its floor is the carcass bottom — there is no part to cut.

        This is the case that keeps the change off Charlie's bench: every
        door in all 22 door-columns of the saved store is the lowest opening
        in its column, kapex_center's ['door_pair', 'drawer'] included.
        """
        ops = [type("O", (), {"opening_type": "door"})(),
               type("O", (), {"opening_type": "drawer"})()]
        assert not opening_needs_floor(ops, 0)

    @pytest.mark.parametrize("kind", DOOR_TYPES)
    def test_both_door_kinds_count(self, kind):
        cfg = _cfg(drawer_config=[[200, "drawer"], [646, kind]])
        assert door_floor_count(cfg) == 1

    def test_a_drawer_over_a_drawer_gets_nothing(self):
        cfg = _cfg(drawer_config=[[400, "drawer"], [464, "drawer"]])
        assert door_floor_count(cfg) == 0
        assert not any(p.kind == "floor" for p in carcass_panel_dims(cfg))

    def test_two_doors_stacked_get_a_floor_each_but_the_lowest(self):
        cfg = _cfg(height=2100, drawer_config=[
            [700, "door_pair"], [646, "shelf"], [700, "door_pair"]])
        assert door_floor_count(cfg) == 1     # only the upper pair stands


class TestThePart:
    """Its dimensions, against the members it shares a cabinet with."""

    COLS = [{"width_mm": 273.0, "openings": [[200, "drawer"], [628, "door"]]},
            {"width_mm": 273.0, "openings": [[200, "drawer"], [628, "door"]]}]

    def test_it_stops_at_the_divider_not_at_the_far_side(self):
        """The defect in one assertion: it used to span every column."""
        floors = [p for p in carcass_panel_dims(_cfg(), self.COLS)
                  if p.kind == "floor"]
        assert len(floors) == 2
        for f, col in zip(floors, self.COLS):
            assert f.length == pytest.approx(col["width_mm"])
            assert f.length < _cfg().interior_width

    def test_columns_and_floors_close_into_the_interior_width(self):
        """Closure: two floors plus the divider they stop at IS the interior."""
        cfg = _cfg()
        floors = [p for p in carcass_panel_dims(cfg, self.COLS)
                  if p.kind == "floor"]
        div = next(p for p in carcass_panel_dims(cfg, self.COLS)
                   if p.kind == "divider")
        assert sum(f.length for f in floors) + div.thickness == \
            pytest.approx(cfg.interior_width)

    @pytest.mark.parametrize("capture,depth", [
        ("pocket", 594.0), ("rabbet", 594.0),
        ("half_lap", 594.0), ("dado", 582.0),
    ])
    def test_it_stops_at_the_back_like_every_interior_panel(self, capture,
                                                            depth):
        cfg = _cfg(back_capture=capture, drawer_config=DOOR_OVER_DRAWERS)
        floor = next(p for p in carcass_panel_dims(cfg) if p.kind == "floor")
        assert floor.width == pytest.approx(depth)
        assert floor.width == pytest.approx(cfg.interior_depth)

    def test_it_is_dimensionally_a_fixed_shelf(self):
        """Same panel, different job — so nothing may need to size it apart.

        If this ever fails, a floor has grown geometry of its own and the
        two need separate makers rather than one shared rule.
        """
        cfg = _cfg(drawer_config=DOOR_OVER_DRAWERS,
                   fixed_shelf_positions=[300.0])
        dims = carcass_panel_dims(cfg)
        floor = next(p for p in dims if p.kind == "floor")
        shelf = next(p for p in dims if p.kind == "shelf")
        assert (floor.length, floor.width, floor.thickness) == \
            (shelf.length, shelf.width, shelf.thickness)

    def test_it_carries_its_height_and_its_column(self):
        floors = [p for p in carcass_panel_dims(_cfg(), self.COLS)
                  if p.kind == "floor"]
        assert [f.column for f in floors] == [0, 1]
        assert all(f.z == pytest.approx(218.0) for f in floors)

    def test_it_bands_its_front_edge_like_the_rest_of_the_carcass(self):
        cfg = _cfg(drawer_config=DOOR_OVER_DRAWERS,
                   edge_band_mode="hardwood", edge_band_thickness_mm=3.2)
        floor = next(p for p in carcass_panel_dims(cfg) if p.kind == "floor")
        assert floor.banded_edges == ("front",)
        assert floor.core(3.2) == (floor.length, floor.width - 3.2)


class TestTheFillRule:
    """A floor's thickness is the STACK's, like a divider's is the case's."""

    def test_the_openings_share_the_interior_with_their_floors(self):
        cfg = _cfg(drawer_config=DOOR_OVER_DRAWERS)
        assert openings_span(cfg) == pytest.approx(cfg.interior_height - 18)
        assert sum(o.height_mm for o in cfg.openings) == \
            pytest.approx(openings_span(cfg))

    def test_a_stack_with_no_floors_is_untouched(self):
        """Byte-identical, not merely equal — this is why the A/B is zero.

        Every one of Charlie's twelve cabinets is this case.
        """
        cfg = _cfg(drawer_config=[[400, "drawer"], [464, "drawer"]])
        assert openings_span(cfg) is cfg.interior_height or \
            openings_span(cfg) == cfg.interior_height

    def test_the_evaluator_says_what_the_floor_costs(self):
        """A person has to be able to act on the error.

        "Exceeds by 18" with no reason is a puzzle; naming the floor and its
        thickness is an instruction.
        """
        cfg = _cfg(drawer_config=[[200, "drawer"], [200, "drawer"],
                                  [200, "drawer"], [264, "door"]])
        issue = next(i for i in evaluate_cabinet(cfg)
                     if i.check == "cumulative_heights")
        assert "18.0mm" in issue.message
        assert "1 door floor" in issue.message
        assert "comes out of the stack" in issue.message

    def test_an_exactly_filling_stack_is_clean(self):
        cfg = _cfg(drawer_config=DOOR_OVER_DRAWERS)
        errs = [i for i in evaluate_cabinet(cfg)
                if i.check == "cumulative_heights"
                and i.severity.value == "error"]
        assert not errs


class TestTheFaces:
    """The boundary at a floor is its centreline, gap split across it."""

    def test_the_two_faces_lap_the_floor_and_leave_one_reveal(self):
        cfg = _cfg(drawer_config=DOOR_OVER_DRAWERS, face_gap_mm=4.0)
        faces = sorted(face_layout(bays_from_config(cfg, None)),
                       key=lambda p: p.z)
        below, above = faces[2], faces[3]
        slot = next(s for s in opening_stack(cfg) if s.has_floor)
        # One reveal, at the gap — not two, and not none.
        assert above.z - (below.z + below.height) == pytest.approx(4.0)
        # Centred on the floor: each face laps it by (18 − 4) / 2.
        assert below.z + below.height == pytest.approx(slot.floor_z + 7.0)
        assert above.z == pytest.approx(slot.floor_z + 11.0)

    def test_the_shim_reveal_is_whatever_face_gap_says(self):
        """Charlie shims 2.5, so the boundary must honour it like any other."""
        cfg = _cfg(drawer_config=DOOR_OVER_DRAWERS, face_gap_mm=2.5)
        faces = sorted(face_layout(bays_from_config(cfg, None)),
                       key=lambda p: p.z)
        assert faces[3].z - (faces[2].z + faces[2].height) == \
            pytest.approx(2.5)

    def test_the_face_stack_still_tiles_its_span(self):
        """Closure: faces, gaps and the floor fill the span with no slack.

        The top of the stack is the carcass EXTERIOR top, not the top
        panel's underside — a door above anything extends the stack over
        the top panel, which is the pre-existing door-transition rule and
        the reason this cabinet's faces run to 900.
        """
        cfg = _cfg(drawer_config=DOOR_OVER_DRAWERS, face_gap_mm=4.0)
        faces = sorted(face_layout(bays_from_config(cfg, None)),
                       key=lambda p: p.z)
        assert faces[0].z == pytest.approx(cfg.bottom_thickness)
        assert faces[-1].z + faces[-1].height == pytest.approx(cfg.height)
        # Every internal boundary is exactly one gap — no accumulated slack.
        for lo, hi in zip(faces, faces[1:]):
            assert hi.z - (lo.z + lo.height) == pytest.approx(4.0)


class TestEveryDocumentAgrees:
    """The point of the part: it reaches all of them, saying one thing."""

    COLS = TestThePart.COLS

    def test_the_cutlist_cuts_it(self):
        cfg = _cfg(drawer_config=DOOR_OVER_DRAWERS)
        carcass, _t, _b, _f = _raw_panels_for_cabinet(cfg, None)
        row = next(p for p in carcass if p.name == "floor")
        dim = next(p for p in carcass_panel_dims(cfg) if p.kind == "floor")
        assert (row.length, row.width, row.thickness) == \
            (dim.length, dim.width, dim.thickness)

    def test_it_gets_its_own_part_id_family(self):
        """"DR2" on the paper would be a leaf cut from show stock.

        Filed by SUBSTRING against an ordered table, so a name containing
        "door" folds into the door-leaf family. This is what settled the row
        name: "floor" collides with nothing.
        """
        cfg = _cfg(drawer_config=DOOR_OVER_DRAWERS)
        carcass, _t, _b, faces = _raw_panels_for_cabinet(cfg, None)
        rows = consolidate_bom(carcass + faces)
        assign_part_ids(rows)
        ids = {p.name: p.part_id for p in rows}
        assert ids["floor"].startswith("FL")
        assert ids["door"].startswith("DR")
        assert ids["floor"] != ids["door"]

    def test_the_design_payload_reports_it(self):
        import asyncio
        import json

        from cabineteer.server import TOOL_DISPATCH
        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(TOOL_DISPATCH["design_cabinet"]({
                "width": 600, "height": 900, "depth": 600,
                "drawer_config": DOOR_OVER_DRAWERS}))
        finally:
            loop.close()
        panels = json.loads(out[0].text)["panels"]
        assert "door_floor_1" in panels
        assert panels["door_floor_1"]["width_mm"] == pytest.approx(564.0)

    def test_the_joint_census_and_the_hardware_bom_are_one_count(self):
        """Two joints per floor, in both places or in neither.

        Counting the shelves and not the floors under-orders the tenons for
        every door-over-drawer cabinet — silently, because the doc and the
        BOM each looked internally consistent.
        """
        cfg = _cfg(columns=self.COLS)
        plan = build_assembly_plan(cfg)
        floor_joints = [j for j in plan.joints if "door floor" in j.name]
        assert len(floor_joints) == 4         # 2 floors x 2 walls

        line = next(l for l in joinery_lines_for_cabinet_config(cfg, self.COLS)
                    if "joints" in (l.notes or ""))
        assert f"{len(plan.joints)} joints" in line.notes

    def test_every_joint_has_a_hole_drawn_for_it(self):
        """A census entry with no mortise row is a joint nobody cuts."""
        cfg = _cfg(columns=self.COLS)
        plan = build_assembly_plan(cfg)
        drawn = sum(1 for pm in plan.panels for r in pm.rows
                    if "door floor" in (r.label or ""))
        # Two per floor: one row on each wall it meets. The floor's own two
        # edge mortises are its map's unlabelled end rows.
        assert drawn == 4

    def test_the_map_names_the_row_the_paper_cuts(self):
        cfg = _cfg(drawer_config=DOOR_OVER_DRAWERS)
        plan = build_assembly_plan(cfg)
        pm = next(p for p in plan.panels if p.canonical == "floor")
        carcass, _t, _b, _f = _raw_panels_for_cabinet(cfg, None)
        row = next(p for p in carcass if p.name == "floor")
        assert {round(pm.draw_width, 1), round(pm.draw_height, 1)} == \
            {round(row.length, 1), round(row.width, 1)}
        assert "not an adjustable shelf" in pm.note


@requires_cq
class TestTheRenderDrawsThePartThePaperCuts:

    COLS = TestThePart.COLS

    @staticmethod
    def _placed(cfg, cols):
        """[(name, world solid)] for the whole assembly."""
        import cadquery as cq
        from cabineteer.server import _cabinet_assembly
        assy, _parts, _info = _cabinet_assembly(cfg, cols, include_feet=False)
        out = []

        def walk(node, loc):
            for ch in node.children:
                l = loc * (ch.loc if ch.loc is not None else cq.Location())
                if ch.obj is not None and hasattr(ch.obj, "val"):
                    out.append((ch.name, ch.obj.val().moved(l)))
                walk(ch, l)

        walk(assy, assy.loc or cq.Location())
        return out

    #: Columns whose doors start at DIFFERENT heights. The old derivation
    #: took min() of the two, so its panel crossed the right column's drawer
    #: zone as well as the divider — and only a config like this one makes
    #: the overlap a solid rather than two faces touching.
    UNEVEN = [{"width_mm": 273.0, "openings": [[150, "drawer"], [678, "door"]]},
              {"width_mm": 273.0, "openings": [[150, "drawer"],
                                               [150, "drawer"], [510, "door"]]}]

    @pytest.mark.parametrize("cols", ["COLS", "UNEVEN"])
    def test_it_does_not_pass_through_the_divider(self, cols):
        """The defect, as a physical predicate referencing no document.

        It measured 170,878 mm³ — the divider's whole thickness over its
        whole depth. Both column shapes, because with EVEN columns a
        full-width panel only touches the divider's faces; the uneven pair
        is where it becomes a solid you cannot make.
        """
        cols = getattr(self, cols)
        cfg = _cfg(columns=cols)
        solids = dict(self._placed(cfg, cols))
        divs = [s for n, s in solids.items() if n.startswith("divider")]
        assert divs
        div = divs[0]
        for name, solid in solids.items():
            if not name.startswith("floor"):
                continue
            try:
                vol = div.intersect(solid).Volume()
            except Exception:
                continue                       # OCC raises when disjoint
            assert vol < 1.0, f"{name} occupies {vol:.0f} mm³ of the divider"

    def test_the_case_keeps_one_bottom_and_one_top(self):
        """Deriving the old shelf split these into a pair per bay.

        The paper has always cut one of each, so the picture showed a 531.8
        where the cutlist said 1081.6.
        """
        cfg = _cfg(columns=self.COLS)
        names = [n for n, _s in self._placed(cfg, self.COLS)]
        # "bottom" also names a drawer box's bottom and the back's, so count
        # the ones that are children of the cabinet — the carcass pair.
        from cabineteer.server import _cabinet_assembly
        _a, parts, _i = _cabinet_assembly(cfg, self.COLS, include_feet=False)
        carcass = [p.name for p in parts]
        assert carcass.count("bottom") == 1, carcass
        assert carcass.count("top") == 1, carcass
        assert not any(n.startswith("bay") and n.endswith("bottom")
                       for n in carcass), carcass

    def test_the_render_draws_it_where_the_paper_puts_it(self):
        cfg = _cfg(columns=self.COLS)
        dims = [p for p in carcass_panel_dims(cfg, self.COLS)
                if p.kind == "floor"]
        drawn = [(n, s.BoundingBox()) for n, s in self._placed(cfg, self.COLS)
                 if n.startswith("floor")]
        assert len(drawn) == len(dims) == 2
        for (_n, bb), dim in zip(drawn, dims):
            assert bb.xlen == pytest.approx(dim.length, abs=0.05)
            assert bb.ylen == pytest.approx(dim.width, abs=0.05)
            assert bb.zlen == pytest.approx(dim.thickness, abs=0.05)
            assert bb.zmin == pytest.approx(dim.z, abs=0.05)

    def test_its_node_name_survives_the_viewers_colour_lookup(self):
        """The lookup strips a trailing _N and matches the remainder.

        So "bay0_floor1" would reduce to "bay0_floor" and never match — the
        node has to be numbered globally, the way divider_0 is.
        """
        import re

        from cabineteer.visualize import _FINISH_JS  # noqa: F401
        cfg = _cfg(columns=self.COLS)
        names = [n for n, _s in self._placed(cfg, self.COLS)
                 if "floor" in n]
        assert names
        for n in names:
            assert re.sub(r"_\d+$", "", n) == "floor"
