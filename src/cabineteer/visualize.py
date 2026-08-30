"""GLTF export and browser-based 3D viewer for cabinet assemblies.

Exports a CadQuery Assembly to GLB (binary GLTF) format and generates a
self-contained HTML file with a Three.js orbit viewer.  No local server
required — the model is embedded in the file — but Three.js itself loads
from the jsDelivr CDN, so an internet connection is needed at view time.

Requires CadQuery ≥ 2.4 for GLB export (``pip install cadquery``).

Typical usage
-------------
::

    from cabineteer.cabinet import CabinetConfig
    from cabineteer.visualize import build_and_visualize

    cfg = CabinetConfig(width=600, height=720, depth=550)
    result = build_and_visualize(cfg, output_dir="~/Desktop/cabinet_view")
    # Opens browser automatically; result["html"] has the path.
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
import webbrowser
from html import escape as html_escape
from pathlib import Path
from typing import Optional, TYPE_CHECKING

try:
    import cadquery as cq
except ImportError:
    cq = None  # CadQuery is an optional dependency


def _require_cq() -> None:
    if cq is None:
        raise ImportError(
            "cadquery is required for 3D export. "
            "Install with: pip install cadquery"
        )


# Output ``name`` becomes a filename stem (``<name>.glb`` / ``<name>_viewer.html``)
# — restrict it exactly as project.py does so it can never contain a path
# separator or traverse out of the output directory.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")


def _validate_name(name: str) -> str:
    """Return ``name`` if it is safe as a filename stem, else raise ValueError."""
    if not _NAME_RE.match(name) or ".." in name:
        raise ValueError(
            f"Invalid name {name!r}: use letters, digits, spaces, '.', '_' or "
            "'-' (must start with a letter or digit)."
        )
    return name


# ── Wood finishes ─────────────────────────────────────────────────────────────
#
# Parameter sets for the viewer's procedural grain generator (see
# _FINISH_JS below).  All colour panels in the scene get the texture; pull
# hardware keeps its metal material.  Keys are exposed as the `finish`
# parameter on visualize_cabinet / visualize_project.
#
#   base        — three hex colours for the horizontal background gradient
#   grain_lo/hi — RGB endpoints; each grain line lerps between them
#   grain_alpha — [min, range] stroke opacity
#   line_gap    — [min, range] px between grain lines (512 px ≈ scale_u mm)
#   line_width  — [min, range] px stroke width
#   waviness    — [min, range] px lateral drift (low = rift-sawn straightness)
#   fleck_count / fleck_rgba — pore flecks: count and [r, g, b, a_min, a_range]
#   fleck_size  — optional [w_min, w_range, h_min, h_range] px; default
#                 [0.8, 1.2, 3, 10] (thin along-grain dashes).  Bamboo uses
#                 wide short dashes for its node knuckles.
#   pattern     — optional "cathedral" adds flat-sawn arch figures on top of
#                 the straight-line background; requires arch_gap [min, range]
#                 px between stacked apexes and arch_spread [min, range]
#                 (parabola width factor: x = spread · √(y − apex))
#   roughness   — PBR roughness applied with the texture
#   scale_u/v   — mm of wood covered by one texture tile (across / along grain)

WOOD_FINISHES: dict[str, dict] = {
    "rift_white_oak": {
        "label": "Rift-Sawn White Oak",
        "base": ["#e6d9bd", "#ddcfae", "#e2d4b6"],
        "grain_lo": [116, 98, 65],
        "grain_hi": [166, 141, 98],
        "grain_alpha": [0.16, 0.24],
        "line_gap": [2, 9],
        "line_width": [0.6, 1.6],
        "waviness": [1.2, 1.8],
        "fleck_count": 900,
        "fleck_rgba": [120, 95, 60, 0.04, 0.07],
        "roughness": 0.62,
        "scale_u": 250,
        "scale_v": 1000,
    },
    "flat_sawn_white_oak": {
        "label": "Flat-Sawn White Oak",
        "base": ["#e4d5b6", "#dbcba8", "#e0d1b2"],
        "grain_lo": [116, 98, 65],
        "grain_hi": [166, 141, 98],
        "grain_alpha": [0.14, 0.22],
        "line_gap": [6, 18],
        "line_width": [0.7, 1.8],
        "waviness": [2.5, 4.0],
        "fleck_count": 700,
        "fleck_rgba": [120, 95, 60, 0.04, 0.07],
        "pattern": "cathedral",
        "arch_gap": [40, 90],
        "arch_spread": [5, 6],
        "roughness": 0.62,
        "scale_u": 250,
        "scale_v": 1000,
    },
    "walnut": {
        "label": "European Walnut",
        "base": ["#8a6f52", "#7e6448", "#856a4e"],
        "grain_lo": [74, 56, 40],
        "grain_hi": [128, 104, 78],
        "grain_alpha": [0.16, 0.24],
        "line_gap": [3, 12],
        "line_width": [0.7, 1.8],
        "waviness": [2.0, 3.0],
        "fleck_count": 450,
        "fleck_rgba": [60, 45, 32, 0.04, 0.07],
        "roughness": 0.58,
        "scale_u": 250,
        "scale_v": 1000,
    },
    "black_walnut": {
        "label": "Black Walnut",
        "base": ["#6b543f", "#5f4936", "#66503b"],
        "grain_lo": [40, 30, 22],
        "grain_hi": [92, 74, 54],
        "grain_alpha": [0.20, 0.30],
        "line_gap": [3, 12],
        "line_width": [0.8, 2.2],
        "waviness": [2.0, 3.0],
        "fleck_count": 500,
        "fleck_rgba": [32, 24, 17, 0.05, 0.08],
        "roughness": 0.55,
        "scale_u": 250,
        "scale_v": 1000,
    },
    "bamboo": {
        "label": "Natural Bamboo",
        "base": ["#e3cf9e", "#ddc794", "#e0cb99"],
        "grain_lo": [170, 145, 95],
        "grain_hi": [195, 170, 120],
        "grain_alpha": [0.18, 0.15],
        "line_gap": [14, 4],
        "line_width": [0.8, 0.8],
        "waviness": [0.2, 0.4],
        "fleck_count": 350,
        "fleck_rgba": [150, 120, 70, 0.10, 0.12],
        "fleck_size": [8, 14, 2, 3],
        "roughness": 0.50,
        "scale_u": 250,
        "scale_v": 1000,
    },
    "maple": {
        "label": "Hard Maple",
        "base": ["#f0e7d4", "#ece1cb", "#eee4d0"],
        "grain_lo": [196, 180, 150],
        "grain_hi": [220, 205, 176],
        "grain_alpha": [0.10, 0.14],
        "line_gap": [4, 14],
        "line_width": [0.6, 1.4],
        "waviness": [2.0, 4.0],
        "fleck_count": 200,
        "fleck_rgba": [180, 162, 132, 0.03, 0.05],
        "roughness": 0.60,
        "scale_u": 250,
        "scale_v": 1000,
    },
    "baltic_birch": {
        "label": "Baltic Birch (WB urethane)",
        "base": ["#f3ecdb", "#efe6d2", "#f1e9d7"],
        "grain_lo": [205, 190, 160],
        "grain_hi": [228, 214, 186],
        "grain_alpha": [0.08, 0.10],
        "line_gap": [5, 16],
        "line_width": [0.6, 1.2],
        "waviness": [2.5, 4.0],
        "fleck_count": 120,
        "fleck_rgba": [186, 168, 138, 0.03, 0.04],
        "roughness": 0.45,
        "scale_u": 250,
        "scale_v": 1000,
    },
    "cherry": {
        "label": "American Cherry",
        "base": ["#b57a5a", "#aa7052", "#b17656"],
        "grain_lo": [118, 68, 46],
        "grain_hi": [160, 100, 70],
        "grain_alpha": [0.14, 0.20],
        "line_gap": [3, 10],
        "line_width": [0.6, 1.6],
        "waviness": [1.8, 3.0],
        "fleck_count": 400,
        "fleck_rgba": [96, 54, 36, 0.04, 0.06],
        "roughness": 0.58,
        "scale_u": 250,
        "scale_v": 1000,
    },
}


#: Applied to drawer-box meshes whenever a main ``finish`` is set and no
#: explicit ``drawer_box_finish`` is given — drawer boxes are almost always
#: built from Baltic birch ply regardless of the show-wood species.
DEFAULT_DRAWER_BOX_FINISH = "baltic_birch"


def _finish_params(finish: Optional[str]) -> Optional[dict]:
    """Resolve a finish key to its parameter dict, or None for the default
    flat vertex-colour rendering.  Raises ValueError on unknown keys."""
    if not finish or finish == "none":
        return None
    if finish not in WOOD_FINISHES:
        raise ValueError(
            f"Unknown finish {finish!r}. Available: {', '.join(sorted(WOOD_FINISHES))}"
        )
    return WOOD_FINISHES[finish]


def _grain_direction(value: Optional[str]) -> str:
    """Normalise/validate a grain direction ('vertical' when unset)."""
    if not value:
        return "vertical"
    if value not in ("vertical", "horizontal"):
        raise ValueError(
            f"Unknown grain_direction {value!r}. Use 'vertical' or 'horizontal'."
        )
    return value


# JavaScript for the procedural wood-grain finish.  Kept as a plain string
# (NOT part of the _build_html f-string) so its braces need no doubling; it is
# interpolated into the template as an opaque value.  Reads the FINISH const
# injected alongside it — a no-op when FINISH is null.
_FINISH_JS = """\
function makeGrainTexture(P) {
  const cvs = document.createElement('canvas');
  cvs.width = 512; cvs.height = 2048;
  const ctx = cvs.getContext('2d');
  const grad = ctx.createLinearGradient(0, 0, 512, 0);
  grad.addColorStop(0,   P.base[0]);
  grad.addColorStop(0.5, P.base[1]);
  grad.addColorStop(1,   P.base[2]);
  ctx.fillStyle = grad; ctx.fillRect(0, 0, 512, 2048);
  // Deterministic LCG so the grain is identical on every load.
  let seed = 42;
  const rnd  = () => (seed = (Math.imul(seed, 1103515245) + 12345) | 0, ((seed >>> 16) & 0x7fff) / 32768);
  const lerp = (a, b, t) => a + (b - a) * t;
  let x = 0;
  while (x < 512) {
    x += P.line_gap[0] + rnd() * P.line_gap[1];
    const t = rnd();
    const r = Math.round(lerp(P.grain_lo[0], P.grain_hi[0], t));
    const g = Math.round(lerp(P.grain_lo[1], P.grain_hi[1], t));
    const b = Math.round(lerp(P.grain_lo[2], P.grain_hi[2], t));
    const a = P.grain_alpha[0] + rnd() * P.grain_alpha[1];
    ctx.strokeStyle = `rgba(${r},${g},${b},${a.toFixed(3)})`;
    ctx.lineWidth = P.line_width[0] + rnd() * P.line_width[1];
    ctx.beginPath();
    let y = -20, gx = x;
    ctx.moveTo(gx, y);
    while (y < 2068) {
      y += 60 + rnd() * 80;
      gx = x + Math.sin(y * 0.004 + x) * (P.waviness[0] + rnd() * P.waviness[1]);
      ctx.lineTo(gx, y);
    }
    ctx.stroke();
  }
  // Flat-sawn cathedral figure: stacked parabolas opening down-canvas from
  // apexes spaced along a jittered centre line, over the straight background.
  if (P.pattern === 'cathedral') {
    const cx0 = 200 + rnd() * 112;
    let apex = -150;
    while (apex < 2048) {
      apex += P.arch_gap[0] + rnd() * P.arch_gap[1];
      const cx = cx0 + (rnd() - 0.5) * 50;
      const spread = P.arch_spread[0] + rnd() * P.arch_spread[1];
      const t = rnd();
      const r = Math.round(lerp(P.grain_lo[0], P.grain_hi[0], t));
      const g = Math.round(lerp(P.grain_lo[1], P.grain_hi[1], t));
      const b = Math.round(lerp(P.grain_lo[2], P.grain_hi[2], t));
      const a = P.grain_alpha[0] + rnd() * P.grain_alpha[1];
      ctx.strokeStyle = `rgba(${r},${g},${b},${a.toFixed(3)})`;
      ctx.lineWidth = P.line_width[0] + rnd() * P.line_width[1];
      ctx.beginPath();
      let started = false;
      for (let y = 2068; y >= apex; y -= 24) {
        const x = cx - spread * Math.sqrt(y - apex);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      }
      for (let y = apex; y <= 2068; y += 24) {
        ctx.lineTo(cx + spread * Math.sqrt(y - apex), y);
      }
      ctx.stroke();
    }
  }
  const [fr, fg, fb, fa0, fa1] = P.fleck_rgba;
  const FS = P.fleck_size || [0.8, 1.2, 3, 10];
  for (let i = 0; i < P.fleck_count; i++) {
    ctx.fillStyle = `rgba(${fr},${fg},${fb},${(fa0 + rnd() * fa1).toFixed(3)})`;
    ctx.fillRect(rnd() * 512, rnd() * 2048, FS[0] + rnd() * FS[1], FS[2] + rnd() * FS[3]);
  }
  const tex = new THREE.CanvasTexture(cvs);
  tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = renderer.capabilities.getMaxAnisotropy();
  return tex;
}

