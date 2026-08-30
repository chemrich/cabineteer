"""Carcass panel geometry: one source of truth, and everything agrees with it.

``cabinet.face_layout`` ended the show-face drift (#88). The case had the
same shape and no such source: the cutlist, both design payloads, the 3D
makers and the assembly-doc mortise maps each derived ``depth − something``
for themselves, and three of them got a different answer.

  D2  the mortise map drew the bottom 6 mm short — it hardcoded
      ``depth − back_thickness`` under a comment calling that "the cutlist
      convention", which it stopped being when ``back_capture`` landed and
      rabbet and dado began running the top AND the bottom full depth. Live
      on 6 of the 12 cabinets in the saved projects.
  D3  ``design_multi_column_cabinet`` reported the divider at ``cfg.height``
      while every other document cut it to the interior — a 720 mm divider
      standing in its own 684 mm interior, in the first document anyone
      reads, at approval time.
  D12 the render does not model ``carcass_corner_style`` at all.

``cabinet.carcass_panel_dims`` is now the single authority. These tests
mirror ``test_face_geometry.py``'s three styles:

- the dims themselves (including Charlie's real project numbers),
- cutlist rows == dims,
- 3D bounding boxes == dims (CadQuery section),

plus the FINISHED/CORE conversion, which is the one place a consumer can
still take the wrong view of a correct number.
"""

import dataclasses

import pytest

from cabineteer.cabinet import (
    CarcassPanel,
    back_capture_geometry,
    build_cabinet_config,
    carcass_panel_dims,
    divider_cut_length,
)
from cabineteer.joinery import CarcassJoinery
from cabineteer.server import _raw_panels_for_cabinet

TOL = 0.05


def _cfg(**kw):
    base = dict(width=800, height=800, depth=457,
                side_thickness=18, bottom_thickness=18, top_thickness=18,
                shelf_thickness=18, back_thickness=6,
                back_rabbet_depth=6, back_groove_setback=12,
                carcass_joinery="floating_tenon",
                drawer_config=[[764, "open"]])
    base.update(kw)
    return build_cabinet_config(base)


def _by_kind(cfg, columns=None) -> dict:
    out: dict = {}
    for p in carcass_panel_dims(cfg, columns):
        out.setdefault(p.kind, []).append(p)
    return out


# ─── Style 1 — the dims themselves ────────────────────────────────────────


class TestTheNumbers:
    """A butt carcass, 800 × 800 × 457 on 18 mm stock, 6 mm back."""

    def test_side_runs_the_full_exterior(self):
        side = _by_kind(_cfg())["side"][0]
        assert side.quantity == 2
        assert (side.length, side.width) == (800.0, 457.0)

    def test_top_and_bottom_seat_between_the_sides(self):
        p = _by_kind(_cfg())
        for kind in ("bottom", "top"):
            assert p[kind][0].length == pytest.approx(764.0)

    @pytest.mark.parametrize("capture,bottom,top", [
        ("pocket", 451.0, 451.0),      # back laps their rear edges
        ("rabbet", 457.0, 457.0),      # back seats INSIDE the perimeter
        ("half_lap", 457.0, 457.0),
        ("dado", 457.0, 457.0),
    ])
    def test_perimeter_depth_follows_the_capture(self, capture, bottom, top):
        p = _by_kind(_cfg(back_capture=capture))
        assert p["bottom"][0].width == pytest.approx(bottom)
        assert p["top"][0].width == pytest.approx(top)

    def test_under_top_caps_the_back_only_on_a_pocket(self):
        """The one capture ``back_style`` can change anything about."""
        p = _by_kind(_cfg(back_capture="pocket", back_style="under_top"))
        assert p["top"][0].width == pytest.approx(457.0)     # full depth, caps
        assert p["bottom"][0].width == pytest.approx(451.0)
        # A machined capture already runs both full depth; there is nothing
        # left for back_style to do.
        q = _by_kind(_cfg(back_capture="rabbet", back_style="under_top"))
        r = _by_kind(_cfg(back_capture="rabbet", back_style="full_height"))
        assert q["top"][0].width == r["top"][0].width == pytest.approx(457.0)

    @pytest.mark.parametrize("capture,depth", [
        ("pocket", 451.0), ("rabbet", 451.0),
        ("half_lap", 451.0), ("dado", 439.0),     # 12 mm setback + 6 back
    ])
    def test_interior_panels_stop_at_the_backs_front_face(self, capture, depth):
        cfg = _cfg(back_capture=capture, fixed_shelf_positions=[400])
        p = _by_kind(cfg)
        assert p["shelf"][0].width == pytest.approx(depth)
        assert depth == pytest.approx(cfg.interior_depth), (
            "an interior panel's depth IS interior_depth — if these ever "
            "differ, the datum has grown a second spelling again")

    def test_shelf_runs_the_interior_width_and_carries_its_height(self):
        shelf = _by_kind(_cfg(fixed_shelf_positions=[350.0]))["shelf"][0]
        assert shelf.length == pytest.approx(764.0)
        assert shelf.z == pytest.approx(350.0)
        assert shelf.column is None      # cabinet-wide, spans every column


