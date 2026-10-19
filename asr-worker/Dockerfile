FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
# 只装 CPU 版 PyTorch：Docker 在 macOS 上无法使用 Apple GPU。
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt
COPY worker.py .

# 模型缓存放在卷里，重建镜像不必重新下载（首次约 1GB）。
ENV MODELSCOPE_CACHE=/models PYTHONUNBUFFERED=1
VOLUME /models
EXPOSE 8000
CMD ["uvicorn", "worker:app", "--host", "0.0.0.0", "--port", "8000"]
