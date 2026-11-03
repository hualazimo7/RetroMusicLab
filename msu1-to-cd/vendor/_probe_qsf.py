# -*- coding: utf-8 -*-
"""_probe_qsf.py —— QSF（Capcom QSound 街机音源）格式研究与解码探针

**这是 QSF 的格式门禁**（v5.9.10 起）。它比 ``test_web_ui.t_qsf`` 深一层：
那边验的是「接线对不对」（模块契约、分流、坏文件报错），这边验的是
**格式本身理解得对不对** —— 逐字节拆容器、逐块拆 ROM、逐首比补丁字节。

分四段：

  ① 纯 Python 解容器（不依赖 qsf2wav.exe）—— 证明格式理解正确
  ② 逐块拆 qsflib —— 证明「Z80 程序 + SMP 采样 + 曲号补丁」三段式
  ③ 逐首对比 miniqsf 的补丁字节 —— 证明「一首歌 = 往 ROM 0x21 写一个字节」
  ④ 用 qsf2wav.exe 真解码全部曲目 —— 证明能出声、时长对得上 tags

用法：
    py -3.14 vendor/_probe_qsf.py
    py -3.14 vendor/_probe_qsf.py --dir "<某个 QSF 游戏目录>"
    py -3.14 vendor/_probe_qsf.py --no-decode      # 只做结构分析，秒级

退出码 0 = 全绿。

⚠ 这一段**必须独立存在**，不能塞进 ``test_web_ui.py``：test_web_ui 的
  坏文件用例只喂了 28 字节垃圾，验不出「11 字节块头 + 载荷能不能严丝合缝
  铺满整个 exe 段」这种算术自洽 —— 而那个算术自洽正是「格式理解正确」
  最硬的证据（实测：``(11+262144) + (11+4194304) = 4,456,470`` =
  解压总长，三项对得上就基本不可能理解错）。
"""

import argparse
import hashlib
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)

#: 默认样本：joshw 的 QSound 街机包里唯一下载过的一个
DEFAULT_DIR = (r"D:/MSU1Downloads/joshw/QSound 街机（Hoot）"
               r"/Mega Man 2 - The Power Fighters "
               r"[Rockman 2 - The Power Fighters] (QSF)(1996)(Capcom)")

_n_ok = 0
_n_bad = 0


def _mark(cond, label, detail=""):
    global _n_ok, _n_bad
    if cond:
        _n_ok += 1
        print("  [OK] %s%s" % (label, ("  → " + detail) if detail else ""))
    else:
        _n_bad += 1
        print("  [!!] %s%s" % (label, ("  → " + detail) if detail else ""))
    return bool(cond)


# ---------------------------------------------------------------------------
# ① PSF 容器（版本字节 0x41 = QSound）
# ---------------------------------------------------------------------------

