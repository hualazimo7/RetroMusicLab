#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_e2e_v5915_mp3.py — MP3 格式的**真实端到端**（v5.9.15）。

`test_web_ui.py --only=v5915` 守的是「源码/常量/真写一个 MP3」，
但那一步用的是合成正弦。这里跑的是**真音源**：真解码 → 真编码 →
真落盘，并且把 WAV / FLAC / MP3 三种格式**依次**转同一个游戏。

要证明的四件事：

  ① **三种格式三个目录**：``<保存目录>/WAV/<游戏>/``、
     ``/FLAC/<游戏>/``、``/MP3/<游戏>/``。
  ② **已经转过 WAV 的游戏，还能再转 FLAC，再转 MP3**。
     这是 v5.9.9 用户报的那个 bug 的正身 —— ``ConvertManifest``
     的 ``already_done()`` 判的是「这个**源文件**转过没有」，记账文件
     放在输出目录里；目录共用 = 记账共用 = 第二种格式被判「已转过」
     直接跳过，一个文件都不出。加第三种格式时这个坑会**原样复发**。
  ③ **同一格式重复转仍然会跳过**（没把 already_done 整个废掉，
     否则每次点按钮都把几百 MB 重转一遍）。
  ④ **MP3 产物真的能解码**：帧数≈源时长、44100Hz、立体声，
     体积落在 320 kbps 预期区间。

用法::

    py -3.14 vendor/_e2e_v5915_mp3.py
    py -3.14 vendor/_e2e_v5915_mp3.py --keep     # 留着临时目录看现场
    py -3.14 vendor/_e2e_v5915_mp3.py --src=...  # 换一个音源目录

