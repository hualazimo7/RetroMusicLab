#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_e2e_zophar_dl.py — 下载页端到端（第二家音源站 Zophar）。

跟 ``_e2e_deliver.py`` 的分工：
  * ``_e2e_deliver`` 走「加本地音源 → 转换」那条路（不联网）；
  * 这个脚本走「**下载页**」那条路：机种树 → 扫描 → 选中 → 下载 → 解压 → 转 WAV。

**它必须联网**（要真的从 zophar 拉包），所以不进常规门禁，交付前手动跑。

用法::

    py -3.14 vendor/_e2e_zophar_dl.py
    py -3.14 vendor/_e2e_zophar_dl.py --exe <别的 exe> --key z_sms --games 3

判据（全过才算这条链路通）：
  1. ``/api/dl/diag``：version == 5.7、``zophar_source`` 在包里、32 个机种
  2. ``/api/dl/platforms``：有「Zophar 原声」组，正好 2 个机种
  3. ``/api/dl/load z_gg``：扫出 >= 200 个游戏（分页走完了；只抓第 1 页是 200）
  4. ``/api/dl/start``：todo == 选中的游戏数（说明**直链解析全成功**）
  5. 落盘 ``<游戏>.zip``（不是 .7z），解出来的目录里有 ``.sgc`` + ``.m3u``
  6. WAV 数 > 选中数（m3u 分轨生效），且第一个是 2ch/16bit/44100、**峰值不为 0**
  7. 状态里每个选中的行都变成「✅ 完成」（``_url_for`` 映射没断）
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import wave

BASE = "http://127.0.0.1:47913"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = r"D:\MSU1Temp\zophar_e2e"

_fails = []


def mark(ok, name, detail=""):
    print(("  [OK] " if ok else "  [!!] ") + name
          + ("  " + str(detail) if detail else ""), flush=True)
    if not ok:
        _fails.append(name)
    return ok


def jreq(path, body=None, timeout=30):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_for(fn, secs, step=0.5):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if fn():
                return True
        except Exception:                          # noqa: BLE001
            pass
        time.sleep(step)
    return False


def wav_info(path):
    with wave.open(path, "rb") as w:
        nch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        n = w.getnframes()
        raw = w.readframes(min(n, sr * 2))
    peak = 0
    if sw == 2:
        for i in range(0, len(raw) - 1, 2):
            v = abs(int.from_bytes(raw[i:i + 2], "little", signed=True))
            if v > peak:
                peak = v
    return nch, sw, sr, n, peak


