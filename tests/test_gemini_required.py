"""Gemini is load-bearing in Redslip, not decorative.

The premise is "machine-measured, not estimated": ffmpeg and ClickHouse produce every
number, and the crew decides which of them buy a title a place in this week's queue.
A deterministic fallback emitting the same shape would make the model removable,
which is the exact mistake recorded in the previous hackathon postmortem.

Note that the crew module auto-loads .env, so clearing environment variables in a
shell does not prove the guard works. These patch the credential check directly.

Goes red:
  - if the crew ever returns a work order without a model
  - if a canned plan is reintroduced anywhere in the agent package
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import crew  # noqa: E402
from agent import triage  # noqa: E402


def test_crew_refuses_to_run_without_a_model(monkeypatch):
    monkeypatch.setattr(crew, "credentials_present", lambda: False)

    async def drain():
        async for _ in crew.run_crew():
            pass

    with pytest.raises(crew.GeminiRequired):
        asyncio.run(drain())


def test_gemini_is_required_but_grafana_is_not(monkeypatch):
    """Only the model is a hard dependency of this crew.

    An earlier revision raised GrafanaRequired here. Redslip is a ClickHouse-track
    entry and the deployed service carries no Grafana token, so that turned the one
    interactive control in the product into an error message for the judge it is aimed
    at. Gemini stays mandatory because the ranking is its judgement; Grafana becomes a
    surface that is either in the graph or reported as skipped.

    The degraded run itself is covered by tests/test_grafana_is_optional.py.
    """
    monkeypatch.setattr(crew, "grafana_credentials_present", lambda: False)
    monkeypatch.setattr(crew, "credentials_present", lambda: False)

    async def drain():
        async for _ in crew.run_crew():
            pass

    with pytest.raises(crew.GeminiRequired):
        asyncio.run(drain())

    names = [child.name for child in crew.build_crew().sub_agents]
    assert "grafana_board" not in names, (
        "the board agent is still in the graph with no token, where it would fail "
        "part way through a run instead of being cleanly absent"
    )


def test_triage_entry_point_refuses_too(monkeypatch):
    """The synchronous entry point must not soften the refusal into a partial result."""
    monkeypatch.setattr(crew, "credentials_present", lambda: False)
    with pytest.raises(crew.GeminiRequired):
        triage.run_triage()


def test_a_crew_that_produces_no_work_order_raises(monkeypatch):
    """A run that ends without an allocated queue is a failure, not an empty success.

    This is the failure mode that matters in production: the model answers, the
    agents run, and the last step returns nothing usable. Dispatching nothing while
    reporting success is worse than raising.
    """
    monkeypatch.setattr(crew, "credentials_present", lambda: True)

    async def only_queries(timeout_s: float = 0.0):
        yield {"type": "agent", "agent": "fleet_scout"}
        yield {"type": "query", "agent": "fleet_scout", "sql": "SELECT 1", "rows": 1}

    monkeypatch.setattr(crew, "run_crew", only_queries)
    monkeypatch.setattr(triage, "run_crew", only_queries)
    with pytest.raises(crew.GeminiRequired):
        triage.run_triage()


def test_no_canned_plan_survives_anywhere_in_the_agent_package():
    """Guard against a fallback being quietly reintroduced later."""
    package = Path(crew.__file__).parent
    banned = ("_deterministic_triage", "def _fallback_plan", "FALLBACK_PLAN")
    offenders = []
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for token in banned:
            if token in text:
                offenders.append(f"{source.name}: {token}")
    assert not offenders, f"a canned plan is back, which makes the model removable: {offenders}"
