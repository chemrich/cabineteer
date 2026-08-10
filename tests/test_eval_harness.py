"""
Tests for the eval harness — assertion evaluator, runner, and reporter.
"""

import json

import pytest

from evals.scenarios import (
    ALL_TAGS,
    Assertion,
    Op,
    Scenario,
    ToolCall,
    SCENARIOS,
    scenarios_by_tag,
    scenarios_by_difficulty,
    scenario_by_name,
)
from evals.harness import (
    MISSING,
    EvalReport,
    ScenarioResult,
    ToolCallResult,
    AssertionResult,
    evaluate_assertion,
    run_tool_call,
    run_scenario,
    run_all,
    _resolve_path,
)


# ─── _resolve_path ────────────────────────────────────────────────────────────

class TestResolvePath:
    def test_simple_key(self):
        assert _resolve_path({"a": 1}, "a") == 1

    def test_nested_key(self):
        assert _resolve_path({"a": {"b": 2}}, "a.b") == 2

    def test_triple_nested(self):
        assert _resolve_path({"a": {"b": {"c": 3}}}, "a.b.c") == 3

    def test_list_index(self):
        assert _resolve_path({"a": [10, 20, 30]}, "a.1") == 20

    def test_list_then_key(self):
        data = {"items": [{"name": "x"}, {"name": "y"}]}
        assert _resolve_path(data, "items.0.name") == "x"

    def test_missing_key_returns_sentinel(self):
        result = _resolve_path({"a": 1}, "b")
        assert result is MISSING

    def test_missing_nested_returns_sentinel(self):
        result = _resolve_path({"a": {"b": 1}}, "a.c")
        assert result is MISSING


# ─── evaluate_assertion ───────────────────────────────────────────────────────

class TestEvaluateAssertion:
    def _data(self):
        return {
            "width": 600,
            "height": 720,
            "name": "test",
            "items": [1, 2, 3],
            "nested": {"val": 42.0, "flag": True, "off": False},
            "summary": {"errors": 0, "warnings": 2},
        }

    def test_eq_pass(self):
        r = evaluate_assertion(self._data(), Assertion("width", Op.EQ, 600))
        assert r.passed

    def test_eq_fail(self):
        r = evaluate_assertion(self._data(), Assertion("width", Op.EQ, 500))
        assert not r.passed

    def test_approx_pass(self):
        r = evaluate_assertion(self._data(), Assertion("nested.val", Op.APPROX, 42.05))
        assert r.passed

    def test_approx_fail(self):
        r = evaluate_assertion(self._data(), Assertion("nested.val", Op.APPROX, 50.0))
        assert not r.passed

    def test_gt_pass(self):
        r = evaluate_assertion(self._data(), Assertion("width", Op.GT, 500))
        assert r.passed

    def test_gt_fail(self):
        r = evaluate_assertion(self._data(), Assertion("width", Op.GT, 600))
        assert not r.passed

    def test_gte_pass_equal(self):
        r = evaluate_assertion(self._data(), Assertion("width", Op.GTE, 600))
        assert r.passed

    def test_lt_pass(self):
        r = evaluate_assertion(self._data(), Assertion("width", Op.LT, 700))
        assert r.passed

    def test_lte_pass(self):
        r = evaluate_assertion(self._data(), Assertion("width", Op.LTE, 600))
        assert r.passed

    def test_has_key_pass(self):
        r = evaluate_assertion(self._data(), Assertion("nested", Op.HAS_KEY, True))
        assert r.passed

    def test_has_key_fail(self):
        r = evaluate_assertion(self._data(), Assertion("nonexistent", Op.HAS_KEY, True))
        assert not r.passed

    def test_has_key_nested_pass(self):
        # expected is a string → the value at `path` must be a dict with that key.
        r = evaluate_assertion(self._data(), Assertion("nested", Op.HAS_KEY, "flag"))
        assert r.passed

    def test_has_key_nested_missing_child_fails(self):
        r = evaluate_assertion(self._data(), Assertion("nested", Op.HAS_KEY, "absent"))
        assert not r.passed

    def test_has_key_nested_on_non_dict_fails(self):
        # `items` resolves to a list, not a dict — nested HAS_KEY must fail.
        r = evaluate_assertion(self._data(), Assertion("items", Op.HAS_KEY, "0"))
        assert not r.passed

    def test_has_key_nested_missing_parent_fails(self):
        r = evaluate_assertion(self._data(), Assertion("nope", Op.HAS_KEY, "child"))
        assert not r.passed

    def test_len_eq_pass(self):
        r = evaluate_assertion(self._data(), Assertion("items", Op.LEN_EQ, 3))
        assert r.passed

    def test_len_gte_pass(self):
        r = evaluate_assertion(self._data(), Assertion("items", Op.LEN_GTE, 2))
        assert r.passed

    def test_is_true_pass(self):
        r = evaluate_assertion(self._data(), Assertion("nested.flag", Op.IS_TRUE))
        assert r.passed

    def test_is_false_pass(self):
        r = evaluate_assertion(self._data(), Assertion("nested.off", Op.IS_FALSE))
        assert r.passed

    def test_no_errors_pass(self):
        r = evaluate_assertion(self._data(), Assertion("", Op.NO_ERRORS))
        assert r.passed

    def test_has_warning_pass(self):
        r = evaluate_assertion(self._data(), Assertion("", Op.HAS_WARNING))
        assert r.passed

    def test_missing_path_fails(self):
        r = evaluate_assertion(self._data(), Assertion("nonexistent.deep", Op.EQ, 1))
        assert not r.passed
        assert "not found" in r.error

    def test_contains_pass(self):
        r = evaluate_assertion(self._data(), Assertion("items", Op.CONTAINS, 2))
        assert r.passed

    def test_in_pass(self):
        r = evaluate_assertion(self._data(), Assertion("name", Op.IN, ["test", "other"]))
        assert r.passed


