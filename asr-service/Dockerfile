FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_ENDPOINT=https://hf-mirror.com \
    HF_HUB_DISABLE_XET=1 \
    HF_HUB_DOWNLOAD_TIMEOUT=60 \
    SY_ASR_DATA=/data \
    SY_MEDIA_ROOTS=/media \
    SY_FALLBACK_SUBTITLES=/fallback/subtitles \
    SY_ASR_PORT=8099

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg util-linux ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# huggingface_hub 1.x/2.x 在部分镜像源上会走 xet 传输且容易卡住，固定到 0.x 稳定线
RUN pip install "huggingface_hub>=0.23,<1.0" "faster-whisper>=1.0,<2" "opencc-python-reimplemented>=0.1.7"

WORKDIR /app
COPY app.py transcriber.py ./
COPY static ./static

EXPOSE 8099
VOLUME ["/data"]

CMD ["python", "app.py"]
