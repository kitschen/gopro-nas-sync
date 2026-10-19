"""Camera driver interface. Sync logic only depends on this module."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass


@dataclass(frozen=True)
class CameraIdentity:
    serial: str
    identifier: str  # last 4 serial digits; what the BLE advertisement name ends with
    model_name: str
    model_number: int
    firmware: str
    ap_ssid: str


@dataclass(frozen=True)
class RemoteMedia:
    folder: str  # e.g. 100GOPRO
    filename: str  # e.g. GX010001.MP4
    created: int  # seconds since epoch (camera local time)
    size: int | None  # None for members of grouped media (bursts, time lapses)

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.filename}"


@dataclass
class MediaStream:
    total: int | None  # full file size, if the camera reports it
    start: int  # byte offset the chunks begin at (0 if the camera ignored the requested offset)
    chunks: AsyncIterator[bytes]


class CameraSession(ABC):
    """An open connection (BLE + Wi-Fi) to one camera."""

    identity: CameraIdentity

    @abstractmethod
    async def list_media(self) -> list[RemoteMedia]: ...

    @abstractmethod
    def stream(self, media: RemoteMedia, offset: int = 0) -> AbstractAsyncContextManager[MediaStream]:
        """Stream one file, starting at `offset` if the camera supports range requests."""

    @abstractmethod
    async def sleep(self) -> None:
        """Put the camera back into standby."""


class CameraDriver(ABC):
    @abstractmethod
    async def pair(self, timeout: float) -> CameraIdentity:
        """Find a camera in pairing mode, pair with it and return its identity."""

    @abstractmethod
    def session(self, identifier: str) -> AbstractAsyncContextManager[CameraSession]:
        """Wake the camera with the given identifier and open BLE + Wi-Fi."""
