#!/usr/bin/env python3
"""Catalog ingest: scan 15-30 archive.org titles, store results in ClickHouse.

HONEST CONSTRAINT: Each title is bounded to the first 2 minutes (120 seconds)
of content. This is stated explicitly in the UI. A 90-minute feature would take
~6 minutes to scan; bounding to 2 minutes makes the live catalog feasible while
keeping the measurements representative (loudness integrates over the window;
loudness failures are consistent through a film).

Scale argument for ClickHouse:
  ebur128 emits about 10 rows per second of content, so 2 minutes is ~1,200 rows
  per title. Ranking never reads them: vault.title_loudness is an
  AggregatingMergeTree kept current by a materialized view, and vault.fleet reads
  one row per title off it. Scanning the full 28,423-title collection in full
  would put roughly 27 million rows in the sample table, which is the projection
  the interface labels as a projection.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Ensure local qc/ is importable
sys.path.insert(0, str(Path(__file__).parent))

from qc import archive, measure, store

# archive.org identifiers, verified by hand to hold a playable video derivative.
# Measured values are not recorded here on purpose: the database is the record, and a
# comment claiming a LUFS figure goes stale the first time a title is re-scanned.
SEED_TITLES = [
    "vicki-1953",
    "what_becomes_of_the_children",
    "WerewolfInAGirlsDormitory",
    "NightOfTheLivingDead720p1968",
    "night-of-the-living-dead-1968_202312",
    "quevadis",
    "rough_riding_ranger",
    "romance_on_the_run",
    "the_shadow_strikes",
    "song_for_miss_julie",
    "framed_760",
    "NightTide16x9CorrectedAudio",
    "fugitive_valley",
    "CrimsonRomance",
    "follow_your_heart",
    "farewell_to_arms",
    "Niagara-Falls_1941",
    "successful_failure",
    "OutpostInMorocco",
    "fitforaking",
    "NothingButTheTruth1929",
    "RomanceOfTheRedwoods",
    "sabaka_ipod",
    "the_big_road",
    "Police_Station_1959",
    "Notorious_But_Nice",
    "TheMemphisBelleAStoryofaFlyingFortress",
]

# How many bytes to fetch per title. ~20 MB covers ~2 minutes of typical 480p video.
MAX_BYTES    = 20_000_000
SCAN_SECONDS = 120   # measure only the first 2 minutes

WORKDIR = Path(__file__).parent / "data"


def scan_title(identifier: str, ch) -> dict:
    """Download, measure, store one title. Returns a status dict."""
    log.info("=== %s ===", identifier)
    workdir = WORKDIR / identifier
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        picked = archive.pick_files(identifier)
    except Exception as exc:
        log.warning("%s: metadata fetch failed: %s", identifier, exc)
        return {"identifier": identifier, "status": "metadata_error", "detail": str(exc)}

    if not picked["video"]:
        log.warning("%s: no video file", identifier)
        return {"identifier": identifier, "status": "no_video"}

    title = picked["title"]
    log.info("  title: %s | video: %s", title, picked["video"])

    # Download first MAX_BYTES
    dest = workdir / "source.mp4"
    if not dest.exists():
        try:
            archive.fetch(identifier, picked["video"], dest, max_bytes=MAX_BYTES)
            log.info("  downloaded %d bytes", dest.stat().st_size)
        except Exception as exc:
            log.warning("%s: download failed: %s", identifier, exc)
            return {"identifier": identifier, "title": title, "status": "download_error", "detail": str(exc)}
    else:
        log.info("  cached (%d bytes)", dest.stat().st_size)

    # Record exactly which public file was measured, so any number on a slip can be
    # re-run by a stranger with ffmpeg and no access to this machine.
    try:
        store.store_source(
            identifier,
            f"https://archive.org/download/{identifier}/{picked['video']}",
            SCAN_SECONDS,
            ch=ch,
        )
    except Exception as exc:
        log.warning("%s: source store failed: %s", identifier, exc)

    # Fetch subtitle
    subs = None
    if picked["subtitle"]:
        try:
            subs = archive.fetch_text(identifier, picked["subtitle"])
            log.info("  subtitle: %d chars", len(subs))
        except Exception as exc:
            log.warning("%s: subtitle fetch failed: %s", identifier, exc)

    # QC measurement
    try:
        report = measure.run_qc(dest, subtitle_text=subs, seconds=SCAN_SECONDS)
    except Exception as exc:
        log.warning("%s: QC failed: %s", identifier, exc)
        return {"identifier": identifier, "title": title, "status": "qc_error", "detail": str(exc)}

    # Store findings
    try:
        store.store_findings(identifier, title, report, ch=ch)
    except Exception as exc:
        log.warning("%s: findings store failed: %s", identifier, exc)

    # Per-occurrence defect rows, for the point-in-time join on the drill path.
    try:
        events = measure.structural_events(measure.measure_structural(dest, seconds=SCAN_SECONDS))
        if subs:
            events += measure.subtitle_events(subs)
        n_events = store.store_events(identifier, events, ch=ch)
        log.info("  %d defect events stored", n_events)
    except Exception as exc:
        log.warning("%s: event store failed: %s", identifier, exc)

    # Parse + store loudness time series
    try:
        samples = store.loudness_timeseries(dest, seconds=SCAN_SECONDS)
        n_samples = store.store_samples(identifier, samples, ch=ch)
        log.info("  %d loudness samples stored", n_samples)
    except Exception as exc:
        log.warning("%s: loudness store failed: %s", identifier, exc)
        n_samples = 0

    # Summary
    fails = len(report.failures)
    log.info("  %d/%d checks failed | %d samples", fails, len(report.findings), n_samples)
    for f in report.failures:
        log.info("    FAIL %s: %s %s (target %s %s)",
                 f.check, f.measured, f.unit, f.target, f.unit)

    return {
        "identifier": identifier,
        "title": title,
        "status": "ok",
        "failures": fails,
        "findings": len(report.findings),
        "samples": n_samples,
        "verdict": "FAIL" if fails else "PASS",
    }


def main():
    parser = argparse.ArgumentParser(description="Redslip catalog ingest")
    parser.add_argument("--titles", nargs="*", help="Override title list")
    parser.add_argument("--limit", type=int, default=len(SEED_TITLES))
    parser.add_argument("--from-search", action="store_true",
                        help="Fetch N titles from archive.org search (adds variety)")
    args = parser.parse_args()

    ch = store.client()
    store.apply_schema(ch)
    log.info("Schema applied. ClickHouse version: %s",
             ch.query("SELECT version()").result_rows[0][0])

    titles = args.titles or SEED_TITLES[: args.limit]

    if args.from_search:
        extra = [d["identifier"] for d in archive.search(rows=args.limit)]
        titles = list(dict.fromkeys(titles + extra))[: args.limit]
        log.info("Using %d titles from search", len(titles))

    results = []
    t0 = time.time()
    for i, tid in enumerate(titles, 1):
        log.info("[%d/%d] %s", i, len(titles), tid)
        result = scan_title(tid, ch)
        results.append(result)

    elapsed = time.time() - t0
    ok   = sum(1 for r in results if r["status"] == "ok")
    fail = sum(1 for r in results if r.get("verdict") == "FAIL")

    # Verify non-empty (REQUIRED: an empty catalog is not a passing state)
    total_rows = store.row_count("vault.loudness_samples", ch=ch)
    finding_rows = store.row_count("vault.findings", ch=ch)
    assert total_rows > 0, "loudness_samples is empty: something went wrong"
    assert finding_rows > 0, "findings is empty: something went wrong"

    log.info("=" * 60)
    log.info("Ingest complete in %.0fs", elapsed)
    log.info("  %d titles processed, %d ok, %d scanned as FAIL", len(results), ok, fail)
    log.info("  vault.loudness_samples: %d rows", total_rows)
    log.info("  vault.findings:         %d rows", finding_rows)
    log.info("  ~%.0f rows per title (loudness)",
             total_rows / max(ok, 1))


if __name__ == "__main__":
    main()
