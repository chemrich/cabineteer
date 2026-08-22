"""
Multi-cabinet projects.

A :class:`CabinetProject` bundles several :class:`CabinetConfig` instances
that are designed to live together (e.g. three matching sideboards, a wall
of base cabinets, a built-in run). The project owns a :class:`SharedDesign`
block of optional design tokens that get merged into each child cabinet at
construction time, so material thicknesses, joinery method, and hardware
brand stay consistent without having to repeat them on every child.

Downstream tools (``evaluate_project``, ``generate_project_cutlist``)
operate on the merged result — every child cabinet picks up the shared
tokens, then anything the child explicitly declared in its ``overrides``
set wins back.

The module is pure-Python — no CadQuery dependency — so it loads in lite
mode and can be exercised by the eval harness directly.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Optional

from .cabinet import CabinetConfig, ColumnConfig, OpeningConfig, build_cabinet_config
from .joinery import (
    CarcassJoinery,
    DrawerJoineryStyle,
    DominoSpec,
    PocketScrewSpec,
    BiscuitSpec,
    DowelSpec,
)


# Fields on SharedDesign that map 1:1 to a CabinetConfig attribute.
# pull_preset is handled specially because it expands into two attributes.
_SHARED_FIELDS = (
    "side_thickness",
    "bottom_thickness",
    "top_thickness",
    "shelf_thickness",
    "back_thickness",
    "drawer_box_thickness",
    "drawer_box_prefinished",
    "face_material",
    "carcass_material",
    "edge_band_mode",
    "edge_band_thickness_mm",
    "edge_band_material",
    "edge_band_stock",
    "face_gap_mm",
    "furniture_top",
    "carcass_joinery",
    "carcass_corner_style",
    "back_style",
    "back_capture",
    "back_groove_setback",
    "drawer_joinery",
    "domino_spec",
    "pocket_screw_spec",
    "biscuit_spec",
    "dowel_spec",
    "drawer_slide",
    "door_hinge",
    "drawer_pull",
    "door_pull",
    "leg_key",
)


@dataclass(frozen=True)
class SharedDesign:
    """Design tokens that apply across every cabinet in a project.

    Every field is ``Optional[...]`` — ``None`` means "do not override; the
    child config keeps its own value." Anything set here is merged into each
    child :class:`CabinetConfig` at project-resolution time.
    """
    # Materials
    side_thickness:   Optional[float] = None
    bottom_thickness: Optional[float] = None
    top_thickness:    Optional[float] = None
    shelf_thickness:  Optional[float] = None
    back_thickness:   Optional[float] = None
    drawer_box_thickness: Optional[float] = None  # box sides + sub-front/back
    drawer_box_prefinished: Optional[bool] = None  # pre-finished BB box stock
    face_material: Optional[str] = None  # false fronts + door panels (cutlist)
    carcass_material: Optional[str] = None  # sides/top/bottom/shelves/dividers
    edge_band_mode: Optional[str] = None       # none | hot_melt | hardwood
    edge_band_thickness_mm: Optional[float] = None
    edge_band_material: Optional[str] = None   # "" → derive from panel material
    edge_band_stock: Optional[dict] = None     # strip stock spec (see CabinetConfig)
    # Show-face geometry: vertical reveal between stacked faces (mm), and the
    # furniture-top style (front cap strip + flush-bottom face drop) — both
    # feed cabinet.face_layout, the single source for face/door dimensions.
    face_gap_mm: Optional[float] = None
    furniture_top: Optional[bool] = None

    # Joinery
    carcass_joinery:  Optional[CarcassJoinery]     = None
    carcass_corner_style: Optional[str] = None     # butt | miter
    back_style: Optional[str] = None               # full_height | under_top
    # pocket | rabbet | half_lap | dado — how the back is HELD, independent
    # of back_style (where its top edge lands) and carcass_joinery.
    back_capture: Optional[str] = None
    back_groove_setback: Optional[float] = None    # "dado" capture only
    drawer_joinery:   Optional[DrawerJoineryStyle] = None
    domino_spec:        Optional[DominoSpec]       = None
    pocket_screw_spec:  Optional[PocketScrewSpec]  = None
    biscuit_spec:       Optional[BiscuitSpec]      = None
    dowel_spec:         Optional[DowelSpec]        = None

    # Hardware
    drawer_slide: Optional[str] = None
    door_hinge:   Optional[str] = None
    drawer_pull:  Optional[str] = None
    door_pull:    Optional[str] = None
    leg_key:      Optional[str] = None

    # Pull preset (resolved into drawer_pull / door_pull at merge time)
    pull_preset:  Optional[str] = None


@dataclass(frozen=True)
class WorktopSpec:
    """A desk/counter surface that spans part of the project run.

    The slab is positioned in run coordinates: ``x_offset_mm`` is its left
    edge measured from the left face of the first cabinet, using whatever
    ``gap_mm`` the run is rendered with. ``surface_height_mm`` is the
    finished top-of-slab height above the FLOOR (feet included), so the
    number matches how a desk or counter height is actually specified.

    ``leg_count`` renders simple round support legs from the floor to the
    slab underside; ``leg_placement`` says where they go:
      "corners"   — leg_count 4 = one per corner; leg_count 2 = front
                    corners only (rear edge carried by cleats into the
                    flanking cabinets).  The default.
      "left_end"  — 2 legs (front + rear) at the slab's left end, e.g. a
                    single-pedestal desk whose right end sits on a cabinet.
      "right_end" — mirror of left_end.
    ``leg_count = 0`` means no legs (slab rests on the cabinets).
    """
    width_mm: float
    depth_mm: float
    thickness_mm: float = 19.0
    surface_height_mm: float = 736.6   # 29" — standard desk height
    x_offset_mm: float = 0.0
    # Front-edge shift relative to the cabinet fronts (y=0). Negative pushes
    # the slab proud of the carcass — e.g. -19 lands it on the face plane.
    y_offset_mm: float = 0.0
    leg_count: int = 0
    leg_diameter_mm: float = 50.0
    leg_inset_mm: float = 60.0
    leg_placement: str = "corners"
    material: str = "finished_wood"

    def __post_init__(self) -> None:
        valid = {"corners", "left_end", "right_end"}
        if self.leg_placement not in valid:
            raise ValueError(
                f"leg_placement must be one of {sorted(valid)}, "
                f"got {self.leg_placement!r}."
            )

    @property
    def leg_height_mm(self) -> float:
        return self.surface_height_mm - self.thickness_mm

    def leg_points(self) -> list[tuple[float, float]]:
        """(x, y) floor positions for the support legs, in run coordinates."""
        if self.leg_count <= 0:
            return []
        inset = self.leg_inset_mm
        x0 = self.x_offset_mm + inset
        x1 = self.x_offset_mm + self.width_mm - inset
        yf = self.y_offset_mm + inset
        yb = self.y_offset_mm + self.depth_mm - inset
        if self.leg_placement == "left_end":
            return [(x0, yf), (x0, yb)]
        if self.leg_placement == "right_end":
            return [(x1, yf), (x1, yb)]
        if self.leg_count >= 4:
            return [(x0, yf), (x1, yf), (x0, yb), (x1, yb)]
        return [(x0, yf), (x1, yf)]


@dataclass(frozen=True)
class ProjectCabinet:
    """One cabinet slot inside a project.

    ``name`` is the user-facing identifier (e.g. "left", "center", "right").
    It's used to tag generated outputs and to disambiguate evaluation issues.

    ``config`` carries the per-cabinet :class:`CabinetConfig`. Any field set
    on the project's ``shared`` block overrides the corresponding field on
    this config — UNLESS the child declared it via ``overrides``, in which
    case the child's value wins.
    """
    name: str
    config: CabinetConfig
    overrides: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class CabinetProject:
    """A set of cabinets designed to live together."""
    name: str
    cabinets: tuple[ProjectCabinet, ...]
    shared:   SharedDesign = field(default_factory=SharedDesign)
    notes:    str = ""
    # Available wall run in mm. When set, consistency checks flag a total
    # cabinet run wider than the wall (error) and report leftover gap (info).
    wall_width_mm: Optional[float] = None
    # Fork lineage — set by duplicate_project. ``forked_from`` names the
    # source project (which may since have been renamed or deleted);
    # ``forked_at`` is an ISO-8601 timestamp of the fork.
    forked_from: Optional[str] = None
    forked_at:   Optional[str] = None
    # Optional desk/counter surface spanning part of the run. Stored as a
    # top-level snapshot key, so older readers ignore it safely.
    worktop: Optional[WorktopSpec] = None

    def resolved(self) -> tuple[tuple[str, CabinetConfig], ...]:
        """Return ``((name, merged_config), ...)`` with shared tokens applied."""
        return tuple(
            (pc.name, _merge(pc.config, self.shared, pc.overrides))
            for pc in self.cabinets
        )


def _merge(
    cfg: CabinetConfig,
    shared: SharedDesign,
    overrides: frozenset[str],
) -> CabinetConfig:
    """Apply non-None shared tokens onto cfg, skipping anything in overrides."""
    updates: dict[str, Any] = {}

    # Handle pull_preset first — it expands into drawer_pull, door_pull, and
    # door_pull_inset_mm (same three attributes build_cabinet_config applies),
    # but only where not already pinned by shared or by the child override.
    if shared.pull_preset is not None and "pull_preset" not in overrides:
        from .hardware import get_pull_preset
        preset = get_pull_preset(shared.pull_preset)
        if "drawer_pull" not in overrides and shared.drawer_pull is None:
            updates["drawer_pull"] = preset.drawer_pull
        if "door_pull" not in overrides and shared.door_pull is None:
            updates["door_pull"] = preset.door_pull
        if "door_pull_inset_mm" not in overrides:
            updates["door_pull_inset_mm"] = preset.door_pull_inset_mm

    for name in _SHARED_FIELDS:
        value = getattr(shared, name)
        if value is None or name in overrides:
            continue
        updates[name] = value

    return replace(cfg, **updates) if updates else cfg


# ─── Persistence ──────────────────────────────────────────────────────────────


def project_dir() -> Path:
    """Directory under ~/.cabineteer where serialized projects live."""
    from .paths import data_dir
    return data_dir() / "projects"


# Project names become filename stems under ~/.cabineteer/projects/ —
# restrict them so a name can never contain a path separator or traverse
# out of the projects directory.
_PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")

# Cap the stem length so a name can never blow past filesystem limits
# (typically 255 bytes) once the ".json" suffix is appended.
_PROJECT_NAME_MAX_LEN = 100


def project_path(name: str) -> Path:
    """Filesystem path for a project's JSON snapshot.

    Raises ValueError for names that are unsafe as a filename stem
    (path separators, leading dots, ``..`` traversal, empty, over-long).
    """
    if not _PROJECT_NAME_RE.match(name) or ".." in name:
        raise ValueError(
            f"Invalid project name {name!r}: use letters, digits, spaces, "
            "'.', '_' or '-' (must start with a letter or digit)."
        )
    if len(name) > _PROJECT_NAME_MAX_LEN:
        raise ValueError(
            f"Project name too long ({len(name)} chars); "
            f"keep it to {_PROJECT_NAME_MAX_LEN} characters or fewer."
        )
    return project_dir() / f"{name}.json"


def save_project(project: CabinetProject) -> Path:
    """Serialize a project to ~/.cabineteer/projects/<name>.json.

    Persisted form is each cabinet's *original* (unmerged) config alongside
    the shared design block and per-cabinet override sets — i.e. exactly the
    inputs to :meth:`CabinetProject.resolved`.  :func:`load_project`
    reconstructs the project and re-applies the shared tokens on demand, so a
    save/load round-trip reproduces the same resolved designs.
    """
    project_dir().mkdir(parents=True, exist_ok=True)
    path = project_path(project.name)
    path.write_text(json.dumps(project_to_dict(project), indent=2), encoding="utf-8")
    return path


def load_project(name: str) -> CabinetProject:
    """Reconstruct a :class:`CabinetProject` from disk."""
    path = project_path(name)
    if not path.exists():
        raise FileNotFoundError(
            f"Project {name!r} not found at {path}. "
            "Run design_project first or pass the inline 'project' payload."
        )
    try:
        return project_from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, KeyError, TypeError) as exc:
        # Name the project and file — in a batch the bare JSON error gives
        # no clue which snapshot is broken.
        raise ValueError(
            f"Project {name!r} snapshot at {path} is unreadable: {exc}"
        ) from exc


# Name prefixes that mark dev/test artifacts (eval scenarios, smoke runs,
# lite-mode probes). list_saved_projects hides these by default so the
# human-facing catalogue isn't buried in tooling debris.
_DEV_NAME_PREFIXES = ("eval_", "test_", "smoke_", "_")


def list_saved_projects(
    query: str | None = None,
    include_all: bool = False,
    sort: str = "recent",
) -> list[dict]:
    """Lightweight metadata for every saved project under :func:`project_dir`.

    Reads each ``*.json`` snapshot without building full config objects so a
    catalogue listing stays cheap and a single corrupt file can't sink the
    whole listing — unreadable files come back as an entry with an ``error``
    field instead.

    ``query`` filters case-insensitively over project name, notes, and
    cabinet names ("shop" finds the miter station via its notes). Unreadable
    entries are kept only when their filename matches, so a corrupt file
    can't hide from a direct-name search.

    Dev artifacts (names starting with ``eval_``/``test_``/``smoke_``/``_``)
    are hidden unless ``include_all`` is true — except when a ``query`` is
    given, which searches everything (an explicit search must be able to
    find anything).

    ``sort``: ``"recent"`` (default, newest ``modified`` first) or
    ``"name"`` (alphabetical).
    """
    from datetime import datetime

    entries: list[dict] = []
    d = project_dir()
    if not d.exists():
        return entries
    for path in sorted(d.glob("*.json")):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime).isoformat(
                timespec="seconds"
            )
        except OSError:
            modified = None  # dangling symlink / raced deletion
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cabinets = data.get("cabinets", [])
            entries.append({
                "name": data.get("name", path.stem),
                "cabinet_count": len(cabinets),
                "cabinet_names": [c.get("name", "") for c in cabinets],
                "total_run_width_mm": round(sum(
                    float(c.get("config", {}).get("width", 0) or 0)
                    for c in cabinets
                ), 1),
                "wall_width_mm": data.get("wall_width_mm"),
                "notes": data.get("notes", ""),
                "modified": modified,
                "path": str(path),
                **(
                    {"forked_from": data["forked_from"]}
                    if data.get("forked_from") else {}
                ),
            })
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            entries.append({
                "name": path.stem,
                "error": f"unreadable snapshot: {exc}",
                "modified": modified,
                "path": str(path),
            })

    if query:
        q = query.lower().strip()

        def _matches(e: dict) -> bool:
            haystack = " ".join((
                e.get("name", ""),
                e.get("notes", ""),
                " ".join(e.get("cabinet_names", ())),
            )).lower()
            return q in haystack

        entries = [e for e in entries if _matches(e)]
    elif not include_all:
        entries = [
            e for e in entries
            if not e.get("name", "").startswith(_DEV_NAME_PREFIXES)
        ]

    if sort == "name":
        entries.sort(key=lambda e: e.get("name", "").lower())
    else:  # "recent" — newest first; entries without a timestamp sink
        entries.sort(key=lambda e: e.get("modified") or "", reverse=True)
    return entries


def rename_project(old_name: str, new_name: str) -> Path:
    """Rename a saved project (file stem AND embedded ``name`` field).

    Both names are validated by :func:`project_path`. Refuses to overwrite
    an existing project. Generated cutlists/visualizations keep their old
    stems — they are output artifacts, not part of the project record.
    """
    old_path = project_path(old_name)
    new_path = project_path(new_name)
    if not old_path.exists():
        raise FileNotFoundError(
            f"Project {old_name!r} not found at {old_path}."
        )
    if new_path.exists():
        raise ValueError(
            f"Project {new_name!r} already exists at {new_path}; "
            "delete it first or pick another name."
        )
    data = json.loads(old_path.read_text(encoding="utf-8"))
    data["name"] = new_name
    new_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    old_path.unlink()
    return new_path


def delete_project(name: str) -> Path:
    """Permanently delete a saved project snapshot. Returns the removed path."""
    path = project_path(name)
    if not path.exists():
        raise FileNotFoundError(f"Project {name!r} not found at {path}.")
    path.unlink()
    return path


def duplicate_project(name: str, new_name: str, notes: str | None = None) -> Path:
    """Fork a saved project under a new name.

    Copies the snapshot verbatim, stamps ``forked_from``/``forked_at``
    lineage into the copy, and refuses to overwrite an existing project.
    ``notes`` replaces the copied notes when given (the original's notes
    often describe decisions specific to that build). The source project
    is never touched.
    """
    from datetime import datetime

    src_path = project_path(name)
    dst_path = project_path(new_name)
    if not src_path.exists():
        raise FileNotFoundError(f"Project {name!r} not found at {src_path}.")
    if dst_path.exists():
        raise ValueError(
            f"Project {new_name!r} already exists at {dst_path}; "
            "delete it first or pick another name."
        )
    data = json.loads(src_path.read_text(encoding="utf-8"))
    data["name"] = new_name
    data["forked_from"] = name
    data["forked_at"] = datetime.now().isoformat(timespec="seconds")
    if notes is not None:
        data["notes"] = notes
    dst_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return dst_path


# Config keys whose child-level value only survives project resolution when
# the key is also listed in the cabinet's ``overrides`` set (see build_project:
# round-tripped overrides lists are exhaustive). A shared pull_preset expands
# into the pull fields at merge time, so those count too.
_PULL_EXPANSION_KEYS = ("drawer_pull", "door_pull", "door_pull_inset_mm")


def _shared_override_keys(shared_dict: dict) -> set[str]:
    """Keys a cabinet-config patch must pin via ``overrides`` to stick."""
    keys = {
        k for k in _SHARED_FIELDS + ("pull_preset",)
        if shared_dict.get(k) is not None
    }
    if shared_dict.get("pull_preset") is not None:
        keys |= set(_PULL_EXPANSION_KEYS)
    return keys


def apply_project_patch(base: dict, patch: dict) -> tuple[dict, list[str]]:
    """Apply an update_project delta to a stored project payload.

    Pure function: returns ``(patched_payload, change_log)`` without touching
    disk. ``base`` is a snapshot dict (the ``project_to_dict`` shape);
    ``patch`` carries any of:

    - ``notes`` — replaces the notes string.
    - ``wall_width_mm`` — replaces the wall constraint; ``None`` clears it.
    - ``worktop`` — shallow-merged into the stored worktop spec (creating
      one if absent — ``width_mm``/``depth_mm`` required then); ``None``
      removes the worktop entirely.
    - ``shared`` — shallow-merged into the shared token block; a ``None``
      value removes that token (children fall back to their own values).
    - ``cabinets`` — list of per-cabinet patches matched by ``name``:
        - ``config``: shallow-merged into the stored config; a ``None``
          value removes the key (reverting it to the shared token or the
          CabinetConfig default at resolve time). Patched keys that collide
          with an active shared token are added to the cabinet's
          ``overrides`` so the patched value survives resolution.
          Convenience keys are canonicalized against the stored snapshot
          (which always carries the expanded form): ``pull_preset``
          expands into the pull keys (``None`` clears them),
          ``drawer_config`` replaces ``openings``, ``num_drawers`` clears
          ``openings``/``columns`` so the stack regenerates, and patching
          ``openings`` clears ``columns`` (and vice versa) so the patched
          layout actually governs. Value-identical patches are dropped
          from the change log and do not re-save the snapshot.
        - ``overrides``: full replacement of the override list (skips the
          automatic addition above).
        - ``new_name``: renames the cabinet within the project.
        - ``remove: true``: drops the cabinet (the project must keep at
          least one).
        - ``add: true``: appends a new cabinet (``config`` required; the
          name must not already exist). Overrides are inferred from the
          config keys, as for a fresh design_project child.
    """
    out = json.loads(json.dumps(base))  # deep copy; payloads are plain JSON
    changes: list[str] = []

    if "notes" in patch:
        # None follows the patch shape's null-clears convention (it used to
        # persist as the literal string "None").
        out["notes"] = "" if patch["notes"] is None else str(patch["notes"])
        changes.append("notes replaced")

    if "wall_width_mm" in patch:
        wall = patch["wall_width_mm"]
        if wall is None:
            out.pop("wall_width_mm", None)
            changes.append("wall_width_mm cleared")
        else:
            out["wall_width_mm"] = float(wall)
            changes.append(f"wall_width_mm = {float(wall)}")

    if "worktop" in patch:
        wpatch = patch["worktop"]
        if wpatch is None:
            if "worktop" in out:
                del out["worktop"]
                changes.append("worktop removed")
        else:
            existed = "worktop" in out
            merged = {**(out.get("worktop") or {}), **dict(wpatch)}
            # A None value inside the patch reverts that field to its default.
            merged = {k: v for k, v in merged.items() if v is not None}
            # Validate now so a bad patch fails before anything is written.
            worktop_from_dict(merged)
            out["worktop"] = merged
            changes.append("worktop updated" if existed else "worktop added")

    if "shared" in patch and patch["shared"]:
        shared = dict(out.get("shared") or {})
        for key, value in patch["shared"].items():
            if value is None:
                if key in shared:
                    del shared[key]
                    changes.append(f"shared.{key} cleared")
            else:
                old = shared.get(key)
                shared[key] = value
                changes.append(
                    f"shared.{key} = {value!r}" if old is None
                    else f"shared.{key}: {old!r} -> {value!r}"
                )
        out["shared"] = shared

    shared_keys = _shared_override_keys(out.get("shared") or {})
    stored = {c["name"]: c for c in out.get("cabinets", [])}

    for cpatch in patch.get("cabinets", []) or []:
        cname = str(cpatch.get("name", ""))
        if cpatch.get("add"):
            if cname in stored:
                raise ValueError(
                    f"Cannot add cabinet {cname!r}: the name already exists. "
                    "Use a config patch to edit it, or pick another name."
                )
            if not cpatch.get("config"):
                raise ValueError(f"Adding cabinet {cname!r} requires a 'config'.")
            entry = {"name": cname, "config": dict(cpatch["config"])}
            # Record the inferred overrides NOW (same rule build_project
            # uses for entries without an 'overrides' key) — a later edit
            # of this same cabinet writes an exhaustive overrides list, and
            # without this the add-time pins would silently vanish
            # (review 2026-07-29 minor 8).
            explicit_keys = set(entry["config"].keys())
            if "pull_preset" in explicit_keys:
                explicit_keys |= set(_PULL_EXPANSION_KEYS)
            entry["overrides"] = sorted(shared_keys & explicit_keys)
            out["cabinets"].append(entry)
            stored[cname] = entry
            changes.append(f"cabinet {cname!r} added")
            continue

        if cname not in stored:
            raise ValueError(
                f"No cabinet named {cname!r} in this project. "
                f"Cabinets: {sorted(stored)}."
            )
        entry = stored[cname]

        if cpatch.get("remove"):
            out["cabinets"] = [c for c in out["cabinets"] if c is not entry]
            del stored[cname]
            if not out["cabinets"]:
                raise ValueError(
                    f"Cannot remove {cname!r}: a project needs at least one cabinet."
                )
            changes.append(f"cabinet {cname!r} removed")
            continue

        overrides = set(entry.get("overrides", []))
        explicit_overrides = "overrides" in cpatch

        # ── Canonicalize design_cabinet conveniences that do NOT survive a
        # round-tripped snapshot (review 2026-07-29 M7). A stored config
        # always carries the expanded keys (openings, drawer_pull: null, …),
        # so merging the convenience key verbatim is a silent no-op:
        # build_cabinet_config's setdefault/alias paths never fire, and the
        # key evaporates on the next canonical re-save.
        cfg_patch = dict(cpatch.get("config") or {})
        if "pull_preset" in cfg_patch:
            pv = cfg_patch.pop("pull_preset")
            if pv is None:
                expansion = {k: None for k in _PULL_EXPANSION_KEYS}
            else:
                from .hardware import get_pull_preset
                preset = get_pull_preset(pv)   # validates the preset key
                expansion = {
                    "drawer_pull": preset.drawer_pull,
                    "door_pull": preset.door_pull,
                    "door_pull_inset_mm": preset.door_pull_inset_mm,
                }
            for k, v in expansion.items():
                # Keys the same patch sets explicitly win over the preset.
                cfg_patch.setdefault(k, v)
        if "drawer_config" in cfg_patch:
            # Alias for openings — replace the stored stack outright.
            dv = cfg_patch.pop("drawer_config")
            cfg_patch.setdefault("openings", dv)
        if cfg_patch.get("num_drawers") is not None:
            # Regenerate the stack from the count at build time: the stored
            # openings/columns would otherwise keep governing the layout.
            cfg_patch.setdefault("openings", None)
            cfg_patch.setdefault("columns", None)
        if cfg_patch.get("openings") is not None:
            cfg_patch.setdefault("columns", None)   # openings governs now
        elif cfg_patch.get("columns") is not None:
            cfg_patch.setdefault("openings", None)  # columns governs now

        for key, value in cfg_patch.items():
            cfg = entry.setdefault("config", {})
            if value is None:
                if key in cfg:
                    del cfg[key]
                    overrides.discard(key)
                    changes.append(f"{cname}.config.{key} cleared")
            else:
                old = cfg.get(key)
                pin_added = False
                if (not explicit_overrides and key in shared_keys
                        and key not in overrides):
                    overrides.add(key)
                    pin_added = True
                if key in cfg and old == value:
                    # True no-op — don't log a fake change (which would
                    # also bump the snapshot's mtime and reorder
                    # newest-first listings); a fresh pin still counts.
                    if pin_added:
                        changes.append(
                            f"{cname}.config.{key} pinned as override")
                    continue
                cfg[key] = value
                if isinstance(old, (int, float, str, bool)) and old != value:
                    changes.append(f"{cname}.config.{key}: {old!r} -> {value!r}")
                else:
                    changes.append(f"{cname}.config.{key} set")

        if explicit_overrides:
            overrides = set(str(k) for k in cpatch["overrides"] or ())
            changes.append(f"{cname}.overrides replaced")
        if "overrides" in entry or overrides:
            entry["overrides"] = sorted(overrides)

        new_cname = cpatch.get("new_name")
        if new_cname and str(new_cname) != cname:
            new_cname = str(new_cname)
            if new_cname in stored:
                raise ValueError(
                    f"Cannot rename cabinet {cname!r} to {new_cname!r}: "
                    "the name already exists."
                )
            entry["name"] = new_cname
            stored[new_cname] = stored.pop(cname)
            changes.append(f"cabinet {cname!r} renamed to {new_cname!r}")

    return out, changes


def update_saved_project(patch: dict) -> tuple["CabinetProject", list[str]]:
    """Load, patch, rebuild, and re-save a stored project.

    ``patch["name"]`` names the saved project (see :func:`apply_project_patch`
    for the delta shape). The rebuild goes through :func:`build_project`, so
    the patched payload is validated exactly like a design_project submission
    before anything is written back. Returns ``(project, change_log)``.
    """
    name = str(patch.get("name") or "")
    path = project_path(name)
    if not path.exists():
        raise FileNotFoundError(
            f"Project {name!r} not found at {path}. See list_projects."
        )
    base = json.loads(path.read_text(encoding="utf-8"))
    patched, changes = apply_project_patch(base, patch)
    project = build_project(patched)
    if changes:  # an empty patch shouldn't bump the snapshot's mtime
        save_project(project)
    return project, changes


# ─── Dict <-> object conversion ───────────────────────────────────────────────


def _opening_to_dict(op: OpeningConfig) -> dict:
    out = {"height_mm": op.height_mm, "opening_type": op.opening_type}
    for k in ("hinge_key", "hinge_side", "pull_key", "num_doors", "door_thickness",
              "bottom_thickness", "slide_key"):
        v = getattr(op, k)
        if v is not None:
            out[k] = v
    return out


def _column_to_dict(col: ColumnConfig) -> dict:
    out = {
        "width_mm": col.width_mm,
        "openings": [_opening_to_dict(op) for op in col.openings],
    }
    if col.fixed_shelf_positions:
        out["fixed_shelf_positions"] = list(col.fixed_shelf_positions)
    return out


def _config_to_dict(cfg: CabinetConfig) -> dict:
    """Flatten a CabinetConfig back to a JSON-friendly dict (round-trippable
    via :func:`config_from_dict`)."""
    return {
        "width": cfg.width,
        "height": cfg.height,
        "depth": cfg.depth,
        "side_thickness":   cfg.side_thickness,
        "bottom_thickness": cfg.bottom_thickness,
        "top_thickness":    cfg.top_thickness,
        "shelf_thickness":  cfg.shelf_thickness,
        "back_thickness":   cfg.back_thickness,
        "drawer_box_thickness": cfg.drawer_box_thickness,
        "drawer_box_prefinished": cfg.drawer_box_prefinished,
        "face_material": cfg.face_material,
        "carcass_material": cfg.carcass_material,
        "edge_band_mode": cfg.edge_band_mode,
        "edge_band_thickness_mm": cfg.edge_band_thickness_mm,
        "edge_band_material": cfg.edge_band_material,
        "edge_band_stock": dict(cfg.edge_band_stock) if cfg.edge_band_stock else None,
        "face_gap_mm": cfg.face_gap_mm,
        "furniture_top": cfg.furniture_top,
        "dado_depth":         cfg.dado_depth,
        "back_rabbet_width":  cfg.back_rabbet_width,
        "back_rabbet_depth":  cfg.back_rabbet_depth,
        "fixed_shelf_positions": list(cfg.fixed_shelf_positions),
        "adj_shelf_holes":       cfg.adj_shelf_holes,
        "shelf_pin_diameter":  cfg.shelf_pin_diameter,
        "shelf_pin_depth":     cfg.shelf_pin_depth,
        "shelf_pin_row_inset": cfg.shelf_pin_row_inset,
        "shelf_pin_start_z":   cfg.shelf_pin_start_z,
        "shelf_pin_end_z":     cfg.shelf_pin_end_z,
        "shelf_pin_spacing":   cfg.shelf_pin_spacing,
        "openings": [_opening_to_dict(op) for op in cfg.openings],
        "columns":  [_column_to_dict(col) for col in cfg.columns],
        "drawer_slide": cfg.drawer_slide,
        "door_hinge":   cfg.door_hinge,
        "drawer_pull":  cfg.drawer_pull,
        "door_pull":    cfg.door_pull,
        "door_hinge_side":    cfg.door_hinge_side,
        "door_pull_inset_mm": cfg.door_pull_inset_mm,
        "leg_key":   cfg.leg_key,
        "leg_count": cfg.leg_count,
        "leg_inset": cfg.leg_inset,
        "carcass_joinery": cfg.carcass_joinery.value,
        "carcass_corner_style": cfg.carcass_corner_style,
        # Back treatment. back_style was missing here until back_capture
        # landed, so a per-cabinet override of it was dropped on save —
        # Charlie's projects only ever set it as a shared token, which is
        # why nothing caught it.
        "back_style": cfg.back_style,
        "back_capture": cfg.back_capture,
        "back_groove_setback": cfg.back_groove_setback,
        "drawer_joinery":  cfg.drawer_joinery.value,
        # Per-method joinery specs — serialized as field dicts;
        # build_cabinet_config reconstructs the spec objects on load.
        "domino_spec":       _spec_to_dict(cfg.domino_spec),
        "pocket_screw_spec": _spec_to_dict(cfg.pocket_screw_spec),
        "biscuit_spec":      _spec_to_dict(cfg.biscuit_spec),
        "dowel_spec":        _spec_to_dict(cfg.dowel_spec),
    }


def _spec_to_dict(spec) -> dict:
    """Serialize a joinery spec dataclass as its field dict."""
    return {fk: getattr(spec, fk) for fk in spec.__dataclass_fields__}


def config_from_dict(d: dict) -> CabinetConfig:
    """Inverse of :func:`_config_to_dict`. Also accepts the lighter shape
    produced by the ``design_cabinet`` MCP tool input — i.e. anything
    :func:`cabinet.build_cabinet_config` would accept."""
    return build_cabinet_config(dict(d))


def _shared_to_dict(shared: SharedDesign) -> dict:
    out: dict[str, Any] = {}
    for name in _SHARED_FIELDS + ("pull_preset",):
        v = getattr(shared, name)
        if v is None:
            continue
        if name in ("carcass_joinery", "drawer_joinery"):
            out[name] = v.value
        elif hasattr(v, "__dataclass_fields__"):
            # joinery specs — serialize as their field dict
            out[name] = {fk: getattr(v, fk) for fk in v.__dataclass_fields__}
        else:
            out[name] = v
    return out


def shared_from_dict(d: dict | None) -> SharedDesign:
    if not d:
        return SharedDesign()
    valid = set(_SHARED_FIELDS) | {"pull_preset"}
    unknown = set(d) - valid
    if unknown:
        raise ValueError(
            f"Unknown shared design token(s) {sorted(unknown)}; "
            f"valid tokens: {sorted(valid)}."
        )
    kwargs: dict[str, Any] = {}
    for k, v in d.items():
        if k == "carcass_joinery" and isinstance(v, str):
            kwargs[k] = CarcassJoinery(v)
        elif k == "drawer_joinery" and isinstance(v, str):
            kwargs[k] = DrawerJoineryStyle(v)
        elif k in ("domino_spec", "pocket_screw_spec", "biscuit_spec", "dowel_spec") and isinstance(v, dict):
            spec_cls = {
                "domino_spec": DominoSpec,
                "pocket_screw_spec": PocketScrewSpec,
                "biscuit_spec": BiscuitSpec,
                "dowel_spec": DowelSpec,
            }[k]
            kwargs[k] = spec_cls(**v)
        else:
            kwargs[k] = v
    return SharedDesign(**kwargs)


def _worktop_to_dict(spec: WorktopSpec) -> dict:
    return asdict(spec)


def worktop_from_dict(d: dict | None) -> Optional[WorktopSpec]:
    """Build a :class:`WorktopSpec` from a payload dict (``None`` passes through).

    ``width_mm`` and ``depth_mm`` are required; everything else defaults.
    Unknown keys are rejected so a typo'd field name fails loudly instead of
    silently falling back to a default.
    """
    if d is None:
        return None
    known = {f.name for f in fields(WorktopSpec)}
    unknown = set(d) - known
    if unknown:
        raise ValueError(
            f"Unknown worktop field(s) {sorted(unknown)}. Known: {sorted(known)}."
        )
    for req in ("width_mm", "depth_mm"):
        if d.get(req) is None:
            raise ValueError(f"worktop requires '{req}'.")
    kwargs: dict[str, Any] = {k: v for k, v in d.items() if v is not None}
    _str_fields = ("material", "leg_placement")
    if "leg_count" in kwargs:
        kwargs["leg_count"] = int(kwargs["leg_count"])
    for k in _str_fields:
        if k in kwargs:
            kwargs[k] = str(kwargs[k])
    for k in kwargs:
        if k != "leg_count" and k not in _str_fields:
            kwargs[k] = float(kwargs[k])
    return WorktopSpec(**kwargs)


def project_to_dict(project: CabinetProject) -> dict:
    out = {
        "name": project.name,
        "notes": project.notes,
        "shared": _shared_to_dict(project.shared),
        "cabinets": [
            {
                "name": pc.name,
                "config": _config_to_dict(pc.config),
                "overrides": sorted(pc.overrides),
            }
            for pc in project.cabinets
        ],
    }
    if project.wall_width_mm is not None:
        out["wall_width_mm"] = project.wall_width_mm
    if project.forked_from is not None:
        out["forked_from"] = project.forked_from
        if project.forked_at is not None:
            out["forked_at"] = project.forked_at
    if project.worktop is not None:
        out["worktop"] = _worktop_to_dict(project.worktop)
    return out


def project_from_dict(d: dict) -> CabinetProject:
    shared = shared_from_dict(d.get("shared"))
    cabinets = tuple(
        ProjectCabinet(
            name=str(c["name"]),
            config=config_from_dict(c["config"]),
            overrides=frozenset(c.get("overrides", [])),
        )
        for c in d["cabinets"]
    )
    wall_raw = d.get("wall_width_mm")
    fork_raw = d.get("forked_from")
    at_raw   = d.get("forked_at")
    return CabinetProject(
        name=str(d["name"]),
        cabinets=cabinets,
        shared=shared,
        notes=str(d.get("notes", "")),
        wall_width_mm=float(wall_raw) if wall_raw is not None else None,
        forked_from=str(fork_raw) if fork_raw is not None else None,
        forked_at=str(at_raw) if at_raw is not None else None,
        worktop=worktop_from_dict(d.get("worktop")),
    )


# ─── Project builder from raw MCP-tool input ─────────────────────────────────


def build_project(payload: dict) -> CabinetProject:
    """Build a :class:`CabinetProject` from the MCP-tool payload.

    Expected shape::

        {
          "name": "kitchen_run",
          "shared": {... SharedDesign fields ...},
          "notes": "optional human-readable notes",
          "cabinets": [
            {"name": "left",   "config": {... design_cabinet args ...}},
            {"name": "center", "config": {...}},
            {"name": "right",  "config": {...}},
          ],
        }

    For each child cabinet, any key explicitly set in ``config`` and also
    present on ``shared`` is recorded as an override — so the child's value
    wins even though the shared tokens are merged in.

    When a cabinet entry carries an explicit ``overrides`` list (round-tripped
    payloads from ``project_to_dict`` / the load_project tool always do), that
    list is EXHAUSTIVE: it replaces key-presence inference entirely, so a
    shared token not named in it is applied even if the child config also
    spells out a value for that field.  To pin a child value in such a
    payload, add the field name to its ``overrides`` list.
    """
    name = str(payload["name"])
    shared = shared_from_dict(payload.get("shared"))
    notes = str(payload.get("notes", ""))
    wall_raw = payload.get("wall_width_mm")
    wall_width_mm = float(wall_raw) if wall_raw is not None else None

    cabinets: list[ProjectCabinet] = []
    shared_keys = {
        k for k in _SHARED_FIELDS + ("pull_preset",)
        if getattr(shared, k) is not None
    }
    # A shared pull_preset expands into drawer_pull, door_pull, and
    # door_pull_inset_mm at merge time, so a child that explicitly sets any
    # of those must be able to register it as an override even though the
    # shared block never names those keys directly.
    if shared.pull_preset is not None:
        shared_keys |= {"drawer_pull", "door_pull", "door_pull_inset_mm"}

    for entry in payload.get("cabinets", []):
        child_name = str(entry["name"])
        cfg_dict = dict(entry.get("config", {}))
        if "overrides" in entry:
            # Round-tripped payloads (project_to_dict / the load_project
            # tool) carry the explicit override set — honor it instead of
            # inferring from key presence.  A serialized config names EVERY
            # CabinetConfig field, so presence-inference would register every
            # shared token as a child override and shared hardware/materials
            # would silently stop applying (Movento reverting to the default
            # Tandem slide, pull presets never expanding).
            overrides = frozenset(str(k) for k in entry["overrides"] or ())
        else:
            explicit_keys = set(cfg_dict.keys())
            # A child-level pull_preset expands into both pulls (and the door
            # pull inset) inside config_from_dict, so treat them as explicitly
            # set too.
            if "pull_preset" in explicit_keys:
                explicit_keys |= {"drawer_pull", "door_pull", "door_pull_inset_mm"}
            overrides = frozenset(shared_keys & explicit_keys)
        cfg = config_from_dict(cfg_dict)
        cabinets.append(ProjectCabinet(
            name=child_name,
            config=cfg,
            overrides=overrides,
        ))

    fork_raw = payload.get("forked_from")
    at_raw   = payload.get("forked_at")
    return CabinetProject(
        name=name,
        cabinets=tuple(cabinets),
        shared=shared,
        notes=notes,
        wall_width_mm=wall_width_mm,
        forked_from=str(fork_raw) if fork_raw is not None else None,
        forked_at=str(at_raw) if at_raw is not None else None,
        worktop=worktop_from_dict(payload.get("worktop")),
    )


# ─── Cross-cabinet checks ─────────────────────────────────────────────────────


def _drawer_face_boundaries(cfg: CabinetConfig) -> tuple[float, ...]:
    """Return the sorted heights (mm from cabinet bottom) of horizontal
    drawer-face edges — the lines the eye tracks across a run. Collected
    from every column stack (or the single-column opening stack)."""
    boundaries: set[float] = set()
    stacks = [col.openings for col in cfg.columns] if cfg.columns else [cfg.openings]
    for stack in stacks:
        z = cfg.bottom_thickness
        for op in stack:
            if op.opening_type == "drawer":
                boundaries.add(round(z, 1))
                boundaries.add(round(z + op.height_mm, 1))
            z += op.height_mm
    return tuple(sorted(boundaries))


#: Fields compared across cabinets in a matched run. severity "warning" for
#: structural/material divergence, "info" for hardware (legal but notable).
_CONSISTENCY_FIELDS = (
    ("side_thickness",  "warning", "mm"),
    ("back_thickness",  "warning", "mm"),
    ("carcass_joinery", "warning", None),
    ("drawer_slide",    "info",    None),
    ("door_hinge",      "info",    None),
    ("drawer_pull",     "info",    None),
    ("door_pull",       "info",    None),
)


def check_project_consistency(project: CabinetProject) -> list[dict]:
    """Run cross-cabinet sanity checks. Returns a list of issue dicts.

    Checks:
      - depth / height match (warning) — cabinets in a flush run usually share both
      - material & joinery match (warning) / hardware match (info)
      - drawer-face alignment (info) — horizontal face lines across the run
      - wall fit (error/info) — when ``wall_width_mm`` is set, the summed run
        width must not exceed it; leftover gap is reported as info

    Warnings, not errors (except wall overflow) — cabinets *can* legally
    differ, but in a matched run they almost always shouldn't.
    """
    issues: list[dict] = []
    resolved = project.resolved()
    if not resolved:
        return issues

    base_name, base_cfg = resolved[0]

    # ── Wall fit ───────────────────────────────────────────────────────────
    if project.wall_width_mm:
        total = sum(cfg.width for _, cfg in resolved)
        gap = project.wall_width_mm - total
        if gap < 0:
            issues.append({
                "severity": "error",
                "check": "project_wall_fit",
                "message": (
                    f"Total run width ({total:.1f} mm) exceeds the available "
                    f"wall ({project.wall_width_mm:.1f} mm) by {-gap:.1f} mm."
                ),
                "value": total,
                "limit": project.wall_width_mm,
            })
        elif gap > 0:
            issues.append({
                "severity": "info",
                "check": "project_wall_fit",
                "message": (
                    f"Total run width ({total:.1f} mm) leaves a {gap:.1f} mm "
                    f"gap on a {project.wall_width_mm:.1f} mm wall — plan a "
                    "filler strip or scribe piece."
                ),
                "value": total,
                "limit": project.wall_width_mm,
            })

    # ── Material / joinery / hardware match ────────────────────────────────
    for field_name, severity, unit in _CONSISTENCY_FIELDS:
        base_val = getattr(base_cfg, field_name)
        for name, cfg in resolved[1:]:
            val = getattr(cfg, field_name)
            if val == base_val:
                continue
            fmt = (lambda v: f"{v:.1f} {unit}") if unit else (
                lambda v: getattr(v, "value", v))
            issues.append({
                "severity": severity,
                "check": f"project_{field_name}_match",
                "message": (
                    f"Cabinet {name!r} {field_name} ({fmt(val)}) differs from "
                    f"{base_name!r} ({fmt(base_val)}) — cabinets in a matched "
                    "run usually share this."
                ),
                "part_a": name,
                "part_b": base_name,
            })

    # ── Drawer-face alignment ──────────────────────────────────────────────
    # Use the first cabinet that actually *has* drawer faces as the baseline —
    # otherwise a leading door-only cabinet (which yields no boundaries) would
    # suppress the whole check and let two later cabinets' faces clash
    # unnoticed.
    align_base_name = align_base_faces = None
    for name, cfg in resolved:
        faces = _drawer_face_boundaries(cfg)
        if faces:
            align_base_name, align_base_faces = name, faces
            break
    if align_base_faces:
        for name, cfg in resolved:
            if name == align_base_name:
                continue
            faces = _drawer_face_boundaries(cfg)
            if faces and faces != align_base_faces:
                issues.append({
                    "severity": "info",
                    "check": "project_drawer_face_alignment",
                    "message": (
                        f"Drawer-face lines of {name!r} "
                        f"({', '.join(f'{z:.0f}' for z in faces)} mm) do not "
                        f"align with {align_base_name!r} "
                        f"({', '.join(f'{z:.0f}' for z in align_base_faces)} mm) — "
                        "horizontal face lines usually carry across a run."
                    ),
                    "part_a": name,
                    "part_b": align_base_name,
                })

    for name, cfg in resolved[1:]:
        if abs(cfg.depth - base_cfg.depth) > 0.5:
            issues.append({
                "severity": "warning",
                "check": "project_depth_match",
                "message": (
                    f"Cabinet {name!r} depth ({cfg.depth:.1f} mm) differs from "
                    f"{base_name!r} ({base_cfg.depth:.1f} mm) — adjacent cabinets "
                    "in a matched run usually share depth."
                ),
                "part_a": name,
                "part_b": base_name,
                "value": cfg.depth,
                "limit": base_cfg.depth,
            })
        if abs(cfg.height - base_cfg.height) > 0.5:
            issues.append({
                "severity": "warning",
                "check": "project_height_match",
                "message": (
                    f"Cabinet {name!r} height ({cfg.height:.1f} mm) differs from "
                    f"{base_name!r} ({base_cfg.height:.1f} mm) — cabinets in a "
                    "flush run usually share height."
                ),
                "part_a": name,
                "part_b": base_name,
                "value": cfg.height,
                "limit": base_cfg.height,
            })

    return issues
