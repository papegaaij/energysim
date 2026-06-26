"""Command-line entry point for the battery simulator (`batterysim`)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from energysim.chart import render_chart
from energysim.dynamic_pricing import (
    DEFAULT_ENERGY_TAX_2027,
    DEFAULT_MARKUP_EUR,
    DynamicContract,
    DynamicCostBreakdown,
    add_cost_columns,
    add_price_columns,
    compute_dynamic_costs,
)
from energysim.prices import (
    PricesError,
    fetch_energyzero_prices,
    load_prices_csv,
)
from energysim.pricing import (
    CostBreakdown,
    EnergyBreakdown,
    Tariff,
    compute_costs,
    split_by_tariff,
)
from energysim.prompts import (
    parse_efficiency,
    prompt_efficiency,
    prompt_float,
    prompt_optional_price,
    prompt_price,
    resolve_input_csv,
)
from energysim.simulate import (
    BatteryParams,
    ReactiveStrategy,
    SimulationError,
    Summary,
    simulate_battery,
)
from energysim.timeutil import local_timestamps


class CliError(Exception):
    """Raised for user-facing CLI errors (bad arguments, missing files)."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="batterysim",
        description="Simulate a home battery over downloaded hourly energy data.",
    )
    parser.add_argument("--input", help="Path to a downloaded energy CSV (skips the prompt).")
    parser.add_argument("--capacity", type=float, help="Battery capacity in kWh.")
    parser.add_argument(
        "--efficiency", help="Round-trip efficiency as a percentage (90) or fraction (0.9)."
    )
    parser.add_argument(
        "--out", help="Output directory (default: the input file's directory)."
    )
    parser.add_argument("--price-import-normal", type=float, help="Import price, normal tariff (EUR/kWh).")
    parser.add_argument("--price-import-reduced", type=float, help="Import price, reduced tariff (EUR/kWh).")
    parser.add_argument("--price-export-normal", type=float, help="Export price, normal tariff (EUR/kWh).")
    parser.add_argument("--price-export-reduced", type=float, help="Export price, reduced tariff (EUR/kWh).")
    parser.add_argument("--no-costs", action="store_true", help="Skip the cost calculation.")

    # Dynamic-contract pricing (2027 model). Provide --prices or --fetch-prices to enable.
    parser.add_argument(
        "--prices", help="Path to a prices CSV (from `energyprices`) for dynamic-contract costing."
    )
    parser.add_argument(
        "--fetch-prices",
        action="store_true",
        help="Download market prices for the data's date range (EnergyZero) instead of --prices.",
    )
    parser.add_argument(
        "--markup", type=float,
        help=f"Dynamic supplier markup, EUR/kWh (default {DEFAULT_MARKUP_EUR:g}).",
    )
    parser.add_argument(
        "--feed-in-factor", type=float,
        help="Share of the bare market price paid for export (default 1.0).",
    )
    parser.add_argument(
        "--energy-tax", type=float,
        help=f"2027 energy tax, EUR/kWh excl. BTW (default {DEFAULT_ENERGY_TAX_2027:g}, "
        "an unofficial placeholder until Belastingplan 2027).",
    )
    parser.add_argument(
        "--feed-in-incl-vat", action="store_true",
        help="Add BTW to the feed-in compensation for export.",
    )
    return parser.parse_args(argv)


def _resolve_tariff(args: argparse.Namespace) -> Tariff | None:
    """Tariff from --price-* flags, else prompt. None means 'skip costs'."""
    if args.no_costs:
        return None

    flags = [
        args.price_import_normal,
        args.price_import_reduced,
        args.price_export_normal,
        args.price_export_reduced,
    ]
    if any(v is not None for v in flags):
        if any(v is None for v in flags):
            raise CliError(
                "Provide all four --price-* values together, or none (to be prompted)."
            )
        if any(v < 0 for v in flags):
            raise CliError("Prices must be >= 0.")
        return Tariff(*flags)

    print(
        "\nTariff (normal = Mon-Fri 07:00-23:00; reduced = nights, weekends, NL holidays)."
    )
    import_normal = prompt_optional_price("Import price - normal")
    if import_normal is None:
        print("  Skipping cost calculation.")
        return None
    return Tariff(
        import_normal=import_normal,
        import_reduced=prompt_price("Import price - reduced"),
        export_normal=prompt_price("Export price - normal"),
        export_reduced=prompt_price("Export price - reduced"),
    )


