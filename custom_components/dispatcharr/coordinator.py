"""DataUpdateCoordinator for Dispatcharr."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    DispatcharrApiClient,
    DispatcharrApiError,
    DispatcharrAuthError,
    DispatcharrPermissionError,
)
from .const import DOMAIN, MANUFACTURER, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class DispatcharrDataUpdateCoordinator(DataUpdateCoordinator):
    """Fetches active-stream status and current EPG programs.

    Data shape: ``dict[channel_uuid, stream_info]`` where ``stream_info`` is
    the /proxy/ts/status summary entry for that channel, plus:
    - ``program``: the currently-airing program from
      /api/epg/current-programs/ (absent if the channel has no EPG data)
    - ``logo_url``: public URL for the channel logo (absent without a logo)
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: DispatcharrApiClient,
    ) -> None:
        """Initialize."""
        self.config_entry = config_entry
        self.client = client
        self.server_version: str | None = None

        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Shared device that groups all entities of this config entry."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.config_entry.entry_id)},
            name="Dispatcharr",
            manufacturer=MANUFACTURER,
            model="IPTV stream manager",
            sw_version=self.server_version,
            configuration_url=self.client.base_url,
        )

    async def _async_update_data(self) -> dict:
        """Fetch active streams, then join the currently-airing programs."""
        try:
            status = await self.client.get_ts_status()
        except (DispatcharrAuthError, DispatcharrPermissionError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except DispatcharrApiError as err:
            raise UpdateFailed(str(err)) from err

        active_streams = status.get("channels") or []
        if not active_streams:
            return {}

        channel_uuids = [
            stream["channel_id"]
            for stream in active_streams
            if stream.get("channel_id")
        ]

        # Program data is enrichment: streams must survive an EPG hiccup.
        programs_by_uuid: dict[str, dict] = {}
        if channel_uuids:
            try:
                programs = await self.client.get_current_programs(channel_uuids)
                programs_by_uuid = {
                    program["channel_uuid"]: program
                    for program in programs
                    if program.get("channel_uuid")
                }
            except DispatcharrApiError as err:
                _LOGGER.warning(
                    "Could not fetch current programs; streams will update "
                    "without EPG data: %s",
                    err,
                )

        data: dict[str, dict] = {}
        for stream in active_streams:
            channel_uuid = stream.get("channel_id")
            if not channel_uuid:
                continue

            enriched = dict(stream)
            if program := programs_by_uuid.get(channel_uuid):
                enriched["program"] = program
            if logo_id := stream.get("logo_id"):
                enriched["logo_url"] = self.client.logo_url(logo_id)

            data[channel_uuid] = enriched

        return data
