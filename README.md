# Redslip: archive delivery triage

**Track: Grafana** | Agentic Cinema: The Blockbuster Hackathon. Partner wiring: `ARCHITECTURE.md`.

Live: https://vault-387894104564.us-central1.run.app
Repo: https://github.com/passionate-dev7/vault

---

## The incident

A distributor uploads a finished film to a streamer. Eleven days later it comes back
rejected: integrated loudness out of spec, subtitle cues over the reading-speed limit, a
black segment flagged as damage. Nobody watched the film wrong. The numbers were simply
never measured before delivery, and the re-deliver cycle costs weeks.

Redslip prevents that at catalog scale. Point it at a film archive and it tells you, with
machine-measured numbers, which titles would fail delivery today, in what order to touch
them, and which ones a machine can fix without a human in the room.

**The user:** the catalog or mastering QC lead at an indie distributor or film archive, the
person who decides which titles get mix-stage time this week. Today they either pay per
seat for Telestream Vantage or Venera Pulsar, or they listen to masters by ear.

**Nobody has run this in production yet.** It runs on 20 public-domain titles from
archive.org, measured live, and every number below can be reproduced by a stranger with
ffmpeg and no access to this machine.

---

## Reproduce any number on the page

The interface prints the exact command next to each measurement. It is not a paraphrase of
what the code does, it is the command:

```bash
ffmpeg -hide_banner -nostats -t 120 \
  -i "https://archive.org/download/NightTide16x9CorrectedAudio/NightTide_512kb.mp4" \
  -af ebur128 -f null -
```

```
Integrated loudness:
  I:         -17.4 LUFS
  Threshold: -27.7 LUFS
```

-17.4 LUFS against the EBU R128 target of -23.0 is 5.6 LU out of spec, which is what the
slip for that title says. `vault.sources` stores the URL that was measured for every title,
so the command is generated from the recorded source rather than assembled by hand.

---

## What it measures

| Spec | Threshold | Encoded at |
|---|---|---|
| EBU R128 integrated loudness | -23.0 LUFS, tolerance 1.0 LU | `qc/measure.py` |
| ATSC A/85, CALM Act | -24.0 LKFS, tolerance 2.0 LU | `qc/measure.py` |
| True peak ceiling | -1.0 dBTP | `qc/measure.py` |
| Netflix TTSS reading speed | 17 characters per second | `qc/measure.py` |
| Netflix TTSS minimum cue | 5/6 second | `qc/measure.py` |
| Netflix TTSS line length | 42 characters per line | `qc/measure.py` |
| Black segments | none at or over 2 seconds | `qc/measure.py` |
| Frozen video | no freeze events | `qc/measure.py` |

Subtitle checks run over the full subtitle track when a title ships one. None of the 20
titles in the current scan carries an SRT, so the subtitle checks contribute zero findings
to the numbers below. The code path is exercised by tests, not by this catalog.

---

## The current scan

20 titles, 24,017 loudness sample rows, 100 spec findings, 29 defect events. 17 titles
would be rejected today.

| Title | Integrated LUFS | LU from target | Verdict | Bay |
|---|---|---|---|---|
| Fugitive Valley | -37.2 | 14.2 | FAIL | BATCH |
| Outpost In Morocco | -15.8 | 7.2 | FAIL | BATCH |
| Farewell to Arms, A | -16.5 | 6.5 | FAIL | BATCH |
| Quo Vadis | -16.5 | 6.5 | FAIL | BATCH |
| Romance on the Run | -16.9 | 6.1 | FAIL | BATCH |
| Night Tide, corrected audio | -17.4 | 5.6 | FAIL | BATCH |
| Follow Your Heart | -18.2 | 4.8 | FAIL | BATCH |
| Framed | -20.1 | 2.9 | FAIL | HUMAN |
| Vicki (1953) | -24.9 | 1.9 | FAIL | HUMAN |
| Werewolf In A Girls' Dormitory | -22.0 | 1.0 | PASS | READY |
| What Becomes Of The Children? | -23.9 | 0.9 | PASS | READY |
| Night Of The Living Dead, 720p | -22.4 | 0.6 | PASS | READY |

