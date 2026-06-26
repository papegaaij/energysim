"""Tests for the dynamic-contract pricing model."""

from __future__ import annotations

import pandas as pd
import pytest

from energysim.dynamic_pricing import (
    DynamicContract,
    add_cost_columns,
    add_price_columns,
    compute_dynamic_costs,
)
from energysim.simulate import BatteryParams, simulate_battery


def _energy_frame():
    return pd.DataFrame(
        {
            "timestamp_utc": [
                "2025-03-01T00:00:00+00:00",
                "2025-03-01T01:00:00+00:00",
                "2025-03-01T02:00:00+00:00",
            ],
            "grid_import_kwh": [2.0, 0.0, 1.0],
            "grid_export_kwh": [0.0, 3.0, 0.0],
        }
    )


def _prices(values):
    return pd.DataFrame(
        {
            "timestamp_utc": [
                "2025-03-01T00:00:00+00:00",
                "2025-03-01T01:00:00+00:00",
                "2025-03-01T02:00:00+00:00",
            ],
            "market_price_eur_kwh": values,
        }
    )


def test_contract_price_formulas():
    c = DynamicContract(energy_tax_eur=0.0916, markup_eur=0.02, feed_in_factor=1.0)
    assert c.import_price(0.10) == pytest.approx((0.10 + 0.02 + 0.0916) * 1.21)
    assert c.export_price(0.10) == pytest.approx(0.10)  # feed-in = bare market by default
    assert c.export_price(-0.05) == pytest.approx(-0.05)  # negative prices stay signed

    c_vat = DynamicContract(feed_in_factor=0.5, feed_in_incl_vat=True)
    assert c_vat.export_price(0.20) == pytest.approx(0.20 * 0.5 * 1.21)


def test_add_price_columns_aligns_and_builds_prices():
    c = DynamicContract(energy_tax_eur=0.0916, markup_eur=0.02, feed_in_factor=1.0)
    # Prices deliberately out of order to prove the merge aligns on timestamp, not row order.
    prices = _prices([0.10, -0.05, 0.20]).iloc[::-1].reset_index(drop=True)
    out = add_price_columns(_energy_frame(), prices, c)

    assert out["market_price_eur_kwh"].tolist() == pytest.approx([0.10, -0.05, 0.20])
    assert out["import_price_eur_kwh"].tolist() == pytest.approx(
        [(0.10 + 0.1116) * 1.21, (-0.05 + 0.1116) * 1.21, (0.20 + 0.1116) * 1.21]
    )
    assert out["export_price_eur_kwh"].tolist() == pytest.approx([0.10, -0.05, 0.20])


def test_missing_price_hour_is_filled_with_warning():
    c = DynamicContract()
    prices = _prices([0.10, 0.10, 0.10]).iloc[:2]  # drop the last hour
    with pytest.warns(UserWarning):
        out = add_price_columns(_energy_frame(), prices, c)
    assert out["market_price_eur_kwh"].notna().all()


def test_compute_dynamic_costs_with_negative_hour():
    c = DynamicContract(energy_tax_eur=0.0916, markup_eur=0.02, feed_in_factor=1.0)
    priced = add_price_columns(_energy_frame(), _prices([0.10, -0.05, 0.20]), c)
    result, _ = simulate_battery(priced, BatteryParams(5.0, 0.9), prices=priced)
    cb = compute_dynamic_costs(result, c)

    imp = lambda m: (m + 0.1116) * 1.21
    # Without battery: import 2@h0 + 1@h2; export 3@h1 (negative price -> you pay to export).
    assert cb.import_without_eur == pytest.approx(2 * imp(0.10) + 1 * imp(0.20))
    assert cb.export_without_eur == pytest.approx(3 * -0.05)
    # With battery: h1 surplus charges, h2 deficit served from battery -> only h0 imports.
    assert cb.import_with_eur == pytest.approx(2 * imp(0.10))
    assert cb.export_with_eur == pytest.approx(0.0)
    assert cb.savings_eur == pytest.approx(cb.net_without_eur - cb.net_with_eur)


def test_add_cost_columns():
    c = DynamicContract(energy_tax_eur=0.0916, markup_eur=0.02)
    priced = add_price_columns(_energy_frame(), _prices([0.10, 0.10, 0.10]), c)
    result, _ = simulate_battery(priced, BatteryParams(5.0, 0.9), prices=priced)
    out = add_cost_columns(result)
    assert "import_cost_eur" in out.columns
    assert "export_cost_eur" in out.columns
    # Per-hour with-battery import cost = sim import kWh * all-in import price.
    expected = out["grid_import_sim_kwh"] * out["import_price_eur_kwh"]
    assert out["import_cost_eur"].tolist() == pytest.approx(expected.tolist())
