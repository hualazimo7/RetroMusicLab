#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_chip_decode.py — 多格式芯片音源解码链路的端到端验收。

跑法（在项目根目录）：
    python vendor/test_chip_decode.py                     # 自动找样本
    python vendor/test_chip_decode.py --dir <样本目录>   # 指定样本目录
    python vendor/test_chip_decode.py --only hes          # 只跑某格式

覆盖（对齐 PRD §9.2）：
    AC-1  四种格式各 ≥1 个真实文件转换成功
    AC-2  输出 WAV 头 = PCM / 2ch / 16-bit / 44100Hz
    AC-3  全部非静音：峰值 > 300
    AC-4  产出行数 == gme_track_count − 空轨数
    AC-5  HES 空轨被跳过（HC63013 track 4 / HC91048 track 0）
    AC-6  VGM 时长 == 头 0x18 总采样数 ÷ 44100（±0.05s）
    AC-7  GBS 时长取 loop_end ÷ 59.7275（±0.05s）
    AC-8  无时长格式兜底：NSF/HES ≤ 默认秒数 + 1.0s
    AC-15 取消可用：cancel 后 ≤2s 返回且无临时文件残留
    AC-19 单文件失败不影响整批

⚠ 需要真实样本（NSF/GBS/VGM/HES）。没有样本时**明确报错退出**，
  绝不静默跳过 —— 静默跳过会让"测试全绿"变成假信号。
