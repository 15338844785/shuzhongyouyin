#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书中有音 - 飞牛 NAS 有声书播放服务端（纯标准库实现）"""

import argparse
import base64
import difflib
import hashlib
import html
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import ssl
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")

AUDIO_EXTS = {
    ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".wav",
    ".ogg", ".oga", ".opus", ".wma", ".asf", ".ape", ".aiff", ".aif", ".mka",
}

COVER_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

BGM_EXTS = set(AUDIO_EXTS)  # 背景音乐与书库共用同一份音频扩展名白名单
MAX_BGM_BYTES = 80 * 1024 * 1024  # 80MB

# 音频魔数：按内容判断真实格式。扩展名不可信（如 FLAC 改名 .mp3 会被 MPEG 解码器
# 当成帧同步乱解，听感就是持续的电流噪声），投递时必须按真实格式给 Content-Type。
# 上传、目录扫描（/api/bgm/list）、投递（/bgm/<name> 与 /bgm/file?p=）共用此判定。
ASF_WMA_MAGIC = bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")


def audio_format_of(head):
    """按文件头识别音频格式，返回 (MIME, 规范扩展名)；无法识别返回 (None, None)"""
    if not head:
        return None, None
    if head[:4] == b"fLaC":
        return "audio/flac", ".flac"
    if head[:4] == b"OggS":
        return "audio/ogg", ".ogg"          # 含 Ogg Vorbis / Opus
    if head[:3] == b"ID3":
        # 仅凭 "ID3" 三个字不能算 MP3：还要有合法版本号，否则是改名的文本
        if len(head) >= 10 and head[3] in (2, 3, 4):
            return "audio/mpeg", ".mp3"
        return None, None
    if head[:4] == b"MAC ":
        return "audio/x-ape", ".ape"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav", ".wav"
    if head[:4] == b"RF64" and head[8:12] == b"WAVE":
        return "audio/wav", ".wav"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "audio/aiff", ".aiff"
    if head[:16] == ASF_WMA_MAGIC:
        return "audio/x-ms-wma", ".wma"
    if head[4:8] == b"ftyp":
        return "audio/mp4", ".m4a"          # M4A / M4B，AAC 或 ALAC
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "audio/x-matroska", ".mka"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # MPEG 音频帧同步；layer 位为 00 时是 ADTS(AAC)
        if ((head[1] >> 1) & 0x03) == 0:
            return "audio/aac", ".aac"
        return "audio/mpeg", ".mp3"
    return None, None

# 章节字幕：同目录同名优先，支持 srt / lrc / vtt，兼容 UTF-8 与 GBK
SUB_EXTS = {".srt", ".lrc", ".vtt"}
SUB_EXT_ORDER = {".srt": 0, ".lrc": 1, ".vtt": 2}

# 目录角色：有声书库 / 背景音乐 / 通用媒体（同时参与两者）
ROLE_LABELS = {"book": "有声书库", "bgm": "背景音乐", "media": "通用媒体"}

# 飞牛 ID 登录（fnOS OAuth + PKCE），接口默认走本机 5667 端口（自签证书）
FNOS_OAUTH_CLIENT_ID = "YJNMPJUGA9"
FNOS_OAUTH_SCOPE = "trim.user.all"
FNOS_DEFAULT_ENDPOINT = "https://127.0.0.1:5667"
FNOS_PENDING_TTL = 300  # 待授权 state 有效期（秒）
FNOS_DEVICE_NAME = "书中有音"
FNOS_DEVICE_MODEL = "fnos"
FNOS_DEVICE_OS = "Linux"

# 飞牛中继访问路径前缀，例如 https://<fnid>.fnos.net/app/shuzhongyouyin/
APP_RELAY_PREFIX = "/app/shuzhongyouyin"

COVER_STEMS = {
    "cover", "folder", "front", "poster", "album", "artwork",
    "封面", "专辑封面", "海报", "封面图",
}

SKIP_DIRS = {
    "@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information",
    ".git", ".svn", "node_modules", "__pycache__", "lost+found",
}

MAX_DEPTH = 8

DEFAULT_SCRAPE_BASE = "https://www.ximalaya.com"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
IMAGE_HOSTS = (".xmcdn.com", ".ximalaya.com", ".lrts.me", ".qianqian.com",
               ".missevan.com", ".maoercdn.com")

SCRAPER_SOURCES = [
    {"id": "ximalaya", "name": "喜马拉雅 + 懒人听书 + 猫耳 FM", "type": "album", "enabled": True},
    {"id": "missevan", "name": "猫耳 FM", "type": "album", "enabled": True},
]

TRPACK_MAGIC = b"TRPACK"
MAX_PLUGIN_BYTES = 24 * 1024 * 1024
PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# 刮削源 id / 域名 → 服务端已内置的抓取实现
PLUGIN_ADAPTERS = (
    ("ximalaya", "ximalaya"),
    ("xmly", "ximalaya"),
    ("lanren", "lanren"),
    ("lrts", "lanren"),
    ("missevan", "missevan"),
)


def plugin_adapter(plugin):
    """根据刮削源声明的 id / 域名判断是否有内置抓取实现"""
    parts = [plugin.get("id") or "", plugin.get("name") or ""]
    parts.extend(plugin.get("domains") or [])
    haystack = " ".join(str(item) for item in parts).lower()
    for keyword, adapter in PLUGIN_ADAPTERS:
        if keyword in haystack:
            return adapter
    return ""


def plugin_label(plugin_id, fallback=""):
    """刮削源中文展示名"""
    text = (plugin_id or "").lower()
    if "ximalaya" in text or "xmly" in text:
        return "喜马拉雅"
    if "lanren" in text or "lrts" in text:
        return "懒人听书"
    return fallback or plugin_id or "刮削源"


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def resolve_data_dir():
    for key in ("TRIM_PKGVAR", "TRIM_DATADIR", "FNPATH_DATA_DIR"):
        value = os.environ.get(key)
        if value:
            try:
                os.makedirs(value, exist_ok=True)
                return value
            except OSError:
                pass
    fallback = os.path.join(os.path.dirname(APP_DIR), "data")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}
CN_NUM_RE = re.compile(r"[一二三四五六七八九十百千零〇两]+")
NUM_SPLIT_RE = re.compile(r"(?P<num>[0-9]+|[一二三四五六七八九十百千零〇两]+)")


def cn_number(text):
    """把中文数字（一 ~ 千九百九十九）转成整数，无法解析返回 None"""
    total, current = 0, 0
    for char in str(text or ""):
        if char in CN_DIGITS:
            current = CN_DIGITS[char]
        elif char in CN_UNITS:
            total += (current or 1) * CN_UNITS[char]
            current = 0
        else:
            return None
    return total + current


def natural_key(text):
    """自然排序键：阿拉伯数字与中文数字都按数值比较（第一章 < 第二章 < 第十章）"""
    parts = []
    for part in NUM_SPLIT_RE.split(str(text)):
        if not part:
            continue
        if part.isdigit():
            parts.append((0, int(part)))
        elif CN_NUM_RE.fullmatch(part):
            value = cn_number(part)
            parts.append((0, value) if value is not None else (1, part.lower()))
        else:
            parts.append((1, part.lower()))
    return parts


def natural_text_key(text):
    """把路径一段里的序号补零，得到可直接字符串比较的排序键"""
    def repl(match):
        token = match.group(0)
        if token.isdigit():
            return "%09d" % int(token)
        value = cn_number(token)
        return "%09d" % value if value is not None else token

    return NUM_SPLIT_RE.sub(repl, str(text).lower())


def natural_path_key(path):
    return [natural_text_key(part) for part in re.split(r"[\\/]+", str(path)) if part]


# 番外 / 花絮 / 特典 等附加内容：排序时统一放到正片之后
# 注意：不要匹配「加更 / 额外」这类普通文案，否则正常正片会被误挪到末尾
EXTRA_CHAPTER_RE = re.compile(r"(番外|花絮|特典|彩蛋|小剧场|\bsp\b|\bextra\b|\bbonus\b|\bova\b)", re.I)


def is_extra_chapter(text):
    return bool(EXTRA_CHAPTER_RE.search(str(text or "")))


def prefix_number(text):
    match = re.match(r"\s*0*([0-9]{1,6})", str(text or ""))
    return int(match.group(1)) if match else None


# 分卷目录：CD1 / Disc 2 / 第一部 / 第二卷 / 上册 / Vol 3 ...
VOLUME_DIR_RE = re.compile(
    r"^(?:"
    r"(?:cd|disc|disk|dvd|part|vol|volume|book)\s*[-_.·]?\s*"
    r"[0-9一二三四五六七八九十百千零〇两]+"
    r"|第?\s*[0-9一二三四五六七八九十百千零〇两]+\s*[部卷册集辑季篇]"
    r"|(?:部|卷|册|集|辑|季|篇)\s*[0-9一二三四五六七八九十百千零〇两]+"
    # 数字区间分卷：1-500 / 501-1000 / 001-500 / 1～500 / 1_500 / 1-500集
    r"|[0-9]{1,6}\s*[-~～—–_至到]\s*[0-9]{1,6}\s*[集话話章]?"
    r"|上|中|下|上册|中册|下册"
    r")$",
    re.I,
)
RANGE_DIR_RE = re.compile(
    r"^([0-9]{1,6})\s*[-~～—–_至到]\s*([0-9]{1,6})\s*[集话話章]?$")



def range_dir_start(name):
    """区间目录（1-500 / 501-1000）返回起始端点，用于稳定排序"""
    match = RANGE_DIR_RE.match(str(name or "").strip())
    return int(match.group(1)) if match else None
SIBLING_NUM_RE = re.compile(r"^\d{1,2}$")


def volume_dir_names(dirnames):
    """识别同级分卷目录；纯数字目录至少出现两个才视为分卷，避免误合并"""
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


def book_signature(book):
    """判重签名：文件名 + 大小 + 修改时间序列，跨根重复的同一本书只留一本"""
    chapters = book.get("chapters") or []
    if not chapters:
        return (("", 0, 0),)
    return tuple(
        (str(ch.get("file_name") or ""), int(ch.get("size") or 0), int(ch.get("mtime") or 0))
        for ch in chapters
    )


def list_dir_entries(folder):
    """列出目录内容：跳过隐藏/系统目录与符号链接，返回 (子目录名, 文件名)"""
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
                if not is_hidden_dir(name):
                    dirs.append(name)
            elif os.path.isfile(path):
                files.append(name)
        except OSError:
            continue
    return dirs, files



def hash_password(password):
    return hashlib.pbkdf2_hmac(
        "sha256", str(password).encode("utf-8"), b"shuzhongyouyin", 12000).hex()


def fnos_message(payload):
    """从 fnOS 返回体中提取错误文案"""
    if isinstance(payload, list):
        for item in payload:
            text = fnos_message(item)
            if text:
                return text
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("msg", "message", "error", "errno_desc", "reason"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def fnos_find_user_info(payload):
    """在 fnOS 返回体中找到含 uid 的用户信息对象（兼容数组与 data 包装）"""
    if isinstance(payload, list):
        for item in payload:
            found = fnos_find_user_info(item)
            if found:
                return found
        return {}
    if not isinstance(payload, dict):
        return {}
    for key in ("userInfo", "data", "user"):
        value = payload.get(key)
        if isinstance(value, dict):
            if value.get("uid") is not None or value.get("userId") is not None:
                return value
            found = fnos_find_user_info(value)
            if found:
                return found
    if payload.get("uid") is not None or payload.get("userId") is not None:
        return payload
    return {}


def app_route_path(path):
    """剥离飞牛中继前缀 /app/shuzhongyouyin，返回应用自身路由路径"""
    if path == APP_RELAY_PREFIX:
        return "/"
    if path.startswith(APP_RELAY_PREFIX + "/"):
        return path[len(APP_RELAY_PREFIX):]
    return path


# ---------- 飞牛本机账号密码校验（WebSocket + RSA + AES，纯标准库实现） ----------

def _aes_mul(left, right):
    """GF(2^8) 乘法"""
    result = 0
    for _ in range(8):
        if right & 1:
            result ^= left
        high = left & 0x80
        left = (left << 1) & 0xFF
        if high:
            left ^= 0x1B
        right >>= 1
    return result


def _aes_build_sbox():
    box = [0] * 256
    for value in range(256):
        inverse = 0
        if value:
            for candidate in range(1, 256):
                if _aes_mul(value, candidate) == 1:
                    inverse = candidate
                    break
        mixed = inverse ^ 0x63
        for shift in range(1, 5):
            mixed ^= ((inverse << shift) | (inverse >> (8 - shift))) & 0xFF
        box[value] = mixed
    return box


_AES_SBOX = _aes_build_sbox()
_AES_INV_SBOX = [0] * 256
for _index, _boxed in enumerate(_AES_SBOX):
    _AES_INV_SBOX[_boxed] = _index


def _aes_key_schedule(key):
    if len(key) != 16:
        raise ValueError("AES 密钥长度必须为 16 字节")
    words = [list(key[index:index + 4]) for index in range(0, 16, 4)]
    rcon = 1
    for index in range(4, 44):
        temp = list(words[index - 1])
        if index % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_AES_SBOX[byte] for byte in temp]
            temp[0] ^= rcon
            rcon = _aes_mul(rcon, 2)
        words.append([left ^ right for left, right in zip(words[index - 4], temp)])
    return words


def _aes_add_key(state, words, rnd):
    for column in range(4):
        word = words[rnd * 4 + column]
        for row in range(4):
            state[column * 4 + row] ^= word[row]


def _aes_sub(state, box):
    for index in range(16):
        state[index] = box[state[index]]


def _aes_shift(state):
    shifted = list(state)
    for row in range(4):
        for column in range(4):
            shifted[row + 4 * column] = state[row + 4 * ((column + row) % 4)]
    state[:] = shifted


def _aes_inv_shift(state):
    shifted = list(state)
    for row in range(4):
        for column in range(4):
            shifted[row + 4 * column] = state[row + 4 * ((column - row) % 4)]
    state[:] = shifted


def _aes_mix(state, weights):
    mixed = list(state)
    for column in range(4):
        base = column * 4
        a0, a1, a2, a3 = state[base:base + 4]
        mixed[base] = _aes_mul(a0, weights[0]) ^ _aes_mul(a1, weights[1]) ^ \
            _aes_mul(a2, weights[2]) ^ _aes_mul(a3, weights[3])
        mixed[base + 1] = _aes_mul(a0, weights[3]) ^ _aes_mul(a1, weights[0]) ^ \
            _aes_mul(a2, weights[1]) ^ _aes_mul(a3, weights[2])
        mixed[base + 2] = _aes_mul(a0, weights[2]) ^ _aes_mul(a1, weights[3]) ^ \
            _aes_mul(a2, weights[0]) ^ _aes_mul(a3, weights[1])
        mixed[base + 3] = _aes_mul(a0, weights[1]) ^ _aes_mul(a1, weights[2]) ^ \
            _aes_mul(a2, weights[3]) ^ _aes_mul(a3, weights[0])
    state[:] = mixed


def _aes_encrypt_block(block, words):
    state = list(block)
    _aes_add_key(state, words, 0)
    for rnd in range(1, 10):
        _aes_sub(state, _AES_SBOX)
        _aes_shift(state)
        _aes_mix(state, (2, 3, 1, 1))
        _aes_add_key(state, words, rnd)
    _aes_sub(state, _AES_SBOX)
    _aes_shift(state)
    _aes_add_key(state, words, 10)
    return bytes(state)


def _aes_decrypt_block(block, words):
    state = list(block)
    _aes_add_key(state, words, 10)
    for rnd in range(9, 0, -1):
        _aes_inv_shift(state)
        _aes_sub(state, _AES_INV_SBOX)
        _aes_add_key(state, words, rnd)
        _aes_mix(state, (14, 11, 13, 9))
    _aes_inv_shift(state)
    _aes_sub(state, _AES_INV_SBOX)
    _aes_add_key(state, words, 0)
    return bytes(state)


def _aes_cbc_encrypt(key, iv, data):
    words = _aes_key_schedule(key)
    data = bytes(data)
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    output = []
    previous = bytes(iv)
    for offset in range(0, len(data), 16):
        block = bytes(left ^ right for left, right in
                      zip(data[offset:offset + 16], previous))
        previous = _aes_encrypt_block(block, words)
        output.append(previous)
    return b"".join(output)


def _aes_cbc_decrypt(key, iv, data):
    words = _aes_key_schedule(key)
    output = []
    previous = bytes(iv)
    for offset in range(0, len(data) - 15, 16):
        block = bytes(data[offset:offset + 16])
        plain = bytes(left ^ right for left, right in
                      zip(_aes_decrypt_block(block, words), previous))
        output.append(plain)
        previous = block
    result = b"".join(output)
    if result:
        pad = result[-1]
        if 0 < pad <= 16 and result[-pad:] == bytes([pad]) * pad:
            result = result[:-pad]
    return result


def _der_item(data, pos):
    """读取一个 DER 条目，返回 (tag, 内容, 下一位置)"""
    tag = data[pos]
    pos += 1
    length = data[pos]
    pos += 1
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    return tag, data[pos:pos + length], pos + length


def _rsa_public_numbers(pem):
    """解析 RSA 公钥（兼容 SPKI 与 PKCS#1，允许省略 PEM 头）"""
    text = "".join(line.strip() for line in str(pem).splitlines() if "-----" not in line)
    text = "".join(text.split())
    der = base64.b64decode(text + "=" * (-len(text) % 4))
    outer_tag, outer, _ = _der_item(der, 0)
    if outer_tag != 0x30:
        raise ValueError("飞牛公钥格式不正确")
    tag, _, pos = _der_item(outer, 0)
    if tag == 0x02:
        _, modulus, pos = _der_item(outer, 0)
        _, exponent, _ = _der_item(outer, pos)
    elif tag == 0x30:
        bit_tag, bit_body, _ = _der_item(outer, pos)
        if bit_tag != 0x03:
            raise ValueError("飞牛公钥格式不正确")
        seq_tag, seq_body, _ = _der_item(bit_body[1:], 0)
        if seq_tag != 0x30:
            raise ValueError("飞牛公钥格式不正确")
        _, modulus, seq_pos = _der_item(seq_body, 0)
        _, exponent, _ = _der_item(seq_body, seq_pos)
    else:
        raise ValueError("飞牛公钥格式不正确")
    return int.from_bytes(modulus, "big"), int.from_bytes(exponent, "big")


def _rsa_encrypt(pem, message):
    """RSA PKCS#1 v1.5 加密"""
    modulus, exponent = _rsa_public_numbers(pem)
    size = (modulus.bit_length() + 7) // 8
    if len(message) > size - 11:
        raise ValueError("待加密内容过长")
    padding = b""
    while len(padding) < size - len(message) - 3:
        padding += bytes([secrets.randbelow(255) + 1])
    block = b"\x00\x02" + padding + b"\x00" + bytes(message)
    return pow(int.from_bytes(block, "big"), exponent, modulus).to_bytes(size, "big")


