#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书中有音 · 独立转写服务（MVP）

职责：浏览媒体库 → 批量建任务 → 单并发转写 → 字幕写到音频同目录
（只读时回退到书中有音的字幕目录）。任务持久化在 SQLite，进程重启后
未完成任务重新排队（第二阶段再做 5 分钟切片的断点续转）。
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
TRANSCRIBER = os.path.join(APP_DIR, "transcriber.py")

AUDIO_EXTS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".wav",
    ".ogg", ".oga", ".opus", ".wma", ".asf", ".ape", ".aiff", ".aif", ".mka",
}
SUB_EXTS = (".srt", ".lrc", ".vtt")
SKIP_DIRS = {
    "@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information",
    ".git", ".svn", "node_modules", "__pycache__", "lost+found",
}

DATA_DIR = os.environ.get("SY_ASR_DATA") or os.path.join(APP_DIR, "data")
WORK_DIR = os.path.join(DATA_DIR, "work")
PROGRESS_DIR = os.path.join(DATA_DIR, "progress")
DB_PATH = os.path.join(DATA_DIR, "tasks.db")
MODEL_DIR = os.environ.get("SY_ASR_MODELS") or os.path.join(DATA_DIR, "models")
FALLBACK_ROOT = os.environ.get("SY_FALLBACK_SUBTITLES") or os.path.join(DATA_DIR, "subtitles")
MODEL = os.environ.get("SY_ASR_MODEL") or "medium"
THREADS = int(os.environ.get("SY_ASR_THREADS") or "4")
NICE_LEVEL = int(os.environ.get("SY_ASR_NICE") or "10")
IONICE_CLASS = os.environ.get("SY_ASR_IONICE_CLASS") or "2"
IONICE_DATA = os.environ.get("SY_ASR_IONICE_DATA") or "7"
PORT = int(os.environ.get("SY_ASR_PORT") or "8099")
TOKEN_FILE = os.path.join(DATA_DIR, "token.txt")
CALLBACK_LIMIT = 500

for folder in (DATA_DIR, WORK_DIR, PROGRESS_DIR, MODEL_DIR, FALLBACK_ROOT):
    os.makedirs(folder, exist_ok=True)


# --------------------------------------------------------------------------
# 访问口令
# --------------------------------------------------------------------------

def load_token():
    token = (os.environ.get("SY_ASR_TOKEN") or "").strip()
    if token:
        return token
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as fp:
            saved = fp.read().strip()
        if saved:
            return saved
    except OSError:
        pass
    token = secrets.token_urlsafe(9)
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as fp:
            fp.write(token)
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass
    return token


TOKEN = load_token()
COOKIE_NAME = "sy_asr"
COOKIE_VALUE = hashlib.sha256(("asr:" + TOKEN).encode("utf-8")).hexdigest()[:40]


# --------------------------------------------------------------------------
# 自然排序（与书中有音保持一致，章节序号才能对齐）
# --------------------------------------------------------------------------

CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}
CN_NUM_RE = re.compile(r"[一二三四五六七八九十百千零〇两]+")
NUM_SPLIT_RE = re.compile(r"(?P<num>[0-9]+|[一二三四五六七八九十百千零〇两]+)")
EXTRA_CHAPTER_RE = re.compile(r"(番外|花絮|特典|彩蛋|小剧场|\bsp\b|\bextra\b|\bbonus\b|\bova\b)", re.I)


def cn_number(text):
    total = current = 0
    for char in str(text or ""):
        if char in CN_DIGITS:
            current = CN_DIGITS[char]
        elif char in CN_UNITS:
            total += (current or 1) * CN_UNITS[char]
            current = 0
        else:
            return None
    return total + current


def natural_text_key(text):
    def repl(match):
        token = match.group(0)
        if token.isdigit():
            return "%09d" % int(token)
        value = cn_number(token)
        return "%09d" % value if value is not None else token
    return NUM_SPLIT_RE.sub(repl, str(text).lower())


def natural_path_key(path):
    return [natural_text_key(part) for part in re.split(r"[\\/]+", str(path)) if part]


def is_extra_chapter(text):
    return bool(EXTRA_CHAPTER_RE.search(str(text or "")))


def is_audio(name):
    return os.path.splitext(name)[1].lower() in AUDIO_EXTS


