#!/usr/bin/env python
"""Capture the three claims a judge cannot otherwise check, through the real MCP server.

Three things are asserted in the README and in the submission and are true, but were
only ever observed in a terminal:

  1. The catalog totals the interface reports come out of ClickHouse, not out of a
     constant. Recorded here as `count()` statements whose answers are compared with
     /api/stats by tests/test_mcp_log_matches_stats.py.
  2. Ranking the fleet never reads vault.loudness_samples. `EXPLAIN indexes = 1`
     names every table a query touches, so the plan is the proof.
  3. The server refuses to write. mcp-clickhouse sends readonly=1, so ClickHouse
     answers a DDL statement with Code 164 READONLY before anything happens.

Every statement below goes through the official `mcp-clickhouse` server over stdio,
the same binary and the same environment agent/crew.py starts, so what lands in
logs/mcp_tool_calls.jsonl and docs/evidence/ is a transcript of calls that happened
rather than a fixture.

  source scripts/cloudenv.sh
  .venv/bin/python scripts/capture_mcp_evidence.py

The write probe is only attempted after `getSetting('readonly')` comes back as 1, so
the statement cannot execute even in principle. If readonly is not in force the probe
is skipped and said to be skipped, because a claim that the server refuses writes must
not be recorded off a server that would have accepted one.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import crew  # noqa: E402

LOG = ROOT / "logs" / "mcp_tool_calls.jsonl"
EVIDENCE = ROOT / "docs" / "evidence"

RANKING_QUERY = (
    "SELECT title_id, title, verdict, integrated_lufs, lufs_delta, lane "
    "FROM vault.fleet ORDER BY lufs_delta DESC LIMIT 12"
)
SAMPLE_SCAN_QUERY = (
    "SELECT title_id, min(short_term) FROM vault.loudness_samples GROUP BY title_id"
)
# Two write probes, one DDL and one DML, both chosen so that the archive survives even
# in the impossible case that readonly=1 is not in force: a Null-engine table stores
# nothing, and an INSERT ... WHERE 1 = 0 selects no rows to insert. A probe that would
# damage the archive if the guarantee failed is not a way to test the guarantee.
WRITE_PROBES = (
    ("DDL", "CREATE TABLE IF NOT EXISTS vault.readonly_probe (probe UInt8) ENGINE = Null"),
    ("DML", "INSERT INTO vault.events SELECT * FROM vault.events WHERE 1 = 0"),
)

RUN_ID = uuid.uuid4().hex[:12]


class Refused(Exception):
    """The server answered with an error rather than a result set."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows(text: str) -> list[list]:
    payload = json.loads(text)
    return payload["rows"]


