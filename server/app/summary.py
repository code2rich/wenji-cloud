"""纪要生成：OpenAI 兼容 /chat/completions，远端服务或宿主机 Ollama / LM Studio 均可。"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta

CALL_TIMEOUT = 600  # 本地大模型处理长转写可能较慢
MAX_TOKENS = 4000
MARKER = "### 📑 智能总结"

# 默认模板沿用听序（Get笔记「智能总结」格式）。
PROMPT_SYS = """你是专业会议纪要助手。基于给定的「语音转写稿」（每行形如 [HH:MM:SS] 说话人N：原文），
产出一份「智能总结」，严格使用下面的 Markdown 模板与层级，不增删章节，模板外不输出多余内容：

### 📑 智能总结

#### 录音信息
- **录音时间**：<从时间戳推 start ~ end>
- **时长**：约 <X> 分钟
- **参与人数**：约 <N> 人（按出现过的说话人数）
- **内容类型**：<会议 / 访谈 / 通话 / 汇报，自行判断>

#### 录音总结
<3-5 句总括：什么场景、核心结论、后续方向。>

**<主题块一>**
* **<小标题>**：<要点>
* **<小标题>**：<要点>

**<主题块二>**
* **<小标题>**：<要点>
（按内容拆 2-5 个「**主题块**」，每块下 2-4 个「* **小标题**：说明」要点；信息不足就少写，不硬凑）

### 📅 章节概要
[HH:MM:SS] **<章节标题>**
<本章 1-2 句概括>
（按时间戳切 2-5 章；转写很短就 1-2 章）

### ✨ 金句精选
- “<原话>”（<类型：执行策略 / 战略洞见 / 思考启发>）
（挑 3-6 句有信息量的原话，保留原话；不足 3 句则按实写）

### 📋 待办事项
- <负责人/相关方>：<事项>（<时间，若原话有>）
（从内容提取；没有则写「本段未明确待办」）

硬性要求：
1. 转写稿存在同音字/专有名词错误，必须结合上下文纠正：公司名、集团名、云厂商名、行业术语要纠对
   对不确定的专有名词保留原文，不自行猜测真实名称。
2. 除多角色需区分外，正文不要保留「说话人N」字样。
3. 不虚构原文没有的信息；拿不准就基于现有文本归纳，不要编造。
4. 只输出模板内容本身，不要解释、不要复述本 prompt。
5. 【绝对禁止】输出任何思考/推理/计划性独白，例如「We need…」「我们需要回答…」「需要判断…」「让我…」「Key facts…」「Note…」。
   你的最终回复从 `### 📑 智能总结` 开始、到此结束；在此之前和章节之间不得有任何其他文字。"""


def transcript(segments: list[dict], speaker_names: dict, recorded_at: str | None) -> str:
    base = datetime.fromisoformat(recorded_at) if recorded_at else None
    lines = []
    for s in segments:
        sec = s["start_ms"] / 1000
        if base:
            stamp = (base + timedelta(seconds=sec)).strftime("%H:%M:%S")
        else:
            stamp = "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)
        spk = s["speaker"]
        who = speaker_names.get(spk) or (f"说话人{spk}" if spk is not None else "")
        lines.append(f"[{stamp}] {who}：{s['text']}" if who else f"[{stamp}] {s['text']}")
    return "\n".join(lines)


MINDMAP_SYS = """你是会议内容结构化助手。基于给定的「语音转写稿」，输出一份脑图大纲，只用 Markdown：
- 第一行是 `# <录音主题>`，作为脑图根节点；
- 用 `##` 表示 3-7 个一级分支（议题 / 模块），其下用 `-` 无序列表表示子节点，可缩进两个空格继续分层，最多 4 层；
- 每个节点是不超过 20 字的短语，不写整句、不写解释；
- 只包含原文出现过的信息，不虚构；人名、数字、时间、待办责任人要保留；
- 只输出大纲本身，不要代码块包裹，不要任何前后说明。"""


def chat(cfg: dict, system: str, user: str) -> str:
    if not cfg["base_url"] or not cfg["model"]:
        raise RuntimeError("总结服务未配置服务地址或模型")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    max_tokens = MAX_TOKENS
    for _ in range(2):  # 输出被截断（finish_reason=length）时加倍 max_tokens 重试一次
        req = urllib.request.Request(
            cfg["base_url"] + "/chat/completions",
            data=json.dumps({"model": cfg["model"], "messages": messages,
                             "max_tokens": max_tokens}).encode(),
            headers={"Authorization": "Bearer " + (cfg["api_key"] or "none"),
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"总结服务返回 HTTP {e.code}：{e.read().decode('utf-8', 'replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接总结服务 {cfg['base_url']}：{e.reason}") from None
        choice = data["choices"][0]
        content = re.sub(r"<think>.*?</think>", "", choice.get("message", {}).get("content") or "", flags=re.S).strip()
        if choice.get("finish_reason") != "length" and content:
            return content
        max_tokens *= 2
    raise RuntimeError("模型输出被截断或为空，请换用上下文更长的模型")


def summarize(cfg: dict, text: str, meta: str) -> str:
    custom = bool(cfg["prompt"].strip())
    content = chat(cfg, cfg["prompt"].strip() or PROMPT_SYS, f"录音元信息：{meta}\n\n转写稿如下：\n{text}")
    if not custom and not content.startswith("###"):
        idx = content.find(MARKER)
        if idx > 0:
            content = content[idx:].strip()  # 剥掉模型在模板前输出的独白
    return content


def mindmap(cfg: dict, text: str, meta: str) -> str:
    content = chat(cfg, MINDMAP_SYS, f"录音元信息：{meta}\n\n转写稿如下：\n{text}")
    content = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", content.strip())
    root = re.search(r"^#\s", content, re.M)  # 去掉根节点之前的说明文字
    return content[root.start():].strip() if root else content


TITLE_SYS = """根据给定的录音纪要，为这段录音起一个标题：
- 不超过 16 个汉字，概括主题或场景，例如：三季度产品复盘会、与供应商的价格沟通、客户访谈：华东区渠道；
- 不写日期、时长，不加引号、书名号或句末标点；
- 只输出标题本身。"""


def title(cfg: dict, summary_text: str) -> str:
    """用纪要（比转写短得多）生成标题；结果不合格时返回空串，由调用方保留原标题。"""
    reply = chat(cfg, TITLE_SYS, summary_text[:6000])
    line = next((l for l in reply.splitlines() if l.strip()), "")
    line = re.sub(r"^(标题[:：]\s*)|[#*`]", "", line)
    # 引号、书名号和句末标点可能交错出现（如 “标题”。），反复剥到稳定为止
    while (trimmed := line.strip().strip("\"'“”‘’《》「」【】").rstrip("。.!！")) != line:
        line = trimmed
    return line[:40]
