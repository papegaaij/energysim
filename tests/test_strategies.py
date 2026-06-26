"""Tests for the smart battery strategies (threshold heuristic + LP optimal)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from energysim.dynamic_pricing import (
    DynamicContract,
    add_price_columns,
    attribute_savings,
    compute_dynamic_costs,
)
from energysim.simulate import BatteryParams, ReactiveStrategy, simulate_battery
from energysim.strategies import (
    OptimalStrategy,
    ThresholdStrategy,
    make_strategy,
)

CONTRACT = DynamicContract(energy_tax_eur=0.0916, markup_eur=0.02, feed_in_factor=1.0)


def _priced_df(market, load, solar=None, start="2025-01-01T00:00"):
    """Build a priced energy frame from per-hour market price, load and (optional) solar."""
    market = np.asarray(market, dtype=float)
    load = np.asarray(load, dtype=float)
    solar = np.zeros(len(market)) if solar is None else np.asarray(solar, dtype=float)
    times = pd.date_range(start, periods=len(market), freq="h", tz="Europe/Amsterdam")
    net = load - solar
    df = pd.DataFrame(
        {
            "timestamp_local": [t.isoformat() for t in times],
            "timestamp_utc": [t.tz_convert("UTC").isoformat() for t in times],
            "grid_import_kwh": np.maximum(net, 0.0),
            "grid_export_kwh": np.maximum(-net, 0.0),
        }
    )
    prices = pd.DataFrame({"timestamp_utc": df["timestamp_utc"], "market_price_eur_kwh": market})
    return add_price_columns(df, prices, CONTRACT)


def _arbitrage_market(days=3):
    """Cheap nights (00-06), expensive evenings (17-21), medium otherwise."""
    hours = np.arange(24 * days) % 24
    m = np.full(24 * days, 0.08)
    m[(hours >= 0) & (hours < 6)] = 0.0
    m[(hours >= 17) & (hours <= 21)] = 0.30
    return m


def _net(df, params, name):
    out, summary = simulate_battery(df, params, make_strategy(name), prices=df)
    return compute_dynamic_costs(out, CONTRACT).net_with_eur, out, summary


def test_smart_strategies_beat_reactive_on_arbitrage():
    # No solar, so reactive does nothing and any saving must come from grid arbitrage.
    market = _arbitrage_market(days=3)
    df = _priced_df(market, load=np.full(len(market), 0.5))
    params = BatteryParams(5.0, 0.9, allow_grid_charge=True)

    reactive, _, _ = _net(df, params, "reactive")
    threshold, _, _ = _net(df, params, "threshold")
    optimal, _, _ = _net(df, params, "optimal")

    assert optimal <= threshold + 1e-6      # the LP is the ceiling
    assert threshold <= reactive + 1e-6     # the heuristic helps on clean arbitrage
    assert optimal < reactive - 1e-6        # and there is a real, positive saving


def test_optimal_is_feasible_and_empties_by_the_end():
    market = _arbitrage_market(days=3)
    df = _priced_df(market, load=np.full(len(market), 0.5))
    params = BatteryParams(5.0, 0.9, allow_grid_charge=True)
    _, out, summary = _net(df, params, "optimal")

    soc = out["battery_soc_kwh"]
    assert soc.min() >= -1e-6 and soc.max() <= params.capacity_kwh + 1e-6  # within bounds
    assert summary.total_grid_charged_kwh > 0                              # it used the grid
    assert soc.iloc[-1] < 0.5  # terminal value is zeroed at the data end, so it doesn't hoard


def test_grid_trading_respects_flags():
    market = _arbitrage_market(days=2)
    df = _priced_df(market, load=np.full(len(market), 0.5))
    no_grid = BatteryParams(5.0, 0.9, allow_grid_charge=False, allow_grid_discharge=False)
    for name in ("threshold", "optimal"):
        _, out, _ = _net(df, no_grid, name)
        assert out["battery_grid_charge_kwh"].abs().max() < 1e-9
        assert out["battery_grid_discharge_kwh"].abs().max() < 1e-9


def test_limited_day_ahead_foresight():
    # Identical day 0, different day 1: decisions committed before the first 13:00 re-plan
    # (which can only see day 0) must be identical — proving foresight is window-limited.
    market = _arbitrage_market(days=2)
    df_a = _priced_df(market, load=np.full(len(market), 0.5))
    market_b = market.copy()
    market_b[24:] = 0.50  # make all of day 1 very expensive
    df_b = _priced_df(market_b, load=np.full(len(market), 0.5))

    params = BatteryParams(5.0, 0.9, allow_grid_charge=True)
    _, out_a, _ = _net(df_a, params, "optimal")
    _, out_b, _ = _net(df_b, params, "optimal")

    # The first re-plan after start is at 13:00, so hours 0..12 are decided from day 0 only.
    a = out_a["battery_grid_charge_kwh"].to_numpy()[:13]
    b = out_b["battery_grid_charge_kwh"].to_numpy()[:13]
    assert np.allclose(a, b)
    assert a.sum() > 0  # and there genuinely was night charging to compare


def test_savings_attribution_reconciles_and_isolates_self_consumption():
    # A solar home: surplus midday (11-14), deficit otherwise, so reactive self-consumes.
    hours = np.arange(72) % 24
    load = np.full(72, 0.5)
    solar = np.where((hours >= 11) & (hours <= 14), 1.0, 0.0)
    df = _priced_df(_arbitrage_market(days=3), load=load, solar=solar)

    # Reactive: every euro of saving must be self-consumption (no grid trading at all).
    out, _ = simulate_battery(df, BatteryParams(5.0, 0.9), make_strategy("reactive"), prices=df)
    cb = compute_dynamic_costs(out, CONTRACT)
    attr = attribute_savings(out, 0.9)
    assert attr.grid_arbitrage_eur == pytest.approx(0.0, abs=1e-9)
    assert attr.sell_back_eur == pytest.approx(0.0, abs=1e-9)
    assert attr.total_eur == pytest.approx(cb.savings_eur, abs=0.05)

    # Optimal with full grid trading: the three channels still reconcile to the total saving.
    params = BatteryParams(5.0, 0.9, allow_grid_charge=True, allow_grid_discharge=True)
    out, _ = simulate_battery(df, params, make_strategy("optimal"), prices=df)
    cb = compute_dynamic_costs(out, CONTRACT)
    attr = attribute_savings(out, 0.9)
    assert attr.total_eur == pytest.approx(cb.savings_eur, abs=0.05)


def test_curtail_negative_export():
    # Midday solar surplus during negative-price hours, no battery to absorb it.
    hours = np.arange(48) % 24
    load = np.full(48, 0.5)
    solar = np.where((hours >= 11) & (hours <= 14), 2.0, 0.0)
    market = np.full(48, 0.10)
    market[(hours >= 11) & (hours <= 14)] = -0.05  # negative midday
    df = _priced_df(market, load, solar)
    neg = df["export_price_eur_kwh"].to_numpy() < 0

    base = BatteryParams(0.0, 0.9)
    out0, _ = simulate_battery(df, base, make_strategy("reactive"), prices=df)
    curtailed = BatteryParams(0.0, 0.9, curtail_negative_export=True)
    out1, summ1 = simulate_battery(df, curtailed, make_strategy("reactive"), prices=df)

    # Export is zeroed exactly in the negative-price hours, and untouched elsewhere.
    assert (out1["grid_export_sim_kwh"].to_numpy()[neg] == 0.0).all()
    assert np.allclose(
        out1["grid_export_sim_kwh"].to_numpy()[~neg],
        out0["grid_export_sim_kwh"].to_numpy()[~neg],
    )
    assert summ1.total_curtailed_kwh > 0

    c0 = compute_dynamic_costs(out0, CONTRACT)
    c1 = compute_dynamic_costs(out1, CONTRACT)
    assert c1.net_with_eur < c0.net_with_eur  # curtailing the loss-making export saves money

    attr = attribute_savings(out1, 0.9)
    assert attr.curtailment_eur > 0
    assert attr.total_eur == pytest.approx(c1.savings_eur, abs=0.05)


def test_make_strategy():
    assert isinstance(make_strategy("reactive"), ReactiveStrategy)
    assert isinstance(make_strategy("threshold"), ThresholdStrategy)
    assert isinstance(make_strategy("optimal"), OptimalStrategy)
    with pytest.raises(Exception):
        make_strategy("nonsense")


def test_smart_strategy_requires_prices():
    # A frame with no price columns must fail clearly when a smart strategy plans.
    df = pd.DataFrame({"grid_import_kwh": [1.0, 0.0], "grid_export_kwh": [0.0, 1.0],
                       "timestamp_local": ["2025-01-01T00:00:00+01:00", "2025-01-01T01:00:00+01:00"],
                       "timestamp_utc": ["2024-12-31T23:00:00+00:00", "2025-01-01T00:00:00+00:00"]})
    with pytest.raises(Exception):
        simulate_battery(df, BatteryParams(5.0, 0.9, allow_grid_charge=True), ThresholdStrategy())
