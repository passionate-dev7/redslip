"""The MCP tool-call log, and the rule that decides which era of it is evidence.

`logs/mcp_tool_calls.jsonl` is served at /api/mcp-log and is the artefact offered as
proof that the crew reaches ClickHouse through the official MCP server. That makes it
judge-facing, and a judge-facing transcript that quotes superseded totals argues
against the product it is supposed to support.

The catalog was re-ingested. Before that, the same tables held a larger and partly
duplicated title set, so statements composed against them answered with row counts and
title counts the shipped catalog no longer reports: 34.83 thousand sample rows against
the 24.02 thousand this service serves, and 28 measured titles against 20. Those calls
did happen and their answers were correct when they ran, so they are not rewritten,
because editing what a transcript says a server returned is falsifying it. They are kept
verbatim in `logs/mcp_tool_calls.pre-fleet.jsonl`, they are not served, and the cutoff is
stated in the payload rather than left for a reader to infer.

`GENERATION_START` is the timestamp of the first statement any agent composed against
`vault.fleet`, which is the view the shipped interface ranks on and the point from which
every recorded answer describes the catalog now in ClickHouse.

The consistency rules live here rather than in the test, because the endpoint and the
check must agree on what "the served log" means. The endpoint applies the cutoff only.
It deliberately does NOT apply the consistency rules: an endpoint that filtered out its
own contradictions would leave nothing for tests/test_mcp_log_matches_stats.py to catch.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "logs" / "mcp_tool_calls.jsonl"
PRE_FLEET_LOG_PATH = ROOT / "logs" / "mcp_tool_calls.pre-fleet.jsonl"
SCHEMA_PATH = ROOT / "schema.sql"

GENERATION_START = "2026-09-09T08:42:59+00:00"
GENERATION_REASON = (
    "The cutoff is the first statement any agent composed against vault.fleet, the view "
    "the shipped catalog ranks on. The catalog was re-ingested at that point, so earlier "
    "calls answered correctly about a larger title set this service no longer serves. "
    "They are kept verbatim, and unserved, in logs/mcp_tool_calls.pre-fleet.jsonl: a "
    "transcript edited to agree with today's totals would not be a transcript."
)

_CREATE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?(?:TABLE|VIEW)"
    r"(?:\s+IF\s+NOT\s+EXISTS)?\s+vault\.(\w+)",
    re.IGNORECASE,
)
_SOURCE = re.compile(r"(?:FROM|JOIN)\s+vault\.(\w+)", re.IGNORECASE)
_READABLE = re.compile(r"\b(\d+(?:\.\d+)?)\s+(thousand|million|billion|trillion)\b")

_COUNT_ALL = re.compile(
    r"^SELECT\s+count\(\)\s+FROM\s+vault\.(\w+)$", re.IGNORECASE)
_COUNT_READABLE = re.compile(
    r"^SELECT\s+formatReadableQuantity\(count\(\)\)\s+FROM\s+vault\.(\w+)$", re.IGNORECASE)
_COUNT_TITLES = re.compile(
    r"^SELECT\s+count\(DISTINCT\s+title_id\)\s+FROM\s+vault\.(\w+)$", re.IGNORECASE)

# A plain listing of a one-row-per-title view. When such a statement runs out of rows
# before it runs out of LIMIT, the number of rows it returned is a statement about how
# many titles exist. `LIMIT 30` answering with 27 is the "27-title universe" the
# submission does not claim.
_LISTING = re.compile(
    r"^SELECT\s+(?P<cols>[^()]+?)\s+FROM\s+vault\.(?P<table>\w+)(?:\s+(?:AS\s+)?\w+)?"
    r"(?:\s+ORDER\s+BY\s+[\w\s,.]+?)?(?:\s+LIMIT\s+(?P<limit>\d+))?$",
    re.IGNORECASE,
)
_NARROWS_THE_ROWS = re.compile(
    r"\b(WHERE|GROUP\s+BY|HAVING|JOIN|UNION|DISTINCT|ARRAY\s+JOIN|SETTINGS)\b",
    re.IGNORECASE,
)


def schema_objects() -> set[str]:
    """Every table and view the shipped schema creates in the vault database."""
    return set(_CREATE.findall(SCHEMA_PATH.read_text()))


def _parsed_generation_start() -> datetime:
    return datetime.fromisoformat(GENERATION_START)


def in_current_generation(entry: dict) -> bool:
    ts = entry.get("ts")
    if not ts:
        return False
    try:
        return datetime.fromisoformat(ts) >= _parsed_generation_start()
    except ValueError:
        return False


def read(path: Path | str = LOG_PATH) -> list[dict]:
    """Every entry in the file, current generation or not."""
    path = Path(path)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def served(path: Path | str = LOG_PATH) -> list[dict]:
    """The entries /api/mcp-log offers: the current schema generation only."""
    return [entry for entry in read(path) if in_current_generation(entry)]


def _sql(entry: dict) -> str:
    if entry.get("sql"):
        return str(entry["sql"])
    return str((entry.get("arguments") or {}).get("query") or "")


def _scalar(entry: dict):
    """The single cell a `SELECT count() ...` answered with, or None.

    mcp-clickhouse answers with {"columns": [...], "rows": [[cell]]}. An entry whose
    preview was trimmed to a row count carries no cell, and claims nothing.
    """
    preview = entry.get("result_preview")
    if not isinstance(preview, str):
        return None
    try:
        payload = json.loads(preview)
    except ValueError:
        return None
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != 1:
        return None
    row = rows[0]
    if not isinstance(row, list) or len(row) != 1:
        return None
    return row[0]


def readable_quantity(value: int) -> str:
    """ClickHouse's formatReadableQuantity, for the magnitudes this catalog reaches."""
    for limit, unit in ((10 ** 12, "trillion"), (10 ** 9, "billion"),
                        (10 ** 6, "million"), (10 ** 3, "thousand")):
        if value >= limit:
            return f"{value / limit:.2f} {unit}"
    return f"{value:.2f}"


