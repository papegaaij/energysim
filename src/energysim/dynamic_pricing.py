"""Dynamic ("dynamisch") contract pricing from real hourly market prices.

A Dutch dynamic contract bills each kWh at that hour's wholesale market price plus a fixed
supplier markup, the per-kWh energy tax, and 21% BTW on top of all three::

    all-in import price = (market + markup + energy_tax) * (1 + BTW)

This module models a single **fiscal year 2027** contract (see the project README for the
rationale): from 1 Jan 2027 net metering (salderen) is abolished, so import and export are
priced independently with no annual netting — exactly how the existing two-tier model in
:mod:`energysim.pricing` already works. Export earns a feed-in compensation expressed as a
share of the bare market price.

Only the per-kWh variable costs are modelled. Fixed standing charges (netbeheer, vastrecht,
the annual tax rebate) are identical across every scenario — fixed vs dynamic, with vs
without battery — so they cancel out of any comparison and are deliberately excluded.

The historical 2025-26 market prices act as a proxy for 2027 market behaviour (2027 prices
cannot exist yet); this is a stated assumption.
"""

from __future__ import annotations

import warnings
from collections import deque
from dataclasses import dataclass

import pandas as pd

from energysim.prices import PRICE_COL, TIMESTAMP_COL
from energysim.simulate import EXPORT_PRICE_COL, IMPORT_PRICE_COL

# --- Fiscal parameters for the modelled year (single set, applied to every hour). ---
MODEL_YEAR = 2027
VAT_RATE = 0.21  # BTW; unchanged for 2027.

# Energy tax (energiebelasting) on electricity, bracket 1 (<10,000 kWh), EUR/kWh excl. BTW.
# Reference values for published years; the 2027 rate is NOT set until Belastingplan 2027
# (~Oct 2026), so we default to the 2026 figure as a clearly-flagged placeholder. Override
# it with --energy-tax once the official 2027 rate is known.
ENERGY_TAX_EXCL_VAT = {2025: 0.10154, 2026: 0.0916}
DEFAULT_ENERGY_TAX_2027 = ENERGY_TAX_EXCL_VAT[2026]  # PLACEHOLDER (unofficial)

# Supplier markup (inkoopvergoeding) on imported kWh, EUR/kWh. Typically 1-3 ct.
DEFAULT_MARKUP_EUR = 0.02

# Column added alongside the all-in prices for transparency/charting.
MARKET_PRICE_COL = "market_price_eur_kwh"


@dataclass
class DynamicContract:
    """The configurable parts of a 2027 dynamic contract."""

    energy_tax_eur: float = DEFAULT_ENERGY_TAX_2027  # EUR/kWh, excl. BTW
    markup_eur: float = DEFAULT_MARKUP_EUR           # EUR/kWh, excl. BTW
    vat_rate: float = VAT_RATE
    feed_in_factor: float = 1.0          # share of the bare market price paid for export
    feed_in_incl_vat: bool = False       # whether BTW is added to the feed-in compensation

    def import_price(self, market_price: float) -> float:
        return (market_price + self.markup_eur + self.energy_tax_eur) * (1.0 + self.vat_rate)

    def export_price(self, market_price: float) -> float:
        mult = (1.0 + self.vat_rate) if self.feed_in_incl_vat else 1.0
        return market_price * self.feed_in_factor * mult


def add_price_columns(
    df: pd.DataFrame, prices: pd.DataFrame, contract: DynamicContract
) -> pd.DataFrame:
    """Merge hourly market prices onto `df` and add the per-hour all-in price columns.

    Aligns on UTC timestamp (robust to string formatting). Missing hours are forward/back
    filled and a warning is emitted. Returns a new frame with `market_price_eur_kwh`,
    `import_price_eur_kwh` and `export_price_eur_kwh` columns added.
    """
    if TIMESTAMP_COL not in df.columns:
        raise ValueError(f"Energy data is missing the {TIMESTAMP_COL!r} column.")

    market_by_time = (
        pd.Series(
            prices[PRICE_COL].to_numpy(dtype=float),
            index=pd.to_datetime(prices[TIMESTAMP_COL], utc=True),
        )
        .sort_index()
    )
    market_by_time = market_by_time[~market_by_time.index.duplicated(keep="first")]

    keys = pd.to_datetime(df[TIMESTAMP_COL], utc=True)
    market = keys.map(market_by_time)

    missing = int(market.isna().sum())
    if missing:
        warnings.warn(
            f"{missing} of {len(df)} hours had no market price; filled from neighbours.",
            stacklevel=2,
        )
        market = market.ffill().bfill()

    market_arr = market.to_numpy(dtype=float)
    out = df.copy()
    out[MARKET_PRICE_COL] = market_arr
    out[IMPORT_PRICE_COL] = contract.import_price(market_arr)
    out[EXPORT_PRICE_COL] = contract.export_price(market_arr)
    return out