class TestTheDividerHeight:
    """D3, in its own class: the number two documents disagreed about."""

    COLS = [{"width_mm": 373, "openings": [[764, "open"]]},
            {"width_mm": 373, "openings": [[764, "open"]]}]

    def test_a_butt_divider_seats_between_bottom_and_top(self):
        cfg = _cfg()
        assert divider_cut_length(cfg) == pytest.approx(764.0)
        assert divider_cut_length(cfg) == pytest.approx(cfg.interior_height)

    def test_a_dado_rabbet_divider_is_housed_full_height(self):
        """The legacy branch, kept deliberately — do not fold these two."""
        cfg = _cfg(carcass_joinery="dado_rabbet")
        assert divider_cut_length(cfg) == pytest.approx(800.0)

    @pytest.mark.parametrize("joinery", ["floating_tenon", "pocket_screw",
                                         "biscuit", "dowel"])
    def test_every_butt_joinery_agrees(self, joinery):
        cfg = _cfg(carcass_joinery=joinery)
        div = _by_kind(cfg, self.COLS)["divider"][0]
        assert div.length == pytest.approx(764.0)
        assert div.quantity == 1

    def test_the_divider_fits_the_interior_it_stands_in(self):
        """Closure: it has to fit, referencing no document.

        The payload reported an 800 mm divider standing in a 764 mm
        interior — a statement that is impossible before it is inconsistent.
        """
        cfg = _cfg()
        assert divider_cut_length(cfg) <= cfg.interior_height + TOL


class TestColumns:
    COLS = [{"width_mm": 300, "openings": [[764, "open"]],
             "fixed_shelf_positions": [300.0]},
            {"width_mm": 446, "openings": [[764, "open"]]}]

    def test_one_divider_per_gap_at_the_shared_thickness(self):
        div = _by_kind(_cfg(), self.COLS)["divider"][0]
        assert div.quantity == 1
        assert div.thickness == pytest.approx(18.0)

    def test_a_column_shelf_runs_its_own_column_width(self):
        shelf = _by_kind(_cfg(), self.COLS)["shelf"][0]
        assert shelf.length == pytest.approx(300.0)
        assert shelf.column == 0

    def test_columns_and_dividers_close_into_the_interior(self):
        """Closure: the widths plus the dividers must be the interior."""
        cfg = _cfg()
        widths = sum(c["width_mm"] for c in self.COLS)
        div = _by_kind(cfg, self.COLS)["divider"][0]
        assert widths + div.quantity * div.thickness == pytest.approx(
            cfg.interior_width, abs=TOL)

    def test_columns_come_from_cfg_when_not_passed(self):
        """``None`` means "use cfg.columns" — bays_from_config's contract.

        The cutlist used to emit a divider only when handed raw column
        dicts, so a config that *had* columns but was called without them
        produced a cabinet with no divider row at all. Same contract in both
        places now.
        """
        cfg = build_cabinet_config(dict(
            width=800, height=800, depth=457, columns=self.COLS))
        assert _by_kind(cfg)["divider"][0].quantity == 1


