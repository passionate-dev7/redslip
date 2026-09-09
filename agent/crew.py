"""The Redslip crew: a Google ADK multi-agent system over the official mcp-clickhouse server.

Why four agents and not one.

A single agent with one prompt and one canned SQL string is a function call with
extra steps, and this file replaces exactly that. The crew below exists because
triage genuinely decomposes into four jobs that need different evidence, and each
one changes an outcome that the others cannot reach:

  fleet_scout        Ranks the catalog. Reads `vault.fleet`, the AggregatingMergeTree
                     rollup, so the ranking never scans the 100ms sample table. It
                     chooses the WHERE and the LIMIT: how deep to cut the queue is a
                     judgement about how much mix-stage time exists this week.
  loudness_analyst   Reads the 100ms stream for the ranked titles only, and decides
                     the thing the numbers do not state: whether a failure is a flat
                     offset one loudnorm pass fixes, or a wide dynamic range where a
                     single gain change would bury the dialogue.
  structural_analyst Reads `vault.events` and ASOF-joins each defect to the loudness
                     at the instant it began. A black segment over silence is a reel
                     change. A black segment over programme audio is dropout. Same
                     row count, opposite dispositions, and only the join separates
                     them.
  work_allocator     Holds no database tools at all. It receives the two evidence
                     blocks and produces the ordered queue with a bay per title.

The two analysts run inside a ParallelAgent because they read different tables and
neither needs the other's answer. They are the only pair in the graph where that is
true; everything else is sequential because the next step is illegal without the
previous artifact.

There is deliberately no critic agent. Checking whether the allocator invented a
number is an equality test between its output and the cells MCP returned, so it is
written as one, in `verify_against_cells`, in Python. Paying a second model to audit
the first is theatre.

The MCP server is the official `mcp-clickhouse`, held through ADK's `McpToolset` over
stdio. `before_tool_callback` refuses any statement that is not read-only before it
reaches the server; `after_tool_callback` records every statement the crew composed,
which is what the interface shows as the run's evidence rail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

log = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
APP_NAME = "redslip"

# How many titles the crew may put on one work order. A queue longer than a week of
# mix-stage time is not a queue.
QUEUE_CAP = 12


class GeminiRequired(RuntimeError):
    """Raised when the crew has no model credentials.

    Redslip does not fall back to a canned plan. ffmpeg and ClickHouse produce every
    number; deciding which of them buy a title a place in the queue, and in what
    order, is the judgement the crew exists for. A deterministic substitute emitting
    the same shape would make the model removable, which is the failure this project
    was built to avoid.
    """


class GrafanaRequired(RuntimeError):
    """Raised when a publish is attempted with no way to reach Grafana.

    Kept, and deliberately not raised by `run_crew`. Redslip is a ClickHouse-track
    entry. The board is a bonus surface, and a bonus must never be able to break the
    product for a judge who has no Grafana token: the deployed service does not carry
    one, and a hard dependency on a credential that is not there turns the only
    interactive control in the product into an error message.

    So the coupling is structural rather than conditional. With no token the
    grafana_board agent is left out of the graph entirely and the run reports the step
    as skipped, with the reason. With a token it is in the graph and is not optional.
    That is the opposite of a toggle: the integration is load-bearing exactly where it
    is claimed to be, and absent where it is not claimed at all.
    """


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------

def credentials_present() -> bool:
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true" and project:
        return True
    return bool(os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY"))


# --------------------------------------------------------------------------
# The official mcp-clickhouse server, as an ADK toolset
# --------------------------------------------------------------------------

def _server_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("CLICKHOUSE_HOST", os.getenv("CLICKHOUSE_HOST", "localhost"))
    env.setdefault("CLICKHOUSE_PORT", os.getenv("CLICKHOUSE_PORT", "8123"))
    env.setdefault("CLICKHOUSE_USER", os.getenv("CLICKHOUSE_USER", "default"))
    env.setdefault("CLICKHOUSE_PASSWORD", os.getenv("CLICKHOUSE_PASSWORD", ""))
    env.setdefault("CLICKHOUSE_SECURE", os.getenv("CLICKHOUSE_SECURE", "false"))
    # The server defaults to read-only. Say so explicitly rather than depending on a
    # default that could change underneath us.
    env.setdefault("CLICKHOUSE_ALLOW_WRITE_ACCESS", "false")
    return env


def server_command() -> list[str]:
    """Locate the official mcp-clickhouse executable.

    It is installed as an isolated tool rather than as a library of this project,
    because mcp-clickhouse pulls fastmcp, which needs the MCP SDK 2.x, while ADK's
    McpToolset is built against 1.x. An MCP server is a separate process by design,
    so giving it a separate environment costs nothing and removes the conflict.
    """
    candidates = [
        Path(sys.executable).parent / "mcp-clickhouse",
        Path.home() / ".local" / "bin" / "mcp-clickhouse",
        Path("/usr/local/bin/mcp-clickhouse"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return [str(candidate)]
    found = shutil.which("mcp-clickhouse")
    if found:
        return [found]
    return ["uvx", "--from", "mcp-clickhouse", "mcp-clickhouse"]


def _stdio_params():
    from mcp import StdioServerParameters

    cmd = server_command()
    return StdioServerParameters(command=cmd[0], args=cmd[1:], env=_server_env())


def grafana_credentials_present() -> bool:
    return bool(
        os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN") or os.getenv("GRAFANA_API_KEY")
    )


def grafana_url() -> str:
    return os.getenv("GRAFANA_URL", "https://redslip.grafana.net").rstrip("/")


def _grafana_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GRAFANA_URL"] = grafana_url()
    token = os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN") or os.getenv("GRAFANA_API_KEY") or ""
    if token:
        env["GRAFANA_SERVICE_ACCOUNT_TOKEN"] = token
    return env


def grafana_server_command() -> list[str]:
    """Locate the official mcp-grafana executable.

    Isolated the same way as mcp-clickhouse: a separate process, so its MCP SDK
    pin cannot collide with ADK's.
    """
    candidates = [
        Path(sys.executable).parent / "mcp-grafana",
        Path.home() / ".local" / "bin" / "mcp-grafana",
        Path("/usr/local/bin/mcp-grafana"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return [str(candidate), "-t", "stdio"]
    found = shutil.which("mcp-grafana")
    if found:
        return [found, "-t", "stdio"]
    return ["uvx", "--from", "mcp-grafana", "mcp-grafana", "-t", "stdio"]


def grafana_toolset():
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
    from mcp import StdioServerParameters

    cmd = grafana_server_command()
    params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=_grafana_env())
    return McpToolset(
        connection_params=StdioConnectionParams(server_params=params, timeout=90),
        tool_filter=[
            "search_dashboards",
            "get_dashboard_summary",
            "update_dashboard",
            "create_annotation",
            "list_datasources",
        ],
    )


def clickhouse_toolset():
    """A fresh McpToolset over the official server.

    Built per run rather than cached in a module global: the toolset owns a
    subprocess and an async context, and sharing one across event loops is how this
    deadlocks under a threaded web server. `_RUN_LOCK` keeps concurrent requests from
    interleaving, so only one run's subprocesses are ever alive.
    """
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

    return McpToolset(
        connection_params=StdioConnectionParams(server_params=_stdio_params(), timeout=90),
        tool_filter=["run_query", "list_tables", "list_databases"],
    )


# --------------------------------------------------------------------------
# Callbacks: the read-only gate, and the evidence rail
# --------------------------------------------------------------------------

# Anything that could change the archive. `system` is in the list because
# SYSTEM DROP / RELOAD is a write in everything but name.
_WRITE_STATEMENT = re.compile(
    r"(?<![a-z_])(insert|alter|drop|truncate|delete|create|rename|attach|detach|"
    r"optimize|grant|revoke|system|exchange|kill)(?![a-z_])",
    re.IGNORECASE,
)

# A statement can start with SELECT and still write. `SELECT ... INTO OUTFILE` is the
# one that matters: it carries no write verb, so the list above waves it through, and
# it puts the archive on the server's filesystem. `INTO DUMPFILE` is the same clause
# for a single blob. Neither is a query the crew has any reason to compose, and the
# claim this gate makes is that nothing but a read gets past it, so a SELECT with a
# write sink has to be refused by name rather than by being unusual.
_WRITE_SINK = re.compile(r"(?<![a-z_])into\s+(outfile|dumpfile)(?![a-z_])", re.IGNORECASE)


def is_read_only(sql: str) -> bool:
    """True when this statement cannot change the archive or write a file.

    Comments are stripped first. `-- drop table` is harmless, but so is
    `SELECT 1 --\\n; DROP TABLE x` to a naive prefix check, and only one of those
    should be allowed through.
    """
    stripped = re.sub(r"--[^\n]*", " ", sql or "")
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.DOTALL)
    # String literals go too. Titles legitimately contain words like "insert", and a
    # WHERE title_id IN (...) list must not be read as a statement. The OUTFILE path
    # is a literal and disappears here, which is why the sink is matched on the
    # clause rather than on what it is pointed at.
    stripped = re.sub(r"'(?:''|\\.|[^'])*'", " ", stripped)
    if not stripped.strip():
        return False
    if _WRITE_SINK.search(stripped):
        return False
    return _WRITE_STATEMENT.search(stripped) is None


def _trace(ctx) -> list[dict]:
    trace = ctx.state.get("sql_trace")
    if trace is None:
        trace = []
    else:
        trace = list(trace)
    return trace


def _push(ctx, entry: dict) -> None:
    trace = _trace(ctx)
    trace.append(entry)
    # Reassign rather than mutating in place: ADK only records a state delta when
    # the key is set, so an appended-to list would never reach the session.
    ctx.state["sql_trace"] = trace


def _guard_tool(tool, args, tool_context):
    """before_tool_callback: refuse a non-read-only statement before MCP sees it.

    Returning a dict short-circuits the tool call, so the statement never reaches
    the server. The model is told plainly what happened, which is what stops it
    retrying the same thing.
    """
    if tool.name != "run_query":
        return None
    sql = args.get("query", "")
    if is_read_only(sql):
        return None
    log.warning("blocked non-read-only statement from %s: %s", tool_context.agent_name, sql[:200])
    _push(tool_context, {
        "agent": tool_context.agent_name,
        "tool": tool.name,
        "sql": sql,
        "blocked": True,
        "rows": 0,
        "result": "",
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "error": (
            "Refused. Redslip's ClickHouse tools are read-only: this crew measures and "
            "ranks, it never modifies the archive. Compose a SELECT instead."
        )
    }


def _record_tool(tool, args, tool_context, tool_response):
    """after_tool_callback: keep the statement the agent composed and what came back.

    This is the run's evidence rail. Two things depend on it: the interface shows the
    SQL each agent actually chose, and `verify_against_cells` uses the returned cells
    to check that the allocator quoted the database rather than itself.
    """
    text = _response_text(tool_response)
    _push(tool_context, {
        "agent": tool_context.agent_name,
        "tool": tool.name,
        "sql": args.get("query", "") if tool.name == "run_query" else json.dumps(args),
        "rows": _row_count(text),
        "chars": len(text),
        "result": text[:6000],
        "blocked": False,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return None


def _row_count(text: str) -> int:
    """How many rows came back, for the evidence rail.

    mcp-clickhouse answers with a JSON payload, so the row count is a length, not a
    line count. Falls back to counting lines when the payload is not JSON, which is
    what a tool other than run_query returns.
    """
    if not text:
        return 0
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return max(text.count("\n") - 1, 0)
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("rows", "data", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        return 1
    return 1


def _response_text(tool_response: Any) -> str:
    if tool_response is None:
        return ""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        for key in ("result", "text", "content", "output"):
            if key in tool_response:
                return _response_text(tool_response[key])
        return json.dumps(tool_response, default=str)
    if isinstance(tool_response, (list, tuple)):
        return "\n".join(_response_text(x) for x in tool_response)
    text = getattr(tool_response, "text", None)
    if isinstance(text, str):
        return text
    return str(tool_response)


# --------------------------------------------------------------------------
# The schema the crew works against, stated once
# --------------------------------------------------------------------------

SCHEMA_BRIEF = """Database `vault`, one archive of film masters, measured by ffmpeg.

  vault.fleet            One row per title. THE ranking table. Columns: title_id, title,
                         verdict, failures, auto_fixable, needs_human, integrated_lufs,
                         lufs_delta (absolute LU from the -23 LUFS target), quietest_lufs,
                         loudest_lufs, listen_at_seconds, loudness_samples, lane.
                         Backed by vault.title_loudness, an AggregatingMergeTree kept
                         current by a materialized view. Reading it costs one row per
                         title and touches none of the 100ms stream.

  vault.loudness_samples One row per 100ms of scanned audio: title_id, t_seconds,
                         momentary, short_term, integrated, true_peak. This is the
                         expensive table. Never scan it to rank. Read it only for named
                         title_ids.

  vault.latest_events    One row per defect occurrence in the newest scan of each title:
                         title_id, kind (black, freeze, silence, subtitle_speed,
                         subtitle_short, subtitle_long_line), start_seconds, end_seconds,
                         detail. Read this rather than vault.events, which is append-only
                         and still holds every earlier scan.

  vault.findings         One row per spec check per title: title_id, title, check_name,
                         spec, measured, target, unit, passed, auto_fixable, rescue_cost,
                         detail. `measured` on integrated_loudness_ebu_r128 is ffmpeg's
                         own gated integrated loudness, the only number allowed to be
                         called "integrated".

