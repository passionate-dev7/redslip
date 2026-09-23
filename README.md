# Redslip

Redslip measures every title in a film archive with ffmpeg, stores the results in
ClickHouse, and ranks the catalog by which titles would fail a streamer's delivery spec
today, worst first. A Google ADK crew then reads that catalog through the official
`mcp-clickhouse` MCP server and writes a work order that puts each failing title in a
lane: BATCH (a machine can fix it), HUMAN (someone has to look), or READY.

The current catalog is 20 public-domain films from archive.org, measured over the first
120 seconds of each.

## Checking a number yourself

Each title's detail view prints the ffmpeg command that produced its loudness figure,
built from the source URL stored in `vault.sources`. For Fugitive Valley:

```bash
ffmpeg -hide_banner -nostats -t 120 \
  -i "https://archive.org/download/fugitive_valley/fugitive_valley_512kb.mp4" \
  -af ebur128 -f null -
```

Running that prints, among other things:

```
  Integrated loudness:
    I:         -37.2 LUFS
    Threshold: -47.3 LUFS
```

That's 14.2 LU below the EBU R128 target of -23 LUFS, so the title lands at the top of
the ranking.

## What gets checked

All thresholds are constants at the top of `qc/measure.py`.

| Check | Limit |
|---|---|
| EBU R128 integrated loudness | -23.0 LUFS, 1.0 LU tolerance |
| ATSC A/85 (CALM Act) | -24.0 LKFS, 2.0 LU tolerance |
| True peak | -1.0 dBTP ceiling |
| Black segments | none of 2 seconds or longer |
| Frozen video | no freeze events |
| Subtitle reading speed (Netflix TTSS) | 17 characters per second |
| Subtitle minimum cue | 5/6 second |
| Subtitle line length | 42 characters, 2 lines |

The subtitle checks only run when a title has an SRT. None of the 20 titles in the
current catalog has one, so they contribute no findings there; `tests/test_qc.py`
covers them.

The catalog is ordered by absolute distance from the R128 target in LU, so a master
7 LU too loud ranks above one 5 LU too quiet.

## How it works

### Ingest (no model involved)

`ingest.py` downloads roughly 20 MB of each title from archive.org (`qc/archive.py`),
then `qc/measure.py` runs ffprobe, `ebur128`, `blackdetect`, `freezedetect` and
`silencedetect` over the first 120 seconds. `qc/store.py` writes the results with
`clickhouse-connect`. The seed list of archive.org identifiers is `SEED_TITLES` in
`ingest.py`.

### ClickHouse tables (`schema.sql`)

- `vault.loudness_samples`: one row per 100 ms `ebur128` reading, about 1,200 rows per
  title. Floats use `Gorilla` + `ZSTD(3)`, the timestamp uses `DoubleDelta`.
- `vault.findings`: one row per spec check per scan. Every verdict comes from here.
- `vault.events`: one row per black, freeze or silence occurrence.
- `vault.title_loudness`: an `AggregatingMergeTree` with one row per title, kept current
  by the materialized view `vault.mv_title_loudness`.
- `vault.fleet`: the ranked catalog. It reads `title_loudness` and `findings`, never the
  sample table. `tests/test_ranking_never_scans_samples.py` checks this against
  `EXPLAIN indexes = 1`.
- `vault.slips`: append-only log of every work order the crew has issued.

The sample table is read for individual titles only: by the two analysts for the titles
in the queue, and by a title's detail view. Both the structural analyst and the detail
view run an `ASOF LEFT JOIN` from `vault.latest_events` to `vault.loudness_samples` to get
the short-term loudness at the moment each defect starts. That separates a reel change
(black over silence) from dropout (black over programme audio).

`docs/evidence/query-cost.txt` has both query shapes priced from ClickHouse's own
response summary against the 20-title catalog: ranking read 320 rows, the percentile
query over the sample stream read 24,017. `scripts/capture_query_cost.py` regenerates
it, and `GET /api/query-cost` runs the same measurement live.

### The agents (`agent/crew.py`)

```
SequentialAgent  redslip_triage
├── LlmAgent      fleet_scout          mcp-clickhouse, reads vault.fleet
├── ParallelAgent evidence
│   ├── LlmAgent  loudness_analyst     mcp-clickhouse, reads vault.loudness_samples
│   └── LlmAgent  structural_analyst   mcp-clickhouse, reads vault.latest_events ASOF vault.loudness_samples
├── LlmAgent      work_allocator       no tools, output_schema pinned
└── LlmAgent      grafana_board        mcp-grafana, only built when a Grafana token is set
```

The scout picks which titles go in the queue (capped at 12). The loudness analyst decides
whether each loudness failure is a flat offset that one `loudnorm` pass fixes or a wide
dynamic range that needs a person. The structural analyst does the reel-change versus
dropout call. The allocator turns both into the ordered work order.

Each ClickHouse agent gets its own `McpToolset` over stdio, filtered to `run_query`,
`list_tables` and `list_databases`. The agents write their own SQL. Two callbacks wrap
every tool call:

- `_guard_tool` (`before_tool_callback`) runs `is_read_only` and refuses anything that
  writes, before the MCP server sees it. It strips comments and string literals first,
  so `SELECT 1 --\n; DROP TABLE x` is refused, and it refuses `INTO OUTFILE` /
  `INTO DUMPFILE`. The server is also started with `CLICKHOUSE_ALLOW_WRITE_ACCESS=false`;
  `docs/evidence/readonly-refusal.txt` shows ClickHouse itself answering `READONLY` to a
  DDL and a DML statement.