class TestMiter:
    def test_top_and_bottom_run_the_full_exterior_long_point(self):
        p = _by_kind(_cfg(carcass_corner_style="miter"))
        assert p["bottom"][0].length == pytest.approx(800.0)
        assert p["top"][0].length == pytest.approx(800.0)

    def test_the_four_exterior_panels_are_beveled_and_nothing_else(self):
        cfg = _cfg(carcass_corner_style="miter", fixed_shelf_positions=[400])
        beveled = {p.kind for p in carcass_panel_dims(cfg) if p.bevel_ends}
        assert beveled == {"side", "bottom", "top"}

    def test_butt_bevels_nothing(self):
        assert not any(p.bevel_ends for p in carcass_panel_dims(_cfg()))


class TestFinishedVersusCore:
    """The one place a consumer can take the wrong view of a right number."""

    def test_dims_are_finished(self):
        side = _by_kind(_cfg(edge_band_mode="hardwood",
                             edge_band_thickness_mm=3.2))["side"][0]
        assert side.width == pytest.approx(457.0)

    def test_core_shrinks_only_the_banded_axis(self):
        side = _by_kind(_cfg())["side"][0]
        assert side.core(3.2) == (800.0, pytest.approx(453.8))

    def test_hot_melt_and_no_banding_shrink_nothing(self):
        """Hot-melt is ironed on after cutting; it grows, it does not replace."""
        side = _by_kind(_cfg())["side"][0]
        assert side.core(0.0) == (side.length, side.width)

    def test_a_furniture_top_is_never_banded_on_its_capped_edge(self):
        """The cap strip covers that edge, so the core must not shrink for it.

        The row once demanded banding on the same edge the cap covers — two
        contradictory instructions on one line of shop paper.
        """
        top = _by_kind(_cfg(furniture_top=True))["top"][0]
        assert top.banded_edges == ()
        assert top.core(3.2) == (top.length, top.width)

    def test_the_back_is_never_banded(self):
        back = _by_kind(_cfg())["back"][0]
        assert back.banded_edges == ()

    def test_every_carcass_band_is_the_front_edge(self):
        """The ``core`` arithmetic assumes it. Assert it rather than trust it.

        Both halves matter: nothing may band an edge other than the front
        (the shrink would go on the wrong axis), and the panels that DO band
        must still be banding — a subset check alone is satisfied by a panel
        that has quietly lost its banding altogether.
        """
        panels = carcass_panel_dims(_cfg(fixed_shelf_positions=[400]),
                                    TestColumns.COLS)
        for p in panels:
            assert set(p.banded_edges) <= {"front"}
        banded = {p.kind for p in panels if p.banded_edges}
        assert banded == {"side", "bottom", "top", "divider", "shelf"}, (
            "the back is unbanded and everything else bands its front edge")


# ─── Style 2 — the cutlist cuts exactly these dims ────────────────────────


CAPTURES = ["pocket", "rabbet", "half_lap", "dado"]
BANDS = [("none", 0.0), ("hot_melt", 0.6), ("hardwood", 3.2)]


class TestCutlistMatchesDims:
    """WIRING: the cutlist emits carcass_panel_dims and does not re-derive.

    Deliberately not a dimension check — the cutlist computes its rows by
    calling ``p.core(band_t)``, so recomputing that here cannot discriminate
    a wrong dimension, and a reader should not mistake it for a test that
    can. Its job is the one thing it does discriminate: the day someone
    grows a private ``depth - something`` back into ``_raw_panels_for_cabinet``,
    this fails. The numbers themselves are pinned by ``TestTheNumbers`` and
    by the closure module's physical predicates.
    """

    COLS = [{"width_mm": 300, "openings": [[764, "open"]],
             "fixed_shelf_positions": [300.0]},
            {"width_mm": 446, "openings": [[764, "open"]]}]

    @pytest.mark.parametrize("capture", CAPTURES)
    @pytest.mark.parametrize("band,band_t", BANDS)
    @pytest.mark.parametrize("style", ["full_height", "under_top"])
    @pytest.mark.parametrize("cols", [None, "cols"])
    def test_every_row_is_a_panel_in_core_view(self, capture, band, band_t,
                                               style, cols):
        columns = self.COLS if cols else None
        cfg = _cfg(back_capture=capture, back_style=style,
                   edge_band_mode=band, edge_band_thickness_mm=band_t,
                   fixed_shelf_positions=[400.0],
                   **({"columns": columns} if columns else {}))
        carcass, six_mm, _, _ = _raw_panels_for_cabinet(cfg, columns)
        dims = carcass_panel_dims(cfg, columns)

        rows = carcass + six_mm
        # The back sits in its own thin-stock list; everything else is one
        # ordered list, and the order has to match too — the assembly doc
        # and the part IDs are built from it.
        assert [p.name for p in dims if p.kind != "back"] == \
            [r.name for r in carcass]

        shrink = band_t if band == "hardwood" else 0.0
        by_name: dict = {}
        for r in rows:
            by_name.setdefault(r.name, []).append(r)
        for p in dims:
            cut_l, cut_w = p.core(shrink)
            match = [r for r in by_name[p.name]
                     if (round(r.length, 2), round(r.width, 2))
                     == (round(cut_l, 2), round(cut_w, 2))]
            assert match, (
                f"{capture}/{band}/{style}: no {p.name} row at "
                f"{cut_l:g} × {cut_w:g}; rows are "
                f"{[(r.length, r.width) for r in by_name[p.name]]}")
            row = match[0]
            assert row.thickness == pytest.approx(p.thickness)
            assert row.quantity == p.quantity
            if band != "none":
                assert list(p.banded_edges) == row.edge_band

    def test_banding_markers_are_stripped_when_the_cabinet_has_none(self):
        """``edge_band=[]`` on an unbanded build, whatever the panel says."""
        carcass, _, _, _ = _raw_panels_for_cabinet(_cfg(), None)
        assert all(r.edge_band == [] for r in carcass)


