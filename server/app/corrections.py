"""从用户手改的转写里提炼纠错规则（错词=正确词），并可回补已有的转写、纪要和脑图。"""
from __future__ import annotations

import difflib
import re

from . import settings
from .db import DB

MAX_LEN = 12  # 超过这个长度的改动更像改写句子，不适合做成规则
CJK = re.compile(r"[一-鿿]")
MEANINGFUL = re.compile(r"[一-鿿0-9A-Za-z]")


ASCII = re.compile(r"[A-Za-z0-9 .+_-]")  # 英文词、版本号（GPT-4o）、带空格的词组（open ai）


def _regions(old: str, new: str) -> list[tuple[int, int, int, int]]:
    """把只隔 1 个字的改动并成一处（“deep sick”→“DeepSeek”这类）；整句大改的当作改写，不提炼规则。"""
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    ops = matcher.get_opcodes()
    changed = sum(i2 - i1 for tag, i1, i2, _, _ in ops if tag != "equal")
    if changed > 4 and matcher.ratio() < 0.6:
        return []
    regions, gap = [], 0
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            gap = i2 - i1
            continue
        if regions and gap <= 1:
            regions[-1] = (regions[-1][0], i2, regions[-1][2], j2)
        else:
            regions.append((i1, i2, j1, j2))
    return regions


def suggest(old: str, new: str) -> list[dict]:
    """找出被替换的词。单纯删掉（语气词）或只改标点不算。
    英文扩到整个词组（open ai→OpenAI），中文至少扩到 2 个字（“文→闻”扩成“文记→闻记”），避免单字规则误伤。"""
    found = []
    for i1, i2, j1, j2 in _regions(old, new):
        if not MEANINGFUL.search(old[i1:i2]) or not MEANINGFUL.search(new[j1:j2]):
            continue  # 单纯增删（删语气词、补字）或字换成标点，不是识别错误
        if (ASCII.match(old[i1:i2] or " ") and old[i1:i2].strip()) or (ASCII.match(new[j1:j2] or " ") and new[j1:j2].strip()):
            while i1 > 0 and ASCII.match(old[i1 - 1]):
                i1 -= 1
            while j1 > 0 and ASCII.match(new[j1 - 1]):
                j1 -= 1
            while i2 < len(old) and ASCII.match(old[i2]):
                i2 += 1
            while j2 < len(new) and ASCII.match(new[j2]):
                j2 += 1
        while min(i2 - i1, j2 - j1) < 2:
            if i2 < len(old) and j2 < len(new) and old[i2] == new[j2] and CJK.match(old[i2]):
                i2, j2 = i2 + 1, j2 + 1
            elif i1 > 0 and j1 > 0 and old[i1 - 1] == new[j1 - 1] and CJK.match(old[i1 - 1]):
                i1, j1 = i1 - 1, j1 - 1
            else:
                break
        src, dst = old[i1:i2].strip(), new[j1:j2].strip()
        if not src or not dst or src == dst or len(src) > MAX_LEN or len(dst) > MAX_LEN:
            continue
        if "".join(MEANINGFUL.findall(src)) == "".join(MEANINGFUL.findall(dst)):
            continue  # 只改了标点或空格
        if len(src) < 2:
            continue  # 单个字的规则太容易误伤
        if not any(r["from"] == src for r in found):
            found.append({"from": src, "to": dst})
    return found


def count(db: DB, src: str, rid: int) -> tuple[int, int]:
    """含错词的句子数：这条录音里 / 全部录音里。用 instr 区分大小写，与 replace 一致（LIKE 不分大小写）。"""
    row = db.one("SELECT COUNT(*) AS total, COALESCE(SUM(recording_id=?), 0) AS here FROM segments "
                 "WHERE instr(text, ?) > 0", (rid, src))
    return row["here"], row["total"]


def suggestions(db: DB, rid: int, old: str, new: str) -> list[dict]:
    known = {(r["from"], r["to"]) for r in settings.load(db)["corrections"]}
    out = []
    for s in suggest(old, new):
        if (s["from"], s["to"]) in known:
            continue
        here, total = count(db, s["from"], rid)
        out.append({**s, "here": here, "total": total})
    return out[:3]


def add(db: DB, src: str, dst: str, scope: str, rid: int | None, hotword: bool) -> int:
    """保存规则（同一个错词只留最新的改法），可选加为热词，按 scope 回补已有内容。返回改动的句子数。"""
    src, dst = src.strip(), dst.strip()
    if not src or not dst or src == dst:
        raise ValueError("错词和正确词都要填写，且不能相同")
    if len(src) > 50 or len(dst) > 50:
        raise ValueError("规则太长（最多 50 字）")
    if scope not in {"future", "recording", "all"} or (scope == "recording" and rid is None):
        raise ValueError("范围须为 future、recording 或 all")
    cfg = settings.load(db)
    rules = [r for r in cfg["corrections"] if r["from"] != src] + [{"from": src, "to": dst}]
    payload = {"corrections": rules}
    if hotword and " " not in dst and dst not in cfg["hotwords"]:
        payload["hotwords"] = cfg["hotwords"] + [dst]
    settings.save(db, payload)
    if scope == "future":
        return 0
    where, args = "instr(text, ?) > 0", [src]
    if scope == "recording":
        where, args = where + " AND recording_id=?", args + [rid]
    with db.tx():
        changed = db.one(f"SELECT COUNT(*) AS n FROM segments WHERE {where}", args)["n"]
        db.run(f"UPDATE segments SET text=replace(text, ?, ?) WHERE {where}", (src, dst, *args))
        # 纪要和脑图里的同一个错词一并改掉，搜索结果才一致
        rec_where = "id=?" if scope == "recording" else "1"
        for col in ("summary", "mindmap"):
            db.run(f"UPDATE recordings SET {col}=replace({col}, ?, ?) WHERE {rec_where} AND instr({col}, ?) > 0",
                   (src, dst, *([rid] if scope == "recording" else []), src))
    return changed
