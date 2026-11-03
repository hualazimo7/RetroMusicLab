# -*- coding: utf-8 -*-
"""v6.0.11 端到端：真起服务，真抓一次封面，看它落不落到 Artworks。

门禁里的封面测试全是桩（fetch 被替换掉），**没有一条验证真实链路**：
选机种 → 勾条目 → 点「下载封面」→ 图写到 `<下载根>/Artworks/<机种>/<游戏名>/`。
这个脚本就是补这一刀。

跑法::

    C:/Python314/python.exe vendor/_e2e_v611.py

只抓**一个**游戏（几百 KB），失败不重试。
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

# 目标机种：NES 的图小、命中率高、libretro 有目录
KEY = "nsf"


def _jget(base, path, timeout=30):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:                             # noqa: BLE001
            return e.code, {}


def _jpost(base, path, body, timeout=60):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:                             # noqa: BLE001
            return e.code, {}


def main():
    ok = 0
    bad = []

    def chk(cond, msg, extra=""):
        nonlocal ok
        if cond:
            ok += 1
            print("  [OK] %s" % msg, flush=True)
        else:
            bad.append(msg)
            print("  [FAIL] %s   %s" % (msg, extra), flush=True)

    from web_ui import server
    httpd, port, _state = server.serve(port=0)
    base = "http://127.0.0.1:%d" % port
    print("服务已起：%s" % base, flush=True)

    try:
        # ---- ① 切机种（load 带 key 就是切机种）----
        code, d = _jpost(base, "/api/dl/load", {"key": KEY})
        chk(code == 200 and d.get("ok"), "切到 %s" % KEY, d)
        _c, _snap = _jget(base, "/api/dl/state?since=0")
        dest = str(_snap.get("dest") or "")
        chk(bool(dest), "快照报得出保存目录", dest)

        # ---- ② 等列表（首次可能扫 30 秒；已有缓存就是秒回）----
        entries = []
        for _ in range(90):
            time.sleep(1)
            code, snap = _jget(base, "/api/dl/state?since=0")
            entries = snap.get("rows") or []
            if entries and not snap.get("scanning"):
                break
        chk(len(entries) > 0, "列表有 %d 条" % len(entries), len(entries))
        if not entries:
            return _done(ok, bad)

        # ---- ③ 封面根应该已经指到 Artworks ----
        import cover_art as ca
        root = ca.cache_root()
        chk("Artworks" in root, "封面根是 <下载根>/Artworks", root)
        chk(os.path.normpath(root) == os.path.normpath(
                os.path.join(os.path.dirname(os.path.dirname(dest)), "Artworks")),
            "根 = dest 往上两级 + Artworks", "%s / dest=%s" % (root, dest))
        plat = ca.platform_sub(KEY)
        chk(plat not in ("", KEY), "机种层用的是显示名（不是 key）", plat)

        # ---- ④ 挑一个**还没有封面目录、且库里确实有图**的游戏，勾上，抓 ----
        #
        # ⚠ 光挑「第一个还没封面目录的」不行：列表顺序随库更新变，
        #   会挑到 ``100-in-1-Contra Function 16`` 这种冷门合卡 ——
        #   libretro 和 IGDB 都没有，抓完 0 张图，看着像功能坏了，
        #   其实日志明写「两个源都没找到」。所以先用**真判据**
        #   (:func:`cover_art.pick`) 在库里筛一遍，挑一个确定有源的。
        #   纯读列表，不下载，几十毫秒。
        items = []
        try:
            items = ca.list_covers(KEY)
        except Exception as e:                        # noqa: BLE001
            print("  [--] 读封面列表失败（%s），退回盲挑" % e, flush=True)

        target = None
        for e in entries[:200]:
            name = str(e.get("name") or "")
            if not name:
                continue
            gdir = os.path.join(root, plat, ca._safe_name(name))
            if os.path.isdir(gdir):
                continue
            if items and not ca.pick(items, name):
                continue
            target = (e.get("url"), name, gdir)
            break
        if target is None:
            print("  [--] 前 200 条都已经有封面目录了，跳过抓图（结构验证已通过）",
                  flush=True)
            return _done(ok, bad)

        url, name, gdir = target
        print("  目标游戏：%s" % name, flush=True)
        code, d = _jpost(base, "/api/dl/fetch_covers", {"urls": [url]})
        chk(code == 200 and d.get("ok"), "fetch_covers 接受请求", d)

        # ---- ⑤ 等抓完 ----
        # ⚠ **不能只看「图文件出现了」，也不能只看 ``cover_done``**：
        #   ``fetch_all`` 一次要下最多 8 张（limit+2），第一张落盘时它还在
        #   下后面的，``_cover_many`` 没返回、``cover_done`` 就还是 0。
        #   实测只等 90 秒会超时，看起来像「查找表没写」，其实是没跑完。
        #   所以直接等**查找表里出现这个游戏**（这才是要验的东西），给足时间。
        got = []
        lk_file = os.path.join(root, "_lookup.json")
        _nk = ca.norm_title(name)
        for _ in range(300):
            time.sleep(1)
            try:
                with open(lk_file, encoding="utf-8") as fh:
                    if _nk in json.load(fh):
                        break
            except (OSError, ValueError):
                pass
        if os.path.isdir(gdir):
            got = [f for f in os.listdir(gdir) if f.lower().endswith(".png")]
        chk(bool(got), "图落到了 %s" % gdir, got)
        _c, snap3 = _jget(base, "/api/dl/state?since=0")
        for _l in (snap3.get("logs") or []):
            # 日志元素是 [seq, text]，不是 dict
            if isinstance(_l, (list, tuple)) and len(_l) > 1:
                _t = str(_l[1])
            elif isinstance(_l, dict):
                _t = str(_l.get("text") or "")
            else:
                _t = str(_l)
            if "🖼" in _t or "⚠" in _t:
                print("   日志| " + _t, flush=True)
        if got:
            sizes = [os.path.getsize(os.path.join(gdir, f)) for f in got]
            chk(all(s > 1000 for s in sizes),
                "每张都 > 1 KB（不是空文件 / 不是 404 的 HTML）",
                sizes)
            chk(all(open(os.path.join(gdir, f), "rb").read(8)
                    == b"\x89PNG\r\n\x1a\n" for f in got),
                "每张都是真 PNG（头 8 字节对得上）", got[:3])

            # ---- ⑥ 查找表里记的 rel 必须是三段，且能拼回真文件 ----
            lk_path = os.path.join(root, "_lookup.json")
            rels = []
            try:
                with open(lk_path, encoding="utf-8") as fh:
                    blob = json.load(fh)
                rels = blob.get(ca.norm_title(name)) or []
            except (OSError, ValueError):
                pass
            chk(bool(rels), "查找表里记了这个游戏", ca.norm_title(name))
            chk(all(str(r).count("/") == 2 for r, _k in rels),
                "rel 都是三段（机种/游戏名/文件名）", rels[:2])
            chk(all(os.path.isfile(os.path.join(root, str(r).replace("/", os.sep)))
                    for r, _k in rels),
                "每条 rel 都能在盘上找到文件（读写两侧对得上）", rels[:2])

            # ---- ⑦ 试听接口能拿到张数 ----
            code, d = _jget(base, "/api/dl/covers?game="
                            + urllib.parse.quote(name))
            chk(code == 200 and int(d.get("n") or 0) >= 1,
                "试听接口报得出张数", d)
    finally:
        try:
            httpd.shutdown()
        except Exception:                             # noqa: BLE001
            pass
    return _done(ok, bad)


def _done(ok, bad):
    print("\n" + "=" * 50)
    print("通过 %d 条，失败 %d 条" % (ok, len(bad)))
    for m in bad:
        print("  x", m)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