class TestAssemblyMapMatchesDims:
    def test_every_map_draws_a_panel_that_exists(self):
        cfg = _cfg(back_capture="rabbet", fixed_shelf_positions=[400.0])
        from cabineteer.assembly import build_assembly_plan
        names = {p.name for p in carcass_panel_dims(cfg)} | {"shelf"}
        for pm in build_assembly_plan(cfg).panels:
            assert pm.canonical in names or pm.canonical == "shelf", (
                f"map {pm.panel!r} joins on {pm.canonical!r}, which is not a "
                "cutlist row name")

    @pytest.mark.parametrize("capture", CAPTURES)
    def test_the_map_draws_the_finished_perimeter(self, capture):
        """D2: the bottom is full depth under every machined capture."""
        from cabineteer.assembly import build_assembly_plan
        cfg = _cfg(back_capture=capture)
        geo = back_capture_geometry(cfg)
        drawn = {pm.canonical: pm.draw_height
                 for pm in build_assembly_plan(cfg).panels
                 if pm.canonical in ("bottom", "top")}
        assert drawn["bottom"] == pytest.approx(geo.bottom_depth)
        assert drawn["top"] == pytest.approx(geo.top_depth)

    def test_a_machined_capture_is_never_said_to_be_capped(self):
        """Nothing caps the back under a rabbet — it seats inside the case."""
        from cabineteer.assembly import build_assembly_plan
        cfg = _cfg(back_capture="rabbet", back_style="under_top")
        for pm in build_assembly_plan(cfg).panels:
            assert "caps the back" not in pm.note


# ─── Style 3 — the render draws exactly these dims ────────────────────────

try:
    import cadquery as cq                                    # noqa: F401
except ImportError:                                          # pragma: no cover
    cq = None

requires_cq = pytest.mark.skipif(cq is None, reason="cadquery not installed")


