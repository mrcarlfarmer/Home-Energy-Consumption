# Analytics methodology

## What is measured

Kraken telemetry describes meter demand in W and cumulative consumed/exported energy in Wh. The application stores integer milliWatts and milliWatt-hours, preserves source timestamps and signed demand, and derives sizing metrics from nonnegative **grid import**.

This is not necessarily total household load: solar or an existing battery can reduce import while appliances still consume power. Instantaneous electrical surges and events shorter than the cloud sampling interval can be missed.

The public Kraken schema's grouping descriptions are inconsistent about representative mean/maximum values. The implementation requests only `TEN_SECONDS` and does not mix grouping levels. Real-meter verification remains necessary before making stronger claims about sampled peak accuracy.

## Time support

For adjacent valid samples at `a` and `b`, hold the earlier demand constant over `[a,b)` only when `0 < b-a <= 30 seconds`. Both bounding demand samples must be valid. Longer gaps and invalid values are unknown. Never extrapolate the newest sample through the current time.

Clip supported segments to each query and bucket boundary. A reading may contribute to the sampled peak even if it has no supported duration yet. Source cadence, rather than the 45-second polling interval, determines the supported-gap policy.

## Energy and thresholds

For supported demand `p` W lasting `d` seconds, import energy is `p*d / 3,600,000` kWh.

For threshold `T`:

- **Time above:** supported seconds where `p > T`, divided by all observed seconds, times 100.
- **Energy while above:** all import energy during those above-threshold periods.
- **Excess energy:** only `(p-T)*d / 3,600,000` during above-threshold periods.

Demand exactly at the rating does not exceed it. Unevenly spaced readings are duration-weighted, not counted equally. If there is no observed duration, averages/percentages/energy are null rather than a misleading zero. Counters are cumulative and are never summed into energy.

The dashboard's **Time above 6 kW** tile shows total supported duration, all import
energy while above 6 kW, and excess energy beyond 6 kW for the selected meter and
window. Duration is displayed in hours, minutes and seconds; energy is in kWh.
These totals use the same summary as the inverter comparison table, including
retained-range alignment. With no observed duration, all three values are
unavailable; observed periods with no exceedance show zero. Missing periods are
not included or extrapolated.

At 8 kW for two fully observed minutes, the 6 kW threshold gives 100% time above, 0.266666667 kWh while above and 0.066666667 kWh excess.

Threshold measures are calculated **before** chart downsampling. Averaging an above-6 kW spike into a below-6 kW chart bucket must not hide the exceedance. The 6 kW rollup retains these integrals independently of the display means.

## Tariff cost estimates

Optional pricing adds two GBP estimates using the selected analysis basis:
all energy while demand exceeds 6 kW, and only energy beyond 6 kW. Each supported
segment is split at applicable dated household import rate boundaries and priced
using Octopus's `value_inc_vat`, in pence/kWh. Decimal arithmetic is used for
accumulation; display rounds to pennies. Negative unit rates remain negative.
Current rates are never substituted backwards for missing historical rates.

The app discovers import agreements for the configured meter. Standard rate
feeds supply dated fixed/variable rates. Daily all-year day/night schedules in
Europe/London or UTC use API-provided times, not a tariff-name/Economy-7 guess.
Half-hour-aligned schedules are expanded in UTC and interpreted locally, so UK
spring/autumn clock changes retain their actual duration. Other schedules and
payment-method-specific feeds are explicitly unsupported rather than inferred.
EV-device-specific prices are not applied to household load.

These are household-rate estimates including VAT, excluding standing charges
and account-specific discounts. Smart-charging adjustments are excluded unless
the explicit Prioritise Savings assumption below is enabled. Grid-only
estimates may therefore differ from the actual billed price of smart EV charging.
Exceeding 6 kW does not itself incur another charge. Neither estimate promises
battery savings: losses, capacity, charging costs and dispatch are not modelled.

Price coverage measures the fraction of **observed above-threshold duration**
with applicable rates. Both monetary totals remain null unless that coverage is
complete; known partial costs are not presented as full totals. Telemetry and EV
coverage remain separate, and missing time is never extrapolated. No observed
duration is unavailable, whereas supported periods with no exceedance cost zero.

An unchanged rate covering a whole retained five-minute/daily aggregate can
price its exact threshold integrals. A rate boundary within an aggregate requires
retained raw readings; otherwise the cost is unsupported because the aggregate
cannot identify when its above-threshold energy occurred. The rolling tariff
cache covers at most 31 days back (bounded by raw retention) and two days ahead.
It does not promise prices for the full ten-year archive.

### Intelligent Octopus Go bonus half-hours

The optional `ev_savings_from` UK-local date confirms that every recorded Ohme
charge from that date uses **Prioritise Savings**, with no Max/Boost or
allowance-exhausted charging. The setting is deliberately opt-in, not inferred
from power shape. It applies only to dated Intelligent (`INTELLI-` product)
agreements with supported API household day/night schedules. It is not a general
rule for Economy 7 or other tariffs.

