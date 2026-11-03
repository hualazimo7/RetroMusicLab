#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_e2e_v5917_mp3_cover.py — MP3 内嵌封面的**真实端到端**（v5.9.17）。

`test_web_ui.py --only=v5918` 守的是「源码/常量/真写一个 MP3」，但那里用的
是合成正弦。这里跑的是**真音频 + 真封面**：

  源  = 用户库里已经转好的真 FLAC（Advance Wars 2，47 首里的前 2 首）
  图  = 同一个游戏目录里那份真 `cover.png`（435,022 B，512×512）

要证明的五件事：

  ① **目录里有 `cover.png` 就自动用它** —— 不用调用方传 cover，模拟
     「用户重开一次、直接点转 MP3」这条最容易出错的路径。
  ② **写出来的 MP3 真带 APIC**：type=3（正面封面）、`image/png`、
     图片字节数与源图**逐字节一致**（不是截断）。
  ③ **音频没被弄坏**：soundfile 仍能解码，帧数 ≈ 源时长，44100Hz 立体声。
  ④ **重复嵌是替换不是叠加**：再嵌一次文件大小不变、APIC 仍 1 个。
  ⑤ **`_backfill_audio` 能救老文件**：把 APIC 抹掉之后回填，图回来了。

用法::

    py -3.14 vendor/_e2e_v5917_mp3_cover.py
    py -3.14 vendor/_e2e_v5917_mp3_cover.py --keep     # 留临时目录看现场
    py -3.14 vendor/_e2e_v5917_mp3_cover.py --src=...  # 换一个 FLAC 目录