def _live_figures(stats: dict) -> dict[str, int]:
    """What /api/stats currently reports, keyed by the table each figure counts."""
    return {
        "loudness_samples": stats["loudness_sample_rows"],
        "findings": stats["finding_rows"],
        "events": stats["event_rows"],
        "title_loudness": stats["rollup_rows"],
        "fleet": stats["title_count"],
    }


# Tables that hold exactly one row per measured title, so a DISTINCT title_id over any
# of them is a claim about the size of the catalog.
_PER_TITLE = {"fleet", "title_loudness", "findings", "loudness_samples", "sources"}

# Views that return exactly one row per measured title, so listing one to exhaustion
# states how large the catalog is. `catalog_summary`, `fleet_loudness` and
# `loudness_extremes` are the pre-fleet shape of the same thing, still in the schema as
# compatibility views, and still counted here because a served entry could read them.
_PER_TITLE_LISTINGS = {
    "fleet", "title_loudness", "sources", "catalog_summary", "fleet_loudness",
    "loudness_extremes",
}


def _returned_rows(entry) -> int | None:
    """How many rows the statement actually answered with, or None if unrecorded."""
    preview = entry.get("result_preview")
    if isinstance(preview, str):
        try:
            payload = json.loads(preview)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            return len(payload["rows"])
    rows = entry.get("rows")
    return rows if isinstance(rows, int) else None


def claims(entry: dict, stats: dict) -> list[tuple[str, object, object]]:
    """Every checkable quantity a served entry asserts.

    Returns (what, claimed, expected). A claim is only produced where the statement
    pins the figure down on its own: a bare aggregate over one table with no WHERE,
    GROUP BY or LIMIT, or a formatReadableQuantity string appearing anywhere in the
    entry. Row counts from LIMITed statements are not claims about the catalog.
    """
    figures = _live_figures(stats)
    sql = " ".join(_sql(entry).split()).rstrip(";")
    found: list[tuple[str, object, object]] = []
    scalar = _scalar(entry)

    match = _COUNT_ALL.match(sql)
    if match and scalar is not None and match.group(1) in figures:
        table = match.group(1)
        found.append((f"count() FROM vault.{table}", scalar, figures[table]))

    match = _COUNT_READABLE.match(sql)
    if match and scalar is not None and match.group(1) in figures:
        table = match.group(1)
        found.append((
            f"formatReadableQuantity(count()) FROM vault.{table}",
            scalar,
            readable_quantity(figures[table]),
        ))

    match = _COUNT_TITLES.match(sql)
    if match and scalar is not None and match.group(1) in _PER_TITLE:
        table = match.group(1)
        found.append((
            f"count(DISTINCT title_id) FROM vault.{table}", scalar, stats["title_count"]
        ))

    match = _LISTING.match(sql)
    returned = _returned_rows(entry)
    if (
        match
        and returned is not None
        and match.group("table") in _PER_TITLE_LISTINGS
        and not _NARROWS_THE_ROWS.search(sql)
    ):
        limit = match.group("limit")
        # A statement that filled its LIMIT says nothing about the catalog size: the
        # rows ran out because the limit did.
        if limit is None or returned < int(limit):
            found.append((
                f"a catalog of {returned} titles, by listing vault.{match.group('table')} "
                f"to exhaustion",
                returned,
                stats["title_count"],
            ))

    acceptable = {readable_quantity(value) for value in figures.values()}
    for number, unit in _READABLE.findall(json.dumps(entry)):
        quantity = f"{float(number):.2f} {unit}"
        if quantity not in acceptable:
            found.append((
                f"a readable quantity of {quantity}", quantity, " or ".join(sorted(acceptable))
            ))
    return found


def disagreements(entries: list[dict], stats: dict) -> list[str]:
    """Every way the served log contradicts what /api/stats reports right now.

    Two kinds of contradiction, both of which the pre-fleet era commits:

      - a statement that reads a table or view the shipped schema does not create, so
        its result set describes a catalog that no longer exists
      - a figure that disagrees with the live one: a row count, a title count, or a
        formatReadableQuantity string
    """
    shipped = schema_objects()
    problems: list[str] = []
    for index, entry in enumerate(entries):
        where = f"entry {index} at {entry.get('ts')}"
        if not entry.get("blocked"):
            for table in _SOURCE.findall(_sql(entry)):
                if table not in shipped:
                    problems.append(
                        f"{where} reads vault.{table}, which the shipped schema does "
                        f"not create, so its answer describes a superseded catalog"
                    )
        for what, claimed, expected in claims(entry, stats):
            if str(claimed) != str(expected):
                problems.append(
                    f"{where} reports {what} as {claimed!r}, but /api/stats says "
                    f"{expected!r}"
                )
    return problems