Any positive recorded EV power within an eligible settlement half-hour awards
that half-hour an **estimated** household bonus rate. A ten-minute charge at
18:07-18:17 therefore awards 18:00-18:30. This does not claim that thirty minutes
of the six-hour allowance were used: the app relies on Prioritise Savings to
enforce that allowance and does not independently reconstruct it. It cannot
prove the charger mode or supplier eligibility from power alone. Standby and
preconditioning must not be mistaken for qualifying charging when configuring
this assumption.

The shared resolver reads the entire UTC-aligned half-hour even for a shorter
query. A charge at 18:17 can therefore determine the rate for a view ending at
18:05. Repeated local half-hours during autumn DST remain distinct UTC slots.
The activation date is interpreted in Europe/London; before it, published
household schedules remain unchanged. Actual dated night prices are cached
separately from expanded household schedules, so daytime-only queries do not
substitute the day price or today's night price for a missing historic price.
Rate/agreement boundaries inside a slot retain their exact prices and dates.

One supported positive interval is sufficient to award an estimated bonus;
unknown minutes elsewhere in that half-hour do not revoke that evidence.
Conversely, no observed charge is not proof of no charging: complete valid EV
coverage of the half-hour is required to retain its ordinary daytime price.
Unknown/unavailable history, expired history and unfinished/future slots leave
the affected effective rate unresolved. Missing dated night prices also leave
an inferred bonus unpriced. Both threshold cost totals remain null when any
observed above-threshold energy intersects an unresolved rate; observed periods
without exceedance can still cost zero. Telemetry and EV coverage stay separate
from above-threshold price coverage.

Effective rates are independent of EV exclusion. Removing EV power never removes
the earned household bonus. Both cost measures use the rate applicable to the
actual supported demand segment; no change is made to energy, peaks, duration
or excess arithmetic. For a fully observed half-hour of 8 kW grid import at
8p/kWh, the 4 kWh while above 6 kW costs GBP 0.32 and the 1 kWh excess costs
GBP 0.08. All costs remain sampled-data estimates, not supplier billing or
predictions of savings from a home battery.

The existing source-scoped EV history is authoritative for this inference and
retains its Recorder/freshness limitations. Corrections change rates and costs
without rewriting the published schedule. A base-price refresh cannot delete EV
history. No extra collection, charger controls or mode/plan entities are needed
for this explicitly assumed policy. Max/Boost, independent allowance accounting,
mixed EV/household billing rates and supplier reconciliation are not supported
by this mode; disable the assumption if they apply.

The resolver uses retained EV intervals, not a charging-shaped household demand
curve. Older archives without these intervals cannot reconstruct eligibility.
Existing rules still require raw detail when a rate boundary splits an
aggregate. No new promise of exact multi-year adjusted costs is made.

## EV exclusion

The optional Home Assistant integration reads the Ohme power entity's current
state and Recorder history. Recorder stores changes, not a uniform thirty-second
sample stream. For each successfully fetched window, hold a recorded EV value
until its next change or the window end. The initial history state establishes
the value at the left boundary; absence of that state leaves initial time unknown.
`unknown`/`unavailable` states and absent history create unsupported intervals.
No synthetic ten-second EV readings are fabricated.

For recent collection, cap the window at the current sensor's `last_reported`
timestamp (falling back to `last_updated`), and require a valid report within two
minutes. If Home Assistant returns a stale cached REST `last_reported`, a read-only
template obtains the power and timestamp together from the current state. This
fallback requires an administrator token; if it is denied, the reading remains
unknown until it can be verified. Never extend recent coverage from a
stale/unavailable sensor. Historical Recorder data cannot prove that the
charger was online throughout every
unchanged state: unreported outages can leave stale values. This limitation is
explicit in the adjusted responses.

Ohme polls every thirty seconds, whereas meter readings and their bounded holds
use separate timestamps. Subtracting these sources across a charging start/stop
can leave a brief full-EV-power residual or an artificially low household value.
For exclusion only, mark the sixty seconds before and after each contiguous,
known zero-to-positive or positive-to-zero EV change as unknown. This allows one
Ohme polling period plus the maximum thirty-second meter hold in either direction;
it is an uncertainty allowance, not a measured clock correction or a guarantee
against longer reporting delays. Overlapping guards are combined. Unavailable
states, gaps and unchanged values do not invent charging transitions. Ordinary
positive power variations are still recorded-state estimates.

The guard is applied before all adjusted integration and peak calculations, not
as a chart filter or a rule discarding values above 6 kW. Any genuine household
event inside a guard is also unavailable; events outside it retain the existing
arithmetic. Guarded time contributes no adjusted energy, threshold duration or
cost, and reduces joint coverage. Trailing fifteen-minute values crossing a guard
are unavailable until their full window is supported again. Fetch neighbouring
EV evidence outside each query/day/cache boundary before deriving the guard, so
changing zoom, resolution or calendar splits cannot change support. Stored EV
history and whole-meter analytics remain unchanged. The bonus-rate resolver still
uses original charging evidence, including charging inside exclusion guards.

