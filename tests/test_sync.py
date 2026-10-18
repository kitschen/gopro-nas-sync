from pathlib import Path

import pytest

from gopro_sync.cameras.fake import FakeSession
from gopro_sync.sync import Busy


async def run_sync(service):
    service.start_sync()
    await service.wait()
    return service.last_job


async def test_sync_transfers_new_files_once(service, db, target: Path):
    job = await run_sync(service)
    assert job.phase == "done" and job.error is None
    files = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert len(files) == 2
    assert all(f.startswith("GoPro/Helmet cam/") for f in files)
    assert not any(f.endswith(".part") for f in files)
    assert {p.stat().st_size for p in target.rglob("*") if p.is_file()} == {3000, 1500}

    job = await run_sync(service)
    assert job.cameras[0].files_new == 0


async def test_reset_single_entry_retransfers_it(service, db):
    await run_sync(service)
    items, total = db.list_transferred()
    assert total == 2
    db.delete_transferred(items[0].id)
    job = await run_sync(service)
    assert job.cameras[0].files_new == 1 and job.cameras[0].files_done == 1


async def test_size_mismatch_is_not_recorded(service, db, monkeypatch, target: Path):
    original = FakeSession.stream

    def short_stream(self, media):
        cm = original(self, media)

        class Wrapper:
            async def __aenter__(self_inner):
                length, chunks = await cm.__aenter__()
                return length + 1, chunks

            async def __aexit__(self_inner, *exc):
                return await cm.__aexit__(*exc)

        return Wrapper()

    monkeypatch.setattr(FakeSession, "stream", short_stream)
    job = await run_sync(service)
    assert job.cameras[0].files_done == 0
    assert "size mismatch" in job.cameras[0].error
    assert db.list_transferred()[1] == 0
    assert not list(target.rglob("*.part"))


async def test_not_enough_space_aborts_camera(service, nas):
    nas.free_bytes = 100
    job = await run_sync(service)
    assert "Not enough space" in job.cameras[0].error


async def test_only_one_job_at_a_time(service):
    service.start_sync()
    with pytest.raises(Busy):
        service.start_sync()
    await service.wait()


async def test_stop_keeps_finished_files_and_resumes(config, db, nas, target: Path, monkeypatch):
    import asyncio

    from gopro_sync.cameras.fake import FakeDriver
    from gopro_sync.sync import SyncService

    files = {f"100GOPRO/GX01000{i}.MP4": 5000 for i in range(1, 4)}
    service = SyncService(config, db, FakeDriver(files=files, chunk=1000, delay=0.02), nas)
    slept = []
    original_sleep = FakeSession.sleep

    async def record_sleep(self):
        slept.append(True)
        await original_sleep(self)

    monkeypatch.setattr(FakeSession, "sleep", record_sleep)

    service.start_sync()
    while not (service.job and service.job.queue_index == 2 and service.job.current
               and service.job.current.bytes_done > 0):
        await asyncio.sleep(0.01)
    assert service.stop()
    await service.wait()

    job = service.last_job
    assert job.phase == "stopped" and job.error is None
    assert job.cameras[0].files_done == 1
    assert db.list_transferred()[1] == 1
    assert not list(target.rglob("*.part"))
    assert len([p for p in target.rglob("*") if p.is_file()]) == 1
    assert slept, "camera should be put back to sleep when stopped"
    assert db.list_cameras()[0].last_error == "Stopped by user"

    service.start_sync()
    await service.wait()
    assert service.last_job.cameras[0].files_new == 2
    assert service.last_job.cameras[0].files_done == 2
    assert db.list_transferred()[1] == 3


async def test_stop_without_job(service):
    assert service.stop() is False


async def test_pairing_stores_camera(service, db):
    db.delete_camera(db.list_cameras()[0].serial)
    service.start_pair()
    await service.wait()
    cams = db.list_cameras()
    assert len(cams) == 1 and cams[0].identifier == "1234"
