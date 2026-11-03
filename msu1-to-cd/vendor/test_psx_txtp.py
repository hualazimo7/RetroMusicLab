# -*- coding: utf-8 -*-
"""v4.41 门禁：PS1 的「.txtp 包」→ WAV 原生解码。

全离线：临时目录里**合成**一个 JoshW 形态的包 ——
标准 CD-XA 扇区文件（``.xa``）、裸 PCM（``.DA`` + ``.DA.TXTH``）、
``.txtp`` 索引、``!tags.m3u`` 曲名表。不读用户的任何真实音源。

覆盖四块：
  ① 索引解析 —— ``.txtp`` 一行里 ``#I`` 是**循环点**不是起点偏移
  ② 时长探测 —— ``.xa`` 按扇区算、``.DA`` 按字节算，都不读音频
  ③ 解码正确性 —— XA 递推、coding 字节不可信、编号 01/02 连续
  ④ 接线 —— ``dl_convert`` 的 policy/is_convertible/estimate/分派

跑法：``python vendor/test_psx_txtp.py``
"""

import os
import shutil
import struct
import sys
import tempfile
import threading
import wave

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import msu1_to_cd as core
import psx_txtp as px

_n_pass = 0
_n_fail = 0


def ok(cond, label, detail=""):
    global _n_pass, _n_fail
    if cond:
        _n_pass += 1
        print("  OK   %s   %s" % (label, detail))
    else:
        _n_fail += 1
        print("  FAIL %s   %s" % (label, detail))


# ---------------------------------------------------------------------------
# 合成素材
# ---------------------------------------------------------------------------

_SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


def _make_xa(path, nsec, coding=0x01, nibble=0x11, submode=0x64):
    """造一个标准 CD-XA 扇区文件（每扇区 2352B）。

    布局：12B sync + 4B header + 8B subheader + 2324B data + 4B EDC。
    数据区实际只用到前 2304 字节（18 sound group × 128B），后 20 字节空着。

    ⚠ v4.42：sound group = ``16B 头 + 112B 数据``；16 字节头是
      ``[sf0..sf3][同一份重复][sf4..sf7][同一份重复]``（8 个子帧各 1 字节）。
      这里 8 个子帧都填 ``0x01`` -> 高半字节 filter=0、低半字节 shift=1；
      数据区全填 ``nibble``。``0x11`` 的两个半字节都是 1，filter=0 不累加，
      于是每声道恒定 ``1<<12>>1 = 2048``（既不静音也不削顶）。
      旧版把 16 字节头按「4 unit × 4 字节、只用 [1]/[2]」排，是错的。

    Args:
        coding: subheader[3]。``0x01`` = 立体声 / 37800Hz / 4bit —— 这是
            **规范值**（v4.42 修正：早先误当成「8bit 单声道」，见 CI 位定义）。
        nibble: 数据区每字节的值。
    """
    # 8 个子帧参数都写 0x01（filter=0 / shift=1），按 [0..3][重复][4..7][重复] 排
    grp = bytes([0x01] * 16) + bytes([nibble]) * 112
    payload = grp * 18                                   # 2304 B
    payload += bytes(2324 - len(payload))                # 补满 2324
    with open(path, "wb") as fh:
        for _ in range(nsec):
            fh.write(_SYNC)
            fh.write(b"\x00\x00\x01\x02")                    # mode=2
            fh.write(bytes([1, 1, submode, coding]) * 2)     # subheader ×2
            fh.write(payload)
            fh.write(b"\x00" * 4)                            # EDC


