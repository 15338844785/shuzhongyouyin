# 书中有音 · 转写服务（asr-service）

独立 Docker 应用：浏览媒体库 → 勾选书籍/章节 → 单并发转写 → 字幕写回音频同目录。
书中有音本体只负责播放与字幕显示，不再本地转写。

## 快速开始

首次启动会拉取约 **1.03 GB** 镜像；模型（medium 约 1.5 GB）在第一次转写时下载。

```bash
docker pull ghcr.io/15338844785/shuzhongyouyin-asr:1.0.0
docker run -d --name shuzhongyouyin-asr --restart unless-stopped \
  -p 8099:8099 \
  -e SY_ASR_TOKEN=你的口令 \
  -e SY_MEDIA_ROOTS=/vol2/1000/有声书 \
  -v /vol2/1000/有声书:/vol2/1000/有声书 \
  -v /vol1/@appdata/shuzhongyouyin:/fallback \
  -v asr-data:/data \
  --memory 4g \
  ghcr.io/15338844785/shuzhongyouyin-asr:1.0.0
```

或修改 `docker-compose.yml` 后：

```bash
docker compose up -d
```

自己从源码构建（开发用）：

```bash
docker build -t shuzhongyouyin-asr:1.0.0 .
```

打开 `http://<NAS>:8099/`，输入 `SY_ASR_TOKEN`。从书中有音设置页进入时会自动带
`?callback=<书中有音地址>`，页面右上角可直接返回。

## 关键约束

- **路径必须一致**：`SY_MEDIA_ROOTS` 与容器内挂载路径都要和书中有音里配置的书库路径完全相同，
  书的 ID 是 `sha1(书目录绝对路径)[:12]`，路径不同会导致两边对不上。
- **回退目录**：媒体目录可写时字幕写音频同目录；不可写时写
  `/fallback/subtitles/<book_id>/<音频文件名>.srt`，与书中有音的回退规则一致。
- **一次只转一章**：单 worker、4 线程、`nice 10`、`ionice -c2 -n7`；内存峰值约 2.3 GB（medium）。
- **任务持久化**：SQLite 位于 `/data/tasks.db`；服务重启后未完成任务重新排队（半成品不对外可见）。
- **模型缓存**：默认在 `/data/models`（medium 约 1.5 GB），首次任务会先下载；建议保留 `/data` 卷。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SY_ASR_TOKEN` | 自动生成并写入 `/data/token.txt` | 访问口令；公网部署必须设置 |
| `SY_MEDIA_ROOTS` | `/media` | 媒体库根目录，多个用 `:` 分隔 |
| `SY_ASR_MODEL` | `medium` | faster-whisper 模型 |
| `SY_ASR_THREADS` | `4` | 单任务 CPU 线程数 |
| `SY_ASR_NICE` | `10` | 进程优先级 |
| `SY_ASR_IONICE_CLASS` / `DATA` | `2` / `7` | 磁盘低优先级 |
| `SY_ASR_DATA` | `/data` | 任务库、模型、临时目录 |
| `SY_FALLBACK_SUBTITLES` | `/data/subtitles` | 只读媒体目录时的字幕回退根 |
| `SY_ASR_PORT` | `8099` | 监听端口 |

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 免鉴权健康检查（书中有音用它判断服务状态） |
| POST | `/api/login` | `{token}`，成功后种 HttpOnly Cookie |
| GET | `/api/state` | 根目录、队列暂停、计数、模型与线程 |
| GET | `/api/library?q=&refresh=1` | 书与章节（含 `has_subtitle`） |
| POST | `/api/tasks` | `{items:[{book_id,from,to}]}` 或 `{book_ids:[]}`，批量建任务 |
| GET | `/api/tasks` | 任务列表 |
| POST | `/api/tasks/<id>/retry` / `cancel` | 失败重试 / 取消 |
| POST | `/api/queue` | `{paused:true\|false}` 暂停或继续队列 |
| POST | `/api/roots` | 修改媒体库根目录 |

鉴权：`Cookie: sy_asr=...`、`X-Auth-Token` 或 `?token=`。口令只保存在文件里（`/data/token.txt`，FPK 部署为 `@appdata/shuzhongyouyin/asr-token.txt`），不打印到日志。

## 第二阶段（本次未实现，已预留）

- 断点续转：5 分钟切片 + manifest + 0.8 s 重叠合并；当前取消/重启会丢弃半成品重新转。
- 夜间时间窗：例如 01:00–07:00 才消费队列。
- 起止时间手动裁剪、按书自动排重、失败自动退避重试。

## 镜像

- 发布标签：`ghcr.io/15338844785/shuzhongyouyin-asr:1.0.0`、`:latest`
- 基础镜像 `python:3.11-slim` + ffmpeg + faster-whisper，不含模型；镜像约 1.03 GB
- 首次启动会拉取约 1.03 GB 镜像，模型（medium 约 1.5 GB）在第一次转写时下载
