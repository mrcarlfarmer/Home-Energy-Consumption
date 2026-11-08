# Home Energy Monitor

A private Octopus Home Mini electricity dashboard for a Raspberry Pi running 64-bit Linux and Docker. One non-root container serves the bundled web UI over HTTPS by default, collects telemetry in the background and stores it in SQLite WAL. An explicit trusted-LAN HTTP mode is available without certificates.

## Features

- Live grid demand and separate collection, freshness, storage and connection indicators over SSE.
- Responsive dark instrument-console UI with a midnight/lavender palette, ice-blue charts and direct navigation to history, inverter comparisons and settings.
- 5-minute through 30-day shortcuts (including 12 hours), custom history, sampled peaks and an expanded five-day peaks table.
- Reference comparison at 6 kW: observed time above the rating, all energy during those periods, and only the excess energy.
- Optional Octopus household tariff pricing, with separate estimated costs for all energy while above 6 kW and only the excess.
- Optional Ohme power history from Home Assistant, with a separate EV-excluded analysis view and joint-data coverage.
- Settings for API-key replacement, account/meter discovery, connectivity testing and polling control; password-protected by default, with explicit password-free LAN access available.
- Always-on collection, restart-safe gap recovery, bounded storage, 30-day raw / 396-day five-minute / ten-year daily history, and gap/coverage reporting with local-calendar/DST handling.
- ARM64/AMD64 multi-stage builds, hash-locked dependencies and a 140 MiB container memory guardrail.

**Measurement limitations:** this is sampled grid import, not necessarily total household demand when solar/batteries supply power. Missing telemetry is unknown, not zero. Results are estimates, not billing reconciliation, an inverter installation design or a G98/G99 compliance decision.

## Run on a Raspberry Pi

**No Pi build required:** [GitHub Actions publishes verified ARM64/AMD64 images to GHCR](docs/registry-deployment.md). Select a successful run's digest and let Portainer download it. Private packages use a read-only package token stored in Portainer; publishing uses GitHub's automatic token. Your Octopus key stays in the Pi's private database, never in the image or build pipeline.

**Using Portainer?** Both stacks now protect the entire dashboard with a username and password. Use the [certificate-free HTTP guide](docs/portainer-http-installation.md) or [HTTPS guide](docs/portainer-installation.md). Default username: `admin`; passwords are supplied through a private runtime file. HTTP credentials remain unencrypted, so use HTTPS for transport security.

For the default HTTPS deployment with the Compose CLI:

**Storage upgrade requirement:** prepare a dedicated bounded data filesystem before
deploying this version. The shipped stacks refuse unbounded data directories.
Follow [storage management and migration](docs/storage-management.md), including
setting `ENERGY_DATA_DIRECTORY` for Compose.

1. Install Docker Engine and the Compose plugin on a **64-bit** OS.
2. Follow [deployment](docs/deployment.md) to create an administrator password file and a locally trusted HTTPS certificate. Never put real secrets in Git.
3. Copy `.env.example` to `.env`, set your Pi's LAN address, certificate paths, trusted hostnames and HTTPS origins.
4. Run `docker compose up -d --build`.
5. Open your configured HTTPS address on port 8443, sign in and enter your Octopus API key/account. Test connectivity to discover the electricity meter, select it, then save with polling enabled.

The device ID is the **electricity smart meter EUI-64**, not the Home Mini serial number. Polling defaults to 45 seconds. No port forwarding or external dashboard service is required.

## Exploring the graph

**Auto-refresh graph** controls only the displayed analysis. Turn it off to keep
the graph and selected dates stable while live readings and background polling
continue. **Refresh graph** updates the view manually; turning auto-refresh back
on updates it immediately. These controls do not save or change collection settings.

Use the **12 hours** shortcut or the other presets to view the latest window.
The **<** and **>** buttons move backward/forward by exactly the selected window's
duration, including custom ranges. Navigation fixes the dates, so subsequent
automatic updates do not jump back to now. Forward navigation cannot move beyond
the current time; choose a preset to return to a live-following window.

**Exclude EV charging** is a toggle: off shows whole-meter grid import; on uses
joint meter/Home Assistant data. The graph shows observed mean and sampled peak,
without the trailing fifteen-minute curve. A dashed **6 kW** reference appears
when within the visible power scale. Shading identifies API-defined household
off-peak intervals that overlap the view, using cached tariff pricing; missing
or older tariff periods are not guessed. Fetch rates in Settings first.

**Daily sampled peaks** is always expanded and shows five calendar days ending
at the viewed window, newest first, independently of the graph's duration.
The latest day may be partial. It uses the configured calendar timezone (or
archive timezone for archived history), the selected meter and the EV toggle.
It also shows each day's estimated cost of only the excess energy above 6 kW,
using dated household tariff rates. Costs cover observed energy only, not gaps;
the latest partial day is not extrapolated. Unavailable days or incomplete
price coverage remain
unavailable rather than showing zero. Enable and fetch tariff pricing in
Settings to see costs.

## Recovering gaps in the graph

Select the active meter and affected time range, then use **Recover missing
readings** below the range controls. Meter polling must already be enabled; the
action never starts it or saves credential drafts. It queues a persistent pass
over the selected raw-history range (up to 31 days, normally 30), preserving any
pending recovery work and excluding the newest three minutes.