Intersect supported meter segments with the remaining valid EV intervals, split
at every EV change, and use `max(0, signed_grid_demand - EV_power)` before applying
the same energy and strict-threshold arithmetic. Grid gap/null rules remain unchanged.
Coverage and time-above percentages use only jointly usable duration. A known
zero EV reading supports subtraction; missing EV data does not.

Derived peaks include usable meter readings and supported intersection
boundaries outside transition guards, so a supported EV power change within a
meter segment cannot hide an adjusted peak. Daily summaries, chart buckets and
the full-coverage trailing fifteen-minute curve share this calculation. The live
card remains whole-meter grid demand.
This is **non-EV grid import**, not total household load when solar/batteries are
present.

Adjusted metrics are computed from raw readings and retained EV intervals, not
from pre-averaged grid chart values or by subtracting EV kWh from excess kWh.
EV history is tied to the configured URL/entity/meter identity, revision-fenced,
and updated when overlapping history is corrected. It expires with raw retention
(normally thirty days). Older grid-only five-minute/daily archives remain
unchanged and are not eligible for EV exclusion.

## Peaks, windows and calendar days

Daily peaks are maximum valid sampled import, with the earliest timestamp breaking ties. Calendar boundaries use the requested IANA timezone; UK DST days may be 23 or 25 hours. First/last partial-day coverage uses the actual clipped duration.

The expanded daily peak table covers five calendar dates ending at the viewed
window's exclusive end, newest first. An end exactly at midnight belongs to the
preceding day. The final day is clipped at the viewed end unless archived
resolution requires a whole calendar day. This table is independent of the
graph duration, uses the selected meter and analysis basis, and includes
unavailable days without inventing peaks. UK DST days retain 23/25-hour coverage
denominators. Expired EV history remains unknown.

The daily table displays only the estimated cost of excess energy beyond 6 kW,
using the same tariff calculation as the selected-window summary but clipped
to that calendar day's actual bounds. These estimates are independent of the
graph duration and honor the selected meter and EV basis.
No observed duration or incomplete applicable price coverage yields unavailable
costs, not zero; supported days without exceedance cost zero when pricing is
enabled and fetched. The daily telemetry coverage remains separate from price
coverage, which is included in each cost cell's explanation. Missing-rate and
pricing-disabled explanations appear below the table heading.

The browser plots observed mean and sampled peak only. The history API retains
trailing 15-minute demand for compatibility: the import integral over the
previous 900 seconds divided by 900, evaluated at each chart bucket's end.
A full-window value requires 100% supported coverage; otherwise it is null.

## Storage and display

Recent raw readings are authoritative and isolated by device/native timestamp.
Corrected/late readings invalidate adjacent affected retained rollups. Five-minute
summaries contain observed duration, import integral, sampled peak and threshold
duration/energy. Rebuilds are atomic and versioned. After raw expiry these summaries
are durable records, not disposable caches; older history is reduced to local-day
archives. See [retention, alignment and archive limitations](storage-management.md).

For large ranges, APIs sum exact retained integrals and recompute partial boundaries
only where raw readings remain. Otherwise endpoints expand to complete retained
buckets/days and responses report actual bounds and warnings. Means are weighted
by observed duration; maxima are not averaged. Dirty caches are not served as current.

The browser shows coverage prominently, breaks unsupported lines, suppresses incomplete bucket-mean lines and displays sampled peaks separately. Custom date inputs and chart axis labels use the browser timezone; daily summaries use the configured timezone, shown explicitly. The 6 kW threshold is a comparison reference, not an electrical design or connection-approval decision.

The graph's auto-refresh toggle is independent of collection. Pausing it keeps
the existing analysis and zoom stable while live readings continue. Manual refresh,
range changes and the EV toggle still request new analysis. Window arrows shift
the exact elapsed duration and pin the resulting dates; presets follow the latest
window again. They do not alter any stored measurement.

Off-peak shading comes from applicable cached `ECO7_NIGHT` household rate
intervals and, when opted in, separately styled `EV_BONUS` intervals from the
same effective-rate resolver used for costs. Observed charging and unresolved
rates have distinct strips and a timestamp/price table. All indicators clip to
the visible time scale, including during chart zoom. Bonus rates remain present
in both EV bases. No schedule is inferred outside the cache or for another meter.
Pricing revisions incorporate configuration, meter/EV history and cached prices;
the browser retries concurrent revisions rather than mixing costs and shading.
The dashed 6 kW
reference is drawn only within the visible power scale; it does not force the
vertical axis to include 6 kW or change sampled values.

The graph can request another Octopus recovery pass over retained raw history.
Only original upstream readings can close gaps; no interpolation or zero fill is
introduced. After recovery, the normal gap rules and rollup invalidation apply,
so coverage, energy, thresholds and tariff estimates share the repaired data.
EV-excluded gaps may also require Home Assistant history, which this meter-only
recovery action does not fetch.
