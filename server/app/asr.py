"""转写：本地 FunASR worker 或远端 OpenAI 兼容接口，输出统一的分段列表。

分段格式：{"start_ms": int, "end_ms": int, "speaker": str | None, "text": str}
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import audio

TIMEOUT = 3 * 3600  # 本地 CPU 转写长录音可能很慢
# 本地 worker 在 Docker 内网，不能走 HTTP(S)_PROXY；远端 API 仍按环境变量使用代理。
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(url: str, data, headers: dict, timeout: int = TIMEOUT, opener=None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"转写服务返回 HTTP {e.code}：{body}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接转写服务 {url}：{e.reason}") from None


def funasr(wav: Path, cfg: dict, hotwords: list[str]) -> list[dict]:
    query = urllib.parse.urlencode({
        "profile": cfg["funasr_profile"],
        "hotwords": " ".join(hotwords),
    })
    with wav.open("rb") as f:
        result = _post(f"{cfg['funasr_url']}/transcribe?{query}", f, {
            "Content-Type": "audio/wav", "Content-Length": str(wav.stat().st_size)}, opener=DIRECT)
    return [{"start_ms": int(s["start_ms"]), "end_ms": int(s["end_ms"]),
             "speaker": None if s.get("speaker") is None else str(s["speaker"]),
             "text": str(s["text"])} for s in result["segments"]]


def _multipart(fields: dict, file_path: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.mp3"\r\n'
                 f'Content-Type: audio/mpeg\r\n\r\n'.encode() + file_path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def openai(wav: Path, cfg: dict, work_dir: Path) -> list[dict]:
    if not cfg["openai_base_url"] or not cfg["openai_model"]:
        raise RuntimeError("远端转写未配置服务地址或模型")
    segments = []
    for offset, part in audio.split_for_upload(wav, work_dir):
        fields = {"model": cfg["openai_model"], "response_format": "verbose_json"}
        if cfg.get("language"):
            fields["language"] = cfg["language"]
        body, ctype = _multipart(fields, part)
        result = _post(cfg["openai_base_url"] + "/audio/transcriptions", body, {
            "Authorization": "Bearer " + cfg["openai_api_key"], "Content-Type": ctype}, timeout=1800)
        items = result.get("segments") or [{"start": 0, "end": audio.duration(part), "text": result.get("text", "")}]
        for s in items:
            segments.append({"start_ms": int((offset + float(s.get("start", 0))) * 1000),
                             "end_ms": int((offset + float(s.get("end", 0))) * 1000),
                             "speaker": None, "text": str(s.get("text", ""))})
    return segments


# ── 字节豆包录音文件识别大模型（提交 + 轮询，音频以 base64 放在请求体）──────
DOUBAO_SUBMIT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
DOUBAO_QUERY = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
DOUBAO_HINTS = {
    "45000001": "鉴权失败：检查 App ID / Access Token，或是否已开通录音文件识别大模型",
    "45000002": "鉴权失败：Access Token 无效或已过期",
    "55000001": "服务侧错误：稍后重试，或到火山控制台查看服务状态",
}
DOUBAO_PENDING = {"20000001", "20000002"}  # 处理中 / 排队中
DOUBAO_PIECE_S = 3 * 3600  # 32kbps MP3 的 3 小时约 43MB，base64 后仍低于 100MB 上限


def _doubao_call(url: str, headers: dict, payload: dict) -> tuple[str, dict]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            status = r.headers.get("X-Api-Status-Code", "")
            body = r.read().decode("utf-8", "replace").strip()
            message = r.headers.get("X-Api-Message", "")
    except urllib.error.HTTPError as e:
        status = e.headers.get("X-Api-Status-Code", "")
        hint = DOUBAO_HINTS.get(status, e.headers.get("X-Api-Message", "") or e.read().decode("utf-8", "replace")[:300])
        raise RuntimeError(f"豆包转写返回 HTTP {e.code}：{hint}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接豆包转写服务：{e.reason}") from None
    if status and status not in {"20000000", "0"} | DOUBAO_PENDING:
        raise RuntimeError(f"豆包转写失败（{status}）：{DOUBAO_HINTS.get(status, message or body[:300])}")
    return status, json.loads(body) if body and body != "{}" else {}


def _doubao_piece(path: Path, cfg: dict) -> list[dict]:
    request_id = str(uuid.uuid4())
    headers = {"Content-Type": "application/json", "X-Api-Resource-Id": cfg["doubao_resource_id"],
               "X-Api-Request-Id": request_id, "X-Api-Sequence": "-1"}
    if cfg["doubao_api_key"]:
        headers["X-Api-Key"] = cfg["doubao_api_key"]
    else:
        headers["X-Api-App-Key"] = cfg["doubao_app_id"]
        headers["X-Api-Access-Key"] = cfg["doubao_access_key"]
    request = {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True,
               "show_utterances": True, "enable_speaker_info": cfg["doubao_speaker"]}
    if cfg["doubao_speaker"]:
        request["ssd_version"] = "300"
        request["language"] = "zh-CN"  # 说话人分离只在中文或不指定语言时可用
    elif cfg.get("language"):
        request["language"] = cfg["language"]
    _doubao_call(DOUBAO_SUBMIT, headers, {
        "user": {"uid": "wenji"},
        "audio": {"data": base64.b64encode(path.read_bytes()).decode("ascii"), "format": "mp3"},
        "request": request,
    })
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        time.sleep(3)
        status, body = _doubao_call(DOUBAO_QUERY, headers, {})
        result = body.get("result") or {}
        utterances = result.get("utterances") or []
        if utterances or result.get("text"):
            if not utterances:
                utterances = [{"start_time": 0, "end_time": 0, "text": result["text"]}]
            return [{"start_ms": int(u.get("start_time") or 0), "end_ms": int(u.get("end_time") or 0),
                     "speaker": str((u.get("additions") or {}).get("speaker") or u.get("speaker") or "") or None,
                     "text": str(u.get("text") or "")} for u in utterances]
    raise RuntimeError("豆包转写超时（1 小时内未返回结果）")


def doubao(wav: Path, cfg: dict, work_dir: Path) -> list[dict]:
    if not (cfg["doubao_api_key"] or (cfg["doubao_app_id"] and cfg["doubao_access_key"])):
        raise RuntimeError("豆包转写未配置 API Key（或 App ID + Access Token）")
    segments = []
    for offset, part in audio.split_for_upload(wav, work_dir, DOUBAO_PIECE_S):
        for s in _doubao_piece(part, cfg):
            segments.append(dict(s, start_ms=s["start_ms"] + int(offset * 1000),
                                 end_ms=s["end_ms"] + int(offset * 1000)))
    return segments


# ── 后处理：清洗、纠错、合并碎片（规则来自听序真机复盘）────────────

def _clean(text: str) -> str:
    # 中文逐字带空格（"王 总 您 好"）时去掉汉字间空格，西文保留。
    return re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", text.strip())


def postprocess(segments: list[dict], corrections: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in segments:
        text = _clean(s["text"])
        # VAD 在噪声上误触发会产出纯标点段，还会被挂上不存在的说话人，直接丢弃。
        if not re.search(r"[一-鿿0-9A-Za-z]", text):
            continue
        for rule in corrections:
            text = text.replace(rule["from"], rule["to"])
        seg = dict(s, text=text)
        # 同一说话人、间隔 ≤2 秒、且任一段不足 10 字的碎片并成一段。
        if out and seg["speaker"] is not None and seg["speaker"] == out[-1]["speaker"]:
            prev = out[-1]
            gap = (seg["start_ms"] - prev["end_ms"]) / 1000
            if 0 <= gap <= 2 and (len(prev["text"]) < 10 or len(text) < 10):
                prev["text"] += text
                prev["end_ms"] = seg["end_ms"]
                continue
        out.append(seg)
    return out
