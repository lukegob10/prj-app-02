# Agora scale and capacity runbook

This is the first scale-hardening profile for the project-scoped sharing slice. It is a
representative staging workload and a set of release gates, not a capacity claim. No staging run
has been completed from this repository yet.

## Representative staging profile

Run against a production-shaped Oracle schema, the portal and content services on separate
scalable pools, horizontally shared artifact storage, TLS termination, and representative
Dashboard/revision/grant distributions. Use synthetic SOEIDs and content only. Ramp for 15
minutes, hold for 30 minutes, then run a 10-minute mutation burst; repeat after a cold start and
with one application instance drained.

| Scenario | Sustained target | Burst/notes |
|---|---:|---|
| Concurrent signed-in sessions | 2,000 | 5,000-session soak is the next sizing step |
| Active-grant authorization checks | 250 requests/s | Mix owners, active viewers, revoked viewers, disabled users, and misses |
| Shared with Me reads | 100 requests/s | 10–100 projects per viewer; bounded page sizes |
| Grant/revoke mutations | 20 requests/s | 100 requests/s for 60 seconds; include duplicate and retry races |
| Render starts | 50 requests/s | Exact pinned published revisions and token issuance |
| HTML delivery | 500 requests/s | Representative artifact sizes and cache-control policy |
| CSV delivery | 100 requests/s | Representative multi-file dashboards and byte throughput |

The scenario runner must retain request IDs and the workload seed, and must record response status,
payload size, and authorization outcome without logging credentials, cookies, render tokens, or
uploaded bytes. If the deployment team uses k6, the profile entry point is:

```powershell
k6 run --vus 2000 --duration 30m --summary-export artifacts/ag004-summary.json load/ag004.js
```

`load/ag004.js` is deployment-owned and is intentionally not a product dependency or CI test;
the command is an unverified staging gate until that scenario exists and has been run against a
representative environment.

## Release gates

Measure each scenario at p50, p95, and p99 after warm-up. Proposed initial gates (the service
owner may tighten them with a recorded SLO) are:

| Measure | Gate |
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
| Render authorization and audit write failures | 0 silent failures; alert and fail closed |

Also inspect query counts for representative pages, database execution plans for the active-grant
and viewer-to-project indexes, connection churn, CPU/memory, storage latency, and artifact bytes/s.
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
- Render-authorization and audit retention/cleanup need bounded indexes, retention policy, and
  operational jobs that cannot contend with hot authorization paths.
- Observability is required: redacted request IDs, latency histograms, query and pool wait
  metrics, denial/error reasons, artifact throughput, and revocation propagation alerts.

Until those gates are met, “thousands of people at once” is a design target for staging rather
than a production guarantee. Do not add Redis, a queue, a SPA, or a service split solely to make
the claim; introduce them only after measured bottlenecks and an approved design change.
