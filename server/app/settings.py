"""模型、热词与纠错设置。密钥只保存在服务端，接口只返回是否已设置。"""
from __future__ import annotations

import copy
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULTS = {
    "general": {
        "auto_process": True,                    # 新录音上传后自动转写；关闭后在网页上手动开始
    },
    "asr": {
        "provider": "funasr",                    # funasr（本地 asr-worker）/ doubao（字节豆包）/ openai（兼容接口）
        "funasr_url": "http://asr-worker:8000",
        "funasr_profile": "paraformer-zh",       # paraformer-zh / sensevoice
        "openai_base_url": "",
        "openai_model": "whisper-1",
        "openai_api_key": "",
        "language": "zh",
        # 豆包录音文件识别大模型：新版控制台用 API Key，旧版用 App ID + Access Token
        "doubao_api_key": "",
        "doubao_app_id": "",
        "doubao_access_key": "",
        "doubao_resource_id": "volc.seedasr.auc",
        "doubao_speaker": True,
    },
    "summary": {
        "enabled": True,
        "base_url": "",                          # 远端服务，或宿主机 Ollama：http://host.docker.internal:11434/v1
        "model": "",
        "api_key": "",
        "prompt": "",                            # 留空使用默认纪要模板
        "mindmap": True,                         # 纪要之后再生成脑图
    },
    "hotwords": [],                              # ["闻记", "会议纪要"]；FunASR 热词不支持权重
    "corrections": [],                           # [{"from": "文记", "to": "闻记"}]
}
SECRETS = {("asr", "openai_api_key"), ("asr", "doubao_api_key"), ("asr", "doubao_access_key"), ("summary", "api_key")}

# 厂商预设：只用于在网页上快速填写地址和模型，模型名以各厂商控制台为准，均可手动修改。
# site 是获取 API Key 的控制台页面。
LLM_PRESETS = [
    {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "site": "https://platform.deepseek.com/api_keys",
     "models": ["deepseek-chat", "deepseek-reasoner"]},
    {"name": "阿里百炼（通义千问）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "site": "https://bailian.console.aliyun.com/", "models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long"]},
    {"name": "字节火山方舟（豆包）", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
     "site": "https://console.volcengine.com/ark", "models": ["doubao-seed-1-6-250615", "doubao-1-5-pro-32k-250115"]},
    {"name": "小米 MiMo", "base_url": "https://api.xiaomimimo.com/v1", "site": "https://platform.xiaomimimo.com/",
     "models": ["mimo-v2-flash"]},
    {"name": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1", "site": "https://platform.moonshot.cn/console/api-keys",
     "models": ["kimi-k2-0905-preview", "moonshot-v1-128k"]},
    {"name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "site": "https://open.bigmodel.cn/",
     "models": ["glm-4.5", "glm-4.5-air"]},
    {"name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1", "site": "https://cloud.siliconflow.cn/account/ak",
     "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen3-235B-A22B-Instruct-2507"]},
    {"name": "本机 Ollama", "base_url": "http://host.docker.internal:11434/v1", "site": "https://ollama.com/library",
     "models": ["qwen3:14b", "qwen3:32b"]},
    {"name": "本机 LM Studio", "base_url": "http://host.docker.internal:1234/v1", "site": "https://lmstudio.ai/", "models": []},
]
# 远端转写里走 OpenAI 兼容 /audio/transcriptions 的厂商；豆包是独立的 provider。
ASR_PRESETS = [
    {"name": "硅基流动 SenseVoice", "base_url": "https://api.siliconflow.cn/v1", "site": "https://cloud.siliconflow.cn/account/ak",
     "models": ["FunAudioLLM/SenseVoiceSmall", "TeleAI/TeleSpeechASR"]},
    {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "site": "https://platform.openai.com/api-keys",
     "models": ["whisper-1", "gpt-4o-transcribe"]},
    {"name": "Groq", "base_url": "https://api.groq.com/openai/v1", "site": "https://console.groq.com/keys",
     "models": ["whisper-large-v3"]},
]
DOUBAO_SITE = "https://console.volcengine.com/speech/app"
# 转写模型名里常见的关键词，用来从厂商的全量模型列表里挑出语音识别模型。
ASR_MODEL_RE = re.compile(r"whisper|transcri|asr|speech|sensevoice|paraformer|audio", re.I)


def load(db) -> dict:
    result = copy.deepcopy(DEFAULTS)
    for key, default in DEFAULTS.items():
        saved = db.get_setting(key, None)
        if isinstance(default, dict) and isinstance(saved, dict):
            result[key].update({k: v for k, v in saved.items() if k in default})
        elif isinstance(default, list) and isinstance(saved, list):
            result[key] = saved
    return result


def public(cfg: dict) -> dict:
    out = copy.deepcopy(cfg)
    for section, key in SECRETS:
        out[section]["has_" + key] = bool(out[section].pop(key, ""))
    return out