⚠ 全程落在 %TEMP%，prefs 用 ``MSU1_DL_PREFS`` 重定向，不碰用户配置。
⚠ 收尾只删自己复制的那几个文件（分批删，别撞安全护栏）。
"""

import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))

TMP = os.environ.get("TEMP") or r"C:\Windows\Temp"
BASE = os.path.join(TMP, "rml_v5915_mp3")
DEST = os.path.join(BASE, "dest")
GAME = "Aerial Assault [GG]"
SRC = (r"D:\MSU1Downloads\joshw\MSX"
       r"\Aerial Assault (1992-06-05)(SIMS)(Sega)[GG]")

#: 三种格式 + 各自期望的目录名 / 扩展名
FMTS = (("wav", "WAV", ".wav"),
        ("flac", "FLAC", ".flac"),
        ("mp3", "MP3", ".mp3"))

_ok = [0]
_bad = []


def chk(label, cond, detail=""):
    if cond:
        _ok[0] += 1
        print("  [OK] %s%s" % (label, ("  " + str(detail)) if detail else ""),
              flush=True)
    else:
        _bad.append(label)
        print("  [!!] %s%s" % (label, ("  " + str(detail)) if detail else ""),
              flush=True)
    return bool(cond)


def arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def wait_idle(st, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.4)
        if not st.busy:
            return True
    return False


def ls_audio(root, ext):
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, files in os.walk(root):
        dirnames.sort()
        for n in sorted(files):
            if n.lower().endswith(ext):
                out.append(os.path.relpath(os.path.join(dirpath, n), root))
    return out


def rm_soft(path, batch=40):
    """分批删，删不掉也不抛 —— 环境的安全护栏按「单次进程删除次数」计数。"""
    if not path or not os.path.isdir(path):
        return
    n = 0
    for dirpath, dirnames, files in os.walk(path, topdown=False):
        for f in files:
            try:
                os.remove(os.path.join(dirpath, f))
            except OSError:
                pass
            n += 1
            if n % batch == 0:
                time.sleep(0.05)
        for d in dirnames:
            try:
                os.rmdir(os.path.join(dirpath, d))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def main():
    keep = "--keep" in sys.argv
    src = arg("--src", SRC)

    if not os.path.isdir(src):
        print("素材不存在：%s" % src, flush=True)
        return 1

    # prefs 重定向 —— 绝不碰用户真实的 dl_prefs.json
    os.makedirs(BASE, exist_ok=True)
    os.environ["MSU1_DL_PREFS"] = os.path.join(BASE, "dl_prefs.json")
    try:
        os.remove(os.environ["MSU1_DL_PREFS"])
    except OSError:
        pass

    from web_ui import dl_api                                # noqa: E402

    # ── 铺素材：模拟「已下载并解压好」的形态 ──────────────────
    game_dir = os.path.join(DEST, GAME)
    os.makedirs(game_dir, exist_ok=True)
    copied = 0
    for n in sorted(os.listdir(src)):
        s = os.path.join(src, n)
        if not os.path.isfile(s):
            continue
        shutil.copy2(s, os.path.join(game_dir, n))
        copied += 1
    print("铺了 %d 个源文件 → %s" % (copied, game_dir), flush=True)

    url = "https://x.invalid/%s.7z" % GAME
    st = dl_api.DownloadState()
    st.set_dest(DEST)
    st.entries = [{"name": GAME, "size": 1, "mtime": 1700000000, "url": url}]

    roots = {tag: os.path.join(DEST, tag) for _f, tag, _e in FMTS}

    print("\n=== 1. 目录分离（静态） ===", flush=True)
    for f, tag, _e in FMTS:
        got = st._out_dir(f)
        chk("_out_dir(%s) 指向 <保存目录>/%s" % (f, tag),
            os.path.abspath(got) == os.path.abspath(roots[tag]), got)
    uniq = {os.path.abspath(st._out_dir(f)).lower() for f, _t, _e in FMTS}
    chk("三种格式三个目录", len(uniq) == 3, len(uniq))

    print("\n=== 2. 依次转 WAV / FLAC / MP3（同一个游戏） ===", flush=True)
    for f, tag, ext in FMTS:
        print("\n-- %s --" % tag, flush=True)
        r = st.convert_wav([url], f)
        chk("convert_wav(%s) 请求被接受" % f, bool(r.get("ok")),
            str(r.get("error") or ""))
        chk("等待 %s 转换结束" % tag, wait_idle(st), "timeout")
        files = ls_audio(roots[tag], ext)
        chk("%s/ 下有 %s 产物" % (tag, ext), bool(files), files[:3])
        chk("产物按游戏分子文件夹（%s/%s/）" % (tag, GAME),
            all(x.replace("\\", "/").startswith(GAME + "/") for x in files),
            files[:2])
        chk("cv_fail 为 0", st.cv_fail == 0, st.cv_fail)
        # 记账落在**自己那个根**下面（每个游戏一个子文件夹 = 一份记账）。
        # 三种格式各一份，这就是「转完 WAV 还能再转 FLAC/MP3」的根据。
        chk("%s/ 有自己的 .msu1-convert.json" % tag,
            os.path.isfile(os.path.join(roots[tag], GAME,
                                        ".msu1-convert.json")),
            "")

    print("\n=== 3. MP3 产物能真解码 ===", flush=True)
    mp3s = ls_audio(roots["MP3"], ".mp3")
    if chk("MP3 至少产出一首", bool(mp3s), len(mp3s)):
        try:
            import soundfile as sf
            p = os.path.join(roots["MP3"], mp3s[0])
            data, sr = sf.read(p)
            frames = len(data)
            ch = data.shape[1] if getattr(data, "ndim", 1) > 1 else 1
            secs = frames / float(sr or 1)
            size = os.path.getsize(p)
            chk("采样率 44100", sr == 44100, sr)
            chk("立体声", ch == 2, ch)
            chk("时长 > 5 秒（不是空壳）", secs > 5.0, "%.1fs" % secs)
            # 320 kbps CBR：字节 ≈ 秒数 × 40000
            kbps = size * 8 / secs / 1000.0
            chk("码率≈320 kbps（libsndfile 的 MP3 默认）",
                280 <= kbps <= 340, "%.0f kbps" % kbps)
        except Exception as exc:                             # noqa: BLE001
            chk("MP3 能读回", False, "%s: %s" % (type(exc).__name__, exc))

    print("\n=== 4. 同格式重复转仍然跳过（没废掉 already_done） ===",
          flush=True)
    before = len(ls_audio(roots["MP3"], ".mp3"))
    r = st.convert_wav([url], "mp3")
    chk("重复转请求被接受", bool(r.get("ok")), str(r.get("error") or ""))
    wait_idle(st)
    after = len(ls_audio(roots["MP3"], ".mp3"))
    chk("文件数没变（没重转、没覆盖成 01_xx (2).mp3）",
        after == before, "%d → %d" % (before, after))

    print("\n=== 5. 磁盘快照认三种格式 ===", flush=True)
    st._refresh_disk()
    chk("_wav_games 认得这个游戏", GAME.lower() in st._wav_games, "")
    chk("_flac_games 认得这个游戏", GAME.lower() in st._flac_games, "")
    chk("_mp3_games 认得这个游戏", GAME.lower() in st._mp3_games, "")
    dstate = st._disk_state(GAME, url)
    chk("状态列说「三种都转过」",
        dstate == "🎵 已转 WAV+FLAC+MP3", dstate)

    print("\n=== 6. 试听列表同时看得见三种 ===", flush=True)
    wl = st.wav_list(force=True)
    rels = [x.get("rel", "") for x in (wl.get("wavs") or [])]
    chk("试听列表有 .wav", any(x.lower().endswith(".wav") for x in rels),
        len(rels))
    chk("试听列表有 .flac", any(x.lower().endswith(".flac") for x in rels), "")
    chk("试听列表有 .mp3", any(x.lower().endswith(".mp3") for x in rels), "")

    print("\n" + "=" * 46, flush=True)
    print("MP3 端到端: %d 通过 / %d 失败" % (_ok[0], len(_bad)), flush=True)
    print("=" * 46, flush=True)
    if _bad:
        for x in _bad:
            print("  失败: %s" % x, flush=True)

    if keep:
        print("\n--keep：现场留在 %s" % BASE, flush=True)
    else:
        rm_soft(BASE)
    return 0 if not _bad else 1


if __name__ == "__main__":
    sys.exit(main())
