"""音频预处理：统一转成 16kHz 单声道 WAV，远端转写前切成小段压缩。"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

from . import opuslib

NAME_TIME_RE = re.compile(r"(\d{8})[-_](\d{6})")


def recorded_at(name: str) -> str | None:
    """卡上文件名形如 note20261019-091446.opus，按文件名推算录音开始时间。"""
    m = NAME_TIME_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S").isoformat(sep=" ")
    except ValueError:
        return None


def _ffmpeg(args: list[str], timeout: int = 1800) -> None:
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg 失败：" + r.stderr.strip()[-300:])


def duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def is_raw_opus(path: Path) -> bool:
    if path.suffix.lower() != ".opus":
        return False
    with path.open("rb") as f:
        return f.read(4) != b"OggS"


def estimate_duration(path: Path) -> float:
    """上传完成时估算时长：卡上裸 Opus 固定 40 字节 / 20 毫秒，其余格式用 ffprobe。"""
    if is_raw_opus(path):
        return path.stat().st_size / (opuslib.PACKET_SIZE / opuslib.PACKET_MS * 1000)
    return duration(path)


def to_wav(src: Path, dst: Path) -> Path:
    """任意音频 → 16k 单声道 WAV。卡上的裸 Opus（无 Ogg 封装）走 opuslib 处理前导污染。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if is_raw_opus(src):
        opuslib.opus_to_wav(src, dst)
        return dst
    _ffmpeg(["-i", str(src), "-ar", "16000", "-ac", "1", str(dst)])
    return dst


def to_playable(src: Path, dst: Path) -> Path:
    """生成网页播放用的 AAC（m4a），各浏览器都能播放，3 小时约 30MB。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.m4a")
    if is_raw_opus(src):
        wav = dst.with_suffix(".tmp.wav")
        try:
            to_wav(src, wav)
            _ffmpeg(["-i", str(wav), "-ac", "1", "-c:a", "aac", "-b:a", "24k", "-movflags", "+faststart", str(tmp)])
        finally:
            wav.unlink(missing_ok=True)
    else:
        _ffmpeg(["-i", str(src), "-ac", "1", "-c:a", "aac", "-b:a", "24k", "-movflags", "+faststart", str(tmp)])
    tmp.replace(dst)  # 生成完成后再替换，避免并发请求读到半个文件
    return dst


def split_for_upload(wav: Path, out_dir: Path, piece_s: int = 600) -> list[tuple[float, Path]]:
    """切成 piece_s 秒、32kbps 单声道 MP3 小段（10 分钟约 2.4MB），返回 [(起始秒, 路径)]。

    远端转写接口普遍限制单文件 25MB，长录音必须切段。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("part-*.mp3"):
        old.unlink()
    _ffmpeg(["-i", str(wav), "-f", "segment", "-segment_time", str(piece_s),
             "-ac", "1", "-ar", "16000", "-b:a", "32k", str(out_dir / "part-%04d.mp3")])
    result, offset = [], 0.0
    for part in sorted(out_dir.glob("part-*.mp3")):
        result.append((offset, part))
        offset += duration(part) or piece_s  # 切点落在帧边界，用实际时长累加偏移
    return result
