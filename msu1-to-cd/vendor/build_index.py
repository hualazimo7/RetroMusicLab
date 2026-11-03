#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/build_index.py -- 预抓 30 个机种的 joshw.info 目录索引, 打成内置快照。

用法::

    python vendor/build_index.py                  # 全部 30 个机种
    python vendor/build_index.py --only spc,nsf   # 只做指定机种（逗号分隔）
    python vendor/build_index.py --force          # 忽略本地缓存, 强制重扫

产物::

    <项目根>/index/<key>.json.gz
    {"ts": <float>, "key": "<key>", "host": "<host>", "count": <int>, "entries": [...]}

``entries`` 与 :func:`joshw_source.scan_platform` 的返回完全一致
（``name / url / size / mtime / letter``），因此 GUI 可以直接把它当扫描结果用。

礼貌约定（joshw.info 各子站 ``robots.txt`` 均为 ``Disallow: /``）：

* 整个过程**串行**执行，不做多机种并发；
* 限速完全交给 :func:`joshw_source.scan_platform` 内部的 0.2 秒全局间隔，
  本脚本不额外并发、不重试风暴；
* 默认优先复用本地缓存（``ttl_days=2``），只有缓存缺失/过期/``--force``
  时才真的联网 —— 打包前跑一次即可。

单个机种失败不会中断整体流程：打印 ``❌ <key>: <原因>`` 后继续下一个，
最后汇总失败清单并返回非 0 退出码。
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import joshw_source as jw  # noqa: E402

# 项目根目录（本脚本位于 <root>/vendor/）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 产物目录：<root>/index
INDEX_DIR = os.path.join(_ROOT, "index")

# 复用本地缓存时接受的年龄（天）。打包前通常刚扫过，2 天足够新。
_REUSE_TTL_DAYS = 2.0

# gzip 压缩级别：9 能在几 MB 的 JSON 上再省一点，耗时可忽略。
_GZIP_LEVEL = 9


def index_path(key: str) -> str:
    """返回某机种的快照文件路径 ``<root>/index/<key>.json.gz``。"""
    return os.path.join(INDEX_DIR, "%s.json.gz" % key)


def write_snapshot(key: str, host: str, entries: List[Dict[str, object]],
                   stamp: Optional[float] = None) -> Tuple[int, int]:
    """把条目列表写成 gzip 快照，返回 ``(原始字节数, 压缩后字节数)``。

    先写 ``.tmp`` 再 ``os.replace``，避免打包时读到半截文件。
    使用 ``mtime=0`` 让同样的内容产出同样的字节（便于 diff / 复现).

    Args:
        key: 机种 key。
        host: 机种子站主机名，仅作元信息。
        entries: 条目列表（字段契约见 :func:`joshw_source.parse_index`）。
        stamp: 快照时间戳（秒）；``None`` 表示取当前时间。

    Returns:
        ``(原始字节数, 压缩后字节数)``。
    """
    ts = float(time.time() if stamp is None else stamp)
    payload = {
        "ts": ts,
        "key": key,
        "host": host,
        "count": len(entries),
        "entries": entries,
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    buffer = io.BytesIO()
    # mtime=0 + 固定文件名 -> 内容相同则字节相同
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer,
                       compresslevel=_GZIP_LEVEL, mtime=0) as handle:
        handle.write(raw)
    blob = buffer.getvalue()

    os.makedirs(INDEX_DIR, exist_ok=True)
    path = index_path(key)
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as handle:
        handle.write(blob)
    os.replace(tmp_path, path)
    return len(raw), len(blob)


def fetch_entries(key: str, force: bool) -> Tuple[List[Dict[str, object]], str]:
    """取得一个机种的全部条目，返回 ``(entries, 来源说明)``。

    Args:
        key: 机种 key。
        force: ``True`` 时跳过本地缓存，强制联网重扫。

    Returns:
        ``(entries, source)``，``source`` 为 ``"cache"`` 或 ``"scan"``。

    Raises:
        Exception: 透传扫描失败（由调用方记为该机种失败）。
    """
    if not force:
        cached = jw.load_cache(key, ttl_days=_REUSE_TTL_DAYS)
        if cached:
            return cached, "cache"
    entries = jw.scan_platform(key)
    return entries, "scan"


