# energysim

Download **hourly** energy data from a [Home Assistant](https://www.home-assistant.io/)
instance over a chosen date range, and write it to CSV in a shape that's ready for offline
analysis — home-battery simulation, cost calculations, etc.

The tool auto-discovers which sensors to read from your **Energy dashboard** configuration,
so you don't have to list entity IDs by hand.

## How it works

Home Assistant keeps **long-term statistics** at hourly resolution forever. This is the only
clean source of true 1-hour energy data (the REST history API returns raw state changes that
would need resampling). energysim talks to HA's WebSocket API and:

1. Authenticates with your long-lived access token.
2. Reads your instance's timezone (`get_config`).
3. Reads your Energy dashboard config (`energy/get_prefs`) to discover grid import/export,
   solar production, battery charge/discharge, gas and water sources, plus any per-device
   consumption sensors.
4. Reads the units of each statistic (`recorder/list_statistic_ids`).
5. Downloads hourly statistics (`recorder/statistics_during_period`, `period: "hour"`),
   using each sensor's per-hour `change` value (kWh used in that hour).

## Setup

Requires Python 3.11+.

```bash
cd energysim
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Create a long-lived access token in Home Assistant
(*Profile → Security → Long-lived access tokens → Create token*), then:

```bash
cp .env.example .env
# edit .env and paste the token into HA_TOKEN
```

`.env` keys:

| Key             | Required | Notes                                                              |
|-----------------|----------|--------------------------------------------------------------------|
| `HA_TOKEN`      | yes      | Long-lived access token.                                           |
| `HA_URL`        | no       | Default base URL, used as the default when prompted.               |
| `HA_VERIFY_SSL` | no       | `false` to skip TLS verification (self-signed certs). Default `true`. |

## Usage

```bash
energysim
# or: python -m energysim
```

You'll be prompted for the HA base URL (e.g. `https://homeassistant.local:8123`) and a start
and end date (`YYYY-MM-DD`, both **inclusive**, interpreted in your HA instance's timezone).

Output is written to `data/energy_<start>_<end>.csv` plus a sidecar
`data/energy_<start>_<end>.metadata.json` describing which entity each column came from.

### Options (skip the prompts)

```bash
energysim --url https://homeassistant.local:8123 --start 2026-01-01 --end 2026-01-31
energysim --debug          # also print discovered sources + raw stats, write data/debug.json
energysim --out ./exports  # change the output directory
```

If sources you expect are missing from the CSV, run with `--debug`: it prints every
discovered energy source and, for each statistic, how many hourly buckets Home Assistant
returned (and writes the same to `data/debug.json`). A source listed with `NO DATA` means HA
has no statistics for that entity in the requested range.

## Output format

One row per hour. Columns are only emitted for sources that exist in your instance:

| Column                  | Meaning                                                  |
|-------------------------|----------------------------------------------------------|
| `timestamp_local`       | Hour start in your HA timezone (ISO 8601)                |
| `timestamp_utc`         | Hour start in UTC (ISO 8601)                             |
| `grid_import_kwh`       | Energy drawn from the grid                                |
| `grid_export_kwh`       | Energy fed back to the grid                               |
| `solar_production_kwh`  | Solar production                                          |
| `battery_charge_kwh`    | Energy stored into the home battery                       |
| `battery_discharge_kwh` | Energy delivered by the home battery                      |
| `gas_*` / `water_*`     | Gas/water consumption (unit from HA, e.g. `m3`)           |
| `<device>_kwh`          | Per-device consumption sensors from the Energy dashboard |
| `home_consumption_kwh`  | Derived household electricity load (see below)            |

`home_consumption_kwh` is computed the same way the Energy dashboard derives "home usage":

```
home_consumption = grid_import + solar_production + battery_discharge
                 - grid_export - battery_charge
```

This is the household load — the quantity a battery-simulation would charge/discharge
against.

### Notes

- Sources of the same role (e.g. two grid meters) are summed into one column.
- Values are normalised to kWh (`Wh`/`MWh` are converted); gas/water keep their HA unit.
- The hour grid is aligned to your **local** calendar days, and stays correct across daylight
  saving switches. (Timezones with a half-hour offset, e.g. `+05:30`, won't align hour
  buckets to local clock hours — fine for CET/CEST.)
- Hours with no data in HA (e.g. instance downtime) are filled with `0.0`; the run prints how
  many such hours were filled.

---

# batterysim — home battery simulation

`batterysim` reads a CSV produced by `energysim` and simulates how a home battery would have
changed your grid import/export over the same hours. It writes a new hourly CSV, renders a
chart, and prints import/export totals **with vs without** the battery so you can compare.

## Usage

```bash
batterysim
# prompts for the input CSV (defaults to the newest data/energy_*.csv),
# the battery capacity (kWh) and the round-trip efficiency.
```

Or non-interactively:

```bash
batterysim --input data/energy_2025-03-01_2026-02-28.csv --capacity 10 --efficiency 90
```

| Flag           | Meaning                                                          |
|----------------|------------------------------------------------------------------|
| `--input`      | Input CSV (default: newest `data/energy_*.csv`).                 |
| `--capacity`   | Battery capacity in kWh.                                         |
| `--efficiency` | Round-trip efficiency as a percentage (`90`) or fraction (`0.9`).|
| `--out`        | Output directory (default: the input file's directory).         |
| `--price-import-normal` / `--price-import-reduced`   | **Fixed contract:** import price per kWh, per tariff. |
| `--price-export-normal` / `--price-export-reduced`   | **Fixed contract:** export price per kWh, per tariff. |
| `--no-costs`   | Skip the cost calculation.                                       |
| `--prices`     | **Dynamic contract:** path to a prices CSV from `energyprices`.  |
| `--fetch-prices` | **Dynamic contract:** download market prices for the data's range instead of `--prices`. |
| `--markup`     | Dynamic supplier markup, EUR/kWh (default `0.02`).              |
| `--feed-in-factor` | Share of the bare market price paid for export (default `1.0`). |
| `--energy-tax` | 2027 energy tax, EUR/kWh excl. BTW (default `0.0916`, see below).|
| `--feed-in-incl-vat` | Add BTW to the export feed-in compensation.               |

Fixed-contract prices are prompted if not given as flags (press Enter at the first price
prompt to skip costs); provide either all four `--price-*` values or none. The dynamic
contract is enabled by `--prices`/`--fetch-prices` (or, interactively, by answering the
"Dynamic contract prices" prompt). The two contracts are shown side by side so you can
compare them, each with and without the battery.

## Model

For each hour the input gives the grid import (deficit) and export (surplus) you had
**without** a battery. The simulated battery captures surplus that would have been exported
and uses it to cover deficit that would have been imported:

```
charged   = min(grid_export, capacity - soc)     # charge efficiency assumed 100%
soc      += charged
deliverable = soc * efficiency                    # round-trip loss applied on discharge
delivered   = min(grid_import, deliverable)
soc        -= delivered / efficiency
```

Assumptions:

- The battery **starts empty**; charging is 100% efficient and the **round-trip efficiency is
  applied on discharge** (the loss happens when you consume from the battery).
- Each hour charges first, then discharges, so same-hour surplus can serve same-hour load.
- The battery only charges from **solar surplus** and only discharges to **cover load** — no
  charging from the grid, and **no charge/discharge power limit** (capacity-bound only).

### Pluggable charge/discharge strategies

The *decision* of how much to charge/discharge each hour is a swappable component
(`BatteryStrategy` in `src/energysim/simulate.py`); the engine only does the physical
accounting (state of charge, capacity/power limits, efficiency losses) and turns a strategy's
intent into the resulting grid flows. The only strategy shipped today is `ReactiveStrategy`,
which reproduces the behaviour above.

The plumbing is already in place for smarter models: a strategy receives the day-ahead
`import_price`/`export_price` series for the whole period (so it can look ahead), and
`BatteryParams` carries `allow_grid_charge`, `allow_grid_discharge` and per-step power limits.
A future price-aware strategy can therefore **charge from the grid when prices are low and
discharge onto the grid when they are high** — the `battery_grid_charge_kwh` /
`battery_grid_discharge_kwh` output columns and the dynamic pricing already account for such
grid trades. (Today, with the reactive model, those columns are always `0`.)

## Tariffs: energy and costs per tariff

Every hour is classified into one of two tariff periods:

- **Normal** tariff: Mon–Fri, **07:00–23:00** (local time).
- **Reduced** tariff: 23:00–07:00 on weekdays, all weekend, and Dutch national holidays
  (Nieuwjaarsdag, Tweede Paasdag, Koningsdag, Hemelvaartsdag, Tweede Pinksterdag, and both
  Kerstdagen — billed reduced for the whole day). Holidays are computed automatically (no
  data file needed).

The **kWh totals split by tariff** (normal / reduced / total) are **always** shown — for
**Without battery**, **With battery** and the **Difference**, separately for import and
export — even when no prices are given.

When prices are supplied, the same breakdown is also produced in money: import and export
each have their own normal/reduced rate, and you additionally get the net cost
(import − export) and the battery's annual saving. Both breakdowns are written to
`summary.json` (`energy_by_tariff` always, `costs` when prices are given).

> Regional note: in Noord-Brabant and Limburg the reduced period starts at 21:00. Change
> `NORMAL_END_HOUR` in `src/energysim/pricing.py` if that applies to you.

## Dynamic contract (real hourly market prices)

Alongside the fixed two-tier contract above, `batterysim` can price a **dynamic
("dynamisch") contract** against the real day-ahead market price for each hour, and show the
two contracts side by side.

### Getting the price data — `energyprices`

Historical hourly Dutch day-ahead prices come from the free, no-token
[EnergyZero API](https://api.energyzero.nl) (the same feed many Home Assistant dynamic-price
integrations use). Download a price file once and reuse it:

```bash
energyprices --input data/energy_2025-03-01_2026-02-28.csv   # match an energy CSV's range
energyprices --start 2025-03-01 --end 2026-02-28             # or give the range directly
```

This writes `data/prices_<start>_<end>.csv` (columns `timestamp_utc`,
`market_price_eur_kwh` — the **bare** wholesale price) plus a metadata sidecar. The Dutch
day-ahead market moved to 15-minute resolution on 1 Oct 2025; the prices are resampled to the
hourly grid the simulation uses. `batterysim --fetch-prices` does the same download inline.

### How the price is built up

Each hour's all-in price is layered on top of the market price:

```
import price = (market + markup + energy_tax) × (1 + BTW)
export credit = market × feed_in_factor   [× (1 + BTW) with --feed-in-incl-vat]
```

Only the **variable, per-kWh** costs are modelled. Fixed standing charges (netbeheer,
vastrecht, the annual tax rebate) are identical across every scenario — fixed vs dynamic,
with vs without battery — so they cancel out of any comparison and are deliberately excluded.

### Assumptions (modelling fiscal year **2027**)

- **No net metering (salderen).** From 1 Jan 2027 salderen is abolished, so import and export
  are priced independently with no annual netting — which is exactly how the cost maths
  already work (export revenue is simply subtracted from import cost). Export earns a feed-in
  compensation (`--feed-in-factor`, default the full bare market price).
- **BTW = 21%** (unchanged for 2027).
- **Energy tax** is applied as a single 2027 value to every hour. ⚠️ The official 2027 rate
  is not set until Belastingplan 2027 (~Oct 2026), so the default `0.0916` EUR/kWh (the 2026
  value) is a **placeholder** — override it with `--energy-tax` once published. For reference:
  2025 = `0.10154`, 2026 = `0.0916` (EUR/kWh, excl. BTW).
- The historical 2025–26 market prices stand in as a **proxy** for 2027 market behaviour
  (2027 prices can't exist yet).
- Prices can be **negative** (~6–7% of hours): exporting then costs money, which the
  arithmetic captures with signed prices.

These assumptions make the dynamic-vs-fixed and with-vs-without-battery **comparison** robust;
treat the absolute euro totals as indicative, not as a bill predictor.

Defaults live in `src/energysim/dynamic_pricing.py` (`DEFAULT_ENERGY_TAX_2027`,
`DEFAULT_MARKUP_EUR`, `VAT_RATE`).

## Output

For an input `energy_<range>.csv` with capacity `C` and efficiency `E` you get three files in
the output directory:

- `energy_<range>_battery_<C>kWh_<E>pct.csv` — the original columns plus
  `battery_charge_kwh`, `battery_discharge_kwh`, `battery_soc_kwh`, `battery_loss_kwh`,
  `grid_import_sim_kwh`, `grid_export_sim_kwh` (the with-battery grid flows), and
  `battery_grid_charge_kwh` / `battery_grid_discharge_kwh` (battery↔grid trades, `0` for the
  reactive model). With a dynamic contract it also gains `market_price_eur_kwh`,
  `import_price_eur_kwh`, `export_price_eur_kwh` and the per-hour with-battery
  `import_cost_eur` / `export_cost_eur`.
- `…​.png` — a chart with grid totals (with vs without), battery state of charge over time, and
  a monthly import/export breakdown; with a dynamic contract a fourth panel shows the monthly
  average import / export / market prices.
- `…​.summary.json` — energy totals, reductions, self-sufficiency, the per-tariff energy split,
  the fixed-contract `costs` (when fixed prices are given) and the `dynamic_costs` breakdown
  (when a dynamic contract is priced).

## Tests

```bash
pip install -e '.[test]'
pytest
```
