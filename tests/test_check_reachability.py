"""Every evaluator check either fires, or says in writing why it cannot.

WHY
---
Two evaluator failure shapes have reached the bench:

  **circular** — the checked quantity is derived from the constant it is
  compared against, so the branch can only pass.  ``check_drawer_carcass_
  clearances`` compared a side gap against the clearance the gap was computed
  from; the branches were eventually deleted as dead code, and the comment
  recording that deletion is the only surviving evidence that the real
  constraint was ever checked.  That is #91.

  **unwired** — the check is correct and nothing calls it on the path the
  paper takes.  The drawer-joinery checks lived on the CadQuery path only for
  months, and no cutlist goes through CadQuery.

Both are invisible in a green suite: a check that cannot fire looks exactly
like a check that has nothing to complain about.

WHAT THIS FILE DOES
-------------------
It enumerates every ``check_*`` in ``evaluation.py`` and requires each to be
in exactly one of two buckets:

  ``FIRES`` — an input is given here that makes it produce an Issue, and this
      module runs it and asserts it does.
  ``UNREACHABLE_BY_DESIGN`` — a written justification for why no user input
      can reach it.

A check in neither bucket fails the manifest.  A check in ``FIRES`` that stops
firing fails.  A check that is deleted or renamed fails.  So the manifest
cannot rot quietly, which is the property the deleted-as-dead-code comment
did not have.

Adding a check to ``UNREACHABLE_BY_DESIGN`` is a claim you are making in
writing.  Prefer finding the input.
"""

from __future__ import annotations

import inspect

import pytest

from cabineteer import evaluation
from cabineteer.cabinet import CabinetConfig, ColumnConfig, to_opening
from cabineteer.door import DoorConfig
from cabineteer.drawer import DrawerConfig
from cabineteer.joinery import CarcassJoinery, DrawerJoineryStyle


def _cfg(**kw) -> CabinetConfig:
    args = dict(width=800.0, height=720.0, depth=457.0,
                side_thickness=18.0, bottom_thickness=18.0, top_thickness=18.0,
                back_thickness=6.0, shelf_thickness=18.0,
                drawer_box_thickness=12.0,
                drawer_slide="blum_tandem_plus_563h",
                drawer_joinery=DrawerJoineryStyle.DRAWER_LOCK,
                carcass_joinery=CarcassJoinery.FLOATING_TENON,
                openings=[(133.0, "drawer"), (110.0, "drawer")])
    args.update(kw)
    return CabinetConfig(**args)


def _door(**kw) -> DoorConfig:
    args = dict(opening_width=400.0, opening_height=600.0)
    args.update(kw)
    return DoorConfig(**args)


def _drawer(**kw) -> DrawerConfig:
    args = dict(opening_width=345.0, opening_height=133.0, opening_depth=448.0,
                side_thickness=12.0, front_back_thickness=12.0,
                slide_key="blum_tandem_plus_563h",
                joinery_style=DrawerJoineryStyle.DRAWER_LOCK)
    args.update(kw)
    return DrawerConfig(**args)


# ─── Bucket one: inputs that make each check fire ─────────────────────────
#
# Each value is a zero-argument callable returning the arguments to pass. The
# check must produce at least one Issue. Keep the input MINIMAL and obviously
# wrong — the point is to demonstrate the branch is live, not to test the
# threshold.