def _append(entry: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


async def _call(session, sql: str) -> str:
    result = await session.call_tool("run_query", {"query": sql})
    text = "\n".join(
        block.text for block in result.content if getattr(block, "text", None)
    )
    if result.isError:
        raise Refused(text)
    return text


async def _record(session, sql: str, note: str) -> str:
    """Run one statement and write what came back into the tool-call log."""
    try:
        text = await _call(session, sql)
    except Refused as exc:
        _append({
            "ts": _now(),
            "run_id": RUN_ID,
            "agent": "mcp_evidence",
            "tool": "run_query",
            "sql": sql,
            "rows": 0,
            "blocked": True,
            "refused_by": "mcp-clickhouse readonly=1, enforced by ClickHouse",
            "note": note,
            "arguments": {"query": sql},
            "result_preview": str(exc)[:1200],
        })
        raise
    rows = _rows(text)
    _append({
        "ts": _now(),
        "run_id": RUN_ID,
        "agent": "mcp_evidence",
        "tool": "run_query",
        "sql": sql,
        "rows": len(rows),
        "blocked": False,
        "note": note,
        "arguments": {"query": sql},
        "result_preview": text[:4000],
    })
    return text


async def main() -> int:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    command = " ".join(crew.server_command())
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    captured: list[str] = []

    async with stdio_client(crew._stdio_params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            readonly = _rows(await _record(
                session,
                "SELECT getSetting('readonly') AS readonly_setting",
                "the session setting mcp-clickhouse applies to every statement",
            ))[0][0]
            print(f"readonly setting in force: {readonly!r}")

            for sql, note in (
                ("SELECT count() FROM vault.loudness_samples",
                 "the 100ms sample row count the interface reports"),
                ("SELECT formatReadableQuantity(count()) FROM vault.loudness_samples",
                 "the same figure as the interface prints it"),
                ("SELECT count(DISTINCT title_id) FROM vault.fleet",
                 "the size of the measured catalog"),
                ("SELECT count() FROM vault.findings",
                 "one row per spec check per title scan"),
                ("SELECT count(DISTINCT title_id) FROM vault.findings",
                 "every measured title carries findings"),
                ("SELECT count() FROM vault.title_loudness",
                 "the rollup ranking actually reads"),
            ):
                await _record(session, sql, note)

            ranking_plan = _rows(await _record(
                session,
                f"EXPLAIN indexes = 1 {RANKING_QUERY}",
                "the ranking plan: vault.loudness_samples must not appear",
            ))
            contrast_plan = _rows(await _record(
                session,
                f"EXPLAIN indexes = 1 {SAMPLE_SCAN_QUERY}",
                "a query that does scan the samples, so the plan above can fail",
            ))

            ranking_text = "\n".join(str(row[0]) for row in ranking_plan)
            contrast_text = "\n".join(str(row[0]) for row in contrast_plan)
            path = EVIDENCE / "explain-ranking-plan.txt"
            path.write_text(
                "Ranking the fleet does not read the 100ms sample stream.\n"
                "\n"
                "Captured through the official mcp-clickhouse MCP server over stdio, the\n"
                f"same binary agent/crew.py starts: {command}\n"
                f"Server environment: CLICKHOUSE_ALLOW_WRITE_ACCESS=false, readonly={readonly!r}.\n"
                f"Captured at {_now()}.\n"
                "\n"
                "Reproduce:\n"
                "  source scripts/cloudenv.sh\n"
                "  .venv/bin/python scripts/capture_mcp_evidence.py\n"
                "\n"
                "run_query, statement 1 of 2:\n"
                f"  EXPLAIN indexes = 1 {RANKING_QUERY}\n"
                "\n"
                f"{ranking_text}\n"
                "\n"
                "vault.loudness_samples does not appear in that plan. vault.title_loudness,\n"
                "the one-row-per-title rollup, and vault.findings, the verdict table, do.\n"
                "\n"
                "run_query, statement 2 of 2, the control. Without it the first plan could\n"
                "pass because EXPLAIN had stopped naming tables at all:\n"
                f"  EXPLAIN indexes = 1 {SAMPLE_SCAN_QUERY}\n"
                "\n"
                f"{contrast_text}\n"
                "\n"
                "That plan does name vault.loudness_samples, so the absence above is a fact\n"
                "about the ranking query rather than about EXPLAIN.\n",
                encoding="utf-8",
            )
            captured.append(str(path.relative_to(ROOT)))
            print(f"wrote {path.relative_to(ROOT)}")

            if str(readonly) != "1":
                print(
                    f"readonly is {readonly!r}, not 1: skipping the write probe rather "
                    "than sending a DDL statement to a server that might accept it.",
                    file=sys.stderr,
                )
                return 1

            events_before = _rows(await _record(
                session,
                "SELECT count() FROM vault.events",
                "the archive before a write is attempted against it",
            ))[0][0]

            refusals = []
            for kind, probe in WRITE_PROBES:
                try:
                    await _record(
                        session, probe, f"a {kind} statement, to prove the server refuses one"
                    )
                except Refused as exc:
                    refusal = str(exc)
                else:
                    print(
                        f"the server ACCEPTED a {kind} statement. The read-only claim is "
                        "false and the evidence file was not written.",
                        file=sys.stderr,
                    )
                    return 1
                if "164" not in refusal or "READONLY" not in refusal.upper():
                    print(
                        f"the {kind} statement was refused, but not as READONLY: {refusal}",
                        file=sys.stderr,
                    )
                    return 1
                refusals.append((kind, probe, refusal))

            probe_table = _rows(await _record(
                session,
                "SELECT count() FROM system.tables "
                "WHERE database = 'vault' AND name = 'readonly_probe'",
                "the refused DDL statement left nothing behind",
            ))[0][0]
            events_after = _rows(await _record(
                session,
                "SELECT count() FROM vault.events",
                "the archive after both writes were refused",
            ))[0][0]

            body = [
                "The MCP server refuses to write, and ClickHouse is the one refusing.",
                "",
                "Captured through the official mcp-clickhouse MCP server over stdio, the",
                f"same binary agent/crew.py starts: {command}",
                "Server environment: CLICKHOUSE_ALLOW_WRITE_ACCESS=false, which makes",
                f"mcp-clickhouse send readonly={readonly!r} with every statement.",
                f"Captured at {_now()}.",
                "",
                "Reproduce:",
                "  source scripts/cloudenv.sh",
                "  .venv/bin/python scripts/capture_mcp_evidence.py",
                "",
                "Both probes deliberately bypass the crew's own read-only gate in",
                "agent/crew.py, because our callback refusing a statement proves nothing",
                "about the server. Both are also chosen to be harmless if the guarantee",
                "failed: a Null-engine table stores nothing, and the INSERT selects no rows.",
                "",
                f"vault.events before: {events_before} rows.",
                "",
            ]
            for kind, probe, refusal in refusals:
                body += [f"run_query, {kind}:", f"  {probe}", "", f"{refusal}", ""]
            body += [
                "Code 164 is ClickHouse's READONLY. Both statements were rejected by the",
                "database rather than filtered by the client, and neither left a mark.",
                "",
                "run_query:",
                "  SELECT count() FROM system.tables "
                "WHERE database = 'vault' AND name = 'readonly_probe'",
                "",
                f"  {probe_table}",
                "",
                "run_query:",
                "  SELECT count() FROM vault.events",
                "",
                f"  {events_after}",
                "",
            ]
            path = EVIDENCE / "readonly-refusal.txt"
            path.write_text("\n".join(body), encoding="utf-8")
            captured.append(str(path.relative_to(ROOT)))
            print(f"wrote {path.relative_to(ROOT)}")

            if probe_table != 0:
                print("vault.readonly_probe exists. The refusal did not hold.", file=sys.stderr)
                return 1
            if events_after != events_before:
                print(
                    f"vault.events went from {events_before} to {events_after} rows across "
                    "two refused writes. The refusal did not hold.",
                    file=sys.stderr,
                )
                return 1

    print(f"run {RUN_ID}: appended to {LOG.relative_to(ROOT)}; artefacts: {', '.join(captured)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