- `_record_tool` (`after_tool_callback`) records each statement, the agent that wrote it
  and the row count, which the UI streams as the run progresses.

After the run, `verify_against_cells` checks every figure in the work order against the
cells MCP actually returned and reports `verified` plus any unsupported figures. The web
layer saves the order to `vault.slips` and prices each statement from
`system.query_log`.

Without Gemini credentials the crew raises `GeminiRequired`. There is no fallback plan.
The measurements, ranking and title views still work.

## Running it

You need Python 3.13+, [uv](https://docs.astral.sh/uv/), ffmpeg on `PATH`, and a
ClickHouse server (local or ClickHouse Cloud).

```bash
git clone https://github.com/passionate-dev7/redslip
cd redslip
uv sync
uv tool install mcp-clickhouse
```

`mcp-clickhouse` goes in as a separate tool because it depends on MCP SDK 2.x while
ADK's `McpToolset` needs 1.x. The Dockerfile pins it to 0.6.0.

Point it at ClickHouse, create the schema, and measure some titles:

```bash
export CLICKHOUSE_HOST=localhost CLICKHOUSE_PORT=8123
uv run python migrate.py
uv run python ingest.py --limit 20
```

The 20-title ingest took 335 seconds here against a local ClickHouse and reported
"20 titles processed, 20 ok, 17 scanned as FAIL". Then serve:

```bash
bash run_web.sh          # http://localhost:8000
```

The ranked catalog loads straight from ClickHouse. Triage (the button in the UI, or
`POST /api/triage`) runs the crew and needs Gemini credentials; without them it returns
the `GeminiRequired` message.

To run the crew once from the command line and print the work order as JSON:

```bash
PYTHONPATH=. uv run python agent/triage.py
```

### Environment

Put these in `.env` (gitignored) or export them.

| Variable | Default | |
|---|---|---|
| `CLICKHOUSE_HOST` | `localhost` | |
| `CLICKHOUSE_PORT` | `8123`, `8443` when secure | |
| `CLICKHOUSE_USER` | `default` | |
| `CLICKHOUSE_PASSWORD` | empty | |
| `CLICKHOUSE_SECURE` | `false` | `true` for ClickHouse Cloud |
| `GOOGLE_API_KEY` | none | or use Vertex AI below |
| `GOOGLE_GENAI_USE_VERTEXAI` | `false` | set `true` with `GOOGLE_CLOUD_PROJECT` |
| `GOOGLE_CLOUD_PROJECT` | none | |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | |
| `GEMINI_MODEL` | `gemini-2.5-flash` | used by every agent |
| `GRAFANA_SERVICE_ACCOUNT_TOKEN` | none | adds `grafana_board` to the crew |
| `GRAFANA_URL` | `https://redslip.grafana.net` | |

`scripts/cloudenv.sh` copies the environment of the deployed Cloud Run service `vault`
into your shell (source it, don't pipe it). It needs `gcloud` access to that project.

### API (`web/app.py`)

| Endpoint | Returns |
|---|---|
| `GET /api/stats` | catalog totals and scan window |
| `GET /api/catalog` | `vault.fleet`, one row per title, worst first |
| `GET /api/title/{id}` | findings, events with loudness at onset, the 100 ms series, the ffmpeg command |
| `GET /api/frame/{id}?t=` | one video frame at a given second |
| `GET /api/agents` | the crew's topology and whether it can run |
| `GET /api/triage/stream` | one crew run as server-sent events |
| `POST /api/triage` | one crew run as a single JSON response |
| `GET /api/slips` | the latest work order from `vault.slips` |
| `GET /api/mcp-log` | statements past crew runs sent through MCP (`logs/mcp_tool_calls.jsonl`) |
| `GET /api/query-cost` | live cost of the ranking and percentile queries |
| `GET /api/health` | readiness and title count |

## Tests

```bash
uv run pytest tests/ -q
```

With no ClickHouse reachable, the tests that need the measured catalog skip and the run
ends with a red "ClickHouse integration NOT exercised" banner. On a fresh clone that gave
`101 passed, 33 skipped`.

The integration tests expect the 20-title catalog. Several need at least 10 measured
titles, and `tests/test_mcp_log_matches_stats.py` checks the counts in the committed MCP
log (`logs/mcp_tool_calls.jsonl`, 24,017 sample rows) against the live catalog, so it fails
if your row counts differ, for example after re-scanning a title, which appends rows.
Against a local catalog where two titles had been scanned twice, the run gave
`3 failed, 124 passed, 7 skipped`, all three failures from that file. If `CLICKHOUSE_HOST` names a
remote host that can't be used, those tests fail instead of skipping.

## Layout

```
agent/crew.py            ADK crew, MCP toolsets, read-only gate, verify_against_cells
agent/triage.py          one-shot entry point used by POST /api/triage
agent/grafana_publish.py direct mcp-grafana publisher (the web app doesn't call it)
qc/measure.py            ffmpeg/ffprobe measurement and spec checks
qc/store.py              ClickHouse writes and reads
qc/archive.py            archive.org search and download
qc/query_cost.py         per-statement cost from system.query_log
qc/mcp_log.py            reads the MCP log and checks it against live stats
ingest.py                measure titles and load them
migrate.py               apply schema.sql, migrate old tables, backfill the rollup
backfill_events.py       rebuild vault.events from cached media in data/
schema.sql               tables and views
web/app.py, index.html   FastAPI server and the single-page UI
scripts/                 evidence capture and cloudenv.sh
docs/evidence/           captured query plans, costs, read-only refusals
logs/                    MCP call logs from real runs
```

## Licence

MIT. See `LICENSE`.
