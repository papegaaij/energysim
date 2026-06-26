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
    SavingsAttribution,
    add_cost_columns,
    add_price_columns,
    attribute_savings,
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
    SimulationError,
    Summary,
    simulate_battery,
)
from energysim.strategies import STRATEGIES, make_strategy
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

    # Battery charge/discharge strategy.
    parser.add_argument(
        "--strategy", choices=sorted(STRATEGIES), default="reactive",
        help="Battery control strategy (default: reactive). 'threshold'/'optimal' need prices.",
    )
    parser.add_argument(
        "--compare", action="store_true",
        help="Run reactive, threshold and optimal and print a comparison table (needs prices).",
    )
    parser.add_argument(
        "--grid-charge", action=argparse.BooleanOptionalAction, default=None,
        help="Allow charging the battery from the grid (default: on for smart strategies).",
    )
    parser.add_argument(
        "--grid-discharge", action=argparse.BooleanOptionalAction, default=None,
        help="Allow selling battery energy back to the grid (default: off).",
    )
    parser.add_argument("--max-charge-kw", type=float, help="Max charge power (kW); default unlimited.")
    parser.add_argument("--max-discharge-kw", type=float, help="Max discharge power (kW); default unlimited.")
    parser.add_argument(
        "--cycle-cost", type=float, default=0.0,
        help="Battery wear cost per kWh of throughput (EUR/kWh) for smart strategies (default 0).",
    )
    parser.add_argument(
        "--curtail-solar", action="store_true",
        help="Throttle solar instead of exporting at a negative price (needs prices). "
        "Works with any strategy, including reactive.",
    )
    return parser.parse_args(argv)


