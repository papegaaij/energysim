"""Two-tier (normal / reduced) time-of-use cost calculations.

Tariff rule (Dutch dal/normaal tariff):
- **Normal** tariff: Mon–Fri, 07:00–23:00 (local time).
- **Reduced** tariff: 23:00–07:00 on weekdays, all weekend, and Dutch national holidays
  (Nieuwjaarsdag, Tweede Paasdag, Koningsdag, Hemelvaartsdag, Tweede Pinksterdag, and both
  Kerstdagen) — these are billed at the reduced tariff for the whole day.

Import and export each have their own normal/reduced rate. The simulator already emits the
with- and without-battery grid flows, so costing is a matter of multiplying each hour's flow
by the rate for that hour's tariff period.

Regional note: in Noord-Brabant and Limburg the reduced period starts at 21:00 instead of
23:00 — change `NORMAL_END_HOUR` below if that applies.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta

import pandas as pd

from energysim.timeutil import local_timestamps

NORMAL_START_HOUR = 7
NORMAL_END_HOUR = 23


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous/Meeus algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month = (h + el - 7 * m + 114) // 31
    day = ((h + el - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nl_energy_holidays(year: int) -> set[date]:
    """The national holidays billed at the reduced (dal) tariff in `year`."""
    easter = easter_sunday(year)
    kings = date(year, 4, 27)
    if kings.weekday() == 6:  # Koningsdag moves to the 26th if the 27th is a Sunday
        kings = date(year, 4, 26)
    return {
        date(year, 1, 1),             # Nieuwjaarsdag
        easter + timedelta(days=1),   # Tweede Paasdag
        kings,                        # Koningsdag
        easter + timedelta(days=39),  # Hemelvaartsdag
        easter + timedelta(days=50),  # Tweede Pinksterdag
        date(year, 12, 25),           # Eerste Kerstdag
        date(year, 12, 26),           # Tweede Kerstdag
    }


def reduced_tariff_mask(timestamps: pd.Series) -> pd.Series:
    """Boolean mask: True where the reduced tariff applies (per the rule above)."""
    ts = pd.DatetimeIndex(timestamps)
    is_night = (ts.hour < NORMAL_START_HOUR) | (ts.hour >= NORMAL_END_HOUR)
    is_weekend = ts.weekday >= 5  # Sat=5, Sun=6

    holidays: set[date] = set()
    for year in set(ts.year):
        holidays |= nl_energy_holidays(int(year))
    is_holiday = pd.Index(ts.date).isin(holidays)

    return pd.Series(is_night | is_weekend | is_holiday)


@dataclass
class FlowSplit:
    """Energy for one flow, split by tariff period (price-independent)."""

    normal_kwh: float
    reduced_kwh: float

    @property
    def total_kwh(self) -> float:
        return self.normal_kwh + self.reduced_kwh

    def minus(self, other: "FlowSplit") -> "FlowSplit":
        return FlowSplit(
            normal_kwh=self.normal_kwh - other.normal_kwh,
            reduced_kwh=self.reduced_kwh - other.reduced_kwh,
        )

    def rounded(self) -> dict:
        return {
            "normal_kwh": round(self.normal_kwh, 3),
            "reduced_kwh": round(self.reduced_kwh, 3),
            "total_kwh": round(self.total_kwh, 3),
        }


@dataclass
class EnergyBreakdown:
    """Per-tariff kWh split of all four grid flows. Computed without any prices."""

    import_without: FlowSplit
    import_with: FlowSplit
    export_without: FlowSplit
    export_with: FlowSplit

    def as_dict(self) -> dict:
        return {
            "import": {
                "without": self.import_without.rounded(),
                "with": self.import_with.rounded(),
                "difference": self.import_with.minus(self.import_without).rounded(),
            },
            "export": {
                "without": self.export_without.rounded(),
                "with": self.export_with.rounded(),
                "difference": self.export_with.minus(self.export_without).rounded(),
            },
        }


def _flow_split(series: pd.Series, reduced: pd.Series) -> FlowSplit:
    values = series.fillna(0.0).reset_index(drop=True)
    mask = reduced.reset_index(drop=True)
    return FlowSplit(
        normal_kwh=float(values[~mask].sum()),
        reduced_kwh=float(values[mask].sum()),
    )


def split_by_tariff(df: pd.DataFrame) -> EnergyBreakdown:
    """Split each grid flow into normal/reduced kWh per the two-tier tariff calendar.

    Reads `grid_import_kwh`/`grid_export_kwh` (without battery) and
    `grid_import_sim_kwh`/`grid_export_sim_kwh` (with battery) from the simulator output.
    Needs no prices, so the kWh-per-tariff totals are always available.
    """
    reduced = reduced_tariff_mask(local_timestamps(df))
    return EnergyBreakdown(
        import_without=_flow_split(df["grid_import_kwh"], reduced),
        import_with=_flow_split(df["grid_import_sim_kwh"], reduced),
        export_without=_flow_split(df["grid_export_kwh"], reduced),
        export_with=_flow_split(df["grid_export_sim_kwh"], reduced),
    )


@dataclass
class Tariff:
    import_normal: float
    import_reduced: float
    export_normal: float
    export_reduced: float


@dataclass
class FlowCost:
    """Energy and cost for one flow, split by tariff period."""

    normal_kwh: float
    normal_eur: float
    reduced_kwh: float
    reduced_eur: float

    @property
    def total_kwh(self) -> float:
        return self.normal_kwh + self.reduced_kwh

    @property
    def total_eur(self) -> float:
        return self.normal_eur + self.reduced_eur

    def minus(self, other: "FlowCost") -> "FlowCost":
        return FlowCost(
            normal_kwh=self.normal_kwh - other.normal_kwh,
            normal_eur=self.normal_eur - other.normal_eur,
            reduced_kwh=self.reduced_kwh - other.reduced_kwh,
            reduced_eur=self.reduced_eur - other.reduced_eur,
        )

    def rounded(self) -> dict:
        return {
            "normal_kwh": round(self.normal_kwh, 3),
            "normal_eur": round(self.normal_eur, 2),
            "reduced_kwh": round(self.reduced_kwh, 3),
            "reduced_eur": round(self.reduced_eur, 2),
            "total_kwh": round(self.total_kwh, 3),
            "total_eur": round(self.total_eur, 2),
        }


@dataclass
class CostBreakdown:
    tariff: Tariff
    import_without: FlowCost
    import_with: FlowCost
    export_without: FlowCost
    export_with: FlowCost
    currency: str = "EUR"

    @property
    def net_without_eur(self) -> float:
        return self.import_without.total_eur - self.export_without.total_eur

    @property
    def net_with_eur(self) -> float:
        return self.import_with.total_eur - self.export_with.total_eur

    @property
    def savings_eur(self) -> float:
        return self.net_without_eur - self.net_with_eur

    def as_dict(self) -> dict:
        return {
            "currency": self.currency,
            "tariff": asdict(self.tariff),
            "import": {
                "without": self.import_without.rounded(),
                "with": self.import_with.rounded(),
                "difference": self.import_with.minus(self.import_without).rounded(),
            },
            "export": {
                "without": self.export_without.rounded(),
                "with": self.export_with.rounded(),
                "difference": self.export_with.minus(self.export_without).rounded(),
            },
            "net_without_eur": round(self.net_without_eur, 2),
            "net_with_eur": round(self.net_with_eur, 2),
            "savings_eur": round(self.savings_eur, 2),
        }


def _apply_price(split: FlowSplit, price_normal: float, price_reduced: float) -> FlowCost:
    return FlowCost(
        normal_kwh=split.normal_kwh,
        normal_eur=split.normal_kwh * price_normal,
        reduced_kwh=split.reduced_kwh,
        reduced_eur=split.reduced_kwh * price_reduced,
    )


def compute_costs(energy: EnergyBreakdown, tariff: Tariff) -> CostBreakdown:
    """Apply the two-tier tariff to an already-computed per-tariff energy split."""
    return CostBreakdown(
        tariff=tariff,
        import_without=_apply_price(
            energy.import_without, tariff.import_normal, tariff.import_reduced
        ),
        import_with=_apply_price(
            energy.import_with, tariff.import_normal, tariff.import_reduced
        ),
        export_without=_apply_price(
            energy.export_without, tariff.export_normal, tariff.export_reduced
        ),
        export_with=_apply_price(
            energy.export_with, tariff.export_normal, tariff.export_reduced
        ),
    )
