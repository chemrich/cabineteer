"""Viewer geometry must match cutlist dimensions (Charlie's audit ask,
2026-08-02: the viewer drew capped-back/dado-era geometry for butt-tenon
builds for years while the cutlist said otherwise — renders must show the
parts the paper produces, exposed edges included).

Butt construction (floating tenon / pocket screw / biscuit / dowel):
plain-slab sides, interior panels seated between them at cutlist dims,
top depth and back height following ``back_style``. Dado/rabbet keeps the
legacy housed geometry (locked here so it can't drift silently).
"""

import pytest

cq = pytest.importorskip("cadquery")

from cabineteer.cabinet import (
    CabinetConfig,
    build_cabinet_config,
    build_multi_bay_cabinet,
)
from cabineteer.joinery import CarcassJoinery
from cabineteer.server import _raw_panels_for_cabinet

TOL = 0.05


def _world_boxes(assy, loc=None):
    """{node name: (xmin,xmax,ymin,ymax,zmin,zmax) in world coords}."""
    out = {}
    for child in assy.children:
        child_loc = child.loc if child.loc is not None else cq.Location()
        world = (loc * child_loc) if loc is not None else child_loc
        if child.obj is not None:
            (dx, dy, dz) = world.toTuple()[0]
            bb = None
            for s in child.obj.solids().vals():
                b = s.BoundingBox()
                cur = [b.xmin, b.xmax, b.ymin, b.ymax, b.zmin, b.zmax]
                bb = cur if bb is None else [
                    min(bb[0], cur[0]), max(bb[1], cur[1]),
                    min(bb[2], cur[2]), max(bb[3], cur[3]),
                    min(bb[4], cur[4]), max(bb[5], cur[5])]
            out[child.name] = (bb[0] + dx, bb[1] + dx, bb[2] + dy,
                               bb[3] + dy, bb[4] + dz, bb[5] + dz)
        out.update(_world_boxes(child, loc=world))
    return out


def _dims(box):
    return tuple(sorted((box[1] - box[0], box[3] - box[2], box[5] - box[4])))


def _build(cfg, bays=None):
    assy, _ = build_multi_bay_cabinet(
        bays or [cfg], include_drawers=False, include_faces=False,
        include_feet=False)
    return _world_boxes(assy)


def _cut_dims(cfg, columns_raw=None):
    carcass, six_mm, _, _ = _raw_panels_for_cabinet(cfg, columns_raw)
    return {p.name: tuple(sorted((p.length, p.width, p.thickness)))
            for p in carcass + six_mm}


def _cfg(**kw):
    kw.setdefault("carcass_joinery", CarcassJoinery.FLOATING_TENON)
    return CabinetConfig(width=600, height=720, depth=550,
                         openings=[[300, "drawer"], [384, "drawer"]], **kw)


def assert_dims(box, expected):
    assert all(abs(a - b) < TOL for a, b in zip(_dims(box), expected)), (
        f"{_dims(box)} != {expected}")


class TestButtMatchesCutlist:
    @pytest.mark.parametrize("style", ["full_height", "under_top"])
    def test_panels_match_cutlist(self, style):
        cfg = _cfg(back_style=style)
        boxes = _build(cfg)
        cut = _cut_dims(cfg)
        assert_dims(boxes["left_side"], cut["side"])
        assert_dims(boxes["right_side"], cut["side"])
        assert_dims(boxes["bottom"], cut["bottom"])
        assert_dims(boxes["top"], cut["top"])
        assert_dims(boxes["back"], cut["back"])

    def test_full_height_back_reaches_top_plane(self):
        # The exposed edge must be VISIBLE in the render: back runs to the
        # top plane, top panel stops short of the rear.
        cfg = _cfg()
        boxes = _build(cfg)
        assert boxes["back"][5] == pytest.approx(720)          # zmax = height
        assert boxes["top"][3] == pytest.approx(550 - 6)       # ymax short
        assert boxes["back"][3] == pytest.approx(550)          # flush rear

    def test_under_top_top_caps_back(self):
        cfg = _cfg(back_style="under_top")
        boxes = _build(cfg)
        assert boxes["top"][3] == pytest.approx(550)           # full depth
        assert boxes["back"][5] == pytest.approx(720 - 18)     # under top
        assert boxes["back"][3] == pytest.approx(550)          # flush rear

    def test_panels_seat_between_sides(self):
        # Butt panels butt the sides' interior faces — no dado overlap.
        boxes = _build(_cfg())
        for name in ("bottom", "top", "back"):
            assert boxes[name][0] == pytest.approx(18), name   # xmin
            assert boxes[name][1] == pytest.approx(600 - 18), name

    def test_fixed_shelf_dims(self):
        cfg = CabinetConfig(width=600, height=720, depth=550,
                            carcass_joinery=CarcassJoinery.FLOATING_TENON,
                            openings=[[684, "door"]],
                            fixed_shelf_positions=[350])
        boxes = _build(cfg)
        cut = _cut_dims(cfg)
        assert_dims(boxes["shelf_0"], cut["shelf_1"])
        assert boxes["shelf_0"][0] == pytest.approx(18)


class TestMultiBayDivider:
    def _mc(self, style):
        cols = [{"width_mm": 300, "openings": [[300, "drawer"], [384, "drawer"]]},
                {"width_mm": 246, "openings": [[300, "drawer"], [384, "drawer"]]}]
        cfg = build_cabinet_config(
            {"width": 600, "height": 720, "depth": 550,
             "carcass_joinery": "floating_tenon", "back_style": style,
             "columns": cols})
        from dataclasses import replace
        bays = [replace(cfg, width=336, columns=[],
                        openings=[[300, "drawer"], [384, "drawer"]]),
                replace(cfg, width=282, columns=[],
                        openings=[[300, "drawer"], [384, "drawer"]])]
        return cfg, bays, cols

    @pytest.mark.parametrize("style", ["full_height", "under_top"])
    def test_divider_interior_height_between_panels(self, style):
        cfg, bays, cols = self._mc(style)
        boxes = _build(cfg, bays=bays)
        cut = _cut_dims(cfg, columns_raw=cols)
        assert_dims(boxes["divider_0"], cut["column_divider"])
        # Seated ON the bottom, stopping at the top's underside.
        assert boxes["divider_0"][4] == pytest.approx(18)
        assert boxes["divider_0"][5] == pytest.approx(720 - 18)


class TestDadoLegacyLocked:
    def test_dado_geometry_unchanged(self):
        # Dado/rabbet keeps the housed geometry (dados + rabbets). Its
        # cutlist emits butt-style dims — a separate, pre-existing gap
        # documented in CLAUDE.md; this test locks the RENDER convention.
        cfg = _cfg(carcass_joinery=CarcassJoinery.DADO_RABBET)
        boxes = _build(cfg)
        assert_dims(boxes["bottom"], (18, 541, 582))   # dado tabs + rabbet
        assert_dims(boxes["top"], (18, 550, 582))      # full depth
        assert_dims(boxes["back"], (6, 576, 702))      # rabbet width
