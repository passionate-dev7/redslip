"""A defect with no loudness row at its onset is reported as unmeasured, not as 0.0 LUFS.

An ASOF LEFT JOIN that finds no earlier row fills the right-hand column with its type's
default. For a non-nullable Float32 that default is 0.0, and 0.0 LUFS is digital full
scale. Night Tide's first defect starts at 0.021s while the first loudness sample lands
at 0.121s, so nothing precedes it, and the page printed "silence at 00:00, 0.0 LUFS at
onset": the loudest possible reading attached to a silence event. Nobody reading that
would know it meant "we have no measurement here".

The same events also came back as zero-length spans, because the ingest captured
`silence_start` and `freeze_start` and discarded the matching `silence_end` and
`freeze_end` that ffmpeg emits a few lines later.

Goes red:
  - if a missed join reappears as 0.0 rather than null
  - if any event carries a level of exactly 0.0 dB, which no film master produces
  - if freeze or silence events are stored as zero-length spans again
  - if the fallback ever invents a level for an unmeasured onset
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qc import measure  # noqa: E402


def _client():
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from web.app import app

    return TestClient(app)


@pytest.fixture(scope="module")
def titles_with_events():
    client = _client()
    catalog = client.get("/api/catalog")
    if catalog.status_code != 200:
        pytest.skip(f"catalog unavailable: {catalog.status_code}")
    found = []
    for row in catalog.json():
        detail = client.get(f"/api/title/{row['title_id']}")
        if detail.status_code != 200:
            continue
        events = detail.json().get("events") or []
        if events:
            found.append((row["title_id"], events))
    if not found:
        pytest.skip("no title carries defect events, so nothing here is being checked")
    return found


def test_an_unmeasured_onset_is_null_not_a_level(titles_with_events):
    """The exact bug: a miss must arrive as null so the page can say so."""
    offenders = []
    for title_id, events in titles_with_events:
        for event in events:
            level = event.get("short_term_at_start")
            if level is None:
                continue
            if abs(float(level)) < 1e-9:
                offenders.append(
                    f"{title_id} {event['kind']} at {event['start_seconds']}s reports "
                    f"{level} dB at onset"
                )
    assert not offenders, (
        "0.0 dB is digital full scale, not a measurement. A missed point-in-time join "
        "is leaking the column default instead of null:\n  " + "\n  ".join(offenders)
    )


def test_the_earliest_event_in_the_catalog_is_the_one_that_used_to_break(titles_with_events):
    """Cover the case directly: an event starting before the first loudness sample.

    Without this, the assertion above could pass simply because no title happens to
    have an event that early, which would make it a check that cannot fail.
    """
    candidates = [
        (event, title_id)
        for title_id, events in titles_with_events
        for event in events
    ]
    event, title_id = min(candidates, key=lambda pair: pair[0]["start_seconds"])
    assert event["start_seconds"] < 1.0, (
        "no event in the catalog starts near the head of the scan, so the miss path is "
        f"not exercised; earliest is {title_id} at {event['start_seconds']}s"
    )
    level = event.get("short_term_at_start")
    assert level is None or abs(float(level)) > 1e-9, (
        f"{title_id} {event['kind']} at {event['start_seconds']}s reports {level} dB, "
        "which is the column default leaking through the join"
    )


def test_no_event_is_a_zero_length_span(titles_with_events):
    degenerate = [
        f"{title_id} {event['kind']} at {event['start_seconds']}s"
        for title_id, events in titles_with_events
        for event in events
        if float(event["end_seconds"]) <= float(event["start_seconds"])
    ]
    assert not degenerate, (
        "a defect with no duration tells an operator nothing about how much of the "
        "master is affected; the detector's end line is being discarded:\n  "
        + "\n  ".join(degenerate)
    )


# --- the span pairing itself, without needing a database ------------------

FFMPEG_PAIRED = """
[silencedetect @ 0x1] silence_start: 0.0213333
[silencedetect @ 0x1] silence_end: 2.763854 | silence_duration: 2.742521
[freezedetect @ 0x2] lavfi.freezedetect.freeze_start: 10.5
[freezedetect @ 0x2] lavfi.freezedetect.freeze_duration: 3.0
[freezedetect @ 0x2] lavfi.freezedetect.freeze_end: 13.5
"""

FFMPEG_UNTERMINATED = """
[silencedetect @ 0x1] silence_start: 100.25
"""


def test_a_paired_event_keeps_its_real_end():
    silence = measure._spans(
        FFMPEG_PAIRED, measure._SILENCE_START, measure._SILENCE_END, 120
    )
    assert len(silence) == 1
    assert silence[0]["start"] == pytest.approx(0.0213333)
    assert silence[0]["end"] == pytest.approx(2.763854)
    assert silence[0]["duration"] == pytest.approx(2.742521, abs=1e-4)
    assert silence[0]["truncated"] is False

    freeze = measure._spans(
        FFMPEG_PAIRED, measure._FREEZE_START, measure._FREEZE_END, 120
    )
    assert freeze[0]["start"] == 10.5 and freeze[0]["end"] == 13.5


def test_an_event_running_past_the_scan_edge_is_closed_at_the_edge_and_flagged():
    """It is kept, because dropping it would hide a defect, and it says it is partial."""
    spans = measure._spans(
        FFMPEG_UNTERMINATED, measure._SILENCE_START, measure._SILENCE_END, 120
    )
    assert len(spans) == 1
    assert spans[0]["start"] == 100.25
    assert spans[0]["end"] == 120.0, "an unterminated event must not collapse to zero length"
    assert spans[0]["truncated"] is True

    events = measure.structural_events({
        "black_segments": [],
        "freeze_events": [],
        "silence_events": spans,
        "window_seconds": 120,
    })
    assert len(events) == 1
    kind, start, end, detail = events[0]
    assert kind == "silence" and end > start
    assert "scan edge" in detail, "a partial observation must say so on the slip"
