"""
Evaluation harness for the cabinet-design MCP server.

Runs each scenario's tool calls through the server handlers, evaluates
assertions, collects timing data, and produces a structured report.

The harness is pure Python — no actual MCP transport is involved.  It imports
the server handler functions directly, which means the eval runs in < 1 s even
for the full scenario catalogue.

Usage from Python::

    from evals.harness import run_all, print_report
    report = run_all()
    print_report(report)

Usage from the CLI::

    python -m evals                  # run everything
    python -m evals --tag kitchen    # only kitchen scenarios
    python -m evals --json           # machine-readable output
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .scenarios import (
    Assertion,
    Op,
    Scenario,
    ToolCall,
    SCENARIOS,
)

# The name→handler map is owned by the server (its MCP path and the CLI use the
# same dict), so the harness drives the exact tools the server exposes.
from cabineteer.server import TOOL_DISPATCH

# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class AssertionResult:
    assertion: Assertion
    passed: bool
    actual: Any = None
    error: str = ""


@dataclass
class ToolCallResult:
    tool_call: ToolCall
    data: dict | None = None
    error: str = ""
    duration_ms: float = 0.0
    assertion_results: list[AssertionResult] = field(default_factory=list)
    saved_vars: dict[str, Any] = field(default_factory=dict)
    # Context variables saved from this step's result (populated when save_as is set)

    @property
    def passed(self) -> bool:
        return (
            self.error == ""
            and all(a.passed for a in self.assertion_results)
        )

    @property
    def assertions_passed(self) -> int:
        return sum(1 for a in self.assertion_results if a.passed)

    @property
    def assertions_total(self) -> int:
        return len(self.assertion_results)


@dataclass
class ScenarioResult:
    scenario: Scenario
    tool_results: list[ToolCallResult] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def passed(self) -> bool:
        return all(t.passed for t in self.tool_results)

    @property
    def assertions_passed(self) -> int:
        return sum(t.assertions_passed for t in self.tool_results)

    @property
    def assertions_total(self) -> int:
        return sum(t.assertions_total for t in self.tool_results)

    @property
    def tool_calls_passed(self) -> int:
        return sum(1 for t in self.tool_results if t.passed)


@dataclass
class EvalReport:
    results: list[ScenarioResult] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def scenarios_passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def scenarios_total(self) -> int:
        return len(self.results)

    @property
    def assertions_passed(self) -> int:
        return sum(r.assertions_passed for r in self.results)

    @property
    def assertions_total(self) -> int:
        return sum(r.assertions_total for r in self.results)

    @property
    def pass_rate(self) -> float:
        if self.assertions_total == 0:
            return 1.0
        return self.assertions_passed / self.assertions_total

    @property
    def score(self) -> float:
        """Weighted score: each scenario contributes equally regardless of assertion count."""
        if not self.results:
            return 1.0
        per_scenario = []
        for r in self.results:
            if r.assertions_total == 0:
                per_scenario.append(1.0)
            else:
                per_scenario.append(r.assertions_passed / r.assertions_total)
        return sum(per_scenario) / len(per_scenario)

    def to_dict(self) -> dict:
        return {
            "summary": {
                "scenarios_passed": self.scenarios_passed,
                "scenarios_total":  self.scenarios_total,
                "assertions_passed": self.assertions_passed,
                "assertions_total": self.assertions_total,
                "pass_rate":        round(self.pass_rate, 4),
                "score":            round(self.score, 4),
                "duration_ms":      round(self.duration_ms, 1),
            },
            "scenarios": [
                {
                    "name":     r.scenario.name,
                    "passed":   r.passed,
                    "tags":     r.scenario.tags,
                    "difficulty": r.scenario.difficulty,
                    "assertions_passed": r.assertions_passed,
                    "assertions_total":  r.assertions_total,
                    "duration_ms": round(r.duration_ms, 1),
                    "failures": [
                        {
                            "tool": tr.tool_call.tool,
                            "label": tr.tool_call.label,
                            "assertion": ar.assertion.path,
                            "op":       ar.assertion.op.value,
                            "expected": ar.assertion.expected,
                            "actual":   ar.actual,
                            "error":    ar.error,
                        }
                        for tr in r.tool_results
                        for ar in tr.assertion_results
                        if not ar.passed
                    ] + (
                        [{"tool": tr.tool_call.tool, "label": tr.tool_call.label,
                          "error": tr.error}
                         for tr in r.tool_results if tr.error]
                    ),
                }
                for r in self.results
            ],
        }


# ─── Assertion evaluator ─────────────────────────────────────────────────────

class _MissingSentinel:
    """Singleton sentinel returned when a path does not resolve."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "<MISSING>"

    def __bool__(self):
        return False