def _data_date_range(df: pd.DataFrame) -> tuple[date, date]:
    ts = local_timestamps(df)
    return ts.min().date(), ts.max().date()


def _resolve_prices(args: argparse.Namespace, df: pd.DataFrame) -> pd.DataFrame | None:
    """Resolve the hourly market-price source for the dynamic contract, or None to skip.

    --prices loads a file; --fetch-prices downloads for the data's range; otherwise (when not
    --no-costs and running interactively) prompt for a path or 'fetch'.
    """
    if args.prices:
        return load_prices_csv(Path(args.prices))
    if args.fetch_prices:
        start, end = _data_date_range(df)
        print(f"Fetching market prices {start}..{end} via EnergyZero ...")
        return fetch_energyzero_prices(start, end)
    if args.no_costs or not sys.stdin.isatty():
        # Scripted run with no --prices/--fetch-prices: skip dynamic pricing silently.
        return None

    raw = input(
        "\nDynamic contract prices CSV (blank to skip; 'fetch' to download): "
    ).strip()
    if not raw:
        return None
    if raw.lower() == "fetch":
        start, end = _data_date_range(df)
        print(f"Fetching market prices {start}..{end} via EnergyZero ...")
        return fetch_energyzero_prices(start, end)
    path = Path(raw)
    if not path.is_file():
        raise CliError(f"Prices file not found: {path}")
    return load_prices_csv(path)


def _resolve_contract(args: argparse.Namespace) -> DynamicContract:
    """Build the dynamic contract from --markup/--feed-in-factor/--energy-tax (else defaults)."""
    markup = args.markup if args.markup is not None else DEFAULT_MARKUP_EUR
    energy_tax = args.energy_tax if args.energy_tax is not None else DEFAULT_ENERGY_TAX_2027
    feed_in_factor = args.feed_in_factor if args.feed_in_factor is not None else 1.0
    for label, value in (("--markup", markup), ("--energy-tax", energy_tax),
                         ("--feed-in-factor", feed_in_factor)):
        if value < 0:
            raise CliError(f"{label} must be >= 0.")
    return DynamicContract(
        energy_tax_eur=energy_tax,
        markup_eur=markup,
        feed_in_factor=feed_in_factor,
        feed_in_incl_vat=args.feed_in_incl_vat,
    )


def _output_stem(input_path: Path, capacity: float, efficiency: float) -> str:
    return f"{input_path.stem}_battery_{capacity:g}kWh_{efficiency * 100:g}pct"


def _format_summary(summary: Summary) -> str:
    lines = [
        f"Without battery:  import {summary.import_without_kwh:>10,.0f} kWh"
        f"   export {summary.export_without_kwh:>10,.0f} kWh",
        f"With battery:     import {summary.import_with_kwh:>10,.0f} kWh"
        f"   export {summary.export_with_kwh:>10,.0f} kWh",
        f"Difference:       import {-summary.import_reduction_kwh:>10,.0f} kWh"
        f" ({-summary.import_reduction_pct:+.1f}%)"
        f"   export {-summary.export_reduction_kwh:>10,.0f} kWh"
        f" ({-summary.export_reduction_pct:+.1f}%)",
        f"Battery:          charged {summary.total_charged_kwh:,.0f} kWh"
        f"   delivered {summary.total_delivered_kwh:,.0f} kWh"
        f"   losses {summary.total_loss_kwh:,.1f} kWh"
        f"   end SoC {summary.end_soc_kwh:,.1f} kWh",
    ]
    if summary.self_sufficiency_without is not None:
        lines.append(
            f"Self-sufficiency: {summary.self_sufficiency_without * 100:.1f}% -> "
            f"{summary.self_sufficiency_with * 100:.1f}%"
        )
    return "\n".join(lines)


