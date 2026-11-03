#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py — 编译 `ssf2wav.exe`（SSF/minissf + DSF/minidsf → WAV 的原生解码器）。

和 `tools/usf2wav/build.py` 是同一套路：`zig cc` 当编译器，产物放到包根目录。

## 一个 exe 管两种机器

`sega_get_state_size(version)` 的 version：1 = Saturn、2 = Dreamcast，
正好对应容器版本 0x11 / 0x12。所以 `.ssf` 和 `.dsf` 共用这个 exe。

## 编译要点

1. **`-mcpu=baseline` 不能省**。理由和 `usf2wav` / `psf2wav` 一样：
   默认按编译机 CPU 出码，换到老机器就是 `0xC000001D`（README ⚠51）。

2. **五个宏一个都不能少**，全部照 kode54 `audacious-xsf` 的 `Makefile`
   （第 79 行）抄：
     `-DEMU_COMPILE -DEMU_LITTLE_ENDIAN -DHAVE_STDINT_H -DUSE_M68K -DLSB_FIRST`
   * 缺 `EMU_COMPILE` → `EMU_CALL` 展开错，调用约定对不上
   * 缺 `EMU_LITTLE_ENDIAN` → 声音数据按大端解释，出来是噪音
   * 缺 `HAVE_STDINT_H` → `emuconfig.h` 自己 typedef 一套 `sint32/uint32`，
     和系统的 `stdint.h` 冲突
   * 缺 `USE_M68K` → Saturn 的 SH-2 驱动要一个 M68K 协处理器，
     核里两套实现（`m68k/` 和 `Starscream/`），不定义就两边都不编
   * 缺 `LSB_FIRST` → `m68k_in.c` 的字节序处理走反

3. **只编 `m68k/`，不编 `Starscream/`**。Starscream 是预编译的 x86 汇编
   （`s68000.asm` + `star.exe` 生成），只能配 MSVC 的 masm，
   zig 编不了；而且 `USE_M68K` 已经指定走纯 C 的 `m68k/` 那套。
   实测 `m68k/` 完全够用（Angelique Special 32 首全出声）。

## 用法

    C:/Python314/python.exe tools/ssf2wav/build.py

产物：`msu1-to-cd/ssf2wav.exe`
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))          # msu1-to-cd/
SRC = os.path.join(HERE, "src")
SEGA = os.path.join(SRC, "sega")
OUT = os.path.join(PKG, "ssf2wav.exe")

#: Highly Theoretical 核的源文件清单。
#:
#: 照 kode54 `audacious-xsf` 的 `Makefile` 里 `HT_OBJS` 抄，正好 7 个：
#:   arm.c      Dreamcast 的 ARM7 驱动 CPU
#:   dcsound.c  AICA 声音芯片（Dreamcast）
#:   satsound.c SCSP 声音芯片（Saturn）
#:   sega.c     顶层调度 + 两种机器共用的声音 RAM
#:   yam.c      雅马哈声音芯片核心（两种机器都用到）
#:   m68k/m68kcpu.c + m68k/m68kops.c   Saturn 驱动用的 M68K 协处理器
_SEGA_SOURCES = [
    "arm.c",
    "dcsound.c",
    "satsound.c",
    "sega.c",
    "yam.c",
    os.path.join("m68k", "m68kcpu.c"),
    os.path.join("m68k", "m68kops.c"),
]

C_SOURCES = (
    [os.path.join(HERE, "ssf2wav.c"),
     os.path.join(SRC, "psflib", "psflib.c")]
    + [os.path.join(SEGA, p) for p in _SEGA_SOURCES]
    + [
        os.path.join(SRC, "zlib", "adler32.c"),
        os.path.join(SRC, "zlib", "crc32.c"),
        os.path.join(SRC, "zlib", "inflate.c"),
        os.path.join(SRC, "zlib", "inffast.c"),
        os.path.join(SRC, "zlib", "inftrees.c"),
        os.path.join(SRC, "zlib", "zutil.c"),
        os.path.join(SRC, "zlib", "uncompr.c"),
    ]
)

CFLAGS = [
    "-O2",
    # ⚠ 不能省，见文件头第 1 条
    "-mcpu=baseline",
    "-w",
    "-include", "string.h",
    # ⚠ 这五个宏一个都不能少，见文件头第 2 条
    "-DEMU_COMPILE",
    "-DEMU_LITTLE_ENDIAN",
    "-DHAVE_STDINT_H",
    "-DUSE_M68K",
    "-DLSB_FIRST",
    "-I", SEGA,
    "-I", os.path.join(SRC, "psflib"),
    "-I", os.path.join(SRC, "zlib"),
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


def main():
    # `--ref` 编「黄金参照」工具（上游 plugin.cc 的 SSF/DSF 原始实现）。
    want_ref = "--ref" in sys.argv[1:]

    zig = find_zig()
    if not zig:
        print("找不到 zig。装一下：\n"
              "    C:/Python314/python.exe -m pip install ziglang\n"
              "或者把 zig 的完整路径放进环境变量 ZIG。", file=sys.stderr)
        return 2

    if want_ref:
        sources = [os.path.join(HERE, "ref", "refdump.c")] + C_SOURCES[1:]
        out = os.path.join(HERE, "ref", "refdump.exe")
        os.makedirs(os.path.dirname(out), exist_ok=True)
    else:
        sources = C_SOURCES
        out = OUT

    missing = [p for p in sources if not os.path.isfile(p)]
    if missing:
        print("缺源文件（%d 个）：\n  " % len(missing)
              + "\n  ".join(missing), file=sys.stderr)
        return 1

    cmd = [zig, "cc"] + CFLAGS + ["-o", out] + sources
    print("编译 %s …（%d 个 .c）" % (os.path.basename(out), len(sources)))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("编译失败（zig 退出码 %d）" % r.returncode, file=sys.stderr)
        return r.returncode

    size = os.path.getsize(out)
    print("OK  %s  %.0f KB" % (out, size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