MISSING = _MissingSentinel()


_BRACKET_RE = re.compile(r"^(.*?)\[(\d+)\](.*)$")


def _tokenise_path(path: str) -> list[str]:
    """Split a path into navigation tokens, handling both notations.

    ``"a.b.0.c"``          → ``["a", "b", "0", "c"]``
    ``"a.b[0].c"``         → ``["a", "b", "0", "c"]``
    ``"a[0][1].b"``        → ``["a", "0", "1", "b"]``

    Both dot-integer and bracket-integer styles resolve identically so
    scenario authors can use whichever feels natural.
    """
    tokens: list[str] = []
    for segment in path.split("."):
        # Expand any bracket subscripts within this segment.
        remainder = segment
        while True:
            m = _BRACKET_RE.match(remainder)
            if not m:
                break
            prefix, idx, remainder = m.group(1), m.group(2), m.group(3)
            if prefix:
                tokens.append(prefix)
            tokens.append(idx)
        if remainder:
            tokens.append(remainder)
    return tokens


def _resolve_path(data: Any, path: str) -> Any:
    """Walk a path into a nested dict/list structure.

    Supports both dot-integer (``"stack.0.type"``) and bracket
    (``"stack[0].type"``) notation for list indices — they are equivalent.
    Returns ``MISSING`` if the path does not exist.
    """
    if not path:
        return data
    current = data
    for key in _tokenise_path(path):
        if isinstance(current, dict):
            current = current.get(key, MISSING)
        elif isinstance(current, list):
            try:
                current = current[int(key)]
            except (ValueError, IndexError):
                return MISSING
        else:
            return MISSING
        if current is MISSING:
            return MISSING
    return current


def evaluate_assertion(data: dict, assertion: Assertion) -> AssertionResult:
    """Evaluate a single assertion against tool output."""
    value = _resolve_path(data, assertion.path)
    missing = value is MISSING

    try:
        op = assertion.op
        exp = assertion.expected

        if op == Op.HAS_KEY:
            if isinstance(exp, str):
                # Nested form: the value at `path` must be a dict containing `exp`.
                passed = (
                    not missing
                    and isinstance(value, dict)
                    and exp in value
                )
            else:
                # Bare-existence form (expected is None or True): the path itself
                # must resolve to something.
                passed = not missing
        elif missing:
            return AssertionResult(assertion, False, actual="<MISSING>",
                                   error=f"Path '{assertion.path}' not found in result")
        elif op == Op.EQ:
            passed = (value == exp)
        elif op == Op.APPROX:
            passed = abs(float(value) - float(exp)) < 0.15
        elif op == Op.GT:
            passed = float(value) > float(exp)
        elif op == Op.GTE:
            passed = float(value) >= float(exp)
        elif op == Op.LT:
            passed = float(value) < float(exp)
        elif op == Op.LTE:
            passed = float(value) <= float(exp)
        elif op == Op.IN:
            passed = value in exp
        elif op == Op.CONTAINS:
            passed = exp in value
        elif op == Op.LEN_EQ:
            passed = len(value) == int(exp)
        elif op == Op.LEN_GTE:
            passed = len(value) >= int(exp)
        elif op == Op.IS_TRUE:
            passed = bool(value) is True
        elif op == Op.IS_FALSE:
            passed = bool(value) is False
        elif op == Op.NO_ERRORS:
            passed = _resolve_path(data, "summary.errors") == 0
        elif op in (Op.HAS_ERROR, Op.HAS_WARNING):
            sev = "error" if op == Op.HAS_ERROR else "warning"
            if assertion.expected is not None:
                # A string expected value names the check that must have
                # fired — the argument used to be silently ignored, so
                # "the error is X" passed on ANY error (review 2026-07-29).
                issues = value if isinstance(value, list) else (
                    _resolve_path(data, "issues") or [])
                passed = any(
                    isinstance(i, dict)
                    and i.get("severity") == sev
                    and i.get("check") == assertion.expected
                    for i in issues)
            else:
                passed = (_resolve_path(data, f"summary.{sev}s") or 0) > 0
        else:
            return AssertionResult(assertion, False, error=f"Unknown op: {op}")

        return AssertionResult(assertion, passed, actual=value)

    except Exception as exc:
        return AssertionResult(assertion, False, actual=value, error=str(exc))


