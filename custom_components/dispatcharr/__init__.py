"""The Dispatcharr integration."""
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    DispatcharrApiClient,
    DispatcharrAuthError,
    DispatcharrConnectionError,
    DispatcharrPermissionError,
)
from .const import (
    CONF_API_KEY,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import DispatcharrDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Dispatcharr from a config entry."""
    client = DispatcharrApiClient(
        session=async_get_clientsession(hass),
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        use_ssl=entry.data.get(CONF_SSL, False),
        api_key=entry.data.get(CONF_API_KEY),
        username=entry.data.get(CONF_USERNAME),
        password=entry.data.get(CONF_PASSWORD),
    )

    try:
        version_info = await client.get_version()
    except DispatcharrConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = DispatcharrDataUpdateCoordinator(hass, entry, client)
    coordinator.server_version = version_info.get("version")

    try:
        await coordinator.async_populate_channel_map_from_xml()
    except (DispatcharrAuthError, DispatcharrPermissionError) as err:
        raise ConfigEntryAuthFailed(str(err)) from err

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
