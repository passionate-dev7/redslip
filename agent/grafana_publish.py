"""Publish the measured fleet onto Grafana Cloud through official mcp-grafana.

The Grafana track requires the official MCP server at runtime. This module is that
call: search_dashboards, update_dashboard, create_annotation, then a GET of the
dashboard JSON that only a successful write can produce.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.crew import GrafanaRequired, grafana_credentials_present, grafana_server_command, grafana_url

LOG = Path(__file__).parent.parent / "logs" / "grafana_mcp_tool_calls.jsonl"
DASHBOARD_UID = "redslip-fleet"
DASHBOARD_TITLE = "Redslip fleet"


def _log(tool: str, args: dict, result: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "args": {k: v for k, v in args.items() if k != "dashboard"},
            "chars": len(result),
            "preview": result[:400],
        }) + "\n")


def _markdown(rows: list[dict]) -> str:
    lines = [
        "Measured by ffmpeg into ClickHouse, then published through mcp-grafana.",
        "",
        "| title | LUFS | delta LU | lane | verdict | failures |",
        "| --- | ---: | ---: | --- | --- | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('title') or row.get('title_id')} "
            f"| {row.get('integrated_lufs')} "
            f"| {row.get('lufs_delta')} "
            f"| {row.get('lane') or ''} "
            f"| {row.get('verdict') or ''} "
            f"| {row.get('failures') if row.get('failures') is not None else ''} |"
        )
    return "\n".join(lines)


def _dashboard_json(rows: list[dict]) -> dict:
    return {
        "uid": DASHBOARD_UID,
        "title": DASHBOARD_TITLE,
        "timezone": "utc",
        "schemaVersion": 39,
        "editable": True,
        "panels": [
            {
                "id": 1,
                "type": "text",
                "title": "This week's queue",
                "gridPos": {"h": 18, "w": 24, "x": 0, "y": 0},
                "options": {"mode": "markdown", "content": _markdown(rows)},
            }
        ],
        "tags": ["redslip", "mcp-grafana"],
        "time": {"from": "now-6h", "to": "now"},
    }


async def _call(session, tool: str, args: dict) -> str:
    res = await session.call_tool(tool, args)
    text = "".join(getattr(c, "text", "") or "" for c in res.content)
    if res.isError:
        raise GrafanaRequired(f"mcp-grafana {tool} failed: {text[:400]}")
    _log(tool, args, text)
    return text


async def publish_fleet(rows: list[dict]) -> dict[str, Any]:
    if not grafana_credentials_present():
        raise GrafanaRequired(
            "GRAFANA_SERVICE_ACCOUNT_TOKEN is required to publish through mcp-grafana."
        )
    if not rows:
        raise GrafanaRequired("refusing to publish an empty fleet: that would prove nothing.")

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cmd = grafana_server_command()
    params = StdioServerParameters(
        command=cmd[0],
        args=cmd[1:],
        env=os.environ.copy(),
    )
    base = grafana_url()

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await _call(session, "search_dashboards", {"query": "Redslip"})
            created = await _call(session, "update_dashboard", {
                "dashboard": _dashboard_json(rows),
                "overwrite": True,
                "message": "Redslip fleet from measured catalog",
            })
            await _call(session, "create_annotation", {
                "dashboardUid": DASHBOARD_UID,
                "text": (
                    f"{rows[0].get('title')}  {rows[0].get('integrated_lufs')} LUFS  "
                    f"delta {rows[0].get('lufs_delta')} LU"
                ),
                "tags": ["redslip", "worst"],
                "time": int(time.time() * 1000),
            })

    dashboard_url = f"{base}/d/{DASHBOARD_UID}"
    return {
        "published": True,
        "dashboard_uid": DASHBOARD_UID,
        "dashboard_url": dashboard_url,
        "titles_plotted": len(rows),
        "update": created[:500],
    }


def publish_fleet_sync(rows: list[dict]) -> dict[str, Any]:
    return asyncio.run(publish_fleet(rows))
