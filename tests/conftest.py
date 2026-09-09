"""The one gate that decides whether a test can see the measured catalog.

The measurements live in ClickHouse Cloud, so a clean clone cannot reach them, and the
tests that read them have nothing to assert. They should say that, by name, rather than
fail: six of them used to report `Database vault does not exist`, which reads as a
broken project to anyone who clones the repo and runs the suite.

The check is deliberately two-stage. `SELECT 1` succeeding only proves that some
ClickHouse answered; a developer with a local server and no ingest passes that and then
fails on the first real query. So the tables are checked too, and the skip names the
host it looked at and how to point it somewhere useful.

Every test that reads real measurements should take `vault_clickhouse` rather than
calling `qc.store.client()` itself, so there is one place that knows what "no data
here" looks like.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The tables and views every reading test needs. `fleet` is a view over the other two,
# so its absence means the schema was applied from an older revision.
REQUIRED_OBJECTS = ("loudness_samples", "findings", "events", "title_loudness", "fleet")

_HOW_TO_GET_THE_DATA = (
    "The measured catalog lives in ClickHouse Cloud. Point this suite at it with "
    "`source scripts/cloudenv.sh`, which reads the deployed service's own environment "
    "and needs gcloud auth on the project that owns the Cloud Run service, or build a "
    "local copy with `python ingest.py`. Tests that only read measurements skip "
    "without it; nothing about the crew, the read-only gate or the interface depends "
    "on this."
)


@pytest.fixture(scope="session")
def vault_clickhouse():
    """A client that can actually read the vault schema, or a skip that says why not."""
    pytest.importorskip("clickhouse_connect")
    from qc import store

    host = os.getenv("CLICKHOUSE_HOST", "localhost")
    port = os.getenv("CLICKHOUSE_PORT", "8443" if _secure() else "8123")
    where = f"{host}:{port}"

    try:
        client = store.client()
        client.query("SELECT 1")
    except Exception as exc:
        pytest.skip(f"no ClickHouse reachable at {where}: {exc}. {_HOW_TO_GET_THE_DATA}")

    try:
        present = {
            row[0] for row in client.query(
                "SELECT name FROM system.tables WHERE database = 'vault'"
            ).result_rows
        }
    except Exception as exc:
        pytest.skip(f"ClickHouse at {where} would not list the vault schema: {exc}. "
                    f"{_HOW_TO_GET_THE_DATA}")

    missing = [name for name in REQUIRED_OBJECTS if name not in present]
    if missing:
        pytest.skip(
            f"ClickHouse at {where} has no vault.{', vault.'.join(missing)}. "
            f"{_HOW_TO_GET_THE_DATA}"
        )
    return client


def _secure() -> bool:
    return os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true"