VOLUME_DIR_RE = re.compile(
    r"^(?:"
    r"(?:cd|disc|disk|dvd|part|vol|volume|book)\s*[-_.·]?\s*"
    r"[0-9一二三四五六七八九十百千零〇两]+"
    r"|第?\s*[0-9一二三四五六七八九十百千零〇两]+\s*[部卷册集辑季篇]"
    r"|(?:部|卷|册|集|辑|季|篇)\s*[0-9一二三四五六七八九十百千零〇两]+"
    r"|[0-9]{1,6}\s*[-~～—–_至到]\s*[0-9]{1,6}\s*[集话話章]?"
    r"|上|中|下|上册|中册|下册"
    r")$", re.I)
SIBLING_NUM_RE = re.compile(r"^\d{1,2}$")


def volume_dir_names(dirnames):
    matched, numeric = set(), []
    for name in dirnames:
        text = str(name).strip()
        if not text:
            continue
        if VOLUME_DIR_RE.match(text):
            matched.add(name)
        elif SIBLING_NUM_RE.match(text):
            numeric.append(name)
    if len(numeric) >= 2:
        matched.update(numeric)
    return matched


def list_entries(folder):
    """返回 (子目录名, 音频文件名)，跳过隐藏目录、系统目录与符号链接"""
    dirs, files = [], []
    try:
        entries = os.listdir(folder)
    except OSError:
        return dirs, files
    for name in entries:
        path = os.path.join(folder, name)
        try:
            if os.path.islink(path):
                continue
            if os.path.isdir(path):
                if not name.startswith(".") and not name.startswith("@") and name not in SKIP_DIRS:
                    dirs.append(name)
            elif os.path.isfile(path) and is_audio(name):
                files.append(name)
        except OSError:
            continue
    return dirs, files


def collect_audio(folder, depth_left=8):
    """递归收集音频，按相对路径自然排序；跳过隐藏目录与系统目录"""
    found = []
    base = len(folder.rstrip(os.sep))
    for dirpath, dirnames, filenames in os.walk(folder):
        depth = dirpath.rstrip(os.sep).count(os.sep) - base
        if depth > depth_left:
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames
                       if not name.startswith(".") and not name.startswith("@")
                       and name not in SKIP_DIRS]
        for name in filenames:
            if is_audio(name) and not name.startswith("."):
                found.append(os.path.join(dirpath, name))
    found.sort(key=lambda path: natural_path_key(os.path.relpath(path, folder)))
    return found


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------

DB_LOCK = threading.RLock()
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row


def db_init():
    with DB_LOCK:
        DB.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          book_id TEXT NOT NULL,
          book_title TEXT DEFAULT '',
          chapter_index INTEGER NOT NULL,
          chapter_title TEXT DEFAULT '',
          audio_path TEXT NOT NULL,
          out_base TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'queued',
          percent REAL DEFAULT 0,
          cues INTEGER DEFAULT 0,
          duration REAL DEFAULT 0,
          tries INTEGER DEFAULT 0,
          pid INTEGER DEFAULT 0,
          error TEXT DEFAULT '',
          created_at INTEGER DEFAULT 0,
          updated_at INTEGER DEFAULT 0,
          started_at INTEGER DEFAULT 0,
          finished_at INTEGER DEFAULT 0,
          UNIQUE(book_id, chapter_index)
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, id);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        DB.commit()
        # 上次进程退出时仍在跑的任务：回收为排队，半成品在 work 目录，不影响成品
        DB.execute("UPDATE tasks SET state='queued', pid=0, percent=0, cues=0, "
                   "error='服务重启，重新排队' WHERE state IN ('running','cancelling')")
        DB.commit()


def db_get(key, default):
    with DB_LOCK:
        row = DB.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def db_set(key, value):
    with DB_LOCK:
        DB.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value, ensure_ascii=False)))
        DB.commit()


def default_roots():
    raw = os.environ.get("SY_MEDIA_ROOTS") or ""
    roots = [item.strip() for item in raw.split(os.pathsep) if item.strip()]
    return roots or ["/media"]


db_init()
ROOTS = db_get("media_roots", default_roots())
PAUSED = bool(db_get("paused", False))


# --------------------------------------------------------------------------
# 媒体库扫描
# --------------------------------------------------------------------------

LIB_CACHE = {"at": 0, "books": []}
LIB_LOCK = threading.Lock()


def subtitle_bases(book_id, audio):
    stem = os.path.splitext(os.path.basename(audio))[0]
    return [
        os.path.join(os.path.dirname(audio), stem),
        os.path.join(FALLBACK_ROOT, book_id, stem),
    ]


def has_subtitle(book_id, audio):
    for base in subtitle_bases(book_id, audio):
        for ext in SUB_EXTS:
            if os.path.isfile(base + ext):
                return True
    return False