class FnosSocket:
    """极简 WebSocket 客户端（文本帧），用于飞牛本机登录"""

    def __init__(self, sock):
        self.sock = sock

    @classmethod
    def connect(cls, host, port, secure, path="/websocket?type=main", timeout=6):
        raw = socket.create_connection((host, port), timeout)
        raw.settimeout(timeout)
        if secure:
            raw = ssl._create_unverified_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        scheme = "https" if secure else "http"
        head = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: %s://%s:%d\r\n\r\n"
        ) % (path, host, port, key, scheme, host, port)
        raw.sendall(head.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = raw.recv(4096)
            if not chunk:
                raise ValueError("飞牛登录服务未响应")
            response += chunk
            if len(response) > 65536:
                raise ValueError("飞牛登录服务响应异常")
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise ValueError("飞牛登录服务拒绝了 WebSocket 连接")
        return cls(raw)

    def _send_frame(self, opcode, data):
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        header += mask
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        self.sock.sendall(bytes(header) + payload)

    def send(self, text):
        self._send_frame(0x01, text.encode("utf-8"))

    def _recv_exact(self, size):
        buf = b""
        while len(buf) < size:
            chunk = self.sock.recv(size - len(buf))
            if not chunk:
                raise ValueError("飞牛登录连接已断开")
            buf += chunk
        return buf

    def recv(self):
        while True:
            header = self._recv_exact(2)
            opcode = header[0] & 0x0F
            masked = header[1] & 0x80
            length = header[1] & 0x7F
            if length == 126:
                length = int.from_bytes(self._recv_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._recv_exact(8), "big")
            mask = self._recv_exact(4) if masked else b""
            data = self._recv_exact(length)
            if mask:
                data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
            if opcode == 0x08:
                raise ValueError("飞牛登录连接已关闭")
            if opcode == 0x09:
                self._send_frame(0x0A, data)
                continue
            if opcode == 0x0A:
                continue
            if opcode in (0x01, 0x02):
                return data.decode("utf-8", "replace")

    def close(self):
        try:
            self._send_frame(0x08, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


def _json_maybe(value):
    """把 JSON 字符串解析为对象，非 JSON 返回 None"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _looks_like_public_key(value):
    if not isinstance(value, str) or not value.strip():
        return False
    if "BEGIN" in value:
        return True
    text = "".join(value.split())
    if len(text) < 64:
        return False
    try:
        der = base64.b64decode(text + "=" * (-len(text) % 4))
    except Exception:
        return False
    return der[:1] == b"\x30"


def _fnos_deep_value(payload, keys, predicate=None):
    """递归查找指定键名对应的值（兼容字符串内嵌 JSON）"""
    if isinstance(payload, str):
        parsed = _json_maybe(payload)
        return _fnos_deep_value(parsed, keys, predicate) if parsed is not None else None
    if isinstance(payload, list):
        for item in payload:
            found = _fnos_deep_value(item, keys, predicate)
            if found is not None:
                return found
        return None
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                value = payload[key]
                if predicate is not None:
                    if predicate(value):
                        return value
                elif value not in (None, "", [], {}):
                    return value
        for value in payload.values():
            found = _fnos_deep_value(value, keys, predicate)
            if found is not None:
                return found
    return None


def _fnos_public_key(payload):
    return _fnos_deep_value(
        payload,
        ("pubkey", "pub", "publicKey", "public_key", "key", "rsaPub", "rsa_pub", "pk"),
        _looks_like_public_key,
    )


def _fnos_ws_read(ws, attempts=6):
    for _ in range(attempts):
        text = ws.recv()
        parsed = _json_maybe(text)
        if parsed is not None:
            return parsed
    return None


def _fnos_decrypt(payload, key):
    """解密飞牛加密应答（明文应答原样返回）"""
    data = payload
    for _ in range(3):
        if not isinstance(data, dict):
            break
        iv = data.get("iv")
        aes = data.get("aes")
        if not (isinstance(iv, str) and isinstance(aes, str)):
            break
        try:
            plain = _aes_cbc_decrypt(key, base64.b64decode(iv), base64.b64decode(aes))
            parsed = _json_maybe(plain.decode("utf-8", "replace"))
        except Exception:
            break
        if parsed is None:
            break
        data = parsed
    return data


def _fnos_error_code(payload):
    if isinstance(payload, list):
        for item in payload:
            code = _fnos_error_code(item)
            if code:
                return code
        return 0
    if not isinstance(payload, dict):
        return 0
    for key in ("errno", "code", "errcode", "err"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    for value in payload.values():
        code = _fnos_error_code(value)
        if code:
            return code
    return 0


def _fnos_split_endpoint(endpoint):
    parsed = urllib.parse.urlparse(str(endpoint or FNOS_DEFAULT_ENDPOINT))
    host = parsed.hostname or "127.0.0.1"
    secure = (parsed.scheme or "https").lower() != "http"
    port = parsed.port or (5667 if secure else 5666)
    return host, port, secure


def fnos_verify_password(endpoint, username, password, timeout=6):
    """调用飞牛本机登录接口校验账号密码，成功返回 (用户信息, "")"""
    host, port, secure = _fnos_split_endpoint(endpoint)
    candidates = [(host, 5666, False), (host, port, secure)]
    seen = set()
    last_error = ""
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        ws = None
        try:
            ws = FnosSocket.connect(candidate[0], candidate[1], candidate[2],
                                    "/websocket?type=main", timeout)
            ws.send(json.dumps({"req": "util.crypto.getRSAPub",
                                "reqid": secrets.token_hex(8)}, ensure_ascii=False))
            reply = _fnos_ws_read(ws)
            if reply is None:
                last_error = "未收到飞牛公钥"
                continue
            pubkey = _fnos_public_key(reply)
            if not pubkey:
                last_error = fnos_message(reply) or "飞牛未返回 RSA 公钥"
                continue
            si = _fnos_deep_value(reply, ("si",)) or ""
            aes_key = os.urandom(16)
            aes_iv = os.urandom(16)
            login = {
                "req": "user.login",
                "reqid": secrets.token_hex(8),
                "user": username,
                "password": password,
                "deviceType": "Browser",
                "deviceName": FNOS_DEVICE_NAME,
                "stay": False,
                "si": str(si),
            }
            plain = json.dumps(login, ensure_ascii=False).encode("utf-8")
            envelope = {
                "req": "encrypted",
                "reqid": secrets.token_hex(8),
                "iv": base64.b64encode(aes_iv).decode("ascii"),
                "rsa": base64.b64encode(_rsa_encrypt(pubkey, aes_key)).decode("ascii"),
                "aes": base64.b64encode(_aes_cbc_encrypt(aes_key, aes_iv, plain)).decode("ascii"),
            }
            ws.send(json.dumps(envelope, ensure_ascii=False))
            answer = _fnos_ws_read(ws)
            if answer is None:
                last_error = "飞牛登录服务无响应"
                continue
            data = _fnos_decrypt(answer, aes_key)
            info = fnos_find_user_info(data)
            if info:
                return info, ""
            code = _fnos_error_code(data) or _fnos_error_code(answer)
            text = fnos_message(data) or fnos_message(answer)
            if code in (131072, 131073):
                return {}, "飞牛账号或密码不正确"
            last_error = text or ("错误码 %d" % code if code else "飞牛登录失败")
        except Exception as exc:
            if isinstance(exc, OSError):
                last_error = "飞牛登录服务不可用"
            else:
                last_error = str(exc) or exc.__class__.__name__
        finally:
            if ws is not None:
                ws.close()
    return {}, "无法校验飞牛账号：%s" % (last_error or "连接失败")


def strip_ext(name):
    return os.path.splitext(name)[0]


def clean_name(name):
    """清理目录名/文件名中的序号、下划线与多余空白"""
    name = strip_ext(name)
    name = name.replace("_", " ").replace("\u3000", " ")
    name = re.sub(r"^\s*[\[\(（【]?\d{1,4}[\]\)）】]?[\.\-、,，:：\s]*", "", name)
    name = re.sub(r"\s+", " ", name).strip(" .-·")
    return name or strip_ext(name) or name


def fmt_ext(name):
    return os.path.splitext(name)[1].lower()


def is_audio(name):
    if name.startswith(".") or name.startswith("._"):
        return False
    return fmt_ext(name) in AUDIO_EXTS


def is_hidden_dir(name):
    return name.startswith(".") or name in SKIP_DIRS or name.startswith("@eaDir")


# --------------------------------------------------------------------------
# 字幕识别与解析（扫描阶段识别，播放按接口按需读取）
# --------------------------------------------------------------------------

SUB_CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?[,.](\d{1,3})")
LRC_TAG_RE = re.compile(r"\[([0-9]{1,3}):([0-9]{1,2})(?:[.:]([0-9]{1,3}))?\]")
LRC_META_RE = re.compile(r"^\[(ti|ar|al|by|offset|re|ve|tool|length|au):.*\]$", re.I)
SUB_TAG_RE = re.compile(r"<[^>]+>")


def read_text_any(path):
    """按 UTF-8 → GB18030 顺序尝试解码，返回 (文本, 编码名)"""
    try:
        with open(path, "rb") as fp:
            raw = fp.read()
    except OSError:
        return "", ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore"), "utf-8"


def subtitle_dir_cache(folder, cache):
    """列一次目录并缓存字幕/音频数量，扫描 7 万章也只 listdir 一次"""
    key = os.path.normcase(os.path.abspath(folder))
    cached = cache.get(key)
    if cached is not None:
        return cached
    subs, audio_count = [], 0
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        if name.startswith(".") or name.startswith("._"):
            continue
        ext = fmt_ext(name)
        if ext in SUB_EXTS:
            subs.append(name)
        elif ext in AUDIO_EXTS:
            audio_count += 1
    cache[key] = (subs, audio_count)
    return cache[key]


def find_subtitle(audio_path, cache=None):
    """章节字幕：同目录同名优先；目录里只有一个音频且只有一个字幕时兜底"""
    if not audio_path:
        return ""
    cache = cache if cache is not None else {}
    folder = os.path.dirname(audio_path)
    subs, audio_count = subtitle_dir_cache(folder, cache)
    if not subs:
        return ""
    stem = strip_ext(os.path.basename(audio_path)).lower()
    matched = [name for name in subs if strip_ext(name).lower() == stem]
    if matched:
        matched.sort(key=lambda name: SUB_EXT_ORDER.get(fmt_ext(name), 9))
        return os.path.join(folder, matched[0])
    if len(subs) == 1 and audio_count == 1:
        return os.path.join(folder, subs[0])
    return ""


def subtitle_seconds(text):
    """解析 00:00:01,000 / 00:01.000 等时间戳为秒"""
    match = SUB_CLOCK_RE.search(str(text or ""))
    if not match:
        return None
    first, second, third, fraction = match.groups()
    hours = int(first) if third is not None else 0
    minutes = int(second) if third is not None else int(first)
    seconds = int(third) if third is not None else int(second)
    return hours * 3600 + minutes * 60 + seconds + float("0." + fraction.ljust(3, "0")[:3])


def subtitle_clean(text):
    """去掉 SRT/VTT 内的标签与常见实体"""
    text = SUB_TAG_RE.sub("", text)
    return (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").strip())


def parse_lrc(text):
    """LRC：支持一行多时间戳，忽略 [ti:/ar:] 等元信息"""
    segments = []
    for line in text.splitlines():
        line = line.strip()
        if not line or LRC_META_RE.match(line):
            continue
        stamps = LRC_TAG_RE.findall(line)
        if not stamps:
            continue
        body = subtitle_clean(LRC_TAG_RE.sub("", line))
        if not body:
            continue
        for minute, second, fraction in stamps:
            value = int(minute) * 60 + int(second)
            if fraction:
                value += float("0." + fraction.ljust(3, "0")[:3])
            segments.append({"start": round(value, 3), "text": body})
    return segments


def subtitle_block_end(block):
    """解析一个 SRT/VTT 块，返回段落或 None"""
    index = next((i for i, line in enumerate(block) if "-->" in line), None)
    if index is None:
        return None
    left, _, right = block[index].partition("-->")
    start = subtitle_seconds(left)
    end = subtitle_seconds(right)
    body = " ".join(subtitle_clean(line) for line in block[index + 1:]).strip()
    if start is None or not body:
        return None
    return {"start": round(start, 3), "end": round(end, 3) if end else None, "text": body}


def parse_timed_subtitle(text):
    """SRT / VTT：按空行分块，兼容 WEBVTT 头与无序号块"""
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    segments, block = [], []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            if block:
                item = subtitle_block_end(block)
                if item:
                    segments.append(item)
                block = []
            continue
        block.append(line)
    if block:
        item = subtitle_block_end(block)
        if item:
            segments.append(item)
    return segments


def normalize_subtitle(segments):
    """补齐结束时间并按开始时间排序"""
    segments.sort(key=lambda item: float(item.get("start") or 0))
    result = []
    for index, item in enumerate(segments):
        start = float(item.get("start") or 0)
        end = item.get("end")
        if end is None or float(end) <= start:
            nxt = segments[index + 1].get("start") if index + 1 < len(segments) else None
            end = float(nxt) if nxt and float(nxt) > start else start + 4.0
        result.append({
            "start": round(start, 3),
            "end": round(float(end), 3),
            "text": item.get("text") or "",
        })
    return result


def parse_subtitle_file(path, ext=None):
    """读取并解析字幕文件，返回 (段落数组, 编码名)"""
    text, encoding = read_text_any(path)
    if not text.strip():
        return [], encoding
    kind = (ext or fmt_ext(path)).lower()
    segments = parse_lrc(text) if kind == ".lrc" else parse_timed_subtitle(text)
    return normalize_subtitle(segments), encoding


def find_cover(folder, prefer_names=()):
    """在书籍目录中查找封面图片：优先同名图片，其次通用封面文件名"""
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    images = sorted(
        (n for n in names if not n.startswith(".") and fmt_ext(n) in COVER_EXTS),
        key=natural_key,
    )
    if not images:
        return None
    for pref in prefer_names:
        stem = strip_ext(pref).strip().lower()
        if not stem:
            continue
        for name in images:
            if strip_ext(name).strip().lower() == stem:
                return os.path.join(folder, name)
    for name in images:
        if strip_ext(name).strip().lower() in COVER_STEMS:
            return os.path.join(folder, name)
    return None


def _join_people(value):
    if isinstance(value, list):
        return "、".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


def read_nfo_metadata(path):
    """解析 book.nfo（Kodi 风格 XML），返回标准化元数据"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {}

    def text_of(tag):
        for node in root.iter():
            if node.tag.split("}")[-1].lower() == tag:
                return (node.text or "").strip()
        return ""

    tags = []
    for node in root.iter():
        if node.tag.split("}")[-1].lower() == "tag" and (node.text or "").strip():
            tags.append(node.text.strip())
    return {
        "title": text_of("title"),
        "author": text_of("author"),
        "narrator": text_of("narrator"),
        "description": text_of("intro") or text_of("description") or text_of("plot"),
        "tags": tags,
        "cover_url": text_of("cover_url") or text_of("thumb") or text_of("cover"),
        "source": text_of("source"),
        "source_id": text_of("source_id"),
    }


def read_sidecar_metadata(folder):
    """读取书籍目录内的 metadata.json / book.nfo，返回标准化元数据（无则 {}）"""
    payload = {}
    json_path = os.path.join(folder, "metadata.json")
    if os.path.isfile(json_path):
        raw = load_json(json_path, None)
        if isinstance(raw, dict):
            payload = {
                "title": raw.get("title"),
                "author": _join_people(raw.get("authors") or raw.get("author")),
                "narrator": _join_people(raw.get("narrators") or raw.get("narrator")),
                "description": raw.get("description") or raw.get("intro"),
                "tags": raw.get("tags") or raw.get("genres"),
                "cover_url": raw.get("cover_url") or raw.get("cover"),
                "chapters": raw.get("chapters"),
            }
    if not (payload.get("title") or payload.get("cover_url")):
        nfo_path = os.path.join(folder, "book.nfo")
        if os.path.isfile(nfo_path):
            nfo = read_nfo_metadata(nfo_path)
            if nfo:
                payload = nfo
    if not payload:
        return {}
    tags = payload.get("tags")
    result = {
        "title": str(payload.get("title") or "").strip(),
        "author": str(payload.get("author") or "").strip(),
        "narrator": str(payload.get("narrator") or "").strip(),
        "description": str(payload.get("description") or "").strip(),
        "tags": _join_people(tags) if isinstance(tags, list) else str(tags or "").strip(),
        "cover_url": str(payload.get("cover_url") or "").strip(),
        "source": str(payload.get("source") or "").strip(),
        "external_id": str(payload.get("source_id") or "").strip(),
    }
    chapters = payload.get("chapters")
    if isinstance(chapters, list):
        titles = [str(c.get("title") or "").strip() for c in chapters if isinstance(c, dict)]
        if titles:
            result["chapter_titles"] = titles
    return result


def split_accessible(raw):
    """解析 TRIM_DATA_ACCESSIBLE_PATHS，兼容 JSON / 换行 / 冒号分隔"""
    if not raw:
        return []
    raw = raw.strip()
    paths = []
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        paths.append(item)
                    elif isinstance(item, dict):
                        for key in ("path", "dir", "name"):
                            if isinstance(item.get(key), str):
                                paths.append(item[key])
                                break
            if paths:
                return paths
        except ValueError:
            pass
    for line in raw.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            continue
        # 冒号分隔（避免误切 Windows 盘符与 URL）
        if ":" in line and not re.match(r"^[A-Za-z]:[\\/]", line):
            paths.extend([p.strip() for p in line.split(":") if p.strip()])
        else:
            paths.append(line)
    return paths


# --------------------------------------------------------------------------
# 本机网络地址（供手机端扫码连接）
# --------------------------------------------------------------------------

# 虚拟网卡（Docker/桥接等）不参与手机连接地址展示
VIRTUAL_IFACE_PREFIXES = ("docker", "veth", "br-", "virbr", "cni", "flannel", "vnet", "tap")


def is_virtual_iface(name):
    lowered = (name or "").lower()
    return any(lowered.startswith(prefix) for prefix in VIRTUAL_IFACE_PREFIXES)


def local_ipv4_addresses():
    """枚举本机 IPv4 地址；Linux 下用 ioctl 读取各网卡，其他平台回退到出口地址探测"""
    found = []
    seen = set()
    try:
        import fcntl
        import struct
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _idx, name in socket.if_nameindex():
                if is_virtual_iface(name):
                    continue
                try:
                    req = struct.pack("256s", name[:15].encode("utf-8"))
                    info = fcntl.ioctl(probe.fileno(), 0x8915, req)  # SIOCGIFADDR
                except OSError:
                    continue
                ip = socket.inet_ntoa(info[20:24])
                if ip.startswith("127.") or ip.startswith("169.254.") or ip in seen:
                    continue
                seen.add(ip)
                found.append({"iface": name, "ip": ip})
        finally:
            probe.close()
    except (ImportError, AttributeError, OSError):
        pass
    if not found:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("223.5.5.5", 53))  # 不实际发包，仅让内核选定出口地址
                ip = probe.getsockname()[0]
            finally:
                probe.close()
        except OSError:
            ip = ""
        if ip and not ip.startswith("127."):
            found.append({"iface": "", "ip": ip})
    return found


def _collect_ipv6(candidate, found):
    """校验并收集一个 IPv6 地址（跳过回环/链路本地/未指定）"""
    text = (candidate or "").split("%")[0].strip()
    try:
        addr = ipaddress.IPv6Address(text)
    except ValueError:
        return
    if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        return
    if str(addr) in found["seen"]:
        return
    found["seen"].add(str(addr))
    found["items"].append({"iface": found["iface"], "ip": str(addr)})


def local_ipv6_addresses():
    """多来源枚举本机 IPv6 全局地址：/proc → getaddrinfo → ip 命令"""
    result = {"items": [], "seen": set(), "iface": ""}

    # 1) 内核地址表（Linux 最可靠）
    try:
        with open("/proc/net/if_inet6", "r", encoding="utf-8") as fp:
            rows = [line.split() for line in fp]
    except OSError:
        rows = []
    for row in rows:
        if len(row) < 6:
            continue
        hexaddr, _ifindex, _plen, scope, _flags, name = row[:6]
        if scope == "20":  # 内核标记的链路本地地址
            continue
        result["iface"] = name
        _collect_ipv6(hexaddr, result)
        result["iface"] = ""

    # 2) 主机名解析兜底
    if not result["items"]:
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6)
        except OSError:
            infos = []
        for info in infos:
            _collect_ipv6(info[4][0], result)

    # 3) ip 命令兜底（部分精简系统 /proc 不可读）
    if not result["items"]:
        try:
            import subprocess
            out = subprocess.run(
                ["ip", "-6", "-o", "addr", "show", "scope", "global"],
                capture_output=True, text=True, timeout=5)
            for line in (out.stdout or "").splitlines():
                match = re.search(r"^(\d+):\s+(\S+).*inet6\s+([0-9a-fA-F:]+)/", line)
                if not match:
                    continue
                name = match.group(2)
                if name == "lo" or is_virtual_iface(name):
                    continue
                result["iface"] = name
                _collect_ipv6(match.group(3), result)
                result["iface"] = ""
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return result["items"]


def network_endpoints(port):
    """生成手机端可用的访问地址列表（IPv6 地址需用方括号包裹）"""
    urls = []
    seen = set()
    for item in local_ipv4_addresses():
        if item["ip"] in seen:
            continue
        seen.add(item["ip"])
        urls.append({
            "family": "ipv4",
            "iface": item["iface"],
            "ip": item["ip"],
            "url": "http://%s:%d" % (item["ip"], port),
        })
    # IPv6 排在前面：公网 IPv6 不经过 NAT，手机离开局域网也能直连
    ipv6_items = []
    for item in local_ipv6_addresses():
        if item["ip"] in seen:
            continue
        seen.add(item["ip"])
        ipv6_items.append({
            "family": "ipv6",
            "iface": item["iface"],
            "ip": item["ip"],
            "url": "http://[%s]:%d" % (item["ip"], port),
        })
    return ipv6_items + urls


def normalize_public_url(value):
    """规范化外网访问地址（DDNS 域名 / 公网 IPv6），允许只填主机"""
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.match(r"^https?://", text, re.I):
        text = "http://" + text
    return text.rstrip("/")


# --------------------------------------------------------------------------
# 在线刮削（喜马拉雅）
# --------------------------------------------------------------------------

def normalize_cover_url(value):
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    if value.startswith("//"):
        return "https:" + value
    if value.startswith("/"):
        return DEFAULT_SCRAPE_BASE.rstrip("/") + value
    return value


def image_host_allowed(url, extra_base=""):
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if any(host == item.lstrip(".") or host.endswith(item) for item in IMAGE_HOSTS):
        return True
    if extra_base:
        try:
            extra_host = (urllib.parse.urlparse(extra_base).hostname or "").lower()
        except ValueError:
            extra_host = ""
        if extra_host and host == extra_host:
            return True
    return False


def fetch_json(url, timeout=12, referer="", headers=None):
    merged = {
        "User-Agent": BROWSER_UA,
        "Referer": referer or DEFAULT_SCRAPE_BASE + "/",
        "Accept": "application/json, text/plain, */*",
    }
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, headers=merged)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "ignore"))


def extract_album_docs(payload):
    """从喜马拉雅搜索响应中提取专辑列表（兼容多种返回结构）"""
    if not isinstance(payload, dict):
        return []
    candidates = []

    def collect(node, depth=0):
        if depth > 4 or not isinstance(node, dict):
            return
        docs = node.get("docs")
        if isinstance(docs, list) and docs:
            candidates.append(docs)
        for key in ("data", "album", "result", "albumResult"):
            if isinstance(node.get(key), (dict, list)):
                collect(node[key], depth + 1)
        if isinstance(node.get("list"), list) and node["list"]:
            candidates.append(node["list"])

    collect(payload)

    def is_album(item):
        if not isinstance(item, dict):
            return False
        has_id = any(item.get(k) for k in ("albumId", "album_id", "id"))
        has_title = bool(item.get("title") or item.get("albumTitle") or item.get("name"))
        return has_id and has_title

    for docs in candidates:
        albums = [item for item in docs if is_album(item)]
        if albums:
            return albums
    return []


def album_to_item(doc):
    album_id = doc.get("albumId") or doc.get("album_id") or doc.get("id")
    title = doc.get("title") or doc.get("albumTitle") or doc.get("name") or ""
    author = (doc.get("nickname") or doc.get("author") or doc.get("announcer")
              or doc.get("userName") or doc.get("speaker") or "")
    intro = doc.get("intro") or doc.get("description") or doc.get("introRich") or ""
    intro = re.sub(r"<[^>]+>", "", str(intro)).strip()
    cover = normalize_cover_url(
        doc.get("coverPath") or doc.get("cover") or doc.get("coverUrl")
        or doc.get("image") or doc.get("albumCover") or ""
    )
    tags = doc.get("tags") or doc.get("categoryTitle") or doc.get("categoryName") or ""
    if isinstance(tags, list):
        tags = ",".join(str(t) for t in tags)
    cover_url = cover
    thumb = ""
    if cover:
        thumb = "/api/image-proxy?url=" + urllib.parse.quote(cover, safe="")
    return {
        "id": str(album_id),
        "source_id": "ximalaya",
        "title": str(title).strip(),
        "author": str(author).strip(),
        "narrator": str(author).strip(),
        "intro": intro,
        "description": intro,
        "cover_url": cover_url,
        "cover": cover_url,
        "thumb_url": thumb,
        "tags": str(tags).strip(),
        "publisher": str(author).strip(),
    }


def ximalaya_search(query, api_base="", limit=12):
    """调用喜马拉雅专辑搜索（seo 接口，按播放量排序），返回 (items, error)"""
    base = (api_base or DEFAULT_SCRAPE_BASE).rstrip("/")
    params = urllib.parse.urlencode({
        "kw": query,
        "core": "album",
        "page": 1,
        "rows": max(1, min(30, int(limit))),
        "condition": "relation",
        "spellchecker": "true",
        "device": "iPhone",
        "isGrayFilter": "true",
    })
    url = base + "/revision/search/seo?" + params
    try:
        payload = fetch_json(url, referer=base + "/")
    except urllib.error.HTTPError as exc:
        return [], "搜索接口返回 HTTP %s" % exc.code
    except Exception as exc:
        return [], "无法连接搜索接口: %s" % exc
    if isinstance(payload, dict):
        data = payload.get("data")
        reason = data.get("reason") if isinstance(data, dict) else ""
        if payload.get("needLogin") or reason == "risk invalid":
            return [], "喜马拉雅接口触发风控校验，请稍后重试"
    docs = extract_album_docs(payload)
    items = []
    for doc in docs:
        item = album_to_item(doc)
        if not item["id"] or not item["title"]:
            continue
        try:
            item["play_count"] = int(doc.get("playCount") or doc.get("play_count") or 0)
        except (TypeError, ValueError):
            item["play_count"] = 0
        items.append(item)
    items.sort(key=lambda item: item.get("play_count", 0), reverse=True)
    return items[:limit], None


def lanren_search(query, limit=12):
    """懒人听书搜索（HTML 解析），返回 (items, error)"""
    url = "https://www.lrts.me/search/book/1/%s" % urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Referer": "https://www.lrts.me/",
        "Accept": "text/html,*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except Exception as exc:
        return [], "无法连接懒人听书: %s" % exc
    items = []
    for li in re.findall(r'<li class="book-item".*?</li>', html, re.DOTALL):
        m_id = re.search(r'href="/book/(\d+)"', li)
        m_cover = re.search(r'<img src="([^"]+)"', li)
        m_title = re.search(r'class="book-item-name"[^>]*>([^<]+)<', li)
        if not (m_id and m_title):
            continue
        narrators = re.findall(r'class="g-user-shutdown"[^>]*>([^<]+)<', li)
        m_author = re.search(r'class="author"[^>]*>([^<]*)<', li)
        m_desc = re.search(r'class="book-item-desc[^"]*"[^>]*>([^<]*)<', li)
        cover = m_cover.group(1).strip() if m_cover else ""
        if cover.startswith("//"):
            cover = "https:" + cover
        author = m_author.group(1).strip() if m_author else ""
        narrator = narrators[1].strip() if len(narrators) > 1 else (narrators[0].strip() if narrators else "")
        intro = (m_desc.group(1).strip() if m_desc else "")
        intro = re.sub(r"<[^>]+>", "", intro).strip()
        items.append({
            "id": m_id.group(1),
            "source_id": "lanren",
            "title": m_title.group(1).strip(),
            "author": author,
            "narrator": narrator,
            "intro": intro,
            "description": intro,
            "cover_url": cover,
            "cover": cover,
            "thumb_url": ("/api/image-proxy?url=" + urllib.parse.quote(cover, safe="")) if cover else "",
            "tags": "",
            "publisher": narrator,
            "play_count": 0,
        })
    return items[:limit], None


MISSEVAN_BASE = "https://www.missevan.com"


def missevan_episode_ordinal(name):
    """猫耳集数多用中文数字（第一集/第十七集），单独解析，避免影响其它平台"""
    m = re.search(r"第\s*([零〇一二三四五六七八九十百千0-9]{1,6})\s*[集章回话期]", str(name or ""))
    if not m:
        return None
    return cn_number(m.group(1))


def missevan_search(query, limit=12):
    """猫耳 FM 广播剧搜索，返回 (items, error)"""
    url = MISSEVAN_BASE + "/dramaapi/search?s=%s&page=1" % urllib.parse.quote(query)
    try:
        payload = fetch_json(url, referer=MISSEVAN_BASE + "/")
    except urllib.error.HTTPError as exc:
        return [], "猫耳搜索返回 HTTP %s" % exc.code
    except Exception as exc:
        text = str(exc).lower()
        if "timed out" in text or "timeout" in text:
            return [], "连接猫耳超时，请稍后重试"
        return [], "无法连接猫耳：%s" % exc
    info = payload.get("info") if isinstance(payload, dict) else None
    datas = info.get("Datas") if isinstance(info, dict) else None
    if not isinstance(datas, list):
        return [], "猫耳搜索返回结构异常（缺少 info.Datas）"
    items = []
    for drama in datas:
        if not isinstance(drama, dict):
            continue
        did = str(drama.get("id") or "").strip()
        title = str(drama.get("name") or "").strip()
        if not did or not title:
            continue
        cover = str(drama.get("cover") or "").strip()
        items.append({
            "id": did,
            "source_id": "missevan",
            "source": "missevan",
            "title": title,
            "author": str(drama.get("author") or "").strip(),
            "narrator": "",
            "intro": "",
            "description": "",
            "cover_url": cover,
            "cover": cover,
            "thumb_url": ("/api/image-proxy?url=" + urllib.parse.quote(cover, safe="")) if cover else "",
            "tags": "",
            "publisher": "",
            "play_count": 0,
        })
    return items[:limit], None


