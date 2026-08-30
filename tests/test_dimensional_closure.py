"""Every document agrees, and every document agrees with physics.

WHY THIS FILE EXISTS
--------------------
Three dimensioning bugs reached the bench in a month, and the suite was green
through all three:

  #88  the 3D builder and the cutlist each sized drawer faces, by different
       rules.  Two producers, one dimension.
  #89  the cutlist double-counted the drawer-box corners.  The only test in
       the area asserted the assembly doc EQUALLED the cutlist — two
       documents agreeing on the same wrong numbers.
  #91  a slide's clearance was applied to the wrong face of the box, and the
       check that should have caught it compared the result back against the
       constant it was derived from.

A 2026-08-29 review counted the suite's dimensional assertions: **5 % are
closure, 72 % are constants copied out of the implementation**.  A mutation
setting a divider to 1.0 mm passed 1927 tests and 313 eval scenarios.

So this module asserts two kinds of thing, and the second is the point.

**Agreement** — the same named dimension, collected from every producer that
emits it, must be one number.

**Physical predicates** — facts that reference no document at all: a slide has
to fit the cabinet it is ordered for; two solids cannot occupy the same space;
parts have to close into the object they are cut for; a stack of faces has to
tile the span it fills.  A predicate cannot be satisfied by two documents
agreeing on a wrong number, which is the whole lesson of #89.

Agreement assertions are only trustworthy when a predicate pins at least one
side.  Adding an agreement test on its own re-creates #89.

HOW TO USE IT
-------------
Known defects are marked ``xfail(strict=True)`` and named for their review ID.
Fixing one means DELETING its xfail, not editing the assertion.  Strict means a
defect fixed by accident also fails, so nothing drifts back silently.

Route collectors through the tool handlers and ``_cabinet_assembly``, never
through ``build_multi_bay_cabinet`` directly — calling the builder directly is
why the transition-shelf state (D8) was untestable for so long.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
from collections import Counter

import pytest

from cabineteer.cabinet import CabinetConfig, bays_from_config, face_layout
from cabineteer.drawer import box_config_for_opening
from cabineteer.joinery import CarcassJoinery, DrawerJoineryStyle
from cabineteer.server import TOOL_DISPATCH, _raw_panels_for_cabinet

HAS_CQ = importlib.util.find_spec("cadquery") is not None
skipif_no_cq = pytest.mark.skipif(not HAS_CQ, reason="needs CadQuery")

#: Solid-intersection tolerance. Two carcass parts touching face-to-face
#: produce a zero-volume common solid; anything above this is real overlap.
INTERSECT_TOL_MM3 = 1.0


def _run(coro):
    """Drive an async tool handler without leaking an event loop.

    A bare ``asyncio.run`` in a test module breaks later files in the same
    session (recorded in CLAUDE.md's gotchas).
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ─── The matrix ───────────────────────────────────────────────────────────
#
# Every axis that has ever moved a dimension, plus a depth sweep. The sweep
# matters: `test_cutlist_rows_close_into_the_box` passed at depth 457 by luck
# and failed at 391, because 457 happens to sit clear of every slide-length
# threshold. Fixed depths hide threshold bugs.

DRAWERS_3 = [(133.0, "drawer"), (110.0, "drawer"), (110.0, "drawer")]
DRAWER_AND_DOOR = [(200.0, "drawer"), (400.0, "door")]

#: Depths chosen to straddle the 563H's available lengths (229/305/381/457/533)
#: rather than to sit comfortably between them.
DEPTH_SWEEP = (330.0, 391.0, 420.0, 457.0, 470.0, 520.0, 600.0)


@dataclasses.dataclass(frozen=True)
class Case:
    """One config plus the raw column argument the tools take."""
    id: str
    cfg: CabinetConfig
    columns_raw: list | None = None


def _base(**kw) -> CabinetConfig:
    args = dict(
        width=800.0, height=720.0, depth=457.0,
        side_thickness=18.0, bottom_thickness=18.0, top_thickness=18.0,
        back_thickness=6.0, shelf_thickness=18.0,
        drawer_box_thickness=12.0,
        drawer_slide="blum_tandem_plus_563h",
        drawer_joinery=DrawerJoineryStyle.DRAWER_LOCK,
        drawer_corner_lip_mm=2.0,
        carcass_joinery=CarcassJoinery.FLOATING_TENON,
        openings=list(DRAWERS_3),
    )
    args.update(kw)
    return CabinetConfig(**args)


def _matrix() -> list[Case]:
    cases: list[Case] = []

    for capture in ("pocket", "rabbet", "dado"):
        for style in ("full_height", "under_top"):
            cases.append(Case(
                f"capture={capture}/back={style}",
                _base(back_capture=capture, back_style=style)))

    for joinery in (CarcassJoinery.FLOATING_TENON, CarcassJoinery.POCKET_SCREW,
                    CarcassJoinery.BISCUIT, CarcassJoinery.DOWEL):
        cases.append(Case(f"joinery={joinery.value}",
                          _base(carcass_joinery=joinery)))

    for style in DrawerJoineryStyle:
        cases.append(Case(f"drawer_joinery={style.value}",
                          _base(drawer_joinery=style)))

    for depth in DEPTH_SWEEP:
        cases.append(Case(f"depth={depth:g}", _base(depth=depth)))

    cases.append(Case("furniture_top", _base(furniture_top=True)))
    cases.append(Case("face_gap=2.5", _base(face_gap_mm=2.5)))
    cases.append(Case("banding=hardwood",
                      _base(edge_band_mode="hardwood", edge_band_thickness_mm=3.2)))
    cases.append(Case("banding=hot_melt",
                      _base(edge_band_mode="hot_melt", edge_band_thickness_mm=0.6)))
    cases.append(Case("door_over_drawer", _base(openings=list(DRAWER_AND_DOOR),
                                                door_hinge="blum_clip_top_blumotion_110_full")))
    cases.append(Case("shelf", _base(openings=[(684.0, "open")],
                                     fixed_shelf_positions=[350.0])))

    cols = [{"width_mm": 300.0, "openings": [[133, "drawer"], [110, "drawer"]]},
            {"width_mm": 446.0, "openings": [[243, "open"]]}]
    cases.append(Case("2col", _base(width=800.0, openings=[]), cols))

    # A depth that puts a slide threshold just past what a grooved back
    # leaves. The review measured the harm here: a 381 mm box on the paper
    # and a 457 mm runner on the BOM of the same sheet.
    cases.append(Case("dado/depth=470",
                      _base(back_capture="dado", depth=470.0)))

    # Two columns whose door-over-drawer transitions land at DIFFERENT
    # heights. This is the shape that grows a render-only transition shelf
    # spanning at min() of the two, driven through the divider.
    uneven = [
        {"width_mm": 380.0, "openings": [[150, "drawer"], [534, "door"]]},
        {"width_mm": 366.0, "openings": [[150, "drawer"], [150, "drawer"],
                                         [384, "door"]]},
    ]
    cases.append(Case("2col_uneven_door_over_drawer",
                      _base(width=800.0, openings=[],
                            door_hinge="blum_clip_top_blumotion_110_half"),
                      uneven))

    return cases


MATRIX = _matrix()
IDS = [c.id for c in MATRIX]


# ─── The defect register ──────────────────────────────────────────────────
#
# Every entry is a defect the 2026-08-29 review confirmed and NOBODY HAS
# FIXED YET. The assertion above it is the one that will pass when it is
# fixed — so the way to close one is to DELETE ITS LINE HERE, not to edit
# the assertion.
#
# ``strict=True`` means a defect that starts passing without its line being
# removed is itself a failure. That is deliberate: it stops a fix landing
# silently and it stops the register rotting into a list of things that used
# to be broken.
#
# Burn-down: len(KNOWN_DEFECTS) -> 0.

KNOWN_DEFECTS: dict[tuple[str, str | None], str] = {
    # ── D1 · "interior depth" has five spellings (448 / 451 / 457) ───────
    # The hardware BOM sizes the runner from `depth − back_thickness` while
    # the cutlist sizes the box from `depth − geo.clear_depth` and the
    # evaluator reads a third number. Fix: one accessor, in cabinet.py;
    # delete the hand-rolled copies at cutlist.py:1446/1553,
    # server.py:3721/4023 and assembly.py:636.
    ("test_the_slide_ordered_fits_the_cabinet_it_is_ordered_for", "depth=391"):
        "D1 — BOM orders a 381 mm runner for a 305 mm box, on the DEFAULT "
        "back capture. He would buy the wrong runners.",
    ("test_the_slide_ordered_fits_the_cabinet_it_is_ordered_for", "dado/depth=470"):
        "D1 — BOM orders a 457 mm runner into 452 mm of clear depth. It "
        "cannot be mounted, and evaluate_cabinet reports zero errors.",

    # ── D2 · assembly map draws the bottom 6 mm short ────────────────────
    # assembly.py:350 hardcodes `depth − back_thickness` under a comment
    # calling it "the cutlist convention", which it stopped being when
    # back_capture landed. Live on 6 of the 11 real cabinets. Fix: read
    # back_capture_geometry, and geo.top_depth/bottom_depth at :441.
    ("test_the_assembly_map_draws_the_panel_the_cutlist_cuts",
     "capture=rabbet/back=full_height"): "D2 — map bottom 451, cut 457.",
    ("test_the_assembly_map_draws_the_panel_the_cutlist_cuts",
     "capture=rabbet/back=under_top"): "D2 — map bottom 451, cut 457.",
    ("test_the_assembly_map_draws_the_panel_the_cutlist_cuts",
     "capture=dado/back=full_height"): "D2 — map bottom 451, cut 457.",
    ("test_the_assembly_map_draws_the_panel_the_cutlist_cuts",
     "capture=dado/back=under_top"): "D2 — map bottom 451, cut 457.",
    ("test_the_assembly_map_draws_the_panel_the_cutlist_cuts",
     "dado/depth=470"): "D2 — same, at a depth that also trips D1.",
    ("test_the_assembly_map_draws_the_panel_the_cutlist_cuts",
     "banding=hardwood"): "D2 — the map draws the finished panel where the "
                          "cutlist cuts a core, or the reverse. Under hardwood "
                          "banding the two differ by the band thickness.",

    ("test_the_design_payload_does_not_contradict_itself", "*"):
        "D1 — design_cabinet states interior.depth_mm 448 and, in the same "
        "JSON, cuts bottom_panel 451. Every case, every capture: this is the "
        "root of the five spellings.",
    # ...but design_multi_column_cabinet is already self-consistent here, so
    # these two are exempt and must STAY passing when D1 is fixed.
    ("test_the_design_payload_does_not_contradict_itself", "2col"): None,
    ("test_the_design_payload_does_not_contradict_itself",
     "2col_uneven_door_over_drawer"): None,

    # ── D3 · the design payload reports a full-height divider ────────────
    # server.py:3747 hardcodes cfg.height; server.py:4151 correctly cuts to
    # interior height for every butt joinery. The payload reports an 800 mm
    # divider standing in its own 764 mm interior — and it is the FIRST
    # document read, at approval time. Fix: cabinet.divider_cut_length(cfg),
    # called by both.
    ("test_the_design_payload_quotes_the_cutlist_dimensions", "2col"):
        "D3 — divider reported 720 mm (exterior), cut 684 mm (interior).",
    ("test_the_design_payload_quotes_the_cutlist_dimensions",
     "2col_uneven_door_over_drawer"):
        "D3 — same divider, plus D8's shelf in the same cabinet.",

    # ── D19 · the design payload reports FINISHED, the cutlist cuts CORE ──
    # Under hardwood banding the payload says the side is 457 deep and the
    # cutlist cuts a 453.8 core that finishes at 457. Both numbers are
    # right; nothing in the payload says which face it means. Same shape as
    # G1 (design_pulls dimensions a finished face and he drills a core) and
    # the same shape as #91. Fix: say the datum, and echo both.
    ("test_the_design_payload_quotes_the_cutlist_dimensions", "banding=hardwood"):
        "D19 — payload 457 (finished) vs cutlist 453.8 (core), with no "
        "statement of which the number is.",

    # ── D8 · the render-only transition shelf ────────────────────────────
    # server.py:5087 derives transition_shelf_zs, which emits a panel the
    # cutlist has never had and spans it at min() of the columns, driving it
    # through the divider. The shipped armoire_2col preset triggers it.
    # Fix: delete the auto-derivation (fixed_shelf_positions already does
    # this properly through every document), or make it a real per-bay panel.
    ("test_no_two_carcass_solids_occupy_the_same_space",
     "2col_uneven_door_over_drawer"):
        "D8 — the transition shelf is driven through the divider. Measured "
        "at 179,496 mm³ in the review.",
    ("test_the_render_contains_exactly_the_carcass_the_cutlist_cuts",
     "2col_uneven_door_over_drawer"):
        "D8 — the render grows a shelf that appears on no cutlist. Same "
        "shape as the furniture_top cap, which cost a batch of paper.",

    # ── D14 · five Domino rows have tenons longer than their mortises ────
    # Fix the five depths in DOMINO_SIZES, and derive
    # carcass_domino_size_for_thickness's threshold from the wall rule the
    # evaluator already applies.
    ("test_every_domino_tenon_fits_the_mortises_it_is_cut_for", None):
        "D14 — 8×40 selected for stock over 19 mm plunges 15 mm each side. "
        "The doc would tell him to count out tenons that cannot seat.",
}


@pytest.fixture(autouse=True)
def _register_known_defects(request):
    """Apply a strict xfail to (test, case) pairs in the register above."""
    node = request.node
    name = getattr(node, "originalname", None) or node.name.split("[")[0]
    case_id = None
    if hasattr(node, "callspec"):
        case = node.callspec.params.get("case")
        case_id = getattr(case, "id", None)
    # "*" registers a defect that shows on EVERY case of a test — a defect
    # that universal is one root cause, not N of them, and listing every
    # case would bury the register. An explicit entry mapping to None is an
    # EXEMPTION from the wildcard: that case is sound and must stay sound.
    if (name, case_id) in KNOWN_DEFECTS:
        reason = KNOWN_DEFECTS[(name, case_id)]
    else:
        reason = KNOWN_DEFECTS.get((name, "*"))
    if reason is not None:
        node.add_marker(pytest.mark.xfail(strict=True, reason=reason))


def test_the_register_is_not_silently_stale():
    """Every entry names a test that exists. Guards the register itself.

    A register entry whose test was renamed stops applying and takes its
    defect off the books without anyone noticing — the same shape as a check
    that cannot fire.
    """
    here = set(globals())
    assert all(v is None or isinstance(v, str) for v in KNOWN_DEFECTS.values())
    missing = sorted({t for (t, _c) in KNOWN_DEFECTS if t not in here})
    assert not missing, f"register names tests that no longer exist: {missing}"

    known_ids = set(IDS) | {None, "*"}
    stray = sorted({c for (_t, c) in KNOWN_DEFECTS if c not in known_ids})
    assert not stray, f"register names cases that are not in the matrix: {stray}"


# ─── Collectors — one per producer ────────────────────────────────────────

def cutlist_panels(case: Case):
    """Every panel the cutlist would cut, as (name, L, W, T, qty)."""
    carcass, thin, box, faces = _raw_panels_for_cabinet(case.cfg, case.columns_raw)
    return carcass + thin + box + faces


def carcass_rows(case: Case) -> Counter:
    """Carcass panels only, as a multiset keyed by (name, L, W, T).

    The back lives in the thin-stock list because it is packed with the
    6 mm goods, but it is a carcass part and the render draws it, so it
    belongs here.
    """
    carcass, thin, _box, _faces = _raw_panels_for_cabinet(
        case.cfg, case.columns_raw)
    rows = list(carcass) + [p for p in thin if not p.name.startswith("drawer_box")]
    c: Counter = Counter()
    for p in rows:
        c[(p.name, round(p.length, 1), round(p.width, 1),
           round(p.thickness, 1))] += p.quantity
    return c


#: Render node names that are not carcass panels.
_NOT_CARCASS = ("face", "door", "drawer", "pull", "leg", "foot", "manga",
                "worktop", "cap")


def render_carcass_name(raw: str) -> str | None:
    """Normalise a render node name to its cutlist row name, or None.

    The 3D names parts per bay (``bay0_left_side``) and per instance
    (``divider_0``); the cutlist names them by kind. Neither is wrong — but
    the comparison needs one vocabulary, and inventing it here rather than
    in the assertion keeps the assertion readable.
    """
    import re
    # Exclude on the WHOLE path: a drawer box's back panel is a node called
    # "back" under "bay0_drawer0", identical to the carcass's back except
    # for its ancestry.
    if any(t in raw for t in _NOT_CARCASS):
        return None
    n = raw.split("/")[-1]
    n = re.sub(r"^bay\d+_", "", n)
    n = re.sub(r"_\d+$", "", n)
    if n in ("left_side", "right_side", "side"):
        return "side"
    if n in ("top", "bottom", "back"):
        return n
    if n.startswith("divider"):
        return "column_divider"
    if "shelf" in n:
        return "shelf"
    return None


def render_parts(case: Case):
    """The 3D assembly's PartInfo list — what the approved picture contains."""
    from cabineteer.server import _cabinet_assembly
    _assy, parts, _info = _cabinet_assembly(
        case.cfg, case.columns_raw, include_feet=False)
    return parts


def placed_solids(case: Case):
    """[(node name, solid in WORLD coordinates)] for the whole assembly.

    ``PartInfo.shape`` holds a part in its own local frame — the assembly
    places it with a ``cq.Location``. Intersecting the local shapes says
    every left side overlaps every right side, which is how this predicate
    read on its first run. Accumulate the locations down the tree instead.
    """
    import cadquery as cq
    from cabineteer.server import _cabinet_assembly
    assy, _parts, _info = _cabinet_assembly(
        case.cfg, case.columns_raw, include_feet=False)

    out = []

    def walk(node, loc, path):
        for child in node.children:
            child_loc = child.loc if child.loc is not None else cq.Location()
            world = (loc * child_loc) if loc is not None else child_loc
            name = f"{path}/{child.name}" if path else child.name
            if child.obj is not None:
                for solid in child.obj.solids().vals():
                    out.append((name, solid.moved(world)))
            walk(child, world, name)

    # Full paths, not bare node names: a drawer box's back panel is called
    # "back", the same as the carcass's, and only the path tells them apart.
    walk(assy, None, "")
    return out


def _bb_overlap(a, b, tol=0.01):
    """Cheap prefilter — solid booleans are expensive, bbox tests are not."""
    return not (a.xmax <= b.xmin + tol or b.xmax <= a.xmin + tol
                or a.ymax <= b.ymin + tol or b.ymax <= a.ymin + tol
                or a.zmax <= b.zmin + tol or b.zmax <= a.zmin + tol)


# ─── Physical predicates ──────────────────────────────────────────────────
#
# These reference no document. They cannot be satisfied by two producers
# agreeing on a wrong number, which is what makes them the load-bearing half
# of this module.

@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_the_slide_ordered_fits_the_cabinet_it_is_ordered_for(case: Case):
    """A runner has to physically fit behind the cabinet face.

    Predicate, not agreement: the chosen slide length is compared against
    the clear space the back capture actually leaves, so it fails even when
    the cutlist and the hardware BOM agree with each other. The 2026-08
    review found a ``dado`` carcass at depth 470 printing a 381 mm box while
    the sheet's own BOM ordered a 457 mm runner that cannot mount, with
    ``evaluate_cabinet`` reporting zero errors.
    """
    from cabineteer.cabinet import back_capture_geometry
    from cabineteer.hardware import get_slide

    from cabineteer.cutlist import slide_lines_for_cabinet_config

    geo = back_capture_geometry(case.cfg)
    clear = case.cfg.depth - geo.clear_depth

    # What the cutlist says he will build.
    box_depths = set()
    for bay in bays_from_config(case.cfg, case.columns_raw):
        for op in bay.openings:
            if op.opening_type == "drawer":
                d = box_config_for_opening(case.cfg, bay.interior_width,
                                           op.height_mm,
                                           case.cfg.interior_depth, op)
                box_depths.add(round(d.box_depth, 1))
    if not box_depths:
        pytest.skip("no drawers")

    # What the BOM says he will buy. Read from the SKU, because the SKU is
    # what arrives in the box.
    for line in slide_lines_for_cabinet_config(case.cfg, case.columns_raw):
        if line.category != "slide":
            continue
        spec = get_slide(case.cfg.drawer_slide)
        lengths = [L for L, sku in spec.part_numbers.items()
                   if sku == line.model_number]
        if not lengths:
            continue
        ordered = float(lengths[0])
        needed = ordered + spec.rear_bracket_inset + spec.front_bracket_inset
        assert needed <= clear + 0.01, (
            f"{case.id}: the BOM orders {line.model_number} — a {ordered:g} mm "
            f"runner, {needed:g} mm with its brackets — but the carcass leaves "
            f"{clear:g} mm clear behind the face (depth {case.cfg.depth:g} less "
            f"{geo.clear_depth:g} for a {case.cfg.back_capture} back). "
            f"It cannot be mounted.")
        assert ordered in box_depths, (
            f"{case.id}: the BOM orders a {ordered:g} mm runner but the cutlist "
            f"prints boxes {sorted(box_depths)} deep. A drawer box has to be as "
            f"deep as the runner it rides on.")


@skipif_no_cq
@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_no_two_carcass_solids_occupy_the_same_space(case: Case):
    """Two parts cannot be in the same place. Needs no reference document.

    This is the predicate that catches a render-only panel driven through a
    divider (D8 measured 179,496 mm³) without anyone having to notice the
    panel is missing from the cutlist first.
    """
    solids = [(n, s) for n, s in placed_solids(case)
              if render_carcass_name(n) is not None]
    boxes = [s.BoundingBox() for _n, s in solids]
    for i in range(len(solids)):
        for j in range(i + 1, len(solids)):
            if not _bb_overlap(boxes[i], boxes[j]):
                continue           # cannot intersect; skip the expensive boolean
            (na, a), (nb, b) = solids[i], solids[j]
            try:
                vol = a.intersect(b).Volume()
            except Exception:
                continue           # OCC raises on disjoint solids; that is a pass
            assert vol < INTERSECT_TOL_MM3, (
                f"{case.id}: {na} and {nb} overlap by {vol:.0f} mm³")


@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_carcass_parts_close_into_the_stated_box(case: Case):
    """Sides + top + bottom have to add up to the cabinet you asked for.

    Closure, not parity: reconstructs the exterior from the parts rather
    than comparing two documents' idea of it.
    """
    rows = carcass_rows(case)
    sides = [k for k in rows if k[0] == "side"]
    tops = [k for k in rows if k[0] == "top"]
    bottoms = [k for k in rows if k[0] == "bottom"]
    assert sides and tops and bottoms, f"{case.id}: missing a carcass panel"

    side_t = sides[0][3]
    # Width: the top spans between the sides (butt) or the full exterior.
    top_len = tops[0][1]
    assert top_len + 2 * side_t == pytest.approx(case.cfg.width, abs=0.05), (
        f"{case.id}: top {top_len:g} + two {side_t:g} sides = "
        f"{top_len + 2 * side_t:g}, not the stated width {case.cfg.width:g}")
    # Height: the sides run the full exterior on a butt carcass.
    assert sides[0][1] == pytest.approx(case.cfg.height, abs=0.05), (
        f"{case.id}: side length {sides[0][1]:g} is not the stated height "
        f"{case.cfg.height:g}")


@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_the_face_stack_tiles_its_span(case: Case):
    """Faces plus their gaps fill the opening exactly — no more, no less.

    The #88 failure was a stack that summed to 371 in a 353 mm interior and
    no document noticed.
    """
    bays = bays_from_config(case.cfg, case.columns_raw)
    faces = [f for f in face_layout(bays, furniture_top=case.cfg.furniture_top)
             if f.kind != "top_cap"]
    if not faces:
        pytest.skip("no faces")
    gap = case.cfg.face_gap_mm
    by_bay: dict[int, list] = {}
    for f in faces:
        by_bay.setdefault(f.bay, []).append(f)
    for bay, panels in by_bay.items():
        panels.sort(key=lambda p: p.z)
        for lo, hi in zip(panels, panels[1:]):
            if lo.slot == hi.slot:      # a door pair — side by side, not stacked
                continue
            actual = hi.z - (lo.z + lo.height)
            assert actual == pytest.approx(gap, abs=0.05), (
                f"{case.id} bay {bay}: {actual:g} mm between faces at "
                f"z={lo.z:g} and z={hi.z:g}, not the {gap:g} mm reveal")


@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_drawer_box_parts_close_into_their_box(case: Case):
    """Sides + front + back close into the stated box width and depth.

    #89's shape. Parametrized over the depth sweep because the pre-existing
    version of this passed at 457 and failed at 391.
    """
    bays = bays_from_config(case.cfg, case.columns_raw)
    seen = 0
    for bay in bays:
        for op in bay.openings:
            if op.opening_type != "drawer":
                continue
            d = box_config_for_opening(case.cfg, bay.interior_width,
                                       op.height_mm, case.cfg.interior_depth, op)
            seen += 1
            j = d.joinery
            if j.laps_front:
                assert d.front_back_panel_length == pytest.approx(d.box_width)
                assert (d.side_panel_length + 2 * j.lip
                        == pytest.approx(d.box_depth)), (
                    f"{case.id}: sides {d.side_panel_length:g} + two "
                    f"{j.lip:g} lips ≠ box depth {d.box_depth:g}")
            else:
                assert d.side_panel_length == pytest.approx(d.box_depth)
                assert (d.front_back_panel_length
                        + 2 * (d.side_thickness - j.engagement_x)
                        == pytest.approx(d.box_width))
            assert (d.bottom_panel_width
                    == pytest.approx(d.box_inside_width + 2 * d.bottom_dado_depth))
    if not seen:
        pytest.skip("no drawers")


# ─── Agreement — every producer, one number ───────────────────────────────

@skipif_no_cq
@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_the_render_contains_exactly_the_carcass_the_cutlist_cuts(case: Case):
    """A part in the picture and not on the paper is a part he never cuts.

    CLAUDE.md already records this shape once: the ``furniture_top`` cap
    "existed in approved renders and never on any cutlist". D8 is the same
    shape, unfixed.
    """
    cut_names = Counter()
    for (name, *_dims), qty in carcass_rows(case).items():
        cut_names[name] += qty

    render_names: Counter = Counter()
    for p in render_parts(case):
        base = render_carcass_name(p.name)
        if base is not None:
            render_names[base] += 1

    cut_shelves = sum(v for k, v in cut_names.items() if k.startswith("shelf"))
    cut_norm = Counter({k: v for k, v in cut_names.items()
                        if not k.startswith("shelf")})
    if cut_shelves:
        cut_norm["shelf"] = cut_shelves

    assert render_names == cut_norm, (
        f"{case.id}: the render and the cutlist disagree about which carcass "
        f"parts exist.\n  render:  {dict(sorted(render_names.items()))}\n"
        f"  cutlist: {dict(sorted(cut_norm.items()))}")


#: design_* payload panel key -> cutlist row name.
_PAYLOAD_TO_CUTLIST = {
    "side_panel": "side",
    "bottom_panel": "bottom",
    "top_panel": "top",
    "back_panel": "back",
    "column_divider": "column_divider",
}


@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_the_design_payload_quotes_the_cutlist_dimensions(case: Case):
    """The first document read at approval time must not contradict the paper.

    D3: ``design_multi_column_cabinet`` reports the divider at full exterior
    height while the cutlist, the 3D and the assembly map all cut it to the
    interior.

    The ``compared`` counter is not decoration. The first version of this
    test used the wrong payload keys, matched nothing, and passed on every
    case — a check that cannot fail, which is the exact class this module
    exists to catch.
    """
    tool = ("design_multi_column_cabinet" if case.columns_raw
            else "design_cabinet")
    args: dict = {"width": case.cfg.width, "height": case.cfg.height,
                  "depth": case.cfg.depth,
                  "side_thickness": case.cfg.side_thickness,
                  "bottom_thickness": case.cfg.bottom_thickness,
                  "top_thickness": case.cfg.top_thickness,
                  "back_thickness": case.cfg.back_thickness,
                  "carcass_joinery": case.cfg.carcass_joinery.value,
                  "back_capture": case.cfg.back_capture,
                  "back_style": case.cfg.back_style}
    if case.columns_raw:
        args["columns"] = case.columns_raw
    else:
        args["drawer_config"] = [[o.height_mm, o.opening_type]
                                 for o in case.cfg.openings]
    payload = json.loads(_run(TOOL_DISPATCH[tool](args))[0].text)

    rows: dict[str, tuple[float, float, float]] = {}
    for p in cutlist_panels(case):
        rows.setdefault(p.name, (p.length, p.width, p.thickness))

    compared = 0
    for key, reported in (payload.get("panels") or {}).items():
        name = _PAYLOAD_TO_CUTLIST.get(key)
        if name is None or name not in rows or not isinstance(reported, dict):
            continue
        cut_l, cut_w, cut_t = rows[name]
        # The payload names its axes (width/height/depth); the cutlist calls
        # them length and width. Compare the SET, so a transposed panel still
        # matches and only a genuinely different size fails.
        said = {round(v, 1) for k, v in reported.items()
                if k.endswith("_mm") and k != "thickness_mm"}
        assert said == {round(cut_l, 1), round(cut_w, 1)}, (
            f"{case.id}: {tool} reports {key} as "
            f"{ {k: v for k, v in reported.items() if k.endswith('_mm')} } "
            f"but the cutlist cuts {cut_l:g} x {cut_w:g}")
        assert round(reported.get("thickness_mm", cut_t), 1) == round(cut_t, 1)
        compared += 1

    assert compared >= 3, (
        f"{case.id}: only compared {compared} panels — this test has gone "
        f"vacuous. Check _PAYLOAD_TO_CUTLIST against the payload's keys.")


@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_the_design_payload_does_not_contradict_itself(case: Case):
    """One JSON, one interior depth.

    D1's most visible face: ``design_cabinet`` prints
    ``interior.depth_mm 448`` beside its own ``bottom_panel 451`` and a
    ``fixed_shelf 448``, in the same object.
    """
    tool = ("design_multi_column_cabinet" if case.columns_raw
            else "design_cabinet")
    args: dict = {"width": case.cfg.width, "height": case.cfg.height,
                  "depth": case.cfg.depth,
                  "side_thickness": case.cfg.side_thickness,
                  "bottom_thickness": case.cfg.bottom_thickness,
                  "top_thickness": case.cfg.top_thickness,
                  "back_thickness": case.cfg.back_thickness,
                  "carcass_joinery": case.cfg.carcass_joinery.value,
                  "back_capture": case.cfg.back_capture,
                  "back_style": case.cfg.back_style}
    if case.columns_raw:
        args["columns"] = case.columns_raw
    else:
        args["drawer_config"] = [[o.height_mm, o.opening_type]
                                 for o in case.cfg.openings]
    payload = json.loads(_run(TOOL_DISPATCH[tool](args))[0].text)

    interior = payload.get("interior") or {}
    stated = interior.get("depth_mm")
    if stated is None:
        pytest.skip("payload states no interior depth")
    bottom = (payload.get("panels") or {}).get("bottom_panel") or {}
    drawn = bottom.get("depth_mm")
    if drawn is None:
        pytest.skip("payload states no bottom depth")
    assert round(stated, 1) == round(drawn, 1), (
        f"{case.id}: {tool} says the interior is {stated:g} mm deep and in "
        f"the same payload cuts the bottom panel {drawn:g} mm deep")


@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_the_assembly_map_draws_the_panel_the_cutlist_cuts(case: Case):
    """The mortise map's own comment promises "map dims match the parts in hand".

    D2: it draws the bottom 6 mm short on 6 of the 11 real cabinets, because
    it hardcodes ``depth − back_thickness`` under a comment calling that
    "the cutlist convention", which it stopped being when back_capture landed.
    """
    from cabineteer.assembly import build_assembly_plan
    if case.cfg.carcass_joinery is not CarcassJoinery.FLOATING_TENON:
        pytest.skip("assembly plan is floating-tenon only")
    plan = build_assembly_plan(case.cfg)
    rows = {}
    for p in cutlist_panels(case):
        rows.setdefault(p.name, (p.length, p.width))

    alias = {"left side": "side", "right side": "side", "top": "top",
             "bottom": "bottom", "column divider": "column_divider"}
    for pm in plan.panels:
        key = alias.get(pm.panel.lower())
        if key is None or key not in rows:
            continue
        cut_l, cut_w = rows[key]
        drawn = {round(pm.draw_width, 1), round(pm.draw_height, 1)}
        expect = {round(cut_l, 1), round(cut_w, 1)}
        assert drawn == expect, (
            f"{case.id}: the assembly map draws '{pm.panel}' at "
            f"{pm.draw_width:g} × {pm.draw_height:g}, the cutlist cuts "
            f"{cut_l:g} × {cut_w:g}")


# ─── Anchors that are not derived from anything in this repo ──────────────

def test_every_domino_tenon_fits_the_mortises_it_is_cut_for():
    """A 40 mm tenon into 2 × 15 mm of mortise does not seat.

    Free win: needs no datasheet, only internal consistency. Five of the ten
    catalogue rows currently violate it, and the assembly doc would tell you
    to count out tenons that cannot go in.
    """
    from cabineteer.joinery import DOMINO_SIZES
    bad = [(k, s.tenon_length, 2 * s.mortise_depth_per_side)
           for k, s in DOMINO_SIZES.items()
           if 2 * s.mortise_depth_per_side < s.tenon_length]
    assert not bad, (
        "tenon longer than the two mortises it seats into: "
        + "; ".join(f"{k}: {t:g} mm tenon into {m:g} mm" for k, t, m in bad))


# ─── The sheet layout is checked where it is produced ─────────────────────

class TestThePlacementPostConditionBites:
    """A post-condition nobody can demonstrate is a post-condition nobody has.

    The review found the sheet-layout surface had no closure coverage at
    all: a one-character kerf-sign flip put 12 overlapping pairs on the
    printed SVG with 1927 tests and 313 eval scenarios green.
    """

    def _result(self):
        from cabineteer.cutlist import CutlistPanel, SheetStock, optimize_cutlist
        panels = [CutlistPanel(name=f"p{i}", length=400.0, width=300.0,
                               thickness=18.0, quantity=1)
                  for i in range(6)]
        stock = SheetStock(name="t", length=2440.0, width=1220.0,
                           thickness=18.0, material="baltic_birch")
        return optimize_cutlist(panels, stock, algorithm="strip")

    def test_a_clean_layout_passes(self):
        assert self._result().placements

    def test_an_overlap_is_refused(self):
        from cabineteer.cutlist import _assert_placements_valid
        res = self._result()
        a, b = res.placements[0], res.placements[1]
        b.x, b.y, b.sheet_index = a.x, a.y, a.sheet_index   # stack them
        with pytest.raises(ValueError, match="overlapped"):
            _assert_placements_valid(res)

    def test_a_piece_off_the_sheet_is_refused(self):
        from cabineteer.cutlist import _assert_placements_valid
        res = self._result()
        res.placements[0].x = res.stock_sheet.length - 1.0
        with pytest.raises(ValueError, match="off a"):
            _assert_placements_valid(res)

    @pytest.mark.parametrize("algorithm", ["strip", "opcut", "rectpack"])
    def test_every_available_optimizer_is_covered(self, algorithm):
        """The post-condition lives in optimize_cutlist, not in one packer."""
        from cabineteer.cutlist import (CutlistPanel, SheetStock,
                                        optimize_cutlist)
        panels = [CutlistPanel(name=f"p{i}", length=600.0, width=400.0,
                               thickness=18.0, quantity=2) for i in range(4)]
        stock = SheetStock(name="t", length=2440.0, width=1220.0,
                           thickness=18.0, material="baltic_birch")
        try:
            res = optimize_cutlist(panels, stock, algorithm=algorithm)
        except ImportError:
            pytest.skip(f"{algorithm} not installed")
        assert res.placements
