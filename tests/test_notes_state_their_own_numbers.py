"""Every number in a note has to come from the thing the note describes.

The 2026-08-29 review named this class and called it the fastest-growing one:
**prose that asserts a dimension it did not derive**. A correct number with a
hand-written sentence beside it that contradicts it.

Three that reached paper:

  D4   every false-front row printed "{face_gap_mm} mm gaps above/below" —
       right at the three internal boundaries of his sideboard and wrong at
       both anchored ends. Obeyed literally it wants 647.6 mm of face in a
       627.6 mm span. It was the only positioning statement on any document.
  D5   the box steps read their numbers off ``boxes[0]`` and asserted them of
       every box: "Every part of every box takes the same groove", printed
       directly under a step warning the bottoms are NOT all the same. The
       groove's width — the one dimension that varies — was never stated.
  D18  band pieces for a 4-edge perimeter were emitted at the panel's CORE
       length, while the doc's own corner note said the long pair laps the
       short pair. 56 strips on the sideboards, and at 1/4" the piece comes
       out shorter than the edge it must cover.

The review explicitly REJECTED the obvious remedy — a provenance string on
every dimension — because ``_face_note``'s gap text, the groove step and the
width remedy already WERE provenance strings, and every one of them was
wrong. More hand-written prose next to correct numbers makes the surface
bigger. What it asked for instead is this test.

WHAT IT ASSERTS
---------------
For every note rendered across a matrix: extract every number out of the
prose, and assert each one appears among the values that object actually
computed. The test is allowed to derive — it is the NOTE that must not.
The objects it derives from (``FacePanel``, ``CutlistPanel``, the capture
geometry) are pinned independently by ``test_dimensional_closure.py``'s
physical predicates, so this is not two documents agreeing with each other.

A hardcoded literal fails by construction: ``face_gap_mm`` is not among a
face's own numbers at an anchored end, because there is no gap there.

WHAT IT CANNOT CATCH, said plainly
----------------------------------
A wrong number that happens to equal a right number elsewhere on the same
object. Reinstating D4 on a four-drawer stack does NOT fail this test — the
hardcoded 4 mm is a genuine reveal at that cabinet's three internal
boundaries, so it is in the allowed set even though the sentence attaches it
to the two anchored ends where the reveal is 0. It fails on ``one_opening``
and ``tall_narrow_face``, where a single face has no internal boundary and 4
is nobody's number. That is why the matrix carries those cases, and it is the
general shape of the limit: this test proves a number BELONGS to the object,
not that it is attached to the right clause. Pinning the clause is the job of
the closure module's physical predicates (faces tile their span) and of the
per-defect tests beside them.

**It does not catch D5 at all**, and an early write-up of this module claimed
it did. Measured: reinstating the ``boxes[0]`` groove step leaves all of these
green, because 6 mm and 13 mm are real numbers of real boxes in that run — the
defect is that the sentence asserts them of EVERY box, which is a claim about
scope, not about a value. ``test_assembly_instructions`` catches that, by
scanning the whole run of steps for "one saw setup" and by requiring the
sentences to be invariant under reversing the box order. Three different
mechanisms for three different shapes; none of them subsumes the others.
"""

from __future__ import annotations

import re

import pytest

from cabineteer.assembly import build_assembly_plan, build_drawer_box_plans
from cabineteer.cabinet import (INNER_FACE_OVERLAY_MM, back_capture_geometry,
                                bays_from_config, build_cabinet_config,
                                face_layout)
from cabineteer.server import _raw_panels_for_cabinet

# ─── The extractor ────────────────────────────────────────────────────────
#
# Rules the real notes forced, not a guess:
#   * "330.8×282.7" must match as ONE token or the right half is lost —
#     a lookbehind cannot reach it.
#   * unitless dimensions are real: "(415.2 inside the box + 12 of groove)".
#   * part numbers (DF 500, 606N, 173L8100) carry no unit and must not be
#     read as dimensions; percentages and durations are excluded by unit.

_TOKEN = re.compile(
    r"""(?P<pair>(?<![\w.])\d+(?:\.\d+)?\s*[×x]\s*\d+(?:\.\d+)?)
      | (?P<unit>(?<![\w.])\d+(?:\.\d+)?(?=\s*(?:mm\b|\bm\b)))
      | (?P<bare>(?<=[(+])\s?\d+(?:\.\d+)?(?=\s+(?:inside|of)\b))
    """,
    re.X,
)