def _tariff_header() -> list[str]:
    return [
        f"  {'':<12}{'Import':^30}   {'Export':^30}",
        f"  {'':<12}{'normal':>10}{'reduced':>10}{'total':>10}   "
        f"{'normal':>10}{'reduced':>10}{'total':>10}",
    ]


def _format_energy_tariff(eb: EnergyBreakdown) -> str:
    def row(name, imp, exp):
        return (
            f"  {name:<12}"
            f"{imp.normal_kwh:>10,.0f}{imp.reduced_kwh:>10,.0f}{imp.total_kwh:>10,.0f}   "
            f"{exp.normal_kwh:>10,.0f}{exp.reduced_kwh:>10,.0f}{exp.total_kwh:>10,.0f}"
        )

    return "\n".join(
        [
            "\nEnergy by tariff (kWh)  [normal = Mon-Fri 07:00-23:00; reduced = "
            "nights, weekends, NL holidays]:",
            *_tariff_header(),
            row("Without", eb.import_without, eb.export_without),
            row("With", eb.import_with, eb.export_with),
            row(
                "Difference",
                eb.import_with.minus(eb.import_without),
                eb.export_with.minus(eb.export_without),
            ),
        ]
    )


def _format_costs(cb: CostBreakdown) -> str:
    cur = cb.currency
    t = cb.tariff

    def row(name, imp, exp):
        return (
            f"  {name:<12}"
            f"{imp.normal_eur:>10.2f}{imp.reduced_eur:>10.2f}{imp.total_eur:>10.2f}   "
            f"{exp.normal_eur:>10.2f}{exp.reduced_eur:>10.2f}{exp.total_eur:>10.2f}"
        )

    return "\n".join(
        [
            f"\nCosts ({cur}/kWh — import: normal {t.import_normal:g} / reduced "
            f"{t.import_reduced:g}; export: normal {t.export_normal:g} / reduced "
            f"{t.export_reduced:g}):",
            *_tariff_header(),
            row("Without", cb.import_without, cb.export_without),
            row("With", cb.import_with, cb.export_with),
            row(
                "Difference",
                cb.import_with.minus(cb.import_without),
                cb.export_with.minus(cb.export_without),
            ),
            f"  Net cost (import - export):  without {cb.net_without_eur:,.2f}   "
            f"with {cb.net_with_eur:,.2f}   savings {cb.savings_eur:,.2f} {cur}",
        ]
    )


def _format_dynamic_costs(dyn: DynamicCostBreakdown) -> str:
    c = dyn.contract
    cur = dyn.currency
    vat = c.vat_rate * 100
    feed = f"{c.feed_in_factor:g}x market" + (" incl BTW" if c.feed_in_incl_vat else "")
    return "\n".join(
        [
            f"\nDynamic contract (2027 model — import: market + markup {c.markup_eur:g} + "
            f"energy tax {c.energy_tax_eur:g}, +{vat:g}% BTW; export: {feed}):",
            f"  Avg all-in import price {dyn.avg_import_price_eur_kwh:.4f} {cur}/kWh   "
            f"avg export credit {dyn.avg_export_price_eur_kwh:.4f} {cur}/kWh",
            f"  {'':<12}{'import':>14}{'export':>14}{'net':>14}",
            f"  {'Without':<12}{dyn.import_without_eur:>14,.2f}"
            f"{dyn.export_without_eur:>14,.2f}{dyn.net_without_eur:>14,.2f}",
            f"  {'With':<12}{dyn.import_with_eur:>14,.2f}"
            f"{dyn.export_with_eur:>14,.2f}{dyn.net_with_eur:>14,.2f}",
            f"  Battery savings (dynamic): {dyn.savings_eur:,.2f} {cur}",
        ]
    )


