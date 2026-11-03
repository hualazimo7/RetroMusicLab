#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/fetch_kss_samples.py — 从 joshw 的 KSS 站抓验收样本。

站点结构（2026-10-18 实测，Apache autoindex）：
    https://kss.joshw.info/
      MSX/            -> 0-9/ a/ ... z/
      Master System/  -> 0-9/ a/ ... z/ zzz_homebrew/
      Game Gear/      -> a/ ... z/
每个字母目录里直接就是 `<游戏名>.7z`，**可直接下载**（无需鉴权、无需登录）。

⚠ 上一版结论「三个子目录没有可直接取的 .7z」是**错的** —— 只列了一层目录就下了
  结论，实际要再进一层 0-9/a-z。本脚本按 BFS 递归下钻，避免重复这个错误。

解包：Windows 自带 bsdtar (`C:/Windows/System32/tar.exe`) 支持 7z 读取，
     不引入 py7zr（PRD 非目标①）。GNU tar 不支持 7z，必须用 bsdtar。

用法:
    python vendor/fetch_kss_samples.py --packs 3
    python vendor/fetch_kss_samples.py --sub "Master%20System/" --packs 5
"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "vendor", "samples", "kss")
BASE = "https://kss.joshw.info/"

DELAY = 0.4          # 限速，别把人家站点打挂
RETRIES = 3
UA = "MSU1Converter-sample-fetcher/1.0 (+local verification only)"

# KSS 站里实际出现的容器扩展名（实测：Game Gear 大量用 .sgc —— Sega 模拟器
# 的 KSS 变种封装，libgme 的 KSS 读取器兼容）。
WANT_EXTS = (".kss", ".sgc")


def log(msg):
    print(msg, flush=True)


def get(url, timeout=60):
    """带重试的 GET。返回 bytes 或 None。"""
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:                           # noqa: BLE001
            log(f"    重试 {attempt}/{RETRIES} -> {e}")
            time.sleep(DELAY * attempt)
    return None


def list_dir(url):
    """列 autoindex 目录，返回子目录名与 .7z 链接名。"""
    html = get(url)
    if not html:
        return [], []
    txt = html.decode("utf-8", "replace")
    dirs, packs = [], []
    for m in re.findall(r'href="([^"]+)"', txt):
        if m.startswith("?") or m.startswith("/"):
            continue
        if m.endswith("/"):
            dirs.append(m)
        elif m.lower().endswith(".7z"):
            packs.append(m)
    return dirs, packs


def find_packs_bfs(sub, want, max_packs):
    """在子树里 BFS 找最多 want 个 .7z（每层取前若干，避免全站遍历）。"""
    found, queue, seen = [], [sub], set()
    while queue and len(found) < want:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        dirs, packs = list_dir(BASE + cur)
        time.sleep(DELAY)
        if cur != sub:
            log(f"    [{cur}] {len(packs)} 包 / {len(dirs)} 子目录")
        for p in packs:
            # 注意：p 是相对 **cur** 的，必须带上 cur 拼成相对 BASE 的完整路径。
            # （只存文件名会丢掉 "Game%20Gear/a/" 前缀 -> 下载 404，实测踩过。）
            found.append(cur + p)
            if len(found) >= want:
                break
        for d in dirs:
            if d not in seen:
                queue.append(cur + d)
    return found[:want]


def bsdtar_path():
    """定位能读 7z 的 tar。GNU tar 不行，必须 bsdtar（Windows 自带）。"""
    cand = [r"C:\Windows\System32\tar.exe"]
    w = shutil.which("tar")
    if w:
        cand.append(w)
    for c in cand:
        if os.path.exists(c):
            return c
    return None


def extract(pack, out_dir):
    tar = bsdtar_path()
    if not tar:
        log("    找不到 tar.exe，跳过解压")
        return False
    try:
        subprocess.run([tar, "-xf", pack, "-C", out_dir], check=True,
                       timeout=180, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE)
        return True
    except Exception as e:                               # noqa: BLE001
        log(f"    解压失败 {os.path.basename(pack)}: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default="", help="起始子目录（'' = 根）")
    ap.add_argument("--packs", type=int, default=3, help="抓几个包")
    ap.add_argument("--dest", default=DEST)
    args = ap.parse_args()

    sub = args.sub
    if sub and not sub.endswith("/"):
        sub += "/"
    log(f"[kss] {BASE}{sub}  取 {args.packs} 个包")
    packs = find_packs_bfs(sub, args.packs, args.packs)
    if not packs:
        log("    没找到 .7z")
        return 1
    log(f"    找到 {len(packs)} 个包")

    os.makedirs(args.dest, exist_ok=True)
    tmp = os.path.join(args.dest, "_packs")
    os.makedirs(tmp, exist_ok=True)
    n_new = 0
    for rel in packs:
        # rel 已含相对 BASE 的完整路径（含子目录前缀），且保持 autoindex 的
        # URL 编码形式 —— **不能**再整体 quote，那会二次编码直接 404。
        url = BASE + rel
        log(f"    下载 {urllib.parse.unquote(rel)[:70]}")
        data = get(url, timeout=180)
        if not data:
            log("    下载失败，跳过")
            continue
        pack = os.path.join(tmp, os.path.basename(urllib.parse.unquote(rel)))
        with open(pack, "wb") as f:
            f.write(data)
        time.sleep(DELAY)
        extract(pack, args.dest)
        got = []
        for e in WANT_EXTS:
            got += glob.glob(os.path.join(args.dest, f"**/*{e}"), recursive=True)
        log(f"    -> 得到 {len(got)} 个 KSS 文件")
        if got:
            n_new += len(got)
            break                     # 一个包够验收了，除非调用方要多几个
    log(f"\n完成：KSS 样本在 {args.dest}（{n_new} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())