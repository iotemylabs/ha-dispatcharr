"""Media Player platform for Dispatcharr."""
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerDeviceClass,
    MediaType,
)
from homeassistant.const import STATE_PLAYING
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.exceptions import PlatformNotReady

from .const import DOMAIN
from .coordinator import DispatcharrDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the media_player platform from a ConfigEntry."""
    try:
        coordinator = hass.data[DOMAIN][config_entry.entry_id]
    except KeyError:
        raise PlatformNotReady(f"Coordinator not found for entry {config_entry.entry_id}")

    DispatcharrStreamManager(coordinator, async_add_entities)


class DispatcharrStreamManager:
    """Manages the creation and removal of media_player entities."""
    def __init__(self, coordinator: DispatcharrDataUpdateCoordinator, async_add_entities: AddEntitiesCallback):
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._known_stream_ids = set()
        self._coordinator.async_add_listener(self._update_entities)

    @callback
    def _update_entities(self) -> None:
        """Update, add, or remove entities based on coordinator data."""
        if self._coordinator.data is None:
            current_stream_ids = set()
        else:
            current_stream_ids = set(self._coordinator.data.keys())

        new_stream_ids = current_stream_ids - self._known_stream_ids
        if new_stream_ids:
            new_entities = [DispatcharrStreamMediaPlayer(self._coordinator, stream_id) for stream_id in new_stream_ids]
            self._async_add_entities(new_entities)
            self._known_stream_ids.update(new_stream_ids)

class DispatcharrStreamMediaPlayer(CoordinatorEntity, MediaPlayerEntity):
    """Representation of a single Dispatcharr stream as a Media Player.

    Keyed on the channel UUID from the status payload, which is stable
    across channel renames; the channel name is only the friendly name.
    """
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_supported_features = 0  # Read-only entity supports no features

    def __init__(self, coordinator: DispatcharrDataUpdateCoordinator, stream_id: str):
        super().__init__(coordinator)
        self._stream_id = stream_id

        stream_data = self.coordinator.data.get(self._stream_id) or {}
        name = stream_data.get("channel_name", stream_data.get("stream_name", f"Stream {self._stream_id[-6:]}"))

        self._attr_name = name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{self._stream_id}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        """Return True if the stream is still in the coordinator's data."""
        return super().available and self.coordinator.data is not None and self._stream_id in self.coordinator.data

    @property
    def support_grouping(self) -> bool:
        """Flag if grouping is supported."""
        return False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if not self.available:
            self.async_write_ha_state()
            return

        stream_data = self.coordinator.data[self._stream_id]
        program_data = stream_data.get("program") or {}

        # Set standard media player properties
        self._attr_state = STATE_PLAYING
        self._attr_app_name = "Dispatcharr"
        self._attr_entity_picture = stream_data.get("logo_url")
        self._attr_media_content_type = MediaType.TVSHOW
        self._attr_media_series_title = program_data.get("title")
        self._attr_media_title = program_data.get("sub_title") or program_data.get("title")

        # current-programs returns season/episode as parsed integers
        self._attr_media_season = program_data.get("season")
        self._attr_media_episode = program_data.get("episode")

        # Store other details in extra attributes
        self._attr_extra_state_attributes = {
            "channel_name": stream_data.get("channel_name"),
            "stream_name": stream_data.get("stream_name"),
            "program_description": program_data.get("description"),
            "program_start": program_data.get("start_time"),
            "program_stop": program_data.get("end_time"),
            "clients": stream_data.get("client_count"),
            "avg_bitrate": stream_data.get("avg_bitrate"),
            "resolution": stream_data.get("resolution"),
            "video_codec": stream_data.get("video_codec"),
            "audio_codec": stream_data.get("audio_codec"),
        }
        self.async_write_ha_state()
