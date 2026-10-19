"""Simulated camera for development and demos (`gopro-sync serve --demo`)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from gopro_sync.cameras.base import (
    CameraDriver, CameraIdentity, CameraSession, MediaStream, RemoteMedia, Report, no_report,
)

IDENTITY = CameraIdentity(
    serial="C3461324701234", identifier="1234", model_name="HERO11 Black (simulated)",
    model_number=58, firmware="H22.01.02.32.00", ap_ssid="GP24701234",
)


def fake_content(offset: int, length: int) -> bytes:
    """Deterministic file content, so resumed downloads can be checked byte for byte."""
    start = offset % 251
    return (_PATTERN * (length // 251 + 2))[start : start + length]


_PATTERN = bytes(range(251))


class FakeSession(CameraSession):
    def __init__(self, driver: FakeDriver) -> None:
        self.identity = IDENTITY
        self._driver = driver

    async def list_media(self) -> list[RemoteMedia]:
        return list(self._driver.media)

    @asynccontextmanager
    async def stream(self, media: RemoteMedia, offset: int = 0) -> AsyncIterator[MediaStream]:
        driver = self._driver
        size = driver.files[media.path]
        start = offset if driver.supports_range else 0

        async def chunks() -> AsyncIterator[bytes]:
            sent = start
            while sent < size:
                if driver.failures and driver.fail_path == media.path and sent >= driver.fail_offset:
                    driver.failures -= 1
                    raise httpx.ReadTimeout("simulated Wi-Fi stall")
                n = min(driver.chunk, size - sent)
                await asyncio.sleep(driver.delay)
                yield fake_content(sent, n)
                sent += n

        yield MediaStream(total=size, start=start, chunks=chunks())

    async def sleep(self) -> None:
        self._driver.sleep_calls += 1


class FakeDriver(CameraDriver):
    def __init__(self, files: dict[str, int] | None = None, chunk: int = 256 * 1024, delay: float = 0.05) -> None:
        self.files = files or {
            "100GOPRO/GX010001.MP4": 24 * 1024 * 1024,
            "100GOPRO/GOPR0002.JPG": 3 * 1024 * 1024,
            "100GOPRO/GX010003.MP4": 12 * 1024 * 1024,
        }
        self.chunk = chunk
        self.delay = delay
        base = int(time.time()) - 86400
        self.media = [
            RemoteMedia(folder=path.split("/")[0], filename=path.split("/")[1], created=base + i * 60, size=size)
            for i, (path, size) in enumerate(self.files.items())
        ]
        # Testing aids
        self.connect_delay = 0.5 if delay else 0.0
        self.connect_error: Exception | None = None
        self.supports_range = True
        self.fail_path: str | None = None
        self.fail_offset = 0
        self.failures = 0
        self.sleep_calls = 0

    async def pair(self, timeout: float = 60, report: Report = no_report) -> CameraIdentity:
        report("pair", "Searching for the camera over Bluetooth...")
        await asyncio.sleep(1)
        return IDENTITY

    @asynccontextmanager
    async def session(self, identifier: str, report: Report = no_report) -> AsyncIterator[FakeSession]:
        report("ble", f"Waking camera {identifier} over Bluetooth...")
        await asyncio.sleep(self.connect_delay)
        if self.connect_error:
            raise self.connect_error
        report("wifi", f"Joining the camera's Wi-Fi '{IDENTITY.ap_ssid}'...")
        await asyncio.sleep(self.connect_delay)
        report("info", "Reading camera info over Bluetooth...")
        yield FakeSession(self)
