# Redslip

**The ledger of rejected masters.** Point Redslip at a film archive and it tells you,
with machine-measured numbers, which titles a streamer would reject today, in what order
to touch them, and which ones a machine can fix without a person in the room.

Live: https://vault-387894104564.us-central1.run.app
Repo: https://github.com/passionate-dev7/vault

---

## The problem it solves

One rejected master is an incident. A catalog of them is a pattern, and the pattern is
what nobody in delivery can see, because each rejection arrives as its own email, to its
own person, weeks apart from the last one.

A distributor uploads a finished film to a streamer. Days later it comes back rejected:
integrated loudness out of spec, subtitle cues over the reading-speed limit, a black
segment flagged as damage. Nobody watched the film wrong. The numbers were simply never
measured before delivery, and the re-deliver cycle costs weeks.

Redslip holds the whole catalog as one queryable ledger. Which titles ship, which need a
person, which a machine can correct unattended.

The regulator has the same visibility problem one level up, and it has been counting.
The FCC's March 2025 proposed rule on the CALM Act reopened a regime that had been in
force for twelve years, and footnote 4 says why: "in 2024 the Commission received at
least 1,700 complaints referencing loud commercials that appear to relate to broadcast
television, cable, and satellite, after receiving approximately 750 in 2022 and 825 in
2023." Complaints roughly doubled while the rule stood still. That is this project's
argument at national scale. A single loud advert is an incident and nobody can act on
it; a rising count across a whole population is a pattern, and a pattern is the thing
that gets a rule reopened. A per-title checker cannot see one. A ledger over the whole
archive can, which is why Redslip is shaped as a catalog rather than as a file inspector.

