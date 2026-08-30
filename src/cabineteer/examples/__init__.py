#!/usr/bin/env python3
"""
Example: Design a base cabinet with drawers, evaluate it, and generate a cutlist.

This demonstrates the full pipeline without requiring CadQuery installed.
The parametric checks, BOM, and cutlist all work with pure Python.
When CadQuery is available, geometric checks (interference, bounding box) are also run.

Usage:
    python -m cabineteer.examples.base_cabinet_with_drawers
"""

from cabineteer.cabinet import CabinetConfig, build_cabinet
from cabineteer.drawer import DrawerConfig, build_drawer, drawers_from_cabinet_config
from cabineteer.evaluation import (
    evaluate_cabinet,
    check_shelf_deflection,
    print_report,
    Severity,
)
from cabineteer.cutlist import (
    extract_bom,
    consolidate_bom,
    print_bom,
    to_json,
    to_csv,
    SHEET_4x8_3_4,
    SHEET_4x8_1_4,
)

HAS_CADQUERY = False
try:
    import cadquery as cq
    HAS_CADQUERY = True
except ImportError:
    pass


def main():
    print("=" * 70)
    print("PARAMETRIC BASE CABINET WITH DRAWERS")
    print("=" * 70)
    print()

    # ── Define cabinet ───────────────────────────────────────────────────
    cfg = CabinetConfig(
        width=600,
        height=720,
        depth=550,
        side_thickness=18,
        bottom_thickness=18,
        shelf_thickness=18,
        back_thickness=6,
        dado_depth=9,
        back_rabbet_width=9,
        back_rabbet_depth=6,
        fixed_shelf_positions=[],  # no fixed shelves — all drawers
        openings=[
            (150, "drawer"),  # top drawer
            (150, "drawer"),  # middle drawer
            (250, "drawer"),  # bottom (tall) drawer
        ],
        drawer_slide="blum_tandem_550h",
    )

    print(f"Cabinet: {cfg.width}W × {cfg.depth}D × {cfg.height}H mm")
    print(f"Interior: {cfg.interior_width}W × {cfg.interior_depth}D × {cfg.interior_height}H mm")
    print(f"Drawer slide: {cfg.drawer_slide}")
    print()

    # ── Run parametric evaluation ────────────────────────────────────────
    print("Running parametric evaluation...")
    print()

    # Create drawer configs for evaluation
    drawer_configs = []
    for op in cfg.openings:
        opening_height = op.height_mm
        if op.opening_type == "drawer":
            dcfg = DrawerConfig(
                opening_width=cfg.interior_width,
                opening_height=opening_height,
                opening_depth=cfg.interior_depth,
                slide_key=cfg.drawer_slide,
            )
            drawer_configs.append((None, dcfg))  # None = no geometry yet

    issues = evaluate_cabinet(
        cab_cfg=cfg,
        drawer_assemblies=drawer_configs,
    )

    # Also check shelf deflection for bottom panel as a shelf
    # (the bottom panel acts as a shelf in terms of load)
    issues.extend(check_shelf_deflection(
        span=cfg.interior_width,
        depth=cfg.interior_depth,
        thickness=cfg.bottom_thickness,
        load_kg=50,  # estimated load on bottom
        material="baltic_birch",
    ))

    print_report(issues)

    # ── Build geometry if CadQuery is available ──────────────────────────
    all_parts = []

    if HAS_CADQUERY:
        print("CadQuery detected — building 3D geometry...")
        print()

        # Build cabinet
        cabinet_assy, cabinet_parts = build_cabinet(cfg)
        all_parts.extend(cabinet_parts)

        # Build drawers
        drawer_results = drawers_from_cabinet_config(cfg)
        for drawer_assy, drawer_parts, z_pos in drawer_results:
            all_parts.extend(drawer_parts)

            # Add drawer to cabinet assembly
            cabinet_assy.add(
                drawer_assy,
                name=f"drawer_z{z_pos:.0f}",
                loc=cq.Location((
                    cfg.side_thickness + 12.7,  # Blum side clearance
                    2.0,  # front gap
                    z_pos,
                )),
            )

        # Run geometric evaluation
        print("Running geometric interference check...")
        from cabineteer.evaluation import check_interference
        geo_issues = check_interference(cabinet_assy)
        print_report(geo_issues)

        # Export
        print("Exporting assembly...")
        cabinet_assy.save("/tmp/base_cabinet.step")
        print("  → /tmp/base_cabinet.step")
        print()

    else:
        print("CadQuery not installed — skipping 3D geometry.")
        print("Install with: pip install cadquery")
        print()

        # Generate approximate BOM from config without geometry
        print("Generating BOM from parametric config...")
        from cabineteer.cutlist import CutlistPanel

        # Manually create BOM from known dimensions
        all_panels = [
            CutlistPanel(name="left_side", length=cfg.height, width=cfg.depth, thickness=cfg.side_thickness, grain_direction="length", edge_band=["front"]),
            CutlistPanel(name="right_side", length=cfg.height, width=cfg.depth, thickness=cfg.side_thickness, grain_direction="length", edge_band=["front"]),
            CutlistPanel(name="bottom", length=cfg.interior_width + cfg.dado_depth * 2, width=cfg.depth - cfg.back_rabbet_width, thickness=cfg.bottom_thickness, grain_direction="width", edge_band=["front"]),
            CutlistPanel(name="back", length=cfg.back_panel_width, width=cfg.back_panel_height, thickness=cfg.back_thickness, grain_direction="width"),
        ]

        # Add drawer parts
        for i, op in enumerate(cfg.openings):
            opening_height = op.height_mm
            if op.opening_type == "drawer":
                dcfg = DrawerConfig(
                    opening_width=cfg.interior_width,
                    opening_height=opening_height,
                    opening_depth=cfg.interior_depth,
                    slide_key=cfg.drawer_slide,
                )
                # Part lengths come from the JOINT, never from the box's
                # outside dimensions — see joinery.part_lengths. Re-deriving
                # them here is exactly the drift that put double-counted
                # corners on the paper for four months.
                all_panels.extend([
                    CutlistPanel(name=f"drawer_{i}_side_L", length=dcfg.side_panel_length, width=dcfg.box_height, thickness=dcfg.side_thickness, grain_direction="length"),
                    CutlistPanel(name=f"drawer_{i}_side_R", length=dcfg.side_panel_length, width=dcfg.box_height, thickness=dcfg.side_thickness, grain_direction="length"),
                    CutlistPanel(name=f"drawer_{i}_front", length=dcfg.front_back_panel_length, width=dcfg.box_height, thickness=dcfg.front_back_thickness, grain_direction="width"),
                    CutlistPanel(name=f"drawer_{i}_back", length=dcfg.front_back_panel_length, width=dcfg.box_height, thickness=dcfg.front_back_thickness, grain_direction="width"),
                    CutlistPanel(name=f"drawer_{i}_bottom", length=dcfg.bottom_panel_width, width=dcfg.bottom_panel_depth, thickness=dcfg.bottom_thickness, grain_direction="width"),
                ])
                if dcfg.applied_face:
                    all_panels.append(
                        CutlistPanel(name=f"drawer_{i}_face", length=dcfg.face_width, width=dcfg.face_height, thickness=dcfg.face_thickness, grain_direction="width", edge_band=["all"]),
                    )

        # ── Print BOM ────────────────────────────────────────────────────
        print_bom(all_panels)

        # Consolidate
        consolidated = consolidate_bom(all_panels)
        print("Consolidated BOM:")
        print_bom(consolidated)

        # ── Export cutlist ───────────────────────────────────────────────
        json_output = to_json(
            consolidated,
            stock=[SHEET_4x8_3_4, SHEET_4x8_1_4],
            kerf=3.2,
        )
        print("Cutlist JSON:")
        print(json_output)
        print()

        csv_output = to_csv(consolidated)
        print("Cutlist CSV:")
        print(csv_output)


if __name__ == "__main__":
    main()