// The GLB meshes carry no UVs, so box-project them in the CadQuery local
// frame (X=width, Y=depth, Z=up — the root node's -90° X rotation maps
// this to GLTF Y-up).  v = along-grain.  P.grain_direction 'vertical'
// (default): grain runs up fronts and sides, across the width on tops.
// 'horizontal': grain runs across fronts, along the depth on sides and tops.
function boxUV(geo, P) {
  if (!geo.attributes.normal) geo.computeVertexNormals();
  const pos = geo.attributes.position, nrm = geo.attributes.normal;
  const uv = new Float32Array(pos.count * 2);
  const horiz = P.grain_direction === 'horizontal';
  for (let i = 0; i < pos.count; i++) {
    const nx = Math.abs(nrm.getX(i)), ny = Math.abs(nrm.getY(i)), nz = Math.abs(nrm.getZ(i));
    const px = pos.getX(i), py = pos.getY(i), pz = pos.getZ(i);
    let u, v;
    if (ny >= nx && ny >= nz)      { u = horiz ? pz : px; v = horiz ? px : pz; }
    else if (nx >= nz)             { u = horiz ? pz : py; v = horiz ? py : pz; }
    else                           { u = horiz ? px : py; v = horiz ? py : px; }
    uv[i * 2] = u / P.scale_u; uv[i * 2 + 1] = v / P.scale_v;
  }
  geo.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
}

// ── Interactive finish state ──────────────────────────────────────────────────
// classifyWood() buckets every wood mesh once at load; setShowFinish() can
// then re-texture the show surfaces live from the finish dropdown / grain
// toggle.  Drawer boxes take BOX_FINISH (Baltic birch by default) whenever
// any show finish is active; 'Flat colors' restores the original materials.
const woodShow = [];   // {mesh, orig} — carcass, drawer faces, doors
const woodBox  = [];   // {mesh, orig} — drawer-box panels
const texCache = {};
let currentFinishKey = INITIAL_FINISH;
let currentGrain     = INITIAL_GRAIN;
let boxesTextured    = false;
let boxTexture       = null;

const BOX_RE  = /^bay\\d+_drawer\\d+(?:_\\d+)?$/;
// Metal hardware — pull nodes (bay{i}_pull{j}_{k}, bay{i}_doorpull{j}_...) and
// adjustable feet (foot_{n}) — keeps its own material.  The pull check is
// anchored so a cabinet whose name merely contains 'pull' still gets its
// finish; the /_\\d+/ tolerates GLTFLoader dedup suffixes.
const HARDWARE_RE = /^(bay\\d+_(?:door)?pull\\d+|foot|worktop_leg\\d+)(?:_\\d+)*$/;
function classifyWood(root) {
  root.traverse(obj => {
    if (!obj.isMesh) return;
    // Manga scale-reference meshes keep their own drawn covers — never wood.
    const mangaLvl = mangaLevel(obj);
    if (mangaLvl !== null) { mangaMeshes.push({ mesh: obj, level: mangaLvl }); return; }
    // Drawer-box meshes live under a bay{i}_drawer{j} group (dedup tolerated).
    let isHardware = false, isBox = false;
    for (let d = 0, n = obj; d < 6 && n; d++, n = n.parent) {
      const nm = n.name || '';
      if (HARDWARE_RE.test(nm)) { isHardware = true; break; }
      if (BOX_RE.test(nm))      { isBox = true; break; }
    }
    if (isHardware) return;
    (isBox ? woodBox : woodShow).push({ mesh: obj, orig: obj.material });
  });
}

// Clone per mesh: box and carcass panels can share GLTF material instances,
// and mutating a shared material would leak one finish into the other.
function texturizeMesh(mesh, P, tex) {
  boxUV(mesh.geometry, P);
  const mk = (m) => {
    const c = m.clone();
    c.map = tex; c.color = new THREE.Color(0xffffff);
    c.vertexColors = false; c.roughness = P.roughness; c.metalness = 0.0;
    c.needsUpdate = true;
    return c;
  };
  mesh.material = Array.isArray(mesh.material)
    ? mesh.material.map(m => m ? mk(m) : m)
    : mk(mesh.material);
}

function texFor(key) {
  if (!texCache[key]) texCache[key] = makeGrainTexture(FINISHES[key]);
  return texCache[key];
}

function setShowFinish(key) {
  try {
    // Drop toggle state that caches materials — it would restore stale ones.
    if (xrayOn) toggleXray();
    if (diagOn) toggleDiagColors();
    for (const { mesh } of woodShow) xrayCache.delete(mesh);
    for (const { mesh } of woodBox)  xrayCache.delete(mesh);
    currentFinishKey = key || null;
    if (currentFinishKey) {
      if (!boxesTextured && BOX_FINISH) {
        boxTexture = boxTexture || makeGrainTexture(BOX_FINISH);
        for (const { mesh } of woodBox) texturizeMesh(mesh, BOX_FINISH, boxTexture);
        boxesTextured = true;
      }
      const P = Object.assign({}, FINISHES[currentFinishKey],
                              { grain_direction: currentGrain });
      const tex = texFor(currentFinishKey);
      for (const { mesh } of woodShow) texturizeMesh(mesh, P, tex);
    } else {
      for (const { mesh, orig } of woodShow) mesh.material = orig;
      for (const { mesh, orig } of woodBox)  mesh.material = orig;
      boxesTextured = false;
    }
    // Diag colors (V) must restore to what is on screen now.
    for (const e of diagMeshes) e.origMat = e.mesh.material;
    const sel = document.getElementById('finish-sel');
    if (sel && sel.value !== (currentFinishKey || '')) sel.value = currentFinishKey || '';
  } catch (e) {
    console.error('finish apply failed:', e);
  }
}

// ── Finish / grain / cutlist UI ───────────────────────────────────────────────
function cutlistRequestText() {
  const fin = currentFinishKey ? FINISHES[currentFinishKey].label : 'not selected';
  // Honour an explicit drawer_box_finish override; default copy names Baltic birch.
  const box = BOX_FINISH && BOX_FINISH.label
    ? BOX_FINISH.label + ' ply, water-based urethane'
    : 'Baltic birch ply, water-based urethane';
  return CUTLIST_PROMPT + ' Exterior finish: ' + fin + ', grain direction: ' +
         currentGrain + '. Drawer boxes: ' + box + '.';
}

