"""Regenerate every README gallery screenshot.

Run from the repo root:

    uv run --with playwright python scripts/readme_screenshots.py

Uses the system Google Chrome (playwright channel="chrome") — playwright is
fetched per-run and is NOT a project dependency. Two phases:

1. Drive the visualize handlers directly (no MCP transport) to emit
   self-contained viewer HTML for each shot into a temp dir.
2. Load each page headless, dispatch the viewer's real keyboard shortcuts
   (X-ray, open drawers, clip plane, manga stack), and screenshot the
   canvas. Paper-trail crops are element screenshots of the cutlist /
   assembly HTML docs on disk.

PNGs land in docs/images/. Saved projects referenced here must exist
(see list_projects); paper crops read the current cutlist/assembly output
folders named below.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))   # `evals` lives at the repo root, not in site-packages
IMAGES = REPO / "docs" / "images"
CUTLIST_DIR = Path.home() / ".cabineteer" / "cutlists" / "all-builds-2026-08-backcap"
ASSEMBLY_DIR = Path.home() / ".cabineteer" / "assembly" / "dining-sideboards-v2-hardwood"

VIEWPORT = {"width": 1500, "height": 950}
LOAD_WAIT_MS = 3500          # GLB parse + texture generation
SETTLE_MS = 800              # after a key toggle / animation

# ── Phase 1: viewer HTML generation ──────────────────────────────────────────

# (name, handler, args) — output_dir is filled in at runtime.
VIEWER_PAGES = [
    ("sideboard-oak", "visualize_project",
     {"project_name": "dining-sideboards-v2-hardwood", "finish": "rift_white_oak"}),
    ("sideboard-walnut", "visualize_project",
     {"project_name": "dining-sideboards-v2-hardwood", "finish": "black_walnut"}),
    ("sideboard-bamboo", "visualize_project",
     {"project_name": "dining-sideboards-v2-hardwood", "finish": "bamboo"}),
    # gap_mm spreads the two towers apart so the worktop spans the real
    # 48" kneehole instead of the towers butting into one block.
    ("desk", "visualize_project",
     {"project_name": "kid1-desk", "finish": "baltic_birch", "furniture_top": True,
      "manga": True, "gap_mm": 1219.2}),
    ("kapex", "visualize_project",
     {"project_name": "kapex_miter_station", "finish": "baltic_birch"}),

    ("shelf", "visualize_project",
     {"project_name": "kid2-shelf", "finish": "baltic_birch"}),
]


async def generate_viewer_pages(out_dir: Path) -> dict[str, Path]:
    from evals.harness import TOOL_DISPATCH
    pages: dict[str, Path] = {}
    for name, tool, args in VIEWER_PAGES:
        # One subdir per page: the HTML filename is the project stem, so
        # same-project pages (finish variants) would overwrite each other.
        page_dir = out_dir / name
        page_dir.mkdir(parents=True, exist_ok=True)
        res = await TOOL_DISPATCH[tool](
            {**args, "output_dir": str(page_dir), "open_browser": False})
        data = json.loads(res[0].text)
        if data.get("error"):
            raise SystemExit(f"{tool} {name}: {data['error']}")
        html = data.get("html") or data.get("files", {}).get("html")
        if not html:
            raise SystemExit(f"{tool} {name}: no HTML path in result "
                             f"{list(data)}")
        pages[name] = Path(html)
        print(f"  viewer page: {name} -> {html}")
    return pages


# ── Phase 2: screenshots ─────────────────────────────────────────────────────

# (out_png, page_key, keys_to_press, (azimuth°, elevation°, padding), extra)
# Azimuth 0 = straight-on; elevation is above horizon. Framing runs AFTER
# the key presses so opened drawers stay inside the frame.
STD = (35, 14, 1.12)
VIEWER_SHOTS = [
    ("hero-sideboard.png", "sideboard-oak", [], STD, None),
    ("gallery-desk.png", "desk", [], STD, None),
    ("gallery-kapex.png", "kapex", [], STD, None),
    ("gallery-shelf.png", "shelf", [], STD, None),
    ("finish-walnut.png", "sideboard-walnut", [], STD, None),
    ("finish-oak.png", "sideboard-oak", [], STD, None),
    ("finish-bamboo.png", "sideboard-bamboo", [], STD, None),
    ("viewer-xray.png", "desk", ["x"], STD, None),
    ("viewer-open.png", "desk", ["o"], (30, 22, 1.1), None),
    ("viewer-clip.png", "sideboard-oak", ["c"], (25, 12, 1.1),
     "setClipForShot()"),
    # Steeper elevation, framed on ONE tower, to look into the open
    # drawers at the manga stacks.
    ("viewer-manga.png", "desk", ["o", "m", "m"],
     (20, 38, 1.1, "tower-left"), None),
]

# Deterministic framing via the viewer's window.cabineteerViewer hook:
# aim the orbit target at the furniture's bounding-box centre and pull the
# camera to the fit distance along its current view direction.
FRAME_JS = """
([az, el, pad, nameFilter]) => {
  const v = window.cabineteerViewer;
  if (!v) return 'no-hook';
  const { scene, camera, controls, THREE } = v;
  const wanted = (o) => {
    if (!nameFilter) return true;
    for (let n = o; n; n = n.parent) if (n.name === nameFilter) return true;
    return false;
  };
  const box = new THREE.Box3();
  scene.traverse(o => { if (o.isMesh && wanted(o)) box.expandByObject(o); });
  if (box.isEmpty()) return 'empty';
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const azr = az * Math.PI / 180, elr = el * Math.PI / 180;
  const dir = new THREE.Vector3(
    Math.sin(azr) * Math.cos(elr), Math.sin(elr),
    Math.cos(azr) * Math.cos(elr));
  controls.target.copy(center);
  // Corner-projection fit. Perspective NDC extent scales ~1/distance, so
  // one proportional step from a safely-far probe distance lands the fit:
  // measure the worst corner at 3x the bbox diagonal (guaranteed fully in
  // frame), then scale the distance so that corner sits at 1/pad of the
  // frame half-extent.
  const corners = [];
  for (const x of [box.min.x, box.max.x])
    for (const y of [box.min.y, box.max.y])
      for (const z of [box.min.z, box.max.z])
        corners.push(new THREE.Vector3(x, y, z));
  const measure = (d) => {
    camera.position.copy(center.clone().addScaledVector(dir, d));
    camera.lookAt(center);           // re-aim: projection needs the view
    camera.updateMatrixWorld(true);  // matrix, not just the position
    let worst = 0;
    for (const c of corners) {
      const ndc = c.clone().project(camera);
      worst = Math.max(worst, Math.abs(ndc.x), Math.abs(ndc.y));
    }
    return worst;
  };
  const probe = size.length() * 3;
  let dist = probe * measure(probe) * pad;
  if (measure(dist) > 1) dist *= measure(dist) * pad;   // rare second step
  camera.position.copy(center.clone().addScaledVector(dir, dist));
  controls.update();
  let worst = 0;
  camera.updateMatrixWorld(true);
  for (const c of corners) {
    const ndc = c.clone().project(camera);
    worst = Math.max(worst, Math.abs(ndc.x), Math.abs(ndc.y));
  }
  return 'framed dist=' + dist.toFixed(0) + ' worst=' + worst.toFixed(3) +
         ' size=' + size.toArray().map(v => v.toFixed(0)).join('x');
}
"""

# Hide the DOM overlays (info panel, shortcut legend) so gallery shots are
# pure furniture; the README text describes the panel instead.
HIDE_UI_JS = """
() => { for (const id of ['panel', 'help']) {
          const el = document.getElementById(id);
          if (el) el.style.display = 'none'; } }
