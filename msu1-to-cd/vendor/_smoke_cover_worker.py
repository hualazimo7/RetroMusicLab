#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_smoke_cover_worker.py — 封面线程接线的离线冒烟。

**不联网**：把 ``cover_art.fetch`` 换成假的，只验接线本身：

1. ``_cover_worker`` 能把 ``cover_*`` 计数器走完（total/done/ok/fail）；
2. 单条失败**不会**中断整批（后面还得继续抓）；
3. 抛异常也**不会**让线程死掉；
4. ``cancel`` 置位后立刻收手；
5. ``snapshot()["stats"]`` 真的带出了这 5 个字段；
6. ``diag()["cover_art"]`` 是 True。

跑法::

    python vendor/_smoke_cover_worker.py
"""

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

import dl_api                                     # noqa: E402
import cover_art                                  # noqa: E402

N_OK = 0
N_TOTAL = 0


def main():
    global N_OK, N_TOTAL
    api = dl_api.DlApi() if hasattr(dl_api, "DlApi") else None
    if api is None:
        # 类名可能不叫 DlApi，按模块里的类名兜一下
        cands = [v for v in vars(dl_api).values()
                 if isinstance(v, type) and hasattr(v, "_cover_worker")]
        if not cands:
            print("[!!] 找不到带 _cover_worker 的类")
            return 1
        api = cands[0]()
        print("[ok] 用类 %s" % cands[0].__name__)

    if cover_art is None:
        print("[!!] dl_api.cover_art 是 None —— 模块没打进去？")
        return 1

    # ---- 1) 正常 + 失败 + 异常三种条目混在一起 ----
    real_fetch = cover_art.fetch
    real_list = cover_art.list_covers

    def fake_list(key, **kw):
        return [{"dir": "X", "name": "Y.png"}]

    def fake_fetch(key, title, **kw):
        if "boom" in title:
            raise RuntimeError("故意炸的")
        if "miss" in title:
            return False, "", "列表里没有"
        return True, os.path.join("C:/tmp", "%s.png" % title), "OK 12 KB"

    cover_art.list_covers = fake_list
    cover_art.fetch = fake_fetch
    try:
        items = [("spc", "Alpha"), ("spc", "miss one"), ("spc", "boom!"),
                 ("spc", "Beta")]
        t = threading.Thread(target=api._cover_worker, args=(items, None))
        t.start()
        t.join(10)
        assert not t.is_alive(), "线程没退出"
        print("[ok] total=%d done=%d ok=%d fail=%d"
              % (api.cover_total, api.cover_done, api.cover_ok,
                 api.cover_fail))
        assert api.cover_done == 4, api.cover_done
        assert api.cover_ok == 2, api.cover_ok
        assert api.cover_fail == 2, api.cover_fail
        assert api.cover_current == "", repr(api.cover_current)
        print("[ok] 三种条目（成功/未找到/抛异常）都算进去了，线程活着")
    finally:
        cover_art.fetch = real_fetch
        cover_art.list_covers = real_list

    # ---- 2) cancel 置位后立刻收手 ----
    api.cover_total = api.cover_done = api.cover_ok = api.cover_fail = 0
    ev = threading.Event()
    ev.set()

    def slow_fetch(key, title, **kw):
        time.sleep(0.05)
        return True, "x.png", "OK"

    cover_art.list_covers = fake_list
    cover_art.fetch = slow_fetch
    try:
        items = [("spc", "G%d" % i) for i in range(20)]
        t = threading.Thread(target=api._cover_worker, args=(items, ev))
        t.start()
        t.join(5)
        assert not t.is_alive(), "cancel 后线程没退出"
        assert api.cover_ok == 0, api.cover_ok
        print("[ok] cancel 置位 → 一张都不抓（ok=%d）" % api.cover_ok)
    finally:
        cover_art.fetch = real_fetch
        cover_art.list_covers = real_list

    # ---- 3) snapshot / diag 带出字段 ----
    snap = api.snapshot()
    st = snap.get("stats") or {}
    for k in ("cover_total", "cover_done", "cover_ok", "cover_fail",
              "cover_current"):
        assert k in st, "snapshot.stats 缺 %s" % k
    print("[ok] snapshot.stats 带出 5 个 cover_* 字段")

    d = api.diag()
    assert d.get("cover_art") is True, d.get("cover_art")
    assert isinstance(d.get("covers"), dict), type(d.get("covers"))
    print("[ok] diag.cover_art=%s covers=%s"
          % (d["cover_art"], d["covers"]))

    # ---- 4) 默认开、能关 ----
    assert api.opts.get("auto_cover") is True, api.opts.get("auto_cover")
    print("[ok] opts.auto_cover 默认 True")

    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
