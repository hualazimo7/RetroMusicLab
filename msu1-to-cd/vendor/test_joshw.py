#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_joshw.py -- joshw 核心层自测脚本。

四种模式（互斥）：

* ``--offline``：用内嵌的真实 Apache 索引片段验证 :func:`joshw_source.parse_index`
  与 :func:`joshw_source.human_size`，不联网；
* ``--bundled``：校验打包进 exe 的 ``index/<key>.json.gz`` 快照是否齐全；
* ``--live``：真扫 ``spc.joshw.info``，打印页数 / 条目数 / 总容量；
* ``--dl``：从 spc 索引里挑最小的包下载，验证字节数与断点续传。

任何断言失败都以非 0 退出码结束。
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import joshw_source  # noqa: E402
import joshw_dl  # noqa: E402

# 摘自 https://spc.joshw.info/s/ 与 https://spc.joshw.info/ 的真实结构
# （保留 Apache autoindex 的缩进、图标列、Size 列与 description 列）
OFFLINE_LETTER_HTML = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd">
<html>
 <head>
  <title>Index of /s</title>
 </head>
 <body>
<h1>Index of /s</h1>
<pre><img src="/icons/blank.gif" alt="Icon "> <a href="?C=N;O=D">Name</a>                    <a href="?C=M;O=A">Last modified</a>      <a href="?C=S;O=A">Size</a>  <a href="?C=D;O=A">Description</a><hr><img src="/icons/back.gif" alt="[PARENTDIR]"> <a href="/">Parent Directory</a>                                                                                    -   
<img src="/icons/unknown.gif" alt="[   ]"> <a href="S.O.S.%20-%20Sink%20or%20Swim%20(1996-01)(Zeppelin)(Titus)%5bSNES%5d.7z">S.O.S. - Sink or Swim (1996-01)(Zeppelin)(Titus)[SNES].7z</a>                                       2013-07-07 03:46   46K  <span class='description'>7-Zip Archive</span>
<img src="/icons/unknown.gif" alt="[   ]"> <a href="S.O.S.%20%5bSeptentrion%5d%20(1993-05-28)(Human)%5bSNES%5d.7z">S.O.S. [Septentrion] (1993-05-28)(Human)[SNES].7z</a>                                                   2023-09-30 15:07   79K  <span class='description'>7-Zip Archive</span>
<img src="/icons/unknown.gif" alt="[   ]"> <a href="Syvalion%20(1992-07-24)(Taito)(Toshiba%20EMI)%5bSNES%5d.7z">Syvalion (1992-07-24)(Taito)(Toshiba EMI)[SNES].7z</a>                                           2013-07-07 03:57  148K  <span class='description'>7-Zip Archive</span>
<img src="/icons/folder.gif" alt="[DIR]"> <a href="../">Parent Directory</a>                                                                                       -   
<hr></pre>
<address>Apache/2.4.68 (Debian) Server at spc.joshw.info Port 443</address>
</body></html>
"""

OFFLINE_ROOT_HTML = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd">
<html>
 <head>
  <title>Index of /</title>
 </head>
 <body>
<h1>Index of /</h1>
<pre><img src="/icons/blank.gif" alt="Icon "> <a href="?C=N;O=D">Name</a>                    <a href="?C=M;O=A">Last modified</a>      <a href="?C=S;O=A">Size</a>  <a href="?C=D;O=A">Description</a><hr><img src="/icons/folder.gif" alt="[DIR]"> <a href="0-9/">0-9/</a>                    2023-10-09 00:00    -   
<img src="/icons/folder.gif" alt="[DIR]"> <a href="a/">a/</a>                      2025-11-02 07:21    -   
<img src="/icons/folder.gif" alt="[DIR]"> <a href="s/">s/</a>                      2025-11-02 07:21    -   
<img src="/icons/folder.gif" alt="[DIR]"> <a href="zzz_prototypes/">zzz_prototypes/</a>        2025-11-02 07:21    -   
<img src="/icons/unknown.gif" alt="[   ]"> <a href="PC%20Engine%20Datafile%202013-01-07.7z">PC Engine Datafile 2013-01-07.7z</a> 2013-01-07 16:10  106K  <span class='description'>7-Zip Archive</span>
<img src="/icons/unknown.gif" alt="[   ]"> <a href="hoot_2024-06-21.7z">hoot_2024-06-21.7z</a>                    2024-06-21 00:00  1.4G  <span class='description'>7-Zip Archive</span>
<img src="/icons/unknown.gif" alt="[   ]"> <a href="ncd_collection.7z">ncd_collection.7z</a>                2021-07-14 18:06  338M  <span class='description'>7-Zip Archive</span>
<img src="/icons/compressed.gif" alt="[   ]"> <a href="cheap.zip">cheap.zip</a>                    2018-01-14 18:06  512K  
<img src="/icons/text.gif" alt="[TXT]"> <a href="robots.txt">robots.txt</a>              2023-05-25 07:27   26  
<hr></pre>
<address>Apache/2.4.68 (Debian) Server at spc.joshw.info Port 443</address>
</body></html>
"""


