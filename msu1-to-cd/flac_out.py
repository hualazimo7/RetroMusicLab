#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flac_out.py — PCM → FLAC（无损）/ MP3 / OGG（有损）。**一个 UI 对象都不碰。**

为什么单独一个模块，而不是复用 ``netease_cloud.wav_to_flac``：

    那个模块是网易云云盘专用，顶部把 ``pycryptodome`` / ``segno`` 这些
    可选依赖全拖进来。主转换流程只是「写个 FLAC」，不该为此把整条云盘
    链路拉进内存 —— 换台机器少装一个 pycryptodome，转换本身就会跟着
    起不来。这里只依赖 ``numpy`` + ``soundfile``（libsndfile 自带 FLAC
    编码器，不需要外挂 flac.exe）。

输出规格与 WAV 完全一致（设备要求，不做可选项）::

    16-bit / 44100Hz / 立体声 / 无损

⚠⚠ 三个必须守住的点，每一个都只在真实转换时才暴露：

1. **FLAC 走 int16 直通，绝不经过 float32**。libsndfile 在 float→int 的落盘路径上
   会加 dither，PCM 值被扰动 —— 实测同一段音频两次独立写入结果不同，
   也就是「无损转出来不等于原文件」。FLAC 全程 ``dtype="int16"``。

   ⚠ **这条只对 FLAC 成立，MP3 恰好相反**：libsndfile 的 MP3 编码器不认 int16，
   把 -32768..32767 当 float 样本解释，每个点放大 32768 倍 → 削顶成方波 →
   满耳杂音（v5.9.18 修的 bug，埋了一年多）。MP3 **必须**先
   ``arr.astype(np.float32) / 32768.0`` 再交给编码器，且必须
   ``compression_level=0.0`` + ``bitrate_mode="CONSTANT"``（否则默认 VBR，
   实际 102 kbps 而非 320 CBR）。详见 :func:`verify_lossy` 与 ``_write`` 的
   MP3 分支注释。
2. **原子写**。先写 ``<dst>.part``，成功后 ``os.replace``。中途取消 / 磁盘满
   / 编码器抛异常时，``.part`` 会被删掉，目标位置**不会**留下半个 FLAC
   （半个 FLAC 能被播放器识别成有效文件，用户以为转好了）。
3. **不伪造成功**。0 帧 / 0 字节输出一律当失败，返回明确原因，不写空文件。

用法::

    import flac_out
    ok, dst, detail = flac_out.encode_pcm(raw, "01_曲名.flac")   # raw = int16 LE
    ok, dst, detail = flac_out.wav_to_flac("a.wav")              # 已有 WAV 再转
