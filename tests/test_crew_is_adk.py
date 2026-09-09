"""The crew is a real Google ADK multi-agent system, and each agent has a job.

These are structural, not behavioural: they build the actual graph the service runs
and assert what it is made of. Behaviour is covered by the guard and verification
tests, and by running it.

What each assertion is protecting against, stated plainly, because a topology test
that only counts nodes is a test that cannot fail usefully:

  - the graph collapsing back to one agent with one prompt
  - the parallel pair quietly becoming sequential, which would make ParallelAgent
    decoration
  - the allocator acquiring database tools, which would let it query for a number
    instead of quoting one, and would make the verification check meaningless
  - an agent losing its output_key, which silently breaks the hand-off because the
    next agent's instruction template then renders empty
  - the ClickHouse tools stopping being the official MCP server
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import crew  # noqa: E402


@pytest.fixture(scope="module")
def graph():
    """The full graph, built explicitly rather than from whatever the shell exports.

    `build_crew()` includes the Grafana board only when a token is configured, so
    reading the environment here would make these structural assertions pass or fail
    depending on the machine. Grafana's optionality is a separate contract, covered by
    tests/test_grafana_is_optional.py.
    """
    return crew.build_crew(with_grafana=True)


def test_the_root_is_an_adk_sequential_agent(graph):
    from google.adk.agents import SequentialAgent

    assert isinstance(graph, SequentialAgent)
    assert graph.name == "redslip_triage"
    assert [child.name for child in graph.sub_agents] == [
        "fleet_scout", "evidence", "work_allocator", "grafana_board"
    ], "the pipeline order changed; Grafana publish cannot run before the work order exists"


def test_the_two_analysts_run_in_parallel_and_write_different_keys(graph):
    from google.adk.agents import ParallelAgent

    evidence = graph.sub_agents[1]
    assert isinstance(evidence, ParallelAgent), (
        "the analysts are sequential again, which makes ParallelAgent decoration"
    )
    names = [child.name for child in evidence.sub_agents]
    assert names == ["loudness_analyst", "structural_analyst"]

    keys = [child.output_key for child in evidence.sub_agents]
    assert keys == ["loudness_evidence", "structural_evidence"], (
        "two parallel agents writing the same state key would race and one would win"
    )
    assert len(set(keys)) == len(keys)


def test_every_agent_hands_its_work_forward_under_its_own_key(graph):
    expected = {
        "fleet_scout": "fleet",
        "loudness_analyst": "loudness_evidence",
        "structural_analyst": "structural_evidence",
        "work_allocator": "work_order",
        "grafana_board": "grafana_publish",
    }
    found = {}

    def walk(agent):
        key = getattr(agent, "output_key", None)
        if key:
            found[agent.name] = key
        for child in getattr(agent, "sub_agents", None) or []:
            walk(child)

    walk(graph)
    assert found == expected, f"state hand-off changed: {found}"


def test_the_three_reading_agents_hold_the_official_mcp_clickhouse_server(graph):
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

    holders = {}

    def walk(agent):
        for tool in getattr(agent, "tools", None) or []:
            if isinstance(tool, McpToolset):
                holders[agent.name] = tool
        for child in getattr(agent, "sub_agents", None) or []:
            walk(child)

    walk(graph)
    clickhouse = {"fleet_scout", "loudness_analyst", "structural_analyst"}
    assert clickhouse <= set(holders), f"ClickHouse holders missing: {sorted(holders)}"
    assert "grafana_board" in holders, "grafana_board lost mcp-grafana"

    command = " ".join(crew.server_command())
    assert command.endswith("mcp-clickhouse") or "mcp-clickhouse" in command, (
        f"the ClickHouse tools are not the official server: {command}"
    )
    grafana_cmd = " ".join(crew.grafana_server_command())
    assert "mcp-grafana" in grafana_cmd, (
        f"the Grafana tools are not the official server: {grafana_cmd}"
    )


def test_the_allocator_holds_no_database_tools(graph):
    """It has to quote its colleagues, because it cannot ask the database itself.

    This is what makes `verify_against_cells` a real check. If the allocator could
    query, an unsupported figure would be indistinguishable from a figure it fetched
    without recording.
    """
    allocator = next(child for child in graph.sub_agents if child.name == "work_allocator")
    assert allocator.name == "work_allocator"
    assert not (allocator.tools or []), (
        "the allocator acquired tools; it must only be able to quote the evidence"
    )
    assert allocator.output_schema is not None, (
        "the work order is no longer schema-pinned, so the queue shape is not guaranteed"
    )


def test_the_read_only_gate_is_wired_on_every_agent_that_can_query(graph):
    def walk(agent):
        from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

        if any(isinstance(tool, McpToolset) for tool in (getattr(agent, "tools", None) or [])):
            assert agent.before_tool_callback is crew._guard_tool, (
                f"{agent.name} can reach ClickHouse without the read-only gate"
            )
            assert agent.after_tool_callback is crew._record_tool, (
                f"{agent.name} can query without its SQL being recorded"
            )
        for child in getattr(agent, "sub_agents", None) or []:
            walk(child)

    walk(graph)


def test_the_published_topology_matches_the_graph_that_runs(graph):
    """The interface draws TOPOLOGY. A diagram that drifts from the code is a lie."""
    described = {entry["name"] for entry in crew.TOPOLOGY}
    actual = set()

    def walk(agent):
        from google.adk.agents import LlmAgent

        if isinstance(agent, LlmAgent):
            actual.add(agent.name)
        for child in getattr(agent, "sub_agents", None) or []:
            walk(child)

    walk(graph)
    assert described == actual, (
        f"the published topology and the running graph disagree: "
        f"described {sorted(described)}, actual {sorted(actual)}"
    )
