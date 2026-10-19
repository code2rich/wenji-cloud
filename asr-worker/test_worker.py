from worker import _segments


def test_realign_fixes_shifted_sentence_punctuation():
    # 真实录音中 FunASR 返回的形态：sentence_info 的标点落后一个字，整段 text 正确。
    raw = {"text": "那过去几年，我们也都收到反馈意见。希望主办方组织比赛。",
           "sentence_info": [
               {"start": 0, "end": 900, "spk": 0, "text": "那过去几年我，"},
               {"start": 900, "end": 3000, "spk": 0, "text": "们也都收到反馈意见希。"},
               {"start": 3000, "end": 5000, "spk": 1, "text": "望主办方组织比赛。"}]}
    segs = _segments(raw)
    assert [s["text"] for s in segs] == ["那过去几年，", "我们也都收到反馈意见。", "希望主办方组织比赛。"]
    assert [s["speaker"] for s in segs] == [0, 0, 1] and segs[1]["start_ms"] == 900


def test_realign_keeps_original_when_counts_differ():
    raw = {"text": "你好。测试，测试。",
           "sentence_info": [{"start": 0, "end": 1, "spk": 0, "text": "你好测试测试。"}]}
    assert _segments(raw)[0]["text"] == "你好测试测试。"


def _write_wav(path, seconds, amplitude):
    import math
    import struct
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(amplitude * math.sin(i / 8)))
                               for i in range(int(16000 * seconds))))


def test_check_clip_rejects_short_and_silent(tmp_path):
    import pytest
    from worker import check_clip
    ok, short, silent = tmp_path / "ok.wav", tmp_path / "short.wav", tmp_path / "silent.wav"
    _write_wav(ok, 3.5, 3000)
    _write_wav(short, 1.5, 3000)
    _write_wav(silent, 4, 5)
    check_clip(str(ok))
    with pytest.raises(ValueError, match="不足"):
        check_clip(str(short))
    with pytest.raises(ValueError, match="静音"):
        check_clip(str(silent))
