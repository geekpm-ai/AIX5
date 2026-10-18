"""Legacy phase-0 schema; the running application uses store.Store instead."""

from __future__ import annotations
import aiosqlite
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras(id TEXT PRIMARY KEY,name TEXT NOT NULL,source_type TEXT NOT NULL,rtsp_url TEXT NOT NULL,transport TEXT NOT NULL DEFAULT 'tcp',enabled INTEGER NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'disconnected',last_error TEXT,last_frame_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,camera_id TEXT NOT NULL,event_type TEXT NOT NULL,target_type TEXT,confidence REAL,occurred_at TEXT NOT NULL,screenshot_path TEXT,bbox_json TEXT,FOREIGN KEY(camera_id) REFERENCES cameras(id));
CREATE INDEX IF NOT EXISTS idx_events_time ON events(occurred_at);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            "PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;" + SCHEMA
        )
        await db.commit()


async def connect():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=5000")
    return db
