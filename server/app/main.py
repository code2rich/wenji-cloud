"""闻记服务端：同步比对、分片续传、录音管理与网页。"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audio, config, corrections, library, mcp_server, pipeline, settings, voice
from .db import DB, now

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
STATIC = Path(__file__).parent / "static"
COOKIE = "wj_token"


# 请求体模型须定义在模块级：启用 postponed annotations 时 FastAPI 无法解析函数内的类。
class Login(BaseModel):
    token: str


class Item(BaseModel):
    name: str
    size: int


class CheckBody(BaseModel):
    items: list[Item]


class UploadBody(BaseModel):
    name: str
    size: int
    source: str = ""


class Patch(BaseModel):
    title: str | None = None
    speaker_names: dict[str, str] | None = None


class SegmentEdit(BaseModel):
    text: str | None = None
    speaker: str | None = None


class Rule(BaseModel):
    source: str
    target: str
    scope: str = "all"               # future 只用于以后的转写 / recording 这条录音 / all 全部录音
    recording_id: int | None = None
    hotword: bool = False


class Merge(BaseModel):
    source: str
    target: str


class TestBody(BaseModel):
    target: str


class ModelsBody(BaseModel):
    target: str
    base_url: str = ""
    api_key: str = ""


class Process(BaseModel):
    kind: str = "full"



def create_app(data_dir: Path | None = None, token: str | None = None, start_worker: bool = True) -> FastAPI:
    data_dir = data_dir or config.DATA_DIR
    token = token if token is not None else config.TOKEN
    if len(token) < config.MIN_TOKEN_LEN:
        raise SystemExit(f"WENJI_TOKEN 未设置或少于 {config.MIN_TOKEN_LEN} 个字符")
    db = DB(data_dir / "wenji.db")
    # 早期版本上传的录音没有记录时长，启动时补算（裸 Opus 按文件大小计算，很快）。
    for rec in db.all("SELECT id, audio_path FROM recordings WHERE duration_s IS NULL"):
        try:
            db.run("UPDATE recordings SET duration_s=? WHERE id=?",
                   (audio.estimate_duration(Path(rec["audio_path"])) or None, rec["id"]))
        except Exception:
            pass
    # 30 天没有续传的上传会话视为放弃，清理掉占用的磁盘；卡还在的话下次插卡会重新上传。
    stale = time.time() - config.STALE_UPLOAD_DAYS * 86400
    for up in db.all("SELECT id FROM uploads"):
        part = data_dir / "uploads" / f"{up['id']}.part"
        if not part.exists() or part.stat().st_mtime < stale:
            part.unlink(missing_ok=True)
            db.run("DELETE FROM uploads WHERE id=?", (up["id"],))
    worker = pipeline.Worker(db, data_dir)
    upload_lock = threading.Lock()
    mcp = mcp_server.build(db)
    # 无状态 + JSON 响应：每个请求独立，适合经过内网穿透的短连接。
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True,
                                      json_response=True, host="0.0.0.0")

    @asynccontextmanager
    async def lifespan(_app):
        if start_worker:
            worker.start()
        async with mcp.session_manager.run():
            yield
        worker.stopped = True
        worker.wake.set()

    app = FastAPI(title="Wenji", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.db, app.state.worker = db, worker

    # ── 鉴权：API 用 Bearer 令牌（插件）或 HttpOnly Cookie（网页）──────────
    @app.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        if (path.startswith("/api/") and path != "/api/login") or path.startswith("/mcp"):
            header = request.headers.get("authorization", "")
            given = header[7:] if header.lower().startswith("bearer ") else request.cookies.get(COOKIE, "")
            if not hmac.compare_digest(given.encode(), token.encode()):
                return JSONResponse({"detail": "未登录或令牌错误"}, status_code=401)
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.post("/api/login")
    def login(body: Login, request: Request):
        if not hmac.compare_digest(body.token.encode(), token.encode()):
            raise HTTPException(401, "令牌错误")
        resp = JSONResponse({"ok": True})
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=secure, max_age=180 * 86400)
        return resp

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ── 同步：先比对，再按需分片上传 ─────────────────────────────────


    def part_path(upload_id: str) -> Path:
        return data_dir / "uploads" / f"{upload_id}.part"

    def valid_name(name: str) -> str:
        if not name or name != PurePosixPath(name).name or "\\" in name or name.startswith("."):
            raise HTTPException(400, f"文件名不合法：{name}")
        if Path(name).suffix.lower() not in config.AUDIO_EXTS:
            raise HTTPException(400, f"不支持的音频格式：{name}")
        return name

    @app.post("/api/sync/check")
    def sync_check(body: CheckBody):
        results = []
        for item in body.items:
            rec = db.one("SELECT id FROM recordings WHERE name=? AND size=?", (item.name, item.size))
            if rec:
                results.append({"name": item.name, "size": item.size, "state": "synced", "recording_id": rec["id"]})
                continue
            up = db.one("SELECT id FROM uploads WHERE name=? AND size=?", (item.name, item.size))
            part = part_path(up["id"]) if up else None
            if part and part.exists():
                results.append({"name": item.name, "size": item.size, "state": "partial",
                                "upload_id": up["id"], "offset": part.stat().st_size})
            else:
                results.append({"name": item.name, "size": item.size, "state": "missing"})
        return {"results": results}

    @app.post("/api/uploads")
    def create_upload(body: UploadBody):
        valid_name(body.name)
        if not 0 < body.size <= config.MAX_FILE:
            raise HTTPException(400, "文件大小不合法")
        rec = db.one("SELECT id FROM recordings WHERE name=? AND size=?", (body.name, body.size))
        if rec:
            return {"state": "synced", "recording_id": rec["id"]}
        with upload_lock:
            up = db.one("SELECT id FROM uploads WHERE name=? AND size=?", (body.name, body.size))
            if not up:
                up = {"id": uuid.uuid4().hex}
                db.run("INSERT INTO uploads(id, name, size, source, created_at) VALUES(?,?,?,?,?)",
                       (up["id"], body.name, body.size, body.source[:100], now()))
            part = part_path(up["id"])
            part.parent.mkdir(parents=True, exist_ok=True)
            part.touch(exist_ok=True)
            return {"state": "partial", "upload_id": up["id"], "offset": part.stat().st_size}

    def finalize(up: dict, part: Path) -> int:
        digest = hashlib.sha256()
        with part.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        suffix = Path(up["name"]).suffix.lower()
        audio_dir = data_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        try:
            duration = audio.estimate_duration(part)
        except Exception:
            duration = None
        with db.tx():
            rid = db.run(
                "INSERT INTO recordings(name, size, source, title, recorded_at, audio_path, sha256, duration_s, "
                "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (up["name"], up["size"], up["source"], Path(up["name"]).stem, audio.recorded_at(up["name"]),
                 "", digest.hexdigest(), duration, "pending", now(), now()))
            target = audio_dir / f"{rid}{suffix}"
            db.run("UPDATE recordings SET audio_path=? WHERE id=?", (str(target), rid))
            db.run("DELETE FROM uploads WHERE id=?", (up["id"],))
            part.replace(target)
        if settings.load(db)["general"]["auto_process"]:
            pipeline.enqueue(db, rid)
            worker.wake.set()
        return rid

    def append(upload_id: str, offset: int, data: bytes) -> dict:
        with upload_lock:
            up = db.one("SELECT * FROM uploads WHERE id=?", (upload_id,))
            part = part_path(upload_id)
            if not up or not part.exists():
                raise HTTPException(404, "上传会话不存在，请重新比对")
            current = part.stat().st_size
            if offset != current:
                raise HTTPException(409, {"message": "偏移不一致，请从服务端偏移继续", "offset": current})
            if current + len(data) > up["size"]:
                raise HTTPException(400, "上传内容超过声明的文件大小")
            with part.open("ab") as f:
                f.write(data)
            current += len(data)
            if current < up["size"]:
                return {"offset": current, "complete": False}
            return {"offset": current, "complete": True, "recording_id": finalize(up, part)}

    @app.put("/api/uploads/{upload_id}")
    async def upload_chunk(upload_id: str, request: Request):
        try:
            offset = int(request.headers.get("upload-offset", ""))
            length = int(request.headers.get("content-length", ""))
        except ValueError:
            raise HTTPException(400, "缺少 Upload-Offset 或 Content-Length")
        if not 0 < length <= config.MAX_CHUNK:
            raise HTTPException(413, f"分片须为 1 字节至 {config.MAX_CHUNK} 字节")
        data = await request.body()
        if len(data) != length:
            raise HTTPException(400, "分片不完整")
        return await run_in_threadpool(append, upload_id, offset, data)

    # ── 录音管理 ─────────────────────────────────────────────────
    @app.get("/api/recordings")
    def list_recordings(q: str = ""):
        rows = library.list_recordings(db, q)
        queued = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')")["n"]
        return {"recordings": rows, "queue": queued}

    def get_rec(rid: int) -> dict:
        rec = library.get(db, rid)
        if not rec:
            raise HTTPException(404, "录音不存在")
        return rec

    @app.get("/api/recordings/{rid}")
    def recording(rid: int):
        rec = get_rec(rid)
        rec.pop("audio_path")
        rec["segments"] = library.segments(db, rid)
        return rec

    play_locks: dict[int, threading.Lock] = {}

    @app.get("/api/recordings/{rid}/audio")
    def recording_audio(rid: int):
        rec = get_rec(rid)
        play = data_dir / "work" / str(rid) / "play.m4a"
        if not play.exists():
            # 未转写的录音首次播放时生成播放文件（3 小时的录音约需 30 秒）
            with play_locks.setdefault(rid, threading.Lock()):
                if not play.exists():
                    try:
                        audio.to_playable(Path(rec["audio_path"]), play)
                    except Exception as e:
                        raise HTTPException(422, f"无法生成播放文件：{e}")
        return FileResponse(play, media_type="audio/mp4")

    @app.patch("/api/recordings/{rid}")
    def patch_recording(rid: int, body: Patch):
        get_rec(rid)
        if body.title is not None and body.title.strip():
            db.run("UPDATE recordings SET title=?, updated_at=? WHERE id=?", (body.title.strip()[:200], now(), rid))
        if body.speaker_names is not None:
            names = {k: v.strip()[:50] for k, v in body.speaker_names.items() if v.strip()}
            db.run("UPDATE recordings SET speaker_names=?, updated_at=? WHERE id=?",
                   (json.dumps(names, ensure_ascii=False), now(), rid))
            cfg = settings.load(db)
            if cfg["voice"]["enabled"]:
                voice.enroll(db, rid, names, cfg["voice"])  # 命名即登记声纹，并重新匹配其他录音
        return recording(rid)

    def editable(rid: int) -> dict:
        rec = get_rec(rid)
        if rec["busy"]:
            raise HTTPException(409, "录音正在处理，完成后再修改")
        return rec

    def speakers_of(rid: int) -> set[str]:
        return {r["speaker"] for r in db.all(
            "SELECT DISTINCT speaker FROM segments WHERE recording_id=? AND speaker IS NOT NULL", (rid,))}

    @app.patch("/api/recordings/{rid}/segments/{idx}")
    def edit_segment(rid: int, idx: int, body: SegmentEdit):
        """修改一句转写的文字，或把这句改记到另一位说话人名下。"""
        editable(rid)
        seg = db.one("SELECT speaker, text FROM segments WHERE recording_id=? AND idx=?", (rid, idx))
        if not seg:
            raise HTTPException(404, "这句转写不存在")
        text, speaker = seg["text"], seg["speaker"]
        if body.text is not None:
            text = body.text.strip()
            if not text:
                raise HTTPException(400, "内容不能为空")
            if len(text) > 5000:
                raise HTTPException(400, "一句最多 5000 字")
        if body.speaker is not None and body.speaker != speaker:
            if speaker is None or body.speaker not in speakers_of(rid):
                raise HTTPException(400, "只能改为这条录音里已有的说话人")
            speaker = body.speaker
        if (text, speaker) != (seg["text"], seg["speaker"]):
            with db.tx():
                db.run("UPDATE segments SET text=?, speaker=?, edited=1 WHERE recording_id=? AND idx=?",
                       (text, speaker, rid, idx))
                db.run("UPDATE recordings SET updated_at=? WHERE id=?", (now(), rid))
        # 改了错词时建议加入纠错规则，以后的转写自动改对，已有的也可以一并修正
        return {**recording(rid), "rule_suggestions": corrections.suggestions(db, rid, seg["text"], text)}

    @app.post("/api/corrections")
    def add_correction(body: Rule):
        if body.recording_id is not None:
            get_rec(body.recording_id)
        try:
            changed = corrections.add(db, body.source, body.target, body.scope, body.recording_id, body.hotword)
        except ValueError as e:
            raise HTTPException(400, str(e))
        cfg = settings.load(db)
        return {"changed": changed, "corrections": cfg["corrections"], "hotwords": cfg["hotwords"]}

    @app.post("/api/recordings/{rid}/speakers/merge")
    def merge_speakers(rid: int, body: Merge):
        """把说话人 source 的所有发言并入 target（说话人分离把一个人拆成了几个时用）。"""
        rec = editable(rid)
        spks = speakers_of(rid)
        if body.source == body.target or not {body.source, body.target} <= spks:
            raise HTTPException(400, "请选择这条录音里两位不同的说话人")
        names = rec["speaker_names"]
        if not names.get(body.target) and names.get(body.source):
            names[body.target] = names[body.source]
        names.pop(body.source, None)
        with db.tx():
            db.run("UPDATE segments SET speaker=? WHERE recording_id=? AND speaker=?", (body.target, rid, body.source))
            db.run("UPDATE recordings SET speaker_names=?, updated_at=? WHERE id=?",
                   (json.dumps(names, ensure_ascii=False), now(), rid))
            had_voices = voice.merge_speakers(db, rid, body.source, body.target)
        if had_voices and settings.load(db)["voice"]["enabled"]:
            pipeline.enqueue(db, rid, "voice")  # 用合并后的全部发言重新提取声纹
            worker.wake.set()
        return recording(rid)

    @app.post("/api/recordings/{rid}/process")
    def process(rid: int, body: Process):
        get_rec(rid)
        if body.kind not in {"full", "summary", "voice"}:
            raise HTTPException(400, "kind 须为 full、summary 或 voice")
        try:
            pipeline.enqueue(db, rid, body.kind)
        except ValueError as e:
            raise HTTPException(409, str(e))
        worker.wake.set()
        return {"ok": True}

    @app.delete("/api/recordings/{rid}")
    def delete_recording(rid: int):
        rec = get_rec(rid)
        if db.one("SELECT id FROM jobs WHERE recording_id=? AND status='running'", (rid,)):
            raise HTTPException(409, "任务进行中，暂不能删除")
        db.run("DELETE FROM recordings WHERE id=?", (rid,))
        Path(rec["audio_path"]).unlink(missing_ok=True)
        shutil.rmtree(data_dir / "work" / str(rid), ignore_errors=True)
        return {"ok": True}

    # ── 声纹库 ───────────────────────────────────────────────────
    @app.get("/api/voices")
    def voices():
        return {"people": voice.library(db)}

    @app.delete("/api/voices/{pid}")
    def delete_voice(pid: int):
        db.run("DELETE FROM voice_people WHERE id=?", (pid,))
        voice.rematch_all(db, settings.load(db)["voice"])
        return {"people": voice.library(db)}

    @app.delete("/api/voices")
    def clear_voices():
        voice.clear(db)
        return {"people": []}

    # ── 设置 ─────────────────────────────────────────────────────
    @app.get("/api/settings")
    def get_settings():
        return settings.public(settings.load(db))

    @app.post("/api/settings/test")
    def test_settings(body: TestBody):
        try:
            return {"ok": True, "message": settings.test(settings.load(db), body.target)}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    @app.get("/api/presets")
    def presets():
        return {"llm": settings.LLM_PRESETS, "asr": settings.ASR_PRESETS, "doubao_site": settings.DOUBAO_SITE}

    @app.post("/api/settings/models")
    def list_models(body: ModelsBody):
        try:
            return {"models": settings.list_models(settings.load(db), body.target, body.base_url, body.api_key)}
        except (RuntimeError, ValueError) as e:
            hint = "API Key 未填写或无效。" if "HTTP 401" in str(e) or "HTTP 403" in str(e) else ""
            raise HTTPException(502, f"获取模型列表失败：{hint}{e}")

    @app.put("/api/settings")
    def put_settings(body: dict):
        try:
            return settings.save(db, body)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))

    app.router.routes.extend(mcp_app.routes)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    return app
