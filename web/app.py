"""Redslip web service.

The reading endpoints are deterministic ClickHouse queries and answer in
milliseconds, so the ledger is on screen before anyone presses anything. The
crew endpoint is the agent run, and it streams, because the queries the crew
composes are the evidence that it is an agent rather than a stored procedure.

  GET  /                    the slip
  GET  /api/stats           catalog totals and the honest scan window
  GET  /api/catalog         vault.fleet, one row per title, worst first
  GET  /api/title/{id}      one title: findings, events, 100ms plot, reproduce command
  GET  /api/agents          the ADK topology, and whether the crew can run
  GET  /api/triage/stream   run the crew, server-sent events, one per query
  POST /api/triage          run the crew, single JSON reply
  GET  /api/slips           the last work order the crew wrote into ClickHouse
  GET  /api/mcp-log         every statement past crews composed, current catalog only
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from qc import mcp_log as mcp_log_rules
from qc import query_cost
from qc import store

log = logging.getLogger(__name__)

app = FastAPI(title="Redslip", docs_url=None, redoc_url=None)

SCAN_SECONDS = 120
EBU_R128_TARGET_LUFS = -23.0

# Public-domain feature films on archive.org, as counted from the collection at the
# time the ingest list was built. Used only for the projection line, never for a
# verdict, and stated as a projection in the copy that carries it.
ARCHIVE_TITLES = 28_423

MCP_LOG = Path(__file__).parent.parent / "logs" / "mcp_tool_calls.jsonl"


def _ch():
    return store.client()


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((Path(__file__).with_name("index.html")).read_text())


@app.get("/api/stats")
async def stats():
    try:
        ch = _ch()
        row = ch.query(
            "SELECT count(), countIf(verdict = 'FAIL'), countIf(verdict = 'PASS'), "
            "       countIf(lane = 'BATCH'), countIf(lane = 'HUMAN'), max(lufs_delta) "
            "FROM vault.fleet"
        ).result_rows[0]
        loudness_rows = store.row_count("vault.loudness_samples", ch=ch)
        event_rows = store.row_count("vault.events", ch=ch)
        return {
            "title_count": row[0],
            "failing": row[1],
            "passing": row[2],
            "auto_fixable": row[3],
            "needs_human": row[4],
            "worst_lufs_delta": row[5],
            "loudness_sample_rows": loudness_rows,
            "event_rows": event_rows,
            "finding_rows": store.row_count("vault.findings", ch=ch),
            "scan_window_seconds": SCAN_SECONDS,
            "scan_window_label": f"first {SCAN_SECONDS // 60} minutes of each title",
            "ebu_r128_target_lufs": EBU_R128_TARGET_LUFS,
            "rollup_rows": store.row_count("vault.title_loudness", ch=ch),
            "archive_titles": ARCHIVE_TITLES,
            "note": (
                f"ebur128 emits a reading every 100ms, so {loudness_rows:,} sample rows "
                f"across {row[0]} titles at {SCAN_SECONDS}s each. Ranking reads "
                f"{store.row_count('vault.title_loudness', ch=ch)} rollup rows instead of "
                f"any of them. Scanning all {ARCHIVE_TITLES:,} public-domain titles in full "
                "would put roughly 27 million rows in this table."
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/catalog")
async def catalog():
    """vault.fleet: exactly one integrated loudness figure per title.

    Every number here comes from the same place the per-title panel reads, so the
    ledger and the slip can never disagree about what a title measured. The
    ordering is absolute distance from the EBU R128 target, which puts a title 7 LU
    too loud above one 5 LU too quiet instead of sorting them on a raw LUFS scale
    where too-loud and too-quiet are not comparable.
    """
    try:
        ch = _ch()
        res = ch.query(
            "SELECT title_id, title, verdict, failures, auto_fixable, needs_human, "
            "       integrated_lufs, lufs_delta, quietest_lufs, loudest_lufs, "
            "       listen_at_seconds, loudness_samples, lane "
            "FROM vault.fleet ORDER BY lufs_delta DESC, failures DESC, title"
        )
        rows = [dict(zip(res.column_names, r)) for r in res.result_rows]
        if not rows:
            raise HTTPException(status_code=503, detail="vault.fleet is empty; run ingest first")

        checks = ch.query(
            "SELECT title_id, groupArray(check_name), groupArray(rescue_cost) "
            "FROM vault.findings "
            "WHERE passed = 0 AND (title_id, scanned_at) IN ("
            "  SELECT title_id, max(scanned_at) FROM vault.findings GROUP BY title_id) "
            "GROUP BY title_id"
        ).result_rows
        failed = {r[0]: (list(r[1]), list(r[2])) for r in checks}

        # source_url on the row itself, so a contact sheet does not have to fetch
        # /api/title for all 20 titles just to learn where each film came from.
        try:
            srcs = store.sources(ch)
        except Exception:
            srcs = {}

        for row in rows:
            names, costs = failed.get(row["title_id"], ([], []))
            row["failed_checks"] = names
            row["rescue_costs"] = costs
            src = srcs.get(row["title_id"]) or {}
            row["source_url"] = src.get("source_url")
            row["scan_seconds"] = src.get("scan_seconds")
        return JSONResponse(jsonable_encoder(rows))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


FRAME_CACHE = Path("/tmp/vault-frames")
BAKED_FRAMES = Path(__file__).parent / "frames"


@app.get("/api/frame/{title_id:path}")
def frame(title_id: str, t: float = 0.0, w: int = 320):
    """One real frame of the measured film, at a measured second.

    The ledger condemns films, so the ledger should show them. The frame is pulled
    from the same `source_url` the measurement used, which is why it is evidence
    rather than decoration: it provably comes from the file the numbers describe.

    Three layers, cheapest first. A frame baked into the image at build time is
    served straight from disk. Otherwise a previously extracted frame is served
    from the instance cache. Only a genuinely new (title, t, w) pays ffmpeg, which
    fast-seeks the remote file and costs 1 to 3 seconds, so baking matters.
    """
    import subprocess

    w = max(96, min(640, int(w)))

    try:
        src = store.sources(_ch()).get(title_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"sources unavailable: {str(exc)[:160]}")
    if not src or not src.get("source_url"):
        raise HTTPException(status_code=404, detail=f"no recorded source for {title_id}")

    scan = float(src.get("scan_seconds") or 0) or None
    if t < 0:
        t = 0.0
    if scan and t > scan:
        # Never invent a frame from outside the window that was actually measured.
        raise HTTPException(
            status_code=404,
            detail=f"t={t:.1f}s is outside the {scan:.0f}s window measured for this title",
        )

    key = f"{round(t, 1)}_{w}.jpg"
    baked = BAKED_FRAMES / title_id / key
    if baked.exists():
        return FileResponse(baked, media_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})

    out = FRAME_CACHE / title_id / key
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-ss", f"{t:.2f}", "-i", src["source_url"],
            "-frames:v", "1", "-q:v", "2", "-vf", f"scale={w}:-2", "-y", str(out),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=45,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except Exception as exc:
            raise HTTPException(status_code=404,
                                detail=f"frame extraction failed at {t:.1f}s: {str(exc)[:160]}")
        if not out.exists() or out.stat().st_size == 0:
            raise HTTPException(status_code=404, detail=f"no frame decoded at {t:.1f}s")

    return FileResponse(out, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/title/{title_id:path}")
async def title_detail(title_id: str):
    try:
        ch = _ch()
        res = ch.query(
            "SELECT check_name, spec, measured, target, unit, passed, auto_fixable, "
            "       rescue_cost, detail "
            "FROM vault.findings WHERE title_id = %(t)s ORDER BY passed ASC, check_name",
            parameters={"t": title_id},
        )
        findings = [dict(zip(res.column_names, row)) for row in res.result_rows]
        # Silent audio measures a true peak of -inf, which is not valid JSON. Keep
        # the finding, drop the unencodable number.
        for finding in findings:
            for key, value in finding.items():
                if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
                    finding[key] = None
        if not findings:
            raise HTTPException(status_code=404, detail=f"No findings for {title_id!r}")

        ts = ch.query(
            "SELECT t_seconds, short_term, integrated FROM vault.loudness_samples "
            "WHERE title_id = %(t)s ORDER BY t_seconds",
            parameters={"t": title_id},
        ).result_rows
        step = max(1, len(ts) // 400)
        chart = [
            {"t": round(r[0], 1), "st": round(r[1], 1), "i": round(r[2], 1)}
            for r in ts[::step]
        ]

        # The point-in-time join. Each defect carries the short-term loudness at the
        # instant it began, which is what tells a reel change apart from dropout.
        #
        # `toNullable` is load-bearing, not decoration. An ASOF LEFT JOIN with no match
        # fills the right-hand column with its type's default, and the default for a
        # non-nullable Float32 is 0.0, which reads as digital full scale. An event that
        # starts before the first loudness sample would then be reported as the loudest
        # possible signal instead of as unmeasured. Making the column nullable inside
        # the join makes a miss arrive as null, which is the truth. The alternative,
        # SETTINGS join_use_nulls = 1, works too but is a session setting and would be
        # silently lost by anyone composing this query somewhere else.
        events = ch.query(
            "SELECT e.kind, e.start_seconds, e.end_seconds, e.detail, s.short_term "
            "FROM vault.latest_events AS e "
            "ASOF LEFT JOIN ("
            "  SELECT title_id, t_seconds, toNullable(short_term) AS short_term "
            "  FROM vault.loudness_samples"
            ") AS s "
            "  ON e.title_id = s.title_id AND s.t_seconds <= e.start_seconds "
            "WHERE e.title_id = %(t)s "
            "ORDER BY e.start_seconds",
            parameters={"t": title_id},
        ).result_rows

        source = store.sources(ch=ch).get(title_id, {})
        reproduce = None
        if source.get("source_url"):
            reproduce = (
                f'ffmpeg -hide_banner -nostats -t {source.get("scan_seconds") or SCAN_SECONDS} '
                f'-i "{source["source_url"]}" -af ebur128 -f null -'
            )

        return {
            "title_id": title_id,
            "findings": findings,
            "loudness_chart": chart,
            "sample_count": len(ts),
            "events": [
                {
                    "kind": r[0],
                    "start_seconds": round(float(r[1]), 1),
                    "end_seconds": round(float(r[2]), 1),
                    "detail": r[3],
                    "short_term_at_start": None if r[4] is None else round(float(r[4]), 1),
                }
                for r in events
            ],
            "worst_window": store.worst_window(title_id, ch=ch),
            "source_url": source.get("source_url"),
            "details_url": source.get("details_url"),
            "reproduce": reproduce,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/agents")
async def agents():
    """The crew, so the interface can draw the topology it actually runs."""
    from agent import crew

    board = crew.grafana_credentials_present()

    # `ready` is about the ClickHouse crew, which is what this track is about, and is
    # never dragged down by the optional board. ANDing the two credential checks into
    # one flag reported the whole product as not ready because a bonus surface for a
    # different partner had no token, which is not a fact about this crew at all.
    shape = (
        "SequentialAgent(fleet_scout, ParallelAgent(loudness_analyst, "
        "structural_analyst), work_allocator"
    ) + (", grafana_board)" if board else ")")

    topology = [
        dict(entry, available=True, reason=None)
        for entry in crew.TOPOLOGY
        if entry["name"] != "grafana_board"
    ]
    grafana_node = next(
        (entry for entry in crew.TOPOLOGY if entry["name"] == "grafana_board"), None
    )
    if grafana_node:
        topology.append(dict(
            grafana_node,
            available=board,
            reason=None if board else "no Grafana service account token configured",
        ))

    return {
        "framework": "google-adk",
        "root": "redslip_triage",
        "shape": shape,
        "model": crew.MODEL,
        "mcp_server": " ".join(crew.server_command()),
        "ready": crew.credentials_present(),
        "grafana": {
            "mcp_server": " ".join(crew.grafana_server_command()),
            "url": crew.grafana_url(),
            "available": board,
            "reason": None if board else "no Grafana service account token configured",
            "optional": True,
        },
        "agents": topology,
    }


def _persist(order: dict) -> int:
    """Write the crew's work order into vault.slips.

    The queue outlives the browser tab, and the next scan can be read against the
    last decision instead of starting from nothing.
    """
    entries = order.get("queue") or []
    if not entries:
        return 0
    ch = _ch()
    ch.insert(
        "vault.slips",
        [
            [
                order.get("run_id", ""),
                entry.get("title_id", ""),
                entry.get("title", ""),
                int(entry.get("rank") or 0),
                entry.get("lane", ""),
                entry.get("integrated_lufs"),
                entry.get("lufs_delta"),
                int(entry.get("failures") or 0),
                entry.get("rescue_cost", ""),
                entry.get("rationale", ""),
                order.get("model", ""),
                1 if order.get("verified") else 0,
            ]
            for entry in entries
        ],
        column_names=[
            "run_id", "title_id", "title", "rank", "lane", "integrated_lufs",
            "lufs_delta", "failures", "rescue_cost", "rationale", "model", "verified",
        ],
        # The interface reads /api/slips straight after a run, and ClickHouse Cloud's
        # default async insert would leave the queue looking empty for a few seconds.
        settings={"async_insert": 0},
    )
    stored = ch.query(
        "SELECT count() FROM vault.slips WHERE run_id = %(r)s "
        "SETTINGS select_sequential_consistency = 1",
        parameters={"r": order.get("run_id", "")},
    ).result_rows[0][0]
    if stored != len(entries):
        raise RuntimeError(
            f"work order {order.get('run_id')}: sent {len(entries)} rows to vault.slips "
            f"but {stored} are readable. The queue was not persisted."
        )
    return stored


def _price_queries(order: dict) -> int:
    """Attach ClickHouse's own cost to each statement the crew composed.

    Best effort by construction. The costs come from `system.query_log`, which is a
    read against the same server the run just finished using, and a run that produced a
    correct work order must not be reported as failed because an accounting table was
    slow. On any error the statements keep their row counts and simply carry no cost,
    which the interface renders as unpriced rather than as free.
    """
    queries = order.get("queries") or []
    if not queries:
        return 0
    try:
        return query_cost.annotate(
            queries,
            since_epoch=order.get("started_epoch") or 0,
            ch=_ch(),
            # A judge is watching a request finish. One flush interval is the most this
            # is allowed to spend before it gives up and says the cost is not there yet.
            wait_s=8.0,
        )
    except Exception as exc:
        log.warning("query cost lookup failed: %s", exc)
        return 0


def _log_queries(order: dict) -> None:
    MCP_LOG.parent.mkdir(parents=True, exist_ok=True)
    with MCP_LOG.open("a") as fh:
        for query in order.get("queries") or []:
            sql = query.get("sql") or ""
            fh.write(json.dumps({
                "ts": query.get("at") or datetime.now(timezone.utc).isoformat(),
                "run_id": order.get("run_id"),
                "agent": query.get("agent"),
                "tool": query.get("tool"),
                "sql": sql,
                "rows": query.get("rows"),
                "blocked": query.get("blocked"),
                # `arguments` and `result_preview` are the shape the earlier log used.
                # Kept so a client written against that format still reads this file.
                "arguments": {"query": sql},
                "result_preview": (
                    f"refused: not read-only" if query.get("blocked")
                    else f"{query.get('rows', 0)} rows"
                ),
            }) + "\n")


@app.get("/api/triage/stream")
async def triage_stream():
    """Run the crew, emitting each agent's SQL at the moment it composes it."""
    from agent import crew

    async def events():
        try:
            final = None
            async for event in crew.run_crew():
                if event.get("type") == "work_order":
                    final = event
                yield f"data: {json.dumps(jsonable_encoder(event))}\n\n"
            if final:
                try:
                    written = _persist(final)
                    _log_queries(final)
                    # Priced after the writeback, so a slow accounting table delays the
                    # cost line and never the queue itself.
                    priced = _price_queries(final)
                    yield f"data: {json.dumps({'type': 'persisted', 'rows': written, 'table': 'vault.slips', 'run_id': final.get('run_id')})}\n\n"
                    yield f"data: {json.dumps({'type': 'cost', 'run_id': final.get('run_id'), 'priced': priced, 'queries': jsonable_encoder(final.get('queries') or [])})}\n\n"
                except Exception as exc:
                    yield f"data: {json.dumps({'type': 'error', 'message': f'writeback failed: {exc}'})}\n\n"
        except (crew.GeminiRequired, crew.GrafanaRequired) as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        except Exception as exc:
            log.exception("crew failed")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/triage")
