"""_probe_new_platforms.py -- 实测 v5.9 新机种能不能真扫出条目。

为什么要单独跑：`joshw_source._pf` 新加的 `path` 字段拼错时，界面上**照样**
会把机种列出来（PLATFORMS 是静态表），点进去才会 404 —— 静态门禁看不出来。
所以这里对每个新机种真发一次请求，至少确认根页面能抓到、且能解析出子目录。

用法：
    py -3.14 vendor\\_probe_new_platforms.py            # 全部新机种
    py -3.14 vendor\\_probe_new_platforms.py kss_gg     # 只测一个
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import joshw_source as jw  # noqa: E402

NEW = ["kss_gg", "kss_msx", "kss_sms", "switch",
       "hoot_arcade", "hoot_qsf"]


def probe(key):
    pf = jw.platform(key)
    url = jw.base_url(pf)
    f = jw._Fetcher()
    t0 = time.time()
    try:
        html = f.fetch(url)
    except BaseException as exc:                      # noqa: BLE001
        return False, "抓取失败: %s" % exc, 0, 0
    entries = jw.parse_index(html, url)
    dirs = jw._parse_dirs(html, url)
    dt = time.time() - t0
    ok = bool(entries or dirs)
    return ok, "%d 个 .7z / %d 个子目录" % (len(entries), len(dirs)), dt, len(entries)


def main():
    keys = sys.argv[1:] or NEW
    bad = 0
    for k in keys:
        ok, msg, dt, _n = probe(k)
        pf = jw.platform(k)
        print("%s %-14s %-22s %-26s %5.1fs" % (
            "OK  " if ok else "FAIL", k, pf["name"], msg, dt))
        if not ok:
            bad += 1
        time.sleep(0.25)
    print()
    print("失败 %d / %d" % (bad, len(keys)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
