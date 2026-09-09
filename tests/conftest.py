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

# Every test the catalog gate turned away, so the run can report at the end whether
# the ClickHouse integration was exercised at all. A suite that prints "91 passed"
# while every ClickHouse test skipped is the exact shape of a check that cannot fail.
_UNMEASURED: list[str] = []

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


def _configured_for_cloud() -> bool:
    """True when this run was pointed at a real catalog rather than a bare clone.

    A clean clone has no CLICKHOUSE_HOST and should skip quietly. A run that names a
    remote host is asserting the data is there, so the gate failing is a broken
    configuration and must be red, not a skip that reads as success.
    """
    host = os.getenv("CLICKHOUSE_HOST", "").strip()
    return bool(host) and host not in ("localhost", "127.0.0.1", "::1")


def _no_catalog(reason: str) -> None:
    """Skip on a bare clone, fail when the run claimed to have a catalog."""
    _UNMEASURED.append(reason)
    if _configured_for_cloud():
        pytest.fail(
            f"CLICKHOUSE_HOST names a remote catalog but it is unusable: {reason} "
            "This run was configured to exercise the ClickHouse integration and could "
            "not, which is a broken configuration rather than an absent one."
        )
    pytest.skip(reason)


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
        _no_catalog(f"no ClickHouse reachable at {where}: {exc}. {_HOW_TO_GET_THE_DATA}")

    try:
        present = {
            row[0] for row in client.query(
                "SELECT name FROM system.tables WHERE database = 'vault'"
            ).result_rows
        }
    except Exception as exc:
        _no_catalog(f"ClickHouse at {where} would not list the vault schema: {exc}. "
                    f"{_HOW_TO_GET_THE_DATA}")

    missing = [name for name in REQUIRED_OBJECTS if name not in present]
    if missing:
        _no_catalog(
            f"ClickHouse at {where} has no vault.{', vault.'.join(missing)}. "
            f"{_HOW_TO_GET_THE_DATA}"
        )
    return client


def pytest_terminal_summary(terminalreporter):
    """Say plainly whether this run exercised ClickHouse, so green is never ambiguous.

    pytest's own one-line tally counts a skip as a non-failure, which is how a suite
    that never touched the track integration still reads as a pass at a glance.
    """
    if not _UNMEASURED:
        if _configured_for_cloud():
            terminalreporter.write_line(
                f"catalog: ClickHouse integration exercised against "
                f"{os.getenv('CLICKHOUSE_HOST')}", green=True)
        return
    terminalreporter.write_sep("=", "ClickHouse integration NOT exercised", red=True)
    terminalreporter.write_line(
        f"{len(_UNMEASURED)} test group(s) skipped the measured catalog. This run "
        "proves nothing about the ClickHouse track. Run `source scripts/cloudenv.sh` "
        "and re-run before treating this suite as green.", red=True)


def _secure() -> bool:
    return os.getenv("CLICKHOUSE_SECURE", "false").lower() == "true"
