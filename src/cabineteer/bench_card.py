"""Bench-card generator — re-cutting show faces from rough donor pieces.

A bench card is a different document from the main cutlist: it does not
plan a purchase of fresh sheet stock, it plans a RE-CUT of specific show
faces (drawer fronts / doors) out of specific rough boards already at the
bench (``DonorPiece``). Every dimension is derived FRESH from
``cabinet.face_layout`` and checked, before it is ever printed, against
the live face stack via ``cabinet.assert_face_heights_close`` — the whole
point of this module is that a wrong ``furniture_top``/``face_gap``/
overhang assumption raises instead of producing a bad card.

Grain convention note (deliberate divergence): ``server.py``'s general
cutlist path (``_raw_panels_for_cabinet``) always sets a false front's
``length`` to its WIDTH (horizontal grain, locked, no choice exposed).
This module introduces a per-row grain POLICY
(``GRAIN_LOCKED_VERTICAL``/``GRAIN_FREE_ROTATION``) that the general tool
never had. The two are allowed to disagree — this is a bench-card-only
concern, not a fix to the general convention, and nobody should "align"
one to the other.

Pure Python: this module never touches the filesystem. ``pdf_bytes`` is
returned in memory; the MCP tool handler in ``server.py`` is the only
place that writes to disk.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .cabinet import (
    CabinetConfig,
    FaceHeightClaim,
    DEFAULT_HEIGHT_TOLERANCE_MM,
    assert_face_heights_close,
    bays_from_config,
    face_layout,
    face_row_bands,
)
from .cutlist import (
    CutlistPanel,
    Placement,
    SheetStock,
    _REPORTLAB_AVAILABLE,
    _inch_frac,
    _panel_colour,
    _panel_colour_dark,
    _paper_size,
    _parts_table,
    _thickness_imperial,
    assign_part_ids,
    optimize_cutlist,
)

if _REPORTLAB_AVAILABLE:  # same install, so this always succeeds when the base does
    from reportlab.graphics.shapes import Drawing, Rect, String, Line, Group
    from .cutlist import (
        _getSampleStyleSheet, _guillotine_cuts, _HexColor, _Paragraph,
        _ParagraphStyle, _rl_mm, _SimpleDocTemplate, _Spacer,
    )


GRAIN_LOCKED_VERTICAL = "locked_vertical"   # -> CutlistPanel.grain_direction="length"
GRAIN_FREE_ROTATION = "free_rotation"       # -> CutlistPanel.grain_direction=""
_GRAIN_POLICIES = (GRAIN_LOCKED_VERTICAL, GRAIN_FREE_ROTATION)


@dataclass(frozen=True)
class DonorPiece:
    """One physical rough-cut piece already in hand — not fresh sheet stock.

    Distinct from ``cutlist.SheetStock``, which models an unlimited supply
    of a stock size to be BOUGHT. A DonorPiece is a specific board Charlie
    is standing at the saw with; ``generate_bench_card`` calls
    ``optimize_cutlist`` ONCE PER DonorPiece, and faces that don't fit any
    donor piece are reported as unassigned rather than silently rolled
    onto imaginary fresh stock.
    """
    id: str                      # short label on the diagram/table, e.g. "E1"
    name: str                    # human description, e.g. "kapex_center-side"
    length_mm: float             # as measured, along THIS PIECE's own grain
    width_mm: float               # as measured, across this piece's own grain
    thickness_mm: float
    material: str = "baltic_birch"     # must match a face row's material to be eligible
    grain_along: str = "length"        # "length" | "width" — which of this
                                         # piece's own dimensions its grain runs
                                         # along. A rough offcut is not always
                                         # oriented the way a fresh sheet is
                                         # assumed to be; normalized internally
                                         # (see as_stock()) so the packer's own
                                         # "SheetStock.length = ungrained axis
                                         # a locked panel must align to" holds.
    notes: str = ""               # provenance/condition, printed on the card

    def as_stock(self) -> "SheetStock":
        """This piece wrapped as a one-off SheetStock, oriented so
        ``length`` is always the piece's OWN grain axis — the axis a
        ``grain_direction="length"`` (locked) CutlistPanel is pinned to."""
        l, w = (self.length_mm, self.width_mm) if self.grain_along == "length" \
            else (self.width_mm, self.length_mm)
        return SheetStock(name=self.name, length=l, width=w,
                           thickness=self.thickness_mm, quantity=1,
                           material=self.material)


@dataclass
class BenchCardAssignment:
    """One face cut from one donor piece — a row on the bench card."""
    donor_id: str
    placement: "Placement"     # x, y, placed_length/width, rotated, part_id
    panel: "CutlistPanel"      # the face row (name, dims, material, part_id)


@dataclass
class BenchCardResult:
    project_name: str
    cabinet_names: list
    donor_results: list           # [(DonorPiece, OptimizationResult), ...]
    assignments: list              # [BenchCardAssignment, ...]
    unassigned: list                # [CutlistPanel, ...] — fit no donor piece
    pdf_bytes: bytes
    warnings: list = field(default_factory=list)


def generate_bench_card(
    project,                                 # CabinetProject, already resolved
    cabinet_names: list,
    donor_pieces: list,
    *,
    grain_policy: str = GRAIN_LOCKED_VERTICAL,
    grain_overrides: Optional[dict] = None,
    face_kinds: tuple = ("drawer_face", "door"),
    face_gap: Optional[float] = None,
    furniture_top: Optional[bool] = None,
    face_bottom_overhang: Optional[float] = None,
    face_top_overhang: Optional[float] = None,
    face_height_overrides: "Optional[dict[str, list[list]]]" = None,
    closure_tolerance_mm: float = DEFAULT_HEIGHT_TOLERANCE_MM,
    kerf: float = 3.2,
    algorithm: str = "auto",
    paper: str = "letter",
    title: str = "Bench Card",
) -> BenchCardResult:
    """Plan a re-cut of specific show faces onto specific donor pieces.

    ``face_height_overrides`` is keyed by cabinet name to a list INDEXED
    BY BAY (``face_height_overrides[cname][bay_idx]``), each entry itself
    a per-row list of ``FaceHeightClaim | None`` (``None`` keeps the
    computed default for that row). Keying by cabinet name alone — one
    row list broadcast positionally to every bay — would collide an
    override meant for one column of a multi-column cabinet with an
    unrelated column's identically-indexed rows; a missing bay entry (or
    a cabinet not present at all) just means "no overrides for that bay",
    same as an empty list.

    Raises ``ValueError`` (before building anything) on an unknown grain
    policy or cabinet name, and — deep inside, via
    ``assert_face_heights_close`` — on any row whose height claim (default
    or overridden) does not close into the live face stack. That last
    raise is the load-bearing one: it means no bench card is ever produced
    from a mismatched ``furniture_top``/``face_gap``/overhang assumption.
    """
    if grain_policy not in _GRAIN_POLICIES:
        raise ValueError(
            f"grain_policy must be one of {_GRAIN_POLICIES}, got {grain_policy!r}.")

    resolved = dict(project.resolved())
    known = set(resolved)
    unknown = [c for c in cabinet_names if c not in known]
    if unknown:
        raise ValueError(
            f"Unknown cabinet name(s) {unknown} — project has {sorted(known)}.")

    grain_overrides = grain_overrides or {}
    face_height_overrides = face_height_overrides or {}

    all_faces: list[CutlistPanel] = []
    for cname in cabinet_names:
        cfg: CabinetConfig = resolved[cname]
        bays = bays_from_config(cfg)
        panels = face_layout(
            bays, face_gap=face_gap, furniture_top=furniture_top,
            face_bottom_overhang=face_bottom_overhang,
            face_top_overhang=face_top_overhang,
        )
        faces = [p for p in panels if p.kind in face_kinds]
        overrides_for_cab = face_height_overrides.get(cname) or []

        for bay_idx in range(len(bays)):
            bay_faces = [p for p in faces if p.bay == bay_idx]
            if not bay_faces:
                continue
            bands = face_row_bands(bay_faces)

            overrides_for_bay = (
                overrides_for_cab[bay_idx]
                if bay_idx < len(overrides_for_cab) else None) or []
            claims: list[FaceHeightClaim] = []
            for i, (z0, z1) in enumerate(bands):
                real_h = round(z1 - z0, 3)
                override = overrides_for_bay[i] if i < len(overrides_for_bay) else None
                claims.append(override if override is not None else
                              FaceHeightClaim(f"{cname} bay{bay_idx} row{i}", real_h))

            # The literal answer to "how does the P1 check get called
            # internally": unconditional, every bay of every cabinet,
            # before a single CutlistPanel is built. Cheap when nothing is
            # overridden; raises straight through to the MCP handler when
            # something is wrong, with nothing written to disk.
            assert_face_heights_close(
                bays, claims, bay_index=bay_idx,
                face_gap=face_gap, furniture_top=furniture_top,
                face_bottom_overhang=face_bottom_overhang,
                face_top_overhang=face_top_overhang,
                face_kinds=face_kinds, tolerance_mm=closure_tolerance_mm,
            )

            band_index = {band: i for i, band in enumerate(bands)}
            for p in sorted(bay_faces, key=lambda q: (q.z, q.x, q.leaf)):
                i = band_index[(round(p.z, 3), round(p.z + p.height, 3))]
                claim = claims[i]
                row_height = claim.height_mm

                key = f"{cname}:{p.bay}:{p.slot}:{p.leaf}"
                policy = grain_overrides.get(key, grain_policy)
                grain_dir = "length" if policy == GRAIN_LOCKED_VERTICAL else ""

                note = (f"row {i} closes at {row_height:g} mm against the "
                        "live face stack (cabinet.assert_face_heights_close)")
                if claim.measure_to_remainder:
                    note += " — measure-to-remainder, may run long"

                all_faces.append(CutlistPanel(
                    name="door" if p.kind == "door" else "false_front",
                    length=row_height, width=p.width, thickness=p.thickness,
                    quantity=1, grain_direction=grain_dir,
                    material=cfg.face_material, source=cname, notes=note,
                ))

    if not all_faces:
        raise ValueError(
            f"No {face_kinds} panels found across cabinets {cabinet_names}.")

    source_letters = assign_part_ids(all_faces)

    remaining = list(all_faces)
    donor_results: list = []
    assignments: list[BenchCardAssignment] = []
    warnings: list[str] = []

    for donor in donor_pieces:
        stock = donor.as_stock()
        pool = [f for f in remaining
                if f.material == donor.material
                and abs(f.thickness - donor.thickness_mm) < 0.01]
        result = optimize_cutlist(pool, stock_sheet=stock, kerf=kerf,
                                   algorithm=algorithm)
        by_part_id = {f.part_id: f for f in pool}
        # optimize_cutlist treats stock_sheet as a BUYABLE size — unlimited
        # copies, packed onto however many ``sheet_index`` values it takes
        # (that's the right model for the general cutlist tool, which is
        # planning a purchase). A DonorPiece is the opposite: one specific
        # physical board already in hand. ``as_stock()`` sets quantity=1
        # precisely to say "there is exactly one of this", but nothing in
        # the packer enforces that cap — it will happily invent a phantom
        # second copy at sheet_index=1 for whatever didn't fit on the
        # first. Anything past the physical supply is not a real
        # assignment: leave it in ``remaining`` so it shows up as
        # unassigned (or gets picked up by a later donor) instead of being
        # silently reported as cut.
        for pl in result.placements:
            if pl.sheet_index >= stock.quantity:
                continue
            origin = by_part_id.get(pl.part_id)
            if origin is None:
                continue
            assignments.append(BenchCardAssignment(
                donor_id=donor.id, placement=pl, panel=origin))
            remaining.remove(origin)
        if result.grain_mismatched:
            warnings.append(
                f"{donor.id} ({donor.name}): grain-locked part(s) "
                f"{', '.join(result.grain_mismatched)} had to rotate to "
                "fit — cross-grain on the finished face.")
        donor_results.append((donor, result))

    unassigned = remaining

    pdf_bytes = _render_bench_card_pdf(
        project_name=project.name, cabinet_names=list(cabinet_names),
        all_faces=all_faces, source_letters=source_letters,
        donor_results=donor_results, assignments=assignments,
        unassigned=unassigned, grain_policy=grain_policy,
        face_gap=face_gap, furniture_top=furniture_top,
        closure_tolerance_mm=closure_tolerance_mm,
        paper=paper, title=title,
    )

    return BenchCardResult(
        project_name=project.name, cabinet_names=list(cabinet_names),
        donor_results=donor_results, assignments=assignments,
        unassigned=unassigned, pdf_bytes=pdf_bytes, warnings=warnings,
    )


# ─── PDF rendering ──────────────────────────────────────────────────────────


def _donor_piece_drawing(piece: "DonorPiece",
                          piece_assignments: list,
                          content_width: float,
                          preset_cuts: "list | None" = None,
                          max_height: "float | None" = None) -> "Drawing":
    """One donor board's rectangle, its placed parts, and cut lines.

    A deliberately simpler sibling of ``cutlist._SheetDrawingFlowable``: a
    bench card's diagram is a handful of parts on one board, not a 4×8
    sheet nesting, so a plain ``reportlab.graphics.shapes.Drawing`` (itself
    a Flowable) needs no custom canvas class. Still fits BOTH axes within
    the page, the way ``_SheetDrawingFlowable.draw()`` does (``scale =
    min(width/sl, height/sw)``) — scaling on length alone left a donor
    piece oriented wide-relative-to-long producing a Drawing many times
    taller than the printable page.

    ``preset_cuts`` is ``OptimizationResult.cuts`` for this donor's sheet
    (same tuple shape as ``_guillotine_cuts`` produces) — when the
    optimizer declared a cut plan (``rips_first`` bundles strips into one
    wide track-saw rip + table-saw splits), draw THAT instead of
    re-deriving lines from geometry, which cannot tell a bundled strip
    from several thin rips when stacks align.
    """
    stock = piece.as_stock()
    scale = content_width / stock.length
    if max_height is not None and stock.width > 0:
        scale = min(scale, max_height / stock.width)
    dw, dh = stock.length * scale, stock.width * scale
    d = Drawing(dw, dh)
    d.add(Rect(0, 0, dw, dh, fillColor=_HexColor("#F5EED8"),
                strokeColor=_HexColor("#888888")))

    for a in piece_assignments:
        pl = a.placement
        fc = _panel_colour(a.panel.name)
        x, y = pl.x * scale, pl.y * scale
        pw, ph = pl.placed_length * scale, pl.placed_width * scale
        d.add(Rect(x, y, pw, ph, fillColor=_HexColor(fc),
                    strokeColor=_HexColor(_panel_colour_dark(fc))))
        label = f"{a.panel.part_id} {a.panel.name}"
        if pl.rotated:
            label += " ↺"
        dims = f"{pl.placed_length:.0f}×{pl.placed_width:.0f} mm"
        grp = Group(String(0, 4, label, fontSize=9, textAnchor="middle"),
                    String(0, -8, dims, fontSize=7, textAnchor="middle"))
        grp.translate(x + pw / 2, y + ph / 2)
        if ph > pw:               # tall piece — rotate the label 90°
            grp.rotate(90)
        d.add(grp)

    if preset_cuts is not None:
        cuts: list = list(preset_cuts)
    else:
        cuts = []
        _guillotine_cuts([a.placement for a in piece_assignments], 0, 0,
                          stock.length, stock.width, depth=0, out=cuts)
    for depth, pos, orient, x0, y0, x1, y1, *_rest in sorted(cuts):
        if orient == "v":
            d.add(Line(x0 * scale, y0 * scale, x0 * scale, y1 * scale,
                        strokeColor=_HexColor("#c0392b"), strokeDashArray=[3, 2]))
        else:
            d.add(Line(x0 * scale, y0 * scale, x1 * scale, y0 * scale,
                        strokeColor=_HexColor("#c0392b"), strokeDashArray=[3, 2]))
    return d


def _render_bench_card_pdf(
    *, project_name: str, cabinet_names: list, all_faces: list,
    source_letters: dict, donor_results: list, assignments: list,
    unassigned: list, grain_policy: str, face_gap, furniture_top,
    closure_tolerance_mm: float, paper: str, title: str,
) -> bytes:
    if not _REPORTLAB_AVAILABLE:
        raise ImportError(
            "reportlab is required for PDF export. "
            "Install with: uv pip install reportlab"
        )
    import dataclasses
    import io
    from xml.sax.saxutils import escape as _esc

    PAGE = _paper_size(paper)
    MARGIN = 14 * _rl_mm
    CW = PAGE[0] - 2 * MARGIN
    MAX_DRAWING_H = PAGE[1] - 2 * MARGIN

    styles = _getSampleStyleSheet()
    title_sty = _ParagraphStyle("bct", parent=styles["Title"], fontSize=18,
                                leading=22, spaceAfter=2 * _rl_mm)
    h2 = _ParagraphStyle("bch2", parent=styles["Heading2"], fontSize=12.5,
                         leading=15, spaceBefore=4 * _rl_mm,
                         spaceAfter=1.5 * _rl_mm)
    norm = _ParagraphStyle("bcn", parent=styles["Normal"], fontSize=10,
                           leading=13, spaceAfter=1 * _rl_mm)
    small = _ParagraphStyle("bcs", parent=styles["Normal"], fontSize=9,
                            leading=12, textColor=_HexColor("#555555"),
                            spaceAfter=1 * _rl_mm)

    # Rotated / grain-mismatched pieces get their notes folded in, and every
    # part (placed or not) gets a "From" label — the reason this document
    # can be read on its own at the saw without cross-referencing. Built as
    # a side table (part_id -> extra note) rather than mutated onto the
    # shared CutlistPanel objects: those are the same instances referenced
    # by BenchCardResult.assignments/all_faces, and a caller re-rendering
    # (or just inspecting the result afterwards) shouldn't see this
    # function's annotation appended a second time — or at all.
    donor_id_by_part: dict = {}
    rotated_cross_grain: set = set()
    for a in assignments:
        donor_id_by_part[a.panel.part_id] = a.donor_id
        if a.placement.rotated and a.panel.grain_direction not in ("", None):
            rotated_cross_grain.add(a.panel.part_id)
    for f in unassigned:
        donor_id_by_part[f.part_id] = "— unassigned —"

    annotated_faces = [
        dataclasses.replace(
            f, notes=(f.notes + " — " if f.notes else "") + "↺ rotated — cross-grain")
        if f.part_id in rotated_cross_grain else f
        for f in all_faces
    ]

    story = [_Paragraph(f"{_esc(project_name)} — {_esc(title)}", title_sty)]
    subtitle = (
        f"Cabinets: {', '.join(cabinet_names)} · generated "
        f"{date.today().isoformat()} · grain policy: {grain_policy} · "
        f"height-closure tolerance: {closure_tolerance_mm:g} mm"
    )
    story.append(_Paragraph(_esc(subtitle), norm))
    assumptions = (
        f"Computed with furniture_top={furniture_top!r}, "
        f"face_gap={face_gap!r} — every height on this card was checked "
        "against cabinet.face_layout under exactly these assumptions "
        "before being printed."
    )
    story.append(_Paragraph(_esc(assumptions), small))
    story.append(_Spacer(1, 2 * _rl_mm))

    story.append(_Paragraph("Parts", h2))
    story.append(_parts_table(annotated_faces, CW, source_letters=source_letters,
                              from_map=donor_id_by_part))

    for donor, result in donor_results:
        piece_assignments = [a for a in assignments if a.donor_id == donor.id]
        story.append(_Paragraph(f"{_esc(donor.id)} — {_esc(donor.name)}", h2))
        dims = (f"{donor.length_mm:g} × {donor.width_mm:g} mm "
                f"({_inch_frac(donor.length_mm)} × {_inch_frac(donor.width_mm)} in) "
                f"× {_thickness_imperial(donor.thickness_mm)} "
                f"{donor.material.replace('_', ' ')}")
        story.append(_Paragraph(_esc(dims), norm))
        if donor.notes:
            story.append(_Paragraph(_esc(donor.notes), small))
        if piece_assignments:
            preset_cuts = (result.cuts or {}).get(0)
            story.append(_donor_piece_drawing(
                donor, piece_assignments, CW,
                preset_cuts=preset_cuts, max_height=MAX_DRAWING_H))
            legend = ", ".join(
                f"{a.panel.part_id} {a.panel.name}" for a in piece_assignments)
            story.append(_Paragraph(_esc(f"On this piece: {legend}"), small))
        else:
            story.append(_Paragraph("Nothing assigned to this piece.", small))
        story.append(_Spacer(1, 2 * _rl_mm))

    if unassigned:
        story.append(_Paragraph("Unassigned — need fresh stock or another "
                                "donor piece for:", h2))
        for f in unassigned:
            story.append(_Paragraph(
                _esc(f"{f.part_id} {f.name} — {f.length:.0f} × {f.width:.0f} "
                     f"× {f.thickness:g} mm, {f.material}"), norm))

    buf = io.BytesIO()
    doc = _SimpleDocTemplate(
        buf, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
        title=f"Bench Card — {project_name}")
    doc.build(story)
    return buf.getvalue()
