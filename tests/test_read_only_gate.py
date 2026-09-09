"""No agent can change the archive, and the gate stops the statement before MCP sees it.

The `mcp-clickhouse` server is started with write access disabled, so this gate is the
second lock rather than the only one. It exists because a defence that depends on one
environment variable being right on one deployment is not a defence, and because a
refused statement should be visible in the evidence rail rather than surfacing as an
opaque server error.

Goes red:
  - if any mutating statement is allowed through
  - if a mutating statement smuggled past a comment is allowed through
  - if the gate lets the call reach the tool instead of short-circuiting it
  - if a refusal is not recorded, so a judge cannot see it happened
  - if an ordinary SELECT is refused, which would make the gate useless in the
    opposite direction
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import crew  # noqa: E402


class FakeTool:
    def __init__(self, name="run_query"):
        self.name = name


class FakeContext:
    """Enough of an ADK tool context for the callback: a name and a state mapping."""

    def __init__(self, agent_name="fleet_scout"):
        self.agent_name = agent_name
        self.state: dict = {}


WRITES = [
    "DROP TABLE vault.findings",
    "TRUNCATE TABLE vault.loudness_samples",
    "DELETE FROM vault.events WHERE 1",
    "ALTER TABLE vault.findings DELETE WHERE 1",
    "INSERT INTO vault.slips SELECT * FROM vault.slips",
    "CREATE TABLE vault.x (a UInt8) ENGINE = MergeTree ORDER BY a",
    "RENAME TABLE vault.findings TO vault.gone",
    "EXCHANGE TABLES vault.findings AND vault.events",
    "SYSTEM DROP MARK CACHE",
    "OPTIMIZE TABLE vault.findings FINAL",
    "KILL QUERY WHERE 1",
    "SELECT 1 -- keep going\n; DROP TABLE vault.findings",
    "SELECT 1 /* nothing to see */ ; TRUNCATE TABLE vault.events",
    "",
    "   ",
]

READS = [
    "SELECT 1",
    "SELECT * FROM vault.fleet ORDER BY lufs_delta DESC LIMIT 12",
    "SELECT title_id, stddevPop(short_term) FROM vault.loudness_samples GROUP BY title_id",
    "SELECT e.kind, s.short_term FROM vault.events AS e ASOF LEFT JOIN "
    "vault.loudness_samples AS s ON e.title_id = s.title_id AND s.t_seconds <= e.start_seconds",
    "SELECT count() FROM vault.findings WHERE passed = 0",
    # A column called `deleted` is not a DELETE, and a comment mentioning drop is not one.
    "SELECT deleted FROM vault.findings",
    "/* drop everything */ SELECT 1",
    "SELECT 'insert' AS word",
]


@pytest.mark.parametrize("sql", WRITES)
def test_a_mutating_statement_is_refused(sql):
    assert not crew.is_read_only(sql), f"allowed through: {sql!r}"


@pytest.mark.parametrize("sql", READS)
def test_an_ordinary_read_is_allowed(sql):
    assert crew.is_read_only(sql), f"wrongly refused: {sql!r}"


def test_a_refused_statement_never_reaches_the_tool():
    """The callback returns a dict, and returning a dict is what stops the call."""
    ctx = FakeContext()
    result = crew._guard_tool(FakeTool(), {"query": "DROP TABLE vault.findings"}, ctx)
    assert isinstance(result, dict), "the gate let the statement reach mcp-clickhouse"
    assert "error" in result
    assert "read-only" in result["error"].lower()


def test_a_refusal_is_recorded_so_it_is_visible():
    ctx = FakeContext(agent_name="loudness_analyst")
    crew._guard_tool(FakeTool(), {"query": "TRUNCATE TABLE vault.events"}, ctx)
    trace = ctx.state.get("sql_trace")
    assert trace, "a refusal left no trace, so nobody can see it happened"
    assert trace[-1]["blocked"] is True
    assert trace[-1]["agent"] == "loudness_analyst"
    assert "TRUNCATE" in trace[-1]["sql"]


def test_a_permitted_statement_is_passed_through_untouched():
    ctx = FakeContext()
    result = crew._guard_tool(
        FakeTool(), {"query": "SELECT * FROM vault.fleet LIMIT 5"}, ctx
    )
    assert result is None, "returning anything here would short-circuit a legitimate read"
    assert not ctx.state.get("sql_trace"), "nothing to record until the tool answers"


def test_tools_other_than_run_query_are_not_second_guessed():
    """list_tables cannot mutate anything, and gating it on SQL keywords would be noise."""
    ctx = FakeContext()
    assert crew._guard_tool(FakeTool("list_tables"), {"database": "vault"}, ctx) is None


def test_the_server_is_started_with_write_access_disabled():
    env = crew._server_env()
    assert env["CLICKHOUSE_ALLOW_WRITE_ACCESS"] == "false"


def test_a_recorded_query_keeps_the_sql_and_the_row_count():
    ctx = FakeContext(agent_name="fleet_scout")
    crew._record_tool(
        FakeTool(),
        {"query": "SELECT title_id FROM vault.fleet LIMIT 2"},
        ctx,
        '[{"title_id": "a"}, {"title_id": "b"}]',
    )
    entry = ctx.state["sql_trace"][-1]
    assert entry["agent"] == "fleet_scout"
    assert entry["sql"].startswith("SELECT title_id")
    assert entry["rows"] == 2, "the evidence rail would show the wrong row count"
    assert entry["blocked"] is False
