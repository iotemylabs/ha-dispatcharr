"""DataUpdateCoordinator for Dispatcharr."""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import slugify

from .api import DispatcharrApiClient, DispatcharrApiError
from .const import DOMAIN, MANUFACTURER, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class DispatcharrDataUpdateCoordinator(DataUpdateCoordinator):
    """Manages fetching and coordinating Dispatcharr data."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        client: DispatcharrApiClient,
    ) -> None:
        """Initialize."""
        self.config_entry = config_entry
        self.client = client
        self.channel_map: dict = {}
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

    async def async_populate_channel_map_from_xml(self) -> None:
        """Fetch the XML file once to build a reliable map of channels."""
        _LOGGER.info("Populating Dispatcharr channel map from XML file...")
        try:
            xml_string = await self.client.get_epg_xml()
        except DispatcharrApiError as err:
            raise ConfigEntryNotReady(
                f"Could not fetch EPG XML file to build channel map: {err}"
            ) from err

        try:
            root = ET.fromstring(xml_string)
            self.channel_map = {}
            for channel in root.iterfind("channel"):
                display_name = channel.findtext("display-name")
                channel_id = channel.get("id")
                icon_tag = channel.find("icon")
                icon_url = icon_tag.get("src") if icon_tag is not None else None

                if display_name and channel_id:
                    slug_name = slugify(display_name)
                    self.channel_map[slug_name] = {
                        "id": channel_id,
                        "name": display_name,
                        "logo_url": icon_url,
                    }

            if not self.channel_map:
                raise ConfigEntryNotReady(
                    "XML was fetched, but no channels could be mapped."
                )

            _LOGGER.info(
                "Successfully built channel map with %d entries.",
                len(self.channel_map),
            )
        except ET.ParseError as e:
            _LOGGER.error("Failed to parse XML for channel map: %s", e)
            raise ConfigEntryNotReady(
                f"Failed to parse XML for channel map: {e}"
            ) from e

    def _get_channel_details_from_stream_name(self, stream_name: str) -> dict | None:
        """Match a stream name to a channel in the map, preferring the longest match."""
        if not stream_name:
            return None

        simple_stream_name = slugify(
            re.sub(r"^\w+:\s*|\s+HD$", "", stream_name, flags=re.IGNORECASE)
        )
        _LOGGER.debug(
            "Attempting to match simplified stream name: '%s'", simple_stream_name
        )

        # 1. Try for a direct, exact match first (most reliable)
        if simple_stream_name in self.channel_map:
            _LOGGER.debug("Found exact match for '%s'", simple_stream_name)
            return self.channel_map[simple_stream_name]

        # 2. If no exact match, find all possible substring matches
        possible_matches = []
        for slug_key, details in self.channel_map.items():
            if slug_key in simple_stream_name:
                possible_matches.append((slug_key, details))

        # 3. If any matches were found, sort them by length and return the longest one
        if possible_matches:
            _LOGGER.debug(
                "Found possible matches: %s", [m[0] for m in possible_matches]
            )
            best_match = sorted(
                possible_matches, key=lambda item: len(item[0]), reverse=True
            )[0]
            _LOGGER.debug("Selected best match: '%s'", best_match[0])
            return best_match[1]

        _LOGGER.debug("Could not find any match for stream name: '%s'", stream_name)
        return None

    async def _async_update_data(self):
        """Update data by fetching from authenticated endpoints."""
        try:
            status_data = await self.client.get_ts_status()
            active_streams = status_data.get("channels", [])
            if not active_streams:
                return {}

            xml_string = await self.client.get_epg_xml()
        except DispatcharrApiError as err:
            raise UpdateFailed(str(err)) from err

        try:
            root = ET.fromstring(xml_string)
        except ET.ParseError as e:
            _LOGGER.error("Could not parse EPG XML on update: %s", e)
            return self.data

        enriched_streams = {}
        now = datetime.now(timezone.utc)

        for stream in active_streams:
            stream_uuid = stream.get("channel_id")
            stream_name = stream.get("stream_name")
            if not stream_uuid or not stream_name:
                continue

            details = self._get_channel_details_from_stream_name(stream_name)
            enriched_stream = stream.copy()

            if details:
                xmltv_id = details["id"]
                enriched_stream["xmltv_id"] = xmltv_id
                enriched_stream["channel_name"] = details["name"]
                enriched_stream["logo_url"] = details.get("logo_url")

                for program in root.iterfind(f".//programme[@channel='{xmltv_id}']"):
                    start_str, stop_str = program.get("start"), program.get("stop")
                    if start_str and stop_str:
                        try:
                            start_time = datetime.strptime(
                                start_str, "%Y%m%d%H%M%S %z"
                            )
                            stop_time = datetime.strptime(stop_str, "%Y%m%d%H%M%S %z")
                            if start_time <= now < stop_time:
                                episode_num_tag = program.find(
                                    "episode-num[@system='onscreen']"
                                )
                                enriched_stream["program"] = {
                                    "title": program.findtext("title"),
                                    "description": program.findtext("desc"),
                                    "start_time": start_time.isoformat(),
                                    "end_time": stop_time.isoformat(),
                                    "subtitle": program.findtext("sub-title"),
                                    "episode_num": episode_num_tag.text
                                    if episode_num_tag is not None
                                    else None,
                                }
                                break
                        except (ValueError, TypeError):
                            continue

            enriched_streams[stream_uuid] = enriched_stream

        return enriched_streams