Source, read and quoted from the primary text rather than a summary of it: FCC,
*Implementation of the Commercial Advertisement Loudness Mitigation (CALM) Act*, Proposed
Rule, 90 FR, FR document
[2025-03800](https://www.federalregister.gov/documents/2025/03/11/2025-03800/implementation-of-the-commercial-advertisement-loudness-mitigation-calm-act),
published 11 March 2025, footnote 4. The full text is at
`https://www.federalregister.gov/documents/full_text/text/2025/03/11/2025-03800.txt`.

**Who it is for:** the catalog or mastering QC lead at an indie distributor or film
archive, the person who decides which titles get mix-stage time this week. The
alternative today is a per-seat licence for Telestream Vantage or Venera Pulsar, or
listening to masters by ear.

It currently runs against 20 public-domain titles from archive.org, measured live. Every
number below can be reproduced by a stranger with ffmpeg and no access to this machine.

---

## Reproduce any number on the page

The interface prints the exact command next to each measurement. It is not a paraphrase
of what the code does, it is the command, generated from the source URL recorded in
`vault.sources`:

```bash
ffmpeg -hide_banner -nostats -t 120 \
  -i "https://archive.org/download/fugitive_valley/fugitive_valley_512kb.mp4" \
  -af ebur128 -f null -
```

```
  Integrated loudness:
    I:         -37.2 LUFS
    Threshold: -47.3 LUFS
```

-37.2 LUFS against the EBU R128 target of -23.0 is 14.2 LU out of spec, which is what the
slip for that title says.

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

---

## The current scan

20 titles, 24,017 loudness sample rows, 100 spec findings, 29 defect events. 17 titles
would be rejected today: 11 into the batch lane, 6 to a person, 3 ready to ship.

| Title | Integrated LUFS | LU from target | Verdict | Lane |
|---|---|---|---|---|
| Fugitive Valley | -37.2 | 14.2 | FAIL | BATCH |
| Outpost In Morocco | -15.8 | 7.2 | FAIL | BATCH |
| quevadis | -16.5 | 6.5 | FAIL | BATCH |
| Farewell to Arms, A | -16.5 | 6.5 | FAIL | BATCH |
| Romance on the Run | -16.9 | 6.1 | FAIL | BATCH |
| Night Tide, corrected audio | -17.4 | 5.6 | FAIL | BATCH |
| Follow Your Heart | -18.2 | 4.8 | FAIL | BATCH |
| framed | -20.1 | 2.9 | FAIL | HUMAN |
| Crimson Romance | -25.9 | 2.9 | FAIL | BATCH |
| Rough Riding Ranger | -20.3 | 2.7 | FAIL | BATCH |
| Fit for a King | -21.0 | 2.0 | FAIL | BATCH |
| Vicki (1953) | -24.9 | 1.9 | FAIL | HUMAN |
| The Shadow Strikes | -24.8 | 1.8 | FAIL | HUMAN |
| Successful Failure | -24.5 | 1.5 | FAIL | HUMAN |
| Song for Miss Julie, A | -21.5 | 1.5 | FAIL | BATCH |
| Werewolf In A Girls' Dormitory | -22.0 | 1.0 | PASS | READY |
| Night of the Living Dead (1968) | -24.0 | 1.0 | FAIL | HUMAN |
| What Becomes Of The Children? | -23.9 | 0.9 | PASS | READY |
| Niagara Falls | -22.3 | 0.7 | FAIL | HUMAN |
| Night Of The Living Dead, 720p | -22.4 | 0.6 | PASS | READY |

Three titles pass, which matters: a gate that fails everything is not measuring anything.
The ordering is absolute distance from target in LU, so a title 7 LU too loud outranks
one 5 LU too quiet. Raw LUFS ordering would put every loud title at one end of the list
and every quiet one at the other, which is not a severity order.

Failures by check, over the same scan:

| Check | Titles failing |
|---|---|
| Integrated loudness, EBU R128 | 15 |
| Integrated loudness, ATSC A/85 | 11 |
| Black segments at or over 2 s | 5 |
| Frozen video | 2 |
| True peak ceiling | 0 |

---

## Why ClickHouse is load-bearing

ffmpeg's `ebur128` filter emits a loudness reading every 100 milliseconds. That is about
10 rows per second of content: a 90-minute feature is roughly 54,000 rows, and
archive.org holds 28,423 public-domain titles.

The interesting part is not the row count, it is which rows get read.

**Ranking the catalog never touches the sample stream.** `vault.title_loudness` is an
`AggregatingMergeTree` keyed on `title_id`, kept current by a materialized view whose
`GROUP BY` is the rollup's `ORDER BY`. Ingesting title 21 merges one part in; it does not
rebuild the fleet. `vault.fleet` reads that rollup, one row per title, joined to the small
findings table.

Two query shapes, measured against ClickHouse Cloud and read from the server's own
response summary rather than counted in Python:

| Measure | Ranking the catalog | Percentiles over the stream |
|---|---|---|
| Rows read | 320 | 24,017 |
| Bytes read | 11,372 | 120,680 |
| Rows returned | 12 | 8 |
| Elapsed | 16.5 ms | 5.5 ms |

```bash
source scripts/cloudenv.sh
.venv/bin/python scripts/capture_query_cost.py   # writes docs/evidence/query-cost.txt
```

The same two shapes are priced live at [`/api/query-cost`](https://vault-387894104564.us-central1.run.app/api/query-cost),
and the panel at the foot of the page is that endpoint, so the figures are this catalog
as it stands rather than a capture from a day that has passed.

Read the rows, not the milliseconds. At 24,017 rows the full scan is the *faster* of the
two: a stream that small is nothing to scan, while the ranking pays for a view stack over
three tables. That row is in the table above rather than dropped from it, because a cost
panel that hides its worst column is an advert. The claim is not that ranking is quick
today, it is that ranking costs a number of rows proportional to how many titles exist
and not to how much audio was measured, so the two columns diverge as the archive grows
in running time. The percentile query is the one that pays for the stream, and it is the
reason the percentiles are percentiles rather than a figure typed into a cell.

Every statement the crew composes is priced the same way, per statement, from
`system.query_log`. That is ClickHouse's own accounting of the agent's own SQL, looked up
by the text the agent wrote, and it is printed under each statement in the run's evidence
rail. A statement whose accounting has not been flushed yet says so; it is never drawn as
zero rows read.

`EXPLAIN indexes = 1` on the ranking query names `vault.findings` and
`vault.title_loudness` and does not mention `vault.loudness_samples`, and there is a test
that asserts exactly that. The plan itself is committed at
[`docs/evidence/explain-ranking-plan.txt`](docs/evidence/explain-ranking-plan.txt),
captured through the MCP server against ClickHouse Cloud, with the control query that
proves `EXPLAIN` does name the samples table when a query genuinely reads it. The
server's refusal to write is committed the same way at
[`docs/evidence/readonly-refusal.txt`](docs/evidence/readonly-refusal.txt): a DDL and a
DML statement, both answered `Code 164 READONLY` by ClickHouse itself, with the archive
unchanged either side. Both files carry the command that produced them, so anyone who
cannot reach the Cloud database can still read what came back, and anyone who can reach
it can re-run them with `.venv/bin/python scripts/capture_mcp_evidence.py`.

**The sample stream exists for one job.** Opening a single title runs an `ASOF LEFT JOIN`
from `vault.latest_events` to `vault.loudness_samples`, stamping each defect with the
short-term loudness at the instant it began. That is the join that separates a reel
change from damage. Night Tide has black at 1.17 s with short-term loudness of -120.7 dB,
which is silence, so it is a reel change; and black at 5.33 s at -42.0 dB, which is
programme audio running under a dark frame, so it wants a person. Same two rows, opposite
dispositions, and only the point-in-time join tells them apart.

**Storage is laid out for the shape of the data.** `Gorilla` then `ZSTD(3)` on the
loudness floats, because Gorilla is the XOR-of-successive-values codec built for a slowly
varying float series. `DoubleDelta` on the scan clock. `LowCardinality(String)` on
`title_id`. `PARTITION BY cityHash64(title_id) % 8`, so re-scanning one title after a
repair is a partition drop rather than a mutation, which is what a re-delivery actually
is.

**The decisions land back in the database.** `vault.slips` is an append-only log of every
work order the crew has issued, with the lane and rationale per title and a flag
recording whether each figure was verified against a returned cell. The queue outlives
the browser tab, and the next scan can be read against the last decision.

Fat append-only series, small interactive rollup, point-in-time join, ledger of what was
decided. Delete ClickHouse and there is no product, only an ffmpeg script.

### The MCP requirement

The crew reaches ClickHouse only through the official `mcp-clickhouse` MCP server, held
by Google ADK's `McpToolset` over stdio. It composes its own SQL; nothing in the agent
layer runs a stored query string. Every statement is recorded with the agent that wrote
it, the row count it returned, and whether it was refused, and the interface shows that
list. Bulk insertion of the 100 ms samples stays on `clickhouse-connect`, because
`run_query` is not an insert path and pretending otherwise would be worse engineering,
not better compliance.

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
│                 reads vault.latest_events ASOF vault.loudness_samples
│                 reel change or dropout: what the audio was doing at the cut
│                 output_key: structural_evidence
└── LlmAgent      work_allocator       no database tools at all
                  ranks the queue, assigns BATCH / HUMAN / READY
                  output_key: work_order, output_schema pinned
```

Every agent changes an outcome the others cannot reach. The scout decides the length of
the queue. The loudness analyst decides whether a failure is repairable by gain, which is
the difference between BATCH and HUMAN and is not in any column. The structural analyst
decides whether a black frame is damage, which needs a join no verdict row contains. The
allocator holds the ordering and the lane assignment, and holds no tools, so it can only
quote its colleagues.

The two analysts are parallel because they read different tables and neither needs the
other's answer. That is the only pair in the graph where that is true.

**There is no critic agent.** Checking whether the allocator invented a number is an
equality test between its output and the cells MCP returned, so it is written as one, in
`verify_against_cells`, in Python. Every run reports `verified` and lists any unsupported
figure. Paying a second model to audit the first would be theatre.

**The read-only gate runs before the server sees the statement.** `before_tool_callback` inspects each statement before it
reaches the MCP server and refuses anything that is not read-only, after stripping
comments and string literals so that `SELECT 1 --\n; DROP TABLE x` does not slip through
a prefix check. A refused statement is recorded as refused and the agent is told why.

**Gemini is not removable.** With no credentials the crew raises `GeminiRequired` and
produces nothing. There is no canned plan behind it, and
`tests/test_gemini_required.py` scans the whole agent package to keep one from being
reintroduced. A deterministic substitute emitting the same shape would make the model
decorative, which is the mistake this project was built to avoid.

**The Grafana board is structural, not a toggle.** A fifth agent publishes the finished
work order to Grafana Cloud through the official `mcp-grafana` server. With no service
account token it is not in the graph at all and the run reports the step as skipped with
its reason and a null publish payload; with a token it is in the graph and is not
optional. `tests/test_grafana_is_optional.py` asserts both graph shapes, asserts the
ClickHouse crew is identical either way, and asserts `/api/agents` still reports
`ready: true` on ClickHouse readiness alone.

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

## Design decisions

**The scan window is 120 seconds per title, and every figure says so.** A full 90-minute
scan is roughly 6 minutes of ffmpeg per title. The window is stated in the interface and
in every projection, and no number is presented as a full-length measurement. A title can
normalise cleanly across an opening reel and drift later; the window catches most
delivery failures and does not claim to be a broadcast-grade full scan.

**Black frames are detected and never auto-repaired.** A reel change, a fade and physical
damage look identical to a machine at the event-row level. Redslip stamps each event with
the loudness at its onset and sends the ambiguous ones to a person, because that
judgement is a person's.

**A missing loudness sample is reported as unmeasured, not as zero.** An `ASOF LEFT JOIN`
that matches nothing fills a `Float32` with its type default of `0.0`, which in this
column reads as digital full scale. The `toNullable` in the join makes a miss arrive as
null, and a null is rendered as unmeasured.

**Redslip measures, ranks and dispatches. It does not repair.** The loudnorm pass and the
cue retiming are separate tooling, and the slip says which lane a title belongs in rather
than pretending the repair already happened.

**The 28,423-title figure is a projection**, labelled as one everywhere it appears. 20
titles are measured.

**Subtitle checks are exercised by tests rather than by this catalog.** The Netflix TTSS
reading-speed, minimum-cue and line-length checks run over the full subtitle track when a
title ships one. None of the 20 titles in the current scan carries an SRT, so those
checks contribute zero findings to the numbers above, and the totals are not padded with
them.

---

## Run it

### Prerequisites

- Python 3.13 or newer, `uv`, `ffmpeg` 7 or newer on PATH
- ClickHouse, either ClickHouse Cloud or a local server on 8123
- The official MCP server as an isolated tool: `uv tool install mcp-clickhouse`

`mcp-clickhouse` is installed as a tool rather than as a project dependency on purpose.
It pulls `fastmcp`, which needs the MCP SDK 2.x, while ADK's `McpToolset` is built
against 1.x. An MCP server is a separate process by design, so giving it a separate
environment removes the conflict instead of pinning around it.

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
ADK crew, which takes roughly 70 to 120 seconds and streams each agent's SQL as the agent
composes it.

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
| `GRAFANA_SERVICE_ACCOUNT_TOKEN` | none | Adds the board agent to the crew |

Secrets belong in `.env`, which is gitignored. `scripts/cloudenv.sh` reads the deployed
service's own environment into your shell for local work, so no credential is ever
written to disk.

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

The suite tells you whether it exercised ClickHouse, because a tally alone cannot. On a
clean checkout with no credentials it reports `91 passed, 32 skipped in 2.40s` and prints
a red banner saying the ClickHouse integration was not exercised: every one of those
skips is a test that reads the measured catalog. Pointed at the catalog it reports `117
passed, 6 skipped in 137.55s` and a green line naming the host it read. A run that names
a remote host and cannot reach it errors rather than skipping, because that is a broken
configuration rather than an absent one.

```bash
source scripts/cloudenv.sh   # do not pipe it, a subshell discards the exports
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
the catalog question: of these titles, which do I touch this week, in what order, and
which ones need a person. That is a question about a corpus of measurements, which is why
the answer lives in a column store and not in a per-file report.

---

## Licence

MIT. See `LICENSE`.
