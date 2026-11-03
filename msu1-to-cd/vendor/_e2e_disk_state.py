#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_e2e_disk_state.py — 「状态」列靠磁盘事实 + 目录能跨机找回（v5.9.3）。

用户报的原话是两句，这个脚本就验这两句：

1. 「exe 程序关闭重启后，状态栏就清空了，也不知道哪个文件是已经下载了」
   → 造一批**不同完成度**的落盘形态（只有压缩包 / 包删了只剩解压目录 /
     已经转出 WAV / 转换失败留下的空目录 / 压根没下过），然后**新建一个
     ``DownloadState`` 实例**（等价于重启程序），看状态列能不能一条条对上。
     关键：这个实例的 ``self.states`` 是空的 —— 全靠磁盘快照算。

2. 「解压目录和 WAV 目录在不同电脑上也要能自动识别，不要绑定本台电脑」
   → 把 ``dl_prefs.json`` 里的路径写成**本机不存在的盘符**（模拟换机器 /
     盘符从 D: 变 E:），看新实例能不能自己找回来。

跑法::

    python vendor/_e2e_disk_state.py
    python vendor/_e2e_disk_state.py --keep      # 保留临时目录，方便手动看

为什么不用浏览器：这两件事 100% 在后端（前端 ``paintState()`` 只是把
``row.state`` 贴进 ``.c-state``，早被 ``test_web_ui.py`` 的 DOM 断言守住了）。
起 Edge 只会让这个脚本从 3 秒变 40 秒，还多一个端口冲突的可能。
"""

import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

PASS = [0]
FAIL = []


def check(name, ok, detail=""):
    if ok:
        PASS[0] += 1
        print("  [OK]   %s%s" % (name, ("  — " + str(detail)) if detail else ""),
              flush=True)
    else:
        FAIL.append(name)
        print("  [FAIL] %s  — %s" % (name, detail), flush=True)


def arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def touch(path, size=1024):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)
    return path


def main():
    keep = "--keep" in sys.argv
    tmp = tempfile.mkdtemp(prefix="rml_state_")

    # 用户状态（wav_cleared.json）和下载页偏好都重定向到临时目录 ——
    # 不能污染 %LOCALAPPDATA%，更不能往仓库目录里写 dl_prefs.json。
    os.environ["LOCALAPPDATA"] = os.path.join(tmp, "localappdata")
    prefs_path = os.path.join(tmp, "dl_prefs.json")
    os.environ["MSU1_DL_PREFS"] = prefs_path

    import dl_api

    plat = "Super Nintendo"
    root = os.path.join(tmp, "MSU1Downloads")
    dest = os.path.join(root, "joshw", plat)
    wav_root = os.path.join(dest, "WAV")

    print("临时目录: %s" % tmp, flush=True)
    print("\n[1] 造落盘形态", flush=True)

    # A：包在，没解压、没转
    touch(os.path.join(dest, "Game A [SNES].7z"))
    # B：勾了「解压后删除原压缩包」—— 包没了，只剩解压目录
    os.makedirs(os.path.join(dest, "Game B [SNES]"), exist_ok=True)
    touch(os.path.join(dest, "Game B [SNES]", "track 01.spc"))
    # C：包还在 + 已转出 WAV
    touch(os.path.join(dest, "Game C [SNES].7z"))
    touch(os.path.join(wav_root, "Game C [SNES]", "01 - Title.wav"))
    # D：转换失败留下的**空目录** —— 不能报「已转 WAV」
    touch(os.path.join(dest, "Game D [SNES].7z"))
    os.makedirs(os.path.join(wav_root, "Game D [SNES]"), exist_ok=True)
    # E：压根没下过
    # F：包在、WAV 目录里只有非 wav 文件（比如 .log）—— 不能报「已转 WAV」
    touch(os.path.join(dest, "Game F [SNES].7z"))
    touch(os.path.join(wav_root, "Game F [SNES]", "convert.log"))
    # G：解压目录在、WAV 也在（最深一级）
    os.makedirs(os.path.join(dest, "Game G [SNES]"), exist_ok=True)
    touch(os.path.join(wav_root, "Game G [SNES]", "01.wav"))

    entries = []
    for nm in ("Game A [SNES]", "Game B [SNES]", "Game C [SNES]",
               "Game D [SNES]", "Game E [SNES]", "Game F [SNES]",
               "Game G [SNES]"):
        entries.append({"name": nm,
                        "url": "https://example.invalid/%s.7z"
                               % nm.replace(" ", "%20").replace("[", "%5B")
                                  .replace("]", "%5D"),
                        "size": 1024, "mtime": 1700000000})
    url_of = {e["name"]: e["url"] for e in entries}

    expect = {
        "Game A [SNES]": "✓ 已下载",
        "Game B [SNES]": "📦 已解压",
        "Game C [SNES]": "🎵 已转 WAV",
        "Game D [SNES]": "✓ 已下载",       # 空 WAV 目录不算转好
        "Game E [SNES]": "",               # 没下过
        "Game F [SNES]": "✓ 已下载",       # 只有 .log 也不算转好
        "Game G [SNES]": "🎵 已转 WAV",
    }

    # ── 第一次运行：设目录（走真实的 set_dest，会落盘 prefs）───────────
    print("\n[2] 第一次运行：设保存目录", flush=True)
    a = dl_api.DownloadState()
    a.set_dest(dest)
    check("set_dest 落盘了 dl_prefs.json", os.path.isfile(prefs_path),
          prefs_path)
    prefs = json.load(open(prefs_path, encoding="utf-8"))
    check("prefs 记了 root", os.path.abspath(prefs.get("root", "")).lower()
          == os.path.abspath(root).lower(), prefs.get("root"))
    check("prefs 记了 dest", os.path.abspath(prefs.get("dest", "")).lower()
          == os.path.abspath(dest).lower(), prefs.get("dest"))
    check("prefs 记了 manual=True", prefs.get("manual") is True, prefs.get("manual"))
    check("prefs 没把选项写进去（换机不继承并发数）",
          "opts" not in prefs and "workers" not in prefs, sorted(prefs))

    a.entries = [dict(e) for e in entries]
    a._refresh_disk()
    snap = a.snapshot()
    # 有磁盘痕迹的是 A/B/C/D/F/G 共 6 条；E 没下过。
    check("第一次运行：盘上有痕迹的 6 条", snap["n_on_disk"] == 6,
          "n_on_disk=%s" % snap["n_on_disk"])

    # ── 模拟重启：全新实例，states 是空的 ─────────────────────────────
    print("\n[3] 模拟重启 exe（新实例，内存状态全空）", flush=True)
    b = dl_api.DownloadState()
    check("重启后 dest 自动恢复", os.path.abspath(b.dest).lower()
          == os.path.abspath(dest).lower(), b.dest)
    check("重启后内存状态确实是空的（不是靠内存蒙对）",
          len(b.states) == 0, len(b.states))
    b.entries = [dict(e) for e in entries]
    b._refresh_disk()
    s2 = b.snapshot()
    got = {r["name"]: r["state"] for r in s2["rows"]}
    bad = []
    for nm, want in expect.items():
        if got.get(nm, "<缺行>") != want:
            bad.append("%s: 期望 %r 实际 %r" % (nm, want, got.get(nm, "<缺行>")))
    check("重启后状态列**一条不差**", not bad,
          "；".join(bad) if bad else "7 条全对")
    check("重启后 n_on_disk 也对（6 条）", s2["n_on_disk"] == 6,
          s2["n_on_disk"])
    check("on_disk 标记与 state 一致",
          all((r["state"] != "") == r["on_disk"] for r in s2["rows"]),
          [(r["name"], r["state"], r["on_disk"]) for r in s2["rows"]])

    # ── 运行期临时状态优先于磁盘状态 ──────────────────────────────────
    print("\n[4] 运行期状态优先（正在转 WAV 时不能显示成「已下载」）", flush=True)
    u = url_of["Game A [SNES]"]
    b.states[u] = "⏳ 待转 WAV"
    s3 = b.snapshot()
    row = [r for r in s3["rows"] if r["url"] == u][0]
    check("内存状态覆盖磁盘状态", row["state"] == "⏳ 待转 WAV", row["state"])
    b.states[u] = ""
    row = [r for r in b.snapshot()["rows"] if r["url"] == u][0]
    check("内存状态清掉后落回磁盘状态", row["state"] == "✓ 已下载", row["state"])

    # ── 换机 1：盘符变了（保留后面的整条路径）─────────────────────────
    print("\n[5] 换机：只换了盘符（D:\\Games\\MSU1Downloads → E:\\…）", flush=True)
    drives = dl_api._local_drives()
    me = os.path.splitdrive(os.path.abspath(root))[0]
    other = next((d for d in drives if d.lower() != me.lower()), "")
    if other:
        # ⚠ 这条用例的 tail 必须**只存在于另一个盘**上。用真实 tmp 的 tail
        #   去试是不行的：那个 tail 在原盘上也有一份，换盘符第一轮就命中
        #   原盘了 —— 结果对，但什么也没证明（第一版就写成这样，白测一轮）。
        alt_tail = os.path.join("rml_alt_%d" % os.getpid(), "MSU1Downloads")
        alt_root = os.path.join(other + os.sep, alt_tail)
        try:
            os.makedirs(os.path.join(alt_root, "joshw", plat), exist_ok=True)
            touch(os.path.join(alt_root, "joshw", plat, "Game A [SNES].7z"))
            asked = os.path.join("Z:" + os.sep, alt_tail)
            got_alt = dl_api._relocate_dir(asked, "joshw")
            check("换盘符能找回（Z: 不存在 → 命中 %s）" % other,
                  os.path.abspath(got_alt).lower()
                  == os.path.abspath(alt_root).lower(),
                  "%r -> %r" % (asked, got_alt))
            check("换盘符找的是**同一条相对路径**，不是随便一个同名目录",
                  os.path.splitdrive(got_alt)[1].lower()
                  == os.path.splitdrive(alt_root)[1].lower(), got_alt)
        except OSError as exc:
            print("  [SKIP] 另一个盘不可写，跳过: %s" % exc, flush=True)
        finally:
            shutil.rmtree(os.path.join(other + os.sep,
                                       "rml_alt_%d" % os.getpid()),
                          ignore_errors=True)
    else:
        print("  [SKIP] 本机只有一个盘，跳过换盘符用例", flush=True)

    # ── 换机 2：位置彻底变了（有界深度搜索）──────────────────────────
    print("\n[6] 换机：目录位置也变了（有界深度搜索）", flush=True)
    deep_name = "MSU1DownloadsDeep"
    moved = os.path.join(tmp, "别的盘", "Games", deep_name)   # 距 tmp 3 层
    shutil.copytree(root, moved, dirs_exist_ok=True)
    hit = dl_api._find_by_name(deep_name, "joshw", roots=[tmp], max_depth=6)
    check("深度搜索能按目录名找到", os.path.abspath(hit).lower()
          == os.path.abspath(moved).lower(), "%r" % hit)
    check("深度搜索尊重 max_depth（2 层到不了，3 层刚好）",
          dl_api._find_by_name(deep_name, "joshw",
                               roots=[tmp], max_depth=2) == ""
          and dl_api._find_by_name(deep_name, "joshw",
                                   roots=[tmp], max_depth=3) != "",
          "max_depth=2 应为空")
    check("深度搜索要 joshw 才算命中（同名但不相干的不算）",
          dl_api._find_by_name(deep_name, "zophar", roots=[tmp],
                               max_depth=6) == "", "must_have=zophar")

    # ── 换机 3：真的重启一次，prefs 里是坏盘符 ────────────────────────
    print("\n[7] 换机：重启 + prefs 里是坏盘符，目录仍能找回", flush=True)
    tail = os.path.splitdrive(os.path.abspath(root))[1].lstrip("\\/")
    bad_root = os.path.join("Z:" + os.sep, tail)
    json.dump({"root": bad_root, "dest": os.path.join(bad_root, "joshw", plat),
               "manual": True},
              open(prefs_path, "w", encoding="utf-8"), ensure_ascii=False)
    c = dl_api.DownloadState()
    check("坏盘符的 dest 被找回", os.path.abspath(c.dest).lower()
          == os.path.abspath(dest).lower(), "%r -> %r" % (bad_root, c.dest))
    check("坏盘符的 root 被找回", os.path.abspath(c._root_path).lower()
          == os.path.abspath(root).lower(), c._root_path)
    check("搬家被标记出来（load 时会写日志告诉用户）",
          getattr(c, "_root_moved", False) is True, c.__dict__.get("_root_saved"))
    check("搬家后把新位置写回了 prefs（下次不用再找）",
          os.path.abspath(json.load(open(prefs_path, encoding="utf-8"))
                          .get("root", "")).lower()
          == os.path.abspath(root).lower(),
          json.load(open(prefs_path, encoding="utf-8")).get("root"))
    c.entries = [dict(e) for e in entries]
    c._refresh_disk()
    s4 = c.snapshot()
    check("搬家后状态列照样对（换机不等于重下）",
          {r["name"]: r["state"] for r in s4["rows"]} == expect,
          s4["n_on_disk"])

    # ── 性能：4000 行 snapshot 不能把轮询拖垮 ─────────────────────────
    print("\n[8] 性能：4000 行 snapshot 耗时", flush=True)
    big = dl_api.DownloadState()
    big.dest = dest
    big._refresh_disk()
    big.entries = [{"name": "Filler %04d [SNES]" % i,
                    "url": "https://example.invalid/f%04d.7z" % i,
                    "size": 1, "mtime": 1700000000} for i in range(4000)]
    t0 = time.time()
    big.snapshot()
    one = (time.time() - t0) * 1000
    t0 = time.time()
    for _ in range(10):
        big.snapshot()
    avg = (time.time() - t0) * 1000 / 10
    check("4000 行 snapshot < 40ms（轮询 300ms 一次）", avg < 40,
          "首次 %.1fms（要算文件名），之后均值 %.1fms（走缓存）" % (one, avg))
    check("缓存真的省了钱（首次比之后慢 2 倍以上）", one > avg * 2,
          "首次 %.1fms / 之后 %.1fms" % (one, avg))
    check("缓存键覆盖 4000 条（没被截断）", len(big._dstate_cache) == 4000,
          len(big._dstate_cache))
    # 磁盘变了必须失效，否则新下完的包一直显示「没下过」
    touch(os.path.join(dest, "Filler 0007 [SNES].7z"))
    big._refresh_disk()
    r7 = [r for r in big.snapshot()["rows"]
          if r["name"] == "Filler 0007 [SNES]"][0]
    check("磁盘快照一变缓存就失效（新落盘立刻显示已下载）",
          r7["state"] == "✓ 已下载", r7["state"])
    os.remove(os.path.join(dest, "Filler 0007 [SNES].7z"))

    # ── 目录不可读时不能崩 ────────────────────────────────────────────
    print("\n[9] 边界：保存目录被删掉", flush=True)
    gone = dl_api.DownloadState()
    gone.dest = os.path.join(tmp, "根本不存在的目录")
    try:
        gone._refresh_disk()
        snap = gone.snapshot()
        check("目录不存在不抛异常、状态列全空", snap["n_on_disk"] == 0,
              snap["n_on_disk"])
    except Exception as exc:                         # noqa: BLE001
        check("目录不存在不抛异常、状态列全空", False, repr(exc))

    # ── 保存目录可以填成任意一层，扫描以「用户填的那一层」为准 ──────────
    print("\n[10] 保存目录填哪一层都能扫到（用户可自定义任意路径）", flush=True)
    ent = [dict(e) for e in entries]
    want_c = expect

    def _states_at(d):
        """把保存目录设成 d，看状态列对不对。"""
        x = dl_api.DownloadState()
        x.set_dest(d)
        x.entries = [dict(e) for e in ent]
        x._refresh_disk()
        s = x.snapshot()
        return ({r["name"]: r["state"] for r in s["rows"]}, s)

    # (a) 填成根：包在下面两层（joshw/<机种>/），一层扫描会全军覆没
    got, s = _states_at(root)
    check("填成根（…\\MSU1Downloads，包在下面两层）", got == want_c,
          "n_on_disk=%s %s" % (s["n_on_disk"],
                              [k for k, v in got.items() if v != want_c.get(k)]))
    # (b) 填成站：包在下面一层（截图里用户填的就是这一层）
    got, s = _states_at(os.path.join(root, "joshw"))
    check("填成站（…\\joshw，包在下面一层）", got == want_c, s["n_on_disk"])
    # (c) 填成机种：包就在这一层
    got, s = _states_at(dest)
    check("填成机种（…\\joshw\\Super Nintendo，包就在这层）",
          got == want_c, s["n_on_disk"])
    # (d) 完全自定义的路径（不在 MSU1Downloads 下面）
    custom = os.path.join(tmp, "我的音乐", "游戏原声 收藏")
    shutil.copytree(dest, custom, dirs_exist_ok=True)
    got, s = _states_at(custom)
    check("填成完全自定义的路径", got == want_c, s["n_on_disk"])
    # (e) 同一份数据在 WAV 也在 —— WAV 跟着走，不能只在默认位置才认
    check("自定义路径下的 WAV 也认得出（🎵 不丢）",
          got.get("Game C [SNES]") == "🎵 已转 WAV", got.get("Game C [SNES]"))
    # (f) scan 字段告诉界面「按哪个目录扫的」
    check("snapshot 带 scan.dir（界面 tooltip 用）",
          os.path.abspath(s.get("scan", {}).get("dir", "")).lower()
          == os.path.abspath(custom).lower(), s.get("scan"))
    check("scan 里有 files / dirs / wav 计数",
          all(k in (s.get("scan") or {}) for k in ("files", "dirs", "wav",
                                                   "limited")),
          s.get("scan"))
    # (g) 有界：再往下几层就超出 DEPTH，扫不到（保证「填成 D:\」不会卡死）
    #     包放在 L1/L2/L3/L4/ 里 = 距 deep_root 4 层 > DEPTH(3)
    deep_root = os.path.join(tmp, "深测")
    too_deep = os.path.join(deep_root, "L1", "L2", "L3", "L4")
    shutil.copytree(dest, too_deep, dirs_exist_ok=True)
    x = dl_api.DownloadState()
    x.set_dest(deep_root)
    x.entries = [dict(e) for e in ent]
    x._refresh_disk()
    check("超出 DEPTH 的更深目录不会被扫（有界，防卡死）",
          x.snapshot()["n_on_disk"] == 0,
          "DEPTH=%s，包在 %s" % (dl_api.DISK_SCAN_DEPTH, too_deep))
    x2 = dl_api.DownloadState()
    x2.set_dest(too_deep)
    x2.entries = [dict(e) for e in ent]
    x2._refresh_disk()
    check("但把目录直接填到那一层就又认得了",
          x2.snapshot()["n_on_disk"] == 6, x2.snapshot()["n_on_disk"])

    print("\n" + "=" * 60, flush=True)
    if FAIL:
        print("❌ 失败 %d 项 / 通过 %d 项" % (len(FAIL), PASS[0]), flush=True)
        for f in FAIL:
            print("   - %s" % f, flush=True)
    else:
        print("✅ 全部通过（%d 项）" % PASS[0], flush=True)
    print("=" * 60, flush=True)

    if keep:
        print("临时目录保留在: %s" % tmp, flush=True)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
