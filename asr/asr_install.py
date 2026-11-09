#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 字幕本地转写引擎 · 一键安装器

由应用服务端以独立子进程调用（系统 python3，不进入 HTTP 请求路径）：
  1. 依赖预检：python3 / ffmpeg / 磁盘 / pypi 镜像 / hf-mirror
  2. 创建独立 venv（放在 .transcribe 点号目录，书库扫描会跳过）
  3. 阿里云 pypi 镜像安装 faster-whisper + ctranslate2 + opencc
  4. 从 hf-mirror 拉取 whisper 模型
  5. 校验导入与模型文件后写就绪状态

进度写入 --progress 指定的 JSON，供服务端与网页端轮询；日志写入 --log。
进程收到 SIGTERM 时会把状态记为 cancelled 并退出，保留已下载的包缓存与模型。
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

PYPI_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
PYPI_HOST = "mirrors.aliyun.com"
HF_ENDPOINT = "https://hf-mirror.com"
PIP_PACKAGES = ("faster-whisper", "ctranslate2", "opencc-python-reimplemented")
MODEL_NEED_MB = {"small": 1400, "medium": 2800, "large-v3": 4200}
STOP = {"flag": False}


def sizeof_dir(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def free_mb(path):
    try:
        usage = shutil.disk_usage(path)
        return int(usage.free / 1024 / 1024)
    except OSError:
        return -1


def http_ok(url, timeout=8):
    """连通性预检：任何 HTTP 响应都算通，只有连接层失败才算不通"""
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": "Mozilla/5.0 shuzhongyouyin-asr-installer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1024)
        return True, ""
    except urllib.error.HTTPError as exc:
        return True, "HTTP %s" % exc.code
    except Exception as exc:  # URLError / timeout / SSL
        return False, str(exc)


def which_py():
    for name in ("python3", "python"):
        path = shutil.which(name)
        if path:
            return path
    return ""