# ─── Scenario catalogue integrity ────────────────────────────────────────────

class TestScenarioCatalogue:
    def test_catalogue_not_empty(self):
        assert len(SCENARIOS) > 0

    def test_all_scenarios_have_names(self):
        for s in SCENARIOS:
            assert s.name, "Scenario missing name"

    def test_all_scenarios_have_prompts(self):
        for s in SCENARIOS:
            assert s.prompt, f"Scenario {s.name} missing prompt"

    def test_all_scenarios_have_tool_calls(self):
        for s in SCENARIOS:
            assert len(s.tool_calls) > 0, f"Scenario {s.name} has no tool calls"

    def test_all_tool_calls_reference_valid_tools(self):
        from evals.harness import TOOL_DISPATCH
        for s in SCENARIOS:
            for tc in s.tool_calls:
                assert tc.tool in TOOL_DISPATCH, (
                    f"Scenario {s.name} references unknown tool: {tc.tool}"
                )

    def test_dispatch_matches_server_tool_list(self):
        """Every MCP tool the server exposes must be reachable from the harness.

        Guards against dispatch→server drift: if a new tool is added to the
        server but not wired into TOOL_DISPATCH (as visualize_cabinet once was),
        it silently gets zero eval coverage.
        """
        import re
        import cabineteer.server as srv
        from evals.harness import TOOL_DISPATCH

        src = open(srv.__file__, encoding="utf-8").read()
        server_tools = set(re.findall(r'types\.Tool\(\s*name="([^"]+)"', src))
        assert server_tools, "could not extract any tool names from server.py"
        missing = server_tools - set(TOOL_DISPATCH)
        assert not missing, f"Server tools missing from TOOL_DISPATCH: {sorted(missing)}"

    def test_unique_names(self):
        names = [s.name for s in SCENARIOS]
        assert len(names) == len(set(names)), "Duplicate scenario names"

    def test_all_tags_discoverable(self):
        assert len(ALL_TAGS) > 0

    def test_scenarios_by_tag(self):
        for tag in ALL_TAGS:
            found = scenarios_by_tag(tag)
            assert len(found) > 0, f"No scenarios for tag '{tag}'"

    def test_scenario_by_name(self):
        s = scenario_by_name("standard_base_cabinet")
        assert s.name == "standard_base_cabinet"

    def test_scenario_by_name_missing(self):
        with pytest.raises(KeyError):
            scenario_by_name("nonexistent_scenario_xyz")


# ─── Tool call execution ─────────────────────────────────────────────────────

class TestRunToolCall:
    def test_basic_design_cabinet(self):
        tc = ToolCall(
            tool="design_cabinet",
            args={"width": 600, "height": 720, "depth": 550},
            assertions=[
                Assertion("exterior.width_mm", Op.EQ, 600),
            ],
        )
        result = run_tool_call(tc)
        assert result.passed
        assert result.data is not None
        assert result.duration_ms > 0

    def test_unknown_tool_returns_error(self):
        tc = ToolCall(tool="nonexistent_tool", args={})
        result = run_tool_call(tc)
        assert not result.passed
        assert "Unknown tool" in result.error

    def test_error_emits_failed_assertions(self):
        # An unknown tool with declared assertions must record them as failed,
        # so assertions_total does not shrink (which would let pass_rate read
        # 100% while the scenario actually failed).
        tc = ToolCall(
            tool="nonexistent_tool",
            args={},
            assertions=[
                Assertion("a", Op.EQ, 1),
                Assertion("b", Op.EQ, 2),
            ],
        )
        result = run_tool_call(tc)
        assert result.error != ""
        assert result.assertions_total == 2
        assert result.assertions_passed == 0
        assert not result.passed

    def test_tool_runtime_error_emits_failed_assertions(self):
        # A handler that raises (invalid args) must also account its assertions.
        tc = ToolCall(
            tool="design_drawer",
            args={"opening_width": 100},
            assertions=[Assertion("box_width_mm", Op.GT, 0)],
        )
        result = run_tool_call(tc)
        assert result.error != ""
        assert result.assertions_total == 1
        assert result.assertions_passed == 0

    def test_tool_error_returns_error(self):
        # Invalid args should produce an error
        tc = ToolCall(tool="design_drawer", args={"opening_width": 100})
        result = run_tool_call(tc)
        assert result.error != ""

    def test_assertions_evaluated(self):
        tc = ToolCall(
            tool="list_hardware",
            args={"category": "hinges"},
            assertions=[
                Assertion("hinges", Op.HAS_KEY, True),
                Assertion("slides", Op.HAS_KEY, True),  # should fail — hinges only
            ],
        )
        result = run_tool_call(tc)
        assert result.assertions_total == 2
        assert result.assertions_passed == 1
        assert not result.passed  # one assertion failed


