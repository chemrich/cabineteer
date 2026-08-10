# The 3D viewer

`visualize_cabinet` and `visualize_project` produce a **single self-contained HTML file** (written to `~/.cabineteer/visualizations/`) with the 3D model embedded. Open it in any browser — nothing to install. The only requirement is an internet connection on first load (the Three.js graphics library loads from a CDN).

Send the file to a client, open it on the shop laptop, or keep it next to the cutlist — it keeps working.

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `O` | Cycle drawer open state: closed → partial → fully open → closed. Partial is a vertical gradient (bottom drawer 100% out, top 20%); boxes, faces, and pulls slide together |
| `X` | X-ray the drawer and door fronts (transparent overlay) so you can see the boxes behind them |
| `C` | Clip plane — slice through the model on any axis with a slider and a live mm readout |
| `V` | Diagnostic colors — every panel family gets a flat vivid color (drawer sides pink, fronts/backs yellow, bottoms green, carcass sides blue, tops/bottoms orange, faces/doors purple) for checking that parts are where they should be |
| `M` | Cycle the manga scale-reference stack (see below) |

Shortcuts ignore keystrokes while you're typing in the side-panel controls.

## Wood finishes

Both tools accept a `finish` parameter, and the side panel has a live dropdown, so you can flip through looks without re-rendering:

`rift_white_oak` · `flat_sawn_white_oak` (cathedral figure) · `maple` · `walnut` (European) · `black_walnut` · `bamboo` · `baltic_birch` · `cherry`

The grain is generated procedurally in the browser — deterministic, so the same design always looks the same. A `grain_direction` toggle switches the show surfaces between vertical (default) and horizontal.

Two shop-realistic defaults:

- **Drawer boxes render as Baltic birch** regardless of the show wood (set `drawer_box_finish` to match the carcass if you really want a uniform look) — because that's how the boxes are actually built.
- **Drawer-box grain is always horizontal** — box sides are cut with the grain along their length.

Pull hardware stays metal under every finish, and worktop legs stay metal too. Omit `finish` entirely for flat color-coded rendering.

## The scale problem, solved with manga

A 3D cabinet floating in a void gives no sense of size. Render with `manga=true` and each drawer gets a stack of standard tankōbon manga volumes (112.5 × 176 × 15 mm — a very consistent real-world object) sitting inside it. Press `M` to cycle the stack from one volume to five, then hidden.

It's a scale reference, not inventory — manga never appear in the cutlist or BOM. And if a drawer is too small to hold the stack, the render tells you so, naming the exact drawer — which is itself useful information about your drawer.

## Overlay styles

How faces relate to the carcass top and bottom is a design decision the viewer takes seriously:

| Parameter | Effect |
|---|---|
| `furniture_top` | "Furniture" style: the top panel gains a front cap flush with the face plane, and the bottom faces drop flush to the carcass underside |
| `face_top_overhang` / `face_bottom_overhang` | Manual control of how far faces extend past the top/bottom panels |

`visualize_project` applies `furniture_top` across the whole run.

## Requesting the cutlist from the viewer

The side panel's **Generate cutlist** button opens a modal with a ready-to-paste request that captures your current finish and grain selections. The viewer is a standalone file and can't call the design engine itself — the button hands you the exact sentence to paste back into your AI chat.
