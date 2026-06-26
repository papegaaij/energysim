"""Command-line entry point for downloading market prices (`energyprices`).

Downloads historical hourly Dutch day-ahead electricity prices from EnergyZero and writes a
`prices_<start>_<end>.csv` that `batterysim --prices ...` can consume. Keeping it a separate
command (mirroring `energysim`) lets one price file be reused across many simulations.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from energysim.prices import PricesError, fetch_energyzero_prices, write_prices
from energysim.prompts import prompt_date_range


class CliError(Exception):
    """Raised for user-facing CLI errors."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="energyprices",
        description="Download historical hourly NL day-ahead electricity prices (EnergyZero).",
    )
    parser.add_argument("--start", help="Start date YYYY-MM-DD, inclusive (skips the prompt).")
    parser.add_argument("--end", help="End date YYYY-MM-DD, inclusive (skips the prompt).")
    parser.add_argument(
        "--input",
        help="Energy CSV whose date range to match (reads its .metadata.json sidecar).",
    )
    parser.add_argument("--out", default="data", help="Output directory (default: ./data).")
    return parser.parse_args(argv)


def _range_from_metadata(input_path: str) -> tuple[date, date]:
    meta_path = Path(input_path).with_suffix(".metadata.json")
    if not meta_path.is_file():
        raise CliError(f"No metadata sidecar found for {input_path} (expected {meta_path}).")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    try:
        start = datetime.strptime(meta["start_date"], "%Y-%m-%d").date()
        end = datetime.strptime(meta["end_date"], "%Y-%m-%d").date()
    except (KeyError, ValueError) as exc:
        raise CliError(f"Could not read start/end date from {meta_path}: {exc}")
    return start, end


def _resolve_dates(args: argparse.Namespace) -> tuple[date, date]:
    if args.input:
        return _range_from_metadata(args.input)
    if args.start and args.end:
        try:
            start = datetime.strptime(args.start, "%Y-%m-%d").date()
            end = datetime.strptime(args.end, "%Y-%m-%d").date()
        except ValueError as exc:
            raise CliError(f"Invalid --start/--end date (expected YYYY-MM-DD): {exc}")
        if end < start:
            raise CliError("--end must be on or after --start.")
        return start, end
    return prompt_date_range()


def _run(args: argparse.Namespace) -> None:
    start, end = _resolve_dates(args)
    print(f"Fetching hourly market prices from {start} to {end} (inclusive) via EnergyZero ...")
    df = fetch_energyzero_prices(start, end)
    csv_path, meta_path = write_prices(df, start=start, end=end, out_dir=Path(args.out))

    avg = float(df["market_price_eur_kwh"].mean())
    negative = int((df["market_price_eur_kwh"] < 0).sum())
    print(
        f"\nWrote {len(df)} hourly prices to {csv_path}\n"
        f"Metadata: {meta_path}\n"
        f"Average market price: {avg:.4f} EUR/kWh   negative-price hours: {negative}"
    )


def main() -> int:
    args = _parse_args()
    try:
        _run(args)
    except (CliError, PricesError) as exc:
        print(f"Error: {exc}")
        return 1
    return 0
