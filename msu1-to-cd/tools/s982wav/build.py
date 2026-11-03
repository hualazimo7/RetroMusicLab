#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py — 编译 `s982wav.exe`（S98 / PC-98 YM2608 → WAV 的原生解码器）。

和 `tools/vio2wav/build.py`、`tools/kss2wav/build.py` 是同一套路：
`zig cc` / `zig c++` 当编译器，产物放到包根目录。

## 这个核的特殊之处

1. **是 C++，不是 C**。ymfm 用了模板、`std::vector`、`namespace`，
   必须 `zig c++` 编。所有源文件都是 `.cpp`，没有 C/C++ 混编的问题。

2. **只需要 OPN 家族，不要 OPL / PCM**。ymfm 一共 6 个 .cpp，这里只编 4 个：

   | 文件 | 要吗 | 为什么 |
   |---|---|---|
   | `ymfm_opn.cpp` | ✅ | YM2608 本体（FM 6 通道 + ADPCM-A/B + SSG 桥接）|
   | `ymfm_adpcm.cpp` | ✅ | ADPCM-A（节奏）/ ADPCM-B |
   | `ymfm_ssg.cpp` | ✅ | YM2608 内建的 AY-3-8910 兼容 SSG |
   | `ymfm_misc.cpp` | ✅ | 时间常数/噪声/包络共用小工具 |
   | `ymfm_opl.cpp` | ❌ | YM3526/YM3812/YMF262，PC-98 用不到 |
   | `ymfm_pcm.cpp` | ❌ | YMF278B 的 PCM 段，用不到 |

   `ymfm_fm.ipp` 是模板实现，被 `ymfm_fm.h` include，**不需要单独编**。

3. **`-mcpu=baseline` 不能省**。理由同 `usf2wav` / `vio2wav`：默认按编译机
   CPU 出码，换到老机器就是 `0xC000001D`（README ⚠51）。

4. **`-O2` 够用，别上 `-O3 -ffast-math`**。FM 的浮点混音对舍入敏感，
   `-ffast-math` 会让逐样本比对对不上；而且 `-O3` 对这个核的收益很小
   （热点在 FIR 的内层循环，`-O2` 已经会向量化）。

## 用法

    C:/Python314/python.exe tools/s982wav/build.py

产物：`msu1-to-cd/s982wav.exe`
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))          # msu1-to-cd/
YM = os.path.join(HERE, "src", "ymfm")
OUT = os.path.join(PKG, "s982wav.exe")
BUILD = os.path.join(HERE, "obj")

#: ymfm 里本核真正需要的四个 .cpp。少一个就是链接期缺符号，
#: 多编一个会白拖 3 万行（opl/pcm 加起来 87 KB 源码）。
_CXX_SOURCES = [
    "ymfm_opn.cpp",
    "ymfm_adpcm.cpp",
    "ymfm_ssg.cpp",
    "ymfm_misc.cpp",
]

CFLAGS = [
    "-O2",
    # ⚠ 不能省，见文件头第 3 条
    "-mcpu=baseline",
    "-w",
    "-std=c++17",
    "-I", YM,
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


def _compile(zig, src, obj):
    cmd = [zig, "c++"] + CFLAGS + ["-c", src, "-o", obj]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("编译失败：%s" % os.path.basename(src), file=sys.stderr)
        return r.returncode
    return 0


def main():
    zig = find_zig()
    if not zig:
        print("找不到 zig。装一下：\n"
              "    C:/Python314/python.exe -m pip install ziglang\n"
              "或者把 zig 的完整路径放进环境变量 ZIG。", file=sys.stderr)
        return 2

    sources = [os.path.join(HERE, "s982wav.cpp")]
    sources += [os.path.join(YM, p) for p in _CXX_SOURCES]

    missing = [p for p in sources if not os.path.isfile(p)]
    if missing:
        print("缺源文件（%d 个）：\n  " % len(missing)
              + "\n  ".join(missing), file=sys.stderr)
        return 1

    os.makedirs(BUILD, exist_ok=True)

    print("编译 s982wav …（%d 个 .cpp）" % len(sources))
    objs = []
    for idx, src in enumerate(sources):
        obj = os.path.join(BUILD, "x%03d_%s.o"
                           % (idx, os.path.splitext(os.path.basename(src))[0]))
        rc = _compile(zig, src, obj)
        if rc:
            return rc
        objs.append(obj)

    # 链接也用 zig c++：链接期要 C++ 运行库
    cmd = [zig, "c++", "-mcpu=baseline", "-o", OUT] + objs
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("链接失败（zig 退出码 %d）" % r.returncode, file=sys.stderr)
        return r.returncode

    size = os.path.getsize(OUT)
    print("OK  %s  %.0f KB" % (OUT, size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
