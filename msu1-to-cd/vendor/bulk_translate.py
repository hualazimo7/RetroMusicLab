# -*- coding: utf-8 -*-
"""把全库(30 个机种)还没中文名的条目一次性翻完。

用法（在本项目目录下）：
    python vendor/bulk_translate.py                 # 全部机种
    python vendor/bulk_translate.py --only nsf,spc  # 只翻指定机种
    python vendor/bulk_translate.py --limit 200     # 每个机种最多翻 200 条(试跑)

特性：
- **可断点续跑**：结果增量写进 `zh_titles_user.json`，重跑时已翻过的自动跳过；
- 只翻 `zh_titles.py` 内置词典里没有的，手工确认过的译名不动；
- 谷歌不通会自动切备用源（见 zh_online.providers）；
- 翻译质量是机器直译，个别不贴切的直接编辑 `zh_titles_user.json` 改掉即可。
"""

import argparse
import gzip
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import zh_online                                            # noqa: E402
import zh_titles                                           # noqa: E402
import joshw_source as jw                                  # noqa: E402

_JUNK = re.compile(r"^[\d\s\-_.()]+$")


def titles_of(key):
    """内置快照里该机种的所有标题(去元数据、去重)。"""
    path = os.path.join(ROOT, "index", "%s.json.gz" % key)
    if not os.path.isfile(path):
        return []
    with gzip.open(path, "rb") as fh:
        data = json.loads(fh.read().decode("utf-8"))
    out, seen = [], set()
    for e in data.get("entries", []):
        core = zh_titles.core_title(e.get("name", ""))
        key2 = zh_titles._norm(core)
        if not key2 or key2 in seen:
            continue
        if _JUNK.match(core):
            continue
        seen.add(key2)
        out.append(core)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只翻这些机种, 逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="每个机种最多翻多少条")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--pause", type=float, default=0.25)
    ap.add_argument("--save-every", type=int, default=120)
    args = ap.parse_args()

    try:
        print("翻译服务探测: %s" % zh_online.probe(), flush=True)
    except zh_online.TranslateError as exc:
        print("❌ 所有翻译服务都不可用: %s" % exc, flush=True)
        return 2

    keys = [k for k in (args.only.split(",") if args.only else
                        [p["key"] for p in jw.PLATFORMS])]
    grand_total = grand_done = 0
    t_start = time.time()

    for key in keys:
        names = titles_of(key)
        todo = [n for n in names if not zh_titles.zh_name(n)]
        if args.limit:
            todo = todo[:args.limit]
        if not todo:
            print("[%-4s] 已有中文名, 跳过 (%d 条)" % (key, len(names)), flush=True)
            continue

        print("[%-4s] 待翻 %d / 共 %d" % (key, len(todo), len(names)), flush=True)
        sink = {}
        t0 = time.time()
        since_save = 0

        def progress(done, total, _key=key, _sink=sink):
            nonlocal since_save
            now = time.time()
            rate = done / max(0.001, now - _t0[0])
            eta = (total - done) / max(0.001, rate)
            print("    %s %d/%d  %.1f 条/秒  预计还需 %dm%02ds  [%s]"
                  % (_key, done, total, rate, eta // 60, eta % 60,
                     zh_online.current_provider()), flush=True)
            since_save = done - getattr(progress, "_last", 0)
            if since_save >= args.save_every:
                progress._last = done          # type: ignore[attr-defined]
                zh_titles.add_pairs(_sink)
                try:
                    zh_titles.save_pairs(_sink)
                except Exception as exc:         # noqa: BLE001
                    print("    ⚠ 落盘失败: %s" % exc, flush=True)

        _t0 = [time.time()]
        try:
            zh_online.translate_titles(todo, on_progress=progress,
                                       batch=args.batch, pause=args.pause,
                                       sink=sink)
        except zh_online.TranslateError as exc:
            print("    ⚠ %s 机种翻译中断: %s" % (key, exc), flush=True)
        finally:
            zh_titles.add_pairs(sink)
            try:
                path = zh_titles.save_pairs(sink)
                print("    ✔ %s 落库 %d 条 -> %s (%.0fs)"
                      % (key, len(sink), path, time.time() - t0), flush=True)
            except Exception as exc:             # noqa: BLE001
                print("    ⚠ 落盘失败: %s" % exc, flush=True)
        grand_total += len(todo)
        grand_done += len(sink)

    print("=" * 60, flush=True)
    print("本次翻译 %d 条，成功 %d 条，用时 %.0f 分钟"
          % (grand_total, grand_done, (time.time() - t_start) / 60), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())