@dataclass
class DynamicCostBreakdown:
    contract: DynamicContract
    import_without_eur: float
    import_with_eur: float
    export_without_eur: float
    export_with_eur: float
    import_without_kwh: float
    import_with_kwh: float
    export_without_kwh: float
    export_with_kwh: float
    avg_import_price_eur_kwh: float  # volume-weighted, all-in
    avg_export_price_eur_kwh: float
    currency: str = "EUR"

    @property
    def net_without_eur(self) -> float:
        return self.import_without_eur - self.export_without_eur

    @property
    def net_with_eur(self) -> float:
        return self.import_with_eur - self.export_with_eur

    @property
    def savings_eur(self) -> float:
        return self.net_without_eur - self.net_with_eur

    def as_dict(self) -> dict:
        return {
            "currency": self.currency,
            "contract": {
                "model_year": MODEL_YEAR,
                "energy_tax_eur_excl_vat": round(self.contract.energy_tax_eur, 5),
                "markup_eur": round(self.contract.markup_eur, 5),
                "vat_rate": self.contract.vat_rate,
                "feed_in_factor": self.contract.feed_in_factor,
                "feed_in_incl_vat": self.contract.feed_in_incl_vat,
            },
            "avg_import_price_eur_kwh": round(self.avg_import_price_eur_kwh, 5),
            "avg_export_price_eur_kwh": round(self.avg_export_price_eur_kwh, 5),
            "import": {
                "without_eur": round(self.import_without_eur, 2),
                "with_eur": round(self.import_with_eur, 2),
                "without_kwh": round(self.import_without_kwh, 3),
                "with_kwh": round(self.import_with_kwh, 3),
            },
            "export": {
                "without_eur": round(self.export_without_eur, 2),
                "with_eur": round(self.export_with_eur, 2),
                "without_kwh": round(self.export_without_kwh, 3),
                "with_kwh": round(self.export_with_kwh, 3),
            },
            "net_without_eur": round(self.net_without_eur, 2),
            "net_with_eur": round(self.net_with_eur, 2),
            "savings_eur": round(self.savings_eur, 2),
        }


def _col(df: pd.DataFrame, name: str):
    return df[name].fillna(0.0).to_numpy(dtype=float)


def compute_dynamic_costs(
    df: pd.DataFrame, contract: DynamicContract
) -> DynamicCostBreakdown:
    """Cost a simulated frame under the dynamic contract.

    `df` must already carry the price columns (from :func:`add_price_columns`) and the
    simulator's `grid_*_sim_kwh` columns. Each hour's kWh is multiplied by that hour's price
    and summed, for both the without-battery and with-battery flows.
    """
    for required in (IMPORT_PRICE_COL, EXPORT_PRICE_COL, "grid_import_sim_kwh"):
        if required not in df.columns:
            raise ValueError(f"Frame is missing required column {required!r}.")

    imp_price = _col(df, IMPORT_PRICE_COL)
    exp_price = _col(df, EXPORT_PRICE_COL)

    iw_kwh = _col(df, "grid_import_kwh")
    iwb_kwh = _col(df, "grid_import_sim_kwh")
    ew_kwh = _col(df, "grid_export_kwh")
    ewb_kwh = _col(df, "grid_export_sim_kwh")

    import_without = float((iw_kwh * imp_price).sum())
    import_with = float((iwb_kwh * imp_price).sum())
    export_without = float((ew_kwh * exp_price).sum())
    export_with = float((ewb_kwh * exp_price).sum())

    total_import_kwh = float(iw_kwh.sum())
    total_export_kwh = float(ew_kwh.sum())

    return DynamicCostBreakdown(
        contract=contract,
        import_without_eur=import_without,
        import_with_eur=import_with,
        export_without_eur=export_without,
        export_with_eur=export_with,
        import_without_kwh=total_import_kwh,
        import_with_kwh=float(iwb_kwh.sum()),
        export_without_kwh=total_export_kwh,
        export_with_kwh=float(ewb_kwh.sum()),
        avg_import_price_eur_kwh=(import_without / total_import_kwh) if total_import_kwh else 0.0,
        avg_export_price_eur_kwh=(export_without / total_export_kwh) if total_export_kwh else 0.0,
    )


