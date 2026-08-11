"""HTTP API client for Dispatcharr."""
from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Dispatcharr throttles POST /api/accounts/token/ at 3/minute/IP.
# Space login attempts so a flapping connection cannot burn the budget,
# and back off hard when the server says 429.
LOGIN_COOLDOWN_SECONDS = 25
LOGIN_THROTTLED_BACKOFF_SECONDS = 65


class DispatcharrApiError(Exception):
    """Raised when an API request fails."""


class DispatcharrConnectionError(DispatcharrApiError):
    """Raised when the host cannot be reached."""


class DispatcharrAuthError(DispatcharrApiError):
    """Raised when authentication fails (bad credentials or revoked key)."""


class DispatcharrPermissionError(DispatcharrApiError):
    """Raised when the authenticated user lacks admin rights (user_level < 10)."""


class DispatcharrApiClient:
    """Client for the Dispatcharr HTTP API.

    Supports two auth modes:
    - API key (preferred): stateless ``X-API-Key`` header, no expiry.
    - Username/password: JWT bearer. Access tokens last 30 minutes; the
      refresh endpoint is not rate-limited, so renewal goes through it and
      the throttled login endpoint is only used as a last resort.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        use_ssl: bool,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._session = session
        self._host = host
        self._port = port
        self._use_ssl = use_ssl
        self._api_key = api_key
        self._username = username
        self._password = password
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._next_login_allowed = 0.0

    @property
    def base_url(self) -> str:
        """Base URL for all API calls."""
        protocol = "https" if self._use_ssl else "http"
        return f"{protocol}://{self._host}:{self._port}"

    @property
    def uses_api_key(self) -> bool:
        """Whether this client authenticates with an API key."""
        return self._api_key is not None

    async def _login(self) -> None:
        """Obtain a new token pair, respecting the server's login throttle."""
        now = time.monotonic()
        if now < self._next_login_allowed:
            raise DispatcharrAuthError(
                "Login temporarily suspended to respect Dispatcharr's "
                f"3/minute rate limit; retrying in {self._next_login_allowed - now:.0f}s"
            )
        self._next_login_allowed = now + LOGIN_COOLDOWN_SECONDS

        url = f"{self.base_url}/api/accounts/token/"
        payload = {"username": self._username, "password": self._password}
        try:
            async with self._session.post(url, json=payload) as response:
                if response.status == 429:
                    self._next_login_allowed = (
                        time.monotonic() + LOGIN_THROTTLED_BACKOFF_SECONDS
                    )
                    raise DispatcharrAuthError(
                        "Login rate-limited by Dispatcharr (3/minute); backing off"
                    )
                if response.status in (401, 403):
                    body = await response.text()
                    raise DispatcharrAuthError(
                        f"Login rejected ({response.status}): {body[:200]}"
                    )
                response.raise_for_status()
                tokens = await response.json()
        except aiohttp.ClientError as err:
            raise DispatcharrConnectionError(f"Login request failed: {err}") from err

        self._access_token = tokens.get("access")
        self._refresh_token = tokens.get("refresh")
        if not self._access_token:
            raise DispatcharrAuthError(
                "Authentication succeeded but no access token was returned"
            )
        _LOGGER.debug("Obtained new Dispatcharr token pair via login")

    async def _refresh_access_token(self) -> bool:
        """Renew the access token via the (unthrottled) refresh endpoint."""
        if not self._refresh_token:
            return False

        url = f"{self.base_url}/api/accounts/token/refresh/"
        try:
            async with self._session.post(
                url, json={"refresh": self._refresh_token}
            ) as response:
                if response.status != 200:
                    _LOGGER.debug(
                        "Token refresh failed with status %s", response.status
                    )
                    self._refresh_token = None
                    return False
                tokens = await response.json()
        except aiohttp.ClientError as err:
            raise DispatcharrConnectionError(
                f"Token refresh request failed: {err}"
            ) from err

        self._access_token = tokens.get("access")
        if not self._access_token:
            return False
        _LOGGER.debug("Renewed Dispatcharr access token via refresh")
        return True

    async def _ensure_jwt(self) -> None:
        """Make sure a plausibly-valid access token is available."""
        if self._access_token:
            return
        if not await self._refresh_access_token():
            await self._login()

    async def _auth_headers(self) -> dict[str, str]:
        if self.uses_api_key:
            return {"X-API-Key": self._api_key}
        await self._ensure_jwt()
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _reauthenticate(self) -> bool:
        """Handle a 401. Returns True if a retry is worthwhile."""
        if self.uses_api_key:
            # Keys do not expire; a 401 means it was revoked.
            return False
        self._access_token = None
        if await self._refresh_access_token():
            return True
        await self._login()
        return True

    async def _request(
        self, method: str, path: str, is_json: bool = True, **kwargs: Any
    ) -> Any:
        """Make an authenticated API request."""
        url = f"{self.base_url}{path}"

        try:
            response = await self._session.request(
                method, url, headers=await self._auth_headers(), **kwargs
            )
            if response.status == 401 and await self._reauthenticate():
                response = await self._session.request(
                    method, url, headers=await self._auth_headers(), **kwargs
                )

            if response.status == 401:
                raise DispatcharrAuthError(
                    "Authentication rejected"
                    + (" (API key revoked?)" if self.uses_api_key else "")
                )
            if response.status == 403:
                raise DispatcharrPermissionError(
                    "Dispatcharr rejected the request (403). The configured "
                    "user must be an admin (user_level >= 10), and network "
                    "access rules must allow this client."
                )
            if response.status >= 400:
                body = await response.text()
                raise DispatcharrApiError(
                    f"{method} {path} failed ({response.status}): {body[:300]}"
                )
            return await response.json() if is_json else await response.text()
        except aiohttp.ClientError as err:
            raise DispatcharrConnectionError(
                f"API request to {url} failed: {err}"
            ) from err

    # ---------------------------------------------------------------- public

    async def get_version(self) -> dict[str, Any]:
        """Return server version info. Unauthenticated; good reachability probe."""
        url = f"{self.base_url}/api/core/version/"
        try:
            async with self._session.get(url) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as err:
            raise DispatcharrConnectionError(
                f"Could not reach Dispatcharr at {self.base_url}: {err}"
            ) from err

    async def validate(self) -> str:
        """Validate connectivity, credentials, and admin rights.

        Returns the server version string. Raises DispatcharrConnectionError,
        DispatcharrAuthError, or DispatcharrPermissionError.
        """
        version_info = await self.get_version()
        await self.get_ts_status()
        return version_info.get("version", "unknown")

    async def get_ts_status(self) -> dict[str, Any]:
        """Return the live-stream status summary."""
        return await self._request("GET", "/proxy/ts/status")

    async def get_current_programs(
        self, channel_uuids: list[str]
    ) -> list[dict[str, Any]]:
        """Return the currently-airing program for each given channel UUID.

        Channels without EPG data are simply absent from the response.
        """
        return await self._request(
            "POST",
            "/api/epg/current-programs/",
            json={"channel_uuids": channel_uuids},
        )

    def logo_url(self, logo_id: int) -> str:
        """Public (unauthenticated) URL for a channel logo."""
        return f"{self.base_url}/api/channels/logos/{logo_id}/cache/"
