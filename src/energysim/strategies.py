"""Smart, price-aware battery charge/discharge strategies.

These plug into the engine in :mod:`energysim.simulate` via the ``BatteryStrategy`` protocol.
Both shipped here are **rolling-horizon** strategies that mirror how real systems (EMHASS,
Predbat, Victron Dynamic ESS) behave: day-ahead prices publish ~13:00 for the next day, so at
any moment a strategy may only use prices through the end of *tomorrow*, re-planning each day
and carrying the state of charge forward (a backtest-flavoured Model Predictive Control).

- :class:`ThresholdStrategy` — a transparent rule of thumb: charge (incl. from the grid, if
  allowed) during the window's cheapest hours, discharge to cover load during its most
  expensive hours, self-consume solar always. No solver.
- :class:`OptimalStrategy` — solves the cost-minimising schedule for each window as a linear
  program (PuLP/CBC). With the realistic day-ahead window this is the best a perfect-foresight
  controller could do given real information — i.e. the savings ceiling.

A crucial simplification: within a window the load/solar are taken as **known** (a perfect
load/solar forecast). Only price foresight is realistically limited. Modelling load/solar
forecast error is a future extension.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from energysim.simulate import (
    EXPORT_COL,
    EXPORT_PRICE_COL,
    IMPORT_COL,
    IMPORT_PRICE_COL,
    BatteryParams,
    SimulationError,
    StepContext,
    StepDecision,
    apply_step,
)
from energysim.timeutil import local_timestamps

DEFAULT_PUBLISH_HOUR = 13  # local hour at which the next day's day-ahead prices are known


@dataclass
class RollingHorizonStrategy:
    """Base class handling the rolling day-ahead window, re-planning and SoC carry-over.

    Subclasses implement :meth:`_solve_window`, which decides one visible window given the
    starting SoC. This class commits each window's plan up to the next re-plan point, advances
    SoC with the engine's own :func:`apply_step`, and stores the full per-hour schedule.
    """

    publish_hour: int = DEFAULT_PUBLISH_HOUR
    cycle_cost: float = 0.0  # EUR/kWh of throughput; discourages over-cycling
    _schedule: list[StepDecision] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def plan(self, df: pd.DataFrame, params: BatteryParams) -> None:
        for col in (IMPORT_PRICE_COL, EXPORT_PRICE_COL):
            if col not in df.columns:
                raise SimulationError(
                    f"{type(self).__name__} needs hourly prices — run batterysim with "
                    "--prices or --fetch-prices."
                )

        surplus = df[EXPORT_COL].fillna(0.0).to_numpy(dtype=float)
        deficit = df[IMPORT_COL].fillna(0.0).to_numpy(dtype=float)
        imp = df[IMPORT_PRICE_COL].to_numpy(dtype=float)
        exp = df[EXPORT_PRICE_COL].to_numpy(dtype=float)

        ts = local_timestamps(df)
        n = len(df)
        midnight = ts.dt.normalize()
        day = (midnight - midnight.min()).dt.days.to_numpy()
        hour = ts.dt.hour.to_numpy()

        # Re-plan at the start of the data and at each day's publish hour.
        replan = sorted({0, *(i for i in range(n) if hour[i] == self.publish_hour)})

        schedule = [StepDecision() for _ in range(n)]
        soc = 0.0
        for k, r in enumerate(replan):
            commit_end = replan[k + 1] if k + 1 < len(replan) else n
            # Prices are known through the end of today, plus tomorrow once past the publish
            # hour. The visible window runs from r to the end of that last known day.
            end_day = day[r] + (1 if hour[r] >= self.publish_hour else 0)
            w_end = r
            while w_end < n and day[w_end] <= end_day:
                w_end += 1

            decisions = self._solve_window(
                surplus[r:w_end], deficit[r:w_end], imp[r:w_end], exp[r:w_end], soc, params,
                final=w_end >= n,
            )
            for j in range(r, commit_end):
                d = decisions[j - r]
                schedule[j] = d
                soc = apply_step(soc, surplus[j], deficit[j], d, params).new_soc

        self._schedule = schedule

    def decide(self, ctx: StepContext) -> StepDecision:
        if self._schedule is None:
            raise SimulationError("Strategy.plan() must run before decide().")
        return self._schedule[ctx.index]

    def _solve_window(
        self,
        surplus: np.ndarray,
        deficit: np.ndarray,
        imp: np.ndarray,
        exp: np.ndarray,
        soc_init: float,
        params: BatteryParams,
        final: bool,
    ) -> list[StepDecision]:
        raise NotImplementedError


@dataclass
class ThresholdStrategy(RollingHorizonStrategy):
    """A transparent rule-of-thumb baseline.

    Per rolling window: self-consume solar always; treat the cheapest ``charge_percentile`` of
    hours as a "charge & hold" band (grid-charge there if allowed and profitable, and *don't*
    spend the battery — importing is cheap anyway); in every other hour discharge the battery
    to cover load (as reactive self-consumption does). Grid charging is capped to the load that
    can still be profitably served from the battery later in the window, so it never buys energy
    it won't use. Selling to the grid (when enabled) happens in the highest export-price hours.

    Note: for a solar-heavy home this simple rule typically only ties plain self-consumption —
    most of the easy value is already in reactive self-consumption, and capturing more needs
    the look-ahead optimisation of :class:`OptimalStrategy`. It shines mainly on grid arbitrage
    in low-solar periods.
    """

    charge_percentile: float = 25.0
    discharge_percentile: float = 75.0  # which future hours count as "peak" for grid charging
    sell_percentile: float = 75.0

    def _solve_window(self, surplus, deficit, imp, exp, soc_init, params, final=False):
        eff = params.efficiency
        cap = params.capacity_kwh
        m = len(imp)
        charge_thr = float(np.percentile(imp, self.charge_percentile))
        peak_thr = float(np.percentile(imp, self.discharge_percentile))
        sell_thr = float(np.percentile(exp, self.sell_percentile)) if m else 0.0

        soc = soc_init
        out: list[StepDecision] = []
        for t in range(m):
            cs = surplus[t]  # absorb free solar surplus every hour
            cg = dl = dg = 0.0

            if imp[t] <= charge_thr:
                # Cheap "charge & hold" band: top up from the grid toward the load we can still
                # profitably serve from the battery later (capped so we never buy unused energy),
                # and don't discharge here — importing now is cheap.
                if params.allow_grid_charge:
                    future_imp = imp[t + 1:]
                    future_def = deficit[t + 1:]
                    peak = future_imp >= peak_thr
                    if peak.any() and float(future_imp[peak].max()) * eff > imp[t] + self.cycle_cost:
                        target = min(cap, float(future_def[peak].sum()) / eff)
                        cg = max(0.0, target - soc)
            else:
                # Otherwise behave like reactive self-consumption: cover load from the battery.
                dl = deficit[t]

            # Sell to the grid in the highest export-price hours (when enabled).
            if params.allow_grid_discharge and exp[t] >= sell_thr and exp[t] > 0:
                dg = cap

            res = apply_step(
                soc, surplus[t], deficit[t],
                StepDecision(cs, cg, dl, dg), params,
            )
            soc = res.new_soc
            out.append(
                StepDecision(
                    res.charge_from_surplus, res.charge_from_grid,
                    res.discharge_to_load, res.discharge_to_grid,
                )
            )
        return out


@dataclass
class OptimalStrategy(RollingHorizonStrategy):
    """Cost-minimising schedule per window, solved as a linear program (PuLP/CBC).

    Minimises ``Σ import·import_price − export·export_price`` (+ optional cycle cost) over the
    visible window, minus a terminal value on left-over SoC so the battery isn't dumped for
    free at the artificial window edge. No integer variables are needed because in this pricing
    model the import price always exceeds the export price, so simultaneous charge+discharge is
    never profitable.
    """

    def _solve_window(self, surplus, deficit, imp, exp, soc_init, params, final=False):
        import pulp

        m = len(imp)
        if m == 0:
            return []
        eff = params.efficiency
        cap = params.capacity_kwh

        # PuLP 3.3 pre-announces 4.0 API changes (LpVariable/PULP_CBC_CMD) we don't act on yet
        # — the dependency is pinned <4, so silence that internal deprecation noise here.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return self._build_and_solve(pulp, m, surplus, deficit, imp, exp, soc_init, params, final)

    def _build_and_solve(self, pulp, m, surplus, deficit, imp, exp, soc_init, params, final):
        eff = params.efficiency
        cap = params.capacity_kwh

        prob = pulp.LpProblem("battery_window", pulp.LpMinimize)
        cg_ub = None if params.allow_grid_charge else 0.0
        dg_ub = None if params.allow_grid_discharge else 0.0
        cs = [pulp.LpVariable(f"cs{t}", 0, float(surplus[t])) for t in range(m)]
        cg = [pulp.LpVariable(f"cg{t}", 0, cg_ub) for t in range(m)]
        dl = [pulp.LpVariable(f"dl{t}", 0, float(deficit[t])) for t in range(m)]
        dg = [pulp.LpVariable(f"dg{t}", 0, dg_ub) for t in range(m)]
        soc = [pulp.LpVariable(f"soc{t}", 0, cap) for t in range(m)]

        for t in range(m):
            prev = soc_init if t == 0 else soc[t - 1]
            prob += soc[t] == prev + (cs[t] + cg[t]) - (dl[t] + dg[t]) / eff
            if params.max_charge_kwh_per_step is not None:
                prob += cs[t] + cg[t] <= params.max_charge_kwh_per_step
            if params.max_discharge_kwh_per_step is not None:
                prob += dl[t] + dg[t] <= params.max_discharge_kwh_per_step

        net_cost = pulp.lpSum(
            (deficit[t] - dl[t] + cg[t]) * float(imp[t])
            - (surplus[t] - cs[t] + dg[t]) * float(exp[t])
            for t in range(m)
        )
        throughput = pulp.lpSum(cs[t] + cg[t] + dl[t] + dg[t] for t in range(m))
        # Value energy left in the battery at the window edge so the LP doesn't dump it for
        # free — UNLESS this window reaches the true end of the data, where leftover SoC is
        # genuinely worthless (valuing it there makes the LP hoard and overstates cost).
        residual_value = 0.0 if final else soc[m - 1] * float(np.mean(imp)) * eff
        prob += net_cost + self.cycle_cost * throughput - residual_value

        prob.solve(pulp.PULP_CBC_CMD(msg=0))
        if pulp.LpStatus[prob.status] != "Optimal":
            return [StepDecision() for _ in range(m)]

        def val(v) -> float:
            x = v.value()
            return max(0.0, x) if x is not None else 0.0

        return [
            StepDecision(val(cs[t]), val(cg[t]), val(dl[t]), val(dg[t])) for t in range(m)
        ]


STRATEGIES = {"reactive", "threshold", "optimal"}


def make_strategy(name: str, *, cycle_cost: float = 0.0, publish_hour: int = DEFAULT_PUBLISH_HOUR):
    """Build a strategy by name. ``reactive`` is imported lazily to avoid a cycle."""
    if name == "reactive":
        from energysim.simulate import ReactiveStrategy

        return ReactiveStrategy()
    if name == "threshold":
        return ThresholdStrategy(publish_hour=publish_hour, cycle_cost=cycle_cost)
    if name == "optimal":
        return OptimalStrategy(publish_hour=publish_hour, cycle_cost=cycle_cost)
    raise SimulationError(f"Unknown strategy {name!r}; choose from {sorted(STRATEGIES)}.")
