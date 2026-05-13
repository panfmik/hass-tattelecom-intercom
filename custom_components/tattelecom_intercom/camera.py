"""Camera component."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final

from homeassistant.components import ffmpeg
from homeassistant.components.camera import ENTITY_ID_FORMAT, Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_SIP_LOGIN,
    ATTR_STREAM_URL,
    CAMERA_INCOMING,
    CAMERA_INCOMING_NAME,
    CAMERA_NAME,
    CONF_STREAM_TYPES,
    DEFAULT_STREAM_TYPES,
    MAINTAINER,
    SIGNAL_CALL_STATE,
    SIGNAL_NEW_INTERCOM,
    STREAM_TYPE_MPEG,
    STREAM_TYPE_HLS,
)
from .entity import IntercomEntity
from .enum import CallState
from .updater import IntercomEntityDescription, IntercomUpdater, async_get_updater

PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

EVENTS: Final = {
    CAMERA_INCOMING: SIGNAL_CALL_STATE,
}

CAMERAS: tuple[EntityDescription, ...] = (
    EntityDescription(
        key=CAMERA_INCOMING,
        name=CAMERA_INCOMING_NAME,
        icon="mdi:phone-incoming",
        entity_registry_enabled_default=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tattelecom intercom camera entry."""
    updater: IntercomUpdater = async_get_updater(hass, config_entry.entry_id)
    stream_types = config_entry.options.get(CONF_STREAM_TYPES, DEFAULT_STREAM_TYPES)

    @callback
    def add_camera(entity: IntercomEntityDescription) -> None:
        """Add cameras for each stream type."""
        for stream_type in stream_types:
            if stream_type == STREAM_TYPE_MPEG:
                data_key = f"{entity.id}_{ATTR_STREAM_URL}_mpeg"
                suffix = "mpeg"
                name_suffix = "MPEG"
                stream_type_val = STREAM_TYPE_MPEG
            elif stream_type == STREAM_TYPE_HLS:
                data_key = f"{entity.id}_{ATTR_STREAM_URL}_hls"
                suffix = "hls"
                name_suffix = "HLS"
                stream_type_val = STREAM_TYPE_HLS
            else:
                continue

            source = updater.data.get(data_key, "")

            camera_description = EntityDescription(
                key=f"{entity.id}_{suffix}",
                name=f"{CAMERA_NAME} ({name_suffix})",
                icon="mdi:doorbell-video",
                entity_registry_enabled_default=True,
            )

            async_add_entities([
                IntercomCamera(
                    f"{config_entry.entry_id}-camera-{entity.id}-{suffix}",
                    camera_description,
                    updater,
                    entity.device_info,
                    source,
                    stream_type_val,
                )
            ])

    entities = [
        IntercomCamera(
            f"{config_entry.entry_id}-{description.key}",
            description,
            updater,
            None,
            None,
            None,
        )
        for description in CAMERAS
    ]
    async_add_entities(entities)

    for intercom in updater.intercoms.values():
        add_camera(intercom)

    updater.new_intercom_callbacks.append(
        async_dispatcher_connect(hass, SIGNAL_NEW_INTERCOM, add_camera)
    )


