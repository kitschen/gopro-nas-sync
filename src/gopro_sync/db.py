"""SQLite state: paired cameras and the list of already transferred files."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    serial        TEXT PRIMARY KEY,
    identifier    TEXT NOT NULL,          -- last 4 serial digits, used for the BLE scan
    name          TEXT NOT NULL,
    model_name    TEXT NOT NULL,
    model_number  INTEGER NOT NULL,
    firmware      TEXT NOT NULL,
    ap_ssid       TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    paired_at     REAL NOT NULL,
    last_sync_at  REAL,
    last_error    TEXT
);
CREATE TABLE IF NOT EXISTS transferred (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_serial  TEXT NOT NULL,
    camera_path    TEXT NOT NULL,         -- e.g. 100GOPRO/GX010001.MP4
    created        INTEGER NOT NULL,      -- camera creation timestamp; distinguishes reused names
    size           INTEGER NOT NULL,
    nas_path       TEXT NOT NULL,
    transferred_at REAL NOT NULL,
    UNIQUE (camera_serial, camera_path, created)
);
"""


@dataclass
class Camera:
    serial: str
    identifier: str
    name: str
    model_name: str
    model_number: int
    firmware: str
    ap_ssid: str
    enabled: bool
    paired_at: float
    last_sync_at: float | None
    last_error: str | None


@dataclass
class TransferredFile:
    id: int
    camera_serial: str
    camera_path: str
    created: int
    size: int
    nas_path: str
    transferred_at: float


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._conn:
            return self._conn.execute(sql, params).rowcount

    # Cameras

    def upsert_camera(self, camera: Camera) -> None:
        self._execute(
            """INSERT INTO cameras (serial, identifier, name, model_name, model_number, firmware, ap_ssid,
                                    enabled, paired_at, last_sync_at, last_error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(serial) DO UPDATE SET
                   identifier=excluded.identifier, model_name=excluded.model_name,
                   model_number=excluded.model_number, firmware=excluded.firmware,
                   ap_ssid=excluded.ap_ssid, paired_at=excluded.paired_at""",
            (
                camera.serial, camera.identifier, camera.name, camera.model_name, camera.model_number,
                camera.firmware, camera.ap_ssid, int(camera.enabled), camera.paired_at,
                camera.last_sync_at, camera.last_error,
            ),
        )

    def list_cameras(self) -> list[Camera]:
        return [self._camera(r) for r in self._query("SELECT * FROM cameras ORDER BY paired_at")]

    def get_camera(self, serial: str) -> Camera | None:
        rows = self._query("SELECT * FROM cameras WHERE serial = ?", (serial,))
        return self._camera(rows[0]) if rows else None

    def update_camera(self, serial: str, *, name: str | None = None, enabled: bool | None = None) -> bool:
        changed = 0
        if name is not None:
            changed += self._execute("UPDATE cameras SET name = ? WHERE serial = ?", (name, serial))
        if enabled is not None:
            changed += self._execute("UPDATE cameras SET enabled = ? WHERE serial = ?", (int(enabled), serial))
        return changed > 0

    def record_camera_sync(self, serial: str, error: str | None) -> None:
        self._execute(
            "UPDATE cameras SET last_sync_at = ?, last_error = ? WHERE serial = ?", (time.time(), error, serial)
        )

    def delete_camera(self, serial: str) -> bool:
        return self._execute("DELETE FROM cameras WHERE serial = ?", (serial,)) > 0

    @staticmethod
    def _camera(row: sqlite3.Row) -> Camera:
        return Camera(**{**dict(row), "enabled": bool(row["enabled"])})

    # Transferred files

    def transferred_keys(self, camera_serial: str) -> set[tuple[str, int]]:
        rows = self._query(
            "SELECT camera_path, created FROM transferred WHERE camera_serial = ?", (camera_serial,)
        )
        return {(r["camera_path"], r["created"]) for r in rows}

    def add_transferred(self, camera_serial: str, camera_path: str, created: int, size: int, nas_path: str) -> None:
        self._execute(
            """INSERT INTO transferred (camera_serial, camera_path, created, size, nas_path, transferred_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(camera_serial, camera_path, created) DO UPDATE SET
                   size=excluded.size, nas_path=excluded.nas_path, transferred_at=excluded.transferred_at""",
            (camera_serial, camera_path, created, size, nas_path, time.time()),
        )

    def list_transferred(
        self, camera_serial: str | None = None, search: str = "", limit: int = 500, offset: int = 0
    ) -> tuple[list[TransferredFile], int]:
        where, params = ["1=1"], []
        if camera_serial:
            where.append("camera_serial = ?")
            params.append(camera_serial)
        if search:
            where.append("(camera_path LIKE ? OR nas_path LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        clause = " AND ".join(where)
        total = self._query(f"SELECT COUNT(*) AS n FROM transferred WHERE {clause}", tuple(params))[0]["n"]
        rows = self._query(
            f"SELECT * FROM transferred WHERE {clause} ORDER BY transferred_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [TransferredFile(**dict(r)) for r in rows], total

    def delete_transferred(self, file_id: int) -> bool:
        return self._execute("DELETE FROM transferred WHERE id = ?", (file_id,)) > 0

    def reset_transferred(self, camera_serial: str | None = None) -> int:
        if camera_serial:
            return self._execute("DELETE FROM transferred WHERE camera_serial = ?", (camera_serial,))
        return self._execute("DELETE FROM transferred")
