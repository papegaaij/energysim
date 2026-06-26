"""Load configuration from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass
class Config:
    token: str
    url: str | None
    verify_ssl: bool


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    """Read HA_TOKEN / HA_URL / HA_VERIFY_SSL from the environment (and .env)."""
    load_dotenv()

    token = os.environ.get("HA_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "HA_TOKEN is not set. Copy .env.example to .env and add your Home "
            "Assistant long-lived access token (Profile -> Security -> Long-lived "
            "access tokens)."
        )

    url = os.environ.get("HA_URL", "").strip() or None
    verify_ssl = _parse_bool(os.environ.get("HA_VERIFY_SSL"), default=True)

    return Config(token=token, url=url, verify_ssl=verify_ssl)