"""

# Position the clip plane at ~45% across the width so the section cuts
# through a drawer bank. The viewer exposes the slider input; drive it the
# way a user would.
CLIP_JS = """
() => {
  const slider = document.querySelector('input[type="range"]');
  if (!slider) return 'no-slider';
  slider.value = slider.min * 1 + (slider.max - slider.min) * 0.45;
  slider.dispatchEvent(new Event('input', { bubbles: true }));
  return 'ok';
}
"""

PAPER_SHOTS = [
    # (out_png, html file, css selector, which match, pre_js)
    # Prefer a sheet that mixes projects — that's the batch colour story.
    ("paper-batch-layout.png",
     CUTLIST_DIR / "all-builds-2026-08-backcap_layout.html", "#shot-target", 0,
     "() => { const cards = [...document.querySelectorAll('.sheet-card')];"
     " const t = cards.find(c =>"
     "   c.querySelectorAll('.legend-swatch').length >= 2) || cards[0];"
     " t.id = 'shot-target'; }"),
    ("paper-hardware-bom.png",
     CUTLIST_DIR / "all-builds-2026-08-backcap_layout.html", ".bom-tbl", 0,
     # The hardware BOM lives in the LAST tab pane — activate its tab.
     "() => { const b = document.querySelectorAll('.tab-btn');"
     " b[b.length - 1].click(); }"),
    # SVGs 0-2 are the registration cross-sections; the per-panel mortise
    # maps follow.
    ("paper-registration.png",
     ASSEMBLY_DIR / "dining-sideboards-v2-hardwood_assembly.html", "svg", 0,
     None),
    ("paper-mortise-map.png",
     ASSEMBLY_DIR / "dining-sideboards-v2-hardwood_assembly.html", "svg", 4,
     None),
]

# The bench parts table only exists in PDF form; macOS sips renders page 1.
PARTS_PDF = CUTLIST_DIR / "kid1-desk_parts.pdf"


def parts_pdf_shot() -> None:
    import subprocess
    if not PARTS_PDF.exists():
        print(f"  SKIP paper-parts-table.png: {PARTS_PDF} missing")
        return
    subprocess.run(["sips", "-s", "format", "png", str(PARTS_PDF),
                    "--out", str(IMAGES / "paper-parts-table.png")],
                   check=True, capture_output=True)
    print("  shot: paper-parts-table.png (sips, PDF page 1)")


def screenshot_all(pages: dict[str, Path]) -> None:
    from playwright.sync_api import sync_playwright

    IMAGES.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
        page = ctx.new_page()

        for out, key, presses, view, extra in VIEWER_SHOTS:
            page.goto(pages[key].as_uri())
            page.wait_for_timeout(LOAD_WAIT_MS)
            page.evaluate(HIDE_UI_JS)
            for k in presses:
                page.keyboard.press(k)
                page.wait_for_timeout(SETTLE_MS)
            if extra == "setClipForShot()":
                print(f"  clip slider: {page.evaluate(CLIP_JS)}")
                page.wait_for_timeout(SETTLE_MS)
            framed = page.evaluate(FRAME_JS, list(view) + [None] * (4 - len(view)))
            print(f"  {out}: {framed}")
            page.wait_for_timeout(300)
            canvas = page.query_selector("canvas")
            target = canvas if canvas else page
            target.screenshot(path=str(IMAGES / out))
            print(f"  shot: {out}")

        # Narrow viewport so document cards hug their drawings.
        page.set_viewport_size({"width": 1100, "height": 900})
        for out, html, selector, idx, pre_js in PAPER_SHOTS:
            if not html.exists():
                print(f"  SKIP {out}: {html} missing")
                continue
            page.goto(html.as_uri())
            page.wait_for_timeout(600)
            if pre_js:
                page.evaluate(pre_js)
                page.wait_for_timeout(300)
            # Tab panes hide most matches — only visible elements screenshot.
            els = [e for e in page.query_selector_all(selector)
                   if e.is_visible()]
            if not els:
                print(f"  SKIP {out}: selector {selector!r} matched nothing")
                continue
            els[idx].screenshot(path=str(IMAGES / out))
            print(f"  shot: {out}")

        browser.close()
    parts_pdf_shot()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="readme-shots-") as td:
        out_dir = Path(td)
        print("Generating viewer pages…")
        pages = asyncio.run(generate_viewer_pages(out_dir))
        print("Screenshotting…")
        screenshot_all(pages)
    print(f"Done -> {IMAGES}")


if __name__ == "__main__":
    sys.exit(main())