#: Values that appear in prose as shop facts, not as dimensions of the part.
#: Whitelisted BY NAME so the number still has to match the named source —
#: a bare literal in a template is not covered by any of these.
_SHOP_FACTS = {
    1.0,      # "within 1 mm" — the dry-fit diagonal tolerance
    2.0,      # "2 mm" cabinetmaker's-triangle / reveal minimum in prose
    3.0,      # "3 mm" clamp-block guidance
    10.0,     # DF500_BASE_HEIGHT_MM, and BAND_PROUD_ALLOWANCE_MM
    90.0,     # "90°"
    45.0,     # "45°"
}


def numbers_in(text: str) -> set[float]:
    """Every dimension stated in ``text``, in mm, unrounded.

    Rounding here and rounding again in the allowed set put 504.55 on
    opposite sides: the printed string round-trips to just below the float
    it was formatted from. Compare raw, with a tolerance, once.
    """
    out: set[float] = set()
    for m in _TOKEN.finditer(text or ""):
        if m.group("pair"):
            for half in re.split(r"[×x]", m.group("pair")):
                out.add(float(half.strip()))
        else:
            out.add(float((m.group("unit") or m.group("bare")).strip()))
    return {v for v in out
            if not any(abs(v - f) < 1e-9 for f in _SHOP_FACTS)}


def unaccounted(text: str, allowed) -> list[float]:
    """Numbers in ``text`` that no value in ``allowed`` explains.

    Tolerance, not equality: a note prints 504.55 and the value behind it is
    504.5500000000001, so ``round(_, 1)`` lands on opposite sides — the
    string round-trips to just below the float it came from. Comparing at
    0.06 mm is far tighter than any real defect in this class (the smallest
    was D18's 6.4 mm) and immune to that.
    """
    return sorted(v for v in numbers_in(text)
                  if not any(abs(v - a) < 0.06 for a in allowed))


# ─── The matrix ───────────────────────────────────────────────────────────
#
# Axes chosen because each one MAKES one of these defects appear. A note
# that quotes a constant is only wrong where the constant and the instance
# disagree, so a single-axis matrix cannot see any of them: the review found
# the band note claiming a band on a furniture_top's capped edge for exactly
# this reason.

def _cfg(**kw):
    base = dict(width=900, height=760, depth=560,
                side_thickness=18, bottom_thickness=18, top_thickness=18,
                shelf_thickness=18, back_thickness=6,
                drawer_box_thickness=12, drawer_joinery="drawer_lock",
                carcass_joinery="floating_tenon")
    base.update(kw)
    return build_cabinet_config(base)


#: (id, cfg, columns_raw)
def _matrix():
    stack4 = [[282.7, "drawer"], [110.3, "drawer"],
              [110.3, "drawer"], [220.7, "drawer"]]
    cases = [
        ("single_stack", _cfg(drawer_config=list(stack4)), None),
        ("one_opening", _cfg(drawer_config=[[724, "drawer"]]), None),
        ("door_over_drawer",
         _cfg(drawer_config=[[200, "drawer"], [524, "door"]],
              door_hinge="blum_clip_top_blumotion_110_full"), None),
        ("furniture_top",
         _cfg(drawer_config=list(stack4), furniture_top=True), None),
        ("face_gap_2.5",
         _cfg(drawer_config=list(stack4), face_gap_mm=2.5), None),
    ]
    for thk in (0.6, 3.2, 6.35):
        mode = "hot_melt" if thk < 1 else "hardwood"
        cases.append((f"band_{mode}_{thk:g}",
                      _cfg(drawer_config=list(stack4), edge_band_mode=mode,
                           edge_band_thickness_mm=thk), None))
    for cap in ("rabbet", "dado"):
        cases.append((f"capture_{cap}",
                      _cfg(drawer_config=list(stack4), back_capture=cap), None))
    # Mirrored bays: identical panels, opposite overlays — the pair that
    # consolidated into one row carrying both claims.
    mirrored = [{"width_mm": 300.0, "openings": [[200, "drawer"]]},
                {"width_mm": 264.0, "openings": [[200, "drawer"]]},
                {"width_mm": 300.0, "openings": [[200, "drawer"]]}]
    cases.append(("mirrored_bays",
                  _cfg(width=900, height=236, columns=mirrored), mirrored))
    # Mixed bottoms: the size rule gives a wide, tall box a 12 mm bottom and
    # a small one 6 mm. This is D5's shape and no other case has it.
    mixed = [{"width_mm": 560.0,
              "openings": [[300, "drawer"], [104, "drawer"]]}]
    cases.append(("mixed_bottoms",
                  _cfg(width=1000, height=460, columns=mixed), mixed))
    # A face taller than it is wide, for the band edge labels.
    cases.append(("tall_narrow_face",
                  _cfg(width=320, height=760,
                       drawer_config=[[724, "drawer"]]), None))
    return cases


