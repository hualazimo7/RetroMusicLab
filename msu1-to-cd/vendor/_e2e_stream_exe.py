"""真 exe 端到端：把 `.hwas` / `.swav` / `.dvi` 各**实转一首**（v5.2.7）。

前面的 `_e2e_skip_reason_exe.py` 只验到「能收进音源列表」（added=33），
**没验能不能真转出 WAV**。这个脚本补上那一步：起真 exe → 加三个文件 →
转换 → 逐个读 WAV，验三件事：

1. **时长对得上**（和 `stream_audio.decode_any` 的帧数换算一致，误差 < 2%）
2. **不是 DC**（旧 bug 的症状是整段几乎不变的低频隆隆声）——
   判据是相邻样本平均绝对差必须够大，且过零率不能接近 0
3. **采样率/声道是输出规格**（44100 Hz / 2ch / 16bit）

跑法（工作目录无所谓，路径都是绝对的）：

    C:/Python314/python.exe vendor/_e2e_stream_exe.py
"""
import json
import os
import struct
import subprocess
import sys
import time
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = os.path.join(ROOT, "dist", "MSU1Converter-web.exe")
BASE = "http://127.0.0.1:47913"
OUT = r"D:\MSU1Temp\e2e_stream_exe"

NDS = (r"D:\MSU1Downloads\joshw\Nintendo DS"
       r"\007 - Quantum of Solace (2008-10-31)(Vicarious Visions)"
       r"(Activision)[NDS]")
SAT = (r"D:\MSU1Downloads\joshw\Saturn"
       r"\Akumajou Dracula X - Gekka no Yasoukyoku (1998-06-25)"
       r"(KCE Tokyo)(KCE Nagoya)(Konami)[SAT]")

#: (文件, 源声道, 源采样率, 源帧数, 说明)
#: 期望值来自 `stream_audio.decode_any`，而它已经和 vgmstream r2117 逐样本
#: 比对过 78 个真实文件、最大差 0 —— 所以这里可以当基准用。
CASES = [
    (os.path.join(NDS, "JBond_Music_IntroLogos.hwas"),
     1, 24356, 98304, ".hwas（NDS，裸 IMA，每块重置）"),
    (os.path.join(NDS, "JBond_Music_Transition_Chill2Action.swav"),
     1, 24050, 15528, ".swav（NDS，文件级 4 字节头）"),
    (os.path.join(SAT, "19 Silence.dvi"),
     2, 44100, 738864, ".dvi（Saturn，高半字节先 + 4 字节交错）"),
]

OUT_RATE = 44100

_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}))       # 绕开系统代理

_fail = 0


def ok(cond, name, detail=""):
    global _fail
    if not cond:
        _fail += 1
    print("  [%s] %s%s" % ("OK" if cond else "FAIL", name,
                           "" if cond else "  ← %s" % (detail,)))


