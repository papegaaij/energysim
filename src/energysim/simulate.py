"""Battery simulation engine.

Pure functions only — no file or console I/O — so this stays easy to test and to reuse
from a future price-calculation module (see pricing.py / dynamic_pricing.py).

Model (see the project README for the rationale):
- The battery starts empty.
- Charge efficiency is 100%; the round-trip efficiency is applied on discharge (the loss
  happens when energy is consumed from the battery).
- Each hour the battery first charges, then discharges.

The *decision* of how much to charge/discharge each hour is delegated to a pluggable
``BatteryStrategy``. The engine here only owns the physical accounting (state of charge,
capacity/power limits, efficiency losses) and turns a strategy's intent into the resulting
grid flows. The only strategy shipped today is :class:`ReactiveStrategy`, which reproduces
the historical behaviour exactly (charge from solar surplus, discharge to cover household
load, never trade with the grid). Future strategies can use the day-ahead ``import_price`` /
``export_price`` series exposed on :class:`StepContext` to charge from the grid when prices
are low and discharge onto the grid when they are high.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

import numpy as np
import pandas as pd

# Columns the simulator reads from the input CSV.
IMPORT_COL = "grid_import_kwh"
EXPORT_COL = "grid_export_kwh"
CONSUMPTION_COL = "home_consumption_kwh"

# Optional per-hour price columns a price-aware strategy can consume (added by
# dynamic_pricing.add_price_columns before the simulation runs).
IMPORT_PRICE_COL = "import_price_eur_kwh"
EXPORT_PRICE_COL = "export_price_eur_kwh"

# Columns the simulator adds.
SIM_COLUMNS = [
    "battery_charge_kwh",
    "battery_discharge_kwh",
    "battery_soc_kwh",
    "battery_loss_kwh",
    "grid_import_sim_kwh",
    "grid_export_sim_kwh",
    # Battery <-> grid trading (always 0 for the reactive model; populated by future
    # price-aware strategies). Kept in the output so trades are visible and priceable.
    "battery_grid_charge_kwh",
    "battery_grid_discharge_kwh",
]


class SimulationError(Exception):
    """Raised when the input data can't be simulated."""


@dataclass
class BatteryParams:
    """Physical battery parameters and the permissions a strategy operates under.

    The four forward-looking knobs default to today's behaviour, so the reactive model is
    unaffected: no power limit and no grid trading.
    """

    capacity_kwh: float
    efficiency: float  # round-trip, applied on discharge
    max_charge_kwh_per_step: float | None = None
    max_discharge_kwh_per_step: float | None = None
    allow_grid_charge: bool = False
    allow_grid_discharge: bool = False


@dataclass
class StepContext:
    """Everything a strategy may look at to decide one hour's charge/discharge.

    ``surplus`` and ``deficit`` are this hour's grid export/import *before* the battery.
    ``import_price`` / ``export_price`` are the full per-hour price series (or None) so a
    strategy can look ahead from ``index``; the reactive model ignores them.
    """

    index: int
    soc: float
    surplus: float
    deficit: float
    params: BatteryParams
    import_price: np.ndarray | None = None
    export_price: np.ndarray | None = None


@dataclass
class StepDecision:
    """A strategy's intent for one hour, as four energy amounts (kWh).

    The engine clamps each to what is physically possible (capacity, available surplus,
    deliverable charge, power limits) and to the ``allow_grid_*`` permissions, so a strategy
    may state its intent without re-checking the constraints.
    """

    charge_from_surplus: float = 0.0
    charge_from_grid: float = 0.0
    discharge_to_load: float = 0.0
    discharge_to_grid: float = 0.0


class BatteryStrategy(Protocol):
    """Decides one hour's charge/discharge given the current :class:`StepContext`."""

    def decide(self, ctx: StepContext) -> StepDecision: ...


@dataclass
class ReactiveStrategy:
    """The only strategy shipped today: fully reactive, no grid trading.

    Charge from all available solar surplus, then discharge to cover all household demand.
    The engine caps both to what fits / is deliverable, so stating the raw amounts is enough.
    """

    def decide(self, ctx: StepContext) -> StepDecision:
        return StepDecision(
            charge_from_surplus=ctx.surplus,
            discharge_to_load=ctx.deficit,
        )


@dataclass
class Summary:
    capacity_kwh: float
    efficiency: float
    import_without_kwh: float
    import_with_kwh: float
    export_without_kwh: float
    export_with_kwh: float
    import_reduction_kwh: float
    import_reduction_pct: float
    export_reduction_kwh: float
    export_reduction_pct: float
    total_charged_kwh: float
    total_delivered_kwh: float
    total_loss_kwh: float
    end_soc_kwh: float
    # Battery <-> grid energy (0 for the reactive model).
    total_grid_charged_kwh: float = 0.0
    total_grid_discharged_kwh: float = 0.0
    # Self-sufficiency = share of household load not drawn from the grid (None if the input
    # has no home_consumption_kwh column).
    self_sufficiency_without: float | None = None
    self_sufficiency_with: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _pct(reduction: float, base: float) -> float:
    return (reduction / base * 100.0) if base else 0.0


def _price_array(prices: pd.DataFrame | None, column: str, n: int) -> np.ndarray | None:
    if prices is None or column not in prices.columns:
        return None
    arr = prices[column].to_numpy(dtype=float)
    return arr if len(arr) == n else None