# ─── Scenario execution ──────────────────────────────────────────────────────

class TestRunScenario:
    def test_run_basic_scenario(self):
        s = scenario_by_name("standard_base_cabinet")
        result = run_scenario(s)
        assert result.assertions_total > 0
        assert result.duration_ms > 0

    def test_passing_scenario(self):
        s = scenario_by_name("list_all_hardware")
        result = run_scenario(s)
        assert result.passed


# ─── Full run ─────────────────────────────────────────────────────────────────

class TestRunAll:
    def test_runs_all_scenarios(self):
        report = run_all()
        assert report.scenarios_total == len(SCENARIOS)
        assert report.duration_ms > 0

    def test_filter_by_tag(self):
        report = run_all(tags=["hardware"])
        assert 0 < report.scenarios_total < len(SCENARIOS)
        for r in report.results:
            assert "hardware" in r.scenario.tags

    def test_filter_by_difficulty(self):
        report = run_all(difficulty="basic")
        assert report.scenarios_total > 0
        for r in report.results:
            assert r.scenario.difficulty == "basic"

    def test_explicit_empty_scenarios_runs_none(self):
        # An explicit empty list must NOT fall back to the full catalogue.
        report = run_all(scenarios=[])
        assert report.scenarios_total == 0

    def test_none_scenarios_runs_full_catalogue(self):
        report = run_all(scenarios=None)
        assert report.scenarios_total == len(SCENARIOS)

    def test_pass_rate_is_float(self):
        report = run_all()
        assert 0.0 <= report.pass_rate <= 1.0

    def test_score_is_float(self):
        report = run_all()
        assert 0.0 <= report.score <= 1.0


# ─── EvalReport ───────────────────────────────────────────────────────────────

class TestEvalReport:
    def test_to_dict_has_summary(self):
        report = run_all(difficulty="basic")
        d = report.to_dict()
        assert "summary" in d
        assert "scenarios" in d
        assert "pass_rate" in d["summary"]
        assert "score" in d["summary"]

    def test_to_dict_is_json_serializable(self):
        report = run_all(tags=["hardware"])
        text = json.dumps(report.to_dict())
        assert len(text) > 0

    def test_empty_report(self):
        report = EvalReport()
        assert report.score == 1.0
        assert report.pass_rate == 1.0
        assert report.scenarios_total == 0


# ─── CLI exit codes ───────────────────────────────────────────────────────────

class TestCLI:
    """Exercise the `python -m evals` entry point in-process so filter
    mistakes cannot exit 0 with an empty, all-green report.

    Calls ``evals.__main__.main`` directly (via a patched argv) and asserts on
    the ``SystemExit`` code — deterministic and fast, no subprocess."""

    def _main_exit_code(self, monkeypatch, *cli_args):
        import evals.__main__ as cli
        monkeypatch.setattr("sys.argv", ["python -m evals", *cli_args])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        code = exc.value.code
        return 0 if code is None else code

    def test_unknown_tag_exits_2(self, monkeypatch, capsys):
        code = self._main_exit_code(monkeypatch, "--tag", "definitely_not_a_real_tag")
        assert code == 2
        assert "Unknown tag" in capsys.readouterr().err

    def test_valid_name_with_nonmatching_difficulty_exits_1(self, monkeypatch, capsys):
        # A known scenario name filtered to a difficulty it doesn't have yields
        # an empty pool → must exit 1, not 0.
        code = self._main_exit_code(
            monkeypatch, "--name", "standard_base_cabinet", "--difficulty", "advanced"
        )
        assert code == 1
        assert "No scenarios matched" in capsys.readouterr().err

    def test_known_tag_runs_normally(self, monkeypatch):
        # A valid tag that matches scenarios must reach the run path (exit 0 if
        # all pass, 1 if any fail) — never the tag-validation error code 2.
        code = self._main_exit_code(monkeypatch, "--tag", "basic_cabinet", "--json")
        assert code in (0, 1)
