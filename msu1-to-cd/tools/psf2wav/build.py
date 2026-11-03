#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py — 编译 `psf2wav.exe`（PSF/minipsf → WAV 的原生解码器）。

## 为什么用 zig 当编译器

本机没有 gcc / cl / clang / cmake。但 Nuitka 的私有 pip 空间里带了
**zig 0.16**（`ziglang` 包），`zig cc` 就是一个自带 libc 的完整 C 编译器，
Windows 上可以直接产出 exe，不需要装 Visual Studio。

    python -m pip show ziglang        # 位置
    <site-packages>/ziglang/zig.exe cc --version

## 用法

    C:/Python314/python.exe tools/psf2wav/build.py
    C:/Python314/python.exe tools/psf2wav/build.py --ref   # 编参照工具

产物默认写到 `msu1-to-cd/psf2wav.exe`（也就是包根目录，跟主程序同级）；
`--ref` 时产物是 `tools/psf2wav/ref/refdump.exe`（只留在开发机，不进发布包）。
找不到 zig 时会给出明确的安装提示，而不是抛一堆编译错误。
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))          # msu1-to-cd/
SRC = os.path.join(HERE, "src")
OUT = os.path.join(PKG, "psf2wav.exe")

#: 要编进 exe 的源文件（顺序无所谓）
C_SOURCES = [
    os.path.join(HERE, "psf2wav.c"),
    os.path.join(SRC, "psflib", "psflib.c"),
    # PSF2 的虚拟文件系统：从 reserved section 解出文件树，供 VFS 读文件
    os.path.join(SRC, "psflib", "psf2fs.c"),
    # HE 的 PSXCore：R3000A + IOP + 定时器 + SPU + VFS + mkhebios
    os.path.join(SRC, "he", "psx.c"),
    os.path.join(SRC, "he", "iop.c"),
    os.path.join(SRC, "he", "ioptimer.c"),
    os.path.join(SRC, "he", "bios.c"),
    os.path.join(SRC, "he", "r3000.c"),
    os.path.join(SRC, "he", "r3000asm.c"),
    os.path.join(SRC, "he", "r3000dis.c"),
    os.path.join(SRC, "he", "vfs.c"),
    os.path.join(SRC, "he", "spu.c"),
    os.path.join(SRC, "he", "spucore.c"),
    os.path.join(SRC, "he", "mkhebios.c"),
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
    # ⚠⚠ **必须显式指定 baseline 指令集**（v5.2.2 起）。
    #
    # ``zig cc`` 不带 -mcpu 时**默认按编译机自己的 CPU** 出码。实测对照
    # （同一份 C 源码、同一个 -O2）：
    #
    #     不加参数          191488 字节
    #     -mcpu=native      191488 字节   ← 和「不加参数」一模一样
    #     -mcpu=baseline    188416 字节   ← 真的换了目标
    #     -mcpu=x86_64      188416 字节
    #     -march=x86_64     188416 字节
    #
    # 本机是 Ryzen 9 8940HX（Zen4，带 AVX-512），于是产出里带着 AVX-512。
    # 换到不支持的老 CPU 上，进程一启动就：
    #
    #     退出码 3221225501 = 0xC000001D = STATUS_ILLEGAL_INSTRUCTION
    #
    # 用户实测：主程序里点「指定 BIOS」→ 「解码器拒绝了这份 BIOS
    # （退出码 3221225501）」。看起来像 BIOS 坏了，其实是**解码器自己被
    # CPU 拒绝了**。这种误报极难查，所以这条参数不能省。
    #
    # 用 ``-mcpu=baseline`` 而不是 ``-march=x86_64``：前者是 zig 的原生写法、
    # 语义就是「架构最低要求」（x86_64 = SSE2），不会因为 zig 改别名而失效。
    "-mcpu=baseline",
    "-w",                     # HE 的代码有一堆老的符号/类型告警，别刷屏
    "-include", "ctype.h",    # r3000asm.c 用了 tolower 却没 include
    "-I", os.path.join(SRC, "psflib"),
    "-I", os.path.join(SRC, "he"),
    "-I", os.path.join(SRC, "zlib"),
    "-DEMU_COMPILE",          # HE 要求：不定义这个它自己 #error
    "-DEMU_LITTLE_ENDIAN",
    "-DHAVE_STDINT_H",
]


def find_zig():
    """找 zig 可执行文件。优先环境变量，其次 ziglang 包，最后 PATH。"""
    env = os.environ.get("ZIG")
    if env and os.path.isfile(env):
        return env

    # ziglang 是 pip 包，装在哪取决于用哪个解释器
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
    """列出所有可能的 site-packages 目录（含用户级）。"""
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
    # `--ref` 编「黄金参照」工具（plugin.cc 的 PSF/PSF2 路径原样副本）。
    # 只在开发机用，不进发布包。
    want_ref = "--ref" in sys.argv[1:]

    zig = find_zig()
    if not zig:
        print("找不到 zig。装一下：\n"
              "    C:/Python314/python.exe -m pip install ziglang\n"
              "或者把 zig 的完整路径放进环境变量 ZIG。", file=sys.stderr)
        return 2

    if want_ref:
        out = os.path.join(HERE, "ref", "refdump.exe")
        main_c = os.path.join(HERE, "ref", "refdump.c")
    else:
        out = OUT
        main_c = os.path.join(HERE, "psf2wav.c")

    missing = [p for p in C_SOURCES if not os.path.isfile(p)]
    if missing:
        print("缺源文件：\n  " + "\n  ".join(missing), file=sys.stderr)
        return 1

    cmd = [zig, "cc"] + CFLAGS + ["-o", out] + [main_c] + C_SOURCES[1:]
    print("编译 %s …" % os.path.basename(out))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("编译失败（zig 退出码 %d）" % r.returncode, file=sys.stderr)
        return r.returncode

    size = os.path.getsize(out)
    print("OK  %s  %.0f KB" % (out, size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
