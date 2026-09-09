"""Synchronous entry points into the crew.

The agent layer lives in `agent/crew.py`, which is a Google ADK multi-agent system
holding the official `mcp-clickhouse` server through ADK's `McpToolset`. This module
is the boundary for callers that want one call and one answer rather than a stream:
the CLI, and the non-streaming `/api/triage`.

`GeminiRequired` is re-exported here because it is the contract the rest of the
project checks against: Redslip refuses to produce a work order without a model
rather than falling back to a canned plan.
"""

from __future__ import annotations

import asyncio

from agent.crew import GeminiRequired, credentials_present, run_crew  # noqa: F401


async def generate_triage_report() -> dict:
    """Run the crew and return only the final, verified work order."""
    final = None
    async for event in run_crew():
        if event.get("type") == "work_order":
            final = event
    if final is None:
        raise GeminiRequired(
            "The crew produced no work order. Nothing is dispatched on a partial run."
        )
    return final


def run_triage() -> dict:
    return asyncio.run(generate_triage_report())


if __name__ == "__main__":
    import json

    print(json.dumps(run_triage(), indent=2, default=str))