def _format_contract_comparison(costs: CostBreakdown, dyn: DynamicCostBreakdown) -> str:
    cur = dyn.currency
    return "\n".join(
        [
            "\nFixed vs dynamic (net cost = import - export):",
            f"  {'':<18}{'fixed':>14}{'dynamic':>14}",
            f"  {'Without battery':<18}{costs.net_without_eur:>14,.2f}"
            f"{dyn.net_without_eur:>14,.2f}",
            f"  {'With battery':<18}{costs.net_with_eur:>14,.2f}{dyn.net_with_eur:>14,.2f}",
            f"  Battery savings   {'':<2}{costs.savings_eur:>14,.2f}{dyn.savings_eur:>14,.2f}"
            f"   ({cur})",
        ]
    )


def _run(args: argparse.Namespace) -> None:
    input_path = resolve_input_csv(args.input, Path("data"))
    if input_path is None:
        raise CliError(
            "No input CSV found. Run the `energysim` downloader first, or pass --input."
        )

    if args.capacity is not None:
        capacity = args.capacity
        if capacity <= 0:
            raise CliError("--capacity must be greater than 0.")
    else:
        capacity = prompt_float("Battery capacity (kWh)", min_value=0)

    if args.efficiency is not None:
        efficiency = parse_efficiency(args.efficiency)
        if efficiency is None:
            raise CliError(f"Invalid --efficiency: {args.efficiency!r} (use e.g. 90 or 0.9).")
    else:
        efficiency = prompt_efficiency()

    tariff = _resolve_tariff(args)

    print(f"\nReading {input_path} ...")
    df = pd.read_csv(input_path)
    if df.empty:
        raise CliError(f"Input CSV has no rows: {input_path}")

    # Dynamic-contract prices are merged in before the simulation so a future price-aware
    # strategy can use them; the reactive model simply ignores them.
    prices = _resolve_prices(args, df)
    contract = _resolve_contract(args) if prices is not None else None
    if contract is not None:
        df = add_price_columns(df, prices, contract)

    print(
        f"Simulating {capacity:g} kWh battery at {efficiency * 100:g}% round-trip "
        f"over {len(df)} hours ..."
    )
    params = BatteryParams(capacity_kwh=capacity, efficiency=efficiency)
    result, summary = simulate_battery(
        df, params, ReactiveStrategy(), prices=df if contract is not None else None
    )
    energy = split_by_tariff(result)
    costs = compute_costs(energy, tariff) if tariff is not None else None

    dynamic = compute_dynamic_costs(result, contract) if contract is not None else None
    if dynamic is not None:
        result = add_cost_columns(result)

    out_dir = Path(args.out) if args.out else input_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _output_stem(input_path, capacity, efficiency)
    csv_path = out_dir / f"{stem}.csv"
    png_path = out_dir / f"{stem}.png"
    summary_path = out_dir / f"{stem}.summary.json"

    result.to_csv(csv_path, index=False)
    render_chart(result, summary, png_path)
    summary_path.write_text(
        json.dumps(
            {
                "input": str(input_path),
                "output_csv": str(csv_path),
                **summary.as_dict(),
                "energy_by_tariff": energy.as_dict(),
                "costs": costs.as_dict() if costs is not None else None,
                "dynamic_costs": dynamic.as_dict() if dynamic is not None else None,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n" + _format_summary(summary))
    print(_format_energy_tariff(energy))
    if costs is not None:
        print(_format_costs(costs))
    if dynamic is not None:
        print(_format_dynamic_costs(dynamic))
    if costs is not None and dynamic is not None:
        print(_format_contract_comparison(costs, dynamic))
    print(f"\nWrote hourly result to {csv_path}")
    print(f"Chart:   {png_path}")
    print(f"Summary: {summary_path}")


def main() -> int:
    args = _parse_args()
    try:
        _run(args)
    except (CliError, SimulationError, PricesError) as exc:
        print(f"Error: {exc}")
        return 1
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        return 1
    return 0
