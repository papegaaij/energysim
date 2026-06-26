"""Tests for the battery engine and the reactive strategy."""

from __future__ import annotations

import pandas as pd
import pytest

from energysim.simulate import (
    BatteryParams,
    ReactiveStrategy,
    SimulationError,
    simulate_battery,
)


def _frame(imports, exports):
    return pd.DataFrame({"grid_import_kwh": imports, "grid_export_kwh": exports})


def test_reactive_charge_discharge_and_loss():
    # h0: import 2 (battery empty, nothing to deliver)
    # h1: export 3 -> charge 3 (100% in)
    # h2: import 1 -> deliver 1 (loss applies on the way out)
    df = _frame([2.0, 0.0, 1.0], [0.0, 3.0, 0.0])
    result, summary = simulate_battery(df, BatteryParams(capacity_kwh=5.0, efficiency=0.9))

    assert result["battery_charge_kwh"].tolist() == [0.0, 3.0, 0.0]
    assert result["battery_discharge_kwh"].tolist() == [0.0, 0.0, 1.0]
    # Output columns are rounded to 6 dp by the engine, so allow that absolute tolerance.
    assert result["battery_soc_kwh"].tolist() == pytest.approx([0.0, 3.0, 3.0 - 1.0 / 0.9], abs=1e-6)
    assert result["battery_loss_kwh"].tolist() == pytest.approx([0.0, 0.0, 1.0 / 0.9 - 1.0], abs=1e-6)
    assert result["grid_import_sim_kwh"].tolist() == [2.0, 0.0, 0.0]
    assert result["grid_export_sim_kwh"].tolist() == [0.0, 0.0, 0.0]

    # Reactive never trades with the grid.
    assert result["battery_grid_charge_kwh"].tolist() == [0.0, 0.0, 0.0]
    assert result["battery_grid_discharge_kwh"].tolist() == [0.0, 0.0, 0.0]
    assert summary.total_grid_charged_kwh == 0.0
    assert summary.total_grid_discharged_kwh == 0.0

    assert summary.import_without_kwh == 3.0
    assert summary.import_with_kwh == 2.0
    assert summary.total_delivered_kwh == pytest.approx(1.0)
    assert summary.total_loss_kwh == pytest.approx(1.0 / 0.9 - 1.0)


def test_capacity_caps_charge():
    df = _frame([0.0], [3.0])
    result, _ = simulate_battery(df, BatteryParams(capacity_kwh=2.0, efficiency=1.0))
    assert result["battery_charge_kwh"].tolist() == [2.0]  # capped at capacity
    assert result["grid_export_sim_kwh"].tolist() == [1.0]  # surplus beyond capacity exported


def test_discharge_limited_by_deliverable_energy():
    # Charge 2 kWh then ask for 5 kWh: only soc*efficiency is deliverable.
    df = _frame([0.0, 5.0], [2.0, 0.0])
    result, _ = simulate_battery(df, BatteryParams(capacity_kwh=10.0, efficiency=0.8))
    assert result["battery_discharge_kwh"].tolist() == pytest.approx([0.0, 2.0 * 0.8])
    assert result["battery_soc_kwh"].tolist() == pytest.approx([2.0, 0.0])


def test_invalid_efficiency_and_missing_columns():
    with pytest.raises(SimulationError):
        simulate_battery(_frame([1.0], [0.0]), BatteryParams(1.0, efficiency=0.0))
    with pytest.raises(SimulationError):
        simulate_battery(pd.DataFrame({"grid_import_kwh": [1.0]}), BatteryParams(1.0, 0.9))


def test_strategy_defaults_to_reactive():
    df = _frame([1.0], [1.0])
    a, _ = simulate_battery(df, BatteryParams(5.0, 0.9))
    b, _ = simulate_battery(df, BatteryParams(5.0, 0.9), ReactiveStrategy())
    pd.testing.assert_frame_equal(a, b)
