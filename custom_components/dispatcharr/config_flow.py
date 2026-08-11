"""Config flow for the Dispatcharr integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    DispatcharrApiClient,
    DispatcharrAuthError,
    DispatcharrConnectionError,
    DispatcharrPermissionError,
)
from .const import (
    AUTH_METHOD_API_KEY,
    AUTH_METHOD_CREDENTIALS,
    CONF_API_KEY,
    CONF_AUTH_METHOD,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_SSL, default=False): bool,
        vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_API_KEY): SelectSelector(
            SelectSelectorConfig(
                options=[AUTH_METHOD_API_KEY, AUTH_METHOD_CREDENTIALS],
                mode=SelectSelectorMode.LIST,
                translation_key="auth_method",
            )
        ),
    }
)

STEP_API_KEY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

STEP_CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class DispatcharrConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Dispatcharr."""

    VERSION = 1

    def __init__(self) -> None:
        self._base: dict[str, Any] = {}

    async def _validate(self, auth: dict[str, Any]) -> tuple[dict[str, str], str]:
        """Try connecting with the collected settings.

        Returns (errors, version); errors is empty on success.
        """
        client = DispatcharrApiClient(
            session=async_get_clientsession(self.hass),
            host=self._base[CONF_HOST],
            port=self._base[CONF_PORT],
            use_ssl=self._base[CONF_SSL],
            api_key=auth.get(CONF_API_KEY),
            username=auth.get(CONF_USERNAME),
            password=auth.get(CONF_PASSWORD),
        )
        try:
            version = await client.validate()
        except DispatcharrConnectionError:
            return {"base": "cannot_connect"}, ""
        except DispatcharrPermissionError:
            return {"base": "not_admin"}, ""
        except DispatcharrAuthError:
            return {"base": "invalid_auth"}, ""
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating Dispatcharr connection")
            return {"base": "unknown"}, ""
        return {}, version

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """First step: host, port, SSL, and auth method."""
        if user_input is not None:
            self._base = {
                CONF_HOST: user_input[CONF_HOST],
                CONF_PORT: user_input[CONF_PORT],
                CONF_SSL: user_input[CONF_SSL],
            }
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
            )
            self._abort_if_unique_id_configured()

            if user_input[CONF_AUTH_METHOD] == AUTH_METHOD_API_KEY:
                return await self.async_step_api_key()
            return await self.async_step_credentials()

        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect and validate an API key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, _version = await self._validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title=f"Dispatcharr ({self._base[CONF_HOST]})",
                    data={**self._base, CONF_API_KEY: user_input[CONF_API_KEY]},
                )

        return self.async_show_form(
            step_id="api_key", data_schema=STEP_API_KEY_SCHEMA, errors=errors
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect and validate username/password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, _version = await self._validate(user_input)
            if not errors:
                return self.async_create_entry(
                    title=f"Dispatcharr ({self._base[CONF_HOST]})",
                    data={
                        **self._base,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="credentials", data_schema=STEP_CREDENTIALS_SCHEMA, errors=errors
        )