Three titles pass, which matters: a gate that fails everything is not measuring anything.
The ordering is absolute distance from target in LU, so a title 7 LU too loud outranks one
5 LU too quiet. Raw LUFS ordering would put every loud title at one end of the list and
every quiet one at the other, which is not a severity order.

---

## Why ClickHouse is load-bearing

ffmpeg's `ebur128` filter emits a loudness reading every 100 milliseconds. That is about 10
rows per second of content: a 90-minute feature is roughly 54,000 rows, and archive.org
holds 28,423 public-domain titles.

The interesting part is not the row count, it is which rows get read.

**Ranking the catalog never touches the sample stream.** `vault.title_loudness` is an
`AggregatingMergeTree` keyed on `title_id`, kept current by a materialized view whose
`GROUP BY` is the rollup's `ORDER BY`. Ingesting title 21 merges one part in; it does not
rebuild the fleet. `vault.fleet` reads that rollup, one row per title, joined to the small
findings table. `EXPLAIN indexes = 1` on the ranking query names `vault.findings` and
`vault.title_loudness` and does not mention `vault.loudness_samples`, and there is a test
that asserts exactly that. The plan itself is committed at
[`docs/evidence/explain-ranking-plan.txt`](docs/evidence/explain-ranking-plan.txt),
captured through the MCP server against ClickHouse Cloud, with the control query that
proves `EXPLAIN` does name the samples table when a query genuinely reads it. The
server's refusal to write is committed the same way at
[`docs/evidence/readonly-refusal.txt`](docs/evidence/readonly-refusal.txt): a DDL and a
DML statement, both answered `Code 164 READONLY` by ClickHouse itself, with the archive
unchanged either side. Both files carry the command that produced them, so a judge who
cannot reach the Cloud database can still read what came back, and anyone who can reach
it can re-run them with `.venv/bin/python scripts/capture_mcp_evidence.py`.

**The sample stream exists for one job.** Opening a single title runs an `ASOF LEFT JOIN`
from `vault.events` to `vault.loudness_samples`, stamping each defect with the short-term
loudness at the instant it began. That is the join that separates a reel change from
damage. Night Tide has black at 1.2s with short-term loudness of -120.7 dB, which is
silence, so it is a reel change; and black at 5.3s at -42.0, which is programme audio
running under a dark frame, so it wants a human. Same two rows, opposite dispositions, and
only the point-in-time join tells them apart.

**Storage is laid out for the shape of the data.** `Gorilla` then `ZSTD(3)` on the loudness
floats, because Gorilla is the XOR-of-successive-values codec built for a slowly varying
float series. `DoubleDelta` on the scan clock. `LowCardinality(String)` on `title_id`.
`PARTITION BY cityHash64(title_id) % 8`, so re-scanning one title after a repair is a
partition drop rather than a mutation, which is what a re-delivery actually is.

**The decisions land back in the database.** `vault.slips` is an append-only log of every
work order the crew has issued, with the lane and rationale per title and a flag recording
whether each figure was verified against a returned cell. The queue outlives the browser
tab, and the next scan can be read against the last decision.

Fat append-only series, small interactive rollup, point-in-time join, ledger of what was
decided. Delete ClickHouse and there is no product, only an ffmpeg script.

### The MCP requirement

The crew reaches ClickHouse only through the official `mcp-clickhouse` MCP server, held by
Google ADK's `McpToolset` over stdio. It composes its own SQL; nothing in the agent layer
runs a stored query string. Every statement is recorded with the agent that wrote it, the
row count it returned, and whether it was refused, and the interface shows that list. Bulk
insertion of the 100ms samples stays on `clickhouse-connect`, because `run_query` is not an
insert path and pretending otherwise would be worse engineering, not better compliance.

---

## The agents

