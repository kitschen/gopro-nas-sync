"""Driver for cameras speaking Open GoPro API 2.0 (HERO9 and newer)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path

import httpx
from open_gopro import WirelessGoPro
from open_gopro.features import CohnFeature
from open_gopro.models import constants
from open_gopro.network.wifi import SsidState, WifiController
from tinydb import TinyDB
from tinydb.storages import MemoryStorage

from gopro_sync.cameras.base import (
    CameraDriver, CameraIdentity, CameraSession, MediaStream, RemoteMedia, Report, no_report,
)
from gopro_sync.cameras.ble import NonInteractiveBleController, disconnect_stale_gopros
from gopro_sync.cameras.media import parse_media_list
from gopro_sync.cameras.wifi import NmcliProfileWifi
from gopro_sync.cameras.wifi import logger as wifi_logger

logger = logging.getLogger(__name__)

CAMERA_BASE_URL = "http://10.5.5.9:8080"
CHUNK_SIZE = 1024 * 1024
KEEP_ALIVE_INTERVAL = 3.0
STEP_TIMEOUT = 20.0
OPEN_TIMEOUT = 240.0


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


class _NoCohn(CohnFeature):
    """COHN is unused here. The real feature registers for COHN status in a background task while
    open() sends its own commands; the SDK matches BLE responses strictly in order, so a rejected
    COHN request (HERO11 answers INVALID_PARAM) gets paired with the wrong command and open() fails."""

    def __init__(self) -> None:
        super().__init__(TinyDB(storage=MemoryStorage))

    async def open(self, loop, gopro, *args, **kwargs) -> None:  # type: ignore[override]
        self._gopro = gopro
        self._supported = False

    async def wait_until_ready(self) -> None:
        return

    async def close(self) -> None:
        return


def _wireless_gopro(**kwargs) -> WirelessGoPro:
    # maintain_state=False: the SDK's busy/encoding tracking can leave its internal ready lock held after the
    # camera reports ready, after which every command (including its keep-alive) blocks forever. We never
    # record, so it is not needed; the keep-alive is sent by _keep_alive() instead.
    gopro = WirelessGoPro(ble_adapter=NonInteractiveBleController, maintain_state=False, **kwargs)
    gopro.cohn = _NoCohn()
    return gopro


async def _keep_alive(gopro: WirelessGoPro) -> None:
    while True:
        await asyncio.sleep(KEEP_ALIVE_INTERVAL)
        try:
            await asyncio.wait_for(gopro.keep_alive(), STEP_TIMEOUT)
        except Exception as e:
            logger.warning("Keep-alive failed: %r", e)


# SDK log messages that mark connection progress -> (step key, text shown in the UI)
_SDK_PROGRESS = [
    ("Opening BLE connection", "ble", "Searching for the camera over Bluetooth..."),
    ("Failed to connect. Retrying", "ble", "Bluetooth connection failed, retrying..."),
    ("BLE connection is ready", "ble", "Bluetooth connected, setting up the camera..."),
    ("Error while opening", "ble", "Connection attempt failed, starting over..."),
    ("Discovering Wifi AP info", "wifi", "Turning on the camera's Wi-Fi..."),
]


class _ProgressBridge(logging.Handler):
    """Turns SDK and Wi-Fi log records into progress reports while a connection is being opened."""

    def __init__(self, report: Report) -> None:
        super().__init__(logging.INFO)
        self._report = report

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.name == wifi_logger.name:
                self._report("wifi", message)
                return
            for prefix, key, text in _SDK_PROGRESS:
                if message.startswith(prefix):
                    self._report(key, text)
                    return
        except Exception:
            pass

    @contextmanager
    def attached(self) -> Iterator[None]:
        loggers = [logging.getLogger("open_gopro"), wifi_logger]
        for lg in loggers:
            lg.addHandler(self)
        try:
            yield
        finally:
            for lg in loggers:
                lg.removeHandler(self)


async def _step(coro, seconds: float, what: str):
    try:
        return await asyncio.wait_for(coro, seconds)
    except TimeoutError:
        raise TimeoutError(f"No answer from the camera within {seconds:.0f} s while {what}") from None


def _content_range_total(header: str | None) -> int | None:
    """Total size from 'bytes 100-199/1000'."""
    if not header or "/" not in header:
        return None
    total = header.rsplit("/", 1)[1].strip()
    return int(total) if total.isdigit() else None


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
        # No connection reuse: the camera closes idle keep-alive connections without notice, which made
        # the next request fail with "Server disconnected without sending a response".
        self._http = httpx.AsyncClient(
            base_url=CAMERA_BASE_URL,
            timeout=httpx.Timeout(30.0, connect=10.0, read=30.0),
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def list_media(self) -> list[RemoteMedia]:
        for attempt in range(1, 4):
            try:
                resp = await self._http.get("/gopro/media/list")
                resp.raise_for_status()
                return parse_media_list(resp.json())
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if attempt == 3:
                    raise
                logger.warning("Reading the media list failed (%r), retrying", e)
                await asyncio.sleep(2 * attempt)
        raise AssertionError("unreachable")

    @asynccontextmanager
    async def stream(self, media: RemoteMedia, offset: int = 0) -> AsyncIterator[MediaStream]:
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with self._http.stream("GET", f"/videos/DCIM/{media.path}", headers=headers) as resp:
            resp.raise_for_status()
            length = resp.headers.get("content-length")
            if resp.status_code == 206 and (total := _content_range_total(resp.headers.get("content-range"))):
                yield MediaStream(total=total, start=offset, chunks=resp.aiter_bytes(CHUNK_SIZE))
            else:
                yield MediaStream(total=int(length) if length else None, start=0, chunks=resp.aiter_bytes(CHUNK_SIZE))

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

    async def pair(self, timeout: float = 60, report: Report = no_report) -> CameraIdentity:
        await disconnect_stale_gopros()
        gopro = _wireless_gopro(
            target=None,
            interfaces={WirelessGoPro.Interface.BLE},
            cohn_db=self._cohn_db,
            wifi_adapter=_NoWifi,
        )
        bridge = _ProgressBridge(lambda _key, text: report("pair", text))
        try:
            with bridge.attached():
                # open() itself retries 5 times; keep each scan short so a failure is reported within minutes
                await _step(gopro.open(timeout=10, retries=3), OPEN_TIMEOUT, "pairing over Bluetooth")
            report("pair", "Reading camera info...")
            return await _step(_identity(gopro), STEP_TIMEOUT, "reading the camera info over Bluetooth")
        finally:
            await gopro.close()

    @asynccontextmanager
    async def session(self, identifier: str, report: Report = no_report) -> AsyncIterator[OpenGoProSession]:
        report("ble", f"Waking camera {identifier} over Bluetooth...")
        await disconnect_stale_gopros()
        gopro = _wireless_gopro(
            target=identifier,
            host_wifi_interface=self._wifi_interface,
            interfaces={WirelessGoPro.Interface.BLE, WirelessGoPro.Interface.WIFI_AP},
            cohn_db=self._cohn_db,
            wifi_adapter=NmcliProfileWifi,
        )
        session: OpenGoProSession | None = None
        keep_alive: asyncio.Task | None = None
        try:
            with _ProgressBridge(report).attached():
                await _step(
                    gopro.open(timeout=15, retries=3), OPEN_TIMEOUT, "connecting over Bluetooth and joining its Wi-Fi"
                )
            keep_alive = asyncio.create_task(_keep_alive(gopro))
            report("info", "Reading camera info over Bluetooth...")
            identity = await _step(_identity(gopro), STEP_TIMEOUT, "reading the camera info over Bluetooth")
            session = OpenGoProSession(gopro, identity)
            report("info", "Enabling turbo transfer mode...")
            await _step(session.set_turbo(True), STEP_TIMEOUT, "enabling turbo transfer over Wi-Fi")
            yield session
        finally:
            if session is not None:
                with suppress(Exception):
                    await asyncio.wait_for(session.set_turbo(False), STEP_TIMEOUT)
                await session.aclose()
            if keep_alive is not None:
                keep_alive.cancel()
                with suppress(asyncio.CancelledError):
                    await keep_alive
            await gopro.close()
