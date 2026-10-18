"""Sync worker: one job at a time, wakes each enabled camera and streams new media to the NAS."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from gopro_sync.cameras.base import CameraDriver, CameraSession, RemoteMedia
from gopro_sync.config import ConfigStore
from gopro_sync.db import Camera, Database
from gopro_sync.nas import NasError, NasManager

logger = logging.getLogger(__name__)


class Busy(RuntimeError):
    pass


@dataclass
class FileProgress:
    camera_path: str
    nas_path: str
    bytes_done: int = 0
    bytes_total: int | None = None
    started_at: float = field(default_factory=time.time)

    @property
    def speed(self) -> float:
        elapsed = time.time() - self.started_at
        return self.bytes_done / elapsed if elapsed > 0 else 0.0


@dataclass
class CameraResult:
    serial: str
    name: str
    files_found: int = 0
    files_new: int = 0
    files_done: int = 0
    bytes_done: int = 0
    error: str | None = None


@dataclass
class Job:
    id: str
    kind: str  # "sync" | "pair"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    phase: str = "starting"
    message: str = ""
    camera: str | None = None
    queue_index: int = 0
    queue_total: int = 0
    current: FileProgress | None = None
    cameras: list[CameraResult] = field(default_factory=list)
    error: str | None = None

    def snapshot(self) -> dict:
        data = asdict(self)
        if self.current:
            data["current"]["speed"] = self.current.speed
            remaining = (self.current.bytes_total or 0) - self.current.bytes_done
            data["current"]["eta"] = remaining / self.current.speed if self.current.speed and remaining > 0 else None
        return data


_SAFE = re.compile(r"[^A-Za-z0-9._ ()\-]+")


def safe_name(value: str) -> str:
    return _SAFE.sub("_", value).strip(" .") or "camera"


def destination(target_dir: Path, layout: str, camera: Camera, media: RemoteMedia) -> Path:
    # Camera timestamps are local camera time encoded as epoch seconds, so interpret them as UTC
    dt = datetime.fromtimestamp(media.created, tz=timezone.utc)
    rel = layout.format(
        camera=safe_name(camera.name), date=dt.strftime("%Y-%m-%d"), year=dt.strftime("%Y"),
        month=dt.strftime("%m"), folder=media.folder,
    )
    parts = [safe_name(p) for p in Path(rel).parts if p not in ("", ".", "..", "/", "\\")]
    return target_dir.joinpath(*parts, media.filename)


class SyncService:
    def __init__(self, config: ConfigStore, db: Database, driver: CameraDriver, nas: NasManager) -> None:
        self.config = config
        self.db = db
        self.driver = driver
        self.nas = nas
        self._task: asyncio.Task | None = None
        self.job: Job | None = None
        self.last_job: Job | None = None

    @property
    def busy(self) -> bool:
        return self.job is not None

    def status(self) -> dict:
        return {
            "busy": self.busy,
            "job": self.job.snapshot() if self.job else None,
            "last_job": self.last_job.snapshot() if self.last_job else None,
        }

    def _start(self, kind: str, coro_factory) -> Job:
        if self.job is not None:
            raise Busy("A sync or pairing is already running")
        job = self.job = Job(id=uuid.uuid4().hex[:12], kind=kind)

        async def runner() -> None:
            try:
                await coro_factory(job)
                job.phase = "done"
            except Exception as e:
                logger.exception("%s job failed", kind)
                job.phase = "failed"
                job.error = str(e) or repr(e)
            finally:
                job.current = None
                job.finished_at = time.time()
                self.last_job, self.job = job, None

        self._task = asyncio.create_task(runner())
        return job

    def start_sync(self, camera_serial: str | None = None) -> Job:
        return self._start("sync", lambda job: self._sync(job, camera_serial))

    def start_pair(self, timeout: float = 60) -> Job:
        return self._start("pair", lambda job: self._pair(job, timeout))

    async def wait(self) -> None:
        if self._task:
            await self._task

    # Pairing

    async def _pair(self, job: Job, timeout: float) -> None:
        job.phase = "pairing"
        job.message = "Scanning for a camera in pairing mode..."
        identity = await self.driver.pair(timeout)
        existing = self.db.get_camera(identity.serial)
        self.db.upsert_camera(Camera(
            serial=identity.serial, identifier=identity.identifier,
            name=existing.name if existing else f"{identity.model_name} {identity.identifier}",
            model_name=identity.model_name, model_number=identity.model_number, firmware=identity.firmware,
            ap_ssid=identity.ap_ssid, enabled=True, paired_at=time.time(),
            last_sync_at=existing.last_sync_at if existing else None, last_error=None,
        ))
        job.camera = identity.serial
        job.message = f"Paired {identity.model_name} ({identity.serial}), firmware {identity.firmware}"

    # Sync

    async def _sync(self, job: Job, camera_serial: str | None) -> None:
        cameras = [c for c in self.db.list_cameras() if c.enabled and (camera_serial in (None, c.serial))]
        if not cameras:
            raise RuntimeError("No (enabled) paired camera" + (f" with serial {camera_serial}" if camera_serial else ""))

        cfg = self.config.config
        job.phase = "preflight"
        job.message = "Checking NAS..."
        nas_status = await self.nas.preflight(cfg.nas)

        for camera in cameras:
            result = CameraResult(serial=camera.serial, name=camera.name)
            job.cameras.append(result)
            job.camera = camera.serial
            try:
                await self._sync_camera(job, camera, result, nas_status.free_bytes)
            except Exception as e:
                logger.exception("Sync of camera %s failed", camera.serial)
                result.error = str(e) or repr(e)
            self.db.record_camera_sync(camera.serial, result.error)

        failed = [r for r in job.cameras if r.error]
        job.message = (
            f"Transferred {sum(r.files_done for r in job.cameras)} file(s) from {len(job.cameras)} camera(s)"
        )
        if failed:
            job.error = "; ".join(f"{r.name}: {r.error}" for r in failed)

    async def _sync_camera(self, job: Job, camera: Camera, result: CameraResult, free_bytes: int | None) -> None:
        cfg = self.config.config
        job.phase = "connecting"
        job.message = f"Waking {camera.name} over Bluetooth and joining its Wi-Fi..."
        async with self.driver.session(camera.identifier) as session:
            if session.identity.serial != camera.serial:
                raise RuntimeError(f"Connected to camera {session.identity.serial}, expected {camera.serial}")
            job.phase = "listing"
            job.message = f"Reading media list of {camera.name}..."
            media = await session.list_media()
            done = self.db.transferred_keys(camera.serial)
            new = [m for m in media if (m.path, m.created) not in done]
            new.sort(key=lambda m: (m.created, m.path))
            result.files_found, result.files_new = len(media), len(new)

            needed = sum(m.size or 0 for m in new)
            if free_bytes is not None and needed > free_bytes:
                raise NasError(f"Not enough space on NAS: need {needed} bytes, {free_bytes} free")

            job.phase = "transferring"
            job.queue_total = len(new)
            errors: list[str] = []
            for index, item in enumerate(new, start=1):
                job.queue_index = index
                dest = destination(cfg.nas.target_dir, cfg.storage.layout, camera, item)
                job.message = f"{camera.name}: {item.path}"
                try:
                    size = await self._transfer(job, session, item, dest)
                except (httpx.TransportError, ConnectionError) as e:
                    raise RuntimeError(f"Connection to camera lost during {item.path}: {e!r}") from e
                except Exception as e:
                    logger.exception("Transfer of %s failed", item.path)
                    errors.append(f"{item.path}: {e}")
                    continue
                self.db.add_transferred(camera.serial, item.path, item.created, size, str(dest))
                result.files_done += 1
                result.bytes_done += size
            job.current = None

            job.phase = "finishing"
            job.message = f"Putting {camera.name} to sleep..."
            await session.sleep()
            if errors:
                raise RuntimeError(f"{len(errors)} file(s) failed: " + "; ".join(errors[:3]))

    async def _transfer(self, job: Job, session: CameraSession, item: RemoteMedia, dest: Path) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        progress = FileProgress(camera_path=item.path, nas_path=str(dest), bytes_total=item.size)
        job.current = progress
        try:
            async with session.stream(item) as (length, chunks):
                if length is not None:
                    progress.bytes_total = length
                with part.open("wb") as fp:
                    async for chunk in chunks:
                        await asyncio.to_thread(fp.write, chunk)
                        progress.bytes_done += len(chunk)
                    await asyncio.to_thread(fp.flush)
                    await asyncio.to_thread(os.fsync, fp.fileno())
            expected = progress.bytes_total
            if expected is not None and progress.bytes_done != expected:
                raise IOError(f"size mismatch: got {progress.bytes_done} of {expected} bytes")
            os.replace(part, dest)
            return progress.bytes_done
        except BaseException:
            part.unlink(missing_ok=True)
            raise