@requires_cq
class TestRenderMatchesDims:
    """Bounding boxes of the solids the picture is actually made of.

    Note which function draws what, because it is not obvious and it hid a
    gap: ``make_top_panel`` / ``make_bottom_panel`` run only on STACKED
    layouts. Every ordinary cabinet's top and bottom are the "continuous"
    pair built inside ``build_multi_bay_cabinet``, which carried its own
    ``back_capture_geometry`` derivation until P3 — a private copy of the
    two numbers D2 was about. So the makers are tested here AND the assembled
    solids are tested through ``_cabinet_assembly`` below; testing only the
    makers would have measured a code path the render does not take.
    """

    @staticmethod
    def _bbox(shape):
        bb = shape.val().BoundingBox()
        return tuple(sorted((round(bb.xlen, 2), round(bb.ylen, 2),
                             round(bb.zlen, 2))))

    @pytest.mark.parametrize("capture", CAPTURES)
    @pytest.mark.parametrize("style", ["full_height", "under_top"])
    def test_perimeter_solids_match(self, capture, style):
        from cabineteer.cabinet import (make_bottom_panel, make_side_panel,
                                        make_top_panel)
        cfg = _cfg(back_capture=capture, back_style=style)
        dims = {p.kind: p for p in carcass_panel_dims(cfg)}
        for kind, maker in (("side", make_side_panel),
                            ("bottom", make_bottom_panel),
                            ("top", make_top_panel)):
            p = dims[kind]
            assert self._bbox(maker(cfg)) == tuple(sorted(
                (round(p.length, 2), round(p.width, 2),
                 round(p.thickness, 2)))), f"{kind} under {capture}/{style}"

    @pytest.mark.parametrize("capture", CAPTURES)
    def test_shelf_and_divider_stop_where_the_paper_says(self, capture):
        from cabineteer.cabinet import make_interior_divider, make_shelf
        cfg = _cfg(back_capture=capture, fixed_shelf_positions=[400.0])
        shelf = {p.kind: p for p in carcass_panel_dims(cfg)}["shelf"]
        assert self._bbox(make_shelf(cfg)) == tuple(sorted(
            (round(shelf.length, 2), round(shelf.width, 2),
             round(shelf.thickness, 2))))
        # The divider's depth is the same interior datum; its height is set
        # by where the caller clips it, so only the depth is pinned here.
        bb = make_interior_divider(cfg).val().BoundingBox()
        assert round(bb.ylen, 2) == pytest.approx(
            round(cfg.interior_depth, 2))

    def test_the_render_does_not_model_miters_and_says_so(self):
        """D12: the picture is butt-cornered whatever the config says.

        Fixing the render is a separate job. What is NOT acceptable is a
        picture that silently disagrees with the paper, so the tool result
        carries the caveat — that is what this pins.
        """
        from cabineteer.cabinet import make_top_panel
        from cabineteer.server import _render_caveats
        cfg = _cfg(carcass_corner_style="miter")
        paper = {p.kind: p for p in carcass_panel_dims(cfg)}["top"]
        assert paper.length == pytest.approx(800.0)          # long point
        drawn = max(make_top_panel(cfg).val().BoundingBox().xlen,
                    make_top_panel(cfg).val().BoundingBox().ylen)
        assert drawn == pytest.approx(764.0)                 # butt, 36 short
        caveats = _render_caveats(cfg)["render_caveats"]
        assert any("iter" in c and "not" in c.lower() for c in caveats)

    def test_a_butt_render_needs_no_caveat(self):
        from cabineteer.server import _render_caveats
        assert _render_caveats(_cfg()) == {}


# ─── Charlie's real cabinets ──────────────────────────────────────────────


class TestHisProjects:
    """Numbers off the builds on the bench, so a regression is legible.

    ``dining-sideboards-v2-hardwood`` and ``kapex_miter_station`` both carry
    ``back_capture: rabbet``, which is exactly why D2 was live: on a rabbet
    the bottom runs the FULL depth and the mortise map drew it 6 mm short.
    """

    def test_a_sideboard_carcass(self):
        cfg = _cfg(width=1219, height=663.6, depth=457,
                   back_capture="rabbet", back_style="under_top",
                   edge_band_mode="hardwood", edge_band_thickness_mm=3.2,
                   carcass_material="rift_white_oak_ply",
                   drawer_config=[[627.6, "open"]])
        p = {x.kind: x for x in carcass_panel_dims(cfg)}
        assert (p["side"].length, p["side"].width) == (663.6, 457.0)
        assert p["side"].core(3.2)[1] == pytest.approx(453.8)
        assert p["bottom"].length == pytest.approx(1183.0)
        assert p["bottom"].width == pytest.approx(457.0)      # was drawn 451
        assert p["bottom"].core(3.2)[1] == pytest.approx(453.8)

    def test_a_kid_tower_carcass(self):
        """kid1-desk, a pocket back — untouched by D2, and it must stay so."""
        cfg = _cfg(width=381, height=1168, depth=457, back_style="full_height")
        p = {x.kind: x for x in carcass_panel_dims(cfg)}
        assert (p["side"].length, p["side"].width) == (1168.0, 457.0)
        assert p["bottom"].width == pytest.approx(451.0)
        assert p["top"].width == pytest.approx(451.0)


