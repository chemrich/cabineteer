"""Keep the counts quoted in README.md honest — regenerated from source.

Run from the repo root:

    uv run --no-group full python scripts/readme_stats.py          # rewrite
    uv run --no-group full python scripts/readme_stats.py --check   # CI gate

Every drift-prone number in the README (catalog sizes, tool count, eval
totals) is wrapped in a marker pair so it can be recomputed from the code
that owns it instead of hand-typed:

    <!--stat:pulls-->48<!--/stat:pulls--> pulls

`--check` recomputes each value and exits non-zero if the committed README
disagrees — wired into CI so a catalog change that forgets the README fails
the build. Without `--check` the script rewrites the markers in place.

The counts come straight from the same objects the engine and eval harness
use (PULLS, PRESETS, SLIDES, the MCP tool list, the eval SCENARIOS), so they
can't drift out of sync the way a typed-in figure does. Test count is left as
a rounded "1,500+" floor in the README — it stays true as the suite grows and
counting it needs a full pytest collection, so it is deliberately not managed
here.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))   # `evals` lives at the repo root, not in site-packages

README = REPO / "README.md"

# Only these counts are wrapped in <!--stat:NAME--> markers today. compute_stats
# knows how to derive more (tools, finishes); add the name here and place the
# marker in README to bring it under CI's honesty gate.
MANAGED = ("pulls", "presets", "slides", "tools", "scenarios", "assertions")


def compute_stats() -> dict[str, int]:
    """Recompute every managed count from the source of truth."""
    from cabineteer.pulls import PULLS
    from cabineteer.presets import PRESETS
    from cabineteer.hardware import SLIDES
    from cabineteer.visualize import WOOD_FINISHES
    from cabineteer import server
    from evals.scenarios import SCENARIOS

    tools = asyncio.run(server.list_tools())
    assertions = sum(len(tc.assertions) for sc in SCENARIOS for tc in sc.tool_calls)

    return {
        "pulls": len(PULLS),
        "presets": len(PRESETS),
        "slides": len(SLIDES),
        "finishes": len(WOOD_FINISHES),
        "tools": len(tools),
        "scenarios": len(SCENARIOS),
        "assertions": assertions,
    }


def _marker_re(name: str) -> re.Pattern:
    return re.compile(rf"(<!--stat:{name}-->)(.*?)(<!--/stat:{name}-->)", re.DOTALL)


def apply_stats(text: str, stats: dict[str, int], *, check: bool) -> tuple[str, list[str]]:
    """Return (new_text, problems). In check mode new_text is unused."""
    problems: list[str] = []
    out = text
    for name in MANAGED:
        value = f"{stats[name]:,}"   # thousands separators so 1150 -> "1,150"
        pat = _marker_re(name)
        matches = pat.findall(text)
        if not matches:
            problems.append(f"marker '{name}' not found in README")
            continue
        for _open, current, _close in matches:
            if current != value:
                problems.append(
                    f"stat '{name}': README says {current!r}, source says {value!r}"
                )
        out = pat.sub(rf"\g<1>{value}\g<3>", out)
    return out, problems


def main() -> None:
    check = "--check" in sys.argv[1:]
    stats = compute_stats()
    text = README.read_text(encoding="utf-8")
    new_text, problems = apply_stats(text, stats, check=check)

    if check:
        if problems:
            print("README stats are stale — run: uv run python scripts/readme_stats.py")
            for p in problems:
                print(f"  - {p}")
            sys.exit(1)
        print("README stats OK:", ", ".join(f"{k}={v}" for k, v in stats.items()))
        return

    # rewrite mode: report only real mismatches (missing markers still surface)
    missing = [p for p in problems if "not found" in p]
    if missing:
        for p in missing:
            print(f"WARNING: {p}")
    if new_text != text:
        README.write_text(new_text, encoding="utf-8")
        print("README.md updated:", ", ".join(f"{k}={v}" for k, v in stats.items()))
    else:
        print("README.md already current:", ", ".join(f"{k}={v}" for k, v in stats.items()))


if __name__ == "__main__":
    main()
