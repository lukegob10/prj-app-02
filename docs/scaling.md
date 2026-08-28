# Agora scale and capacity runbook

This is the first scale-hardening profile for the project-scoped sharing slice. It is a
representative staging workload and a set of release gates, not a capacity claim. No staging run
has been completed from this repository yet.

> **UNVERIFIED CAPACITY TARGETS:** every concurrency, rate, latency, error, query, pool-wait,
> throughput, and propagation target in this document remains unverified until it is measured on
> representative Oracle, horizontally shared storage, and the intended deployment topology. Unit
> tests and local runs cannot verify these targets, and this repository makes no 40,000–50,000
> concurrency claim.

## Representative staging profile

Run against a production-shaped Oracle schema, the portal and content services on separate
scalable pools, horizontally shared artifact storage, TLS termination, and representative
Dashboard/revision/grant distributions. Use synthetic SOEIDs and content only. Ramp for 15
minutes, hold for 30 minutes, then run a 10-minute mutation burst; repeat after a cold start and
with one application instance drained.

| Scenario | UNVERIFIED sustained target | UNVERIFIED burst/notes |
|---|---:|---|
| Concurrent signed-in sessions | 2,000 | 5,000-session soak is the next sizing step |
| Active-grant authorization checks | 250 requests/s | Mix owners, active viewers, revoked viewers, disabled users, and misses |
| Shared with Me reads | 100 requests/s | 10–100 projects per viewer; bounded page sizes |
| Grant/revoke mutations | 20 requests/s | 100 requests/s for 60 seconds; include duplicate and retry races |
| Favorites / Recently viewed | 100 requests/s | Bounded top-N reads; include revoked and unpublished intersections |
| Dashboard access-request queue | 50 requests/s | One selected Dashboard, keyset pages, sparse and full pages |
| Ownership transfer | 5 requests/s | Race transfer against revision, Grant, request, and render issuance |
| Render starts | 50 requests/s | Exact pinned revision plus Authorized Open/viewer-state capture |
| Authorized-open aggregation | 1,000 rows/batch | Serialized checkpoint; measure rollup and retention batches off request path |
| HTML delivery | 500 requests/s | Representative artifact sizes and cache-control policy |
| CSV delivery | 100 requests/s | Representative multi-file dashboards and byte throughput |

The scenario runner must retain request IDs and the workload seed, and must record response status,
payload size, and authorization outcome without logging credentials, cookies, render tokens, or
uploaded bytes.

## Opt-in capacity harness

[`scripts/load/agora_capacity.py`](../scripts/load/agora_capacity.py) is a dependency-light Python
harness for the existing workload shape. It is validation-only unless `--execute` is supplied, and
grant/revoke traffic requires the additional `--allow-mutations` acknowledgement. The example
profile at [`load/agora-capacity.example.json`](../load/agora-capacity.example.json) contains no
identity or password: it names environment variables that the operator must inject through an
approved secret mechanism.

First copy the example to a deployment-owned, ignored profile and replace only fixture metadata,
origins, timing, and weights. Validate its shape without making a network request:

```powershell
uv run python -m scripts.load.agora_capacity --profile path/to/staging-capacity.json
```

For an explicitly authorized staging run, inject the four synthetic owner/viewer identity and
password variables named by the profile, create the output directory, and choose a new event file:

```powershell
uv run python -m scripts.load.agora_capacity `
  --profile path/to/staging-capacity.json `
  --execute `
  --allow-mutations `
  --events-output artifacts/agora-capacity-20260828.jsonl
```

The events file is created with no-overwrite semantics. It contains only the workload seed,
profile digest, deterministic request ID, scenario label, status, full duration, first-byte time,
response-byte count, expected/error classification, allowlisted authorization outcome (`allowed`,
`denied`, or `not_applicable`), and numeric Oracle telemetry. It never serializes request URLs,
query strings, form fields, SOEIDs, passwords, cookies, CSRF values, render tokens, response
headers, HTML, or CSV. Portal bodies needed to extract a CSRF value, revoke link, or isolated iframe
URL are bounded and transient; HTML and CSV delivery bodies are streamed only into byte counters.
HTTP proxies inherited from the process are disabled so token-bearing content requests are sent
only to the pinned content origin. HTTPS verification remains enabled. `--allow-http-loopback` is
available only for local smoke diagnostics and cannot produce capacity evidence.

The configured staging services must expose numeric per-request telemetry in the profile's
`oracle_query_ms` and `oracle_pool_wait_ms` response headers. The example uses
`X-Agora-Oracle-Query-Ms` and `X-Agora-Oracle-Pool-Wait-Ms`. Missing or malformed samples are counted
explicitly; a run with missing samples cannot verify the Oracle gates.

