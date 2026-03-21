"""
WebRTC utilities for Tattelecom Intercom integration.
Based on code from AlexxIT/WebRTC integration.
"""
import asyncio
import io
import logging
import os
import platform
import re
import stat
import subprocess
import zipfile
from threading import Thread
from typing import Optional
from urllib.parse import urljoin, urlencode

import aiohttp
import requests
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

DOMAIN = "tattelecom_intercom"

BINARY_VERSION = "1.9.12"

SYSTEM = {
    "Windows": {"AMD64": "go2rtc_win64.zip", "ARM64": "go2rtc_win_arm64.zip"},
    "Darwin": {"x86_64": "go2rtc_mac_amd64.zip", "arm64": "go2rtc_mac_arm64.zip"},
    "Linux": {
        "armv7l": "go2rtc_linux_arm",
        "armv8l": "go2rtc_linux_arm",  # https://github.com/AlexxIT/WebRTC/issues/18
        "aarch64": "go2rtc_linux_arm64",
        "x86_64": "go2rtc_linux_amd64",
        "i386": "go2rtc_linux_386",
        "i486": "go2rtc_linux_386",
        "i586": "go2rtc_linux_386",
        "i686": "go2rtc_linux_386",
    },
}

DEFAULT_URL = "http://localhost:1984/"

BINARY_NAME = re.compile(
    r"^(go2rtc-\d\.\d\.\d+|go2rtc_v0\.1-rc\.[5-9]|rtsp2webrtc_v[1-5])(\.exe)?$"
)


def get_arch() -> Optional[str]:
    system = SYSTEM.get(platform.system())
    if not system:
        return None
    return system.get(platform.machine())


def unzip(content: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for filename in zf.namelist():
            with zf.open(filename) as f:
                return f.read()


def validate_binary(hass: HomeAssistant) -> Optional[str]:
    """Check if go2rtc binary exists and is correct version, otherwise download it."""
    filename = f"go2rtc-{BINARY_VERSION}"
    if platform.system() == "Windows":
        filename += ".exe"

    filename = hass.config.path(filename)
    try:
        if os.path.isfile(filename) and subprocess.check_output(
            [filename, "-v"]
        ).startswith(b"go2rtc"):
            return filename
    except:
        pass

    # remove all old binaries
    for file in os.listdir(hass.config.config_dir):
        if BINARY_NAME.match(file):
            _LOGGER.debug(f"Remove old binary: {file}")
            os.remove(hass.config.path(file))

    # download new binary
    arch = get_arch()
    if not arch:
        _LOGGER.error("Unsupported platform")
        return None
    url = (
        f"https://github.com/AlexxIT/go2rtc/releases/download/"
        f"v{BINARY_VERSION}/{arch}"
    )
    _LOGGER.debug(f"Download new binary: {url}")
    r = requests.get(url)
    if not r.ok:
        return None

    raw = r.content

    # unzip binary for windows
    if url.endswith(".zip"):
        raw = unzip(raw)

    # save binary to config folder
    with open(filename, "wb") as f:
        f.write(raw)

    # change binary access rights
    os.chmod(filename, os.stat(filename).st_mode | stat.S_IEXEC)

    return filename


async def check_go2rtc(hass: HomeAssistant, url: str = DEFAULT_URL) -> Optional[str]:
    """Check if go2rtc is already running."""
    session = async_get_clientsession(hass)
    try:
        r = await session.head(url, timeout=2, allow_redirects=False)
        return url if r.status < 300 else None
    except Exception:
        return None


def api_streams(hass: HomeAssistant, go_url: str) -> str:
    """Return go2rtc streams API URL."""
    return urljoin(go_url, "api/streams")


def ws_url(go_url: str, src: str, name: str = "") -> str:
    """Return WebSocket URL for go2rtc."""
    query = {"src": src}
    if name:
        query["name"] = name
    return urljoin("ws" + go_url[4:], "api/ws") + "?" + urlencode(query)


class Server(Thread):
    """Thread that runs go2rtc binary."""

    def __init__(self, binary: str):
        super().__init__(name="go2rtc", daemon=True)
        self.binary = binary
        self.process = None

    @property
    def available(self):
        return self.process.poll() is None if self.process else False

    def run(self):
        while self.binary:
            self.process = subprocess.Popen(
                [self.binary], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )

            # check alive
            while self.process.poll() is None:
                line = self.process.stdout.readline()
                if line == b"":
                    break
                _LOGGER.debug(line[:-1].decode())

    def stop(self, *args):
        self.binary = None
        if self.process:
            self.process.terminate()


async def ensure_go2rtc(hass: HomeAssistant) -> str:
    """Ensure go2rtc is running, return its base URL."""
    # First, check if go2rtc is already running
    go_url = await check_go2rtc(hass)
    if go_url:
        _LOGGER.debug("go2rtc is already running at %s", go_url)
        return go_url

    # If not, try to start embedded binary
    binary = await hass.async_add_executor_job(validate_binary, hass)
    if not binary:
        raise RuntimeError("Cannot download or validate go2rtc binary")

    server = Server(binary)
    server.start()
    # Store server instance in hass data to stop later
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    hass.data[DOMAIN]["webrtc_server"] = server

    # Wait a bit for server to start
    await asyncio.sleep(2)
    go_url = await check_go2rtc(hass)
    if go_url:
        _LOGGER.info("Started go2rtc at %s", go_url)
        return go_url
    else:
        raise RuntimeError("Failed to start go2rtc")