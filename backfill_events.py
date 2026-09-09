#!/usr/bin/env python3
"""Populate vault.events for titles already measured, from the cached media.

The findings table has always counted defects. It has never held the occurrences,
so there was nothing for the point-in-time join to join to. This re-runs
blackdetect / freezedetect / silencedetect over the media already on disk in
data/, and re-reads the subtitle track where one exists, and writes one row per
occurrence.

It does not download and does not re-measure loudness, so it costs seconds per
title and cannot change any number already on a slip.

    source scripts/cloudenv.sh
    uv run python backfill_events.py
    uv run python backfill_events.py --rebuild   # discard the table first

`--rebuild` exists because of a real failure, not for convenience. An earlier version of
this script deleted a title's previous events before inserting the new ones. A
lightweight delete is recorded as an `UPDATE _row_exists = 0 WHERE title_id = ...`
mutation, and a mutation applies to every part whose data version is below it, including
parts written afterwards. Once a handful of those had accumulated, every subsequent
insert for those title_ids was masked the moment it landed: the insert reported success
and the row was never readable. The table is fully rebuildable from cached media in
about two minutes, so the way out is to drop it and lose the mutation history with it.
Nothing deletes from this table any more.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))

from qc import archive, measure, store  # noqa: E402

WORKDIR = Path(__file__).parent / "data"
SCAN_SECONDS = 120


def main() -> None:
    parser = argparse.ArgumentParser(description="Redslip defect-event backfill")
    parser.add_argument(
        "--rebuild", action="store_true",
        help="drop and recreate vault.events first, discarding any mutation history",
    )
    args = parser.parse_args()

    ch = store.client()
    if args.rebuild:
        before = ch.query("SELECT count() FROM vault.events").result_rows[0][0]
        ch.command("DROP TABLE IF EXISTS vault.events SYNC")
        store.apply_schema(ch)
        after = ch.query("SELECT count() FROM vault.events").result_rows[0][0]
        if after:
            raise RuntimeError(f"vault.events still holds {after} rows after the drop")
        log.info("rebuilt vault.events, discarded %d rows and all mutation history", before)

    measured = {
        row[0] for row in ch.query("SELECT DISTINCT title_id FROM vault.findings").result_rows
    }
    log.info("%d titles already measured", len(measured))

    total = 0
    for title_id in sorted(measured):
        source = WORKDIR / title_id / "source.mp4"
        if not source.exists():
            log.warning("%s: no cached media, skipping", title_id)
            continue

        events = measure.structural_events(
            measure.measure_structural(source, seconds=SCAN_SECONDS)
        )

        try:
            picked = archive.pick_files(title_id)
            if picked.get("video"):
                store.store_source(
                    title_id,
                    f"https://archive.org/download/{title_id}/{picked['video']}",
                    SCAN_SECONDS,
                    ch=ch,
                )
            if picked.get("subtitle"):
                events += measure.subtitle_events(
                    archive.fetch_text(title_id, picked["subtitle"])
                )
        except Exception as exc:
            log.info("%s: archive metadata unavailable (%s)", title_id, str(exc)[:80])

        n = store.store_events(title_id, events, ch=ch)
        total += n
        kinds = sorted({kind for kind, *_ in events})
        log.info("%-44s %3d events %s", title_id, n, ",".join(kinds) or "none")

    rows = ch.query("SELECT count(), uniqExact(title_id) FROM vault.events").result_rows[0]
    log.info("vault.events now holds %d rows across %d titles (this run wrote %d)",
             rows[0], rows[1], total)


if __name__ == "__main__":
    main()
