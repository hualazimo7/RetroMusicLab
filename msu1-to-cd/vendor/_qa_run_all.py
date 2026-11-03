# -*- coding: utf-8 -*-
"""QA 独立验收跑批器 (v4.0 下载后自动转 WAV)

用途: 一次性跑完全部测试并把分项结果写进 _qa_run_all.log。
严格串行执行 —— 测试之间会共享 vendor/samples 与 DLL, 并发会互相干扰。
用法: C:/Python314/python.exe vendor/_qa_run_all.py
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
LOG = os.path.join(ROOT, "vendor", "_qa_run_all.log")

# (标签, 参数列表) —— 顺序敏感: 先基线回归, 再新增
CASES = [
    ("基线·test_chip_formats", ["vendor/test_chip_formats.py"]),
    ("基线·test_gme_info", ["vendor/test_gme_info.py"]),
    ("基线·test_chip_batching", ["vendor/test_chip_batching.py"]),
    ("基线·test_unpack", ["vendor/test_unpack.py"]),
    ("基线·test_chip_decode", ["vendor/test_chip_decode.py"]),
    ("基线·test_joshw --offline", ["vendor/test_joshw.py", "--offline"]),
    ("新增·test_dl_convert (结构)", ["vendor/test_dl_convert.py"]),
    ("新增·test_dl_convert --decode", ["vendor/test_dl_convert.py", "--decode"]),
    ("新增·test_dl_convert_worker", ["vendor/test_dl_convert_worker.py"]),
    ("新增·test_dl_convert_e2e (结构)", ["vendor/test_dl_convert_e2e.py"]),
    ("新增·test_dl_convert_e2e --decode", ["vendor/test_dl_convert_e2e.py", "--decode"]),
    ("新增·test_dl_convert_e2e --full --decode", ["vendor/test_dl_convert_e2e.py", "--full", "--decode"]),
]


def main():
    lines = []
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = ROOT

    def emit(s):
        print(s, flush=True)
        lines.append(s)

    emit("=" * 78)
    emit("QA 独立验收跑批  pid=%d  python=%s" % (os.getpid(), PY))
    emit("开始时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("=" * 78)

    summary = []
    for label, args in CASES:
        emit("")
        emit("#" * 78)
        emit("### %s" % label)
        emit("#" * 78)
        t0 = time.time()
        try:
            p = subprocess.run([PY] + args, cwd=ROOT, env=env,
                               capture_output=True, timeout=1800)
            rc = p.returncode
            out = p.stdout.decode("utf-8", "replace")
            err = p.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired as e:
            rc = -999
            out = (e.stdout or b"").decode("utf-8", "replace")
            err = "TIMEOUT after 1800s"
        dt = time.time() - t0
        tail = [x for x in out.splitlines() if x.strip()]
        # 摘出统计行(通常是最后几行)
        stats = [x for x in tail[-12:] if ("通过" in x or "PASS" in x or "FAIL" in x
                                           or "合计" in x or "断言" in x or "OK" in x)]
        for s in (stats or tail[-6:]):
            emit("   " + s)
        if rc != 0:
            for x in tail[-40:]:
                emit("   | " + x)
            if err.strip():
                for x in err.splitlines()[-20:]:
                    emit("   ! " + x)
        emit(">>> RESULT %-46s rc=%-4s %.1fs" % (label, rc, dt))
        summary.append((label, rc, dt))

    emit("")
    emit("=" * 78)
    emit("汇总")
    emit("=" * 78)
    bad = 0
    for label, rc, dt in summary:
        flag = "PASS" if rc == 0 else "FAIL(rc=%s)" % rc
        if rc != 0:
            bad += 1
        emit("  %-6s %-46s %6.1fs" % (flag, label, dt))
    emit("")
    emit("失败项: %d / %d" % (bad, len(summary)))
    emit("结束时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))

    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n[log] %s" % LOG)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
