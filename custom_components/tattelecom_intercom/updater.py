"""Tattelecom Intercom updater."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
import logging
from random import randint
from functools import cached_property
from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import event
from homeassistant.util.dt import utcnow
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.httpx_client import create_async_httpx_client
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
import httpx

from .const import (
    ATTR_MUTE,
    ATTR_SIP_LOGIN,
    ATTR_STREAM_URL,
    ATTR_STREAM_URL_MPEG,
    ATTR_SIP_ADDRESS,
    ATTR_SIP_PORT,
    ATTR_SIP_PASSWORD,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DEFAULT_STREAM_TYPES,
    SIP_DEFAULT_RETRY,
    DOMAIN,
    MAINTAINER,
    NAME,
    SIGNAL_NEW_INTERCOM,
    SIGNAL_CALL_STATE,
    UPDATER,
)
from .exceptions import IntercomConnectionError
from .client import IntercomClient
from .voip import IntercomVoip, Call

CALLBACK_TYPE = Callable[[Any], None]

_LOGGER = logging.getLogger(__name__)


# pylint: disable=too-many-branches,too-many-lines,too-many-arguments
class IntercomUpdater(DataUpdateCoordinator[dict[str, Any]]):
    """Tattelecom Intercom data updater."""

    client: IntercomClient

    voip: IntercomVoip | None = None
    last_call: Call | None = None

    code: httpx.codes = httpx.codes.BAD_GATEWAY

    phone: int
    token: str

    new_intercom_callbacks: list[CALLBACK_TYPE] = []

    _scan_interval: int
    _is_first_update: bool

    def __init__(
        self,
        hass: HomeAssistant,
        phone: int,
        token: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
        timeout: int = DEFAULT_TIMEOUT,
        stream_types: list[str] = DEFAULT_STREAM_TYPES,
    ) -> None:
        """Initialize updater."""

        super().__init__(
            hass,
            _LOGGER,
            name=f"{NAME} updater",
            update_interval=timedelta(seconds=scan_interval),
        )

        self.phone = phone
        self.token = token
        self._scan_interval = scan_interval
        self._is_first_update = True

        self.client = IntercomClient(
            create_async_httpx_client(
                hass,
                verify_ssl=True,
                http1=False,
                http2=True
            ),
            phone,
            token,
            timeout,
        )

        self.stream_types = stream_types

        self.voip: IntercomVoip | None = None
        self.last_call: Call | None = None
        self.code = httpx.codes.BAD_GATEWAY
        self.new_intercom_callbacks: list[CALLBACK_TYPE] = []
        self.intercoms: dict[int, IntercomEntityDescription] = {}
        self.code_map: dict[str, int] = {}

    async def get_snapshot(self, intercom_id: int) -> bytes | None:
        """Get snapshot from intercom stream.
        
        :param intercom_id: int: Intercom ID
        :return bytes | None: Snapshot image
        """
        try:
            # Получаем URL потока из данных
            stream_url = self.data.get(f"{intercom_id}_{ATTR_STREAM_URL}")
            if not stream_url:
                _LOGGER.error("No stream URL for intercom %s", intercom_id)
                return None

            _LOGGER.debug("Getting snapshot from %s", stream_url)
            
            # Используем httpx для получения кадра
            async with self.client._client as client:
                response = await client.get(
                    stream_url,
                    timeout=10.0,
                    follow_redirects=True
                )
                
                if response.status_code == 200:
                    _LOGGER.debug("Got snapshot for intercom %s, size: %d bytes", 
                                 intercom_id, len(response.content))
                    return response.content
                else:
                    _LOGGER.error("Failed to get snapshot: %s", response.status_code)
                    return None
                    
        except (httpx.HTTPError, httpx.TimeoutException, OSError) as err:
            _LOGGER.error("Error getting snapshot for intercom %s: %s",
                         intercom_id, err, exc_info=True)
            return None

    async def _async_update_data(self) -> dict[str, Any]:
        """Update data."""
        try:
            data: dict = {}
            _LOGGER.debug("Starting data update for phone %s", self.phone)
            
            await self._async_prepare_intercoms(data)
            
            _LOGGER.debug("Data update completed, got %d items: %s", 
                         len(data), list(data.keys()))
            
            # Проверяем, что данные не пустые
            if not data:
                _LOGGER.warning("No data received from API for phone %s", self.phone)
            else:
                # Логируем первые несколько ключей для отладки
                sample_keys = list(data.keys())[:5]
                _LOGGER.debug("Sample data keys: %s", sample_keys)
                
            return data
            
        except (IntercomConnectionError, asyncio.TimeoutError, httpx.HTTPError) as exc:
            _LOGGER.error("Update failed for phone %s: %s", self.phone, exc, exc_info=True)
            raise UpdateFailed(f"Error communicating with API: {exc}") from exc

    async def async_stop(self) -> None:
        """Stop updater"""

        for _callback in self.new_intercom_callbacks:
            _callback()

        if self.voip:
            await self.voip.stop()

    @cached_property
    def _update_interval(self) -> timedelta:
        """Update interval

        :return timedelta: update_interval
        """

        return timedelta(seconds=self._scan_interval)

    def update_data(self, field: str, value: Any) -> None:
        """Update data

        :param field: str
        :param value: Any
        """

        self.data[field] = value

    @property
    def device_info(self) -> DeviceInfo:
        """Device info.

        :return DeviceInfo: Service DeviceInfo.
        """

        return DeviceInfo(
            identifiers={(DOMAIN, str(self.phone))},
            name=NAME,
            manufacturer=MAINTAINER,
        )

    def schedule_refresh(self, offset: timedelta) -> None:
        """Schedule refresh.

        :param offset: timedelta
        """

        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

        self._unsub_refresh = event.async_track_point_in_utc_time(
            self.hass,
            self._job,
            utcnow().replace(microsecond=0) + offset,
        )

    async def _async_prepare(self, data: dict, retry: int = 1) -> None:
        """Prepare data.

        :param data: dict
        :param retry: int
        """

        _error: IntercomConnectionError | None = None

        try:
            await self._async_prepare_sip_settings(data)
            self._is_first_update = False
        except IntercomConnectionError as _err:
            _error = _err

        await asyncio.sleep(randint(5, 10))

        try:
            await self._async_prepare_intercoms(data)
        except IntercomConnectionError as _err:
            _error = _err

        with contextlib.suppress(IntercomConnectionError):
            await self.client.streams()

        if _error:
            if self._is_first_update and retry <= SIP_DEFAULT_RETRY:
                await asyncio.sleep(retry)

                _LOGGER.debug("Error start. retry (%r): %r", retry, _error)

                return await self._async_prepare(data, retry + 1)

            raise _error

    async def _async_prepare_intercoms(self, data: dict) -> None:
        """Prepare intercoms.

        :param data: dict
        """

        _LOGGER.debug("Fetching intercoms from API for phone %s", self.phone)
        
        try:
            response: dict = await self.client.intercoms()
            _LOGGER.debug("Got response from API: %s", response)
        except (IntercomConnectionError, httpx.HTTPError, asyncio.TimeoutError) as err:
            _LOGGER.error("Error fetching intercoms: %s", err, exc_info=True)
            raise

        if "gates" in response:
            gates = response["gates"]
            _LOGGER.debug("Found %d gates in response", len(gates))
            
            for gate in gates:
                gate_id = gate.get("gate_id")
                _LOGGER.debug("Processing gate %s: %s", gate_id, gate.get("gate_name"))
                
                stream_url = gate.get(ATTR_STREAM_URL)
                stream_url_mpeg = gate.get(ATTR_STREAM_URL_MPEG)

                # Основной URL для потока: MPEG-TS если есть, иначе HLS
                primary_stream_url = stream_url_mpeg if stream_url_mpeg else stream_url

                _LOGGER.debug(
                    "Gate %s: stream_url=%s, stream_url_mpeg=%s, primary=%s",
                    gate_id, stream_url, stream_url_mpeg, primary_stream_url
                )

                data[f"{gate_id}_{ATTR_STREAM_URL}"] = primary_stream_url
                data[f"{gate_id}_{ATTR_STREAM_URL}_hls"] = stream_url
                data[f"{gate_id}_{ATTR_STREAM_URL}_mpeg"] = stream_url_mpeg

                for attr in [ATTR_MUTE, ATTR_SIP_LOGIN]:
                    data[f"{gate_id}_{attr}"] = gate.get(attr)

                if gate_id in self.intercoms:
                    _LOGGER.debug("Gate %s already known", gate_id)
                    continue

                self.code_map[gate["sip_login"]] = gate_id

                gate_name = gate.get("gate_name", f"Intercom {gate_id}").strip()
                self.intercoms[gate_id] = IntercomEntityDescription(
                    id=gate_id,
                    key=gate_id,
                    name=gate_name,
                    device_info=DeviceInfo(
                        identifiers={(DOMAIN, str(gate_id))},
                        name=gate_name,
                        manufacturer=MAINTAINER,
                    ),
                )

                _LOGGER.debug("Added new intercom: %s", gate_id)

                if self.new_intercom_callbacks:
                    async_dispatcher_send(
                        self.hass,
                        SIGNAL_NEW_INTERCOM,
                        self.intercoms[gate_id],
                    )
        else:
            _LOGGER.warning("No 'gates' in response: %s", response)

    async def _async_prepare_sip_settings(self, data: dict) -> None:
        """Prepare sip_settings.

        :param data: dict
        """

        _LOGGER.debug("Fetching SIP settings from API for phone %s", self.phone)
        
        try:
            response: dict = await self.client.sip_settings()
            _LOGGER.debug("Got SIP settings response: %s", response)
        except (IntercomConnectionError, httpx.HTTPError, asyncio.TimeoutError) as err:
            _LOGGER.error("Error fetching SIP settings: %s", err, exc_info=True)
            raise

        init: bool = False
        if "success" in response and response["success"]:
            del response["success"]

            init = (
                len(
                    [
                        code
                        for code, value in response.items()
                        if code not in data or data[code] != value
                    ]
                )
                > 0
            )

            data |= response
            _LOGGER.debug("Updated data with SIP settings, init=%s", init)

        if init:
            _LOGGER.debug("Initializing VoIP with new SIP settings")
            self.voip = IntercomVoip(
                self.hass,
                data[ATTR_SIP_ADDRESS],
                data[ATTR_SIP_PORT],
                data[ATTR_SIP_LOGIN],
                data[ATTR_SIP_PASSWORD],
                self._call_callback,
            )

            self.hass.loop.call_soon(
                lambda: self.hass.async_create_task(
                    self.voip.safe_start(SIP_DEFAULT_RETRY)
                )
            )

    async def _call_callback(self, call: Call) -> None:
        """Call callback

        :param call: Call
        """

        _LOGGER.debug("Call callback received: %s", call)
        self.last_call = call
        async_dispatcher_send(self.hass, SIGNAL_CALL_STATE)


@dataclass
class IntercomEntityDescription:
    """Intercom entity description."""

    id: int
    key: int
    name: str
    device_info: DeviceInfo


@callback
def async_get_updater(hass: HomeAssistant, identifier: str) -> IntercomUpdater:
    """Return IntercomUpdater for username or entry id.

    :param hass: HomeAssistant
    :param identifier: str
    :return IntercomUpdater
    """

    if (
        DOMAIN not in hass.data
        or identifier not in hass.data[DOMAIN]
        or UPDATER not in hass.data[DOMAIN][identifier]
    ):
        raise ValueError(f"Integration with identifier: {identifier} not found.")

    return hass.data[DOMAIN][identifier][UPDATER]