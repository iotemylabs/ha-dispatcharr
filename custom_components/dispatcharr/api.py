"""HTTP API client for Dispatcharr."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class DispatcharrApiError(Exception):
    """Raised when an API request fails."""


class DispatcharrAuthError(DispatcharrApiError):
    """Raised when authentication fails."""


class DispatcharrApiClient:
    """Client for the Dispatcharr HTTP API using JWT authentication."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        use_ssl: bool,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._use_ssl = use_ssl
        self._username = username
        self._password = password
        self._access_token: str | None = None

    @property
    def base_url(self) -> str:
        """Base URL for all API calls."""
        protocol = "https" if self._use_ssl else "http"
        return f"{protocol}://{self._host}:{self._port}"

    async def _login(self) -> None:
        """Obtain a new access token using username and password."""
        url = f"{self.base_url}/api/accounts/token/"
        auth_data = {"username": self._username, "password": self._password}
        try:
            async with self._session.post(url, json=auth_data) as response:
                response.raise_for_status()
                tokens = await response.json()
        except aiohttp.ClientError as err:
            raise DispatcharrAuthError(f"Authentication failed: {err}") from err

        self._access_token = tokens.get("access")
        if not self._access_token:
            raise DispatcharrAuthError(
                "Authentication successful, but no access token received."
            )
        _LOGGER.info("Successfully authenticated with Dispatcharr")

    async def _request(
        self, method: str, path: str, is_json: bool = True, **kwargs: Any
    ) -> Any:
        """Make an authenticated API request, re-authenticating once on 401."""
        if not self._access_token:
            await self._login()

        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            response = await self._session.request(
                method, url, headers=headers, **kwargs
            )
            if response.status == 401:
                _LOGGER.info("Access token expired or invalid, requesting a new one.")
                await self._login()
                headers["Authorization"] = f"Bearer {self._access_token}"
                response = await self._session.request(
                    method, url, headers=headers, **kwargs
                )

            response.raise_for_status()
            return await response.json() if is_json else await response.text()
        except aiohttp.ClientError as err:
            raise DispatcharrApiError(f"API request to {url} failed: {err}") from err

    async def get_ts_status(self) -> dict[str, Any]:
        """Return the live-stream status summary."""
        return await self._request("GET", "/proxy/ts/status")

    async def get_epg_xml(self) -> str:
        """Return the full XMLTV EPG document."""
        return await self._request("GET", "/output/epg", is_json=False)
