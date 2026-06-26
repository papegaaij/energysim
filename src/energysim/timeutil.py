"""Shared time helpers."""

from __future__ import annotations

import pandas as pd


def local_timestamps(df: pd.DataFrame) -> pd.Series:
    """Parse the local wall-clock timeline from timestamp_local (fallback timestamp_utc).

    The UTC offset is sliced off and parsed tz-naive: across a year the local column mixes
    offsets (e.g. +01:00 / +02:00 for DST), which pandas refuses to combine. The wall-clock
    is exactly what we want for month grouping, the SoC plot, and tariff classification.
    """
    col = "timestamp_local" if "timestamp_local" in df.columns else "timestamp_utc"
    wall_clock = df[col].astype(str).str.slice(0, 19)
    return pd.to_datetime(wall_clock, format="%Y-%m-%dT%H:%M:%S")