The mutation phase deliberately uses one dedicated owner, viewer, and published project and runs
unpaced revoke/deny-observation/regrant/allow-observation cycles sequentially. The fixture must
start and finish with exactly one active synthetic Viewer grant, one representative CSV, and no
other Viewer grants. This safely measures revocation propagation without racing a shared grant,
but it is **not** evidence for the unverified 20 requests/s sustained or 100 requests/s burst
targets. For those gates, run parallel harness processes with exclusive fixture triples and unique
event files, or port the same request shape to an approved distributed load generator. Never let
two mutation workers share a project/grant fixture. A single profile also reuses one synthetic
viewer identity across distinct cookie sessions; shard across synthetic identities when user
distribution matters. An interrupted or failed mutation run can stop after a revoke; inspect the
dedicated fixture and restore its active grant through the normal portal workflow before reuse.

The harness exercises:

- signed-in session establishment with Django CSRF and isolated cookie jars;
- Shared with Me and project-scoped active-grant reads;
- render starts followed by exact isolated HTML and configured CSV delivery;
- sequential grant/revoke cycles, generic denial polling on Project Detail, and regrant recovery;
- p50/p95/p99 request latency, error/status counts, response bytes, Oracle query time, Oracle pool
  wait, and revoke/regrant propagation.

Keep the retained JSONL artifact in an approved operational location. Although it is designed to be
redacted, it still contains internal timing and request-correlation data.

## Release gates

Measure each scenario at p50, p95, and p99 after warm-up. These proposed initial gates are all
**UNVERIFIED**; the service owner may tighten them with a recorded SLO:

| Measure | UNVERIFIED gate |
|---|---:|
| Portal metadata, grant check, and Shared with Me p50 | ≤ 75 ms |
| Portal metadata, grant check, and Shared with Me p95 | ≤ 150 ms |
| Portal metadata, grant check, and Shared with Me p99 | ≤ 400 ms |
| Render start/token issuance p50 | ≤ 125 ms |
| Render start/token issuance p95 / p99 | ≤ 250 / 600 ms |
| Authorized HTML/CSV first-byte p50 | ≤ 150 ms |
| Authorized HTML/CSV first-byte p95 / p99 | ≤ 300 / 800 ms |
| Error rate (5xx, timeout, and unexpected 4xx) | < 0.1% overall; 0 auth bypasses |
| Oracle query latency p95 / p99 | ≤ 75 / 250 ms |
| Oracle pool wait p95 | ≤ 25 ms; no pool exhaustion |
| Revoke-to-next-check propagation | ≤ 1 second p99 on the primary authorization path |
| Render authorization / Authorized Open write failures | 0 silent failures; alert and fail closed |

Also inspect query counts for representative pages, database execution plans for the active-grant,
viewer-to-project, personal-list, Dashboard-scoped request, stale-after, and analytics-rollup
indexes, connection churn, CPU/memory, storage latency, and artifact bytes/s.
The bounded queryset tests are regression checks, not throughput evidence.

## Current production blockers

Capacity cannot honestly be claimed until the following are resolved and measured:

- `CONN_MAX_AGE=0` currently permits direct connection creation per request. Pooling must be
  implemented and qualified through the `treasury_analytics` package boundary; Agora must not
  bypass that ownership boundary.
- Artifact storage must be horizontally shared and durable before portal/content instances scale
  independently. A local filesystem is not a production multi-instance artifact tier.
- Portal metadata/authorization and content byte delivery need independent scaling, health,
  timeout, and rate-limit policies while preserving the separate origins and security headers.
- Authorized-open aggregation and 90-day raw retention need scheduled bounded invocations,
  partition qualification, lag alerts, and execution-plan evidence without contending with hot
  authorization paths. The schema and typed job boundaries do not themselves schedule work.
- Render-authorization and audit retention/cleanup need bounded indexes, retention policy, and
  operational jobs that cannot contend with hot authorization paths.
- Representative Oracle execution plans must verify every keyset read path. Existing indexes cover
  SOEID, dashboard/revision number, and grant scopes, but owner-project recency ordering does not
  yet have a proven composite covering index. Measurement may justify a separately owned schema
  migration; this read-path lane deliberately does not change models or migrations.
- Observability is required: redacted request IDs, latency histograms, query and pool wait
  metrics, denial/error reasons, artifact throughput, and revocation propagation alerts.

Until those gates are met, “thousands of people at once” is a design target for staging rather
than a production guarantee. Do not add Redis, a queue, a SPA, or a service split solely to make
the claim; introduce them only after measured bottlenecks and an approved design change.
