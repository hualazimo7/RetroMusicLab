"""真 exe 端到端：用打包后的 MSU1Converter-web.exe 实转一首 GBA 的 .MOD。

跑法（工作目录无所谓，路径都是绝对的）：

    C:/Python314/python.exe vendor/_e2e_mod_exe.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXE = os.path.join(ROOT, "dist", "MSU1Converter-web.exe")
BASE = "http://127.0.0.1:47913"

MOD = (r"D:\MSU1Downloads\joshw\Game Boy Advance"
       r"\007 - Nightfire (2003-03-18)(JV)(Electronic Arts)[GBA]"
       r"\09 Mission Complete.MOD")
OUT = r"D:\MSU1Temp\probe8\e2e_exe"

_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}))       # 绕开系统代理


def _get(path, timeout=5):
    try:
        with _opener.open(BASE + path, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception:                          # noqa: BLE001
        return 0, {}


def _post(path, body, timeout=15):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with _opener.open(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def main():
    if not os.path.isfile(EXE):
        print("找不到 exe:", EXE)
        return 2
    if not os.path.isfile(MOD):
        print("找不到样本:", MOD)
        return 2
    code, _ = _get("/api/state", timeout=1)
    if code:
        print("端口 47913 已被占用，先关掉在跑的实例")
        return 2

    proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        ok = False
        for _ in range(120):
            time.sleep(0.5)
            if _get("/api/state", timeout=1)[0] == 200:
                ok = True
                break
            if proc.poll() is not None:
                break
        print("启动:", "OK" if ok else "失败（进程 returncode=%s）" % proc.poll())
        if not ok:
            return 2

        _post("/api/clear", {})
        code, d = _post("/api/add_sources", {"paths": [MOD]})
        print("加音源:", code, d)
        code, st = _get("/api/state")
        srcs = st.get("sources") or []
        print("音源:", [(s.get("name"), s.get("kind"), s.get("n_tracks"),
                         round(float(s.get("total_secs") or 0), 2))
                        for s in srcs])

        os.makedirs(OUT, exist_ok=True)
        for f in os.listdir(OUT):
            os.remove(os.path.join(OUT, f))
        code, d = _post("/api/start", {"opts": {"out_dir": OUT,
                                                "do_cd": True, "do_wav": True}})
        print("启动转换:", code, d)

        for _ in range(240):
            time.sleep(0.5)
            code, st = _get("/api/state")
            if st.get("state") != "running":
                break
        print("终态:", st.get("state"), "结果:", st.get("result"))

        wavs = sorted(f for f in os.listdir(OUT) if f.lower().endswith(".wav"))
        print("产出:", [(f, os.path.getsize(os.path.join(OUT, f))) for f in wavs])
        if wavs:
            import wave
            with wave.open(os.path.join(OUT, wavs[0])) as w:
                print("WAV: %d ch / %d Hz / %d bit / %.2f 秒"
                      % (w.getnchannels(), w.getframerate(),
                         w.getsampwidth() * 8,
                         w.getnframes() / float(w.getframerate())))
        return 0 if wavs else 1
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
