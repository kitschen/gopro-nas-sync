"""Driver for cameras speaking Open GoPro API 2.0 (HERO9 and newer)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from open_gopro import WirelessGoPro
from open_gopro.models import constants
from open_gopro.network.wifi import SsidState, WifiController

from gopro_sync.cameras.base import CameraDriver, CameraIdentity, CameraSession, RemoteMedia
from gopro_sync.cameras.media import parse_media_list
from gopro_sync.cameras.wifi import NmcliProfileWifi

logger = logging.getLogger(__name__)

CAMERA_BASE_URL = "http://10.5.5.9:8080"
CHUNK_SIZE = 1024 * 1024


class _NoWifi(WifiController):
    """Placeholder for BLE-only sessions (pairing), so no Wi-Fi hardware is touched."""

    def __init__(self, interface: str | None = None, password: str | None = None) -> None:
        WifiController.__init__(self, interface, password)
        self._interface = "none"

    async def connect(self, ssid: str, password: str, timeout: float = 15) -> bool:
        return False

    async def disconnect(self) -> bool:
        return True

    def current(self) -> tuple[str | None, SsidState]:
        return None, SsidState.DISCONNECTED

    def available_interfaces(self) -> list[str]:
        return ["none"]

    def power(self, power: bool) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return False


async def _identity(gopro: WirelessGoPro) -> CameraIdentity:
    info = (await gopro.ble_command.get_hardware_info()).data
    return CameraIdentity(
        serial=info.serial_number,
        identifier=info.serial_number[-4:],
        model_name=info.model_name,
        model_number=info.model_number,
        firmware=info.firmware_version,
        ap_ssid=info.ap_ssid,
    )


class OpenGoProSession(CameraSession):
    def __init__(self, gopro: WirelessGoPro, identity: CameraIdentity) -> None:
        self._gopro = gopro
        self.identity = identity
        self._http = httpx.AsyncClient(
            base_url=CAMERA_BASE_URL, timeout=httpx.Timeout(30.0, connect=10.0, read=60.0)
        )

    async def list_media(self) -> list[RemoteMedia]:
        resp = await self._http.get("/gopro/media/list")
        resp.raise_for_status()
        return parse_media_list(resp.json())

    @asynccontextmanager
    async def stream(self, media: RemoteMedia) -> AsyncIterator[tuple[int | None, AsyncIterator[bytes]]]:
        async with self._http.stream("GET", f"/videos/DCIM/{media.path}") as resp:
            resp.raise_for_status()
            length = resp.headers.get("content-length")
            yield (int(length) if length else None), resp.aiter_bytes(CHUNK_SIZE)

    async def set_turbo(self, enable: bool) -> None:
        try:
            mode = constants.Toggle.ENABLE if enable else constants.Toggle.DISABLE
            await self._gopro.http_command.set_turbo_mode(mode=mode)
        except Exception as e:  # not supported by every model / firmware
            logger.info("Turbo transfer mode not available: %r", e)

    async def sleep(self) -> None:
        try:
            await self._gopro.ble_command.sleep()
        except Exception as e:
            logger.warning("Could not put camera to sleep: %r", e)

    async def aclose(self) -> None:
        await self._http.aclose()


class OpenGoProDriver(CameraDriver):
    def __init__(self, wifi_interface: str, state_dir: Path) -> None:
        self._wifi_interface = wifi_interface
        self._cohn_db = state_dir / "cohn_db.json"

    async def pair(self, timeout: float = 60) -> CameraIdentity:
        gopro = WirelessGoPro(
            target=None,
            interfaces={WirelessGoPro.Interface.BLE},
            cohn_db=self._cohn_db,
            wifi_adapter=_NoWifi,
        )
        await gopro.open(timeout=int(timeout), retries=3)
        try:
            return await _identity(gopro)
        finally:
            await gopro.close()

    @asynccontextmanager
    async def session(self, identifier: str) -> AsyncIterator[OpenGoProSession]:
        gopro = WirelessGoPro(
            target=identifier,
            host_wifi_interface=self._wifi_interface,
            interfaces={WirelessGoPro.Interface.BLE, WirelessGoPro.Interface.WIFI_AP},
            cohn_db=self._cohn_db,
            wifi_adapter=NmcliProfileWifi,
        )
        await gopro.open(timeout=30, retries=5)
        session: OpenGoProSession | None = None
        try:
            session = OpenGoProSession(gopro, await _identity(gopro))
            await session.set_turbo(True)
            yield session
        finally:
            if session is not None:
                await session.set_turbo(False)
                await session.aclose()
            await gopro.close()
