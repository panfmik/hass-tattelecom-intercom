"""Tattelecom intercom API client."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from httpx import AsyncClient, ConnectError, HTTPError, Response, TransportError

from .const import (
    CLIENT_URL,
    DEFAULT_TIMEOUT,
    DEVICE_CODE,
    DEVICE_OS,
    DIAGNOSTIC_CONTENT,
    DIAGNOSTIC_DATE_TIME,
    DIAGNOSTIC_MESSAGE,
    HEADERS,
    MAX_RETRIES,
    RETRY_DELAY,
    RETRY_BACKOFF_MULTIPLIER,
    RETRY_STATUS_CODES,
)
from .intercom_enum import ApiVersion, Method
from .exceptions import (
    IntercomConnectionError,
    IntercomNotFoundError,
    IntercomRequestError,
    IntercomUnauthorizedError,
)

_LOGGER = logging.getLogger(__name__)


class IntercomClient:
    """Tattelecom intercom API Client."""

    _client: AsyncClient
    _timeout: int

    _token: str | None = None
    _phone: int

    def __init__(
        self,
        client: AsyncClient,
        phone: int,
        token: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        """Initialize API client."""

        self._client = client
        self._timeout = timeout

        self._token = token
        self._phone = phone

        self.diagnostics: dict[str, Any] = {}

    async def request(
        self,
        path: str,
        method: Method = Method.GET,
        body: dict | None = None,
        params: dict | None = None,
        api_version: ApiVersion = ApiVersion.V1,
    ) -> dict:
        """Request method with retries."""
        _url: str = CLIENT_URL.format(api_version=api_version, path=path)
        _headers: dict = HEADERS.copy()
        if self._token:
            _headers["access-token"] = self._token

        last_exception: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response: Response = await self._client.request(
                    method,
                    _url,
                    json=body,
                    params=params,
                    headers=_headers,
                    timeout=self._timeout,
                )

                self._debug("Successful request", _url, response.content, path)

                _data: dict = json.loads(response.content)

                if response.status_code in RETRY_STATUS_CODES:
                    raise IntercomConnectionError(
                        f"Server error {response.status_code}"
                    )

                if response.status_code == 404:
                    raise IntercomNotFoundError("Not found")

                if response.status_code == 401:
                    raise IntercomUnauthorizedError("Unauthorized")

                if response.status_code > 400 or (
                    "status" in _data and int(_data["status"]) > 400
                ):
                    raise IntercomRequestError(
                        _data.get("error_text", _data.get("message", "Request error"))
                    )

                return _data

            except (IntercomNotFoundError, IntercomUnauthorizedError, IntercomRequestError):
                raise
            except (HTTPError, ConnectError, TransportError, ValueError, TypeError, json.JSONDecodeError) as e:
                last_exception = e
                self._debug("Connection error", _url, e, path)
                if attempt == MAX_RETRIES:
                    break
                delay = RETRY_DELAY * (RETRY_BACKOFF_MULTIPLIER ** attempt)
                _LOGGER.debug(
                    "Request failed (attempt %d/%d), retrying in %.1f seconds: %s",
                    attempt + 1,
                    MAX_RETRIES + 1,
                    delay,
                    str(e),
                )
                await asyncio.sleep(delay)
            except IntercomConnectionError as e:
                last_exception = e
                self._debug("Connection error", _url, e, path)
                if attempt == MAX_RETRIES:
                    break
                delay = RETRY_DELAY * (RETRY_BACKOFF_MULTIPLIER ** attempt)
                _LOGGER.debug(
                    "Request failed (attempt %d/%d), retrying in %.1f seconds: %s",
                    attempt + 1,
                    MAX_RETRIES + 1,
                    delay,
                    str(e),
                )
                await asyncio.sleep(delay)

        raise IntercomConnectionError("Connection error after retries") from last_exception

    async def signin(self) -> dict:
        """Signin"""

        return await self.request(
            "auth",
            Method.POST,
            {
                "device_code": DEVICE_CODE,
                "phone": str(self._phone),
            },
            api_version=ApiVersion.V2,
        )

    async def sms_confirm(self, code: str) -> dict:
        """Sms confirm"""

        return await self.request(
            "auth/confirm-sms",
            Method.POST,
            {
                "device_code": DEVICE_CODE,
                "phone": str(self._phone),
                "sms_code": code,
                "device_os_id": DEVICE_OS,
            },
            api_version=ApiVersion.V2,
        )

    async def update_push_token(self, token: str) -> dict:
        """Update push token"""

        self._token = token

        return await self.request(
            "subscriber/update-push-token",
            Method.POST,
            {
                "device_code": DEVICE_CODE,
                "phone": str(self._phone),
                "push_token": DEVICE_CODE,
            },
        )

    async def sip_settings(self) -> dict:
        """Get sip settings"""

        return await self.request(
            "subscriber/sipsettings",
            params={
                "device_code": DEVICE_CODE,
                "phone": str(self._phone),
            },
        )

    async def intercoms(self) -> dict:
        """Get available intercoms"""

        return await self.request(
            "subscriber/gates",
            api_version=ApiVersion.V2,
        )

    async def streams(self) -> dict:
        """Get available streams"""

        return await self.request(
            "subscriber/available-streams",
            params={
                "device_code": DEVICE_CODE,
                "phone": str(self._phone),
            },
            api_version=ApiVersion.V2,
        )

    async def open(self, intercom_id: int) -> dict:
        """Open intercom"""

        return await self.request(
            "gate/open-door",
            Method.POST,
            {"gate_id": intercom_id, "data": {"screen_id": 1}},
            api_version=ApiVersion.V2,
        )

    async def mute(self, intercom_id: int) -> dict:
        """Disable calls"""

        return await self.request(
            "subscriber/disable-intercom-calls",
            Method.POST,
            {
                "device_code": DEVICE_CODE,
                "phone": str(self._phone),
                "intercom_id": intercom_id,
            },
        )

    async def unmute(self, intercom_id: int) -> dict:
        """Enable calls"""

        return await self.request(
            "subscriber/enable-intercom-calls",
            Method.POST,
            {
                "device_code": DEVICE_CODE,
                "phone": str(self._phone),
                "intercom_id": intercom_id,
            },
        )

    async def schedule(
        self,
        intercom_id: int,
        start_h: int = 0,
        start_m: int = 0,
        finish_h: int = 0,
        finish_m: int = 0,
        monday: bool = True,
        tuesday: bool = True,
        wednesday: bool = True,
        thursday: bool = True,
        friday: bool = True,
        saturday: bool = True,
        sunday: bool = True,
    ) -> dict:
        """Set schedule"""

        return await self.request(
            "subscriber/set-schedule",
            Method.POST,
            {
                "device_code": DEVICE_CODE,
                "phone": str(self._phone),
                "intercom_id": intercom_id,
                "start_h": start_h,
                "start_m": start_m,
                "finish_h": finish_h,
                "finish_m": finish_m,
                "monday": monday,
                "tuesday": tuesday,
                "wednesday": wednesday,
                "thursday": thursday,
                "friday": friday,
                "saturday": saturday,
                "sunday": sunday,
            },
        )

    def _debug(self, message: str, url: str, content: Any, path: str) -> None:
        """Debug log"""

        _LOGGER.debug("%s (%s): %s", message, url, str(content))

        _content: dict | str = {}

        try:
            _content = json.loads(content)
        except (ValueError, TypeError):
            _content = str(content)

        self.diagnostics[path] = {
            DIAGNOSTIC_DATE_TIME: datetime.now().replace(microsecond=0).isoformat(),
            DIAGNOSTIC_MESSAGE: message,
            DIAGNOSTIC_CONTENT: _content,
        }