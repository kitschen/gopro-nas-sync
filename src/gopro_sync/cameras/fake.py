"""Simulated camera for development and demos (`gopro-sync serve --demo`)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from gopro_sync.cameras.base import CameraDriver, CameraIdentity, CameraSession, RemoteMedia

IDENTITY = CameraIdentity(
    serial="C3461324701234", identifier="1234", model_name="HERO11 Black (simulated)",
    model_number=58, firmware="H22.01.02.32.00", ap_ssid="GP24701234",
)


class FakeSession(CameraSession):
    def __init__(self, files: dict[str, int], media: list[RemoteMedia], chunk: int, delay: float) -> None:
        self.identity = IDENTITY
        self._files = files
        self._media = media
        self._chunk = chunk
        self._delay = delay

    async def list_media(self) -> list[RemoteMedia]:
        return list(self._media)

    @asynccontextmanager
    async def stream(self, media: RemoteMedia) -> AsyncIterator[tuple[int | None, AsyncIterator[bytes]]]:
        size = self._files[media.path]

        async def chunks() -> AsyncIterator[bytes]:
            sent = 0
            while sent < size:
                n = min(self._chunk, size - sent)
                await asyncio.sleep(self._delay)
                sent += n
                yield b"\0" * n

        yield size, chunks()

    async def sleep(self) -> None:
        pass


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

    async def pair(self, timeout: float = 60) -> CameraIdentity:
        await asyncio.sleep(1)
        return IDENTITY

    @asynccontextmanager
    async def session(self, identifier: str) -> AsyncIterator[FakeSession]:
        await asyncio.sleep(1)
        yield FakeSession(self.files, self.media, self.chunk, self.delay)
