# syntax=docker/dockerfile:1

# ---------- 构建阶段：只做语法校验，产物与运行时一致 ----------
FROM python:3.11-alpine AS build
WORKDIR /src
COPY server.py ./
RUN python -m py_compile server.py

# ---------- 运行阶段 ----------
FROM python:3.11-alpine AS runtime

# 时区数据用于 TZ 环境变量；应用本身为纯标准库，无第三方依赖
RUN apk add --no-cache tzdata \
    && addgroup -g 1000 -S app \
    && adduser -u 1000 -G app -S -H -s /sbin/nologin app

WORKDIR /app
COPY --from=build /src/server.py /app/server.py
COPY static/ /app/static/
# 合规：随镜像分发本项目许可与上游 AGPL 许可原文
COPY LICENSE NOTICE /app/
COPY thirdparty/ /app/thirdparty/

RUN mkdir -p /data \
    && chown -R app:app /app /data

USER app

ENV FNPATH_DATA_DIR=/data \
    TRIM_SERVICE_PORT=8098 \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8098
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8098/api/health',timeout=3)"

ENTRYPOINT ["python", "/app/server.py"]
CMD ["--host", "0.0.0.0", "--port", "8098"]
