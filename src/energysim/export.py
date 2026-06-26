"""Write the hourly DataFrame to CSV plus a metadata sidecar."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd


def write_output(
    df: pd.DataFrame,
    column_meta: dict,
    *,
    base_url: str,
    start: date,
    end: date,
    time_zone: str,
    filled_hours: int,
    out_dir: Path,
) -> tuple[Path, Path]:
    """Write `data/energy_<start>_<end>.csv` and a `.metadata.json` sidecar."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"energy_{start.isoformat()}_{end.isoformat()}"
    csv_path = out_dir / f"{stem}.csv"
    meta_path = out_dir / f"{stem}.metadata.json"

    df.to_csv(csv_path, index=False)

    metadata = {
        "source_url": base_url,
        "time_zone": time_zone,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "resolution": "1h",
        "rows": int(len(df)),
        "filled_hours": filled_hours,
        "columns": column_meta,
    }
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    return csv_path, meta_path
