"""ClickHouse writes for Redslip.

This module holds the insert path only, over clickhouse-connect, because MCP's
run_query tool is not an insert path and a 1,200-row bulk load should not pretend
to be a query. Every read the AGENTS perform goes through the official
mcp-clickhouse MCP server instead: see agent/crew.py.

The host comes from CLICKHOUSE_HOST, so the same code talks to a local server or
to ClickHouse Cloud without a branch.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import clickhouse_connect

# Per-100ms ebur128 sample regex matching ffmpeg's per-frame log line.
_SAMPLE = re.compile(
    r"t:\s*([\d.]+)\s+TARGET:\s*-?\d+\s*LUFS\s+"
    r"M:\s*(-?[\d.inf]+)\s+S:\s*(-?[\d.inf]+)\s+"
    r"I:\s*(-?[\d.inf]+)\s*LUFS\s+LRA:\s*(-?[\d.inf]+)\s*LU"
    r"(?:\s+FTPK:\s*(-?[\d.inf]+))?"
)


def _f(raw: str | None) -> float:
    if raw is None:
        return -120.0
    if "inf" in str(raw):
        return -120.0
    try:
        return float(raw)
    except ValueError:
        return -120.0


def client() -> clickhouse_connect.driver.Client:
    """Return a ClickHouse client from env / defaults."""
    host     = os.getenv("CLICKHOUSE_HOST", "localhost")
    secure   = os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true"
    port     = int(os.getenv("CLICKHOUSE_PORT", "8443" if secure else "8123"))
    return clickhouse_connect.get_client(
        host=host, port=port,
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
        secure=secure,
    )


def apply_schema(ch=None) -> None:
    ch = ch or client()
    sql = (Path(__file__).parent.parent / "schema.sql").read_text()
    # Strip comments before splitting to avoid empty-statement errors
    lines = [l for l in sql.splitlines() if not l.strip().startswith("--")]
    clean_sql = "\n".join(lines)
    for stmt in [s.strip() for s in clean_sql.split(";") if s.strip()]:
        ch.command(stmt)


def loudness_timeseries(path: str | Path, seconds: int | None = None) -> list[tuple]:
    """Parse every 100ms ebur128 reading from ffmpeg stderr.

    This is the table that justifies ClickHouse: ~10 rows/second of content.
    A 2-minute excerpt = ~1,200 rows; a 30-title catalog scan = ~36,000+ rows.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path)]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-af", "ebur128=peak=true", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

    rows: list[tuple] = []
    for m in _SAMPLE.finditer(proc.stderr):
        rows.append((
            float(m.group(1)),  # t_seconds
            _f(m.group(2)),     # momentary (M)
            _f(m.group(3)),     # short_term (S)
            _f(m.group(4)),     # integrated (I)
            _f(m.group(6)),     # true_peak (FTPK)
        ))
    return rows


def store_findings(title_id: str, title: str, report, ch=None) -> None:
    """Insert all findings from a QCReport into vault.findings."""
    ch = ch or client()
    if not report.findings:
        return
    ch.insert(
        "vault.findings",
        [
            [
                title_id, title, f.check, f.spec,
                f.measured, f.target, f.unit,
                1 if f.passed else 0,
                1 if f.auto_fixable else 0,
                f.rescue_cost, f.detail,
            ]
            for f in report.findings
        ],
        column_names=[
            "title_id", "title", "check_name", "spec",
            "measured", "target", "unit",
            "passed", "auto_fixable", "rescue_cost", "detail",
        ],
    )


def store_samples(title_id: str, samples: list[tuple], ch=None) -> int:
    """Insert loudness time series. Returns row count inserted."""
    ch = ch or client()
    if not samples:
        return 0
    ch.insert(
        "vault.loudness_samples",
        [[title_id, t, mo, st, i, tp] for t, mo, st, i, tp in samples],
        column_names=["title_id", "t_seconds", "momentary", "short_term", "integrated", "true_peak"],
    )
    return len(samples)


