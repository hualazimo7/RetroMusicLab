#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py — 编译 `vio2wav.exe`（2SF/mini2sf → WAV 的原生解码器）。

和 `tools/usf2wav/build.py` 是同一套路：`zig cc` 当编译器，产物放到包根目录。

## 这个核和前两个不一样的地方

1. **`SPU.cpp` 是 C++**。vio2sf 的 SPU 核心用了 `<string>` 和 `bool`，
   所以这一个文件必须用 `zig c++` 编。其余全是 C，用 `zig cc`。
   混编的做法是：C 文件全部 `-c` 成 .o，`SPU.cpp` 也 `-c` 成 .o，
   最后用 `zig c++` 统一链接（链接期要 C++ 运行库）。
   ⚠ 不能图省事用 `zig c++` 一次编完 —— clang++ 会把 `.c` 当 C++ 编，
   DeSmuME 那堆 C 代码立刻炸（K&R 风格声明、隐式 int 转换到处都是）。

2. **`-mcpu=baseline` 不能省**。理由和 `usf2wav` / `psf2wav` 一样：
   默认会按编译机 CPU 出码，换到老一点的机器就是 `0xC000001D`（README ⚠51）。

3. **不要开 `-ffast-math`**。SPU 的定点/浮点混算对舍入敏感，
   开了之后逐样本比对就对不上参照了。

## 用法

    C:/Python314/python.exe tools/vio2wav/build.py

产物：`msu1-to-cd/vio2wav.exe`
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))          # msu1-to-cd/
SRC = os.path.join(HERE, "src")
DSM = os.path.join(SRC, "vio2sf", "desmume")
OUT = os.path.join(PKG, "vio2wav.exe")
BUILD = os.path.join(HERE, "obj")

#: DeSmuME 最小核的源文件清单。
#:
#: 照 kode54 `audacious-xsf` 的 `Makefile` 里 `VIO2SF_OBJS` 抄，16 个。
#: 一个不多一个不少 —— 少一个就是链接期缺符号，多一个会拖进 GPU 渲染之类
#: 根本用不到的模块（`GPU.c` 虽然也在清单里，但只用到它的内存映射桩）。
_C_SOURCES = [
    "FIFO.c",
    "GPU.c",
    "MMU.c",
    "NDSSystem.c",
    "arm_instructions.c",
    "armcpu.c",
    "barray.c",
    "bios.c",
    "cp15.c",
    "isqrt.c",
    "matrix.c",
    "mc.c",
    "resampler.c",
    "state.c",
    "thumb_instructions.c",
]

#: 唯一的 C++ 文件，见文件头第 1 条。
_CXX_SOURCES = [
    "SPU.cpp",
]

CFLAGS = [
    "-O2",
    # ⚠ 不能省，见文件头第 2 条
    "-mcpu=baseline",
    # ⚠ 不能加 -ffast-math，见文件头第 3 条
    "-w",
    "-include", "string.h",   # 有几个文件用了 memcpy/strcmp 却没 include
    "-I", DSM,
    "-I", os.path.join(SRC, "psflib"),
    "-I", os.path.join(SRC, "zlib"),
    # DeSmuME 的类型头靠这个开关选 32 位定长类型
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


def _compile(zig, compiler, src, obj, extra=None):
    cmd = [zig, compiler] + CFLAGS + (extra or []) + ["-c", src, "-o", obj]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("编译失败：%s" % os.path.basename(src), file=sys.stderr)
        return r.returncode
    return 0


def main():
    # `--ref` 编「黄金参照」工具（上游 vio2sf 自带 harness 的原样副本）。
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
        main_c = os.path.join(HERE, "vio2wav.c")

    # ---- 收集源文件 ----
    c_files = [main_c,
               os.path.join(SRC, "psflib", "psflib.c")]
    c_files += [os.path.join(DSM, p) for p in _C_SOURCES]
    c_files += [
        os.path.join(SRC, "zlib", "adler32.c"),
        os.path.join(SRC, "zlib", "crc32.c"),
        os.path.join(SRC, "zlib", "inflate.c"),
        os.path.join(SRC, "zlib", "inffast.c"),
        os.path.join(SRC, "zlib", "inftrees.c"),
        os.path.join(SRC, "zlib", "zutil.c"),
        os.path.join(SRC, "zlib", "uncompr.c"),
    ]
    cxx_files = [os.path.join(DSM, p) for p in _CXX_SOURCES]

    missing = [p for p in c_files + cxx_files if not os.path.isfile(p)]
    if missing:
        print("缺源文件（%d 个）：\n  " % len(missing)
              + "\n  ".join(missing), file=sys.stderr)
        return 1

    os.makedirs(BUILD, exist_ok=True)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    # ---- 逐个编成 .o（C 用 zig cc，C++ 用 zig c++） ----
    print("编译 vio2wav …（%d 个 .c + %d 个 .cpp）"
          % (len(c_files), len(cxx_files)))

    objs = []
    for idx, src in enumerate(c_files):
        obj = os.path.join(BUILD, "c%03d_%s.o"
                           % (idx, os.path.splitext(os.path.basename(src))[0]))
        rc = _compile(zig, "cc", src, obj)
        if rc:
            return rc
        objs.append(obj)

    for idx, src in enumerate(cxx_files):
        obj = os.path.join(BUILD, "x%03d_%s.o"
                           % (idx, os.path.splitext(os.path.basename(src))[0]))
        rc = _compile(zig, "c++", src, obj)
        if rc:
            return rc
        objs.append(obj)

    # ---- 链接：必须用 zig c++，链接期要 C++ 运行库 ----
    cmd = [zig, "c++", "-mcpu=baseline", "-o", out] + objs
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("链接失败（zig 退出码 %d）" % r.returncode, file=sys.stderr)
        return r.returncode

    size = os.path.getsize(out)
    print("OK  %s  %.0f KB" % (out, size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
