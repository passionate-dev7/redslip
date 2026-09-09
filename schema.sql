-- Redslip schema. Catalog-scale archive delivery triage.
--
-- The shape of the problem: ffmpeg's ebur128 filter emits a loudness reading
-- every 100ms, so the measured timeline is the expensive object and the verdict
-- is the cheap one. A 90-minute feature is ~54,000 sample rows. The catalog is
-- 28,423 public-domain titles on archive.org.
--
-- The design rule that follows from that, and the one thing to check when
-- reading this file: the agent NEVER scans loudness_samples to decide which
-- titles to touch. Ranking reads vault.fleet, which is one row per title,
-- maintained incrementally by a materialized view into an AggregatingMergeTree.
-- The raw 100ms stream is only read for the single title an operator opens, and
-- then only through an ASOF JOIN that aligns a defect event with the loudness at
-- the instant it began.

CREATE DATABASE IF NOT EXISTS vault;

-- ---------------------------------------------------------------------------
-- 1. The expensive table: per-100ms loudness telemetry.
--
-- Gorilla is the XOR-of-successive-floats codec built for exactly this: a
-- slowly-varying float time series. ZSTD(3) after it. DoubleDelta on the
-- monotonic scan clock. LowCardinality on title_id because a catalog has
-- thousands of distinct titles against tens of millions of rows.
--
-- Partitioning by a hash of title_id rather than by month means one title can be
-- dropped or re-scanned with a partition DROP instead of a mutation, which is
-- what a re-delivery actually looks like.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vault.loudness_samples
(
    title_id   LowCardinality(String),
    scanned_at DateTime DEFAULT now() CODEC(DoubleDelta, ZSTD(3)),
    t_seconds  Float32 CODEC(Gorilla, ZSTD(3)),   -- offset into the scan window
    momentary  Float32 CODEC(Gorilla, ZSTD(3)),   -- M: 400ms window
    short_term Float32 CODEC(Gorilla, ZSTD(3)),   -- S: 3s window
    integrated Float32 CODEC(Gorilla, ZSTD(3)),   -- I: cumulative, gated
    true_peak  Float32 CODEC(Gorilla, ZSTD(3))    -- FTPK, dBFS
)
ENGINE = MergeTree
ORDER BY (title_id, t_seconds)
PARTITION BY cityHash64(title_id) % 8;

-- ---------------------------------------------------------------------------
-- 2. One row per spec check per title scan. Small, and the source of every
--    verdict. `measured` here is ffmpeg's own gated integrated loudness from the
--    ebur128 summary block, not an average of the sample stream.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vault.findings
(
    title_id     String,
    title        String,
    scanned_at   DateTime DEFAULT now(),
    check_name   LowCardinality(String),
    spec         LowCardinality(String),
    measured     Nullable(Float64),
    target       Nullable(Float64),
    unit         LowCardinality(String),
    passed       UInt8,
    auto_fixable UInt8,
    rescue_cost  String,
    detail       String
)
ENGINE = MergeTree
ORDER BY (title_id, check_name, scanned_at);

-- ---------------------------------------------------------------------------
-- 3. Per-event defects, one row per occurrence rather than one row per count.
--    A black segment at 00:41 is a different object from "3 black segments".
--    This is the table the drill query ASOF JOINs against the loudness stream.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vault.events
(
    title_id   LowCardinality(String),
    scanned_at DateTime DEFAULT now(),
    kind       LowCardinality(String),   -- black | freeze | silence | subtitle_speed | subtitle_short | subtitle_long_line
    start_seconds Float32 CODEC(Gorilla, ZSTD(3)),
    end_seconds   Float32 CODEC(Gorilla, ZSTD(3)),
    detail     String CODEC(ZSTD(3))
)
ENGINE = MergeTree
ORDER BY (title_id, kind, start_seconds)
PARTITION BY cityHash64(title_id) % 8;

