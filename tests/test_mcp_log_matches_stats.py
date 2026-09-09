"""The judge-facing MCP transcript agrees with the numbers the service reports.

`/api/mcp-log` is the artefact offered as proof that the crew reaches ClickHouse
through the official MCP server, so it is the first thing a ClickHouse judge opens. If
it quotes a row count or a title count the service no longer reports, the single piece
of evidence for the integration is also the strongest argument against the submission.
Nothing about that is caught by a type checker or by a passing crew run, so it is
caught here.

Goes red:
  - if a served entry reads a table or view the shipped schema does not create
  - if a served `count()` disagrees with /api/stats
  - if a served formatReadableQuantity string disagrees with /api/stats
  - if the generation cutoff is widened back over the superseded ingest
  - if the served log stops carrying any checkable figure at all, which would leave
    this test passing for the reason that there is nothing left to check
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qc import mcp_log  # noqa: E402


@pytest.fixture(scope="module")
def stats(vault_clickhouse):
    """What /api/stats reports right now, through the endpoint the interface reads."""
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from web.app import app

    with TestClient(app) as client:
        response = client.get("/api/stats")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(scope="module")
def served():
    entries = mcp_log.served()
    assert entries, (
        "no statements are served at /api/mcp-log, so this test would assert nothing"
    )
    return entries


def test_the_served_log_carries_figures_that_can_be_checked(served, stats):
    """Guard against the vacuous pass.

    Every other assertion here iterates the claims the log makes. A log that made no
    claims would satisfy all of them, so the presence of the claims is asserted first.
    """
    checkable = [claim for entry in served for claim in mcp_log.claims(entry, stats)]
    subjects = {claim[0] for claim in checkable}
    assert any("count() FROM vault.loudness_samples" in s for s in subjects), (
        "the served log states no sample row count, so nothing here checks one:\n"
        + "\n".join(sorted(subjects))
    )
    assert any("count(DISTINCT title_id)" in s for s in subjects), (
        "the served log states no title count, so nothing here checks one:\n"
        + "\n".join(sorted(subjects))
    )
    assert any("formatReadableQuantity" in s for s in subjects), (
        "the served log states no readable quantity, which is the shape the "
        "superseded entries used:\n" + "\n".join(sorted(subjects))
    )


def test_no_served_statement_disagrees_with_the_live_catalog(served, stats):
    problems = mcp_log.disagreements(served, stats)
    assert not problems, (
        f"/api/mcp-log contradicts /api/stats in {len(problems)} places:\n"
        + "\n".join(problems)
    )


def test_the_endpoint_serves_the_same_entries_and_states_the_cutoff(stats):
    """The payload a judge reads, not just the file on disk."""
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from web.app import app

    with TestClient(app) as client:
        payload = client.get("/api/mcp-log?limit=1000").json()

    assert payload["generation_start"] == mcp_log.GENERATION_START
    assert payload["generation_reason"], "the cutoff is applied but not explained"
    assert payload["withheld_earlier_entries"] > 0, (
        "no entries are held back, so either the cutoff moved or the superseded log "
        "was deleted rather than kept"
    )
    assert not mcp_log.disagreements(payload["entries"], stats)
    for entry in payload["entries"]:
        assert mcp_log.in_current_generation(entry), entry.get("ts")


def test_the_superseded_log_is_kept_unedited_and_is_not_served():
    """The earlier era is retained as history, and it is the era this test would fail on.

    Keeping it matters twice: a transcript is only evidence if it is not curated after
    the fact, and it is the fixture that proves the rules below can fail.
    """
    earlier = mcp_log.read(mcp_log.PRE_FLEET_LOG_PATH)
    assert earlier, "the superseded transcript was deleted instead of set aside"
    assert not any(mcp_log.in_current_generation(entry) for entry in earlier)


def test_the_check_catches_the_superseded_era(stats):
    """Run the rules over the era the cutoff excludes. They must go red.

    This is the failure the cutoff exists to prevent, so it is asserted rather than
    assumed: without it, every assertion above could be passing because the rules never
    fire on anything.
    """
    problems = mcp_log.disagreements(mcp_log.read(mcp_log.PRE_FLEET_LOG_PATH), stats)
    assert problems, (
        "the pre-fleet transcript quotes a superseded catalog, and the rules did not "
        "notice, so they would not notice it coming back either"
    )
    joined = "\n".join(problems)
    # The three headline figures the earlier ingest reported, each of which the shipped
    # catalog contradicts.
    assert "34.83 thousand" in joined, joined[:2000]
    assert "count(DISTINCT title_id) FROM vault.findings" in joined, joined[:2000]
    assert "formatReadableQuantity(count()) FROM vault.findings" in joined, joined[:2000]


def test_the_check_catches_a_row_count_that_drifts(served, stats):
    """If the catalog is re-ingested and the log is not, the log must go red.

    A count that was true yesterday is the exact defect this guards, so the drift is
    simulated against the real served entries rather than against a fixture.
    """
    drifted = dict(stats, loudness_sample_rows=stats["loudness_sample_rows"] + 1)
    problems = mcp_log.disagreements(served, drifted)
    assert problems, (
        "the sample row count moved and the served log still agreed with it, so this "
        "check cannot fail"
    )
    assert any("loudness_samples" in problem for problem in problems), problems


def test_the_check_catches_a_title_count_that_drifts(served, stats):
    drifted = dict(stats, title_count=stats["title_count"] + 1)
    problems = mcp_log.disagreements(served, drifted)
    assert problems, (
        "the title count moved and the served log still agreed with it, so this check "
        "cannot fail"
    )
    assert any("title_id" in problem for problem in problems), problems


def test_the_check_catches_a_statement_against_an_object_the_schema_lost(served, stats):
    """A served entry reading something schema.sql no longer creates must go red.

    `vault.catalog_summary` and `vault.loudness_extremes` are still in the schema as
    compatibility views, so the name is not what dates an entry. This rule is for the
    case where a view really is dropped and old transcripts keep quoting it.
    """
    dropped = "catalog_rollup_v1"
    assert dropped not in mcp_log.schema_objects(), "pick a name the schema does not have"
    tampered = list(served) + [{
        "ts": "2999-01-01T00:00:00+00:00",
        "agent": "fleet_scout",
        "tool": "run_query",
        "sql": f"SELECT title, verdict FROM vault.{dropped} LIMIT 5",
        "rows": 5,
        "blocked": False,
        "result_preview": "5 rows",
    }]
    problems = mcp_log.disagreements(tampered, stats)
    assert any(dropped in problem for problem in problems), problems


def test_the_check_catches_a_listing_that_advertises_the_wrong_universe(served, stats):
    """The shape the earlier era used: a LIMIT wide enough to expose the whole catalog.

    `LIMIT 30` answering with 27 rows is a claim that 27 titles exist. The submission
    says 20, so it has to go red.
    """
    tampered = list(served) + [{
        "ts": "2999-01-01T00:00:00+00:00",
        "agent": "fleet_scout",
        "tool": "run_query",
        "sql": "SELECT title_id, title, verdict FROM vault.fleet ORDER BY title LIMIT 30",
        "blocked": False,
        "result_preview": json.dumps({
            "columns": ["title_id", "title", "verdict"],
            "rows": [[f"t{n}", f"Title {n}", "FAIL"] for n in range(27)],
        }),
    }]
    problems = mcp_log.disagreements(tampered, stats)
    assert any("27 titles" in problem for problem in problems), problems

    # And a statement that filled its LIMIT claims nothing, or every run in the log
    # would be read as a claim that the catalog is exactly twelve titles long.
    honest = list(served) + [dict(
        tampered[-1],
        sql="SELECT title_id, title, verdict FROM vault.fleet ORDER BY title LIMIT 12",
        result_preview=json.dumps({
            "columns": ["title_id", "title", "verdict"],
            "rows": [[f"t{n}", f"Title {n}", "FAIL"] for n in range(12)],
        }),
    )]
    assert not mcp_log.disagreements(honest, stats)