Specs: EBU R128 integrated -23 LUFS, tolerance 1.0 LU. ATSC A/85 -24 LKFS, tolerance
2.0 LU. True peak ceiling -1.0 dBTP. Netflix TTSS 17 chars per second, 5/6 s minimum
cue, 42 characters per line.

Rules for every agent here:
  - Query through the run_query tool. The connection is read-only; anything that is not
    a SELECT is refused before it reaches the server.
  - Never state a number you did not read out of a query result.
  - A scan covers the first 120 seconds of each title, so say "in the scanned window"
    rather than implying a full-length measurement.
"""


SCOUT_INSTRUCTION = SCHEMA_BRIEF + """
You are the fleet scout. You open the catalog and decide how deep this week's queue cuts.

Run exactly one query against vault.fleet, ordered by lufs_delta descending, and choose
the LIMIT yourself: enough titles to fill a week of mix-stage time, never more than
""" + str(QUEUE_CAP) + """. Select title_id, title, verdict, lane, failures, auto_fixable,
needs_human, integrated_lufs, lufs_delta, quietest_lufs, listen_at_seconds,
loudness_samples.

Do not query vault.loudness_samples. Ranking the catalog off the raw stream is the
mistake vault.fleet exists to prevent, and a query that scans it here is wrong even when
it returns the same answer.