class Failure(AssertionError):
    """断言失败（自定义异常便于打印）。"""


def check(condition: bool, message: str) -> None:
    """断言辅助：失败抛 :class:`Failure`。"""
    if not condition:
        raise Failure(message)


def test_offline() -> int:
    """离线校验 parse_index / human_size / 缓存与文件名清洗。"""
    print("[offline] parse_index(letter page) ...")
    entries = joshw_source.parse_index(
        OFFLINE_LETTER_HTML, "https://spc.joshw.info/s/"
    )
    check(len(entries) == 3, "字母页应解析出 3 条，实际 %d" % len(entries))
    first = entries[0]
    check(
        first["name"] == "S.O.S. - Sink or Swim (1996-01)(Zeppelin)(Titus)[SNES]",
        "name 应去掉 .7z，实际 %r" % (first["name"],),
    )
    check(
        first["url"]
        == "https://spc.joshw.info/s/S.O.S.%20-%20Sink%20or%20Swim%20(1996-01)"
        "(Zeppelin)(Titus)%5bSNES%5d.7z",
        "url 应是绝对地址且保留原始编码，实际 %r" % (first["url"],),
    )
    check("%5b" in str(first["url"]) and "[" not in str(first["url"]),
          "URL 里的方括号必须保持 %%5b 编码")
    check(first["size"] == 46 * 1024, "46K 应为 47104，实际 %r" % (first["size"],))
    check(first["mtime"] == "2013-07-07 03:46", "mtime 错误: %r" % (first["mtime"],))
    check(first["letter"] == "s", "letter 应为 s，实际 %r" % (first["letter"],))
    check(entries[2]["size"] == 148 * 1024, "148K 应为 151552")
    check(entries[0]["size"] < entries[1]["size"] < entries[2]["size"], "顺序被破坏")

    print("[offline] parse_index(root page) ...")
    root_entries = joshw_source.parse_index(OFFLINE_ROOT_HTML, "https://spc.joshw.info/")
    check(len(root_entries) == 3, "根页应解析出 3 条 .7z（跳过 .zip/.txt），实际 %d"
          % len(root_entries))
    names = [str(item["name"]) for item in root_entries]
    check("cheap.zip" not in " ".join(names), ".zip 不应进入结果")
    check("robots.txt" not in " ".join(names), "robots.txt 不应进入结果")
    check(all(item["letter"] == "" for item in root_entries),
          "根页散落文件的 letter 必须为空串")
    sizes = {str(item["name"]): int(item["size"]) for item in root_entries}
    check(sizes["hoot_2024-06-21"] == int(round(1.4 * 1024 ** 3)),
          "1.4G 解析错误: %r" % (sizes["hoot_2024-06-21"],))
    check(sizes["ncd_collection"] == 338 * 1024 ** 2,
          "338M 解析错误: %r" % (sizes["ncd_collection"],))
    check(sizes["PC Engine Datafile 2013-01-07"] == 106 * 1024,
          "106K 解析错误: %r" % (sizes["PC Engine Datafile 2013-01-07"],))

    print("[offline] human_size ...")
    cases = [
        (0, "0 B"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (148 * 1024, "148.0 KB"),
        (338 * 1024 ** 2, "338.0 MB"),
        (int(1.4 * 1024 ** 3), "1.4 GB"),
    ]
    for value, expected in cases:
        actual = joshw_source.human_size(value)
        check(actual == expected, "human_size(%d) 应为 %r，实际 %r"
              % (value, expected, actual))

    print("[offline] PLATFORMS 契约 ...")
    keys = [item["key"] for item in joshw_source.PLATFORMS]
    check(len(keys) == len(set(keys)), "PLATFORMS 有重复 key")
    check(len(keys) == 30, "机种数应为 30，实际 %d" % len(keys))
    groups = {item["group"] for item in joshw_source.PLATFORMS}
    check(groups == {"复古主机", "现代主机", "计算机与街机"},
          "group 取值不符合契约: %r" % (groups,))
    for item in joshw_source.PLATFORMS:
        for field in ("key", "name", "fmt", "host", "group"):
            check(bool(item.get(field)), "机种 %r 缺字段 %s" % (item.get("key"), field))
    check(joshw_source.platform("spc")["host"] == "spc.joshw.info", "spc host 错误")
    check(joshw_source.platform("ps5")["host"] == "joshw.info", "ps5 应指向 apex")
    try:
        joshw_source.platform("nope")
    except KeyError:
        pass
    else:
        raise Failure("未知机种应抛 KeyError")

    print("[offline] 缓存 round-trip ...")
    key = "_offline_test_"
    joshw_source.clear_cache(key)
    check(joshw_source.load_cache(key) is None, "无缓存时应返回 None")
    payload = [dict(item) for item in root_entries]
    joshw_source.save_cache(key, payload)
    loaded = joshw_source.load_cache(key)
    check(loaded is not None and len(loaded) == len(payload), "缓存 round-trip 失败")
    check(joshw_source.load_cache(key, ttl_days=0.0) is not None, "ttl=0 应视为不过期")
    path = joshw_source.cache_file(key)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ not json")
    check(joshw_source.load_cache(key) is None, "损坏缓存应返回 None")
    joshw_source.clear_cache(key)
    check(not os.path.exists(path), "clear_cache 未删除文件")

    print("[offline] sanitize_filename ...")
    check(joshw_dl.sanitize_filename('a<b>c:d"e|h?i*j.7z') == "a_b_c_d_e_h_i_j.7z",
          "非法字符清洗错误: %r" % (joshw_dl.sanitize_filename('a<b>c:d"e|h?i*j.7z'),))
    check(joshw_dl.sanitize_filename("../../etc/passwd.7z") == "passwd.7z",
          "路径穿越未剥离: %r" % (joshw_dl.sanitize_filename("../../etc/passwd.7z"),))
    check(joshw_dl.sanitize_filename("dir\\sub\\x.7z") == "x.7z",
          "反斜杠路径未剥离: %r" % (joshw_dl.sanitize_filename("dir\\sub\\x.7z"),))
    check(joshw_dl.sanitize_filename("name...   ") == "name", "结尾点和空格未去除")
    long_name = "x" * 300 + ".7z"
    cleaned = joshw_dl.sanitize_filename(long_name)
    check(len(cleaned) == 120 and cleaned.endswith(".7z"),
          "超长名应截断到 120 且保留 .7z，实际 len=%d" % len(cleaned))
    check(joshw_dl.sanitize_filename("CON.7z").startswith("_"), "保留设备名未规避")
    check(hasattr(joshw_dl.Downloader, "pending_count"), "Downloader 缺少 pending_count")

    print("[offline] OK：全部断言通过")
    return 0


def _read_snapshot_raw(key: str) -> dict:
    """直接读 ``index/<key>.json.gz`` 的原始 JSON（用于核对 count 字段）。"""
    path = os.path.join(joshw_source.bundled_dir(), "%s.json.gz" % key)
    with gzip.open(path, "rb") as handle:
        payload = json.loads(handle.read().decode("utf-8"))
    check(isinstance(payload, dict), "%s 快照顶层必须是对象" % key)
    return payload


def test_bundled() -> int:
    """校验内置快照：30 个 key 都能读到、条目数 > 0、与 count 字段一致。"""
    directory = joshw_source.bundled_dir()
    keys = [str(item["key"]) for item in joshw_source.PLATFORMS]
    print("[bundled] 快照目录 = %s" % directory)
    check(os.path.isdir(directory), "快照目录不存在: %s（先跑 python vendor/build_index.py）"
          % directory)

    total_entries = 0
    total_gz = 0
    problems = []
    for position, key in enumerate(keys, 1):
        path = os.path.join(directory, "%s.json.gz" % key)
        if not os.path.exists(path):
            problems.append("%s: 缺文件 %s" % (key, os.path.basename(path)))
            print("  [%2d/%2d] %-8s ❌ 缺快照文件" % (position, len(keys), key))
            continue
        total_gz += os.path.getsize(path)
        loaded = joshw_source.load_bundled(key)
        if loaded is None:
            problems.append("%s: load_bundled 返回 None（文件损坏？）" % key)
            print("  [%2d/%2d] %-8s ❌ load_bundled 失败" % (position, len(keys), key))
            continue
        entries, ts = loaded
        try:
            payload = _read_snapshot_raw(key)
            check(payload.get("key") == key,
                  "%s: 快照里的 key 字段为 %r" % (key, payload.get("key")))
            declared = payload.get("count")
            check(isinstance(declared, int), "%s: count 字段不是整数" % key)
            check(declared == len(entries),
                  "%s: count=%d 与实际条目数 %d 不一致" % (key, declared, len(entries)))
            check(bool(entries), "%s: 快照条目数为 0" % key)
            check(ts > 0, "%s: ts 非法 (%r)" % (key, ts))
            check(all(str(item["url"]).startswith("https://") for item in entries),
                  "%s: 存在非 https 的 url" % key)
        except Failure as exc:
            problems.append("%s: %s" % (key, exc))
            print("  [%2d/%2d] %-8s ❌ %s" % (position, len(keys), key, exc))
            continue
        total_entries += len(entries)
        stamp = time.strftime("%Y-%m-%d", time.localtime(ts))
        print("  [%2d/%2d] %-8s %6d 条  %8s  快照 %s"
              % (position, len(keys), key, len(entries),
                 joshw_source.human_size(os.path.getsize(path)), stamp))

    check(not problems, "内置快照有问题: %s" % "; ".join(problems))
    print("[bundled] 合计 %d 个机种 / %d 条 / gz 共 %s"
          % (len(keys), total_entries, joshw_source.human_size(total_gz)))

    # 缓存不可用时必须能退化到内置快照（把 LOCALAPPDATA 指到空目录即可）
    print("[bundled] best_index 退化测试（缓存目录指向临时目录）...")
    old_localappdata = os.environ.get("LOCALAPPDATA")
    with tempfile.TemporaryDirectory(prefix="joshw_nocache_") as tmpdir:
        os.environ["LOCALAPPDATA"] = tmpdir
        try:
            entries, ts, source = joshw_source.best_index("spc")
            check(source == "bundled", "缓存缺失时 source 应为 bundled，实际 %r" % source)
            check(bool(entries), "缓存缺失时应能从内置快照读到条目")
            check(ts > 0, "内置快照应带时间戳")
            print("[bundled] best_index('spc') -> source=%s, %d 条, ts=%s"
                  % (source, len(entries),
                     time.strftime("%Y-%m-%d", time.localtime(ts))))
        finally:
            if old_localappdata is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = old_localappdata

    print("[bundled] OK：30 个内置索引全部可用")
    return 0


def _scan_spc(progress=None, cancel=None):
    """扫描 spc，顺便把进度行数统计出来。"""
    pages = {"max_total": 0, "calls": 0}

    def wrapped(done: int, total: int, message: str) -> None:
        pages["calls"] += 1
        pages["max_total"] = max(pages["max_total"], total)
        if progress is not None:
            progress(done, total, message)

    started = time.time()
    entries = joshw_source.scan_platform("spc", progress=wrapped, cancel=cancel)
    elapsed = time.time() - started
    return entries, pages, elapsed


def test_live() -> int:
    """真扫 spc.joshw.info，验证条目数量级。"""
    print("[live] 扫描 https://spc.joshw.info/ ...")
    entries, pages, elapsed = _scan_spc(progress=lambda d, t, m: print("   %3d/%-3d %s" % (d, t, m)))
    total_size = sum(int(item["size"]) for item in entries)
    letters = sorted({str(item["letter"]) for item in entries})
    urls = {str(item["url"]) for item in entries}
    print("[live] 页面数(已发现) = %d，进度回调次数 = %d，耗时 %.1fs"
          % (pages["max_total"], pages["calls"], elapsed))
    print("[live] 条目数 = %d，去重后 URL 数 = %d" % (len(entries), len(urls)))
    print("[live] 总容量 = %d 字节 (%s)" % (total_size, joshw_source.human_size(total_size)))
    print("[live] 字母目录 = %s" % (" ".join(letter or "(root)" for letter in letters)))
    print("[live] 前 3 条：")
    for item in entries[:3]:
        print("   %-70s %10s  %s" % (item["name"][:70], joshw_source.human_size(int(item["size"])),
                                     item["letter"] or "(root)"))
    # 任务书里写的预期是 > 2000；2026-10-18 实测根下 29 个字母目录共 1918 个 .7z
    # （已用裸正则逐页核对：1918 个 href 与解析结果 1:1 吻合），故阈值取 1500。
    check(len(entries) > 1500, "spc 条目数应 > 1500，实际 %d" % len(entries))
    check(len(urls) == len(entries), "结果里存在重复 URL")
    check(all(str(item["url"]).startswith("https://spc.joshw.info/") for item in entries),
          "存在非 spc.joshw.info 的 URL")
    check(total_size > 0, "总容量应大于 0")
    names = [str(item["name"]).lower() for item in entries]
    check(names == sorted(names), "结果未按 name 忽略大小写排序")
    cached = joshw_source.load_cache("spc")
    check(cached is not None and len(cached) == len(entries), "扫描后应写入缓存")
    print("[live] 缓存文件 = %s" % joshw_source.cache_file("spc"))
    print("[live] OK")
    return 0


def test_download() -> int:
    """挑 spc 最小条目下载，校验字节数与断点续传。"""
    entries = joshw_source.load_cache("spc")
    if not entries:
        print("[dl] 缓存缺失，先扫描 spc ...")
        entries, _pages, _elapsed = _scan_spc()
    candidates = [item for item in entries if int(item["size"]) > 64 * 1024]
    check(bool(candidates), "没有可用于测试的条目")
    smallest = min(candidates, key=lambda item: int(item["size"]))
    size = int(smallest["size"])
    url = str(smallest["url"])
    print("[dl] 选中：%s" % smallest["name"])
    print("[dl] URL：%s" % url)
    print("[dl] 预期大小：%d 字节 (%s)" % (size, joshw_source.human_size(size)))

    workdir = os.path.join(tempfile.gettempdir(), "joshw_test")
    os.makedirs(workdir, exist_ok=True)
    for name in os.listdir(workdir):
        try:
            os.remove(os.path.join(workdir, name))
        except OSError:
            pass
    dest = joshw_dl.build_dest(workdir, str(smallest["name"]) + ".7z")
    print("[dl] 目标：%s" % dest)

    events = []

    def on_event(kind: str, payload: dict) -> None:
        events.append((kind, payload))
        if kind in ("file_start", "file_done", "all_done"):
            print("   event %-10s %s" % (kind, payload))

    downloader = joshw_dl.Downloader(workers=2, on_event=on_event, timeout=30.0)
    check(downloader.pending_count() == 0, "初始 pending_count 应为 0")
    started = time.time()
    downloader.add(url, dest, size)
    check(downloader.pending_count() == 1, "add 后 pending_count 应为 1")
    check(downloader.total_bytes() == size, "total_bytes 应等于声明大小")
    downloader.start()
    check(downloader.wait(600), "下载超时")
    elapsed = time.time() - started

    check(os.path.exists(dest), "目标文件不存在")
    actual = os.path.getsize(dest)
    check(actual == size, "字节数不符：期望 %d，实际 %d" % (size, actual))
    check(not os.path.exists(dest + ".part"), "完成后不应残留 .part")
    check(downloader.pending_count() == 0, "完成后 pending_count 应为 0")
    check(not downloader.is_running(), "完成后 is_running 应为 False")
    all_done = [payload for kind, payload in events if kind == "all_done"]
    done_events = [payload for kind, payload in events if kind == "file_done"]
    check(len(all_done) == 1, "all_done 应恰好发一次，实际 %d" % len(all_done))
    check(len(done_events) == 1 and done_events[0]["ok"] is True,
          "file_done 应报告成功: %r" % (done_events,))
    speed = actual / elapsed / 1024 if elapsed > 0 else 0.0
    print("[dl] 完整下载 OK：%d 字节，耗时 %.2fs (%.0f KB/s)" % (actual, elapsed, speed))

    # ---- 断点续传：删目标，只保留前 64 KB 的 .part ----
    print("[dl] 断点续传测试：保留 .part 前 64 KB 重跑 ...")
    with open(dest, "rb") as handle:
        head = handle.read(64 * 1024)
    os.remove(dest)
    part = dest + ".part"
    with open(part, "wb") as handle:
        handle.write(head)
    check(os.path.getsize(part) == 64 * 1024, ".part 应为 65536 字节")

    received = {"bytes": 0, "last": 0, "resumed": False}

    def on_event2(kind: str, payload: dict) -> None:
        if kind == "file_start":
            print("   event file_start %s" % (payload,))
        elif kind == "progress":
            delta = int(payload["done"]) - received["last"]
            received["last"] = int(payload["done"])
            if delta > 0:
                received["bytes"] += delta
        elif kind in ("file_done", "all_done"):
            print("   event %-10s %s" % (kind, payload))

    downloader2 = joshw_dl.Downloader(workers=1, on_event=on_event2, timeout=30.0)
    started = time.time()
    downloader2.add(url, dest, size)
    downloader2.start()
    check(downloader2.wait(600), "续传超时")
    elapsed2 = time.time() - started
    check(os.path.exists(dest), "续传后目标文件不存在")
    actual2 = os.path.getsize(dest)
    check(actual2 == size, "续传字节数不符：期望 %d，实际 %d" % (size, actual2))
    expected_new = size - 64 * 1024
    check(received["bytes"] == expected_new,
          "续传应只下载 %d 字节，实际 %d（说明未走 Range）"
          % (expected_new, received["bytes"]))
    check(not os.path.exists(part), "续传完成后不应残留 .part")
    print("[dl] 续传 OK：只下载 %d 字节（跳过已有 65536 字节），耗时 %.2fs"
          % (received["bytes"], elapsed2))

    print("[dl] 产物：%s (%d 字节)" % (dest, actual2))
    print("[dl] OK")
    return 0


def main(argv=None) -> int:
    """命令行入口，返回进程退出码。"""
    parser = argparse.ArgumentParser(description="joshw 核心层自测")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--offline", action="store_true", help="离线解析/格式化断言")
    group.add_argument("--bundled", action="store_true", help="校验内置索引快照")
    group.add_argument("--live", action="store_true", help="真扫 spc.joshw.info")
    group.add_argument("--dl", action="store_true", help="下载最小条目并验证续传")
    args = parser.parse_args(argv)

    mode = "offline"
    if args.bundled:
        mode = "bundled"
    elif args.live:
        mode = "live"
    elif args.dl:
        mode = "dl"

    try:
        if mode == "offline":
            return test_offline()
        if mode == "bundled":
            return test_bundled()
        if mode == "live":
            return test_live()
        return test_download()
    except Failure as exc:
        print("FAIL: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - 自测脚本统一兜底
        import traceback

        traceback.print_exc()
        print("ERROR: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