def parse_psf(path):
    """按 PSF 规范解一层：magic(4) + reserved_size + exe_size + crc32 + ...
    返回 dict：magic/version/reserved/exe(解压后)/tags。"""
    b = open(path, "rb").read()
    magic = b[:3]
    version = b[3]
    rsv, csz, crc = struct.unpack("<III", b[4:16])
    body = b[16 + rsv:16 + rsv + csz]
    exe = zlib.decompress(body) if csz else body
    tail = b[16 + rsv + csz:]
    tags = {}
    if tail[:5] == b"[TAG]":
        for line in tail[5:].decode("utf-8", "replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                tags[k.strip().lower()] = v.strip()
    return {"magic": magic, "version": version, "reserved": rsv,
            "exe_csize": csz, "exe_crc": crc, "exe": exe,
            "exe_ok": (zlib.crc32(body) & 0xFFFFFFFF) == crc if csz else True,
            "tags": tags}


# ---------------------------------------------------------------------------
# ② QSF 的块：id[3] + dataofs(u32) + datasize(u32) + data
# ---------------------------------------------------------------------------

def parse_chunks(blob):
    """把 exe section 拆成 [(id, dataofs, datasize, data), ...]"""
    out = []
    i = 0
    n = len(blob)
    while n - i >= 11:
        cid = blob[i:i + 3].decode("latin-1")
        dataofs, datasize = struct.unpack("<II", blob[i + 3:i + 11])
        i += 11
        if datasize > n - i:
            out.append((cid, dataofs, datasize, b"", "TRUNCATED"))
            break
        out.append((cid, dataofs, datasize, blob[i:i + datasize], ""))
        i += datasize
    return out


def parse_time_ms(s):
    """`0:45` / `1:23.5` / `83.5` → 毫秒；非法 -1。"""
    if not s:
        return -1
    parts = s.strip().split(":")
    if len(parts) > 3:
        return -1
    try:
        if len(parts) == 1:
            return int(round(float(parts[0]) * 1000))
        if len(parts) == 2:
            return int(parts[0]) * 60000 + int(round(float(parts[1]) * 1000))
        return (int(parts[0]) * 3600 + int(parts[1]) * 60
                + int(round(float(parts[2])))) * 1000
    except ValueError:
        return -1


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--exe", default=os.path.join(PKG, "qsf2wav.exe"))
    ap.add_argument("--no-decode", action="store_true",
                    help="只做结构分析，不跑 qsf2wav.exe")
    args = ap.parse_args()

    d = args.dir
    print("目录：%s\n" % d)
    if not os.path.isdir(d):
        print("[!!] 目录不存在")
        return 1

    # ⚠ 不能用 glob：joshw 的目录名里带 `[Rockman 2 - ...]`，
    #   方括号会被 glob 当成**字符集**，于是「目录明明在、文件一个都找不到」。
    names = sorted(os.listdir(d))
    minis = [os.path.join(d, n) for n in names if n.lower().endswith(".miniqsf")]
    libs = [os.path.join(d, n) for n in names if n.lower().endswith(".qsflib")]
    if not minis:
        print("[!!] 没找到 .miniqsf")
        return 1

    # ---------------- ① 容器 ----------------
    print("=== ① PSF 容器（版本字节 0x41 = Capcom QSound）===")
    one = parse_psf(minis[0])
    _mark(one["magic"] == b"PSF", "magic 是 'PSF'", repr(one["magic"]))
    _mark(one["version"] == 0x41, "版本字节 = 0x41（QSound）",
          "0x%02X" % one["version"])
    # ⚠ 别想当然写成「miniQSF 没有 CRC」—— 它**有**（csz=17、crc=3E09407E），
    #   而且校验的就是那 17 字节**压缩后**的数据。PSF 规范里 CRC 一直是
    #   算在压缩体上的，不是解压后。
    _mark(one["exe_ok"], "miniQSF 的 exe 段 CRC32 校验通过",
          "csz=%d crc=%08X" % (one["exe_csize"], one["exe_crc"]))
    _mark(len(one["exe"]) == 12, "miniQSF 程序段解压后 12 字节",
          "%d B：%s" % (len(one["exe"]), one["exe"].hex(" ")))
    _mark(one["tags"].get("_lib", "").endswith(".qsflib"),
          "_lib 指向 .qsflib", one["tags"].get("_lib", "（缺）"))
    _mark("length" in one["tags"], "带 length 标签", one["tags"].get("length", ""))
    _mark("volume" in one["tags"], "带 volume 标签（QSound 的输出偏轻，靠它补）",
          one["tags"].get("volume", ""))

    # ---------------- ② qsflib 的块 ----------------
    print("\n=== ② qsflib 解压后的块结构 ===")
    if not libs:
        _mark(False, "找到 .qsflib", "没有")
        return 1
    lib = parse_psf(libs[0])
    _mark(lib["version"] == 0x41, "qsflib 也是 0x41", "0x%02X" % lib["version"])
    _mark(lib["exe_ok"], "qsflib 的 zlib CRC 校验通过",
          "%d B → %d B" % (lib["exe_csize"], len(lib["exe"])))
    chunks = parse_chunks(lib["exe"])
    print("  解压后 %d 字节，%d 个块：" % (len(lib["exe"]), len(chunks)))
    for cid, ofs, size, _data, note in chunks:
        print("    %-4s dataofs=0x%-6X datasize=%9d %s"
              % (cid, ofs, size, note))
    ids = [c[0] for c in chunks]
    _mark("Z80" in ids, "有 Z80 程序块",
          next((str(c[2]) + " B" for c in chunks if c[0] == "Z80"), ""))
    _mark("SMP" in ids, "有 SMP 采样块",
          next((str(c[2]) + " B" for c in chunks if c[0] == "SMP"), ""))
    total = sum(11 + c[2] for c in chunks)
    _mark(total == len(lib["exe"]),
          "「11 字节头 + 载荷」能严丝合缝铺满整个 exe 段",
          "%d == %d" % (total, len(lib["exe"])))

    z80 = next((c[3] for c in chunks if c[0] == "Z80"), b"")
    smp = next((c[3] for c in chunks if c[0] == "SMP"), b"")
    _mark(len(z80) and z80[0] == 0xF3,
          "Z80 程序第 1 个字节是 0xF3（DI，中断关闭）",
          z80[:12].hex(" ") if z80 else "（无）")
    _mark(len(smp) == 4 * 1024 * 1024, "SMP 是 4MB（CPS2 的采样 ROM 尺寸）",
          "%d B" % len(smp))

    # ---------------- ③ 曲号补丁 ----------------
    print("\n=== ③ 每首 miniqsf 都是「往 Z80 ROM 写 1 个字节」 ===")
    rows = []
    bad_patch = []
    for p in minis:
        info = parse_psf(p)
        ch = parse_chunks(info["exe"])
        if len(ch) != 1 or ch[0][0] != "Z80" or ch[0][2] != 1:
            bad_patch.append((os.path.basename(p), ch))
            continue
        rows.append((os.path.basename(p), ch[0][1], ch[0][3][0],
                     info["tags"].get("title", ""),
                     info["tags"].get("length", ""),
                     info["tags"].get("fade", "")))
    _mark(not bad_patch, "全部 %d 首都只有 1 个块、且 datasize=1" % len(minis),
          ("异常 %d 个" % len(bad_patch)) if bad_patch else "")
    offs = {r[1] for r in rows}
    _mark(offs == {0x21}, "写入偏移全部是 0x21", "实测 %s" % sorted(hex(o) for o in offs))
    songs = [r[2] for r in rows]
    _mark(len(set(songs)) == len(songs),
          "曲号互不重复（%d 首 → %d 个不同曲号）" % (len(songs), len(set(songs))),
          "范围 0x%02X~0x%02X" % (min(songs), max(songs)))
    _mark(z80[0x21] == 0x01,
          "qsflib 里 ROM[0x21] 的初值就是 0x01（第 1 首）",
          "0x%02X" % z80[0x21] if len(z80) > 0x21 else "（越界）")

    print("\n  %-3s %-6s %-6s %-8s %s" % ("#", "曲号", "字节", "length", "title"))
    for i, r in enumerate(rows[:6], 1):
        print("  %-3d 0x%-4X 0x%-4X %-8s %s"
              % (i, r[2], r[2], r[4], r[3]))
    print("  …（共 %d 首）" % len(rows))

    # ---------------- ④ 真解码 ----------------
    if args.no_decode:
        print("\n=== ④ 跳过解码（--no-decode）===")
    else:
        print("\n=== ④ 真解码（%s）===" % os.path.basename(args.exe))
        if not os.path.isfile(args.exe):
            _mark(False, "找到解码器", args.exe)
        else:
            out = os.path.join(tempfile.gettempdir(), "_qsf_probe.wav")
            t0 = time.time()
            ok = 0
            silent = []
            mismatch = []
            peaks = []
            secs_all = 0.0
            for p, song, byte, title, length, fade in rows:
                r = subprocess.run([args.exe, os.path.join(d, p), out],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
                m = re.search(r"seconds=([\d.]+) samples=(\d+) peak=(\d+)",
                              r.stdout or "")
                if r.returncode != 0 or not m:
                    silent.append((p, r.returncode,
                                   (r.stderr or "").strip().splitlines()[-1:]))
                    continue
                ok += 1
                sec = float(m.group(1))
                peak = int(m.group(3))
                peaks.append(peak)
                secs_all += sec
                want = parse_time_ms(length)
                fd = parse_time_ms(fade) or 0
                if want > 0:
                    exp = (want + fd) / 1000.0
                    if abs(sec - exp) > 0.02:
                        mismatch.append((p, sec, exp))
            el = time.time() - t0
            try:
                os.remove(out)
            except OSError:
                pass

            _mark(ok == len(rows), "全部 %d 首都能解码" % len(rows),
                  "成功 %d，失败 %d" % (ok, len(silent)))
            for s in silent[:3]:
                print("       失败：%s rc=%s %s" % s)
            _mark(all(p > 0 for p in peaks), "没有一首是静音",
                  "peak 范围 %d ~ %d（约 %.1f dBFS）"
                  % (min(peaks), max(peaks),
                     20 * __import__("math").log10(max(peaks) / 32768.0)))
            _mark(not mismatch, "解码时长与 length+fade 标签一致（±20ms）",
                  ("不符 %d 首：%s" % (len(mismatch), mismatch[:2]))
                  if mismatch else "")
            print("  音频总时长 %.1f 分钟，解码耗时 %.1f 秒（约 %.0f× 实时）"
                  % (secs_all / 60.0, el, secs_all / max(el, 1e-6)))

            # 确定性：同一首解两次必须逐字节相同
            a = os.path.join(tempfile.gettempdir(), "_qsf_a.wav")
            b = os.path.join(tempfile.gettempdir(), "_qsf_b.wav")
            src = os.path.join(d, rows[0][0])
            for t in (a, b):
                subprocess.run([args.exe, src, t], capture_output=True)
            ha = hashlib.md5(open(a, "rb").read()).hexdigest()
            hb = hashlib.md5(open(b, "rb").read()).hexdigest()
            _mark(ha == hb, "同一首解两次逐字节相同（可复现）", ha[:16])
            for t in (a, b):
                try:
                    os.remove(t)
                except OSError:
                    pass

    print("\n=== 汇总：%d 通过 / %d 失败 ===" % (_n_ok, _n_bad))
    return 0 if _n_bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