After the query returns, stop calling tools and emit JSON and nothing else, with keys:
cut (integer, how many titles you kept), reason (one sentence on where you cut the queue
and why), titles (a list of the rows you selected, each an object using the column names
above).
"""


LOUDNESS_INSTRUCTION = SCHEMA_BRIEF + """
You are the loudness analyst. The fleet scout has already ranked the catalog:

{fleet}

Your job is the question the ranking cannot answer: for each failing title, is this a
flat offset that one loudnorm pass corrects, or is the programme so wide that a single
gain change would bury the dialogue while fixing the average?

For the title_ids in the ranking above, and only those, query vault.loudness_samples.
Useful shapes, adapt them:
  - stddevPop(short_term), and max(short_term) - min(short_term), grouped by title_id,
    over rows where short_term > -70. That spread is what decides the answer.
  - quantile(0.1)(short_term) and quantile(0.9)(short_term) grouped by title_id.
Use WHERE title_id IN (a list of the ranked ids) so you read only the queued titles.

Call run_query at most three times. Then stop calling tools and emit JSON and nothing
else, with key titles: a list of objects each holding title_id, spread_lu (float, from
your query), profile (either FLAT or WIDE), offset_lu (signed LU the title must move to
reach -23, from the ranking), and note (one sentence citing a number you queried).