"""

import os

#: 与 spc_wav.SR 同源：设备固定的 44100Hz / 16bit / 2ch
SR = 44100
CHANNELS = 2

#: libsndfile 的 FLAC 压缩级别，取值 0.0~1.0（映射到 FLAC 原生 0~8）。
#: 0.8 ≈ 原生 level 6~7：实测 3 分钟方波 31.8MB → 10.9MB，耗时 0.72s。
#: 调到 1.0 只再小 1~2%，耗时翻倍，不值得。
LEVEL = 0.8

__all__ = ["SR", "CHANNELS", "LEVEL", "available", "encode_pcm",
           "wav_to_flac", "probe_flac", "embed_picture", "pictures",
           "build_picture_block", "PICTURE_FRONT_COVER", "WRITE_FMTS",
           # v6.0.0：OGG 与「有损格式」的收口
           "LOSSY_FMTS", "OGG_LEVEL",
           # v5.9.17：MP3 的 ID3v2 APIC
           "embed_id3_picture", "id3_pictures", "build_id3_block",
           "has_picture", "ID3_FRONT_COVER",
           # v5.9.19：有损落盘后的自检
           "verify_lossy", "VERIFY_MIN_SNR", "VERIFY_MIN_CORR",
           "VERIFY_MIN_SNR_BY_FMT",
           "VERIFY_MAX_PEAK_RATIO"]

#: 本模块能写的容器（``spc_wav`` / ``convert_job`` / ``server`` / 前端都用它
#: 做白名单）。**加格式只改这一处** —— 别在调用方各写一份 ``in (...)``。
#:
#: v6.0.0：从 ``("flac", "mp3")`` 扩到四档，新增 **OGG Vorbis**。
WRITE_FMTS = ("flac", "mp3", "ogg")

#: 有损格式（写完必须跑 :func:`verify_lossy`）。FLAC 无损，不在里面。
LOSSY_FMTS = ("mp3", "ogg")


def _norm_fmt(fmt):
    """任意输入 → ``WRITE_FMTS`` 里的一个值；认不出来退回 ``"flac"``。

    收口用。以前每个调用点各写一句
    ``"mp3" if fmt == "mp3" else "flac"``，加第三种格式时这种写法会
    **静默把 ogg 当成 flac**（无损容器写进去、扩展名却是 .ogg），
    所以统一走这里。
    """
    f = str(fmt or "").strip().lower()
    return f if f in WRITE_FMTS else "flac"


def available(fmt="flac"):
    """编码器能不能用。返回 ``(ok, 说明)``，**永不抛异常**。

    ``fmt`` 支持 ``"flac"``（默认）/ ``"mp3"``（v5.9.15）/
    ``"ogg"``（v6.0.0）。三者都走 soundfile，只是 ``format`` 不同 ——
    libsndfile 1.2+ 自带 LAME（MP3）和 libvorbis（OGG），都不用外挂。

    调用方应该先用它决定「要不要把某个格式的选项露出来」——
    依赖缺失时界面还摆着一个点了必失败的按钮，比没有这个按钮更糟。
    """
    want = _WRITERS[_norm_fmt(fmt)][0]
    try:
        import soundfile as sf
    except Exception as exc:                       # pragma: no cover
        return False, ("缺少 soundfile，无法写 %s：%s" % (want, exc))
    try:
        fmts = sf.available_formats()
    except Exception as exc:                       # pragma: no cover
        return False, "读取 soundfile 格式表失败：%s" % exc
    if want not in fmts:
        return False, ("当前 libsndfile 不带 %s 编码器（版本 %s），"
                       "请升级 soundfile" % (want, getattr(sf, "__version__", "?")))
    return True, "soundfile %s / libsndfile %s" % (
        getattr(sf, "__version__", "?"), getattr(sf, "__libsndfile_version__", "?"))


#: ``fmt`` -> ``(soundfile format, subtype, 扩展名)``。真加格式时在这里加一行。
_WRITERS = {
    "flac": ("FLAC", "PCM_16", ".flac"),
    "mp3": ("MP3", "MPEG_LAYER_III", ".mp3"),
    # v6.0.0：OGG Vorbis。libsndfile 自带 libvorbis，同样不用外挂。
    "ogg": ("OGG", "VORBIS", ".ogg"),
}

#: OGG 的 ``compression_level``（0.0~1.0，对应 Vorbis 的 q0~q10）。
#:
#: ⚠ **必须是 0.0（q10，约 395 kbps VBR）**，这不是「求好听」而是
#: **自检能不能过**的问题。实测（4 秒、44.1k、立体声）：
#:
#:   | level | 体积占 WAV | 满量程白噪 SNR | 满量程方波 SNR |
#:   |-------|-----------|---------------|---------------|
#:   | 0.0   | 28%       | 10.39 dB      | **9.95 dB**   |
#:   | 0.1   | 20%       | 9.00 dB       | **3.21 dB**   |
#:   | 0.2   | 13%       | 8.81 dB       | **0.82 dB**   |
#:
#: 方波不是刁难 —— **NES / GB 的声道本来就是方波**，芯片音乐里满地都是。
#: 0.1 以下会把合法的方波内容判成「编码异常」直接丢文件。所以宁可体积
#: 大一点（28%，比 MP3 的 23% 略大），也要让真实内容全部过检。
OGG_LEVEL = 0.0

#: OGG 一次 ``write`` 最多喂多少帧（5 秒 @44.1k）。
#:
#: ⚠ **必须分块写**，这不是性能优化，是**避免进程直接消失**。
#: libsndfile 1.2.2 的 Vorbis 写入器在「一次性写很长一段」时会**栈溢出**：
#: 进程不是抛异常，而是被系统直接干掉（Windows 退出码 ``0xC00000FD``，
#: 日志里什么都没有、``.part`` 留在盘上，用户看到的是「转 OGG 一转就闪退」）。
#:
#: 实测边界（44.1k / 立体声 / float32，``sf.write`` 一次性写）：
#:
#:   | 时长 | 结果 |
#:   |------|------|
#:   | 8 / 10 / 12 / 14 / 16 秒 | 正常 |
#:   | 18 / 20 / 40 / 80 / 160 秒 | **0xC00000FD 崩溃** |
#:
#: 分块写（5 秒一块）实测 20 / 60 / 150 / 300 秒全部正常。
#: 同一段音频 FLAC / MP3 一次性写到 900 秒都没事 —— **只有 OGG 有这个坑**。
#: 5 秒（= 220500 帧）离崩溃边界（约 17 秒）有三倍余量，且不影响码流质量
#: （Vorbis 是流式编码，分块喂和一次喂出来的码流一致）。
OGG_BLOCK_FRAMES = 44100 * 5

# ---- v5.9.19：MP3 落盘后的**自检**门槛 ----
#
# 为什么要自检：v5.9.15~v5.9.17 那三个版本，MP3 编码器把 int16 当 float 用，
# 写出来的文件「能播、时长对、体积对、帧结构自洽、三个解码器解得一模一样」，
# 唯一的破绽是**内容不对**。这种 bug 只有「解回来跟源逐样本比」才发现得了，
# 所以干脆把这条检查做进转换流程本身 —— 每写一首就验一首，不过就报错，
# 绝不让坏文件落地。
#
# 三个判据**任意一个**触发就判失败（实测值见每行后面的数）：
#
#   | 判据        | 真实音乐 320k | 满量程白噪 | 老 bug（int16 直传） |
#   |-------------|--------------|-----------|---------------------|
#   | SNR         | 36 ~ 43 dB   | 15.3 dB   | **-21.6 dB**        |
#   | 相关系数     | 0.9999       | 0.99      | **0.1 左右**        |
#   | 解码/源峰值比 | 1.02 ~ 1.12  | 1.00      | **2.0（削顶到满量程）** |
#
# 阈值取 8 dB 而不是 20 dB：满量程白噪是最坏情况下的**合法**内容，只有
# 15.3 dB —— 门槛压到 20 dB 会把「转一段噪声/音效」误判成失败（实测踩过，
# t_v5915 立刻红）。8 dB 离最坏合法内容还有 7 dB 余量，离 bug 有 30 dB，
# 两边都安全。
#
# 相关系数和峰值比是**互补**的保险：如果源本身就在满量程附近，bug 放大后
# 峰值比只有 1.02，光看峰值比抓不住；但 SNR 和相关系数一定崩。
VERIFY_MIN_SNR = 8.0

#: 每个有损格式各自的 SNR 下限。不传 ``fmt`` 时退回 :data:`VERIFY_MIN_SNR`。
#:
#: 为什么要分格式：**Vorbis 的误差结构和 LAME 不一样**。同样是「满量程方波」
#: 这种合法内容，MP3 是 15 dB 以上，OGG(q10) 只有 9.95 dB —— 门槛都用 8
#: 只剩 2 dB 余量，实机遇到更密的素材就可能误杀。OGG 降到 6.0：
#: 离最坏合法内容 4 dB，离真 bug（-21 dB 那一档）还有 27 dB，两头都安全。
#:
#: ⚠ 降这个数**不是放宽标准**：这道自检要抓的是「int16 当 float 用、放大
#: 三万倍削顶」那种灾难性错误（SNR 是负的），不是分辨 8 dB 和 6 dB 的差别。
VERIFY_MIN_SNR_BY_FMT = {"mp3": VERIFY_MIN_SNR, "ogg": 6.0}

#: 波形相关系数下限。正常编码 ≥ 0.99；老 bug 是 0.1 量级（波形完全不相关）。
VERIFY_MIN_CORR = 0.80

#: 解码峰值 / 源峰值 的上限。老 bug 把样本放大三万倍再削顶，这个比值会
#: 逼近「满量程 / 源峰值」；正常编码只有 1.0~1.1（MP3 重建波形会略过冲）。
VERIFY_MAX_PEAK_RATIO = 1.6

#: 自检只取**中段**最多 3 秒 —— 中段最不容易是静音，而且 3 秒足够暴露
#: 「整体放大三万倍」这种灾难性错误；全曲比较会让转换慢一倍，没必要。
VERIFY_MAX_SEC = 3.0

#: 源峰值低于这个值（≈ -50 dBFS）就当静音，跳过自检 —— 静音的 SNR
#: 没有意义（分母是 0），硬算会误判成失败。
VERIFY_SILENT_PEAK = 100


def _snr_db(ref, got):
    """两个同长 float64 数组的 SNR（dB）。"""
    import numpy as np
    err = ref - got
    pr = float(np.sum(ref * ref))
    pe = float(np.sum(err * err))
    if pe <= 0:
        return 99.0
    if pr <= 0:
        return -99.0
    return 10.0 * float(np.log10(pr / pe))


def _corr(ref, got):
    """波形相关系数（去均值后）。对「整体缩放 / 削顶」这类失真极敏感。"""
    import numpy as np
    a = ref.ravel() - float(ref.mean())
    b = got.ravel() - float(got.mean())
    d = float(np.sqrt(np.dot(a, a) * np.dot(b, b)))
    if d <= 0:
        return 0.0
    return float(np.dot(a, b) / d)


def verify_lossy(path, arr, sr, min_snr=None, max_sec=None, fmt=None):
    """把**刚写好的有损文件**解回来，跟源 PCM 比。返回 ``(ok, snr, note)``。

    ``arr`` 是写之前那份 int16 ``(n, ch)`` 源数据。有损编码必然有误差，
    所以比的不是「是否逐样本相同」，而是「听起来还是不是同一段音频」：

    * ``snr`` —— 误差是否远小于信号（下限 :data:`VERIFY_MIN_SNR`）；
    * ``corr`` —— 波形是否还跟源同相（下限 :data:`VERIFY_MIN_CORR`）；
    * ``峰值比`` —— 有没有被放大到削顶（上限 :data:`VERIFY_MAX_PEAK_RATIO`）。

    只在**中段**取 ``max_sec`` 秒来比：
      · 首尾有编码器延迟 / 补零，容易引入假误差；
      · 中段一般不是静音，不会踩到「分母为 0」。

    返回值里的 ``snr`` 即使判失败也有意义（日志要打出来）。

    ``fmt``（v6.0.0）只影响 SNR 下限的取值 —— 传 ``"ogg"`` 时用
    :data:`VERIFY_MIN_SNR_BY_FMT` 里那一档；相关系数与峰值比两种格式
    共用同一套（它们判的是「波形还在不在」，与编码器无关）。

    这个函数**永不抛异常** —— 自检本身出问题不该把已经写好的文件判死。
    """
    import numpy as np
    import soundfile as sf

    if min_snr is None:
        min_snr = VERIFY_MIN_SNR_BY_FMT.get(str(fmt or "").strip().lower(),
                                            VERIFY_MIN_SNR)
    min_snr = float(min_snr)
    max_sec = VERIFY_MAX_SEC if max_sec is None else float(max_sec)
    try:
        src_peak = int(np.abs(arr).max(initial=0))
        if src_peak < VERIFY_SILENT_PEAK:
            return True, 99.0, "静音，跳过自检"
        got, gsr = sf.read(path, dtype="int16", always_2d=True)
        if int(gsr) != int(sr):
            return False, -99.0, "解码采样率 %s != 源 %s" % (gsr, sr)
        if got.shape[1] != arr.shape[1]:
            return False, -99.0, "解码声道 %d != 源 %d" % (got.shape[1], arr.shape[1])
        n = min(len(arr), len(got))
        if n < 2048:
            return True, 99.0, "太短（%d 帧），跳过自检" % n
        span = min(n, max(2048, int(sr * max_sec)))
        lo = max(0, (n - span) // 2)

        def _stats(start):
            ref = arr[start:start + span].astype(np.float64)
            dec = got[start:start + span].astype(np.float64)
            return _snr_db(ref, dec), _corr(ref, dec), ref, dec

        snr, cor, ref, dec = _stats(lo)
        # 误差若偏大，可能是中段起点没对齐；再在 ±64 样本内找最佳位移。
        # （正常 MP3 有 LAME tag 的 gapless，lag 应该是 0；这里只是保险。）
        if snr < min_snr and lo + span + 64 <= len(got):
            best = (snr, lo)
            for lag in range(-64, 65):
                a = lo + lag
                if a < 0 or a + span > len(got):
                    continue
                s = _snr_db(ref, got[a:a + span].astype(np.float64))
                if s > best[0]:
                    best = (s, a)
            if best[1] != lo:
                snr, cor, ref, dec = _stats(best[1])

        dec_peak = int(np.abs(dec).max(initial=0))
        ref_peak = int(np.abs(ref).max(initial=0))
        peak_ratio = (dec_peak / float(ref_peak)) if ref_peak > 0 else 1.0

        if snr < min_snr:
            return False, snr, ("解码回来跟源对不上（SNR %.1f dB < %.0f dB）"
                                % (snr, min_snr))
        if cor < VERIFY_MIN_CORR:
            return False, snr, ("解码波形跟源不相关（相关 %.3f < %.2f）"
                                % (cor, VERIFY_MIN_CORR))
        if peak_ratio > VERIFY_MAX_PEAK_RATIO:
            return False, snr, ("解码峰值被放大到 %.1f 倍（%d → %d），疑似削顶"
                                % (peak_ratio, ref_peak, dec_peak))
        return True, snr, "自检 SNR %.1f dB / 相关 %.4f" % (snr, cor)
    except Exception as exc:                      # noqa: BLE001
        return True, -99.0, "自检跳过（%s）" % exc


def _sf_write(path, buf, sr, kw, block=0):
    """把 ``buf`` 写成音频文件；``block > 0`` 时按块喂给编码器。

    为什么需要这个：libsndfile 1.2.2 的 **OGG/Vorbis** 写入器一次性喂太长
    的缓冲区会**栈溢出**（进程被系统杀掉，退出码 0xC00000FD，不抛异常、
    留一个 ``.part``）。分块写就没有这个问题 —— 见 :data:`OGG_BLOCK_FRAMES`
    里的实测边界。FLAC / MP3 不需要分块（实测 900 秒一次性写也正常），
    所以默认 ``block=0`` 走原来那条路，行为一个字都不变。
    """
    import soundfile as sf

    n = int(buf.shape[0]) if getattr(buf, "ndim", 0) > 0 else 0
    if block <= 0 or n <= block:
        sf.write(path, buf, sr, **kw)
        return
    ch = int(buf.shape[1]) if buf.ndim > 1 else 1
    # SoundFile 构造函数吃同一套 kw（format / subtype / compression_level /
    # bitrate_mode），所以这里不用把参数再翻译一遍。
    with sf.SoundFile(path, "w", sr, ch, **kw) as f:
        for i in range(0, n, block):
            f.write(buf[i:i + block])


def _write(dst, arr, sr, level, cover=None, fmt="flac"):
    """把 int16 的 2D 数组写成 FLAC / MP3 / OGG（内部用，已保证 arr 是 int16）。

    ``cover`` 给了就在落盘后补图片：FLAC 走 ``METADATA_BLOCK_PICTURE``
    （:func:`embed_picture`），MP3 走 ID3v2 的 APIC 帧
    （:func:`embed_id3_picture`）。**嵌封面失败不影响转码成功** —— 音频已经
    写好了，为了一张图把整首判失败是本末倒置；原因会拼进 detail 里。
    OGG 目前**不嵌**（libsndfile 不提供写 Vorbis comment 图片块的接口，
    自己拼 comment header 风险太大），封面照常以 ``cover.png`` 落在音频旁边。

    Returns:
        ``(ok, dst, detail)``
    """
    import numpy as np
    import soundfile as sf

    fmt = _norm_fmt(fmt)
    sf_fmt, sf_sub, _ext = _WRITERS[fmt]
    tag = fmt.upper()

    tmp = dst + ".part"
    try:
        kw = {"format": sf_fmt, "subtype": sf_sub}
        if fmt == "flac":
            kw["compression_level"] = level
            buf = arr
        else:
            # ⚠⚠ v5.9.18 修的一个**严重** bug（v5.9.15 起就存在）：
            #
            # libsndfile 1.2.x 的 MP3 编码器**不认 int16 输入**。把 int16
            # 数组直接交给它，它会把 -32768..32767 这些**整数值**当成
            # float 样本（本该是 -1.0..1.0）来解释 —— 于是每个采样点都被
            # 放大三万倍、立刻削顶成 ±1 的方波，写出来就是**满耳持续的
            # 杂音**，而且：
            #   · 帧数对（时长不变）
            #   · 采样率对、声道数对
            #   · 文件大小也对（320kbps CBR）
            # 所以任何「能解码 + 时长对 + 体积对」的检查都发现不了它。
            # 实测 SNR：int16 直传 = **-4.4 dB**，float32 = **+21 dB**。
            #
            # 必须自己归一化成 float32。除 32768（不是 32767）是为了让
            # -32768 正好落到 -1.0，跟 soundfile 自己的 int→float 转换
            # 口径一致，0 dBFS 不越界。
            #
            # v6.0.0：**OGG 走同一条路**，不是抄过来的 —— Vorbis 同样是
            # 浮点编码器，同样不吃 int16。新增格式时最容易漏的就是这一句。
            buf = arr.astype(np.float32) / 32768.0
            if fmt == "mp3":
                # 码率：libsndfile 的 MP3 码率**只受 compression_level 控制**，
                # 而且**必须显式要求 CONSTANT**，否则默认是 VBR（约 170 kbps）。
                # 实测（31.2 秒真音频，320k 目标）：
                #   默认              → 170 kbps / SNR 31.5 dB
                #   compression 0.2   → 256 kbps / SNR 35.0 dB
                #   CONSTANT + 0.0    → **321 kbps / SNR 39.1 dB**  ← 就是它
                # 说明书一直宣称「320 kbps CBR」，v5.9.18 之前其实只有 102 kbps
                # （而且内容还是坏的），这里才对上。
                kw["compression_level"] = 0.0
                kw["bitrate_mode"] = "CONSTANT"
            else:
                # OGG：**不能**传 bitrate_mode —— Vorbis 不认，libsndfile
                # 直接抛 LibsndfileError("Error set bitrate mode CONSTANT")。
                # 档位为什么必须是 0.0 见 :data:`OGG_LEVEL` 的表。
                kw["compression_level"] = OGG_LEVEL
        # OGG 必须分块（否则长曲会栈溢出把进程带走，见 OGG_BLOCK_FRAMES）。
        _blk = OGG_BLOCK_FRAMES if fmt == "ogg" else 0
        try:
            _sf_write(tmp, buf, sr, kw, block=_blk)
        except TypeError as exc:
            # 老 soundfile（< 0.13）没有 bitrate_mode 这个关键字。
            # 只在这种情况回退 —— 别的 TypeError 照常往上抛，
            # 别把「参数写错」也吞成「码率降级」。
            if "bitrate_mode" not in str(exc):
                raise
            kw.pop("bitrate_mode", None)
            kw.pop("compression_level", None)
            _sf_write(tmp, buf, sr, kw, block=_blk)
        if not os.path.exists(tmp) or os.path.getsize(tmp) <= 0:
            raise OSError("%s 输出为空" % tag)
        size = os.path.getsize(tmp)
        # ---- v5.9.19 自检：有损格式必须「解回来还像源」 ----
        # 放在 os.replace 之前 —— 不达标就让异常走下面的清理分支，
        # 目标位置**不会**留下一个听着是噪声的文件。
        # FLAC 是无损，跳过（真要验也是逐字节相同，写的时候已经保证）。
        # v6.0.0：判定改成「在 LOSSY_FMTS 里」，新加有损格式自动纳入。
        vnote = ""
        if fmt in LOSSY_FMTS:
            vok, vsnr, vwhy = verify_lossy(tmp, arr, sr, fmt=fmt)
            if not vok:
                raise OSError("%s 编码结果异常：%s。已放弃该文件，"
                              "避免写出「能播但满是杂音」的音频" % (tag, vwhy))
            vnote = "  " + vwhy
        os.replace(tmp, dst)                      # 原子替换：不留半个文件
        note = ""
        if cover:
            # FLAC 走 METADATA_BLOCK_PICTURE，MP3 走 ID3v2 APIC ——
            # 容器不同，语义相同（都是 type=3 正面封面）。
            if fmt == "flac":
                okp, why = embed_picture(dst, cover)
            elif fmt == "mp3":
                okp, why = embed_id3_picture(dst, cover)
            else:
                okp, why = False, "OGG 不支持内嵌封面（图已存为同目录 cover.png）"
            note = ("  封面 %s" % why) if okp else ("  ⚠ 封面没嵌上：%s" % why)
        return True, dst, "%d 帧 / %d Hz / %d B%s%s" % (
            arr.shape[0], sr, size, vnote, note)
    except Exception as exc:                      # noqa: BLE001
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, dst, "写 %s 失败：%s" % (tag, exc)


def encode_pcm(raw, dst, sr=SR, channels=CHANNELS, level=LEVEL, cover=None,
               fmt="flac"):
    """内存里的 PCM（int16 小端交织）→ FLAC / MP3 / OGG 文件。

    这是主流程用的入口：``spc_wav.write_tracks`` 手上本来就是这段 ``raw``
    字节串，**直接写比「先写 WAV 再读回来转」省掉一整次磁盘往返**。
    实测 FLAC 两条路径产物逐字节一致（md5 相同），所以没有理由绕。

    Args:
        raw: PCM 字节串，int16 小端、``channels`` 路交织。
        dst: 目标 ``.flac`` / ``.mp3`` / ``.ogg`` 路径。
        sr: 采样率。
        channels: 声道数。
        level: FLAC 压缩级别 0.0~1.0（MP3 / OGG 忽略，各自用固定档）。
        cover: 可选的封面图片路径（PNG / JPEG）。给了就嵌进 FLAC 的
            ``METADATA_BLOCK_PICTURE``（type 3 = 正面封面）或 MP3 的
            ID3v2 APIC，播放器和资源管理器都能看到。OGG 不嵌（图仍会
            以 ``cover.png`` 落在音频旁边）。
        fmt: ``"flac"``（默认）/ ``"mp3"`` / ``"ogg"``（v6.0.0）。

    Returns:
        ``(ok, dst, detail)``；失败时目标文件不存在。
    """
    try:
        import numpy as np
    except Exception as exc:                       # pragma: no cover
        return False, dst, "缺少 numpy，无法写 %s：%s" % (fmt, exc)

    nbytes = len(raw or b"")
    frame = 2 * max(1, int(channels))
    if nbytes <= 0:
        return False, dst, "PCM 为空（0 字节）"
    if nbytes % frame:
        # 半个采样点 / 少一路声道 —— 截到整帧再写，别让整首失败。
        # 只有解码器截断或 _limit_len 出 bug 才会走到这里。
        raw = raw[:nbytes - (nbytes % frame)]
        if not raw:
            return False, dst, "PCM 长度不是整帧（%d 字节）" % nbytes

    try:
        arr = np.frombuffer(raw, dtype="<i2")
        if int(channels) > 1:
            arr = arr.reshape(-1, int(channels))
    except Exception as exc:                      # noqa: BLE001
        return False, dst, "PCM 解析失败：%s" % exc

    try:
        os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    except OSError:
        pass
    return _write(dst, arr, sr, level, cover=cover, fmt=fmt)


def wav_to_flac(src, dst=None, level=LEVEL, cover=None):
    """已有 WAV 文件 → FLAC。给「产物已经是 WAV、事后想再转一份」用。

    ⚠ ``sf.read(..., dtype="int16")`` 是**必须**的，理由见模块头第 1 条。

    Args:
        src: 源 WAV。
        dst: 目标；``None`` 时把扩展名换成 ``.flac``。
        level: 压缩级别。
        cover: 可选封面图片路径，见 :func:`encode_pcm`。

    Returns:
        ``(ok, dst, detail)``
    """
    dst = dst or (os.path.splitext(src)[0] + ".flac")
    try:
        import soundfile as sf
    except Exception as exc:                       # pragma: no cover
        return False, dst, "缺少 soundfile，无法转 FLAC：%s" % exc
    try:
        data, sr = sf.read(src, dtype="int16", always_2d=True)
    except Exception as exc:                      # noqa: BLE001
        return False, dst, "读取 WAV 失败：%s" % exc
    if data is None or data.shape[0] <= 0:
        return False, dst, "源 WAV 没有音频帧"
    return _write(dst, data, sr, level, cover=cover)


def probe_flac(path):
    """读一个 FLAC 的 ``(帧数, 采样率, 声道)``。读不了返回 ``None``。

    门禁用它证明「转出来的确实是 44100/16bit/立体声、时长与 WAV 一致」——
    光看文件存在、大小不为 0，证明不了它没被转坏。
    """
    try:
        import soundfile as sf
        info = sf.info(path)
        return int(info.frames), int(info.samplerate), int(info.channels)
    except Exception:                              # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 封面：METADATA_BLOCK_PICTURE（type 6）
# ---------------------------------------------------------------------------
#
# 为什么自己写而不用库：
#
#   * ``soundfile`` / libsndfile **不支持**写图片块（只支持字符串元信息）；
#   * ``mutagen`` 能做，但为了一个封面多带一个纯 Python 依赖不划算 ——
#     打进 exe 还要多一份体积，而且它是 GPL，与本工程的许可口径要重新评估；
#   * ``flac.exe --picture=`` 需要外挂命令行工具。
#
# FLAC 的文件结构很规整，自己插一个块只要几十行：
#
#     "fLaC"
#     METADATA_BLOCK_HEADER(1B: last<<7 | type, 3B BE: 长度) + 块体   × N
#     音频帧 ...
#
# STREAMINFO（type 0）必须在最前面。封面块（type 6）的块体是::
#
#     4B 图片类型（3 = 正面封面）
#     4B MIME 长度 + MIME 字符串（"image/png" / "image/jpeg"）
#     4B 描述长度 + 描述（UTF-8，可以空）
#     4B 宽 / 4B 高 / 4B 色深 / 4B 调色板颜色数
#     4B 图片字节数 + 图片本体
#
# 插入后要**重算 last-block 标志**（原来最后一个块的标志位要让给新末尾）。

#: METADATA_BLOCK_PICTURE 里的图片类型。3 = 正面封面。
PICTURE_FRONT_COVER = 3
#: 单张封面的大小上限。正常盒绘 100~600 KB，4 MB 是安全阀。
MAX_COVER_BYTES = 4 * 1024 * 1024
#: FLAC 元数据块类型：图片。
_BLK_PICTURE = 6
_BLK_STREAMINFO = 0


def _png_meta(data):
    """PNG -> ``(宽, 高, 色深, 调色板颜色数)``。读不出返回 ``(0,0,0,0)``。"""
    try:
        if len(data) < 33 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return 0, 0, 0, 0
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        bit_depth = data[24]
        color_type = data[25]
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 3)
        depth = bit_depth * channels
        colors = 0
        if color_type == 3:                      # 索引色：调色板条目数
            pos = 8
            while pos + 8 <= len(data):
                ln = int.from_bytes(data[pos:pos + 4], "big")
                typ = data[pos + 4:pos + 8]
                if typ == b"PLTE":
                    colors = ln // 3
                    break
                if typ in (b"IDAT", b"IEND"):
                    break
                pos += 12 + ln
        return w, h, depth, colors
    except Exception:                              # noqa: BLE001
        return 0, 0, 0, 0


def _jpeg_meta(data):
    """JPEG -> ``(宽, 高, 色深, 0)``。只认 SOF0/SOF1/SOF2。"""
    try:
        pos = 2
        n = len(data)
        while pos + 9 < n:
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            seglen = int.from_bytes(data[pos + 2:pos + 4], "big")
            if marker in (0xC0, 0xC1, 0xC2):
                h = int.from_bytes(data[pos + 5:pos + 7], "big")
                w = int.from_bytes(data[pos + 7:pos + 9], "big")
                comps = data[pos + 9] if pos + 9 < n else 3
                return w, h, 8 * comps, 0
            pos += 2 + seglen
        return 0, 0, 0, 0
    except Exception:                              # noqa: BLE001
        return 0, 0, 0, 0


def _image_meta(data):
    """识别图片格式 -> ``(mime, 宽, 高, 色深, 颜色数)``；不认识返回 ``("",0,0,0,0)``。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        w, h, d, c = _png_meta(data)
        return "image/png", w, h, d, c
    if data.startswith(b"\xff\xd8\xff"):
        w, h, d, c = _jpeg_meta(data)
        return "image/jpeg", w, h, d, c
    return "", 0, 0, 0, 0