def _get(path, timeout=5):
    try:
        with _opener.open(BASE + path, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception:                          # noqa: BLE001
        return 0, {}


def _post(path, body, timeout=20):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with _opener.open(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _analyze(path):
    """读 WAV，返回 (声道, 采样率, 位深, 帧数, 峰值, 相邻平均绝对差, 过零率)。"""
    with wave.open(path, "rb") as w:
        nch, rate, width = w.getnchannels(), w.getframerate(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
        n = w.getnframes()
    # 只看左声道就够判断「是不是 DC」
    cnt = len(raw) // (width * nch)
    vals = struct.unpack_from("<%dh" % (cnt * nch), raw)
    left = vals[0::nch]
    peak = max((abs(v) for v in left), default=0)
    if len(left) < 2:
        return nch, rate, width * 8, n, peak, 0.0, 0.0
    diff = sum(abs(left[i] - left[i - 1]) for i in range(1, len(left)))
    avg_diff = diff / float(len(left) - 1)
    zc = sum(1 for i in range(1, len(left))
             if (left[i - 1] < 0) != (left[i] < 0))
    return nch, rate, width * 8, n, peak, avg_diff, zc / float(len(left))


def main():
    if not os.path.isfile(EXE):
        print("找不到 exe:", EXE)
        return 2
    miss = [p for p, _c, _r, _n, _d in CASES if not os.path.isfile(p)]
    if miss:
        print("找不到样本:\n  " + "\n  ".join(miss))
        return 2

    if _get("/api/state", timeout=1)[0]:
        print("端口 47913 已被占用，先关掉在跑的实例")
        return 2

    proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        started = False
        for _ in range(120):
            time.sleep(0.5)
            if _get("/api/state", timeout=1)[0] == 200:
                started = True
                break
            if proc.poll() is not None:
                break
        print("启动:", "OK" if started else
              "失败（进程 returncode=%s）" % proc.poll())
        if not started:
            return 2

        # ---- 1. 三个文件都要被收成音源 ----
        _post("/api/clear", {})
        code, d = _post("/api/add_sources",
                        {"paths": [p for p, *_ in CASES]})
        print("\n[1] 加三个文件：added=%s" % d.get("added"))
        ok(code == 200 and d.get("added") == 3,
           "3 个音源全部收下（.hwas/.swav/.dvi）", d.get("added"))

        # ---- 2. 真转 ----
        if os.path.isdir(OUT):
            for f in os.listdir(OUT):
                fp = os.path.join(OUT, f)
                if os.path.isfile(fp):
                    os.remove(fp)
        os.makedirs(OUT, exist_ok=True)
        code, d = _post("/api/start", {"opts": {"out_dir": OUT,
                                                "do_cd": False,
                                                "do_wav": True}})
        print("\n[2] 启动转换：%s %s" % (code, d))
        st = {}
        for _ in range(360):
            time.sleep(0.5)
            code, st = _get("/api/state")
            if st.get("state") != "running":
                break
        print("    终态：%s  结果：%s" % (st.get("state"), st.get("result")))
        ok(st.get("state") == "done", "转换正常结束", st.get("state"))

        wavs = []
        for root, _dirs, fs in os.walk(OUT):
            wavs += [os.path.join(root, f) for f in fs
                     if f.lower().endswith(".wav")]
        print("\n[3] 产出 %d 个 WAV" % len(wavs))
        ok(len(wavs) == 3, "三个文件都转出了 WAV", len(wavs))

        # ---- 3. 逐个验波形 ----
        for path, _sch, srate, frames, desc in CASES:
            stem = os.path.splitext(os.path.basename(path))[0]
            hit = [w for w in wavs if stem in os.path.basename(w)]
            print("\n[4] %s" % desc)
            if not hit:
                ok(False, "%s 有产出" % stem, "没找到含 %r 的 WAV" % stem)
                continue
            nch, rate, bits, n, peak, avg_diff, zcr = _analyze(hit[0])
            want_sec = frames / float(srate)
            got_sec = n / float(rate)
            print("    %s" % os.path.basename(hit[0]))
            print("    %dch %dHz %dbit %.3f 秒（期望 %.3f 秒）峰值 %d "
                  "相邻差 %.1f 过零率 %.4f"
                  % (nch, rate, bits, got_sec, want_sec, peak,
                     avg_diff, zcr))
            ok(nch == 2 and rate == OUT_RATE and bits == 16,
               "输出规格 2ch/44100/16bit", "%dch/%dHz/%dbit" % (nch, rate, bits))
            ok(abs(got_sec - want_sec) <= max(0.05, want_sec * 0.02),
               "时长对得上（±2%）", "%.3f vs %.3f" % (got_sec, want_sec))
            # DC 的判据：相邻样本几乎不变、过零率接近 0
            ok(avg_diff >= 50.0,
               "不是 DC（相邻样本平均差 ≥ 50）", "%.1f" % avg_diff)
            ok(zcr >= 0.002,
               "有真实波形（过零率 ≥ 0.2%）", "%.5f" % zcr)
            ok(peak >= 3000,
               "音量正常（峰值 ≥ 3000）", peak)

        print("\n" + "=" * 46)
        print("exe 流式音频验收: %s" % ("全部通过" if _fail == 0
                                        else "%d 项失败" % _fail))
        print("=" * 46)
        return 0 if _fail == 0 else 1
    finally:
        try:
            _post("/api/quit", {})
        except Exception:                      # noqa: BLE001
            pass
        time.sleep(1.0)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:                  # noqa: BLE001
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
