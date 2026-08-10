"""Headless CLI — argument parsing, dispatch, and exit codes.

The CLI is a thin front end over ``server.TOOL_DISPATCH``; these tests pin the
parsing helpers, the exit-code contract (0 ok / 1 tool error / 2 unknown tool),
and — most importantly — that the CLI can reach exactly the tools the MCP
server advertises, so the two never drift apart.
"""

import asyncio
import json

import pytest

from cabineteer import cli, server


def test_parse_kv_json_and_string():
    out = cli._parse_kv(["a=1", "b=hello", 'c={"x": 2}', "d=true"])
    assert out == {"a": 1, "b": "hello", "c": {"x": 2}, "d": True}


def test_parse_kv_requires_equals():
    with pytest.raises(SystemExit):
        cli._parse_kv(["missing-equals"])


def test_list_tools_lists_all(capsys):
    rc = cli.main(["list-tools"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "design_cabinet" in captured.out
    assert "generate_project_cutlist" in captured.out
    # the "<N> tools" summary is a stderr note, not part of the machine output
    assert "tools" in captured.err


def test_run_readonly_tool_returns_json(capsys):
    rc = cli.main(["run", "list_joinery_options"])
    assert rc == 0
    json.loads(capsys.readouterr().out)   # payload is valid JSON


def test_unknown_tool_exits_2(capsys):
    rc = cli.main(["run", "no_such_tool"])
    assert rc == 2
    assert "Unknown tool" in capsys.readouterr().err


def test_tool_error_exits_1(capsys):
    # A missing project makes the handler raise, which call_tool wraps as
    # "ERROR: ..." — the CLI must surface that on stderr and exit non-zero.
    rc = cli.main(["evaluate", "definitely-not-a-real-project-zzz"])
    assert rc == 1
    assert "ERROR:" in capsys.readouterr().err


def test_friendly_wrapper_drops_unset_options(capsys):
    # `presets` with no flags must not pass category=None/tag=None through.
    rc = cli.main(["presets"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "presets" in payload


def test_dispatch_matches_advertised_tools():
    advertised = {t.name for t in asyncio.run(server.list_tools())}
    assert advertised == set(server.TOOL_DISPATCH), (
        "list_tools() and TOOL_DISPATCH have drifted — every advertised tool "
        "must be dispatchable and vice versa"
    )
