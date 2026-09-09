"""Grafana publish goes through official mcp-grafana, against a live stack.

Goes red:
  - if the server command is not mcp-grafana
  - if an empty fleet is allowed through
  - if the dashboard GET does not return title Redslip fleet after a publish
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import crew  # noqa: E402
from agent import grafana_publish as gp  # noqa: E402


def test_grafana_tools_are_the_official_server():
    cmd = " ".join(crew.grafana_server_command())
    assert "mcp-grafana" in cmd, cmd


def test_empty_fleet_is_refused():
    with pytest.raises(crew.GrafanaRequired):
        gp.publish_fleet_sync([])


@pytest.mark.skipif(
    not crew.grafana_credentials_present(),
    reason="GRAFANA_SERVICE_ACCOUNT_TOKEN unset",
)
def test_live_dashboard_is_redslip_fleet():
    tok = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN") or os.environ.get("GRAFANA_API_KEY")
    url = crew.grafana_url() + "/api/dashboards/uid/" + gp.DASHBOARD_UID
    req = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + tok, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read())
    assert payload["dashboard"]["title"] == "Redslip fleet"
    assert payload["dashboard"]["panels"], "dashboard has no panels"
    md = payload["dashboard"]["panels"][0]["options"]["content"]
    assert "Fugitive Valley" in md or "LUFS" in md
