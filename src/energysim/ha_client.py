"""Minimal async Home Assistant WebSocket API client.

Only the handful of commands this tool needs are implemented:
- authentication handshake
- get_config (for the instance timezone)
- energy/get_prefs (Energy dashboard configuration)
- recorder/list_statistic_ids (units per statistic)
- recorder/statistics_during_period (the hourly data)
"""

from __future__ import annotations

import json
import ssl
from datetime import datetime
from urllib.parse import urlparse

import websockets


class HAError(Exception):
    """Base class for Home Assistant client errors."""


class AuthError(HAError):
    """Raised when authentication with Home Assistant fails."""


class CommandError(HAError):
    """Raised when a WebSocket command returns an error result."""


class HAClient:
    """Async context manager wrapping a single authenticated WebSocket connection."""

    def __init__(self, ws_url: str, token: str, verify_ssl: bool = True):
        self._ws_url = ws_url
        self._token = token
        self._verify_ssl = verify_ssl
        self._ws = None
        self._id = 0

    async def __aenter__(self) -> "HAClient":
        ssl_context = None
        if urlparse(self._ws_url).scheme == "wss":
            ssl_context = ssl.create_default_context()
            if not self._verify_ssl:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

        # max_size=None: statistics responses for long ranges can exceed the 1 MiB default.
        self._ws = await websockets.connect(
            self._ws_url, ssl=ssl_context, max_size=None, open_timeout=30
        )
        await self._authenticate()
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _authenticate(self) -> None:
        # HA sends an `auth_required` frame first.
        hello = json.loads(await self._ws.recv())
        if hello.get("type") != "auth_required":
            raise AuthError(f"Unexpected first message from server: {hello!r}")

        await self._ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        result = json.loads(await self._ws.recv())
        if result.get("type") != "auth_ok":
            raise AuthError(
                result.get("message")
                or "Authentication failed. Check that HA_TOKEN is a valid, "
                "non-expired long-lived access token."
            )

    async def _command(self, command_type: str, **payload):
        if self._ws is None:
            raise HAError("Not connected.")
        self._id += 1
        message_id = self._id
        await self._ws.send(
            json.dumps({"id": message_id, "type": command_type, **payload})
        )
        # We never subscribe to events, so only `result` frames arrive. Read until ours.
        while True:
            message = json.loads(await self._ws.recv())
            if message.get("id") != message_id:
                continue
            if not message.get("success", False):
                error = message.get("error", {})
                raise CommandError(
                    f"{command_type} failed: "
                    f"{error.get('code', '?')} {error.get('message', '')}".strip()
                )
            return message.get("result")

    async def get_config(self) -> dict:
        return await self._command("get_config")

    async def get_energy_prefs(self) -> dict:
        return await self._command("energy/get_prefs")

    async def list_statistic_ids(self) -> list[dict]:
        return await self._command(
            "recorder/list_statistic_ids", statistic_type="sum"
        )

    async def statistics_during_period(
        self,
        start: datetime,
        end: datetime,
        statistic_ids: list[str],
    ) -> dict:
        return await self._command(
            "recorder/statistics_during_period",
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            statistic_ids=statistic_ids,
            period="hour",
            types=["change", "sum", "state"],
        )