A title is WIDE when the short-term spread is over 20 LU. A WIDE title cannot be fixed
by gain alone, and its note must say so.
"""


STRUCTURAL_INSTRUCTION = SCHEMA_BRIEF + """
You are the structural analyst. The fleet scout has already ranked the catalog:

{fleet}

Your job is to separate damage from ordinary film. A black segment over silence is a
reel change and is not a defect. A black segment over programme audio is dropout and a
human has to look at it. The event row alone cannot tell those apart; the loudness at
the instant the event began can.

For the title_ids in the ranking above, query vault.latest_events. Where events exist, run the
point-in-time join that stamps each event with the loudness at its start:

  SELECT e.title_id, e.kind, e.start_seconds, e.end_seconds, e.detail, s.short_term
  FROM vault.latest_events AS e
  ASOF LEFT JOIN (
    SELECT title_id, t_seconds, toNullable(short_term) AS short_term
    FROM vault.loudness_samples
  ) AS s
    ON e.title_id = s.title_id AND s.t_seconds <= e.start_seconds
  WHERE e.title_id IN (the ranked ids)
  ORDER BY e.title_id, e.start_seconds

Keep the toNullable. An ASOF LEFT JOIN that matches nothing fills the right-hand
column with its type default, and the default for a Float32 is 0.0, which reads as
digital full scale. An event starting before the first loudness sample would come back
looking like the loudest possible signal. With toNullable a miss arrives as null, and a
null means "no loudness row at that instant", which you must report as unmeasured
rather than treating as a level.

