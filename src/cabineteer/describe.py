"""Human-readable design summaries for CabinetConfig.

Generates a concise prose description suitable for presenting to the user
during the review step of the design workflow.  The output is deliberately
plain English — dimensions in familiar terms, hardware by name, joinery
method, opening layout — so the user can approve or request changes before
moving to the visualizer.

The module has no CadQuery dependency.
"""

from __future__ import annotations

from .cabinet import CabinetConfig
from .drawer import DrawerConfig, DrawerJoineryStyle
from .hardware import HINGES, PULLS, SLIDES
from .joinery import CarcassJoinery


# ── Formatting helpers ───────────────────────────────────────────────────────

def _mm_to_ft_in(mm: float) -> str:
    """Return a natural imperial rendering for the given millimetre value.

    Examples: 1800 → "5 ft 11 in", 600 → "1 ft 11½ in", 18 → "¾ in".
    """
    total_in = mm / 25.4
    if total_in < 6:
        # Small dimensions → fractional inches to nearest 1/16.
        sixteenths = round(total_in * 16)
        whole = sixteenths // 16
        frac  = sixteenths % 16
        frac_str = {
            0:  "", 1: "¹⁄₁₆", 2: "⅛", 3: "³⁄₁₆", 4: "¼", 5: "⁵⁄₁₆",
            6: "⅜", 7: "⁷⁄₁₆", 8: "½", 9: "⁹⁄₁₆", 10: "⅝", 11: "¹¹⁄₁₆",
            12: "¾", 13: "¹³⁄₁₆", 14: "⅞", 15: "¹⁵⁄₁₆",
        }[frac]
        if whole and frac_str:
            return f"{whole}{frac_str} in"
        if whole:
            return f"{whole} in"
        return f"{frac_str or '0'} in"

    # Larger dimensions → feet + inches to the nearest ½ in.  Round to
    # half-inches *first*, then carry into feet with divmod, so a value like
    # 600 mm (23.62 in) never renders as "1 ft 12 in".
    half_inches = round(total_in * 2)
    feet, rem_half = divmod(half_inches, 24)   # 24 half-inches = 12 in = 1 ft
    inches_whole, inches_half = divmod(rem_half, 2)
    frac = "½" if inches_half else ""
    if inches_whole == 0 and not frac:
        return f"{feet} ft"
    if feet == 0:
        return f"{inches_whole}{frac} in"
    return f"{feet} ft {inches_whole}{frac} in"


def _fmt_dim(mm: float) -> str:
    """Combined metric + imperial formatting, e.g. '1800 mm (5 ft 11 in)'."""
    return f"{mm:.0f} mm ({_mm_to_ft_in(mm)})"


def _capture_name(cfg) -> str:
    """Plain-English adjective for how the back is held, or "" for the
    let-in pocket (the default, which needs no explaining)."""
    return {
        "rabbet":   "rabbeted-in ",
        "half_lap": "half-lapped ",
        "dado":     "grooved-in ",
    }.get(getattr(cfg, "back_capture", "pocket"), "")


def _joinery_name(j: CarcassJoinery) -> str:
    return {
        CarcassJoinery.DADO_RABBET:    "dado-and-rabbet",
        CarcassJoinery.FLOATING_TENON: "floating-tenon (Domino)",
        CarcassJoinery.POCKET_SCREW:   "pocket-screw",
        CarcassJoinery.BISCUIT:        "biscuit",
        CarcassJoinery.DOWEL:          "dowel",
    }.get(j, j.value)


def _drawer_joinery_name(j: DrawerJoineryStyle) -> str:
    return {
        DrawerJoineryStyle.BUTT:         "butt joint",
        DrawerJoineryStyle.QQQ:          "lock-rabbet (QQQ)",
        DrawerJoineryStyle.HALF_LAP:     "half-lap",
        DrawerJoineryStyle.DRAWER_LOCK:  "drawer-lock joint",
    }.get(j, j.value)


def _default_drawer_joinery() -> DrawerJoineryStyle:
    """Return the current default DrawerJoineryStyle from DrawerConfig."""
    return DrawerConfig.__dataclass_fields__["joinery_style"].default


