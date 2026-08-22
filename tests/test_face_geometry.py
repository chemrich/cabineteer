"""Show-face geometry: one source of truth, and everything agrees with it.

Charlie caught this at the bench (2026-08-22): the kids'-desk false fronts
were cut 365 mm wide from cutlist paper against 381 mm carcasses whose 3D
renders — the design he approved — drew the fronts flush. Two independent
sizing bases had coexisted since the initial commit: the 3D builder used
flush-outer / split-inner overlays with an anchored, gapped face stack,
while the cutlist used DrawerConfig's flat 10/3/3 mm overlays with no gap
at all (so no multi-drawer stack it printed could physically tile its
opening span). Doors carried a third basis. Nothing asserted a face
dimension anywhere, and the 2026-08 viewer audit ran with
``include_faces=False``, so all three drifted unchecked.

``cabinet.face_layout`` is now the single authority. These tests pin:
- the layout's numbers themselves (including the exact B/C bench values),
- cutlist rows == layout,
- 3D face bounding boxes == layout (CadQuery section),
- the furniture_top cap strip reaching paper,
- config plumbing for ``face_gap_mm`` / ``furniture_top``.
"""

import pytest

from cabineteer.cabinet import (
    CabinetConfig,
    INNER_FACE_OVERLAY_MM,
    build_cabinet_config,
    face_layout,
)
from cabineteer.server import _raw_panels_for_cabinet

TOL = 0.05


def _cfg(**kw):
    base = dict(width=381, height=389, depth=457,
                drawer_config=[[133, "drawer"], [110, "drawer"], [110, "drawer"]])
    base.update(kw)
    return build_cabinet_config(base)


def _drawer_faces(panels):
    return [p for p in panels if p.kind == "drawer_face"]


class TestFaceLayoutSingleColumn:
    """The kid2-desk pedestal (C) — the cabinet the bug was caught on."""

    def test_widths_flush_to_exterior(self):
        faces = _drawer_faces(face_layout([_cfg()]))
        assert [round(p.width, 1) for p in faces] == [381.0, 381.0, 381.0]
        # flush: left edge at 0, right edge at the carcass exterior
        for p in faces:
            assert abs(p.x) < TOL
            assert abs(p.x + p.width - 381) < TOL

    def test_heights_tile_the_opening_span(self):
        cfg = _cfg()
        faces = _drawer_faces(face_layout([cfg]))
        assert [round(p.height, 1) for p in faces] == [131.0, 106.0, 108.0]
        span = cfg.height - cfg.bottom_thickness - cfg.top_thickness
        total = sum(p.height for p in faces) + cfg.face_gap_mm * (len(faces) - 1)
        assert abs(total - span) < TOL
        # stack anchored: lowest face starts at the bottom panel top,
        # highest ends at the top panel underside
        assert abs(faces[0].z - cfg.bottom_thickness) < TOL
        top = max(faces, key=lambda p: p.z)
        assert abs(top.z + top.height - (cfg.height - cfg.top_thickness)) < TOL

    def test_face_gap_mm_config_drives_heights(self):
        # Charlie shims at 2.5 — the exact bench numbers from the C repair
        faces = _drawer_faces(face_layout([_cfg(face_gap_mm=2.5)]))
        assert [round(p.height, 2) for p in faces] == [131.75, 107.5, 108.75]

    def test_face_gap_param_overrides_config(self):
        faces = _drawer_faces(face_layout([_cfg(face_gap_mm=4.0)], face_gap=2.5))
        assert [round(p.height, 2) for p in faces] == [131.75, 107.5, 108.75]

    def test_kid1_tower_numbers(self):
        cfg = _cfg(height=1168, drawer_config=[
            [296, "drawer"], [296, "drawer"],
            [180, "drawer"], [180, "drawer"], [180, "drawer"]])
        faces = _drawer_faces(face_layout([cfg]))
        assert [round(p.height, 1) for p in faces] == [294.0, 292.0, 176.0, 176.0, 178.0]
        assert all(round(p.width, 1) == 381.0 for p in faces)


