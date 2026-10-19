import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import asr, pipeline, summary
from app.main import create_app

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, TOKEN, start_worker=False)
    with TestClient(app) as c:
        c.data_dir = tmp_path
        c.db = app.state.db
        yield c


def upload(client, name, data, chunk=4):
    r = client.post("/api/uploads", json={"name": name, "size": len(data), "source": "TEST"}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    offset = body["offset"]
    while offset < len(data):
        part = data[offset:offset + chunk]
        r = client.put(f"/api/uploads/{body['upload_id']}", content=part,
                       headers={**AUTH, "Upload-Offset": str(offset)})
        assert r.status_code == 200, r.text
        offset = r.json()["offset"]
    return r.json()


def test_rejects_short_token(tmp_path):
    with pytest.raises(SystemExit):
        create_app(tmp_path, "short", start_worker=False)


def test_api_requires_token(client):
    assert client.get("/api/recordings").status_code == 401
    assert client.get("/api/recordings", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/mcp", json={}).status_code == 401
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200  # 页面本身不含数据


def test_login_sets_cookie(client):
    assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
    r = client.post("/api/login", json={"token": TOKEN})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert client.get("/api/recordings").status_code == 200


def test_sync_check_and_resumable_upload(client):
    data = b"0123456789abcdef"
    items = [{"name": "note20261019-091446.opus", "size": len(data)}]
    r = client.post("/api/sync/check", json={"items": items}, headers=AUTH)
    assert r.json()["results"][0]["state"] == "missing"

    created = client.post("/api/uploads", json={**items[0], "source": "NO NAME"}, headers=AUTH).json()
    uid = created["upload_id"]
    r = client.put(f"/api/uploads/{uid}", content=data[:6], headers={**AUTH, "Upload-Offset": "0"})
    assert r.json() == {"offset": 6, "complete": False}

    # 中断后再次比对：返回已收到的偏移，从这里续传
    result = client.post("/api/sync/check", json={"items": items}, headers=AUTH).json()["results"][0]
    assert result["state"] == "partial" and result["offset"] == 6 and result["upload_id"] == uid

    # 偏移不一致被拒绝，并告知正确偏移
    r = client.put(f"/api/uploads/{uid}", content=data[6:], headers={**AUTH, "Upload-Offset": "0"})
    assert r.status_code == 409 and r.json()["detail"]["offset"] == 6

    r = client.put(f"/api/uploads/{uid}", content=data[6:], headers={**AUTH, "Upload-Offset": "6"})
    done = r.json()
    assert done["complete"] and done["recording_id"]

    result = client.post("/api/sync/check", json={"items": items}, headers=AUTH).json()["results"][0]
    assert result["state"] == "synced"
    again = client.post("/api/uploads", json=items[0], headers=AUTH).json()
    assert again == {"state": "synced", "recording_id": done["recording_id"]}

    rec = client.get(f"/api/recordings/{done['recording_id']}", headers=AUTH).json()
    assert rec["recorded_at"] == "2026-10-19 09:14:46"
    assert rec["status"] == "queued" and rec["source"] == "NO NAME"
    assert (client.data_dir / "audio" / f"{done['recording_id']}.opus").read_bytes() == data


def test_same_name_different_size_is_new_recording(client):
    first = upload(client, "a.wav", b"aaaa")
    second = upload(client, "a.wav", b"aaaaaaaa")
    assert first["recording_id"] != second["recording_id"]


def test_rejects_bad_uploads(client):
    for name in ["../x.wav", "dir/x.wav", ".hidden.wav", "x.exe"]:
        r = client.post("/api/uploads", json={"name": name, "size": 4}, headers=AUTH)
        assert r.status_code == 400, name
    uid = client.post("/api/uploads", json={"name": "b.wav", "size": 4}, headers=AUTH).json()["upload_id"]
    r = client.put(f"/api/uploads/{uid}", content=b"12345", headers={**AUTH, "Upload-Offset": "0"})
    assert r.status_code == 400


def test_settings_hide_secrets(client):
    r = client.put("/api/settings", headers=AUTH, json={
        "summary": {"base_url": "https://llm.example.com/v1/", "model": "m", "api_key": "sk-secret"},
        "hotwords": ["闻记", " ", "闻记"], "corrections": [{"from": "文记", "to": "闻记"}]})
    assert r.status_code == 200, r.text
    cfg = client.get("/api/settings", headers=AUTH).json()
    assert "sk-secret" not in json.dumps(cfg)
    assert cfg["summary"]["has_api_key"] and cfg["summary"]["base_url"] == "https://llm.example.com/v1"
    assert cfg["hotwords"] == ["闻记"]
    # 空密钥表示保留
    client.put("/api/settings", headers=AUTH, json={"summary": {"api_key": ""}})
    assert client.get("/api/settings", headers=AUTH).json()["summary"]["has_api_key"]
    assert client.put("/api/settings", headers=AUTH, json={"asr": {"provider": "x"}}).status_code == 400


def test_postprocess_cleans_merges_and_corrects():
    segs = [
        {"start_ms": 0, "end_ms": 1000, "speaker": "0", "text": "文 记 你好"},
        {"start_ms": 1500, "end_ms": 2000, "speaker": "0", "text": "在吗"},
        {"start_ms": 2100, "end_ms": 2200, "speaker": "1", "text": "，。"},
        {"start_ms": 3000, "end_ms": 4000, "speaker": "1", "text": "我在这里开会讨论项目进度"},
    ]
    out = asr.postprocess(segs, [{"from": "文记", "to": "闻记"}])
    assert [s["text"] for s in out] == ["闻记你好在吗", "我在这里开会讨论项目进度"]
    assert out[0]["end_ms"] == 2000


def run_queue(client):
    """像后台 Worker 一样处理完队列中的任务。"""
    worker = pipeline.Worker(client.db, client.data_dir)
    while worker.step():
        pass


def make_wav(path):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-ar", "16000", "-ac", "1", str(path)], check=True)
    return path.read_bytes()


def test_pipeline_full_run(client, tmp_path, monkeypatch):
    data = make_wav(tmp_path / "src.wav")
    rid = upload(client, "note20261019-091446.wav", data, chunk=len(data))["recording_id"]
    client.put("/api/settings", headers=AUTH, json={
        "summary": {"base_url": "http://llm.local/v1", "model": "qwen"}, "hotwords": ["闻记"]})

    seen = {}

    def fake_funasr(wav, cfg, hotwords):
        seen["hotwords"] = hotwords
        assert wav.exists()
        return [{"start_ms": 0, "end_ms": 1500, "speaker": "0", "text": "今天讨论闻记上线计划"},
                {"start_ms": 1600, "end_ms": 1900, "speaker": "1", "text": "好的我来负责测试工作安排"}]

    def fake_chat(cfg, system, user):
        if "起一个标题" in system:
            assert user.startswith("### 📑 智能总结")  # 用纪要而不是整篇转写生成标题
            return "标题：“闻记上线计划讨论”。\n"
        assert "[09:14:46] 说话人0：今天讨论闻记上线计划" in user
        return "# 闻记上线\n## 分工\n- 测试" if "脑图" in system else "我先想一下\n### 📑 智能总结\n内容"

    monkeypatch.setattr(asr, "funasr", fake_funasr)
    monkeypatch.setattr(summary, "chat", fake_chat)
    run_queue(client)

    rec = client.get(f"/api/recordings/{rid}", headers=AUTH).json()
    assert rec["status"] == "done"
    assert seen["hotwords"] == ["闻记"]
    assert rec["summary"] == "### 📑 智能总结\n内容"  # 模板前的独白被去掉
    assert rec["mindmap"].startswith("# 闻记上线")
    assert rec["title"] == "闻记上线计划讨论"  # 默认文件名标题被替换，引号和标点已去掉
    assert len(rec["segments"]) == 2 and rec["duration_s"] > 1
    audio = client.get(f"/api/recordings/{rid}/audio", headers=AUTH)
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/mp4"
    assert not (client.data_dir / "work" / str(rid) / "audio.wav").exists()

    # 命名说话人后，转写和搜索都使用新名字
    client.patch(f"/api/recordings/{rid}", headers=AUTH, json={"speaker_names": {"1": "小王"}})
    hits = client.get("/api/recordings?q=测试工作", headers=AUTH).json()["recordings"]
    assert hits[0]["id"] == rid and "测试工作" in hits[0]["snippet"]

    r = client.post(f"/api/recordings/{rid}/process", headers=AUTH, json={"kind": "summary"})
    assert r.status_code == 200
    assert client.post(f"/api/recordings/{rid}/process", headers=AUTH, json={"kind": "full"}).status_code == 409

    # 用户改过的标题，重新生成纪要时不会被覆盖
    client.patch(f"/api/recordings/{rid}", headers=AUTH, json={"title": "我的标题"})
    run_queue(client)
    assert client.get(f"/api/recordings/{rid}", headers=AUTH).json()["title"] == "我的标题"


def test_worker_records_failure_and_continues(client, monkeypatch):
    rid = upload(client, "bad.wav", b"not audio")["recording_id"]
    monkeypatch.setattr(pipeline, "run_job", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    run_queue(client)
    rec = client.get(f"/api/recordings/{rid}", headers=AUTH).json()
    assert rec["status"] == "failed" and rec["error"] == "boom"
    assert client.db.one("SELECT status FROM jobs")["status"] == "failed"


def mcp_call(client, method, params=None, rid=1):
    r = client.post("/mcp", headers={**AUTH, "Accept": "application/json, text/event-stream",
                                     "Content-Type": "application/json"},
                    json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
    assert r.status_code == 200, r.text
    return r.json()


def test_mcp_tools(client, tmp_path, monkeypatch):
    data = make_wav(tmp_path / "src.wav")
    rid = upload(client, "note20261019-091446.wav", data, chunk=len(data))["recording_id"]
    client.put("/api/settings", headers=AUTH, json={"summary": {"base_url": "http://llm.local/v1", "model": "m"}})
    monkeypatch.setattr(asr, "funasr", lambda *a: [
        {"start_ms": 0, "end_ms": 1000, "speaker": "0", "text": "预算下周五前提交"}])
    monkeypatch.setattr(summary, "chat", lambda cfg, system, user:
                        "# 预算\n## 待办\n- 周五提交" if "脑图" in system else "### 📑 智能总结\n预算")
    run_queue(client)
    client.patch(f"/api/recordings/{rid}", headers=AUTH, json={"speaker_names": {"0": "张三"}})

    init = mcp_call(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "test", "version": "1"}})
    assert init["result"]["serverInfo"]["name"] == "wenji"
    tools = {t["name"] for t in mcp_call(client, "tools/list")["result"]["tools"]}
    assert tools == {"list_recordings", "search_transcripts", "get_recording", "get_transcript",
                     "get_summary", "get_mindmap"}

    def call(name, **args):
        result = mcp_call(client, "tools/call", {"name": name, "arguments": args})["result"]
        return result, result["content"][0]["text"]

    result, _ = call("list_recordings", query="预算")
    assert result["structuredContent"]["result"][0]["id"] == rid
    _, text = call("get_transcript", recording_id=rid)
    assert text == "[09:14:46] 张三：预算下周五前提交"
    _, text = call("get_mindmap", recording_id=rid)
    assert text.startswith("# 预算")
    _, text = call("get_summary", recording_id=rid)
    assert "预算" in text
    result, _ = call("search_transcripts", query="周五")
    assert result["structuredContent"]["result"][0]["speaker"] == "张三"
    result, text = call("get_summary", recording_id=999)
    assert result["isError"] and "不存在" in text


def test_presets_and_doubao_secrets(client):
    presets = client.get("/api/presets", headers=AUTH).json()
    names = {p["name"] for p in presets["llm"]}
    assert {"DeepSeek", "阿里百炼（通义千问）", "字节火山方舟（豆包）", "小米 MiMo"} <= names
    r = client.put("/api/settings", headers=AUTH, json={"asr": {"provider": "doubao", "doubao_api_key": "ak-secret"}})
    assert r.status_code == 200
    cfg = client.get("/api/settings", headers=AUTH).json()
    assert cfg["asr"]["has_doubao_api_key"] and "ak-secret" not in json.dumps(cfg)


def test_doubao_submit_and_poll(tmp_path, monkeypatch):
    wav = tmp_path / "a.wav"
    make_wav(wav)
    calls = []

    def fake_call(url, headers, payload):
        calls.append((url, headers, payload))
        if url == asr.DOUBAO_SUBMIT:
            return "20000000", {}
        if len(calls) == 2:
            return "20000001", {}  # 处理中，继续轮询
        return "20000000", {"result": {"utterances": [
            {"start_time": 100, "end_time": 900, "text": "你好", "additions": {"speaker": "1"}}]}}

    monkeypatch.setattr(asr, "_doubao_call", fake_call)
    monkeypatch.setattr(asr.time, "sleep", lambda s: None)
    cfg = {"doubao_api_key": "k", "doubao_app_id": "", "doubao_access_key": "", "doubao_resource_id": "r",
           "doubao_speaker": True, "language": "zh"}
    segs = asr.doubao(wav, cfg, tmp_path / "parts")
    assert segs == [{"start_ms": 100, "end_ms": 900, "speaker": "1", "text": "你好"}]
    submit = calls[0]
    assert submit[1]["X-Api-Key"] == "k" and submit[2]["request"]["enable_speaker_info"]
    assert submit[2]["audio"]["format"] == "mp3" and submit[2]["audio"]["data"]
    assert len(calls) == 3
    with pytest.raises(RuntimeError, match="未配置"):
        asr.doubao(wav, dict(cfg, doubao_api_key=""), tmp_path / "parts")


def test_unconfigured_summary_is_skipped(client, monkeypatch):
    rid = upload(client, "a.wav", b"data")["recording_id"]
    monkeypatch.setattr(pipeline, "transcribe", lambda *a: None)
    run_queue(client)
    rec = client.get(f"/api/recordings/{rid}", headers=AUTH).json()
    assert rec["status"] == "done" and rec["summary"] == ""


def test_auto_process_off_keeps_pending_and_lazy_playback(client, tmp_path):
    client.put("/api/settings", headers=AUTH, json={"general": {"auto_process": False}})
    data = make_wav(tmp_path / "a.wav")
    rid = upload(client, "note20261019-091446.wav", data, chunk=len(data))["recording_id"]
    rec = client.get(f"/api/recordings/{rid}", headers=AUTH).json()
    assert rec["status"] == "pending" and not rec["busy"] and rec["duration_s"] == pytest.approx(2, abs=0.1)
    assert client.db.one("SELECT COUNT(*) AS n FROM jobs")["n"] == 0
    # 未转写的录音首次播放时生成播放文件
    audio = client.get(f"/api/recordings/{rid}/audio", headers=AUTH)
    assert audio.status_code == 200 and audio.headers["content-type"] == "audio/mp4"
    listed = client.get("/api/recordings", headers=AUTH).json()["recordings"][0]
    assert listed["status"] == "pending" and listed["busy"] == 0
    client.post(f"/api/recordings/{rid}/process", headers=AUTH, json={"kind": "full"})
    assert client.get(f"/api/recordings/{rid}", headers=AUTH).json()["busy"]


def test_requested_summary_without_model_fails_clearly(client, monkeypatch):
    rid = upload(client, "a.wav", b"data")["recording_id"]
    monkeypatch.setattr(pipeline, "transcribe", lambda *a: None)
    run_queue(client)
    client.db.run("INSERT INTO segments(recording_id, idx, start_ms, end_ms, speaker, text) "
                  "VALUES(?, 0, 0, 1000, '0', '你好')", (rid,))
    client.post(f"/api/recordings/{rid}/process", headers=AUTH, json={"kind": "summary"})
    run_queue(client)
    rec = client.get(f"/api/recordings/{rid}", headers=AUTH).json()
    assert rec["status"] == "failed" and "还没有配置纪要模型" in rec["error"]


def test_settings_connection_test(client, monkeypatch):
    r = client.post("/api/settings/test", headers=AUTH, json={"target": "summary"}).json()
    assert not r["ok"] and "未配置" in r["message"]
    client.put("/api/settings", headers=AUTH, json={"summary": {"base_url": "http://llm.local/v1", "model": "m"}})
    monkeypatch.setattr(summary, "chat", lambda cfg, s, u: "可以")
    r = client.post("/api/settings/test", headers=AUTH, json={"target": "summary"}).json()
    assert r["ok"] and "可以" in r["message"]
    r = client.post("/api/settings/test", headers=AUTH, json={"target": "asr"}).json()
    assert not r["ok"] and "无法连接" in r["message"]  # 测试环境没有 asr-worker


def test_stale_uploads_cleaned_on_startup(tmp_path):
    import os
    app = create_app(tmp_path, TOKEN, start_worker=False)
    with TestClient(app) as c:
        old = c.post("/api/uploads", json={"name": "old.opus", "size": 10}, headers=AUTH).json()["upload_id"]
        fresh = c.post("/api/uploads", json={"name": "new.opus", "size": 10}, headers=AUTH).json()["upload_id"]
    old_part = tmp_path / "uploads" / f"{old}.part"
    os.utime(old_part, (0, 0))
    app = create_app(tmp_path, TOKEN, start_worker=False)
    ids = {r["id"] for r in app.state.db.all("SELECT id FROM uploads")}
    assert ids == {fresh} and not old_part.exists()


def test_worker_loop_survives_unexpected_error(tmp_path, monkeypatch):
    app = create_app(tmp_path, TOKEN, start_worker=False)
    worker = app.state.worker
    calls = []

    def boom():
        calls.append(1)
        if len(calls) >= 2:
            worker.stopped = True
        raise RuntimeError("db locked")

    monkeypatch.setattr(worker, "step", boom)
    monkeypatch.setattr(worker.wake, "wait", lambda timeout=None: None)
    worker.run()  # 不抛异常，并且出错后继续下一轮
    assert len(calls) == 2


def test_list_models_uses_saved_key_only_for_same_endpoint(client, monkeypatch):
    from app import settings
    calls = []

    def fake_get(url, headers=None, direct=False):
        calls.append((url, headers["Authorization"]))
        return {"data": [{"id": "whisper-1"}, {"id": "gpt-4o"}, {"id": "FunAudioLLM/SenseVoiceSmall"}]}

    monkeypatch.setattr(settings, "_get", fake_get)
    client.put("/api/settings", headers=AUTH, json={"summary": {"base_url": "https://a.example/v1", "api_key": "sk-a"}})
    r = client.post("/api/settings/models", headers=AUTH, json={"target": "summary"}).json()
    assert r["models"] == ["FunAudioLLM/SenseVoiceSmall", "gpt-4o", "whisper-1"]
    assert calls[-1] == ("https://a.example/v1/models", "Bearer sk-a")
    # 换了厂商地址又没填新密钥时，不把已保存的密钥发过去
    client.post("/api/settings/models", headers=AUTH, json={"target": "summary", "base_url": "https://b.example/v1/"})
    assert calls[-1] == ("https://b.example/v1/models", "Bearer none")
    # 转写只列语音模型
    r = client.post("/api/settings/models", headers=AUTH,
                    json={"target": "asr", "base_url": "https://b.example/v1", "api_key": "sk-b"}).json()
    assert r["models"] == ["FunAudioLLM/SenseVoiceSmall", "whisper-1"] and calls[-1][1] == "Bearer sk-b"
    assert client.post("/api/settings/models", headers=AUTH, json={"target": "asr"}).status_code == 502
    presets = client.get("/api/presets", headers=AUTH).json()
    assert all(p["site"].startswith("https://") for p in presets["llm"] + presets["asr"])


def _unit(*pairs):
    """192 维向量：pairs 是 (维度, 数值)，结果归一化。"""
    v = [0.0] * 192
    for i, x in pairs:
        v[i] = x
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def test_pick_spans_prefers_long_clean_turns():
    from app import voice
    segs = [{"start_ms": 0, "end_ms": 2000, "speaker": "0"},          # 太短
            {"start_ms": 3000, "end_ms": 9000, "speaker": "0"},       # 与说话人 1 重叠
            {"start_ms": 8000, "end_ms": 8500, "speaker": "1"},
            {"start_ms": 10000, "end_ms": 50000, "speaker": "0"},     # 截到 30 秒
            {"start_ms": 60000, "end_ms": 64000, "speaker": "0"}]
    assert voice.pick_spans(segs, "0") == [(10000, 40000), (60000, 64000)]


def test_voiceprint_enroll_auto_name_suggest_and_clear(client, tmp_path, monkeypatch):
    from app import voice
    wav = tmp_path / "long.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=26",
                    "-ar", "16000", "-ac", "1", str(wav)], check=True)
    data = wav.read_bytes()
    turns = [{"start_ms": 0, "end_ms": 12000, "speaker": "0", "text": "第一位说话人在讲预算"},
             {"start_ms": 13000, "end_ms": 25000, "speaker": "1", "text": "第二位说话人在讲排期"}]
    monkeypatch.setattr(asr, "funasr", lambda *a: turns)
    wang, li, other = _unit((0, 1)), _unit((1, 1)), _unit((2, 1))
    current = {}
    monkeypatch.setattr(voice, "_embed_clip", lambda url, clip: current[clip.stem.split("-")[0]])

    def transcribe(name, vectors):
        current.clear()
        current.update(vectors)
        rid = upload(client, name, data, chunk=len(data))["recording_id"]
        run_queue(client)
        return rid

    def get(rid):
        return client.get(f"/api/recordings/{rid}", headers=AUTH).json()

    # 1. 声纹库为空：只提取，不命名
    r1 = transcribe("a.wav", {"0": wang, "1": li})
    rec = get(r1)
    assert set(rec["voices"]) == {"0", "1"} and rec["speaker_names"] == {} and rec["voice_matches"]["speakers"] == {}
    # 这条录音在登记前就处理过，稍后应被回补
    r0 = transcribe("b.wav", {"0": _unit((0, 1), (1, 0.15)), "1": other})
    assert get(r0)["speaker_names"] == {}

    # 2. 命名即登记，并回补之前处理过的录音
    client.patch(f"/api/recordings/{r1}", headers=AUTH, json={"speaker_names": {"0": "王总", "1": "李工"}})
    assert {p["name"] for p in client.get("/api/voices", headers=AUTH).json()["people"]} == {"王总", "李工"}
    rec0 = get(r0)
    assert rec0["speaker_names"] == {"0": "王总"} and rec0["voice_matches"]["speakers"]["0"]["auto"]

    # 3. 新录音：相似度高的自动命名，中等的只给建议，无关的不匹配
    r2 = transcribe("c.wav", {"0": other, "1": _unit((0, 0.66), (3, 0.75))})   # 与王总相似度约 0.66
    rec2 = get(r2)
    assert rec2["speaker_names"] == {}
    assert rec2["voice_matches"]["speakers"]["1"]["name"] == "王总" and not rec2["voice_matches"]["speakers"]["1"]["auto"]
    assert "0" not in rec2["voice_matches"]["speakers"]
    r3 = transcribe("d.wav", {"0": _unit((1, 1), (5, 0.1)), "1": _unit((0, 1), (6, 0.1))})
    assert get(r3)["speaker_names"] == {"0": "李工", "1": "王总"}

    # 4. 撤销命名会撤销登记，人没有样本后从声纹库消失
    client.patch(f"/api/recordings/{r1}", headers=AUTH, json={"speaker_names": {"0": "王总"}})
    client.patch(f"/api/recordings/{r3}", headers=AUTH, json={"speaker_names": {"1": "王总"}})
    assert [p["name"] for p in client.get("/api/voices", headers=AUTH).json()["people"]] == ["王总"]

    # 5. 单独识别声纹：没有说话人的录音失败，但录音状态不变
    monkeypatch.setattr(asr, "funasr", lambda *a: [{"start_ms": 0, "end_ms": 5000, "speaker": None, "text": "没有说话人信息"}])
    r4 = transcribe("e.wav", {})
    client.post(f"/api/recordings/{r4}/process", headers=AUTH, json={"kind": "voice"})
    run_queue(client)
    rec4 = get(r4)
    assert rec4["status"] == "done" and "没有区分说话人" in rec4["voice_matches"]["error"]

    # 6. 一键清空
    client.delete("/api/voices", headers=AUTH)
    assert client.get("/api/voices", headers=AUTH).json()["people"] == [] and get(r1)["voices"] == {}