def _slot_phrase(count: int, label: str) -> str:
    """Pluralize a slot label.  2, 'drawer' → 'two drawers'."""
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
             6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
    n = words.get(count, str(count))
    if count == 1:
        return f"{n} {label}"
    # Simple pluralization — works for the vocabulary we use.
    plural = label + ("s" if not label.endswith("s") else "")
    if label == "door_pair":
        plural = "door pairs"
    return f"{n} {plural}"


# ── Public API ───────────────────────────────────────────────────────────────

def describe_design(cfg: CabinetConfig) -> dict:
    """Return a structured description + a prose summary of a CabinetConfig.

    The return value includes both a human-readable ``prose`` string (for
    dropping straight into a chat message) and the structured bits that
    generated it (``dimensions``, ``openings``, ``hardware``, ``materials``)
    so a client can rearrange or re-render the information if desired.
    """
    # ── 1. Dimensions ─────────────────────────────────────────────────────────
    dimensions = {
        "exterior": {
            "width_mm":  cfg.width,
            "height_mm": cfg.height,
            "depth_mm":  cfg.depth,
            "pretty":    f"{_fmt_dim(cfg.width)} wide × "
                         f"{_fmt_dim(cfg.height)} tall × "
                         f"{_fmt_dim(cfg.depth)} deep",
        },
        "interior": {
            "width_mm":  cfg.interior_width,
            "height_mm": cfg.interior_height,
            "depth_mm":  cfg.interior_depth,
        },
    }

    # ── 2. Opening layout ────────────────────────────────────────────────────
    # Multi-column cabinets carry their openings on each ColumnConfig; single-
    # column cabinets use the top-level ``openings`` stack.  ``ops`` is the
    # flattened list of every opening across all columns — hardware selection
    # and pull gating below key off it, so a multi-column cabinet is no longer
    # mistaken for an empty carcass.
    is_multi_column = bool(cfg.columns)
    if is_multi_column:
        column_stacks = [list(col.openings) for col in cfg.columns]
    else:
        column_stacks = [list(cfg.openings)]
    ops = [op for stack in column_stacks for op in stack]

    counts: dict[str, int] = {}
    for op in ops:
        counts[op.opening_type] = counts.get(op.opening_type, 0) + 1
    stack_total = sum(op.height_mm for op in ops)

    def _stack_dict(stack: list) -> list[dict]:
        return [
            {"height_mm": float(op.height_mm), "type": op.opening_type}
            for op in stack
        ]

    def _stack_fills(stack: list) -> bool:
        # Tolerance covers the intentional reveal auto_fix leaves (it targets
        # interior − _FILL_EPSILON_MM = 1 mm, then floors slots to whole mm, so a
        # repaired stack sits ~1–2 mm short by design). A genuine under-fill
        # (open top compartment) is far larger, so this still flags real cases.
        #
        # The openings share the interior with any door floors, exactly as
        # the columns share the interior width with their dividers.
        from .cabinet import DOOR_TYPES
        n_floors = sum(1 for i, op in enumerate(stack)
                       if i > 0 and op.opening_type in DOOR_TYPES)
        available = cfg.interior_height - n_floors * cfg.shelf_thickness
        return sum(op.height_mm for op in stack) >= available - 2.5

    openings: dict = {
        "total_stack_height_mm":  stack_total,
        "interior_height_mm":     cfg.interior_height,
        "counts": counts,
    }
    if is_multi_column:
        openings["columns"] = [
            {
                "width_mm": col.width_mm,
                "stack_from_bottom": _stack_dict(list(col.openings)),
                "stack_fills_interior": _stack_fills(list(col.openings)),
            }
            for col in cfg.columns
        ]
        # A multi-column layout is "filled" only if every column stack is.
        openings["stack_fills_interior"] = all(
            _stack_fills(stack) for stack in column_stacks
        )
    else:
        openings["stack_from_bottom"] = _stack_dict(ops)
        openings["stack_fills_interior"] = _stack_fills(ops)

    def _describe_stack(stack: list) -> str:
        """Prose for one bottom-to-top opening stack."""
        stack_counts: dict[str, int] = {}
        for op in stack:
            stack_counts[op.opening_type] = stack_counts.get(op.opening_type, 0) + 1
        parts = [_slot_phrase(stack_counts[k], k) for k in sorted(stack_counts)]
        layout_phrase = ", ".join(parts[:-1])
        if layout_phrase:
            layout_phrase = f"{layout_phrase} and {parts[-1]}"
        else:
            layout_phrase = parts[-1]
        return (
            f"{layout_phrase} stacked from bottom to top — "
            + ", ".join(f"{int(op.height_mm)} mm {op.opening_type}" for op in stack)
        )

    if not ops:
        stack_desc = "an open carcass with no fixed openings"
    elif is_multi_column:
        col_descs = [
            f"column {i + 1} ({col.width_mm:.0f} mm wide): {_describe_stack(list(col.openings))}"
            for i, col in enumerate(cfg.columns)
            if col.openings
        ]
        stack_desc = f"{len(cfg.columns)} columns — " + "; ".join(col_descs)
    else:
        stack_desc = _describe_stack(ops)

    # ── 3. Hardware ──────────────────────────────────────────────────────────
    slide = SLIDES.get(cfg.drawer_slide)
    hinge = HINGES.get(cfg.door_hinge)
    has_drawers = any(op.opening_type == "drawer" for op in ops)
    has_doors   = any(op.opening_type in ("door", "door_pair") for op in ops)

    hardware: dict = {}
    hardware_phrases: list[str] = []
    if slide and has_drawers:
        hardware["drawer_slide"] = {
            "key":          cfg.drawer_slide,
            "name":         slide.name,
            "max_load_kg":  slide.max_load_kg,
        }
        soft = " (soft-close)" if "soft" in slide.name.lower() or "blumotion" in slide.name.lower() else ""
        hardware_phrases.append(f"{slide.name}{soft} drawer slides rated to {slide.max_load_kg:.0f} kg")
    if hinge and has_doors:
        hardware["door_hinge"] = {
            "key":          cfg.door_hinge,
            "name":         hinge.name,
            "overlay_type": hinge.overlay_type.value,
            "soft_close":   hinge.soft_close,
        }
        soft = " soft-close" if hinge.soft_close else ""
        overlay = hinge.overlay_type.value.replace("_", "-")
        hardware_phrases.append(f"{hinge.name}{soft} hinges ({overlay} overlay)")

    pull_selection_required = False
    drawer_pull_spec = PULLS.get(cfg.drawer_pull or "")
    door_pull_spec   = PULLS.get(cfg.door_pull or "")
    if drawer_pull_spec and has_drawers:
        hardware["drawer_pull"] = {
            "key":  cfg.drawer_pull,
            "name": drawer_pull_spec.name,
        }
        hardware_phrases.append(f"{drawer_pull_spec.name} drawer pulls")
    elif has_drawers and not drawer_pull_spec:
        # No pull, OR a pull key that doesn't resolve to a catalog spec —
        # either way the caller must pick one before visualizing.
        pull_selection_required = True
    if door_pull_spec and has_doors:
        hardware["door_pull"] = {
            "key":  cfg.door_pull,
            "name": door_pull_spec.name,
        }
        # Announce the door pull whenever it differs from the drawer pull
        # (a shared pull is already covered by the "drawer pulls" phrase).
        if door_pull_spec is not drawer_pull_spec:
            hardware_phrases.append(f"{door_pull_spec.name} door pulls")
    elif has_doors and not door_pull_spec:
        pull_selection_required = True

    # ── 4. Materials + joinery ───────────────────────────────────────────────
    drawer_joinery = getattr(cfg, "drawer_joinery", _default_drawer_joinery())
    materials = {
        "carcass_joinery":        cfg.carcass_joinery.value,
        "drawer_box_joinery":     drawer_joinery.value,
        "back_capture":           getattr(cfg, "back_capture", "pocket"),
        # Two tokens that change what the saw does and were reported by no
        # document a person reads: the corner style (which the 3D does not
        # even draw) and the banding mode (which shrinks every carcass core).
        "carcass_corner_style":   getattr(cfg, "carcass_corner_style", "butt"),
        "edge_band_mode":         getattr(cfg, "edge_band_mode", "none"),
        "side_thickness_mm":      cfg.side_thickness,
        "back_thickness_mm":      cfg.back_thickness,
        "shelf_thickness_mm":     cfg.shelf_thickness,
        "drawer_box_thickness_mm": getattr(cfg, "drawer_box_thickness", 15.0),
        "drawer_box_prefinished": getattr(cfg, "drawer_box_prefinished", False),
        "adj_shelf_holes":        cfg.adj_shelf_holes,
    }
    material_phrase = (
        f"{_mm_to_ft_in(cfg.side_thickness)} carcass panels with a "
        f"{_mm_to_ft_in(cfg.back_thickness)} {_capture_name(cfg)}back, "
        f"{_joinery_name(cfg.carcass_joinery)} carcass joinery"
    )
    if has_drawers:
        material_phrase += f", {_drawer_joinery_name(drawer_joinery)} drawer-box corners"
        if getattr(cfg, "drawer_box_prefinished", False):
            material_phrase += " in pre-finished Baltic birch"
    if cfg.adj_shelf_holes:
        material_phrase += ", 32-mm adjustable shelf-pin holes"
    if cfg.fixed_shelf_positions:
        n = len(cfg.fixed_shelf_positions)
        material_phrase += f", {n} fixed shelf{'es' if n > 1 else ''}"
    if getattr(cfg, "carcass_corner_style", "butt") == "miter":
        # The render does not model this; say it in the document that gets
        # read aloud, not only in the cutlist notes.
        material_phrase += (
            ", 45\u00b0 waterfall miters at the four exterior corners "
            f"(top and bottom cut to the full {cfg.width:g} mm long-point, "
            "beveled both ends)")
    _band = getattr(cfg, "edge_band_mode", "none")
    if _band != "none":
        _bt = float(getattr(cfg, "edge_band_thickness_mm", 0.6))
        material_phrase += (
            f", {_bt:g} mm "
            + ("hardwood edge banding on the front edges \u2014 carcass "
               "panels are cut that much narrower so the finished depth "
               "holds" if _band == "hardwood"
               else "hot-melt edge banding, ironed on after cutting (it "
                    "adds to the finished size, so no panel shrinks)"))

    # ── 5. Prose summary ─────────────────────────────────────────────────────
    lines = [
        f"Cabinet: {dimensions['exterior']['pretty']}.",
        f"Layout: {stack_desc}.",
    ]
    if hardware_phrases:
        lines.append("Hardware: " + "; ".join(hardware_phrases) + ".")
    lines.append("Construction: " + material_phrase + ".")
    # Show-face style — name the tokens in plain English (project
    # convention: describe_design explains every token, like the
    # "grooved-in back").
    if getattr(cfg, "furniture_top", False):
        lines.append(
            "Furniture-top style: a front cap strip closes the top panel's "
            "front edge flush with the drawer faces, and the lowest face "
            "drops to the carcass underside."
        )
    _gap = getattr(cfg, "face_gap_mm", 4.0)
    if _gap != 4.0:
        lines.append(
            f"Faces are cut for a {_gap:g} mm reveal between fronts "
            f"(face_gap_mm)."
        )

    if not openings["stack_fills_interior"] and ops:
        if is_multi_column:
            bad = [
                f"column {i + 1} ({sum(op.height_mm for op in stack) - cfg.interior_height:+.0f} mm)"
                for i, stack in enumerate(column_stacks)
                if stack and not _stack_fills(stack)
            ]
            lines.append(
                f"⚠ Opening stack does not fill interior in {', '.join(bad)}. "
                f"Run auto_fix_cabinet or adjust the column stacks before "
                f"visualizing."
            )
        else:
            delta = stack_total - cfg.interior_height
            lines.append(
                f"⚠ Opening stack does not fill interior "
                f"(off by {delta:+.0f} mm). Run auto_fix_cabinet or adjust "
                f"drawer_config before visualizing."
            )
    if pull_selection_required:
        lines.append(
            "⚠ No pull hardware selected. Ask the user to choose a pull style "
            "(call list_pull_presets) before calling visualize_cabinet."
        )

    prose = " ".join(lines)

    return {
        "prose":                   prose,
        "dimensions":              dimensions,
        "openings":                openings,
        "hardware":                hardware,
        "materials":               materials,
        "pull_selection_required": pull_selection_required,
    }