def _make_da(path, frames=22050, rate=44100, ch=2, amp=3000):
    """造一个裸 PCM16LE 文件（默认 0.5 秒 44.1k 立体声）。"""
    pcm = struct.pack("<%dh" % (frames * ch), *([amp, -amp] * (frames * ch // 2)))
    with open(path, "wb") as fh:
        fh.write(pcm)
    return pcm


def _make_txth(path, rate=44100, ch=2, codec="PCM16LE", n=0):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("codec = %s\nchannels = %d\nsample_rate = %d\n"
                 "num_samples = %d\n" % (codec, ch, rate, n))


def _peak_of(path):
    with wave.open(path, "rb") as w:
        raw = w.readframes(w.getnframes())
        rate = w.getframerate()
        ch = w.getnchannels()
    peak = 0
    for i in range(0, len(raw) - 1, 2):
        v = struct.unpack_from("<h", raw, i)[0]
        if abs(v) > peak:
            peak = abs(v)
    return peak, rate, ch, len(raw)


_tmp = tempfile.mkdtemp(prefix="msu1_txtp_gate_")
try:
    # 扇区数：既要够长（>0.2s，否则 estimate_files 会按 150s 兜底），
    # 又要短（自检跑得快）。40 扇区 = 40×2016/37800 ≈ 2.13s。
    _NSEC = 40
    _XA_SECS = _NSEC * 2016.0 / 37800.0
    pack = os.path.join(_tmp, "PS1 - Test Pack")
    snd = os.path.join(pack, "sound")
    os.makedirs(snd, exist_ok=True)

    _make_xa(os.path.join(snd, "T.xa"), _NSEC)
    _make_da(os.path.join(snd, "T.DA"), frames=22050)
    _make_txth(os.path.join(snd, ".DA.TXTH"), n=22050 * 2)

    with open(os.path.join(pack, "A.txtp"), "w", encoding="utf-8") as fh:
        fh.write("sound/T.xa #I 2153088\n")
    with open(os.path.join(pack, "B.txtp"), "w", encoding="utf-8") as fh:
        fh.write("sound/T.DA\n")
    with open(os.path.join(pack, "!tags.m3u"), "w", encoding="utf-8") as fh:
        fh.write("# %TITLE 曲名 A\nA.txtp\n# %TITLE 曲名 B\nB.txtp\n")

    # -----------------------------------------------------------------------
    print()
    print("=" * 62)
    print("① 索引解析")
    print("=" * 62)

    ok(px.is_txtp("x.txtp") and px.is_txtp("X.TXTP"), "扩展名大小写不敏感")
    ok(not px.is_txtp("x.xa") and not px.is_txtp(""), "非 .txtp 判否")
    ok(not px.is_txtp(None), "None 不炸")

    idx = px.list_txtp(pack)
    ok(len(idx) == 2, "只扫一层、按名排序", "、".join(os.path.basename(i) for i in idx))
    ok([os.path.basename(i) for i in idx] == ["A.txtp", "B.txtp"], "排序稳定")
    ok(px.list_txtp(os.path.join(_tmp, "nope")) == [], "目录不存在 -> 空列表")

    a = px.parse_txtp(os.path.join(pack, "A.txtp"))
    ok(a["ok"], "解析成功")
    ok(a["target"] == os.path.normpath(os.path.join(snd, "T.xa")),
       "目标是绝对路径", os.path.basename(a["target"]))
    # ★ 核心语义：#I 是 install-loop（循环点，单位采样数），**不是**起点偏移。
    #   早先按偏移算会出现负时长（起 4458s 但文件只有 8.9MB）。
    ok(a["loop"] == 2153088, "#I 读成循环点", "2153088 采样")

    b = px.parse_txtp(os.path.join(pack, "B.txtp"))
    ok(b["ok"] and b["loop"] == 0, "无 #I 时 loop=0")

    open(os.path.join(pack, "empty.txtp"), "w").close()
    e = px.parse_txtp(os.path.join(pack, "empty.txtp"))
    ok(not e["ok"] and "空" in e["error"], "空索引 -> 明确报错", e["error"])
    e2 = px.parse_txtp(os.path.join(_tmp, "nope.txtp"))
    ok(not e2["ok"] and e2["error"], "不存在 -> 明确报错", e2["error"])

    # 反斜杠/正斜杠混写：包内 .txtp 常用 /，Windows 上必须换掉
    with open(os.path.join(pack, "C.txtp"), "w", encoding="utf-8") as fh:
        fh.write("sound/T.DA\n")
    c = px.parse_txtp(os.path.join(pack, "C.txtp"))
    ok(c["ok"] and os.sep in c["target"], "分隔符已换成平台默认")

    titles = px.load_titles(pack)
    ok(titles.get("A.txtp") == "曲名 A", "曲名表 A", titles.get("A.txtp", "?"))
    ok(titles.get("B.txtp") == "曲名 B", "曲名表 B", titles.get("B.txtp", "?"))
    ok(px.load_titles(_tmp) == {}, "没有 !tags.m3u -> 空 dict")

    # -----------------------------------------------------------------------
    print()
    print("=" * 62)
    print("② 时长探测（不读音频）")
    print("=" * 62)

    pa = px.probe_txtp(os.path.join(pack, "A.txtp"))
    # 每扇区每声道 2016 采样、37800 Hz
    ok(pa["ok"] and pa["kind"] == "XA", "XA 识别", pa["kind"])
    ok(abs(pa["seconds"] - _XA_SECS) < 1e-6, "XA 时长",
       "%.4fs" % pa["seconds"])
    ok(pa["size"] == _NSEC * 2352, "XA 体积", "%d B" % pa["size"])

    pb_ = px.probe_txtp(os.path.join(pack, "B.txtp"))
    ok(pb_["ok"] and pb_["kind"] == "CD-DA", "裸 PCM 识别", pb_["kind"])
    ok(abs(pb_["seconds"] - 0.5) < 1e-6, "裸 PCM 时长", "%.4fs" % pb_["seconds"])

    # 目标不存在 -> 报错而不是 0
    with open(os.path.join(pack, "MISS.txtp"), "w", encoding="utf-8") as fh:
        fh.write("sound/NOPE.xa\n")
    pm = px.probe_txtp(os.path.join(pack, "MISS.txtp"))
    ok(not pm["ok"] and "不存在" in pm["error"], "目标缺失 -> 报错", pm["error"])

    # 不认识的目标类型
    open(os.path.join(snd, "T.minipsf"), "wb").write(b"PSF\x01")
    with open(os.path.join(pack, "PSF.txtp"), "w", encoding="utf-8") as fh:
        fh.write("sound/T.minipsf\n")
    pp = px.probe_txtp(os.path.join(pack, "PSF.txtp"))
    ok(not pp["ok"] and ".xa" in pp["error"], "只认 .xa/.DA", pp["error"])

    # .DA.TXTH 参数不是 44.1k 立体声 -> 解的时候要明确拒绝（不能硬塞 WAV）
    _make_da(os.path.join(snd, "MONO.DA"), frames=1000, ch=1)
    _make_txth(os.path.join(snd, ".DA.TXTH"), ch=1, n=1000)
    try:
        px._decode_da(os.path.join(snd, "MONO.DA"))
        ok(False, "非 44.1k 立体声要拒绝", "居然没报错")
    except RuntimeError as ex:
        ok("44100Hz" in str(ex), "非 44.1k 立体声要拒绝", str(ex)[:40])
    # 还原正确的 TXTH，后面的转换还要用
    _make_txth(os.path.join(snd, ".DA.TXTH"), n=22050 * 2)

    # -----------------------------------------------------------------------
    print()
    print("=" * 62)
    print("③ 解码与落盘")
    print("=" * 62)

    # ---- XA 递推：filter=0/shift=1 -> 恒定 2048（既不静音也不削顶）----
    # ⚠ 必须直接看 _xa_decode_group 的**重采样前**输出：_decode_xa 会把
    #   37800Hz 重采样到 44100Hz，重采样后不再恒定，断言会假失败。
    groups = core.xa_sector_groups(os.path.join(snd, "T.xa"))
    ok(len(groups) == 1, "只认出一条 XA 流", "%d 条" % len(groups))
    offs = list(groups.values())[0]
    ok(len(offs) == _NSEC, "扇区数", "%d" % len(offs))
    l2, r2 = core._xa_decode_group(os.path.join(snd, "T.xa"), offs)
    ok(len(l2) == _NSEC * 2016 and len(r2) == _NSEC * 2016,
       "每声道采样数 = 扇区×2016", "%d" % len(l2))
    ok(all(v == 2048 for v in l2) and all(v == 2048 for v in r2),
       "XA 递推值恒定 2048", "first=%d" % l2[0])

    # 重采样后的字节数：nsec×2016 采样/声道 @37800 -> @44100，再 ×2 声道 ×2 字节
    _exp_bytes = int(round(_NSEC * 2016 * 44100.0 / 37800.0)) * 2 * 2
    raw = px._decode_xa(os.path.join(snd, "T.xa"))
    ok(abs(len(raw) - _exp_bytes) <= 8, "重采样到 44.1k 后字节数",
       "%d ≈ %d" % (len(raw), _exp_bytes))
    vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
    ok(max(vals) > 0, "重采样后仍非静音", "peak=%d" % max(vals))

    # ★ 解码器不看 coding 位：0x01 已是「立体声/37800/4bit」，但真实包里
    #   也有 0x09 这类非常规值，必须照样按 4bit 立体声解。
    #   判据是**每声道采样数 = 扇区×2016** —— 若按 8bit 单声道解，长度会差 4 倍。
    _make_xa(os.path.join(snd, "C9.xa"), 2, coding=0x09)
    g9 = core.xa_sector_groups(os.path.join(snd, "C9.xa"))
    l9, r9 = core._xa_decode_group(os.path.join(snd, "C9.xa"),
                                   list(g9.values())[0])
    ok(len(l9) == 2 * 2016, "coding=0x09 仍按 4bit 立体声解",
       "%d 采样/声道" % len(l9))

    rawda = px._decode_da(os.path.join(snd, "T.DA"))
    ok(len(rawda) == 22050 * 4, "裸 PCM 直通字节数", "%d" % len(rawda))

    # 截断的 .DA（奇数字节）要被砍到 4 的倍数，否则 WAV 写出来是噪音
    with open(os.path.join(snd, "ODD.DA"), "wb") as fh:
        fh.write(b"\x01\x02\x03\x04\x05\x06\x07")       # 7 字节
    _make_txth(os.path.join(snd, ".DA.TXTH"), n=1)
    odd = px._decode_da(os.path.join(snd, "ODD.DA"))
    ok(len(odd) == 4, "奇数字节截到 4 的倍数", "%d 字节" % len(odd))
    _make_txth(os.path.join(snd, ".DA.TXTH"), n=22050 * 2)

    # 取消要能立刻生效
    ev = threading.Event()
    ev.set()
    try:
        px._decode_xa(os.path.join(snd, "T.xa"), cancel=ev)
        ok(False, "cancel 已置位 -> 抛「已取消」", "居然没抛")
    except RuntimeError as ex:
        ok("取消" in str(ex), "cancel 已置位 -> 抛「已取消」")

    # ---- 真转一遍 ----
    out = os.path.join(_tmp, "WAV")
    logs = []
    st = px.convert([os.path.join(pack, "A.txtp"), os.path.join(pack, "B.txtp")],
                    out, on_log=logs.append)
    ok(st["ok"] == 2 and st["fail"] == 0, "转换 ok=2 fail=0", str(st["fail"]))
    ok(abs(st["seconds"] - (_XA_SECS + 0.5)) < 0.05, "累计秒数",
       "%.2fs" % st["seconds"])
    names = sorted(os.listdir(out))
    ok(len(names) == 2, "产出 2 个 WAV", "、".join(names))
    # ★ v4.41 修过的 bug：write_tracks 的 last_idx 已经是最终序号，
    #   外面再 +1 会让 01 之后直接跳 03。
    ok(names[0].startswith("01_"), "第一个是 01", names[0])
    ok(names[1].startswith("02_"), "第二个是 02（不是 03）", names[1])
    ok("曲名 A" in names[0], "曲名表生效", names[0])
    ok(st["last_idx"] == 2, "last_idx=2", str(st["last_idx"]))

    peak, rate, ch, nbytes = _peak_of(os.path.join(out, names[0]))
    ok(rate == 44100 and ch == 2, "输出 44.1k/2ch", "%dHz/%dch" % (rate, ch))
    ok(peak > 0, "输出不是静音", "peak=%d" % peak)

    # 续编：再来一批要从 03 接下去，不能覆盖 01/02
    st2 = px.convert([os.path.join(pack, "A.txtp")], out, start_idx=2)
    names2 = sorted(os.listdir(out))
    ok(len(names2) == 3, "续编不覆盖", "、".join(names2))
    ok(any(n.startswith("03_") for n in names2), "第三批从 03 开始")
    ok(os.path.getsize(os.path.join(out, names[0])) > 44,
       "旧文件仍是完整 WAV（不是 0 字节）")

    # 非 .txtp 传进来要被忽略，不能当曲目处理
    st3 = px.convert([os.path.join(pack, "!tags.m3u")], os.path.join(_tmp, "W3"))
    ok(st3["ok"] == 0 and st3["fail"] == 0, "非 .txtp 直接忽略")

    # 目标缺失要记 fail 而不是崩
    st4 = px.convert([os.path.join(pack, "MISS.txtp")], os.path.join(_tmp, "W4"))
    ok(st4["fail"] == 1 and st4["errors"], "目标缺失 -> fail=1", st4["errors"][0][:40])

    # -----------------------------------------------------------------------
    print()
    print("=" * 62)
    print("④ 接线：dl_convert")
    print("=" * 62)

    import dl_convert as dc

    ok(dc.psx_txtp is not None, "dl_convert 已导入 psx_txtp")
    ok(dc.policy_for(os.path.join(pack, "A.txtp")) == "txtp", "policy=txtp")
    ok(dc.policy_for("x.spc") == "id666", "spc 仍是 id666（没被抢走）")
    ok(dc.policy_for("x.nsf") == "chip", "nsf 仍是 chip")
    ok(dc.is_convertible(os.path.join(pack, "A.txtp")), ".txtp 可转")
    # v5.2.4 起独立 .XA **可转** —— JoshW 那种「15 个 .XA + 一个 .PSF」的
    # 平铺包没有 .txtp 索引，不放行就只转得出 PSF 那一首（用户报障原话：
    # 「只有菜单音乐」）。有 .txtp 的包里 .xa 不单独收，那是**收集层**的事
    # （本文件 ⑤ 段的 _list_payload、Web 版 scan_dir_sources），不是这里。
    ok(dc.is_convertible(os.path.join(snd, "T.xa")), ".xa 可转（v5.2.4 独立 XA）")
    ok(not dc.is_convertible(os.path.join(pack, "!tags.m3u")), "!tags.m3u 不可转")

    # 估算必须走真实时长，不能退回 150s 兜底
    est = dc.estimate_files([os.path.join(pack, "A.txtp")], 150.0)
    ok(est["tracks"] == 1, "估算 1 首", str(est["tracks"]))
    ok(abs(est["secs"] - _XA_SECS) < 0.02, "估算用真实时长（不是 150s）",
       "%.2fs" % est["secs"])

    # 分派：_convert_txtp 要真的产出
    wout = os.path.join(_tmp, "W5")
    worker = dc.ConvertWorker(wout, lambda *_a: None, threading.Event())
    sub = worker._convert_txtp(os.path.join(pack, "A.txtp"), [], threading.Event())
    ok(sub["ok"] == 1 and sub["fail"] == 0, "_convert_txtp 产出 1 首",
       "ok=%s" % sub["ok"])
    ok(sorted(os.listdir(wout))[0].startswith("01_"), "编号从 01 起")
    worker.cancel()

    # -----------------------------------------------------------------------
    print()
    print("=" * 62)
    print("⑤ 接线：GUI")
    print("=" * 62)

    import msu1_gui as g_ui

    ok(g_ui.psx_txtp is not None, "GUI 已导入 psx_txtp")
    ok(g_ui.APP_VER >= "4.41", "APP_VER 已升", g_ui.APP_VER)
    ok(hasattr(g_ui, "run_txtp_selftest"), "有 --txtp-selftest 入口")

    # _list_payload 必须把 .txtp 收进来（用户的包根目录就是 41 个 .txtp）。
    # 这里刻意有 6 个：4 个正常 + MISS/PSF 两个坏索引 + empty（空文件）。
    # **坏索引也要收** —— 它们是「能识别但转不动」，要在转换时给出具体原因，
    # 而不是在列表阶段被静默吞掉（那正是用户报障时的体验）。
    listed = g_ui.DownloadDialog._list_payload(pack)
    ok(len(listed) == 6, "_list_payload 收到全部 .txtp",
       "、".join(os.path.basename(x) for x in listed))
    ok(not any(x.lower().endswith(".xa") for x in listed),
       "sound/ 里的 .xa 不被单独收（必须靠 .txtp 索引）")

    # 提示文案三档：PSF / .txtp 目标缺失 / 其它
    psfpack = os.path.join(_tmp, "psfpack")
    os.makedirs(psfpack, exist_ok=True)
    for i in range(3):
        open(os.path.join(psfpack, "T%d.minipsf" % i), "wb").write(b"PSF\x01")
    open(os.path.join(psfpack, "L.psflib"), "wb").write(b"PSF\x01")
    h1 = g_ui.DownloadDialog._payload_hint(psfpack)
    ok("PSF" in h1 and "foobar2000" in h1, "PSF 包 -> 指向 foobar2000", h1[:36])

    h2 = g_ui.DownloadDialog._payload_hint(pack)   # 本包 4 个 txtp 里 2 个目标缺失
    ok(h2, "有 .txtp 但没转成 -> 提示包不完整", h2[:36])

    bare = os.path.join(_tmp, "bare")
    os.makedirs(bare, exist_ok=True)
    open(os.path.join(bare, "a.xyz"), "wb").write(b"x")
    h3 = g_ui.DownloadDialog._payload_hint(bare)
    ok(".xyz" in h3, "认不出时把扩展名报出来", h3[:36])

    empty = os.path.join(_tmp, "empty")
    os.makedirs(empty, exist_ok=True)
    h4 = g_ui.DownloadDialog._payload_hint(empty)
    ok("未找到" in h4, "空目录 -> 兜底文案", h4[:24])

finally:
    shutil.rmtree(_tmp, ignore_errors=True)

print()
print("=" * 62)
print("结果: %d 通过 / %d 失败" % (_n_pass, _n_fail))
print("=" * 62)
sys.exit(1 if _n_fail else 0)