def missevan_drama_chapter_list(drama_id):
    """猫耳广播剧全量集数，返回 (items, error, complete)"""
    url = MISSEVAN_BASE + "/dramaapi/getdrama?drama_id=%s" % urllib.parse.quote(str(drama_id))
    try:
        payload = fetch_json(url, referer=MISSEVAN_BASE + "/")
    except urllib.error.HTTPError as exc:
        return [], "猫耳详情返回 HTTP %s" % exc.code, False
    except Exception as exc:
        text = str(exc).lower()
        if "timed out" in text or "timeout" in text:
            return [], "连接猫耳超时，请稍后重试", False
        return [], "无法连接猫耳：%s" % exc, False
    info = payload.get("info") if isinstance(payload, dict) else None
    episodes = info.get("episodes") if isinstance(info, dict) else None
    batch = episodes.get("episode") if isinstance(episodes, dict) else None
    if not isinstance(batch, list):
        return [], "猫耳详情返回结构异常（缺少 info.episodes.episode）", False
    items = []
    for index, ep in enumerate(batch):
        if not isinstance(ep, dict):
            continue
        name = str(ep.get("name") or "").strip()
        if not name:
            continue
        items.append({
            "order": index + 1,
            "ordinal": missevan_episode_ordinal(name),
            "title": name,
        })
    return items, None, True


def missevan_drama_detail(drama_id):
    """猫耳广播剧详情（名称/作者/封面/简介），返回 (detail, error)"""
    url = MISSEVAN_BASE + "/dramaapi/getdrama?drama_id=%s" % urllib.parse.quote(str(drama_id))
    try:
        payload = fetch_json(url, referer=MISSEVAN_BASE + "/")
    except Exception as exc:
        return {}, friendly_net_error(exc)
    info = payload.get("info") if isinstance(payload, dict) else None
    drama = info.get("drama") if isinstance(info, dict) else None
    if not isinstance(drama, dict):
        return {}, "猫耳详情返回结构异常（缺少 info.drama）"
    raw_intro = (drama.get("abstract") or drama.get("introduction") or drama.get("intro")
                 or drama.get("description") or "")
    description = html.unescape(re.sub(r"<[^>]+>", " ", str(raw_intro))).strip()
    return {
        "title": str(drama.get("name") or "").strip(),
        "author": str(drama.get("author") or "").strip(),
        "cover": str(drama.get("cover") or "").strip(),
        "description": description,
    }, None


PLATFORM_TTL = 24 * 3600
PLATFORM_MATCH_MIN = 0.88

PLATFORM_MARKETING_RE = re.compile(
    r"(免费|多人|有声小说|有声剧|小说|剧场|推荐|完结|完本|精选|广播剧|精品|听书|评书)", re.I)


