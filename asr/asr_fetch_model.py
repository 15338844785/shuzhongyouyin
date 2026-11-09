#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 hf-mirror 下载 whisper 模型（在独立 venv 中运行）"""

import argparse
import os
import sys

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="medium")
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()
    os.makedirs(args.dir, exist_ok=True)
    from faster_whisper.utils import download_model  # 延迟导入，避免无依赖时直接报错
    path = download_model(args.model, cache_dir=args.dir, local_files_only=False)
    print("model ready at %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
