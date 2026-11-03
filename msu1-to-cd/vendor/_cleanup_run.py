#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_cleanup_run.py — 按清单顺序把目标送进回收站，逐项打印进度。

为什么要单独一个跑器，而不是一条条敲命令：

    1. 回收站删除是**慢操作**（实测约 33 个文件/秒，2.3 万文件要 12 分钟），
       必须放后台跑，不能卡住交互；
    2. 中途某一项被占用（比如 exe 还在跑、文件被播放器打开）不该让整批停下 ——
       记下来继续下一项，最后统一报；
    3. 每一项删前删后都要打体积，出问题能一眼看出是哪一项没清掉。

清单是 JSON：``[{"tag": "名字", "paths": [...]}, ...]``，每项一个 tag。
"""
import argparse
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import _trash  # noqa: E402


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.1f GB" % n


def size_of(paths):
    total = 0
    for p in paths:
        if os.path.isfile(p):
            total += os.path.getsize(p)
        elif os.path.isdir(p):
            for dp, _dn, fn in os.walk(p):
                for f in fn:
                    try:
                        total += os.path.getsize(os.path.join(dp, f))
                    except OSError:
                        pass
    return total


def expand(paths):
    out = []
    for p in paths:
        if any(c in p for c in "*?"):
            out.extend(sorted(glob.glob(p)))
        else:
            out.append(p)
    # 去重、保序
    seen, uniq = set(), []
    for p in out:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", help="JSON 清单路径")
    args = ap.parse_args(argv)

    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)

    t0 = time.time()
    grand = 0
    failed = []
    for item in plan:
        tag = item.get("tag") or "?"
        paths = expand(item.get("paths") or [])
        if not paths:
            print("[跳过] %s —— 没有匹配到任何路径" % tag, flush=True)
            continue
        before = size_of(paths)
        print("[开始] %-28s %3d 项 / %s" % (tag, len(paths), human(before)),
              flush=True)
        t1 = time.time()
        ok, bad = _trash.trash(paths)
        after = size_of(paths)
        grand += max(0, before - after)
        print("[完成] %-28s 成功 %d / 失败 %d / 回收 %s / 耗时 %.0fs"
              % (tag, len(ok), len(bad), human(before - after), time.time() - t1),
              flush=True)
        for p, why in bad:
            print("        [未清] %s —— %s" % (p, why), flush=True)
            failed.append((tag, p, why))

    print("", flush=True)
    print("=" * 60, flush=True)
    print("合计回收 %s ｜ 耗时 %.1f 分钟 ｜ 未清 %d 项"
          % (human(grand), (time.time() - t0) / 60.0, len(failed)), flush=True)
    print("=" * 60, flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