def walk(root, exts):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if os.path.splitext(f)[1].lower() in exts:
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def main(argv):
    exe = os.path.join(ROOT, "dist", "MSU1Converter-web.exe")
    key = "z_gg"
    n_games = 2
    i = 0
    while i < len(argv):
        if argv[i] == "--exe":
            exe = argv[i + 1]; i += 2
        elif argv[i] == "--key":
            key = argv[i + 1]; i += 2
        elif argv[i] == "--games":
            n_games = int(argv[i + 1]); i += 2
        else:
            i += 1

    print("exe = %s" % exe, flush=True)
    print("key = %s   games = %d" % (key, n_games), flush=True)
    if not mark(os.path.isfile(exe), "exe 存在", exe):
        return 1

    try:
        jreq("/api/state", timeout=2)
        print("  端口已被占用，先关掉在跑的实例", flush=True)
        return 1
    except Exception:                              # noqa: BLE001
        pass

    env = dict(os.environ)
    env.setdefault("MSU1_PSF_BIOS",
                   os.path.join(os.path.dirname(exe), "bios", "scph39001.bin"))
    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=env)
    print("  已拉起 pid=%d" % proc.pid, flush=True)
    try:
        t0 = time.time()
        ok = wait_for(lambda: jreq("/api/state", timeout=2).get("state") is not None, 180)
        mark(ok, "后端起来", "%.1f s" % (time.time() - t0))
        if not ok:
            return 1

        # ---- 1. diag ----
        d = jreq("/api/diag")
        mark(d.get("version") == "5.7", "/api/diag 版本 = 5.7", d.get("version"))

        dd = jreq("/api/dl/diag")
        mark(dd.get("ok") is True, "/api/dl/diag ok", dd.get("ok"))
        mark(dd.get("zophar_source") is True,
              "zophar_source 在包里（HIDDEN 没漏）", dd.get("zophar_source"))
        mark(int(dd.get("platforms") or 0) == 32,
              "机种数 32（joshw 30 + zophar 2）", dd.get("platforms"))

        # ---- 2. 机种树 ----
        p = jreq("/api/dl/platforms")
        groups = p.get("groups") or []
        zg = [g for g in groups if g.get("group") == "Zophar 原声"]
        mark(len(zg) == 1 and len(zg[0].get("items") or []) == 2,
              "「Zophar 原声」组正好 2 个机种",
              [g.get("group") for g in groups])
        names = [it.get("name") for g in groups for it in (g.get("items") or [])]
        mark("Game Gear" in names and "Master System" in names,
              "Game Gear / Master System 都在树里", names[-3:])

        # ---- 3. 扫描 ----
        r = jreq("/api/dl/load", {"key": key, "force": False})
        mark(r.get("ok") is True, "load(%s) ok" % key, r)
        wait_for(lambda: not jreq("/api/dl/state").get("scanning"), 120, 0.5)
        snap = jreq("/api/dl/state")
        n = int(snap.get("count") or 0)
        # 分页每页 200 条：只抓第 1 页正好是 200，走完是 25x
        mark(n >= 200, "扫出 >= 200 个游戏（分页走完了）", n)
        mark(snap.get("key") == key, "当前机种正确", snap.get("key"))

        # ---- 4. 选中 + 开始 ----
        rows = snap.get("rows") or []
        if not mark(len(rows) >= n_games, "列表够选 %d 个" % n_games, len(rows)):
            return 1
        picks = [rows[i]["url"] for i in range(n_games)]
        pick_names = [rows[i]["name"] for i in range(n_games)]
        print("  选中: %s" % pick_names, flush=True)
        s = jreq("/api/dl/select", {"urls": picks, "mode": "set"})
        mark(s.get("sel") == n_games or s.get("ok") is True,
              "选中 %d 个" % n_games, s)

        if os.path.isdir(OUT_DIR):
            shutil.rmtree(OUT_DIR, ignore_errors=True)
        st = jreq("/api/dl/start", {"dest": OUT_DIR, "auto_wav": True,
                                    "auto_extract": True, "merge_wav": False,
                                    "del_archive": False, "workers": 2})
        mark(st.get("ok") is True, "start ok", st)
        # todo == 选中数 —— 解析阶段全部成功才会这样
        mark(st.get("todo") == n_games,
              "todo == %d（说明直链解析全成功）" % n_games, st.get("todo"))

        t1 = time.time()
        ok = wait_for(lambda: not jreq("/api/dl/state").get("busy"), 600, 1.0)
        snap = jreq("/api/dl/state")
        mark(ok, "下载+解压+转 WAV 跑完", "%.1f s" % (time.time() - t1))

        stats = snap.get("stats") or {}
        mark(int(stats.get("dl_ok") or 0) == n_games,
              "下载成功 %d 个" % n_games, stats.get("dl_ok"))
        mark(int(stats.get("dl_fail") or 0) == 0, "下载失败 0 个", stats.get("dl_fail"))
        mark(int(stats.get("ex_pkgs") or 0) == n_games,
              "解压成功 %d 个" % n_games, stats.get("ex_pkgs"))
        mark(int(stats.get("cv_fail") or 0) == 0, "转 WAV 失败 0 个", stats.get("cv_fail"))

        # ---- 5. 落盘形态 ----
        zips = walk(OUT_DIR, {".zip"})
        mark(len(zips) == n_games, "落盘 %d 个 .zip（不是 .7z）" % n_games,
              [os.path.basename(z) for z in zips])
        mark(all(not z.lower().endswith(".7z") for z in walk(OUT_DIR, {".7z"})),
              "没有误落成 .7z")
        sgcs = walk(OUT_DIR, {".sgc"}) + walk(OUT_DIR, {".vgm"})
        mark(bool(sgcs), "解出了 .sgc / .vgm 原始音源",
              [os.path.basename(x) for x in sgcs][:3])
        m3us = walk(OUT_DIR, {".m3u"})
        mark(bool(m3us), "解出了 .m3u 播放列表", len(m3us))

        # ---- 6. WAV ----
        wavs = walk(os.path.join(OUT_DIR, "WAV"), {".wav"})
        mark(len(wavs) > n_games,
              "WAV 数 > %d（m3u 分轨生效）" % n_games, len(wavs))
        if wavs:
            nch, sw, sr, nframes, peak = wav_info(wavs[0])
            mark((nch, sw, sr) == (2, 2, 44100),
                  "第一个 WAV 是 2ch/16bit/44100", (nch, sw, sr))
            mark(peak > 500, "第一个 WAV 峰值不为 0（不是静音）", peak)
            mark(nframes > 44100, "第一个 WAV 长于 1 秒",
                  "%.1f s" % (nframes / float(sr or 1)))
            total = sum(os.path.getsize(w) for w in wavs)
            mark(total > 5 * 1024 * 1024, "WAV 总体积 > 5 MB",
                  "%.1f MB" % (total / 1048576.0))

        # ---- 7. 行状态回写（_url_for 那条链）----
        rows = {r["url"]: r for r in (jreq("/api/dl/state").get("rows") or [])}
        bad = []
        for u in picks:
            stt = str((rows.get(u) or {}).get("state") or "")
            # 终态有三种：只下载 / 已存在 / 下载并转完 WAV
            if not any(k in stt for k in ("✅", "⏭", "🎵")):
                bad.append((rows.get(u, {}).get("name"), stt))
        mark(not bad, "每个选中的行状态都变成完成（_url_for 没断）", bad)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:                          # noqa: BLE001
            try:
                proc.kill()
            except Exception:                      # noqa: BLE001
                pass

    print()
    if _fails:
        print("失败 %d 项：%s" % (len(_fails), _fails), flush=True)
        return 1
    print("全部通过。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
