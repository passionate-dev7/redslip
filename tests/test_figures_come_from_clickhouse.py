"""Every figure on a work order is a cell ClickHouse returned, checked in Python.

The allocator holds no database tools, so each number it prints was either copied out
of the evidence its colleagues queried or produced by the model. Only the first kind
belongs on a slip a QC lead acts on.

This is deliberately not a critic agent. Asking a second model whether the first model
made a number up is theatre: it costs a round trip, it can be wrong, and it cannot be
tested. An equality check against the returned cells can be tested, and is, here.

Goes red:
  - if an invented figure is reported as verified
  - if a figure copied from the database is reported as unsupported
  - if the check passes on an empty trace, which would make it vacuous
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import crew  # noqa: E402

# What mcp-clickhouse actually hands back: JSON rows.
TRACE = [
    {
        "agent": "fleet_scout",
        "sql": "SELECT title_id, integrated_lufs, lufs_delta FROM vault.fleet",
        "result": (
            '[{"title_id": "fugitive_valley", "integrated_lufs": -37.2, "lufs_delta": 14.2}, '
            '{"title_id": "OutpostInMorocco", "integrated_lufs": -15.8, "lufs_delta": 7.2}]'
        ),
    },
    {
        "agent": "loudness_analyst",
        "sql": "SELECT title_id, stddevPop(short_term) FROM vault.loudness_samples GROUP BY title_id",
        "result": '[{"title_id": "fugitive_valley", "spread": 8.41}]',
    },
]


def _entry(title_id, integrated, delta):
    return {
        "rank": 1,
        "title_id": title_id,
        "title": title_id,
        "lane": "BATCH",
        "integrated_lufs": integrated,
        "lufs_delta": delta,
        "failures": 2,
        "rescue_cost": "loudnorm pass",
        "rationale": "measured out of spec",
    }


def test_a_quoted_figure_verifies():
    order = {"queue": [_entry("fugitive_valley", -37.2, 14.2)]}
    verified, unsupported = crew.verify_against_cells(order, TRACE)
    assert verified, f"a figure straight out of the query was rejected: {unsupported}"
    assert unsupported == []


def test_an_invented_figure_is_caught():
    """The exact failure this exists for: a plausible number nobody measured."""
    order = {"queue": [_entry("fugitive_valley", -19.0, 4.0)]}
    verified, unsupported = crew.verify_against_cells(order, TRACE)
    assert not verified, "an invented loudness figure was reported as verified"
    fields = {row["field"] for row in unsupported}
    assert fields == {"integrated_lufs", "lufs_delta"}
    assert unsupported[0]["title_id"] == "fugitive_valley"


def test_one_bad_figure_among_good_ones_is_still_caught():
    order = {
        "queue": [
            _entry("fugitive_valley", -37.2, 14.2),
            _entry("OutpostInMorocco", -15.8, 99.9),
        ]
    }
    verified, unsupported = crew.verify_against_cells(order, TRACE)
    assert not verified
    assert [row["field"] for row in unsupported] == ["lufs_delta"]
    assert unsupported[0]["claimed"] == 99.9


def test_rounding_the_database_is_still_quoting_the_database():
    """-37.24 reported as -37.2 is a quote. -19.0 is not."""
    trace = [{"agent": "fleet_scout", "sql": "SELECT 1",
              "result": '[{"integrated_lufs": -37.24}]'}]
    order = {"queue": [_entry("x", -37.2, None)]}
    verified, _ = crew.verify_against_cells(order, trace)
    assert verified


def test_the_check_cannot_pass_on_an_empty_trace():
    """With no queries recorded, no figure is supported, so nothing may verify.

    A check that returns clean when it has no evidence is worse than no check.
    """
    order = {"queue": [_entry("fugitive_valley", -37.2, 14.2)]}
    verified, unsupported = crew.verify_against_cells(order, [])
    assert not verified, "the check passed with nothing to compare against"
    assert len(unsupported) == 2


def test_a_missing_figure_is_allowed_but_a_wrong_one_is_not():
    """The allocator is told to leave a figure null rather than guess it."""
    order = {"queue": [_entry("fugitive_valley", None, None)]}
    verified, unsupported = crew.verify_against_cells(order, TRACE)
    assert verified and unsupported == []


def test_cells_are_read_out_of_the_result_payload_not_the_sql():
    """A number that only appears in the query text is not a measurement.

    `LIMIT 12` must not make 12 a citable loudness figure, or the check would be
    satisfied by the agent's own SQL.
    """
    trace = [{
        "agent": "fleet_scout",
        "sql": "SELECT integrated_lufs FROM vault.fleet ORDER BY lufs_delta DESC LIMIT 12",
        "result": "[]",
    }]
    cells = crew.result_cells(trace)
    assert "12" not in cells, "numbers from the SQL text are being treated as results"