def test_the_dataclass_stays_a_value_object():
    """Frozen, like FacePanel — a consumer must not mutate the source."""
    p = carcass_panel_dims(_cfg())[0]
    assert isinstance(p, CarcassPanel)
    # FrozenInstanceError specifically — a bare `Exception` here also passes
    # on the AttributeError you get from a typo'd field name, so it would
    # keep passing if the dataclass stopped being frozen and the test's own
    # attribute name went stale at the same time.
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.length = 1.0          # type: ignore[misc]


def test_dado_rabbet_is_not_this_functions_business():
    """The housed construction has its own geometry and a documented gap.

    ``carcass_panel_dims`` returns the cutlist convention, which for
    dado/rabbet has never carried the dado allowances. Pinning that here
    means a future reader finds the limitation stated rather than inferring
    the numbers are wrong.
    """
    cfg = _cfg(carcass_joinery="dado_rabbet")
    p = {x.kind: x for x in carcass_panel_dims(cfg)}
    assert p["bottom"].length == pytest.approx(cfg.interior_width)
    assert cfg.carcass_joinery is CarcassJoinery.DADO_RABBET


# ─── The disclosures, tested where they are DELIVERED ─────────────────────
#
# Every one of these was found by an adversarial review of this change:
# each mitigation below could be deleted outright and the whole suite stayed
# green. A disclosure is the entire justification for leaving D12 open, so
# an untested disclosure is a review item closed on a promise.


class TestTheCaveatReachesTheToolResult:
    """`_render_caveats` is load-bearing: it is why D12 may stay unfixed.

    Testing the helper directly is not enough — deleting the splat from
    `_tool_visualize_cabinet` left the suite byte-identical to baseline. So
    these go through the tool. The heavy `_visualize_assembly` (which writes
    files) is stubbed: nothing in this suite may write to ~/.cabineteer.
    """

    @staticmethod
    def _call(tool, args, monkeypatch):
        import asyncio
        import json

        from cabineteer import server

        def _stub(*_a, **_kw):
            return {"html": "x.html", "glb": "x.glb", "parts": [],
                    "glb_size_kb": 1.0}

        monkeypatch.setattr(server, "_visualize_assembly", _stub)
        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(server.TOOL_DISPATCH[tool](args))
        finally:
            loop.close()
        return json.loads(out[0].text)

    def _base_args(self, **kw):
        args = dict(width=800, height=800, depth=457, side_thickness=18,
                    bottom_thickness=18, top_thickness=18, back_thickness=6,
                    drawer_config=[[764, "open"]], open_browser=False)
        args.update(kw)
        return args

    def test_a_mitered_render_says_it_is_not_mitered(self, monkeypatch):
        res = self._call("visualize_cabinet",
                         self._base_args(carcass_corner_style="miter"),
                         monkeypatch)
        caveats = res.get("render_caveats") or []
        assert any("miter" in c.lower() and "not modelled" in c.lower()
                   for c in caveats), caveats
        # It must also say which document to believe.
        assert any("cutlist" in c for c in caveats), caveats

    def test_a_hardwood_render_says_it_draws_finished(self, monkeypatch):
        res = self._call("visualize_cabinet",
                         self._base_args(edge_band_mode="hardwood",
                                         edge_band_thickness_mm=3.2),
                         monkeypatch)
        assert any("banding" in c for c in res.get("render_caveats") or [])

    def test_a_plain_butt_render_carries_no_caveat(self, monkeypatch):
        res = self._call("visualize_cabinet", self._base_args(), monkeypatch)
        assert "render_caveats" not in res

    def test_the_number_in_the_caveat_is_the_real_one(self):
        """"the full 800 mm long-point" has to be what the paper cuts."""
        from cabineteer.server import _render_caveats
        cfg = _cfg(carcass_corner_style="miter")
        paper = {p.kind: p for p in carcass_panel_dims(cfg)}["top"]
        text = " ".join(_render_caveats(cfg)["render_caveats"])
        assert f"{paper.length:g} mm long-point" in text


