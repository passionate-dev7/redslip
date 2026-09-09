# Redslip: Devpost submission copy

Track: ClickHouse. Agentic Cinema: The Blockbuster Hackathon.

---

## Project name

Redslip

---

## Elevator pitch

*(200 character limit, this is 195)*

> Redslip ranks a whole film archive by which titles would fail delivery today, worst first. A Google ADK crew reads ClickHouse through the official mcp-clickhouse server and writes the work order.

---

## Inspiration

A film archive with a few hundred titles has one question every Monday, and it is not "is this file good". It is "which of these would a streamer reject today, which one do I touch first, and which ones can a machine fix without booking mix-stage time". That is a question about a corpus of measurements, not about a file. Telestream Vantage, Venera Pulsar and Interra Baton all answer per file, and a stack of per-file reports does not rank a catalog.

Measuring is not the hard part. ffmpeg's `ebur128` filter has been able to tell you a master sits 14 LU off the EBU R128 target for years, and it will do it for free. The hard part is holding a catalog of those measurements in a shape where the ordering question stays cheap while the forensic question stays possible, because those two want opposite storage. Ordering wants one small row per title. Forensics wants every 100ms reading ffmpeg ever emitted. Redslip exists because that tension is exactly what a column store is for.

---

## What it does

Point Redslip at an archive and it returns a red slip per title: the failed check, the measured number against the spec it failed, the timecode to listen to, and the bay the title goes to. BATCH means a flat offset that gain alone can correct. HUMAN means someone has to listen. READY means the title passes.

The current scan covers 20 public-domain titles pulled from archive.org and measured live: 24,017 rows in `vault.loudness_samples`, 100 spec findings in `vault.findings`, and 29 defect events across 15 titles. Seventeen of the twenty would be rejected today. Three pass, which matters more than it looks, because a gate that fails everything is not measuring anything.

The ledger is ordered by absolute distance from the -23.0 LUFS EBU R128 target, measured in LU, not by raw LUFS. Sorting on raw LUFS drops every too-loud title at one end of the list and every too-quiet title at the other, and that is not a severity order. Fugitive Valley leads at -37.2 LUFS, 14.2 LU out of spec, and it stays at the top whether the next title is loud or quiet.

Opening a single title switches from ranking to forensics. Each defect event is stamped with the short-term loudness at the instant it began, using an `ASOF LEFT JOIN` from `vault.events` into the 100ms sample stream. Black picture during silence is a reel change and nobody needs to care. Black picture with programme audio still running underneath is damage, and that one wants a person. Same two rows in the same table, opposite dispositions, and only the point-in-time join separates them.

The first screen is a printed slip rather than a dashboard. Left column is a newspaper-style list of titles, worst delta first, failures set in red ink. Right is the slip itself, typeset as a letter, already filled in on load with the worst failing title so nobody has to click anything to see what the product does. Next to each measurement the interface prints the exact ffmpeg command that produced it, generated from the source URL recorded in `vault.sources`, so a stranger with ffmpeg and no access to our machine can reproduce any number on the page.

### Honest limits, stated up front

- Every figure comes from a 120-second window per title, not the full feature. A title can normalise across its opening reel and drift later. The interface says the window in words rather than implying a full-length scan.
- None of the 20 titles in this catalog ships an SRT, so the Netflix TTSS subtitle path is exercised by tests only and contributes zero findings to the numbers above.
- The 28,423-title figure below is a projection from row arithmetic, not a measurement.
- Nobody has run this in production. It runs on public-domain material, measured live, and that is the whole of the evidence.

---

## How we built it

**Ingestion is deterministic and holds no model.** `ingest.py` runs `ffprobe` for stream metadata, then `ffmpeg` with `ebur128` for loudness, then `blackdetect`, `freezedetect` and `silencedetect` for structural defects, then parses any SRT against Netflix TTSS limits. Spec verdicts land in `vault.findings`, defect occurrences in `vault.events`, and the raw loudness series in `vault.loudness_samples`. No model touches any of it, because a measurement that a model can influence is not a measurement.

**The schema is laid out for the shape of the data.** `Gorilla` followed by `ZSTD(3)` on the loudness floats, since Gorilla is the XOR-of-successive-values codec built for a slowly varying float series. `DoubleDelta` on the scan clock. `LowCardinality(String)` on `title_id`. `PARTITION BY cityHash64(title_id) % 8`, so re-scanning one title after a repair is a partition drop instead of a mutation, which is what a re-delivery actually is.