def match_text(text):
    """归一化书名/作者，去掉标点、空白与音译符号后再比较"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", str(text or "").lower())


def strip_decorations(text):
    """去掉书名里的【】（）方括号块与常见营销词"""
    raw = re.sub(r"【[^】]*】|（[^）]*）|\([^)]*\)|\[[^\]]*\]", " ", str(text or ""))
    return re.sub(PLATFORM_MARKETING_RE, " ", raw)


def title_segments(text):
    raw = strip_decorations(text)
    return [part.strip() for part in re.split(r"[丨|｜/\\\-—_:：,，、·&＋+~～]", raw) if part.strip()]


def core_title(text):
    """书名核心：首个有意义的片段，通常是书名本身"""
    segments = title_segments(text)
    for segment in segments:
        if len(match_text(segment)) >= 2:
            return segment
    return segments[0] if segments else str(text or "").strip()


def _contain_score(left, right):
    if not left or not right:
        return 0.0
    if left in right or right in left:
        shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
        if len(shorter) >= 3 and len(shorter) / float(len(longer)) >= 0.6:
            return 1.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def text_similarity(left, right):
    """书名相似度：核心书名 65% + 完整标题 35%，防止靠通用短语串书"""
    left_core = match_text(core_title(left))
    right_core = match_text(core_title(right))
    left_full = match_text(strip_decorations(left))
    right_full = match_text(strip_decorations(right))
    return round(_contain_score(left_core, right_core) * 0.65 + _contain_score(left_full, right_full) * 0.35, 4)


def search_queries(title):
    """生成用于平台检索的关键词：核心书名优先，再退回完整书名"""
    queries = []
    core = core_title(title).strip()
    if core:
        queries.append(core)
    full = str(title or "").strip()
    if full and full not in queries:
        queries.append(full)
    return queries


def rank_platform_items(title, author, items):
    """按书名+作者给平台结果打分，返回最优项（分数, 书名分, 条目）"""
    best = None
    for item in items:
        title_score = text_similarity(title, item.get("title"))
        item_author = clean_author(item.get("author"))
        score = title_score
        if author and item_author:
            author_score = text_similarity(author, item_author)
            if author_score < 0.5:
                continue
            score = title_score * 0.85 + author_score * 0.15
        key = (round(score, 4), round(title_score, 4))
        if best is None or key > best[0]:
            best = (key, item, score, title_score)
    return best


def clean_author(value):
    text = str(value or "").strip()
    return "" if text.lower() in ("unknown", "未知", "佚名") else text


def lanren_book_status(book_id):
    """懒人听书详情页：解析「N集 + 已完结/连载中」"""
    url = "https://www.lrts.me/book/" + str(book_id)
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Referer": "https://www.lrts.me/",
        "Accept": "text/html,*/*",
    })
    with urllib.request.urlopen(req, timeout=12) as resp:
        html = resp.read().decode("utf-8", "ignore")
    text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    count = None
    match = re.search(r"(\d{1,6})\s*集", text)
    if match:
        try:
            count = int(match.group(1)) or None
        except (TypeError, ValueError):
            count = None
    if "已完结" in text:
        finished = True
    elif "连载中" in text:
        finished = False
    else:
        finished = None
    return {"finished": finished, "count": count}


def ximalaya_album_tracks(album_id, api_base=""):
    """喜马拉雅专辑详情只取总集数；isFinished 语义不可靠，不用于连载判断"""
    base = (api_base or DEFAULT_SCRAPE_BASE).rstrip("/")
    payload = fetch_json(base + "/revision/album?albumId=" + str(album_id), referer=base + "/")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    tracks = data.get("tracksInfo") or {}
    try:
        count = int(tracks.get("trackTotalCount") or 0) or None
    except (TypeError, ValueError):
        count = None
    return {"finished": None, "count": count}


CHAPTER_TITLE_NUM_RE = re.compile(r"(?:第\s*)?([0-9]{1,5})\s*[集章回话节篇]")
CHAPTER_TITLE_DASH_NUM_RE = re.compile(r"\s([0-9]{1,5})\s*[-–—－]\s*")
CHAPTER_LEADING_NUM_RE = re.compile(r"^\s*[\[\(（【]?\s*([0-9]{1,5})")


def chapter_ordinal_from_title(name):
    """从章节标题/文件名中解析序号（优先「第N集/章」，其次「N-」写法，最后开头数字）"""
    base = strip_ext(os.path.basename(str(name or "")))
    m = CHAPTER_TITLE_NUM_RE.search(base)
    if m:
        return int(m.group(1))
    m = CHAPTER_TITLE_DASH_NUM_RE.search(base)
    if m:
        return int(m.group(1))
    m = CHAPTER_LEADING_NUM_RE.match(base)
    if m:
        return int(m.group(1))
    return None


def ximalaya_album_chapter_list(album_id, page_size=100, max_pages=60):
    """喜马拉雅：匿名可用的移动端曲目接口，按 pageId 翻页取全量。
    返回 (items, error, complete)，items=[{order, ordinal, title}]。
    付费/受限专辑该接口会返回 924，此时退回专辑详情接口（最多 100 条）。
    """
    items = []
    error = ""
    complete = True
    total = None
    for page in range(1, max_pages + 1):
        url = ("https://mobile.ximalaya.com/mobile/v1/album/track?albumId=%s"
               "&device=wp&isAsc=true&pageId=%d&pageSize=%d"
               % (urllib.parse.quote(str(album_id)), page, page_size))
        try:
            payload = fetch_json(url, referer="https://www.ximalaya.com/")
        except Exception as exc:
            error = friendly_net_error(exc)
            complete = False
            break
        if payload.get("ret") != 0:
            error = str(payload.get("msg") or "喜马拉雅曲目接口拒绝匿名访问")
            complete = False
            break
        data = payload.get("data") or {}
        tracks = data.get("tracks") if isinstance(data.get("tracks"), dict) else data
        batch = tracks.get("list") or []
        if total is None:
            total = tracks.get("totalCount")
        if not batch:
            break
        for track in batch:
            title = track.get("title") or track.get("trackName") or ""
            items.append({
                "order": track.get("orderNo"),
                "ordinal": chapter_ordinal_from_title(title),
                "title": title.strip(),
            })
        if isinstance(total, int) and len(items) >= total:
            break
        if len(batch) < page_size:
            break
    if items and not error:
        items.sort(key=lambda it: (it.get("order") is None, it.get("order") if isinstance(it.get("order"), int) else 0))
    if not items:
        # 受限专辑：退回移动端专辑详情（匿名最多 100 条）
        try:
            payload = fetch_json("https://mobile.ximalaya.com/mobile/v1/album?albumId=%s&pageSize=100"
                                 % urllib.parse.quote(str(album_id)),
                                 referer="https://www.ximalaya.com/")
            data = payload.get("data") or {}
            tracks = data.get("tracks") if isinstance(data.get("tracks"), dict) else data
            for track in tracks.get("list") or []:
                title = track.get("title") or track.get("trackName") or ""
                items.append({"order": track.get("orderNo"),
                              "ordinal": chapter_ordinal_from_title(title),
                              "title": title.strip()})
            items.sort(key=lambda it: (it.get("order") is None, it.get("order") if isinstance(it.get("order"), int) else 0))
            complete = False
            if not error:
                error = "喜马拉雅该专辑仅公开前 %d 条曲目" % len(items)
        except Exception as exc:
            error = error or friendly_net_error(exc)
    return items, error, complete


def lanren_book_chapter_list(book_id, page_size=50, max_pages=80):
    """懒人听书：getBookMenu 接口按 pageNum 翻页取全量章节标题。"""
    items = []
    error = ""
    complete = True
    total = None
    for page in range(1, max_pages + 1):
        url = ("https://m.lrts.me/ajax/getBookMenu?bookId=%s&pageNum=%d&pageSize=%d"
               % (urllib.parse.quote(str(book_id)), page, page_size))
        try:
            payload = fetch_json(url, referer="https://m.lrts.me/book/%s" % book_id,
                                 headers={"X-Requested-With": "XMLHttpRequest",
                                          "Origin": "https://m.lrts.me",
                                          "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                                                         "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1")})
        except Exception as exc:
            error = friendly_net_error(exc)
            complete = False
            break
        batch = payload.get("list") or []
        if total is None:
            total = payload.get("sections")
        if not batch:
            break
        for item in batch:
            title = item.get("name") or item.get("title") or ""
            items.append({"order": item.get("section"),
                          "ordinal": chapter_ordinal_from_title(title),
                          "title": title.strip()})
        if isinstance(total, int) and len(items) >= total:
            break
        if len(batch) < page_size:
            break
    if items:
        items.sort(key=lambda it: (it.get("order") is None, it.get("order") if isinstance(it.get("order"), int) else 0))
    return items, error, complete


def align_chapter_titles(chapters, platform_items):
    """把本地章节与平台曲目对齐；不串章优先。
    返回 (mapping, strategy, warning)，mapping=[{index, old, title, ordinal}]。
    """
    local = []
    for idx, chapter in enumerate(chapters):
        name = chapter.get("file_name") or chapter.get("filename") or chapter.get("title") or ""
        local.append({"index": idx, "old": chapter.get("title") or "",
                      "ordinal": chapter_ordinal_from_title(name)})
    if not local or not platform_items:
        return [], "", "本地或平台章节列表为空"

    local_ords = [it["ordinal"] for it in local]
    local_unique = (all(ordinal is not None for ordinal in local_ords)
                    and len(set(local_ords)) == len(local_ords))
    numbered_platform = [it for it in platform_items if it.get("ordinal") is not None]

    if numbered_platform:
        if not local_unique:
            return [], "", ("平台章节带集号，但本地章节集号缺失或重复，"
                            "无法安全对齐，为避免串章未改动")
        by_ordinal = {}
        duplicated = False
        for it in numbered_platform:
            if it["ordinal"] in by_ordinal:
                duplicated = True
                continue
            by_ordinal[it["ordinal"]] = it
        mapping = []
        matched = 0
        for it in local:
            hit = by_ordinal.get(it["ordinal"])
            if hit and hit.get("title"):
                matched += 1
                if hit["title"] != it["old"]:
                    mapping.append({"index": it["index"], "old": it["old"],
                                    "title": hit["title"], "ordinal": it["ordinal"]})
        enough = matched >= min(3, len(local)) or matched == len(local)
        if not enough:
            return [], "", "按集号只匹配到 %d/%d 条，为避免串章未改动" % (matched, len(local))
        strategy = "按集号对齐（解析标题中的「第N集」），平台未标注集号的条目已跳过"
        notes = []
        if len(platform_items) != len(local):
            notes.append("本地 %d 条 / 平台 %d 条不相等，已按集号对齐，多出的平台条目忽略" % (
                len(local), len(platform_items)))
        skipped = len(platform_items) - len(numbered_platform)
        if skipped:
            notes.append("平台有 %d 条未标注集号（预告/花絮等），已跳过不套用" % skipped)
        if duplicated:
            notes.append("平台存在重复集号，重复项已忽略")
        return mapping, strategy, "；".join(notes)

    if len(platform_items) == len(local):
        mapping = []
        for it, plat in zip(local, platform_items):
            if plat.get("title") and plat["title"] != it["old"]:
                mapping.append({"index": it["index"], "old": it["old"],
                                "title": plat["title"], "ordinal": it["ordinal"]})
        return mapping, "条数一致且平台标题无集号，按顺序一一对齐", ""

    return [], "", ("条数不一致（本地 %d / 平台 %d）且平台标题缺少可靠集号，"
                    "为避免串章未改动" % (len(local), len(platform_items)))


def friendly_net_error(text):
    """把 urllib 的原始报错翻译成用户能看懂的提示"""
    text = str(text or "")
    if "Connection refused" in text or "拒绝" in text:
        return "连接被拒绝，请检查接口地址与端口是否正确"
    if "timed out" in text or "超时" in text:
        return "连接超时，NAS 可能无法访问外网"
    if "getaddrinfo" in text or "Name or service not known" in text:
        return "域名解析失败，请检查 NAS 的 DNS 设置"
    if "CERTIFICATE" in text or "certificate" in text:
        return "HTTPS 证书校验失败"
    return text


def download_image(url, dest):
    """下载封面到本地，返回 (真实路径, 错误)"""
    target = urllib.parse.urlparse(url).netloc.lower()
    if "lrts" in target:
        referer = "https://www.lrts.me/"
    elif "missevan" in target or "maoercdn" in target:
        referer = MISSEVAN_BASE + "/"
    else:
        referer = DEFAULT_SCRAPE_BASE + "/"
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Referer": referer})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except Exception as exc:
        return None, "封面下载失败: %s" % exc
    if not body:
        return None, "封面内容为空"
    ext = ".jpg"
    if "png" in ctype:
        ext = ".png"
    elif "webp" in ctype:
        ext = ".webp"
    elif "gif" in ctype:
        ext = ".gif"
    else:
        guessed = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
        if guessed in COVER_EXTS:
            ext = guessed
    if not body.startswith(b"\xff\xd8") and not body.startswith(b"\x89PNG") \
            and not body.startswith(b"RIFF") and not body.startswith(b"GIF8"):
        return None, "返回内容不是图片"
    final = os.path.splitext(dest)[0] + ext
    os.makedirs(os.path.dirname(final), exist_ok=True)
    with open(final, "wb") as fp:
        fp.write(body)
    return final, None


# --------------------------------------------------------------------------
# 刮削源（.tr / TRPACK：8 字节头 + zstd + tar）
# --------------------------------------------------------------------------

def _zstd_with_ctypes(data):
    """调用系统 libzstd 流式解压（fnOS 通常自带 libzstd.so.1）"""
    import ctypes
    import ctypes.util

    lib = None
    for name in ("libzstd.so.1", "libzstd.so", "libzstd.1.dylib",
                 "libzstd.dylib", "zstd.dll", "libzstd.dll"):
        try:
            lib = ctypes.CDLL(name)
            break
        except OSError:
            lib = None
    if lib is None:
        found = ctypes.util.find_library("zstd")
        if found:
            try:
                lib = ctypes.CDLL(found)
            except OSError:
                lib = None
    if lib is None:
        raise RuntimeError(
            "服务端缺少 zstd 解压能力（安装 zstandard 或系统 libzstd 后重试）")

    class InBuffer(ctypes.Structure):
        _fields_ = [("src", ctypes.c_void_p), ("size", ctypes.c_size_t),
                    ("pos", ctypes.c_size_t)]

    class OutBuffer(ctypes.Structure):
        _fields_ = [("dst", ctypes.c_void_p), ("size", ctypes.c_size_t),
                    ("pos", ctypes.c_size_t)]

    lib.ZSTD_createDStream.restype = ctypes.c_void_p
    lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
    lib.ZSTD_initDStream.restype = ctypes.c_size_t
    lib.ZSTD_decompressStream.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(OutBuffer), ctypes.POINTER(InBuffer)]
    lib.ZSTD_decompressStream.restype = ctypes.c_size_t
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
    lib.ZSTD_getErrorName.restype = ctypes.c_char_p

    source = ctypes.create_string_buffer(data, len(data))
    in_buf = InBuffer(ctypes.cast(source, ctypes.c_void_p), len(data), 0)
    out_buf = ctypes.create_string_buffer(1 << 16)
    ctx = lib.ZSTD_createDStream()
    if not ctx:
        raise RuntimeError("libzstd 初始化失败")
    chunks = []
    try:
        lib.ZSTD_initDStream(ctx)
        while True:
            out_ready = OutBuffer(ctypes.cast(out_buf, ctypes.c_void_p),
                                  len(out_buf), 0)
            code = lib.ZSTD_decompressStream(
                ctx, ctypes.byref(out_ready), ctypes.byref(in_buf))
            if lib.ZSTD_isError(code):
                raise RuntimeError(
                    lib.ZSTD_getErrorName(code).decode("utf-8", "ignore"))
            if out_ready.pos:
                chunks.append(out_buf.raw[:out_ready.pos])
            if code == 0 or in_buf.pos >= in_buf.size:
                break
    finally:
        lib.ZSTD_freeDStream(ctx)
    return b"".join(chunks)


def zstd_decompress(data):
    """按可用实现解压 zstd 数据，失败抛出 RuntimeError"""
    try:
        import zstandard  # 可选依赖：开发环境 / 已安装 pip 包
    except ImportError:
        zstandard = None
    if zstandard is not None:
        try:
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as reader:
                return reader.read()
        except Exception:
            pass
    try:
        from compression import zstd as std_zstd  # Python 3.14+
    except Exception:
        std_zstd = None
    if std_zstd is not None:
        try:
            return std_zstd.decompress(data)
        except Exception:
            pass
    try:
        return _zstd_with_ctypes(data)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("zstd 解压失败: %s" % exc)


def parse_plugin_yml(text):
    """解析 plugin.yml 中本服务需要的字段（轻量实现，无需 YAML 库）"""
    text = text.replace("\r\n", "\n")

    def grab(key):
        match = re.search(r"^%s:\s*(.*)$" % key, text, re.M)
        return match.group(1).strip().strip("'\"") if match else ""

    zh = ""
    match = re.search(r"^description:\s*\n\s+zh:\s*(.+)$", text, re.M)
    if not match:
        match = re.search(r"^description:\s*(.+)$", text, re.M)
    if match:
        zh = match.group(1).strip().strip("'\"")
    domains = re.findall(
        r"-\s*type:\s*network_access\s*\n\s+domain:\s*([^\s]+)", text)
    return {
        "id": grab("id"),
        "name": grab("name"),
        "version": grab("version"),
        "author": grab("author"),
        "runtime": grab("runtime") or "wasm",
        "entry_point": grab("entry_point"),
        "description": zh,
        "domains": domains,
    }


def parse_trpack(raw):
    """解析 .tr 刮削源包，返回 (plugin_info, error)"""
    if len(raw) < 12 or not raw.startswith(TRPACK_MAGIC):
        return None, "不是有效的刮削源（缺少 TRPACK 文件头）"
    body = raw[8:]
    if body.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            body = zstd_decompress(body)
        except RuntimeError as exc:
            return None, str(exc)
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tar:
            members = {}
            for member in tar.getmembers():
                name = member.name
                if name.startswith("./"):
                    name = name[2:]
                if member.isfile():
                    members[name] = member
            pkg_member = members.get(".trpack/package.json") or members.get("package.json")
            yml_member = members.get("plugin.yml") or members.get(".trpack/plugin.yml")
            if pkg_member is None or yml_member is None:
                return None, "刮削源包缺少 package.json 或 plugin.yml"
            package = json.loads(tar.extractfile(pkg_member).read().decode("utf-8"))
            yml_text = tar.extractfile(yml_member).read().decode("utf-8", "ignore")
            verified = True
            for item in package.get("files") or []:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "")
                member = members.get(path)
                if member is None:
                    verified = False
                    continue
                digest = hashlib.sha256()
                stream = tar.extractfile(member)
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
                if digest.hexdigest() != item.get("sha256"):
                    verified = False
            signed = ".trpack/signature.json" in members
            signature = {}
            if signed:
                try:
                    signature = json.loads(
                        tar.extractfile(members[".trpack/signature.json"]).read().decode("utf-8"))
                except (ValueError, KeyError):
                    signature = {}
    except (tarfile.TarError, ValueError, OSError, EOFError) as exc:
        return None, "刮削源包解析失败: %s" % exc

    yml = parse_plugin_yml(yml_text)
    plugin_id = str(package.get("plugin_id") or yml.get("id") or "").strip()
    if not PLUGIN_ID_RE.match(plugin_id):
        return None, "刮削源标识无效: %s" % (plugin_id or "（空）")
    info = {
        "id": plugin_id,
        "name": plugin_label(plugin_id, yml.get("name")),
        "vendor_name": yml.get("name") or plugin_id,
        "version": str(package.get("plugin_version") or yml.get("version") or ""),
        "runtime": str(package.get("runtime") or yml.get("runtime") or "wasm"),
        "entry_point": str(package.get("entry_point") or yml.get("entry_point") or ""),
        "author": yml.get("author") or "",
        "description": yml.get("description") or "",
        "domains": yml.get("domains") or [],
        "format": str(package.get("format") or ""),
        "verified": verified,
        "signed": signed,
        "key_id": str(signature.get("key_id") or ""),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return info, None



# --------------------------------------------------------------------------
# 应用状态：书库扫描、进度、设置
# --------------------------------------------------------------------------

class AppState(object):
    def __init__(self):
        self.data_dir = resolve_data_dir()
        self.library_path = os.path.join(self.data_dir, "library.json")
        self.progress_path = os.path.join(self.data_dir, "progress.json")
        self.settings_path = os.path.join(self.data_dir, "settings.json")
        self.metadata_path = os.path.join(self.data_dir, "metadata.json")
        self.platform_path = os.path.join(self.data_dir, "platform_status.json")
        self.cover_dir = os.path.join(self.data_dir, "covers")
        self.users_path = os.path.join(self.data_dir, "users.json")
        self.sessions_path = os.path.join(self.data_dir, "sessions.json")
        self.playlists_path = os.path.join(self.data_dir, "playlists.json")
        self.avatar_dir = os.path.join(self.data_dir, "avatars")
        self.scraper_dir = os.path.join(self.data_dir, "scrapers")
        self.scrapers_path = os.path.join(self.scraper_dir, "registry.json")
        self.bgm_dir = os.path.join(self.data_dir, "bgm")
        for path in (self.cover_dir, self.avatar_dir, self.scraper_dir, self.bgm_dir):
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                pass

        self.lock = threading.RLock()
        self.scanning = False
        self.scan_error = ""
        self.scan_started_at = 0
        self.scan_finished_at = 0
        self.scan_root_label = ""
        self.scan_thread = None
        self.scan_report = {"scanned": [], "skipped": [], "duplicates": 0}
        self._meta_dirty = False

        self.settings = load_json(self.settings_path, {})
        self.settings.setdefault("extra_roots", [])
        self.settings.setdefault("scrape", {})
        self._migrate_roots()
        self.metadata = load_json(self.metadata_path, {})
        self.metadata.setdefault("books", {})
        self.platform = load_json(self.platform_path, {})
        self.platform.setdefault("books", {})
        self.platform.setdefault("updated_at", 0)
        self.status_progress = {"running": False, "done": 0, "total": 0, "updated_at": 0}
        self._status_thread = None
        self.progress = load_json(self.progress_path, {})
        self.progress.setdefault("books", {})
        self.progress.setdefault("favorites", [])
        self.progress.setdefault("last_played", None)

        self.users = load_json(self.users_path, {})
        self.users.setdefault("users", [])
        self.sessions = load_json(self.sessions_path, {})
        # 二维码一次性登录票据：ticket -> {user_id, expires}
        self.login_tickets = {}
        # 飞牛 ID 待授权会话：state -> {verifier, endpoint, created}
        self.fnos_pending = {}
        self.seed_users()

        self.scrapers = load_json(self.scrapers_path, {})
        self.scrapers.setdefault("plugins", [])

        self.playlists = load_json(self.playlists_path, {})
        self.playlists.setdefault("playlists", [])

        library = load_json(self.library_path, {})
        self.books = library.get("books", [])
        self.roots = library.get("roots", [])
        self.scanned_at = library.get("scanned_at", 0)
        self.books_by_id = {book["id"]: book for book in self.books}

    # ---------------- 路径 ----------------

    def accessible_paths(self):
        raw = os.environ.get("TRIM_DATA_ACCESSIBLE_PATHS", "")
        if not raw:
            path_file = os.path.join(self.data_dir, "accessible_paths")
            if os.path.exists(path_file):
                try:
                    with open(path_file, "r", encoding="utf-8") as fp:
                        raw = fp.read()
                except OSError:
                    raw = ""
        return split_accessible(raw)

    def _migrate_roots(self):
        """目录角色迁移：旧版只有 extra_roots / bgm_dir，启动时幂等补成 roots 列表"""
        raw = self.settings.get("roots")
        entries, changed = [], False
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    entries.append({"path": item.strip(), "role": "book"})
                elif isinstance(item, dict):
                    path = str(item.get("path") or "").strip()
                    role = item.get("role")
                    if role not in ("book", "bgm", "media"):
                        role = "book"
                    if path:
                        entries.append({"path": path, "role": role})
        else:
            changed = True
            for path in self.settings.get("extra_roots") or []:
                path = str(path or "").strip()
                if path:
                    entries.append({"path": path, "role": "book"})
            bgm = str(self.settings.get("bgm_dir") or "").strip()
            if bgm:
                entries.append({"path": bgm, "role": "bgm"})
        # 同一路径只保留第一次出现的角色，避免一目录两角色导致扫描规则冲突
        deduped, seen = [], set()
        for entry in entries:
            key = os.path.normcase(os.path.abspath(entry["path"]))
            if key in seen:
                changed = True
                continue
            seen.add(key)
            deduped.append(entry)
        if deduped != entries:
            changed = True
        self.settings["roots"] = deduped
        book_paths = [entry["path"] for entry in deduped if entry["role"] in ("book", "media")]
        if self.settings.get("extra_roots") != book_paths:
            self.settings["extra_roots"] = book_paths
            changed = True
        if changed:
            self.save_settings()

    def root_entries(self):
        """目录角色列表：[{path, role}]，role 为 book / bgm / media"""
        return list(self.settings.get("roots") or [])

    def resolve_roots(self):
        """有声书扫描根：飞牛授权目录 + 角色为 book/media 的目录"""
        candidates = []
        candidates.extend(self.accessible_paths())
        if not candidates:
            # 仅在未配置授权目录时回退到共享根目录，避免扫描整个 NAS
            for key in ("FNPATH_SHARES_DIR", "TRIM_SHARES_DIR"):
                value = os.environ.get(key)
                if value:
                    candidates.extend(split_accessible(value))
            appdest = os.environ.get("TRIM_APPDEST", "")
            if appdest:
                candidates.append(os.path.join(appdest, "shares"))
        candidates.extend(entry["path"] for entry in self.root_entries()
                          if entry.get("role") in ("book", "media"))

        roots, seen = [], set()
        for path in candidates:
            if not path:
                continue
            path = os.path.abspath(os.path.expanduser(path.strip()))
            if path in seen or not os.path.isdir(path):
                continue
            seen.add(path)
            roots.append(path)
        return roots

    def book_skip_dirs(self):
        """有声书扫描必须整棵剪枝的目录：bgm 角色目录 + 当前背景音乐目录"""
        skip = set()
        for entry in self.root_entries():
            if entry.get("role") == "bgm":
                skip.add(os.path.normcase(os.path.realpath(entry["path"])))
        bgm = self.bgm_dir_current()
        if bgm:
            skip.add(os.path.normcase(os.path.realpath(bgm)))
        return skip

    def bgm_roots(self):
        """背景音乐目录：bgm/media 角色目录 + 当前选择的音乐目录"""
        bases, seen = [], set()
        for entry in self.root_entries():
            if entry.get("role") not in ("bgm", "media"):
                continue
            real = os.path.realpath(entry["path"])
            if os.path.isdir(real) and real not in seen:
                seen.add(real)
                bases.append(real)
        current = self.bgm_dir_current()
        if current and current not in seen:
            seen.add(current)
            bases.append(current)
        return bases

    # ---------------- 持久化 ----------------

    def save_library(self):
        save_json(self.library_path, {
            "books": self.books,
            "roots": self.roots,
            "scanned_at": self.scanned_at,
        })

    def save_library_data(self, books, roots, scanned_at):
        """在锁外序列化书库（271MB 级），返回临时文件路径；调用方只需短暂加锁做 os.replace"""
        tmp = self.library_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"books": books, "roots": roots, "scanned_at": scanned_at},
                      fp, ensure_ascii=False, indent=2)
        return tmp

    def save_progress(self):
        save_json(self.progress_path, self.progress)

    def save_settings(self):
        save_json(self.settings_path, self.settings)

    def save_metadata(self):
        save_json(self.metadata_path, self.metadata)

    def save_playlists(self):
        save_json(self.playlists_path, self.playlists)

    # ---------------- 章节标题覆盖（可回滚） ----------------

    def apply_chapter_overrides(self, books):
        """扫描重建章节后，把已保存的平台章节标题重新盖到对应序号上。
        章节文件路径与文件名始终不变，只改 title。"""
        meta_books = self.metadata.get("books") or {}
        changed = False
        for book in books:
            entry = meta_books.get(book.get("id")) or {}
            override = entry.get("chapter_titles")
            if not isinstance(override, dict):
                continue
            items = override.get("items") or {}
            chapters = book.get("chapters") or []
            for key, info in items.items():
                try:
                    index = int(key)
                except (TypeError, ValueError):
                    continue
                if not (0 <= index < len(chapters)):
                    continue
                title = (info or {}).get("title")
                if title and chapters[index].get("title") != title:
                    chapters[index]["title"] = title
                    changed = True
        return changed

    def set_chapter_overrides(self, book, mapping, source, external_id):
        """写入一批章节标题覆盖；original 只记录首次覆盖前的原值，用于按书回滚"""
        entry = self.metadata.setdefault("books", {}).setdefault(book["id"], {})
        prev_items = (entry.get("chapter_titles") or {}).get("items") or {}
        items = dict(prev_items)
        for item in mapping:
            key = str(item["index"])
            prev = prev_items.get(key) or {}
            items[key] = {
                "title": item["title"],
                "original": prev.get("original") or item.get("old") or "",
                "ordinal": item.get("ordinal"),
            }
        entry["chapter_titles"] = {
            "source": source or "",
            "external_id": str(external_id or ""),
            "updated": int(time.time()),
            "items": items,
        }
        for item in mapping:
            index = item["index"]
            if 0 <= index < len(book.get("chapters") or []):
                book["chapters"][index]["title"] = item["title"]
        self.save_metadata()
        self.save_library()

    def reset_chapter_titles(self, book):
        """按书回滚：删除覆盖，标题恢复为文件名派生的原名（与扫描时一致）"""
        entry = (self.metadata.get("books") or {}).get(book["id"]) or {}
        override = entry.pop("chapter_titles", None)
        chapters = book.get("chapters") or []
        restored = 0
        for chapter in chapters:
            base = chapter.get("file_name") or chapter.get("filename") or ""
            if not base:
                continue
            title = clean_name(base) or strip_ext(base) or "第%d章" % (int(chapter.get("index") or 0) + 1)
            if chapter.get("title") != title:
                chapter["title"] = title
                restored += 1
        if override is not None:
            self.save_metadata()
        self.save_library()
        return restored, override is not None

    def apply_chapter_scrape(self, book, source, external_id):
        """抓取平台章节列表并与本地序号对齐，只改标题，失败时不落库"""
        source = (source or "").strip()
        external_id = str(external_id or "").strip()
        if not source or not external_id:
            return {"ok": False, "error": "缺少平台来源或专辑/书籍 ID"}
        if getattr(self, "scanning", False):
            return {"ok": False, "error": "书库正在扫描，请稍后再试"}
        if source == "ximalaya":
            items, error, complete = ximalaya_album_chapter_list(external_id)
        elif source == "lanren":
            items, error, complete = lanren_book_chapter_list(external_id)
        elif source == "missevan":
            items, error, complete = missevan_drama_chapter_list(external_id)
        else:
            return {"ok": False, "error": "该平台暂不支持章节标题刮削：%s" % source}
        mapping, strategy, warning = align_chapter_titles(book.get("chapters") or [], items)
        if not mapping:
            if strategy:
                return {
                    "ok": True,
                    "applied": 0,
                    "local_count": len(book.get("chapters") or []),
                    "platform_count": len(items),
                    "platform_complete": complete,
                    "strategy": strategy,
                    "warning": warning or "章节标题已与平台一致，无需更新",
                }
            return {
                "ok": False,
                "error": warning or error or "没有可安全对齐的章节",
                "platform_count": len(items),
                "platform_complete": complete,
                "strategy": strategy,
            }
        self.set_chapter_overrides(book, mapping, source, external_id)
        return {
            "ok": True,
            "applied": len(mapping),
            "local_count": len(book.get("chapters") or []),
            "platform_count": len(items),
            "platform_complete": complete,
            "strategy": strategy,
            "warning": warning or error,
            "sample": mapping[:3],
        }

    # ---------------- 账号 / 会话 ----------------

    def save_users(self):
        save_json(self.users_path, self.users)

    def save_sessions(self):
        save_json(self.sessions_path, self.sessions)

    def seed_users(self):
        """首次运行创建默认管理员账号"""
        if self.users["users"]:
            return
        self.users["users"].append({
            "id": "u_admin",
            "username": "admin",
            "password": hash_password("admin123"),
            "role": "admin",
            "avatar": "",
            "created_at": int(time.time()),
        })
        self.save_users()

    def find_user(self, username):
        username = (username or "").strip().lower()
        if not username:
            return None
        for user in self.users["users"]:
            if (user.get("username") or "").lower() == username:
                return user
        return None

    def user_by_id(self, user_id):
        for user in self.users["users"]:
            if user.get("id") == user_id:
                return user
        return None

    def avatar_path(self, user):
        name = (user or {}).get("avatar") or ""
        if not name:
            return None
        path = os.path.join(self.avatar_dir, name)
        return path if os.path.isfile(path) else None

    def public_user(self, user):
        if not user:
            return None
        avatar = self.avatar_path(user)
        data = {
            "id": user["id"],
            "username": user.get("username") or "",
            "role": user.get("role") or "user",
            "created_at": user.get("created_at"),
            "avatar_url": "",
            "login_source": user.get("login_source") or "password",
            "fnos_name": user.get("fnos_name") or "",
        }
        if avatar:
            try:
                data["avatar_url"] = "/api/avatar/%s?v=%d" % (
                    urllib.parse.quote(user["id"]), int(os.path.getmtime(avatar)))
            except OSError:
                pass
        return data

    def create_user(self, username, password, role="user"):
        username = (username or "").strip()
        password = str(password or "")
        if not username or not password:
            return None, "账号和密码不能为空"
        if len(username) > 32:
            return None, "账号名称过长"
        if len(password) < 3:
            return None, "密码至少 3 位"
        if self.find_user(username):
            return None, "该账号已存在"
        with self.lock:
            user = {
                "id": "u_" + hashlib.sha1(
                    (username + str(time.time())).encode("utf-8")).hexdigest()[:10],
                "username": username,
                "password": hash_password(password),
                "role": role,
                "avatar": "",
                "created_at": int(time.time()),
            }
            self.users["users"].append(user)
            self.save_users()
        return user, None

    def update_user(self, user, username=None, password=None, role=None):
        if username:
            username = username.strip()
            if len(username) > 32:
                return "账号名称过长"
            other = self.find_user(username)
            if other and other["id"] != user["id"]:
                return "该账号已存在"
        if role in ("admin", "user") and role != user.get("role"):
            if user.get("role") == "admin" and role == "user":
                admins = [u for u in self.users["users"] if u.get("role") == "admin"]
                if len(admins) <= 1:
                    return "至少保留一个管理员账号"
        with self.lock:
            if username:
                user["username"] = username
            if password:
                if len(str(password)) < 3:
                    return "密码至少 3 位"
                user["password"] = hash_password(password)
            if role in ("admin", "user"):
                user["role"] = role
            self.save_users()
        return None

    def delete_user(self, user):
        with self.lock:
            self.users["users"] = [u for u in self.users["users"] if u["id"] != user["id"]]
            path = self.avatar_path(user)
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            tokens = [t for t, s in self.sessions.items() if s.get("user_id") == user["id"]]
            for token in tokens:
                self.sessions.pop(token, None)
            self.save_users()
            self.save_sessions()

    def save_avatar(self, user, data_url):
        """解析 data URL 头像并写入 avatars 目录"""
        if not isinstance(data_url, str) or "," not in data_url:
            return "头像数据无效"
        header, raw = data_url.split(",", 1)
        lower = header.lower()
        if "png" in lower:
            ext = ".png"
        elif "webp" in lower:
            ext = ".webp"
        elif "gif" in lower:
            ext = ".gif"
        else:
            ext = ".jpg"
        try:
            body = base64.b64decode(raw, validate=False)
        except Exception:
            return "头像数据无效"
        if not body:
            return "头像内容为空"
        if len(body) > 5 * 1024 * 1024:
            return "头像不能超过 5MB"
        if not body.startswith(b"\xff\xd8") and not body.startswith(b"\x89PNG") \
                and not body.startswith(b"RIFF") and not body.startswith(b"GIF8"):
            return "返回内容不是图片"
        with self.lock:
            for old_ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                old = os.path.join(self.avatar_dir, "user-%s%s" % (user["id"], old_ext))
                if os.path.isfile(old):
                    try:
                        os.remove(old)
                    except OSError:
                        pass
            name = "user-%s%s" % (user["id"], ext)
            with open(os.path.join(self.avatar_dir, name), "wb") as fp:
                fp.write(body)
            user["avatar"] = name
            self.save_users()
        return None

    def new_session(self, user):
        token = hashlib.sha1(
            (user["id"] + str(time.time()) + os.urandom(8).hex()).encode("utf-8")).hexdigest()
        with self.lock:
            self.sessions[token] = {
                "user_id": user["id"],
                "created": int(time.time()),
            }
            self.save_sessions()
        return token

    def session_user(self, token):
        if not token:
            return None
        entry = self.sessions.get(token)
        if not entry:
            return None
        return self.user_by_id(entry.get("user_id") or "")

    def drop_session(self, token):
        with self.lock:
            if token in self.sessions:
                self.sessions.pop(token, None)
                self.save_sessions()

    # -------- 飞牛 ID 登录（fnOS OAuth） --------

    def fnos_enabled(self):
        return bool(self.settings.get("fnos_login_enabled", True))

    def fnos_endpoint(self):
        """服务端换取令牌 / 读取用户信息使用的 fnOS 接口地址"""
        value = str(self.settings.get("fnos_endpoint") or "").strip()
        return (value or FNOS_DEFAULT_ENDPOINT).rstrip("/")

    def fnos_device_id(self):
        """稳定的设备标识，首次生成后写入设置"""
        with self.lock:
            device_id = str(self.settings.get("fnos_device_id") or "")
            if not device_id:
                device_id = secrets.token_hex(16)
                self.settings["fnos_device_id"] = device_id
                self.save_settings()
        return device_id

    def fnos_public_endpoint(self, host_header):
        """浏览器打开的授权页地址：优先设置项，否则按当前访问主机推导"""
        configured = str(self.settings.get("fnos_public_endpoint") or "").strip()
        if configured:
            return configured.rstrip("/")
        endpoint = self.fnos_endpoint()
        host = urllib.parse.urlparse(endpoint).hostname or ""
        if host and host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            return endpoint
        raw = (host_header or "").strip()
        host = ""
        if raw.startswith("["):
            host = raw.split("]")[0] + "]"
        elif raw:
            host = raw.split(":")[0]
        return ("https://%s:5667" % host) if host else endpoint

    def fnos_post(self, url, payload, token=""):
        """请求 fnOS 接口并解析 JSON（自签证书不校验）"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        context = ssl._create_unverified_context() if url.startswith("https://") else None
        try:
            with urllib.request.urlopen(request, timeout=15, context=context) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                data = {}
            return None, fnos_message(data) or ("HTTP %d" % exc.code)
        except (urllib.error.URLError, OSError) as exc:
            return None, "无法连接飞牛服务：%s" % exc
        try:
            return json.loads(raw.decode("utf-8")), None
        except ValueError:
            return None, "飞牛服务返回内容异常"

    def fnos_begin(self, host_header):
        """生成 PKCE 参数与授权链接"""
        endpoint = self.fnos_endpoint()
        public = self.fnos_public_endpoint(host_header)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("utf-8")).digest()
        ).decode("utf-8").rstrip("=")
        state = secrets.token_urlsafe(24)
        now = int(time.time())
        with self.lock:
            self.fnos_pending = {
                key: item for key, item in self.fnos_pending.items()
                if item.get("created", 0) + FNOS_PENDING_TTL > now
            }
            self.fnos_pending[state] = {
                "verifier": verifier,
                "endpoint": endpoint,
                "created": now,
            }
        params = {
            "client_id": FNOS_OAUTH_CLIENT_ID,
            "scope": FNOS_OAUTH_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "device_id": self.fnos_device_id(),
            "device_name": FNOS_DEVICE_NAME,
            "device_model": FNOS_DEVICE_MODEL,
        }
        url = public + "/signin?" + urllib.parse.urlencode(params)
        return {"url": url, "state": state, "expires_in": FNOS_PENDING_TTL}, None

    def user_by_fnos_uid(self, uid):
        if not uid:
            return None
        for user in self.users["users"]:
            if str(user.get("fnos_uid") or "") == uid:
                return user
        return None

    def create_fnos_user(self, name, uid, role="user"):
        """为飞牛 ID 创建本地账号（不设密码，只能通过飞牛 ID 登录）"""
        base = re.sub(r"[^\w\u4e00-\u9fff.-]", "", str(name or "")).strip(" .-")
        base = base[:28] or ("fnos_" + uid[-8:])
        username, index = base, 2
        while self.find_user(username):
            suffix = str(index)
            username = base[:28 - len(suffix)] + suffix
            index += 1
        with self.lock:
            user = {
                "id": "u_" + hashlib.sha1(
                    ("fnos:" + uid + str(time.time())).encode("utf-8")).hexdigest()[:10],
                "username": username,
                "password": "",
                "role": role if role in ("admin", "user") else "user",
                "avatar": "",
                "login_source": "fnos",
                "fnos_uid": uid,
                "fnos_name": str(name or ""),
                "created_at": int(time.time()),
            }
            self.users["users"].append(user)
            self.save_users()
        return user, None

    def fnos_claim(self, state, code):
        """用授权码换取飞牛身份，并返回对应本地账号"""
        state = str(state or "").strip()
        code = re.sub(r"\s+", "", str(code or ""))
        if not state:
            return None, "授权状态无效，请重新获取授权链接"
        if not code:
            return None, "请填写飞牛授权页显示的授权码"
        now = int(time.time())
        with self.lock:
            pending = self.fnos_pending.pop(state, None)
        if not pending:
            return None, "授权已失效，请重新获取授权链接"
        if pending.get("created", 0) + FNOS_PENDING_TTL <= now:
            return None, "授权码已过期，请重新获取授权链接"
        endpoint = pending.get("endpoint") or self.fnos_endpoint()
        token_payload = {
            "client_id": FNOS_OAUTH_CLIENT_ID,
            "code": code,
            "code_verifier": pending.get("verifier") or "",
            "device_id": self.fnos_device_id(),
            "device_name": FNOS_DEVICE_NAME,
            "device_model": FNOS_DEVICE_MODEL,
            "device_os": FNOS_DEVICE_OS,
        }
        data, error = self.fnos_post(endpoint + "/oauthapi/third-part/token", token_payload)
        if error:
            return None, "获取飞牛令牌失败：%s" % error
        token_data = data
        if isinstance(data, list):
            token_data = data[0] if data else {}
        if isinstance(token_data, dict) and isinstance(token_data.get("data"), dict):
            token_data = token_data["data"]
        if not isinstance(token_data, dict):
            token_data = {}
        access_token = str(token_data.get("access_token") or "").strip()
        if not access_token:
            return None, "获取飞牛令牌失败：%s" % (fnos_message(data) or "返回缺少 access_token")
        info, error = self.fnos_post(
            endpoint + "/ogh/ac/w",
            {"req": "user.info", "reqid": secrets.token_hex(8)},
            token=access_token,
        )
        if error:
            return None, "读取飞牛账号信息失败：%s" % error
        user_info = fnos_find_user_info(info)
        uid = str(user_info.get("uid") or user_info.get("userId") or "").strip()
        if not uid:
            return None, "读取飞牛账号信息失败：%s" % (fnos_message(info) or "缺少用户 uid")
        name = str(
            user_info.get("user") or user_info.get("username") or user_info.get("name") or ""
        ).strip()
        role = "admin" if str(user_info.get("admin")).strip().lower() in ("1", "true", "yes") else "user"
        user = self.user_by_fnos_uid(uid)
        if not user:
            user, error = self.create_fnos_user(name, uid, role)
            if error:
                return None, error
        elif name and user.get("fnos_name") != name:
            with self.lock:
                user["fnos_name"] = name
                self.save_users()
        return user, None

    def fnos_password_login(self, username, password, timeout=6):
        """用飞牛账号密码校验并映射为本地账号，返回 (用户, 错误)"""
        if not self.fnos_enabled():
            return None, "未启用飞牛 ID 登录"
        info, error = fnos_verify_password(self.fnos_endpoint(), username, password, timeout)
        if error:
            return None, error
        uid = str(info.get("uid") or info.get("userId") or "").strip()
        if not uid:
            return None, "飞牛未返回用户标识"
        name = str(
            info.get("user") or info.get("username") or info.get("name") or ""
        ).strip()
        role = "admin" if str(info.get("admin")).strip().lower() in ("1", "true", "yes") else "user"
        user = self.user_by_fnos_uid(uid)
        if not user:
            user, error = self.create_fnos_user(name or username, uid, role)
            if error:
                return None, error
        elif name and user.get("fnos_name") != name:
            with self.lock:
                user["fnos_name"] = name
                self.save_users()
        return user, ""

    # -------- 二维码一次性登录票据 --------

    def create_login_ticket(self, user):
        """生成短时效、单次使用的登录票据（供手机扫码自动登录）"""
        token = secrets.token_urlsafe(24)
        now = int(time.time())
        with self.lock:
            self.login_tickets = {
                key: val for key, val in self.login_tickets.items()
                if val.get("expires", 0) > now
            }
            self.login_tickets[token] = {
                "user_id": user["id"],
                "expires": now + LOGIN_TICKET_TTL,
            }
        return token

    def claim_login_ticket(self, token):
        """核销票据；票据一次性使用，过期或已用均返回 None"""
        now = int(time.time())
        with self.lock:
            entry = self.login_tickets.pop(token or "", None)
        if not entry or entry.get("expires", 0) <= now:
            return None
        return self.user_by_id(entry.get("user_id") or "")

    # ---------------- 元数据 / 封面 ----------------

    def meta_of(self, book_id):
        return self.metadata["books"].get(book_id) or {}

    def meta_seconds(self, meta, key):
        """读取以秒为单位的元数据字段（支持小数），非法值回退 0"""
        try:
            value = float(meta.get(key) or 0)
        except (TypeError, ValueError):
            return 0
        return round(max(0.0, min(600.0, value)), 1)

    def display_title(self, book):
        value = (self.meta_of(book["id"]).get("title") or "").strip()
        return value or book["title"]

    def scraped_cover(self, book_id):
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            path = os.path.join(self.cover_dir, book_id + ext)
            if os.path.isfile(path):
                return path
        return None

    def cover_info(self, book):
        path = self.scraped_cover(book["id"])
        if path:
            return path
        # _cover_abs 是扫描时 find_cover 已确认存在的路径；首屏接口不再实时
        # os.path.isfile/getmtime，避免在 rclone FUSE 挂载上做同步 stat（可能挂起）
        return book.get("_cover_abs") or None

    def cover_url(self, book):
        path = self.cover_info(book)
        if not path:
            return None
        if path == book.get("_cover_abs"):
            # FUSE 封面：版本号用扫描时记录的书籍 mtime，不访问挂载点
            version = int(book.get("mtime") or 0)
        else:
            try:
                version = int(os.path.getmtime(path))
            except OSError:
                return None
        return "/cover-img/%s?v=%d" % (book["id"], version)

    def scrape_config(self):
        config = self.settings.get("scrape") or {}
        return {
            "api_base": (config.get("api_base") or DEFAULT_SCRAPE_BASE).strip(),
        }

    def cover_is_manual(self, book):
        """判断封面是否由用户手动/本地提供：这类封面不允许被刮削自动覆盖"""
        meta = self.meta_of(book["id"])
        flag = str(meta.get("cover_manual") or "").strip().lower()
        if flag in ("1", "true", "yes", "on"):
            return True
        # 书目录里自带封面，且还没被刮削覆盖过：视为用户自己的封面
        return bool(book.get("_cover_abs")) and not self.scraped_cover(book["id"])

    def apply_scraped_cover(self, book, url):
        """应用刮削封面；返回三态结果（updated / skipped_manual / failed）"""
        result = {"status": "not_found", "error": None, "url": url or ""}
        if not str(url or "").strip():
            return result
        if self.cover_is_manual(book):
            result["status"] = "skipped_manual"
            result["error"] = "该书封面为手动/本地设置，已跳过"
            return result
        path, error = self.save_scraped_cover(book["id"], url)
        if error:
            result["status"] = "failed"
            result["error"] = error
            return result
        result["status"] = "updated"
        result["path"] = path
        return result

    def save_scraped_cover(self, book_id, url):
        """下载并保存刮削封面，返回 (路径, 错误)"""
        target = os.path.join(self.cover_dir, book_id + ".jpg")
        path, error = download_image(url, target)
        if error:
            return None, friendly_net_error(error)
        for ext in COVER_EXTS:
            other = os.path.join(self.cover_dir, book_id + ext)
            if other != path and os.path.isfile(other):
                try:
                    os.remove(other)
                except OSError:
                    pass
        return path, None

    def apply_metadata(self, book_id, values):
        """保存刮削与播放偏好字段"""
        allowed = ("title", "author", "narrator", "description",
                   "tags", "genre", "year", "publisher", "source", "external_id",
                   "skip_intro", "skip_outro", "theme_color", "cover_manual")
        with self.lock:
            entry = self.metadata["books"].setdefault(book_id, {})
            for key in allowed:
                if key in values and values[key] is not None:
                    entry[key] = str(values[key]).strip()
            entry["updated"] = int(time.time())
            self.save_metadata()
            return entry

    def seed_metadata(self, book_id, values):
        """用目录内 metadata.json / book.nfo 播种元数据；已有条目（用户改过或已播种）不覆盖"""
        allowed = ("title", "author", "narrator", "description",
                   "tags", "source", "external_id")
        with self.lock:
            if self.metadata["books"].get(book_id):
                return
            entry = {}
            for key in allowed:
                value = values.get(key)
                if value:
                    entry[key] = str(value).strip()
            if not entry:
                return
            entry["seeded"] = True
            entry["updated"] = int(time.time())
            self.metadata["books"][book_id] = entry
            self._meta_dirty = True

    def search_albums(self, query, limit=12, source=None):
        config = self.scrape_config()
        adapter = ""
        if source and source not in ("ximalaya", "builtin", "missevan"):
            plugin = self.plugin_by_id(source)
            if not plugin:
                return [], "刮削源不存在，请先在「我的 - 刮削源」中添加"
            if not plugin.get("enabled"):
                return [], "刮削源「%s」已停用，请先启用" % (plugin.get("name") or source)
            adapter = plugin_adapter(plugin)
            if not adapter:
                return [], ("刮削源「%s」声明为 %s 运行时，服务端尚未内置该来源的抓取实现，"
                            "可改用内置来源") % (
                                plugin.get("name") or source, plugin.get("runtime") or "wasm")
        if adapter == "ximalaya":
            items, error = ximalaya_search(query, config["api_base"], limit)
            if not error:
                return items, None
            return [], friendly_net_error(error)
        if adapter == "lanren":
            items, error = lanren_search(query, limit)
            if not error:
                return items, None
            return [], friendly_net_error(error)
        if adapter == "missevan" or source == "missevan":
            items, error = missevan_search(query, limit)
            if not error:
                return items, None
            return [], friendly_net_error(error)
        # 内置：喜马拉雅 + 懒人听书 + 猫耳并行搜索，按来源均衡后合并
        results = {}

        def run(key, fn):
            results[key] = fn()

        threads = [
            threading.Thread(target=run, args=("xm", lambda: ximalaya_search(query, config["api_base"], limit))),
            threading.Thread(target=run, args=("lr", lambda: lanren_search(query, limit))),
            threading.Thread(target=run, args=("ms", lambda: missevan_search(query, limit))),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        xm_items, xm_error = results.get("xm", ([], "搜索失败"))
        lr_items, lr_error = results.get("lr", ([], None))
        ms_items, ms_error = results.get("ms", ([], None))
        groups = [xm_items, lr_items, ms_items]
        merged = []
        picked = set()
        # 每个来源先各取最多 4 条，保证候选卡片能区分来源，剩余按播放量补齐
        for group in groups:
            for item in group[:4]:
                key = (item.get("source_id") or item.get("source"), item.get("id"))
                if key in picked:
                    continue
                picked.add(key)
                merged.append(item)
        rest = sorted(sum(groups, []), key=lambda item: item.get("play_count", 0), reverse=True)
        for item in rest:
            if len(merged) >= limit:
                break
            key = (item.get("source_id") or item.get("source"), item.get("id"))
            if key in picked:
                continue
            picked.add(key)
            merged.append(item)
        if merged:
            return merged[:limit], None
        errors = []
        for label, error in (("喜马拉雅", xm_error), ("懒人听书", lr_error), ("猫耳 FM", ms_error)):
            if error:
                errors.append("%s：%s" % (label, friendly_net_error(error)))
        if errors:
            return [], "；".join(errors)
        return [], "没有搜到相关书籍"

    # ---------------- 刮削源 ----------------

    def save_scrapers(self):
        save_json(self.scrapers_path, self.scrapers)

    def plugin_by_id(self, plugin_id):
        for plugin in self.scrapers["plugins"]:
            if plugin.get("id") == plugin_id:
                return plugin
        return None

    def plugin_file(self, plugin):
        return os.path.join(self.scraper_dir, "%s.tr" % plugin["id"])

    def plugin_payload(self, plugin):
        return {
            "id": plugin["id"],
            "name": plugin.get("name") or plugin["id"],
            "version": plugin.get("version") or "",
            "runtime": plugin.get("runtime") or "wasm",
            "author": plugin.get("author") or "",
            "description": plugin.get("description") or "",
            "domains": plugin.get("domains") or [],
            "enabled": bool(plugin.get("enabled")),
            "usable": bool(plugin_adapter(plugin)),
            "adapter": plugin_adapter(plugin),
            "verified": bool(plugin.get("verified")),
            "signed": bool(plugin.get("signed")),
            "size": plugin.get("size") or 0,
            "installed_at": plugin.get("installed_at") or 0,
        }

    def scraper_sources(self):
        sources = [dict(item) for item in SCRAPER_SOURCES]
        for plugin in self.scrapers["plugins"]:
            if not plugin.get("enabled"):
                continue
            sources.append({
                "id": plugin["id"],
                "name": plugin.get("name") or plugin["id"],
                "type": "plugin",
                "runtime": plugin.get("runtime") or "wasm",
                "version": plugin.get("version") or "",
                "enabled": True,
            })
        return sources

    def install_plugin(self, filename, data_url):
        """保存并登记 .tr 刮削源，返回 (刮削源记录, 错误)"""
        name = os.path.basename(filename or "")
        if name.lower().endswith(".tr"):
            name = name[:-3]
        if not isinstance(data_url, str) or "," not in data_url:
            return None, "刮削源数据无效"
        raw = data_url.split(",", 1)[1]
        try:
            body = base64.b64decode(raw, validate=False)
        except Exception:
            return None, "刮削源数据无效"
        if not body:
            return None, "刮削源内容为空"
        if len(body) > MAX_PLUGIN_BYTES:
            return None, "刮削源不能超过 %dMB" % (MAX_PLUGIN_BYTES / 1024 / 1024)
        info, error = parse_trpack(body)
        if error:
            return None, error
        with self.lock:
            plugin = self.plugin_by_id(info["id"])
            if plugin is None:
                plugin = {"id": info["id"], "enabled": True,
                          "installed_at": int(time.time())}
                self.scrapers["plugins"].append(plugin)
            plugin.update({
                "name": info["name"],
                "vendor_name": info["vendor_name"],
                "version": info["version"],
                "runtime": info["runtime"],
                "entry_point": info["entry_point"],
                "author": info["author"],
                "description": info["description"],
                "domains": info["domains"],
                "format": info["format"],
                "verified": info["verified"],
                "signed": info["signed"],
                "key_id": info["key_id"],
                "size": info["size"],
                "sha256": info["sha256"],
                "file": name or info["id"],
                "updated_at": int(time.time()),
            })
            with open(self.plugin_file(plugin), "wb") as fp:
                fp.write(body)
            self.save_scrapers()
        return plugin, None

    def set_plugin_enabled(self, plugin_id, enabled):
        plugin = self.plugin_by_id(plugin_id)
        if not plugin:
            return None, "刮削源不存在"
        with self.lock:
            plugin["enabled"] = bool(enabled)
            self.save_scrapers()
        return plugin, None

    def remove_plugin(self, plugin_id):
        plugin = self.plugin_by_id(plugin_id)
        if not plugin:
            return "刮削源不存在"
        with self.lock:
            self.scrapers["plugins"] = [
                item for item in self.scrapers["plugins"] if item.get("id") != plugin_id]
            try:
                os.remove(self.plugin_file(plugin))
            except OSError:
                pass
            self.save_scrapers()
        return None

    # ---------------- 背景音乐（BGM） ----------------

    def authorized_roots(self):
        """飞牛授权的文件夹（与书库扫描同一来源，含手动添加目录）"""
        roots, seen = [], set()
        for path in self.resolve_roots():
            norm = os.path.normcase(os.path.realpath(path))
            if norm not in seen:
                seen.add(norm)
                roots.append(path)
        return roots

    def path_within_authorized(self, path):
        """校验 path 位于授权目录内，返回规范化绝对路径；否则 None"""
        if not path:
            return None
        try:
            real = os.path.realpath(str(path))
        except (OSError, ValueError):
            return None
        norm = os.path.normcase(real)
        for root in self.authorized_roots():
            root_norm = os.path.normcase(os.path.realpath(root))
            if norm == root_norm or norm.startswith(root_norm + os.sep):
                return real
        return None

    def bgm_dir_current(self):
        """当前配置的音乐目录（必须仍在授权范围内），无则空字符串"""
        raw = str(self.settings.get("bgm_dir") or "").strip()
        if not raw:
            return ""
        real = self.path_within_authorized(raw)
        if real and os.path.isdir(real):
            return real
        return ""

    def bgm_scan_folder(self):
        """只扫描 bgm/media 角色的音乐目录（深度 3），不把书库音频当背景音乐"""
        bases = self.bgm_roots()
        items = []
        if not bases:
            return items

        def walk(base, folder, depth):
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                return
            for name in names:
                if (name.startswith(".") or name.startswith("._")
                        or name in SKIP_DIRS or name.startswith("@eaDir")):
                    continue
                path = os.path.join(folder, name)
                if os.path.isdir(path):
                    if depth < 3 and not os.path.islink(path):
                        walk(base, path, depth + 1)
                elif fmt_ext(name) in BGM_EXTS:
                    try:
                        stat = os.stat(path)
                        with open(path, "rb") as fp:
                            head = fp.read(16)
                    except OSError:
                        continue
                    if not audio_format_of(head)[0]:
                        # 改名的文本/图片等非音频文件不收录，避免播放时才出噪声
                        print("[bgm] 跳过非音频文件: %s" % path, file=sys.stderr)
                        continue
                    rel = os.path.relpath(path, base).replace("\\", "/")
                    items.append({
                        "name": rel,
                        "title": strip_ext(name),
                        "size": stat.st_size,
                        "mtime": int(stat.st_mtime),
                        "url": "/bgm/file?p=" + urllib.parse.quote(path, safe=""),
                        "from": "folder",
                        "base": base,
                    })

        for base in bases:
            walk(base, base, 0)
        # 同一文件出现在多个音乐目录时按 url 去重
        deduped, seen = [], set()
        for item in items:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            deduped.append(item)
        return deduped

    def bgm_list(self):
        """列出背景音乐：应用内上传 + 授权目录中的音乐文件夹"""
        items = []
        try:
            names = sorted(os.listdir(self.bgm_dir))
        except OSError:
            names = []
        for name in names:
            path = os.path.join(self.bgm_dir, name)
            if not os.path.isfile(path):
                continue
            ext = fmt_ext(name)
            if ext not in BGM_EXTS:
                continue
            try:
                stat = os.stat(path)
                with open(path, "rb") as fp:
                    head = fp.read(16)
            except OSError:
                continue
            if not audio_format_of(head)[0]:
                print("[bgm] 跳过非音频文件: %s" % path, file=sys.stderr)
                continue
            items.append({
                "name": name,
                "title": strip_ext(name),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "url": "/bgm/" + urllib.parse.quote(name),
                "from": "upload",
            })
        seen_urls = {item["url"] for item in items}
        for item in self.bgm_scan_folder():
            if item["url"] not in seen_urls:
                items.append(item)
        return items

    def bgm_dir_set(self, path):
        """设置背景音乐目录（必须在飞牛授权文件夹内），返回 ({dir}, None) 或 (None, 错误)"""
        path = str(path or "").strip()
        if not path:
            self.settings.pop("bgm_dir", None)
            self.save_settings()
            return {"dir": ""}, None
        real = self.path_within_authorized(path)
        if not real or not os.path.isdir(real):
            return None, "目录必须在飞牛授权的文件夹内"
        self.settings["bgm_dir"] = real
        self.save_settings()
        return {"dir": real}, None

    def bgm_browse(self, path):
        """浏览授权目录（path 为空时返回授权根目录列表），返回 ({...}, None) 或 (None, 错误)"""
        if not path:
            dirs = []
            for root in self.authorized_roots():
                dirs.append({
                    "name": os.path.basename(root.rstrip("\\/")) or root,
                    "path": root,
                })
            return {"path": "", "parent": "", "dirs": dirs}, None
        real = self.path_within_authorized(path)
        if not real or not os.path.isdir(real):
            return None, "目录必须在飞牛授权的文件夹内"
        parent = ""
        upper = os.path.dirname(real)
        if self.path_within_authorized(upper):
            parent = upper
        dirs = []
        try:
            names = sorted(os.listdir(real))
        except OSError:
            names = []
        for name in names:
            if name.startswith(".") or name in SKIP_DIRS or name.startswith("@eaDir"):
                continue
            full = os.path.join(real, name)
            if os.path.isdir(full):
                dirs.append({"name": name, "path": full})
        return {"path": real, "parent": parent, "dirs": dirs}, None

    def bgm_save(self, filename, data_url):
        """保存背景音乐文件，返回 (记录, 错误)"""
        name = os.path.basename(filename or "")
        ext = fmt_ext(name).lower()
        if not name or ext not in BGM_EXTS:
            return None, "不支持的音频格式：%s" % ext
        if not isinstance(data_url, str) or "," not in data_url:
            return None, "背景音乐数据无效"
        raw = data_url.split(",", 1)[1]
        try:
            body = base64.b64decode(raw, validate=False)
        except Exception:
            return None, "背景音乐数据无效"
        if not body:
            return None, "背景音乐内容为空"
        if len(body) > MAX_BGM_BYTES:
            return None, "背景音乐不能超过 %dMB" % (MAX_BGM_BYTES / 1024 / 1024)
        mime, canonical = audio_format_of(body[:16])
        if not mime:
            return None, ("不是有效的音频文件：文件头无法识别。支持 MP3 / FLAC / "
                          "OGG(Opus) / WAV / M4A(AAC、ALAC) / AAC / AIFF / APE / "
                          "WMA / MKA；扩展名与实际格式不一致时按实际格式播放，"
                          "改名的文本/图片会被拒绝")
        try:
            dest = os.path.join(self.bgm_dir, name)
            with open(dest, "wb") as fp:
                fp.write(body)
        except OSError as exc:
            return None, "保存失败: %s" % exc
        return {"name": name, "title": strip_ext(name), "size": len(body)}, None

    def bgm_delete(self, filename):
        """删除背景音乐文件，返回错误字符串或 None"""
        name = os.path.basename(filename or "")
        if not name or ".." in name or name.startswith("/"):
            return "文件名非法"
        path = os.path.join(self.bgm_dir, name)
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(self.bgm_dir)):
            return "路径非法"
        if not os.path.isfile(path):
            return "文件不存在"
        try:
            os.remove(path)
        except OSError as exc:
            return "删除失败: %s" % exc
        return None

    # ---------------- tingmusic 兼容结构 ----------------

    def compat_book(self, book):
        meta = self.meta_of(book["id"])
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(book.get("mtime") or 0))
        return {
            "id": book["id"],
            "library_id": book.get("root") or "default",
            "title": self.display_title(book),
            "author": meta.get("author") or None,
            "narrator": meta.get("narrator") or None,
            "cover_url": "/api/cover/%s.jpg" % book["id"],
            "theme_color": meta.get("theme_color") or None,
            "description": meta.get("description") or None,
            "skip_intro": self.meta_seconds(meta, "skip_intro"),
            "skip_outro": self.meta_seconds(meta, "skip_outro"),
            "path": book.get("path") or "",
            "hash": book["id"],
            "tags": meta.get("tags") or None,
            "genre": meta.get("genre") or None,
            "year": meta.get("year") or None,
            "created_at": created,
            "is_favorite": book["id"] in self.progress["favorites"],
            "library_type": "audiobook",
            "manual_corrected": bool(meta),
            "has_subtitle": bool(book.get("has_subtitle")),
            "chapter_count": book.get("chapter_count") or len(book.get("chapters") or []),
        }

    def compat_chapter(self, book, chapter, index):
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(book.get("mtime") or 0))
        rel = chapter.get("file_name") or ""
        return {
            "id": "%s_%d" % (book["id"], index),
            "book_id": book["id"],
            "title": chapter.get("title") or strip_ext(rel),
            "path": rel,
            "duration": None,
            "chapter_index": index,
            "is_extra": 0,
            "hash": hashlib.md5(("%s/%s" % (book["id"], rel)).encode("utf-8")).hexdigest(),
            "created_at": created,
            "has_subtitle": bool(chapter.get("has_subtitle") or chapter.get("_sub")),
        }

    def compat_progress(self, book_id):
        entry = self.progress["books"].get(book_id)
        book = self.books_by_id.get(book_id)
        if not entry or not book:
            chapter_id = "%s_0" % book_id if book else None
            return {
                "book_id": book_id,
                "chapter_id": chapter_id,
                "position": 0.0,
                "duration": 0.0,
                "updated_at": None,
            }
        index = max(0, min(len(book["chapters"]) - 1, int(entry.get("chapter", 0))))
        return {
            "id": "%s_%d" % (book_id, index),
            "user_id": "local",
            "book_id": book_id,
            "chapter_id": "%s_%d" % (book_id, index),
            "position": float(entry.get("position") or 0),
            "duration": float(entry.get("duration") or 0),
            "updated_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry.get("updated") or 0)),
        }

    def compat_recent(self, limit=20):
        entries = []
        for book_id, entry in self.progress["books"].items():
            book = self.books_by_id.get(book_id)
            if not book or not entry.get("updated"):
                continue
            index = max(0, min(len(book["chapters"]) - 1, int(entry.get("chapter", 0))))
            chapter = book["chapters"][index]
            entries.append({
                "id": "%s_%d" % (book_id, index),
                "user_id": "local",
                "book_id": book_id,
                "book_title": self.display_title(book),
                "cover_url": "/api/cover/%s.jpg" % book_id,
                "chapter_id": "%s_%d" % (book_id, index),
                "chapter_title": chapter.get("title") or strip_ext(chapter.get("file_name", "")),
                "chapter_duration": float((entry.get("durations") or {}).get(str(index)) or 0),
                "position": float(entry.get("position") or 0),
                "duration": float(entry.get("duration") or 0),
                "updated_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry.get("updated") or 0)),
            })
        entries.sort(key=lambda item: item["updated_at"] or "", reverse=True)
        return entries[:limit]

    # ---------------- 书单 ----------------

    def new_playlist_id(self):
        while True:
            playlist_id = "pl_" + secrets.token_hex(5)
            if not any(p.get("id") == playlist_id for p in self.playlists["playlists"]):
                return playlist_id

    def playlist_payload(self, playlist):
        """输出与上游客户端 /api/playlists 兼容的字段，供网页与鸿蒙客户端共用"""
        items, books, book_ids = [], [], []
        for raw in sorted(playlist.get("items") or [],
                          key=lambda it: int(it.get("order") or 0)):
            item_type = raw.get("item_type") or "book"
            item_id = raw.get("item_id") or ""
            book = self.books_by_id.get(item_id) if item_type == "book" else None
            items.append({
                "item_type": item_type,
                "item_id": item_id,
                "order": int(raw.get("order") or 0),
                "book": self.compat_book(book) if book else None,
            })
            if book and item_id not in book_ids:
                book_ids.append(item_id)
                books.append(self.compat_book(book))
        return {
            "id": playlist.get("id"),
            "user_id": playlist.get("user_id"),
            "title": playlist.get("title") or "",
            "description": playlist.get("description") or "",
            "created_at": playlist.get("created_at") or "",
            "updated_at": playlist.get("updated_at") or "",
            "book_ids": book_ids,
            "books": books,
            "items": items,
            "book_count": len(book_ids),
        }

    def normalize_playlist_items(self, items):
        """只接受仍然存在的书籍，按传入顺序重排 order"""
        result, seen = [], set()
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            if (raw.get("item_type") or "book") != "book":
                continue
            item_id = str(raw.get("item_id") or "").strip()
            if not item_id or item_id in seen or item_id not in self.books_by_id:
                continue
            seen.add(item_id)
            result.append({
                "item_type": "book",
                "item_id": item_id,
                "order": len(result),
            })
        return result

    def playlist_items_from_ids(self, book_ids):
        items = [{"item_type": "book", "item_id": str(item or "").strip()}
                 for item in (book_ids if isinstance(book_ids, list) else [])]
        return self.normalize_playlist_items(items)

    def playlist_owner(self, playlist, user):
        return (playlist.get("user_id") == user.get("id")
                or user.get("role") == "admin")

    # ---------------- 扫描 ----------------

    def start_scan(self):
        with self.lock:
            if self.scanning:
                return False
            self.scanning = True
            self.scan_error = ""
            self.scan_started_at = time.time()
            self.scan_thread = threading.Thread(target=self._scan_worker, daemon=True)
            self.scan_thread.start()
            return True

    def _scan_worker(self):
        try:
            roots = self.resolve_roots()
            skip_dirs = self.book_skip_dirs()
            report = {"scanned": [], "skipped": [], "duplicates": 0}
            self._scan_sub_cache = {}
            books = []
            for root in roots:
                normalized = os.path.normcase(os.path.realpath(root))
                if self._under_dirs(normalized, skip_dirs):
                    report["skipped"].append(root)
                    continue
                report["scanned"].append(root)
                books.extend(self._scan_root(root, skip_dirs, report))
            # 判重：文件名 + 大小 + 修改时间序列一致视为同一本书（跨根重复只留先出现者）
            unique, signatures, duplicates = [], {}, {}
            for book in books:
                signature = book_signature(book)
                kept = signatures.get(signature)
                if kept is not None:
                    duplicates[book["id"]] = kept["id"]
                    continue
                signatures[signature] = book
                unique.append(book)
            books = unique
            report["duplicates"] = len(duplicates)
            books.sort(key=lambda b: natural_key(b["title"]))
            old_books = {book["id"]: book for book in self.books}
            scanned_at = int(time.time())
            # 标题覆盖只改本次构建的 books；进度迁移涉及共享 progress，短锁完成
            self.apply_chapter_overrides(books)
            with self.lock:
                self.scan_report = report
                progress_changed = self._adopt_scan_progress(old_books, books, duplicates)
            # 271MB 序列化放锁外，避免整库写盘期间所有加锁接口被拖住
            tmp_library = self.save_library_data(books, roots, scanned_at)
            with self.lock:
                self.books = books
                self.books_by_id = {book["id"]: book for book in books}
                self.roots = roots
                self.scanned_at = scanned_at
                os.replace(tmp_library, self.library_path)
                if progress_changed:
                    self.save_progress()
                if self._meta_dirty:
                    self.save_metadata()
                    self._meta_dirty = False
                self.scan_finished_at = time.time()
                self.scan_error = ""
        except Exception as exc:  # 扫描异常不应导致服务不可用
            with self.lock:
                self.scan_error = str(exc)
                self.scan_finished_at = time.time()
        finally:
            with self.lock:
                self.scanning = False

    def _under_dirs(self, path, dirs):
        """路径是否位于任一目录内（含目录自身）"""
        for base in dirs or ():
            if path == base or path.startswith(base + os.sep):
                return True
        return False

    def _scan_root(self, root, skip_dirs=None, report=None):
        root = os.path.abspath(root)
        skip_dirs = skip_dirs or set()
        base_depth = root.rstrip(os.sep).count(os.sep)
        found = []
        visited = set()
        queue = [root]
        while queue:
            dirpath = queue.pop(0)
            # 背景音乐目录不属于有声书库：进入目录前整棵子树剪枝
            real = os.path.normcase(os.path.realpath(dirpath))
            if self._under_dirs(real, skip_dirs):
                if report is not None:
                    report["skipped"].append(dirpath)
                continue
            if real in visited:
                continue
            visited.add(real)
            depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
            if depth > MAX_DEPTH:
                continue
            dirnames, filenames = list_dir_entries(dirpath)
            dirnames.sort(key=natural_key)
            audio = sorted([f for f in filenames if is_audio(f)], key=natural_key)
            if depth == 0:
                # 共享根目录的散文件仍按单文件成书，保持既有行为与既有书 id
                for name in audio:
                    path = os.path.join(dirpath, name)
                    book = self._book_from_files(path, [path], root, single=True)
                    if book:
                        found.append(book)
                queue.extend(os.path.join(dirpath, name) for name in dirnames)
                continue
            volumes = volume_dir_names(dirnames)
            if volumes:
                # 分卷 / CD / 部 / 册 / 数字区间：父目录松散音频 + 各卷音频合并为一本书
                files = [os.path.join(dirpath, name) for name in audio]
                for volume in sorted(volumes, key=natural_key):
                    files.extend(self._collect_audio(os.path.join(dirpath, volume),
                                                    MAX_DEPTH - depth, skip_dirs, set(), report))
                if files:
                    book = self._book_from_files(dirpath, files, root, volume=True)
                    if book:
                        found.append(book)
                    continue
            if audio:
                # 书根目录：本层音频 + 非分卷子目录内的音频都归入本书，书名取本层目录
                files = [os.path.join(dirpath, name) for name in audio]
                for name in dirnames:
                    if name in volumes:
                        continue
                    files.extend(self._collect_audio(os.path.join(dirpath, name),
                                                    MAX_DEPTH - depth, skip_dirs, set(), report))
                book = self._book_from_files(dirpath, files, root)
                if book:
                    found.append(book)
                continue
            queue.extend(os.path.join(dirpath, name) for name in dirnames
                         if name not in volumes)
        return found

    def _collect_audio(self, folder, depth_left, skip_dirs=None, visited=None, report=None):
        """递归收集目录内全部音频，按相对路径自然排序"""
        skip_dirs = skip_dirs or set()
        visited = visited if visited is not None else set()
        result = []
        stack = [(folder, 0)]
        while stack:
            dirpath, depth = stack.pop()
            if depth > depth_left:
                continue
            real = os.path.normcase(os.path.realpath(dirpath))
            if self._under_dirs(real, skip_dirs):
                if report is not None:
                    report["skipped"].append(dirpath)
                continue
            if real in visited:
                continue
            visited.add(real)
            dirnames, filenames = list_dir_entries(dirpath)
            result.extend(os.path.join(dirpath, name) for name in filenames if is_audio(name))
            stack.extend((os.path.join(dirpath, name), depth + 1) for name in dirnames)
        result.sort(key=lambda path: natural_path_key(os.path.relpath(path, folder)))
        return result

    def _book_from_files(self, book_path, files, root, single=False, volume=False):
        book_id = hashlib.sha1(book_path.encode("utf-8")).hexdigest()[:12]
        if single:
            title = clean_name(os.path.basename(book_path))
            folder = book_path
            cover_dir = os.path.dirname(book_path) or book_path
            prefer = [os.path.basename(book_path)]
        else:
            title = clean_name(os.path.basename(book_path.rstrip(os.sep)))
            folder = book_path
            cover_dir = book_path
            prefer = [os.path.basename(book_path.rstrip(os.sep)), title]

        # 先建章节骨架（按相对路径自然排序），章节阶段异常也要保住至少一章
        ordered = []
        for file_path in files:
            try:
                rel = os.path.relpath(file_path, root)
            except ValueError:
                rel = os.path.basename(file_path)
            ordered.append((natural_path_key(rel), os.path.basename(file_path), file_path, rel))
        ordered.sort(key=lambda item: (is_extra_chapter(item[1]) or is_extra_chapter(item[3]), item[0], item[1]))

        chapters, total_size, max_mtime = [], 0, 0
        try:
            for index, (_, _, file_path, rel) in enumerate(ordered):
                try:
                    stat = os.stat(file_path)
                    size, mtime = stat.st_size, int(stat.st_mtime)
                except OSError:
                    size, mtime = 0, 0
                total_size += size
                max_mtime = max(max_mtime, mtime)
                chapters.append({
                    "index": index,
                    "title": clean_name(os.path.basename(file_path)) or "第%d章" % (index + 1),
                    "file_name": os.path.basename(file_path),
                    "rel": rel,
                    "size": size,
                    "mtime": mtime,
                    "_abs": file_path,
                })
        except Exception:
            chapters = []
        if not chapters:
            # 兜底：每本书强制至少一章，避免书在书库中退回 0 章
            first = ordered[0][2] if ordered else book_path
            rel = ordered[0][3] if ordered else os.path.basename(book_path)
            chapters = [{
                "index": 0,
                "title": clean_name(os.path.basename(first)) or title or "第1章",
                "file_name": os.path.basename(first),
                "rel": rel,
                "size": 0,
                "mtime": 0,
                "_abs": first,
            }]

        # 扫描阶段识别字幕：每目录只 listdir 一次，结果随章节写入书库
        sub_cache = getattr(self, "_scan_sub_cache", None)
        if sub_cache is None:
            sub_cache = {}
            self._scan_sub_cache = sub_cache
        for chapter in chapters:
            try:
                subtitle = find_subtitle(chapter.get("_abs") or "", sub_cache)
            except Exception:
                subtitle = ""
            if subtitle:
                chapter["has_subtitle"] = True
                chapter["_sub"] = subtitle

        # 章节骨架建好后再读 sidecar / 封面，元数据失败不影响章节
        cover, sidecar = None, {}
        try:
            cover = find_cover(cover_dir, prefer)
        except Exception:
            cover = None
        if not single:
            try:
                sidecar = read_sidecar_metadata(cover_dir)
            except Exception:
                sidecar = {}
            if sidecar:
                try:
                    # 目录内 metadata.json / book.nfo：播种元数据，无封面时按 cover_url 下载
                    self.seed_metadata(book_id, sidecar)
                    if not cover and sidecar.get("cover_url") and not self.scraped_cover(book_id):
                        download_image(sidecar["cover_url"],
                                       os.path.join(self.cover_dir, book_id + ".jpg"))
                except Exception:
                    pass

        sidecar_titles = sidecar.get("chapter_titles") or []
        if len(sidecar_titles) == len(chapters) and sidecar_titles:
            file_numbers = [prefix_number(ch.get("file_name")) for ch in chapters]
            title_numbers = [prefix_number(t) for t in sidecar_titles]
            complete = None not in file_numbers and None not in title_numbers
            # 情况一：两侧序号集合完全一致 → 按序号对号入座，容忍 sidecar 顺序不同
            if (complete and len(set(file_numbers)) == len(file_numbers)
                    and len(set(title_numbers)) == len(title_numbers)
                    and set(file_numbers) == set(title_numbers)):
                mapped = {num: text for num, text in zip(title_numbers, sidecar_titles)}
                for chapter in chapters:
                    text = mapped.get(prefix_number(chapter.get("file_name")))
                    if text:
                        chapter["title"] = text
            # 情况二：顺序本身已经一致 → 按位置套用
            elif all(a is not None and b is not None and a == b
                     for a, b in zip(file_numbers, title_numbers)):
                for chapter, text in zip(chapters, sidecar_titles):
                    if text:
                        chapter["title"] = text
            # 其它情况：sidecar 顺序与音频文件对不上，保留本地文件名标题，避免串章

        return {
            "id": book_id,
            "title": title,
            "path": folder,
            "root": root,
            "chapter_count": len(chapters),
            "size": total_size,
            "mtime": max_mtime,
            "chapters": chapters,
            "has_subtitle": any(ch.get("has_subtitle") for ch in chapters),
            "is_single": bool(single),
            "is_volume": bool(volume),
            "_cover_abs": cover,
        }

    def _adopt_scan_progress(self, old_books, new_books, duplicates):
        """分卷合并后把旧书进度/收藏幂等地补到新书；只补不删，失败不影响书库"""
        by_id = {book["id"]: book for book in new_books}
        owners = {}
        signature_map = {}
        for book in new_books:
            signature_map.setdefault(book_signature(book), book["id"])
            for chapter in book.get("chapters") or []:
                abs_path = chapter.get("_abs")
                if abs_path:
                    owners.setdefault(abs_path, book["id"])
        progress_books = self.progress.setdefault("books", {})
        favorites = self.progress.setdefault("favorites", [])
        changed = False
        for old_id, old_book in old_books.items():
            target_id = old_id if old_id in by_id else duplicates.get(old_id)
            if not target_id:
                votes = {}
                for chapter in old_book.get("chapters") or []:
                    owner = owners.get(chapter.get("_abs"))
                    if owner:
                        votes[owner] = votes.get(owner, 0) + 1
                target_id = max(votes, key=votes.get) if votes else None
            if not target_id:
                # 目录改名/移动后绝对路径失效，用文件名+大小+修改时间签名重新识别
                target_id = signature_map.get(book_signature(old_book))
            if not target_id or target_id not in by_id:
                continue
            target = by_id[target_id]
            index_map = self._chapter_index_map(old_book, target)
            entry = progress_books.get(old_id)
            if entry:
                if target_id not in progress_books:
                    migrated = dict(entry)
                    migrated["chapter"] = int(index_map.get(int(entry.get("chapter", 0) or 0),
                                                          int(entry.get("chapter", 0) or 0)))
                    durations = entry.get("durations")
                    if isinstance(durations, dict):
                        migrated["durations"] = {
                            str(index_map.get(int(key), int(key))): value
                            for key, value in durations.items() if str(key).isdigit()
                        }
                    progress_books[target_id] = migrated
                    changed = True
                elif target_id == old_id:
                    current = int(entry.get("chapter", 0) or 0)
                    mapped = int(index_map.get(current, current))
                    if mapped != current:
                        entry["chapter"] = mapped
                        changed = True
            if old_id in favorites and target_id not in favorites:
                favorites.append(target_id)
                changed = True
            if self.progress.get("last_played") == old_id:
                self.progress["last_played"] = target_id
                changed = True
        return changed

    @staticmethod
    def _chapter_index_map(old_book, target):
        """旧书章节序号 -> 新书章节序号（按音频绝对路径对齐）"""
        targets = {}
        for position, chapter in enumerate(target.get("chapters") or []):
            abs_path = chapter.get("_abs")
            if abs_path:
                targets[abs_path] = int(chapter.get("index", position) or position)
        mapping = {}
        for position, chapter in enumerate(old_book.get("chapters") or []):
            abs_path = chapter.get("_abs")
            if abs_path in targets:
                mapping[position] = targets[abs_path]
        if not mapping:
            # 目录改名后绝对路径全变，按文件名+大小回退对齐
            by_name = {}
            for position, chapter in enumerate(target.get("chapters") or []):
                by_name[(chapter.get("file_name"), chapter.get("size"))] = int(
                    chapter.get("index", position) or position)
            for position, chapter in enumerate(old_book.get("chapters") or []):
                key = (chapter.get("file_name"), chapter.get("size"))
                if key in by_name:
                    mapping[position] = by_name[key]
        return mapping

    # ---------------- 进度 / 收藏 ----------------

    def book_progress(self, book_id):
        entry = self.progress["books"].get(book_id) or {}
        return {
            "chapter": int(entry.get("chapter", 0)),
            "position": float(entry.get("position", 0.0)),
            "duration": float(entry.get("duration", 0.0)),
            "updated": int(entry.get("updated", 0)),
            "finished": bool(entry.get("finished", False)),
        }

    def update_progress(self, book_id, chapter, position, duration, finished=False):
        with self.lock:
            entry = self.progress["books"].setdefault(book_id, {})
            entry["chapter"] = int(chapter)
            entry["position"] = float(position)
            if duration:
                entry["duration"] = float(duration)
                durations = entry.setdefault("durations", {})
                durations[str(int(chapter))] = float(duration)
            entry["updated"] = int(time.time())
            entry["finished"] = bool(finished)
            self.progress["last_played"] = book_id
            self.save_progress()

    def platform_record(self, book_id):
        with self.lock:
            return (self.platform.get("books") or {}).get(book_id)

    def save_platform_record(self, record):
        with self.lock:
            books = self.platform.setdefault("books", {})
            books[record["book_id"]] = record
            save_json(self.platform_path, self.platform)

    def platform_summary(self):
        with self.lock:
            books = self.platform.get("books") or {}
            matched = sum(1 for item in books.values() if item.get("source"))
            return {
                "running": bool(self.status_progress.get("running")),
                "done": self.status_progress.get("done", 0),
                "total": self.status_progress.get("total", 0),
                "updated_at": self.platform.get("updated_at", 0),
                "matched": matched,
            }

    def refresh_platform_status(self, book, force=False):
        """按书名+作者匹配平台书籍；懒人给完结状态，喜马拉雅只给总章数"""
        now = int(time.time())
        previous = self.platform_record(book["id"])
        if previous and not force and now - int(previous.get("checked_at") or 0) < PLATFORM_TTL:
            print("[platform] cache-hit %s %s" % (book["id"], self.display_title(book)), flush=True)
            return previous
        title = self.display_title(book)
        author = clean_author((self.meta_of(book["id"]) or {}).get("author"))
        print("[platform] lookup %s %s" % (book["id"], title), flush=True)
        record = {
            "book_id": book["id"], "title": title, "checked_at": now,
            "source": "", "external_id": "", "finished": None, "count": None,
            "confidence": 0.0, "matched_title": "", "matched_author": "", "error": "",
        }
        errors = []
        network_error = False
        queries = search_queries(title)
        for query in queries:
            try:
                items, err = lanren_search(query, limit=6)
            except Exception as exc:
                network_error = True
                errors.append("懒人听书查询失败: %s" % exc)
                continue
            if err:
                network_error = True
                errors.append(err)
            best = rank_platform_items(title, author, items)
            if best and best[2] >= PLATFORM_MATCH_MIN:
                try:
                    info = lanren_book_status(best[1]["id"])
                except Exception as exc:
                    network_error = True
                    errors.append("懒人听书详情失败: %s" % exc)
                    continue
                local_count = int(book.get("chapter_count") or 0)
                if info["count"] and local_count >= 20 and info["count"] < local_count * 0.5:
                    errors.append("平台章数 %s 明显少于本地 %s，疑似不同专辑" % (info["count"], local_count))
                    continue
                if info["count"] or info["finished"] is not None:
                    record.update(
                        source="lanren", external_id=best[1]["id"], finished=info["finished"],
                        count=info["count"], confidence=round(best[2], 3),
                        matched_title=best[1].get("title") or "", matched_author=best[1].get("author") or "",
                    )
                    self.save_platform_record(record)
                    return record
        for query in queries:
            try:
                items, err = ximalaya_search(query, limit=6)
            except Exception as exc:
                network_error = True
                errors.append("喜马拉雅查询失败: %s" % exc)
                continue
            if err:
                network_error = True
                errors.append(err)
            best = rank_platform_items(title, author, items)
            if best and best[2] >= PLATFORM_MATCH_MIN:
                try:
                    info = ximalaya_album_tracks(best[1]["id"])
                except Exception as exc:
                    network_error = True
                    errors.append("喜马拉雅详情失败: %s" % exc)
                    continue
                local_count = int(book.get("chapter_count") or 0)
                if info["count"] and local_count >= 20 and info["count"] < local_count * 0.5:
                    errors.append("平台章数 %s 明显少于本地 %s，疑似不同专辑" % (info["count"], local_count))
                    continue
                if info["count"]:
                    record.update(
                        source="ximalaya", external_id=best[1]["id"], finished=None,
                        count=info["count"], confidence=round(best[2], 3),
                        matched_title=best[1].get("title") or "", matched_author=best[1].get("author") or "",
                    )
                    self.save_platform_record(record)
                    return record
        record["error"] = "；".join(errors) or "未匹配到平台书籍"
        if network_error and previous and previous.get("source") and previous.get("count"):
            # 仅断网/请求失败时沿用上次成功结果，匹配失败则清理旧角标
            return previous
        self.save_platform_record(record)
        return record

    def start_platform_refresh(self):
        with self.lock:
            if self._status_thread and self._status_thread.is_alive():
                return False
            books = list(self.books)
        if not books:
            return False
        self.status_progress = {"running": True, "done": 0, "total": len(books), "updated_at": 0}

        def worker():
            for index, book in enumerate(books):
                try:
                    self.refresh_platform_status(book)
                except Exception:
                    pass
                with self.lock:
                    self.status_progress["done"] = index + 1
                time.sleep(1.2)
            now = int(time.time())
            with self.lock:
                self.platform["updated_at"] = now
                save_json(self.platform_path, self.platform)
                self.status_progress.update(running=False, updated_at=now)

        self._status_thread = threading.Thread(target=worker, daemon=True)
        self._status_thread.start()
        return True

    def book_payload(self, book):
        chapters = []
        for chapter in book["chapters"]:
            item = {k: v for k, v in chapter.items() if k not in ("_abs", "_sub")}
            item["has_subtitle"] = bool(chapter.get("has_subtitle") or chapter.get("_sub"))
            chapters.append(item)
        meta = self.meta_of(book["id"])
        data = {
            "id": book["id"],
            "title": self.display_title(book),
            "author": meta.get("author") or "",
            "narrator": meta.get("narrator") or "",
            "description": meta.get("description") or "",
            "tags": meta.get("tags") or "",
            "genre": meta.get("genre") or "",
            "publisher": meta.get("publisher") or "",
            "theme_color": meta.get("theme_color") or "",
            "skip_intro": self.meta_seconds(meta, "skip_intro"),
            "skip_outro": self.meta_seconds(meta, "skip_outro"),
            "scraped": bool(meta),
            "chapter_count": book["chapter_count"],
            "size": book["size"],
            "mtime": book["mtime"],
            "chapters": chapters,
            "has_subtitle": bool(book.get("has_subtitle") or any(c["has_subtitle"] for c in chapters)),
        }
        progress = self.book_progress(book["id"])
        chapter_count = max(1, book["chapter_count"])
        durations = (self.progress["books"].get(book["id"]) or {}).get("durations", {})
        ratio = 0.0
        if progress["duration"] > 0:
            ratio = min(1.0, progress["position"] / progress["duration"])
        percent = 1.0 if progress["finished"] else min(1.0, (progress["chapter"] + ratio) / chapter_count)
        data["progress"] = dict(progress, percent=round(percent * 100, 1), durations=durations)
        data["favorite"] = book["id"] in self.progress["favorites"]
        data["cover_url"] = self.cover_url(book)
        data["platform"] = self.platform_record(book["id"])
        return data


