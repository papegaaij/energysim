"""Interactive prompts and URL helpers."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse


def normalize_base_url(raw: str) -> str | None:
    """Normalise a user-supplied URL to `scheme://host[:port]`, or None if invalid."""
    raw = raw.strip()
    if not raw:
        return None
    # Assume https if no scheme was given.
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    # Strip any path/query so we can append /api/websocket cleanly.
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def prompt_url(default: str | None) -> str:
    """Prompt for the Home Assistant base URL, returning a normalised http(s) URL."""
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"Home Assistant URL{suffix}: ").strip()
        if not raw and default:
            raw = default
        url = normalize_base_url(raw)
        if url is None:
            print("  Please enter a valid http(s) URL, e.g. https://homeassistant.local:8123")
            continue
        return url


def websocket_url(base_url: str) -> str:
    """Derive the ws(s)://host/api/websocket URL from a base http(s) URL."""
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/api/websocket", "", "", ""))


def _prompt_date(label: str) -> date:
    while True:
        raw = input(f"{label} (YYYY-MM-DD): ").strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print("  Please enter a date as YYYY-MM-DD, e.g. 2026-01-31")


def prompt_date_range() -> tuple[date, date]:
    """Prompt for an inclusive start/end date, ensuring end >= start."""
    while True:
        start = _prompt_date("Start date")
        end = _prompt_date("End date")
        if end < start:
            print("  End date must be on or after the start date. Try again.")
            continue
        return start, end


def prompt_float(
    label: str,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
    default: float | None = None,
) -> float:
    """Prompt for a float, optionally bounded (min exclusive, max inclusive)."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            print("  Please enter a number.")
            continue
        if min_value is not None and value <= min_value:
            print(f"  Please enter a value greater than {min_value}.")
            continue
        if max_value is not None and value > max_value:
            print(f"  Please enter a value of at most {max_value}.")
            continue
        return value


def parse_efficiency(raw: str) -> float | None:
    """Parse '90', '90%' or '0.9' into a fraction in (0, 1], or None if invalid."""
    raw = raw.strip().rstrip("%").replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    if value > 1:  # entered as a percentage
        value /= 100.0
    if not 0 < value <= 1:
        return None
    return value


def prompt_efficiency() -> float:
    """Prompt for round-trip efficiency, accepting a percentage or fraction."""
    while True:
        raw = input("Round-trip efficiency (e.g. 90 or 0.9): ").strip()
        value = parse_efficiency(raw)
        if value is None:
            print("  Please enter a value between 0 and 100 (%) or 0 and 1.")
            continue
        return value


def _parse_nonneg_float(raw: str) -> float | None:
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None
    return value if value >= 0 else None


def prompt_price(label: str) -> float:
    """Prompt for a non-negative price (EUR/kWh)."""
    while True:
        value = _parse_nonneg_float(input(f"{label} (EUR/kWh): ").strip())
        if value is None:
            print("  Please enter a non-negative number.")
            continue
        return value


def prompt_optional_price(label: str) -> float | None:
    """Prompt for a price; blank input returns None (i.e. skip cost calculation)."""
    while True:
        raw = input(f"{label} (EUR/kWh, blank to skip costs): ").strip()
        if not raw:
            return None
        value = _parse_nonneg_float(raw)
        if value is None:
            print("  Please enter a non-negative number (or blank to skip).")
            continue
        return value


def resolve_input_csv(arg: str | None, default_dir: Path) -> Path | None:
    """Return the input CSV path: use `arg` if given, else default to the most recent
    energy_*.csv in `default_dir` and prompt to confirm/override. None if unresolved."""
    if arg:
        path = Path(arg)
        return path if path.is_file() else None

    candidates = sorted(
        # Downloader outputs only — exclude the simulator's own *_battery_* files.
        (p for p in default_dir.glob("energy_*.csv") if "_battery_" not in p.name),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    default = candidates[0] if candidates else None
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"Input CSV path{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if not raw:
            print("  Please enter the path to a downloaded energy CSV.")
            continue
        path = Path(raw)
        if path.is_file():
            return path
        print(f"  File not found: {path}")