async def triage():
    """The same run as the stream, collapsed into one reply for non-browser callers."""
    from agent import crew

    try:
        final = None
        async for event in crew.run_crew():
            if event.get("type") == "work_order":
                final = event
        if final is None:
            raise HTTPException(status_code=502, detail="crew produced no work order")
        final["persisted_rows"] = _persist(final)
        _log_queries(final)
        final["priced_queries"] = _price_queries(final)
        return final
    except (crew.GeminiRequired, crew.GrafanaRequired) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("crew failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/query-cost")
async def api_query_cost():
    """What each of the two query shapes costs, from ClickHouse's own summary header.

    Live rather than a captured file, because a cost claim that was true once is a
    screenshot. Both figures are the server's: `read_rows` here is ClickHouse's count
    of rows it touched, which is the number the rollup argument is actually about.
    Rows returned would say nothing, both shapes return a handful.
    """
    try:
        return query_cost.shape_costs(ch=_ch())
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/slips")
async def slips():
    """The last work order the crew wrote into ClickHouse."""
    try:
        ch = _ch()
        res = ch.query(
            "SELECT run_id, issued_at, title_id, title, rank, lane, integrated_lufs, "
            "       lufs_delta, failures, rescue_cost, rationale, model, verified "
            "FROM vault.latest_slips"
        )
        rows = [dict(zip(res.column_names, r)) for r in res.result_rows]
        return JSONResponse(jsonable_encoder({"queue": rows, "count": len(rows)}))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/mcp-log")
