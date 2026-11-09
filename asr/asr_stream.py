#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流式音频转写：按 60 秒切片顺序识别，逐片/逐句追加写入同名 .srt，结束时补 .lrc。

由应用服务端以独立子进程调用，不参与 HTTP 请求路径。进度写入 --progress 指定的
JSON 文件，供服务端和网页端轮询；进程收到 SIGTERM 时尽量收尾（已完成的部分保留）。
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

ASR_ROOT = os.environ.get("SY_ASR_ROOT") or "/vol2/1000/.transcribe"
ASR_MODELS = os.path.join(ASR_ROOT, "models")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HOME", ASR_MODELS)
os.environ.setdefault("XDG_CACHE_HOME", os.path.join(ASR_ROOT, "cache"))

from faster_whisper import WhisperModel  # noqa: E402

try:
    from opencc import OpenCC
    _T2S = OpenCC("t2s").convert
except Exception:  # 没装 opencc 时退化为原样输出
    _T2S = lambda text: text

PUNCT_END = "。！？!?；;…"
MAX_CUE_CHARS = 18
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
STOP = {"flag": False}


def ts_srt(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def ts_lrc(sec):
    sec = max(0.0, float(sec))
    m = int(sec // 60)
    s = sec - m * 60
    return "[%02d:%05.2f]" % (m, s)


def clean_text(text):
    return re.sub(r"\s+", " ", _T2S(text)).strip()


def split_units(text, start, end):
    """中文按单字切、英文按词切，并把时间按长度均分到每个字"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) > 1 and CJK_RE.search(text):
        span = max(0.0, float(end) - float(start))
        step = span / len(text)
        return [(ch, start + step * i, start + step * (i + 1)) for i, ch in enumerate(text)]
    return [(text, start, end)]


class CueBuilder(object):
    """把字级时间戳合并成 6~18 字的短句；跨片段保留缓冲"""

    def __init__(self):
        self.cues = []
        self.buf = []
        self.buf_start = None
        self.last_end = None

    def add_word(self, text, start, end):
        text = text.strip()
        if not text:
            return False
        gap = start - self.last_end if self.last_end is not None else 0
        if self.buf and (len("".join(item["t"] for item in self.buf)) >= MAX_CUE_CHARS or gap > 0.65):
            self._flush()
        if not self.buf:
            self.buf_start = start
        self.buf.append({"t": text, "start": start, "end": end})
        self.last_end = end
        joined = "".join(item["t"] for item in self.buf)
        if joined and joined[-1] in PUNCT_END and len(joined) >= 4:
            self._flush()
            return True
        return False

    def _flush(self):
        if not self.buf:
            return
        texts = [_T2S(item["t"]) for item in self.buf]
        self.cues.append({
            "start": self.buf_start, "end": self.last_end,
            "text": re.sub(r"\s+", " ", "".join(texts)).strip(),
            "words": [{"start": round(float(item["start"]), 3),
                       "end": round(float(item["end"]), 3), "t": text}
                      for item, text in zip(self.buf, texts)],
        })
        self.buf = []
        self.buf_start = None

    def flush(self):
        self._flush()


def cue_rows(cues, upto=None):
    """补齐结束时间：每句延续到下一句开始（避免高亮闪烁），最后一句至少 2 秒"""
    items = cues if upto is None else cues[:upto]
    rows = []
    for i, cue in enumerate(items):
        start = float(cue["start"])
        end = float(cue["end"] or start)
        nxt = float(items[i + 1]["start"]) if i + 1 < len(items) else None
        if nxt is not None and nxt > start:
            end = max(end, nxt)
        elif nxt is None:
            end = max(end, start + 2.0)
        if end <= start:
            end = start + 1.0
        rows.append({"start": round(start, 3), "end": round(end, 3),
                     "text": cue["text"], "words": cue.get("words") or []})
    return rows


def write_srt(path, rows):
    out = []
    for i, cue in enumerate(rows, 1):
        out.append(str(i))
        out.append("%s --> %s" % (ts_srt(cue["start"]), ts_srt(cue["end"])))
        out.append(cue["text"])
        out.append("")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write("\n".join(out))
    os.replace(tmp, path)


def write_lrc(path, rows, title):
    lines = ["[ti:%s]" % title]
    for cue in rows:
        lines.append(ts_lrc(cue["start"]) + cue["text"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def write_words(path, rows):
    """字级时间戳：{format:"words", segments:[{start,end,text,words:[{start,end,t}]}]}"""
    payload = {"format": "words", "segments": rows}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False)
    os.replace(tmp, path)


def write_progress(path, data):
    if not path:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def media_duration(path):
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            stderr=subprocess.DEVNULL, timeout=30)
        return float(out.strip().splitlines()[0])
    except Exception:
        return 0.0


def slice_wav(audio, start, length, tmp_dir):
    """用 ffmpeg 抽出 16k 单声道 wav 切片"""
    path = os.path.join(tmp_dir, "slice-%06d.wav" % int(start))
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
         "-ss", "%.2f" % start, "-t", "%.2f" % length, "-i", audio,
         "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", path],
        check=True, timeout=180)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--srt", required=True)
    ap.add_argument("--lrc", default="")
    ap.add_argument("--words", default="")
    ap.add_argument("--progress", default="")
    ap.add_argument("--model", default="medium")
    ap.add_argument("--threads", type=int, default=10)
    ap.add_argument("--slice", type=float, default=60.0)
    ap.add_argument("--hotwords", default="张若尘 池瑶 昆仑界 飞天鱼 万古神帝 八百年后 九帝 女皇 神武印记")
    ap.add_argument("--prompt", default="以下是普通话有声小说，请用简体中文并带标点。")
    args = ap.parse_args()

    started_at = time.time()
    progress = {
        "state": "starting", "audio": args.audio, "srt": args.srt,
        "duration": 0, "done": 0, "cues": 0,
        "percent": 0, "started_at": started_at, "updated": started_at,
        "first_cue_at": 0, "first_cue_elapsed": 0,
        "finished_at": 0, "elapsed": 0, "error": "",
    }
    write_progress(args.progress, progress)

    def on_term(signum, frame):
        STOP["flag"] = True

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    tmp_dir = tempfile.mkdtemp(prefix="asr-slice-", dir=os.path.join(ASR_ROOT, "tmp")
                               if os.path.isdir(os.path.join(ASR_ROOT, "tmp")) else None)
    builder = CueBuilder()
    total = media_duration(args.audio)
    progress["duration"] = round(total, 2)
    progress["state"] = "loading"
    write_progress(args.progress, progress)

    try:
        model = WhisperModel(args.model, device="cpu", compute_type="int8",
                             cpu_threads=args.threads,
                             download_root=ASR_MODELS)
        progress["state"] = "running"
        write_progress(args.progress, progress)

        pos = 0.0
        last_write = 0.0
        while pos < total and not STOP["flag"]:
            length = min(args.slice, total - pos)
            wav = slice_wav(args.audio, pos, length, tmp_dir)
            segments, _info = model.transcribe(
                wav, language="zh", beam_size=5, word_timestamps=True,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 600},
                initial_prompt=args.prompt or None,
                hotwords=args.hotwords or None,
            )
            changed = False
            for seg in segments:
                if STOP["flag"]:
                    break
                for word in (seg.words or []):
                    text = (word.word or "")
                    start = pos + float(word.start or 0)
                    end = pos + float(word.end or word.start or 0)
                    for unit, u_start, u_end in split_units(text, start, end):
                        if builder.add_word(unit, u_start, u_end):
                            changed = True
                if changed and time.time() - last_write >= 0.4:
                    rows = cue_rows(builder.cues)
                    write_srt(args.srt, rows)
                    if args.words:
                        write_words(args.words, rows)
                    if not progress["first_cue_at"] and rows:
                        progress["first_cue_at"] = time.time()
                        progress["first_cue_elapsed"] = round(progress["first_cue_at"] - started_at, 1)
                    progress["cues"] = len(rows)
                    progress["done"] = round(pos + float(seg.end or 0), 1)
                    progress["percent"] = round(100.0 * min(pos + float(seg.end or 0), total) / total, 1) if total else 0
                    progress["updated"] = time.time()
                    write_progress(args.progress, progress)
                    last_write = time.time()
                    changed = False
            # 切片收尾：把缓冲刷成一句，立即落盘
            builder.flush()
            rows = cue_rows(builder.cues)
            write_srt(args.srt, rows)
            if args.words:
                write_words(args.words, rows)
            if not progress["first_cue_at"] and rows:
                progress["first_cue_at"] = time.time()
                progress["first_cue_elapsed"] = round(progress["first_cue_at"] - started_at, 1)
            pos += length
            progress["done"] = round(min(pos, total), 2)
            progress["cues"] = len(rows)
            progress["percent"] = round(100.0 * pos / total, 1) if total else 0
            progress["updated"] = time.time()
            write_progress(args.progress, progress)
            try:
                os.remove(wav)
            except OSError:
                pass

        rows = cue_rows(builder.cues)
        write_srt(args.srt, rows)
        if args.words:
            write_words(args.words, rows)
        if args.lrc:
            write_lrc(args.lrc, rows, os.path.basename(args.lrc))
        cancelled = bool(STOP["flag"]) or pos < total
        progress["state"] = "cancelled" if cancelled else "ready"
        progress["cues"] = len(rows)
        progress["done"] = round(min(pos, total), 2)
        progress["percent"] = progress["percent"] if cancelled else 100
        progress["finished_at"] = time.time()
        progress["elapsed"] = round(progress["finished_at"] - started_at, 1)
        if total:
            progress["rtf"] = round(progress["elapsed"] / total, 3)
        write_progress(args.progress, progress)
        return 0
    except Exception as exc:
        progress["state"] = "failed"
        progress["error"] = "%s: %s" % (type(exc).__name__, exc)
        progress["finished_at"] = time.time()
        progress["elapsed"] = round(progress["finished_at"] - started_at, 1)
        write_progress(args.progress, progress)
        raise
    finally:
        try:
            for name in os.listdir(tmp_dir):
                os.remove(os.path.join(tmp_dir, name))
            os.rmdir(tmp_dir)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