def _battery_params(args: argparse.Namespace, capacity: float, efficiency: float, strategy_name: str) -> BatteryParams:
    """Build BatteryParams, defaulting grid trading on/off per strategy unless flags override."""
    smart = strategy_name != "reactive"
    grid_charge = args.grid_charge if args.grid_charge is not None else smart
    grid_discharge = args.grid_discharge if args.grid_discharge is not None else False
    if not smart:  # the reactive model never trades with the grid
        grid_charge = grid_discharge = False
    return BatteryParams(
        capacity_kwh=capacity,
        efficiency=efficiency,
        max_charge_kwh_per_step=args.max_charge_kw,
        max_discharge_kwh_per_step=args.max_discharge_kw,
        allow_grid_charge=grid_charge,
        allow_grid_discharge=grid_discharge,
        curtail_negative_export=args.curtail_solar,
    )


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
    if summary.total_curtailed_kwh > 0:
        lines.append(
            f"Solar curtailed:  {summary.total_curtailed_kwh:,.0f} kWh "
            "(throttled to avoid exporting at a negative price)"
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


def _format_savings_attribution(attr: SavingsAttribution) -> str:
    lines = [
        "\nWhere the dynamic saving comes from (vs no battery, no curtailment):",
        f"  {'Self-consumption (stored solar -> load)':<42}{attr.self_consumption_eur:>10,.2f}",
        f"  {'Grid arbitrage (cheap grid -> load)':<42}{attr.grid_arbitrage_eur:>10,.2f}",
        f"  {'Sell-back (battery -> grid)':<42}{attr.sell_back_eur:>10,.2f}",
    ]
    if abs(attr.curtailment_eur) >= 0.005:
        lines.append(f"  {'Solar curtailment (avoided export fee)':<42}{attr.curtailment_eur:>10,.2f}")
    if abs(attr.unused_end_soc_eur) >= 0.005:
        lines.append(f"  {'Unused stored energy (end SoC)':<42}{attr.unused_end_soc_eur:>10,.2f}")
    lines.append(f"  {'Total saving':<42}{attr.total_eur:>10,.2f}  EUR")
    return "\n".join(lines)


def _format_attribution_table(attributions: dict) -> str:
    show_curtail = any(abs(a.curtailment_eur) >= 0.005 for a in attributions.values())
    header = f"  {'strategy':<12}{'self-consum':>13}{'grid-arb':>11}{'sell-back':>11}"
    if show_curtail:
        header += f"{'curtail':>10}"
    header += f"{'total':>10}"
    lines = ["\nSaving by source (EUR, dynamic contract):", header]
    for name, attr in attributions.items():
        row = (
            f"  {name:<12}{attr.self_consumption_eur:>13,.2f}{attr.grid_arbitrage_eur:>11,.2f}"
            f"{attr.sell_back_eur:>11,.2f}"
        )
        if show_curtail:
            row += f"{attr.curtailment_eur:>10,.2f}"
        row += f"{attr.total_eur:>10,.2f}"
        lines.append(row)
    return "\n".join(lines)


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

    # Smart strategies, --compare and --curtail-solar all need per-hour prices.
    needs_prices = args.compare or args.strategy != "reactive" or args.curtail_solar
    if needs_prices and contract is None:
        raise CliError(
            "The threshold/optimal strategies, --compare and --curtail-solar need market "
            "prices; pass --prices <file> or --fetch-prices."
        )

    out_dir = Path(args.out) if args.out else input_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.compare:
        _run_compare(args, df, input_path, out_dir, capacity, efficiency, contract, tariff)
        return

    strategy_name = args.strategy
    params = _battery_params(args, capacity, efficiency, strategy_name)
    print(
        f"Simulating {capacity:g} kWh battery [{strategy_name}] at {efficiency * 100:g}% "
        f"round-trip over {len(df)} hours ..."
    )
    result, summary = simulate_battery(
        df, params, make_strategy(strategy_name, cycle_cost=args.cycle_cost),
        prices=df if contract is not None else None,
    )
    energy = split_by_tariff(result)
    # A price-aware strategy optimises the *dynamic* hourly signal, so its grid-trading flows
    # are meaningless under a flat fixed tariff (which has no hourly spread to arbitrage) — the
    # fixed-contract cost of those flows would be nonsensically bad. Only price the fixed
    # contract for the price-agnostic reactive strategy.
    fixed_applies = strategy_name == "reactive"
    costs = compute_costs(energy, tariff) if (tariff is not None and fixed_applies) else None

    dynamic = compute_dynamic_costs(result, contract) if contract is not None else None
    attribution = attribute_savings(result, efficiency) if dynamic is not None else None
    if dynamic is not None:
        result = add_cost_columns(result)

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
                "strategy": strategy_name,
                **summary.as_dict(),
                "energy_by_tariff": energy.as_dict(),
                "costs": costs.as_dict() if costs is not None else None,
                "dynamic_costs": dynamic.as_dict() if dynamic is not None else None,
                "dynamic_savings_attribution": (
                    attribution.as_dict() if attribution is not None else None
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n" + _format_summary(summary))
    print(_format_energy_tariff(energy))
    if costs is not None:
        print(_format_costs(costs))
    if tariff is not None and not fixed_applies:
        print(
            f"\nNote: fixed-contract costs are omitted for the '{strategy_name}' strategy. It "
            "optimises the dynamic hourly price signal, which a flat fixed tariff doesn't have, "
            "so pricing its grid trades under a fixed contract is not meaningful. Use "
            "--strategy reactive for the fixed-contract battery scenario."
        )
    if dynamic is not None:
        print(_format_dynamic_costs(dynamic))
    if costs is not None and dynamic is not None:
        print(_format_contract_comparison(costs, dynamic))
    if attribution is not None:
        print(_format_savings_attribution(attribution))
    print(f"\nWrote hourly result to {csv_path}")
    print(f"Chart:   {png_path}")
    print(f"Summary: {summary_path}")


def _run_compare(args, df, input_path, out_dir, capacity, efficiency, contract, tariff) -> None:
    """Report a decision matrix: each contract run with its sensible control strategy.

    Smart strategies only make sense on the dynamic contract; the fixed contract is paired with
    the reactive (self-consumption) battery, which is the right control for a flat tariff. Each
    scenario is priced under its own contract, so the net costs are directly comparable.
    """
    attributions = {}
    dyn_results = {}
    for name in ("reactive", "threshold", "optimal"):
        params = _battery_params(args, capacity, efficiency, name)
        print(f"Simulating dynamic [{name}] ...")
        result, summary = simulate_battery(
            df, params, make_strategy(name, cycle_cost=args.cycle_cost), prices=df
        )
        dyn_results[name] = (result, summary, compute_dynamic_costs(result, contract))
        attributions[name] = attribute_savings(result, efficiency)

    scenarios = []
    if tariff is not None:
        # Fixed contract: only the reactive battery is meaningful (no hourly spread to arbitrage).
        fixed = compute_costs(split_by_tariff(dyn_results["reactive"][0]), tariff)
        scenarios.append(
            {"contract": "Fixed", "control": "reactive",
             "without": fixed.net_without_eur, "with": fixed.net_with_eur, "grid_charged": 0.0}
        )
    for name in ("reactive", "threshold", "optimal"):
        _, summ, dyn = dyn_results[name]
        scenarios.append(
            {"contract": "Dynamic", "control": name,
             "without": dyn.net_without_eur, "with": dyn.net_with_eur,
             "grid_charged": summ.total_grid_charged_kwh}
        )

    print(_format_scenario_comparison(scenarios))
    print(_format_attribution_table(attributions))

    stem = _output_stem(input_path, capacity, efficiency)
    out_path = out_dir / f"{stem}.strategies.json"
    out_path.write_text(
        json.dumps(
            {
                "input": str(input_path),
                "capacity_kwh": capacity,
                "efficiency": efficiency,
                "scenarios": [
                    {
                        "contract": s["contract"],
                        "control": s["control"],
                        "no_battery_net_eur": round(s["without"], 2),
                        "with_battery_net_eur": round(s["with"], 2),
                        "battery_saving_eur": round(s["without"] - s["with"], 2),
                        "grid_charged_kwh": round(s["grid_charged"], 1),
                        "saving_attribution": (
                            attributions[s["control"]].as_dict()
                            if s["contract"] == "Dynamic" else None
                        ),
                    }
                    for s in scenarios
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote comparison to {out_path}")


def _format_scenario_comparison(scenarios: list) -> str:
    best = min(range(len(scenarios)), key=lambda i: scenarios[i]["with"])
    lines = [
        "\nScenario comparison (net cost EUR/yr, lower is better; each priced on its own contract):",
        f"  {'contract':<9}{'control':<11}{'no battery':>12}{'with battery':>14}"
        f"{'battery saves':>15}",
    ]
    for i, s in enumerate(scenarios):
        mark = "  <-- cheapest" if i == best else ""
        lines.append(
            f"  {s['contract']:<9}{s['control']:<11}{s['without']:>12,.2f}{s['with']:>14,.2f}"
            f"{s['without'] - s['with']:>15,.2f}{mark}"
        )
    return "\n".join(lines)


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