MATRIX = _matrix()
IDS = [c[0] for c in MATRIX]


# ─── What each object legitimately knows ──────────────────────────────────


def _face_values(cfg, columns_raw) -> set[float]:
    """Every number a show-face note may state.

    Read straight off ``cabinet.face_placements`` — the object's own
    computed values, which is precisely the rule being enforced. That the
    placement is itself correct is a different question, pinned by
    ``TestAnchoredEndsAreDerivedToo`` (explicit numbers for a flush stack,
    an inset door and a furniture_top) and by the closure module's predicate
    that a face stack tiles its span. This test's job is only that the PROSE
    quotes nothing else.
    """
    from cabineteer.cabinet import face_placements

    band_t = (float(cfg.edge_band_thickness_mm)
              if cfg.edge_band_mode == "hardwood" else 0.0)
    bays = bays_from_config(cfg, columns_raw)

    vals: set[float] = {float(cfg.side_thickness),
                        float(INNER_FACE_OVERLAY_MM)}
    if band_t:
        vals.add(band_t)
    for p in face_layout(bays):
        vals |= {p.width, p.height, p.thickness,
                 p.width - 2 * band_t, p.height - 2 * band_t}
    for pl in face_placements(bays):
        vals |= {pl.reveal_below, pl.reveal_above, pl.datum,
                 pl.left_lap, pl.right_lap,
                 abs(pl.reveal_below), abs(pl.reveal_above), abs(pl.datum),
                 abs(pl.left_lap), abs(pl.right_lap)}
    return vals


def _carcass_values(cfg) -> set[float]:
    """Numbers a carcass row's note may state."""
    geo = back_capture_geometry(cfg)
    band_t = (float(cfg.edge_band_thickness_mm)
              if cfg.edge_band_mode == "hardwood" else 0.0)
    vals = {round(v, 1) for v in (
        cfg.width, cfg.height, cfg.depth, cfg.interior_width,
        cfg.interior_height, cfg.interior_depth,
        cfg.side_thickness, cfg.top_thickness, cfg.bottom_thickness,
        cfg.shelf_thickness, cfg.back_thickness,
        geo.width, geo.height, geo.top_depth, geo.bottom_depth,
        geo.cut_run, geo.cut_depth, geo.setback, geo.engagement,
        geo.lap_run, geo.lap_depth, geo.clear_depth,
    )}
    if band_t:
        vals.add(round(band_t, 1))
        vals |= {round(v - band_t, 1) for v in list(vals)}
    return vals


def _box_values(cfg, columns_raw) -> set[float]:
    """Numbers a drawer-box row or box step may state, over the whole run."""
    boxes = build_drawer_box_plans(cfg)
    vals: set[float] = set()
    for b in boxes:
        for attr in ("side_length", "side_height", "front_back_length",
                     "front_back_height", "stock_thickness", "bottom_length",
                     "bottom_width", "bottom_thickness", "dado_depth",
                     "dado_inset", "lip", "slide_length", "opening_width",
                     "box_width", "box_inside_width", "box_depth",
                     "box_height", "opening_height"):
            v = getattr(b, attr, None)
            if isinstance(v, (int, float)):
                vals.add(round(float(v), 1))
    # A box step may also state how many of a thing there are.
    vals |= {float(len(boxes)),
             float(len({b.side_height for b in boxes})),
             float(len({b.bottom_thickness for b in boxes}))}
    # Derived statements the steps make about the parts.
    for b in boxes:
        vals.add(round(b.box_width - b.box_inside_width, 1))
        vals.add(round(2 * b.dado_depth, 1))
        vals.add(round(2 * b.lip, 1))       # what a front-lapped side loses
    return vals


# ─── The assertions ───────────────────────────────────────────────────────


