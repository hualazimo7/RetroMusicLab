#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/fetch_samples.py — 从 joshw 各机种子站抓取多格式验收样本。

样本用途：跑 vendor/test_chip_decode.py 的端到端验收（NSF/GBS/VGM/HES）。
**这些文件是开发期验证素材，不随产品分发。**

来源（与 PRD §0.1 一致）：
    NSF  https://nsf.joshw.info
    GBS  https://gbs.joshw.info
    VGM  https://smd.joshw.info        （实测 .vgm 实为 gzip，即 VGZ）
    HES  https://hes.joshw.info

用法：
    python vendor/fetch_samples.py                 # 抓每站前 N 个包
    python vendor/fetch_samples.py --limit 2
    python vendor/fetch_samples.py --copy-from C:/temp/gmev2   # 直接复用已有样本

限速 0.3s/请求、失败重试 3 次、只抓每站前几个包（够验收即可，不当下载器用）。
"""

import argparse
import glob
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "vendor", "samples")
# 产品模块（joshw_source / joshw_dl）在 ROOT 下。本脚本从 vendor/ 直接跑，
# 不一定把 ROOT 放进 sys.path，所以显式补上 —— 否则复用它们的逻辑会
# 以 "No module named 'joshw_source'" 静默退化。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 机种 -> (子站, 该站的扩展名)
SITES = {
    "nsf": ("nsf.joshw.info", ".nsf"),
    "gbs": ("gbs.joshw.info", ".gbs"),
    "smd": ("smd.joshw.info", ".vgm"),
    "hes": ("hes.joshw.info", ".hes"),
}
DELAY = 0.3           # 限速（秒/请求），别把人家站点打挂
RETRIES = 3
UA = "MSU1Converter-sample-fetcher/1.0 (+local verification only)"


def log(msg):
    print(msg, flush=True)


def _ctx():
    try:
        return ssl.create_default_context()
    except Exception:                                    # noqa: BLE001
        return ssl._create_unverified_context()


def get(url, timeout=30):
    """带重试的 GET。返回 bytes 或 None。"""
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=_ctx()) as r:
                return r.read()
        except Exception as e:                           # noqa: BLE001
            log(f"    重试 {attempt}/{RETRIES} {url} -> {e}")
            time.sleep(DELAY * (attempt + 1))
    return None


def find_packs(host, limit):
    """在机种首页找 limit 个 .7z 包链接。

    joshw 各子站是 **Apache autoindex**，而且 .7z 全部放在按首字母分的
    子目录里（``0-9/ a/ b/ …``）—— 站点根目录的索引页上一个 .7z 都没有。
    所以单靠正则扫首页必然是 0 条（这是之前"抓不到样本"的真正原因）。

    这里按"根页 -> 逐个子目录扫"的顺序取，拿到 limit 个就停。
    刻意**不用** joshw_source.scan_platform：它会把整站上千条目、
    几十个字母目录全扫完才返回，而验收脚本只要几个包，等不起。
    """
    import re

    base = "https://%s/" % host
    html = get(base)
    if not html:
        return []
    dirs = [d.decode("latin-1") for d in
            re.findall(rb'href="([^"?#]+/)"', html)]
    out, seen = [], set()
    for sub in dirs:
        if len(out) >= limit:
            break
        page = get(base + sub)
        if not page:
            continue
        for link in re.findall(rb'href="([^"]+\.7z)"', page, re.I):
            url = urllib.parse.urljoin(base + sub, link.decode("latin-1"))
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
            if len(out) >= limit:
                break
        time.sleep(DELAY)
    return out


def extract(pack, out_dir):
    """用 Windows 自带的 bsdtar 解压（不引入 py7zr，PRD 非目标①）。

    注意**不能**用 ``shutil.which("tar")``：PATH 上的 tar 是 Git 带的
    GNU tar 1.35，既不支持 7z，还会把 ``C:\\`` 当远程主机。这里直接复用
    :func:`joshw_dl.find_tar`，与产品代码保持同一套定位规则。
    """
    try:
        import joshw_dl
        tar = joshw_dl.find_tar()
    except Exception:                                   # noqa: BLE001
        tar = None
    if not tar:
        log("    找不到可解 7z 的 bsdtar，跳过解压")
        return False
    try:
        subprocess.run([tar, "-xf", pack, "-C", out_dir],
                       check=True, timeout=300, shell=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True
    except Exception as e:                               # noqa: BLE001
        log(f"    解压失败 {os.path.basename(pack)}: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3, help="每站抓几个包")
    ap.add_argument("--copy-from", default="",
                    help="从已有样本目录复制（跳过网络）")
    args = ap.parse_args()

    if args.copy_from:
        src = args.copy_from
        if not os.path.isdir(src):
            log(f"源目录不存在: {src}")
            return 2
        n = 0
        for sub in ("nsf", "gbs", "smd", "hes", "nsfe"):
            s = os.path.join(src, sub)
            if not os.path.isdir(s):
                continue
            d = os.path.join(DEST, sub)
            os.makedirs(d, exist_ok=True)
            for f in glob.glob(os.path.join(s, "*")):
                if os.path.splitext(f)[1].lower() in (
                        ".nsf", ".nsfe", ".gbs", ".vgm", ".vgz", ".hes"):
                    shutil.copy2(f, d)
                    n += 1
        log(f"已从 {src} 复制 {n} 个样本到 {DEST}")
        return 0

    os.makedirs(DEST, exist_ok=True)
    total = 0
    for key, (host, ext) in SITES.items():
        log(f"[{key}] {host}")
        packs = find_packs(host, args.limit)
        if not packs:
            log(f"    没找到 .7z 链接（站点结构可能变了，需人工确认）")
            continue
        out_dir = os.path.join(DEST, key)
        for u in packs:
            log(f"    下载 {os.path.basename(u)}")
            data = get(u, timeout=120)
            if not data:
                log("    下载失败，跳过")
                continue
            pack = os.path.join(out_dir, os.path.basename(u))
            os.makedirs(out_dir, exist_ok=True)
            with open(pack, "wb") as f:
                f.write(data)
            time.sleep(DELAY)
            extract(pack, out_dir)
            got = glob.glob(os.path.join(out_dir, f"*{ext}"))
            log(f"    -> 得到 {len(got)} 个 {ext}")
            total += len(got)
            if got:
                break                      # 每站够一个包即可
        time.sleep(DELAY)
    log(f"\n完成：样本在 {DEST}（共 {total} 个音频文件）")
    log("接着跑:  python vendor/test_chip_decode.py "
        f"--dir {DEST.replace(os.sep, '/')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