STATE = AppState()


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------

def cover_svg(title):
    digest = hashlib.md5(title.encode("utf-8")).hexdigest()
    hue = int(digest[:6], 16) % 360
    hue2 = (hue + 28) % 360
    char = ""
    for ch in title:
        if ch.strip():
            char = ch
            break
    c1 = "hsl(%d,42%%,34%%)" % hue
    c2 = "hsl(%d,38%%,15%%)" % hue2
    safe = (char.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="480" viewBox="0 0 360 480">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/></linearGradient></defs>'
        '<rect width="360" height="480" rx="18" fill="url(#g)"/>'
        '<g opacity="0.20" stroke="#ffffff" stroke-width="3" fill="none">'
        '<path d="M52 84h256v312H52z" stroke-opacity="0.25"/>'
        '<path d="M100 84v312M180 84v312M260 84v312" stroke-opacity="0.12"/>'
        '</g>'
        '<g opacity="0.55">'
        '<rect x="42" y="352" width="34" height="60" rx="4" fill="#ffffff" fill-opacity="0.22"/>'
        '<rect x="86" y="326" width="34" height="86" rx="4" fill="#ffffff" fill-opacity="0.30"/>'
        '<rect x="130" y="300" width="34" height="112" rx="4" fill="#ffffff" fill-opacity="0.38"/>'
        '</g>'
        '<text x="180" y="238" font-size="64" font-family="-apple-system,\'PingFang SC\',\'Microsoft YaHei\',sans-serif" '
        'fill="#ffffff" fill-opacity="0.92" text-anchor="middle">%s</text>'
        '</svg>' % (c1, c2, safe)
    )