class TestFurnitureTop:
    def test_flag_from_config(self):
        cfg = _cfg(furniture_top=True)
        panels = face_layout([cfg])
        faces = _drawer_faces(panels)
        # bottom face drops to the carcass underside (+18), top face gives
        # one gap back under the cap
        assert [round(p.height, 1) for p in faces] == [149.0, 106.0, 104.0]
        assert abs(faces[0].z) < TOL  # z=0: carcass underside
        caps = [p for p in panels if p.kind == "top_cap"]
        assert len(caps) == 1
        cap = caps[0]
        assert (round(cap.width, 1), round(cap.height, 1),
                round(cap.thickness, 1)) == (381.0, 18.0, 18.0)
        assert abs(cap.z - (389 - 18)) < TOL

    def test_param_overrides_config_off(self):
        panels = face_layout([_cfg(furniture_top=True)], furniture_top=False)
        assert not [p for p in panels if p.kind == "top_cap"]

    def test_cap_reaches_the_cutlist(self):
        # THE gap that kept the strip off Charlie's paper: furniture_top was
        # render-only, so the cap existed in every approved render and on no
        # cutlist.
        cfg = _cfg(furniture_top=True)
        _, _, _, ff = _raw_panels_for_cabinet(cfg, None)
        caps = [p for p in ff if p.name == "top_front_cap"]
        assert len(caps) == 1
        assert (caps[0].length, caps[0].width, caps[0].thickness) == (381.0, 18.0, 18.0)


class TestMultiBay:
    def _bays(self):
        mk = lambda w, ops: build_cabinet_config(dict(
            width=w, height=389, depth=457, drawer_config=list(ops)))
        ops = [[133, "drawer"], [110, "drawer"], [110, "drawer"]]
        return [mk(645.6, ops), mk(290.0, ops), mk(319.6, ops)]

    def test_outer_flush_inner_split(self):
        bays = self._bays()
        faces = _drawer_faces(face_layout(bays))
        w = {p.bay: round(p.width, 1) for p in faces}
        # leftmost: 18 flush + interior + 8 divider share
        assert w[0] == round(18 + bays[0].interior_width + 8, 1)
        # middle: 8 + interior + 8
        assert w[1] == round(8 + bays[1].interior_width + 8, 1)
        assert w[2] == round(8 + bays[2].interior_width + 18, 1)

    def test_divider_reveal_between_bay_faces(self):
        faces = _drawer_faces(face_layout(self._bays()))
        by_bay = {}
        for p in faces:
            by_bay.setdefault(p.bay, p)
        # gap between adjacent bay faces = divider 18 − 2×8 = 2 mm
        for a, b in [(0, 1), (1, 2)]:
            gap = by_bay[b].x - (by_bay[a].x + by_bay[a].width)
            assert abs(gap - (18 - 2 * INNER_FACE_OVERLAY_MM)) < TOL


class TestDoors:
    def test_single_door_width_is_hinge_derived(self):
        cfg = build_cabinet_config(dict(width=609.6, height=720, depth=550,
                            drawer_config=[[648, "door"]]))
        doors = [p for p in face_layout([cfg]) if p.kind == "door"]
        assert len(doors) == 1
        from cabineteer.hardware import get_hinge
        ov = get_hinge(cfg.door_hinge).overlay
        assert abs(doors[0].width - (cfg.interior_width + 2 * ov)) < TOL

    def test_pair_leaves_and_centre_gap(self):
        cfg = build_cabinet_config(dict(width=609.6, height=720, depth=550,
                            drawer_config=[[648, "door_pair"]]))
        doors = sorted((p for p in face_layout([cfg]) if p.kind == "door"),
                       key=lambda p: p.leaf)
        assert len(doors) == 2
        centre_gap = doors[1].x - (doors[0].x + doors[0].width)
        from cabineteer.door import DoorConfig
        expected_gap = DoorConfig(opening_width=cfg.interior_width,
                                  opening_height=648, num_doors=2).gap_between
        assert abs(centre_gap - expected_gap) < 0.1

    def test_door_above_drawer_extends_to_exterior_top(self):
        cfg = build_cabinet_config(dict(width=609.6, height=720, depth=550,
                            drawer_config=[[110, "drawer"], [538, "door_pair"]]))
        panels = face_layout([cfg])
        door = next(p for p in panels if p.kind == "door")
        # transition rule: the door runs to the carcass exterior top
        assert abs(door.z + door.height - cfg.height) < TOL

    def test_cutlist_door_rows_match_layout(self):
        cfg = build_cabinet_config(dict(width=609.6, height=720, depth=550,
                            drawer_config=[[110, "drawer"], [538, "door_pair"]]))
        door = next(p for p in face_layout([cfg]) if p.kind == "door")
        _, _, _, ff = _raw_panels_for_cabinet(cfg, None)
        row = next(p for p in ff if p.name == "door")
        assert row.quantity == 2
        assert abs(row.length - round(door.height, 1)) < TOL
        assert abs(row.width - round(door.width, 1)) < TOL