Google ADK, `google-adk` 2.8.0, in `agent/crew.py`:

```
SequentialAgent  redslip_triage
├── LlmAgent      fleet_scout          McpToolset -> mcp-clickhouse
│                 reads vault.fleet, ranks the catalog, chooses how deep to cut
│                 output_key: fleet
├── ParallelAgent evidence
│   ├── LlmAgent  loudness_analyst     McpToolset -> mcp-clickhouse
│   │             reads vault.loudness_samples for the queued titles only
│   │             flat offset or wide programme: can gain alone fix this master
│   │             output_key: loudness_evidence
│   └── LlmAgent  structural_analyst   McpToolset -> mcp-clickhouse
│                 reads vault.events ASOF vault.loudness_samples
│                 reel change or dropout: what the audio was doing at the cut
│                 output_key: structural_evidence
└── LlmAgent      work_allocator       no database tools at all
                  ranks the queue, assigns BATCH / HUMAN / READY
                  output_key: work_order, output_schema pinned
```

Every agent changes an outcome the others cannot reach. The scout decides the length of the
queue. The loudness analyst decides whether a failure is repairable by gain, which is the
difference between BATCH and HUMAN and is not in any column. The structural analyst decides
whether a black frame is damage, which needs a join no verdict row contains. The allocator
holds the ordering and the lane assignment, and holds no tools, so it can only quote its
colleagues.

The two analysts are parallel because they read different tables and neither needs the
other's answer. That is the only pair in the graph where that is true.

**There is no critic agent.** Checking whether the allocator invented a number is an
equality test between its output and the cells MCP returned, so it is written as one, in
`verify_against_cells`, in Python. Every run reports `verified` and lists any unsupported
figure. Paying a second model to audit the first would be theatre.

**A read-only gate, not a hope.** `before_tool_callback` inspects each statement before it
reaches the MCP server and refuses anything that is not read-only, after stripping comments
so that `SELECT 1 -- \n; DROP TABLE x` does not slip through a prefix check. A refused
statement is recorded as refused and the agent is told why.

**Gemini is not removable.** With no credentials the crew raises `GeminiRequired` and
produces nothing. There is no canned plan behind it. A deterministic substitute emitting the
same shape would make the model decorative, which is the mistake this project was built to
avoid.

---

## Architecture

```
archive.org
    |
ingest.py                          deterministic, no model
    ffprobe stream metadata
    ffmpeg ebur128            -> vault.loudness_samples   (100ms rows, Gorilla + ZSTD)
    ffmpeg blackdetect/freezedetect/silencedetect
                              -> vault.events             (one row per occurrence)
    SRT parse, Netflix TTSS   -> vault.events + vault.findings
    spec verdicts             -> vault.findings
    source URL                -> vault.sources
    |
materialized view             -> vault.title_loudness     (AggregatingMergeTree)
                                 vault.fleet              (the ranking, no sample scan)
    |
agent/crew.py                      Google ADK over official mcp-clickhouse
    fleet_scout, loudness_analyst, structural_analyst, work_allocator
    verify_against_cells          Python equality check, not a second model
                              -> vault.slips              (append-only decision log)
    |
web/app.py                         FastAPI, streams the run as it happens
```

---

## Honest limitations

- **Scan window.** Each title is measured over its first 120 seconds. A full 90-minute scan
  is roughly 6 minutes of ffmpeg per title. The window is stated in the interface and in
  every projection, and no number is presented as a full-length measurement.
- **Loudness over a window is not loudness over a film.** A title can normalise cleanly
  across an opening reel and drift later. The window catches most delivery failures. It is
  not a broadcast-grade full-scan and does not claim to be.
- **No remediation.** Redslip measures, ranks and dispatches. It does not repair. The
  loudnorm pass and the cue retiming are separate tooling.
- **No subtitle findings in the current catalog.** None of the 20 titles ships an SRT, so
  the TTSS checks are exercised by tests rather than by this scan.
- **The 28,423-title figure is a projection**, labelled as one everywhere it appears. 20
  titles are measured.