**Ranking never reads the sample stream.** `vault.title_loudness` is an `AggregatingMergeTree` keyed on `title_id`, kept current by a materialized view whose `GROUP BY` matches the rollup's `ORDER BY`, so ingesting title 21 merges one part in rather than rebuilding the fleet. `vault.fleet` reads that rollup, one row per title, joined to the small findings table. We did not want to claim this, we wanted it checkable, so `EXPLAIN indexes = 1` on the ranking query is asserted to name `vault.findings` and `vault.title_loudness` and never `vault.loudness_samples`. A companion test proves `EXPLAIN` does name the samples table when a query genuinely reads it, which is the part that makes the first assertion able to fail.

**The crew is Google ADK, and the graph is the argument.** `SequentialAgent(fleet_scout, ParallelAgent(loudness_analyst, structural_analyst), work_allocator)`. The scout ranks the catalog off the rollup and decides how deep the queue cuts. The two analysts run in parallel because they read different tables and neither needs the other's answer, which is the only pair in the graph where that is true: one reads the loudness spread to decide whether gain alone can rescue a master, the other reads events ASOF-joined to loudness to decide whether black picture is damage. The allocator holds the ordering and the bay assignment and **holds no database tools at all**, by design, so it can only quote its colleagues rather than inventing a figure.

**ClickHouse is reached only through the official MCP server.** Three of the four agents hold `mcp-clickhouse` through ADK's `McpToolset` over stdio. They compose their own SQL; nothing in the agent layer runs a stored query string. `/api/agents` on the live service reports that exact shape with `ready: true` and the resolved path to the MCP server binary. Bulk insertion of the 100ms samples stays on `clickhouse-connect`, because `run_query` is not an insert path and pretending otherwise would be worse engineering dressed up as better compliance.

**The agents cannot write, and we did not take that on trust.** There are two independent gates and they fail in different places. The client-side one is a `before_tool_callback` that inspects each composed statement and refuses anything not read-only, stripping comments first, so a statement that opens `SELECT 1`, hides the rest of the line behind a SQL line comment, then puts `DROP TABLE x` after a newline cannot walk through a prefix check. Our own callback passing its own tests proves very little, though, so we went and checked the other end: we sent `TRUNCATE TABLE vault.events` through a live MCP session and the server refused it, `isError=True, Code 164 READONLY`, with all 29 rows still there afterwards. That refusal came from ClickHouse, not from our code, which means the model is holding a connection that physically cannot mutate anything.

**Verification is a Python equality check, not a second model.** `verify_against_cells` compares every figure in the allocator's output against the cells MCP actually returned. Each run reports `verified` and lists any unsupported figure. Paying a second Gemini to audit the first would be theatre; an equality test is what the question actually is.

**Gemini is not removable.** With no credentials the crew raises `GeminiRequired` and produces nothing at all. There is no canned plan sitting behind it, because a deterministic substitute emitting the same shape is the definition of a decorative model.

The web layer is FastAPI streaming each composed statement as server-sent events, deployed on Cloud Run against ClickHouse Cloud. The visual system is derived from a Wired-style token set: white stock, black ink, zero corner radius, hairline rules instead of cards, Newsreader for display, and mono reserved for LUFS values only.

One further agent exists and is genuinely optional: a `grafana_board` step that publishes the finished work order to Grafana through the official `mcp-grafana` server, built only when a service account token is configured. Without that token it is never constructed and never appended, so the graph stays the four-agent ClickHouse crew, `/api/agents` reports `ready: true` on ClickHouse readiness alone, and the board returns `{ran: false, skipped: true, publish: null}`, which is exactly how the deployed demo runs.

---

## Challenges we ran into

**Two MCP SDKs that could not share a room.** ADK's `McpToolset` is built against `mcp<2`. The official `mcp-clickhouse` package pulls `fastmcp`, which requires `mcp>=2`. There is no pin that satisfies both, and we spent a while trying to find one before realising the premise was wrong. An MCP server is a separate process by design; it does not need to be importable from our environment, it only needs to exist as an executable. Installing it with `uv tool install mcp-clickhouse` gives it its own isolated environment, and the conflict stops existing rather than getting negotiated. The Dockerfile does the same thing at `/usr/local/bin/mcp-clickhouse`, which is the path `/api/agents` reports.

**Twenty-nine rows written, thirteen readable.** Re-scanning a title deleted its previous events before inserting the new ones. ClickHouse records that delete as a mutation, and the mutation went on applying to parts written after it, so events inserted seconds later were silently eaten. The insert returned clean every time. `system.mutations` is where we finally found it. The fix was to stop deleting: events are append-only now, with a view that exposes only the newest scan per title, and any write we intend to report on gets read back with sequential consistency before we call it stored. That lesson cost an afternoon and it generalises well past ClickHouse. An accepted insert is not a stored row until you have read it back.

**Ordering that looked right and was wrong.** The first ledger sorted on raw LUFS, which produced a list that looked plausible and ranked a title 1 LU too quiet above one 7 LU too loud. Switching to absolute distance from target in LU changed which titles a QC lead would touch first, which is the entire output of the product.

