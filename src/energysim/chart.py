"""Render the simulation result to a PNG chart (headless)."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: render to file, never open a window

import matplotlib.pyplot as plt
import pandas as pd

from energysim.simulate import Summary
from energysim.timeutil import local_timestamps


def render_chart(df: pd.DataFrame, summary: Summary, png_path: Path) -> Path:
    """Render totals comparison, battery SoC over time, and a monthly breakdown.

    When the frame carries dynamic-price columns (added by dynamic_pricing), a fourth panel
    with the monthly average prices is appended.
    """
    times = local_timestamps(df)
    has_prices = "import_price_eur_kwh" in df.columns

    n_panels = 4 if has_prices else 3
    fig, axes = plt.subplots(
        n_panels, 1, figsize=(12, 13 + 4 * (n_panels - 3)), constrained_layout=True
    )
    ax_totals, ax_soc, ax_month = axes[0], axes[1], axes[2]
    fig.suptitle(
        f"Battery simulation — {summary.capacity_kwh:g} kWh, "
        f"{summary.efficiency * 100:g}% round-trip",
        fontsize=14,
        fontweight="bold",
    )

    # 1) Totals comparison.
    groups = ["Import", "Export"]
    without = [summary.import_without_kwh, summary.export_without_kwh]
    with_bat = [summary.import_with_kwh, summary.export_with_kwh]
    x = range(len(groups))
    width = 0.38
    b1 = ax_totals.bar([i - width / 2 for i in x], without, width, label="Without battery",
                       color="#9e9e9e")
    b2 = ax_totals.bar([i + width / 2 for i in x], with_bat, width, label="With battery",
                       color="#1f77b4")
    ax_totals.bar_label(b1, fmt="%.0f", padding=2, fontsize=9)
    ax_totals.bar_label(b2, fmt="%.0f", padding=2, fontsize=9)
    ax_totals.set_xticks(list(x), groups)
    ax_totals.set_ylabel("kWh")
    ax_totals.set_title("Grid totals: with vs without battery")
    ax_totals.legend()

    # 2) Battery state of charge over time.
    ax_soc.plot(times, df["battery_soc_kwh"], color="#2ca02c", linewidth=0.8)
    ax_soc.axhline(summary.capacity_kwh, color="#d62728", linestyle="--", linewidth=0.8,
                   label=f"Capacity ({summary.capacity_kwh:g} kWh)")
    ax_soc.set_ylabel("State of charge (kWh)")
    ax_soc.set_ylim(bottom=0)
    ax_soc.set_title("Battery state of charge over time")
    ax_soc.legend(loc="upper right")

    # 3) Monthly import/export, with vs without battery.
    monthly = (
        df[["grid_import_kwh", "grid_export_kwh", "grid_import_sim_kwh",
            "grid_export_sim_kwh"]]
        .set_axis(times)
        .resample("MS")
        .sum()
    )
    labels = [d.strftime("%Y-%m") for d in monthly.index]
    mx = range(len(labels))
    w = 0.2
    ax_month.bar([i - 1.5 * w for i in mx], monthly["grid_import_kwh"], w,
                 label="Import (without)", color="#c7c7c7")
    ax_month.bar([i - 0.5 * w for i in mx], monthly["grid_import_sim_kwh"], w,
                 label="Import (with)", color="#1f77b4")
    ax_month.bar([i + 0.5 * w for i in mx], monthly["grid_export_kwh"], w,
                 label="Export (without)", color="#ffbb78")
    ax_month.bar([i + 1.5 * w for i in mx], monthly["grid_export_sim_kwh"], w,
                 label="Export (with)", color="#ff7f0e")
    ax_month.set_xticks(list(mx), labels, rotation=45, ha="right")
    ax_month.set_ylabel("kWh")
    ax_month.set_title("Monthly grid import/export: with vs without battery")
    ax_month.legend(ncol=2, fontsize=9)

    # 4) Dynamic prices (only when present): monthly average all-in import, export and market.
    if has_prices:
        ax_price = axes[3]
        monthly_price = (
            df[["import_price_eur_kwh", "export_price_eur_kwh", "market_price_eur_kwh"]]
            .set_axis(times)
            .resample("MS")
            .mean()
        )
        plabels = [d.strftime("%Y-%m") for d in monthly_price.index]
        ax_price.plot(plabels, monthly_price["import_price_eur_kwh"], marker="o",
                      label="All-in import", color="#1f77b4")
        ax_price.plot(plabels, monthly_price["export_price_eur_kwh"], marker="o",
                      label="Export credit", color="#ff7f0e")
        ax_price.plot(plabels, monthly_price["market_price_eur_kwh"], marker="o",
                      linestyle="--", label="Bare market", color="#9e9e9e")
        ax_price.axhline(0, color="black", linewidth=0.6)
        ax_price.set_ylabel("EUR/kWh")
        ax_price.set_title("Monthly average dynamic prices")
        ax_price.set_xticks(range(len(plabels)), plabels, rotation=45, ha="right")
        ax_price.legend(fontsize=9)

    png_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png_path, dpi=110)
    plt.close(fig)
    return png_path