FIRES = {
    "check_cumulative_heights":
        lambda: (_cfg(height=400.0,
                      openings=[(300.0, "drawer"), (300.0, "drawer")]),),
    "check_drawer_carcass_clearances":
        lambda: (_cfg(width=100.0),),
    "check_drawer_hardware_clearances":
        lambda: (_drawer(opening_height=40.0),),
    "check_drawer_joinery":
        lambda: (_drawer(corner_lip_mm=11.0),),
    "check_shelf_deflection":
        lambda: (1200.0, 400.0, 6.0, 40.0),
    "check_back_panel_fit":
        lambda: (_cfg(carcass_joinery=CarcassJoinery.DADO_RABBET,
                      back_thickness=25.0),),
    "check_cabinet_pull_consistency":
        # Two real catalogue keys with genuinely different PullSpec.style.
        lambda: (_cfg(drawer_pull="rockler-wnl-160",      # Contemporary
                      door_pull="rockler-42250",           # Mission
                      openings=[(133.0, "drawer"), (500.0, "door")]),),
    "check_carcass_joinery":
        lambda: (_cfg(carcass_joinery=CarcassJoinery.FLOATING_TENON,
                      side_thickness=6.0),),
    "check_column_stack_heights":
        lambda: (_cfg(openings=[], columns=[
                     ColumnConfig(width_mm=380.0,
                                  openings=(to_opening([100, "drawer"]),))]),),
    "check_column_widths":
        lambda: (_cfg(openings=[], columns=[
                     ColumnConfig(width_mm=100.0,
                                  openings=(to_opening([684, "open"]),)),
                     ColumnConfig(width_mm=100.0,
                                  openings=(to_opening([684, "open"]),))]),),
    "check_dado_alignment":
        # A bottom thicker than the side it is housed in: the dado would be
        # wider than the panel it is cut into.
        lambda: (_cfg(bottom_thickness=25.0, side_thickness=18.0),),
    "check_drawer_stack_order":
        lambda: (_cfg(openings=[(110.0, "drawer"), (300.0, "drawer")]),),
    "check_edge_band_face_gap":
        lambda: (_cfg(edge_band_mode="hot_melt", edge_band_thickness_mm=3.0,
                      face_gap_mm=2.0),),
    "check_domino_layout":
        lambda: (__import__("cabineteer.joinery", fromlist=["DEFAULT_DOMINO"])
                 .DEFAULT_DOMINO, 40.0, 18.0),
    "check_pocket_screw_layout":
        lambda: (__import__("cabineteer.joinery", fromlist=["DEFAULT_POCKET_SCREW"])
                 .DEFAULT_POCKET_SCREW, 40.0, 18.0),
    "check_door_dimensions":
        lambda: (_door(opening_height=60.0),),
    "check_door_hinge_count":
        lambda: (_door(opening_height=2400.0, door_weight_kg=60.0),),
    "check_door_pair_width":
        lambda: (_door(opening_width=1600.0, num_doors=2),),
    "check_back_style":
        lambda: (_cfg(back_style="under_top",
                      carcass_joinery=CarcassJoinery.DADO_RABBET),),
    "check_back_capture":
        lambda: (_cfg(back_capture="half_lap", back_thickness=6.0),),
    "check_face_top_bottom_style":
        lambda: (_cfg(face_top_style="bogus"),),
    "check_miter_corners":
        lambda: (_cfg(carcass_corner_style="miter",
                      carcass_joinery=CarcassJoinery.POCKET_SCREW),),
    "check_edge_banding":
        lambda: (_cfg(edge_band_mode="hardwood", edge_band_thickness_mm=25.0),),
    "check_face_clearances":
        # Two bays sharing a divider thinner than the two overlays that claim
        # it — the faces physically collide at the joint.
        lambda: ([_cfg(openings=[(684.0, "drawer")]),
                  _cfg(openings=[(684.0, "drawer")])],
                 8.0, None, 6.0),
    "check_door_overlay_collisions":
        lambda: (_cfg(width=800.0, side_thickness=6.0,
                      door_hinge="blum_clip_top_blumotion_110_full",
                      columns=[
                          ColumnConfig(width_mm=380.0,
                                       openings=(to_opening([684, "door"]),)),
                          ColumnConfig(width_mm=376.0,
                                       openings=(to_opening([684, "door"]),))],
                      openings=[]),),
}

# ─── Bucket two: checks no user input can reach, with the reason ──────────
#
# Every entry is an assertion in writing. If you cannot write a convincing
# sentence, the check is probably reachable and belongs in FIRES.

UNREACHABLE_BY_DESIGN = {
    "check_interference":
        "CadQuery-only. Takes an assembled cq.Assembly, which no MCP tool "
        "constructs on the paper path; evaluate_cabinet passes one only when "
        "a caller supplies it.",
    "check_drawer_in_opening":
        "CadQuery-only, same reason — it measures a built assembly's bounding "
        "box. The pure-Python equivalent is check_drawer_carcass_clearances.",
    "check_drawer_pull":
        "REACHABLE IN PRINCIPLE, NOT WIRED. Sits behind `if drawer_assemblies:` "
        "in evaluate_cabinet and no MCP tool passes assemblies, so a 316 mm "
        "pull on a 300 mm face evaluates clean. This is the same wiring gap "
        "#89 fixed for the drawer-joinery checks. Tracked as a review finding; "
        "move to FIRES when it is wired.",
    "check_door_pull":
        "Same as check_drawer_pull — correct, and unreachable from the paper.",
}


def _all_checks() -> dict:
    return {n: f for n, f in vars(evaluation).items()
            if n.startswith("check_") and inspect.isfunction(f)
            and f.__module__ == evaluation.__name__}


def test_every_check_is_in_exactly_one_bucket():
    """The manifest cannot rot: a new, renamed or deleted check fails here."""
    checks = set(_all_checks())
    listed = set(FIRES) | set(UNREACHABLE_BY_DESIGN)

    unlisted = sorted(checks - listed)
    assert not unlisted, (
        "these checks are in neither bucket — give each one an input that "
        "makes it fire, or a written reason it cannot be reached: "
        f"{unlisted}")

    stale = sorted(listed - checks)
    assert not stale, (
        f"the manifest names checks that no longer exist: {stale}")

    both = sorted(set(FIRES) & set(UNREACHABLE_BY_DESIGN))
    assert not both, f"listed in both buckets: {both}"


@pytest.mark.parametrize("name", sorted(FIRES))
def test_the_check_actually_fires(name):
    """Prove the branch is live by making it complain."""
    fn = _all_checks()[name]
    issues = fn(*FIRES[name]())
    assert issues, (
        f"{name} produced no Issue for an input chosen to break it. Either "
        f"the input no longer reaches the branch, or the check has gone "
        f"circular — compare its expected value against where the actual "
        f"value comes from.")


@pytest.mark.parametrize("name", sorted(UNREACHABLE_BY_DESIGN))
def test_the_unreachable_justification_is_a_real_sentence(name):
    """A one-word excuse is not a justification."""
    reason = UNREACHABLE_BY_DESIGN[name]
    assert len(reason) > 60 and "." in reason, (
        f"{name}'s justification is too thin to audit: {reason!r}")
