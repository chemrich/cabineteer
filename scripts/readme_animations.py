"""Render animated GIF candidates for the README from the saved projects.

    uv run --with playwright --with pillow python scripts/readme_animations.py [outdir]

Same machinery as readme_screenshots.py (direct handler drive + headless
system Chrome + the window.cabineteerViewer hook), plus: a deterministic
slow revolve around the piece's centroid (orbit radius fixed to the
worst-case azimuth so the framing never pumps), keyframed state changes
(finish swaps, O/X/C/M, clip-plane sweeps), and pillow GIF assembly.

GIFs land in the given outdir (default: ./anim-preview next to the repo,
NOT docs/images — candidates are previewed before anything is committed).
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

VIEWPORT = {"width": 1140, "height": 720}
GIF_WIDTH = 640
FPS = 10
LOAD_WAIT_MS = 3500

# ── The grand tour: a deliberately-styled collection ─────────────────────────
# Mock designs built to look like real catalogue furniture (Blu Dot-ish):
# lifted stances on slim legs, real pulls, furniture tops, considered
# proportions and grain. Built inline via visualize_cabinet — never saved
# to the project store. Every stack is computed to sum exactly so the
# evaluator passes (validated at render time).

SIDE_T = 18.0  # side/top/bottom thickness used across the collection


def _split(total, ratios):
    """Split `total` mm into len(ratios) parts summing exactly to total."""
    s = sum(ratios)
    parts = [round(total * r / s, 1) for r in ratios]
    parts[-1] = round(total - sum(parts[:-1]), 1)
    return parts


def drawers(height, ratios):
    """Drawer openings whose heights sum to the interior height.

    Openings are indexed bottom-up (index 0 = bottom), and real chests put
    the DEEP drawers at the bottom and shallow ones on top — so we always
    sort deepest-first regardless of the order the ratios are written in.
    (Matches the product default: proportions.graduated_drawer_heights and
    design_cabinet both place the largest drawer at the bottom.)"""
    ih = height - 2 * SIDE_T
    parts = sorted(_split(ih, ratios), reverse=True)   # deepest at bottom
    return [[h, "drawer"] for h in parts]


def column(width, openings):
    return {"width_mm": round(width, 1), "openings": openings}


def cols(width, entries):
    """entries: list of (width_ratio, openings). Column widths + inter-column
    dividers sum exactly to the interior width."""
    iw = width - 2 * SIDE_T
    avail = iw - (len(entries) - 1) * SIDE_T
    widths = _split(avail, [r for r, _ in entries])
    return [column(w, o) for w, (_, o) in zip(widths, entries)]


def door(height, kind="door"):
    return [[height - 2 * SIDE_T, kind]]


def mixed(height, lower, ratios, kinds):
    """A single column: openings from bottom up, heights summing to interior.
    `ratios`/`kinds` parallel lists (e.g. drawer, then door_pair above)."""
    parts = _split(height - 2 * SIDE_T, ratios)
    return [[h, k] for h, k in zip(parts, kinds)]


# Each: (name, label, visualize_cabinet arg dict). Wood pulls on wood
# pieces, matte-black bars for contrast, hairpin legs for lift.
def _pieces():
    P = []
    P.append(("credenza", "Credenza · walnut", dict(
        width=1500, height=740, depth=450, finish="walnut",
        grain_direction="horizontal", furniture_top=True,
        leg_key="hairpin_200mm", leg_count=4,
        door_hinge="blum_clip_top_blumotion_110_half",
        door_pull="rockler-wnl-160", drawer_pull="rockler-wnl-224",
        columns=cols(1500, [
            (1.15, door(740)),
            # shallow top → deep bottom, strong graduation
            (1.0, drawers(740, [1.0, 1.55, 2.1])),
            (1.15, door(740)),
        ]))))
    P.append(("dresser", "Nine-drawer dresser · black walnut", dict(
        width=1120, height=800, depth=480, finish="black_walnut",
        grain_direction="horizontal", furniture_top=True,
        leg_key="hairpin_152mm", leg_count=4,
        drawer_pull="rockler-wnl-224",
        # Asymmetric: a bank of small drawers beside big graduated ones —
        # no two columns match, so it never reads as a grid.
        columns=cols(1120, [
            (0.72, drawers(800, [1, 1, 1, 1, 1])),      # 5 shallow
            (1.35, drawers(800, [1.0, 1.7, 2.5])),      # 3 deep, graduated
        ]))))
    P.append(("chest", "Tall chest · white oak", dict(
        width=560, height=1240, depth=460, finish="flat_sawn_white_oak",
        grain_direction="vertical", furniture_top=True,
        leg_key="hairpin_152mm", leg_count=4,
        drawer_pull="generic-knob-blk-32",   # round knobs (mixed w/ pulls elsewhere)
        # two small "jewellery" drawers up top, then a clear run to deep
        drawer_config=drawers(1240, [0.8, 0.8, 1.4, 1.9, 2.5]))))
    P.append(("nightstand", "Nightstand · walnut", dict(
        width=460, height=540, depth=400, finish="walnut",
        grain_direction="horizontal", furniture_top=True,
        leg_key="hairpin_200mm", leg_count=4,
        drawer_pull="generic-knob-blk-32",
        drawer_config=drawers(540, [1.0, 1.9]))))    # shallow over deep
    P.append(("media", "Media console · rift oak", dict(
        width=1650, height=500, depth=420, finish="rift_white_oak",
        grain_direction="horizontal", furniture_top=True,
        leg_key="richelieu_17613b106", leg_count=6,
        door_hinge="blum_clip_top_blumotion_110_half",
        door_pull="rockler-okl-224",
        columns=cols(1650, [
            (1.0, door(500)),
            (1.0, door(500)),
            (1.0, door(500)),
        ]))))
    P.append(("bar", "Bar cabinet · walnut", dict(
        width=780, height=1080, depth=440, finish="walnut",
        grain_direction="vertical", furniture_top=True,
        leg_key="hairpin_200mm", leg_count=4,
        door_pull="generic-knob-brs-30", drawer_pull="generic-knob-brs-30",
        drawer_config=mixed(1080, None, [1.4, 3.0],
                            ["drawer", "door_pair"]))))
    P.append(("bookcase", "Open shelving · white oak", dict(
        width=900, height=1180, depth=340, finish="flat_sawn_white_oak",
        grain_direction="vertical", furniture_top=True,
        leg_key="hairpin_152mm", leg_count=4,
        drawer_config=door(1180, "open"),
        fixed_shelf_positions=[310, 590, 870])))
    P.append(("console", "Console table · white oak", dict(
        width=1150, height=750, depth=340, finish="flat_sawn_white_oak",
        grain_direction="horizontal", furniture_top=True,
        leg_key="hairpin_200mm", leg_count=4,
        drawer_pull="rockler-okl-224",
        columns=cols(1150, [
            (1.0, mixed(750, None, [3.0, 1.0], ["open", "drawer"])),
            (1.0, mixed(750, None, [3.0, 1.0], ["open", "drawer"])),
        ]))))
    P.append(("wardrobe", "Wardrobe · white oak", dict(
        width=1080, height=1680, depth=540, finish="flat_sawn_white_oak",
        grain_direction="vertical", furniture_top=True,
        leg_key="richelieu_17613b106", leg_count=4,
        door_hinge="blum_clip_top_blumotion_110_half",
        door_pull="rockler-okl-288", drawer_pull="rockler-okl-224",
        columns=cols(1080, [
            (1.25, door(1680)),
            # graduated drawer bank beside the wardrobe door
            (1.0, drawers(1680, [1.0, 1.4, 1.85, 2.35])),
        ]))))
    return P


DESIGNED_PIECES = _pieces()

VIEWER_PAGES = [
    ("sideboard", "visualize_project",
     {"project_name": "dining-sideboards-v2-hardwood",
      "finish": "rift_white_oak"}),
    ("desk", "visualize_project",
     {"project_name": "kid1-desk", "finish": "baltic_birch",
      "furniture_top": True, "manga": True, "gap_mm": 1219.2}),
    ("kapex", "visualize_project",
     {"project_name": "kapex_miter_station", "finish": "baltic_birch"}),
    ("shelf", "visualize_project",
     {"project_name": "kid2-shelf", "finish": "baltic_birch"}),
]


def key(k):
    return ("key", k)


def finish(name):
    return ("js",
            f"() => {{ const s = document.getElementById('finish-sel');"
            f" s.value = '{name}';"
            f" s.dispatchEvent(new Event('change', {{bubbles: true}}));"
            f" return s.value; }}")


def drawer_state(n):
    """Deterministically set the drawer open state (0 closed · 1 staggered ·
    2 open) via the viewer hook — more reliable than counting O presses."""
    return ("js", f"() => window.cabineteerViewer.setDrawerState({n})")


def clip_pos(frac):
    return ("js",
            "() => { const s = document.getElementById('clip-pos') ||"
            " document.querySelector('#clip-ui input[type=range]');"
            " if (!s) return 'no-slider';"
            f" s.value = +s.min + (+s.max - +s.min) * {frac};"
            " s.dispatchEvent(new Event('input', {bubbles: true}));"
            " return 'ok'; }")


# Drawer-rich subset for the state tours: each has drawers (so O does
# something) and most also carry doors (so X-ray reveals interiors too).
_TOUR_SUBSET = [
    ("credenza", "Credenza · walnut"),
    ("dresser", "Nine-drawer dresser · black walnut"),
    ("chest", "Tall chest · white oak"),
    ("bar", "Bar cabinet · walnut"),
    ("wardrobe", "Wardrobe · white oak"),
]

# Each spec: name, list of segments. Segment: page, frame count, azimuth
# sweep (degrees), elevation, actions keyed by segment-local frame index.
# A slow full revolve at 12 fps: 96 frames = 8 s loop.
SPECS = [
    # 1 — The whole catalogue: one mock design per distinct furniture
    # type, each holding the frame for a second of the same slow pass,
    # with a caption chip naming the piece.
    # The azimuth is CONTINUOUS across the whole tour: each piece adds
    # `turn` to a running angle (no reset on piece change), so the camera
    # makes ONE unbroken pan while pieces swap underneath it. The sweep is
    # kept inside the FRONT hemisphere (-75°…+80°) so every piece is shown
    # front-on — cabinet backs are blank panels and never face the camera.
    # ~1/3 of the pieces (indices 1/5/8 — dresser, bar, wardrobe: spread
    # across the loop, all drawer-bearing) are shown STATICALLY in the
    # partial gradient-open state for their whole turn (set once at segment
    # start); the rest stay closed. No drawer motion during the pan.
    {"name": "grand-tour", "revolve": True, "el": 15, "az_start": -75,
     "segments": [
        {"page": f"d-{name}", "frames": 15, "turn": 155 / 9, "label": label,
         **({"pre_js": [drawer_state(1)[1]]} if i in (1, 5, 8) else {})}
        for i, (name, label, _args) in enumerate(DESIGNED_PIECES)
    ]},
    # 1b/1c/1d — the same continuous front-hemisphere pan over a drawer-rich
    # subset, holding a viewer state on each piece: staggered-open, x-ray,
    # and both. `pre_keys` fire once at segment start (one O press = the
    # staggered cascade) and settle before the pan, so each piece is already
    # in-state as the camera moves. Doors don't animate in the viewer (O
    # drives drawers only), so the roster is drawer pieces that also carry
    # doors for x-ray variety.
    {"name": "open-tour", "revolve": True, "el": 15, "az_start": -75,
     "segments": [
        {"page": f"d-{name}", "frames": 16, "turn": 155 / len(_TOUR_SUBSET),
         "label": f"{label.split(' · ')[0]} · gradient open",
         "pre_js": [drawer_state(1)[1]]}
        for name, label in _TOUR_SUBSET
    ]},
    {"name": "xray-tour", "revolve": True, "el": 15, "az_start": -75,
     "segments": [
        {"page": f"d-{name}", "frames": 16, "turn": 155 / len(_TOUR_SUBSET),
         "label": f"{label.split(' · ')[0]} · x-ray fronts",
         "pre_keys": ["x"]}
        for name, label in _TOUR_SUBSET
    ]},
    {"name": "open-xray-tour", "revolve": True, "el": 15, "az_start": -75,
     "segments": [
        {"page": f"d-{name}", "frames": 16, "turn": 155 / len(_TOUR_SUBSET),
         "label": f"{label.split(' · ')[0]} · gradient + x-ray",
         "pre_js": [drawer_state(1)[1]], "pre_keys": ["x"]}
        for name, label in _TOUR_SUBSET
    ]},
    # 1e — feature demo: ONE piece walked through the full O cycle
    # (closed → staggered → full → closed) while it slowly turns, each
    # state held long enough to read. States are set deterministically via
    # the viewer hook at segment-local frames (no press-counting drift).
    {"name": "open-cycle", "revolve": True, "el": 15, "az_start": -30,
     "segments": [
        {"page": "d-dresser", "frames": 52, "turn": 60,
         "label": "O cycles: closed → partial → open",
         "actions": {2: drawer_state(0), 14: drawer_state(1),
                     28: drawer_state(2), 42: drawer_state(0)}},
    ]},
    # 2 — One design, continuous revolve, the wood changes under you.
    {"name": "finish-carousel", "segments": [
        {"page": "sideboard", "frames": 72, "az": (0, 360), "el": 14,
         "actions": {18: finish("black_walnut"),
                     36: finish("bamboo"),
                     54: finish("cherry")}},
    ]},
    # 3 — Feature demo: ping-pong across the FRONT arc (a full revolve
    # wastes half the loop showing the features to the cabinet's back)
    # while drawers open, x-ray on, then everything reverses for a clean
    # loop.
    {"name": "open-xray", "segments": [
        {"page": "desk", "frames": 36, "az": (-35, 55), "el": 18,
         "actions": {8: key("o"), 26: key("x")}},
        {"page": "desk", "frames": 36, "az": (55, -35), "el": 18,
         "actions": {10: key("x"), 24: key("o")}},
    ]},
    # 4 — CT scan: the clip plane sweeps down through the carcass while
    # the camera drifts. Clip card stays visible (live mm readout).
    {"name": "clip-sweep", "segments": [
        {"page": "sideboard", "frames": 72, "az": (10, 70), "el": 18,
         "actions": {0: key("c")},
         "clip": (0.98, 0.02)},
    ]},
    # 5 — Close-up on one desk tower: drawers open, manga stack cycles.
    {"name": "manga-peek", "segments": [
        {"page": "desk", "frames": 72, "az": (-10, 40), "el": 42,
         "filter": "tower-left",
         "actions": {8: key("o"), 22: key("m"), 36: key("m"),
                     50: key("m"), 62: key("m")}},
    ]},
]

LABEL_JS = """
(text) => {
  let el = document.getElementById('anim-label');
  if (!el) {
    el = document.createElement('div');
    el.id = 'anim-label';
    el.style.cssText = 'position:fixed;left:24px;bottom:20px;' +
      'background:rgba(20,22,43,0.85);color:#f0c674;' +
      'font:600 20px/1.4 -apple-system,sans-serif;' +
      'padding:8px 16px;border-radius:8px;letter-spacing:.02em;' +
      'z-index:99';
    document.body.appendChild(el);
  }
  el.textContent = text;
}
"""

HIDE_UI_JS = """
() => { for (const id of ['panel', 'help']) {
          const el = document.getElementById(id);
          if (el) el.style.display = 'none'; } }
