# Redslip: partner wiring

One product, one Devpost track: **Grafana**. The warehouse is ClickHouse. The partner the form selects is the official `mcp-grafana` server talking to Grafana Cloud (`https://redslip.grafana.net`).

ClickHouse is how the archive is measured and ranked. Grafana is how the week's queue is published for a human to open.

## Runtime path

```
POST /api/triage
    ->  ADK SequentialAgent
          fleet_scout           McpToolset -> mcp-clickhouse
          ParallelAgent
            loudness_analyst    McpToolset -> mcp-clickhouse
            structural_analyst  McpToolset -> mcp-clickhouse
          work_allocator        no DB tools
          grafana_board         McpToolset -> mcp-grafana
    ->  agent/grafana_publish.py
          search_dashboards
          update_dashboard      uid redslip-fleet
          create_annotation
    ->  https://redslip.grafana.net/d/redslip-fleet
```

`grafana_publish.publish_fleet` is the same official server on stdio, used when the board is written from a measured catalog. GET `/api/dashboards/uid/redslip-fleet` returning title `Redslip fleet` is the post-condition.

## Where it is in code

| Piece | File |
|---|---|
| ClickHouse MCP | `agent/crew.py` `clickhouse_toolset` |
| Grafana MCP | `agent/crew.py` `grafana_toolset` |
| Board write | `agent/grafana_publish.py` |
| Live board | `https://redslip.grafana.net/d/redslip-fleet` |
| Token | `GRAFANA_SERVICE_ACCOUNT_TOKEN` in `.env` (not in git) |

## Google

Gemini decides queue order. ffmpeg and ClickHouse produce every number. Grafana holds the board a mixer opens.

Hosted `https://mcp.grafana.com/mcp` is the interactive Grok/Claude hook (OAuth). Unattended publish uses open-source `mcp-grafana` with a service account token, which Grafana's own unattended-deploy note allows.