def _endpoint(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    p = urlparse(value)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.query:
        raise ValueError(f"服务地址格式不正确：{value}")
    return value


def save(db, payload: dict) -> dict:
    cfg = load(db)
    for section in ("general", "asr", "summary"):
        incoming = payload.get(section) or {}
        if not isinstance(incoming, dict):
            raise ValueError("设置格式不正确")
        for key, default in DEFAULTS[section].items():
            if key not in incoming:
                continue
            value = incoming[key]
            if not isinstance(value, type(default)):
                raise ValueError(f"{section}.{key} 类型不正确")
            if (section, key) in SECRETS and not value.strip():
                continue  # 空密码框表示保留原密钥
            cfg[section][key] = value.strip() if isinstance(value, str) and key != "prompt" else value
        for section_key in [k for k in incoming if k.startswith("clear_")]:
            target = section_key[len("clear_"):]
            if (section, target) in SECRETS and incoming[section_key]:
                cfg[section][target] = ""
    a, s = cfg["asr"], cfg["summary"]
    if a["provider"] not in {"funasr", "doubao", "openai"}:
        raise ValueError("未知转写服务")
    if a["funasr_profile"] not in {"paraformer-zh", "sensevoice"}:
        raise ValueError("未知本地转写模型")
    a["funasr_url"] = _endpoint(a["funasr_url"])
    a["openai_base_url"] = _endpoint(a["openai_base_url"])
    s["base_url"] = _endpoint(s["base_url"])
    if len(s["prompt"]) > 32000:
        raise ValueError("提示词过长（最多 32000 字符）")

    if "hotwords" in payload:
        words = [str(w).strip() for w in payload["hotwords"] if str(w).strip()]
        if any(" " in w for w in words):
            raise ValueError("热词不能包含空格")
        cfg["hotwords"] = list(dict.fromkeys(words))
    if "corrections" in payload:
        cfg["corrections"] = [
            {"from": str(i.get("from", "")).strip(), "to": str(i.get("to", "")).strip()}
            for i in payload["corrections"] if str(i.get("from", "")).strip()
        ]
    for key in DEFAULTS:
        db.set_setting(key, cfg[key])
    return public(cfg)


def _get(url: str, headers: dict | None = None, direct: bool = False) -> dict:
    handlers = [urllib.request.ProxyHandler({})] if direct else []
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:200]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 {url}：{e.reason}") from None


def list_models(cfg: dict, target: str, base_url: str = "", api_key: str = "") -> list[str]:
    """调用 OpenAI 兼容的 GET /models 拉取模型列表。

    base_url / api_key 取网页上尚未保存的填写值；API Key 留空时只在地址与已保存地址相同时
    沿用已保存的密钥，避免把一个厂商的密钥发给另一个厂商。
    """
    if target == "summary":
        saved_url, saved_key = cfg["summary"]["base_url"], cfg["summary"]["api_key"]
    elif target == "asr":
        saved_url, saved_key = cfg["asr"]["openai_base_url"], cfg["asr"]["openai_api_key"]
    else:
        raise RuntimeError("未知对象")
    url = _endpoint(base_url) if base_url.strip() else saved_url
    if not url:
        raise RuntimeError("请先填写服务地址")
    key = api_key.strip() or (saved_key if url == saved_url else "")
    data = _get(url + "/models", {"Authorization": "Bearer " + (key or "none")},
                direct=urlparse(url).hostname == "host.docker.internal")
    items = data.get("data") if isinstance(data.get("data"), list) else data.get("models") or []
    ids = sorted({str(m.get("id") or m.get("name") or "") for m in items if isinstance(m, dict)} - {""})
    if target == "asr":
        speech = [m for m in ids if ASR_MODEL_RE.search(m)]
        ids = speech or ids
    if not ids:
        raise RuntimeError("服务没有返回模型列表，请到厂商控制台查看模型名后手动填写")
    return ids[:500]


def test(cfg: dict, target: str) -> str:
    """用已保存的设置做一次轻量连接测试，返回给用户看的结果说明。"""
    if target == "asr":
        a = cfg["asr"]
        if a["provider"] == "funasr":
            info = _get(a["funasr_url"] + "/healthz", direct=True)
            loaded = "、".join(info.get("loaded") or []) or "尚未加载（首次转写时加载）"
            return f"本地转写服务正常，已加载模型：{loaded}"
        if a["provider"] == "doubao":
            if not (a["doubao_api_key"] or (a["doubao_app_id"] and a["doubao_access_key"])):
                raise RuntimeError("请填写 API Key，或 App ID + Access Token")
            return "凭据已填写。豆包没有免费的连通性测试接口，请转写一条短录音验证"
        if not a["openai_base_url"]:
            raise RuntimeError("请填写服务地址")
        _get(a["openai_base_url"] + "/models", {"Authorization": "Bearer " + a["openai_api_key"]})
        return "服务地址和 API Key 可用"
    if target == "summary":
        from . import summary
        s = cfg["summary"]
        reply = summary.chat(s, "你是连通性测试助手。", "请只回复：可以")
        return f"模型 {s['model']} 可用，回复：{reply[:40]}"
    raise RuntimeError("未知测试对象")
