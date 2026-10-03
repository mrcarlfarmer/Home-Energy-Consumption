# Home Energy Monitor

A private Octopus Home Mini electricity dashboard for a Raspberry Pi running 64-bit Linux and Docker. One non-root container serves the bundled web UI over HTTPS, collects telemetry in the background and stores it in SQLite WAL.

## Features

- Live grid demand and separate collection, freshness, storage and connection indicators over SSE.
- 24-hour, 7-day, 30-day and custom history, sampled peaks and trailing 15-minute demand.
- Inverter comparisons at 3.68, 5 and 6 kW: observed time above the rating, all energy during those periods, and only the excess energy.
- Password-protected settings for API-key replacement, account/meter discovery, connectivity testing and polling control.
- Permanent device-scoped raw history, rebuildable five-minute analytics, gap/coverage reporting and local-calendar/DST handling.
- ARM64/AMD64 multi-stage builds, hash-locked dependencies and a 140 MiB container memory guardrail.

**Measurement limitations:** this is sampled grid import, not necessarily total household demand when solar/batteries supply power. Missing telemetry is unknown, not zero. Results are estimates, not billing reconciliation, an inverter installation design or a G98/G99 compliance decision.

## Run on a Raspberry Pi

**Using Portainer?** Follow the [Portainer installation plan](docs/portainer-installation.md) and its [ready-to-paste stack](deploy/portainer-stack.yml), including migration from the local trial.

1. Install Docker Engine and the Compose plugin on a **64-bit** OS.
2. Follow [deployment](docs/deployment.md) to create an administrator password file and a locally trusted HTTPS certificate. Never put real secrets in Git.
3. Copy `.env.example` to `.env`, set your Pi's LAN address, certificate paths, trusted hostnames and HTTPS origins.
4. Run `docker compose up -d --build`.
5. Open your configured HTTPS address on port 8443, sign in and enter your Octopus API key/account. Test connectivity to discover the electricity meter, select it, then save with polling enabled.

The device ID is the **electricity smart meter EUI-64**, not the Home Mini serial number. Polling defaults to 45 seconds. No port forwarding or external dashboard service is required.

## Documentation

- [Deployment and HTTPS](docs/deployment.md)
- [Portainer installation and configuration](docs/portainer-installation.md)
- [Architecture](docs/architecture.md)
- [API contracts](docs/api.md)
- [Analytics methodology](docs/analytics-methodology.md)
- [Operations, backup and recovery](docs/operations.md)
- [Validation and outstanding release gates](docs/validation.md)
- [Original detailed implementation plan](docs/implementation-plan.md)

## Development

Python 3.12+ and Node 22+ are required. On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r requirements-dev.lock
.\.venv\Scripts\python -m pip install --no-deps --no-build-isolation -e .
Set-Location frontend
npm ci
npm run build
npx playwright install chromium
npm test
Set-Location ..
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m mypy backend\app
.\.venv\Scripts\python -m ruff check backend tests
```

On Linux, use `.venv/bin/python` instead. Browser tests start a loopback-only, synthetic HTTP fixture; production always requires HTTPS. The installed `energy-monitor` entry point validates production environment settings and starts exactly one uvicorn worker. Do not use uvicorn reload or additional workers: they would create duplicate collectors and independent session caches.

Regenerate Python locks after deliberate dependency changes:

```text
python -m piptools compile --generate-hashes --strip-extras --allow-unsafe --no-emit-index-url --no-emit-trusted-host --output-file requirements.lock pyproject.toml
python -m piptools compile --generate-hashes --strip-extras --allow-unsafe --no-emit-index-url --no-emit-trusted-host --extra dev --output-file requirements-dev.lock pyproject.toml
```

Use `npm install` to update the frontend lock after changing its manifest. The runtime contains no Node, browser, test runner, ORM, pandas, Redis or external CSS/JS CDN dependency.

The frontend build retains uPlot's license at `/assets/uplot-LICENSE.txt`. Installed Python wheels retain their distribution license metadata in the image.
