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
