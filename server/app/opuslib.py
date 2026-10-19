#!/usr/bin/env python3
"""qs668 设备裸 Opus（40B/包）→ Ogg 封装 / 解码 / 前导污染自检 共享库。

抄自 dsh-ai-recorder 的 opus_probe + wrap_raw_opus，本地 ffmpeg 实现。
设备存的是 40B/包 裸 Opus 流（20ms/包，16kHz 时间线）；真机下载件前 ~4KB
可能是非确定性数据，整段包 Ogg 会从头解失败。本库扫出最小可解码偏移。

供 qs668-opus-probe.py / qs668-opus2wav.py 复用。
"""
from __future__ import annotations

import struct
import subprocess
import tempfile
from pathlib import Path

PACKET_SIZE = 40          # 设备裸 Opus：固定 40B/包
PACKET_MS = 20            # 每包 20ms
GRANULE_PER_PACKET = 960  # Ogg granule 走 48kHz 时间线
OGG_SERIAL = 0x51533638   # 'QS68'
PAGE_PACKETS = 50

_CRC_TABLE: list | None = None


def _crc_table() -> list:
    global _CRC_TABLE
    if _CRC_TABLE is None:
        table = []
        for value in range(256):
            reg = value << 24
            for _ in range(8):
                reg = ((reg << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if reg & 0x80000000 else (reg << 1) & 0xFFFFFFFF
            table.append(reg)
        _CRC_TABLE = table
    return _CRC_TABLE


def ogg_crc(data: bytes) -> int:
    table = _crc_table()
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) & 0xFF) ^ byte]
    return crc


def ogg_make_page(payloads, granule: int, serial: int, seq: int, flags: int) -> bytes:
    body = b''.join(payloads)
    laces = []
    for payload in payloads:
        remaining = len(payload)
        while remaining >= 255:
            laces.append(255)
            remaining -= 255
        laces.append(remaining)
    header = bytearray()
    header += b'OggS'
    header += bytes([0, flags])
    header += struct.pack('<Q', granule)
    header += struct.pack('<I', serial)
    header += struct.pack('<I', seq)
    header += struct.pack('<I', 0)
    header += bytes([len(laces)])
    header += bytes(laces)
    page = header + body
    page[22:26] = struct.pack('<I', ogg_crc(bytes(page)))
    return bytes(page)


def wrap_raw_opus(raw: bytes, sample_rate: int = 16000) -> tuple[bytes, int, int]:
    """裸 40B 包流 → Ogg/Opus。返回 (ogg, packet_count, trailing)。"""
    usable = len(raw) - (len(raw) % PACKET_SIZE)
    trailing = len(raw) - usable
    if usable < PACKET_SIZE:
        raise ValueError(f'裸 Opus 不足一整包（{len(raw)}B）')
    packets = [raw[i:i + PACKET_SIZE] for i in range(0, usable, PACKET_SIZE)]
    pages = []
    seq = 0
    head = (b'OpusHead' + bytes([1, 1]) + struct.pack('<H', 312)
            + struct.pack('<I', sample_rate) + struct.pack('<h', 0) + bytes([0]))
    tags = b'OpusTags' + struct.pack('<I', len(b'QS668')) + b'QS668' + struct.pack('<I', 0)
    pages.append(ogg_make_page([head], 0, OGG_SERIAL, seq, 0x02))
    seq += 1
    pages.append(ogg_make_page([tags], 0, OGG_SERIAL, seq, 0x00))
    seq += 1
    granule = 0
    for start in range(0, len(packets), PAGE_PACKETS):
        group = packets[start:start + PAGE_PACKETS]
        granule += GRANULE_PER_PACKET * len(group)
        flags = 0x04 if start + PAGE_PACKETS >= len(packets) else 0x00
        pages.append(ogg_make_page(group, granule, OGG_SERIAL, seq, flags))
        seq += 1
    return b''.join(pages), len(packets), trailing


def _decode_duration_ms(ogg: bytes) -> float:
    """Ogg/Opus → 16k 单声道 wav，返回解码时长（ms）。解不出返回 0。"""
    with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tf:
        tf.write(ogg)
        tmp_ogg = tf.name
    wav = tmp_ogg + '.wav'
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp_ogg,
             "-ar", "16000", "-ac", "1", wav],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not Path(wav).exists():
            return 0.0
        d = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", wav],
            capture_output=True, text=True, timeout=30)
        return float(d.stdout.strip() or 0.0) * 1000.0
    except Exception:
        return 0.0
    finally:
        for p in (tmp_ogg, wav):
            try:
                Path(p).unlink()
            except OSError:
                pass


