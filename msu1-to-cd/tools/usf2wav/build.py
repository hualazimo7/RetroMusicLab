#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py — 编译 `usf2wav.exe`（USF/miniusf → WAV 的原生解码器）。

和 `tools/psf2wav/build.py` 是同一套路：`zig cc` 当编译器，产物放到包根目录，
跟主程序同级。区别只在源文件清单和宏。

## 三个必须记住的编译要点

1. **不要定义 `DYNAREC`**。lazyusf2 有 x86 / x86_64 两套 JIT，JIT 按**编译机**
   的 CPU 出码 —— 换到不支持的机器上就是 `0xC000001D`。本项目在 `psf2wav` 的
   `-mcpu` 上已经栽过一次（README ⚠51）。这里只编 `r4300/cached_interp.c`
   这条纯 C 的 cached interpreter 路径，不编 `r4300/x86*`。
   实测约 9 倍实时，够用。

2. **`-mcpu=baseline` 不能省**。理由同上，见 `psf2wav/build.py` 里那段长注释。

3. **`-Wno-return-mismatch`**。`rsp_hle/plugin.c` 里有一堆老式函数指针赋值，
   新版 clang（zig 0.16 内置的就是 clang 20）把它从警告升级成了**硬错误**。

## 用法

    C:/Python314/python.exe tools/usf2wav/build.py

产物：`msu1-to-cd/usf2wav.exe`
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))          # msu1-to-cd/
SRC = os.path.join(HERE, "src")
USF = os.path.join(SRC, "lazyusf2")
OUT = os.path.join(PKG, "usf2wav.exe")

#: lazyusf2 需要编进去的源文件。
#:
#: 清单照它的 `Makefile` 里的 `OBJS` 抄，但**去掉了 `$(OBJS_RECOMPILER_x86_64)`**
#: （那是 JIT，见上面第 1 条）。共 50 个 .c。
_USF_SOURCES = [
    # ---- 外设控制器 ----
    "ai/ai_controller.c",
    "api/callbacks.c",
    "debugger/dbg_decoder.c",
    "main/main.c",
    "main/rom.c",
    "main/savestates.c",
    "main/util.c",
    "memory/memory.c",
    "pi/cart_rom.c",
    "pi/pi_controller.c",
    # ---- R4300 CPU（cached interpreter，无 JIT） ----
    "r4300/cached_interp.c",
    "r4300/cp0.c",
    "r4300/cp1.c",
    # ⚠ `empty_dynarec.c` 提供 init_assembler / passe2 / 等 JIT 函数的**空桩**。
    #   不编它就会在链接期缺符号。这就是「不定义 DYNAREC 但又要能链上」的
    #   官方解法（Mupen64Plus 也这么做）。
    "r4300/empty_dynarec.c",
    "r4300/exception.c",
    "r4300/interupt.c",
    "r4300/mi_controller.c",
    "r4300/pure_interp.c",
    "r4300/r4300.c",
    "r4300/r4300_core.c",
    "r4300/recomp.c",
    "r4300/reset.c",
    "r4300/tlb.c",
    # ---- RDP / RDRAM ----
    "rdp/rdp_core.c",
    "ri/rdram.c",
    "ri/rdram_detection_hack.c",
    "ri/ri_controller.c",
    # ---- RSP ----
    "rsp/rsp_core.c",
    # ---- RSP HLE：音频微码（alist / musyx / mp3 / hvqm …） ----
    "rsp_hle/alist.c",
    "rsp_hle/alist_audio.c",
    "rsp_hle/alist_naudio.c",
    "rsp_hle/alist_nead.c",
    "rsp_hle/audio.c",
    "rsp_hle/cicx105.c",
    "rsp_hle/hle.c",
    "rsp_hle/hvqm.c",
    "rsp_hle/jpeg.c",
    "rsp_hle/memory.c",
    "rsp_hle/mp3.c",
    "rsp_hle/musyx.c",
    "rsp_hle/plugin.c",
    "rsp_hle/re2.c",
    # ---- RSP LLE（个别游戏要，留着） ----
    "rsp_lle/rsp.c",
    # ---- SI / PIF / CIC ----
    "si/cic.c",
    "si/game_controller.c",
    "si/n64_cic_nus_6105.c",
    "si/pif.c",
    "si/si_controller.c",
    # ---- USF 本体：容器回调 + 重采样 ----
    "usf/usf.c",
    "usf/barray.c",
    "usf/resampler.c",
    "vi/vi_controller.c",
]

C_SOURCES = (
    [os.path.join(HERE, "usf2wav.c"),
     os.path.join(SRC, "psflib", "psflib.c")]
    + [os.path.join(USF, p.replace("/", os.sep)) for p in _USF_SOURCES]
    + [
        # zlib：psflib 只用 uncompress()，但 inflate 那一串是它的依赖
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
    # ⚠ 不能省，见文件头第 2 条
    "-mcpu=baseline",
    # ⚠ rsp_hle/plugin.c 在老式函数指针赋值上会硬报错，见文件头第 3 条
    "-Wno-return-mismatch",
    "-w",
    "-include", "string.h",   # 有几个文件用了 strcmp/memcpy 却没 include
    "-I", USF,
    "-I", os.path.join(SRC, "psflib"),
    "-I", os.path.join(SRC, "zlib"),
    # rsp_lle 的 SIMD 分支开关（Makefile 里只给 rsp.c 加，全局加无副作用）
    "-DARCH_MIN_SSE2",
    # ⚠ 故意**不**定义 DYNAREC
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
    # `--ref` 编「黄金参照」工具（上游 dumpresampled.c 的原样副本，输出裸 PCM）。
    # 只在开发机用，不进发布包 —— 见 ref/refdump.c 的说明。
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
    print("编译 %s …（%d 个 .c）"
          % (os.path.basename(out), len(sources)))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("编译失败（zig 退出码 %d）" % r.returncode, file=sys.stderr)
        return r.returncode

    size = os.path.getsize(out)
    print("OK  %s  %.0f KB" % (out, size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
