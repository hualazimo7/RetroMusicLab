"""v5.9.11 门禁：PS3 / PSP / PS Vita / GameCube / Wii / Nintendo 3DS 六个机种。

## 为什么要有这个脚本

这一轮加的不是「解码器」，而是**分派**。五个机种靠 FFmpeg 现成解、GameCube
靠 ``pivotal_psf`` 原生解 —— 每条解码路径本身都单独验过了（见
``_probe_new6_pyav.py`` 与 ``_check_pivotal_ref.py``）。这里验的是**接线**：

* ``classify()`` 把包归到哪个「格式」列；
* ``policy_for()`` 给下载页的是哪条策略；
* ``is_convertible()`` 放不放行；
* 真正调 ``convert_one()`` 能不能落下 WAV，产物是不是 44100/16bit/立体声；
* ``.psf`` 撞名有没有判错（PS1 的走 BIOS，GameCube 的走纯 Python）。

⚠ 最后一条是本轮最危险的坑：判错时症状是「能解的包报 BIOS 有问题」，
  用户会去翻 BIOS 目录白忙。

## 用法

    python vendor/_e2e_v5911_platforms.py
    python vendor/_e2e_v5911_platforms.py --root D:/MSU1Downloads/joshw

缺某个机种的样本包时**跳过**（不算失败）—— 门禁要在没有素材的机器上也能跑。
"""

import argparse
import os
import shutil
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_ROOT = r"D:\MSU1Downloads\joshw"

#: (机种目录, 取哪个后缀当样本, 期望的 policy)
PYAV_CASES = [
    ("PlayStation 3", ".xvag", "stream"),
    ("PlayStation 3", ".at3", "stream"),
    ("PSP", ".hca", "stream"),
    ("PSP", ".at3", "stream"),
    ("Wii", ".brstm", "stream"),
    ("Nintendo 3DS", ".laac", "stream"),
    # v5.9.11 补：PS Vita。原先以为这个机种「包里没有音频」，是判断错了 ——
    # .at9（ATRAC9）和 .vag（PSX-ADPCM）都在，FFmpeg 认前者、后者走原生解码。
    ("PS Vita", ".at9", "stream"),
    ("PS Vita", ".vag", "stream"),
]