def select_keys(only: str) -> List[str]:
    """解析 ``--only``，返回要处理的 key 列表（保持 PLATFORMS 顺序）。

    Raises:
        SystemExit: ``--only`` 里出现未知机种时。
    """
    all_keys = [str(item["key"]) for item in jw.PLATFORMS]
    if not only:
        return all_keys
    wanted = [part.strip() for part in only.split(",") if part.strip()]
    if not wanted:
        return all_keys
    unknown = [key for key in wanted if key not in all_keys]
    if unknown:
        print("❌ 未知机种: %s（可用: %s）" % (", ".join(unknown), ", ".join(all_keys)),
              file=sys.stderr)
        raise SystemExit(2)
    chosen = set(wanted)
    return [key for key in all_keys if key in chosen]


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口，返回进程退出码（有失败机种时为 1）。"""
    parser = argparse.ArgumentParser(
        description="预抓 joshw.info 各机种索引, 生成 index/<key>.json.gz 内置快照")
    parser.add_argument("--only", default="",
                        help="只处理指定机种, 逗号分隔（如 spc,nsf）")
    parser.add_argument("--force", action="store_true",
                        help="忽略本地缓存, 强制重新联网扫描")
    args = parser.parse_args(argv)

    keys = select_keys(args.only)
    total = len(keys)
    print("▶ 生成内置索引: %d 个机种 -> %s%s"
          % (total, INDEX_DIR, "（--force 强制重扫）" if args.force else ""))

    started_all = time.time()
    ok_count = 0
    total_entries = 0
    total_raw = 0
    total_gz = 0
    failures: List[Tuple[str, str]] = []

    for position, key in enumerate(keys, 1):
        meta = jw.platform(key)
        name = str(meta.get("name") or key)
        host = str(meta.get("host") or "")
        stamp = "[%2d/%2d] %-8s" % (position, total, key)
        started = time.time()
        try:
            entries, source = fetch_entries(key, args.force)
        except Exception as exc:                      # noqa: BLE001 - 单机种失败继续
            reason = "%s: %s" % (type(exc).__name__, exc)
            failures.append((key, reason))
            print("%s ❌ %s" % (stamp, reason), flush=True)
            continue

        if not entries:
            reason = "扫描结果为空（站点可能 404 或结构异常）"
            failures.append((key, reason))
            print("%s ❌ %s" % (stamp, reason), flush=True)
            continue

        try:
            raw_size, gz_size = write_snapshot(key, host, entries)
        except OSError as exc:
            reason = "写入失败: %s" % (exc,)
            failures.append((key, reason))
            print("%s ❌ %s" % (stamp, reason), flush=True)
            continue

        ok_count += 1
        total_entries += len(entries)
        total_raw += raw_size
        total_gz += gz_size
        elapsed = time.time() - started
        print("%s %-24s %6d 条  %8s -> %8s  (%.1fs, %s)"
              % (stamp, name, len(entries), jw.human_size(raw_size),
                 jw.human_size(gz_size), elapsed, source), flush=True)

    elapsed_all = time.time() - started_all
    ratio = (100.0 * total_gz / total_raw) if total_raw else 0.0
    print("-" * 72)
    print("合计 %d/%d 个机种 / %d 条 / 原始 %s / 压缩后 %s（%.1f%%）/ 耗时 %.1fs"
          % (ok_count, total, total_entries, jw.human_size(total_raw),
             jw.human_size(total_gz), ratio, elapsed_all))

    if failures:
        print("❌ 失败 %d 个机种:" % len(failures))
        for key, reason in failures:
            print("   - %s: %s" % (key, reason))
        return 1
    print("✅ 全部机种快照已就绪: %s" % INDEX_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