function fallbackCopy(txt, done) {
  const ta = document.createElement('textarea');
  ta.value = txt; document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); done(); } catch (e) {}
  ta.remove();
}

function initFinishUI() {
  const sel = document.getElementById('finish-sel');
  const grainBtn = document.getElementById('grain-btn');
  if (!sel || !grainBtn) return;
  sel.add(new Option('Flat colors (none)', ''));
  for (const [k, p] of Object.entries(FINISHES)) sel.add(new Option(p.label, k));
  sel.value = INITIAL_FINISH || '';
  sel.onchange = () => setShowFinish(sel.value || null);
  grainBtn.textContent = 'Grain: ' + currentGrain;
  grainBtn.onclick = () => {
    currentGrain = currentGrain === 'vertical' ? 'horizontal' : 'vertical';
    grainBtn.textContent = 'Grain: ' + currentGrain;
    if (currentFinishKey) setShowFinish(currentFinishKey);
  };
  const modal = document.getElementById('cutlist-modal');
  document.getElementById('cutlist-btn').onclick = () => {
    document.getElementById('cutlist-text').textContent = cutlistRequestText();
    modal.classList.add('active');
  };
  document.getElementById('cutlist-close').onclick = () => modal.classList.remove('active');
  const copyBtn = document.getElementById('cutlist-copy');
  copyBtn.onclick = () => {
    const txt = document.getElementById('cutlist-text').textContent;
    const done = () => {
      copyBtn.textContent = 'Copied ✓';
      setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(done, () => fallbackCopy(txt, done));
    } else fallbackCopy(txt, done);
  };
}
initFinishUI();"""


# Manga scale-reference JS — plain (non-f-string) constant like _FINISH_JS, so
# braces need no doubling.  Meshes named manga{k} (see drawer.add_manga_stack)
# are collected by classifyWood, re-textured with drawn tankōbon covers, and
# cycled by the M key / side-panel button (1…5 volumes, then hidden).
_MANGA_JS = """\

// ── Manga scale reference (M toggle) ──────────────────────────────────────────
const MANGA_NODE_RE = /^manga(\\d+)(?:_\\d+)*$/;
const mangaMeshes = [];   // { mesh, level } — level = position in the stack
let mangaCount = 1;       // volumes shown per drawer; 0 = hidden

function mangaLevel(obj) {
  for (let d = 0, n = obj; d < 6 && n; d++, n = n.parent) {
    const m = MANGA_NODE_RE.exec(n.name || '');
    // Structural gate: real manga nodes are direct children of a
    // bay{i}_drawer{j} group. Without it, a cabinet legitimately named
    // "manga2" classified all its meshes as manga volumes and rendered
    // invisible at load.
    if (m && n.parent && BOX_RE.test(n.parent.name || '')) {
      return parseInt(m[1], 10);
    }
  }
  return null;
}

const MANGA_ACCENTS = ['#e8433a', '#2b6fd4', '#12a05a', '#f28c1b', '#8b46c8'];
const MANGA_FONT = '"Hiragino Kaku Gothic ProN", "Hiragino Sans", "Yu Gothic", "Noto Sans JP", sans-serif';
const mangaMatCache = {};
let mangaPagesMat = null;

function mangaTex(canvas) {
  const t = new THREE.CanvasTexture(canvas);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

// A generic, unmistakably-manga tankōbon cover: title band with katakana,
// big-eyed spiky-haired character, speed lines, screentone dots, volume badge.
function mangaCoverCanvas(level) {
  const W = 256, H = 384;
  const c = document.createElement('canvas'); c.width = W; c.height = H;
  const g = c.getContext('2d');
  const accent = MANGA_ACCENTS[level % MANGA_ACCENTS.length];

  g.fillStyle = '#f7f3e8'; g.fillRect(0, 0, W, H);

  // Speed lines radiating from the face
  const cx = W / 2, cy = H * 0.60;
  g.strokeStyle = 'rgba(35,35,35,0.5)'; g.lineWidth = 1.4;
  for (let i = 0; i < 46; i++) {
    const a = (i / 46) * Math.PI * 2;
    const r0 = 120 + 34 * ((i * 7) % 5) / 5;
    g.beginPath();
    g.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0 * 1.35);
    g.lineTo(cx + Math.cos(a) * 420, cy + Math.sin(a) * 420);
    g.stroke();
  }

  // Screentone dots, lower-left corner triangle
  g.fillStyle = 'rgba(60,60,60,0.4)';
  for (let y = H - 110; y < H - 8; y += 8)
    for (let x = 10; x < H - 26 - y + 110; x += 8) {
      g.beginPath(); g.arc(x + ((y >> 3) % 2) * 4, y, 1.7, 0, Math.PI * 2); g.fill();
    }

  // Character — spiky hair fan
  g.fillStyle = '#20242c';
  g.strokeStyle = '#20242c';
  for (let i = 0; i < 9; i++) {
    const a = Math.PI * (1.08 + 0.84 * i / 8);   // arc over the head
    const bx = cx + Math.cos(a) * 56, by = cy - 14 + Math.sin(a) * 60;
    const tx = cx + Math.cos(a) * 105, ty = cy - 20 + Math.sin(a) * 112;
    const px = Math.cos(a + Math.PI / 2) * 14, py = Math.sin(a + Math.PI / 2) * 14;
    g.beginPath();
    g.moveTo(bx - px, by - py); g.lineTo(tx, ty); g.lineTo(bx + px, by + py);
    g.closePath(); g.fill();
  }

  // Face
  g.fillStyle = '#ffe3c9'; g.strokeStyle = '#1c1c1c'; g.lineWidth = 5;
  g.beginPath(); g.ellipse(cx, cy, 60, 66, 0, 0, Math.PI * 2);
  g.fill(); g.stroke();

  // Bangs over the forehead
  g.fillStyle = '#20242c';
  g.beginPath(); g.moveTo(cx - 58, cy - 26);
  for (let i = 0; i <= 6; i++) {
    const x = cx - 58 + (116 * i / 6);
    g.lineTo(x + 9, cy - (i % 2 ? 6 : 30));
  }
  g.lineTo(cx + 58, cy - 26);
  g.quadraticCurveTo(cx, cy - 92, cx - 58, cy - 26);
  g.closePath(); g.fill();

  // Enormous sparkly eyes
  for (const s of [-1, 1]) {
    const ex = cx + s * 27, ey = cy + 12;
    g.fillStyle = '#fff'; g.strokeStyle = '#1c1c1c'; g.lineWidth = 4;
    g.beginPath(); g.ellipse(ex, ey, 20, 26, 0, 0, Math.PI * 2); g.fill(); g.stroke();
    g.fillStyle = accent;
    g.beginPath(); g.arc(ex, ey + 4, 14, 0, Math.PI * 2); g.fill();
    g.fillStyle = '#111';
    g.beginPath(); g.arc(ex, ey + 4, 7, 0, Math.PI * 2); g.fill();
    g.fillStyle = '#fff';
    g.beginPath(); g.arc(ex - 6, ey - 4, 6, 0, Math.PI * 2); g.fill();
    g.beginPath(); g.arc(ex + 5, ey + 11, 2.6, 0, Math.PI * 2); g.fill();
    // Heavy top lash
    g.strokeStyle = '#111'; g.lineWidth = 6;
    g.beginPath(); g.ellipse(ex, ey - 4, 21, 22, 0, Math.PI * 1.15, Math.PI * 1.85); g.stroke();
    // Eyebrow
    g.lineWidth = 4;
    g.beginPath(); g.moveTo(ex - 18, ey - 38); g.quadraticCurveTo(ex, ey - 46, ex + 18, ey - 40); g.stroke();
    // Cheek blush strokes
    g.strokeStyle = 'rgba(230,120,110,0.9)'; g.lineWidth = 2.5;
    for (let i = 0; i < 3; i++) {
      g.beginPath();
      g.moveTo(ex + s * (10 + i * 6) - 4, ey + 34);
      g.lineTo(ex + s * (10 + i * 6) + 4, ey + 26);
      g.stroke();
    }
  }

  // Tiny nose + open shonen grin
  g.strokeStyle = '#1c1c1c'; g.lineWidth = 3;
  g.beginPath(); g.moveTo(cx - 1, cy + 34); g.lineTo(cx + 3, cy + 38); g.stroke();
  g.fillStyle = '#fff'; g.strokeStyle = '#1c1c1c'; g.lineWidth = 4;
  g.beginPath(); g.ellipse(cx, cy + 50, 15, 9, 0, 0, Math.PI); g.fill(); g.stroke();

  // Title band with katakana (latin fallback keeps it readable without CJK fonts)
  g.fillStyle = accent; g.fillRect(0, 0, W, 86);
  g.fillStyle = '#111'; g.fillRect(0, 86, W, 6);
  g.fillStyle = '#fff';
  g.textAlign = 'center'; g.textBaseline = 'middle';
  g.font = '900 54px ' + MANGA_FONT;
  g.strokeStyle = '#111'; g.lineWidth = 7;
  g.strokeText('マンガ', W / 2, 40); g.fillText('マンガ', W / 2, 40);
  g.font = '700 17px ' + MANGA_FONT;
  g.fillText('M  A  N  G  A', W / 2, 72);

  // Volume badge
  g.beginPath(); g.arc(W - 36, H - 36, 25, 0, Math.PI * 2);
  g.fillStyle = '#fff'; g.fill();
  g.strokeStyle = accent; g.lineWidth = 6; g.stroke();
  g.fillStyle = '#111'; g.font = '900 30px ' + MANGA_FONT;
  g.fillText(String(level + 1), W - 36, H - 34);

  // Cover frame
  g.strokeStyle = '#111'; g.lineWidth = 8; g.strokeRect(0, 0, W, H);
  return c;
}