def _voice_rec(db, rid, voices):
    """直接写入一条录音和它的声纹：voices = {说话人: (向量, 秒数)}。"""
    from app.db import now
    db.run("INSERT INTO recordings(id, name, size, title, audio_path, sha256, status, created_at, updated_at) "
           "VALUES(?,?,?,?,?,?,?,?,?)", (rid, f"v{rid}", rid, f"v{rid}", "", "", "done", now(), now()))
    for spk, (vector, seconds) in voices.items():
        db.run("INSERT INTO recording_voices VALUES(?,?,?,?)", (rid, spk, json.dumps(vector), seconds))


def test_voiceprint_little_data_not_enrolled_and_not_auto(client):
    from app import voice
    cfg = {"auto": 0.75, "suggest": 0.6, "margin": 0.08}
    wang = _unit((0, 1))
    # 只有 4 秒声纹：命名后不登记
    _voice_rec(client.db, 901, {"0": (wang, 4.0)})
    voice.enroll(client.db, 901, {"0": "王总"}, cfg)
    assert voice.library(client.db) == []
    # 声纹充足时登记；另一条录音里同一个人只说了 4 秒，只给建议不自动命名
    _voice_rec(client.db, 902, {"0": (wang, 25.0)})
    _voice_rec(client.db, 903, {"0": (_unit((0, 1), (4, 0.1)), 4.0)})
    voice.enroll(client.db, 902, {"0": "王总"}, cfg)
    m = voice.match(client.db, 903, cfg)
    assert m["0"]["name"] == "王总" and not m["0"]["auto"]
    assert json.loads(client.db.one("SELECT speaker_names FROM recordings WHERE id=903")["speaker_names"]) == {}


