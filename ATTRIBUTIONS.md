# Attributions

Hardware dimensions, placement rules, and part numbers in this project are derived from the following primary and secondary sources. All specs should be verified against current manufacturer documentation before purchasing. Per-item citations also live in the docstrings of `hardware.py` and `joinery.py`.

## Drawer slides

- **Blum Tandem 550H** — Blum Inc. 550H official datasheet; distributor cross-reference via [mcfaddens.com](https://mcfaddens.com) and [interfitco.com](https://interfitco.com); CabinetParts.com SKU confirmations (450 mm = 550H4500B, 550 mm = 550H5500B).
- **Blum Tandem Plus 563H** — Blum 563H official datasheet © 2016; catalog index via cabinetdoor.store and d2.blum.com; CabinetParts.com confirmed SKUs (18″ = 563H4570B, 21″ = 563H5330B).
- **Blum Movento 760H** — Blum Movento brochure "The Evolution of Motion" (2016/2024); distributor SKU tables via [mcfaddens.com](https://mcfaddens.com) and [hwt-pro.com](https://hwt-pro.com); Amazon listings.
- **Blum Movento 769** — Blum 769 catalog page © 2019; CabinetParts and Indian River Cabinet Supply SKU listings; [rokhardware.com](https://rokhardware.com) spec page. Confirmed SKUs: 769.4570S (18″), 769.5330S (21″), 769.6100S (24″).
- **Accuride 3832** — [Accuride International](https://www.accuride.com) product page and distributor listings.
- **Salice Futura** — Salice Futura catalog D0CASG010ENG; [wwhardware.com](https://wwhardware.com) specs. Confirmed SKUs via CabinetParts and woodworkerexpress.
- **Salice Progressa+** — Salice PROGRESSA catalog D0CASAA36USA; [cabinetparts.com](https://cabinetparts.com) SKU SHG5U6S533XXF6 (21″ confirmed).

## Door hinges

- **Blum Clip Top 110° / 170° family** — Blum CLIP top official datasheet ([d2.blum.com/en/HingeDataSheet_cliptop.pdf](https://d2.blum.com/en/HingeDataSheet_cliptop.pdf)); Blum catalog "Kitchen & Bedroom" © 2023; confirmed SKUs via [hafele.com](https://hafele.com) and [hardware.com](https://hardware.com). Overlay is encoded in the last two of the four core digits — **35** = full, **36** = half, **37** = inset — and the family letter marks soft-close: plain **71T3590 / 71T3690 / 71T3790**, integrated-BLUMOTION **71B3590 / 71B3690 / 71B3790**; the 170° hinge is **71T6550**. Trailing **…90** = INSERTA (tool-free cup, no cup screws), **…50** = screw-on (2× 606N cup screws). These match `hardware.py`; the earlier 71H (half) / 71N (inset) prefixes were a placeholder scheme, not real Blum SKUs, and were corrected to the digit-coded numbers above on 2026-07-28.
- Hinge count and placement rules (100 mm from top/bottom; spacing thresholds at 1 200 mm and 1 800 mm) from Blum published door-height/weight tables.

## Furniture legs

- **Richelieu 176138106** (100 mm brushed nickel contemporary square leg) — [thebuilderssupply.com](https://thebuilderssupply.com/richelieu-contemporary-furniture-leg-1761-176138106); [dspoutlet.com](https://dspoutlet.com/products/richelieu-3-15-16-100-mm-contemporary-furniture-leg-brushed-nickel-2-pack). Height confirmed as 3-15/16″ (100 mm), not 4″.
- **Richelieu adjustable leg** — [woodcraft.com](https://www.woodcraft.com/products/richelieu-1-9-16-40-mm-adjustable-contemporary-furniture-leg-dark-brown); [Richelieu Hardware catalog](https://www.richelieu.com/us/en/category/furniture-equipment/furniture-legs/adjustable-furniture-legs/1003965).

## Pulls and knobs

Pull dimensions (center-to-center, overall length, projection), model numbers, pack quantities, finishes, and style classifications are taken from manufacturer catalog and retailer product pages. Verify current availability and pricing at the source before ordering.

- **Top Knobs — Kinney bar pull series** (TK76HB/AG/BLK through TK305HB/AG/BLK) — [topknobs.com](https://www.topknobs.com/catalogsearch/result/?q=Kinney). Honey Bronze, Antique Gold, and Flat Black finishes across cc 76 / 96 / 128 / 160 / 305 mm.
- **Rockler — Ashley Norton "Urban Designer" wood cabinet pulls** (MN-160/224/288-WNL, MN-160/224/288-OKL) — [rockler.com](https://www.rockler.com/9-3-4-ashley-norton-urban-designer-wood-cabinet-pull-walnut). Walnut and oak variants.
- **Rockler — Unfinished White Oak Mission Pull** (42250) — [rockler.com](https://www.rockler.com/unfinished-white-oak-mission-pulls).
- **Richelieu — Modern aluminum edge pulls and contemporary surface pulls** — [richelieu.com](https://www.richelieu.com/us/en/product/decorative-hardware/cabinet-hardware/pulls-and-knobs/pulls/modern-aluminum-edge-pull-9898/1063166). Center-to-center dimensions confirmed against Richelieu's SKU-level product pages.
- **Häfele — Modern wood surface pull** (193.18.766) and **minimalist flush pull** (151.35.665) — [hafele.com](https://www.hafele.com). Dimensions from Häfele product catalog.
- **IKEA — Bagganäs Handle Black, 128 mm** (803.384.11) — [ikea.com](https://www.ikea.com/us/en/p/bagganaes-handle-black-80338411/). Sold in 2-packs.
- **IKEA — Häckås Handle Anthracite, 128 mm** (303.424.77) — [ikea.com](https://www.ikea.com/us/en/p/hackas-handle-anthracite-30342477/). Sold in 2-packs.
- **IKEA — Borghamn Handle Black, 416 mm** (203.160.49) — [ikea.com](https://www.ikea.com/us/en/p/borghamn-handle-black-20316049/). Sold in 2-packs.
- **IKEA — Billsbro Handle White, 120 mm** — [ikea.com](https://www.ikea.com/us/en/). Sold in 2-packs.

Placement policy (600 mm dual-pull threshold, 40 mm end margin, vertical placement conventions) is derived from general cabinet-shop practice — see [Fine Homebuilding](https://www.finehomebuilding.com), [Fine Woodworking](https://www.finewoodworking.com), and [Sawmill Creek Woodworking Community](https://sawmillcreek.org) discussions on drawer-pull placement — and not from any single datasheet.

## Drawer box height standards

- Industry standard box heights (½″ and 1″ increment series, 3″–12″) — [Cabinet Doors 'N' More](https://cabinetdoorsnmore.com/pages/how-to-drawer-boxes); [Eagle Woodworking](https://www.eaglewoodworking.com/dovetail-drawers/drawer-wood-types/maple-drawer-boxes).
- Kitchen drawer sizing conventions — [Sawmill Creek Woodworking Community](https://sawmillcreek.org/threads/kitchen-cabinet-top-drawer-sizing.316059/); [Kreg Tool — Demystifying Drawer Sizing](https://learn.kregtool.com/learn/demystifying-drawer-sizing/); [PALET Cabinetry Drawer Height Guide](https://paletcabinets.com/pages/drawer-height-specifications).

## Joinery

- **QQQ locking rabbet** — Stephen Phipps, *This Is Carpentry* (2014): "The Quarter-Quarter-Quarter Method." All cuts set to material thickness ÷ 2.
- **Festool Domino tenon dimensions** — Festool DF 500 and DF 700 official datasheets; confirmed mortise and tenon dimensions from Festool USA product pages.
- **Kreg pocket-screw geometry** — Kreg Tool Company pocket-screw jig documentation; [kregtool.com](https://www.kregtool.com).
- **Biscuit sizes** (#0, #10, #20) — industry-standard dimensions as catalogued by major manufacturers (Lamello, DeWalt, Porter-Cable).