def store_events(title_id: str, events: list[tuple[str, float, float, str]], ch=None) -> int:
    """Append this scan's per-occurrence defect rows. Nothing is ever deleted.

    A title is re-scanned after a repair, and both scans stay in the table. Readers go
    through `vault.latest_events`, which keeps only the newest scan per title, exactly
    as `vault.catalog_summary` does for findings.

    The obvious implementation was to delete the title's previous events first, and it
    silently destroyed data. A lightweight delete is a mutation, and on ClickHouse Cloud
    a mutation issued immediately before an insert matching the same predicate can still
    swallow the rows that insert wrote. The first backfill wrote 29 events and kept 13,
    and `lightweight_deletes_sync = 2` did not close it. Appending removes the race
    instead of racing more carefully.

    The read-back is not decoration. An accepted insert is not a stored row: ClickHouse
    Cloud buffers by default, so the count is taken with async insert off and sequential
    consistency on, and a mismatch raises rather than being reported as a success.
    """
    ch = ch or client()
    if not events:
        return 0
    ch.insert(
        "vault.events",
        [[title_id, kind, float(start), float(end), detail] for kind, start, end, detail in events],
        column_names=["title_id", "kind", "start_seconds", "end_seconds", "detail"],
        settings={"async_insert": 0},
    )
    stored = ch.query(
        "SELECT count() FROM vault.latest_events WHERE title_id = %(t)s "
        "SETTINGS select_sequential_consistency = 1",
        parameters={"t": title_id},
    ).result_rows[0][0]
    if stored != len(events):
        raise RuntimeError(
            f"{title_id}: wrote {len(events)} events but the latest scan reads {stored}. "
            "An accepted insert is not a stored row."
        )
    return len(events)


def store_source(title_id: str, source_url: str, scan_seconds: int, ch=None) -> None:
    """Record the exact public file that was measured, so a judge can re-run it."""
    ch = ch or client()
    ch.insert(
        "vault.sources",
        [[title_id, source_url, f"https://archive.org/details/{title_id}", int(scan_seconds)]],
        column_names=["title_id", "source_url", "details_url", "scan_seconds"],
    )


def sources(ch=None) -> dict[str, dict]:
    ch = ch or client()
    res = ch.query(
        "SELECT title_id, argMax(source_url, fetched_at), argMax(details_url, fetched_at), "
        "argMax(scan_seconds, fetched_at) FROM vault.sources GROUP BY title_id"
    )
    return {
        row[0]: {"source_url": row[1], "details_url": row[2], "scan_seconds": row[3]}
        for row in res.result_rows
    }


def row_count(table: str = "vault.loudness_samples", ch=None) -> int:
    ch = ch or client()
    res = ch.query(f"SELECT count() FROM {table}")
    return res.result_rows[0][0]


def catalog_summary(ch=None) -> list[dict]:
    """Pull the catalog view: asserts non-empty before returning."""
    ch = ch or client()
    res = ch.query(
        "SELECT title_id, title, last_scanned, failures, passes, "
        "auto_fixable, needs_human, verdict, failed_checks, rescue_costs "
        "FROM vault.catalog_summary"
    )
    rows = [dict(zip(res.column_names, row)) for row in res.result_rows]
    if not rows:
        raise RuntimeError("vault.catalog_summary is empty: run ingest first")
    return rows


def loudness_extremes(ch=None) -> list[dict]:
    ch = ch or client()
    res = ch.query(
        "SELECT title_id, integrated_lufs, worst_short_term_lufs, "
        "best_short_term_lufs, max_true_peak_dbfs, worst_window_at_seconds, sample_count "
        "FROM vault.loudness_extremes ORDER BY integrated_lufs ASC"
    )
    return [dict(zip(res.column_names, row)) for row in res.result_rows]


def worst_window(title_id: str, ch=None) -> dict | None:
    """Quietest sustained short-term window for one title, or None if unmeasured."""
    ch = ch or client()
    res = ch.query(
        "SELECT worst_window_at_seconds, worst_short_term_lufs, best_short_term_lufs "
        "FROM vault.loudness_extremes WHERE title_id = %(t)s",
        parameters={"t": title_id},
    )
    if not res.result_rows:
        return None
    at, quietest, loudest = res.result_rows[0]
    return {
        "quietest_at_seconds": float(at),
        "quietest_short_term_lufs": float(quietest),
        "loudest_short_term_lufs": float(loudest),
    }
