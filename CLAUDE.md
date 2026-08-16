# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Full install — CadQuery, opcut, rectpack, and dev tools (recommended)
uv pip install -e ".[full,dev]"

# Lite install — pure-Python only: parametric checks, cutlist BOM, MCP, evals
uv pip install -e .

# Or just sync everything via uv (default-groups = full + dev, so this is equivalent to full)
uv sync

# Run the MCP server (stdio, for Claude Desktop / Gemini CLI)
uv run cabineteer

# Lite mode — skips CadQuery, opcut, and rectpack
uv run --no-group full cabineteer

# Run the MCP server (HTTP/SSE, port 3749 auto-incrementing)
uv run cabineteer --http
uv run cabineteer --http --port 4200

# Run tests
uv run pytest tests/ -v

# Run a single test file
uv run pytest tests/test_evaluation.py -v

# Run a single test by name
uv run pytest tests/test_evaluation.py -v -k "test_valid_drawer"

# Run evals (full suite)
uv run python -m evals

# Run evals with filters
uv run python -m evals --tag kitchen
uv run python -m evals --tag drawer --tag door
uv run python -m evals --difficulty advanced
uv run python -m evals --name overflow_drawer_stack
uv run python -m evals --json          # machine-readable
uv run python -m evals --list          # print scenario catalogue
```

## Architecture

The package lives in `src/cabineteer/`. All units are millimetres. The data-flow is:

```
hardware.py + joinery.py
        │
        ▼
cabinet.py / drawer.py / door.py   ← parametric dataclasses (no CadQuery required)
        │
        ▼
project.py      ← multi-cabinet projects, shared-design merge, persistence (no CadQuery required)
        │
        ▼
evaluation.py   ← returns typed Issue objects (no CadQuery required)
        │
        ▼
cutlist.py      ← BOM, guillotine optimiser, JSON/CSV export (no CadQuery required)
        │
        ▼
