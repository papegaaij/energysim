"""Fetch and store historical day-ahead electricity prices for the Netherlands.

Source: the EnergyZero public API (``api.energyzero.nl``) — the same feed many Home
Assistant dynamic-price integrations use. No API key is required. The values returned are
the **bare wholesale market price** (kale marktprijs) in EUR/kWh, excluding energy tax, BTW
and any supplier markup; those are layered on in :mod:`energysim.dynamic_pricing`.

The Dutch day-ahead market switched to 15-minute resolution on 1 Oct 2025, so this module
always resamples the response onto a clean hourly UTC grid (the mean of the quarters) to
match the hourly simulation grid. Timestamps are stored in UTC, ISO 8601 with a ``+00:00``
offset, so they merge directly onto the energy CSV's ``timestamp_utc`` column.

Only the standard library is used for HTTP, so no new dependency is needed.
"""

from __future__ import annotations

import json
import ssl
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

API_URL = "https://api.energyzero.nl/v1/energyprices"
SOURCE_NAME = "energyzero"

TIMESTAMP_COL = "timestamp_utc"
PRICE_COL = "market_price_eur_kwh"

_USER_AGENT = "energysim/0.1 (battery price simulation)"
_CHUNK_DAYS = 31


class PricesError(Exception):
    """Raised when prices can't be fetched or loaded."""


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _fetch_chunk(
    from_dt: datetime, till_dt: datetime, ctx: ssl.SSLContext
) -> list[tuple[str, float]]:
    params = {
        "fromDate": _iso_z(from_dt),
        "tillDate": _iso_z(till_dt),
        "interval": "4",   # day-ahead market prices
        "usageType": "1",  # 1 = electricity, 2 = gas
        "inclBtw": "false",  # bare market price; tax/BTW are added in dynamic_pricing
    }
    url = f"{API_URL}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=30, context=ctx) as resp:
            payload = json.load(resp)
    except HTTPError as exc:
        raise PricesError(f"EnergyZero API returned HTTP {exc.code} for {from_dt:%Y-%m-%d}.")
    except (URLError, TimeoutError) as exc:
        raise PricesError(f"Could not reach the EnergyZero API: {exc}")
    except json.JSONDecodeError as exc:
        raise PricesError(f"EnergyZero API returned invalid JSON: {exc}")

    points: list[tuple[str, float]] = []
    for entry in payload.get("Prices") or []:
        reading = entry.get("readingDate")
        price = entry.get("price")
        if reading is not None and price is not None:
            points.append((reading, float(price)))
    return points


def fetch_energyzero_prices(
    start_date: date, end_date: date, *, verify_ssl: bool = True
) -> pd.DataFrame:
    """Download hourly NL market prices covering the inclusive ``start_date``..``end_date``.

    Fetches in monthly chunks (with a one-day margin so the local-day boundaries at the ends
    of the range are covered), then resamples onto an hourly UTC grid. Returns a frame with
    ``timestamp_utc`` (ISO 8601, +00:00) and ``market_price_eur_kwh`` columns.
    """
    if end_date < start_date:
        raise PricesError("end_date must be on or after start_date.")

    ctx = ssl.create_default_context() if verify_ssl else ssl._create_unverified_context()

    window_start = datetime.combine(start_date, time.min, tzinfo=timezone.utc) - timedelta(days=1)
    window_end = datetime.combine(end_date, time.min, tzinfo=timezone.utc) + timedelta(days=2)

    raw: list[tuple[str, float]] = []
    cursor = window_start
    while cursor < window_end:
        chunk_end = min(cursor + timedelta(days=_CHUNK_DAYS), window_end)
        raw.extend(_fetch_chunk(cursor, chunk_end - timedelta(milliseconds=1), ctx))
        cursor = chunk_end

    if not raw:
        raise PricesError(
            "EnergyZero returned no price data for the requested range. "
            "Check the dates (history is only available for the past)."
        )

    frame = pd.DataFrame(raw, columns=["readingDate", "price"])
    index = pd.to_datetime(frame["readingDate"], utc=True)
    series = pd.Series(frame["price"].to_numpy(dtype=float), index=index).sort_index()
    series = series[~series.index.duplicated(keep="first")]
    hourly = series.resample("1h").mean().dropna()

    return pd.DataFrame(
        {
            TIMESTAMP_COL: [ts.isoformat() for ts in hourly.index],
            PRICE_COL: hourly.to_numpy(dtype=float),
        }
    )


def load_prices_csv(path: Path) -> pd.DataFrame:
    """Load a prices CSV produced by :func:`write_prices`, validating its columns."""
    df = pd.read_csv(path)
    missing = [c for c in (TIMESTAMP_COL, PRICE_COL) if c not in df.columns]
    if missing:
        raise PricesError(
            f"Prices CSV {path} is missing column(s): {', '.join(missing)}. "
            "Expected a file written by the `energyprices` command."
        )
    return df


def write_prices(
    df: pd.DataFrame,
    *,
    start: date,
    end: date,
    out_dir: Path,
    source: str = SOURCE_NAME,
) -> tuple[Path, Path]:
    """Write ``prices_<start>_<end>.csv`` plus a ``.metadata.json`` sidecar."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"prices_{start.isoformat()}_{end.isoformat()}"
    csv_path = out_dir / f"{stem}.csv"
    meta_path = out_dir / f"{stem}.metadata.json"

    df.to_csv(csv_path, index=False)
    metadata = {
        "source": source,
        "api_url": API_URL,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "resolution": "1h",
        "rows": int(len(df)),
        "unit": "EUR/kWh",
        "note": "Bare wholesale day-ahead price, excl. energy tax / BTW / supplier markup.",
    }
    meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return csv_path, meta_path
