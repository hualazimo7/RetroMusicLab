#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5.9.12 门禁：PS4 / Xbox 360 / Xbox / Wii U / PC-98 五个机种。

## 为什么要有这个脚本

这一轮和以前每一轮都不同 —— **第一次随包带第三方解码器**（vgmstream）。
所以门禁要验的东西也分两类：

* **接线**（和以前一样）：``classify()`` 归哪个格式列、``policy_for()`` 给
  哪条策略、``is_convertible()`` 放不放行、``convert_one()`` 落不落 WAV。
* **解码器本身**（新的一类）：``vgmstream-cli.exe`` 在不在、**11 个 DLL 齐不齐**、
  ``probe`` 与实转的时长**是否一致**、4 声道降混有没有错位。

⚠ 最后两条是本轮踩过的真坑，写在这里防止回归：

  1. **11 个 DLL 缺一不可**。它们是**载入时**硬依赖（不是用到才加载）：
     只拷 exe + libvorbis.dll → ``exit 127`` 报 ``libspeex-1.dll``；
     补到 8 个（想省掉 FFmpeg 那 4 个约 6MB）→ 仍 ``exit 127``，报
     ``avutil-vgmstream-57.dll``。所以这里逐个文件点名检查。
  2. **4 声道被当立体声**。Wwise 的 .wem 实测 195/224 个是 4 声道
     （FL FR BL BR）。按 ``stereo`` 声明会让 SWR 把「前两个样本当左右、
     后两个当下一帧」—— **输出速度翻倍、音调全错**，而且不报错。
  3. **txtp 的时长字段两个都错**。``numberOfSamples`` 是素材全长
     （裁短的 txtp 会多报 1.15 秒）；``playSamples`` 对 layered 是**各层之和**
     （70 秒的曲子报成 151 秒）。判据是「playSamples ≤ numberOfSamples 才信
     playSamples」。这里用真样本对比 probe 与实转，误差 > 50ms 就失败。