class TestCutlistMatchesLayout:
    """The core regression: paper == render, for every face, always."""

    @pytest.mark.parametrize("kwargs", [
        dict(),                          # C pedestal
        dict(height=1168, drawer_config=[
            [296, "drawer"], [296, "drawer"],
            [180, "drawer"], [180, "drawer"], [180, "drawer"]]),  # B tower
        dict(furniture_top=True),
        dict(face_gap_mm=2.5),
    ])
    def test_false_front_rows_equal_layout(self, kwargs):
        cfg = _cfg(**kwargs)
        expected = sorted(
            (round(p.width, 1), round(p.height, 1))
            for p in _drawer_faces(face_layout([cfg])))
        _, _, _, ff = _raw_panels_for_cabinet(cfg, None)
        got = sorted((p.length, p.width) for p in ff if p.name == "false_front")
        assert got == expected

    def test_multi_column_rows_equal_layout(self):
        import dataclasses
        cfg = build_cabinet_config(dict(
            width=1219.2, height=389, depth=457,
            columns=[
                {"width_mm": 609.6, "openings": [[133, "drawer"], [110, "drawer"], [110, "drawer"]]},
                {"width_mm": 254.0, "openings": [[133, "drawer"], [110, "drawer"], [110, "drawer"]]},
                {"width_mm": 283.6, "openings": [[133, "drawer"], [110, "drawer"], [110, "drawer"]]},
            ]))
        bays = [dataclasses.replace(
            cfg, width=c.width_mm + 2 * cfg.side_thickness, columns=[],
            openings=list(c.openings), fixed_shelf_positions=[])
            for c in cfg.columns]
        expected = sorted(
            (round(p.width, 1), round(p.height, 1))
            for p in _drawer_faces(face_layout(bays)))
        cols_raw = [{"width_mm": c.width_mm,
                     "openings": [[o.height_mm, o.opening_type] for o in c.openings]}
                    for c in cfg.columns]
        _, _, _, ff = _raw_panels_for_cabinet(cfg, cols_raw)
        got = sorted((p.length, p.width) for p in ff if p.name == "false_front")
        assert got == expected

    def test_no_face_wider_than_its_claim_no_overlap(self):
        # multi-bay: adjacent claims may not overlap (the old cutlist put
        # 10 mm from each neighbour on an 18 mm divider — 2 mm of physical
        # overlap at every joint)
        cfg = build_cabinet_config(dict(
            width=1219.2, height=389, depth=457,
            columns=[
                {"width_mm": 609.6, "openings": [[353, "drawer"]]},
                {"width_mm": 254.0, "openings": [[353, "drawer"]]},
                {"width_mm": 283.6, "openings": [[353, "drawer"]]},
            ]))
        import dataclasses
        bays = [dataclasses.replace(
            cfg, width=c.width_mm + 2 * cfg.side_thickness, columns=[],
            openings=list(c.openings), fixed_shelf_positions=[])
            for c in cfg.columns]
        faces = sorted(_drawer_faces(face_layout(bays)), key=lambda p: p.x)
        for a, b in zip(faces, faces[1:]):
            assert b.x - (a.x + a.width) > 0


class TestConfigPlumbing:
    def test_shared_design_tokens_round_trip(self):
        from cabineteer.project import shared_from_dict, _shared_to_dict
        sd = shared_from_dict({"face_gap_mm": 2.5, "furniture_top": True})
        assert sd.face_gap_mm == 2.5 and sd.furniture_top is True
        assert _shared_to_dict(sd) == {"face_gap_mm": 2.5, "furniture_top": True}

    def test_config_dict_round_trip(self):
        from cabineteer.project import _config_to_dict, config_from_dict
        cfg = _cfg(face_gap_mm=2.5, furniture_top=True)
        d = _config_to_dict(cfg)
        assert d["face_gap_mm"] == 2.5 and d["furniture_top"] is True
        back = config_from_dict(d)
        assert back.face_gap_mm == 2.5 and back.furniture_top is True

    def test_build_cabinet_config_keeps_furniture_top(self):
        from cabineteer.cabinet import build_cabinet_config
        cfg = build_cabinet_config({"width": 381, "height": 389, "depth": 457,
                                    "furniture_top": True})
        assert cfg.furniture_top is True