def add_cost_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Add per-hour with-battery `import_cost_eur` / `export_cost_eur` columns for the CSV."""
    out = df.copy()
    out["import_cost_eur"] = (_col(df, "grid_import_sim_kwh") * _col(df, IMPORT_PRICE_COL)).round(6)
    out["export_cost_eur"] = (_col(df, "grid_export_sim_kwh") * _col(df, EXPORT_PRICE_COL)).round(6)
    return out


@dataclass
class SavingsAttribution:
    """The dynamic-contract battery saving (vs no battery), split by the channel it came from.

    The three channels plus the leftover term sum **exactly** to the dynamic ``savings_eur``:

    - ``self_consumption_eur`` — stored *solar* used to cover load (avoided import minus the
      export it would otherwise have earned).
    - ``grid_arbitrage_eur`` — energy bought *from the grid* in cheap hours and used to cover
      load later (avoided import minus the grid-charging cost).
    - ``sell_back_eur`` — energy discharged to the grid (sale revenue minus its charging cost).
    - ``curtailment_eur`` — fee avoided by throttling solar instead of exporting at a negative
      price (not a battery action, but part of the dynamic saving).
    - ``unused_end_soc_eur`` — charge cost of energy still in the battery at the end (≤ 0).
    """

    self_consumption_eur: float
    grid_arbitrage_eur: float
    sell_back_eur: float
    curtailment_eur: float
    unused_end_soc_eur: float

    @property
    def total_eur(self) -> float:
        return (
            self.self_consumption_eur
            + self.grid_arbitrage_eur
            + self.sell_back_eur
            + self.curtailment_eur
            + self.unused_end_soc_eur
        )

    def as_dict(self) -> dict:
        return {
            "self_consumption_eur": round(self.self_consumption_eur, 2),
            "grid_arbitrage_eur": round(self.grid_arbitrage_eur, 2),
            "sell_back_eur": round(self.sell_back_eur, 2),
            "curtailment_eur": round(self.curtailment_eur, 2),
            "unused_end_soc_eur": round(self.unused_end_soc_eur, 2),
            "total_eur": round(self.total_eur, 2),
        }


def attribute_savings(df: pd.DataFrame, efficiency: float) -> SavingsAttribution:
    """Attribute the battery's dynamic-contract saving to its three channels via FIFO lots.

    Each charged kWh becomes a "lot" tagged with its source and cost-per-kWh-in-cell (a grid
    lot costs that hour's import price; a solar lot costs that hour's forgone export). When the
    battery discharges, energy is drawn FIFO and the realised value (avoided import, or sale
    revenue) minus the lot's charge cost is booked to the matching channel. The three channels
    plus the end-of-data leftover reconcile to the total saving.
    """
    imp = _col(df, IMPORT_PRICE_COL)
    exp = _col(df, EXPORT_PRICE_COL)
    charge_total = _col(df, "battery_charge_kwh")
    charge_grid = _col(df, "battery_grid_charge_kwh")
    disch_total = _col(df, "battery_discharge_kwh")
    disch_grid = _col(df, "battery_grid_discharge_kwh")

    # Fee avoided by curtailing solar instead of exporting at a negative price.
    curtailed = _col(df, "solar_curtailed_kwh") if "solar_curtailed_kwh" in df.columns else None
    curtailment = float(-(curtailed * exp).sum()) if curtailed is not None else 0.0

    lots: deque[list[float]] = deque()  # [remaining_cell_kwh, cost_per_cell_kwh, is_grid]
    self_c = arbitrage = sell = 0.0
    eps = 1e-12

    for i in range(len(df)):
        solar_charge = charge_total[i] - charge_grid[i]
        if solar_charge > eps:
            lots.append([solar_charge, exp[i], False])  # solar lot: cost = forgone export
        if charge_grid[i] > eps:
            lots.append([charge_grid[i], imp[i], True])  # grid lot: cost = import price

        to_load = disch_total[i] - disch_grid[i]
        to_grid = disch_grid[i]

        for delivered, selling in ((to_load, False), (to_grid, True)):
            if delivered <= eps:
                continue
            need = delivered / efficiency  # cell energy drawn for this delivery
            while need > eps and lots:
                lot = lots[0]
                take = min(lot[0], need)
                value = take * efficiency * (exp[i] if selling else imp[i])
                cost = take * lot[1]
                if selling:
                    sell += value - cost
                elif lot[2]:
                    arbitrage += value - cost
                else:
                    self_c += value - cost
                lot[0] -= take
                need -= take
                if lot[0] <= eps:
                    lots.popleft()

    unused = -sum(lot[0] * lot[1] for lot in lots)
    return SavingsAttribution(self_c, arbitrage, sell, curtailment, unused)