function mangaSpineCanvas(level) {
  const W = 384, H = 48;
  const c = document.createElement('canvas'); c.width = W; c.height = H;
  const g = c.getContext('2d');
  const accent = MANGA_ACCENTS[level % MANGA_ACCENTS.length];
  g.fillStyle = accent; g.fillRect(0, 0, W, H);
  g.fillStyle = '#fff'; g.fillRect(0, 0, 10, H); g.fillRect(W - 10, 0, 10, H);
  g.fillStyle = '#fff'; g.textAlign = 'center'; g.textBaseline = 'middle';
  g.font = '900 26px ' + MANGA_FONT;
  g.fillText('マンガ', W * 0.30, H / 2 + 1);
  g.font = '700 22px ' + MANGA_FONT;
  g.fillText('VOL. ' + (level + 1), W * 0.68, H / 2 + 1);
  g.strokeStyle = '#111'; g.lineWidth = 4; g.strokeRect(0, 0, W, H);
  return c;
}

function mangaPagesCanvas() {
  const c = document.createElement('canvas'); c.width = 64; c.height = 64;
  const g = c.getContext('2d');
  g.fillStyle = '#efe7d2'; g.fillRect(0, 0, 64, 64);
  g.strokeStyle = '#cfc4a8'; g.lineWidth = 1;
  for (let y = 1; y < 64; y += 3) {
    g.beginPath(); g.moveTo(0, y); g.lineTo(64, y); g.stroke();
  }
  return c;
}

function mangaMaterials(level) {
  if (!mangaMatCache[level]) {
    if (!mangaPagesMat) {
      mangaPagesMat = new THREE.MeshStandardMaterial({
        map: mangaTex(mangaPagesCanvas()), roughness: 0.9, metalness: 0.0 });
    }
    mangaMatCache[level] = [
      new THREE.MeshStandardMaterial({
        map: mangaTex(mangaCoverCanvas(level)), roughness: 0.5, metalness: 0.0 }),
      new THREE.MeshStandardMaterial({
        map: mangaTex(mangaSpineCanvas(level)), roughness: 0.5, metalness: 0.0 }),
      mangaPagesMat,
    ];
  }
  return mangaMatCache[level];
}

// Split the box into cover (+Z), spine/back (+X, −Z) and page-edge faces.
// The GLB export splits vertices per face (normals differ), so classifying
// per-vertex by normal is exact.
function setupMangaMesh(mesh, level) {
  const geo = mesh.geometry;
  const pos = geo.attributes.position, nor = geo.attributes.normal;
  const mats = mangaMaterials(level);
  if (!pos || !nor) { mesh.material = mats[0]; return; }
  geo.computeBoundingBox();
  const bb = geo.boundingBox;
  const sx = Math.max(bb.max.x - bb.min.x, 1e-6);
  const sy = Math.max(bb.max.y - bb.min.y, 1e-6);
  const sz = Math.max(bb.max.z - bb.min.z, 1e-6);
  const uv = new Float32Array(pos.count * 2);
  for (let i = 0; i < pos.count; i++) {
    const nx = nor.getX(i), nz = nor.getZ(i);
    const x = pos.getX(i), y = pos.getY(i), z = pos.getZ(i);
    let u, v;
    if (Math.abs(nz) >= 0.5)      { u = (x - bb.min.x) / sx; v = (y - bb.min.y) / sy; }
    else if (Math.abs(nx) >= 0.5) { u = (y - bb.min.y) / sy; v = (z - bb.min.z) / sz; }
    else                          { u = (x - bb.min.x) / sx; v = (z - bb.min.z) / sz; }
    uv[2 * i] = u; uv[2 * i + 1] = v;
  }
  geo.setAttribute('uv', new THREE.BufferAttribute(uv, 2));

  const idx = geo.index
    ? Array.from(geo.index.array)
    : Array.from({ length: pos.count }, (_, i) => i);
  const buckets = [[], [], []];   // cover, spine+back, pages
  for (let t = 0; t < idx.length; t += 3) {
    const i0 = idx[t];
    const nz = nor.getZ(i0), nx = nor.getX(i0);
    const b = nz >= 0.5 ? 0 : (nx >= 0.5 || nz <= -0.5) ? 1 : 2;
    buckets[b].push(idx[t], idx[t + 1], idx[t + 2]);
  }
  geo.setIndex(buckets[0].concat(buckets[1], buckets[2]));
  geo.clearGroups();
  let start = 0;
  buckets.forEach((b, mi) => {
    if (b.length) geo.addGroup(start, b.length, mi);
    start += b.length;
  });
  mesh.material = mats;
}

function applyMangaCount() {
  for (const mm of mangaMeshes) mm.mesh.visible = mm.level < mangaCount;
  const btn = document.getElementById('manga-btn');
  if (btn) btn.textContent = mangaCount === 0
    ? 'Manga: hidden'
    : 'Manga: ' + mangaCount + (mangaCount === 1 ? ' volume' : ' volumes');
}

function cycleManga() {
  if (!mangaMeshes.length) return;
  mangaCount = (mangaCount + 1) % 6;
  applyMangaCount();
}

