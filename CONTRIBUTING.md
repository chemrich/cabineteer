# Contributing to cabineteer

Thanks for looking under the hood. cabineteer is a deterministic casework
engine with an MCP server, a CLI, and an eval harness in front of it; this
guide is the short path to making a change that lands cleanly.

## Dev setup

```bash
git clone https://github.com/chemrich/cabineteer.git
cd cabineteer
uv sync                      # full install (CadQuery + nesting + PDF) plus dev tools
```

`uv sync` is the supported path — the repo pins a few dependency overrides in
`pyproject.toml` (`[tool.uv]`) that a plain `pip install` won't apply. If
CadQuery won't build on your machine, work in **lite** mode
(`uv pip install -e .`); the pure-Python core — design, evaluation, cutlist
BOM, CLI, MCP server, and both test suites — needs nothing native.

## The two things that must stay green

```bash
uv run pytest tests/ -v      # unit + integration (1,500+ tests)
uv run python -m evals       # 300+ scenarios of natural-language prompts + typed assertions
```

The eval harness calls the same tool handlers the MCP server and CLI expose
(no transport), so it runs in about a second. **Run both after any non-trivial
change** — a green pytest and a 100%-pass eval run are the bar for a PR. Neither
requires CadQuery.

Useful eval filters: `--tag kitchen`, `--difficulty advanced`, `--name <scenario>`,
`--list`, `--json`.

## Common changes and their conventions

- **Add a preset** (`src/cabineteer/presets.py`): register a `CabinetPreset`.
  The opening/column heights must sum to the interior, and a guard test
  requires every preset to `evaluate_cabinet` with **zero errors** — a preset
  is a *known-good* starting point, so make it one.
- **Add an eval scenario** (`evals/scenarios.py`): a `Scenario` is a
  natural-language `prompt` plus `ToolCall`s carrying typed `Assertion`s
  (`EQ`, `APPROX`, `GT`, `CONTAINS`, `NO_ERRORS`, `HAS_WARNING`, …). Cover new
  behaviour *and* pin any bug you fix with a regression scenario.
- **Add or edit a hardware spec** (`src/cabineteer/hardware.py`): dimensions,
  placement rules, and part numbers come from **manufacturer datasheets** —
  cite the source in the docstring and mirror it in
  [ATTRIBUTIONS.md](ATTRIBUTIONS.md). Don't invent a part number; leave it
  blank if unconfirmed.
- **Change a catalogue count** the README quotes (pulls, presets, slides,
  tools, eval totals): run `uv run python scripts/readme_stats.py` to update
  the `<!--stat:-->` markers. CI runs `--check` and fails on drift.

New tools register in one place — `server.TOOL_DISPATCH` — and a test asserts
`list_tools()` and the dispatch stay in sync, so the MCP server, the CLI, and
the eval harness all see the same set automatically.

## Architecture

[docs/architecture.md](docs/architecture.md) maps the modules; `CLAUDE.md` has
the design patterns and the current known-issues list. The data flow is
`hardware/joinery → cabinet/drawer/door → project → evaluation → cutlist →
server`, and CadQuery is optional at every layer (pure-Python paths are what
the tests exercise).

## Reporting bugs

Open a [GitHub Issue](https://github.com/chemrich/cabineteer/issues) with the
prompt or tool call you ran and what you expected. A failing eval scenario or a
short `cabineteer-cli run <tool> --json '…'` reproduction is the fastest way to
get it fixed — and often becomes the regression test.