## 用法

    python vendor/_e2e_v5912_platforms.py
    python vendor/_e2e_v5912_platforms.py --root D:/MSU1Downloads/joshw
    python vendor/_e2e_v5912_platforms.py --quick      # 只验接线，不解码

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
CASES = [
    ("PlayStation 4", ".wem", "stream"),
    ("Xbox 360", ".xma", "stream"),
    ("Xbox", ".araw", "stream"),
    ("Wii U", ".fsb", "stream"),
    ("Wii U", ".btsnd", "stream"),
    ("PlayStation 4", ".bik", "stream"),
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
    names = [n for n in sorted(os.listdir(d))
             if os.path.isdir(os.path.join(d, n))]
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


def peak_of(path):
    import numpy as np
    with wave.open(path, "rb") as w:
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype="<i2")
    return int(np.abs(a).max()) if len(a) else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--quick", action="store_true",
                    help="只验接线与解码器存在性，不真解码")
    ap.add_argument("--deep", action="store_true",
                    help="额外跑 PS4 整包 186 个 txtp 全转（约 160 秒，"
                         "会在临时目录里产生并删除 186 个 wav）")
    a = ap.parse_args()

    import chip_formats as cf
    import convert_job as cj
    import dl_convert as dc
    import joshw_dl
    import psx_txtp
    import stream_audio as sa
    import vgm_audio as vg

    tmp = tempfile.mkdtemp(prefix="v5912_")
    try:
        # ── ① 扩展名清单 ────────────────────────────────────────────────
        print("\n[1] 扩展名清单")
        for e in (".wem", ".fsb", ".btsnd", ".xma", ".bik", ".araw"):
            ck(e in cf.ALL_EXTS, "chip_formats.ALL_EXTS 含 %s" % e)
            ck(e in sa.ALL_EXTS, "stream_audio.ALL_EXTS 含 %s" % e)
            ck(e in joshw_dl._PAYLOAD_EXTS,
               "joshw_dl._PAYLOAD_EXTS 含 %s（否则重复下载）" % e)
        ck(".bnk" in dc.NON_AUDIO_EXTS,
           ".bnk 归「非音频」（SoundBank 里没有 DIDX/DATA）")
        # v5.9.13：``.s98`` 从「故意不登记」翻成正向 —— 现在可转（ymfm 核）。
        ck(".s98" in cf.ALL_EXTS and cf.is_chip_ext(".s98"),
           ".s98 登记进 ALL_EXTS / CHIP_EXTS（v5.9.13 起可转）")
        ck(".s98" not in sa.EXT_KIND,
           ".s98 不走流式音频（FFmpeg 没有 YM2608 模拟）")

        # ── ② vgmstream 解码器 + 11 个 DLL ──────────────────────────────
        print("\n[2] vgmstream 解码器与 11 个 DLL")
        ok, why = vg.decoder_ready()
        ck(ok, "vgm_audio.decoder_ready() 可用（%s）"
              % (os.path.basename(why) if ok else why))
        d = vg.app_dir() if hasattr(vg, "app_dir") else ""
        need = ("vgmstream-cli.exe",
                "avcodec-vgmstream-59.dll", "avformat-vgmstream-59.dll",
                "avutil-vgmstream-57.dll", "swresample-vgmstream-4.dll",
                "libatrac9.dll", "libcelt-0061.dll", "libcelt-0110.dll",
                "libg719_decode.dll", "libmpg123-0.dll", "libspeex-1.dll",
                "libvorbis.dll")
        if d:
            miss = [n for n in need if not os.path.isfile(os.path.join(d, n))]
            ck(not miss, "12 个 vgmstream 文件都在 %s" % d, )
            if miss:
                print("       缺：%s" % ", ".join(miss))
            ck(os.path.isfile(os.path.join(d, "vgmstream-COPYING.txt")),
               "ISC 版权声明在（随包分发的唯一义务）")

        # ── ③ 五个机种：真转一首 ────────────────────────────────────────
        print("\n[3] 五个机种端到端（真转 WAV）")
        for sub, ext, want in CASES:
            pkg = first_pkg(a.root, sub)
            src = find_ext(pkg, ext) if pkg else ""
            if not src:
                SKIP.append("%s %s（没有样本）" % (sub, ext))
                print("  [--] %s %s：没有样本，跳过" % (sub, ext))
                continue
            tag = "%s %s" % (sub, ext)
            ck(sa.is_stream(src), "%s：stream_audio 认这个后缀" % tag)
            ck(dc.policy_for(src) == want, "%s：policy_for = %r" % (tag, want))
            ck(dc.is_convertible(src), "%s：is_convertible 放行" % tag)
            pr = sa.probe(src)
            # ⚠ ``.xma`` 的 probe **只能给 0 秒**，这不是 bug：XMA 的容器里
            #   没有 RIFF 级的 sample count（``container.duration`` /
            #   ``stream.duration`` 都是 None、``frames=0``，demux 出来的
            #   30 个包 ``pk.duration`` 全是 0）。所以这里只要求
            #   「采样率/声道数对得上、文件能打开」，时长交给下面的实转去验
            #   —— 时长估算会走 150 秒默认值，方向是安全的（宁长不短）。
            _min_secs = 0.0 if ext == ".xma" else 0.1
            ck(pr["ok"] and pr["secs"] >= _min_secs and pr["rate"] > 0
               and pr["channels"] > 0,
               "%s：probe ok，%.2f 秒 %d Hz %dch（%s）"
               % (tag, pr["secs"], pr["rate"], pr["channels"], pr["codec"]))
            if a.quick:
                continue
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
            ck(nfr > rate * 0.3, "%s：产物 %d 帧（%.1f 秒）"
               % (tag, nfr, nfr / float(rate)))
            # ⚠ 时长必须和 probe 对得上 —— 差太多就是解码器参数错了
            #   （比如漏了 -i，vgmstream 会按「循环 2 遍 + 淡出 10 秒」渲染）。
            #   ``.xma`` 例外：probe 给的是 0，只能验「转出来有东西」。
            _got = nfr / float(rate)
            _want = float(pr["secs"])
            if ext == ".xma":
                ck(_got > 0.5,
                   "%s：实转 %.2fs（probe 无时长可比，只验非空）" % (tag, _got))
            else:
                ck(abs(_got - _want) <= max(0.5, _want * 0.02),
                   "%s：实转 %.2fs vs probe %.2fs（差 %.2fs）"
                   % (tag, _got, _want, abs(_got - _want)))
            pk = peak_of(files[0])
            print("       （峰值 %d）" % pk)

        # ── ④ 4 声道降混（本轮最严重的 bug） ────────────────────────────
        print("\n[4] 4 声道降混（Wwise .wem 有 195/224 个是 4 声道）")
        import numpy as np
        n = 4800
        q = np.zeros((n, 4), dtype=np.int16)
        q[:, 0] = 1000
        q[:, 1] = -1000
        q[:, 2] = 1000
        q[:, 3] = -1000
        o = np.frombuffer(sa._resample_to_out(q.reshape(-1), 48000, 4),
                          dtype="<i2")
        ck(abs(len(o) // 2 - 4410) <= 2,
           "4ch 48000→44100 输出 4410 帧（不是 9600 的翻倍错位）",
           )
        ck(o[0] > 0 and o[1] < 0, "4ch 降混后 L 正 / R 负")

        # ── ⑤ PS4 的 .txtp 序列（真曲目在这 186 个里，不是裸 .wem） ─────
        print("\n[5] PS4 的 .txtp 序列（分层 + 循环 + 裁剪）")
        ps4 = first_pkg(a.root, "PlayStation 4")
        if not ps4:
            SKIP.append("PS4 .txtp（没有样本包）")
            print("  [--] 没有 PS4 样本包，跳过")
        else:
            txtps = [p for p in joshw_dl._snapshot_dir(ps4)
                     if p.lower().endswith(".txtp")]
            ck(bool(txtps), "PS4 包里有 %d 个 .txtp 序列" % len(txtps))
            ck(cj.classify(ps4) == "PS4 音频",
               "整个 .txtp 目录 classify 成「PS4 音频」（不是 PSX）")
            ck(cj._txtp_target_ext(ps4, os.listdir(ps4)) == ".wem",
               "第一个 txtp 的目标是 .wem")
            if txtps:
                pr = psx_txtp.probe_txtp(txtps[0])
                ck(pr["ok"] and pr["seconds"] > 0.5,
                   "probe_txtp ok，%.2f 秒（%s）"
                   % (pr.get("seconds", 0), pr.get("kind")))
                if not a.quick:
                    d2 = psx_txtp.decode_pcm(txtps[0])
                    ck(d2["ok"] and len(d2["pcm"]) > 44100,
                       "decode_pcm 出 %d 字节 PCM"
                       % (len(d2["pcm"]) if d2["ok"] else 0))
                    if d2["ok"]:
                        _got = len(d2["pcm"]) / 4.0 / 44100.0
                        _want = float(pr.get("seconds") or 0)
                        ck(abs(_got - _want) < 0.05,
                           "probe 时长与实转一致（%.2fs vs %.2fs）"
                           % (_want, _got))
                    # 整包全转（v5.9.12 实测 186/186）。
                    #
                    # ⚠ 这一段**必须挂在 ``--deep`` 下面**，不能跟其它断言一样
                    #   默认跑。它会在 ``tmp`` 里落 186 个 wav、再被
                    #   ``shutil.rmtree`` 一次删掉 —— 环境里的 safe-delete
                    #   护栏按「每轮删除次数」计数（阈值 50/100），跑到这里
                    #   就被判成 BULK_CONFIRM_REQUIRED 并**直接终止进程**。
                    #   表现极具误导性：``EXIT=1``、**没有任何 traceback**、
                    #   日志停在上一节末尾（连 ``finally`` 之后的汇总都没有），
                    #   看着像脚本自己崩了，其实是外部把进程掐了。
                    if a.deep:
                        out = os.path.join(tmp, "ps4_all")
                        st = psx_txtp.convert(txtps, out, on_log=lambda _s: None)
                        ck(int(st["fail"]) == 0,
                           "PS4：%d 个序列全转，失败 %d"
                           % (int(st["ok"]), int(st["fail"])))
                        ck(float(st["seconds"]) > 600.0,
                           "PS4：合计 %.0f 秒音频（%.1f 小时）"
                           % (st["seconds"], st["seconds"] / 3600.0))
                        wavs = [os.path.join(out, x)
                                for x in sorted(os.listdir(out))
                                if x.lower().endswith(".wav")]
                        if wavs:
                            nch, sw, rate, nfr = wav_info(wavs[0])
                            ck((nch, sw, rate) == (2, 2, 44100),
                               "PS4：产物 2ch/16bit/44100（实际 %dch/%dbit/%dHz）"
                               % (nch, sw * 8, rate))
                    else:
                        print("  [--] 整包 186 个 txtp 全转（--deep 才跑）")

        # ── ⑥ PC-98 的 .s98（v5.9.13 起可转，走 ymfm 核） ────────────────
        #
        # 上一轮这里是「本轮无解，验证不会假装能转」。现在翻成正向：
        # 真解一首出来，验时长对得上、峰值不为 0。
        #
        # ⚠ 只挑**最小的**那个 .s98 —— 整包 18 首共 1948 秒，全解要 55 秒，
        #   门禁里没这个预算；最短那首 14 秒，0.5 秒就能解完。
        print("\n[6] PC-98 的 .s98（v5.9.13：ymfm 核，真转一首）")
        pc98 = first_pkg(a.root, "PC-98")
        cands = []
        if pc98:
            for dirpath, _dn, fns in os.walk(pc98):
                for n in fns:
                    if n.lower().endswith(".s98"):
                        p_ = os.path.join(dirpath, n)
                        cands.append((os.path.getsize(p_), p_))
        cands.sort()
        s98 = cands[0][1] if cands else ""
        if not s98:
            SKIP.append("PC-98 .s98（没有样本）")
            print("  [--] 没有 .s98 样本，跳过")
        else:
            import numpy as _np
            import s98_native as sn
            ck(dc.is_convertible(s98), ".s98 算可转（v5.9.13 起）")
            ck(dc.policy_for(s98) == "chip", ".s98 的 policy 是 chip")
            ck(not sa.is_stream(s98),
               "stream_audio 不收 .s98（FFmpeg 没有 YM2608 模拟）")
            ck(not vg.probe(s98).get("ok"),
               "vgmstream 打不开 .s98（本来就不该能开）")
            with open(s98, "rb") as fh:
                ck(fh.read(4) == b"S981", "魔数是 S981（PC-98 v1 芯片日志）")
            info = cf.probe(s98)
            ck(info.ok and info.fmt == "S98", ".s98 识别成 S98")
            ck(info.exact_secs > 1.0,
               "chip_formats 算出精确时长 %.2fs" % info.exact_secs)
            ck(info.exact_src == "s98_ticks", "时长来源 s98_ticks")
            ck(bool(info.title), "读到内嵌曲名 %r" % info.title)
            ok, why = sn.decoder_ready()
            ck(ok, "s98_native 找得到 s982wav.exe（%s）" % (why or ""))
            if ok and not a.quick:
                r = sn.decode_track(s98)
                ck(r["ok"], "decode_track 成功（%s）" % (r.get("error") or ""))
                if r["ok"]:
                    secs = r["seconds"]
                    ck(abs(secs - (info.exact_secs + sn.TAIL_SECS)) <= 0.5,
                       "实解 %.2fs = 精确时长 %.2fs + 余音 %.1fs"
                       % (secs, info.exact_secs, sn.TAIL_SECS))
                    arr = _np.frombuffer(r["pcm"], dtype="<i2")
                    pk = int(_np.abs(arr.astype(_np.int32)).max())
                    ck(pk > 500,
                       "有声音（峰值 %d，不是 libgme 那种精确静音）" % pk)
                    ck(len(r["pcm"]) % 4 == 0, "PCM 是 16bit 立体声对齐的")
            elif ok:
                print("  [--] 真解码（--quick 跳过）")

        # ── ⑦ 合成信号：音高对不对 ─────────────────────────────────────
        #
        # 上一节用真实素材，只能证明「有声音、时长对」。音高错半个八度
        # 照样峰值正常、时长正常 —— 那种错**只有拿已知音高比才看得出来**。
        #
        # 这一节不依赖任何外部素材（``_s98_fixture`` 现造一个），
        # 所以在没下过 PC-98 包的机器上也跑。
        print("\n[7] 合成信号：音高（不依赖外部素材）")
        import _s98_fixture as fx
        fx_path = fx.make_s98(os.path.join(tmp, "fixture.s98"))
        fi = cf.probe(fx_path)
        ck(fi.ok and fi.fmt == "S98", "合成的 .s98 能识别")
        ck(abs(fi.exact_secs - fx.EXPECT_SECS) < 0.01,
           "时长 %.3fs（期望 %.3fs，tick 换算无误差）"
           % (fi.exact_secs, fx.EXPECT_SECS))
        ck(fi.title == "Fixture Tone", "内嵌曲名读对了：%r" % fi.title)
        ok2, why2 = sn.decoder_ready()
        if ok2 and not a.quick:
            fr = sn.decode_track(fx_path)
            ck(fr["ok"], "合成样本解码成功（%s）" % (fr.get("error") or ""))
            if fr["ok"]:
                want = fx.EXPECT_SECS + sn.TAIL_SECS
                ck(abs(fr["seconds"] - want) <= 0.05,
                   "时长 %.3fs = %.3fs + 余音 %.1fs（尾部 tick 没丢）"
                   % (fr["seconds"], fx.EXPECT_SECS, sn.TAIL_SECS))
                _a = _np.frombuffer(fr["pcm"], dtype="<i2").astype(_np.float64)
                _l = _a[0::2]
                _pk = int(_np.abs(_a).max())
                ck(_pk > 500, "有声音（峰值 %d）" % _pk)
                # 0.3~0.8 秒是稳定段：跳过起音、避开松键
                seg = _l[int(0.3 * 44100):int(0.8 * 44100)]
                seg = seg - seg.mean()
                win = _np.blackman(len(seg))
                spec = _np.abs(_np.fft.rfft(seg * win))
                freqs = _np.fft.rfftfreq(len(seg), 1 / 44100.0)
                got = float(freqs[spec.argmax()])
                # 容差 1%：FFT 分辨率是 1/0.5s = 2 Hz，440 Hz 上就是 0.45%
                ck(abs(got - fx.EXPECT_HZ) / fx.EXPECT_HZ < 0.01,
                   "音高 %.2f Hz（期望 %.2f Hz）" % (got, fx.EXPECT_HZ))
        elif ok2:
            print("  [--] 真解码（--quick 跳过）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 46)
    print("通过 %d / 失败 %d / 跳过 %d" % (len(PASS), len(FAIL), len(SKIP)))
    for s in SKIP:
        print("  跳过：%s" % s)
    for f in FAIL:
        print("  失败：%s" % f)
    print("=" * 46)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