-- Events are append-only, like findings: a re-scan adds a generation rather than
-- replacing one. Deleting the previous scan first was the obvious approach and it was
-- wrong. A lightweight delete is a mutation, and on ClickHouse Cloud a mutation issued
-- immediately before an insert on the same predicate can still swallow the rows that
-- insert wrote. The first backfill wrote 29 events and kept 13. Rather than tune the
-- race, this drops it: nothing is ever deleted, and every reader filters to the newest
-- scan per title. Mutations are the thing to avoid in a column store, not to
-- synchronise.
CREATE OR REPLACE VIEW vault.latest_events AS
SELECT title_id, scanned_at, kind, start_seconds, end_seconds, detail
FROM vault.events
WHERE (title_id, scanned_at) IN (
    SELECT title_id, max(scanned_at) FROM vault.events GROUP BY title_id
);

-- ---------------------------------------------------------------------------
-- 3b. Where each title came from. The strongest thing this project has is that a
--     stranger with ffmpeg can reproduce any number on any slip from a public
--     file, so the URL that was measured is stored next to the measurement and
--     printed in the interface with the command that produces it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vault.sources
(
    title_id     String,
    fetched_at   DateTime DEFAULT now(),
    source_url   String,
    details_url  String,
    scan_seconds UInt32
)
ENGINE = ReplacingMergeTree(fetched_at)
ORDER BY title_id;

-- ---------------------------------------------------------------------------
-- 4. The rollup. This is the table that makes the fleet question cheap.
--
--    AggregatingMergeTree keyed on title_id, maintained incrementally by the
--    materialized view below. Ingesting a new title merges one part in; it does
--    not rebuild the fleet. argMin keeps the timestamp of the quietest sustained
--    passage, which is the timecode the operator is told to listen to.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vault.title_loudness
(
    title_id      LowCardinality(String),
    samples       SimpleAggregateFunction(sum, UInt64),
    quietest_lufs SimpleAggregateFunction(min, Float32),
    loudest_lufs  SimpleAggregateFunction(max, Float32),
    max_true_peak SimpleAggregateFunction(max, Float32),
    listen_at     AggregateFunction(argMin, Float32, Float32)
)
ENGINE = AggregatingMergeTree
ORDER BY title_id;

-- The GROUP BY here is the rollup's ORDER BY, which is the condition for the
-- view to be mergeable rather than merely correct on the day it was written.
CREATE MATERIALIZED VIEW IF NOT EXISTS vault.mv_title_loudness
TO vault.title_loudness AS
SELECT
    title_id,
    toUInt64(count())                  AS samples,
    min(short_term)                    AS quietest_lufs,
    max(short_term)                    AS loudest_lufs,
    max(true_peak)                     AS max_true_peak,
    argMinState(t_seconds, short_term) AS listen_at
FROM vault.loudness_samples
WHERE short_term > -70          -- drop uninitialised and digital-silence readings
GROUP BY title_id;

-- Readable face of the rollup. 1 row per title, no sample scan.
CREATE OR REPLACE VIEW vault.fleet_loudness AS
SELECT
    title_id,
    sum(samples)            AS samples,
    min(quietest_lufs)      AS quietest_lufs,
    max(loudest_lufs)       AS loudest_lufs,
    max(max_true_peak)      AS max_true_peak,
    argMinMerge(listen_at)  AS listen_at_seconds
FROM vault.title_loudness
GROUP BY title_id;

-- ---------------------------------------------------------------------------
-- 5. Latest verdict per title, from the small findings table.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vault.catalog_summary AS
SELECT
    title_id,
    any(title)                                        AS title,
    max(scanned_at)                                   AS last_scanned,
    countIf(failed = 1)                               AS failures,
    countIf(failed = 0)                               AS passes,
    countIf(failed = 1 AND fixable = 1)               AS auto_fixable,
    countIf(failed = 1 AND fixable = 0)               AS needs_human,
    multiIf(countIf(failed = 1) = 0, 'PASS', 'FAIL')  AS verdict,
    groupArray(if(failed = 1, check_name, ''))        AS failed_checks,
    groupArray(if(failed = 1, rescue_cost, ''))       AS rescue_costs
