#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_probe_fmt_dirs.py — 验证「同一游戏可以分别转 WAV 和 FLAC」。

要证明的三件事（对应 v5.9.9 用户报的两条）：

  ① **已转 WAV 的游戏还能再转 FLAC**。以前两种格式都写进 ``WAV/``、
     共用一份 ``.msu1-convert.json``，而 ``already_done()`` 判的是
     「这个源文件转过没有」—— 转完 WAV 后记账里有 ``01_xx.wav`` 且文件
     还在，转 FLAC 时直接判「已转过」跳过，一个 FLAC 都不出。
  ② **产物分目录**：WAV → ``<保存目录>/WAV/<游戏>/``，
     FLAC → ``<保存目录>/FLAC/<游戏>/``，一游戏一文件夹。
  ③ **同格式重复转仍然会跳过**（没把 already_done 整个废掉，
     否则每次点按钮都把几百 MB 重转一遍）。

用法::

    py -3.14 vendor/_probe_fmt_dirs.py
    py -3.14 vendor/_probe_fmt_dirs.py --keep      # 留着临时目录看现场

⚠ 全程落在 %TEMP%，prefs 用 MSU1_DL_PREFS 重定向，不碰用户配置。
⚠ 素材只复制 1 个包（11 首），收尾只删自己复制的那几个文件。
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
BASE = os.path.join(TMP, "rml_fmtdirs")
DEST = os.path.join(BASE, "dest")
GAME = "Aerial Assault [GG]"
SRC = (r"D:\MSU1Downloads\joshw\MSX"
       r"\Aerial Assault (1992-06-05)(SIMS)(Sega)[GG]")

ok_n = [0]
bad = []


def chk(label, cond, detail=""):
    if cond:
        ok_n[0] += 1
        print("  [OK] %s%s" % (label, ("  " + detail) if detail else ""), flush=True)
    else:
        bad.append(label)
        print("  [!!] %s%s" % (label, ("  " + detail) if detail else ""), flush=True)


def arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def wait_idle(st, timeout=180):
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


