#!/usr/bin/env bash
# Export the deployed service's own environment into this shell, so local runs
# and tests hit the same ClickHouse Cloud instance and the same Vertex project a
# judge hits. Nothing is written to disk: the values are read from Cloud Run at
# call time and only ever live in the shell that sources this file.
#
#   source scripts/cloudenv.sh
#
# Requires gcloud auth on the project that owns the `vault` service.
set -u

_REGION="${CLOUD_RUN_REGION:-us-central1}"
_SERVICE="${CLOUD_RUN_SERVICE:-vault}"

_json="$(gcloud run services describe "$_SERVICE" --region "$_REGION" --format=json 2>/dev/null)" || {
  echo "cloudenv: could not describe Cloud Run service $_SERVICE in $_REGION" >&2
  return 1 2>/dev/null || exit 1
}

eval "$(printf '%s' "$_json" | python3 -c '
import json, shlex, sys
spec = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
for entry in spec.get("env", []):
    if "value" in entry:
        print("export %s=%s" % (entry["name"], shlex.quote(entry["value"])))
')"

unset _json _REGION _SERVICE
echo "cloudenv: ClickHouse host ${CLICKHOUSE_HOST:-unset}, Vertex project ${GOOGLE_CLOUD_PROJECT:-unset}"