@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_every_number_on_a_panel_row_is_that_panels_own(case):
    """A cutlist row's note may only quote numbers the row computed."""
    _id, cfg, cols = case
    carcass, thin, box, faces = _raw_panels_for_cabinet(cfg, cols)

    allowed = {
        "carcass": _carcass_values(cfg),
        "face": _face_values(cfg, cols) | _carcass_values(cfg),
        "box": _box_values(cfg, cols) | {float(cfg.drawer_box_thickness)},
    }
    # Group by what the row IS, not which list it arrived in: a 6 mm drawer
    # bottom is emitted into the thin-stock list and would otherwise be
    # checked against the carcass's numbers.
    def _kind(p):
        if p.name.startswith("drawer_box"):
            return "box"
        return "face" if p.name in ("false_front", "door", "top_front_cap") \
            else "carcass"

    rows = carcass + thin + box + faces
    groups = [(k, [p for p in rows if _kind(p) == k])
              for k in ("carcass", "face", "box")]

    checked = 0
    for kind, panels in groups:
        ok = allowed[kind] | {round(float(v), 1) for p in panels for v in
                              (p.length, p.width, p.thickness, p.quantity)}
        for p in panels:
            if not p.notes:
                continue
            checked += 1
            stray = unaccounted(p.notes, ok)
            assert not stray, (
                f"{_id}: the {p.name} row's note states {stray}, which is "
                f"not among that panel's own numbers.\n  note: {p.notes}")
    assert checked >= 3, f"{_id}: only checked {checked} notes"


@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_every_number_in_a_box_step_is_the_runs_own(case):
    """D5: the steps describe the RUN, so box zero's numbers are not enough."""
    _id, cfg, cols = case
    if cfg.carcass_joinery.value != "floating_tenon":
        pytest.skip("assembly plan is floating-tenon only")
    plan = build_assembly_plan(cfg)
    if not plan.box_steps:
        pytest.skip("no drawer boxes in this case")

    ok = _box_values(cfg, cols) | _carcass_values(cfg)
    for i, step in enumerate(plan.box_steps):
        text = " ".join((step.title, step.body) + tuple(step.checklist))
        stray = unaccounted(text, ok)
        assert not stray, (
            f"{_id}: box step {i} ({step.title!r}) states {stray}, which is "
            f"not a number of any box in this run")


@pytest.mark.parametrize("case", MATRIX, ids=IDS)
def test_a_merged_row_never_carries_two_answers_to_one_question(case):
    """D4's other half: consolidation must not concatenate contradictions.

    Two mirrored bays cut the same panel, so their rows consolidate — and
    the note then claimed both "18 mm left / 8 mm right" and "8 mm left /
    18 mm right" for one part, plus "species TBD" twice.
    """
    from cabineteer.cutlist import consolidate_bom

    _id, cfg, cols = case
    carcass, thin, box, faces = _raw_panels_for_cabinet(cfg, cols)
    for row in consolidate_bom(carcass + thin + box + faces):
        if not row.notes:
            continue
        clauses = [c.strip() for c in row.notes.split(";") if c.strip()]
        assert len(clauses) == len(set(clauses)), (
            f"{_id}: the {row.name} row repeats a clause verbatim — "
            f"consolidation concatenated instead of merging.\n  {row.notes}")
        # One clause SHAPE may appear once per physical part — a qty-2 row
        # of identical faces at two heights states two datums, and both are
        # true. What must never happen is more variants than there are
        # parts: that is one part being handed two different instructions,
        # which is what a mirrored pair of bays used to produce ("18 mm
        # left / 8 mm right" AND "8 mm left / 18 mm right" on one row).
        shapes: dict[str, set] = {}
        for c in clauses:
            shapes.setdefault(_TOKEN.sub("#", c), set()).add(c)
        for shape, variants in shapes.items():
            assert len(variants) <= row.quantity, (
                f"{_id}: the {row.name} row covers {row.quantity} part(s) "
                f"but states {len(variants)} different answers to the same "
                f"question: {sorted(variants)}")


