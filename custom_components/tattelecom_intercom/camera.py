"""Camera component."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Final

import httpx
from homeassistant.components import ffmpeg
from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    ENTITY_ID_FORMAT as CAMERA_ENTITY_ID_FORMAT,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_SIP_LOGIN,
    ATTR_STREAM_URL,
    ATTR_STREAM_URL_MPEG,
    ATTR_STREAM_TYPE,
    CAMERA_INCOMING,
    CAMERA_INCOMING_NAME,
    CAMERA_NAME,
    MAINTAINER,
    SIGNAL_CALL_STATE,
    SIGNAL_NEW_INTERCOM,
    STREAM_TYPE_MPEG,
    STREAM_TYPE_HLS,
)
from .entity import IntercomEntity
from .intercom_enum import CallState
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
        translation_key="incoming",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tattelecom intercom camera entry."""
    updater: IntercomUpdater = async_get_updater(hass, config_entry.entry_id)

    @callback
    def add_camera(entity: IntercomEntityDescription) -> None:
        """Add cameras for each stream type."""
        for stream_type in updater.stream_types:
            if stream_type == "webrtc":
                continue
                
            async_add_entities([
                IntercomCamera(
                    f"{config_entry.entry_id}-camera-{entity.id}-{stream_type}",
                    EntityDescription(
                        key=f"{entity.id}_{stream_type}",
                        name=f"{CAMERA_NAME} ({stream_type.upper()})",
                        icon="mdi:doorbell-video",
                        entity_registry_enabled_default=True,
                        translation_key="camera",
                    ),
                    updater,
                    entity.device_info,
                    stream_type=stream_type,
                )
            ])

    entities = [
        IntercomCamera(
            f"{config_entry.entry_id}-{description.key}",
            description,
            updater,
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
    _intercom_id: int | None = None
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
        stream_type: str | None = None,
    ) -> None:
        """Initialize camera."""
        IntercomEntity.__init__(self, unique_id, description, updater, CAMERA_ENTITY_ID_FORMAT)
        Camera.__init__(self)

        self._attr_brand = MAINTAINER
        self._attr_stream_url = ""
        self._attr_stream_type = stream_type or "unknown"
        self._attr_is_streaming = True
        self._attr_supported_features = CameraEntityFeature.STREAM
        self._attr_extra_state_attributes = {}
        
        if description.key not in [CAMERA_INCOMING]:
            try:
                self._intercom_id = int(description.key.split('_')[0])
            except ValueError:
                self._intercom_id = None
                _LOGGER.error("Invalid intercom ID: %s", description.key)
        
        self._stream_type = stream_type
        
        if device_info:
            self._attr_device_info = device_info

        self._update_stream_url()

    def _get_url_key(self) -> str:
        """Get the data key for current stream type."""
        if self._stream_type == STREAM_TYPE_HLS:
            return f"{self._intercom_id}_{ATTR_STREAM_URL}_hls"
        elif self._stream_type == STREAM_TYPE_MPEG:
            return f"{self._intercom_id}_{ATTR_STREAM_URL}_mpeg"
        else:
            return f"{self._intercom_id}_{ATTR_STREAM_URL}"

    def _update_stream_url(self) -> bool:
        """Update stream URL from updater data."""
        if not self._intercom_id or self._updater.data is None:
            return False

        url_key = self._get_url_key()
        sip_key = f"{self._intercom_id}_{ATTR_SIP_LOGIN}"
        
        new_stream_url = self._updater.data.get(url_key, "")
        sip_login = self._updater.data.get(sip_key)

        new_attributes = {}
        if new_stream_url:
            new_attributes = {
                ATTR_STREAM_URL: new_stream_url,
                ATTR_STREAM_TYPE: self._stream_type,
                ATTR_SIP_LOGIN: sip_login,
            }

        changed = (
            new_stream_url != self._attr_stream_url
            or new_attributes != self._attr_extra_state_attributes
        )

        if new_stream_url != self._attr_stream_url:
            self._last_image = None
            self._last_image_time = 0.0
            self._last_image_url = ""
            self._error_count = 0

        self._attr_stream_url = new_stream_url
        self._attr_extra_state_attributes = new_attributes

        if changed and self._stream_type == STREAM_TYPE_MPEG:
            _LOGGER.debug("Camera %s MPEG URL updated", self.entity_id)

        return changed

    @property
    def available(self) -> bool:
        """Camera available if we have a stream URL and error count not exceeded."""
        if not self._attr_stream_url:
            return False
            
        if self._error_count >= self._MAX_ERRORS:
            if time.time() - self._last_error_time < self._RECOVERY_INTERVAL:
                return False
            self._error_count = 0
            
        return True

    async def stream_source(self) -> str | None:
        """Return the source of the stream."""
        return self._attr_stream_url

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
        if self._update_stream_url():
            self.async_write_ha_state()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image response from the camera."""
        if not self._attr_stream_url:
            return None

        current_time = time.time()

        if (self._last_image is not None and
            self._last_image_url == self._attr_stream_url and
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
                    self._attr_stream_url,
                    extra_cmd=extra_cmd,
                    width=width,
                    height=height,
                )
                if snapshot:
                    self._last_image = snapshot
                    self._last_image_time = current_time
                    self._last_image_url = self._attr_stream_url
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