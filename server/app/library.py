"""录音查询：网页 API 与 MCP 共用。"""
from __future__ import annotations

import json

from . import summary
from .db import DB

LIST_COLS = ("r.id, r.name, r.size, r.title, r.recorded_at, r.duration_s, r.status, r.error, r.source, "
             "r.created_at, r.summary != '' AS has_summary, r.mindmap != '' AS has_mindmap, "
             "EXISTS (SELECT 1 FROM jobs j WHERE j.recording_id=r.id AND j.status IN ('queued','running')) AS busy")


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_recordings(db: DB, q: str = "", since: str = "", limit: int = 500) -> list[dict]:
    """q 匹配标题、文件名、纪要和转写正文；since 是录音时间下限（YYYY-MM-DD）。"""
    where, args, snippet = [], [], "NULL"
    if q.strip():
        like = _like(q.strip())
        snippet = ("(SELECT text FROM segments s WHERE s.recording_id=r.id AND s.text LIKE ? ESCAPE '\\' "
                   "ORDER BY idx LIMIT 1)")
        args.append(like)
        where.append("(r.title LIKE ? ESCAPE '\\' OR r.name LIKE ? ESCAPE '\\' OR r.summary LIKE ? ESCAPE '\\' "
                     "OR EXISTS (SELECT 1 FROM segments s WHERE s.recording_id=r.id AND s.text LIKE ? ESCAPE '\\'))")
        args += [like] * 4
    if since:
        where.append("COALESCE(r.recorded_at, r.created_at) >= ?")
        args.append(since)
    sql = (f"SELECT {LIST_COLS}, {snippet} AS snippet FROM recordings r "
           f"{'WHERE ' + ' AND '.join(where) if where else ''} "
           f"ORDER BY COALESCE(r.recorded_at, r.created_at) DESC LIMIT ?")
    return db.all(sql, (*args, max(1, min(limit, 500))))


def search_segments(db: DB, q: str, limit: int = 50) -> list[dict]:
    rows = db.all(
        "SELECT s.recording_id, r.title, r.recorded_at, r.speaker_names, s.start_ms, s.speaker, s.text "
        "FROM segments s JOIN recordings r ON r.id = s.recording_id "
        "WHERE s.text LIKE ? ESCAPE '\\' ORDER BY COALESCE(r.recorded_at, r.created_at) DESC, s.idx LIMIT ?",
        (_like(q.strip()), max(1, min(limit, 200))))
    for row in rows:
        names = json.loads(row.pop("speaker_names"))
        row["speaker"] = names.get(row["speaker"], row["speaker"])
    return rows


def get(db: DB, rid: int) -> dict | None:
    rec = db.one("SELECT * FROM recordings WHERE id=?", (rid,))
    if rec:
        rec["speaker_names"] = json.loads(rec["speaker_names"])
        rec["job"] = db.one("SELECT kind, status, created_at, updated_at FROM jobs WHERE recording_id=? "
                            "AND status IN ('queued','running') ORDER BY id LIMIT 1", (rid,))
        rec["busy"] = rec["job"] is not None
    return rec


def segments(db: DB, rid: int) -> list[dict]:
    return db.all("SELECT start_ms, end_ms, speaker, text FROM segments WHERE recording_id=? ORDER BY idx", (rid,))


def transcript_text(db: DB, rec: dict) -> str:
    return summary.transcript(segments(db, rec["id"]), rec["speaker_names"], rec["recorded_at"])
