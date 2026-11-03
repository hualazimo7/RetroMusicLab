#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py — 编译 `gsf2wav.exe`（GSF/minigsf → WAV 的原生解码器）。

和 `tools/usf2wav` / `tools/vio2wav` / `tools/ssf2wav` 是同一套路：
`zig cc` 当编译器，产物放到包根目录。

## 这个核和前三个不一样的地方

1. **源文件多得多（80 多个）**。mGBA 是个完整模拟器，最小核也要
   ARM 解释器 + GBA 主板 + mCore 框架 + 工具层。清单照
   `CMakeLists.txt` 的 `CORE_SRC = ARM_SRC + GBA_SRC + UTIL_SRC +
   CORE_VFS_SRC + OS_SRC + THIRD_PARTY_SRC` 抄，各段的文件来自
   `src/{arm,core,gba,util}/CMakeLists.txt`。

2. **宏必须凑齐 `MINIMAL_CORE=2` 那一套**。缺 `M_CORE_GBA` 就
   `mCoreFindVF` 直接返回 NULL；缺 `MINIMAL_CORE` 会把 sqlite / png /
   elf / 调试器全拖进来，缺一堆可选依赖。

3. **`-mcpu=baseline` 不能省**。理由和另外三个一样：默认会按编译机
   CPU 出码，换到老一点的机器就是 `0xC000001D`（README ⚠51）。

4. **不要开 `-ffast-math`**。mGBA 的音频混合有定点/浮点混算，
   开了之后逐样本比对就对不上参照了。

## 用法

    C:/Python314/python.exe tools/gsf2wav/build.py
    C:/Python314/python.exe tools/gsf2wav/build.py --ref   # 编参照工具

产物：`msu1-to-cd/gsf2wav.exe`
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))          # msu1-to-cd/
SRC = os.path.join(HERE, "src")
MGBA = os.path.join(SRC, "mgba")                      # vendored mGBA 0.10.5
OUT = os.path.join(PKG, "gsf2wav.exe")
BUILD = os.path.join(HERE, "obj")

#: ARM 解释器（`src/arm/CMakeLists.txt` 的 `SOURCE_FILES`，6 个）。
_ARM_SOURCES = [
    "arm/arm.c",
    "arm/decoder-arm.c",
    "arm/decoder.c",
    "arm/decoder-thumb.c",
    "arm/isa-arm.c",
    "arm/isa-thumb.c",
]

#: GBA 主板（`src/gba/CMakeLists.txt` 的 `SOURCE_FILES`，34 个）。
#: `SIO_FILES` / `EXTRA_FILES` 被 `MINIMAL_CORE` 挡掉了，不要抄进来。
_GBA_SOURCES = [
    "gb/audio.c",                      # ← gba/CMakeLists 里写的 `../gb/audio.c`
    "gba/audio.c",
    "gba/bios.c",
    "gba/cart/ereader.c",
    "gba/cart/gpio.c",
    "gba/cart/matrix.c",
    "gba/cart/vfame.c",
    "gba/cheats.c",
    "gba/cheats/codebreaker.c",
    "gba/cheats/gameshark.c",
    "gba/cheats/parv3.c",
    "gba/core.c",
    "gba/dma.c",
    "gba/gba.c",
    "gba/hle-bios.c",
    "gba/input.c",
    "gba/io.c",
    "gba/memory.c",
    "gba/overrides.c",
    "gba/renderers/cache-set.c",
    "gba/renderers/common.c",
    "gba/renderers/gl.c",
    "gba/renderers/software-bg.c",
    "gba/renderers/software-mode0.c",
    "gba/renderers/software-obj.c",
    "gba/renderers/video-software.c",
    "gba/savedata.c",
    "gba/serialize.c",
    "gba/sharkport.c",
    "gba/sio.c",
    "gba/sio/gbp.c",
    "gba/sio/joybus.c",
    "gba/timer.c",
    "gba/video.c",
]