def test_the_extractor_actually_finds_numbers():
    """A parser that matches nothing makes every assertion above vacuous."""
    assert numbers_in("core — band 4 edges to finished 330.8×282.7") == \
        {330.8, 282.7}
    assert numbers_in("6 mm deep, 13 mm up from the bottom edge") == \
        {6.0, 13.0}
    assert numbers_in("16.1 m of edges") == {16.1}
    # Part numbers carry no unit and must not read as dimensions.
    assert numbers_in("DF 500 with the 606N screws, plate 173L8100") == set()
    # A percentage is not a dimension.
    assert numbers_in("+15% waste") == set()
    # Unitless dimensions in parentheses are real.
    assert 415.2 in numbers_in("(415.2 inside the box + 12 of groove)")


# ─── The same class, found by the lens that mapped the surface ────────────


class TestClaimsAboutStockAndMachines:
    """Three more notes that asserted a number they never read.

    Each was found by an inventory sweep of every prose template in the
    package, not by a failing build — which is the point: none of them broke
    a single test when it was corrected, because nothing was looking.
    """

    def test_the_hot_melt_roll_note_measures_the_edges_it_covers(self):
        """N1 — the note said "covers 18 mm edges" whatever the stock.

        It is the line the roll gets ORDERED from, and a 7/8" roll is
        22.2 mm: on a 25 mm carcass it cannot cover the edge, and
        check_edge_banding validates the roll for nobody — its strip-width
        guard runs only on the hardwood stock spec.
        """
        from cabineteer.cutlist import edge_band_lines_for_panels

        for thk, covered in ((18, True), (25, False)):
            cfg = _cfg(side_thickness=thk, top_thickness=thk,
                       bottom_thickness=thk, edge_band_mode="hot_melt",
                       drawer_config=[[724, "drawer"]])
            carcass, _t, _b, faces = _raw_panels_for_cabinet(cfg, None)
            notes = " ".join(ln.notes for ln in
                             edge_band_lines_for_panels(carcass + faces, cfg))
            assert f"{thk:g} mm edges" in notes
            assert ("does NOT cover" in notes) is not covered

    def test_the_rip_width_note_states_the_configured_width(self):
        """N2a — "~20 mm strips" was a literal beside a configurable key."""
        from cabineteer.cutlist import edge_band_lines_for_panels

        cfg = _cfg(edge_band_mode="hardwood", edge_band_thickness_mm=3.2,
                   drawer_config=[[724, "drawer"]])
        carcass, _t, _b, faces = _raw_panels_for_cabinet(cfg, None)
        notes = " ".join(ln.notes for ln in
                         edge_band_lines_for_panels(carcass + faces, cfg))
        # 20 mm rip on an 18 mm edge leaves 2 mm proud — stated, not implied.
        assert "20 mm strips" in notes
        assert "2 mm proud of the 18 mm edges" in notes

    def test_the_assembly_doc_rips_the_same_width_the_bom_orders(self):
        """N2b — the doc said ~20 mm while the BOM said 32 mm strips.

        The step had no object to read from: AssemblyPlan carried the band
        mode and thickness but not the width, so the sentence invented one.
        """
        from cabineteer.cutlist import edge_band_lines_for_panels

        for width in (20.0, 32.0):
            cfg = _cfg(
                edge_band_mode="hardwood", edge_band_thickness_mm=3.2,
                edge_band_stock={"width_mm": 139.7, "length_mm": 1219.2,
                                 "price_usd": 52.0, "strip_width_mm": width},
                drawer_config=[[724, "drawer"]])
            plan = build_assembly_plan(cfg)
            step = next(s for s in plan.steps if "Band the front" in s.title)
            assert f"ripped {width:g} mm wide" in step.body
            carcass, _t, _b, faces = _raw_panels_for_cabinet(cfg, None)
            bom = " ".join(ln.notes for ln in
                           edge_band_lines_for_panels(carcass + faces, cfg))
            assert f"strips of {width:g} mm" in bom

    def test_the_domino_wall_claim_uses_the_thinnest_panel(self):
        """N3 — it read the SIDE thickness and asserted it of every panel.

        The top and bottom carry face mortises too (their divider rows), so
        18 mm sides with a 12 mm top and a 15 mm plunge do not leave a 3 mm
        wall: the cutter exits the far face by 3 mm. The correct pattern was
        already two sentences later in the same paragraph.
        """
        cols = [{"width_mm": 423.0, "openings": [[724, "drawer"]]},
                {"width_mm": 423.0, "openings": [[724, "drawer"]]}]
        uniform = build_assembly_plan(_cfg(side_thickness=18, columns=cols))
        step = next(s for s in uniform.steps if "Domino" in s.title)
        assert "leaves a 3 mm wall" in step.body
        assert "BREAKS THROUGH" not in step.body

        thin = build_assembly_plan(_cfg(side_thickness=18, top_thickness=12,
                                        bottom_thickness=12, columns=cols))
        step = next(s for s in thin.steps if "Domino" in s.title)
        assert "BREAKS THROUGH 3 mm on the 12 mm panels" in step.body

    def test_the_machine_table_says_what_the_step_says(self):
        """Two renderings of one setting had two copies of the arithmetic."""
        cols = [{"width_mm": 423.0, "openings": [[724, "drawer"]]},
                {"width_mm": 423.0, "openings": [[724, "drawer"]]}]
        plan = build_assembly_plan(_cfg(side_thickness=18, top_thickness=12,
                                        bottom_thickness=12, columns=cols))
        step = next(s for s in plan.steps if "Domino" in s.title)
        from cabineteer.assembly import _wall_text
        from cabineteer.joinery import get_domino_size
        wall = _wall_text(plan, get_domino_size(plan.size_key))
        assert wall in step.body