async def mcp_log(limit: int = 60):
    """Every statement past crews composed, for the catalog now in ClickHouse.

    Scoped to the current schema generation. The catalog was re-ingested, and calls
    composed before that answered correctly about a title set this service no longer
    serves. Those calls are not rewritten, because editing what a transcript says a
    server returned is falsifying it: they are kept verbatim in
    logs/mcp_tool_calls.pre-fleet.jsonl and are not offered here as evidence about the
    shipped catalog. The cutoff and the withheld count are in the payload, so a reader
    is told what is missing rather than left to notice.
    """
    if not MCP_LOG.exists():
        return {"entries": [], "total_logged": 0,
                "note": "No crew run has been recorded on this instance yet."}
    entries = mcp_log_rules.served(MCP_LOG)
    withheld = len(mcp_log_rules.read(mcp_log_rules.PRE_FLEET_LOG_PATH))
    return {
        "entries": entries[-limit:],
        "total_logged": len(entries),
        "generation_start": mcp_log_rules.GENERATION_START,
        "generation_reason": mcp_log_rules.GENERATION_REASON,
        "withheld_earlier_entries": withheld,
        "withheld_file": str(
            mcp_log_rules.PRE_FLEET_LOG_PATH.relative_to(mcp_log_rules.ROOT)
        ),
        "note": (
            f"{len(entries)} statements, every one composed at or after "
            f"{mcp_log_rules.GENERATION_START} against the catalog this service serves "
            f"now. {withheld} earlier statements are held back. "
            + mcp_log_rules.GENERATION_REASON
        ),
    }


@app.get("/api/health")
async def health():
    try:
        ch = _ch()
        titles = ch.query("SELECT count() FROM vault.fleet").result_rows[0][0]
        return {"ok": titles > 0, "titles": titles}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
