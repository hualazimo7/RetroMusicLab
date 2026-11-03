"""真 exe 端到端：「转不了」的说明到底有没有送到界面上（v5.2.6）。

跑法（工作目录无所谓，路径都是绝对的）：

    C:/Python314/python.exe vendor/_e2e_skip_reason_exe.py

验四件事：
1. 一个**全是** `.mini2sf` 的 NDS 目录（Contra 4）→ 0 个音源，且跳过原因里
   带「NDS 内存快照」这类**具体**说法（不是「不认识的文件类型」）
2. 一个**有** `.hwas`/`.swav` 的 NDS 目录（007 量子危机）→ **33 个音源**
   —— v5.2.7 接通后这三个格式不再被挡在外面
3. 一个**有** `.mod` 的 GBA 目录 → 10 个音源（说明只是提示变了，没把能转的挡掉）
4. `/api/state?since=0` 的日志里真的出现了那行「⚠ 跳过 … —— …」
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

NDS = (r"D:\MSU1Downloads\joshw\Nintendo DS"
       r"\Contra 4 [Contra - Dual Spirits] (2007-11-13)(WayForward)(Konami)[NDS]")
NDS_HWAS = (r"D:\MSU1Downloads\joshw\Nintendo DS"
            r"\007 - Quantum of Solace (2008-10-31)(Vicarious Visions)"
            r"(Activision)[NDS]")
GBA = (r"D:\MSU1Downloads\joshw\Game Boy Advance"
       r"\007 - Nightfire (2003-03-18)(JV)(Electronic Arts)[GBA]")

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


def main():
    if not os.path.isfile(EXE):
        print("找不到 exe:", EXE)
        return 2
    for d in (NDS, NDS_HWAS, GBA):
        if not os.path.isdir(d):
            print("找不到样本目录:", d)
            return 2
    if _get("/api/state", timeout=1)[0]:
        print("端口 47913 已被占用，先关掉在跑的实例")
        return 2

    proc = subprocess.Popen([EXE], cwd=os.path.dirname(EXE),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(120):
            time.sleep(0.5)
            if _get("/api/state", timeout=1)[0] == 200:
                up = True
                break
            if proc.poll() is not None:
                break
        print("启动:", "OK" if up else "失败（returncode=%s）" % proc.poll())
        if not up:
            return 2

        # ---- 1. NDS：一个能转的都没有 ----
        _post("/api/clear", {})
        code, d = _post("/api/add_sources", {"paths": [NDS]})
        why = " | ".join(s.get("why", "") for s in (d.get("skipped") or []))
        print("\n[1] 加 NDS 目录：added=%s" % d.get("added"))
        print("    原因：%s" % why[:160])
        ok(code == 200 and d.get("added") == 0, "NDS 目录 0 个音源", d.get("added"))
        ok("NDS" in why, "原因里点名 NDS 机种", why[:80])
        ok("内存快照" in why,
           "原因说的是「NDS 内存快照」，不是「不认识的文件类型」", why[:80])

        # ---- 1b. NDS（007）：.hwas/.swav 已经能转，33 个全收 ----
        _post("/api/clear", {})
        code, d = _post("/api/add_sources", {"paths": [NDS_HWAS]})
        print("\n[1b] 加 NDS(007) 目录：added=%s" % d.get("added"))
        ok(code == 200 and d.get("added") == 33,
           "NDS(007) 目录 33 个音源（29 .hwas + 4 .swav）", d.get("added"))

        # ---- 2. GBA：10 个 .mod 照旧能加 ----
        _post("/api/clear", {})
        code, d = _post("/api/add_sources", {"paths": [GBA]})
        print("\n[2] 加 GBA 目录：added=%s" % d.get("added"))
        ok(code == 200 and d.get("added") == 10, "GBA 目录仍是 10 个音源",
           d.get("added"))

        # ---- 3. 日志里真的有一行「跳过」 ----
        # ⚠ 日志不是单独的 /api/logs，而是跟着 /api/state?since=N 增量下发的。
        _post("/api/clear", {})
        _post("/api/add_sources", {"paths": [NDS]})
        code, lg = _get("/api/state?since=0")
        text = "\n".join(row[1] for row in (lg.get("logs") or []))
        hit = [ln for ln in text.splitlines() if "跳过" in ln]
        print("\n[3] 日志里的跳过行：")
        for ln in hit[:2]:
            print("    " + ln[:150])
        ok(bool(hit), "日志里出现了「⚠ 跳过 … —— 原因」")
        ok(any("——" in ln for ln in hit),
           "长原因用破折号接，不套成「（…（…）…）」", hit[:1])
        ok(any("内存快照" in ln for ln in hit),
           "日志里的原因也是具体的", hit[:1])
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
