"""SQLite 存储：录音、上传会话、转写分段、任务队列、设置、声纹库。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,              -- 卡上原始文件名，与 size 一起作为同步比对键
    size INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    recorded_at TEXT,
    audio_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    duration_s REAL,
    status TEXT NOT NULL,            -- queued / transcribing / summarizing / done / failed
    error TEXT NOT NULL DEFAULT '',
    speaker_names TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '',
    mindmap TEXT NOT NULL DEFAULT '',  -- Markdown 大纲，markmap 等工具可直接渲染
    asr_provider TEXT NOT NULL DEFAULT '',
    summary_model TEXT NOT NULL DEFAULT '',
    voice_matches TEXT NOT NULL DEFAULT '{}',  -- 声纹匹配结果，见 voice.py
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (name, size)
);
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (name, size)
);
CREATE TABLE IF NOT EXISTS segments (
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    speaker TEXT,
    text TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0,  -- 用户在网页上改过；重新转写会覆盖
    PRIMARY KEY (recording_id, idx)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,              -- full / summary / voice
    status TEXT NOT NULL,            -- queued / running / done / failed
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 列表页每条录音都要查是否有排队/进行中的任务，jobs 只增不删，需要索引。
CREATE INDEX IF NOT EXISTS jobs_recording ON jobs(recording_id, status);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
-- 声纹库：用户给说话人命名后登记的人和样本。声纹是生物特征，只存在本机。
CREATE TABLE IF NOT EXISTS voice_people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_samples (
    id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES voice_people(id) ON DELETE CASCADE,
    recording_id INTEGER REFERENCES recordings(id) ON DELETE SET NULL,
    speaker TEXT,
    embedding TEXT NOT NULL,         -- 192 维单位向量（JSON）
    seconds REAL NOT NULL,
    created_at TEXT NOT NULL
);
-- 每条录音里每位说话人的声纹，转写后提取，用于登记和匹配。
CREATE TABLE IF NOT EXISTS recording_voices (
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    speaker TEXT NOT NULL,
    embedding TEXT NOT NULL,
    seconds REAL NOT NULL,
    PRIMARY KEY (recording_id, speaker)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            # 旧库补列：CREATE TABLE IF NOT EXISTS 不会给已有的表加新列。
            columns = {r[1] for r in self.conn.execute("PRAGMA table_info(recordings)")}
            if "voice_matches" not in columns:
                self.conn.execute("ALTER TABLE recordings ADD COLUMN voice_matches TEXT NOT NULL DEFAULT '{}'")
            if "edited" not in {r[1] for r in self.conn.execute("PRAGMA table_info(segments)")}:
                self.conn.execute("ALTER TABLE segments ADD COLUMN edited INTEGER NOT NULL DEFAULT 0")
            # 服务重启时中断的任务重新排队，不丢任务。
            self.conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")

    def all(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.all(sql, args)
        return rows[0] if rows else None

    def run(self, sql: str, args=()) -> int:
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    def tx(self):
        """with db.tx(): ... 在一个事务内执行多条语句。"""
        return _Tx(self)

    def get_setting(self, key: str, default):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        self.run("INSERT INTO settings(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))


class _Tx:
    def __init__(self, db: DB):
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.db.conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, *_):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.lock.release()