class TestAnchoredEndsAreDerivedToo:
    """The second hardcoded constant, standing where the first had been.

    The first pass at D4 replaced ``face_gap_mm`` with the panel's real
    neighbour gaps — and wrote a literal 0.0 at the ends of every stack,
    on the reasoning that "the face runs to the carcass so there is no
    reveal there". That is true of exactly one build style. It is wrong for
    an inset door (a real 2 mm reveal at both ends) and inverted for a
    furniture_top, whose bottom face hangs 18 mm OVER the bottom panel and
    was described as "0 mm reveal below; bottom edge -18 mm ABOVE the
    bottom panel's top face" — a negative distance in a stated direction,
    which is not a sentence anyone can act on.

    One formula per end now, with the sign carrying the meaning.
    """

    @staticmethod
    def _placements(cfg):
        from cabineteer.cabinet import face_placements
        return face_placements(bays_from_config(cfg, None))

    def test_a_flush_overlay_stack_is_flush_at_both_ends(self):
        cfg = _cfg(drawer_config=[[282.7, "drawer"], [110.3, "drawer"],
                                  [110.3, "drawer"], [220.7, "drawer"]])
        pl = self._placements(cfg)
        assert pl[0].reveal_below == 0.0
        assert pl[-1].reveal_above == 0.0
        assert pl[0].datum == 0.0

    def test_an_inset_door_has_a_real_reveal_at_the_carcass(self):
        cfg = _cfg(width=400, door_hinge="blum_clip_top_110_inset",
                   drawer_config=[[724, "door"]])
        pl, = self._placements(cfg)
        assert pl.reveal_below == 2.0
        assert pl.reveal_above == 2.0
        # And it laps nothing — it sits inside the opening.
        assert pl.left_lap < 0 and pl.right_lap < 0

    def test_a_furniture_top_bottom_face_laps_the_panel(self):
        cfg = _cfg(furniture_top=True,
                   drawer_config=[[282.7, "drawer"], [110.3, "drawer"],
                                  [110.3, "drawer"], [220.7, "drawer"]])
        pl = self._placements(cfg)
        assert pl[0].reveal_below == -cfg.bottom_thickness
        assert pl[0].datum == -cfg.bottom_thickness
        # And the topmost face meets the CAP, not the top panel.
        assert pl[-1].above_member == "top cap"
        assert pl[-1].reveal_above > 0

    @pytest.mark.parametrize("style,expect", [
        ("plain", ["below: flush with the bottom panel",
                   "above: flush with the top panel"]),
        ("furniture_top", ["laps the bottom panel by 18 mm",
                           "BELOW the bottom panel's top face"]),
        ("inset", ["below: 2 mm to the bottom panel", "inset —",
                   "2 mm inside the cabinet side"]),
    ])
    def test_the_row_says_it_in_words_a_person_can_act_on(self, style, expect):
        if style == "inset":
            cfg = _cfg(width=400, door_hinge="blum_clip_top_110_inset",
                       drawer_config=[[724, "door"]])
        else:
            cfg = _cfg(furniture_top=(style == "furniture_top"),
                       drawer_config=[[282.7, "drawer"], [110.3, "drawer"],
                                      [110.3, "drawer"], [220.7, "drawer"]])
        _c, _t, _b, faces = _raw_panels_for_cabinet(cfg, None)
        notes = " ".join(p.notes for p in faces)
        for phrase in expect:
            assert phrase in notes, f"{style}: missing {phrase!r}\n{notes}"
        assert "-" not in notes.replace("—", "").replace("pre-", ""), (
            f"{style}: a negative number reached the paper\n{notes}")

    def test_the_x_offset_loop_has_one_home(self):
        """It was written out three times; a fourth was about to appear."""
        from cabineteer.cabinet import bay_x_offsets
        # Columns plus their dividers must fill the interior: 900 exterior
        # on 18 mm sides is an 864 interior, less two 18 mm dividers = 828.
        cfg = _cfg(width=900, columns=[
            {"width_mm": 300.0, "openings": [[200, "drawer"]]},
            {"width_mm": 228.0, "openings": [[200, "drawer"]]},
            {"width_mm": 300.0, "openings": [[200, "drawer"]]}])
        bays = bays_from_config(cfg, None)
        xs, total = bay_x_offsets(bays)
        assert xs[0] == 0.0
        # Adjacent bays share a divider, so each step is one side thinner.
        assert xs[1] == bays[0].width - bays[0].side_thickness
        # Closure: the run the faces are laid out on IS the cabinet.
        assert total == pytest.approx(cfg.width)


