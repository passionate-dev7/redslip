#!/usr/bin/env bash
# Serve Redslip locally. Reads ClickHouse and Vertex config from the environment.
set -e
cd "$(dirname "$0")"
export PYTHONPATH="."
uv run uvicorn web.app:app --host 0.0.0.0 --port 8000 --reload
