"""Camera component."""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
    STREAM_TYPE_WEBRTC,
)
from .entity import IntercomEntity
from .intercom_enum import CallState
from .updater import IntercomEntityDescription, IntercomUpdater, async_get_updater
from . import webrtc_utils

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
        """Add camera (legacy, without stream type)."""
        # Создаём камеры для каждого выбранного типа потока
        for stream_type in updater.stream_types:
            async_add_entities(
                [
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
                ]
            )

    # Добавляем основную камеру для входящих вызовов
    entities = [
        IntercomCamera(
            f"{config_entry.entry_id}-{description.key}",
            description,
            updater,
        )
        for description in CAMERAS
    ]
    async_add_entities(entities)
    _LOGGER.info("Added %d incoming cameras for entry %s", len(entities), config_entry.entry_id)

    # Добавляем камеры для каждого домофона и каждого типа потока
    for intercom in updater.intercoms.values():
        add_camera(intercom)

    updater.new_intercom_callbacks.append(
        async_dispatcher_connect(hass, SIGNAL_NEW_INTERCOM, add_camera)
    )
    _LOGGER.info(
        "Camera setup completed for entry %s: %d intercoms, stream_types: %s",
        config_entry.entry_id, len(updater.intercoms), updater.stream_types
    )


class IntercomCamera(IntercomEntity, Camera):
    """Intercom camera entry."""

    _CACHE_TIMEOUT: float = 5.0  # seconds
    _MAX_ERRORS: int = 3
    _RECOVERY_INTERVAL: float = 30.0  # seconds
    _RETRY_ATTEMPTS: int = 2
    _RETRY_DELAY: float = 1.0  # seconds

    _attr_stream_url: str
    _attr_stream_type: str
    _unsub_update: CALLBACK_TYPE | None = None
    _intercom_id: int | None = None
    _last_image: bytes | None = None
    _last_image_time: float = 0.0
    _last_image_url: str = ""
    _error_count: int = 0
    _last_error_time: float = 0.0
    _webrtc_server: str | None = None

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
        self._attr_should_poll = False
        self._attr_is_streaming = True  # IP камера всегда стримит
        # Поддержка дополнительных действий, если доступно
        extra_features = CameraEntityFeature.STREAM
        if hasattr(CameraEntityFeature, "EXTRA_ACTIONS"):
            extra_features |= CameraEntityFeature.EXTRA_ACTIONS
        self._attr_supported_features = extra_features
        self._attr_extra_state_attributes = {}
        
        # Сохраняем ID домофона для камер конкретных домофонов
        if description.key not in [CAMERA_INCOMING]:
            try:
                self._intercom_id = int(description.key.split('_')[0])  # Извлекаем ID из ключа (может быть "id_type")
            except ValueError:
                self._intercom_id = None
                _LOGGER.error("Invalid intercom ID: %s", description.key)
        
        self._stream_type = stream_type
        
        if device_info:
            self._attr_device_info = device_info

        # Инициализируем URL из данных
        self._update_stream_url()
        _LOGGER.debug("Camera %s initialized with ID %s, stream_type %s, URL: %s, attributes: %s",
                     self.entity_id, self._intercom_id, self._stream_type, self._attr_stream_url,
                     self._attr_extra_state_attributes)
        # Отладочная проверка stream_source
        _LOGGER.debug("Camera %s stream_source attribute type: %s, value: %s",
                     self.entity_id, type(getattr(self, 'stream_source', None)),
                     getattr(self, 'stream_source', None))
        # Если stream_source является строкой (не методом), удаляем его, чтобы использовался метод класса
        if isinstance(getattr(self, 'stream_source', None), str):
            _LOGGER.warning(
                "Camera %s stream_source incorrectly set to string, removing. Value: %s",
                self.entity_id, getattr(self, 'stream_source', None)
            )
            del self.stream_source

    def _update_stream_url(self) -> bool:
        """Update stream URL from updater data based on stream type.
        
        Returns True if attributes changed, False otherwise.
        """
        if not self._intercom_id:
            return False
        
        # Если данные ещё не загружены, пропускаем обновление
        if self._updater.data is None:
            _LOGGER.debug("Camera %s: updater data is None, skipping update", self.entity_id)
            return False
        
        skip_standard_fetch = False
        new_stream_url = ""
        url_key = ""
        
        # Определяем ключ данных в зависимости от типа потока
        if self._stream_type == STREAM_TYPE_HLS:
            url_key = f"{self._intercom_id}_{ATTR_STREAM_URL}_hls"
        elif self._stream_type == STREAM_TYPE_MPEG:
            url_key = f"{self._intercom_id}_{ATTR_STREAM_URL}_mpeg"
        elif self._stream_type == STREAM_TYPE_WEBRTC:
            # Для WebRTC используем HLS, если доступен, иначе MPEG, иначе primary
            hls_key = f"{self._intercom_id}_{ATTR_STREAM_URL}_hls"
            mpeg_key = f"{self._intercom_id}_{ATTR_STREAM_URL}_mpeg"
            primary_key = f"{self._intercom_id}_{ATTR_STREAM_URL}"
            # Проверяем наличие URL в данных
            hls_url = self._updater.data.get(hls_key, "")
            mpeg_url = self._updater.data.get(mpeg_key, "")
            primary_url = self._updater.data.get(primary_key, "")
            if hls_url:
                url_key = hls_key
                new_stream_url = hls_url
            elif mpeg_url:
                url_key = mpeg_key
                new_stream_url = mpeg_url
            else:
                url_key = primary_key
                new_stream_url = primary_url
            # Пропускаем стандартное получение new_stream_url ниже, так как уже получили
            skip_standard_fetch = True
        else:
            # fallback на primary stream URL
            url_key = f"{self._intercom_id}_{ATTR_STREAM_URL}"
        
        sip_key = f"{self._intercom_id}_{ATTR_SIP_LOGIN}"
        
        if not skip_standard_fetch:
            new_stream_url = self._updater.data.get(url_key, "")
        sip_login = self._updater.data.get(sip_key)
        
        # Логируем информацию о потоке
        if self._stream_type == STREAM_TYPE_MPEG:
            _LOGGER.info(
                "Camera %s MPEG stream data: url_key=%s, stream_url=%s",
                self.entity_id, url_key, new_stream_url
            )
        else:
            _LOGGER.debug(
                "Camera %s (type %s) data: url_key=%s, stream_url=%s",
                self.entity_id, self._stream_type, url_key, new_stream_url
            )
        
        # Создаем новые атрибуты
        new_attributes = {}
        if new_stream_url:
            new_attributes = {
                ATTR_STREAM_URL: new_stream_url,
                ATTR_STREAM_TYPE: self._stream_type,
                ATTR_SIP_LOGIN: sip_login,
            }
            if self._stream_type == STREAM_TYPE_MPEG:
                _LOGGER.info(
                    "Camera %s MPEG stream URL updated: %s",
                    self.entity_id, new_stream_url
                )
            else:
                _LOGGER.debug(
                    "Camera %s stream URL updated: %s (type: %s)",
                    self.entity_id, new_stream_url, self._stream_type
                )
        else:
            _LOGGER.warning(
                "Camera %s has no stream URL for type %s",
                self.entity_id, self._stream_type
            )
        
        # Проверяем изменения
        changed = (
            new_stream_url != self._attr_stream_url
            or new_attributes != self._attr_extra_state_attributes
            or self._stream_type != self._attr_stream_type
        )
        
        # Если URL изменился, сбрасываем кэш изображения и счетчик ошибок
        if new_stream_url != self._attr_stream_url:
            self._last_image = None
            self._last_image_time = 0.0
            self._last_image_url = ""
            self._error_count = 0
            self._last_error_time = 0.0
        
        self._attr_stream_url = new_stream_url
        self._attr_stream_type = self._stream_type
        self._attr_extra_state_attributes = new_attributes
        
        return changed

    async def _get_go2rtc_url(self) -> str | None:
        """Get go2rtc base URL, start if necessary."""
        if self._webrtc_server is not None:
            return self._webrtc_server
        try:
            go_url = await webrtc_utils.ensure_go2rtc(self.hass)
            self._webrtc_server = go_url
            _LOGGER.info("go2rtc URL obtained: %s", go_url)
            return go_url
        except Exception as e:
            _LOGGER.error("Failed to start go2rtc for WebRTC stream: %s", e)
            self._webrtc_server = None
            return None

    async def _async_init_webrtc(self) -> None:
        """Initialize WebRTC (start go2rtc if needed) and update attributes."""
        if self._stream_type != STREAM_TYPE_WEBRTC:
            return
        go_url = await self._get_go2rtc_url()
        if go_url and self._attr_stream_url:
            # Вычисляем WebSocket URL для потока
            webrtc_url = webrtc_utils.ws_url(go_url, self._attr_stream_url, self.entity_id)
            # Добавляем в атрибуты
            self._attr_extra_state_attributes["webrtc_url"] = webrtc_url
            _LOGGER.debug("Camera %s webrtc_url updated: %s", self.entity_id, webrtc_url)
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Camera available if we have a stream URL and error count not exceeded."""
        import time
        # Если нет URL, недоступна
        if not self._attr_stream_url:
            return False
        # Если ошибок слишком много, проверяем время восстановления
        if self._error_count >= self._MAX_ERRORS:
            current_time = time.time()
            if current_time - self._last_error_time < self._RECOVERY_INTERVAL:
                _LOGGER.debug(
                    "Camera %s temporarily unavailable due to %d consecutive errors",
                    self.entity_id, self._error_count
                )
                return False
            else:
                # Интервал восстановления прошел, сбрасываем счетчик
                _LOGGER.info(
                    "Camera %s recovery interval passed, resetting error count",
                    self.entity_id
                )
                self._error_count = 0
        return True

    async def stream_source(self) -> str | None:
        """Return the source of the stream."""
        _LOGGER.debug(
            "Camera %s stream_source called, type of self.stream_source: %s, _attr_stream_url: %s",
            self.entity_id, type(getattr(self, 'stream_source', None)), self._attr_stream_url
        )
        # Проверяем, не был ли stream_source перезаписан строкой
        if isinstance(getattr(self, 'stream_source', None), str):
            _LOGGER.error(
                "Camera %s stream_source is a string! Value: %s",
                self.entity_id, getattr(self, 'stream_source', None)
            )
            # Возвращаем строку из атрибута
            return self._attr_stream_url

        # Для всех типов потоков возвращаем исходный URL (для WebRTC это HLS/MPEG URL)
        return self._attr_stream_url

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = super().extra_state_attributes
        if not isinstance(attrs, dict):
            attrs = {}
        attrs.update(self._attr_extra_state_attributes)
        _LOGGER.debug("Camera %s extra_state_attributes: %s", self.entity_id, attrs)
        return attrs

    @property
    def extra_actions(self) -> list[dict[str, Any]]:
        """Return list of extra actions for the camera."""
        if not self._intercom_id:
            return []
        actions = []
        # Кнопка открытия двери
        actions.append({
            "action": "open_door",
            "title": "Открыть дверь",
            "icon": "mdi:door-open",
            "entity_id": self.entity_id,
        })
        # Кнопка переключения без звука
        actions.append({
            "action": "toggle_mute",
            "title": "Без звука",
            "icon": "mdi:volume-mute",
            "entity_id": self.entity_id,
        })
        return actions

    async def async_handle_extra_action(self, action: str) -> None:
        """Handle extra action."""
        if not self._intercom_id:
            _LOGGER.warning("Camera %s has no intercom ID, cannot handle action %s", self.entity_id, action)
            return
        if action == "open_door":
            _LOGGER.info("Opening door for intercom %s", self._intercom_id)
            await self._updater.client.open(self._intercom_id)
        elif action == "toggle_mute":
            # Определяем текущее состояние mute из данных
            mute_key = f"{self._intercom_id}_mute"
            if self._updater.data is None:
                _LOGGER.warning("Camera %s: updater data is None, assuming not muted", self.entity_id)
                is_muted = False
            else:
                is_muted = self._updater.data.get(mute_key, False)
            if is_muted:
                _LOGGER.info("Unmuting intercom %s", self._intercom_id)
                await self._updater.client.unmute(self._intercom_id)
            else:
                _LOGGER.info("Muting intercom %s", self._intercom_id)
                await self._updater.client.mute(self._intercom_id)
        else:
            _LOGGER.warning("Unknown action %s for camera %s", action, self.entity_id)

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()

        # Подписываемся на обновления через updater
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
        
        # Обновляем URL при добавлении
        self._update_stream_url()
        
        # Если тип потока WebRTC, инициализируем go2rtc (лениво)
        if self._stream_type == STREAM_TYPE_WEBRTC:
            # Запускаем go2rtc в фоне, но не блокируем добавление
            self.hass.async_create_task(self._async_init_webrtc())

    async def will_remove_from_hass(self) -> None:
        """Remove event"""
        _LOGGER.info("Camera %s is being removed from hass (stream_type=%s, intercom_id=%s)",
                     self.entity_id, self._stream_type, self._intercom_id)
        if self._unsub_update is not None:
            self._unsub_update()
            self._unsub_update = None
        await super().will_remove_from_hass()

    @callback
    def _handle_event_update(self) -> None:
        """Update state from event."""
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update state from coordinator."""
        changed = self._update_stream_url()
        
        if changed:
            _LOGGER.debug("Camera %s attributes changed, updating state", self.entity_id)
            self.async_write_ha_state()
        super()._handle_coordinator_update()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image response from the camera."""
        import time
        
        if not self._attr_stream_url:
            _LOGGER.debug("Camera %s: no stream URL", self.entity_id)
            return None

        # Проверяем кэш
        current_time = time.time()
        if (self._last_image is not None and
            self._last_image_url == self._attr_stream_url and
            current_time - self._last_image_time < self._CACHE_TIMEOUT):
            _LOGGER.debug("Camera %s: using cached image", self.entity_id)
            return self._last_image

        # Определяем тип потока для выбора параметров ffmpeg
        extra_cmd = ""
        if self._attr_stream_url.startswith("rtsp://"):
            extra_cmd = "-prefix_rtsp_flags prefer_tcp"
            _LOGGER.debug("Camera %s: using RTSP-specific ffmpeg parameters", self.entity_id)
        elif self._stream_type == STREAM_TYPE_MPEG:
            # Для MPEG-TS потока используем параметры для быстрого захвата кадра
            extra_cmd = "-frames:v 1 -skip_frame nokey"
            _LOGGER.info("Camera %s: using MPEG-specific ffmpeg parameters", self.entity_id)
        elif self._stream_type == STREAM_TYPE_HLS:
            # Для HLS можно добавить параметры для ускорения
            extra_cmd = "-flags low_delay -fflags +discardcorrupt"
            _LOGGER.debug("Camera %s: using HLS-specific ffmpeg parameters", self.entity_id)
        
        # Пытаемся получить снимок через ffmpeg с повторными попытками
        snapshot = None
        ffmpeg_error = None
        for attempt in range(self._RETRY_ATTEMPTS):
            try:
                _LOGGER.info("Trying to get snapshot via ffmpeg from %s (type: %s) attempt %d/%d",
                             self._attr_stream_url, self._stream_type, attempt + 1, self._RETRY_ATTEMPTS)
                snapshot = await ffmpeg.async_get_image(
                    self.hass,
                    self._attr_stream_url,
                    extra_cmd=extra_cmd,
                    width=width,
                    height=height,
                )
                if snapshot:
                    break  # Успех, выходим из цикла
            except Exception as err:
                ffmpeg_error = err
                _LOGGER.warning(
                    "Camera %s ffmpeg snapshot failed (attempt %d/%d): %s",
                    self.entity_id, attempt + 1, self._RETRY_ATTEMPTS, err
                )
                if attempt < self._RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(self._RETRY_DELAY)
                else:
                    # Последняя попытка, увеличиваем счетчик ошибок
                    self._error_count += 1
                    self._last_error_time = current_time
                    _LOGGER.debug(
                        "Camera %s error count increased to %d after all ffmpeg attempts",
                        self.entity_id, self._error_count
                    )

        if snapshot:
            _LOGGER.info("Got snapshot via ffmpeg for %s, size: %d bytes",
                         self.entity_id, len(snapshot))
            self._last_image = snapshot
            self._last_image_time = current_time
            self._last_image_url = self._attr_stream_url
            # Успех, сбрасываем счетчик ошибок
            self._error_count = 0
            return snapshot

        # Fallback: используем httpx для получения кадра с повторными попытками
        for attempt in range(self._RETRY_ATTEMPTS):
            try:
                _LOGGER.info("Getting snapshot via HTTP from %s attempt %d/%d",
                             self._attr_stream_url, attempt + 1, self._RETRY_ATTEMPTS)
                
                async with self._updater.client._client as client:
                    response = await client.get(
                        self._attr_stream_url,
                        timeout=10.0,
                        follow_redirects=True,
                        headers={
                            "User-Agent": "HomeAssistant",
                            "Accept": "image/jpeg,image/png,image/*"
                        }
                    )
                    
                    if response.status_code == 200:
                        content = response.content
                        _LOGGER.info("Got snapshot via HTTP for %s, size: %d bytes",
                                     self.entity_id, len(content))
                        # Сохраняем в кэш
                        self._last_image = content
                        self._last_image_time = current_time
                        self._last_image_url = self._attr_stream_url
                        # Успех, сбрасываем счетчик ошибок
                        self._error_count = 0
                        return content
                    else:
                        _LOGGER.error("Failed to get snapshot: HTTP %s (attempt %d/%d)",
                                     response.status_code, attempt + 1, self._RETRY_ATTEMPTS)
                        if attempt < self._RETRY_ATTEMPTS - 1:
                            await asyncio.sleep(self._RETRY_DELAY)
                            continue
                        else:
                            return None
                        
            except asyncio.TimeoutError:
                _LOGGER.error("Timeout getting snapshot for %s (attempt %d/%d)",
                             self.entity_id, attempt + 1, self._RETRY_ATTEMPTS)
                if attempt < self._RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(self._RETRY_DELAY)
                    continue
                else:
                    self._error_count += 1
                    self._last_error_time = current_time
                    _LOGGER.debug(
                        "Camera %s error count increased to %d after all HTTP attempts (timeout)",
                        self.entity_id, self._error_count
                    )
                    return None
            except (httpx.HTTPError, OSError) as err:
                _LOGGER.error("Error getting snapshot for %s (attempt %d/%d): %s",
                             self.entity_id, attempt + 1, self._RETRY_ATTEMPTS, err, exc_info=True)
                if attempt < self._RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(self._RETRY_DELAY)
                    continue
                else:
                    self._error_count += 1
                    self._last_error_time = current_time
                    _LOGGER.debug(
                        "Camera %s error count increased to %d after all HTTP attempts (HTTP/OS error)",
                        self.entity_id, self._error_count
                    )
                    return None