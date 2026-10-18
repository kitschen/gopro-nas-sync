"""HTTP API and status web page."""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gopro_sync import __version__
from gopro_sync.cameras.models import PAIRING_INSTRUCTIONS, lookup
from gopro_sync.config import ConfigStore, NasConfig
from gopro_sync.db import Database
from gopro_sync.nas import NasError, NasManager
from gopro_sync.sync import Busy, SyncService

STATIC_DIR = Path(__file__).parent / "static"


class NasUpdate(NasConfig):
    password: str | None = None  # None = keep stored password
    layout: str | None = None


class CameraUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None


def create_app(config: ConfigStore, db: Database, sync: SyncService, nas: NasManager) -> FastAPI:
    app = FastAPI(title="gopro-nas-sync", version=__version__)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def require_token(request: Request) -> None:
        token = config.config.server.api_token
        if not token:
            return
        header = request.headers.get("authorization", "")
        supplied = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(401, "Missing or invalid API token")

    auth = [Depends(require_token)]

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # Sync

    @app.post("/api/sync", status_code=202, dependencies=auth)
    async def start_sync(camera: str | None = None) -> dict:
        try:
            job = sync.start_sync(camera)
        except Busy as e:
            raise HTTPException(409, str(e))
        return {"job_id": job.id}

    @app.get("/api/status")
    async def status() -> dict:
        return {**sync.status(), "version": __version__, "auth_required": bool(config.config.server.api_token)}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream():
            last = None
            while not await request.is_disconnected():
                payload = json.dumps(sync.status())
                if payload != last:
                    yield f"data: {payload}\n\n"
                    last = payload
                else:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Cameras

    @app.get("/api/cameras")
    async def cameras() -> dict:
        items = []
        for cam in db.list_cameras():
            info = lookup(cam.model_name)
            items.append({**asdict(cam), "family": info.family, "ble_wake": info.ble_wake, "notes": info.notes})
        return {"cameras": items, "pairing_instructions": PAIRING_INSTRUCTIONS}

    @app.post("/api/cameras/pair", status_code=202, dependencies=auth)
    async def pair() -> dict:
        try:
            job = sync.start_pair()
        except Busy as e:
            raise HTTPException(409, str(e))
        return {"job_id": job.id}

    @app.patch("/api/cameras/{serial}", dependencies=auth)
    async def update_camera(serial: str, body: CameraUpdate) -> dict:
        if not db.update_camera(serial, name=body.name, enabled=body.enabled):
            raise HTTPException(404, "Unknown camera")
        return {"ok": True}

    @app.delete("/api/cameras/{serial}", dependencies=auth)
    async def delete_camera(serial: str) -> dict:
        if not db.delete_camera(serial):
            raise HTTPException(404, "Unknown camera")
        return {"ok": True}

    # NAS

    @app.get("/api/nas")
    async def get_nas() -> dict:
        cfg = config.config
        status = await nas.status(cfg.nas)
        return {"config": cfg.nas.model_dump(mode="json"), "layout": cfg.storage.layout, "status": status.as_dict()}

    @app.put("/api/nas", dependencies=auth)
    async def put_nas(body: NasUpdate) -> dict:
        new_nas = NasConfig.model_validate(body.model_dump(exclude={"password", "layout"}))
        try:
            result = await nas.apply(new_nas, body.password)
        except NasError as e:
            raise HTTPException(400, str(e))
        cfg = config.config.model_copy(deep=True)
        cfg.nas = new_nas
        if body.layout:
            cfg.storage.layout = body.layout
        config.save(cfg)
        return {"ok": True, "helper": result, "status": (await nas.status(new_nas)).as_dict()}

    @app.post("/api/nas/test", dependencies=auth)
    async def test_nas(body: NasUpdate | None = None) -> dict:
        target = NasConfig.model_validate(body.model_dump(exclude={"password", "layout"})) if body else config.config.nas
        return await nas.test_connection(target)

    @app.post("/api/nas/mount", dependencies=auth)
    async def mount_nas() -> dict:
        try:
            return await nas.mount()
        except NasError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/nas/unmount", dependencies=auth)
    async def unmount_nas() -> dict:
        if sync.busy:
            raise HTTPException(409, "Cannot unmount while a sync is running")
        try:
            return await nas.unmount()
        except NasError as e:
            raise HTTPException(400, str(e))

    # Transferred files

    @app.get("/api/transferred")
    async def transferred(camera: str | None = None, search: str = "", limit: int = 200, offset: int = 0) -> dict:
        items, total = db.list_transferred(camera, search, min(limit, 1000), offset)
        return {"items": [asdict(i) for i in items], "total": total}

    @app.delete("/api/transferred", dependencies=auth)
    async def reset_transferred(camera: str | None = None) -> dict:
        return {"deleted": db.reset_transferred(camera)}

    @app.delete("/api/transferred/{file_id}", dependencies=auth)
    async def delete_transferred(file_id: int) -> dict:
        if not db.delete_transferred(file_id):
            raise HTTPException(404, "Unknown entry")
        return {"ok": True}

    return app