# ─── Runner ───────────────────────────────────────────────────────────────────

def _run_sync(coro):
    """Run a coroutine; reuse an existing loop if available."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def _error_result(tc: ToolCall, error: str, duration_ms: float = 0.0) -> ToolCallResult:
    """Build a ToolCallResult for a failed tool call.

    Every declared assertion is recorded as failed so that a tool error can
    never make ``assertions_total`` shrink — otherwise ``pass_rate`` / ``score``
    could read 100 % while the scenario as a whole failed.
    """
    return ToolCallResult(
        tc,
        error=error,
        duration_ms=duration_ms,
        assertion_results=[
            AssertionResult(a, passed=False, error=f"tool call errored: {error}")
            for a in tc.assertions
        ],
    )


def run_tool_call(
    tc: ToolCall,
    context: dict[str, Any] | None = None,
) -> ToolCallResult:
    """Execute a single tool call and evaluate its assertions.

    Parameters
    ----------
    tc : ToolCall
        The tool call specification (tool name, args, assertions).
    context : dict, optional
        Shared scenario context dict.  When provided:
        - Values named in ``tc.context_args`` are resolved from it and merged
          into the args before the call (applying ``tc.arg_transforms`` if set).
        - Values named in ``tc.save_as`` are extracted from the result and
          written back into the context for downstream steps.
    """
    handler = TOOL_DISPATCH.get(tc.tool)
    if handler is None:
        return _error_result(tc, f"Unknown tool: {tc.tool}")

    # ── Resolve context-injected args ────────────────────────────────────────
    resolved_args = dict(tc.args)
    if context is not None and tc.context_args:
        for arg_name, ctx_var in tc.context_args.items():
            if ctx_var not in context:
                return _error_result(
                    tc,
                    f"Context variable '{ctx_var}' not set by a prior step",
                )
            value = context[ctx_var]
            transform = tc.arg_transforms.get(arg_name)
            if transform is not None:
                value = transform(value)
            resolved_args[arg_name] = value

    t0 = time.perf_counter()
    try:
        result = _run_sync(handler(resolved_args))

        # Parse the TextContent response inside the try so a malformed or empty
        # response records a single tool failure instead of crashing the run.
        if not result or getattr(result[0], "text", None) is None:
            duration_ms = (time.perf_counter() - t0) * 1000
            return _error_result(tc, "empty or non-text response from handler",
                                 duration_ms)

        text = result[0].text
        if text.startswith("ERROR:"):
            duration_ms = (time.perf_counter() - t0) * 1000
            return _error_result(tc, text, duration_ms)

        data = json.loads(text)
        duration_ms = (time.perf_counter() - t0) * 1000
    except json.JSONDecodeError as exc:
        duration_ms = (time.perf_counter() - t0) * 1000
        return _error_result(tc, f"JSON parse error: {exc}", duration_ms)
    except Exception as exc:
        duration_ms = (time.perf_counter() - t0) * 1000
        return _error_result(tc, f"{type(exc).__name__}: {exc}", duration_ms)

    # ── Save context variables from this result ──────────────────────────────
    saved: dict[str, Any] = {}
    if context is not None and tc.save_as:
        for var_name, path in tc.save_as.items():
            value = _resolve_path(data, path)
            if value is not MISSING:
                context[var_name] = value
                saved[var_name] = value

    # Evaluate assertions
    assertion_results = [evaluate_assertion(data, a) for a in tc.assertions]

    return ToolCallResult(
        tool_call=tc,
        data=data,
        duration_ms=duration_ms,
        assertion_results=assertion_results,
        saved_vars=saved,
    )


def run_scenario(scenario: Scenario) -> ScenarioResult:
    """Run all tool calls in a scenario and return results.

    A fresh ``context`` dict is created for each scenario and passed to every
    ``run_tool_call`` invocation.  Steps that declare ``save_as`` populate it;
    steps that declare ``context_args`` read from it.  The context is local to
    this scenario and discarded after all steps complete.
    """
    t0 = time.perf_counter()
    context: dict[str, Any] = {}
    tool_results = [run_tool_call(tc, context) for tc in scenario.tool_calls]
    duration_ms = (time.perf_counter() - t0) * 1000
    return ScenarioResult(scenario=scenario, tool_results=tool_results,
                          duration_ms=duration_ms)


def run_all(
    scenarios: list[Scenario] | None = None,
    tags: list[str] | None = None,
    difficulty: str | None = None,
) -> EvalReport:
    """Run the eval suite and return a report.

    Parameters
    ----------
    scenarios : list, optional
        Explicit list of scenarios.  If None, uses the full catalogue.
    tags : list[str], optional
        Only run scenarios matching any of these tags.
    difficulty : str, optional
        Only run scenarios at this difficulty level.
    """
    pool = SCENARIOS if scenarios is None else scenarios

    if tags:
        pool = [s for s in pool if any(t in s.tags for t in tags)]
    if difficulty:
        pool = [s for s in pool if s.difficulty == difficulty]

    # Sandbox HOME for the whole run: every tool-side write (projects,
    # cutlists, assembly docs) roots at Path.home()/.cabineteer, and
    # running against the user's real store both pollutes it and makes
    # count/list assertions state-dependent (review 2026-07-29 M9).
    import tempfile
    from pathlib import Path as _Path

    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="cabineteer-evals-") as tmp:
        _orig_home = _Path.home
        _Path.home = lambda: _Path(tmp)  # type: ignore[method-assign]
        try:
            results = [run_scenario(s) for s in pool]
        finally:
            _Path.home = _orig_home
    duration_ms = (time.perf_counter() - t0) * 1000

    return EvalReport(results=results, duration_ms=duration_ms)


# ─── Reporter ─────────────────────────────────────────────────────────────────

def print_report(report: EvalReport, verbose: bool = False) -> None:
    """Print a human-readable eval report to stdout."""
    print()
    print("=" * 72)
    print("  CABINETEER EVALUATION REPORT")
    print("=" * 72)
    print()
    print(f"  Scenarios:   {report.scenarios_passed}/{report.scenarios_total} passed")
    print(f"  Assertions:  {report.assertions_passed}/{report.assertions_total} passed")
    print(f"  Pass rate:   {report.pass_rate:.1%}")
    print(f"  Score:       {report.score:.1%}")
    print(f"  Duration:    {report.duration_ms:.0f} ms")
    print()

    # Per-scenario summary
    for r in report.results:
        status = "PASS" if r.passed else "FAIL"
        marker = "  " if r.passed else ">>"
        print(f"  {marker} [{status}] {r.scenario.name}"
              f"  ({r.assertions_passed}/{r.assertions_total})"
              f"  {r.duration_ms:.0f}ms"
              f"  [{', '.join(r.scenario.tags)}]")

        if not r.passed or verbose:
            for tr in r.tool_results:
                if not tr.passed or verbose:
                    label = tr.tool_call.label or tr.tool_call.tool
                    if tr.error:
                        print(f"       {label}: {tr.error}")
                    for ar in tr.assertion_results:
                        if not ar.passed:
                            desc = ar.assertion.description or ar.assertion.path
                            print(f"       FAIL  {desc}")
                            print(f"             {ar.assertion.op.value} "
                                  f"expected={ar.assertion.expected!r} "
                                  f"actual={ar.actual!r}")
                            if ar.error:
                                print(f"             error: {ar.error}")

    print()
    print("-" * 72)

    # Tag breakdown
    tag_stats: dict[str, tuple[int, int]] = {}
    for r in report.results:
        for tag in r.scenario.tags:
            p, t = tag_stats.get(tag, (0, 0))
            tag_stats[tag] = (p + (1 if r.passed else 0), t + 1)

    if tag_stats:
        print("  By tag:")
        for tag in sorted(tag_stats):
            p, t = tag_stats[tag]
            print(f"    {tag:20s} {p}/{t}")

    # Difficulty breakdown
    diff_stats: dict[str, tuple[int, int]] = {}
    for r in report.results:
        d = r.scenario.difficulty
        p, t = diff_stats.get(d, (0, 0))
        diff_stats[d] = (p + (1 if r.passed else 0), t + 1)

    if diff_stats:
        print("  By difficulty:")
        for d in ("basic", "standard", "advanced"):
            if d in diff_stats:
                p, t = diff_stats[d]
                print(f"    {d:20s} {p}/{t}")

    print("=" * 72)
    print()