FROM (
    SELECT title_id, title, scanned_at, check_name, rescue_cost,
           1 - passed         AS failed,
           auto_fixable       AS fixable
    FROM vault.findings
    WHERE (title_id, scanned_at) IN (
        SELECT title_id, max(scanned_at) FROM vault.findings GROUP BY title_id
    )
)
GROUP BY title_id
ORDER BY failures DESC;

-- ---------------------------------------------------------------------------
-- 6. THE FLEET QUERY, as a view so it can be read straight through MCP.
--
--    This is the one the agent ranks on. It touches vault.title_loudness
--    (one row per title) and vault.findings (one row per check), and reads
--    exactly zero rows of vault.loudness_samples. That property is what the
--    rollup exists for, and `EXPLAIN indexes = 1` on this view is the proof.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vault.fleet AS
SELECT
    cs.title_id                                             AS title_id,
    cs.title                                                AS title,
    cs.verdict                                              AS verdict,
    cs.failures                                             AS failures,
    cs.auto_fixable                                         AS auto_fixable,
    cs.needs_human                                          AS needs_human,
    round(ints.integrated_lufs, 1)                          AS integrated_lufs,
    round(abs(ints.integrated_lufs + 23.0), 1)              AS lufs_delta,
    round(fl.quietest_lufs, 1)                              AS quietest_lufs,
    round(fl.loudest_lufs, 1)                               AS loudest_lufs,
    round(fl.listen_at_seconds, 1)                          AS listen_at_seconds,
    fl.samples                                              AS loudness_samples,
    multiIf(cs.failures = 0, 'READY',
            cs.needs_human > 0, 'HUMAN',
            'BATCH')                                        AS lane
FROM vault.catalog_summary AS cs
LEFT JOIN vault.fleet_loudness AS fl ON fl.title_id = cs.title_id
LEFT JOIN (
    SELECT title_id, argMax(measured, scanned_at) AS integrated_lufs
    FROM vault.findings
    WHERE check_name = 'integrated_loudness_ebu_r128' AND measured IS NOT NULL
    GROUP BY title_id
) AS ints ON ints.title_id = cs.title_id;

-- Kept for compatibility with the per-title drill path in web/app.py.
CREATE OR REPLACE VIEW vault.loudness_extremes AS
SELECT
    title_id,
    round(quietest_lufs, 1)   AS integrated_lufs,
    round(quietest_lufs, 1)   AS worst_short_term_lufs,
    round(loudest_lufs, 1)    AS best_short_term_lufs,
    round(max_true_peak, 1)   AS max_true_peak_dbfs,
    listen_at_seconds         AS worst_window_at_seconds,
    samples                   AS sample_count
FROM vault.fleet_loudness;

-- ---------------------------------------------------------------------------
-- 7. Writeback. The agent crew's work order lands here, so the queue survives
--    the browser tab and the next scan can be compared against the last plan.
--    Every row carries the SQL the crew ran to produce it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vault.slips
(
    run_id        String,
    issued_at     DateTime DEFAULT now(),
    title_id      LowCardinality(String),
    title         String,
    rank          UInt16,
    lane          LowCardinality(String),   -- BATCH | HUMAN | READY
    integrated_lufs Nullable(Float32),
    lufs_delta      Nullable(Float32),
    failures      UInt16,
    rescue_cost   String,
    rationale     String,
    model         LowCardinality(String),
    verified      UInt8                     -- every cited figure matched a ClickHouse cell
)
ENGINE = MergeTree
ORDER BY (run_id, rank);

-- Latest work order only.
CREATE OR REPLACE VIEW vault.latest_slips AS
SELECT *
FROM vault.slips
WHERE run_id = (SELECT argMax(run_id, issued_at) FROM vault.slips)
ORDER BY rank;
