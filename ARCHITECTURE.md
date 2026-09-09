# Redslip: partner wiring

The warehouse is ClickHouse, reached only through the official `mcp-clickhouse` MCP
server. Grafana is an optional publishing surface: when a service account token is
configured, a fifth agent writes the finished work order onto a Grafana Cloud board
through the official `mcp-grafana` server.

ClickHouse is how the archive is measured and ranked. Grafana is how the week's queue is
published for a human to open.

## Runtime path

```
POST /api/triage
    ->  ADK SequentialAgent
          fleet_scout           McpToolset -> mcp-clickhouse
          ParallelAgent
            loudness_analyst    McpToolset -> mcp-clickhouse
            structural_analyst  McpToolset -> mcp-clickhouse
          work_allocator        no DB tools
          grafana_board         McpToolset -> mcp-grafana   (only with a token)
    ->  agent/grafana_publish.py
          search_dashboards
          update_dashboard      uid redslip-fleet
          create_annotation
    ->  https://redslip.grafana.net/d/redslip-fleet
```

The board agent is built into the graph only when `grafana_credentials_present()` is
true. Without a token it is absent, the run reports the step as skipped with its reason
and a null publish payload, and `/api/agents` still reports `ready: true` on ClickHouse
readiness alone. `tests/test_grafana_is_optional.py` asserts both graph shapes and the
readiness split; `tests/test_gemini_required.py` asserts that only the model is a hard
dependency.

`grafana_publish.publish_fleet` uses the same official server on stdio when the board is
written from a measured catalog. `GET /api/dashboards/uid/redslip-fleet` returning title
`Redslip fleet` is the post-condition.

## Where it is in code

| Piece | File |
|---|---|
| ClickHouse MCP | `agent/crew.py` `clickhouse_toolset` |
| Read-only gate | `agent/crew.py` `is_read_only`, `_guard_tool` |
| Statement recorder | `agent/crew.py` `_record_tool` |
| Figure verification | `agent/crew.py` `verify_against_cells` |
| Grafana MCP | `agent/crew.py` `grafana_toolset` |
| Board write | `agent/grafana_publish.py` |
| Token | `GRAFANA_SERVICE_ACCOUNT_TOKEN` in `.env`, not in git |

## Google

Gemini decides queue order. ffmpeg and ClickHouse produce every number. Grafana holds the
board a mixer opens.

Hosted `https://mcp.grafana.com/mcp` is the interactive OAuth hook. Unattended publish
uses open-source `mcp-grafana` with a service account token, which Grafana's own
unattended-deploy note allows.
