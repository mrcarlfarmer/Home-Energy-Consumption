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

At 4 kW for two fully observed minutes, the 3.68 kW preset gives 100% time above, 0.133333333 kWh while above and 0.010666667 kWh excess.

Threshold measures are calculated **before** chart downsampling. Averaging a 6 kW spike into a sub-3.68 kW chart bucket must not hide the exceedance. The three predefined threshold rollups retain these integrals independently of the display means.

## Peaks, windows and calendar days

Daily peaks are maximum valid sampled import, with the earliest timestamp breaking ties. Calendar boundaries use the requested IANA timezone; UK DST days may be 23 or 25 hours. First/last partial-day coverage uses the actual clipped duration.

Trailing 15-minute demand is the import integral over the previous 900 seconds divided by 900, evaluated at each returned chart bucket's end. A full-window value is available only with 100% supported coverage; otherwise it is null with a coverage percentage. The API retrieves context before the requested chart start.

## Storage and display

Raw readings are authoritative and retained indefinitely, isolated by device/native timestamp. Corrected/late readings invalidate adjacent affected rollups. Five-minute caches contain observed duration, import integral, sampled peak and threshold duration/energy. Rebuilds are atomic and versioned.

For large ranges, APIs sum exact cached integrals and recompute partial boundaries from raw readings. Means are weighted by observed duration; maxima are not averaged. Dirty caches are not served as current.

The browser shows coverage prominently, breaks unsupported lines, suppresses incomplete bucket-mean lines and displays sampled peaks separately. Custom date inputs and chart axis labels use the browser timezone; daily summaries use the configured timezone, shown explicitly. The 3.68 kW preset is a comparison reference, not a G98/G99 connection decision.
