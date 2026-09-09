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
    panels = payload["dashboard"]["panels"]
    assert panels, "dashboard has no panels"

    # The board agent composes the panel itself, so the panel type is the model's
    # choice: a markdown table carries the queue in `options.content`, a table panel
    # carries it in `targets[].rows`. Asserting one shape made this test red whenever
    # the crew picked the other, which is a fact about the panel type and not about
    # whether the work order reached Grafana. What has to be true is that the published
    # board carries the queue.
    published = json.dumps(panels)
    assert "Fugitive Valley" in published, (
        "the published board does not name the worst title in the queue:\n"
        + published[:2000]
    )
    assert "lufs_delta" in published or "LUFS" in published, (
        "the published board carries no loudness figure:\n" + published[:2000]
    )