def build_picture_block(image, ptype=PICTURE_FRONT_COVER, desc=""):
    """图片字节 -> METADATA_BLOCK_PICTURE 的**块体**（不含块头）。

    格式不认识时抛 ``ValueError`` —— 让调用方明确知道没嵌上，而不是
    塞一个播放器读不出来的块进去。
    """
    mime, w, h, depth, colors = _image_meta(image)
    if not mime:
        raise ValueError("不认识的图片格式（只支持 PNG / JPEG）")
    d = str(desc or "").encode("utf-8")

    def _u32(v):
        return int(v).to_bytes(4, "big")

    return b"".join((
        _u32(int(ptype)),
        _u32(len(mime)), mime.encode("ascii"),
        _u32(len(d)), d,
        _u32(w), _u32(h), _u32(depth), _u32(colors),
        _u32(len(image)), image,
    ))


def _split_blocks(buf):
    """拆 FLAC 的元数据块 -> ``(块列表, 音频起始偏移)``。

    块列表是 ``[(类型, 块体), ...]``，含 STREAMINFO。
    """
    if len(buf) < 8 or not buf.startswith(b"fLaC"):
        raise ValueError("不是 FLAC（缺少 fLaC 魔数）")
    blocks, pos = [], 4
    while True:
        if pos + 4 > len(buf):
            raise ValueError("元数据块头越界（文件被截断？）")
        hdr = buf[pos]
        btype = hdr & 0x7F
        ln = int.from_bytes(buf[pos + 1:pos + 4], "big")
        start, end = pos + 4, pos + 4 + ln
        if end > len(buf):
            raise ValueError("元数据块体越界（文件被截断？）")
        blocks.append((btype, buf[start:end]))
        pos = end
        if hdr & 0x80:                            # last-metadata-block
            break
    return blocks, pos


