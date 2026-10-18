from pathlib import Path

import pytest

from gopro_sync.cameras.fake import IDENTITY, FakeDriver
from gopro_sync.config import Config, ConfigStore, NasConfig, NasType
from gopro_sync.db import Camera, Database
from gopro_sync.nas import NasManager, NasStatus
from gopro_sync.sync import SyncService


class StubNas(NasManager):
    def __init__(self) -> None:
        super().__init__("eth0", helper=Path("/nonexistent"))
        self.free_bytes: int | None = 10**12

    async def preflight(self, nas: NasConfig) -> NasStatus:
        return NasStatus(type="path", target_dir=str(nas.target_dir), mounted=True, writable=True,
                         free_bytes=self.free_bytes)


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = tmp_path / "nas"
    path.mkdir()
    return path


@pytest.fixture
def config(tmp_path: Path, target: Path) -> ConfigStore:
    store = ConfigStore(tmp_path / "config.toml")
    store.save(Config(nas=NasConfig(type=NasType.PATH, path=str(target), subdir="GoPro")))
    return store


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state.db")
    database.upsert_camera(Camera(
        serial=IDENTITY.serial, identifier=IDENTITY.identifier, name="Helmet cam", model_name=IDENTITY.model_name,
        model_number=IDENTITY.model_number, firmware=IDENTITY.firmware, ap_ssid=IDENTITY.ap_ssid, enabled=True,
        paired_at=0, last_sync_at=None, last_error=None,
    ))
    return database


@pytest.fixture
def driver() -> FakeDriver:
    return FakeDriver(files={"100GOPRO/GX010001.MP4": 3000, "100GOPRO/GOPR0002.JPG": 1500}, chunk=1000, delay=0)


@pytest.fixture
def nas() -> StubNas:
    return StubNas()


@pytest.fixture
def service(config, db, driver, nas) -> SyncService:
    return SyncService(config, db, driver, nas)
