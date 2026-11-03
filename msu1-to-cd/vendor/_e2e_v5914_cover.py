#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_e2e_v5914_cover.py — 封面链路的**真联网**端到端（v5.9.14）。

用户的原话是两件事：

> 「在下载音源界面增加封面下载功能，并在转换 flac 文件时，将对应游戏的
> 封面图片嵌入 flac 文件中。」

这两件事各自单独测过（``_smoke_cover_live.py`` 验抓图、``t_v5914`` 验
FLAC 图片块），但**接在一起**才叫用户要的功能。所以这里走完整的真实路径：

    起服务 → 选机种 → 拉真索引 → 勾 1 个条目 → 开始下载
      → 自动解压 → 自动转 FLAC（convert_job 按游戏名反查封面）
      → 读回 FLAC 的 METADATA_BLOCK_PICTURE

任何一环断了，最后一步都读不出图。

跑法::

    py -3.14 vendor/_e2e_v5914_cover.py              # 默认 gbs（包小）
    py -3.14 vendor/_e2e_v5914_cover.py spc 2        # 指定机种 + 条数
    py -3.14 vendor/_e2e_v5914_cover.py --no-dl      # 只验封面缓存反查
"""

import json
import os
import shutil
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

import tmpdir                                     # noqa: E402
tmpdir.init(sweep_old=False)

# 和门禁同款：偏好文件重定向到临时目录，别写进仓库、别读开发机的真实目录。
os.environ["MSU1_DL_PREFS"] = os.path.join(tmpdir.root(), "e2e_cover_prefs.json")
try:
    os.remove(os.environ["MSU1_DL_PREFS"])
except OSError:
    pass

from web_ui import server                          # noqa: E402

P = F = 0


def _m(cond, label, detail=""):
    global P, F
    if cond:
        P += 1
        print("  [OK] %s" % label, flush=True)
    else:
        F += 1
        print("  [!!] %s%s" % (label, ("  → " + str(detail)) if detail else ""),
              flush=True)
    return bool(cond)


BASE = ""


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


def main(argv):
    global BASE, P, F
    argv = [a for a in argv[1:]]
    no_dl = "--no-dl" in argv
    # ⚠ ``--keep`` 是给**本机护栏**留的：环境里挂着一道「本回合删除超过
    #   50 个文件要二次确认」的安全守卫，超了会**直接终止进程** ——
    #   一段跑到一半被砍掉的门禁，比不跑还糟（会让人以为是断言失败）。
    #   带 ``--keep`` 时不删临时目录，收尾自己去清。
    keep = "--keep" in argv
    # ``--base=http://127.0.0.1:47913`` 打到**已经在跑的 exe** 上。
    # 这条最有用：源码树里跑通不等于打包后跑通（``cover_art`` 是可选 import，
    # PyInstaller 静态分析算不出来，漏在 HIDDEN 外面的表现是「静默少一个功能」）。
    base = ""
    for a in argv:
        if a.startswith("--base="):
            base = a.split("=", 1)[1].rstrip("/")
    # ``--dest=`` 换一个干净的落盘目录。**打 exe 时一定要换**：
    # 复用上一轮的目录会让「下载跳过 / 转换跳过」，整段测试变成
    # 「什么都没干，但旧产物还在所以看起来通过」。
    dest_arg = ""
    for a in argv:
        if a.startswith("--dest="):
            dest_arg = a.split("=", 1)[1]
    argv = [a for a in argv if not a.startswith("--")]
    key = (argv[0] if argv else "gbs").strip().lower()
    want = int(argv[1]) if len(argv) > 1 else 1

    dest = dest_arg or os.path.join(tmpdir.root(), "e2e_cover_dl")
    httpd = None
    if base:
        BASE = base
        print("e2e 封面链路 · **外部后端** %s · 机种 %s · 目标目录 %s"
              % (BASE, key, dest), flush=True)
    else:
        httpd, port, state = server.serve(port=0)
        BASE = "http://127.0.0.1:%d" % port
        print("e2e 封面链路 · 后端 %s · 机种 %s · 目标目录 %s"
              % (BASE, key, dest), flush=True)

    try:
        # ---- 1) 依赖 ----
        print("\n[1] 依赖", flush=True)
        d = _get("/api/dl/diag")
        _m(d.get("cover_art") is True, "diag.cover_art", d.get("cover_art"))
        _m(d.get("joshw_dl") is True, "diag.joshw_dl", d.get("joshw_dl"))
        _m(d.get("dl_convert") is True, "diag.dl_convert", d.get("dl_convert"))
        _m(d.get("tar") is True, "diag.tar（解压 7z 要用）", d.get("tar"))

        import cover_art as ca
        import flac_out as fo

        # ---- 2) 封面能不能真抓到 ----
        print("\n[2] 真抓封面（%s）" % key, flush=True)
        if not ca.supported(key):
            print("  [!!] 机种 %s 没有封面源，换一个" % key, flush=True)
            return 1
        t0 = time.time()
        items = ca.list_covers(key)
        _m(bool(items), "拉到封面索引 %d 条（%.1fs）" % (len(items),
                                                      time.time() - t0), "")

        # ---- 3) 选机种 / 拿条目 ----
        print("\n[3] 下载页：选机种", flush=True)
        _post("/api/dl/set_opts", {"dest": dest, "auto_extract": True,
                                   "del_archive": False, "merge_wav": False,
                                   "auto_cover": True, "workers": 2,
                                   # ⚠ 必须是 auto_flac，不是 auto_wav：
                                   #   封面只嵌 FLAC，转 WAV 的话这一步永远
                                   #   读不出图片块（第一版就漏了这个）。
                                   "auto_flac": True})
        r = _post("/api/dl/load", {"key": key})
        _m(r.get("ok"), "load(%s) → %s / %d 条"
           % (key, r.get("source"), r.get("count") or 0), r)

        st = _get("/api/dl/state")
        rows = st.get("rows") or []
        _m(bool(rows), "列表有 %d 行" % len(rows), "")
        if not rows:
            return 1
        # 挑**最小的**几个：这条链路要真下载真转码，别拿 30 MB 的包折腾。
        # ⚠ 但太小的是**空壳**（实测 gbs 里有 340 B 的包，解出来一个文件都
        #   转不了）—— 那不是封面的问题，会让整段测试误报。所以下限 20 KB。
        pool = [x for x in rows if x.get("url") and (x.get("size") or 0) >= 20000]
        if not pool:
            pool = [x for x in rows if x.get("url")]
        picked = sorted(pool, key=lambda x: x.get("size") or 0)[:want]
        names = [x["name"] for x in picked]
        print("  选中：%s" % names, flush=True)

        # ---- 4) 下载 + 自动解压 + 自动转 FLAC ----
        if no_dl:
            print("\n[4] --no-dl：跳过真实下载", flush=True)
            return 0 if F == 0 else 1

        print("\n[4] 下载 → 解压 → 转 FLAC（真跑）", flush=True)
        r = _post("/api/dl/select", {"urls": [x["url"] for x in picked]})
        _m(r.get("ok"), "勾选 %d 个条目" % (r.get("selected") or 0), r)
        r = _post("/api/dl/start", {})
        _m(r.get("ok"), "start() → todo=%s" % r.get("todo"), r)

        t0 = time.time()
        last = ""
        cover_seen = 0
        while time.time() - t0 < 900:
            st = _get("/api/dl/state")
            s = st.get("stats") or {}
            line = "%s | 封面 %s/%s 转码 %s/%s" % (
                st.get("status"), s.get("cover_done"), s.get("cover_total"),
                s.get("cv_ok"), s.get("cv_queued"))
            if line != last:
                print("  · %s" % line, flush=True)
                last = line
            cover_seen = max(cover_seen, int(s.get("cover_done") or 0))
            if (not st.get("busy") and st.get("phase") in ("done", "idle")
                    and int(s.get("cv_ok") or 0) >= 1):
                break
            if st.get("phase") in ("done", "error") and not st.get("busy"):
                break
            time.sleep(2)

        st = _get("/api/dl/state")
        s = st.get("stats") or {}
        _m(int(s.get("dl_ok") or 0) >= 1, "下载成功 %s 个" % s.get("dl_ok"), s)
        _m(int(s.get("cv_ok") or 0) >= 1, "转出 %s 个文件" % s.get("cv_ok"), s)
        _m(int(s.get("cover_ok") or 0) >= 1,
           "抓到封面 %s 张（失败 %s）" % (s.get("cover_ok"), s.get("cover_fail")),
           s)

        # ---- 5) FLAC 里到底有没有图 ----
        print("\n[5] 读回 FLAC 的图片块", flush=True)
        found = []
        for root, _dirs, files in os.walk(dest):
            for f in files:
                if f.lower().endswith(".flac"):
                    found.append(os.path.join(root, f))
        _m(bool(found), "产物里有 %d 个 FLAC" % len(found), dest)
        n_pic = 0
        for f in found:
            pics = fo.pictures(f)
            if pics:
                n_pic += 1
                print("  🖼 %s → %s" % (os.path.basename(f), pics), flush=True)
        _m(n_pic >= 1, "%d/%d 个 FLAC 带封面" % (n_pic, len(found)), "")

        # 音频本身还得是对的（封面不许把音频搞坏）
        bad = []
        for f in found:
            info = fo.probe_flac(f)
            if not info or info[1] != 44100 or info[2] != 2:
                bad.append((os.path.basename(f), info))
        _m(not bad, "所有 FLAC 都是 44100/立体声", bad[:2])
    finally:
        if httpd is not None:
            httpd.shutdown()
        if keep:
            print("\n（--keep：临时目录保留在 %s）" % dest, flush=True)
        else:
            try:
                shutil.rmtree(dest, ignore_errors=True)
            except OSError:
                pass

    print("\n" + "=" * 46, flush=True)
    print("e2e 封面链路: %d 通过 / %d 失败" % (P, F), flush=True)
    print("=" * 46, flush=True)
    return 0 if F == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
