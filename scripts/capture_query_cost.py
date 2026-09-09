#!/usr/bin/env python
"""Record what each of the two query shapes costs, in rows and bytes read.

Redslip's whole storage argument is that ranking the catalog and reading the 100ms
stream are different questions with different costs, and that the ranking one stays
cheap as the archive grows. That is a claim about rows read, not rows returned, and
rows read is a number only the server knows.

ClickHouse reports it per statement in the response summary, so this asks the same
two queries the product asks and writes down what came back:

  ranking      the shape fleet_scout composes, over vault.fleet
  percentiles  the shape loudness_analyst composes, over vault.loudness_samples

Both figures are read from the server's own summary, not counted here.

  source scripts/cloudenv.sh
  .venv/bin/python scripts/capture_query_cost.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qc import store  # noqa: E402

EVIDENCE = ROOT / "docs" / "evidence"

RANKING = (
    "SELECT title_id, title, verdict, lane, failures, integrated_lufs, lufs_delta\n"
    "FROM vault.fleet\n"
    "ORDER BY lufs_delta DESC\n"
    "LIMIT 12"
)

PERCENTILES = (
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


def _int(summary: dict, key: str) -> int:
    return int(summary.get(key, 0))


def main() -> int:
    client = store.client()
    total = client.query("SELECT count() FROM vault.loudness_samples").result_rows[0][0]

    blocks: list[str] = [
        "What each query shape costs, read from ClickHouse's own response summary.",
        "",
        f"Captured at {datetime.now(timezone.utc).isoformat()} against "
        f"{os.getenv('CLICKHOUSE_HOST', 'localhost')}.",
        f"vault.loudness_samples holds {total} rows at capture time.",
        "",
        "Reproduce:",
        "  source scripts/cloudenv.sh",
        "  .venv/bin/python scripts/capture_query_cost.py",
        "",
    ]

    costs: dict[str, dict] = {}
    for name, sql in (("ranking", RANKING), ("percentiles", PERCENTILES)):
        result = client.query(sql)
        summary = dict(result.summary or {})
        costs[name] = summary
        blocks += [
            f"--- {name} ---",
            "",
            sql,
            "",
            f"  rows read      {_int(summary, 'read_rows')}",
            f"  bytes read     {_int(summary, 'read_bytes')}",
            f"  rows returned  {len(result.result_rows)}",
            f"  elapsed        {_int(summary, 'elapsed_ns') / 1e6:.1f} ms",
            "",
        ]

    ranking_read = _int(costs["ranking"], "read_rows")
    percentiles_read = _int(costs["percentiles"], "read_rows")
    blocks += [
        "The ranking reads a rollup, so its cost is a function of how many titles exist,",
        f"not how much audio was measured: {ranking_read} rows against a table of {total}.",
        f"The percentile query is the one that pays for the stream: {percentiles_read} rows.",
        "Both answers are correct. Only one of them gets more expensive as the archive",
        "grows in length rather than in title count.",
        "",
    ]

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / "query-cost.txt"
    path.write_text("\n".join(blocks), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")

    if percentiles_read <= ranking_read:
        print(
            f"the percentile query read {percentiles_read} rows and the ranking read "
            f"{ranking_read}. The two shapes are supposed to differ by orders of "
            "magnitude, so this capture is not evidence of anything.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
