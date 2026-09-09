"""What a statement cost, taken from ClickHouse's own accounting rather than counted here.

Redslip's storage argument is a claim about rows READ, not rows returned: ranking the
catalog off the AggregatingMergeTree rollup touches one row per title, while the
percentile question over the 100ms stream touches every sample. Rows returned cannot
tell those apart, both come back small. Rows read can, and rows read is a number only
the server holds. Counting it in Python would be marking our own homework.

Elapsed time is deliberately not the headline. At this catalog size the rollup query is
sometimes SLOWER in wall clock than the full sample scan, because 24,017 rows is nothing
to scan and the rollup pays for a view stack over three tables. The claim is about how
each shape GROWS, and rows read is what grows. Elapsed is reported anyway, next to the
rows rather than hidden, because a cost panel that quietly omits the one column that
argues against us is an advert.

There are two places the server states the cost, and this module uses both because they
answer different questions.

`summary_for` asks a statement now and reads clickhouse-connect's `result.summary`,
which is its parse of the X-ClickHouse-Summary header the server sends back. Exact,
immediate, and about a statement this process just issued.

`annotate` prices the statements the CREW composed. Those go out through the
mcp-clickhouse subprocess, so this process never sees their response headers.
`system.query_log` has them: ClickHouse's own per-statement accounting, looked up by the
query text the agent actually wrote.

That table is flushed on an interval rather than on commit, so a lookup fired the instant
a run ends can legitimately find nothing yet. That is reported as an absent cost with the
reason, never as a zero: a statement whose accounting has not been flushed and a statement
that read no rows are different facts and must not render the same.
"""

from __future__ import annotations

import time
from typing import Any

from qc import store

# ClickHouse Cloud flushes system.query_log on an interval (7.5s by default), so a
# lookup fired the instant a run ends can outrun the flush. Poll a little past one
# interval, then stop and say the cost is unavailable. The web path passes a shorter
# wait than this: a request must not sit waiting on a log flush.
DEFAULT_WAIT_S = 10.0
POLL_S = 2.0

# Lifted verbatim from system.query_log. Every one is ClickHouse's own measurement.
COST_COLUMNS = (
    "read_rows",
    "read_bytes",
    "result_rows",
    "query_duration_ms",
    "memory_usage",
)

UNFLUSHED = "not in system.query_log yet: ClickHouse flushes that table on an interval"

# The two shapes the product asks, priced side by side so the storage claim is on the
# page whether or not anyone runs the crew. These are the SHAPES. The crew composes its
# own text and is priced separately, per statement, in `annotate`.
RANKING_SQL = (
    "SELECT title_id, title, verdict, lane, failures, integrated_lufs, lufs_delta\n"
    "FROM vault.fleet\n"
    "ORDER BY lufs_delta DESC\n"
    "LIMIT 12"
)

PERCENTILES_SQL = (
    "SELECT title_id,\n"
    "       quantile(0.1)(short_term) AS p10,\n"
    "       quantile(0.5)(short_term) AS p50,\n"
    "       quantile(0.9)(short_term) AS p90,\n"
    "       max(short_term) - min(short_term) AS spread_lu\n"
    "FROM vault.loudness_samples\n"
    "WHERE short_term > -70\n"
    "GROUP BY title_id\n"
    "ORDER BY spread_lu DESC\n"
    "LIMIT 8"
)


def normalise(sql: str) -> str:
    """The statement as ClickHouse will have recorded it.

    clickhouse-connect, which mcp-clickhouse uses, appends a FORMAT clause before
    sending, so the logged text starts with what the agent wrote and then carries a
    suffix. Matching is therefore a prefix match on the stripped statement, and a
    trailing semicolon has to go or the prefix stops matching at it.
    """
    text = (sql or "").strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    return text


def _lookup(ch, statements: list[str], since_epoch: float) -> dict[str, dict]:
    """One pass over system.query_log for these exact statements.

    Matched by prefix rather than by normalized_query_hash. The hash replaces every
    literal with a placeholder, so two runs that chose different LIMITs would collide
    and one agent's cost would be reported as another's. The prefix is the text the
    agent composed, which is the thing being priced.
    """
    found: dict[str, dict] = {}
    for sql in statements:
        if not sql:
            continue
        res = ch.query(
            "SELECT " + ", ".join(COST_COLUMNS) + ", query_id "
            "FROM system.query_log "
            "WHERE type = 'QueryFinish' "
            "  AND event_time >= toDateTime(%(since)s) "
            "  AND startsWith(query, %(sql)s) "
            "ORDER BY event_time_microseconds DESC "
            "LIMIT 1",
            parameters={"sql": sql, "since": int(since_epoch)},
        )
        if not res.result_rows:
            continue
        row = res.result_rows[0]
        record: dict[str, Any] = {name: int(row[i]) for i, name in enumerate(COST_COLUMNS)}
        record["query_id"] = row[len(COST_COLUMNS)]
        record["source"] = "system.query_log"
        found[sql] = record
    return found