def pictures(path):
    """读一个 FLAC 里已有的封面块 -> ``[{"type","mime","w","h","bytes"}]``。

    门禁靠它证明「封面真的嵌进去了」—— 只看转换返回值说 ok 是不够的，
    那可能只是把图写到了别处。
    """
    out = []
    try:
        with open(path, "rb") as fh:
            buf = fh.read()
        blocks, _pos = _split_blocks(buf)
    except (OSError, ValueError):
        return out
    for btype, body in blocks:
        if btype != _BLK_PICTURE or len(body) < 32:
            continue
        try:
            ptype = int.from_bytes(body[0:4], "big")
            mlen = int.from_bytes(body[4:8], "big")
            pos = 8 + mlen
            dlen = int.from_bytes(body[pos:pos + 4], "big")
            pos += 4 + dlen
            w = int.from_bytes(body[pos:pos + 4], "big")
            h = int.from_bytes(body[pos + 4:pos + 8], "big")
            pos += 16
            dlen2 = int.from_bytes(body[pos:pos + 4], "big")
            out.append({"type": ptype,
                        "mime": body[8:8 + mlen].decode("ascii", "replace"),
                        "w": w, "h": h, "bytes": dlen2})
        except Exception:                          # noqa: BLE001
            continue
    return out


def embed_picture(flac_path, image_path, ptype=PICTURE_FRONT_COVER, desc=""):
    """把一张图片嵌进**已有** FLAC 的 METADATA_BLOCK_PICTURE。

    插在 STREAMINFO 之后；已有的图片块会被**替换**（不叠加，否则反复转
    同一个文件会让 FLAC 越滚越大）。

    Returns:
        ``(ok, 说明)``；失败时原文件保持不动（先写 ``.part`` 再 ``os.replace``）。
    """
    try:
        with open(image_path, "rb") as fh:
            image = fh.read(MAX_COVER_BYTES + 1)
    except OSError as exc:
        return False, "读不到封面：%s" % exc
    if not image:
        return False, "封面文件是空的"
    if len(image) > MAX_COVER_BYTES:
        return False, "封面超过 %d KB" % (MAX_COVER_BYTES // 1024)
    try:
        block = build_picture_block(image, ptype=ptype, desc=desc)
    except ValueError as exc:
        return False, str(exc)

    try:
        with open(flac_path, "rb") as fh:
            buf = fh.read()
        blocks, audio_at = _split_blocks(buf)
    except (OSError, ValueError) as exc:
        return False, "读 FLAC 失败：%s" % exc

    # 去掉旧图片块；STREAMINFO 必须留在第一个
    rest = [(t, b) for t, b in blocks if t != _BLK_PICTURE]
    stream = [x for x in rest if x[0] == _BLK_STREAMINFO]
    others = [x for x in rest if x[0] != _BLK_STREAMINFO]
    seq = stream[:1] + [(_BLK_PICTURE, block)] + others + stream[1:]

    out = bytearray(b"fLaC")
    for i, (btype, body) in enumerate(seq):
        last = 0x80 if i == len(seq) - 1 else 0
        out.append(last | (btype & 0x7F))
        out += len(body).to_bytes(3, "big")
        out += body
    out += buf[audio_at:]

    tmp = flac_path + ".picpart"
    try:
        with open(tmp, "wb") as fh:
            fh.write(out)
        os.replace(tmp, flac_path)
    except OSError as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, "写回 FLAC 失败：%s" % exc
    mime, w, h = _image_meta(image)[:3]
    return True, "%s %dx%d / %d KB" % (mime or "?", w, h, len(image) // 1024)


# ---------------------------------------------------------------------------
# MP3：ID3v2 图片帧（APIC）
# ---------------------------------------------------------------------------
#
# MP3 没有 FLAC 那样的 METADATA_BLOCK_PICTURE，封面走 ID3v2 的 APIC 帧
# （Attached Picture）。结构简单到不值得引依赖：
#
#     标签头 10 字节： ``ID3`` + 版本(2) + 标志(1) + 长度(4, synchsafe)
#     帧头   10 字节： 帧ID(4) + 长度(4, 普通大端) + 标志(2)
#     APIC 帧体：      编码(1) + MIME(NUL 结尾) + 图片类型(1)
#                      + 描述(NUL 结尾) + 图片数据
#
# ⚠ **写 2.3 而不是 2.4**：2.3 的帧长度是普通大端，2.4 改成了 synchsafe。
#   2.3 的兼容面更广（Windows 资源管理器、foobar2000、老车机都认），
#   而我们没有必须用 2.4 的特性。读取时两种都认。

#: ID3v2 里「正面封面」的图片类型编号，语义同 :data:`PICTURE_FRONT_COVER`。
ID3_FRONT_COVER = 3

_ID3_MAGIC = b"ID3"
_ID3_APIC = b"APIC"


def _synchsafe(n):
    """普通整数 -> ID3v2 的 4 字节 synchsafe（每字节只用低 7 位）。"""
    n = int(n) & 0x0FFFFFFF
    return bytes(((n >> 21) & 0x7F, (n >> 14) & 0x7F,
                  (n >> 7) & 0x7F, n & 0x7F))


def _un_synchsafe(b):
    """4 字节 synchsafe -> 普通整数。"""
    return (((b[0] & 0x7F) << 21) | ((b[1] & 0x7F) << 14)
            | ((b[2] & 0x7F) << 7) | (b[3] & 0x7F))


def _apic_body(image, ptype=ID3_FRONT_COVER, desc=""):
    """APIC 帧体（不含 10 字节帧头）。"""
    mime, _w, _h, _d, _c = _image_meta(image)
    if not mime:
        raise ValueError("不认识的图片格式（只支持 PNG / JPEG）")
    return (b"\x00"                                  # 文本编码 0 = ISO-8859-1
            + mime.encode("ascii") + b"\x00"        # MIME，NUL 结尾
            + bytes([int(ptype) & 0xFF])            # 图片类型
            + str(desc or "").encode("latin-1", "replace") + b"\x00"
            + image)


def _pack_id3(frames):
    """``[(帧ID, 帧体)]`` -> 完整 ID3v2.3 标签（含 10 字节标签头）。"""
    body = b"".join(fid + len(b).to_bytes(4, "big") + b"\x00\x00" + b
                    for fid, b in frames)
    return _ID3_MAGIC + b"\x03\x00" + b"\x00" + _synchsafe(len(body)) + body


def build_id3_block(image, ptype=ID3_FRONT_COVER, desc=""):
    """图片字节 -> 完整 ID3v2.3 标签（只含一个 APIC 帧）。

    格式不认识时抛 ``ValueError`` —— 和 :func:`build_picture_block` 同一口径：
    宁可明确报错，也别塞一个播放器读不出来的帧。
    """
    if not image:
        raise ValueError("图片是空的")
    return _pack_id3([(_ID3_APIC, _apic_body(image, ptype, desc))])


def _split_id3(buf):
    """切出已有 ID3v2 标签的帧 -> ``([(帧ID, 帧体)], 音频起点)``。

    没有标签返回 ``([], 0)``。ID3v2.2（3 字符帧ID）太老，且本工具从不产生，
    直接当「没有标签」处理 —— 整体替换，音频照样完整保留。
    """
    if len(buf) < 10 or buf[:3] != _ID3_MAGIC:
        return [], 0
    major = buf[3]
    size = _un_synchsafe(buf[6:10])
    end = min(10 + size, len(buf))
    if major not in (3, 4):
        return [], end
    frames = []
    off = 10
    while off + 10 <= end:
        fid = buf[off:off + 4]
        if not fid.strip(b"\x00"):
            break                                 # 填充区，到此为止
        raw = buf[off + 4:off + 8]
        fsz = _un_synchsafe(raw) if major >= 4 else int.from_bytes(raw, "big")
        if fsz < 0 or off + 10 + fsz > end:
            break
        frames.append((fid, buf[off + 10:off + 10 + fsz]))
        off += 10 + fsz
    return frames, end


def embed_id3_picture(mp3_path, image_path, ptype=ID3_FRONT_COVER, desc=""):
    """把封面嵌进**已有** MP3 的 ID3v2 APIC 帧。

    已有图片帧会被**替换**（不叠加，否则反复嵌会让文件越滚越大）；
    其它帧（标题 / 艺人 / 专辑）原样保留。

    Returns:
        ``(ok, 说明)``；失败时原文件保持不动（先写 ``.id3part`` 再 ``os.replace``）。
    """
    try:
        with open(image_path, "rb") as fh:
            image = fh.read(MAX_COVER_BYTES + 1)
    except OSError as exc:
        return False, "读不到封面：%s" % exc
    if not image:
        return False, "封面文件是空的"
    if len(image) > MAX_COVER_BYTES:
        return False, "封面超过 %d KB" % (MAX_COVER_BYTES // 1024)
    try:
        apic = _apic_body(image, ptype, desc)
    except ValueError as exc:
        return False, str(exc)

    try:
        with open(mp3_path, "rb") as fh:
            buf = fh.read()
    except OSError as exc:
        return False, "读 MP3 失败：%s" % exc
    if not buf:
        return False, "MP3 是空的"

    old_frames, audio_at = _split_id3(buf)
    kept = [(f, b) for f, b in old_frames if f != _ID3_APIC]
    out = _pack_id3(kept + [(_ID3_APIC, apic)]) + buf[audio_at:]

    tmp = mp3_path + ".id3part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(out)
        os.replace(tmp, mp3_path)
    except OSError as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False, "写回 MP3 失败：%s" % exc
    mime, w, h = _image_meta(image)[:3]
    return True, "%s %dx%d / %d KB" % (mime or "?", w, h, len(image) // 1024)


def id3_pictures(path):
    """读 MP3 里已有的 ID3 APIC -> ``[{"type","mime","bytes"}]``。

    门禁靠它证明「封面真的写进去了」—— 只看返回值说 ok 是不够的。
    """
    out = []
    try:
        with open(path, "rb") as fh:
            buf = fh.read()
    except OSError:
        return out
    frames, _end = _split_id3(buf)
    for fid, body in frames:
        if fid != _ID3_APIC or len(body) < 4:
            continue
        try:
            enc = body[0]
            mlen = body.index(b"\x00", 1)
            mime = body[1:mlen].decode("ascii", "replace")
            ptype = body[mlen + 1]
            rest = body[mlen + 2:]
            if enc in (1, 2):                     # UTF-16：描述按双字节找 NUL
                i = 0
                while i + 1 < len(rest) and rest[i:i + 2] != b"\x00\x00":
                    i += 2
                dlen = i + 2
            else:
                dlen = rest.index(b"\x00") + 1
            out.append({"type": ptype, "mime": mime,
                        "bytes": len(rest) - dlen})
        except Exception:                         # noqa: BLE001
            continue
    return out


def has_picture(path):
    """这个文件里有没有封面块（FLAC 的 PICTURE / MP3 的 ID3 APIC）。

    **只读块头和标签区**，不把音频读进内存 —— 一首 150 秒的 FLAC 是
    10~30 MB，几十首就是几百 MB 的纯 IO，只为读 4 个字节的块类型。

    认不出的容器（WAV 等）一律返回 ``False``。
    """
    try:
        with open(path, "rb") as fh:
            magic = fh.read(4)
            if magic == b"fLaC":
                while True:
                    hdr = fh.read(4)
                    if len(hdr) < 4:
                        return False
                    last = hdr[0] & 0x80
                    btype = hdr[0] & 0x7F
                    blen = int.from_bytes(hdr[1:4], "big")
                    if btype == _BLK_PICTURE:
                        return True
                    fh.seek(blen, 1)
                    if last:
                        return False
            if magic[:3] == _ID3_MAGIC:
                # 已经读了 4 字节（``ID3`` + 版本主号），还差 6 字节：
                # 版本次号(1) + 标志(1) + 长度(4)
                major = magic[3]
                rest = fh.read(6)
                if len(rest) < 6:
                    return False
                size = (((rest[2] & 0x7F) << 21) | ((rest[3] & 0x7F) << 14)
                        | ((rest[4] & 0x7F) << 7) | (rest[5] & 0x7F))
                # 上限 4 MB：封面图本身就可能 400 KB，再留足余量；
                # 超过这个数说明标签结构坏了，别去 malloc 一个巨大 buffer
                if size <= 0 or size > (4 << 20):
                    return False
                if major not in (3, 4):
                    return False
                body = fh.read(size)
                off = 0
                while off + 10 <= len(body):
                    fid = body[off:off + 4]
                    if not fid.strip(b"\x00"):
                        return False              # 填充区，后面不会再有帧
                    raw = body[off + 4:off + 8]
                    fsz = (_un_synchsafe(raw) if major >= 4
                           else int.from_bytes(raw, "big"))
                    if fsz < 0 or off + 10 + fsz > len(body):
                        return False
                    if fid == _ID3_APIC:
                        return True
                    off += 10 + fsz
                return False
    except OSError:
        return False
    return False