PASS, FAIL, SKIP = [], [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print("  [%s] %s" % ("OK" if cond else "!!", msg))
    return bool(cond)


def first_pkg(root, sub):
    d = os.path.join(root, sub)
    if not os.path.isdir(d):
        return ""
    names = sorted(os.listdir(d))
    return os.path.join(d, names[0]) if names else ""


def find_ext(folder, ext):
    for dirpath, _dn, fns in os.walk(folder):
        for n in sorted(fns):
            if n.lower().endswith(ext):
                return os.path.join(dirpath, n)
    return ""


def wav_info(path):
    with wave.open(path, "rb") as w:
        return (w.getnchannels(), w.getsampwidth(), w.getframerate(),
                w.getnframes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    a = ap.parse_args()

    import chip_formats as cf
    import convert_job as cj
    import dl_convert as dc
    import joshw_dl
    import psx_txtp
    import stream_audio as sa

    tmp = tempfile.mkdtemp(prefix="v5911_")
    try:
        # ── ① 扩展名清单 ────────────────────────────────────────────────
        print("\n[1] 扩展名清单")
        for e in (".xvag", ".at3", ".hca", ".brstm", ".laac"):
            ck(e in cf.ALL_EXTS, "chip_formats.ALL_EXTS 含 %s" % e)
            ck(e in cj.STREAM_EXTS, "convert_job.STREAM_EXTS 含 %s" % e)
        ck(".txtp" in cf.ALL_EXTS, "ALL_EXTS 含 .txtp（PS1/GC 包的正常入口）")
        ck(set(sa.ALL_EXTS) <= set(cf.ALL_EXTS),
           "stream_audio.ALL_EXTS 全部并进 chip_formats.ALL_EXTS")

        # ── ② 五个 PyAV 机种：真转一首 ──────────────────────────────────
        print("\n[2] PyAV 机种端到端（真转 WAV）")
        for sub, ext, want in PYAV_CASES:
            pkg = first_pkg(a.root, sub)
            src = find_ext(pkg, ext) if pkg else ""
            if not src:
                SKIP.append("%s %s（没有样本）" % (sub, ext))
                print("  [--] %s %s：没有样本，跳过" % (sub, ext))
                continue
            tag = "%s %s" % (sub, ext)
            ck(sa.is_stream(src), "%s：stream_audio 认这个后缀" % tag)
            ck(dc.policy_for(src) == want,
               "%s：policy_for = %r" % (tag, want))
            ck(dc.is_convertible(src), "%s：is_convertible 放行" % tag)
            pr = sa.probe(src)
            ck(pr["ok"] and pr["secs"] > 0.5,
               "%s：probe ok，%.2f 秒 %d Hz %dch（%s）"
               % (tag, pr["secs"], pr["rate"], pr["channels"], pr["codec"]))
            out = os.path.join(tmp, tag.replace(" ", "_").replace(".", ""))
            st = sa.convert_one(src, out, used=set(), start_idx=0)
            files = st.get("files") or []
            if not ck(bool(files) and os.path.isfile(files[0]),
                      "%s：convert_one 产出 %d 个文件" % (tag, len(files))):
                continue
            nch, sw, rate, nfr = wav_info(files[0])
            ck((nch, sw, rate) == (2, 2, 44100),
               "%s：产物 2ch/16bit/44100（实际 %dch/%dbit/%dHz）"
               % (tag, nch, sw * 8, rate))
            ck(nfr > rate * 0.5, "%s：产物 %d 帧（%.1f 秒）"
               % (tag, nfr, nfr / float(rate)))

        # ── ③ GameCube：Pivotal PSF（.txtp 包）──────────────────────────
        print("\n[3] GameCube 的 Pivotal PSF")
        gcpkg = first_pkg(a.root, "GameCube")
        gcpsf = find_ext(gcpkg, ".psf") if gcpkg else ""
        if not gcpsf:
            SKIP.append("GameCube（没有样本）")
            print("  [--] GameCube：没有样本，跳过")
        else:
            import pivotal_psf
            ck(pivotal_psf.is_pivotal(gcpsf),
               "GC 的 .psf 被认成 Pivotal（第 4 字节 0x%02X）"
               % open(gcpsf, "rb").read(4)[3])
            ck(cj.classify(gcpsf) == "GameCube 音频",
               "classify 给的是「GameCube 音频」而不是「PSF」")
            ck(dc.policy_for(gcpsf) == "pivotal",
               "policy_for 给的是 pivotal（不是 psf —— 那个要 BIOS）")
            ck(pivotal_psf.probe(gcpsf)["ok"], "probe ok")
            st = pivotal_psf.convert_one(gcpsf, os.path.join(tmp, "gc_psf"),
                                         used=set(), start_idx=0)
            ck(bool(st.get("files")), "pivotal_psf.convert_one 产出文件")

            # 整个 .txtp 包（24 首）—— 下载页走的就是这条路
            txtps = [n for n in os.listdir(gcpkg) if n.lower().endswith(".txtp")]
            if txtps:
                ck(cj.classify(gcpkg) == "GameCube 音频",
                   "整个 .txtp 目录 classify 成「GameCube 音频」")
                st = psx_txtp.convert(
                    [os.path.join(gcpkg, n) for n in sorted(txtps)],
                    os.path.join(tmp, "gc_all"), start_idx=0)
                ck(st["ok"] == len(txtps),
                   "24 个 .txtp 全部转出（成功 %d / 失败 %d）"
                   % (st["ok"], st["fail"]))
                if st.get("files"):
                    nch, sw, rate, nfr = wav_info(st["files"][0])
                    ck((nch, sw, rate) == (2, 2, 44100),
                       "GC 产物 2ch/16bit/44100")

        # ── ④ .psf 撞名：PS1 的必须还是 PS1 ────────────────────────────
        print("\n[4] .psf 撞名分流")
        import pivotal_psf

        # 先做**合成头**的判定表 —— 不依赖任何素材，任何机器都能跑。
        # PS1/PS2 的版本字节必须全部判给 PSF 家族（那才需要 BIOS）；
        # Pivotal 的类型字节必须全部判给 pivotal。
        ps_ver = (0x01, 0x02, 0x11, 0x12, 0x21, 0x22)
        piv = (0x31, 0x40, 0x60, 0x80, 0x81, 0xA1, 0xC0, 0xD1)
        for b in ps_ver:
            fake = os.path.join(tmp, "fake_%02x.psf" % b)
            with open(fake, "wb") as fh:
                fh.write(b"PSF" + bytes([b]) + b"\x00" * 32)
            ck(not pivotal_psf.is_pivotal(fake),
               "版本字节 0x%02X → 判给 PS1/PS2 PSF（要 BIOS）" % b)
        for b in piv:
            fake = os.path.join(tmp, "fake_%02x.psf" % b)
            with open(fake, "wb") as fh:
                fh.write(b"PSF" + bytes([b]) + b"\x00" * 32)
            ck(pivotal_psf.is_pivotal(fake),
               "类型字节 0x%02X → 判给 Pivotal（纯 Python）" % b)
        ck(not pivotal_psf.is_pivotal(os.path.join(tmp, "nope.psf")),
           "文件不存在时 is_pivotal 返回 False（不抛异常）")
        with open(os.path.join(tmp, "notpsf.psf"), "wb") as fh:
            fh.write(b"RIFF" + b"\x00" * 32)
        ck(not pivotal_psf.is_pivotal(os.path.join(tmp, "notpsf.psf")),
           "魔数不是 PSF 时 is_pivotal 返回 False")

        ps1 = ""
        for sub in ("PlayStation", "PlayStation 2"):
            pkg = first_pkg(a.root, sub)
            if pkg:
                ps1 = find_ext(pkg, ".psf")
                if ps1:
                    break
        if ps1:
            ck(not pivotal_psf.is_pivotal(ps1),
               "PS1 的 .psf 不被认成 Pivotal（第 4 字节 0x%02X）"
               % open(ps1, "rb").read(4)[3])
            ck(dc.policy_for(ps1) == "psf",
               "PS1 的 .psf 仍走 psf（要 BIOS）那条")
            ck(cj.classify(ps1) == "PSF", "PS1 的 .psf 仍显示「PSF」")
        else:
            SKIP.append("PS1 样本（.psf 撞名对照）")
            print("  [--] 没有 PS1 的 .psf 样本，跳过撞名对照")

        # ── ⑤ 负载识别（防止重复下载）──────────────────────────────────
        print("\n[5] 负载识别 has_payload 的扩展名表")
        for e in (".xvag", ".at3", ".hca", ".brstm", ".laac", ".at9", ".vag",
                  ".txtp"):
            ck(e in joshw_dl._PAYLOAD_EXTS,
               "joshw_dl._PAYLOAD_EXTS 含 %s" % e)
        for sub in ("PlayStation 3", "PSP", "GameCube", "Wii", "Nintendo 3DS",
                    "PS Vita"):
            pkg = first_pkg(a.root, sub)
            if not pkg:
                continue
            hits = [p for p in joshw_dl._snapshot_dir(pkg)
                    if joshw_dl._is_payload_file(p)]
            ck(bool(hits), "%s：%d 个产物被认作音源负载" % (sub, len(hits)))

        # ── ⑥ 说明文件不该算「转不了」───────────────────────────────────
        print("\n[6] 索引/说明文件不计入「转不了」")
        ck(".txth" in dc.NON_AUDIO_EXTS,
           "dl_convert.NON_AUDIO_EXTS 含 .txth（3DS 的参数说明）")
        ck(".m3u" in dc.NON_AUDIO_EXTS, "NON_AUDIO_EXTS 含 .m3u")
        ck(".txtp" not in dc.NON_AUDIO_EXTS,
           ".txtp **不**在 NON_AUDIO_EXTS 里（它是曲目入口）")

        # ── ⑦ PS Vita：.txtp 索引指向 .at9 / .vag ───────────────────────
        #
        # 这一段是 v5.9.11 收尾时补的。上一轮把这个机种判成「包里没有音频」，
        # **判错了** —— 60 个 .at9 + 2 个 .vag 都在 ``root/assets_psvita/`` 下面，
        # 只是顶层只看得见 .txtp 索引。判错的代价是整个机种白丢，所以这里
        # 用**真实的索引链**（.txtp → 目标文件 → 解码）把它钉住。
        print("\n[7] PS Vita：.txtp 索引 → .at9 / .vag")
        psv = first_pkg(a.root, "PS Vita")
        if not psv:
            SKIP.append("PS Vita（没有样本包）")
            print("  [--] 没有 PS Vita 样本包，跳过")
        else:
            txtps = [p for p in joshw_dl._snapshot_dir(psv)
                     if p.lower().endswith(".txtp")]
            ck(bool(txtps), "PS Vita 包里有 %d 个 .txtp 索引" % len(txtps))
            # 至少各挑一个指向 .at9 和 .vag 的，两种编码都要过
            picks, seen = [], set()
            for p in txtps:
                info = psx_txtp.parse_txtp(p)
                ext = os.path.splitext(info.get("target") or "")[1].lower()
                if info.get("ok") and ext and ext not in seen:
                    seen.add(ext)
                    picks.append((p, ext))
            ck({e for _, e in picks} >= {".at9", ".vag"},
               "索引覆盖 .at9 与 .vag 两种目标（实际 %s）"
               % sorted(e for _, e in picks))
            for p, ext in picks[:4]:
                tag = "PS Vita %s" % ext
                pr = psx_txtp.probe_txtp(p)
                ck(pr["ok"] and pr["seconds"] > 0.5,
                   "%s：probe_txtp ok，%.2f 秒（%s）"
                   % (tag, pr["seconds"], pr["kind"]))
                d = psx_txtp.decode_pcm(p)
                ck(d["ok"] and len(d["pcm"]) > 44100,
                   "%s：decode_pcm 出 %d 字节 PCM" % (tag, len(d["pcm"])))
                ck(d["seconds"] > 0.5,
                   "%s：解出来 %.2f 秒" % (tag, d["seconds"]))
            # 整包：66 个索引全转（本机实测 66/66）
            out = os.path.join(tmp, "psv_txtp_out")
            st = psx_txtp.convert(txtps, out, on_log=lambda _s: None)
            ck(int(st["fail"]) == 0,
               "PS Vita：%d 个索引全转，失败 %d"
               % (int(st["ok"]), int(st["fail"])))
            ck(float(st["seconds"]) > 60.0,
               "PS Vita：合计 %.0f 秒音频（%.1f 分钟）"
               % (st["seconds"], st["seconds"] / 60.0))
            wavs = [os.path.join(out, n) for n in sorted(os.listdir(out))
                    if n.lower().endswith(".wav")]
            if wavs:
                nch, sw, rate, nfr = wav_info(wavs[0])
                ck((nch, sw, rate) == (2, 2, 44100),
                   "PS Vita：产物 2ch/16bit/44100（实际 %dch/%dbit/%dHz）"
                   % (nch, sw * 8, rate))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 46)
    print("通过 %d / 失败 %d / 跳过 %d" % (len(PASS), len(FAIL), len(SKIP)))
    for s in SKIP:
        print("  （跳过）%s" % s)
    for f in FAIL:
        print("  ✗ %s" % f)
    print("=" * 46)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