server.py       ← MCP server (30 tools, stdio or HTTP/SSE)
```

`evals/` (harness + scenarios) imports server handler functions directly — no MCP transport involved — so the full eval suite runs in under 1 second.

### Key design patterns

- **Dataclasses everywhere; specs are frozen, configs are not.** `OpeningConfig`, `ColumnConfig`, hardware specs, joinery specs, and the project dataclasses are `@dataclass(frozen=True)`. The three user-facing configs (`CabinetConfig`, `DrawerConfig`, `DoorConfig`) are plain `@dataclass` — `CabinetConfig.__post_init__` normalizes `openings`/`columns` in place. Computed properties (e.g. `interior_width`, `box_height`) are `@property`.
- **CadQuery is optional.** The `try: import cadquery` pattern is used throughout. `evaluation.py` and `cutlist.py` have CadQuery-backed and pure-Python code paths. The pure-Python paths run in all environments and are what the tests and evals exercise.
- **MCP tool handlers are plain async functions** (e.g. `_tool_design_cabinet`) that return `list[types.TextContent]`. The evals harness calls these directly via `TOOL_DISPATCH`, bypassing the MCP transport layer entirely.
- **opcut item IDs must be globally unique.** opcut 0.1.3 uses item IDs as a set for placement tracking; duplicate IDs cause `Exception('result is done')` mid-solve. `_optimize_with_opcut` assigns IDs via a global counter (`name__0`, `name__1`, …) to avoid collisions when multiple `CutlistPanel` objects share the same name.

### Module responsibilities

| Module | Responsibility |
|---|---|
| `hardware.py` | Frozen specs for Blum/Accuride/Salice drawer slides and Blum Clip Top hinges; `HingeSpec.hinges_for_height()` and `hinge_positions()` implement manufacturer placement rules; **hinge part numbers are REAL Blum SKUs** (corrected 2026-07-28 at order time — the old 71H/71N scheme was invented): 71T=plain / 71B=BLUMOTION, 35/36/37xx = full/half/inset, ..90 = INSERTA (tool-free cup, no screws) / ..50 = screw-on (2× 606N cup screws), 170° = 71T6550; every Clip Top carries `mounting_plate_part="173L8100"` (CLIP 0mm wing plate, $0.91, pre-mounted 5 mm Euro system screws — pilot 5 mm at 37 mm from front edge) and the hinge BOM emits one `hinge_accessory` plate line per hinge + a `fastener` line for screw-on cups |
| `joinery.py` | `DrawerJoinerySpec.from_stock()` computes all cut dimensions; `DominoSpec`, `PocketScrewSpec`, `BiscuitSpec`, `DowelSpec` each provide `count_for_span()` and `positions_for_span()`; `carcass_domino_size_for_thickness()` — carcass tenon rule shared by the hardware BOM and assembly instructions: 5×30 (Festool 494938, 300-pack, $25) for stock ≤ 19 mm, 8×40 (493298, 780-pack) above |
| `assembly.py` | `build_assembly_plan(cfg)` → `AssemblyPlan` (pure Python): carcass joint census mirroring the BOM (top/bottom ×4 + 2/divider + 2/fixed shelf), mortise centres from `DominoSpec.positions_for_span(interior_depth)` measured from the FRONT edge on both mating parts, DF 500 setup block, per-panel mortise maps (red = face, blue = edge mortises); **thickness-direction registration (2026-08-02, Charlie's catch): every slot sits `DF500_BASE_HEIGHT_MM` (10) from a marked per-panel REFERENCE face (top/bottom outside, shelf underside, divider left) — fence set to 10 to match the DF 500's fixed base height (= 0-offset Domiplate), NOT t/2 (t/2 edge slots vs base-registered face slots mismatch by 1 mm in 18 mm and a tight joint won't close; pre-2026-08 printed docs carry the bad number); face rows batten ON the reference line (cutter lands 10 past it); stock < `BASE_REF_MIN_THICKNESS_MM` (15) falls back to centred t/2 + batten clamped (10−t/2) SHORT; map row centres are at ref+10 (divider rows = left face + 10, so +1 mm vs old centreline in 18 mm)**, consumables incl. PETG dry-fit tenon print count (printables.com model 689403 by paulengel), and an ordered step list where a full no-glue dry fit always precedes glue-up; **v2 awareness**: mitered corners turn the four corner joints `kind="miter"` (purple rows in the maps, side rows move to the beveled ends, top/bottom maps go long-point wide with divider centrelines shifted by side_t), the machine table gains fence-45° rows with the solved placement from `miter_mortise_placement`, a dedicated miter-mortise step (dial in on scrap) precedes the dry fit, and glue-up switches to tape-hinge + band clamps; edge banding inserts mode-correct steps — hardwood banding BEFORE mortising (banded front edge becomes the reference) and hot-melt ironing AFTER cure; `generate_assembly_html()` / `generate_assembly_pdf()` render it (PDF reuses cutlist's guarded reportlab imports; portrait US Letter, ~10 pt body — landscape 7.5 pt was unreadable at the bench, 2026-08-02) |
| `cabinet.py` | `CabinetConfig` with `drawer_config` list of `(height_mm, opening_type)` tuples — each row may carry an optional third element, a per-opening options dict (`bottom_thickness`, `slide_key`, `pull_key`, `hinge_key`, `hinge_side`, `num_doors`, `door_thickness`), normalised by `to_opening`; `drawer_box_thickness` (default 15 mm) sets box side/front-back stock and `drawer_box_prefinished` (default False; True on workshop presets) switches boxes+bottoms to pre-finished Baltic birch in the cutlist — both are also `SharedDesign` tokens; `face_material` (default `finished_wood`, also a token) sets the cutlist material for show faces — applied false fronts AND door leaves; `carcass_material` (default `baltic_birch`, also a token) does the same for sides/top/bottom/shelves/dividers (backs + drawer boxes keep their own stock); sheet materials (BB stocks or any name ending `_ply`) pack per (material, thickness) with a `price_missing` flag when PRICE_LIST has no `sheet_<mat>_<t>mm` entry, while other strings (solid stock, `finished_wood`) stay a labeled order-out group; `carcass_joinery` field selects method; **mitered corners** (`carcass_corner_style` butt/miter, SharedDesign token): miter puts 45° waterfall miters on the four exterior corners — top/bottom cut to FULL exterior width **long-point** with bevel notes, sides keep their numbers but note beveled ends, divider/shelf joints stay butt tenons; `joinery.miter_mortise_placement(size, t)` solves the Domino seat in the 45° face (plunge eats toward the OUTSIDE/show face at cos 45° — 5×30 @ 15 mm needs ≥ 16.5 mm stock, placement biases toward the HEEL, ≥2 mm `show_face_wall`; the pre-2026-07-29 solver had this mirrored and would exit the show face) and `evaluation.check_miter_corners` errors on non-tenon joinery, unequal mating thicknesses, or infeasible stock; **back panel cap** (`back_style` full_height/under_top, SharedDesign token — Charlie's ask 2026-08-02, "back invisible from top or sides"): `under_top` cuts the TOP panel to FULL depth (rear edge flush with the sides) and stops the back at its underside (height − top_t) so no back edge shows from above — the back slides into its pocket from the carcass's bottom end during glue-up; butt corners + non-dado joinery only (`evaluation.check_back_style` errors otherwise); NOTE the 3D viewer's continuous top has ALWAYS drawn under_top geometry, so `full_height` renders (the legacy cutlist default, kept for the mid-cut kids' builds) show a capped back the paper doesn't deliver; **back capture** (`back_capture` pocket/rabbet/half_lap/dado + `back_groove_setback`, both SharedDesign tokens — Charlie's ask 2026-08-15, 6 mm ply backs deflect on long pieces): how the back is HELD, an axis independent of `carcass_joinery` (corners) and `back_style` (top edge) — before this the rabbeted back was welded to `DADO_RABBET`, so a Domino carcass was silently forced into the let-in pocket. `cabinet.back_capture_geometry(cfg) -> BackCapture` is the SINGLE SOURCE (cutlist, 3D makers, evaluator, assembly doc all read it; never re-derive). `pocket` = legacy let-in, byte-identical output, the only capture that seats OUTSIDE the top/bottom and so the only one `back_style` affects; `rabbet`/`half_lap`/`dado` seat the back INSIDE the perimeter (cut oversize by `back_rabbet_depth` on all four edges, top AND bottom both run full depth, `back_style` inert). `rabbet` cuts the back's full thickness at the rear edge; `half_lap` cuts half in the case and half in the back's own FRONT-face perimeter rabbet (interlocks fore-and-aft, two glue planes, needs ≥ `HALF_LAP_MIN_BACK_MM` 9 mm); `dado` is a groove held `back_groove_setback` (12) in from the rear — the back is CAPTIVE (slides in at glue-up, never after) and an unglued solid back can float and move. `interior_depth` is capture-aware but clamped to never exceed the legacy allowance (drawer boxes size off it — a capture must not deepen boxes already in the shop). `evaluation.check_back_capture` errors on unknown values, dado_rabbet joinery, mitered corners (a groove exits through the 45° end), a wall under `MIN_CAPTURE_WALL_MM` (6), a half lap under 9 mm, and too little meat behind a groove. The SIDE cut is STOPPED (bottom panel's top face → top panel's underside, engagement past each): butt corners seat top/bottom BETWEEN the sides, so a side's end grain is part of the finished top/bottom surface and a through cut exits there as an open notch — the top/bottom cuts DO run through (their ends butt into the sides). Panel notes carry the machining and the stop, `assembly._back_capture_step` adds a router step, `design_cabinet`/`design_multi_column_cabinet` report a `back_seat` block (engagement, case_cut, setback, clear_depth, captive) and identical panel dims, and `describe_design` names the capture in plain English ("grooved-in back"); **edge banding** (`edge_band_mode` none/hot_melt/hardwood + `edge_band_thickness_mm` + `edge_band_material`, all SharedDesign tokens): fronts of carcass panels + all four edges of faces/doors are the banded set (`CutlistPanel.edge_band` markers are authoritative) — hardwood mode shrinks panel CORES by band thickness per banded edge so finished dims/reveals hold (core notes on rows), hot-melt leaves dims alone; `cutlist.edge_band_lines_for_panels()` emits the BOM line (hot-melt: 7/8"×50' pre-glued rolls, `edgeband-hotmelt-white_oak` $14.80 / `white_birch` $15.50, veneersupplies Jul 2026; hardwood: unpriced rip-from-stock footage — or, with the `edge_band_stock` token `{width_mm, length_mm, price_usd, strip_width_mm=20}`, a line priced in boards-to-order via `pack_band_strips` FFD packing of real per-edge piece lengths (10 mm proud allowance; board thickness IS `edge_band_thickness_mm`; `HardwareLine.unit_price_usd` spec-price override survives consolidation; notes flag pieces at ≈ full strip length — no flush-trim overhang — and pieces LONGER than the stock; project cutlists aggregate ONE band line per material per project; a hardwood run with a stock spec also emits `<name>_banding_cutlist.html/.csv/.pdf` (PDF guarded like the layout PDF) — the doc LEADS with rip width + kerf assumption and a qty-at-each-length **length schedule** (`band_length_schedule`) plus **corner-treatment notes** (`_band_corner_notes`: miter carcass fronts meet in a 45° seam trimmed flush to the beveled panel ends; butt carcasses run side bands through; face perimeters band SHORT edges first, LONG edges overlap their end grain — Charlie's ask, the strip detail alone was hard to follow), with the board→strip→piece chop plan as an appendix via `pack_band_pieces`/`band_pieces_for_panels`/`generate_banding_cutlist_html`/`to_banding_csv`/`generate_banding_cutlist_pdf`, boards numbered `#1..#N` (never `B1` — that's a part-ID family), part IDs matching the layout drawings, dead-length pieces cut AT finished size, over-length pieces listed separately; `_cutlist_pipeline(band_cfg=)` wires it into both cutlist tools using the run's first hardwood-with-stock cfg), `evaluation.check_edge_banding` validates mode/thickness/order-out faces + the stock spec (1/8"/1/4" thickness, 3–5.5" width envelope, strip covers thickest edge, longest banded edge vs strip length), `check_edge_band_face_gap` errors/warns when hot-melt growth (applied AFTER cutting, no core compensation) closes the `DEFAULT_FACE_GAP_MM` 4 mm between stacked faces, and `check_door_overlay_collisions` adds hot-melt growth to the door-edge + neighbour-face claims (hardwood is core-compensated and dimension-neutral everywhere); `build_multi_bay_cabinet` accepts `furniture_top=True` for "furniture top, flush bottom" overlay style |
| `drawer.py` | `DrawerConfig` computes box dimensions from opening + slide clearances; `joinery_style` applies corner joints; `bottom_thickness=None` resolves by size — boxes > 127 mm (5") tall **and** ≥ 406.4 mm (16") wide default to 12 mm (1/2") bottoms, else 6 mm (constants `HEAVY_BOTTOM_*`); explicit thin bottoms on qualifying boxes draw a `drawer_bottom_thickness` warning from the evaluator; `add_manga_stack` (constants `MANGA_*`: 112.5×176×15 mm tankōbon, max stack 5) adds the viewer's scale-reference pile to a drawer box via `build_drawer(include_manga=True)` — raises `ValueError` when the interior can't hold the full stack, wrapped as `bay{i}_drawer{j}: …` by `build_multi_bay_cabinet` and `cabinet '<name>': …` by `visualize_project`; manga are viewer props, never PartInfo/BOM rows |
| `door.py` | `DoorConfig` for single doors and matched pairs; full/half/inset overlay; hinge cup borings via CadQuery |
| `project.py` | `CabinetProject` bundles multiple `CabinetConfig`s with a `SharedDesign` token block; child `overrides` win back over shared tokens; JSON persistence under `~/.cabineteer/projects/` (names validated as filename stems); `list_saved_projects(query=, include_all=, sort=)` catalogues/filters the store — newest-first by default, dev-artifact names (`eval_`/`test_`/`smoke_`/`_`) hidden unless `include_all` or a query; `rename_project()`/`delete_project()`/`duplicate_project()` manage snapshots (surfaced via `list_projects`/`load_project`/`rename_project`/`delete_project`/`duplicate_project` tools) — duplicates stamp `forked_from`/`forked_at` lineage that survives round-trips and shows in listings; `apply_project_patch()`/`update_saved_project()` power the `update_project` delta-edit tool (shallow merge; null clears a key; per-cabinet config keys colliding with an active shared token are auto-pinned as overrides; cabinet rename/add/remove; `worktop` patches shallow-merge, null removes the slab); `WorktopSpec` models a desk/counter slab spanning part of the run (top-level snapshot key, ignored by older readers; `surface_height_mm` is measured from the FLOOR — feet included; `leg_count` 4 = corners, 2 = front-only, 0 = none) — rendered by `visualize_project` (slab + `worktop_leg{i}` nodes; legs stay metal in the finish JS via `HARDWARE_RE`) and emitted as one finished-stock panel in project cutlists; the `design_project` tool refuses an existing name unless `overwrite=true`; `generate_project_cutlist` accepts `project_names` to batch several saved projects into one cutlist — panels carry a `source` project tag (part of the consolidation key, so identical cross-project panels stay distinct rows while sheet packing pools everything), layouts colour by project (Okabe–Ito `_PROJECT_PALETTE`) with a legend, CSV/parts tables gain a Project column, and hardware lines carry `source_counts`; `check_project_consistency()` cross-cabinet warnings |
| `evaluation.py` | `evaluate_cabinet(cfg) -> list[Issue]`; `Issue` has `severity`, `value`, `limit`; `check_door_overlay_collisions` guards hinge overlay + neighbour-face claim vs the shared divider/side budget (uses `cabinet.INNER_FACE_OVERLAY_MM` = the build's inner_overlay default, 8 mm; error on overlap, warning under `MIN_FACE_REVEAL_MM` 2 mm — full-overlay doors beside drawer columns need half-overlay hinges); CadQuery path adds interference checks |
| `cutlist.py` | `consolidate_bom()` (merges by name + dims), `optimize_cutlist(algorithm=)` — opcut FORWARD_GREEDY (primary), rectpack GuillotineBssfSas (optional, `algorithm="rectpack"`), strip-cutting (pure-Python fallback); `rips_first` (opt-in, NOT in auto): Charlie's shop-sequence 3-stage guillotine — track-saw rips into strips, cross-cuts per strip, table-saw secondary rips capped at `RIPS_FIRST_FENCE_LIMIT_MM` (508 = his 20" fence), best-fit strips-of-columns with dual-orientation placement, narrow classes bundle into strips ≥ `RIPS_FIRST_MIN_STRIP_MM` (150 — no thin track rips; the table saw splits segments to final width), and the optimizer DECLARES its cut plan via `OptimizationResult.cuts` so renderers show strip rips as the numbered breakdown cuts instead of re-deriving from geometry (aligned stacks would read as thin rips); `assign_part_ids()` stamps `A-DB1`-style part IDs (project letter by first appearance — dropped on solo runs — + family code + row seq) that flow to CSV, PDF/HTML tables, `panels_summary`, and graphics labels; tables show bold metric + fractional imperial to 1/32 via `_inch_frac` (graphics stay metric-only); multi-project batches colour by the pastel Okabe–Ito `_PROJECT_PALETTE` with per-sheet project keys and global sheet numbers #1..N (HTML+PDF iterate identically); `generate_sheet_layout_html()` produces a self-contained HTML file with per-sheet SVG layouts, numbered breakdown cut lines with dimensions, and rotated part labels; `generate_sheet_layout_pdf()` produces a landscape PDF (US Letter default — Charlie has no A4 paper; `paper="a4"` option on all three PDF-emitting tools via `_paper_size`) with sheet drawings, parts list, and guillotine cut sequence tables; **bench parts format (Charlie's pick 2026-08-02)**: `_parts_table` renders per part a bold-metric row + grey imperial sub-row + spanning note row (edge-band markers fold into notes; batch mode gets 'Project X — name' section rows, NOT a Project column) — shared by the layout PDF and `generate_parts_list_pdf` (standalone portrait cut-parts doc; `_cutlist_pipeline` writes `<name>_parts.pdf` always + `<project>_parts.pdf` per source on batches) — free-text table cells are reportlab `Paragraph`s (plain strings never wrap and overprint neighbouring columns), panel labels are `stringWidth`-fitted/ellipsis-truncated to their panel, and cut-dimension labels sit on white chips clear of the marker circles; `to_json()`, `to_csv()` |
| `server.py` | Thirty MCP tools; `main()` entry point; `generate_assembly_instructions` (project-level carcass assembly docs → `~/.cabineteer/assembly/<project>/`, identical cabinets collapse ×N, part IDs matched to the project cutlist); `_raw_panels_for_cabinet` cuts column dividers to interior height for butt-joint carcasses (floating tenon / pocket screw / biscuit / dowel — Charlie, 2026-07-26); dado/rabbet keeps the full-height divider; `--http` flag switches stdio → HTTP/SSE; port auto-increments from 3749; `_cutlist_pipeline()` is the shared post-panel pipeline (per-thickness sheet optimisation, pricing, file output) behind both `generate_cutlist` and `generate_project_cutlist`; both tools take `sheet_size_overrides` ({material: [L, W]} — only the named stock's sheets resize; Charlie's rift oak measures 2453×1234 while his Baltic birch is nominal 2440×1220); and `optimizer_overrides` ({key: algorithm}, keys `material@thickness` > `material` > `@thickness`, non-default — Charlie's mix: run-level `rips_first` + `{"@6": "opcut"}` keeps the heterogeneous 6mm groups on opcut at zero sheet cost; each sheet_goods row reports its `algorithm`); drawer-box parts and backs/bottoms pool by (material, thickness) so same-stock panels share sheets while pre-finished never mixes with raw |

### Eval harness

Scenarios live in `evals/scenarios.py`. Each `Scenario` has a natural-language `prompt`, a list of `ToolCall`s with `Assertion`s, and tags/difficulty for filtering. Available assertion operators: `EQ`, `APPROX`, `GT`, `GTE`, `LT`, `LTE`, `IN`, `CONTAINS`, `HAS_KEY`, `LEN_EQ`, `LEN_GTE`, `IS_TRUE`, `IS_FALSE`, `NO_ERRORS`, `HAS_ERROR`, `HAS_WARNING`.

Baseline: 307 scenarios / 1168 assertions / 100% pass rate; the pytest suite is 1650 passed / 6 skipped (2026-08-15, post back_capture). Run the eval suite after any non-trivial change.

## Known issues

### Geometry / evaluation bugs
- ~~**`cabinet.py` shelf pin holes wrong workplane**~~ — fixed: `make_side_panel` now uses "YZ" workplane (normal = X) so cylinders bore horizontally into the interior face. `x_start` computes correctly for both mirror/non-mirror panels.
- ~~**Shelf pin hole x-position** is identical for both panels~~ — fixed: left panel uses `side_thickness - shelf_pin_depth`, right uses `0`.
- ~~**`evaluation.py`** emits a duplicate drawer height error~~ — fixed: `check_drawer_carcass_clearances` now only flags the degenerate `box_height ≤ 0` case; the `min_drawer_height` check lives exclusively in `check_drawer_hardware_clearances`.
- ~~**Drawer dado / corner joinery on outside face**~~ — fixed: `make_drawer_side(cfg, side="left"|"right")` and `make_drawer_front_back(cfg, position="front"|"back")` now place the bottom dado and corner joinery on the inside face of each panel; `apply_drawer_joinery_to_side`/`_to_front_back` accept the same parameter. Verified by `tests/test_drawer_orientation.py` (10 intersect-volume probes, all four panels × BUTT and HALF_LAP).
- ~~**Drawer corner joinery (QQQ / HALF_LAP / DRAWER_LOCK) does not engage**~~ — fixed: introduced `DrawerJoinerySpec.engagement_x` (= `side_dado_depth_x` for non-BUTT, 0 for BUTT). HALF_LAP and DRAWER_LOCK use a uniform inner-face rabbet `engagement_x` deep in X and full `front_back_thickness` deep in Y on the side; sub-front / back is widened by `2 × engagement_x` and seats into that rabbet edge-to-edge. **QQQ now models Phipps' hidden tongue-in-pocket joint** (2026-05-03): the side panel keeps a full-thickness **lip** at each end (panel-local Y `0…t_s/2`) that wraps the corner from outside, and the dado pocket on the inner face is **set in by t_s/2** from the end (panel-local X `t_s/2…t_s`, Y `t_s/2…t_s`). `apply_drawer_joinery_to_front_back` cuts an outer-face rabbet on the sub-front / back at each end (X `0…t_s/2`, Y `0…t_fb − t_s/2` for sub-front; mirrored for back), leaving an **inside-face tongue** that protrudes into the side's pocket. Result: the joint is invisible from outside the box (the side lip occupies world X `0…t_s` at the very front, with no front-piece material visible there), and the QQQ vs HALF_LAP corner exterior reads differently — the seam falls at world X = `t_s` (QQQ) vs world X = `t_s/2` (HALF_LAP). DRAWER_LOCK's L-step is still spec-only (BOM, not 3D). `tests/test_drawer_assembly.py` verifies bbox, side clearance, wall interference, bottom-dado engagement, bottom containment, joint engagement across all four styles × three opening sizes; `TestQQQGeometry` adds 21 QQQ-specific cases (lip intact / dado-pocket removed × front+back ends, outer rabbet on sub-front removed, tongue fills side pocket, outside corner owned by side alone).

### Cutlist / BOM gaps
- ~~**`cutlist.extract_bom_parametric`** silent empty-list bug~~ — already correct in current code; fallback path returns one placeholder panel per input part when CadQuery is unavailable.
- ~~**`generate_cutlist` gaps**~~ — fixed: `columns` parameter added; dividers, drawer box parts (sides/front/back at `drawer_box_thickness`, dado-captured bottoms), applied false fronts, and **door leaves** (one `door` panel row per leaf, DoorConfig dims, `face_material` stock — added 2026-07-22; before that doors were silently absent from every cutlist) are now included. BOM grouped by material/thickness with uncut sheet counts. Files written to `~/.cabineteer/cutlists/`.
- ~~**`consolidate_bom` merges differently-named identical panels**~~ — fixed: `name` is now part of the consolidation key, so "top" and "bottom" panels with the same dimensions stay distinct.

### Visualizer bugs
- ~~**Viewer rendered dado-era geometry for every carcass joinery**~~ — fixed (2026-08-02, Charlie's audit ask after the back_style work): butt-joint carcasses (floating tenon / pocket screw / biscuit / dowel) now render plain-slab sides (no dados/rabbets) with interior panels seated BETWEEN them at exact cutlist dims — bottoms/shelves interior-width × (depth − back_t), top depth and back height following `back_style` (full_height shows the back's top edge in the top plane, as the paper builds it), shared dividers at interior height on the bottom panel. `cabinet._is_butt()` gates every panel maker + `build_cabinet`/`build_multi_bay_cabinet` placement; DADO_RABBET keeps the housed legacy geometry (locked by `tests/test_viewer_geometry.py::TestDadoLegacyLocked`). Verified by bbox-probe tests matching `_raw_panels_for_cabinet` output (9 tests).
- **Known gap: dado_rabbet cutlist has no dado/rabbet allowances** — `_raw_panels_for_cabinet` emits butt-style dims (interior width, depth − back_t) even for DADO_RABBET construction, whose real panels need +2×dado_depth width and rabbet-based depths (the 3D model shows the correct housed panels). Charlie never uses dado_rabbet; flagged 2026-08-02 during the viewer audit, unfixed.
- ~~**`visualize_cabinet` pulls not rendered**~~ — fixed: `build_multi_bay_cabinet` now adds a `bay{i}_pull{j}_{k}` mesh for each pull placement; `visualize_cabinet` now forwards `drawer_pull` into per-column bay configs for multi-column layouts; the viewer tracks `bay{i}_pull{j}_{k}` nodes and animates them alongside the face when "O" is pressed.
- ~~**`visualize_cabinet` "O" shortcut** (open drawers) does not work~~ — fixed. Two root causes:
  1. **Wrong traversal depth for `pair.box`**: Three.js r165 GLTFLoader wraps multi-primitive GLTF meshes in an extra Group node, making the ancestry `leaf_mesh → _part_N Group → panel_part Group → panel_name Group → bay{i}_drawer{j} Group`. The old code only searched 3 levels (depths 0–2); `bay{i}_drawer{j}` sits at depth 3. Fixed by adding `p3 = p2?.parent` and extending `searchNames`/`searchNodes` to 4 entries.
  2. **Unanchored face/drawer regex set `pair.face` to a leaf mesh**: The match regex `/^bay(\d+)_(face|drawer)(\d+)/` had no `$` anchor, so leaf mesh names like `bay0_face0_part_0` matched at `si=0`, storing the individual mesh primitive as `pair.face` instead of the `bay0_face0` group node. Only the last-processed primitive was moved on open/close. Fixed by adding `$` to the regex: `/^bay(\d+)_(face|drawer)(\d+)$/`.
- ~~**Pulls don't hide in X-ray mode**~~ — fixed: added `pullMeshes` array; pull mesh objects are pushed there during traversal alongside `pair.pulls`; `toggleXray` now iterates `[...drawerFronts, ...pullMeshes]`.
- ~~**V-key diag colors: left unit only, no drawer faces, no carcass top/bottom**~~ — fixed. Three causes: (1) the drawer/carcass group regexes lacked the GLTFLoader dedup-suffix tolerance (`bay0_drawer0_1`), so second-and-later cabinets were skipped; (2) carcass `top`/`bottom` are *siblings* of `bay_0` (children of the cabinet node), so the `^bay_\d+$` group gate structurally excluded them — carcass panels are now matched by name with no group requirement (drawer-box members are claimed by the drawer branch first, keeping the duplicate `bottom`/`back` names apart); (3) faces had no diag entry — drawer faces and doors are now purple. Diag materials also null their texture `map` so V shows flat vivid colors even over a wood finish.
- ~~**`visualize_project`: only the leftmost cabinet's drawers open (O key)**~~ — fixed. Two root causes: (1) every cabinet reuses the same node names (`bay0_face0`, `bay0_drawer0`, …), and Three.js GLTFLoader dedupes the repeats to `bay0_face0_1` etc., which failed the `$`-anchored match regexes — all bay/pull/door regexes now accept an optional `_\d+` dedup suffix; (2) pair keys were `"{bay}_{slot}"` and collided across cabinets — keys are now prefixed with the matched group's `parent.uuid` (face, box, and pull groups are siblings under the same cabinet node, so they share a prefix). The pull regex is additionally `$`-anchored to the group node so its parent uuid matches the face's; `pair.pulls` stores the group (deduped) while `pullMeshes` keeps leaf meshes for the X-ray toggle.

### Viewer keyboard shortcuts

| Key | Action |
|-----|--------|
| `X` | X-ray drawer and door fronts (transparent overlay) |
| `O` | Cycle drawer open state: closed → partial → fully open → closed (slides box + face + pulls together). Partial is a vertical gradient by drawer height — bottom drawer 100% out, top drawer 20%, linear between — so it reads as an intentional bottom-heavy cascade |
| `C` | Toggle clip plane (axis buttons + slider + mm readout) |
| `V` | Toggle diagnostic colors: drawer sides → pink, drawer front/back → yellow, drawer bottom → green, carcass sides → blue, carcass top/bottom → orange, drawer faces / doors → purple |
| `M` | Cycle the manga scale-reference stack per drawer (1…5 volumes, then hidden). Only present when the render was made with `manga=true`; the legend row and side-panel button hide themselves otherwise |

### Viewer wood finishes

The viewer side panel has live controls: a finish dropdown ("Flat colors" + all
presets — the full `WOOD_FINISHES` catalogue is embedded in the HTML), a
grain-direction toggle, and a "Generate cutlist" button that opens a modal with
a copyable request (seeded via the `cutlist_prompt` parameter; the viewer is a
standalone file and cannot invoke MCP tools itself). The `finish` /
`grain_direction` tool parameters set the *initial* dropdown/toggle state only.
Switching finishes force-disables the X-ray and diag-color toggles and
refreshes their material caches; keyboard shortcuts ignore events from form
controls.

`visualize_cabinet` and `visualize_project` accept an optional `finish` parameter
(`rift_white_oak`, `flat_sawn_white_oak`, `maple`, `walnut` (European),
`black_walnut`, `bamboo`, `baltic_birch`, `cherry`) applied to the carcass,
drawer faces, and doors. Drawer-box meshes (any node under a `bay{i}_drawer{j}`
ancestor) take the separate `drawer_box_finish` parameter, which **defaults to
`baltic_birch`** whenever `finish` is set (drawer boxes are almost always Baltic
birch ply regardless of show wood; pass the same key as `finish` for a uniform
look). `grain_direction` (`vertical` default | `horizontal`) orients the
show-surface grain; **drawer boxes are always horizontal** (box sides are cut
with grain along their length). Preset parameters live in
`visualize.WOOD_FINISHES`; the viewer generates a deterministic procedural grain
texture on a canvas at load time and box-projects UVs per panel (the GLB meshes
carry none). `flat_sawn_white_oak` uses `pattern: "cathedral"` (stacked
parabolic arch figure over the straight background); `bamboo` uses `fleck_size`
for its wide node knuckles. Pull hardware (any node matching `/pull/i` in its
ancestry) keeps its metal material. Materials are **cloned per mesh** before
texturing — box and carcass panels can share GLTF material instances, and
mutating a shared one would leak one finish into the other. Omitting `finish`
keeps the original flat vertex-colour rendering. The grain JS lives in
`visualize._FINISH_JS` as a plain (non-f-string) constant so its braces need no
doubling.

### Viewer GLTF node hierarchy (Three.js r165)

Three.js GLTFLoader wraps each multi-primitive GLTF mesh in an extra `Group`, adding one level beyond the GLTF JSON hierarchy. Confirmed ancestry (depth 0 = leaf `THREE.Mesh`):

```
bay0_face0_part_N  (THREE.Mesh, si=0)
  bay0_face0_part  (Group,      si=1)
    bay0_face0     (Object3D,   si=2)  ← pair.face set here (regex $ match)
      multi_bay_cabinet

bay0_drawer0/side_L_part_N  (THREE.Mesh, si=0)
  side_L_part                (Group,     si=1)
    side_L                   (Object3D,  si=2)
      bay0_drawer0           (Object3D,  si=3)  ← pair.box set here (depth-4 search)
        multi_bay_cabinet
```

Three.js deduplicates repeated node names across the scene by appending `_1`, `_2`, … (e.g. `back` → `back_1` for the second drawer's back panel, since the carcass already has a `back` node). The V-key diagnostic color logic strips this suffix with `name.replace(/_\d+$/, '')` before the PANEL_DIAG_COLS lookup.

## Vertical overlay styles

`build_multi_bay_cabinet` supports two named parameters for controlling how the
top and bottom of the carcass relate to the drawer faces:

| Parameter | Effect |
|---|---|
| `face_bottom_overhang` | How far the lowest drawer face drops below the top surface of the bottom panel. Default 0 (face starts at top of bottom panel). Set to `bottom_thickness` for flush-to-carcass-exterior. |
| `face_top_overhang` | How far the highest drawer face rises above the underside of the top panel. Default 0. Set to `top_thickness` for flush-to-carcass-exterior. |
| `furniture_top` | Shorthand for "furniture top, flush bottom": automatically sets `face_bottom_overhang = bottom_thickness` so the lowest face drops to the carcass underside, and adds a `top_front_cap` strip that extends the top panel forward to the face plane. |

The `visualize_cabinet` and `visualize_project` MCP tools expose `furniture_top` as a boolean parameter (`visualize_project` applies it to every cabinet in the run).

## Planned enhancements

- ~~**Cutlist PDF hardware BOM**~~ — shipped: both the layout HTML and the PDF include a hardware BOM (slides, hinges, pulls, legs) with per-line unit prices and totals.
