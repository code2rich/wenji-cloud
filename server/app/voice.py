"""声纹识别：转写后为每位说话人提取声纹，与声纹库比对后自动命名或给出建议。

- 声纹由 asr-worker 的 CAM++ 模型计算（POST /embed），与说话人分离用的是同一个模型；
  豆包转写的录音也可以用，只要 asr-worker 在运行。
- 用户在网页上保存说话人命名时登记到声纹库（enroll），并用新样本重新匹配其他录音。
- 匹配结果存在 recordings.voice_matches：
  {"speakers": {"0": {"person_id", "name", "score", "auto"}}, "error": "", "at": "..."}
"""
from __future__ import annotations

import json
import logging
import math
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import audio
from .db import DB, now

log = logging.getLogger("wenji.voice")

MIN_CLIP_MS = 3000       # 短于 3 秒的片段声纹不稳定
MAX_CLIP_MS = 30000
CLIPS_PER_SPEAKER = 3
SAMPLES_PER_PERSON = 20  # 每人保留最近的样本数
# 一位说话人的声纹总时长不足 10 秒时不登记、不自动命名，只给建议。实测只用一段 8 秒左右的片段登记，
# 之后只有约六成能被认出，且有人会被别人的声纹比下去；两段以上、16 秒左右时九成五以上能认出。
MIN_VOICE_S = 10.0
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # asr-worker 在内网，不走代理


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def _mean(vectors: list[list[float]]) -> list[float]:
    return _unit([sum(values) / len(vectors) for values in zip(*vectors)])


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def pick_spans(segments: list[dict], speaker: str) -> list[tuple[int, int]]:
    """挑这位说话人最长的几段单人讲话：至少 3 秒、不与其他人重叠，每段最多取 30 秒。"""
    others = [s for s in segments if s["speaker"] is not None and s["speaker"] != speaker]
    spans = []
    for s in segments:
        if s["speaker"] != speaker or s["end_ms"] - s["start_ms"] < MIN_CLIP_MS:
            continue
        if any(o["start_ms"] < s["end_ms"] and o["end_ms"] > s["start_ms"] for o in others):
            continue
        spans.append((s["start_ms"], min(s["end_ms"], s["start_ms"] + MAX_CLIP_MS)))
    spans.sort(key=lambda span: span[1] - span[0], reverse=True)
    return spans[:CLIPS_PER_SPEAKER]


def _embed_clip(worker_url: str, clip: Path) -> list[float] | None:
    req = urllib.request.Request(worker_url + "/embed", data=clip.read_bytes(), method="POST",
                                 headers={"Content-Type": "audio/wav"})
    try:
        with DIRECT.open(req, timeout=300) as r:
            return json.loads(r.read())["embedding"]
    except urllib.error.HTTPError as e:
        if e.code == 422:  # 片段静音或过短，换下一段
            return None
        raise RuntimeError(f"声纹服务返回 HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:200]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接声纹服务 {worker_url}：{e.reason}（需要以 local-asr 启动 asr-worker）") from None


