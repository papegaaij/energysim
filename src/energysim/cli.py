"""Command-line entry point: prompt, download, write CSV."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import websockets

from energysim.config import Config, ConfigError, load_config
from energysim.export import write_output
from energysim.ha_client import HAClient, HAError
from energysim.prompts import (
    normalize_base_url,
    prompt_date_range,
    prompt_url,
    websocket_url,
)
from energysim.sources import extract_metric_groups
from energysim.transform import NoDataError, build_dataframe


def _utc_window(start_date: date, end_date: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Inclusive local date range -> [start_utc, end_utc) in UTC.

    The window covers local midnight of `start_date` up to (but excluding) local midnight
    of the day after `end_date`, so both dates are fully included.
    """
    start_local = datetime.combine(start_date, time.min, tzinfo=tz)
    end_local = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _unit_of(meta: dict) -> str | None:
    return (
        meta.get("statistics_unit_of_measurement")
        or meta.get("display_unit_of_measurement")
        or meta.get("unit_of_measurement")
    )


def _debug_prefs(prefs: dict) -> None:
    print("\n[debug] energy_sources:")
    for source in prefs.get("energy_sources", []):
        kind = source.get("type")
        fields = {
            k: v
            for k, v in source.items()
            if k.startswith("stat_") or k in ("flow_from", "flow_to")
        }
        print(f"  - type={kind}: {json.dumps(fields, default=str)}")
    devices = [d.get("stat_consumption") for d in prefs.get("device_consumption", [])]
    print(f"[debug] device_consumption: {devices}")


def _debug_stats(stat_ids: list[str], stats: dict, out_dir: Path) -> None:
    print("\n[debug] statistics returned per statistic_id:")
    summary = {}
    for stat_id in stat_ids:
        buckets = stats.get(stat_id) or []
        sample = buckets[0] if buckets else None
        summary[stat_id] = {"buckets": len(buckets), "sample": sample}
        marker = "NO DATA" if not buckets else f"{len(buckets)} buckets; sample={sample}"
        print(f"  - {stat_id}: {marker}")
    out_dir.mkdir(parents=True, exist_ok=True)
    debug_path = out_dir / "debug.json"
    debug_path.write_text(
        json.dumps({"requested": stat_ids, "stats": summary}, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"[debug] wrote {debug_path}")


async def _download(
    config: Config,
    base_url: str,
    start_date: date,
    end_date: date,
    out_dir: Path,
    debug: bool = False,
) -> None:
    ws_url = websocket_url(base_url)
    print(f"Connecting to {ws_url} ...")
    async with HAClient(ws_url, config.token, verify_ssl=config.verify_ssl) as client:
        ha_config = await client.get_config()
        tz_name = ha_config.get("time_zone") or "UTC"
        tz = ZoneInfo(tz_name)
        print(f"Connected. Home Assistant timezone: {tz_name}")

        start_utc, end_utc = _utc_window(start_date, end_date, tz)
        # Request one extra leading hour so the sum-diff fallback has a previous value.
        query_start = start_utc - timedelta(hours=1)

        prefs = await client.get_energy_prefs()
        if debug:
            _debug_prefs(prefs)
        groups = extract_metric_groups(prefs)
        if not groups:
            raise NoDataError(
                "No energy sources found in the Energy dashboard configuration. "
                "Configure the Energy dashboard in Home Assistant first."
            )
        print(f"Discovered {len(groups)} energy source(s):")
        for group in groups:
            print(f"  - {group.base_name}: {', '.join(group.stat_ids)}")

        stat_meta = await client.list_statistic_ids()
        units = {m["statistic_id"]: _unit_of(m) for m in stat_meta}

        stat_ids = [stat_id for group in groups for stat_id in group.stat_ids]
        print(
            f"Downloading hourly statistics for {len(stat_ids)} statistic(s) "
            f"from {start_date} to {end_date} (inclusive) ..."
        )
        stats = await client.statistics_during_period(query_start, end_utc, stat_ids)
        if debug:
            _debug_stats(stat_ids, stats, out_dir)

        df, column_meta, filled = build_dataframe(
            stats, groups, units, start_utc, end_utc, tz
        )
        csv_path, meta_path = write_output(
            df,
            column_meta,
            base_url=base_url,
            start=start_date,
            end=end_date,
            time_zone=tz_name,
            filled_hours=filled,
            out_dir=out_dir,
        )

        print(f"\nWrote {len(df)} hourly rows to {csv_path}")
        print(f"Metadata: {meta_path}")
        if filled:
            print(f"Note: {filled} hour(s) had no data in HA and were filled with 0.0.")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="energysim",
        description="Download hourly energy data from Home Assistant.",
    )
    parser.add_argument("--url", help="Home Assistant base URL (skips the prompt).")
    parser.add_argument("--start", help="Start date YYYY-MM-DD, inclusive (skips the prompt).")
    parser.add_argument("--end", help="End date YYYY-MM-DD, inclusive (skips the prompt).")
    parser.add_argument(
        "--out", default="data", help="Output directory (default: ./data)."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print discovered energy sources and raw statistics info for troubleshooting.",
    )
    return parser.parse_args(argv)


def _resolve_dates(args: argparse.Namespace) -> tuple[date, date]:
    if args.start and args.end:
        try:
            start = datetime.strptime(args.start, "%Y-%m-%d").date()
            end = datetime.strptime(args.end, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ConfigError(f"Invalid --start/--end date (expected YYYY-MM-DD): {exc}")
        if end < start:
            raise ConfigError("--end must be on or after --start.")
        return start, end
    return prompt_date_range()


def main() -> int:
    args = _parse_args()
    try:
        config = load_config()

        if args.url:
            base_url = normalize_base_url(args.url)
            if base_url is None:
                raise ConfigError(f"Invalid --url: {args.url!r}")
        else:
            base_url = prompt_url(config.url)

        start_date, end_date = _resolve_dates(args)
    except ConfigError as exc:
        print(f"Error: {exc}")
        return 1

    try:
        asyncio.run(
            _download(config, base_url, start_date, end_date, Path(args.out), args.debug)
        )
    except (HAError, NoDataError) as exc:
        print(f"\nError: {exc}")
        return 1
    except (OSError, websockets.exceptions.WebSocketException) as exc:
        print(f"\nError: could not connect to Home Assistant at {base_url}: {exc}")
        return 1

    return 0