def test_voiceprint_one_person_library_needs_clear_lead(client):
    """声纹库只有一个人时，没法和“第二像的人”比，靠同一录音里的其他说话人做对照。"""
    from app import voice
    cfg = {"auto": 0.75, "suggest": 0.6, "margin": 0.08}
    _voice_rec(client.db, 911, {"0": (_unit((0, 1)), 25.0)})
    voice.enroll(client.db, 911, {"0": "王总"}, cfg)
    # 王总不在场：两位陌生人都和他有几分像（0.80 / 0.78），不能把名字自动给其中一位，也不重复提示
    _voice_rec(client.db, 912, {"0": (_unit((0, 0.8), (1, 0.6)), 20.0), "1": (_unit((0, 0.78), (2, 0.626)), 20.0)})
    m = voice.match(client.db, 912, cfg)
    assert list(m) == ["0"] and not m["0"]["auto"]
    # 王总在场且明显领先其他人：自动命名
    _voice_rec(client.db, 913, {"0": (_unit((0, 0.95), (1, 0.31)), 20.0), "1": (_unit((0, 0.78), (2, 0.626)), 20.0)})
    m = voice.match(client.db, 913, cfg)
    assert list(m) == ["0"] and m["0"]["auto"]
    assert json.loads(client.db.one("SELECT speaker_names FROM recordings WHERE id=913")["speaker_names"]) == {"0": "王总"}


