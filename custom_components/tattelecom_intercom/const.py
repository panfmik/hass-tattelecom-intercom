"""Constants for the Tattelecom Intercom integration."""
from __future__ import annotations

from typing import Final

from aiohttp.hdrs import ACCEPT, ACCEPT_CHARSET, ACCEPT_ENCODING, USER_AGENT
from homeassistant.const import Platform

# fmt: off
DOMAIN: Final = "tattelecom_intercom"
"""Integration domain."""

NAME: Final = "Tattelecom Intercom"
"""Integration name."""

MAINTAINER: Final = "Tattelecom"
ATTRIBUTION: Final = "Data provided by Tattelecom Intercom"

PLATFORMS: Final = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.CAMERA,
    Platform.MEDIA_PLAYER,
]

"""Diagnostic const"""
DIAGNOSTIC_DATE_TIME: Final = "date_time"
DIAGNOSTIC_MESSAGE: Final = "message"
DIAGNOSTIC_CONTENT: Final = "content"

"""Helper const"""
UPDATER: Final = "updater"
UPDATE_LISTENER: Final = "update_listener"
OPTION_IS_FROM_FLOW: Final = "is_from_flow"

SIGNAL_NEW_INTERCOM: Final = f"{DOMAIN}-new-intercom"
SIGNAL_SIP_STATE: Final = f"{DOMAIN}-sip-state"
SIGNAL_CALL_STATE: Final = f"{DOMAIN}-call-state"

CONF_PHONE: Final = "phone"
CONF_SMS_CODE: Final = "sms_code"
CONF_LOGIN: Final = "login"

PHONE_MIN: Final = 70000000000
PHONE_MAX: Final = 79999999999
SMS_CODE_LENGTH: Final = 6

"""Default settings"""
DEFAULT_SCAN_INTERVAL: Final = 3600
MIN_SCAN_INTERVAL: Final = 600
DEFAULT_TIMEOUT: Final = 10
DEFAULT_CALL_DELAY: Final = 1
DEFAULT_SLEEP: Final = 3
DEFAULT_RETRY: Final = 10

"""Retry settings"""
MAX_RETRIES: Final = 3
RETRY_DELAY: Final = 1
RETRY_BACKOFF_MULTIPLIER: Final = 2
RETRY_STATUS_CODES: Final = (500, 502, 503, 504)

"""Tattelecom intercom API client const"""
CLIENT_URL: Final = "https://domofon.tattelecom.ru/{api_version}/{path}"
HEADERS: Final = {
    ACCEPT: "application/json",
    ACCEPT_CHARSET: "UTF-8",
    USER_AGENT: "ktor-client",
    ACCEPT_ENCODING: "gzip",
}
DEVICE_CODE: Final = "Android_empty_push_token"
DEVICE_OS: Final = 1

"""Attributes"""
ATTR_UPDATE_STATE: Final = "update_state"
ATTR_UPDATE_STATE_NAME: Final = "Состояние обновления"

ATTR_SIP_ADDRESS: Final = "sip_address"
ATTR_SIP_LOGIN: Final = "sip_login"
ATTR_SIP_PASSWORD: Final = "sip_password"
ATTR_SIP_PORT: Final = "sip_port"

ATTR_STREAM_URL: Final = "stream_url"
ATTR_STREAM_URL_MPEG: Final = "stream_url_mpeg"
ATTR_MUTE: Final = "mute"
ATTR_STREAM_TYPE: Final = "stream_type"

"""Attributes sensor"""
SENSOR_SIP_STATE: Final = "sip_state"
SENSOR_SIP_STATE_NAME: Final = "Состояние SIP"

SENSOR_CALL_STATE: Final = "call_state"
SENSOR_CALL_STATE_NAME: Final = "Состояние вызова"

"""Attributes camera"""
CAMERA_NAME: Final = "Камера"

CAMERA_INCOMING: Final = "incoming"
CAMERA_INCOMING_NAME: Final = "Входящий вызов"

"""Attributes media player"""
MEDIA_PLAYER_OUTGOING: Final = "outgoing"
MEDIA_PLAYER_OUTGOING_NAME: Final = "Исходящий вызов"

"""Attributes button"""
BUTTON_OPEN: Final = "open_door"
BUTTON_OPEN_NAME: Final = "Открыть дверь"

BUTTON_ANSWER: Final = "answer"
BUTTON_ANSWER_NAME: Final = "Ответить"

BUTTON_DECLINE: Final = "decline"
BUTTON_DECLINE_NAME: Final = "Отклонить"

BUTTON_HANGUP: Final = "hangup"
BUTTON_HANGUP_NAME: Final = "Положить трубку"

"""Attributes switch"""
SWITCH_MUTE_NAME: Final = "Без звука"

"""VoIP"""
SIP_PORT: Final = 60266

TAG_REGISTER: Final = "register"
TAG_DEREGISTER: Final = "deregister"
SIP_EXPIRES: Final = 3600
SIP_TIMEOUT: Final = 10
SIP_PING_TIMEOUT: Final = 10
SIP_RETRY_SLEEP: Final = 5
SIP_DEFAULT_RETRY: Final = 10
SIP_USER_AGENT: Final = "Unknown (belle-sip/4.4.0)"
VOIP_CLEAN_DELAY: Final = 1800

PHONE_EVENT_KEYS: Final = (
    "0",
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "*",
    "#",
    "A",
    "B",
    "C",
    "D",
)
CONF_API_VERSION: Final = "api_version"
DEFAULT_API_VERSION: Final = "v1"
API_VERSIONS: Final = ["v1", "v2"]

"""Stream types"""
CONF_STREAM_TYPES: Final = "stream_types"
DEFAULT_STREAM_TYPES: Final = ["mpeg"]
STREAM_TYPE_MPEG: Final = "mpeg"
STREAM_TYPE_HLS: Final = "hls"
STREAM_TYPE_WEBRTC: Final = "webrtc"
STREAM_TYPE_OPTIONS: Final = {
    STREAM_TYPE_MPEG: "MPEG-TS",
    STREAM_TYPE_HLS: "HLS",
    STREAM_TYPE_WEBRTC: "WebRTC",
}