# 书中有音（Shuzhong Youyin）

> 项目地址：https://github.com/15338844785/shuzhongyouyin
> FPK 安装包下载：https://github.com/15338844785/shuzhongyouyin/releases （`arm` / `x86` 两个版本，按设备架构选择）

自托管的有声书管理与播放服务：扫描本地有声书目录、自动分章、在线刮削书名/作者/简介/封面、背景音乐播放、多端访问。纯 Python 标准库实现，零第三方依赖，单个 `server.py` 即可运行。

> 本仓库是 Docker 化发行版。镜像同时提供 `linux/amd64`、`linux/arm64`、`linux/arm/v7` 三种架构。

## 特性

- 有声书目录扫描与自动分章，支持多级目录、章节序号/集号识别
- 在线刮削：喜马拉雅 / 懒人听书 / 猫耳 FM，章节标题与专辑封面一并写回
- 封面本地落盘 + 缓存版本号，换图后刷新即可见
- 背景音乐：曲库扫描、上传、多格式真实格式识别、HTTP Range 断点投递
- 播放进度、收藏、播放列表、倍速、定时关闭
- 账号体系：管理员/普通用户、多用户独立进度、头像
- 多端访问：桌面浏览器、手机浏览器，局域网直连即可
- 字幕：读取音频同目录的 `.srt` / `.lrc` / `.vtt`；批量转写由独立 Docker 服务完成

## 字幕转写（独立服务）

从 1.7.0 起，书中有音本体**只负责播放与字幕显示**，不再内置 Whisper 模型、venv 与转写进程。
批量把音频转成字幕由配套的 Docker 应用 [`asr-service`](asr-service/) 完成：

- 浏览媒体库、搜索书名/章节，按书或按章节范围批量建任务
- 单并发队列（一次一章、4 线程、`nice 10`、`ionice 7`），SQLite 持久化，可暂停、失败重试
- 字幕写到音频同目录；媒体目录只读时回退到 `@appdata/shuzhongyouyin/subtitles/<book_id>/`
- 访问口令由环境变量 `SY_ASR_TOKEN` 设置，公网部署不会裸奔；FPK 部署时随机生成并保存在
  `@appdata/shuzhongyouyin/asr-token.txt`（仅 root 可读，面板只显示「已配置/未配置」，不回显明文、不写日志）

FPK 安装/升级时会检查 `docker`/`dockerd` 并自动部署容器 `shuzhongyouyin-asr`（端口 `8099`，
`--restart unless-stopped`、`mem_limit 4g`）。**本机没有可用的 Docker 时，安装会直接中止**并提示先安装
Docker，不会装成半残状态。手动部署见 [`asr-service/docker-compose.yml`](asr-service/docker-compose.yml)。

镜像发布在 `ghcr.io/15338844785/shuzhongyouyin-asr:1.0.0`（`:latest` 同源）。首次启动会拉取约 **1.03 GB**
镜像，模型（medium 约 1.5 GB）在第一次转写时下载。

装好后在书中有音「我的 → 转写服务」里可以看到服务状态、修改服务地址，并点「选择要转换的书籍」跳到转写页面。

## 三种安装方式

### 1. docker run

```bash
docker run -d \
  --name shuzhongyouyin \
  --restart unless-stopped \
  -p 8098:8098 \
  -e TZ=Asia/Shanghai \
  -e FNPATH_DATA_DIR=/data \
  -v /volume1/docker/shuzhongyouyin/data:/data \
  -v /volume1/有声书:/media/books:ro \
  ghcr.io/15338844785/shuzhongyouyin:1.7.0
```

### 2. docker-compose

把仓库里的 `docker-compose.yml` 下载下来，按实际情况修改宿主机路径后：

```bash
docker compose up -d
docker compose logs -f
```

### 3. 源码直跑

```bash
git clone https://github.com/15338844785/shuzhongyouyin.git
cd shuzhongyouyin
python3 server.py --port 8098
```

要求 Python 3.8 及以上（代码未使用 3.9+ 专属语法），不需要 `pip install`（无第三方依赖）。默认数据目录为仓库上一级的 `data/`；可用 `FNPATH_DATA_DIR=/your/data python3 server.py` 指定。最低版本也可用 `python3 server.py --port 8098 --host 0.0.0.0` 显式指定监听地址。

> 本项目没有发布到 npm，Node 不是运行依赖。

## 端口与目录

| 项目 | 容器内 | 说明 |
|---|---|---|
| HTTP 端口 | `8098` | 网页与接口；可用 `-p 8099:8098` 改宿主机端口 |
| 数据目录 | `/data` | 必须持久化，挂载到宿主机 |
| 有声书库 | 自定义（示例 `/media/books`） | 只读挂载即可，在网页里添加为书库目录 |
| BGM 目录 | `/data/bgm`（默认） | 也可挂载已有音乐目录后在设置里指过去 |