The collector skips fully supported fifteen-minute windows and retries missing
ones through the existing Octopus rate governor, alternating with live requests.
Watch the recovery status and Collection health; a long range can take many
polling intervals. An animated indicator shows queued/checking/waiting recovery,
along with the cursor, target and last progress time. Paused recovery explains
whether polling, storage or connection settings need attention. Completion is
shown separately from **finished with gaps**, including after a page reload.
Disconnected live updates show that progress is unknown, not that recovery
finished. The animation respects reduced-motion preferences.
Recovered readings update the graph, coverage, energy and
cost estimates. Missing/unavailable upstream data remains a visible gap, never
an interpolated line or fabricated zero. This recovers Octopus meter data, not
missing Home Assistant history, and cannot restore expired raw archives.

## Excluding EV charging with Home Assistant

If the Ohme integration is already configured in Home Assistant:

1. Find its **Power** entity under **Developer tools > States** (for example
   `sensor.ohme_home_pro_power`). The unit must be W or kW. Ensure Recorder includes
   the entity; Home Assistant normally retains detailed history for ten days.
2. Create a long-lived access token under your Home Assistant profile's
   **Security** tab. An administrator token is required when the dashboard needs
   to verify a stale cached timestamp. Treat it as a credential; do not put it in
   Git or screenshots.
3. In this dashboard's **Settings > Ohme via Home Assistant**, enter the Home
   Assistant URL, entity ID and token. Use a URL reachable from the energy
   monitor's container, such as the Home Assistant LAN IP. Container localhost is
   not your Home Assistant host, and `.local` discovery may not work in Docker.
4. Select **Test Home Assistant** to check power and recent history without
   saving. Enable **Collect Ohme power history** and **Save settings**.
5. In the overview, turn on **Exclude EV charging**. The live
   card stays whole-meter grid demand; history, coverage, peaks and inverter
   threshold totals use the selected basis.

Home Assistant collection is independent of Octopus polling and runs every 60
seconds. It reads current freshness and recent history, then gradually backfills
in fifteen-minute chunks. Initial lookback uses `APP_RECOVERY_LOOKBACK_DAYS`
(default one day); persisted recovery resumes after restart, limited to raw
retention. Watch collection health and joint coverage while backfill progresses.
No charging controls or Home Assistant mutations are issued.

EV exclusion is an estimate based on recorded power changes, not a household
submeter. Missing/unavailable EV history is unknown, not zero. Unreported Home
Assistant outages can leave stale recorded states. Adjusted analysis is limited
to the selected active meter's retained raw readings (normally 30 days); older
grid-only aggregates cannot be subtracted accurately. Solar/batteries can still
make non-EV grid import lower than actual household load.

The Home Assistant token stays in the backend database and private backups,
never in browser responses. HTTPS is recommended for both connections; HTTP
sends credentials unencrypted. Keep dashboard access restricted and protect
backups.

## Octopus tariff pricing

In **Settings > Tariff pricing**, fetch pricing for your saved Octopus account
and selected electricity meter. Enable cost estimates and save settings to add
both costs to the above-6 kW tile and inverter comparison. Pricing refreshes every
six hours while enabled, independently of meter polling; fetching prices never
starts meter collection.

The app discovers dated import agreements and retrieves published unit rates
including VAT. For supported day/night tariffs, including Intelligent Octopus Go,
it uses the account API's household schedule rather than guessing off-peak times.
Rate changes and UK clock changes are respected. These are household-rate
estimates, excluding standing charges, EV-specific smart-charging adjustments and
account-specific discounts, not an Octopus bill or guaranteed battery savings.
Exceeding 6 kW does not itself incur an extra Octopus charge.

Choose **Excluding EV charging** to price supported non-EV grid import using Home
Assistant data. Missing telemetry or EV history is never extrapolated. Missing
applicable rates make costs unavailable, rather than zero or a partial total.
Price coverage refers to observed above-threshold time, separately from telemetry
coverage. The cache covers up to 31 days of raw retention and two days ahead;
older periods may have no applicable prices. Unsupported schedules or
payment-method-specific feeds are reported explicitly, not guessed. See the
[methodology](docs/analytics-methodology.md) for archived-data limitations.

This version upgrades the database to schema 7. Take a consistent private backup
before upgrading; rolling back to an older application requires that backup.

## Documentation

- [Deployment and HTTPS](docs/deployment.md)
- [Portainer installation and configuration](docs/portainer-installation.md)
- [Username-protected Portainer HTTP deployment](docs/portainer-http-installation.md)
- [Automated image publishing and GHCR deployment](docs/registry-deployment.md)
- [Architecture](docs/architecture.md)
- [API contracts](docs/api.md)
- [Analytics methodology](docs/analytics-methodology.md)
- [Operations, backup and recovery](docs/operations.md)
- [Storage limits, retention and gap recovery](docs/storage-management.md)
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

On Linux, use `.venv/bin/python` instead. Browser tests start a loopback-only, synthetic HTTP fixture. The installed `energy-monitor` entry point defaults to HTTPS; certificate-free HTTP requires explicit `APP_TRANSPORT=http` and matching HTTP origins. It validates environment settings and starts exactly one uvicorn worker. Do not use uvicorn reload or additional workers: they would create duplicate collectors and independent session caches.

Regenerate Python locks after deliberate dependency changes:

```text
python -m piptools compile --generate-hashes --strip-extras --allow-unsafe --no-emit-index-url --no-emit-trusted-host --output-file requirements.lock pyproject.toml
python -m piptools compile --generate-hashes --strip-extras --allow-unsafe --no-emit-index-url --no-emit-trusted-host --extra dev --output-file requirements-dev.lock pyproject.toml
```

Use `npm install` to update the frontend lock after changing its manifest. The runtime contains no Node, browser, test runner, ORM, pandas, Redis or external CSS/JS CDN dependency.

The frontend build retains uPlot's license at `/assets/uplot-LICENSE.txt`. Installed Python wheels retain their distribution license metadata in the image.