---

## Run it

### Prerequisites

- Python 3.13 or newer, `uv`, `ffmpeg` 7 or newer on PATH
- ClickHouse, either ClickHouse Cloud or a local server on 8123
- The official MCP server as an isolated tool: `uv tool install mcp-clickhouse`

`mcp-clickhouse` is installed as a tool rather than as a project dependency on purpose. It
pulls `fastmcp`, which needs the MCP SDK 2.x, while ADK's `McpToolset` is built against 1.x.
An MCP server is a separate process by design, so giving it a separate environment removes
the conflict instead of pinning around it.

### Setup

```bash
git clone https://github.com/passionate-dev7/vault
cd vault
uv sync
uv tool install mcp-clickhouse

# Apply the schema, migrate an existing database, seed the rollup
uv run python migrate.py

# Measure 20 titles, first 2 minutes each, roughly 10 to 15 minutes
uv run python ingest.py --limit 20
```

### Serve

```bash
bash run_web.sh
# or: PYTHONPATH=. uv run uvicorn web.app:app --port 8000
```

The ledger is on screen immediately, from a deterministic query. Pressing triage runs the
ADK crew, which takes roughly 50 seconds and streams each agent's SQL as the agent composes
it.

### Environment

| Variable | Default | Notes |
|---|---|---|
| `CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `CLICKHOUSE_PORT` | `8123`, or `8443` when secure | HTTP port |
| `CLICKHOUSE_USER` | `default` | Username |
| `CLICKHOUSE_PASSWORD` | empty | Password |
| `CLICKHOUSE_SECURE` | `false` | `true` for ClickHouse Cloud |
| `GOOGLE_GENAI_USE_VERTEXAI` | `false` | `true` to use Vertex AI |
| `GOOGLE_CLOUD_PROJECT` | none | Required with Vertex AI |
| `GOOGLE_CLOUD_LOCATION` | `us-central1` | Vertex AI region |
| `GOOGLE_API_KEY` | none | Alternative to Vertex AI |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Model for every agent |

Secrets belong in `.env`, which is gitignored. `scripts/cloudenv.sh` reads the deployed
service's own environment into your shell for local work, so no credential is ever written
to disk.

### API

| Endpoint | Returns |
|---|---|
| `GET /api/stats` | catalog totals, the scan window, the projection line |
| `GET /api/catalog` | `vault.fleet`, one row per title, worst first |
| `GET /api/title/{id}` | findings, defect events with loudness at each, the 100ms plot, the reproduce command |
| `GET /api/agents` | the ADK topology the crew actually runs |
| `GET /api/triage/stream` | one crew run as server-sent events, one per composed statement |
| `POST /api/triage` | the same run, one JSON reply |
| `GET /api/slips` | the last work order the crew wrote into ClickHouse |
| `GET /api/mcp-log` | every statement past crews composed against the catalog now in ClickHouse, with the generation cutoff stated in the payload |
| `GET /api/health` | readiness |

### Tests

```bash
uv run pytest tests/ -q
```

Checks that matter here were each broken deliberately, confirmed red, restored, and
confirmed green. The catalog-versus-findings agreement check runs over every title in the
live catalog and refuses to pass on an empty one, because an empty catalog agrees with
itself and proves nothing.

---

## Competitors

| Tool | Measures | Ranks a catalog | Repairs | Cost |
|---|---|---|---|---|
| Telestream Vantage | yes | reports per file | yes | per-seat licence |
| Venera Pulsar | yes | reports per file | no | paid cloud |
| Interra Baton | yes | reports per file | no | paid |
| **Redslip** | yes | yes, and dispatches to a bay | no | open source |

Vantage repairs and Redslip does not. What Redslip does that none of them does is answer
the catalog question: of these titles, which do I touch this week, in what order, and which
ones need a person. That is a question about a corpus of measurements, which is why the
answer lives in a column store and not in a per-file report.

---

## Licence

MIT. See `LICENSE`.
