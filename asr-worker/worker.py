"""本地 FunASR 转写服务：POST /transcribe，请求体是 16k 单声道 WAV。

只在 Docker 内网中供 server 调用，不对外暴露端口。
"""
from __future__ import annotations

import os
import re
import tempfile
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

NCPU = int(os.environ.get("ASR_THREADS", "8"))
PROFILES = {
    # 模型、VAD、标点、说话人分离（cam++）
    "paraformer-zh": ("paraformer-zh", "fsmn-vad", "ct-punc-c", "cam++"),
    "sensevoice": ("SenseVoiceSmall", "fsmn-vad", None, "cam++"),
}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_models: dict[str, object] = {}
_lock = threading.Lock()  # 模型不是线程安全的，且一次转写已占满 CPU


def _model(profile: str):
    if profile not in _models:
        from funasr import AutoModel

        model, vad, punc, spk = PROFILES[profile]
        kwargs = dict(model=model, vad_model=vad, spk_model=spk, device="cpu",
                      ncpu=NCPU, disable_update=True)
        if punc:
            kwargs["punc_model"] = punc
        _models[profile] = AutoModel(**kwargs)
    return _models[profile]


PUNCT = "，。？！；、,.?!;"
_SENTENCE_RE = re.compile(f"[^{PUNCT}]+[{PUNCT}]*")


def realign(text: str, sentences: list[dict]) -> list[str]:
    """修正 sentence_info 的切句错位。

    FunASR 1.4 开启说话人分离时，sentence_info 的标点会落后一个字（"过去几年我，/们也都"），
    而整段 text 的标点是对的。两者去掉标点后是同一串字，句数也一致，所以用 text 重新切句，
    时间戳和说话人沿用 sentence_info。对不上时保留原样。
    """
    original = [str(s.get("text") or "") for s in sentences]
    pieces = _SENTENCE_RE.findall(text)
    strip = lambda t: re.sub(f"[{PUNCT}\\s]", "", t)
    if len(pieces) != len(original) or strip("".join(pieces)) != strip("".join(original)):
        return original
    return pieces


def _segments(result) -> list[dict]:
    items = result if isinstance(result, list) else [result]
    out = []
    for item in items:
        sentences = item.get("sentence_info")
        if sentences:
            texts = realign(str(item.get("text") or ""), sentences)
            out += [{"start_ms": int(s.get("start", 0)), "end_ms": int(s.get("end", 0)),
                     "speaker": s.get("spk"), "text": t} for s, t in zip(sentences, texts)]
            continue
        ts = item.get("timestamp") or []
        out.append({"start_ms": int(ts[0][0]) if ts else 0, "end_ms": int(ts[-1][1]) if ts else 0,
                    "speaker": None, "text": str(item.get("text") or "")})
    return out


def _transcribe(path: str, profile: str, hotwords: str) -> list[dict]:
    with _lock:
        model = _model(profile)
        kwargs = {"batch_size_s": 300}
        if hotwords and profile == "paraformer-zh":
            kwargs["hotword"] = hotwords
        return _segments(model.generate(path, **kwargs))


@app.get("/healthz")
def healthz():
    return {"ok": True, "loaded": list(_models)}


@app.post("/transcribe")
async def transcribe(request: Request, profile: str = "paraformer-zh", hotwords: str = ""):
    if profile not in PROFILES:
        raise HTTPException(400, f"未知模型方案：{profile}")
    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        async for chunk in request.stream():
            tmp.write(chunk)
        tmp.flush()
        if tmp.tell() == 0:
            raise HTTPException(400, "请求体为空")
        segments = await run_in_threadpool(_transcribe, tmp.name, profile, hotwords)
    return {"segments": segments}
