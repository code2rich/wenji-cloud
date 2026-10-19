"""后台任务：转换音频 → 转写 → 后处理 → 纪要。任务存在数据库里，服务重启后继续。"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from pathlib import Path

from . import asr, audio, settings, summary
from .db import DB, now

log = logging.getLogger("wenji.pipeline")


def enqueue(db: DB, recording_id: int, kind: str = "full") -> None:
    with db.tx():
        pending = db.one("SELECT id FROM jobs WHERE recording_id=? AND status IN ('queued','running')",
                         (recording_id,))
        if pending:
            raise ValueError("该录音已有排队或进行中的任务")
        db.run("INSERT INTO jobs(recording_id, kind, status, created_at, updated_at) VALUES(?,?,?,?,?)",
               (recording_id, kind, "queued", now(), now()))
        db.run("UPDATE recordings SET status='queued', error='', updated_at=? WHERE id=?", (now(), recording_id))


def _status(db: DB, rid: int, status: str, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    db.run(f"UPDATE recordings SET status=?, updated_at=?{', ' + sets if sets else ''} WHERE id=?",
           (status, now(), *fields.values(), rid))


def transcribe(db: DB, rec: dict, cfg: dict, work: Path) -> None:
    _status(db, rec["id"], "transcribing")
    wav = work / "audio.wav"
    if not wav.exists():
        audio.to_wav(Path(rec["audio_path"]), wav)
    play = work / "play.m4a"
    if not play.exists():
        audio.to_playable(wav, play)
    a = cfg["asr"]
    if a["provider"] == "funasr":
        raw = asr.funasr(wav, a, cfg["hotwords"])
    elif a["provider"] == "doubao":
        raw = asr.doubao(wav, a, work / "parts")
    else:
        raw = asr.openai(wav, a, work / "parts")
    segments = asr.postprocess(raw, cfg["corrections"])
    if not segments:
        raise RuntimeError("转写结果为空，请确认录音中有人声")
    with db.tx():
        db.run("DELETE FROM segments WHERE recording_id=?", (rec["id"],))
        for i, s in enumerate(segments):
            db.run("INSERT INTO segments(recording_id, idx, start_ms, end_ms, speaker, text) VALUES(?,?,?,?,?,?)",
                   (rec["id"], i, s["start_ms"], s["end_ms"], s["speaker"], s["text"]))
        # 重新转写后说话人编号可能变化，旧的命名不再适用。
        db.run("UPDATE recordings SET duration_s=?, asr_provider=?, speaker_names='{}' WHERE id=?",
               (audio.duration(wav), "funasr:" + a["funasr_profile"] if a["provider"] == "funasr" else a["provider"],
                rec["id"]))


def summarize(db: DB, rec: dict, cfg: dict, requested: bool = False) -> None:
    """requested=True 表示用户单独点了“生成纪要”，此时未配置要明确报错。"""
    s = cfg["summary"]
    if not (s["base_url"] and s["model"]):
        if requested:
            raise RuntimeError("还没有配置纪要模型，请先到“设置 → 纪要与脑图”填写服务地址和模型")
        return  # 自动流程中未配置纪要模型时只做转写，不算失败
    if not s["enabled"] and not requested:
        return
    _status(db, rec["id"], "summarizing")
    rec = db.one("SELECT * FROM recordings WHERE id=?", (rec["id"],))
    segments = db.all("SELECT * FROM segments WHERE recording_id=? ORDER BY idx", (rec["id"],))
    if not segments:
        raise RuntimeError("没有转写内容，无法总结")
    text = summary.transcript(segments, json.loads(rec["speaker_names"]), rec["recorded_at"])
    minutes = round((rec["duration_s"] or 0) / 60)
    meta = f"录音时间={rec['recorded_at'] or '未知'}；时长=约 {minutes} 分钟；文件={rec['name']}"
    result = summary.summarize(s, text, meta)
    db.run("UPDATE recordings SET summary=?, summary_model=? WHERE id=?", (result, s["model"], rec["id"]))
    # 标题还是卡上的文件名时，用纪要生成一个能看出主题的标题；用户改过的标题不动。
    if rec["title"] == Path(rec["name"]).stem:
        try:
            new_title = summary.title(s, result)
        except Exception:  # 标题只是锦上添花，失败不影响纪要
            log.warning("recording %s title generation failed", rec["id"], exc_info=True)
            new_title = ""
        if new_title:
            db.run("UPDATE recordings SET title=? WHERE id=?", (new_title, rec["id"]))
    if s["mindmap"]:
        db.run("UPDATE recordings SET mindmap=? WHERE id=?", (summary.mindmap(s, text, meta), rec["id"]))


def run_job(db: DB, job: dict, data_dir: Path) -> None:
    rec = db.one("SELECT * FROM recordings WHERE id=?", (job["recording_id"],))
    if not rec:
        return
    cfg = settings.load(db)
    work = data_dir / "work" / str(rec["id"])
    work.mkdir(parents=True, exist_ok=True)
    if job["kind"] == "full":
        transcribe(db, rec, cfg, work)
    summarize(db, rec, cfg, requested=job["kind"] == "summary")
    _status(db, rec["id"], "done")
    # 中间文件可随时由原始音频重新生成，完成后清理以节省磁盘。
    shutil.rmtree(work / "parts", ignore_errors=True)
    (work / "audio.wav").unlink(missing_ok=True)


class Worker(threading.Thread):
    """单线程顺序执行：本地模型占满 CPU，并发没有收益。"""

    def __init__(self, db: DB, data_dir: Path):
        super().__init__(daemon=True, name="pipeline")
        self.db, self.data_dir = db, data_dir
        self.wake = threading.Event()
        self.stopped = False

    def run(self) -> None:
        while not self.stopped:
            try:
                busy = self.step()
            except Exception:  # 数据库等意外错误不能让线程退出，否则队列从此停摆
                log.exception("pipeline loop error")
                busy = False
            if not busy:
                self.wake.wait(5)
                self.wake.clear()

    def step(self) -> bool:
        """执行一个排队任务；队列为空时返回 False。"""
        job = self.db.one("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1")
        if not job:
            return False
        self.db.run("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (now(), job["id"]))
        started = time.time()
        try:
            run_job(self.db, job, self.data_dir)
            self.db.run("UPDATE jobs SET status='done', updated_at=? WHERE id=?", (now(), job["id"]))
            log.info("job %s done in %.0fs", job["id"], time.time() - started)
        except Exception as exc:  # 单个任务失败不影响后续任务
            log.exception("job %s failed", job["id"])
            self.db.run("UPDATE jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                        (str(exc), now(), job["id"]))
            _status(self.db, job["recording_id"], "failed", error=str(exc))
        return True