def output_base(book_id, audio):
    """可写就写音频同目录；否则回退到 <fallback>/<book_id>/<stem>"""
    folder = os.path.dirname(audio) or "/"
    writable = False
    try:
        writable = os.access(folder, os.W_OK)
        if writable:
            probe = os.path.join(folder, ".asr-write-test")
            with open(probe, "a", encoding="utf-8"):
                pass
            os.remove(probe)
    except OSError:
        writable = False
    if writable:
        return os.path.splitext(audio)[0]
    target = os.path.join(FALLBACK_ROOT, book_id)
    os.makedirs(target, exist_ok=True)
    return os.path.join(target, os.path.splitext(os.path.basename(audio))[0])


def make_book(root, book_path, files):
    """与书中有音一致：book_id = sha1(书目录绝对路径)[:12]，章节按自然序 + 番外置后"""
    book_id = hashlib.sha1(book_path.encode("utf-8")).hexdigest()[:12]
    ordered = []
    for path in files:
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            rel = os.path.basename(path)
        ordered.append((natural_path_key(rel), os.path.basename(path), path, rel))
    ordered.sort(key=lambda item: (is_extra_chapter(item[1]) or is_extra_chapter(item[3]),
                                   item[0], item[1]))
    chapters = []
    for index, (_key, name, path, _rel) in enumerate(ordered):
        chapters.append({
            "index": index,
            "title": os.path.splitext(name)[0],
            "file_name": name,
            "audio": path,
            "has_subtitle": has_subtitle(book_id, path),
        })
    return {
        "book_id": book_id,
        "title": os.path.basename(book_path.rstrip(os.sep)) or book_path,
        "path": book_path,
        "single": os.path.isfile(book_path),
        "chapters": chapters,
    }


def scan_root(root):
    """按书中有音的成书规则扫描：目录内有音频才成书，分卷目录合并，否则继续下钻"""
    books = []
    stack = [(root, 0)]
    while stack:
        dirpath, depth = stack.pop()
        if depth > 8:
            continue
        dirs, audio_names = list_entries(dirpath)
        dirs.sort(key=natural_path_key)
        audio = sorted(audio_names, key=natural_path_key)
        if depth == 0:
            # 根目录散文件各自成书；目录继续下钻
            for name in audio:
                path = os.path.join(dirpath, name)
                books.append(make_book(root, path, [path]))
            stack.extend((os.path.join(dirpath, name), 1) for name in dirs)
            continue
        volumes = volume_dir_names(dirs)
        if volumes:
            files = [os.path.join(dirpath, name) for name in audio]
            for name in sorted(volumes, key=natural_path_key):
                files.extend(collect_audio(os.path.join(dirpath, name), 8 - depth))
            if files:
                books.append(make_book(root, dirpath, files))
            continue
        if audio:
            files = [os.path.join(dirpath, name) for name in audio]
            for name in dirs:
                if name not in volumes:
                    files.extend(collect_audio(os.path.join(dirpath, name), 8 - depth))
            books.append(make_book(root, dirpath, files))
            continue
        stack.extend((os.path.join(dirpath, name), depth + 1) for name in dirs
                     if name not in volumes)
    return books


def scan_library(force=False):
    now = time.time()
    with LIB_LOCK:
        if not force and now - LIB_CACHE["at"] < 120 and LIB_CACHE["books"]:
            return LIB_CACHE["books"]
        books = []
        for root in ROOTS:
            if not os.path.isdir(root):
                continue
            try:
                books.extend(scan_root(root))
            except OSError:
                continue
        books.sort(key=lambda item: natural_path_key(item["title"]))
        LIB_CACHE["at"] = now
        LIB_CACHE["books"] = books
        return books


def find_book(book_id, force=False):
    for book in scan_library(force):
        if book["book_id"] == book_id:
            return book
    return None


# --------------------------------------------------------------------------
# 任务
# --------------------------------------------------------------------------