def simulate_battery(
    df: pd.DataFrame,
    params: BatteryParams,
    strategy: BatteryStrategy | None = None,
    prices: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, Summary]:
    """Simulate a battery over the hourly data under a charge/discharge strategy.

    `df` must contain `grid_import_kwh` and `grid_export_kwh`; all input columns are kept and
    the SIM_COLUMNS are appended. `strategy` defaults to :class:`ReactiveStrategy`. `prices`,
    if given, is a frame aligned to `df` carrying `import_price_eur_kwh`/`export_price_eur_kwh`
    that price-aware strategies can look ahead over (the reactive model ignores it). Returns
    the augmented frame plus a Summary.
    """
    if strategy is None:
        strategy = ReactiveStrategy()

    missing = [c for c in (IMPORT_COL, EXPORT_COL) if c not in df.columns]
    if missing:
        raise SimulationError(
            f"Input CSV is missing required column(s): {', '.join(missing)}. "
            "Re-download with the energysim tool so grid import/export are included."
        )
    if not 0 < params.efficiency <= 1:
        raise SimulationError("Efficiency must be a fraction in (0, 1].")
    if params.capacity_kwh < 0:
        raise SimulationError("Capacity must be >= 0.")

    imports = df[IMPORT_COL].fillna(0.0).to_numpy(dtype=float)
    exports = df[EXPORT_COL].fillna(0.0).to_numpy(dtype=float)
    n = len(df)

    import_price = _price_array(prices, IMPORT_PRICE_COL, n)
    export_price = _price_array(prices, EXPORT_PRICE_COL, n)

    capacity = params.capacity_kwh
    efficiency = params.efficiency
    max_charge = params.max_charge_kwh_per_step
    max_discharge = params.max_discharge_kwh_per_step

    charge = [0.0] * n
    discharge = [0.0] * n
    soc_series = [0.0] * n
    loss = [0.0] * n
    import_sim = [0.0] * n
    export_sim = [0.0] * n
    grid_charge = [0.0] * n
    grid_discharge = [0.0] * n

    soc = 0.0
    for i in range(n):
        surplus = exports[i]
        deficit = imports[i]

        decision = strategy.decide(
            StepContext(
                index=i,
                soc=soc,
                surplus=surplus,
                deficit=deficit,
                params=params,
                import_price=import_price,
                export_price=export_price,
            )
        )

        # --- Charge: surplus first (free), then grid (only if allowed). ---
        from_surplus = min(max(decision.charge_from_surplus, 0.0), surplus)
        from_grid = max(decision.charge_from_grid, 0.0) if params.allow_grid_charge else 0.0
        room = max(capacity - soc, 0.0)
        allowed_charge = min(from_surplus + from_grid, room)
        if max_charge is not None:
            allowed_charge = min(allowed_charge, max_charge)
        from_surplus = min(from_surplus, allowed_charge)
        from_grid = allowed_charge - from_surplus
        soc += allowed_charge

        # --- Discharge: load first, then grid (only if allowed). `delivered` is energy
        #     leaving the battery terminals; the round-trip loss is applied here. ---
        to_load = min(max(decision.discharge_to_load, 0.0), deficit)
        to_grid = max(decision.discharge_to_grid, 0.0) if params.allow_grid_discharge else 0.0
        deliverable = soc * efficiency
        allowed_discharge = min(to_load + to_grid, deliverable)
        if max_discharge is not None:
            allowed_discharge = min(allowed_discharge, max_discharge)
        to_load = min(to_load, allowed_discharge)
        to_grid = allowed_discharge - to_load
        delivered = allowed_discharge
        soc -= delivered / efficiency

        charge[i] = allowed_charge
        discharge[i] = delivered
        soc_series[i] = soc
        loss[i] = delivered * (1.0 / efficiency - 1.0)
        grid_charge[i] = from_grid
        grid_discharge[i] = to_grid
        import_sim[i] = deficit - to_load + from_grid
        export_sim[i] = surplus - from_surplus + to_grid

    out = df.copy()
    out["battery_charge_kwh"] = charge
    out["battery_discharge_kwh"] = discharge
    out["battery_soc_kwh"] = soc_series
    out["battery_loss_kwh"] = loss
    out["grid_import_sim_kwh"] = import_sim
    out["grid_export_sim_kwh"] = export_sim
    out["battery_grid_charge_kwh"] = grid_charge
    out["battery_grid_discharge_kwh"] = grid_discharge
    out[SIM_COLUMNS] = out[SIM_COLUMNS].round(6)

    import_without = float(imports.sum())
    import_with = float(sum(import_sim))
    export_without = float(exports.sum())
    export_with = float(sum(export_sim))

    consumption = (
        float(df[CONSUMPTION_COL].fillna(0.0).sum())
        if CONSUMPTION_COL in df.columns
        else None
    )

    summary = Summary(
        capacity_kwh=capacity,
        efficiency=efficiency,
        import_without_kwh=import_without,
        import_with_kwh=import_with,
        export_without_kwh=export_without,
        export_with_kwh=export_with,
        import_reduction_kwh=import_without - import_with,
        import_reduction_pct=_pct(import_without - import_with, import_without),
        export_reduction_kwh=export_without - export_with,
        export_reduction_pct=_pct(export_without - export_with, export_without),
        total_charged_kwh=float(sum(charge)),
        total_delivered_kwh=float(sum(discharge)),
        total_loss_kwh=float(sum(loss)),
        end_soc_kwh=soc,
        total_grid_charged_kwh=float(sum(grid_charge)),
        total_grid_discharged_kwh=float(sum(grid_discharge)),
        self_sufficiency_without=(
            (1.0 - import_without / consumption) if consumption else None
        ),
        self_sufficiency_with=(
            (1.0 - import_with / consumption) if consumption else None
        ),
    )
    return out, summary