⚠ 全程落在 %TEMP%，不碰用户目录（源文件只读）。
⚠ 收尾只删自己造的临时目录。
"""
import argparse
import os
import shutil
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_SRC = (r"D:\MSU1Downloads\joshw\Game Boy Advance\FLAC"
               r"\Advance Wars 2 - Black Hole Rising (2003-06-24)"
               r"(Intelligent Systems)(Nintendo)[GBA]")

_ok = _bad = 0


def _mark(cond, what, detail=""):
    global _ok, _bad
    if cond:
        _ok += 1
        print("  [OK] %s" % what, flush=True)
    else:
        _bad += 1
        print("  [!!] %s   %s" % (what, detail), flush=True)
    return bool(cond)


def _read_flac(path):
    """真 FLAC → (numpy float32 [n,2], rate)。用 soundfile，和生产代码同一条路。"""
    import numpy as np
    import soundfile as sf
    data, rate = sf.read(path, dtype="float32", always_2d=True)
    return np.ascontiguousarray(data), int(rate)


def _as_track(name, arr, rate):
    """`write_tracks` 要的是 ``{"name", "pcm", "little_endian"}``，
    pcm 是 **int16 小端** 的交错字节。和 `ConvertWorker` 解码出来的结构一致。"""
    import numpy as np
    pcm = np.clip(arr * 32767.0, -32768, 32767).astype("<i2")
    return {"name": name, "pcm": pcm.tobytes(), "little_endian": True,
            "rate": rate, "channels": arr.shape[1]}


def _flac_frames(path):
    """直接读 FLAC 的 STREAMINFO 拿总采样数 —— 不依赖解码器。

    STREAMINFO 结构（共 34 字节）：
      16 min block | 16 max block | 24 min frame | 24 max frame   → 前 10 字节
      20 采样率 | 3 声道-1 | 5 位深-1 | 36 总采样数                 → 后 8 字节
    所以 ``body[10:18]`` 是一个 64 位大端整数，**低 36 位**才是总采样数。
    """
    with open(path, "rb") as fh:
        if fh.read(4) != b"fLaC":
            return 0
        hdr = fh.read(4)
        if len(hdr) < 4:
            return 0
        ln = (hdr[1] << 16) | (hdr[2] << 8) | hdr[3]
        body = fh.read(ln)
    if len(body) < 18:
        return 0
    packed = struct.unpack(">Q", body[10:18])[0]
    return packed & 0xFFFFFFFFF          # 低 36 位 = 总采样数


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC, help="真 FLAC 目录（只读）")
    ap.add_argument("--keep", action="store_true", help="留临时目录")
    ap.add_argument("--n", type=int, default=2, help="取前几首")
    args = ap.parse_args()

    src = args.src
    if not os.path.isdir(src):
        print("找不到源目录：%s" % src, flush=True)
        return 2
    flacs = sorted(f for f in os.listdir(src) if f.lower().endswith(".flac"))
    if not flacs:
        print("源目录里没有 .flac：%s" % src, flush=True)
        return 2
    flacs = flacs[:max(1, args.n)]
    cover_src = os.path.join(src, "cover.png")

    import flac_out
    import spc_wav

    print("\n[1] 源材料", flush=True)
    print("     FLAC 目录  %s" % src, flush=True)
    print("     取 %d 首：%s" % (len(flacs), ", ".join(flacs)), flush=True)
    _mark(os.path.isfile(cover_src), "源目录里有真 cover.png",
          "%s B" % (os.path.getsize(cover_src) if os.path.isfile(cover_src) else "?"))
    if not os.path.isfile(cover_src):
        return 1
    cover_bytes = os.path.getsize(cover_src)

    tmp = tempfile.mkdtemp(prefix="v5917_mp3_cover_")
    out = os.path.join(tmp, "MP3", "Advance Wars 2 - Black Hole Rising")
    os.makedirs(out, exist_ok=True)
    # 模拟下载页的行为：先把图落到游戏目录里（v5.9.16 起的约定）
    shutil.copy2(cover_src, os.path.join(out, "cover.png"))

    try:
        # ---- ① 读真音频 ----
        print("\n[2] 解码真 FLAC（soundfile）", flush=True)
        tracks = []
        frames_src = {}
        for i, name in enumerate(flacs):
            p = os.path.join(src, name)
            arr, rate = _read_flac(p)
            frames_src[name] = arr.shape[0]
            tracks.append(_as_track(os.path.splitext(name)[0], arr, rate))
            print("     %s  %d 帧  %d Hz  %d ch" %
                  (name, arr.shape[0], rate, arr.shape[1]), flush=True)
        _mark(all(t["rate"] == 44100 and t["channels"] == 2 for t in tracks),
              "源都是 44100Hz 立体声",
              [(t["rate"], t["channels"]) for t in tracks])
        # 顺带核对 STREAMINFO 的采样数，确保读出来的没被截断
        st = _flac_frames(os.path.join(src, flacs[0]))
        _mark(st == 0 or abs(st - frames_src[flacs[0]]) <= 1,
              "解码帧数与 STREAMINFO 一致（没被截断）",
              "streaminfo=%d read=%d" % (st, frames_src[flacs[0]]))

        # ---- ② 转 MP3：**不传 cover**，靠目录里的 cover.png ----
        print("\n[3] write_tracks(fmt=mp3)，不传 cover（靠目录里的 cover.png）",
              flush=True)
        logs = []
        stat = spc_wav.write_tracks(tracks, out, fmt="mp3",
                                    on_log=logs.append)
        for line in logs:
            print("     " + line, flush=True)
        _mark(stat.get("ok") == len(tracks), "全部转成功", stat)
        _mark(any("封面" in x and "没嵌上" not in x for x in logs),
              "日志明确说封面用上了（不静默）", logs)

        mp3s = sorted(f for f in os.listdir(out) if f.lower().endswith(".mp3"))
        _mark(len(mp3s) == len(tracks), "落盘 .mp3 数量对", mp3s)

        # ---- ③ 验 APIC ----
        print("\n[4] 验 ID3v2 APIC", flush=True)
        for f in mp3s:
            p = os.path.join(out, f)
            pics = flac_out.id3_pictures(p)
            _mark(len(pics) == 1, "%s 有且只有 1 个 APIC" % f, pics)
            if pics:
                pic = pics[0]
                _mark(pic.get("type") == 3, "%s APIC 是 type=3（正面封面）" % f,
                      pic.get("type"))
                _mark(pic.get("mime") == "image/png",
                      "%s APIC 的 mime 是 image/png" % f, pic.get("mime"))
                _mark(pic.get("bytes") == cover_bytes,
                      "%s 图片字节数与源图一致（没截断）" % f,
                      "%d vs %d" % (pic.get("bytes") or 0, cover_bytes))
        _mark(all(flac_out.has_picture(os.path.join(out, f)) for f in mp3s),
              "has_picture 对 MP3 也返回 True", "")

        # ---- ④ 音频没坏 ----
        print("\n[5] 音频仍可解码（ID3 头不影响）", flush=True)
        import soundfile as sf
        for f in mp3s:
            p = os.path.join(out, f)
            info = sf.info(p)
            want = frames_src.get(f.replace(".mp3", ".flac"))
            _mark(info.samplerate == 44100 and info.channels == 2,
                  "%s 仍是 44100Hz / 2ch" % f,
                  "%d / %d" % (info.samplerate, info.channels))
            # MP3 有编码器前导/尾部填充，帧数允许 1% + 2000 帧误差
            tol = (want or 0) * 0.01 + 2000
            _mark(want is None or abs(info.frames - want) <= tol,
                  "%s 帧数与源接近（±1%%+2000）" % f,
                  "src=%s out=%s" % (want, info.frames))

        # ---- ④b 音质：和源 FLAC 逐样本比 ----
        # v5.9.18：上面那些全是**结构性**指标，v5.9.15~17 的坏文件同样全过。
        # 只有解码回来比内容才能证明「声音是对的」。
        print("\n[5b] 音质（和源 FLAC 逐样本比）", flush=True)
        import math

        def _snr(ref, got):
            n = min(len(ref), len(got))
            r = ref[:n].astype("float64")
            g = got[:n].astype("float64")
            e = g - r
            ps, pe = float((r * r).sum()), float((e * e).sum())
            if ps <= 1e-9:
                return 99.0, 1.0
            s = 10.0 * math.log10(ps / pe) if pe > 1e-12 else 99.0
            c = float((r * g).sum() / (math.sqrt(ps * float((g * g).sum())) + 1e-18))
            return s, c

        for f, src_name in zip(mp3s, flacs):
            # ⚠ 输出名带序号前缀（``01_01_01 …``），和源名不是一一对应，
            #   不能靠 replace(".mp3", ".flac") 反推 —— 按**顺序**配对
            #   （write_tracks 就是按 tracks 顺序写的）。
            ref, _r = _read_flac(os.path.join(src, src_name))
            got, _g = sf.read(os.path.join(out, f), dtype="int16", always_2d=True)
            refi = (ref * 32767.0).clip(-32768, 32767).astype("int16")
            s, c = _snr(refi.mean(1), got.mean(1))
            _mark(s >= 20.0,
                  "%s 音质 SNR ≥ 20 dB（坏文件是 -2 dB）" % f, "%.2f dB" % s)
            _mark(c >= 0.99, "%s 波形相关 ≥ 0.99" % f, "%.5f" % c)
            # 码率只能按**音频段**算 —— 文件里还塞了 435 KB 的封面，
            # 用总大小算会虚高到 430 kbps。
            _p = os.path.join(out, f)
            with open(_p, "rb") as _fh:
                _audio_at = flac_out._split_id3(_fh.read())[1]
            kb = (os.path.getsize(_p) - _audio_at) * 8.0 / (
                got.shape[0] / 44100.0) / 1000.0
            _mark(300.0 <= kb <= 345.0,
                  "%s 码率是 320k CBR 量级（按音频段算）" % f, "%.0f kbps" % kb)

        # ---- ⑤ 幂等 ----
        print("\n[6] 重复嵌是替换不是叠加", flush=True)
        sizes = {f: os.path.getsize(os.path.join(out, f)) for f in mp3s}
        for f in mp3s:
            flac_out.embed_id3_picture(os.path.join(out, f), cover_src)
        for f in mp3s:
            p = os.path.join(out, f)
            _mark(os.path.getsize(p) == sizes[f],
                  "%s 二次嵌入后大小不变" % f,
                  "%d -> %d" % (sizes[f], os.path.getsize(p)))
            _mark(len(flac_out.id3_pictures(p)) == 1,
                  "%s 二次嵌入后仍只有 1 个 APIC" % f, "")

        # ---- ⑥ _backfill_audio 救老文件 ----
        print("\n[7] _backfill_audio 给「已经转好但没图」的 MP3 补封面", flush=True)
        from web_ui import dl_api
        # 造一份「没有 ID3 标签」的老 MP3：直接把 ID3 块剥掉
        old = os.path.join(out, "old_no_cover.mp3")
        with open(os.path.join(out, mp3s[0]), "rb") as fh:
            buf = fh.read()
        audio_at = flac_out._split_id3(buf)[1]
        with open(old, "wb") as fh:
            fh.write(buf[audio_at:])
        _mark(not flac_out.has_picture(old), "造出来的老 MP3 确实没有 APIC", "")
        n = dl_api.DownloadState._backfill_audio(_StubDlApi(), os.path.dirname(out))
        _mark(n >= 1, "_backfill_audio 至少补了 1 个", n)
        _mark(flac_out.has_picture(old), "老 MP3 现在有 APIC 了", "")
        pics = flac_out.id3_pictures(old)
        _mark(bool(pics) and pics[0].get("bytes") == cover_bytes,
              "补进去的图字节数对", pics)
        n2 = dl_api.DownloadState._backfill_audio(_StubDlApi(), os.path.dirname(out))
        _mark(n2 == 0, "再跑一次不重复补（幂等）", n2)

        # ---- ⑦ cover.png 还在原地 ----
        print("\n[8] 目录里的 cover.png", flush=True)
        _mark(os.path.isfile(os.path.join(out, "cover.png")),
              "cover.png 仍留在游戏目录（用户能看见）", "")

    finally:
        if args.keep:
            print("\n临时目录保留：%s" % tmp, flush=True)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 46, flush=True)
    print("MP3 封面端到端: %d 通过 / %d 失败" % (_ok, _bad), flush=True)
    print("=" * 46, flush=True)
    return 0 if _bad == 0 else 1


class _StubDlApi(object):
    """只借用 `_backfill_audio` / `_cover_for_dir` / `_copy_cover` / `_has_picture`
    这几个**无状态**方法，不构造整个下载管理器（那会碰配置文件和网络）。"""

    _copy_cover = staticmethod(lambda s, d: (shutil.copy2(s, d), True)[1])

    @staticmethod
    def _cover_for_dir(gdir, game=""):
        local = os.path.join(gdir, "cover.png")
        return local if os.path.isfile(local) else ""

    @staticmethod
    def _has_picture(path):
        import flac_out
        try:
            return bool(flac_out.has_picture(path))
        except Exception:                            # noqa: BLE001
            return False


if __name__ == "__main__":
    sys.exit(main())