class Installer(object):
    def __init__(self, root, model, progress_path, log_path):
        self.root = root
        self.model = model
        self.progress_path = progress_path
        self.log_path = log_path
        self.log_fp = None
        self.started = time.time()
        self.progress = {
            "state": "installing", "step": "准备", "percent": 0,
            "message": "", "error": "", "error_kind": "", "checks": [],
            "model": model, "root": root, "started_at": self.started,
            "updated": self.started, "finished_at": 0, "elapsed": 0,
        }

    # ------------------------------ 状态与日志

    def write(self):
        self.progress["updated"] = time.time()
        tmp = self.progress_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self.progress, fp, ensure_ascii=False)
            os.replace(tmp, self.progress_path)
        except OSError:
            pass

    def log(self, text):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), text)
        if self.log_fp:
            try:
                self.log_fp.write(line + "\n")
                self.log_fp.flush()
            except OSError:
                pass
        print(line, flush=True)

    def step(self, name, percent, message=""):
        self.progress["step"] = name
        self.progress["percent"] = percent
        self.progress["message"] = message
        self.write()
        self.log("%s (%d%%) %s" % (name, percent, message))

    def fail(self, kind, message):
        self.progress["state"] = "failed"
        self.progress["error"] = message
        self.progress["error_kind"] = kind
        self.progress["finished_at"] = time.time()
        self.progress["elapsed"] = round(self.progress["finished_at"] - self.started, 1)
        self.write()
        self.log("失败[%s]: %s" % (kind, message))
        return 1

    # ------------------------------ 预检

    def precheck(self):
        self.step("依赖预检", 2, "检查 python3 / ffmpeg / 磁盘 / 镜像")
        checks = []

        def add(key, label, ok, detail):
            checks.append({"key": key, "label": label, "ok": bool(ok), "detail": detail})

        py = which_py()
        version = ""
        if py:
            try:
                version = subprocess.check_output(
                    [py, "-V"], stderr=subprocess.STDOUT, timeout=15,
                    text=True).strip()
            except Exception as exc:
                version = "无法运行: %s" % exc
        add("python3", "python3", bool(py) and "Python 3" in version,
            version or "未找到 python3")

        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        add("ffmpeg", "ffmpeg / ffprobe", bool(ffmpeg and ffprobe),
            (ffmpeg or "未找到 ffmpeg") + ("" if ffprobe else "；缺少 ffprobe") )

        free = free_mb(self.root if os.path.isdir(self.root) else os.path.dirname(self.root) or "/")
        need = MODEL_NEED_MB.get(self.model, 2800)
        add("disk", "磁盘空间", free >= need if free >= 0 else False,
            "剩余 %s MB / 需要约 %s MB" % (free, need))

        pypi_ok, pypi_detail = http_ok(PYPI_INDEX)
        add("pypi", "阿里云 pypi 镜像", pypi_ok, pypi_detail or PYPI_INDEX)
        hf_ok, hf_detail = http_ok(HF_ENDPOINT)
        add("hf", "hf-mirror 模型源", hf_ok, hf_detail or HF_ENDPOINT)
        self.progress["checks"] = checks

        missing = [item for item in checks if not item["ok"]]
        if missing:
            item = missing[0]
            kind = {"python3": "dependency", "ffmpeg": "dependency",
                    "disk": "disk", "pypi": "network", "hf": "network"}[item["key"]]
            return kind, "%s 未就绪：%s" % (item["label"], item["detail"])
        return "", ""

    # ------------------------------ 安装步骤

    def env(self):
        env = os.environ.copy()
        env.update({
            "HOME": self.root,
            "XDG_CACHE_HOME": os.path.join(self.root, "cache"),
            "HF_HOME": os.path.join(self.root, "models"),
            "HF_ENDPOINT": HF_ENDPOINT,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
        })
        return env

    def run(self, cmd, timeout=1800):
        self.log("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, cwd=self.root, env=self.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    self.log(line)
                if STOP["flag"]:
                    proc.terminate()
                    break
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("命令超时：%s" % cmd[0])
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
        return proc.returncode

    def ensure_venv(self):
        self.step("创建独立运行环境", 8, "venv 位于 %s" % self.root)
        venv_dir = os.path.join(self.root, "venv")
        venv_py = os.path.join(venv_dir, "bin", "python")
        py = which_py()
        usable = False
        if os.path.isfile(venv_py):
            try:
                usable = subprocess.call([venv_py, "-m", "pip", "--version"],
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, timeout=20) == 0
            except Exception:
                usable = False
        if not usable:
            if os.path.isdir(venv_dir):
                shutil.rmtree(venv_dir, ignore_errors=True)
            code = self.run([py, "-m", "venv", venv_dir])
            if code != 0 or not os.path.isfile(venv_py):
                raise RuntimeError("创建 venv 失败（退出码 %s），请确认 python3-venv 可用" % code)
        return venv_py

    def install_packages(self):
        self.step("安装 Python 依赖", 18, "阿里云 pypi 镜像")
        pip = [os.path.join(self.root, "venv", "bin", "pip")]
        base = ["--index-url", PYPI_INDEX, "--trusted-host", PYPI_HOST, "--timeout", "60"]
        code = self.run(pip + ["install", "--upgrade", "pip"] + base, timeout=600)
        if code != 0:
            self.log("pip 自升级失败（退出码 %s），继续使用自带版本" % code)
        self.step("安装 Python 依赖", 30, "faster-whisper / ctranslate2 / opencc")
        code = self.run(pip + ["install"] + list(PIP_PACKAGES) + base, timeout=2400)
        if code != 0:
            raise RuntimeError("pip 安装失败（退出码 %s），详见日志" % code)
        self.step("安装 Python 依赖", 70, "校验模块导入")
        code = self.run([os.path.join(self.root, "venv", "bin", "python"), "-c",
                         "import faster_whisper, ctranslate2, opencc; "
                         "print('faster-whisper', faster_whisper.__version__, "
                         "ctranslate2.__version__)"], timeout=300)
        if code != 0:
            raise RuntimeError("依赖安装后校验失败，可能是镜像缺包或磁盘写入不完整")

    def fetch_model(self):
        self.step("下载语音模型", 74, "%s（hf-mirror）" % self.model)
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr_fetch_model.py")
        if not os.path.isfile(script):
            script = os.path.join(self.root, "asr_fetch_model.py")
        code = self.run([os.path.join(self.root, "venv", "bin", "python"), script,
                         "--model", self.model, "--dir", os.path.join(self.root, "models")],
                        timeout=3600)
        if code != 0:
            raise RuntimeError("模型下载失败（退出码 %s），请检查 hf-mirror 连通性后重试" % code)
        target = os.path.join(self.root, "models",
                              "models--Systran--faster-whisper-" + self.model)
        if not os.path.isdir(target):
            raise RuntimeError("模型目录未生成：%s" % target)

    def deploy_scripts(self):
        """把转写脚本放到点号目录，运行时不依赖应用安装目录"""
        src_dir = os.path.dirname(os.path.abspath(__file__))
        for name in ("asr_stream.py", "asr_fetch_model.py"):
            src = os.path.join(src_dir, name)
            if os.path.isfile(src):
                dst = os.path.join(self.root, name)
                try:
                    shutil.copyfile(src, dst)
                except OSError as exc:
                    raise RuntimeError("写入 %s 失败：%s" % (dst, exc))

    def main(self):
        try:
            os.makedirs(self.root, exist_ok=True)
            os.makedirs(os.path.join(self.root, "logs"), exist_ok=True)
        except OSError as exc:
            kind = "disk" if getattr(exc, "errno", 0) == 28 else "permission"
            message = "无法创建转写目录 %s：%s" % (self.root, exc)
            self.progress["state"] = "failed"
            self.progress["error"] = message
            self.progress["error_kind"] = kind
            self.progress["finished_at"] = time.time()
            self.progress["elapsed"] = round(self.progress["finished_at"] - self.started, 1)
            self.write()
            self.log(message)
            return 1
        self.log_fp = open(self.log_path, "a", encoding="utf-8")
        self.write()
        self.log("安装开始：root=%s model=%s" % (self.root, self.model))
        try:
            kind, message = self.precheck()
            if kind:
                return self.fail(kind, message)
            self.step("依赖预检", 6, "全部通过")
            self.ensure_venv()
            self.install_packages()
            self.fetch_model()
            self.step("部署转写脚本", 96, "写入 .transcribe 目录")
            self.deploy_scripts()
            self.step("完成", 100, "引擎就绪")
            self.progress["state"] = "ready"
            self.progress["finished_at"] = time.time()
            self.progress["elapsed"] = round(self.progress["finished_at"] - self.started, 1)
            self.write()
            return 0
        except PermissionError as exc:
            return self.fail("permission", "权限不足：%s" % exc)
        except OSError as exc:
            kind = "disk" if getattr(exc, "errno", 0) == 28 else "permission"
            return self.fail(kind, "文件写入失败：%s" % exc)
        except Exception as exc:
            return self.fail("unknown", "%s: %s" % (type(exc).__name__, exc))
        finally:
            if self.log_fp:
                try:
                    self.log_fp.close()
                except Exception:
                    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--model", default="medium")
    ap.add_argument("--progress", required=True)
    ap.add_argument("--log", default="")
    args = ap.parse_args()

    def on_term(_signum, _frame):
        STOP["flag"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    root = os.path.abspath(args.root)
    log_path = args.log or os.path.join(root, "logs", "install.log")
    installer = Installer(root, args.model, args.progress, log_path)
    code = installer.main()
    if STOP["flag"] and installer.progress.get("state") != "ready":
        installer.progress["state"] = "cancelled"
        installer.progress["message"] = "安装已取消"
        installer.progress["finished_at"] = time.time()
        installer.progress["elapsed"] = round(installer.progress["finished_at"] - installer.started, 1)
        installer.write()
    return code


if __name__ == "__main__":
    sys.exit(main())
