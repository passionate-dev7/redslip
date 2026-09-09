#!/usr/bin/env python3
"""Bring an existing vault database up to the schema in schema.sql.

Two things `CREATE TABLE IF NOT EXISTS` cannot do to a table that already holds
rows: change its codecs and change its partition key. This script does both by
building the new table beside the old one, copying every row, asserting the
counts match, and swapping the names atomically with EXCHANGE TABLES.

It is idempotent. Running it twice is a no-op the second time.

    source scripts/cloudenv.sh
    uv run python migrate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from qc import store

SAMPLES_V2 = """
CREATE TABLE IF NOT EXISTS vault.loudness_samples_v2
(
    title_id   LowCardinality(String),
    scanned_at DateTime DEFAULT now() CODEC(DoubleDelta, ZSTD(3)),
    t_seconds  Float32 CODEC(Gorilla, ZSTD(3)),
    momentary  Float32 CODEC(Gorilla, ZSTD(3)),
    short_term Float32 CODEC(Gorilla, ZSTD(3)),
    integrated Float32 CODEC(Gorilla, ZSTD(3)),
    true_peak  Float32 CODEC(Gorilla, ZSTD(3))
)
ENGINE = MergeTree
ORDER BY (title_id, t_seconds)
PARTITION BY cityHash64(title_id) % 8
"""


def samples_need_migration(ch) -> bool:
    ddl = ch.query("SHOW CREATE TABLE vault.loudness_samples").result_rows[0][0]
    return "Gorilla" not in ddl


def migrate_samples(ch) -> None:
    """Rewrite loudness_samples with the time-series codecs and hash partitioning."""
    if not samples_need_migration(ch):
        print("loudness_samples: already on the codec schema, skipping")
        return

    before = ch.query("SELECT count() FROM vault.loudness_samples").result_rows[0][0]
    print(f"loudness_samples: {before:,} rows to migrate")

    ch.command("DROP TABLE IF EXISTS vault.loudness_samples_v2")
    ch.command(SAMPLES_V2)
    ch.command(
        "INSERT INTO vault.loudness_samples_v2 "
        "(title_id, scanned_at, t_seconds, momentary, short_term, integrated, true_peak) "
        "SELECT title_id, scanned_at, t_seconds, momentary, short_term, integrated, true_peak "
        "FROM vault.loudness_samples"
    )
    after = ch.query("SELECT count() FROM vault.loudness_samples_v2").result_rows[0][0]
    if after != before:
        raise RuntimeError(
            f"refusing to swap: copied {after:,} rows but the source holds {before:,}"
        )

    ch.command("EXCHANGE TABLES vault.loudness_samples AND vault.loudness_samples_v2")
    ch.command("DROP TABLE vault.loudness_samples_v2")
    print(f"loudness_samples: migrated {after:,} rows, old table dropped")


def backfill_rollup(ch) -> None:
    """Seed title_loudness from the rows that predate the materialized view.

    The view only fires on new inserts, so a database that already holds samples
    would otherwise show an empty fleet.
    """
    rollup = ch.query("SELECT count() FROM vault.title_loudness").result_rows[0][0]
    if rollup:
        print(f"title_loudness: already holds {rollup} rows, skipping backfill")
        return
    ch.command(
        "INSERT INTO vault.title_loudness "
        "SELECT title_id, toUInt64(count()), min(short_term), max(short_term), "
        "       max(true_peak), argMinState(t_seconds, short_term) "
        "FROM vault.loudness_samples WHERE short_term > -70 GROUP BY title_id"
    )
    n = ch.query("SELECT count() FROM vault.title_loudness").result_rows[0][0]
    print(f"title_loudness: backfilled {n} titles")


def main() -> None:
    ch = store.client()
    print("ClickHouse", ch.query("SELECT version()").result_rows[0][0])
    store.apply_schema(ch)
    print("schema applied")
    migrate_samples(ch)
    backfill_rollup(ch)

    fleet = ch.query(
        "SELECT count(), countIf(verdict = 'FAIL'), max(lufs_delta) FROM vault.fleet"
    ).result_rows[0]
    print(f"vault.fleet: {fleet[0]} titles, {fleet[1]} failing, worst delta {fleet[2]} LU")


if __name__ == "__main__":
    main()