function initManga() {
  const helpRow = document.getElementById('help-manga');
  if (!mangaMeshes.length) {
    if (helpRow) helpRow.remove();
    return;
  }
  for (const mm of mangaMeshes) setupMangaMesh(mm.mesh, mm.level);
  const ui = document.getElementById('finish-ui');
  if (ui) {
    const btn = document.createElement('button');
    btn.id = 'manga-btn';
    btn.title = 'Cycle the manga scale-reference stack (M)';
    btn.addEventListener('click', cycleManga);
    ui.appendChild(btn);
  }
  applyMangaCount();
}"""


# ── Public API ────────────────────────────────────────────────────────────────

def export_glb(
    assy: "cq.Assembly",
    output_path: "Path | str",
    tolerance: float = 0.1,
    angular_tolerance: float = 0.1,
) -> Path:
    """Export a CadQuery Assembly to GLB (binary GLTF).

    The output format is determined by the ``.glb`` extension.
    Colors assigned in the assembly are preserved as vertex colours.

    Args:
        assy: The CadQuery Assembly to export.
        output_path: Destination file (must end in ``.glb``).
        tolerance: Linear deflection tolerance for mesh tessellation (mm).
            Smaller = finer mesh, larger file.  0.05–0.2 is a good range.
        angular_tolerance: Angular deflection in radians.

    Returns:
        Resolved ``Path`` to the written file.
    """
    _require_cq()
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    assy.save(str(output_path), tolerance=tolerance, angularTolerance=angular_tolerance)
    return output_path


def generate_viewer_html(
    glb_path: "Path | str",
    output_html: "Path | str",
    title: str = "Cabinet Viewer",
    cabinet_info: Optional[dict] = None,
    finish: Optional[str] = None,
    drawer_box_finish: Optional[str] = None,
    grain_direction: str = "vertical",
    cutlist_prompt: Optional[str] = None,
) -> Path:
    """Generate a self-contained Three.js HTML viewer with the GLB embedded.

    The binary GLB data is base64-encoded and inlined as a JavaScript string,
    so no local server is needed to open the file.  Three.js loads from the
    jsDelivr CDN, so viewing does require an internet connection.

    Args:
        glb_path: Path to the source ``.glb`` file.
        output_html: Destination ``.html`` file.
        title: Title shown in the browser tab and info panel.
        cabinet_info: Optional dict with display info.  Recognised keys:
            ``width``, ``height``, ``depth`` (all in mm), plus any arbitrary
            string keys whose values will be displayed verbatim.
        finish: Optional wood finish key (see ``WOOD_FINISHES``).  When set,
            the viewer textures the carcass, drawer faces, and doors with a
            procedural grain instead of the flat vertex colours.
        drawer_box_finish: Finish for drawer-box meshes.  Defaults to
            ``baltic_birch`` whenever ``finish`` is set; pass the same key
            as ``finish`` for a uniform look.
        grain_direction: 'vertical' (default) or 'horizontal' — orients the
            show-surface grain; drawer boxes are always horizontal.
        cutlist_prompt: Request text behind the viewer's "Generate cutlist"
            button (the finish/grain selection is appended live).

    Returns:
        Resolved ``Path`` to the written HTML file.
    """
    glb_path = Path(glb_path).expanduser().resolve()
    output_html = Path(output_html).expanduser().resolve()
    output_html.parent.mkdir(parents=True, exist_ok=True)

    glb_b64 = base64.b64encode(glb_path.read_bytes()).decode("ascii")
    html = _build_html(
        title, glb_b64, cabinet_info or {},
        finish=finish, drawer_box_finish=drawer_box_finish,
        grain_direction=grain_direction, cutlist_prompt=cutlist_prompt,
    )
    output_html.write_text(html, encoding="utf-8")
    return output_html


def visualize_assembly(
    assy: "cq.Assembly",
    parts: list,
    output_dir: "Path | str" = "~/.cabineteer/visualizations",
    name: str = "cabinet",
    open_browser: bool = True,
    tolerance: float = 0.1,
    angular_tolerance: float = 0.1,
    info: Optional[dict] = None,
    finish: Optional[str] = None,
    drawer_box_finish: Optional[str] = None,
    grain_direction: str = "vertical",
    cutlist_prompt: Optional[str] = None,
) -> dict:
    """Export a pre-built CadQuery Assembly to GLB and generate an HTML viewer.

    Use this when you have already called a builder function (e.g.
    ``build_multi_bay_cabinet``) and just need the export step.

    Args:
        assy:        Pre-built CadQuery Assembly.
        parts:       Parts list (used for part count).
        output_dir:  Directory for output files.
        name:        Base filename stem.
        open_browser: If True, open the HTML file in the default browser.
        tolerance:   Mesh tessellation tolerance in mm.
        angular_tolerance: Angular tessellation tolerance in radians.
        info:        Optional dict of key/value pairs shown in the info panel.
        finish:      Optional wood finish key (see ``WOOD_FINISHES``) for the
            carcass, drawer faces, and doors.
        drawer_box_finish: Finish for drawer-box meshes; defaults to
            ``baltic_birch`` whenever ``finish`` is set.
        grain_direction: 'vertical' (default) or 'horizontal' for the
            show-surface grain; drawer boxes are always horizontal.

    Returns:
        Dict with keys ``glb``, ``html``, ``parts``, ``glb_size_kb``.
    """
    _require_cq()
    # Validate everything before the slow export.
    name = _validate_name(name)
    finish_params = _finish_params(finish)
    grain_direction = _grain_direction(grain_direction)
    if finish and drawer_box_finish is None:
        drawer_box_finish = DEFAULT_DRAWER_BOX_FINISH
    box_params = _finish_params(drawer_box_finish)

    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    glb_path  = output_dir / f"{name}.glb"
    html_path = output_dir / f"{name}_viewer.html"

    export_glb(assy, glb_path, tolerance=tolerance, angular_tolerance=angular_tolerance)

    panel_info = dict(info) if info else {}
    panel_info.setdefault("parts", len(parts))
    if finish_params:
        panel_info.setdefault("finish", finish_params["label"])
    if box_params and box_params is not finish_params:
        panel_info.setdefault("drawer_boxes", box_params["label"])
    if finish_params and grain_direction != "vertical":
        panel_info.setdefault("grain", grain_direction)

    generate_viewer_html(
        glb_path,
        html_path,
        title=name.replace("_", " ").title(),
        cabinet_info=panel_info,
        finish=finish,
        drawer_box_finish=drawer_box_finish,
        grain_direction=grain_direction,
        cutlist_prompt=cutlist_prompt,
    )

    if open_browser:
        webbrowser.open(html_path.as_uri())

    return {
        "glb":         str(glb_path),
        "html":        str(html_path),
        "parts":       len(parts),
        "glb_size_kb": round(glb_path.stat().st_size / 1024, 1),
    }


def build_and_visualize(
    cfg: "CabinetConfig",  # type: ignore[name-defined]
    output_dir: "Path | str" = "~/.cabineteer/visualizations",
    name: str = "cabinet",
    open_browser: bool = True,
    tolerance: float = 0.1,
    angular_tolerance: float = 0.1,
    finish: Optional[str] = None,
    drawer_box_finish: Optional[str] = None,
    grain_direction: str = "vertical",
) -> dict:
    """Build a full cabinet assembly, export GLB, and generate the HTML viewer.

    Args:
        cfg: Cabinet configuration (``CabinetConfig`` instance).
        output_dir: Directory for output files (created if absent).
            Defaults to ``~/.cabineteer/visualizations``.
        name: Base filename stem (e.g. ``"kitchen_base"`` →
            ``kitchen_base.glb`` + ``kitchen_base_viewer.html``).
        open_browser: If ``True``, open the HTML file in the default browser.
        tolerance: Mesh tessellation tolerance in mm (lower = finer, bigger).
        angular_tolerance: Angular tessellation tolerance in radians.
        finish: Optional wood finish key (see ``WOOD_FINISHES``) for the
            carcass, drawer faces, and doors.
        drawer_box_finish: Finish for drawer-box meshes; defaults to
            ``baltic_birch`` whenever ``finish`` is set.
        grain_direction: 'vertical' (default) or 'horizontal' for the
            show-surface grain; drawer boxes are always horizontal.

    Returns:
        Dict with keys:
        - ``"glb"``   — absolute path to the exported GLB file
        - ``"html"``  — absolute path to the generated HTML viewer
        - ``"parts"`` — number of parts in the assembly
        - ``"glb_size_kb"`` — GLB file size in KB
    """
    _require_cq()
    from .cabinet import build_cabinet

    # Validate everything before the slow build.
    name = _validate_name(name)
    finish_params = _finish_params(finish)
    grain_direction = _grain_direction(grain_direction)
    if finish and drawer_box_finish is None:
        drawer_box_finish = DEFAULT_DRAWER_BOX_FINISH
    box_params = _finish_params(drawer_box_finish)

    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    assy, parts = build_cabinet(cfg)

    glb_path = output_dir / f"{name}.glb"
    html_path = output_dir / f"{name}_viewer.html"

    export_glb(assy, glb_path, tolerance=tolerance, angular_tolerance=angular_tolerance)

    cabinet_info = {
        "width":  cfg.width,
        "height": cfg.height,
        "depth":  cfg.depth,
        "parts":  len(parts),
    }
    if cfg.openings:
        cabinet_info["openings"] = len(cfg.openings)
    if finish_params:
        cabinet_info["finish"] = finish_params["label"]
    if box_params and box_params is not finish_params:
        cabinet_info["drawer_boxes"] = box_params["label"]
    if finish_params and grain_direction != "vertical":
        cabinet_info["grain"] = grain_direction

    generate_viewer_html(
        glb_path,
        html_path,
        title=name.replace("_", " ").title(),
        cabinet_info=cabinet_info,
        finish=finish,
        drawer_box_finish=drawer_box_finish,
        grain_direction=grain_direction,
        cutlist_prompt=f"Generate the cutlist for cabinet '{name}'.",
    )

    if open_browser:
        webbrowser.open(html_path.as_uri())

    return {
        "glb":         str(glb_path),
        "html":        str(html_path),
        "parts":       len(parts),
        "glb_size_kb": round(glb_path.stat().st_size / 1024, 1),
    }


# ── HTML builder ──────────────────────────────────────────────────────────────

def _build_html(
    title: str,
    glb_b64: str,
    info: dict,
    finish: Optional[str] = None,
    drawer_box_finish: Optional[str] = None,
    grain_direction: str = "vertical",
    cutlist_prompt: Optional[str] = None,
) -> str:
    """Construct the self-contained HTML viewer string.

    Uses Three.js r165 via importmap from the jsDelivr CDN.  The GLB data
    is embedded verbatim as a base64 string constant.  The full
    ``WOOD_FINISHES`` catalogue is embedded so the viewer's dropdown can
    re-texture the show surfaces (carcass, drawer faces, doors) live;
    ``finish`` and ``grain_direction`` only set the initial selection.
    Drawer-box meshes take ``drawer_box_finish`` (default ``baltic_birch``,
    always horizontal grain) whenever any show finish is active.
    ``cutlist_prompt`` seeds the "Generate cutlist" button's copyable
    request text.
    """
    grain_direction = _grain_direction(grain_direction)
    params = _finish_params(finish)  # validates the initial key
    if drawer_box_finish is None:
        drawer_box_finish = DEFAULT_DRAWER_BOX_FINISH
    box_params = _finish_params(drawer_box_finish)
    # json.dumps does not escape '/', so a literal "</script>" in any embedded
    # value would terminate the <script> element (the HTML parser ends the
    # element at the first "</script" regardless of JS string context) and
    # inject the remainder as markup.  Escape "</" → "<\/" (a valid JS string
    # escape) in every embed so no value can break out of the script.
    def _js_str(obj) -> str:
        # "</" blocks </script> breakout; "<!--" blocks the script-data
        # double-escaped parser state (an embedded "<!--<script>" would stop
        # the real </script> from closing the block).
        return json.dumps(obj).replace("</", "<\\/").replace("<!--", "<\\u0021--")

    finishes_json = _js_str(WOOD_FINISHES)
    initial_finish_json = _js_str(finish if params else None)
    initial_grain_json = _js_str(grain_direction)
    box_finish_json = _js_str(
        {**box_params, "grain_direction": "horizontal"} if box_params else None
    )
    cutlist_prompt_json = _js_str(
        cutlist_prompt or "Generate the cutlist for this design."
    )
    finish_js = _FINISH_JS
    manga_js = _MANGA_JS

    # Build info panel rows.  Title and info values are interpolated straight
    # into the HTML, so escape them to prevent markup injection / breakage.
    safe_title = html_escape(title)
    # A caveat that only reaches the tool's JSON does not reach the person
    # approving the picture. `render_caveats` is the whole justification for
    # leaving the mitered-corner divergence unfixed, and approval-from-the-
    # viewer is precisely the failure it exists to prevent — the furniture_top
    # cap "existed in approved renders and never on any cutlist" and cost a
    # batch of paper. So it goes on the page, not only in the result.
    caveats = [c for c in (info.pop("render_caveats", None) or []) if c]
    info_rows: list[str] = []
    if "width" in info:
        info_rows.append(f'<div class="row">W <span>{info["width"]:.0f} mm</span></div>')
    if "height" in info:
        info_rows.append(f'<div class="row">H <span>{info["height"]:.0f} mm</span></div>')
    if "depth" in info:
        info_rows.append(f'<div class="row">D <span>{info["depth"]:.0f} mm</span></div>')
    for k, v in info.items():
        if k not in ("width", "height", "depth"):
            label = html_escape(k.replace("_", " ").capitalize())
            info_rows.append(
                f'<div class="row">{label} <span>{html_escape(str(v))}</span></div>'
            )
    info_html = "\n    ".join(info_rows)
    caveat_html = ""
    if caveats:
        items = "\n      ".join(
            f"<li>{html_escape(c)}</li>" for c in caveats)
        caveat_html = (
            '<div id="caveats"><b>This render does not show everything the '
            'cutlist cuts</b><ul>\n      ' + items + "\n    </ul></div>")

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title}</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ background: #f3f5f8; overflow: hidden; font-family: system-ui, -apple-system, sans-serif; }}
    canvas {{ display: block; }}

    #panel {{
      position: absolute; top: 16px; left: 16px;
      background: rgba(10, 10, 25, 0.72);
      color: #ccc;
      padding: 14px 18px; border-radius: 10px;
      font-size: 13px; line-height: 1.0;
      backdrop-filter: blur(8px);
      border: 1px solid rgba(255, 255, 255, 0.09);
      min-width: 170px;
      user-select: none;
    }}
    #panel h2 {{
      font-size: 14px; font-weight: 600; color: #fff;
      margin-bottom: 10px; letter-spacing: 0.02em;
    }}
    .row {{
      display: flex; justify-content: space-between;
      gap: 16px; padding: 3px 0;
      color: #999; font-size: 12px;
    }}
    .row span {{ color: #f0c060; font-weight: 500; }}

    #help {{
      position: absolute; bottom: 16px; right: 20px;
      color: rgba(255, 255, 255, 0.28);
      font-size: 11px; text-align: right; line-height: 1.9;
      user-select: none;
    }}

    #loading {{
      position: absolute; top: 50%; left: 50%;
      transform: translate(-50%, -50%);
      color: #666; font-size: 15px; letter-spacing: 0.06em;
    }}

    #clip-ui {{
      position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%);
      display: none; flex-direction: column; align-items: center; gap: 6px;
      background: rgba(10, 10, 25, 0.72); backdrop-filter: blur(8px);
      border: 1px solid rgba(255,255,255,0.09); border-radius: 10px;
      padding: 10px 18px; user-select: none;
    }}
    #clip-ui.active {{ display: flex; }}
    #clip-label {{
      color: rgba(240,192,96,0.85); font-size: 11px; letter-spacing: 0.06em;
    }}
    #clip-axis-btns {{ display: flex; gap: 8px; }}
    #clip-axis-btns button {{
      background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.15);
      color: #ccc; border-radius: 5px; padding: 2px 10px; font-size: 11px; cursor: pointer;
    }}
    #clip-axis-btns button.sel {{
      background: rgba(240,192,96,0.22); border-color: rgba(240,192,96,0.55); color: #f0c060;
    }}
    #clip-range {{
      width: 260px; accent-color: #f0c060;
    }}

    #finish-ui {{
      margin-top: 12px; display: flex; flex-direction: column; gap: 6px;
      border-top: 1px solid rgba(255,255,255,0.09); padding-top: 10px;
    }}
    #finish-ui select, #finish-ui button {{
      background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.15);
      color: #ccc; border-radius: 5px; padding: 4px 8px; font-size: 11px; cursor: pointer;
      font-family: inherit;
    }}
    #finish-ui select option {{ background: #1c1c30; }}
    #cutlist-btn {{
      background: rgba(240,192,96,0.22) !important;
      border-color: rgba(240,192,96,0.55) !important;
      color: #f0c060 !important;
    }}

    #cutlist-modal {{
      position: absolute; inset: 0; display: none;
      align-items: center; justify-content: center;
      background: rgba(0,0,0,0.5); z-index: 10;
    }}
    #cutlist-modal.active {{ display: flex; }}
    #cutlist-card {{
      background: #1c1c30; border: 1px solid rgba(255,255,255,0.15);
      border-radius: 10px; padding: 18px 20px; max-width: 480px;
      color: #ccc; font-size: 13px; line-height: 1.5;
    }}
    #cutlist-card pre {{
      white-space: pre-wrap; background: rgba(255,255,255,0.05);
      padding: 10px; border-radius: 6px; margin: 10px 0; font-size: 12px;
      font-family: inherit;
    }}
    #cutlist-card .actions {{ display: flex; gap: 8px; justify-content: flex-end; }}
    #cutlist-card button {{
      background: rgba(240,192,96,0.22); border: 1px solid rgba(240,192,96,0.55);
      color: #f0c060; border-radius: 5px; padding: 4px 12px; cursor: pointer;
      font-size: 12px; font-family: inherit;
    }}

    #caveats {{
      margin-top: 10px; padding: 8px 10px; border-radius: 6px;
      background: rgba(190, 120, 30, 0.22);
      border-left: 3px solid #e0913a;
      color: #f0d9b8; font-size: 11px; line-height: 1.35;
      max-width: 320px;
    }}
    #caveats b {{ display: block; margin-bottom: 4px; color: #ffcf90; }}
    #caveats ul {{ margin: 0; padding-left: 16px; }}
    #caveats li {{ margin-bottom: 3px; }}
  </style>
</head>
<body>
  <div id="loading">Loading model…</div>

  <div id="panel">
    <h2>{safe_title}</h2>
    {info_html}
    {caveat_html}
    <div id="finish-ui">
      <select id="finish-sel" title="Exterior finish (drawer boxes stay Baltic birch)"></select>
      <button id="grain-btn" title="Toggle show-surface grain orientation">Grain: vertical</button>
      <button id="cutlist-btn" title="Copy a cutlist request for your assistant">Generate cutlist &rarr;</button>
    </div>
  </div>

  <div id="cutlist-modal">
    <div id="cutlist-card">
      <b>Generate cutlist</b>
      <div style="margin-top:6px; color:#999;">
        This viewer is a standalone file, so it can't run the tool itself.
        Copy this request and paste it to your assistant / MCP client:
      </div>
      <pre id="cutlist-text"></pre>
      <div class="actions">
        <button id="cutlist-copy">Copy</button>
        <button id="cutlist-close">Close</button>
      </div>
    </div>
  </div>

  <div id="help">
    Left-drag&nbsp;&nbsp;rotate<br>
    Right-drag&nbsp;&nbsp;pan<br>
    Scroll&nbsp;&nbsp;zoom<br>
    <span style="color: rgba(240, 192, 96, 0.55);">X</span>&nbsp;&nbsp;x-ray fronts<br>
    <span style="color: rgba(240, 192, 96, 0.55);">O</span>&nbsp;&nbsp;drawers: closed → staggered → open<br>
    <span style="color: rgba(240, 192, 96, 0.55);">C</span>&nbsp;&nbsp;clip plane<br>
    <span style="color: rgba(240, 192, 96, 0.55);">V</span>&nbsp;&nbsp;diag colors<span id="help-manga"><br>
    <span style="color: rgba(240, 192, 96, 0.55);">M</span>&nbsp;&nbsp;manga stack</span>
  </div>

  <div id="clip-ui">
    <div id="clip-label">CLIP PLANE</div>
    <div id="clip-axis-btns">
      <button id="btn-x" onclick="setClipAxis('x')">X</button>
      <button id="btn-y" onclick="setClipAxis('y')">Y  depth</button>
      <button id="btn-z" class="sel" onclick="setClipAxis('z')">Z  height</button>
    </div>
    <input id="clip-range" type="range" min="0" max="100" step="0.1" value="50"
           oninput="updateClipPlane()">
    <div id="clip-pos" style="color:#f0c060;font-size:11px;font-variant-numeric:tabular-nums;letter-spacing:0.04em;">— mm</div>
  </div>

  <script type="importmap">
  {{
    "imports": {{
      "three":          "https://cdn.jsdelivr.net/npm/three@0.165.0/build/three.module.js",
      "three/addons/":  "https://cdn.jsdelivr.net/npm/three@0.165.0/examples/jsm/"
    }}
  }}
  </script>

  <script type="module">
import * as THREE from 'three';
import {{ GLTFLoader }}    from 'three/addons/loaders/GLTFLoader.js';
import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';

// ── Embedded GLB (base64) ─────────────────────────────────────────────────────
const GLB_B64 = `{glb_b64}`;

function b64ToBuffer(b64) {{
  const bin = atob(b64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}}

// ── Wood finishes (see WOOD_FINISHES in visualize.py) ─────────────────────────
const FINISHES = {finishes_json};
const INITIAL_FINISH = {initial_finish_json};
const INITIAL_GRAIN = {initial_grain_json};
const BOX_FINISH = {box_finish_json};
const CUTLIST_PROMPT = {cutlist_prompt_json};
{finish_js}
{manga_js}

// ── Renderer ──────────────────────────────────────────────────────────────────
const renderer = new THREE.WebGLRenderer({{ antialias: true }});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.15;
document.body.appendChild(renderer.domElement);

// ── Scene ─────────────────────────────────────────────────────────────────────
const scene = new THREE.Scene();
// Light "graph paper" backdrop — a soft off-white so wood finishes read
// like a product catalogue rather than a dark technical viewer.
scene.background = new THREE.Color(0xf3f5f8);

// ── Camera ────────────────────────────────────────────────────────────────────
const camera = new THREE.PerspectiveCamera(
  40, window.innerWidth / window.innerHeight, 1, 50000
);

// ── Lights ────────────────────────────────────────────────────────────────────
scene.add(new THREE.AmbientLight(0xfff5ee, 0.65));

const key = new THREE.DirectionalLight(0xfff0cc, 1.9);
key.position.set(1000, 1600, 800);
key.castShadow = true;
key.shadow.mapSize.set(2048, 2048);
key.shadow.camera.near = 10;
key.shadow.camera.far  = 12000;
key.shadow.camera.left = key.shadow.camera.bottom = -2500;
key.shadow.camera.right = key.shadow.camera.top   =  2500;
scene.add(key);

const fill = new THREE.DirectionalLight(0xc0d0ff, 0.55);
fill.position.set(-800, 400, -600);
scene.add(fill);

const rim = new THREE.DirectionalLight(0xffe0a0, 0.35);
rim.position.set(0, -200, -1000);
scene.add(rim);

// ── Grid ──────────────────────────────────────────────────────────────────────
// Graph-paper grid: a slightly stronger blue-grey major line + soft
// minor lines on the off-white ground.
const grid = new THREE.GridHelper(5000, 50, 0x9db4d0, 0xccd6e4);
scene.add(grid);

// ── Controls ──────────────────────────────────────────────────────────────────
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping    = true;
controls.dampingFactor    = 0.055;
controls.screenSpacePanning = true;
controls.minDistance      = 50;
controls.maxDistance      = 20000;
controls.maxPolarAngle    = Math.PI / 2 + 0.18;

// Automation/debug hook: the page is an ES module, so scene/camera/controls
// are unreachable from outside without this. Used by
// scripts/readme_screenshots.py to frame shots deterministically.
window.cabineteerViewer = {{
  scene, camera, controls, renderer, THREE,
  // Lazy getters — drawerPairs/openState are defined later in the load
  // callback but the closures resolve them at call time.
  openState: () => (typeof openState !== 'undefined') ? openState : null,
  drawerFractions: () => (typeof drawerPairs !== 'undefined')
    ? [...drawerPairs.values()].map(p => p._applied || 0) : [],
  // Set the drawer state directly (0 closed · 1 staggered · 2 open) —
  // deterministic for scripted captures, vs. counting O presses.
  setDrawerState: (n) => {{ openState = ((n % 3) + 3) % 3;
                            setDrawersOpen(openState); }},
}};

// Suppress the browser context menu so right-drag pan works.
renderer.domElement.addEventListener('contextmenu', e => e.preventDefault());

// ── Load model ────────────────────────────────────────────────────────────────
// Drawer pair bookkeeping for the X-ray & Open toggles.
// Names come from cabinet.py:  "bay{{i}}_drawer{{j}}" (box)  +  "bay{{i}}_face{{j}}" (front).
const drawerFronts  = [];
const pullMeshes    = [];
const doorFaces     = [];   // bay{{i}}_door{{j}} and bay{{i}}_door{{j}}_{{k}}
const doorPullMeshes = [];  // bay{{i}}_doorpull{{j}}_...
const drawerPairs  = new Map();       // key "i_j" → {{ box, face, pullVec }}

function _pairFor(key) {{
  if (!drawerPairs.has(key)) drawerPairs.set(key, {{}});
  return drawerPairs.get(key);
}}

let cabinetRoot = null;
new GLTFLoader().parse(b64ToBuffer(GLB_B64), '', (gltf) => {{
  const model = gltf.scene;
  cabinetRoot = model;

  model.traverse(obj => {{
    if (!obj.isMesh) return;
    obj.castShadow    = true;
    obj.receiveShadow = true;
    const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
    mats.forEach(m => {{
      if (!m) return;
      // Preserve CadQuery vertex colours; add realistic PBR properties.
      m.roughness  = 0.72;
      m.metalness  = 0.0;
    }});

    // Bucket drawer meshes by (bay, slot) for the X-ray + Open toggles.
    // Three.js GLTFLoader wraps multi-primitive meshes in a Group, adding one
    // extra level vs the GLTF JSON: leaf mesh → _part Group → panel Group →
    // bay{{i}}_drawer{{j}} Group.  Search 4 levels to cover all node types.
    const p1 = obj.parent;
    const p2 = p1 ? p1.parent : null;
    const p3 = p2 ? p2.parent : null;
    const searchNames = [obj.name, p1 ? p1.name : '', p2 ? p2.name : '', p3 ? p3.name : ''];
    const searchNodes = [obj, p1, p2, p3];
    for (let si = 0; si < searchNames.length; si++) {{
      const nm = searchNames[si];
      if (!nm) continue;

      // Drawer face (bay_i_face_j) or box (bay_i_drawer_j) — $ ensures we
      // match the group node name exactly, not leaf mesh names like
      // bay0_face0_part_0.  In multi-cabinet project scenes every cabinet
      // reuses the same node names, so GLTFLoader dedupes the repeats by
      // appending _1, _2, … — the optional (?:_\\d+)? accepts those.  Pair
      // keys are scoped by the parent cabinet node's uuid (face, box, and
      // pull groups are all siblings under the same cabinet node) so
      // same-named drawers in different cabinets stay independent.
      const dm = nm.match(/^bay(\\d+)_(face|drawer)(\\d+)(?:_\\d+)?$/);
      if (dm) {{
        const grp = searchNodes[si];
        const key = (grp.parent ? grp.parent.uuid : '') + '|' + dm[1] + '_' + dm[3];
        const pair = _pairFor(key);
        if (dm[2] === 'face') {{
          pair.face = grp;              // group whose name matched — moves whole face
          drawerFronts.push(obj);       // mesh ref kept for x-ray material swap
        }} else {{
          // Store the group whose name matched so position.add() moves all
          // child meshes together.
          if (!pair.box) pair.box = grp;
        }}
        break;
      }}

      // Pull hardware (bay_i_pull_j_k) — anchored to the group node (leaf
      // meshes are bay0_pull0_0_part_0) so the pair key uses the same parent
      // uuid as the drawer face; the trailing (?:_\\d+)? absorbs either the
      // per-pull index k or a GLTFLoader dedup suffix — the key only needs
      // the drawer index j either way.
      const pm = nm.match(/^bay(\\d+)_pull(\\d+)_\\d+(?:_\\d+)?$/);
      if (pm) {{
        const grp = searchNodes[si];
        const key = (grp.parent ? grp.parent.uuid : '') + '|' + pm[1] + '_' + pm[2];
        const pair = _pairFor(key);
        if (!pair.pulls) pair.pulls = [];
        if (!pair.pulls.includes(grp)) pair.pulls.push(grp);
        pullMeshes.push(obj);  // leaf mesh ref kept for the x-ray toggle
        break;
      }}

      // Door faces (bay_i_door_j or bay_i_door_j_k — single door or pair
      // leaf, plus an optional GLTFLoader dedup suffix)
      if (/^bay\\d+_door\\d+(_\\d+){{0,2}}$/.test(nm)) {{
        doorFaces.push(obj);
        break;
      }}

      // Door pull hardware (bay_i_doorpull_j_...)
      if (/^bay\\d+_doorpull\\d+/.test(nm)) {{
        doorPullMeshes.push(obj);
        break;
      }}
    }}
  }});

  // Compute each drawer's pull vector in CadQuery local space so that
  // position.add() works correctly.  The root node has a -90° X rotation
  // (CadQuery Z-up → GLTF Y-up); world-space direction vectors do not match
  // the local-space positions we are adding to.  Drawers always open in the
  // local -Y direction (CadQuery depth axis / cabinet front).
  // After the -90° X rotation, local Y ↔ world Z, so world Z extent == local Y depth.
  for (const pair of drawerPairs.values()) {{
    if (!pair.box || !pair.face) continue;
    const dir    = new THREE.Vector3(0, -1, 0);          // local -Y = pull out
    const boxBB  = new THREE.Box3().setFromObject(pair.box);
    const depth  = boxBB.getSize(new THREE.Vector3()).z;  // world Z = local Y
    pair.pullVec = dir.multiplyScalar(depth * 0.70);
  }}

  // Per-drawer fraction for the partial ("staggered") open state: a
  // vertical GRADIENT by drawer height — bottom drawer fully out (100%),
  // top drawer barely (20%), linear between. Reads as an intentional
  // bottom-heavy cascade at any layout (world Y is vertical after the
  // root's -90° X rotation).
  {{
    const ps = [...drawerPairs.values()]
      .filter(p => p.pullVec && p.box && p.face);
    let lo = Infinity, hi = -Infinity;
    for (const p of ps) {{
      const bb = new THREE.Box3().setFromObject(p.box);
      p._cy = (bb.min.y + bb.max.y) / 2;   // world Y = vertical centre
      lo = Math.min(lo, p._cy); hi = Math.max(hi, p._cy);
    }}
    const span = hi - lo;
    for (const p of ps) {{
      const t = span > 1e-6 ? (p._cy - lo) / span : 0;   // 0 bottom … 1 top
      p.stagger = 1.0 - 0.8 * t;                          // 1.0 … 0.2
    }}
  }}

  classifyWood(model);
  initManga();
  // Apply the current selection (initialised to INITIAL_FINISH) rather than
  // INITIAL_FINISH directly, so a finish picked while the model was still
  // parsing is honoured instead of reverted.
  if (currentFinishKey) setShowFinish(currentFinishKey);
  scene.add(model);

  // Sit model on grid and centre camera
  const box  = new THREE.Box3().setFromObject(model);
  const size = box.getSize(new THREE.Vector3());
  model.position.y -= box.min.y;          // floor it
  box.setFromObject(model);
  const centre = box.getCenter(new THREE.Vector3());

  const maxDim = Math.max(size.x, size.y, size.z);
  const fov    = camera.fov * (Math.PI / 180);
  const dist   = (maxDim / (2 * Math.tan(fov / 2))) * 2.0;

  camera.position.set(
    centre.x + dist * 0.75,
    centre.y + dist * 0.55,
    centre.z + dist * 0.75
  );
  camera.lookAt(centre);
  controls.target.copy(centre);
  controls.update();

  initDiagColors();
  document.getElementById('loading').style.display = 'none';

}}, err => {{
  const el = document.getElementById('loading');
  el.textContent = 'Error loading model: ' + (err.message || err);
  el.style.color = '#f06060';
  console.error(err);
}});

// ── Toggles (X = x-ray fronts, O = cycle drawer open state) ───────────────────
let xrayOn       = false;
// 0 = closed · 1 = staggered (partial cascade) · 2 = fully open. O cycles
// 0 → 1 → 2 → 0.
let openState    = 0;
const xrayCache  = new WeakMap();    // mesh → {{ orig, xray }}

function _makeXrayMaterial(src) {{
  const x = src.clone();
  x.transparent      = true;
  x.opacity          = 0.28;
  x.depthWrite       = false;
  x.alphaToCoverage  = true;   // routes alpha through MSAA — much smoother edges
  x.side             = THREE.FrontSide;  // DoubleSide doubles fragments and worsens aliasing
  return x;
}}

function toggleXray() {{
  // Diag colors (V) swap the same face/door materials; leaving them on would
  // let x-ray cache the diag material as "orig" and strand it after toggling.
  if (!xrayOn && diagOn) toggleDiagColors();
  xrayOn = !xrayOn;
  for (const mesh of [...drawerFronts, ...pullMeshes, ...doorFaces, ...doorPullMeshes]) {{
    if (!xrayCache.has(mesh)) {{
      const orig = mesh.material;
      const xray = Array.isArray(orig) ? orig.map(_makeXrayMaterial) : _makeXrayMaterial(orig);
      xrayCache.set(mesh, {{ orig, xray }});
    }}
    const c = xrayCache.get(mesh);
    mesh.material   = xrayOn ? c.xray : c.orig;
    mesh.castShadow = !xrayOn;
  }}
}}

// Move every drawer to the open fraction its `state` calls for. Each pair
// tracks the fraction currently applied so we only add the difference —
// state changes compose without drift.
function setDrawersOpen(state) {{
  for (const pair of drawerPairs.values()) {{
    if (!pair.pullVec || !pair.box || !pair.face) continue;
    const frac = state === 2 ? 1
               : state === 1 ? (pair.stagger != null ? pair.stagger : 0.6)
               : 0;
    const applied = pair._applied || 0;
    if (Math.abs(frac - applied) < 1e-6) continue;
    const delta = pair.pullVec.clone().multiplyScalar(frac - applied);
    pair.box.position.add(delta);
    pair.face.position.add(delta);
    if (pair.pulls) {{
      for (const pull of pair.pulls) pull.position.add(delta);
    }}
    pair._applied = frac;
  }}
}}

function cycleOpenDrawers() {{
  openState = (openState + 1) % 3;   // closed → staggered → open → closed
  setDrawersOpen(openState);
}}

// ── Clipping plane ────────────────────────────────────────────────────────────
let clipActive = false;
let clipAxis   = 'z';
const clipPlane = new THREE.Plane(new THREE.Vector3(0, 0, -1), 0);

function axisSpan(box, axis) {{
  if (axis === 'x') return {{ span: box.max.x - box.min.x, min: box.min.x }};
  if (axis === 'y') return {{ span: box.max.z - box.min.z, min: box.min.z }};  // cabinet Y = Three.js Z
  return {{ span: box.max.y - box.min.y, min: box.min.y }};                    // cabinet Z = Three.js Y
}}

function setSliderStep(axis) {{
  const box = new THREE.Box3().setFromObject(cabinetRoot || scene);
  const {{ span }} = axisSpan(box, axis);
  // Target 1 mm per slider step
  const step = Math.max(0.001, Math.min(1, 100 / span));
  document.getElementById('clip-range').step = step.toFixed(4);
}}

function setClipAxis(axis) {{
  clipAxis = axis;
  document.querySelectorAll('#clip-axis-btns button').forEach(b => b.classList.remove('sel'));
  document.getElementById('btn-' + axis).classList.add('sel');
  setSliderStep(axis);
  updateClipPlane();
}}

function updateClipPlane() {{
  if (!clipActive) return;
  const t = parseFloat(document.getElementById('clip-range').value) / 100;
  const box = new THREE.Box3().setFromObject(cabinetRoot || scene);
  const {{ span, min }} = axisSpan(box, clipAxis);
  const v = min + t * span;
  let normal, constant, axisLabel;
  if (clipAxis === 'x') {{
    normal = new THREE.Vector3(-1, 0, 0); constant = v; axisLabel = 'from left';
  }} else if (clipAxis === 'y') {{
    // Cabinet front is world +Z (drawers open along local -Y → world -Z after
    // the root's -90° X rotation), so box.min.z is the BACK.  axisSpan('y')
    // measures t*span up from box.min.z, i.e. from the back.
    normal = new THREE.Vector3(0, 0, -1); constant = v; axisLabel = 'from back';
  }} else {{
    normal = new THREE.Vector3(0, -1, 0); constant = v; axisLabel = 'from bottom';
  }}
  clipPlane.set(normal, constant);
  const posEl = document.getElementById('clip-pos');
  if (posEl) posEl.textContent = Math.round(t * span) + ' mm ' + axisLabel;
}}

function toggleClip() {{
  clipActive = !clipActive;
  const ui = document.getElementById('clip-ui');
  if (clipActive) {{
    ui.classList.add('active');
    renderer.clippingPlanes = [clipPlane];
    renderer.localClippingEnabled = true;
    setSliderStep(clipAxis);
    updateClipPlane();
  }} else {{
    ui.classList.remove('active');
    renderer.clippingPlanes = [];
  }}
}}

window.setClipAxis  = setClipAxis;
window.updateClipPlane = updateClipPlane;

window.addEventListener('keydown', (e) => {{
  // Ignore when modifier keys are held so we don't clash with browser shortcuts,
  // and when a form control (finish dropdown, buttons) has focus.
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target && /^(SELECT|INPUT|TEXTAREA|BUTTON)$/.test(e.target.tagName)) return;
  if (e.key === 'x' || e.key === 'X') {{ toggleXray();        e.preventDefault(); }}
  if (e.key === 'o' || e.key === 'O') {{ cycleOpenDrawers(); e.preventDefault(); }}
  if (e.key === 'c' || e.key === 'C') {{ toggleClip();        e.preventDefault(); }}
  if (e.key === 'v' || e.key === 'V') {{ toggleDiagColors(); e.preventDefault(); }}
  if (e.key === 'm' || e.key === 'M') {{ if (mangaMeshes.length) {{ cycleManga(); e.preventDefault(); }} }}
}});

// ── Diagnostic colors (V key) ─────────────────────────────────────────────────
// Vivid per-panel-type colors for drawer box inspection.
// Three.js deduplicates node names across the scene (back → back_1, back_2, …)
// so we strip the numeric suffix before looking up the panel type.
const PINK   = new THREE.Color(1.00, 0.25, 0.60);
const YELLOW = new THREE.Color(1.00, 0.85, 0.10);
const GREEN  = new THREE.Color(0.10, 0.82, 0.40);
const BLUE   = new THREE.Color(0.25, 0.55, 1.00);
const ORANGE = new THREE.Color(1.00, 0.60, 0.15);
const PURPLE = new THREE.Color(0.62, 0.35, 1.00);

// Drawer box panels (inside bay{{i}}_drawer{{j}} groups)
const DRAWER_DIAG_COLS = {{
  side_L: PINK, side_R: PINK,
  sub_front: YELLOW, back: YELLOW,
  bottom: GREEN,
}};
// Carcass panels.  The top/bottom/back panels are siblings of the bay_{{i}}
// group (children of the cabinet node), so panels are matched by name alone
// with no group requirement; drawer-box members are claimed by the drawer
// branch first, which is what keeps the two 'bottom'/'back' names apart.
const CARCASS_DIAG_COLS = {{
  left_side: BLUE, right_side: BLUE,
  top: ORANGE, bottom: ORANGE,
}};

let diagOn = false;
const diagMeshes = [];

function _diagAncestorColor(obj, table) {{
  let cur = obj.parent;
  for (let i = 0; i < 5; i++) {{
    if (!cur) return null;
    const base = cur.name.replace(/_\\d+$/, '');
    if (table[base]) return table[base];
    cur = cur.parent;
  }}
  return null;
}}

// Both regexes tolerate the GLTFLoader dedup suffix (bay0_drawer0_1, …) that
// second-and-later cabinets carry in composed project scenes.
function _hasDiagAncestor(obj, re) {{
  let cur = obj.parent;
  for (let i = 0; i < 7; i++) {{
    if (!cur) return false;
    if (re.test(cur.name || '')) return true;
    cur = cur.parent;
  }}
  return false;
}}
const DIAG_DRAWER_RE = /^bay\\d+_drawer\\d+(?:_\\d+)?$/;
const DIAG_FACE_RE   = /^bay\\d+_(face|door)\\d+/;   // faces + door leaves; 'doorpull' cannot match

function initDiagColors() {{
  (cabinetRoot || scene).traverse(obj => {{
    if (!obj.isMesh || !obj.material) return;
    let diagCol = null;
    if (_hasDiagAncestor(obj, DIAG_DRAWER_RE)) {{
      diagCol = _diagAncestorColor(obj, DRAWER_DIAG_COLS);
    }} else if (_hasDiagAncestor(obj, DIAG_FACE_RE)) {{
      diagCol = PURPLE;   // drawer faces + doors
    }} else {{
      diagCol = _diagAncestorColor(obj, CARCASS_DIAG_COLS);
    }}
    if (diagCol) {{
      const diagMat = obj.material.clone();
      diagMat.color.copy(diagCol);
      diagMat.map = null;            // flat vivid color even over a wood finish
      diagMat.vertexColors = false;
      diagMat.roughness = 0.45;
      diagMat.needsUpdate = true;
      diagMeshes.push({{ mesh: obj, origMat: obj.material, diagMat }});
    }}
  }});
}}

function toggleDiagColors() {{
  // X-ray (X) swaps the same face/door materials; turn it off first so diag's
  // captured origMat is the real material, not a stranded x-ray/diag clone.
  if (!diagOn && xrayOn) toggleXray();
  diagOn = !diagOn;
  for (const {{ mesh, origMat, diagMat }} of diagMeshes) {{
    mesh.material = diagOn ? diagMat : origMat;
  }}
}}

// ── Render loop ───────────────────────────────────────────────────────────────
(function animate() {{
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}})();

window.addEventListener('resize', () => {{
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}});
  </script>
</body>
</html>"""