def test_voice_settings_validation(client):
    r = client.put("/api/settings", headers=AUTH, json={"voice": {"auto": "0.8", "suggest": "0.62"}})
    assert r.status_code == 200 and r.json()["voice"]["auto"] == 0.8
    assert client.put("/api/settings", headers=AUTH, json={"voice": {"auto": 0.5, "suggest": 0.6}}).status_code == 400


def test_edit_segment_text_and_speaker(client, monkeypatch, tmp_path):
    wav = tmp_path / "x.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=6",
                    "-ar", "16000", "-ac", "1", str(wav)], check=True)
    monkeypatch.setattr(asr, "funasr", lambda *a: [
        {"start_ms": 0, "end_ms": 2000, "speaker": "0", "text": "文记很好用"},
        {"start_ms": 2000, "end_ms": 4000, "speaker": "1", "text": "我也觉得"},
        {"start_ms": 4000, "end_ms": 6000, "speaker": "0", "text": "这句其实是说话人二说的"}])
    data = wav.read_bytes()
    rid = upload(client, "edit.wav", data, chunk=len(data))["recording_id"]
    run_queue(client)
    url = f"/api/recordings/{rid}/segments"

    r = client.patch(f"{url}/0", headers=AUTH, json={"text": "  闻记很好用 "})
    seg = r.json()["segments"][0]
    assert r.status_code == 200 and seg["text"] == "闻记很好用" and seg["edited"] == 1
    assert r.json()["segments"][1]["edited"] == 0
    # 改动进入搜索、MCP 用的转写文本
    assert client.get("/api/recordings?q=闻记很好用", headers=AUTH).json()["recordings"][0]["id"] == rid

    r = client.patch(f"{url}/2", headers=AUTH, json={"speaker": "1"})
    assert r.json()["segments"][2]["speaker"] == "1" and r.json()["segments"][2]["text"] == "这句其实是说话人二说的"

    assert client.patch(f"{url}/0", headers=AUTH, json={"text": "   "}).status_code == 400
    assert client.patch(f"{url}/0", headers=AUTH, json={"speaker": "9"}).status_code == 400
    assert client.patch(f"{url}/99", headers=AUTH, json={"text": "x"}).status_code == 404
    # 有任务在排队时不允许修改，避免被转写结果覆盖
    client.post(f"/api/recordings/{rid}/process", headers=AUTH, json={"kind": "summary"})
    assert client.patch(f"{url}/0", headers=AUTH, json={"text": "y"}).status_code == 409


