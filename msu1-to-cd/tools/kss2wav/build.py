#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py — 编译 kss2wav.exe（libkss 后端，KSS / MSX + SMS/GG）。

## 它是干什么的

`.kss` 原本走 libgme。但 **libgme 的 KSS 实现是残缺的**：只映射
0x0000-0x3FFF 与 0x8000-0xBFFF，**不映射 0x4000-0x7FFF**。于是 ROM 落在
0x4000-0x7FFF 的 KSS（典型是 32 KB 的 Game Gear rip，load 到 0x3E10）
会丢掉 16 KB 代码，Z80 跑飞 —— 每一轨都精确静音（peak == 0），
而 libgme **不报任何错**，界面上只看到「解码零产出」。

实测（Zophar 的 Game Gear 栏抽 12 个包）：SGC 6/6 正常；KSS 里 16 KB 布局的
正常，32 KB / 8 KB 布局的静音，约占三成。同一份 `G-3324.kss` 在 joshw 的
Game Gear 目录里一模一样 —— 不是素材坏了，是解码器不行。

libkss（digital-sound-antiques）是完整实现：MSX（AY-3-8910 / OPLL / SCC）
与 SMS/GG（SN76489）都按规范做内存映射和 bank 切换。同一个文件它 peak 4017。

## 依赖

`libkss` 有 6 个 git submodule（kmz80 / emu76489 / emu2149 / emu2212 /
emu2413 / emu8950）。本仓库把**已展开的完整源码**放在 `src/libkss/`，
所以换台机器直接能编，不需要再 clone：

    py -3.14 tools/kss2wav/build.py

## 编译要点（踩过的坑）

1. **`-DEXCLUDE_DRIVER_ALL`** —— 不定义的话 `src/kss/kss.c` 会引用
   `KSS_isMGSdata` 等函数，它们来自 `drivers` submodule（另一个仓库
   `kss-drivers`，只为「把 .mgs/.opx 转成 .kss」服务，播放用不到）。
   定义后这些文件整体被 `#if !defined(EXCLUDE_DRIVER_ALL)` 关掉，
   但 `kss.c` 仍需要那几个符号 —— 所以**这几个 .c 还是要编进去**，
   只是内容变成空壳。
2. **必须排除 `modules/emu2413/sample2413.c` 和 `modules/kmz80/makeft.c`**
   —— 它们各自带 `int main()`，链接期报 duplicate symbol: main。
3. **必须排除 `modules/kmz80/test/`** —— 测试程序，缺 `kmz80i.h`。
4. **`-mcpu=baseline`**（老 CPU 不能吃 AVX，见 README ⚠51）。
   编译和链接两处都要带。
5. 头文件搜索路径：`-I src/libkss/src -I src/libkss/modules`。
   `kssplay.h` 里写的是 `#include "kss/kss.h"` / `"vm/vm.h"` 这种相对形式。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))
LIB = os.path.join(HERE, "src", "libkss")
OUT = os.path.join(PKG, "kss2wav.exe")

#: libkss 里要参与编译的 .c。规则：src 下全部（含 kss.c 引用的那几个空壳），
#: modules 下全部，但排掉自带 main 的示例和 test 目录。
EXCLUDE = {
    "modules/emu2413/sample2413.c",
    "modules/kmz80/makeft.c",
}


def sources():
    got = []
    for root, dirs, files in os.walk(LIB):
        dirs[:] = [d for d in dirs if d not in ("test", ".github", "_dl")]
        for fn in sorted(files):
            if not fn.endswith(".c"):
                continue
            rel = os.path.relpath(os.path.join(root, fn), LIB).replace("\\", "/")
            if rel in EXCLUDE:
                continue
            if not (rel.startswith("src/") or rel.startswith("modules/")):
                continue
            got.append(rel)
    return sorted(got)


def zig():
    try:
        import ziglang
    except ImportError:
        sys.exit("缺 ziglang：py -3.14 -m pip install ziglang")
    p = os.path.join(os.path.dirname(ziglang.__file__), "zig.exe")
    if not os.path.isfile(p):
        sys.exit("找不到 zig.exe: %s" % p)
    return p


def main():
    if not os.path.isdir(LIB):
        sys.exit("缺源码：%s（libkss 应整棵放在这里）" % LIB)
    srcs = sources()
    if len(srcs) < 20:
        sys.exit("源文件只找到 %d 个，目录结构不对" % len(srcs))
    cmd = [zig(), "cc", "-O2", "-mcpu=baseline", "-DEXCLUDE_DRIVER_ALL",
           "-o", OUT, os.path.join(HERE, "kss2wav.c")]
    cmd += [os.path.join(LIB, s) for s in srcs]
    cmd += ["-I", os.path.join(LIB, "src"),
            "-I", os.path.join(LIB, "modules"), "-lm"]
    print("源文件 %d 个" % len(srcs))
    print(" ".join(cmd[:6]), "...")
    rc = subprocess.call(cmd, cwd=HERE)
    if rc != 0:
        sys.exit("编译失败 rc=%d" % rc)
    if not os.path.isfile(OUT):
        sys.exit("没有产物：%s" % OUT)
    print("OK %s  %d B" % (OUT, os.path.getsize(OUT)))


if __name__ == "__main__":
    main()
