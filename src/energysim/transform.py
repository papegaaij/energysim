"""Assemble the hourly statistics into a tidy wide DataFrame."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from energysim.sources import MetricGroup

# Columns that make up the derived household electricity load.
_CONSUMPTION_TERMS = {
    "grid_import_kwh": 1,
    "solar_production_kwh": 1,
    "battery_discharge_kwh": 1,
    "grid_export_kwh": -1,
    "battery_charge_kwh": -1,
}


class NoDataError(Exception):
    """Raised when no statistics were returned for the requested range."""


def _bucket_start(raw) -> pd.Timestamp:
    """Parse a statistics bucket 'start' (ms-since-epoch or ISO string) to a UTC Timestamp."""
    if isinstance(raw, (int, float)):
        return pd.Timestamp(raw, unit="ms", tz="UTC")
    ts = pd.Timestamp(raw)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _normalise(series: pd.Series, unit: str | None) -> pd.Series:
    """Convert Wh/MWh energy to kWh; leave everything else (kWh, m³, ...) untouched."""
    u = (unit or "").strip().lower()
    if u == "wh":
        return series / 1000.0
    if u == "mwh":
        return series * 1000.0
    return series


def _stat_series(buckets: list[dict]) -> pd.Series:
    """One statistic's per-hour values: prefer `change`, fall back to diff of `sum`."""
    index, change, cumulative = [], [], []
    for bucket in buckets:
        index.append(_bucket_start(bucket["start"]))
        change.append(bucket.get("change"))
        cumulative.append(bucket.get("sum"))
    change_series = pd.Series(change, index=index, dtype="float64")
    if change_series.notna().any():
        return change_series
    # Older HA: derive per-hour delta from the cumulative sum (the extra leading hour
    # requested by the caller makes the first real bucket's delta correct).
    return pd.Series(cumulative, index=index, dtype="float64").diff()


def _unit_slug(unit: str | None) -> str:
    u = (unit or "").strip().lower().replace("³", "3").replace("²", "2")
    return re.sub(r"[^0-9a-z]+", "", u) or "value"


def _column_suffix(group: MetricGroup, units: dict[str, str]) -> str:
    if group.electric:
        return "kwh"
    return _unit_slug(units.get(group.stat_ids[0]))


def build_dataframe(
    stats: dict[str, list[dict]],
    groups: list[MetricGroup],
    units: dict[str, str],
    start_utc: datetime,
    end_utc: datetime,
    time_zone: ZoneInfo,
) -> tuple[pd.DataFrame, dict, int]:
    """Build the hourly DataFrame plus per-column metadata and a filled-hour count.

    `start_utc` is inclusive and `end_utc` exclusive (local midnight of end + 1 day).
    """
    columns: dict[str, pd.Series] = {}
    column_meta: dict[str, dict] = {}

    for group in groups:
        combined: pd.Series | None = None
        for stat_id in group.stat_ids:
            series = _normalise(_stat_series(stats.get(stat_id) or []), units.get(stat_id))
            combined = series if combined is None else combined.add(series, fill_value=0.0)
        if combined is None or combined.empty:
            continue
        suffix = _column_suffix(group, units)
        column = f"{group.base_name}_{suffix}"
        columns[column] = combined
        column_meta[column] = {
            "role": group.role,
            "entity_ids": group.stat_ids,
            "unit": suffix,
        }

    if not columns:
        raise NoDataError(
            "No hourly statistics were returned for the requested range. Check that the "
            "date range overlaps recorded data and that the Energy dashboard is configured."
        )

    df = pd.DataFrame(columns).sort_index()

    # Reindex onto a gap-free hourly grid; record how many hours HA had no data for.
    full_index = pd.date_range(start=start_utc, end=end_utc, freq="h", inclusive="left")
    filled_hours = int((~full_index.isin(df.index)).sum())
    df = df.reindex(full_index).fillna(0.0).round(6)

    # Derived household electricity load (Energy-dashboard "home usage").
    present_terms = [c for c in _CONSUMPTION_TERMS if c in df.columns]
    if present_terms:
        consumption = sum(_CONSUMPTION_TERMS[c] * df[c] for c in present_terms)
        df["home_consumption_kwh"] = consumption.round(6)
        column_meta["home_consumption_kwh"] = {
            "role": "derived",
            "formula": "grid_import + solar_production + battery_discharge "
            "- grid_export - battery_charge",
            "unit": "kwh",
        }

    local_index = df.index.tz_convert(time_zone)
    df.insert(0, "timestamp_utc", [t.isoformat() for t in df.index])
    df.insert(0, "timestamp_local", [t.isoformat() for t in local_index])

    return df, column_meta, filled_hours