def _probe_window_ratio(raw: bytes, skip: int, probe_bytes: int) -> float:
    window = raw[skip:skip + probe_bytes]
    usable = len(window) - (len(window) % PACKET_SIZE)
    if usable < PACKET_SIZE * 10:
        return 0.0
    expected_ms = (usable // PACKET_SIZE) * PACKET_MS
    try:
        ogg, _n, _t = wrap_raw_opus(window[:usable], 16000)
    except Exception:
        return 0.0
    got_ms = _decode_duration_ms(ogg)
    return (got_ms / expected_ms) if expected_ms > 0 else 0.0


def probe(raw: bytes, max_skip: int = 65536, min_ratio: float = 0.9) -> dict:
    """扫描出最小可解码偏移。返回 dict(skip_bytes, packets, expected_ms, status)。"""
    total_packets = len(raw) // PACKET_SIZE
    expected_ms = total_packets * PACKET_MS
    probe_bytes = PACKET_SIZE * 1000  # ~20s 窗口

    ratio0 = _probe_window_ratio(raw, 0, probe_bytes)
    if ratio0 >= min_ratio:
        return {"skip_bytes": 0, "packets": total_packets, "expected_ms": expected_ms,
                "decoded_ms": int(ratio0 * min(expected_ms, probe_bytes)),
                "ratio": round(ratio0, 3), "status": "clean"}

    limit = min(max_skip, len(raw) - PACKET_SIZE * 10)
    coarse = PACKET_SIZE * 8
    found = None
    skip = coarse
    while skip <= limit:
        if _probe_window_ratio(raw, skip, probe_bytes) >= min_ratio:
            found = skip
            break
        skip += coarse
    if found is not None:
        start = max(0, found - coarse)
        for s in range(start, found + 1, PACKET_SIZE):
            if _probe_window_ratio(raw, s, probe_bytes) >= min_ratio:
                found = s
                break
    skip_bytes = found or 0
    remaining = (len(raw) - skip_bytes) // PACKET_SIZE
    return {
        "skip_bytes": skip_bytes, "packets": total_packets, "expected_ms": expected_ms,
        "decoded_ms": remaining * PACKET_MS if found is not None else 0,
        "ratio": 1.0 if found is not None else round(ratio0, 3),
        "status": "contaminated" if found is not None else "undecodable",
    }


def opus_to_wav(src: Path, dst_wav: Path, max_skip: int = 65536,
                min_ratio: float = 0.9) -> dict:
    """设备裸 .opus → 前导污染裁切 → Ogg → 16k 单声道 wav。

    返回 {skip_bytes, status, wav_path, duration_ms, expected_ms}。
    dst_wav 落盘裁切后的 16k wav，供 FunASR 直接消费（免二次转码）。
    """
    raw = Path(src).read_bytes()
    info = probe(raw, max_skip, min_ratio)
    if info["status"] == "undecodable":
        raise RuntimeError(f"裸 Opus 无法解码（前 {max_skip}B 内找不到可解码窗口）: {src}")

    skip = info["skip_bytes"]
    raw_ok = raw[skip:] if skip else raw
    # 按包对齐（skip 已是 40B 倍数，保险起见再对齐）
    raw_ok = raw_ok[: len(raw_ok) - (len(raw_ok) % PACKET_SIZE)]
    ogg, _n, _t = wrap_raw_opus(raw_ok, 16000)

    dst_wav.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tf:
        tf.write(ogg)
        tmp_ogg = tf.name
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp_ogg,
             "-ar", "16000", "-ac", "1", str(dst_wav)],
            capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg 解码失败: {r.stderr.strip()[-200:]}")
    finally:
        Path(tmp_ogg).unlink(missing_ok=True)

    d = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(dst_wav)],
        capture_output=True, text=True, timeout=30)
    info["wav_path"] = str(dst_wav)
    info["duration_ms"] = int(float(d.stdout.strip() or 0.0) * 1000)
    return info
