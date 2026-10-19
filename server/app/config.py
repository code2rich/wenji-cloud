"""运行配置：全部来自环境变量，见仓库根目录 .env.example。"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("WENJI_DATA", "/data"))
TOKEN = os.environ.get("WENJI_TOKEN", "")
MIN_TOKEN_LEN = 24
# 单个分片上限：内网穿透常见 100MB 请求体限制，分片远小于它。
MAX_CHUNK = 16 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024 * 1024
AUDIO_EXTS = {".wav", ".opus", ".ogg", ".mp3", ".m4a", ".flac", ".aac", ".webm", ".mp4"}

# 超过这么多天没有续传的上传会话在服务启动时清理。
STALE_UPLOAD_DAYS = 30
