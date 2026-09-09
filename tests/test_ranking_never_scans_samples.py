"""Ranking the catalog does not read the 100ms sample stream.

This is the whole reason vault.title_loudness exists. The samples table is the fat
append-only series and grows with titles times running time; the rollup is one row per
title, maintained incrementally by a materialized view. If ranking scans the samples,
the column store is being used as a dump and the design claim in the README is false.

`EXPLAIN indexes = 1` names every table a query reads, so this is checkable rather
than asserted. The assertion is on the plan, not on a timing, because a fast query on
a small table proves nothing.

Goes red:
  - if vault.fleet is rewritten to aggregate loudness_samples directly
  - if the materialized view or the rollup is dropped, leaving the view to fall back
  - if the rollup stops covering every measured title, which would silently drop
    titles out of the ranking
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RANKING_QUERY = (
    "SELECT title_id, title, verdict, integrated_lufs, lufs_delta, lane "
    "FROM vault.fleet ORDER BY lufs_delta DESC LIMIT 12"
)


@pytest.fixture(scope="module")
def ch():
    pytest.importorskip("clickhouse_connect")
    from qc import store

    try:
        client = store.client()
        client.query("SELECT 1")
    except Exception as exc:
        pytest.skip(f"no ClickHouse: {exc}")
    return client


def _plan(ch, sql: str) -> str:
    rows = ch.query(f"EXPLAIN indexes = 1 {sql}").result_rows
    return "\n".join(row[0] for row in rows)


def test_the_ranking_plan_does_not_read_the_sample_table(ch):
    plan = _plan(ch, RANKING_QUERY)
    assert "vault.loudness_samples" not in plan, (
        "ranking now scans the 100ms stream, which is what the rollup exists to avoid:\n"
        + plan
    )
    assert "vault.title_loudness" in plan, (
        "ranking is not reading the rollup at all:\n" + plan
    )
    assert "vault.findings" in plan, (
        "ranking is not reading the verdict table:\n" + plan
    )


def test_the_check_can_tell_the_difference(ch):
    """A query that does scan the samples must show up in the plan.

    Without this, the assertion above would pass for a reason unrelated to the design:
    for instance if EXPLAIN stopped naming tables at all.
    """
    plan = _plan(
        ch,
        "SELECT title_id, min(short_term) FROM vault.loudness_samples GROUP BY title_id",
    )
    assert "vault.loudness_samples" in plan, (
        "EXPLAIN is not naming tables, so the assertion above cannot fail:\n" + plan
    )


def test_the_rollup_is_one_row_per_title_and_covers_every_measured_title(ch):
    rollup = ch.query("SELECT count(DISTINCT title_id) FROM vault.title_loudness").result_rows[0][0]
    with_samples = ch.query(
        "SELECT count(DISTINCT title_id) FROM vault.loudness_samples"
    ).result_rows[0][0]
    assert with_samples > 0, "no loudness has been measured, so this proves nothing"
    assert rollup == with_samples, (
        f"the rollup covers {rollup} titles but {with_samples} have samples; "
        "titles would silently drop out of the ranking"
    )

    ranked = ch.query("SELECT count() FROM vault.fleet").result_rows[0][0]
    assert ranked >= with_samples


def test_the_rollup_is_an_aggregating_engine_fed_by_a_materialized_view(ch):
    ddl = ch.query("SHOW CREATE TABLE vault.title_loudness").result_rows[0][0]
    assert "AggregatingMergeTree" in ddl, (
        "the rollup is no longer an aggregating engine, so merges stop collapsing it:\n" + ddl
    )
    assert "ORDER BY title_id" in ddl

    views = ch.query(
        "SELECT name FROM system.tables WHERE database = 'vault' AND engine = 'MaterializedView'"
    ).result_rows
    assert any(row[0] == "mv_title_loudness" for row in views), (
        "the materialized view is gone, so the rollup would stop updating on ingest"
    )


def test_the_sample_table_is_stored_as_a_time_series(ch):
    """Codecs and key order are the reason the fat table is affordable at all."""
    ddl = ch.query("SHOW CREATE TABLE vault.loudness_samples").result_rows[0][0]
    assert "Gorilla" in ddl, "the float codec is gone:\n" + ddl
    assert "ZSTD" in ddl
    assert "LowCardinality(String)" in ddl, "title_id is no longer low cardinality:\n" + ddl
    assert "ORDER BY (title_id, t_seconds)" in ddl, (
        "the sorting key no longer matches how the drill path reads:\n" + ddl
    )
    assert "cityHash64(title_id)" in ddl, (
        "partitioning by title is what makes re-scanning one master a partition drop:\n" + ddl
    )


def test_the_drill_path_uses_a_point_in_time_join(ch):
    """One title, defects stamped with the loudness at the instant they began.

    Runs the real join against the real tables and asserts it returns rows, because
    an ASOF JOIN that quietly matches nothing looks identical to one that works.
    """
    rows = ch.query(
        "SELECT e.title_id, e.kind, e.start_seconds, s.short_term "
        "FROM vault.events AS e "
        "ASOF LEFT JOIN vault.loudness_samples AS s "
        "  ON e.title_id = s.title_id AND s.t_seconds <= e.start_seconds "
        "WHERE e.start_seconds > 0 "
        "ORDER BY e.title_id, e.start_seconds LIMIT 50"
    ).result_rows
    assert rows, "the point-in-time join returned nothing, so no slip can carry an event"
    matched = [row for row in rows if row[3] is not None]
    assert matched, (
        "every event matched a null sample, so the join is aligning on nothing"
    )