"""

# Compute the piece's centroid and a fixed orbit radius: the fit distance
# is measured at 12 azimuths (corner-projection fit, as in the stills
# script) and the maximum wins, so the revolve never zoom-pumps.
SETUP_JS = """
([el, pad, nameFilter]) => {
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
  const corners = [];
  for (const x of [box.min.x, box.max.x])
    for (const y of [box.min.y, box.max.y])
      for (const z of [box.min.z, box.max.z])
        corners.push(new THREE.Vector3(x, y, z));
  const elr = el * Math.PI / 180;
  const probe = box.getSize(new THREE.Vector3()).length() * 3;
  let radius = 0;
  for (let az = 0; az < 360; az += 30) {
    const azr = az * Math.PI / 180;
    const dir = new THREE.Vector3(
      Math.sin(azr) * Math.cos(elr), Math.sin(elr),
      Math.cos(azr) * Math.cos(elr));
    camera.position.copy(center.clone().addScaledVector(dir, probe));
    camera.lookAt(center);
    camera.updateMatrixWorld(true);
    let worst = 0;
    for (const c of corners) {
      const ndc = c.clone().project(camera);
      worst = Math.max(worst, Math.abs(ndc.x), Math.abs(ndc.y));
    }
    radius = Math.max(radius, probe * worst * pad);
  }
  window.__anim = { cx: center.x, cy: center.y, cz: center.z, r: radius };
  controls.target.copy(center);
  return 'ok';
}
"""

POSE_JS = """
([az, el]) => {
  const v = window.cabineteerViewer, a = window.__anim;
  if (!v || !a) return 'no-setup';
  const { camera, controls, THREE } = v;
  const azr = az * Math.PI / 180, elr = el * Math.PI / 180;
  const center = new THREE.Vector3(a.cx, a.cy, a.cz);
  camera.position.set(
    a.cx + a.r * Math.sin(azr) * Math.cos(elr),
    a.cy + a.r * Math.sin(elr),
    a.cz + a.r * Math.cos(azr) * Math.cos(elr));
  camera.lookAt(center);
  controls.target.copy(center);
  return 'ok';
}
"""


async def generate_viewer_pages(out_dir: Path,
                                needed: set[str]) -> dict[str, Path]:
    from evals.harness import TOOL_DISPATCH

    async def call(tool, args):
        res = await TOOL_DISPATCH[tool](args)
        data = json.loads(res[0].text)
        if data.get("error"):
            raise SystemExit(f"{tool}: {data['error']}")
        return data

    pages: dict[str, Path] = {}
    for name, tool, args in VIEWER_PAGES:
        if name not in needed:
            continue
        page_dir = out_dir / name
        page_dir.mkdir(parents=True, exist_ok=True)
        data = await call(tool, {**args, "output_dir": str(page_dir),
                                 "open_browser": False})
        pages[name] = Path(data["html"])
        print(f"  page: {name}")
    for piece_name, _label, args in DESIGNED_PIECES:
        name = f"d-{piece_name}"
        if name not in needed:
            continue
        # Validate the design before rendering — a bad stack should fail
        # loudly here, not produce a silently-wrong frame.
        ev = await call("evaluate_cabinet",
                        {k: v for k, v in args.items()
                         if k not in ("finish", "grain_direction",
                                      "furniture_top", "drawer_pull",
                                      "door_pull", "leg_key", "leg_count")})
        errs = ev.get("summary", {}).get("errors", 0)
        if errs:
            msgs = [i["message"] for i in ev.get("issues", [])
                    if i.get("severity") == "error"]
            raise SystemExit(f"{name}: {errs} eval error(s): {msgs}")
        page_dir = out_dir / name
        page_dir.mkdir(parents=True, exist_ok=True)
        data = await call("visualize_cabinet",
                          {**args, "output_dir": str(page_dir),
                           "open_browser": False})
        pages[name] = Path(data["html"])
        print(f"  page: {name} ({args['finish']})")
    return pages


def render_spec(page, pages, spec, frames_dir: Path) -> list[Path]:
    frame_paths: list[Path] = []
    current_page = None
    n = 0
    az_run = spec.get("az_start", 0.0)   # running azimuth, no reset per piece
    spec_el = spec.get("el", 15)
    for seg in spec["segments"]:
        el = seg.get("el", spec_el)
        if seg["page"] != current_page:
            page.goto(pages[seg["page"]].as_uri())
            page.wait_for_timeout(LOAD_WAIT_MS)
            page.evaluate(HIDE_UI_JS)
            current_page = seg["page"]
        r = page.evaluate(SETUP_JS, [el, 1.12, seg.get("filter")])
        if r != "ok":
            raise SystemExit(f"{spec['name']}/{seg['page']}: setup {r}")
        if seg.get("label"):
            page.evaluate(LABEL_JS, seg["label"])
        # pre_js / pre_keys: set a viewer state once, let it settle, THEN pan.
        for js in seg.get("pre_js", []):
            page.evaluate(js)
        for k in seg.get("pre_keys", []):
            page.keyboard.press(k)
        if seg.get("pre_js") or seg.get("pre_keys"):
            page.wait_for_timeout(600)
        if spec.get("revolve"):
            az0 = az_run
            az1 = az_run + seg["turn"]
            az_run = az1
        else:
            az0, az1 = seg["az"]
        actions = seg.get("actions", {})
        clip = seg.get("clip")
        canvas = page.query_selector("canvas")
        for i in range(seg["frames"]):
            t = i / max(1, seg["frames"] - 1)
            act = actions.get(i)
            if act:
                kind, payload = act
                if kind == "key":
                    page.keyboard.press(payload)
                else:
                    page.evaluate(payload)
            if clip is not None:
                c0, c1 = clip
                page.evaluate(clip_pos(c0 + (c1 - c0) * t)[1])
            page.evaluate(POSE_JS, [az0 + (az1 - az0) * t, el])
            page.wait_for_timeout(70)   # let tweens/textures advance
            fp = frames_dir / f"{spec['name']}-{n:04d}.png"
            canvas.screenshot(path=str(fp))
            frame_paths.append(fp)
            n += 1
    return frame_paths


def compose_gif(frame_paths: list[Path], out: Path) -> None:
    """ffmpeg palettegen/paletteuse with diff-mode: only changed regions
    are re-encoded each frame, which is what makes a mostly-static dark
    scene small. Falls back to pillow if ffmpeg is unavailable."""
    import shutil
    import subprocess
    pattern = str(frame_paths[0]).replace("-0000.png", "-%04d.png")
    if shutil.which("ffmpeg"):
        vf = (f"fps={FPS},scale={GIF_WIDTH}:-1:flags=lanczos,"
              "split[s0][s1];"
              "[s0]palettegen=max_colors=128:stats_mode=diff[p];"
              "[s1][p]paletteuse=dither=bayer:bayer_scale=4:"
              "diff_mode=rectangle")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS),
             "-i", pattern, "-vf", vf, "-loop", "0", str(out)],
            check=True)
    else:
        from PIL import Image
        frames = []
        for fp in frame_paths:
            im = Image.open(fp).convert("RGB")
            w, h = im.size
            im = im.resize((GIF_WIDTH, int(h * GIF_WIDTH / w)),
                           Image.LANCZOS)
            frames.append(im.quantize(colors=128, method=Image.MEDIANCUT))
        frames[0].save(out, save_all=True, append_images=frames[1:],
                       duration=int(1000 / FPS), loop=0, optimize=True)
    print(f"  gif: {out.name}  {out.stat().st_size / 1e6:.1f} MB  "
          f"({len(frame_paths)} frames)")


def main() -> None:
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = None
    if "--only" in sys.argv:
        raw = sys.argv[sys.argv.index("--only") + 1]
        only = set(raw.split(","))          # comma-separated spec names
        argv = [a for a in argv if a != raw]
    out_dir = Path(argv[0]) if argv else REPO / "anim-preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = [sp for sp in SPECS if only is None or sp["name"] in only]
    if not specs:
        raise SystemExit(f"no spec named {only!r}")
    needed = {seg["page"] for sp in specs for seg in sp["segments"]}
    from playwright.sync_api import sync_playwright
    with tempfile.TemporaryDirectory(prefix="readme-anim-") as td:
        tmp = Path(td)
        print("Generating viewer pages…")
        pages = asyncio.run(generate_viewer_pages(tmp, needed))
        frames_dir = tmp / "frames"
        frames_dir.mkdir()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome")
            ctx = browser.new_context(viewport=VIEWPORT,
                                      device_scale_factor=1)
            page = ctx.new_page()
            for spec in specs:
                print(f"Rendering {spec['name']}…")
                frame_paths = render_spec(page, pages, spec, frames_dir)
                compose_gif(frame_paths, out_dir / f"{spec['name']}.gif")
            browser.close()
    print(f"Done -> {out_dir}")


if __name__ == "__main__":
    sys.exit(main())