def task_payload(row):
    return {
        "id": row["id"], "book_id": row["book_id"], "book_title": row["book_title"],
        "chapter_index": row["chapter_index"], "chapter_title": row["chapter_title"],
        "audio_path": row["audio_path"], "state": row["state"],
        "percent": round(row["percent"] or 0, 1), "cues": row["cues"],
        "duration": round(row["duration"] or 0, 1), "tries": row["tries"],
        "error": row["error"], "created_at": row["created_at"],
        "updated_at": row["updated_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def list_tasks(state="all", limit=800):
    sql = "SELECT * FROM tasks"
    args = []
    if state and state != "all":
        sql += " WHERE state=?"
        args.append(state)
    sql += " ORDER BY CASE state WHEN 'running' THEN 0 WHEN 'cancelling' THEN 0 " \
           "WHEN 'queued' THEN 1 ELSE 2 END, id ASC LIMIT ?"
    args.append(limit)
    with DB_LOCK:
        rows = DB.execute(sql, args).fetchall()
    return [task_payload(row) for row in rows]


def create_tasks(items, skip_existing=True):
    created, skipped = 0, 0
    for item in items:
        book = find_book(item.get("book_id"))
        if not book:
            continue
        chapters = book["chapters"]
        start = int(item.get("from") or 0)
        end = int(item.get("to") if item.get("to") is not None else len(chapters) - 1)
        start = max(0, min(start, len(chapters) - 1))
        end = max(start, min(end, len(chapters) - 1))
        for chapter in chapters[start:end + 1]:
            audio = chapter["audio"]
            if skip_existing and has_subtitle(book["book_id"], audio):
                skipped += 1
                continue
            with DB_LOCK:
                exists = DB.execute("SELECT id FROM tasks WHERE book_id=? AND chapter_index=?",
                                    (book["book_id"], chapter["index"])).fetchone()
                if exists:
                    skipped += 1
                    continue
                now = int(time.time())
                DB.execute(
                    "INSERT INTO tasks(book_id, book_title, chapter_index, chapter_title, "
                    "audio_path, out_base, state, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?, 'queued', ?, ?)",
                    (book["book_id"], book["title"], chapter["index"], chapter["title"],
                     audio, output_base(book["book_id"], audio), now, now))
                DB.commit()
                created += 1
    return {"created": created, "skipped": skipped}


def counts():
    with DB_LOCK:
        rows = DB.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state").fetchall()
    result = {row["state"]: row["n"] for row in rows}
    for key in ("queued", "running", "done", "failed", "cancelled"):
        result.setdefault(key, 0)
    result["total"] = sum(result.values())
    return result


def update_task(task_id, **fields):
    if not fields:
        return
    fields["updated_at"] = int(time.time())
    keys = list(fields.keys())
    sql = "UPDATE tasks SET " + ", ".join("%s=?" % key for key in keys) + " WHERE id=?"
    with DB_LOCK:
        DB.execute(sql, [fields[key] for key in keys] + [task_id])
        DB.commit()


# --------------------------------------------------------------------------
# 转写 worker
# --------------------------------------------------------------------------

def tail_text(path, limit=1200):
    try:
        with open(path, "rb") as fp:
            fp.seek(0, os.SEEK_END)
            size = fp.tell()
            fp.seek(max(0, size - limit * 4))
            return fp.read().decode("utf-8", "replace")[-limit:]
    except OSError:
        return ""


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="asr-worker")
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            if PAUSED:
                time.sleep(1)
                continue
            with DB_LOCK:
                row = DB.execute("SELECT * FROM tasks WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
                if row:
                    DB.execute("UPDATE tasks SET state='running', tries=tries+1, started_at=?, "
                               "updated_at=?, error='' WHERE id=?",
                               (int(time.time()), int(time.time()), row["id"]))
                    DB.commit()
            if not row:
                time.sleep(1)
                continue
            try:
                self.run_task(dict(row))
            except Exception as exc:  # worker 不能死
                update_task(row["id"], state="failed", finished_at=int(time.time()),
                            error="%s: %s" % (type(exc).__name__, exc))

    def run_task(self, task):
        task_id = task["id"]
        work = os.path.join(WORK_DIR, str(task_id))
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
        part_srt = os.path.join(work, "out.srt")
        part_lrc = os.path.join(work, "out.lrc")
        part_words = os.path.join(work, "out.words.json")
        progress_path = os.path.join(PROGRESS_DIR, "%d.json" % task_id)
        env = dict(os.environ)
        env.setdefault("HF_HOME", MODEL_DIR)
        env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        env.setdefault("XDG_CACHE_HOME", os.path.join(DATA_DIR, "cache"))

        cmd = [sys.executable, TRANSCRIBER,
               "--audio", task["audio_path"], "--srt", part_srt, "--lrc", part_lrc,
               "--words", part_words, "--progress", progress_path,
               "--model", MODEL, "--threads", str(THREADS)]
        wrapper = []
        if shutil.which("ionice"):
            wrapper += ["ionice", "-c", IONICE_CLASS, "-n", IONICE_DATA]
        if shutil.which("nice"):
            wrapper += ["nice", "-n", str(NICE_LEVEL)]
        cmd = wrapper + cmd

        log_path = os.path.join(work, "run.log")
        state = {"cancelled": False}
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                    env=env, start_new_session=True)
            update_task(task_id, pid=proc.pid)
            last_read = 0.0
            announced = False
            while proc.poll() is None:
                time.sleep(1)
                self.check_cancel(task_id, proc, state)
                if time.time() - last_read >= 1:
                    last_read = time.time()
                    info = read_progress(progress_path)
                    if info:
                        update_task(task_id, percent=float(info.get("percent") or 0),
                                    cues=int(info.get("cues") or 0),
                                    duration=float(info.get("duration") or 0))
                        if not announced and (info.get("cues") or 0) > 0:
                            announced = True
            # 进程退出后再读一次，避免最后一秒的进度丢失
            info = read_progress(progress_path) or {}
            code = proc.returncode

        if state["cancelled"] or task_state(task_id) == "cancelling":
            cleanup_outputs(part_srt, part_lrc, part_words)
            shutil.rmtree(work, ignore_errors=True)
            update_task(task_id, state="cancelled", pid=0, finished_at=int(time.time()))
            return

        if code == 0 and os.path.isfile(part_srt) and info.get("state") in ("ready", "cancelled"):
            self.publish(task, part_srt, part_lrc, part_words)
            shutil.rmtree(work, ignore_errors=True)
            update_task(task_id, state="done", percent=100, pid=0,
                        cues=int(info.get("cues") or 0),
                        duration=float(info.get("duration") or 0),
                        finished_at=int(time.time()))
        else:
            error = str(info.get("error") or "").strip() or tail_text(log_path)
            update_task(task_id, state="failed", pid=0, finished_at=int(time.time()),
                        percent=float(info.get("percent") or 0),
                        error=error[-1000:] or ("转写进程退出码 %s" % code))

    def check_cancel(self, task_id, proc, state):
        row = task_state(task_id)
        if row == "cancelling" and not state["cancelled"]:
            state["cancelled"] = True
            terminate(proc)

    def publish(self, task, part_srt, part_lrc, part_words):
        base = task["out_base"]
        os.makedirs(os.path.dirname(base) or "/", exist_ok=True)
        for src, suffix in ((part_srt, ".srt"), (part_lrc, ".lrc"), (part_words, ".words.json")):
            if os.path.isfile(src):
                tmp = base + suffix + ".tmp"
                shutil.copyfile(src, tmp)
                os.replace(tmp, base + suffix)
        LIB_CACHE["at"] = 0


def task_state(task_id):
    with DB_LOCK:
        row = DB.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
    return row["state"] if row else ""


def read_progress(path):
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return None


def terminate(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def cleanup_outputs(*paths):
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


WORKER = Worker()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def json_bytes(data):
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "shuzhongyouyin-asr/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def send_json(self, data, code=200, headers=None):
        body = json_bytes(data)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 4 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def authed(self):
        header = (self.headers.get("X-Auth-Token") or "").strip()
        if header and secrets.compare_digest(header, TOKEN):
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and secrets.compare_digest(value, COOKIE_VALUE):
                return True
        query = urllib.parse.urlparse(self.path).query
        token = urllib.parse.parse_qs(query).get("token", [""])[0]
        return bool(token) and secrets.compare_digest(token, TOKEN)

    def require(self):
        if self.authed():
            return True
        self.send_json({"ok": False, "error": "未授权，请先输入访问口令"}, 401)
        return False

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/health":
                self.send_json({"ok": True, "app": "shuzhongyouyin-asr", "version": VERSION,
                                "paused": PAUSED, "counts": counts()})
                return
            if path.startswith("/api/"):
                if not self.require():
                    return
                if path == "/api/state":
                    self.send_json({"ok": True, "version": VERSION, "paused": PAUSED,
                                    "roots": ROOTS, "counts": counts(),
                                    "model": MODEL, "threads": THREADS,
                                    "fallback": FALLBACK_ROOT})
                elif path == "/api/library":
                    query = urllib.parse.parse_qs(parsed.query)
                    force = query.get("refresh", ["0"])[0] == "1"
                    books = scan_library(force)
                    keyword = (query.get("q", [""])[0] or "").strip().lower()
                    if keyword:
                        filtered = []
                        for book in books:
                            if keyword in book["title"].lower():
                                filtered.append(book)
                                continue
                            chapters = [ch for ch in book["chapters"]
                                        if keyword in ch["title"].lower()]
                            if chapters:
                                copy = dict(book)
                                copy["chapters"] = chapters
                                filtered.append(copy)
                        books = filtered
                    self.send_json({"ok": True, "books": books})
                elif path == "/api/tasks":
                    state = urllib.parse.parse_qs(parsed.query).get("state", ["all"])[0]
                    self.send_json({"ok": True, "tasks": list_tasks(state), "counts": counts(),
                                    "paused": PAUSED})
                else:
                    self.send_json({"ok": False, "error": "接口不存在"}, 404)
                return
            self.serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self.send_json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/api/login":
                data = self.read_json()
                token = str(data.get("token") or "")
                if not token or not secrets.compare_digest(token, TOKEN):
                    self.send_json({"ok": False, "error": "访问口令不正确"}, 401)
                    return
                self.send_json({"ok": True}, headers={
                    "Set-Cookie": "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"
                                  % (COOKIE_NAME, COOKIE_VALUE)})
                return
            if path == "/api/logout":
                self.send_json({"ok": True}, headers={
                    "Set-Cookie": "%s=; Path=/; HttpOnly; Max-Age=0" % COOKIE_NAME})
                return
            if not self.require():
                return
            if path == "/api/tasks":
                data = self.read_json()
                items = data.get("items")
                if not isinstance(items, list):
                    items = [{"book_id": bid, "from": 0, "to": None}
                             for bid in (data.get("book_ids") or [])]
                result = create_tasks(items, bool(data.get("skip_existing", True)))
                self.send_json({"ok": True, "result": result, "counts": counts()})
            elif path == "/api/queue":
                global PAUSED
                data = self.read_json()
                PAUSED = bool(data.get("paused"))
                db_set("paused", PAUSED)
                self.send_json({"ok": True, "paused": PAUSED})
            elif path == "/api/roots":
                global ROOTS
                data = self.read_json()
                roots = [str(item).strip().rstrip("/") for item in (data.get("roots") or [])
                         if str(item).strip()]
                if not roots:
                    self.send_json({"ok": False, "error": "至少保留一个媒体库根目录"}, 400)
                    return
                ROOTS = roots
                db_set("media_roots", ROOTS)
                LIB_CACHE["at"] = 0
                self.send_json({"ok": True, "roots": ROOTS})
            elif path.startswith("/api/tasks/"):
                parts = [part for part in path.split("/") if part]
                if len(parts) != 4:
                    self.send_json({"ok": False, "error": "接口路径错误"}, 400)
                    return
                task_id = int(parts[2])
                action = parts[3]
                if action == "retry":
                    update_task(task_id, state="queued", percent=0, cues=0, error="",
                                pid=0, finished_at=0)
                    self.send_json({"ok": True, "counts": counts()})
                elif action == "cancel":
                    with DB_LOCK:
                        row = DB.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
                    if not row:
                        self.send_json({"ok": False, "error": "任务不存在"}, 404)
                    elif row["state"] == "queued":
                        update_task(task_id, state="cancelled", finished_at=int(time.time()))
                        self.send_json({"ok": True})
                    else:
                        update_task(task_id, state="cancelling")
                        self.send_json({"ok": True, "cancelling": True})
                else:
                    self.send_json({"ok": False, "error": "未知操作"}, 404)
            else:
                self.send_json({"ok": False, "error": "接口不存在"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self.send_json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500)

    def serve_static(self, path):
        if path in ("/", "/index.html"):
            file_path = os.path.join(STATIC_DIR, "index.html")
            mime = "text/html; charset=utf-8"
        else:
            name = path.lstrip("/")
            if "/" in name or name.startswith("."):
                self.send_json({"ok": False, "error": "Not found"}, 404)
                return
            file_path = os.path.join(STATIC_DIR, name)
            mime = {"js": "application/javascript", "css": "text/css",
                    "svg": "image/svg+xml"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
        try:
            with open(file_path, "rb") as fp:
                body = fp.read()
        except OSError:
            self.send_json({"ok": False, "error": "Not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def main():
    WORKER.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("[asr] listening on 0.0.0.0:%d, roots=%s" % (PORT, ROOTS), flush=True)
    # 口令不落日志：只提示是否已配置以及来源
    source = "env" if (os.environ.get("SY_ASR_TOKEN") or "").strip() else (
        "file" if os.path.isfile(TOKEN_FILE) else "generated")
    print("[asr] access token configured (%s)" % source, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
