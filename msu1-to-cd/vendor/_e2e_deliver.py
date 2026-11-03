# -*- coding: utf-8 -*-
"""交付前的一次真机端到端：拿**打好的 exe** 转一个真实 WonderSwan 包。

为什么单独写一个：`check_web_exe.py` 验的是「包完整 / 页面在 / API 通」，
`test_wsr.py` 验的是「源码里 WSR 解得对」。两者都不覆盖
「用户双击 exe → 拖进一个 .wsr 包 → 真的出一堆能听的 WAV」这条最外层的路。

用法::

    py -3.14 vendor/_e2e_deliver.py [--exe <路径>] [--src <WSR 包目录>]
    # 换一个包（v5.6 起支持 3DO 等任意格式）：
    py -3.14 vendor/_e2e_deliver.py --src <3DO 包目录> --tracks 4 \
        --sources 4 --min-mb 30 --min-secs 10

判据（全过才算交付）：
  1. /api/diag 的 version == 5.7，wsr 解码器 ok，aif_audio / zophar_source 在包里
  2. 加音源 → 1 个音源 / N 轨（WSR 默认 33：m3u 说 33，扫描会说 121 —— 防它回退）
  3. 转换 → done，ok == N，fail == 0
  4. 产出 N 个 WAV，总体积 > 阈值（WSR 默认 200 MB）
  5. 抽第一个 WAV 验格式（2ch / 44100 / 16bit）且**峰值不为 0**（不是静音）
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

DEFAULT_SRC = (r"D:\MSU1Downloads\joshw\WonderSwan"
               r"\Card Captor Sakura - Sakura to Fushigi na Clow Card "
               r"(1999-12-02)(SIMS)(Bandai)[WS]")
OUT_DIR = r"D:\MSU1Temp\deliver_e2e"

_fails = []


def mark(ok, name, detail=""):
    print(("  [OK] " if ok else "  [!!] ") + name + ("  " + str(detail) if detail else ""),
          flush=True)
    if not ok:
        _fails.append(name)
    return ok


def jreq(path, body=None, timeout=20):
    url = BASE + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_for(fn, secs, step=0.4):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if fn():
                return True
        except Exception:                     # noqa: BLE001
            pass
        time.sleep(step)
    return False


def peak_of(path):
    with wave.open(path, "rb") as w:
        nch, sw, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(min(n, sr * 3))
    if sw != 2:
        return nch, sw, sr, n, 0
    peak = 0
    for i in range(0, len(raw) - 1, 2):
        v = int.from_bytes(raw[i:i + 2], "little", signed=True)
        if v < 0:
            v = -v
        if v > peak:
            peak = v
    return nch, sw, sr, n, peak


def main(argv):
    exe = os.path.join(ROOT, "dist", "MSU1Converter-web.exe")
    src = DEFAULT_SRC
    n_want = 33
    min_mb = 200.0
    # 没有播放列表的包（3DO 的 .aif、平铺的 .adx…）是**逐文件展开**的：
    # 每个文件一个音源、一个音源 1 轨。这是有意设计（一首坏掉只废那一首），
    # 不是 bug —— 所以这两个期望值必须能改。
    n_src = 1
    min_secs = 30.0
    i = 0
    while i < len(argv):
        if argv[i] == "--exe":
            exe = argv[i + 1]; i += 2
        elif argv[i] == "--src":
            src = argv[i + 1]; i += 2
        elif argv[i] == "--tracks":
            n_want = int(argv[i + 1]); i += 2
        elif argv[i] == "--min-mb":
            min_mb = float(argv[i + 1]); i += 2
        elif argv[i] == "--sources":
            n_src = int(argv[i + 1]); i += 2
        elif argv[i] == "--min-secs":
            min_secs = float(argv[i + 1]); i += 2
        else:
            i += 1

    print("exe = %s" % exe, flush=True)
    print("src = %s" % src, flush=True)
    if not mark(os.path.isfile(exe), "exe 存在", exe):
        return 1
    if not mark(os.path.isdir(src), "样本包存在", src):
        return 1

    try:
        jreq("/api/state", timeout=2)
        print("  端口已被占用，先关掉在跑的实例", flush=True)
        return 1
    except Exception:                         # noqa: BLE001
        pass

    env = dict(os.environ)
    env.setdefault("MSU1_PSF_BIOS", os.path.join(os.path.dirname(exe), "bios", "scph39001.bin"))
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

        # 1. 版本 + 解码器
        d = jreq("/api/diag")
        # 版本号从源码里读，别在这里写死 —— 每发一版都要改这一处，
        # 改漏了就是一条「假红」（v5.9.7 就撞过一次：源码 5.9.7、这里 5.9.6）。
        import re as _re
        _want = "?"
        try:
            with open(os.path.join(ROOT, "web_ui", "server.py"),
                      encoding="utf-8") as _fh:
                _m = _re.search(r'APP_VERSION = "([\d.]+)"', _fh.read())
            _want = _m.group(1) if _m else "?"
        except OSError:
            pass
        mark(d.get("version") == _want,
             "/api/diag 版本与源码一致（%s）" % _want, d.get("version"))
        wsr = (d.get("wsr") or {})
        mark(bool(wsr.get("ok")), "WSR 解码器可用", wsr)
        # v5.6: 3DO 的 aif_audio 是函数内 import，静态分析看不到 ——
        # 打漏了只有「真去转一个 3DO 包」才会暴露。这里先查 diag。
        mods = d.get("modules") or {}
        mark(bool(mods.get("aif_audio")), "aif_audio 模块在包里（3DO）", mods)
        # zophar_source 只在**下载页**的 diag 里（它是 dl_api 的可选 import），
        # 主 diag 的 modules 看不到 —— 查错地方会得到一条假失败。
        dld = jreq("/api/dl/diag") or {}
        mark(bool(dld.get("zophar_source")),
             "zophar_source 在包里（下载页 diag）", dld.get("zophar_source"))
        # v5.8: KSS 改走 libkss。kss_native 是 __import__ 动态加载的，
        # 静态分析看不到；kss2wav.exe 靠 EXTRA_EXE 带 —— 两件都可能漏。
        mark(bool(mods.get("kss_native")), "kss_native 模块在包里（KSS）", mods)
        kssd = d.get("kss") or {}
        mark(bool(kssd.get("decoder")), "kss2wav.exe 在包里", kssd)

        # 2. 加音源
        r = jreq("/api/add_sources", {"paths": [src]})
        mark(r.get("added") == n_src, "加入 %d 个音源" % n_src, r.get("added"))
        st = jreq("/api/state")
        srcs = st.get("sources") or []
        n_tracks = srcs[0].get("n_tracks") if srcs else None
        mark(n_tracks == max(1, n_want // max(1, n_src)),
              "第一个音源报 %d 轨" % max(1, n_want // max(1, n_src)), n_tracks)

        # 3. 转换
        if os.path.isdir(OUT_DIR):
            shutil.rmtree(OUT_DIR, ignore_errors=True)
        jreq("/api/start", {"opts": {"do_wav": True, "do_cd": False,
                                     "out_dir": OUT_DIR}})
        t1 = time.time()
        ok = wait_for(lambda: jreq("/api/state").get("state") in ("done", "error", "idle"),
                      600, 0.5)
        st = jreq("/api/state")
        mark(ok and st.get("state") == "done", "转换结束", st.get("state"))
        res = st.get("result") or {}
        mark(res.get("ok") == n_want and res.get("fail") == 0,
             "ok=%d / fail=0" % n_want, "%s，%.1f s" % (res, time.time() - t1))

        # 4. 产出
        wavs = []
        for root, _dirs, files in os.walk(OUT_DIR):
            wavs += [os.path.join(root, f) for f in files if f.lower().endswith(".wav")]
        total = sum(os.path.getsize(p) for p in wavs)
        mark(len(wavs) == n_want, "产出 %d 个 WAV" % n_want, len(wavs))
        mark(total > min_mb * 1048576, "总体积 > %.0f MB" % min_mb,
             "%.1f MB" % (total / 1048576.0))

        # 5. 抽第一个验格式与峰值
        if wavs:
            nch, sw, sr, n, peak = peak_of(sorted(wavs)[0])
            mark((nch, sw, sr) == (2, 2, 44100), "WAV 规格 2ch/16bit/44100",
                 "%dch/%dbit/%dHz" % (nch, sw * 8, sr))
            mark(peak > 3000, "峰值不为 0（真的出声）", peak)
            mark(n / float(sr) > min_secs, "时长 > %.0f 秒" % min_secs,
                 "%.1f s" % (n / float(sr)))

        print("\n" + "=" * 46, flush=True)
        if _fails:
            print("交付端到端: %d 项失败 → %s" % (len(_fails), _fails), flush=True)
        else:
            print("交付端到端: 全部通过", flush=True)
        print("=" * 46, flush=True)
        return 1 if _fails else 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:                     # noqa: BLE001
            try:
                proc.kill()
            except Exception:                 # noqa: BLE001
                pass
        shutil.rmtree(OUT_DIR, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