def test_merge_speakers(client, monkeypatch, tmp_path):
    from app import voice
    wav = tmp_path / "m.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=40",
                    "-ar", "16000", "-ac", "1", str(wav)], check=True)
    monkeypatch.setattr(asr, "funasr", lambda *a: [
        {"start_ms": 0, "end_ms": 12000, "speaker": "0", "text": "第一段"},
        {"start_ms": 12000, "end_ms": 24000, "speaker": "1", "text": "第二段"},
        {"start_ms": 24000, "end_ms": 36000, "speaker": "2", "text": "其实还是第一个人"}])
    vectors = {"0": _unit((0, 1)), "1": _unit((1, 1)), "2": _unit((0, 1), (2, 0.3))}
    calls = []

    def embed(url, clip):
        calls.append(clip.stem)
        return vectors[clip.stem.split("-")[0]]
    monkeypatch.setattr(voice, "_embed_clip", embed)
    data = wav.read_bytes()
    rid = upload(client, "merge.wav", data, chunk=len(data))["recording_id"]
    run_queue(client)
    client.patch(f"/api/recordings/{rid}", headers=AUTH, json={"speaker_names": {"2": "王总"}})
    rec = client.get(f"/api/recordings/{rid}", headers=AUTH).json()
    # 声纹相似度提示：说话人 2 最像说话人 0
    assert rec["voice_similar"]["2"]["0"] > 0.9 > rec["voice_similar"]["2"]["1"]

    url = f"/api/recordings/{rid}/speakers/merge"
    assert client.post(url, headers=AUTH, json={"source": "2", "target": "2"}).status_code == 400
    assert client.post(url, headers=AUTH, json={"source": "2", "target": "7"}).status_code == 400

    r = client.post(url, headers=AUTH, json={"source": "2", "target": "0"}).json()
    assert [s["speaker"] for s in r["segments"]] == ["0", "1", "0"]
    assert r["speaker_names"] == {"0": "王总"}           # 名字随发言转到目标说话人
    assert set(r["voices"]) == {"0", "1"} and r["busy"]  # 旧声纹作废，排队重新提取
    sample = client.db.one("SELECT speaker FROM voice_samples WHERE recording_id=?", (rid,))
    assert sample["speaker"] == "0"
    calls.clear()
    run_queue(client)
    assert calls and all(c.split("-")[0] in {"0", "1"} for c in calls)
    rec = client.get(f"/api/recordings/{rid}", headers=AUTH).json()
    assert rec["status"] == "done" and set(rec["voices"]) == {"0", "1"} and rec["speaker_names"] == {"0": "王总"}
