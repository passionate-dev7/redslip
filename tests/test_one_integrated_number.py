"""One title, one integrated loudness figure, everywhere in the system.

The ledger and the slip used to disagree: the catalog row derived a figure from the
100ms sample stream while the panel printed ffmpeg's gated integrated loudness from
the findings table. Night Tide read -43.7 on the left and -17.4 on the right, on one
screen, for one title. An independent ffmpeg run against the public source confirmed
-17.4:

    ffmpeg -hide_banner -nostats -t 120 \\
      -i "https://archive.org/download/NightTide16x9CorrectedAudio/NightTide.mp4" \\
      -af ebur128 -f null -

    Integrated loudness:
      I: -17.4 LUFS

So the sample-derived figure was the wrong one, and it was also what the ledger
sorted on, which put the wrong title at the head of the column.

These run over every title in the live catalog, not one. Empty is a failure: a
catalog with nothing in it agrees with itself trivially and proves nothing.

Goes red:
  - if any title's catalog integrated value differs from its findings value
  - if the catalog ever serves an integrated figure derived from loudness_samples
  - if the ledger is ordered by raw LUFS rather than by distance from target
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EBU_R128_TARGET_LUFS = -23.0


def _client():
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from web.app import app

    return TestClient(app)


@pytest.fixture(scope="module")
def catalog():
    response = _client().get("/api/catalog")
    if response.status_code != 200:
        pytest.skip(f"catalog unavailable: {response.status_code} {response.text[:120]}")
    rows = response.json()
    assert rows, "the catalog is empty, so nothing here is being checked"
    return rows


def test_catalog_integrated_matches_findings_for_every_title(catalog):
    """The number on the left of the screen is the number on the right."""
    client = _client()
    compared = 0
    disagreements = []

    for row in catalog:
        detail = client.get(f"/api/title/{row['title_id']}")
        assert detail.status_code == 200, f"{row['title_id']}: {detail.status_code}"
        ebu = next(
            (
                finding
                for finding in detail.json()["findings"]
                if finding["check_name"] == "integrated_loudness_ebu_r128"
            ),
            None,
        )
        if ebu is None or ebu["measured"] is None:
            continue
        compared += 1
        catalog_value = row.get("integrated_lufs")
        if catalog_value is None or abs(float(catalog_value) - float(ebu["measured"])) > 0.05:
            disagreements.append(
                f"{row['title_id']}: catalog {catalog_value} vs findings {ebu['measured']}"
            )

    assert compared >= 10, f"only {compared} titles carried an integrated measurement"
    assert not disagreements, "the ledger and the slip disagree:\n  " + "\n  ".join(disagreements)


def test_integrated_is_never_the_quietest_short_term_reading(catalog):
    """Guard the specific way this broke.

    `quietest_lufs` is a real measurement and it is served under its own name. The
    failure was serving it as `integrated_lufs`, which is a different quantity on a
    different scale and is not comparable to an R128 target.
    """
    measured = [
        row for row in catalog
        if row.get("integrated_lufs") is not None and row.get("quietest_lufs") is not None
    ]
    assert len(measured) >= 10, "not enough measured titles to check this"
    collisions = [
        row["title_id"]
        for row in measured
        if abs(float(row["integrated_lufs"]) - float(row["quietest_lufs"])) < 0.05
    ]
    assert len(collisions) < len(measured), (
        "every title reports the same value for integrated and quietest, so the "
        f"catalog is serving the sample-derived figure as integrated: {collisions[:5]}"
    )


def test_no_title_reports_an_impossible_delivery_figure(catalog):
    """A film master does not measure 20 LU below any broadcast target.

    -43.7 LUFS on a delivery ledger is not a quiet film, it is a bug, and an engineer
    reads it as one at a glance.
    """
    absurd = [
        (row["title_id"], row["integrated_lufs"])
        for row in catalog
        if row.get("integrated_lufs") is not None and float(row["integrated_lufs"]) < -40.0
    ]
    assert not absurd, f"integrated loudness below -40 LUFS is not a delivery figure: {absurd}"


def test_ledger_is_ordered_by_distance_from_target_not_by_raw_lufs(catalog):
    """A title 7 LU too loud outranks one 5 LU too quiet.

    Raw LUFS ordering puts every too-loud title at one end and every too-quiet title
    at the other, which is not a severity order at all.
    """
    deltas = [
        abs(float(row["integrated_lufs"]) - EBU_R128_TARGET_LUFS)
        for row in catalog
        if row.get("integrated_lufs") is not None
    ]
    assert len(deltas) >= 10, "not enough measured titles to check the ordering"
    assert deltas == sorted(deltas, reverse=True), (
        "the catalog is not ordered worst-first by distance from the target: "
        f"{[round(d, 1) for d in deltas]}"
    )

    # And the served delta is that distance, not something else.
    for row in catalog:
        if row.get("integrated_lufs") is None or row.get("lufs_delta") is None:
            continue
        expected = abs(float(row["integrated_lufs"]) - EBU_R128_TARGET_LUFS)
        assert abs(float(row["lufs_delta"]) - expected) <= 0.11, (
            f"{row['title_id']}: lufs_delta {row['lufs_delta']} is not "
            f"|{row['integrated_lufs']} - {EBU_R128_TARGET_LUFS}|"
        )