class TestThePayloadAxisNames:
    """`_PANEL_AXES` exists only to name axes, and nothing looked at names.

    The closure module compares the payload's numbers as an unordered SET
    ("so a transposed panel still matches"), which is deliberate there — but
    it means the one thing this table decides was unchecked, in the very
    change whose docstring says the two tools used to disagree about it.
    """

    @staticmethod
    def _payload(tool, args):
        import asyncio
        import json

        from cabineteer.server import TOOL_DISPATCH
        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(TOOL_DISPATCH[tool](args))
        finally:
            loop.close()
        return json.loads(out[0].text)["panels"]

    ARGS = dict(width=900, height=720, depth=550, side_thickness=18,
                bottom_thickness=18, top_thickness=18, back_thickness=6)

    def test_both_tools_name_a_side_panels_axes_identically(self):
        single = self._payload("design_cabinet",
                               dict(self.ARGS, drawer_config=[[684, "open"]]))
        multi = self._payload("design_multi_column_cabinet", dict(
            self.ARGS,
            columns=[{"width_mm": 423, "openings": [[684, "open"]]},
                     {"width_mm": 423, "openings": [[684, "open"]]}]))
        assert set(single["side_panel"]) == set(multi["side_panel"])

    def test_a_side_panels_front_to_back_dimension_is_called_depth(self):
        p = self._payload("design_cabinet",
                          dict(self.ARGS, drawer_config=[[684, "open"]]))
        assert p["side_panel"]["depth_mm"] == pytest.approx(550.0)
        assert p["side_panel"]["height_mm"] == pytest.approx(720.0)
        assert "width_mm" not in p["side_panel"]

    def test_a_panel_that_lies_down_reports_a_width_not_a_height(self):
        p = self._payload("design_cabinet",
                          dict(self.ARGS, drawer_config=[[684, "open"]]))
        for key in ("bottom_panel", "top_panel"):
            assert p[key]["width_mm"] == pytest.approx(864.0)
            assert p[key]["depth_mm"] == pytest.approx(544.0)
            assert "height_mm" not in p[key]

    def test_a_divider_stands_up_so_its_long_axis_is_a_height(self):
        p = self._payload("design_multi_column_cabinet", dict(
            self.ARGS,
            columns=[{"width_mm": 423, "openings": [[684, "open"]]},
                     {"width_mm": 423, "openings": [[684, "open"]]}]))
        assert p["column_divider"]["height_mm"] == pytest.approx(684.0)
        assert p["column_divider"]["depth_mm"] == pytest.approx(544.0)


class TestDescribeNamesWhatTheSawWillDo:
    """`describe_design` is the document read aloud, and it said neither.

    The corner style changes what the saw does and the render does not draw
    it; the banding mode shrinks every carcass core. Both were absent from
    the prose AND the materials block, so the whole miter half of the D12
    disclosure could be deleted with the suite green.
    """

    @staticmethod
    def _describe(**kw):
        import asyncio
        import json

        from cabineteer.server import TOOL_DISPATCH
        args = dict(width=1219, height=663.6, depth=457,
                    drawer_config=[[627.6, "open"]])
        args.update(kw)
        loop = asyncio.new_event_loop()
        try:
            out = loop.run_until_complete(TOOL_DISPATCH["describe_design"](args))
        finally:
            loop.close()
        return json.loads(out[0].text)

    def test_materials_carries_both_tokens(self):
        m = self._describe()["materials"]
        assert m["carcass_corner_style"] == "butt"
        assert m["edge_band_mode"] == "none"

    def test_miters_are_described_with_the_long_point_number(self):
        d = self._describe(carcass_corner_style="miter")
        assert d["materials"]["carcass_corner_style"] == "miter"
        assert "miter" in d["prose"]
        assert "1219 mm long-point" in d["prose"]

    def test_hardwood_banding_says_the_panels_are_cut_narrower(self):
        prose = self._describe(edge_band_mode="hardwood",
                               edge_band_thickness_mm=3.2)["prose"]
        assert "3.2 mm hardwood edge banding" in prose
        assert "narrower" in prose

    def test_hot_melt_says_the_opposite_because_it_is(self):
        """It is ironed on after cutting, so no panel shrinks for it."""
        prose = self._describe(edge_band_mode="hot_melt")["prose"]
        assert "hot-melt" in prose
        assert "no panel shrinks" in prose

    def test_a_plain_build_gains_no_prose(self):
        prose = self._describe()["prose"]
        assert "miter" not in prose and "banding" not in prose
