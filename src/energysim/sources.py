"""Turn an `energy/get_prefs` response into the metrics we want to download."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class MetricGroup:
    """One output column, fed by one or more Home Assistant statistic ids."""

    role: str  # grid_import, grid_export, solar_production, battery_charge, ...
    base_name: str  # column name without unit suffix
    stat_ids: list[str] = field(default_factory=list)
    electric: bool = True  # True -> normalised to kWh; False -> keep HA unit (gas/water)


def _slug(text: str) -> str:
    slug = re.sub(r"[^0-9a-z]+", "_", text.strip().lower()).strip("_")
    return slug or "device"


def extract_metric_groups(prefs: dict) -> list[MetricGroup]:
    """Map Energy dashboard preferences to ordered metric groups.

    Multiple sources of the same role (e.g. two grid meters) are collected into one
    group and summed downstream, mirroring the Energy dashboard.
    """
    grid_import: list[str] = []
    grid_export: list[str] = []
    solar: list[str] = []
    battery_charge: list[str] = []
    battery_discharge: list[str] = []
    gas: list[str] = []
    water: list[str] = []

    for source in prefs.get("energy_sources", []):
        kind = source.get("type")
        if kind == "grid":
            # Current ("unified") format: import/export are source-level fields.
            if source.get("stat_energy_from"):
                grid_import.append(source["stat_energy_from"])
            if source.get("stat_energy_to"):
                grid_export.append(source["stat_energy_to"])
            # Legacy format: import/export nested under flow_from / flow_to lists.
            for flow in source.get("flow_from", []):
                if flow.get("stat_energy_from"):
                    grid_import.append(flow["stat_energy_from"])
            for flow in source.get("flow_to", []):
                if flow.get("stat_energy_to"):
                    grid_export.append(flow["stat_energy_to"])
        elif kind == "solar":
            if source.get("stat_energy_from"):
                solar.append(source["stat_energy_from"])
        elif kind == "battery":
            # stat_energy_from = energy out of the battery (discharge to home);
            # stat_energy_to = energy into the battery (charge from home/solar).
            if source.get("stat_energy_from"):
                battery_discharge.append(source["stat_energy_from"])
            if source.get("stat_energy_to"):
                battery_charge.append(source["stat_energy_to"])
        elif kind == "gas":
            if source.get("stat_energy_from"):
                gas.append(source["stat_energy_from"])
        elif kind == "water":
            if source.get("stat_energy_from"):
                water.append(source["stat_energy_from"])

    groups: list[MetricGroup] = []

    def add(role: str, base_name: str, stat_ids: list[str], electric: bool) -> None:
        unique_ids = list(dict.fromkeys(stat_ids))  # dedupe, preserve order
        if unique_ids:
            groups.append(MetricGroup(role, base_name, unique_ids, electric))

    add("grid_import", "grid_import", grid_import, True)
    add("grid_export", "grid_export", grid_export, True)
    add("solar_production", "solar_production", solar, True)
    add("battery_charge", "battery_charge", battery_charge, True)
    add("battery_discharge", "battery_discharge", battery_discharge, True)
    add("gas", "gas", gas, False)
    add("water", "water", water, False)

    used_names = {g.base_name for g in groups}
    for device in prefs.get("device_consumption", []):
        stat_id = device.get("stat_consumption")
        if not stat_id:
            continue
        base = _slug(device.get("name") or stat_id)
        # Avoid clashing with role columns or other devices.
        name = base
        suffix = 2
        while name in used_names:
            name = f"{base}_{suffix}"
            suffix += 1
        used_names.add(name)
        groups.append(MetricGroup("device", name, [stat_id], True))

    return groups
