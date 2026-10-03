# Validation

## Automated coverage

- Unit/integration tests cover native timestamp normalization, idempotency/corrections, revision fencing, threshold arithmetic, gap/null/zero semantics, UTC/local DST boundaries and raw/cache equivalence.
- Mocked Kraken tests cover single-flight tokens, refresh-token epoch expiry, HTTP 200 throttling, cooldowns and the one-authentication-retry limit.
- API tests exercise authentication, redaction, CSRF, optimistic settings revisions and range bounds.
- Storage tests exercise SQLite WAL, cancellation/reader reuse and a consistent backup/integrity check.
- Chromium tests use the real FastAPI/SSE server with synthetic telemetry: login, live state, chart rendering, settings, presets, missing data and logout.
- CI checks Python formatting/lint/types/tests, the TypeScript/frontend build, Chromium scenarios and AMD64/ARM64 image builds.

## Resource workload

The following checks were completed on 2026-10-03:

- 18 backend tests, strict mypy, Ruff lint/format, TypeScript/production frontend build and two Chromium end-to-end scenarios passed.
- Both AMD64 and ARM64 production images built. The ARM64 build used an isolated emulation-capable BuildKit builder; its runtime imported the application, native SSE and SQLite successfully.
- The normal production container served HTTPS using a locally issued certificate, with strict client chain/hostname verification and the separate certificate-validating health probe.
- UID 10001, read-only root filesystem, dropped capabilities, 140 MiB memory/no additional swap, private-key readability and invalid Host rejection were exercised.
- A full-year consistent backup passed SQLite integrity checking. Starting the application from that backup preserved its password, configuration, device history and analytics. Graceful shutdown returned exit code 0.
- The public device-discovery query returned the expected authentication-required error, without schema-validation errors. This is not a credentialed account test.

The resource run used Docker Desktop 29.5.2 on a Windows host, an x86_64 Linux container, Python 3.12.15 and SQLite 3.40.1. The initial synthetic database held **3,153,601 raw readings** and five-minute/threshold rollups, using **706,015,232 bytes**. Its cumulative counters were null, so this size is not a general prediction for real telemetry.

The final workload ran the real collector and maintenance tasks with a **synthetic HTTP transport**, while four authenticated SSE clients and sequential analytical queries ran over HTTPS. Database data versions changed from 4 to 5 during the workload, explicitly confirming ingestion progressed rather than merely leaving a sleeping worker enabled.

| Query | Median of 3 requests | Maximum |
|---|---:|---:|
| 24-hour summary | 0.011 s | 0.269 s |
| 24-hour history | 0.225 s | 0.236 s |
| 30-day summary | 0.150 s | 0.158 s |
| 30-day history | 1.542 s | 1.614 s |
| 365-day summary | 1.663 s | 1.669 s |
| 365-day history | 3.053 s | 3.109 s |

All returned history arrays remained within the requested 1,500-bucket bound. Filesystem caches were not forcibly cleared on the shared host; these figures are not a cold-cache or statistically meaningful p95 certification.

The concurrent run reported cgroup `memory.peak = 146,800,640` bytes, below 150,000,000 bytes, with **zero OOM/kill events** and a healthy process. Memory-limit reclaim events occurred: this demonstrates operation under the cap, not unlimited spare headroom. A separate warm-cache production-server run peaked at 66,985,984 bytes. Cache accounting explains why process RSS or one warm run alone would be misleading.

Images used for the resource measurements above (not published release identifiers):

```text
AMD64: sha256:48a97db271fe97b7717801383eded222bcf1ca5f854da3fe48cf8b0f85be1772
ARM64: sha256:f2187abe91146c1f880d8c009160a94c48afae714f377b8b19066b83134373d7
```

Reproduce the disposable-data workload with `tests/performance/seed_history.py`, optionally `serve_mock.py` for a network-free synthetic collector, and `workload.py --require-ingestion`. The synthetic server refuses databases not marked `A-SYNTHETIC`, and removes its fake key/disables collection on graceful exit. Never mount real household data into this harness.

## Outstanding release gates

These cannot be replaced by mocked tests or an AMD64/emulated workload:

1. **A real Octopus account/meter:** verify the current device discovery relationship, authorization header, native timestamp semantics, 10-second grouping/peak interpretation, lookback and account-specific quotas. No real credentials are committed or required by CI.
2. **A physical Raspberry Pi ARM64:** repeat the one-year workload on the intended Pi/storage/OS and prove total container-accounted memory stays below 150,000,000 bytes without OOM/restarts. Include concurrent collection, four SSE clients, login, health probes and maintenance.
3. **Deployment's trusted HTTPS certificate:** confirm SANs/client trust and UID 10001 file access on the actual deployment host.

Until those gates are recorded, treat this as a working implementation with outstanding hardware/provider acceptance, not certified peak-metering accuracy or a measured universal Pi memory guarantee.