class IntercomCamera(IntercomEntity, Camera):
    """Intercom camera entry."""

    _CACHE_TIMEOUT: float = 5.0
    _MAX_ERRORS: int = 3
    _RECOVERY_INTERVAL: float = 30.0
    _RETRY_ATTEMPTS: int = 2
    _RETRY_DELAY: float = 1.0

    _unsub_update: CALLBACK_TYPE | None = None
    _last_image: bytes | None = None
    _last_image_time: float = 0.0
    _last_image_url: str = ""
    _error_count: int = 0
    _last_error_time: float = 0.0

    def __init__(
        self,
        unique_id: str,
        description: EntityDescription,
        updater: IntercomUpdater,
        device_info: DeviceInfo | None = None,
        stream_source: str | None = None,
        stream_type: str | None = None,
    ) -> None:
        """Initialize camera."""
        IntercomEntity.__init__(self, unique_id, description, updater, ENTITY_ID_FORMAT)
        Camera.__init__(self)

        self._attr_brand = MAINTAINER
        self._attr_stream_source = stream_source or ""
        self._attr_is_streaming = bool(stream_source)
        self._attr_supported_features = CameraEntityFeature.STREAM
        self._attr_extra_state_attributes = {}
        self._stream_type = stream_type

        if description.key != CAMERA_INCOMING and stream_source:
            self._attr_extra_state_attributes = {
                "stream_url": stream_source,
                ATTR_SIP_LOGIN: updater.data.get(f"{description.key}_{ATTR_SIP_LOGIN}"),
            }

        if device_info:
            self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        """Return if camera is available."""
        if not self._attr_stream_source:
            return False
        if self._error_count >= self._MAX_ERRORS:
            if time.time() - self._last_error_time < self._RECOVERY_INTERVAL:
                return False
            self._error_count = 0
        return True

    async def stream_source(self) -> str | None:
        """Return the stream source URL."""
        return self._attr_stream_source if self._attr_stream_source else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = super().extra_state_attributes or {}
        attrs.update(self._attr_extra_state_attributes)
        return attrs

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._updater.async_add_listener(self._handle_coordinator_update)
        )

        if self.entity_description.key in EVENTS:
            self._unsub_update = async_dispatcher_connect(
                self.hass,
                EVENTS[self.entity_description.key],
                self._handle_event_update,
            )
            self.async_on_remove(self._unsub_update)

    @callback
    def _handle_event_update(self) -> None:
        """Update state from event."""
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update state from coordinator."""
        key = f"{self.entity_description.key}_{ATTR_STREAM_URL}"

        if self.entity_description.key == CAMERA_INCOMING:
            if (self._updater.last_call
                and self._updater.last_call.login in self._updater.code_map
                and self._updater.last_call.state in (CallState.RINGING, CallState.ANSWERED)):
                gate_id = self._updater.code_map[self._updater.last_call.login]
                if self._stream_type == STREAM_TYPE_MPEG:
                    key = f"{gate_id}_{ATTR_STREAM_URL}_mpeg"
                else:
                    key = f"{gate_id}_{ATTR_STREAM_URL}_hls"
            else:
                return

        new_stream_url = self._updater.data.get(key, "")

        if self._attr_stream_source == new_stream_url:
            return

        self._attr_stream_source = new_stream_url
        self._attr_is_streaming = bool(new_stream_url)

        if new_stream_url:
            self._attr_extra_state_attributes["stream_url"] = new_stream_url

        self.async_write_ha_state()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image response from the camera."""
        if not self._attr_stream_source:
            return None

        current_time = time.time()

        if (self._last_image is not None and
            self._last_image_url == self._attr_stream_source and
            current_time - self._last_image_time < self._CACHE_TIMEOUT):
            return self._last_image

        extra_cmd = ""
        if self._stream_type == STREAM_TYPE_MPEG:
            extra_cmd = "-frames:v 1 -skip_frame nokey"
        elif self._stream_type == STREAM_TYPE_HLS:
            extra_cmd = "-flags low_delay -fflags +discardcorrupt"

        for attempt in range(self._RETRY_ATTEMPTS):
            try:
                snapshot = await ffmpeg.async_get_image(
                    self.hass,
                    self._attr_stream_source,
                    extra_cmd=extra_cmd,
                    width=width,
                    height=height,
                )
                if snapshot:
                    self._last_image = snapshot
                    self._last_image_time = current_time
                    self._last_image_url = self._attr_stream_source
                    self._error_count = 0
                    return snapshot
            except Exception as err:
                _LOGGER.debug(
                    "Camera %s ffmpeg attempt %d/%d failed: %s",
                    self.entity_id, attempt + 1, self._RETRY_ATTEMPTS, err
                )
                if attempt < self._RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(self._RETRY_DELAY)

        self._error_count += 1
        self._last_error_time = current_time
        return None