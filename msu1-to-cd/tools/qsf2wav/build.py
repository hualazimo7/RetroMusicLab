#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py — 编译 `qsf2wav.exe`（QSF / miniqsf → WAV 的原生解码器）。

## 为什么用 zig 当编译器

和 `tools/psf2wav/build.py` 同一个理由：本机没有 gcc / cl / clang / cmake，
但 `ziglang` pip 包里自带一份完整的 C 编译器（`zig cc`），自带 libc，
Windows 上能直接产出 exe。

    C:/Python314/python.exe -m pip install ziglang
    C:/Python314/python.exe tools/qsf2wav/build.py

产物默认写到 `msu1-to-cd/qsf2wav.exe`（包根目录，跟主程序同级）。

## 拼了哪几块

    qsf2wav.c          本目录        命令行驱动：解容器 → 灌 ROM → 渲染 → 写 WAV
    psflib.c           src/psflib    （MIT）PSF 容器：zlib 解压、_lib 链、tags
    qsound.c           src/hq        QSound 顶层：Z80 内存映射 / bank / IRQ / 采样节流
    qsound_ctr.c       src/hq        混音器：16 声道 PCM + ADPCM + 声像 + 回声
    z80.c              src/hq        Z80 核心（源自 MAME，kode54 改造成可独立跑）
    kabuki.c           src/hq        CPS1 的 Kabuki 解密
    zlib/*.c           src/zlib      psflib 只用 uncompress()

⚠ `src/hq/qmix.c` **不参与编译** —— 它是 highly_quixotic 里另一条混音路径，
   `qsound.c` 实际走的是 `qsound_ctr.c`（`device_start_qsound_ctr`）。
   留着只为对照上游，别把它加进 C_SOURCES，会重复定义符号。
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))          # msu1-to-cd/
SRC = os.path.join(HERE, "src")
OUT = os.path.join(PKG, "qsf2wav.exe")

#: 要编进 exe 的源文件
C_SOURCES = [
    os.path.join(HERE, "qsf2wav.c"),
    os.path.join(SRC, "psflib", "psflib.c"),
    os.path.join(SRC, "hq", "qsound.c"),
    os.path.join(SRC, "hq", "qsound_ctr.c"),
    os.path.join(SRC, "hq", "z80.c"),
    os.path.join(SRC, "hq", "kabuki.c"),
    # zlib：psflib 只用 uncompress()，但 inflate 那一串是它的依赖
    os.path.join(SRC, "zlib", "adler32.c"),
    os.path.join(SRC, "zlib", "crc32.c"),
    os.path.join(SRC, "zlib", "inflate.c"),
    os.path.join(SRC, "zlib", "inffast.c"),
    os.path.join(SRC, "zlib", "inftrees.c"),
    os.path.join(SRC, "zlib", "zutil.c"),
    os.path.join(SRC, "zlib", "uncompr.c"),
]

CFLAGS = [
    "-O2",
    # ⚠ 必须显式指定 baseline 指令集。``zig cc`` 不带 -mcpu 时按**编译机自己
    #   的 CPU** 出码（本机是 Zen4，会带 AVX-512），换到老 CPU 上进程一启动就
    #   0xC000001D（STATUS_ILLEGAL_INSTRUCTION）。psf2wav 当初就是栽在这上面，
    #   用户看到的却是「解码器拒绝了这份 BIOS」，极难查。别删这条。
    "-mcpu=baseline",
    "-w",                     # 上游代码有一堆老式符号/类型告警，别刷屏
    "-I", os.path.join(SRC, "psflib"),
    "-I", os.path.join(SRC, "hq"),
    "-I", os.path.join(SRC, "zlib"),
    "-DEMU_COMPILE",          # emuconfig.h 要求：不定义它自己 #error
    "-DEMU_LITTLE_ENDIAN",
    "-DHAVE_STDINT_H",
]


def find_zig():
    """找 zig 可执行文件。优先环境变量，其次 ziglang 包，最后 PATH。"""
    env = os.environ.get("ZIG")
    if env and os.path.isfile(env):
        return env

    cands = []
    for sp in _site_packages():
        cands.append(os.path.join(sp, "ziglang", "zig.exe"))
        cands.append(os.path.join(sp, "ziglang", "zig"))
    for c in cands:
        if os.path.isfile(c):
            return c

    for name in ("zig.exe", "zig"):
        p = shutil.which(name)
        if p:
            return p
    return ""


def _site_packages():
    out = []
    try:
        import site
        for p in list(getattr(site, "getsitepackages", lambda: [])()) + \
                [site.getusersitepackages()]:
            if p and p not in out:
                out.append(p)
    except Exception:                                   # noqa: BLE001
        pass
    for p in sys.path:
        if p and p.endswith("site-packages") and p not in out:
            out.append(p)
    return out


def main():
    zig = find_zig()
    if not zig:
        print("找不到 zig。装一下：\n"
              "    C:/Python314/python.exe -m pip install ziglang\n"
              "或者把 zig 的完整路径放进环境变量 ZIG。", file=sys.stderr)
        return 2

    missing = [p for p in C_SOURCES if not os.path.isfile(p)]
    if missing:
        print("缺源文件：\n  " + "\n  ".join(missing), file=sys.stderr)
        return 1

    cmd = [zig, "cc"] + CFLAGS + ["-o", OUT] + C_SOURCES
    print("编译 %s …" % os.path.basename(OUT))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("编译失败（zig 退出码 %d）" % r.returncode, file=sys.stderr)
        return r.returncode

    size = os.path.getsize(OUT)
    print("OK  %s  %.0f KB" % (OUT, size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