class TestDoorRowsNameTheirOwnHinge:
    """A door's style is the hinge's, and the row asserted one for all three.

    "width set by the hinge overlay" is false for an inset hinge, whose
    overlay is 0.0 — the leaf is sized to the opening less its gaps. And
    "full overlay — 9.5 mm" was printed for a HALF-overlay hinge: the number
    was read from the object and the word beside it was not.
    """

    @staticmethod
    def _note(hinge):
        cfg = _cfg(width=400, door_hinge=hinge, drawer_config=[[724, "door"]])
        _c, _t, _b, faces = _raw_panels_for_cabinet(cfg, None)
        return next(p.notes for p in faces if p.name == "door")

    @pytest.mark.parametrize("hinge,lead,overlay", [
        ("blum_clip_top_blumotion_110_full", "full overlay", 16.0),
        ("blum_clip_top_blumotion_110_half", "half overlay", 9.5),
    ])
    def test_the_lead_matches_the_hinges_overlay_type(self, hinge, lead,
                                                      overlay):
        from cabineteer.hardware import get_hinge
        note = self._note(hinge)
        assert get_hinge(hinge).overlay == overlay
        assert f"{lead} — {overlay:g} mm over the cabinet side" in note
        assert "width set by the hinge overlay" in note

    def test_an_inset_leaf_is_not_described_by_an_overlay_it_has_not_got(self):
        from cabineteer.hardware import get_hinge
        note = self._note("blum_clip_top_110_inset")
        assert get_hinge("blum_clip_top_110_inset").overlay == 0.0
        assert "width set by the hinge overlay" not in note
        assert "sized to the opening less its gaps" in note
        assert "inset — 2 mm inside the cabinet side" in note
        assert "full overlay" not in note