#: mCore 框架（`src/core/CMakeLists.txt` 的 `SOURCE_FILES`，19 个）。
_CORE_SOURCES = [
    "core/bitmap-cache.c",
    "core/cache-set.c",
    "core/cheats.c",
    "core/config.c",
    "core/core.c",
    "core/directories.c",
    "core/input.c",
    "core/interface.c",
    "core/library.c",
    "core/lockstep.c",
    "core/log.c",
    "core/map-cache.c",
    "core/mem-search.c",
    "core/rewind.c",
    "core/serialize.c",
    "core/sync.c",
    "core/thread.c",
    "core/tile-cache.c",
    "core/timing.c",
]

#: 工具层（`src/util/CMakeLists.txt` 的 `SOURCE_FILES`，19 个 + `memory.c`）。
#: `gui*` 不要（那是 mGBA 自己的界面）。
_UTIL_SOURCES = [
    "util/circle-buffer.c",
    "util/configuration.c",
    "util/crc32.c",
    "util/formatting.c",
    "util/gbk-table.c",
    "util/hash.c",
    "util/string.c",
    "util/table.c",
    "util/vfs.c",
    "util/convolve.c",
    "util/elf-read.c",
    "util/export.c",
    "util/patch.c",
    "util/patch-fast.c",
    "util/patch-ips.c",
    "util/patch-ups.c",
    "util/png-io.c",
    "util/ring-fifo.c",
    "util/text-codec.c",
    "util/memory.c",                   # ← `OS_SRC`：没有平台层时就用它
]

#: `CORE_VFS_SRC`（`CMakeLists.txt:105`）+ `vfs-fd.c`。
#:
#: `vfs-mem` / `vfs-fifo` 是核心必需的；`vfs-fd` 是 `util/vfs.c` 里
#: `VFileOpen` 在非 PSP/3DS 平台上的落点（`return VFileOpenFD(...)`）——
#: 不带上就是链接期一个 undefined symbol。
#: `vfs-file` / `vfs-zip` / `vfs-lzma` 由 `VFS_SRC` 按需加，最小核不加。
_VFS_SOURCES = [
    "util/vfs/vfs-mem.c",
    "util/vfs/vfs-fifo.c",
    "util/vfs/vfs-fd.c",
]

#: 第三方：ini 解析（`configuration.c` 要）+ blip_buf（音频合成器）。
_THIRD_PARTY = [
    "third-party/inih/ini.c",
    "third-party/blip_buf/blip_buf.c",
]

CFLAGS = [
    "-O2",
    # ⚠ 不能省，见文件头第 3 条
    "-mcpu=baseline",
    # ⚠ 不能加 -ffast-math，见文件头第 4 条
    "-w",
    "-include", "string.h",   # 有几个文件用了 memcpy/strcmp 却没 include
    # ⚠ 最小核那一套，见文件头第 2 条
    "-DMINIMAL_CORE=2",
    "-DM_CORE_GBA",
    "-DMGBA_STANDALONE",
    "-DDISABLE_THREADING",
    "-DCOLOR_16_BIT",
    "-DCOLOR_5_6_5",
    "-I", os.path.join(MGBA, "include"),
    # 引号式 include 走这里：`"gba/hle-bios.h"` / `"third-party/inih/ini.h"`
    "-I", MGBA,
    "-I", os.path.join(SRC, "psflib"),
    "-I", os.path.join(SRC, "zlib"),
]

#: `version.c` 是 CMake 用 `src/core/version.c.in` 生成的，这里手写一份。
#: 内容只在 `--info` / 崩溃日志里露脸，不影响音频输出。
_VERSION_C = r'''#include <mgba/core/version.h>

MGBA_EXPORT const char* const gitCommit = "(vendored)";
MGBA_EXPORT const char* const gitCommitShort = "(vendored)";
MGBA_EXPORT const char* const gitBranch = "(vendored)";
MGBA_EXPORT const int gitRevision = -1;
MGBA_EXPORT const char* const binaryName = "gsf2wav";
MGBA_EXPORT const char* const projectName = "mGBA";
MGBA_EXPORT const char* const projectVersion = "0.10.5";
'''