def avatar_svg(name):
    digest = hashlib.md5((name or "?").encode("utf-8")).hexdigest()
    hue = int(digest[:6], 16) % 360
    char = ""
    for ch in (name or "?"):
        if ch.strip():
            char = ch
            break
    safe = char.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">'
        '<defs><linearGradient id="a" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="hsl(%d,46%%,46%%)"/>'
        '<stop offset="1" stop-color="hsl(%d,42%%,27%%)"/></linearGradient></defs>'
        '<rect width="128" height="128" fill="url(#a)"/>'
        '<text x="64" y="82" font-size="54" text-anchor="middle" fill="#ffffff" '
        'fill-opacity="0.92" font-family="-apple-system,\'PingFang SC\',\'Microsoft YaHei\',sans-serif">%s</text>'
        '</svg>' % (hue, (hue + 30) % 360, safe)
    )


def book_api(state, book):
    return state.book_payload(book)


class Handler(BaseHTTPRequestHandler):
    server_version = "ShuYin/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 基础响应 ----------
    def session_cookie(self, token):
        """登录态 Cookie：路径跟随入口（中继带前缀 / 直连为根），供 <audio>/<img> 等媒体请求自动携带"""
        raw = urllib.parse.urlparse(self.path).path
        path = APP_RELAY_PREFIX + "/" if raw.startswith(APP_RELAY_PREFIX) else "/"
        return "shuyin_token=%s; Path=%s; Max-Age=%d; HttpOnly; SameSite=Lax" % (
            token, path, 30 * 24 * 3600)

    def clear_session_cookie(self):
        raw = urllib.parse.urlparse(self.path).path
        path = APP_RELAY_PREFIX + "/" if raw.startswith(APP_RELAY_PREFIX) else "/"
        return "shuyin_token=; Path=%s; Max-Age=0; HttpOnly; SameSite=Lax" % path

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        cookie = getattr(self, "_pending_cookie", "")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message, status=400):
        self.send_json({"ok": False, "error": message}, status)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return None

    def serve_file(self, path, cache=True):
        try:
            stat = os.stat(path)
        except OSError:
            self.send_error_json("文件不存在", 404)
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".json": "application/json; charset=utf-8",
            ".ico": "image/x-icon",
        }.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fp:
            body = fp.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", "public, max-age=3600")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def handle_network(self):
        """返回本机可用的访问地址（IPv4 + IPv6）与外网地址，供 Web 页生成二维码"""
        urls = network_endpoints(LISTEN_PORT)
        public = normalize_public_url(STATE.settings.get("public_url", ""))
        host_header = (self.headers.get("Host") or "").strip()
        current = ""
        if host_header:
            current = "http://" + host_header
            # 将当前访问地址对应的条目排到最前，便于优先展示
            for idx, item in enumerate(urls):
                if item["url"] == current or host_header == item["ip"] or \
                        host_header == "[%s]" % item["ip"]:
                    urls.insert(0, urls.pop(idx))
                    break
        self.send_json({
            "ok": True,
            "port": LISTEN_PORT,
            "current": current,
            "public": public,
            "ipv6": any(item["family"] == "ipv6" for item in urls),
            "urls": urls,
        })

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        raw_path = urllib.parse.unquote(parsed.path)
        if raw_path == APP_RELAY_PREFIX:
            target = APP_RELAY_PREFIX + "/"
            if parsed.query:
                target += "?" + parsed.query
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = app_route_path(raw_path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/" or path == "/index.html":
                self.serve_file(os.path.join(STATIC_DIR, "index.html"), cache=False)
            elif path.startswith("/static/"):
                name = path[len("/static/"):]
                target = os.path.normpath(os.path.join(STATIC_DIR, name))
                if target.startswith(STATIC_DIR):
                    self.serve_file(target, cache=False)
                else:
                    self.send_error_json("非法路径", 403)
            elif path == "/favicon.svg":
                self.serve_file(os.path.join(STATIC_DIR, "favicon.svg"))
            elif path == "/api/status/summary":
                self.send_json({"ok": True, "platform": STATE.platform_summary()})
            elif path == "/api/library":
                self.handle_library(query)
            elif path.startswith("/api/book/"):
                self.handle_book(path[len("/api/book/"):])
            elif path == "/api/network":
                self.handle_network()
            elif path == "/api/scan/status":
                self.handle_scan_status()
            elif path == "/api/settings":
                self.send_json({
                    "ok": True,
                    "roots": STATE.resolve_roots(),
                    "extra_roots": STATE.settings.get("extra_roots", []),
                    "dir_roles": STATE.root_entries(),
                    "bgm_dir": STATE.bgm_dir_current(),
                    "scrape": STATE.scrape_config(),
                    "scanned_at": STATE.scanned_at,
                    "scan_report": STATE.scan_report,
                    "fnos_login": {
                        "enabled": STATE.fnos_enabled(),
                        "endpoint": STATE.fnos_endpoint(),
                    },
                })
            elif path == "/api/scrape/search":
                self.handle_scrape_search(query)
            elif path == "/api/image-proxy":
                self.handle_image_proxy(query)
            elif path == "/api/progress":
                self.send_json({"ok": True, "progress": STATE.progress})
            elif path == "/api/progress/recent":
                self.send_json({"ok": True, "progress": STATE.compat_recent()})
            elif path.startswith("/api/progress/"):
                self.send_json(STATE.compat_progress(path[len("/api/progress/"):]))
            elif path == "/api/books":
                self.send_json({"ok": True, "books": [STATE.compat_book(b) for b in STATE.books]})
            elif path.startswith("/api/books/") and path.endswith("/subtitle"):
                self.handle_book_subtitle(path[len("/api/books/"):-len("/subtitle")])
            elif path.startswith("/api/books/"):
                self.handle_compat_book(path[len("/api/books/"):])
            elif path == "/api/playlists":
                self.handle_playlists_list()
            elif path.startswith("/api/playlists/"):
                self.handle_playlist_detail(path[len("/api/playlists/"):])
            elif path == "/api/favorites":
                books = [STATE.compat_book(b) for b in STATE.books
                         if b["id"] in STATE.progress["favorites"]]
                self.send_json({
                    "ok": True,
                    "books": books,
                    "favorites": books,
                    "ids": [b["id"] for b in books],
                })
            elif path == "/api/auth/me":
                self.handle_auth_me()
            elif path == "/api/users":
                self.handle_users_list()
            elif path.startswith("/api/avatar/"):
                self.handle_avatar(path[len("/api/avatar/"):])
            elif path == "/api/libraries":
                self.send_json({"ok": True, "libraries": [
                    {"id": root, "name": os.path.basename(root) or root,
                     "type": "audiobook", "path": root}
                    for root in STATE.roots
                ]})
            elif path == "/api/scraper/sources":
                self.send_json({"ok": True, "sources": STATE.scraper_sources()})
            elif path == "/api/scrapers":
                self.handle_scrapers_list()
            elif path == "/api/v1/fn/con":
                self.send_json({
                    "server_name": "书中有音",
                    "serverName": "书中有音",
                    "version": APP_VERSION,
                    "requires_access_code": False,
                    "requiresAccessCode": False,
                    "musicPort": 0,
                })
            elif path.startswith("/api/stream/"):
                self.handle_compat_stream(path[len("/api/stream/"):])
            elif path.startswith("/api/cover/"):
                self.handle_compat_cover(path[len("/api/cover/"):])
            elif path.startswith("/cover/"):
                self.handle_cover(path[len("/cover/"):])
            elif path.startswith("/cover-img/"):
                self.handle_cover_image(path[len("/cover-img/"):])
            elif path.startswith("/media/"):
                self.handle_media(path[len("/media/"):])
            elif path in ("/api/health", "/health"):
                self.send_json({"ok": True, "version": APP_VERSION, "scanned_at": STATE.scanned_at})
            elif path == "/api/bgm/list":
                self.send_json({
                    "ok": True,
                    "dir": STATE.bgm_dir_current(),
                    "bgm": STATE.bgm_list(),
                })
            elif path == "/api/bgm/roots":
                self.send_json({"ok": True, "roots": STATE.authorized_roots()})
            elif path == "/api/bgm/browse":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                payload, error = STATE.bgm_browse((query.get("p") or [""])[0])
                if error:
                    self.send_error_json(error, 403)
                else:
                    self.send_json({"ok": True, **payload})
            elif path == "/bgm/file":
                self.handle_bgm_file_path((query.get("p") or [""])[0])
            elif path.startswith("/bgm/"):
                self.handle_bgm_file(path[len("/bgm/"):])
            else:
                self.send_error_json("未找到接口", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self.send_error_json("服务端错误: %s" % exc, 500)
            except Exception:
                pass

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = app_route_path(urllib.parse.unquote(parsed.path))
        try:
            if path == "/api/scan":
                started = STATE.start_scan()
                self.send_json({"ok": True, "started": started, **self.scan_snapshot()})
            elif path == "/api/status/refresh":
                self.handle_platform_refresh_all()
            elif path.startswith("/api/books/") and path.endswith("/status/refresh"):
                self.handle_platform_refresh_book(path[len("/api/books/"):-len("/status/refresh")], force="force=1" in parsed.query)
            elif path == "/api/progress":
                self.handle_progress_save()
            elif path == "/api/favorite":
                self.handle_favorite()
            elif path == "/api/settings":
                self.handle_settings_save()
            elif path == "/api/progress/reset":
                self.handle_progress_reset()
            elif path == "/api/scrape/apply":
                self.handle_scrape_apply()
            elif path == "/api/scraper/search":
                self.handle_scraper_search()
            elif path == "/api/auth/login":
                self.handle_compat_login()
            elif path == "/api/auth/fnos/begin":
                self.handle_fnos_begin()
            elif path == "/api/auth/fnos/claim":
                self.handle_fnos_claim()
            elif path == "/api/auth/ticket":
                self.handle_ticket_create()
            elif path == "/api/auth/ticket/claim":
                self.handle_ticket_claim()
            elif path == "/api/auth/logout":
                self.handle_logout()
            elif path == "/api/users":
                self.handle_user_create()
            elif path.startswith("/api/users/") and path.endswith("/avatar"):
                self.handle_avatar_upload(path[len("/api/users/"):-len("/avatar")])
            elif path.startswith("/api/books/") and path.endswith("/chapters/reset"):
                self.handle_compat_chapter_reset(path[len("/api/books/"):-len("/chapters/reset")])
            elif path.startswith("/api/books/") and path.endswith("/scrape-apply"):
                self.handle_compat_scrape_apply(path[len("/api/books/"):-len("/scrape-apply")])
            elif path == "/api/playlists":
                self.handle_playlist_create()
            elif path == "/api/scrapers/upload":
                self.handle_plugin_upload()
            elif path.startswith("/api/scrapers/") and path.endswith("/toggle"):
                self.handle_plugin_toggle(path[len("/api/scrapers/"):-len("/toggle")])
            elif path.startswith("/api/favorites/"):
                self.handle_compat_favorite(path[len("/api/favorites/"):], True)
            elif path == "/api/bgm/upload":
                self.handle_bgm_upload()
            elif path == "/api/bgm/dir":
                self.handle_bgm_dir_set()
            elif path.startswith("/api/v1/auth/"):
                self.send_json({"ok": True, "token": "shuzhongyouyin", "accessCode": ""})
            elif path == "/api/refresh":
                STATE.start_scan()
                self.send_json({"ok": True, **self.scan_snapshot()})
            else:
                self.send_error_json("未找到接口", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self.send_error_json("服务端错误: %s" % exc, 500)
            except Exception:
                pass

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        path = app_route_path(urllib.parse.unquote(parsed.path))
        try:
            if path.startswith("/api/users/"):
                self.handle_user_update(path[len("/api/users/"):])
            elif path.startswith("/api/playlists/"):
                self.handle_playlist_update(path[len("/api/playlists/"):])
            elif path.startswith("/api/books/"):
                self.handle_compat_book_update(path[len("/api/books/"):])
            else:
                self.send_error_json("未找到接口", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self.send_error_json("服务端错误: %s" % exc, 500)
            except Exception:
                pass

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        path = app_route_path(urllib.parse.unquote(parsed.path))
        try:
            if path.startswith("/api/users/"):
                self.handle_user_delete(path[len("/api/users/"):])
            elif path.startswith("/api/scrapers/"):
                self.handle_plugin_delete(path[len("/api/scrapers/"):])
            elif path.startswith("/api/playlists/"):
                self.handle_playlist_delete(path[len("/api/playlists/"):])
            elif path.startswith("/api/favorites/"):
                self.handle_compat_favorite(path[len("/api/favorites/"):], False)
            elif path.startswith("/api/bgm/"):
                self.handle_bgm_delete(path[len("/api/bgm/"):])
            else:
                self.send_error_json("未找到接口", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self.send_error_json("服务端错误: %s" % exc, 500)
            except Exception:
                pass

    # ---------- 接口实现 ----------
    def scan_snapshot(self):
        return {
            "scanning": STATE.scanning,
            "error": STATE.scan_error,
            "started_at": int(STATE.scan_started_at),
            "finished_at": int(STATE.scan_finished_at),
            "book_count": len(STATE.books),
            "roots_scanned": STATE.scan_report.get("scanned", []),
            "roots_skipped": STATE.scan_report.get("skipped", []),
            "duplicates": STATE.scan_report.get("duplicates", 0),
        }

    def handle_library(self, query):
        if query.get("refresh"):
            STATE.start_scan()
        # 只做一次短暂的加锁快照；书籍 payload 的构建放到锁外，
        # 这样单个请求里哪怕 FUSE 卡死，也不会把其它加锁接口一起拖住。
        with STATE.lock:
            snapshot = list(STATE.books)
            roots = list(STATE.roots)
            scanned_at = STATE.scanned_at
            progress_view = dict(STATE.progress)
            platform = STATE.platform_summary()
            scanner = self.scan_snapshot()
        books = [book_api(STATE, book) for book in snapshot]
        books.sort(key=lambda b: natural_key(b["title"]))
        # 书库列表不返回章节明细：有声书章节动辄上千条，全量下发会让
        # 飞牛中继（nginx 代理）因响应过大/超时而加载失败。
        # 客户端打开书籍详情时再按需请求 /api/book/<id>。
        if not query.get("full"):
            for item in books:
                item.pop("chapters", None)
        scanned_roots = scanner.get("roots_scanned") or []
        skipped_roots = scanner.get("roots_skipped") or []
        root_info = []
        for root in roots:
            count = sum(1 for book in snapshot if book.get("root") == root)
            root_info.append({
                "path": root,
                # 首屏不访问 FUSE：命中扫描/跳过名单即视为存在
                "exists": root in scanned_roots or root in skipped_roots,
                "books": count,
            })
        self.send_json({
            "ok": True,
            "books": books,
            "roots": root_info,
            "scanned_at": scanned_at,
            "progress": progress_view,
            "platform": platform,
            **scanner,
        })

    def handle_book(self, book_id):
        book = STATE.books_by_id.get(book_id)
        if not book:
            self.send_error_json("书籍不存在，请重新扫描", 404)
            return
        data = book_api(STATE, book)
        data["ok"] = True
        self.send_json(data)

    def handle_platform_refresh_all(self):
        started = STATE.start_platform_refresh()
        self.send_json({"ok": True, "started": started, "platform": STATE.platform_summary()})

    def handle_platform_refresh_book(self, rest, force=False):
        """/api/books/<book_id>/status/refresh：默认 24h 内走缓存，?force=1 强制刷新"""
        book_id = rest.strip("/")
        book = STATE.books_by_id.get(book_id)
        if not book:
            self.send_error_json("书籍不存在，请重新扫描", 404)
            return
        record = STATE.refresh_platform_status(book, force=force)
        self.send_json({"ok": True, "platform": record, "summary": STATE.platform_summary()})

    def handle_book_subtitle(self, rest):
        """/api/books/<book_id>/chapters/<index>/subtitle：按章节返回解析后的段落"""
        parts = [part for part in rest.split("/") if part]
        if len(parts) != 3 or parts[1] != "chapters":
            self.send_error_json("接口路径错误", 400)
            return
        book_id, index_raw = parts[0], parts[2]
        book = STATE.books_by_id.get(book_id)
        if not book:
            self.send_error_json("书籍不存在，请重新扫描", 404)
            return
        try:
            index = int(index_raw)
        except ValueError:
            self.send_error_json("章节序号错误", 400)
            return
        chapters = book.get("chapters") or []
        if index < 0 or index >= len(chapters):
            self.send_error_json("章节不存在", 404)
            return
        chapter = chapters[index]
        path = chapter.get("_sub") or ""
        if not path or not os.path.isfile(path):
            # 兼容旧书库数据：扫描时未记录字幕的章节按需补查一次
            path = find_subtitle(chapter.get("_abs") or "")
        if not path or not os.path.isfile(path):
            self.send_json({
                "ok": False,
                "has_subtitle": False,
                "error": "本章没有字幕文件",
                "book_id": book_id,
                "chapter": index,
            }, 404)
            return
        try:
            segments, encoding = parse_subtitle_file(path)
        except Exception as exc:
            self.send_error_json("字幕解析失败: %s" % exc, 500)
            return
        self.send_json({
            "ok": True,
            "has_subtitle": True,
            "book_id": book_id,
            "chapter": index,
            "file": os.path.basename(path),
            "format": fmt_ext(path).lstrip("."),
            "encoding": encoding,
            "count": len(segments),
            "segments": segments,
        })

    def handle_scan_status(self):
        self.send_json({"ok": True, **self.scan_snapshot(), "scanned_at": STATE.scanned_at})

    def handle_cover(self, rest):
        name = rest[:-4] if rest.endswith(".svg") else rest
        book = STATE.books_by_id.get(name)
        title = book["title"] if book else "书中有音"
        body = cover_svg(title).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def handle_cover_image(self, rest):
        parts = [p for p in rest.split("/") if p]
        book_id = parts[0] if parts else ""
        book = STATE.books_by_id.get(book_id)
        path = STATE.cover_info(book) if book else None
        if path:
            self.serve_file(path, cache=True)
        else:
            self.handle_cover(book_id + ".svg")

    # ---------- 在线刮削 ----------

    def handle_scrape_search(self, query):
        book_id = (query.get("book_id") or [""])[0]
        text = (query.get("q") or [""])[0].strip()
        book = STATE.books_by_id.get(book_id) if book_id else None
        if not text and book:
            text = STATE.display_title(book)
        if not text:
            self.send_json({"ok": False, "error": "缺少搜索关键词", "items": []})
            return
        items, error = STATE.search_albums(text, source=(query.get("source") or [""])[0])
        self.send_json({
            "ok": error is None,
            "error": error,
            "query": text,
            "items": items,
        })

    def handle_scrape_apply(self):
        data = self.read_json() or {}
        book_id = data.get("book_id") or ""
        book = STATE.books_by_id.get(book_id)
        if not book:
            self.send_error_json("书籍不存在，请重新扫描", 404)
            return
        values = dict(data.get("values") or {})
        for key in ("title", "author", "narrator", "description",
                    "tags", "genre", "year", "publisher"):
            if data.get(key) is not None:
                values.setdefault(key, data[key])
        cover_url = (data.get("cover_url") or "").strip()
        if not cover_url:
            value = values.get("cover")
            if isinstance(value, dict):
                value = value.get("value")
            cover_url = str(value or "").strip()
        cover_result = STATE.apply_scraped_cover(book, cover_url)
        if values:
            STATE.apply_metadata(book_id, values)
        self.send_json({
            "ok": True,
            "cover": cover_result,
            "cover_error": cover_result.get("error"),
            "book": STATE.book_payload(book),
            "chapters": None,
        })

    def handle_image_proxy(self, query):
        target = (query.get("url") or [""])[0]
        config = STATE.scrape_config()
        if not target or not image_host_allowed(target, config["api_base"]):
            self.send_error_json("只允许代理白名单图片域名", 403)
            return
        host = urllib.parse.urlparse(target).netloc.lower()
        if "lrts" in host:
            referer = "https://www.lrts.me/"
        elif "missevan" in host or "maoercdn" in host:
            referer = MISSEVAN_BASE + "/"
        else:
            referer = DEFAULT_SCRAPE_BASE + "/"
        req = urllib.request.Request(target, headers={
            "User-Agent": BROWSER_UA,
            "Referer": referer,
        })
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type") or ""
        except Exception as exc:
            self.send_error_json("图片获取失败: %s" % exc, 502)
            return
        if not ctype.startswith("image/"):
            ctype = mimetypes.guess_type(target)[0] or "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---------- tingmusic 兼容接口 ----------

    def handle_compat_book(self, rest):
        parts = [p for p in rest.split("/") if p]
        if not parts:
            self.send_error_json("未找到接口", 404)
            return
        book = STATE.books_by_id.get(parts[0])
        if not book:
            self.send_error_json("书籍不存在", 404)
            return
        if len(parts) == 1:
            self.send_json(STATE.compat_book(book))
            return
        if parts[1] == "chapters":
            chapters = [STATE.compat_chapter(book, c, i)
                        for i, c in enumerate(book["chapters"])]
            self.send_json({"ok": True, "chapters": chapters})
            return
        self.send_error_json("未找到接口", 404)

    def handle_compat_stream(self, chapter_id):
        try:
            book_id, index_raw = chapter_id.rsplit("_", 1)
            index = int(index_raw)
        except (AttributeError, ValueError):
            self.send_error_json("章节参数错误", 400)
            return
        book = STATE.books_by_id.get(book_id)
        if not book:
            self.send_error_json("书籍不存在", 404)
            return
        self.send_media(book, index)

    def handle_compat_cover(self, rest):
        book_id = rest.split(".")[0].split("/")[0]
        book = STATE.books_by_id.get(book_id)
        path = STATE.cover_info(book) if book else None
        if path:
            self.serve_file(path, cache=True)
        else:
            self.send_error_json("封面不存在", 404)

    def request_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if token:
                return token
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        token = (query.get("token") or [""])[0].strip()
        if token:
            return token
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            key, _, value = part.strip().partition("=")
            if key in ("shuyin_token", "ting_reader_token") and value:
                return value
        return ""

    def require_auth(self):
        token = self.request_token()
        user = STATE.session_user(token)
        if not user:
            self.send_error_json("请先登录", 401)
            return user
        # 老登录态（只有 localStorage token、没有 Cookie）在这里补齐，媒体请求随即可用
        self._pending_cookie = self.session_cookie(token)
        return user

    def handle_auth_me(self):
        user = self.require_auth()
        if not user:
            return
        self.send_json({"ok": True, "user": STATE.public_user(user)})

    def handle_compat_login(self):
        data = self.read_json() or {}
        username = (data.get("username") or data.get("user") or "").strip()
        password = str(data.get("password") or "")
        if not username or not password:
            self.send_error_json("请输入账号和密码", 400)
            return
        user = STATE.find_user(username)
        if user and user.get("password") == hash_password(password):
            self.send_login(user)
            return
        # 本地没有该账号（或该账号是飞牛映射账号）时，改用飞牛账号密码校验
        if STATE.fnos_enabled() and (not user or user.get("login_source") == "fnos"):
            fnos_user, error = STATE.fnos_password_login(username, password)
            if fnos_user:
                self.send_login(fnos_user)
                return
            self.send_error_json(error or "账号或密码不正确", 401)
            return
        self.send_error_json("账号或密码不正确", 401)

    def send_login(self, user):
        token = STATE.new_session(user)
        self._pending_cookie = self.session_cookie(token)
        payload = STATE.public_user(user)
        payload["status"] = "active"
        self.send_json({"ok": True, "token": token, "user": payload})

    def handle_fnos_begin(self):
        """生成飞牛 ID 授权链接（登录页调用，无需登录态）"""
        self.read_json()
        if not STATE.fnos_enabled():
            self.send_error_json("未启用飞牛 ID 登录", 403)
            return
        payload, error = STATE.fnos_begin(self.headers.get("Host") or "")
        if error:
            self.send_error_json(error, 400)
            return
        self.send_json({"ok": True, **payload})

    def handle_fnos_claim(self):
        """用授权码换取本地登录态（登录页调用，无需登录态）"""
        data = self.read_json() or {}
        user, error = STATE.fnos_claim(data.get("state"), data.get("code"))
        if error:
            self.send_error_json(error, 400)
            return
        token = STATE.new_session(user)
        self._pending_cookie = self.session_cookie(token)
        payload = STATE.public_user(user)
        payload["status"] = "active"
        self.send_json({"ok": True, "token": token, "user": payload})

    def handle_ticket_create(self):
        """为已登录的 Web 会话生成扫码登录票据"""
        # 先消费请求体，避免残留字节污染同一条 keep-alive 连接上的下一个请求
        self.read_json()
        user = self.require_auth()
        if not user:
            return
        ticket = STATE.create_login_ticket(user)
        self.send_json({
            "ok": True,
            "ticket": ticket,
            "user": user["username"],
            "expires_in": LOGIN_TICKET_TTL,
        })

    def handle_ticket_claim(self):
        """手机端凭票据换取登录态（票据单次有效）"""
        data = self.read_json() or {}
        user = STATE.claim_login_ticket(str(data.get("ticket") or ""))
        if not user:
            self.send_error_json("登录票据无效或已过期，请重新扫码", 401)
            return
        token = STATE.new_session(user)
        self._pending_cookie = self.session_cookie(token)
        payload = STATE.public_user(user)
        payload["status"] = "active"
        self.send_json({"ok": True, "token": token, "user": payload})

    def handle_logout(self):
        STATE.drop_session(self.request_token())
        self._pending_cookie = self.clear_session_cookie()
        self.send_json({"ok": True})

    def handle_users_list(self):
        user = self.require_auth()
        if not user:
            return
        self.send_json({
            "ok": True,
            "users": [STATE.public_user(item) for item in STATE.users["users"]],
            "current": STATE.public_user(user),
        })

    def handle_user_create(self):
        user = self.require_auth()
        if not user:
            return
        if user.get("role") != "admin":
            self.send_error_json("只有管理员可以添加账号", 403)
            return
        data = self.read_json() or {}
        role = data.get("role") if data.get("role") in ("admin", "user") else "user"
        new_user, error = STATE.create_user(data.get("username"), data.get("password"), role)
        if error:
            self.send_error_json(error, 400)
            return
        avatar = data.get("avatar")
        if avatar:
            avatar_error = STATE.save_avatar(new_user, avatar)
            if avatar_error:
                self.send_error_json(avatar_error, 400)
                return
        self.send_json({"ok": True, "user": STATE.public_user(new_user)})

    def handle_user_update(self, rest):
        user_id = [p for p in rest.split("/") if p]
        user_id = user_id[0] if user_id else ""
        current = self.require_auth()
        if not current:
            return
        target = STATE.user_by_id(user_id)
        if not target:
            self.send_error_json("账号不存在", 404)
            return
        if current["id"] != target["id"] and current.get("role") != "admin":
            self.send_error_json("没有权限修改该账号", 403)
            return
        data = self.read_json() or {}
        role = data.get("role")
        if role not in ("admin", "user"):
            role = None
        if role and current.get("role") != "admin":
            self.send_error_json("只有管理员可以调整账号角色", 403)
            return
        error = STATE.update_user(target, data.get("username"), data.get("password"), role)
        if error:
            self.send_error_json(error, 400)
            return
        self.send_json({"ok": True, "user": STATE.public_user(target), "current": STATE.public_user(current)})

    def handle_user_delete(self, rest):
        user_id = [p for p in rest.split("/") if p]
        user_id = user_id[0] if user_id else ""
        current = self.require_auth()
        if not current:
            return
        target = STATE.user_by_id(user_id)
        if not target:
            self.send_error_json("账号不存在", 404)
            return
        if target["id"] == current["id"]:
            self.send_error_json("不能删除当前登录账号", 400)
            return
        if current.get("role") != "admin":
            self.send_error_json("只有管理员可以删除账号", 403)
            return
        if len(STATE.users["users"]) <= 1:
            self.send_error_json("至少保留一个账号", 400)
            return
        STATE.delete_user(target)
        self.send_json({"ok": True})

    def handle_avatar_upload(self, user_id):
        current = self.require_auth()
        if not current:
            return
        target = STATE.user_by_id(user_id)
        if not target:
            self.send_error_json("账号不存在", 404)
            return
        if current["id"] != target["id"] and current.get("role") != "admin":
            self.send_error_json("没有权限修改该账号", 403)
            return
        data = self.read_json() or {}
        error = STATE.save_avatar(target, data.get("data"))
        if error:
            self.send_error_json(error, 400)
            return
        self.send_json({"ok": True, "user": STATE.public_user(target)})

    def handle_avatar(self, rest):
        user_id = [p for p in rest.split("/") if p]
        user_id = user_id[0] if user_id else ""
        user = STATE.user_by_id(user_id)
        path = STATE.avatar_path(user) if user else None
        if path:
            self.serve_file(path, cache=True)
            return
        name = (user or {}).get("username") or "?"
        body = avatar_svg(name).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def handle_scrapers_list(self):
        user = self.require_auth()
        if not user:
            return
        self.send_json({
            "ok": True,
            "plugins": [STATE.plugin_payload(item) for item in STATE.scrapers["plugins"]],
            "sources": STATE.scraper_sources(),
        })

    def handle_plugin_upload(self):
        user = self.require_auth()
        if not user:
            return
        data = self.read_json() or {}
        plugin, error = STATE.install_plugin(data.get("filename"), data.get("data"))
        if error:
            self.send_error_json(error, 400)
            return
        self.send_json({"ok": True, "plugin": STATE.plugin_payload(plugin),
                        "sources": STATE.scraper_sources()})

    def handle_plugin_toggle(self, rest):
        user = self.require_auth()
        if not user:
            return
        plugin_id = [p for p in rest.split("/") if p]
        plugin_id = plugin_id[0] if plugin_id else ""
        data = self.read_json() or {}
        plugin, error = STATE.set_plugin_enabled(plugin_id, bool(data.get("enabled")))
        if error:
            self.send_error_json(error, 404)
            return
        self.send_json({"ok": True, "plugin": STATE.plugin_payload(plugin),
                        "sources": STATE.scraper_sources()})

    def handle_plugin_delete(self, rest):
        user = self.require_auth()
        if not user:
            return
        plugin_id = [p for p in rest.split("/") if p]
        plugin_id = plugin_id[0] if plugin_id else ""
        error = STATE.remove_plugin(plugin_id)
        if error:
            self.send_error_json(error, 404)
            return
        self.send_json({"ok": True, "sources": STATE.scraper_sources()})

    def handle_scraper_search(self):
        data = self.read_json() or {}
        text = (data.get("query") or data.get("title") or "").strip()
        book_id = data.get("book_id") or ""
        book = STATE.books_by_id.get(book_id) if book_id else None
        if not text and book:
            text = STATE.display_title(book)
        if not text:
            self.send_error_json("缺少搜索关键词", 400)
            return
        items, error = STATE.search_albums(text, source=data.get("source") or "")
        if error:
            self.send_json({"ok": False, "error": error, "items": [], "results": []})
            return
        self.send_json({"ok": True, "items": items, "results": items})

    def handle_compat_scrape_apply(self, book_id):
        data = self.read_json() or {}
        book = STATE.books_by_id.get(book_id)
        if not book:
            self.send_error_json("书籍不存在", 404)
            return
        values = {}
        external_id = ""
        source = ""
        fields = data.get("fields")
        if isinstance(fields, dict):
            for key, item in fields.items():
                if isinstance(item, dict):
                    values[key] = item.get("value")
                    source = item.get("source") or source
                    external_id = str(item.get("external_id") or external_id)
                elif item is not None:
                    values[key] = item
        elif isinstance(data.get("book"), dict):
            values.update(data["book"])
        cover_url = str(values.get("cover") or "").strip()
        if source == "missevan" and values:
            detail, _detail_error = missevan_drama_detail(external_id)
            if detail:
                if not str(values.get("title") or "").strip():
                    values["title"] = detail.get("title") or ""
                if not str(values.get("author") or "").strip():
                    values["author"] = detail.get("author") or ""
                if not str(values.get("description") or "").strip():
                    values["description"] = detail.get("description") or ""
                if not cover_url:
                    cover_url = str(detail.get("cover") or "").strip()
        elif source == "ximalaya" and external_id and not cover_url:
            items, _ = STATE.search_albums(STATE.display_title(book))
            match = next((i for i in items if i.get("id") == external_id), None)
            if match and match.get("cover_url"):
                cover_url = match["cover_url"]
        # 封面与章节互不影响：封面失败只记原因，章节标题照常更新
        cover_result = STATE.apply_scraped_cover(book, cover_url)
        STATE.apply_metadata(book_id, {k: v for k, v in values.items() if v is not None})
        chapter_result = None
        if data.get("update_chapters"):
            chapter_result = STATE.apply_chapter_scrape(
                book,
                source or data.get("source"),
                external_id or data.get("external_id"),
            )
        self.send_json({"ok": True, "book": STATE.compat_book(book),
                        "chapters": chapter_result, "cover": cover_result})

    def handle_compat_chapter_reset(self, book_id):
        """按书重置章节标题（回滚到扫描时的文件名标题）"""
        book = STATE.books_by_id.get(book_id)
        if not book:
            self.send_error_json("书籍不存在", 404)
            return
        restored, had_override = STATE.reset_chapter_titles(book)
        self.send_json({"ok": True, "had_override": had_override,
                        "restored": restored, "book": STATE.compat_book(book)})

    def handle_compat_book_update(self, rest):
        parts = [p for p in rest.split("/") if p]
        book_id = parts[0] if parts else ""
        book = STATE.books_by_id.get(book_id)
        if not book:
            self.send_error_json("书籍不存在", 404)
            return
        data = self.read_json() or {}
        values = {}
        for key in ("title", "author", "narrator", "description",
                    "tags", "genre", "year", "publisher"):
            value = data.get(key)
            if isinstance(value, str):
                values[key] = value
        for key in ("skip_intro", "skip_outro"):
            if data.get(key) is not None:
                try:
                    values[key] = round(max(0.0, min(600.0, float(data[key])),), 1)
                except (TypeError, ValueError):
                    pass
        theme_color = data.get("theme_color")
        if isinstance(theme_color, str) and re.match(r"^#[0-9a-fA-F]{6}$", theme_color.strip()):
            values["theme_color"] = theme_color.strip().lower()
        fields = data.get("fields")
        if isinstance(fields, dict):
            for key, item in fields.items():
                if isinstance(item, dict) and item.get("value") is not None:
                    values[key] = item["value"]
                elif item is not None:
                    values[key] = item
        if values:
            STATE.apply_metadata(book_id, values)
        self.send_json({"ok": True, "book": STATE.compat_book(book)})

    def handle_compat_favorite(self, book_id, favorite):
        book_id = book_id.split("/")[0]
        if book_id not in STATE.books_by_id:
            self.send_error_json("书籍不存在", 404)
            return
        with STATE.lock:
            favorites = STATE.progress["favorites"]
            if favorite and book_id not in favorites:
                favorites.append(book_id)
            if not favorite and book_id in favorites:
                favorites.remove(book_id)
            STATE.save_progress()
        self.send_json({"ok": True, "favorite": favorite})

    # ---------------- 书单 ----------------

    def visible_playlists(self, user):
        items = STATE.playlists["playlists"]
        if user.get("role") != "admin":
            return [p for p in items if p.get("user_id") == user.get("id")]
        return list(items)

    def find_playlist(self, playlist_id):
        for playlist in STATE.playlists["playlists"]:
            if playlist.get("id") == playlist_id:
                return playlist
        return None

    def authorize_playlist(self, playlist_id):
        """返回 (playlist, user)；未登录/不存在/无权限时已自行响应"""
        user = self.require_auth()
        if not user:
            return None, None
        playlist = self.find_playlist(playlist_id)
        if not playlist:
            self.send_error_json("书单不存在", 404)
            return None, user
        if not STATE.playlist_owner(playlist, user):
            self.send_error_json("无权操作该书单", 403)
            return None, user
        return playlist, user

    def handle_playlists_list(self):
        user = self.require_auth()
        if not user:
            return
        with STATE.lock:
            payloads = [STATE.playlist_payload(p) for p in self.visible_playlists(user)]
        payloads.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
        # data 字段同时满足网页与鸿蒙客户端的解析方式
        self.send_json({"ok": True, "data": payloads, "playlists": payloads})

    def handle_playlist_detail(self, rest):
        playlist_id = [p for p in rest.split("/") if p]
        if not playlist_id:
            self.send_error_json("缺少书单 ID", 400)
            return
        playlist, _user = self.authorize_playlist(playlist_id[0])
        if not playlist:
            return
        with STATE.lock:
            payload = STATE.playlist_payload(playlist)
        self.send_json({"ok": True, "data": payload})

    def handle_playlist_create(self):
        user = self.require_auth()
        if not user:
            return
        data = self.read_json() or {}
        title = (data.get("title") or "").strip() if isinstance(data.get("title"), str) else ""
        if not title:
            self.send_error_json("请填写书单名称", 400)
            return
        description = data.get("description") if isinstance(data.get("description"), str) else ""
        with STATE.lock:
            items = data.get("items")
            if isinstance(items, list):
                normalized = STATE.normalize_playlist_items(items)
            else:
                normalized = STATE.playlist_items_from_ids(data.get("book_ids") or [])
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            playlist = {
                "id": STATE.new_playlist_id(),
                "user_id": user.get("id") or "",
                "title": title[:80],
                "description": description[:400],
                "created_at": now,
                "updated_at": now,
                "items": normalized,
            }
            STATE.playlists["playlists"].append(playlist)
            STATE.save_playlists()
            payload = STATE.playlist_payload(playlist)
        self.send_json({"ok": True, "data": payload})

    def handle_playlist_update(self, rest):
        playlist_id = [p for p in rest.split("/") if p]
        if not playlist_id:
            self.send_error_json("缺少书单 ID", 400)
            return
        playlist, _user = self.authorize_playlist(playlist_id[0])
        if not playlist:
            return
        data = self.read_json() or {}
        changed = False
        with STATE.lock:
            if isinstance(data.get("title"), str):
                title = data["title"].strip()
                if title:
                    playlist["title"] = title[:80]
                    changed = True
            if isinstance(data.get("description"), str):
                playlist["description"] = data["description"].strip()[:400]
                changed = True
            if isinstance(data.get("items"), list):
                playlist["items"] = STATE.normalize_playlist_items(data["items"])
                changed = True
            elif isinstance(data.get("book_ids"), list):
                playlist["items"] = STATE.playlist_items_from_ids(data["book_ids"])
                changed = True
            if changed:
                playlist["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                STATE.save_playlists()
            payload = STATE.playlist_payload(playlist)
        self.send_json({"ok": True, "data": payload})

    def handle_playlist_delete(self, rest):
        playlist_id = [p for p in rest.split("/") if p]
        if not playlist_id:
            self.send_error_json("缺少书单 ID", 400)
            return
        playlist, _user = self.authorize_playlist(playlist_id[0])
        if not playlist:
            return
        with STATE.lock:
            STATE.playlists["playlists"].remove(playlist)
            STATE.save_playlists()
        self.send_json({"ok": True, "deleted": playlist_id[0]})

    def handle_progress_save(self):
        data = self.read_json()
        if not data:
            self.send_error_json("参数错误", 400)
            return
        book_id = data.get("book_id")
        chapter_raw = data.get("chapter")
        chapter_id = data.get("chapter_id")
        if chapter_id and chapter_raw is None:
            try:
                book_id = book_id or chapter_id.rsplit("_", 1)[0]
                chapter_raw = chapter_id.rsplit("_", 1)[1]
            except (AttributeError, IndexError):
                self.send_error_json("chapter_id 格式错误", 400)
                return
        if not book_id:
            self.send_error_json("缺少 book_id", 400)
            return
        try:
            chapter = max(0, int(chapter_raw or 0))
            position = max(0.0, float(data.get("position", 0.0)))
            duration = max(0.0, float(data.get("duration", 0.0)))
        except (TypeError, ValueError):
            self.send_error_json("参数格式错误", 400)
            return
        STATE.update_progress(book_id, chapter, position, duration, bool(data.get("finished")))
        self.send_json({"ok": True})

    def handle_favorite(self):
        data = self.read_json() or {}
        book_id = data.get("book_id")
        if not book_id:
            self.send_error_json("缺少 book_id", 400)
            return
        favorite = bool(data.get("favorite"))
        with STATE.lock:
            favorites = STATE.progress["favorites"]
            if favorite and book_id not in favorites:
                favorites.append(book_id)
            if not favorite and book_id in favorites:
                favorites.remove(book_id)
            STATE.save_progress()
        self.send_json({"ok": True, "favorite": favorite})

    def _normalize_dir_roles(self, raw_entries, warnings):
        """整理目录角色：去重、校验角色、同路径双角色给出警告"""
        entries, seen = [], {}
        for item in raw_entries or []:
            if isinstance(item, str):
                path, role = item.strip(), "book"
            elif isinstance(item, dict):
                path = str(item.get("path") or "").strip()
                role = item.get("role") if item.get("role") in ("book", "bgm", "media") else "book"
            else:
                continue
            if not path:
                continue
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                if seen[key]["role"] != role:
                    warnings.append("目录 %s 同时被设为「%s」和「%s」，已按先出现的角色处理"
                                    % (path, ROLE_LABELS.get(seen[key]["role"], seen[key]["role"]),
                                       ROLE_LABELS.get(role, role)))
                continue
            entry = {"path": path, "role": role}
            seen[key] = entry
            entries.append(entry)
        return entries

    def handle_settings_save(self):
        data = self.read_json() or {}
        warnings = []
        old_roots = STATE.resolve_roots()
        changed_roles = False
        roles_input = data.get("dir_roles")
        if isinstance(roles_input, list):
            entries = self._normalize_dir_roles(roles_input, warnings)
            STATE.settings["roots"] = entries
            STATE.settings["extra_roots"] = [e["path"] for e in entries
                                             if e["role"] in ("book", "media")]
            changed_roles = True
        else:
            extra = data.get("extra_roots")
            if isinstance(extra, list):
                cleaned, seen = [], set()
                for item in extra:
                    if not isinstance(item, str):
                        continue
                    item = item.strip()
                    if item and item not in seen:
                        seen.add(item)
                        cleaned.append(item)
                # 旧版只维护书库目录：保留已有 bgm/media 角色，替换书库角色
                keep = [e for e in STATE.root_entries() if e.get("role") not in ("book", "media")]
                merged = keep + [{"path": path, "role": "book"} for path in cleaned]
                STATE.settings["roots"] = self._normalize_dir_roles(merged, warnings)
                STATE.settings["extra_roots"] = cleaned
                changed_roles = True
        public_url = data.get("public_url")
        if isinstance(public_url, str):
            STATE.settings["public_url"] = normalize_public_url(public_url)
        fnos = data.get("fnos")
        if isinstance(fnos, dict):
            if isinstance(fnos.get("enabled"), bool):
                STATE.settings["fnos_login_enabled"] = fnos["enabled"]
            for key in ("endpoint", "public_endpoint"):
                value = fnos.get(key)
                if isinstance(value, str):
                    STATE.settings["fnos_" + key] = value.strip().rstrip("/")
            STATE.save_settings()
        scrape = data.get("scrape")
        if isinstance(scrape, dict):
            current = STATE.settings.setdefault("scrape", {})
            if "api_base" in scrape:
                value = scrape["api_base"]
                current["api_base"] = value.strip() if isinstance(value, str) else ""
            current.pop("proxy", None)  # 代理已下线，忽略并清除旧配置
            STATE.save_settings()
        if changed_roles or isinstance(public_url, str):
            STATE.save_settings()
        # 删除目录后立即清理对应索引，避免重扫完成前留下僵尸书籍
        removed_books = 0
        if changed_roles:
            new_roots = STATE.resolve_roots()
            removed = [root for root in old_roots if root not in new_roots]
            if removed:
                with STATE.lock:
                    kept = [book for book in STATE.books if book.get("root") not in removed]
                    removed_books = len(STATE.books) - len(kept)
                    if removed_books:
                        STATE.books = kept
                        STATE.books_by_id = {book["id"]: book for book in kept}
                        STATE.save_library()
        started = STATE.start_scan() if data.get("rescan", True) else False
        self.send_json({
            "ok": True,
            "extra_roots": STATE.settings.get("extra_roots", []),
            "dir_roles": STATE.root_entries(),
            "warnings": warnings,
            "removed_books": removed_books,
            "public_url": STATE.settings.get("public_url", ""),
            "scrape": STATE.scrape_config(),
            "fnos_login": {
                "enabled": STATE.fnos_enabled(),
                "endpoint": STATE.fnos_endpoint(),
            },
            "started": started,
        })

    def handle_progress_reset(self):
        data = self.read_json() or {}
        book_id = data.get("book_id")
        with STATE.lock:
            if book_id:
                STATE.progress["books"].pop(book_id, None)
                if STATE.progress.get("last_played") == book_id:
                    STATE.progress["last_played"] = None
            else:
                STATE.progress["books"] = {}
                STATE.progress["favorites"] = []
                STATE.progress["last_played"] = None
            STATE.save_progress()
        self.send_json({"ok": True})

    def handle_media(self, rest):
        parts = [p for p in rest.split("/") if p]
        if len(parts) != 2:
            self.send_error_json("参数错误", 400)
            return
        book_id, chapter_raw = parts[0], parts[1]
        book = STATE.books_by_id.get(book_id)
        if not book:
            self.send_error_json("书籍不存在", 404)
            return
        try:
            index = int(chapter_raw)
        except ValueError:
            self.send_error_json("章节参数错误", 400)
            return
        if index < 0 or index >= len(book["chapters"]):
            self.send_error_json("章节不存在", 404)
            return
        self.send_media(book, index)

    def handle_bgm_file_path(self, raw):
        """提供授权目录音乐文件夹中的背景音乐（支持 Range 请求以便 seek）"""
        user = self.require_auth()
        if not user:
            return
        real = STATE.path_within_authorized(raw)
        if not real or not os.path.isfile(real) or fmt_ext(real) not in BGM_EXTS:
            self.send_error_json("文件不存在或不在授权目录内", 404)
            return
        self.stream_file(real)

    def handle_bgm_file(self, rest):
        """提供应用内上传的背景音乐文件（支持 Range 请求以便 seek）"""
        user = self.require_auth()
        if not user:
            return
        filename = os.path.basename(rest or "")
        if not filename or ".." in filename or filename.startswith("/"):
            self.send_error_json("文件名非法", 400)
            return
        path = os.path.join(STATE.bgm_dir, filename)
        real = os.path.realpath(path)
        if not real.startswith(os.path.realpath(STATE.bgm_dir)):
            self.send_error_json("路径非法", 400)
            return
        if not os.path.isfile(path):
            self.send_error_json("文件不存在", 404)
            return
        self.stream_file(path)

    def handle_bgm_upload(self):
        user = self.require_auth()
        if not user:
            return
        data = self.read_json() or {}
        item, error = STATE.bgm_save(data.get("filename"), data.get("data"))
        if error:
            self.send_error_json(error, 400)
            return
        self.send_json({"ok": True, "item": item, "bgm": STATE.bgm_list()})

    def handle_bgm_dir_set(self):
        user = self.require_auth()
        if not user:
            return
        data = self.read_json() or {}
        payload, error = STATE.bgm_dir_set(data.get("path"))
        if error:
            self.send_error_json(error, 403)
            return
        # 选中音乐目录后登记为 bgm 角色，书库扫描整棵子树跳过
        path = payload["dir"]
        entries = STATE.root_entries()
        key = os.path.normcase(os.path.realpath(path))
        hit = None
        for entry in entries:
            if os.path.normcase(os.path.realpath(entry["path"])) == key:
                hit = entry
                break
        if hit:
            hit["role"] = "bgm"
        else:
            entries.append({"path": path, "role": "bgm"})
        STATE.settings["roots"] = entries
        STATE.settings["extra_roots"] = [e["path"] for e in entries
                                         if e.get("role") in ("book", "media")]
        STATE.save_settings()
        # 音乐目录变更后重扫书库，避免音乐被当成有声书收录
        STATE.start_scan()
        self.send_json({"ok": True, "dir": payload["dir"], "bgm": STATE.bgm_list()})

    def handle_bgm_delete(self, rest):
        user = self.require_auth()
        if not user:
            return
        filename = rest.strip()
        error = STATE.bgm_delete(filename)
        if error:
            self.send_error_json(error, 404)
            return
        self.send_json({"ok": True, "bgm": STATE.bgm_list()})

    def send_media(self, book, index):
        if index < 0 or index >= len(book["chapters"]):
            self.send_error_json("章节不存在", 404)
            return
        chapter = book["chapters"][index]
        path = chapter.get("_abs") or os.path.join(book["path"], chapter["file_name"])
        if not os.path.isfile(path):
            self.send_error_json("音频文件不存在", 404)
            return
        self.stream_file(path)

    def stream_file(self, path):
        size = os.path.getsize(path)
        ext = fmt_ext(path)
        ctype = {
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".m4b": "audio/mp4",
            ".aac": "audio/aac",
            ".flac": "audio/flac",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".oga": "audio/ogg",
            ".opus": "audio/ogg",
            ".wma": "audio/x-ms-wma",
            ".asf": "audio/x-ms-wma",
            ".mka": "audio/x-matroska",
            ".ape": "audio/x-ape",
            ".aiff": "audio/aiff",
            ".aif": "audio/aiff",
        }.get(ext, "application/octet-stream")

        # 先打开文件再发响应头：打开失败时能返回干净的 4xx/5xx，
        # 不会把错误 JSON 追加进已发出的音频响应体（那会让播放器解出噪声）
        try:
            fp = open(path, "rb")
        except OSError as exc:
            self.send_error_json("音频读取失败: %s" % exc, 500)
            return
        try:
            try:
                sniffed = audio_format_of(fp.read(16))[0]
            except OSError:
                sniffed = None
            if sniffed:
                ctype = sniffed

            range_header = self.headers.get("Range", "")
            start, end = 0, size - 1
            partial = False
            match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match and size > 0:
                first, last = match.group(1), match.group(2)
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:
                    start = max(0, size - int(last))
                    end = size - 1
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True

            length = end - start + 1 if size > 0 else 0
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command == "HEAD":
                return

            fp.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fp.read(min(262144, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 响应头已经发出，不能再写第二条 HTTP 响应；直接断开连接
            self.close_connection = True
        finally:
            fp.close()

    def log_message(self, fmt, *args):
        # 静默常规访问日志，避免日志无限增长
        return

    def handle(self):
        try:
            BaseHTTPRequestHandler.handle(self)
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass


LOGIN_TICKET_TTL = 600  # 扫码登录票据有效期（秒）


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DualStackServer(Server):
    """IPv6 双栈监听：同一个端口同时接受 IPv4 与 IPv6 连接"""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        Server.server_bind(self)


APP_VERSION = "1.6.6"
# 实际监听端口（main() 启动时更新，供 /api/network 生成地址使用）
LISTEN_PORT = int(os.environ.get("TRIM_SERVICE_PORT") or 8098)


def main():
    global LISTEN_PORT
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("TRIM_SERVICE_PORT") or 8098))
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    LISTEN_PORT = args.port

    # 默认监听所有地址时使用 IPv6 双栈，IPv4/IPv6 均可直连
    if args.host in ("0.0.0.0", "", "::"):
        httpd = DualStackServer(("::", args.port), Handler)
        bind_desc = ":: (IPv4+IPv6)"
    else:
        httpd = Server((args.host, args.port), Handler)
        bind_desc = args.host

    def shutdown(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    try:
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
    except (ValueError, AttributeError):
        pass

    print("书中有音 server listening on %s:%d, data=%s" % (bind_desc, args.port, STATE.data_dir), flush=True)
    # 运维用：设置 SZY_NO_AUTOSCAN=1 可在启动时跳过自动扫描（默认行为不变）
    if not os.environ.get("SZY_NO_AUTOSCAN") and (not STATE.books or STATE.settings.get("bgm_dir")):
        STATE.start_scan()
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
