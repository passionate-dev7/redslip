"""The Grafana board is a bonus surface and can never break the ClickHouse product.

Redslip is a ClickHouse-track entry. A fifth agent holding the official `mcp-grafana`
server is worth having, but the deployed Cloud Run service carries no Grafana token, and
for a while a missing token made `run_crew` raise before it did anything. That turned the
one interactive control in the product into an error message for exactly the judge the
entry is aimed at.

The rule now: with no token the board agent is not in the graph at all and the run says
so; with a token it is in the graph and is not optional. That is the opposite of a
toggle. The integration is load-bearing where it is claimed and absent where it is not.

Goes red:
  - if run_crew raises GrafanaRequired, or anything else, because a token is missing
  - if the ClickHouse crew is reported not ready purely because Grafana is unconfigured
  - if a skipped board step is reported in a shape that could be drawn as a completed one
  - if the board agent stays in the graph without a token, where it would fail mid-run
  - if the board agent is dropped from the graph when a token IS present
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import crew  # noqa: E402


# --- the graph ------------------------------------------------------------

def test_the_board_agent_is_absent_without_a_token():
    """Left out of the graph, not left in it to fail when it is reached."""
    names = [child.name for child in crew.build_crew(with_grafana=False).sub_agents]
    assert names == ["fleet_scout", "evidence", "work_allocator"], names
    assert "grafana_board" not in names


def test_the_board_agent_is_present_with_a_token_and_is_not_optional_then():
    names = [child.name for child in crew.build_crew(with_grafana=True).sub_agents]
    assert names == ["fleet_scout", "evidence", "work_allocator", "grafana_board"], names


def test_the_clickhouse_crew_is_identical_either_way():
    """Nothing about the ClickHouse path may change because of a Grafana token."""
    without = crew.build_crew(with_grafana=False).sub_agents
    with_board = crew.build_crew(with_grafana=True).sub_agents[:3]
    assert [a.name for a in without] == [a.name for a in with_board]
    assert [getattr(a, "output_key", None) for a in without] == [
        getattr(a, "output_key", None) for a in with_board
    ]


# --- the run --------------------------------------------------------------

class _FakeSession:
    id = "s1"
    state: dict = {}


class _FakeSessionService:
    def __init__(self, state):
        self._state = state

    async def create_session(self, **_):
        return _FakeSession()

    async def get_session(self, **_):
        session = _FakeSession()
        session.state = self._state
        return session


class _FakeRunner:
    """Stands in for InMemoryRunner so the degraded path is checked without a model.

    It answers the four things run_crew uses. The point of the test is what run_crew
    does about Grafana, not what Gemini says, and driving a real 80 second crew to find
    out would make this check too slow to run.
    """

    state = {
        "sql_trace": [{
            "agent": "fleet_scout", "tool": "run_query", "rows": 1, "blocked": False,
            "sql": "SELECT integrated_lufs FROM vault.fleet",
            "result": '[{"integrated_lufs": -37.2}]', "at": "2026-09-09T00:00:00Z",
        }],
        "work_order": {
            "summary": "one title",
            "queue": [{
                "rank": 1, "title_id": "fugitive_valley", "title": "Fugitive Valley",
                "lane": "BATCH", "integrated_lufs": -37.2, "lufs_delta": None,
                "failures": 2, "rescue_cost": "one loudnorm pass", "rationale": "-37.2 LUFS",
            }],
            "operator_note": "start today, defer nothing, 120s window",
        },
    }

    def __init__(self, *_, **__):
        self.session_service = _FakeSessionService(self.state)

    def run_async(self, **_):
        async def _empty():
            return
            yield  # pragma: no cover

        return _empty()


def _drive(monkeypatch, *, grafana: bool) -> list[dict]:
    monkeypatch.setattr(crew, "credentials_present", lambda: True)
    monkeypatch.setattr(crew, "grafana_credentials_present", lambda: grafana)

    import google.adk.runners as runners

    monkeypatch.setattr(runners, "InMemoryRunner", _FakeRunner)

    async def collect():
        return [event async for event in crew.run_crew(timeout_s=10)]

    return asyncio.run(collect())


def test_run_crew_does_not_raise_when_the_grafana_token_is_missing(monkeypatch):
    """The check the deployed service depends on."""
    try:
        events = _drive(monkeypatch, grafana=False)
    except crew.GrafanaRequired as exc:
        pytest.fail(
            "run_crew refused to run because Grafana is unconfigured. The deployed "
            f"service has no Grafana token, so this is the live URL failing: {exc}"
        )
    orders = [event for event in events if event.get("type") == "work_order"]
    assert orders, "the crew produced no work order with Grafana absent"
    assert orders[0]["queue"], "the ClickHouse queue is empty with Grafana absent"


def test_a_skipped_board_says_it_was_skipped_and_carries_no_publish(monkeypatch):
    """A step that did not happen must not be drawable as one that did."""
    order = next(
        event for event in _drive(monkeypatch, grafana=False)
        if event.get("type") == "work_order"
    )
    board = order["grafana_board"]
    assert board["ran"] is False
    assert board["skipped"] is True
    assert board["publish"] is None, "a skipped step is carrying a publish payload"
    assert "token" in (board["reason"] or "").lower(), board["reason"]


def test_a_board_that_ran_is_reported_as_having_run(monkeypatch):
    order = next(
        event for event in _drive(monkeypatch, grafana=True)
        if event.get("type") == "work_order"
    )
    board = order["grafana_board"]
    assert board["ran"] is True and board["skipped"] is False
    assert board["reason"] is None


# --- the readiness the interface reads ------------------------------------

def _agents(monkeypatch, *, grafana: bool) -> dict:
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from web.app import app

    monkeypatch.setattr(crew, "credentials_present", lambda: True)
    monkeypatch.setattr(crew, "grafana_credentials_present", lambda: grafana)
    return TestClient(app).get("/api/agents").json()


def test_the_clickhouse_crew_reads_ready_without_a_grafana_token(monkeypatch):
    """ClickHouse readiness and Grafana readiness are separate facts."""
    payload = _agents(monkeypatch, grafana=False)
    assert payload["ready"] is True, (
        "the crew this track is about is reported not ready because a bonus surface "
        "for a different partner has no token"
    )


def test_the_board_reports_its_own_state_with_a_reason(monkeypatch):
    payload = _agents(monkeypatch, grafana=False)
    assert payload["grafana"]["available"] is False
    assert payload["grafana"]["optional"] is True
    assert "token" in payload["grafana"]["reason"].lower()

    board = next(a for a in payload["agents"] if a["name"] == "grafana_board")
    assert board["available"] is False
    assert board["reason"], "the board node is unavailable with no reason given"

    for agent in payload["agents"]:
        if agent["name"] != "grafana_board":
            assert agent["available"] is True, (
                f"{agent['name']} is a ClickHouse agent and must not be marked "
                "unavailable because Grafana is unconfigured"
            )


def test_the_published_shape_matches_what_will_actually_run(monkeypatch):
    """The interface draws `shape`. A diagram that overstates the graph is a lie."""
    without = _agents(monkeypatch, grafana=False)["shape"]
    assert "grafana_board" not in without, without

    with_board = _agents(monkeypatch, grafana=True)["shape"]
    assert "grafana_board" in with_board, with_board