def main():
    keep = "--keep" in sys.argv

    # prefs 重定向 —— 绝不碰用户真实的 dl_prefs.json
    os.makedirs(BASE, exist_ok=True)
    os.environ["MSU1_DL_PREFS"] = os.path.join(BASE, "dl_prefs.json")

    from web_ui import dl_api                                   # noqa: E402

    if not os.path.isdir(SRC):
        print("素材不存在：%s" % SRC, flush=True)
        return 1

    # ── 铺素材：模拟「已下载并解压好」的形态 ──────────────────
    game_dir = os.path.join(DEST, GAME)
    os.makedirs(game_dir, exist_ok=True)
    copied = []
    for n in sorted(os.listdir(SRC)):
        s = os.path.join(SRC, n)
        if not os.path.isfile(s):
            continue
        d = os.path.join(game_dir, n)
        shutil.copy2(s, d)
        copied.append(d)
    print("铺了 %d 个源文件到 %s" % (len(copied), game_dir), flush=True)

    url = "https://x.invalid/%s.7z" % GAME
    st = dl_api.DownloadState()
    st.set_dest(DEST)
    st.entries = [{"name": GAME, "size": 1, "mtime": 1700000000, "url": url}]

    wav_root = os.path.join(DEST, "WAV")
    flac_root = os.path.join(DEST, "FLAC")

    print("\n=== 1. 目录分离（静态） ===", flush=True)
    chk("_out_dir(wav) 指向 <保存目录>/WAV",
        st._out_dir("wav") == os.path.abspath(wav_root), st._out_dir("wav"))
    chk("_out_dir(flac) 指向 <保存目录>/FLAC",
        st._out_dir("flac") == os.path.abspath(flac_root), st._out_dir("flac"))
    chk("两者不是同一个目录", st._out_dir("wav") != st._out_dir("flac"))

    print("\n=== 2. 转 WAV ===", flush=True)
    r = st.convert_wav([url], "wav")
    print("  convert_wav(wav) → %s" % r, flush=True)
    chk("请求被接受", bool(r.get("ok")), str(r.get("error") or ""))
    chk("返回的 dir 是 WAV 目录",
        os.path.abspath(str(r.get("dir") or "")) == os.path.abspath(wav_root),
        str(r.get("dir")))
    chk("转换跑完（没有超时）", wait_idle(st), "")
    wavs = ls_audio(wav_root, ".wav")
    print("  WAV/ 下：%d 个 .wav（前 3：%s）" % (len(wavs), wavs[:3]), flush=True)
    chk("WAV/ 里真的出了 .wav", len(wavs) >= 1, "%d 个" % len(wavs))
    chk("WAV/ 里没有混进 .flac", len(ls_audio(wav_root, ".flac")) == 0,
        "%d 个" % len(ls_audio(wav_root, ".flac")))
    chk("产物在按游戏分的子文件夹里",
        all(w.replace("\\", "/").startswith(GAME + "/") for w in wavs),
        wavs[0] if wavs else "")
    chk("状态列显示「已转 WAV」",
        st._disk_state(GAME, url) == "🎵 已转 WAV",
        st._disk_state(GAME, url))

    print("\n=== 3. 再转 FLAC（**这是用户报的 bug**） ===", flush=True)
    r2 = st.convert_wav([url], "flac")
    print("  convert_wav(flac) → %s" % r2, flush=True)
    chk("请求被接受（没被「已转过」挡回去）", bool(r2.get("ok")),
        str(r2.get("error") or ""))
    chk("返回的 dir 是 FLAC 目录",
        os.path.abspath(str(r2.get("dir") or "")) == os.path.abspath(flac_root),
        str(r2.get("dir")))
    chk("转换跑完（没有超时）", wait_idle(st), "")
    flacs = ls_audio(flac_root, ".flac")
    print("  FLAC/ 下：%d 个 .flac（前 3：%s）" % (len(flacs), flacs[:3]), flush=True)
    chk("FLAC/ 里真的出了 .flac", len(flacs) >= 1, "%d 个" % len(flacs))
    chk("FLAC/ 里没有混进 .wav", len(ls_audio(flac_root, ".wav")) == 0,
        "%d 个" % len(ls_audio(flac_root, ".wav")))
    chk("产物同样按游戏分子文件夹",
        all(f.replace("\\", "/").startswith(GAME + "/") for f in flacs),
        flacs[0] if flacs else "")
    chk("WAV 产物还在（没被 FLAC 那一轮覆盖/清掉）",
        len(ls_audio(wav_root, ".wav")) >= 1,
        "%d 个" % len(ls_audio(wav_root, ".wav")))
    chk("状态列变成「已转 WAV+FLAC」",
        st._disk_state(GAME, url) == "🎵 已转 WAV+FLAC",
        st._disk_state(GAME, url))

    print("\n=== 4. 同格式再转一次：应该跳过（不是重转） ===", flush=True)
    n_before = len(ls_audio(flac_root, ".flac"))
    r3 = st.convert_wav([url], "flac")
    chk("请求仍被接受（排队是允许的）", bool(r3.get("ok")),
        str(r3.get("error") or ""))
    wait_idle(st)
    n_after = len(ls_audio(flac_root, ".flac"))
    chk("文件数没变（already_done 仍在起作用，没白转一遍）",
        n_after == n_before, "%d → %d" % (n_before, n_after))

    print("\n=== 5. 试听 / 上传云盘 能同时看到两种格式 ===", flush=True)
    st._refresh_disk()
    wl = st.wav_list(force=True)
    fmts = sorted(set(w.get("fmt") for w in (wl.get("wavs") or [])))
    chk("wav_list 同时列出 wav 和 flac", fmts == ["flac", "wav"], str(fmts))
    chk("wav_list 的 total 是两种之和", (wl.get("total") or 0) >= 2,
        str(wl.get("total")))
    gp = st.wav_games()
    g0 = (gp.get("games") or [{}])[0]
    chk("wav_games 把同一游戏合成一条", len(gp.get("games") or []) == 1,
        "%d 条" % len(gp.get("games") or []))
    chk("wav_games 标出两种格式", g0.get("fmt_text") == "FLAC+WAV",
        str(g0.get("fmt_text")))
    # 试听取文件的越界校验：两种格式各挑一个，都必须能取到
    for tag, items in (("wav", wavs), ("flac", flacs)):
        rel = items[0].replace("\\", "/") if items else ""
        chk("wav_path 能取到 %s 文件" % tag, bool(st.wav_path(rel)), rel)
    chk("wav_path 挡住越界路径",
        st.wav_path("../../../Windows/System32/calc.exe") is None, "")
    chk("wav_path 挡住非音频扩展名",
        st.wav_path("%s/01.txt" % GAME) is None, "")

    print("\n=== 6. 打开文件夹会先给 WAV，没有才给 FLAC ===", flush=True)
    p, tag, _note = st.open_dir_target()
    chk("有 WAV 目录时开 WAV", tag == "wav", "%s / %s" % (tag, p))

    # ── 收尾：只删自己复制的那几个文件 ──────────────────────
    if not keep:
        for p in copied:
            try:
                os.remove(p)
            except OSError:
                pass
        for sub in ("WAV", "FLAC"):
            root = os.path.join(DEST, sub)
            g = os.path.join(root, GAME)
            try:
                for n in os.listdir(g):
                    try:
                        os.remove(os.path.join(g, n))
                    except OSError:
                        pass
                os.rmdir(g)
                os.rmdir(root)
            except OSError:
                pass
        try:
            os.rmdir(game_dir)
        except OSError:
            pass
        try:
            os.remove(os.path.join(BASE, "dl_prefs.json"))
        except OSError:
            pass

    print("\n" + "=" * 56, flush=True)
    print("结果：%d 项通过，%d 项失败" % (ok_n[0], len(bad)), flush=True)
    for b in bad:
        print("  失败：%s" % b, flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
