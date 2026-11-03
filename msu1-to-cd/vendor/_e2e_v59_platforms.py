"""_e2e_v59_platforms.py -- 成品 exe 里验 v5.9 的新机种真能扫出条目。

为什么非用 exe 不可：新机种靠 `PLATFORMS` 里的 `path` 字段指到容器站子目录
（`kss/Game Gear/`、`hoot/qsf/`）。这个字段拼错时**静态门禁全绿** ——
机种照样列出来、`platforms == 33` 照样成立，只有真去扫才会 404 或 0 条。
所以这里起真 exe，对每个新机种发一次 `/api/dl/load` 并等扫描结束。

用法：
    py -3.14 vendor\\_e2e_v59_platforms.py
    py -3.14 vendor\\_e2e_v59_platforms.py --exe dist\\MSU1Converter-web.exe
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 47913
BASE = "http://127.0.0.1:%d" % PORT

# 机种 -> 至少要有多少条（宁可放宽，也不要写死精确值 —— 站上条目会增删）
TARGETS = [
    ("kss_gg", "Game Gear", 100),
    ("kss_sms", "Master System", 50),
    ("switch", "Nintendo Switch", 50),
    ("hoot_arcade", "街机 VGM", 100),
    ("hoot_qsf", "QSound", 10),
]

OK = 0
BAD = 0


def mark(ok, what, detail=""):
    global OK, BAD
    if ok:
        OK += 1
        print("  [OK] %s%s" % (what, ("  " + str(detail)) if detail != "" else ""), flush=True)
    else:
        BAD += 1
        print("  [!!] %s  %s" % (what, detail), flush=True)


def jget(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def jpost(path, body, timeout=30):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def wait_port(deadline=90):
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            jget("/api/whoami", timeout=3)
            return True
        except Exception:                            # noqa: BLE001
            time.sleep(0.6)
    return False


def wait_scan(deadline=180):
    t0 = time.time()
    while time.time() - t0 < deadline:
        try:
            _c, d = jget("/api/dl/state", timeout=10)
        except Exception:                            # noqa: BLE001
            time.sleep(1.0)
            continue
        if not d.get("scanning"):
            return d
        time.sleep(1.0)
    return None


def main():
    exe = None
    for i, a in enumerate(sys.argv):
        if a == "--exe" and i + 1 < len(sys.argv):
            exe = sys.argv[i + 1]
    if not exe:
        for cand in (os.path.join(ROOT, "build", "MSU1Converter-web-pyi",
                                  "MSU1Converter-web.exe"),
                     os.path.join(ROOT, "dist", "MSU1Converter-web.exe")):
            if os.path.isfile(cand):
                exe = cand
                break
    if not exe or not os.path.isfile(exe):
        print("找不到 exe")
        return 2
    print("exe: %s (%d B)" % (exe, os.path.getsize(exe)))

    proc = subprocess.Popen([exe], cwd=os.path.dirname(exe) or ".",
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port():
            print("端口没起来（可能已有实例在跑）")
            return 2
        print("服务已起\n")

        code, d = jget("/api/dl/platforms")
        groups = d.get("groups") or []
        n = sum(len(g.get("items") or []) for g in groups)
        mark(code == 200 and n == 33, "exe 里 33 个机种 / %d 组" % len(groups), n)

        print("\n逐个机种真扫（每个都要联网，慢一点）")
        for key, name, need in TARGETS:
            code, d = jpost("/api/dl/load", {"key": key})
            if code != 200 or not d.get("ok"):
                mark(False, "%s 能发起扫描" % name, d.get("error"))
                continue
            st = wait_scan()
            if st is None:
                mark(False, "%s 扫描在 180 秒内结束" % name, "超时")
                continue
            cnt = int(st.get("count") or 0)
            rows = st.get("rows") or []
            first = rows[0].get("name") if rows else "—"
            mark(cnt >= need, "%s 扫到 >= %d 条" % (name, need), "%d 条，首条 %s" % (cnt, first))

        print("\n" + "=" * 46)
        print("v5.9 机种 e2e: %d 通过 / %d 失败" % (OK, BAD))
        print("=" * 46)
        return 0 if BAD == 0 else 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:                            # noqa: BLE001
            try:
                proc.kill()
            except Exception:                        # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
