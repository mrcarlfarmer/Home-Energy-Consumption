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

These are household-rate estimates including VAT, excluding standing charges,
EV-specific smart-charging adjustments and account-specific discounts. Grid-only
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

Intersect supported meter segments with valid EV intervals, split at every EV
change, and use `max(0, signed_grid_demand - EV_power)` before applying the same
energy and strict-threshold arithmetic. Grid gap/null rules remain unchanged.
Coverage and time-above percentages use only jointly usable duration. A known
zero EV reading supports subtraction; missing EV data does not.

Derived peaks include usable meter readings and supported intersection
boundaries, so an EV power change within a meter segment cannot hide an adjusted
peak. Daily summaries, chart buckets and the full-coverage trailing fifteen-minute
curve share this calculation. The live card remains whole-meter grid demand.
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

Off-peak shading comes only from applicable cached `ECO7_NIGHT` household rate
intervals, clipped to the visible time scale, including during chart zoom.
No schedule is inferred outside the cache or for another meter. The dashed 6 kW
reference is drawn only within the visible power scale; it does not force the
vertical axis to include 6 kW or change sampled values.

The graph can request another Octopus recovery pass over retained raw history.
Only original upstream readings can close gaps; no interpolation or zero fill is
introduced. After recovery, the normal gap rules and rollup invalidation apply,
so coverage, energy, thresholds and tariff estimates share the repaired data.
EV-excluded gaps may also require Home Assistant history, which this meter-only
recovery action does not fetch.
