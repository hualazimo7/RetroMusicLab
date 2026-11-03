#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py — 编译 wsr2wav.exe（WonderSwan 核 + 最小 libretro 前端）。

## 它是干什么的

`.wsr`（WonderSwan Sound Rip）是「裸 WonderSwan ROM 镜像」，要出声必须模拟
V30MZ（80186）+ WS 4 通道声卡。本工程的 `libgme.dll` 没有 WonderSwan 机种，
所以这里用 `beetle-wswan-libretro`（mednafen 的 WonderSwan 核）编一个最小
libretro 前端当解码器。

**关键点：曲号通过 AX 寄存器传入。** WSR 规范写明启动时
`CS=0xFFFF, IP=0x0000, AX=曲号`，而普通模拟器复位时把 AX 清 0 —— 这就是
「WSR 转出来全静音」的根因。前端在 `retro_load_game` 之后直接调核里的
`v30mz_set_reg(NEC_AW, track)`（核是静态链接的，符号可见），详见
`wsr2wav.c` 头部注释。

## 依赖

`beetle-wswan-libretro` 需要单独 clone（体积不小，没进仓库）：

    git clone --depth 1 https://github.com/libretro/beetle-wswan-libretro.git \\
        msu1-to-cd/tools/wswan2wav/src/beetle-wswan-libretro

## 编译要点（踩过的坑）

1. **必须带 `-D__LIBRETRO__`** —— libretro.c 的胶水依赖它。
2. **必须带 `-DMEDNAFEN_VERSION_NUMERIC=931`** —— 否则 state.c 编不过。
3. **必须带 `-DLSB_FIRST` / `-DNEED_BLIP=1` / `-DNEED_STEREO_SOUND=1`** ——
   Blip_Buffer 的位深和声道数靠这几个宏定，漏了链接期符号不全。
4. **`-I<core>/libretro-common/include`** —— 少了会报 `boolean.h` not found。
5. **额外要编 `libretro-common/compat/compat_strl.c` 和 `compat_snprintf.c`**
   —— state.c / settings.c 用 `strlcpy_retro__` / `snprintf_retro__`。
6. **所有文件都要 `-mcpu=baseline`**（老 CPU 不能吃 AVX，见 README ⚠51）。
   编译和链接两处都要带 —— 只在编译带、链接不带，LTO 阶段还能给你塞回去。
7. `_LOW_ACCURACY_` 在这个核里**零引用**，可带可不带。

## 用法

    py -3.14 tools/wswan2wav/build.py

产物：`msu1-to-cd/wsr2wav.exe`
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))
SRC_DIR = os.path.join(HERE, "src", "beetle-wswan-libretro")
OUT = os.path.join(PKG, "wsr2wav.exe")

SOURCES = [
    "mednafen/wswan/v30mz.c", "mednafen/wswan/sound.c",
    "mednafen/wswan/wswan-memory.c", "mednafen/wswan/interrupt.c",
    "mednafen/wswan/gfx.c", "mednafen/wswan/eeprom.c",
    "mednafen/wswan/rtc.c", "mednafen/wswan/comm.c",
    "mednafen/wswan/tcache.c",
    "mednafen/sound/Blip_Buffer.c",
    "mednafen/state.c", "mednafen/settings.c", "mednafen/mempatcher.c",
    "libretro.c",
    "libretro-common/compat/compat_strl.c",
    "libretro-common/compat/compat_snprintf.c",
]

DEFS = ["-DNDEBUG", "-DSIZEOF_DOUBLE=8", "-DMEDNAFEN_VERSION_NUMERIC=931",
        "-DMPC_FIXED_POINT", "-DSTDC_HEADERS", "-D__STDC_LIMIT_MACROS",
        "-D__STDC_CONSTANT_MACROS",
        "-D__LIBRETRO__", "-DLSB_FIRST",
        "-DNEED_BLIP=1", "-DNEED_STEREO_SOUND=1",
        "-DFRONTEND_SUPPORTS_RGB565=1"]
INCS = ["-I.", "-Imednafen", "-Imednafen/include", "-Imednafen/sound",
        "-Ilibretro-common/include"]


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
    return out


def find_zig():
    """找 zig 可执行文件。优先环境变量，其次 ziglang 包，最后 PATH。

    ⚠ 别把路径写死成 `C:/Users/<某人>/...` —— 换台机器/换个用户名就炸。
    本工程其它 5 个 build.py 都是这套，保持一致。
    """
    env = os.environ.get("ZIG")
    if env and os.path.isfile(env):
        return env
    for sp in _site_packages():
        for name in ("zig.exe", "zig"):
            c = os.path.join(sp, "ziglang", name)
            if os.path.isfile(c):
                return c
    for name in ("zig.exe", "zig"):
        p = shutil.which(name)
        if p:
            return p
    return ""


def main():
    zig = find_zig()
    if not zig:
        print("找不到 zig。装一下：\n"
              "    py -3.14 -m pip install ziglang\n"
              "或者设环境变量 ZIG=<zig.exe 路径>", file=sys.stderr)
        return 1
    if not os.path.isdir(SRC_DIR):
        print("缺核源码：%s" % SRC_DIR, file=sys.stderr)
        print("先 clone: git clone --depth 1 "
              "https://github.com/libretro/beetle-wswan-libretro.git "
              "tools/wswan2wav/src/beetle-wswan-libretro", file=sys.stderr)
        return 1
    obj_dir = os.path.join(HERE, "obj")
    os.makedirs(obj_dir, exist_ok=True)
    objs = []
    for rel in SOURCES:
        src = os.path.join(SRC_DIR, rel)
        obj = os.path.join(obj_dir, rel.replace("/", "_") + ".o")
        cmd = ([zig, "cc", "-c", "-O2", "-mcpu=baseline"] + DEFS + INCS
               + [src, "-o", obj])
        r = subprocess.run(cmd, cwd=SRC_DIR, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(obj):
            print("编译失败 %s\n%s" % (rel, r.stderr[:500]), file=sys.stderr)
            return 1
        objs.append(obj)
    # 前端自己也 -c 成 .o，最后统一链接（别用一条命令编完：那样 INCS 是
    # 相对核目录的，前端文件在上一层，路径会错位）
    front_obj = os.path.join(obj_dir, "wsr2wav.c.o")
    cmd = ([zig, "cc", "-c", "-O2", "-mcpu=baseline"] + DEFS + INCS
           + [os.path.join(HERE, "wsr2wav.c"), "-o", front_obj])
    r = subprocess.run(cmd, cwd=SRC_DIR, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(front_obj):
        print("编译前端失败\n%s" % r.stderr[:800], file=sys.stderr)
        return 1
    objs.append(front_obj)

    cmd = [zig, "cc", "-mcpu=baseline", "-o", OUT] + objs
    r = subprocess.run(cmd, cwd=SRC_DIR, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(OUT):
        print("链接失败\n%s" % r.stderr[:800], file=sys.stderr)
        return 1
    print("产物: %s (%.1f MB)" % (OUT, os.path.getsize(OUT) / 1048576.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