class TestPairsAndScopeFromTheReview:
    """Four defects an adversarial review found in the P4 work itself.

    Every one is the class P4 exists to remove — a number stated against
    something it was never measured from — which is the useful reminder that
    writing the rule down does not exempt the person applying it.
    """

    PAIR = dict(width=900, height=760, depth=560,
                door_hinge="blum_clip_top_blumotion_110_half")

    def _pair_cfg(self, **kw):
        base = dict(self.PAIR)
        base.update(kw)
        return _cfg(**base)

    def test_a_pairs_inner_edge_meets_its_partner_not_the_carcass(self):
        """It read "269.7 mm inside the divider" on ten real rows.

        The lap was measured to the BAY's interior edge, which is right for a
        face that spans the bay and fabricated for one leaf of a pair: that
        edge meets the other leaf, 2 mm away.
        """
        from cabineteer.cabinet import face_placements
        from cabineteer.door import DoorConfig

        cfg = self._pair_cfg(drawer_config=[[724, "door_pair"]])
        pls = [p for p in face_placements(bays_from_config(cfg, None))
               if p.panel.kind == "door"]
        assert len(pls) == 2
        inner = [(p.left_lap, p.left_member) if p.panel.leaf else
                 (p.right_lap, p.right_member) for p in pls]
        for lap, member in inner:
            assert member == "meeting leaf"
            assert lap == pytest.approx(-DoorConfig.gap_between)

    def test_the_row_says_the_gap_a_person_setting_the_pair_needs(self):
        cfg = self._pair_cfg(drawer_config=[[724, "door_pair"]])
        _c, _t, _b, faces = _raw_panels_for_cabinet(cfg, None)
        note = next(p.notes for p in faces if p.name == "door")
        assert "2 mm gap to the meeting leaf" in note
        assert "inside the divider" not in note

    def test_both_leaves_see_the_face_above_not_the_carcass(self):
        """``leaf`` is a HORIZONTAL index; keying the vertical stack by it
        made the right leaf a stack of one, so both its reveals were
        measured to the carcass."""
        from cabineteer.cabinet import face_placements

        cfg = self._pair_cfg(drawer_config=[[400, "door_pair"],
                                            [324, "drawer"]])
        pls = [p for p in face_placements(bays_from_config(cfg, None))
               if p.panel.kind == "door"]
        assert len(pls) == 2
        assert {p.above_member for p in pls} == {"face above"}
        assert len({p.reveal_above for p in pls}) == 1

    def test_mirrored_bays_really_do_read_identically(self):
        """The docstring claimed this before the code did it.

        The overlay was emitted left-then-right, so the two bays produced
        two orderings of one fact and their shared row carried both.
        """
        from cabineteer.cutlist import consolidate_bom

        cols = [{"width_mm": 300.0, "openings": [[200, "drawer"]]},
                {"width_mm": 228.0, "openings": [[200, "drawer"]]},
                {"width_mm": 300.0, "openings": [[200, "drawer"]]}]
        cfg = _cfg(width=900, height=236, columns=cols)
        _c, _t, _b, faces = _raw_panels_for_cabinet(cfg, cols)
        for row in consolidate_bom(faces):
            overlay = [c for c in row.notes.split(";") if "overlay" in c]
            assert len(set(overlay)) <= 1, (
                f"one row, {len(set(overlay))} orderings of its overlay: "
                f"{overlay}")

    def test_a_faces_position_survives_consolidation_as_one_clause(self):
        """Split into three, dedup could keep two "below"s and one "above"."""
        from cabineteer.cutlist import consolidate_bom

        cfg = _cfg(width=400, height=700,
                   drawer_config=[[200, "drawer"], [150, "drawer"],
                                  [202, "drawer"], [112, "drawer"]])
        _c, _t, _b, faces = _raw_panels_for_cabinet(cfg, None)
        for row in consolidate_bom(faces):
            hangs = [c for c in row.notes.split(";") if "hangs" in c]
            assert len(hangs) == row.quantity or len(hangs) == 1
            for clause in hangs:
                assert "below:" in clause and "above:" in clause

    @pytest.mark.parametrize("kw,expect", [
        (dict(top_thickness=12, bottom_thickness=12,
              drawer_config=[[724, "drawer"]]), "no face mortises"),
        (dict(shelf_thickness=12, fixed_shelf_positions=[350.0],
              drawer_config=[[724, "open"]]), "3 mm wall"),
    ])
    def test_the_wall_claim_only_counts_face_mortised_panels(self, kw, expect):
        """It cried wolf on panels that are EDGE-mortised.

        An edge mortise goes into the panel's end and has the whole panel
        behind it, so a thin edge-mortised panel is not a blow-through risk —
        and the doc was telling him to shorten a plunge that was correct.
        """
        cfg = _cfg(width=900, height=760, depth=560, side_thickness=18, **kw)
        step = next(s for s in build_assembly_plan(cfg).steps
                    if "Domino" in s.title)
        assert expect in step.body
        assert "BREAKS THROUGH" not in step.body

    def test_a_real_blow_through_still_stops_the_work(self):
        cols = [{"width_mm": 423.0, "openings": [[724, "drawer"]]},
                {"width_mm": 423.0, "openings": [[724, "drawer"]]}]
        cfg = _cfg(width=900, height=760, depth=560, side_thickness=18,
                   top_thickness=12, bottom_thickness=12, columns=cols)
        step = next(s for s in build_assembly_plan(cfg).steps
                    if "Domino" in s.title)
        assert "BREAKS THROUGH 3 mm on the 12 mm panels" in step.body