def costs_for_statements(
    statements: list[str],
    since_epoch: float,
    ch=None,
    wait_s: float = DEFAULT_WAIT_S,
) -> dict[str, dict]:
    """Cost per statement from system.query_log. A missing key means not yet flushed.

    Returns only what the server actually reported, so a caller must treat an absent
    key as unknown, because that is what it is.
    """
    wanted = list(dict.fromkeys(normalise(s) for s in statements if normalise(s)))
    if not wanted:
        return {}
    ch = ch or store.client()
    deadline = time.time() + max(0.0, wait_s)
    found: dict[str, dict] = {}
    while True:
        outstanding = [s for s in wanted if s not in found]
        if not outstanding:
            break
        found.update(_lookup(ch, outstanding, since_epoch))
        if len(found) == len(wanted) or time.time() >= deadline:
            break
        time.sleep(POLL_S)
    return found


def annotate(
    queries: list[dict],
    since_epoch: float,
    ch=None,
    wait_s: float = DEFAULT_WAIT_S,
) -> int:
    """Attach a `cost` block to every statement the crew composed. Returns how many.

    Mutates in place: the caller is handing this same object to the browser, and a
    second copy is one more thing that can drift from the run it describes.

    A blocked statement is skipped entirely rather than priced, because it never reached
    the server and therefore has no cost. Pricing a refusal at zero rows read would
    render exactly like a query that ran and found nothing, which is the opposite of
    what happened.
    """
    priced = [q for q in queries if q.get("tool") == "run_query" and not q.get("blocked")]
    if not priced:
        return 0
    costs = costs_for_statements(
        [q.get("sql", "") for q in priced], since_epoch, ch=ch, wait_s=wait_s
    )
    attached = 0
    for query in priced:
        record = costs.get(normalise(query.get("sql", "")))
        if record:
            query["cost"] = record
            attached += 1
        else:
            query["cost"] = {"source": "system.query_log", "unavailable": UNFLUSHED}
    return attached


def summary_for(sql: str, ch=None) -> dict[str, Any]:
    """Ask a statement now and return the server's own response summary.

    `result.summary` is clickhouse-connect's parse of the X-ClickHouse-Summary header,
    so read_rows here is the server's count of rows it touched, not a length measured
    over the rows that came back.
    """
    ch = ch or store.client()
    result = ch.query(sql)
    summary = dict(result.summary or {})
    return {
        "sql": normalise(sql),
        "read_rows": int(summary.get("read_rows", 0)),
        "read_bytes": int(summary.get("read_bytes", 0)),
        "elapsed_ms": round(int(summary.get("elapsed_ns", 0)) / 1e6, 1),
        "result_rows": len(result.result_rows),
        "source": "X-ClickHouse-Summary",
    }


def shape_costs(ch=None) -> dict[str, Any]:
    """Price both shapes now, and state the ratio between them.

    The ratio is the whole argument in one number: the ranking's cost tracks how many
    TITLES exist, the percentile query's tracks how much AUDIO was measured. An archive
    that doubles in running time doubles one of these and moves the other not at all.
    """
    ch = ch or store.client()
    total = int(ch.query("SELECT count() FROM vault.loudness_samples").result_rows[0][0])
    titles = int(ch.query("SELECT count() FROM vault.title_loudness").result_rows[0][0])

    ranking = summary_for(RANKING_SQL, ch=ch)
    ranking["name"] = "ranking"
    ranking["reads"] = "vault.fleet, over the AggregatingMergeTree rollup"
    ranking["grows_with"] = "titles in the catalog"

    percentiles = summary_for(PERCENTILES_SQL, ch=ch)
    percentiles["name"] = "percentiles"
    percentiles["reads"] = "vault.loudness_samples, the 100ms stream"
    percentiles["grows_with"] = "audio measured"

    ratio = (
        round(percentiles["read_rows"] / ranking["read_rows"], 1)
        if ranking["read_rows"]
        else None
    )
    return {
        "sample_rows": total,
        "rollup_rows": titles,
        "shapes": [ranking, percentiles],
        "ratio": ratio,
        "source": "X-ClickHouse-Summary",
        "note": (
            "Both figures are ClickHouse's own, read from the summary header it returns "
            f"with each response rather than counted here. Ranking reads "
            f"{ranking['read_rows']:,} rows against a stream of {total:,}; the percentile "
            f"question reads {percentiles['read_rows']:,}. Both answers are correct. Only "
            "one of them gets more expensive as the archive grows in running time rather "
            "than in title count, which is the whole reason the rollup exists. At this "
            "size neither is slow, so read the rows and not the milliseconds."
        ),
    }