If vault.latest_events holds nothing for these titles, say so plainly and fall back to
vault.findings for the structural check rows rather than inventing events.

Call run_query at most three times. Then stop calling tools and emit JSON and nothing
else, with key titles: a list of objects each holding title_id, events (integer),
needs_human (true or false), and note (one sentence citing a timecode and the loudness
you read at it, or stating plainly that no event rows exist for this title).
"""


ALLOCATOR_INSTRUCTION = """You are the delivery operations lead at a film archive. You
hold no database access. Three colleagues have already queried it for you.

The ranked catalog, from the fleet scout:
{fleet}

Loudness profiles:
{loudness_evidence?}

Structural findings:
{structural_evidence?}

Produce this week's work order. It is a queue, not a report: an ordered list of titles
with the bay each one goes to.

  BATCH  every failure on the title is auto-fixable and the loudness profile is FLAT, so
         it can go through an unattended loudnorm pass.
  HUMAN  the title needs a person: a structural defect, a subtitle line that has to be
         re-wrapped semantically, or a WIDE loudness profile where gain alone would bury
         the dialogue.
  READY  no failed checks. It ships.

Rules:
  - Order by how far out of spec the title is, worst first, using lufs_delta.
  - Never write a number that is not in the evidence above. If you want a figure nobody
    queried, leave it null.
  - rescue_cost is words and time, never money.
  - rationale is one sentence and must quote a measured number.
  - operator_note is three sentences: what to start today, what to defer, and the honest
    limit of this scan.
"""


GRAFANA_INSTRUCTION = """You publish this week's work order onto Grafana Cloud through the
official mcp-grafana tools. You do not query ClickHouse.

Work order:
{work_order}