def extract(db: DB, rid: int, wav: Path, worker_url: str) -> int:
    """为录音的每位说话人提取声纹并保存，返回提取成功的人数。"""
    segments = db.all("SELECT start_ms, end_ms, speaker, text FROM segments WHERE recording_id=? ORDER BY idx", (rid,))
    speakers = sorted({s["speaker"] for s in segments if s["speaker"] is not None}, key=lambda x: (len(x), x))
    voices = {}
    with tempfile.TemporaryDirectory(prefix="wenji-voice-") as tmp:
        for spk in speakers:
            vectors, seconds = [], 0.0
            for i, (start, end) in enumerate(pick_spans(segments, spk)):
                clip = Path(tmp) / f"{spk}-{i}.wav"
                audio._ffmpeg(["-ss", f"{start / 1000:.3f}", "-t", f"{(end - start) / 1000:.3f}", "-i", str(wav),
                               "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(clip)], timeout=120)
                vector = _embed_clip(worker_url, clip)
                if vector:
                    vectors.append(vector)
                    seconds += (end - start) / 1000
            if vectors:
                voices[spk] = (_mean(vectors), seconds)
    with db.tx():
        db.run("DELETE FROM recording_voices WHERE recording_id=?", (rid,))
        for spk, (vector, seconds) in voices.items():
            db.run("INSERT INTO recording_voices(recording_id, speaker, embedding, seconds) VALUES(?,?,?,?)",
                   (rid, spk, json.dumps([round(v, 6) for v in vector]), round(seconds, 1)))
    return len(voices)


def _people(db: DB) -> list[dict]:
    """声纹库里每个人的平均声纹。"""
    people = {}
    for row in db.all("SELECT p.id, p.name, s.embedding FROM voice_people p JOIN voice_samples s ON s.person_id=p.id"):
        people.setdefault(row["id"], {"id": row["id"], "name": row["name"], "vectors": []})["vectors"].append(
            json.loads(row["embedding"]))
    return [{"id": p["id"], "name": p["name"], "centroid": _mean(p["vectors"])} for p in people.values()]


def _save_matches(db: DB, rid: int, speakers: dict, error: str = "") -> None:
    db.run("UPDATE recordings SET voice_matches=? WHERE id=?",
           (json.dumps({"speakers": speakers, "error": error, "at": now()}, ensure_ascii=False), rid))


def record_error(db: DB, rid: int, error: str) -> None:
    rec = db.one("SELECT voice_matches FROM recordings WHERE id=?", (rid,))
    if rec:
        _save_matches(db, rid, json.loads(rec["voice_matches"]).get("speakers", {}), error)


def match(db: DB, rid: int, cfg: dict, people: list[dict] | None = None) -> dict:
    """把录音的说话人与声纹库比对。相似度 ≥ auto 且领先明显时直接命名（只填还没命名的说话人），
    ≥ suggest 时只给建议。返回 {说话人: 匹配}。"""
    people = _people(db) if people is None else people
    voices = db.all("SELECT speaker, embedding, seconds FROM recording_voices WHERE recording_id=?", (rid,))
    rec = db.one("SELECT speaker_names FROM recordings WHERE id=?", (rid,))
    if not rec:
        return {}
    names = json.loads(rec["speaker_names"])
    found = {}
    for v in voices:
        vector = json.loads(v["embedding"])
        ranked = sorted(((_dot(vector, p["centroid"]), p) for p in people), key=lambda x: x[0], reverse=True)
        if not ranked:
            continue
        score, person = ranked[0]
        gap = score - ranked[1][0] if len(ranked) > 1 else 1.0
        if score >= cfg["suggest"] and gap >= cfg["margin"]:
            found[v["speaker"]] = {"person_id": person["id"], "name": person["name"], "score": round(score, 3),
                                   "auto": score >= cfg["auto"] and v["seconds"] >= MIN_VOICE_S}
    # 同一个人在一条录音里只对应相似度最高的那位说话人，其余不再提示。最高的那位若没有明显领先第二位，
    # 只给建议：声纹库只有一个人时，上面的“领先其他人”无从比较，此人不在场也可能被认到最像他的人身上。
    by_person = {}
    for spk, m in found.items():
        by_person.setdefault(m["person_id"], []).append(spk)
    for spks in by_person.values():
        spks.sort(key=lambda s: found[s]["score"], reverse=True)
        if len(spks) > 1 and found[spks[0]]["score"] - found[spks[1]]["score"] < cfg["margin"]:
            found[spks[0]]["auto"] = False
        for spk in spks[1:]:
            del found[spk]
    for spk, m in found.items():
        if m["auto"] and not names.get(spk) and m["name"] not in names.values():
            names[spk] = m["name"]
    db.run("UPDATE recordings SET speaker_names=? WHERE id=?", (json.dumps(names, ensure_ascii=False), rid))
    _save_matches(db, rid, found)
    return found


def enroll(db: DB, rid: int, names: dict[str, str], cfg: dict) -> None:
    """用户保存命名后，把这条录音里已命名说话人的声纹登记到对应的人，清空命名的则撤销登记；
    声纹不足 MIN_VOICE_S 秒的不登记。然后用更新后的声纹库重新匹配其他录音。"""
    voices = {v["speaker"]: v for v in db.all("SELECT * FROM recording_voices WHERE recording_id=?", (rid,))}
    if not voices:
        return
    with db.tx():
        db.run("DELETE FROM voice_samples WHERE recording_id=?", (rid,))
        for spk, v in voices.items():
            name = names.get(spk, "").strip()
            if not name or v["seconds"] < MIN_VOICE_S:
                continue
            person = db.one("SELECT id FROM voice_people WHERE name=?", (name,))
            pid = person["id"] if person else db.run(
                "INSERT INTO voice_people(name, created_at) VALUES(?,?)", (name, now()))
            db.run("INSERT INTO voice_samples(person_id, recording_id, speaker, embedding, seconds, created_at) "
                   "VALUES(?,?,?,?,?,?)", (pid, rid, spk, v["embedding"], v["seconds"], now()))
            db.run("DELETE FROM voice_samples WHERE person_id=? AND id NOT IN (SELECT id FROM voice_samples "
                   "WHERE person_id=? ORDER BY id DESC LIMIT ?)", (pid, pid, SAMPLES_PER_PERSON))
        db.run("DELETE FROM voice_people WHERE id NOT IN (SELECT DISTINCT person_id FROM voice_samples)")
    rematch_all(db, cfg, skip=rid)


def rematch_all(db: DB, cfg: dict, skip: int | None = None) -> None:
    people = _people(db)
    for row in db.all("SELECT DISTINCT recording_id FROM recording_voices"):
        if row["recording_id"] != skip:
            match(db, row["recording_id"], cfg, people)


def library(db: DB) -> list[dict]:
    return db.all("SELECT p.id, p.name, COUNT(s.id) AS samples, ROUND(SUM(s.seconds)) AS seconds, "
                  "MAX(s.created_at) AS updated_at FROM voice_people p JOIN voice_samples s ON s.person_id=p.id "
                  "GROUP BY p.id ORDER BY p.name")


def clear(db: DB) -> None:
    """一键清空：声纹库、每条录音的声纹和匹配结果都删除。"""
    with db.tx():
        db.run("DELETE FROM voice_people")
        db.run("DELETE FROM recording_voices")
        db.run("UPDATE recordings SET voice_matches='{}'")
