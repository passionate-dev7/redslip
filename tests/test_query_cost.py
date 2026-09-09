"""A cost the server did not report is never rendered as a cost.

The point of `qc/query_cost.py` is that Redslip stops counting its own work. Every
figure it shows comes out of ClickHouse: the summary header for a statement issued
here, `system.query_log` for a statement the crew composed and sent through MCP.

The failure this guards is the quiet one. If a lookup misses, the honest answer is
"not flushed yet" and the dishonest one is `0 rows read`, which renders exactly like a
query that ran and touched nothing. A judge reading a zero under a real statement would
conclude the rollup is free. So the tests below are mostly about absence.

Goes red:
  - if a statement with no accounting is given a numeric cost rather than a reason
  - if a blocked statement is priced at all, when it never reached the server
  - if the prefix used to match system.query_log stops surviving the FORMAT suffix
    clickhouse-connect appends, which is the only reason the match works
  - if `annotate` reports having priced more statements than it actually did
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qc import query_cost  # noqa: E402


class FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class FakeClient:
    """A client that answers for exactly one known statement and nothing else.

    Standing in for ClickHouse here is legitimate because what is under test is this
    module's handling of a hit and a miss, not ClickHouse's accounting. The accounting
    itself is exercised against the real server by `scripts/capture_query_cost.py` and
    by the live-catalog suite.
    """

    def __init__(self, known_prefix: str | None):
        self.known_prefix = known_prefix
        self.asked: list[str] = []

    def query(self, sql, parameters=None):
        wanted = (parameters or {}).get("sql", "")
        self.asked.append(wanted)
        if self.known_prefix is not None and wanted == self.known_prefix:
            # read_rows, read_bytes, result_rows, query_duration_ms, memory_usage, id
            return FakeResult([(24017, 120680, 8, 4, 5173496, "abc-123")])
        return FakeResult([])


RANKING = "SELECT title_id FROM vault.fleet ORDER BY lufs_delta DESC LIMIT 12"


def test_a_statement_the_server_priced_carries_the_servers_numbers():
    queries = [{"tool": "run_query", "sql": RANKING, "blocked": False}]
    priced = query_cost.annotate(queries, 0, ch=FakeClient(RANKING), wait_s=0)
    assert priced == 1
    cost = queries[0]["cost"]
    assert cost["read_rows"] == 24017
    assert cost["read_bytes"] == 120680
    assert cost["query_id"] == "abc-123"
    assert cost["source"] == "system.query_log"


def test_a_statement_with_no_accounting_says_so_instead_of_reading_as_free():
    queries = [{"tool": "run_query", "sql": RANKING, "blocked": False}]
    priced = query_cost.annotate(queries, 0, ch=FakeClient(None), wait_s=0)
    assert priced == 0
    cost = queries[0]["cost"]
    assert cost.get("unavailable"), "a missed lookup must state why, not imply zero cost"
    assert "read_rows" not in cost, "an unflushed statement must not render as 0 rows read"


def test_a_blocked_statement_is_not_priced_at_all():
    """It never reached the server, so it has no cost. Zero would be a lie about it."""
    queries = [{"tool": "run_query", "sql": "DROP TABLE vault.findings", "blocked": True}]
    assert query_cost.annotate(queries, 0, ch=FakeClient(None), wait_s=0) == 0
    assert "cost" not in queries[0]


def test_the_match_survives_the_format_suffix_clickhouse_connect_appends():
    """The logged text is the statement plus a FORMAT clause, so matching is a prefix.

    `startsWith(query, <statement>)` is the whole mechanism. If `normalise` ever stopped
    stripping the trailing semicolon, the prefix would end one character past the logged
    text and every lookup would silently miss.
    """
    assert query_cost.normalise(f"{RANKING};") == RANKING
    assert query_cost.normalise(f"  {RANKING}  ;; ") == RANKING
    client = FakeClient(RANKING)
    queries = [{"tool": "run_query", "sql": f"{RANKING};", "blocked": False}]
    assert query_cost.annotate(queries, 0, ch=client, wait_s=0) == 1
    assert client.asked == [RANKING], "the semicolon would have been sent to query_log"


def test_a_tool_that_is_not_run_query_is_not_priced():
    queries = [{"tool": "list_tables", "sql": '{"database": "vault"}', "blocked": False}]
    assert query_cost.annotate(queries, 0, ch=FakeClient(None), wait_s=0) == 0
    assert "cost" not in queries[0]
