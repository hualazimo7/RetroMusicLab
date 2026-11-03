#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_tmpdir.py — 临时目录门禁（v4.44）。

守的是什么：**本工程所有临时落盘不许再写到 C 盘系统 %TEMP%**。

背景：v4.43 之前临时 PCM 全走 `%TEMP%`，2026-10-22 实测积了 21 GB
（xa_pcm 9.0G / chip_pcm 5.7G / _MEI 4.3G / exe_disc 1.4G），把用户 C 盘
从 60 GB 可用拖到 27 GB。用户要求统一放 D 盘。

跑法：
    python vendor/test_tmpdir.py

全离线，不碰真实音源，约 1 秒。
"""

import os
import shutil
import sys
import tempfile
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import tmpdir                                     # noqa: E402

_green, _red, _rst = "\033[32m", "\033[31m", "\033[0m"
if not sys.stdout.isatty():
    _green = _red = _rst = ""
_p = _f = 0


def check(name, cond, detail=""):
    global _p, _f
    if cond:
        _p += 1
        print(f"  {_green}PASS{_rst} {name}" + (f"  {detail}" if detail else ""))
    else:
        _f += 1
        print(f"  {_red}FAIL{_rst} {name}  {detail}")
    return bool(cond)


def main():
    print("=" * 62)
    print("① 目录解析")
    r = tmpdir.root()
    check("root() 是个存在的目录", os.path.isdir(r), r)
    if os.name == "nt":
        check("Windows 下默认落在 D:\\MSU1Temp",
              os.path.normcase(r) == os.path.normcase("D:\\MSU1Temp"), r)
        check("没有回落到系统 temp", not tmpdir.is_fallback(),
              "fallback" if tmpdir.is_fallback() else "ok")
    check("目录可写", os.access(r, os.W_OK), r)

    print("\n② init() 接管 tempfile")
    got = tmpdir.init(sweep_old=False)
    check("init() 返回 root()", got == r, got)
    check("tempfile.gettempdir() 已跟着走",
          os.path.normcase(tempfile.gettempdir()) == os.path.normcase(r),
          tempfile.gettempdir())
    d = tempfile.mkdtemp(prefix="xa_pcm_")
    check("mkdtemp 落在新目录下",
          os.path.normcase(os.path.dirname(d)) == os.path.normcase(r), d)
    shutil.rmtree(d, ignore_errors=True)
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".chipbin")
    f.close()
    check("NamedTemporaryFile 也落在新目录下",
          os.path.normcase(os.path.dirname(f.name)) == os.path.normcase(r),
          f.name)
    os.unlink(f.name)

    print("\n③ sweep()：只清旧的前缀项")
    old = os.path.join(r, "xa_pcm_gate_old")
    fresh = os.path.join(r, "xa_pcm_gate_fresh")
    other = os.path.join(r, "someone_else_data")
    for p in (old, fresh, other):
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "x.bin"), "wb") as fh:
            fh.write(b"\0" * 1024)
    # 把 old / other 的 mtime 拨到 25 小时前
    past = time.time() - 25 * 3600
    for p in (old, other):
        os.utime(p, (past, past))
    n, freed = tmpdir.sweep(max_age_h=24.0)
    check("旧的前缀目录被删掉", not os.path.exists(old), f"清理 {n} 项")
    check("新的前缀目录保留（可能是正在用的）", os.path.exists(fresh), fresh)
    check("非本工程前缀的目录一律不碰", os.path.exists(other), other)
    check("sweep 报告的释放量 > 0", freed >= 1024, f"{freed} 字节")
    for p in (fresh, other):
        shutil.rmtree(p, ignore_errors=True)

    print("\n④ 环境变量 MSU1_TEMP 优先（子进程验证，不污染本进程）")
    import subprocess
    alt = os.path.join(tempfile.gettempdir(), "msu1_tmpdir_alt")
    env = dict(os.environ, MSU1_TEMP=alt)
    code = ("import sys, tempfile; sys.path.insert(0, %r); import tmpdir; "
            "print(tmpdir.init(sweep_old=False))" % PROJ)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env, timeout=60)
    check("MSU1_TEMP 生效",
          os.path.normcase(out.stdout.strip()) == os.path.normcase(alt),
          (out.stdout or out.stderr).strip()[:80])
    shutil.rmtree(alt, ignore_errors=True)

    print("\n⑤ describe() 可读")
    line = tmpdir.describe()
    check("describe() 含实际路径", r in line, line)

    print("\n" + "=" * 62)
    print(f"结果: {_p} 通过 / {_f} 失败")
    print("=" * 62)
    return 0 if _f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