#: ⚠ mGBA 自带一个 `crc32()`（Gary S. Brown 那份，`util/crc32.c`），
#: 和 zlib 的 `crc32()` **同名不同源**。两者签名恰好都是
#: `(uint32_t crc, const void *buf, size_t/uint len)`，但符号只有一个，
#: 链接期直接 duplicate symbol。
#:
#: 处理办法：给**所有 mGBA 源文件**加 `-Dcrc32=mgba_crc32`，把 mGBA 那套
#: 整体改名，zlib 的 `crc32` 留给 psflib 用（它要校验 2SF 的 SAVE 块）。
#: 副作用是 `include/mgba/core/library.h` 里的同名结构成员也一起改名 ——
#: 只要所有 mGBA 文件用同一个宏，成员名一致，行为不变。
#: （那个成员属于 ROM 库功能，`USE_SQLITE3` 关掉后根本编不进来。）
_MGBA_DEFS = ["-Dcrc32=mgba_crc32"]


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
        print("编译失败：%s" % os.path.relpath(src, HERE), file=sys.stderr)
        return r.returncode
    return 0


def main():
    # `--ref` 编「黄金参照」工具（plugin.cc 的 0x22 分支原样副本）。
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
        main_c = os.path.join(HERE, "gsf2wav.c")

    # ---- 收集源文件（每项是「路径 + 额外的 -D」）----
    mgba_files = (_ARM_SOURCES + _GBA_SOURCES + _CORE_SOURCES
                  + _UTIL_SOURCES + _VFS_SOURCES + _THIRD_PARTY)
    jobs = [(main_c, [])]
    jobs.append((os.path.join(SRC, "psflib", "psflib.c"), []))
    jobs += [(os.path.join(MGBA, p), _MGBA_DEFS) for p in mgba_files]
    jobs += [(os.path.join(SRC, "zlib", n), []) for n in (
        "adler32.c", "crc32.c", "inflate.c", "inffast.c",
        "inftrees.c", "zutil.c", "uncompr.c",
    )]

    missing = [p for p, _ in jobs if not os.path.isfile(p)]
    if missing:
        print("缺源文件（%d 个）：\n  " % len(missing)
              + "\n  ".join(missing), file=sys.stderr)
        return 1

    os.makedirs(BUILD, exist_ok=True)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    # ---- 生成 version.c ----
    ver_c = os.path.join(BUILD, "version.c")
    with open(ver_c, "w", encoding="utf-8") as f:
        f.write(_VERSION_C)
    jobs.append((ver_c, _MGBA_DEFS))

    # ---- 逐个编成 .o ----
    print("编译 gsf2wav …（%d 个 .c）" % len(jobs))

    # 增量：宏一变（比如改了 -D）就得全量重编，所以把宏的指纹存下来比。
    # 94 个文件全量编一次要 4 分半，改一行重编一次太贵。
    stamp_path = os.path.join(BUILD, "stamp.txt")
    stamp = "\n".join(CFLAGS + _MGBA_DEFS)
    old_stamp = ""
    if os.path.isfile(stamp_path):
        with open(stamp_path, "r", encoding="utf-8") as f:
            old_stamp = f.read()
    fresh = (old_stamp == stamp)

    objs = []
    n_skip = 0
    for idx, (src, extra) in enumerate(jobs):
        obj = os.path.join(BUILD, "c%03d_%s.o"
                           % (idx, os.path.splitext(os.path.basename(src))[0]))
        if (fresh and os.path.isfile(obj)
                and os.path.getmtime(obj) >= os.path.getmtime(src)):
            n_skip += 1
            objs.append(obj)
            continue
        rc = _compile(zig, "cc", src, obj, extra)
        if rc:
            return rc
        objs.append(obj)
    if n_skip:
        print("  （%d 个没变，跳过）" % n_skip)

    with open(stamp_path, "w", encoding="utf-8") as f:
        f.write(stamp)

    # ---- 链接 ----
    cmd = [zig, "cc", "-mcpu=baseline", "-o", out] + objs
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("链接失败（zig 退出码 %d）" % r.returncode, file=sys.stderr)
        return r.returncode

    print("产物: %s（%.1f MB）" % (out, os.path.getsize(out) / 1048576.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
