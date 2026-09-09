## MCP tool call log

Proof that the Redslip crew queries ClickHouse through the official `mcp-clickhouse`
MCP server. Every line was written by a real run: the crew's own statements by
`_log_queries` in `web/app.py`, the evidence statements by
`scripts/capture_mcp_evidence.py`. Nothing here is a fixture.

Tool: `run_query` (mcp-clickhouse 0.6.0)
Transport: stdio, JSON-RPC 2.0
Server: `mcp-clickhouse` subprocess started with `CLICKHOUSE_ALLOW_WRITE_ACCESS=false`,
which makes it send `readonly=1` with every statement

### Two files, because the catalog was re-ingested

`mcp_tool_calls.jsonl` is the current schema generation and is what `/api/mcp-log`
serves. It starts at `2026-09-09T08:42:59+00:00`, the first statement any agent composed
against `vault.fleet`, and every answer in it describes the catalog this service serves
now: 20 titles, 24,017 loudness sample rows.

`mcp_tool_calls.pre-fleet.jsonl` is everything before that instant, kept verbatim. Those
calls happened and their answers were correct when they ran, against a larger and partly
duplicated ingest: 34.83 thousand sample rows, 28 measured titles, 145 findings. They are
not edited to match today's numbers, because changing what a transcript says a server
returned is falsifying it, and they are not served, because a transcript quoting a
superseded catalog is not evidence about this one.

The cutoff, the reason for it, and the number of withheld entries are all in the
`/api/mcp-log` payload, so a reader is told what is missing rather than left to notice.
`tests/test_mcp_log_matches_stats.py` fails if any figure in the served log disagrees
with what `/api/stats` reports, and the rules that test applies live in `qc/mcp_log.py`.

`grafana_mcp_tool_calls.jsonl` is the same thing for the optional `mcp-grafana` server.

### Artefacts

`docs/evidence/` holds the captured output of the two claims a judge cannot reach the
Cloud database to re-run: the `EXPLAIN indexes = 1` plan showing the ranking never reads
`vault.loudness_samples`, and the `Code 164 READONLY` refusal showing the server will not
write. Both carry the command that produced them.