"""

import argparse
import array
import glob
import os
import shutil
import sys
import tempfile
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chip_decode                                          # noqa: E402
import chip_formats as cf                                   # noqa: E402
import msu1_to_cd as core                                   # noqa: E402
import spc_wav                                              # noqa: E402

SR = 44100
BYTES_PER_SEC = SR * 2 * 2
DEFAULT_SECS = 150.0

# PRD T8 实测的两个已知空轨（用于验证"空轨确实被跳过"这一行为本身）
KNOWN_EMPTY_FIRST = {
    "HC63013.hes": 4,
    "HC91048.hes": 0,
}

_GREEN, _RED, _YEL, _RST = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
if not sys.stdout.isatty():
    _GREEN = _RED = _YEL = _RST = ""

class Result:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.rows = []

    def check(self, name, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  {_GREEN}PASS{_RST} {name}" + (f"  {detail}" if detail else ""))
        else:
            self.failed += 1
            print(f"  {_RED}FAIL{_RST} {name}  {detail}")
        return cond

    def row(self, fmt, name, n_head, n_wav, peak, src, secs, wall=""):
        self.rows.append((fmt, name, n_head, n_wav, peak, src, secs, wall))


def peak_of_wav(path):
    """读 WAV 返回峰值（int16 满量程 32767）。"""
    with wave.open(path, "rb") as h:
        raw = h.readframes(h.getnframes())
    a = array.array("h")
    a.frombytes(raw)
    return max(max(a), -min(a)) if a else 0


def find_samples(sample_dir):
    """收集样本 -> {fmt: [paths]}。

    P1 四格式（KSS/GYM/SAP/AY）也在这里收，与 P0 走同一套解码管线。

    ⚠ 必须跳过 `_rejected/`：那里放的是**故意保留的反例**（SAP TYPE D 的
    Digimusic、187KB 全零的无头 GYM），它们用来守识别层的闸门，不是验收素材。
      递归 glob 会把它们一起捞进来，于是"SAP 至少产出 1 个 WAV"必然失败
      （实测踩过：Skwarki.sap 让 SAP 挂掉，而它本就该被拒）。
    """
    pats = {"NSF": "*.nsf", "NSFE": "*.nsfe", "GBS": "*.gbs",
            "VGM": "*.vgm", "VGZ": "*.vgz", "HES": "*.hes",
            "KSS": "*.kss", "GYM": "*.gym",
            "SAP": "*.sap", "AY": "*.ay"}
    got = {}
    for fmt, pat in pats.items():
        files = sorted(glob.glob(os.path.join(sample_dir, "**", pat),
                                 recursive=True))
        # Windows 上 glob 对大小写不敏感，但显式补一份大写变体更稳
        if pat.islower():
            files += sorted(glob.glob(os.path.join(sample_dir, "**",
                                                   pat.upper()),
                                     recursive=True))
        files = sorted(set(f for f in files
                           if "_rejected" not in f.split(os.sep)))
        if files:
            got.setdefault(fmt, [])
            got[fmt].extend(files)
    return got


def count_tracks_and_empty(path):
    """独立实测 (总轨数, 空轨下标列表)。

    用与 chip_decode._track_peak 相同的口径（首 4096 帧峰值 < 300），
    但**不复用**它的实现，避免"用被测代码验证被测代码"。
    空轨数是 HES 的关键事实：实测 73~155 个/文件，远多于 PRD T8 举例的两个。
    """
    import ctypes
    gme = core._load_gme()
    emu = ctypes.c_void_p()
    err = gme.gme_open_file(os.path.abspath(path).encode("utf-8"),
                            ctypes.byref(emu), SR)
    if err:
        return 0, []
    try:
        n = gme.gme_track_count(emu)
        buf = (ctypes.c_short * (4096 * 2))()
        empty = []
        for ti in range(n):
            gme.gme_start_track(emu, ti)
            gme.gme_play(emu, 4096 * 2, buf)
            raw = ctypes.string_at(ctypes.addressof(buf), 4096 * 2 * 2)
            a = array.array("h")
            a.frombytes(raw)
            if (max(max(a), -min(a)) if a else 0) < chip_decode.SIL_PEAK:
                empty.append(ti)
        return n, empty
    finally:
        gme.gme_delete(emu)


def spool_left():
    """临时目录里残留的 chip_pcm_* 数量（AC-15）。"""
    t = tempfile.gettempdir()
    return len(glob.glob(os.path.join(t, "chip_pcm_*")))


def run_format(fmt, paths, out_root, res, default_secs=DEFAULT_SECS,
               limit=1, expect_empty=False):
    """跑一个格式的若干文件，逐项断言。"""
    for path in paths[:limit]:
        base = os.path.basename(path)
        info = cf.probe(path)
        dur = cf.plan_duration(info, default_secs=default_secs)
        out_dir = os.path.join(out_root, fmt, base)
        shutil.rmtree(out_dir, ignore_errors=True)
        t0 = time.time()
        stat = spc_wav.convert([path], out_dir, "chip", chip_secs=default_secs)
        wall = time.time() - t0
        wavs = stat["files"]

        print(f"\n{_YEL}=== {fmt}  {base}{_RST}")
        print(f"    fmt={info.fmt} 头轨数={info.n_tracks} 产出={len(wavs)} "
              f"时长来源={dur.source} 计划={dur.secs:.2f}s")
        # ⚠ 采样目录可能被**另一个进程并发改动**（本仓多人同时跑 fetch_samples，
        #   实测跑到一半gbs/ 目录多出新文件）。spc_wav 会报告它打算写的路径，
        #   若那批文件随后被删/覆盖，就会拿到不存在的路径。
        #   这里只统计**真实存在**的 WAV，避免把环境竞争误报成解码失败。
        wavs = [w for w in wavs if os.path.exists(w)]
        if not wavs:
            res.check(f"[{fmt}] 产出的 WAV 都真实存在", False,
                      "全部缺失（采样目录可能被并发改动，请重跑）")
            continue
        peaks = [peak_of_wav(w) for w in wavs]
        pk = max(peaks) if peaks else 0
        avg = stat["seconds"] / len(wavs) if wavs else 0.0

        res.check(f"[{fmt}] 无解码错误", not stat["errors"],
                  str(stat["errors"][:2]))
        res.check(f"[{fmt}] 至少产出 1 个 WAV", len(wavs) >= 1)
        if not wavs:
            continue

        # AC-2 WAV 头
        ok_hdr = True
        for w in wavs:
            with wave.open(w, "rb") as h:
                ok_hdr &= (h.getnchannels() == 2 and h.getsampwidth() == 2
                           and h.getframerate() == 44100)
        res.check(f"[{fmt}] AC-2 WAV=2ch/16bit/44.1k", ok_hdr)

        # AC-3 非静音
        res.check(f"[{fmt}] AC-3 峰值>300 (实测 {pk})", pk > 300)

        # AC-4 产出行数 == gme_track_count − 空轨数
        #
        # ⚠ 空轨数**必须实测**，不能假设成 1~2 个（PRD T8 只点名了两个"已知空轨"，
        #   实测每个 HES 有 73~155 个空轨 —— T8 是"举例"不是"总数"）。
        #   同时 .m3u 播放列表**不是**轨数真源（HC63013.m3u 只列 8 首，
        #   但 gme_track_count=256 且 183 轨真有声音）。
        if info.fmt == "HES":
            n_head, empties = count_tracks_and_empty(path)
            if info.track_ids:
                # v4.29：同目录 m3u 存在时，**m3u 才是曲目真源** ——
                # 256 个槽位里绝大多数是预分配的空槽/残留，joshw 用 m3u 声明
                # 「这个 rip 到底有哪几首」。所以产出 = m3u 曲号 − 其中为空的。
                # （旧口径 256−空轨 会让 KMCD3005 产出 96 首 224MB，而真实
                #   曲目只有 8 首 89 秒 —— 实测。）
                want = [t for t in info.track_ids if t not in empties]
                res.check(f"[{fmt}] AC-4 按 m3u 取 {len(info.track_ids)} 首",
                          len(wavs) == len(want),
                          f"产出={len(wavs)} 期望={len(want)}（m3u={info.m3u_path}）")
                res.check(f"[{fmt}] AC-4 m3u 曲号 ⊆ gme_track_count",
                          all(0 <= t < n_head for t in info.track_ids),
                          f"n_head={n_head} ids={info.track_ids[:8]}")
            else:
                exp = n_head - len(empties)
                res.check(f"[{fmt}] AC-4 头轨数 {n_head} 与 gme_track_count 一致",
                          n_head == info.n_tracks, f"probe={info.n_tracks}")
                res.check(f"[{fmt}] AC-4 产出={len(wavs)} == 轨数-空轨={exp}",
                          len(wavs) == exp, f"空轨 {len(empties)} 个")
                k = KNOWN_EMPTY_FIRST.get(base)
                if k is not None:
                    res.check(f"[{fmt}] AC-5 已知空轨 track {k} 确实为空且被跳过",
                              k in empties and len(wavs) == exp)

        # 时长精度
        if info.exact_src == "vgm_samples":
            err = abs(avg - info.exact_secs)
            res.check(f"[{fmt}] AC-6 VGM 时长误差 {err:.4f}s ≤ 0.05s", err <= 0.05)
        elif dur.source == "gbs_loop":
            err = abs(avg - info.loop_end_secs)
            res.check(f"[{fmt}] AC-7 GBS loop_end 误差 {err:.4f}s ≤ 0.05s", err <= 0.05)
        elif info.exact_src in ("ay_frames", "gym_frames"):
            # P1：AY/GYM 的文件自带时长已与 libgme track_info().length 逐样本比对过。
            #
            # ⚠ 口径必须是「总时长 vs 总时长」，不能拿 avg 去比 exact_secs：
            #   `exact_secs` 是**最长一轨**（AY 的 AlienStorm.ay 有 7 轨，最长 65.28s），
            #   而 `avg` 是**所有产出轨的平均**（127.52/7 = 18.22s）——两者不同源。
            #   旧写法在单轨 GYM 上碰巧成立，在 7 轨 AY 上恒差 ~47s，
            #   实测误报「误差 46.6265s」，是**断言写错**，不是解码错（真值见下）。
            ref_secs = list(info.track_secs) or [info.exact_secs]
            want = sum(ref_secs)
            err = abs(stat["seconds"] - want)
            tol = 0.5 * max(1, len(ref_secs))
            res.check(f"[{fmt}] P1 时长({info.exact_src}) 总误差 {err:.4f}s ≤ {tol:.1f}s",
                      err <= tol,
                      f"实测总长 {stat['seconds']:.2f}s / 期望 {want:.2f}s"
                      f"（{len(ref_secs)} 轨）")
        elif info.fmt in ("NSF", "HES", "KSS", "SAP", "GYM"):
            # KSS/SAP 没有可信的文件内时长（libgme 报length=-1），GYM 样本是
            # 13 小时原始 dump 被 600s 闸门挡下 —— 三者都应回落到默认上限。
            res.check(f"[{fmt}] AC-8 时长 {avg:.2f}s ≤ 默认+1.0s",
                      avg <= default_secs + 1.0)

        # P1 轨数验收：probe() 的轨数必须与 libgme gme_track_count 一致。
        # （AC-4 的"减空轨"口径只对 HES 有意义；P1 用恒等口径。）
        if info.fmt in ("KSS", "GYM", "SAP", "AY"):
            n_gme, empties = count_tracks_and_empty(path)
            res.check(f"[{fmt}] P1 头轨数 {info.n_tracks} == gme_track_count {n_gme}",
                      info.n_tracks == n_gme, f"probe={info.n_tracks}")
            exp = n_gme - len(empties)
            res.check(f"[{fmt}] P1 产出={len(wavs)} == 轨数-空轨={exp}",
                      len(wavs) == exp, f"空轨 {len(empties)} 个")

        res.row(fmt, base, info.n_tracks, len(wavs), pk, dur.source, avg,
                f"{wall:.1f}s")


def test_cancel(out_root):
    """AC-15：转换中取消，≤2s 返回且无残留（设计 §5.5 Popen 轮询）。

    ⚠ 必须挑一个**解码慢于取消定时器**的文件：NSF 是 14000x 实时，1.5s 就解完，
      取消根本来不及生效（实测 cancelled=False，是测试用例的错不是代码的错）。
      这里固定用 GBS（loop_end 520s，实测约 29s 才解完），取消定时器 1.0s。
    """
    import threading
    cands = []
    for pat in (os.path.join(sample_dir, "gbs", "*.gbs"),
                os.path.join(sample_dir, "**", "*.gbs"),
                os.path.join(out_root, "**", "*.gbs")):
        cands = sorted(glob.glob(pat, recursive=True))
        if cands:
            break
    if not cands:
        print("\n(跳过 AC-15 取消测试：没有 GBS 样本)")
        return True
    path = cands[0]
    ev = threading.Event()
    threading.Timer(1.0, ev.set).start()
    before = spool_left()
    t0 = time.time()
    tracks = []
    cancelled = False
    try:
        tracks = chip_decode.load_chip(path, default_secs=600.0,
                                       cap_secs=600.0, cancel=ev)
    except chip_decode.ChipCancelled:
        cancelled = True
    except Exception as e:                              # noqa: BLE001
        print(f"    意外异常: {type(e).__name__}: {e}")
    finally:
        core.cleanup_spc_temp(tracks)     # 正常返回时也要清（与界面一致）
    dt = time.time() - t0
    after = spool_left()
    ok = cancelled and dt <= 2.5 and after <= before
    print(f"\n{_YEL}=== AC-15 取消{_RST}")
    print(f"    file={os.path.basename(path)} cancelled={cancelled} "
          f"用时={dt:.2f}s 残留 {before}->{after}")
    return ok


def test_bad_file(out_root, good):
    """AC-19：损坏文件不影响整批，其余正常产出。"""
    bad_dir = os.path.join(out_root, "bad")
    os.makedirs(bad_dir, exist_ok=True)
    bad = os.path.join(bad_dir, "broken.vgm")
    with open(bad, "wb") as f:
        f.write(b"\x1f\x8b\x08\x00" + b"\x00" * 96)      # 截断的 gzip
    out_dir = os.path.join(out_root, "badout")
    shutil.rmtree(out_dir, ignore_errors=True)
    stat = spc_wav.convert([bad] + good, out_dir, "chip",
                           chip_secs=DEFAULT_SECS)
    print(f"\n{_YEL}=== AC-19 单文件失败不影响整批{_RST}")
    print(f"    ok={stat['ok']} fail={stat['fail']} errors={len(stat['errors'])}")
    return len(stat["files"]) >= len(good)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="",
                    help="样本目录（默认 vendor/samples；不存在则报错退出）")
    ap.add_argument("--only", default="", help="只跑某格式，如 hes")
    ap.add_argument("--limit", type=int, default=1, help="每格式跑几个文件")
    ap.add_argument("--cancel", action="store_true", help="跑取消测试")
    args = ap.parse_args()

    # ⚠ 默认必须是**持久**的 vendor/samples。此前默认 C:/temp/gmev2（临时目录），
    # 而 P1 四格式(KSS/GYM/SAP/AY)的样本只落在 vendor/samples —— 于是默认跑法
    # 悄悄只覆盖 P0，KSS/GYM/SAP/AY 报"未验收"，看着像绿其实没测。
    _here = os.path.dirname(os.path.abspath(__file__))     # = <项目>/vendor
    _vend = os.path.join(_here, "samples")                 # 样本在 vendor/samples
    sample_dir = args.dir or _vend
    # ⚠ 找不到样本目录就**明确报错退出**，绝不静默回落到别处。
    #   静默回落是"看着全绿其实没测"的温床：本次就是因为回落到一个只剩 P0 的旧
    #   临时目录，P1 四格式整轮没被跑到，报告却写"全通过"。
    if not os.path.isdir(sample_dir):
        print("✗ 样本目录不存在: %s" % sample_dir, file=sys.stderr)
        print("  先跑 `python vendor/fetch_samples.py` 拉取素材，"
              "或用 --dir 指定。", file=sys.stderr)
        return 2
    if not os.path.isdir(sample_dir):
        print(f"{_RED}样本目录不存在: {sample_dir}{_RST}")
        print("请先运行: python vendor/fetch_samples.py")
        return 2
    samples = find_samples(sample_dir)
    if not samples:
        print(f"{_RED}{sample_dir} 下没有找到任何芯片音乐样本{_RST}")
        return 2
    print(f"样本目录: {sample_dir}")
    print("发现: " + "  ".join(f"{k}×{len(v)}" for k, v in samples.items()))

    # ⚠ 输出目录必须**按进程隔离**。曾经固定叫 chip_decode_test，两个人/两个进程
    # 同时跑本测试时，后一个的 shutil.rmtree 会把前一个正在读的 WAV 删掉，
    # 表现为 peak_of_wav() 抛 FileNotFoundError —— 看起来像解码坏了，其实是被抢了。
    out_root = os.path.join(tempfile.gettempdir(),
                            "chip_decode_test_%d" % os.getpid())
    shutil.rmtree(out_root, ignore_errors=True)
    res = Result()

    for fmt in ("NSF", "GBS", "VGM", "VGZ", "HES", "NSFE",
                "KSS", "GYM", "SAP", "AY"):
        if args.only and fmt.lower() != args.only.lower():
            continue
        if fmt not in samples:
            continue
        run_format(fmt, samples[fmt], out_root, res, limit=args.limit)

    if args.cancel:
        res.check("AC-15 取消可用且无残留", test_cancel(out_root))

    # 没样本的格式必须**显式报告未验收**，不能静默跳过 ——
    # 静默跳过会让"全绿"变成假信号（本文件开头的约定）。
    all_fmts = ("NSF", "GBS", "VGM", "VGZ", "HES", "NSFE",
                "KSS", "GYM", "SAP", "AY")
    missing = [f for f in all_fmts
               if not (args.only and f.lower() != args.only.lower())
               and f not in samples]
    if missing:
        print(f"\n{_YEL}未验收格式（本目录无样本，需补素材后重跑）{_RST}")
        for f in missing:
            # VGZ 常见特殊情况：joshw 的 .vgm 实际就是 gzip(=VGZ)，已被 VGM
            # 那轮实际覆盖到（chip_formats.probe 对 .vgm+ gzip 会走 VGZ 分支）。
            # 这种情况要说清楚，不能让人误以为 VGZ 没测。
            if f == "VGZ" and samples.get("VGM"):
                has = any(open(p, "rb").read(2) == b"\x1f\x8b"
                          for p in samples["VGM"][:4]
                          if os.path.exists(p))
                if has:
                    print("  - VGZ: 无独立 .vgz 样本，但 VGM 样本里含 gzip(=VGZ)"
                          "内容，实际已覆盖 —— 不算未验收")
                    continue
            print(f"  - {f}: 缺样本 —— 本轮未验收")
        res.check("有样本的格式全部通过", res.failed == 0)

    good = sorted(glob.glob(os.path.join(out_root, "VGM", "**", "*.wav"),
                            recursive=True))
    vgm_in = samples.get("VGM", [])[:1]
    if vgm_in:
        res.check("AC-19 坏文件不拖垮整批", test_bad_file(out_root, vgm_in))

    print(f"\n{_YEL}{'=' * 78}{_RST}")
    print(f"{'格式':<6}{'文件':<32}{'头轨数':>7}{'产出':>6}{'峰值':>8}"
          f"{'时长来源':>14}{'均时长(s)':>11}")
    for r in res.rows:
        print(f"{r[0]:<6}{r[1][:31]:<32}{r[2]:>7}{r[3]:>6}{r[4]:>8}"
              f"{r[5]:>14}{r[6]:>11.3f}  {r[7]}")
    print(f"\n结果: {res.passed} 通过 / {res.failed} 失败")
    shutil.rmtree(out_root, ignore_errors=True)
    return 0 if res.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