`/data` 下会生成：`settings.json`、`users.json`、`sessions.json`、`library.json`、`metadata.json`、`covers/`、`avatars/`、`bgm/`、`scrapers/`、`playlists.json`。

## 首次访问与初始化管理员

1. 浏览器打开 `http://<NAS-IP>:8098`。
2. 首次启动会自动创建管理员账号：
   - 用户名：`admin`
   - 密码：`admin123`
3. 登录后请立刻在「设置 → 用户」里修改密码，并删除或降级不需要的账号。
4. 在「设置 → 书库」添加有声书目录（容器内路径，例如 `/media/books`），保存后触发扫描。

> 账号信息保存在 `/data/users.json`，容器重建不会丢失；但请勿把 `/data` 暴露到公网。

## 局域网 / 公网 / 中继访问

- **局域网**：直接访问 `http://NAS-IP:8098`，无需额外配置。
- **公网**：不要把 8098 明文端口直接映射到公网。建议用 Nginx / Caddy / 飞牛反向代理做 HTTPS 终止并转发到 `127.0.0.1:8098`；公网暴露时建议再加一层访问控制（VPN、IP 白名单、Basic Auth 或 ZeroTier/Tailscale）。
- **中继/远程访问**：可使用飞牛自带远程访问、Tailscale、ZeroTier 等把 8098 暴露在虚拟内网，再按局域网方式访问。
- 服务自带账号登录，但没有内置 HTTPS；公网场景请务必在有 TLS 的反代之后运行。

## 背景音乐

- 默认目录：`/data/bgm`，网页「设置 → 背景音乐」可上传、删除、试听。
- 也可挂载已有音乐目录，例如 `-v /volume1/音乐:/media/bgm`，然后在设置里把 BGM 目录设为 `/media/bgm`（该目录需要容器内可写，才能上传）。
- 支持的格式：`mp3`、`m4a`、`m4b`、`aac`、`flac`、`wav`、`ogg`/`oga`、`opus`、`wma`/`asf`、`ape`、`aiff`/`aif`、`mka`。
- 投递时按文件真实格式返回 Content-Type，并支持 HTTP Range（可拖动进度、断点续播）。

## 升级与备份

- **升级**：`docker compose pull && docker compose up -d`。镜像升级不会改动 `/data`。
- **备份**：停止容器后整目录备份 `/data` 即可；最少要备份 `users.json`、`settings.json`、`library.json`、`metadata.json`、`covers/`、`progress` 相关文件。
- **迁移**：把 `/data` 复制到新机器，改挂载路径后启动即可。

## ARM / ARMv7 注意事项

- `linux/amd64`：x86_64 NAS / 服务器。
- `linux/arm64`：ARMv8 设备（如飞牛 ARM 版、部分 RK3588 设备）。
- `linux/arm/v7`：32 位 ARM（armhf）。仅适用于 32 位用户空间的设备；64 位系统请用 `arm64` 镜像。
- ARMv7 设备性能通常较弱，首次扫描大书库会明显更慢，建议分批添加目录。
- 如果某个架构的基础镜像不可用（例如上游 `python:3.11-alpine` 不再提供 `arm/v7`），该架构构建会失败。届时以 Actions 的实际结果为准：只保留成功架构，并在 Release 说明里明确写出，不会用「应该可以」代替验证结果。

## 常见问题

**端口被占用**
`docker compose logs` 若出现 `Address already in use`，把宿主机端口改成别的，例如 `-p 8099:8098`。

**权限错误 / 无法写入 `/data`**
容器以 UID/GID `1000` 运行。宿主机目录需可写：

```bash
sudo chown -R 1000:1000 /volume1/docker/shuzhongyouyin/data
```

**时区不对**
设置 `TZ=Asia/Shanghai`（镜像已内置 tzdata），然后重启容器。

**中文文件名显示异常**
宿主机文件系统需为 UTF-8 挂载；NFS/SMB 请确认没有启用非 UTF-8 编码选项。容器内不需要额外设置 locale。

**扫描不到书**
确认目录已挂载进容器（`docker exec shuzhongyouyin ls /media/books`），且在网页设置里添加的是容器内路径，不是宿主机路径。

**封面不更新**
刮削成功后封面会带版本号，重新进入详情页即可看到；若仍是旧图，强制刷新浏览器缓存。

## 许可

- 本项目基于 [ting-reader](https://github.com/dqsq2e2/ting-reader)（AGPL-3.0）的扫描/解析实现二次开发，衍生作品继续以 AGPL-3.0 分发。
- 「书中有音」自身的使用许可与免责声明见 [LICENSE](LICENSE)；上游贡献与 AGPL 说明见 [NOTICE](NOTICE)。