Do this, in order:
  1. search_dashboards for title "Redslip fleet".
  2. If none exists, update_dashboard to create one titled Redslip fleet. One table
     panel. Columns: title, integrated LUFS, lufs_delta, lane. Use only numbers from
     the work order above. Do not invent a figure.
  3. create_annotation on that dashboard for rank 1: the title and its lufs_delta.
  4. Stop calling tools. Emit JSON and nothing else with keys dashboard_uid,
     dashboard_url (https://marooncandy1058.grafana.net/d/<uid>), titles_plotted
     (integer, the queue length you put on the panel).

If every Grafana tool call fails with authentication, emit
{"published": false, "reason": "grafana mcp refused authentication"} and no URL.
Never invent a dashboard URL.
"""


# --------------------------------------------------------------------------
# The output contract for the allocator
# --------------------------------------------------------------------------

def work_order_schema():
    from pydantic import BaseModel, Field

    class QueueEntry(BaseModel):
        rank: int = Field(description="1 is the first title into the bay")
        title_id: str
        title: str
        lane: str = Field(description="BATCH, HUMAN or READY")
        integrated_lufs: float | None = None
        lufs_delta: float | None = None
        failures: int = 0
        rescue_cost: str
        rationale: str

    class WorkOrder(BaseModel):
        summary: str = Field(description="two sentences for the archive manager")
        queue: list[QueueEntry]
        operator_note: str

    return WorkOrder


# --------------------------------------------------------------------------
# The crew
# --------------------------------------------------------------------------

def build_crew(with_grafana: bool | None = None):
    """SequentialAgent(scout -> ParallelAgent(loudness, structural) -> allocator [-> grafana]).

    `with_grafana` defaults to whether a Grafana token is configured. When it is not,
    the board agent is not built and not added, so the ClickHouse crew is the same four
    agents it has always been and runs in the same time. Nothing about the ClickHouse
    path is conditional on Grafana.
    """
    from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent

    if with_grafana is None:
        with_grafana = grafana_credentials_present()

    scout = LlmAgent(
        name="fleet_scout",
        model=MODEL,
        description="Ranks the catalog off the rollup and decides how deep the queue cuts.",
        instruction=SCOUT_INSTRUCTION,
        tools=[clickhouse_toolset()],
        before_tool_callback=_guard_tool,
        after_tool_callback=_record_tool,
        output_key="fleet",
    )

    loudness = LlmAgent(
        name="loudness_analyst",
        model=MODEL,
        description="Decides whether each loudness failure is a flat offset or a wide programme.",
        instruction=LOUDNESS_INSTRUCTION,
        tools=[clickhouse_toolset()],
        before_tool_callback=_guard_tool,
        after_tool_callback=_record_tool,
        output_key="loudness_evidence",
    )

    structural = LlmAgent(
        name="structural_analyst",
        model=MODEL,
        description="Separates reel changes from damage by joining events to loudness.",
        instruction=STRUCTURAL_INSTRUCTION,
        tools=[clickhouse_toolset()],
        before_tool_callback=_guard_tool,
        after_tool_callback=_record_tool,
        output_key="structural_evidence",
    )

    evidence = ParallelAgent(
        name="evidence",
        description="Two readings of the same queue, taken from two different tables.",
        sub_agents=[loudness, structural],
    )

    allocator = LlmAgent(
        name="work_allocator",
        model=MODEL,
        description="Turns the evidence into an ordered queue with a bay per title.",
        instruction=ALLOCATOR_INSTRUCTION,
        output_schema=work_order_schema(),
        output_key="work_order",
    )

    pipeline = [scout, evidence, allocator]

    if with_grafana:
        pipeline.append(LlmAgent(
            name="grafana_board",
            model=MODEL,
            description="Publishes the work order onto Grafana Cloud through mcp-grafana.",
            instruction=GRAFANA_INSTRUCTION,
            tools=[grafana_toolset()],
            before_tool_callback=_guard_tool,
            after_tool_callback=_record_tool,
            output_key="grafana_publish",
        ))

    return SequentialAgent(
        name="redslip_triage",
        description="Rank the archive, read the evidence, allocate the week's work.",
        sub_agents=pipeline,
    )


TOPOLOGY = [
    {
        "name": "fleet_scout",
        "kind": "LlmAgent",
        "step": 1,
        "tools": "mcp-clickhouse",
        "reads": "vault.fleet",
        "does": "Ranks the catalog off the rollup and chooses how deep the queue cuts.",
    },
    {
        "name": "loudness_analyst",
        "kind": "LlmAgent, parallel",
        "step": 2,
        "tools": "mcp-clickhouse",
        "reads": "vault.loudness_samples",
        "does": "Flat offset or wide programme: can gain alone fix this master.",
    },
    {
        "name": "structural_analyst",
        "kind": "LlmAgent, parallel",
        "step": 2,
        "tools": "mcp-clickhouse",
        "reads": "vault.events ASOF vault.loudness_samples",
        "does": "Reel change or dropout: what the audio was doing when the picture went black.",
    },
    {
        "name": "work_allocator",
        "kind": "LlmAgent",
        "step": 3,
        "tools": "none",
        "reads": "the other three agents",
        "does": "Orders the queue and sends each title to BATCH, HUMAN or READY.",
    },
    {
        "name": "grafana_board",
        "kind": "LlmAgent",
        "step": 4,
        "tools": "mcp-grafana",
        "reads": "the work order",
        "does": "Publishes the queue onto Grafana Cloud through the official MCP server.",
    },
]


# One run of the crew at a time. Every agent holding MCP spawns a stdio subprocess,
# and two concurrent requests interleaving on those pipes is how this hangs.
_RUN_LOCK = asyncio.Lock()


async def run_crew(timeout_s: float = 300.0):
    """Run the crew, yielding one dict per event as it happens.

    An async generator rather than a return value, so the interface can show each
    agent's SQL at the moment the agent composed it. The final event carries the
    verified work order.
    """
    if not credentials_present():
        raise GeminiRequired(
            "Gemini credentials are required. Set GOOGLE_GENAI_USE_VERTEXAI=true with "
            "GOOGLE_CLOUD_PROJECT, or GOOGLE_API_KEY. Which titles buy a place in the "
            "queue, and in what order, is the judgement this crew exists for, and it has "
            "no offline substitute."
        )
    # Grafana is a bonus surface on a ClickHouse-track entry, so a missing token skips
    # the board rather than failing the run. The skip is reported, never silent: a step
    # that did not happen must not read like a step that succeeded.
    with_grafana = grafana_credentials_present()

    from google.adk.runners import InMemoryRunner
    from google.genai import types

    run_id = uuid.uuid4().hex[:12]
    started = time.time()

    async with _RUN_LOCK:
        runner = InMemoryRunner(agent=build_crew(with_grafana), app_name=APP_NAME)
        session = await runner.session_service.create_session(app_name=APP_NAME, user_id="qc")
        message = types.Content(
            role="user",
            parts=[types.Part(text=(
                "Triage this archive. Rank it, read the evidence, and give me this week's "
                "work order."
            ))],
        )

        emitted = 0
        state: dict = {}
        events = runner.run_async(
            user_id="qc", session_id=session.id, new_message=message
        ).__aiter__()

        while True:
            remaining = timeout_s - (time.time() - started)
            if remaining <= 0:
                yield {"type": "error", "message": f"crew exceeded {timeout_s:.0f}s"}
                break
            try:
                event = await asyncio.wait_for(events.__anext__(), timeout=remaining)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                yield {"type": "error", "message": f"crew exceeded {timeout_s:.0f}s"}
                break

            author = getattr(event, "author", None)
            if author and author not in ("user", APP_NAME):
                yield {"type": "agent", "agent": author}

            state = await _session_state(runner, session)
            trace = state.get("sql_trace") or []
            while emitted < len(trace):
                entry = trace[emitted]
                emitted += 1
                yield {
                    "type": "query",
                    "agent": entry.get("agent"),
                    "tool": entry.get("tool"),
                    "sql": entry.get("sql"),
                    "rows": entry.get("rows"),
                    "blocked": entry.get("blocked"),
                    "at": entry.get("at"),
                }

        state = await _session_state(runner, session)

    trace = list(state.get("sql_trace") or [])
    raw_order = state.get("work_order")
    if not raw_order:
        raise GeminiRequired(
            "The crew finished without a work order: the allocator returned no structured "
            "output, so there is nothing to dispatch."
        )
    order = raw_order if isinstance(raw_order, dict) else json.loads(raw_order)

    verified, unsupported = verify_against_cells(order, trace)
    yield {
        "type": "work_order",
        "run_id": run_id,
        "model": MODEL,
        "elapsed_s": round(time.time() - started, 1),
        # When the run began, so the caller can price the statements above against
        # system.query_log without matching anything older than this run.
        "started_epoch": started,
        "queries": [
            {k: entry.get(k) for k in ("agent", "tool", "sql", "rows", "blocked", "at")}
            for entry in trace
        ],
        "verified": verified,
        "unsupported_figures": unsupported,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "grafana_board": _board_result(with_grafana, state),
        **order,
    }


def _board_result(with_grafana: bool, state: dict) -> dict:
    """What the board step did, in a shape the interface can render honestly.

    `ran` is the only field a caller should branch on. A skipped step carries the
    reason it was skipped and no publish payload, so it can never be drawn as a
    completed one: there is nothing there to draw.
    """
    if not with_grafana:
        return {
            "ran": False,
            "skipped": True,
            "reason": "no Grafana service account token configured",
            "detail": (
                "Redslip is a ClickHouse-track entry and the Grafana board is a bonus "
                "surface. Set GRAFANA_SERVICE_ACCOUNT_TOKEN to add the grafana_board "
                f"agent to the crew; GRAFANA_URL defaults to {grafana_url()}."
            ),
            "publish": None,
        }
    return {
        "ran": True,
        "skipped": False,
        "reason": None,
        "detail": None,
        "publish": state.get("grafana_publish"),
    }


async def _session_state(runner, session) -> dict:
    live = await runner.session_service.get_session(
        app_name=APP_NAME, user_id="qc", session_id=session.id
    )
    return dict(getattr(live, "state", {}) or {})


# --------------------------------------------------------------------------
# Verification: a Python equality check, not a second model
# --------------------------------------------------------------------------

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def result_cells(trace: list[dict]) -> set[str]:
    """Every number ClickHouse returned during this run, as text.

    Each is kept in its exact form and rounded to one decimal, because an agent
    quoting -26.14 as -26.1 is quoting the database, while an agent quoting -19.0
    when the database said -26.1 is not.
    """
    out: set[str] = set()
    for entry in trace:
        for raw in _NUMBER.findall(entry.get("result") or ""):
            out.add(raw)
            try:
                out.add(f"{round(float(raw), 1):g}")
                out.add(f"{float(raw):.1f}")
            except ValueError:
                pass
    return out


def _supported(value, cells: set[str]) -> bool:
    if value is None:
        return True
    try:
        number = float(value)
    except (TypeError, ValueError):
        return True
    for form in (f"{number:g}", f"{number:.1f}", f"{round(number, 1):g}", str(value)):
        if form in cells:
            return True
    return False


def verify_against_cells(order: dict, trace: list[dict]) -> tuple[bool, list[dict]]:
    """Check that every figure on the work order came out of ClickHouse.

    The allocator holds no database tools, so each number it prints was either copied
    from the evidence its colleagues queried or produced by the model. Only the first
    kind belongs on a slip. Returns (all_supported, offenders).
    """
    cells = result_cells(trace)
    unsupported: list[dict] = []
    for entry in order.get("queue") or []:
        for field in ("integrated_lufs", "lufs_delta"):
            value = entry.get(field)
            if not _supported(value, cells):
                unsupported.append({
                    "title_id": entry.get("title_id"),
                    "field": field,
                    "claimed": value,
                })
    return (not unsupported), unsupported