class TestFaceClearanceCheckWired:
    def test_negative_height_face_flags_error(self):
        from cabineteer.evaluation import evaluate_cabinet, Severity
        # A 2 mm middle opening cannot survive ±2 mm of gap trim
        cfg = build_cabinet_config(dict(width=381, height=94, depth=457,
                            drawer_config=[[28, "drawer"], [2, "drawer"],
                                           [28, "drawer"]]))
        issues = evaluate_cabinet(cfg)
        assert any(i.check == "face_clearance" and i.severity == Severity.ERROR
                   for i in issues)


# ── 3D parity (CadQuery) ─────────────────────────────────────────────────────
# Guarded import, NOT importorskip: importorskip at module level would skip
# the pure-Python classes above in the lite environment too.

try:
    import cadquery as cq
except ImportError:  # pragma: no cover - lite env
    cq = None

if cq is not None:
    from cabineteer.cabinet import build_multi_bay_cabinet

_needs_cq = pytest.mark.skipif(cq is None, reason="CadQuery not installed")


def _face_bboxes(assy):
    """{name: (xlen, zlen, xmin, zmin)} for face/door/cap nodes."""
    out = {}
    def walk(node, loc):
        child_loc = node.loc if node.loc is not None else cq.Location()
        world = (loc * child_loc) if loc is not None else child_loc
        if node.obj is not None and (
                "face" in node.name or "door" in node.name
                or node.name == "top_front_cap"):
            if "pull" not in node.name:
                (dx, _dy, dz) = world.toTuple()[0]
                b = node.obj.solids().vals()[0].BoundingBox()
                out[node.name] = (b.xlen, b.zlen, b.xmin + dx, b.zmin + dz)
        for c in node.children:
            walk(c, world)
    for c in assy.children:
        walk(c, None)
    return out


@_needs_cq
class TestRenderMatchesLayout:
    """The audit #69 should have run with include_faces=True. Now it does."""

    @pytest.mark.parametrize("kwargs,ft", [
        (dict(), None),
        (dict(furniture_top=True), None),
        (dict(), True),                       # param override
        (dict(face_gap_mm=2.5), None),
        (dict(height=1168, drawer_config=[
            [296, "drawer"], [296, "drawer"],
            [180, "drawer"], [180, "drawer"], [180, "drawer"]]), None),
    ])
    def test_single_bay_faces(self, kwargs, ft):
        cfg = _cfg(**kwargs)
        panels = face_layout([cfg], furniture_top=ft)
        assy, _ = build_multi_bay_cabinet([cfg], furniture_top=ft,
                                          include_drawers=False,
                                          include_feet=False)
        boxes = _face_bboxes(assy)
        for p in panels:
            name = ("top_front_cap" if p.kind == "top_cap"
                    else f"bay{p.bay}_face{p.slot}")
            assert name in boxes, name
            xlen, zlen, xmin, zmin = boxes[name]
            assert abs(xlen - p.width) < TOL, name
            assert abs(zlen - p.height) < TOL, name
            assert abs(xmin - p.x) < TOL, name
            assert abs(zmin - p.z) < TOL, name

    def test_door_leaves(self):
        cfg = build_cabinet_config(dict(width=609.6, height=720, depth=550,
                            drawer_config=[[110, "drawer"], [538, "door_pair"]]))
        panels = [p for p in face_layout([cfg]) if p.kind == "door"]
        assy, _ = build_multi_bay_cabinet([cfg], include_drawers=False,
                                          include_feet=False)
        boxes = _face_bboxes(assy)
        for p in panels:
            name = f"bay0_door{p.slot}_{p.leaf}"
            assert name in boxes, sorted(boxes)
            xlen, zlen, xmin, zmin = boxes[name]
            assert abs(xlen - p.width) < TOL
            assert abs(zlen - p.height) < TOL
            assert abs(xmin - p.x) < TOL
            assert abs(zmin - p.z) < TOL

    def test_multi_bay_faces(self):
        ops = [[133, "drawer"], [110, "drawer"], [110, "drawer"]]
        bays = [build_cabinet_config(dict(width=w, height=389, depth=457,
                                          drawer_config=list(ops)))
                for w in (645.6, 290.0, 319.6)]
        panels = _drawer_faces(face_layout(bays))
        assy, _ = build_multi_bay_cabinet(bays, include_drawers=False,
                                          include_feet=False)
        boxes = _face_bboxes(assy)
        for p in panels:
            name = f"bay{p.bay}_face{p.slot}"
            xlen, zlen, xmin, zmin = boxes[name]
            assert abs(xlen - p.width) < TOL, name
            assert abs(xmin - p.x) < TOL, name