---

## Accomplishments that we're proud of

The ClickHouse access pattern is the thing, and it is checkable rather than asserted. `EXPLAIN indexes = 1` naming the rollup and not the sample stream is a claim a judge can run, and the companion test that proves `EXPLAIN` does name the samples table when a query reads it is what stops the first test from being a check that cannot fail.

The read-only guarantee is enforced by ClickHouse itself rather than by our callback alone. A `TRUNCATE` sent through a live MCP session came back `Code 164 READONLY`, and the 29 rows survived it.

One number was confirmed by a route that shares nothing with the pipeline: an independent ffmpeg run against the public source measured Night Tide at `I: -17.4 LUFS`, matching the stored finding exactly.

The suite runs 96 passed and 6 skipped, and six of those checks were each broken deliberately, confirmed red, restored, and confirmed green. Those six cover re-deriving the catalog rollup value from the raw samples, narrowing the SQL gate, stubbing the verifier, and swapping `ParallelAgent` for `SequentialAgent` to prove the topology test notices. The catalog-agreement check refuses to pass on an empty catalog, because an empty catalog agrees with itself and proves nothing.

Two full crew runs over HTTP finished in 82.5 and 118.6 seconds, both reporting `verified=True` with `unsupported_figures=[]`, each writing its decisions into `vault.slips` and reading them back. The spread between those two runs is Gemini latency rather than query time, which is worth knowing before you press the button.

---

## What we learned

An API that returns without an error has accepted your request, and that is all it has done. Thirteen readable rows out of twenty-nine written taught that faster than any amount of reading about mutations would have.

Giving an agent fewer tools made it more trustworthy. The allocator, which produces the actual output, is the one agent in the graph with no database access, so the worst it can do is misquote colleagues we can check it against.

Deciding not to build the obvious agent was the better call twice over. A critic agent auditing the allocator is a five-line equality test wearing a costume, and a second model would have cost latency and added a new thing that can hallucinate.

The version conflict was the most useful mistake, because pinning was never going to work and the process boundary was sitting right there the whole time.

---

## What's next

Full-length scans instead of the 120-second window, which is roughly six minutes of ffmpeg per title, and a partition-per-title layout that already makes re-scanning cheap. A catalog that actually ships SRTs, so the TTSS path earns findings instead of only tests. Scanning all 28,423 public-domain titles at full length would put roughly 27 million rows in `vault.loudness_samples`, which is a projection from the 100ms emission rate and stays labelled as one until somebody runs it.

After that, the part we deliberately did not build: handing a BATCH-bay title to a `loudnorm` pass and re-measuring to prove the delta. Redslip measures, ranks and dispatches. It does not repair, and the slip says so.

The gap that matters most is a real one. This has never run against a distributor's own catalog, and until it does, everything above is evidence that the code works on public-domain material.

---

## Built with

Google ADK (`google-adk`), Gemini 2.5 Flash, Google Cloud Run, ClickHouse Cloud, the official `mcp-clickhouse` MCP server over stdio via ADK `McpToolset`, `clickhouse-connect` for bulk sample ingestion, ffmpeg (`ebur128`, `blackdetect`, `freezedetect`, `silencedetect`) and ffprobe, Python 3.13, `uv`, FastAPI with server-sent events, pytest, Docker, archive.org as the source catalog, and vanilla HTML and CSS on a Wired-derived token set with Newsreader and JetBrains Mono.

---

## Try it yourself

**Live:** https://vault-387894104564.us-central1.run.app

The ledger renders immediately from a deterministic query, so there is nothing to wait for on load. `GET /api/agents` returns the ADK topology the service actually runs. Pressing triage runs the crew and streams each agent's SQL as it is composed, between roughly 80 and 120 seconds end to end over HTTP. The spread is Gemini latency rather than query time, so a slow run is the model thinking, not ClickHouse working.

**Repo:** https://github.com/passionate-dev7/vault (MIT licence, `LICENSE` in the root)

**Demo video:** https://youtube.com/@kamal

> **TODO REPLACE. This is a placeholder and must not be submitted.** Swap in the public 3-minute demo video URL before filling the Devpost form.

**Reproduce a number without cloning anything:**

```bash
ffmpeg -hide_banner -nostats -t 120 \
  -i "https://archive.org/download/NightTide16x9CorrectedAudio/NightTide_512kb.mp4" \
  -af ebur128 -f null -
```

That prints `I: -17.4 LUFS`, which is 5.6 LU from the EBU R128 target of -23.0, and it is the same figure the slip for that title carries.

**Run it locally:**

```bash
git clone https://github.com/passionate-dev7/vault
cd vault
uv sync
uv tool install mcp-clickhouse   # isolated on purpose, see Challenges
uv run python migrate.py
uv run python ingest.py --limit 20
bash run_web.sh
```
