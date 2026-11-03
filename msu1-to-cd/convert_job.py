#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""convert_job.py — 一次「音源 → 成品」的完整流程，**不依赖任何 UI 框架**。

## 为什么要单独一个模块

原来这段流程长在 `qt_ui/worker.py` 的 `ConvertWorker.run()` 里，绑死了 QThread
和 Qt 信号。Web 版（Edge --app）也要跑同一套流程，但它不能 import PySide6
—— 一 import 就等于把 90 MB 的 Qt 拖进打包体积。

所以把流程搬到本模块，UI 层只做两件事：**喂参数、收回调**。

    qt_ui/worker.py   ConvertWorker(QThread)  → 转发成 Qt 信号
    web_ui/server.py  JobRunner(threading)    → 转发成 HTTP 状态

## 业务口径

解码/写盘/合并的每一条口径都来自 `msu1_to_cd` / `spc_wav` / `wavmerge`，
本模块只负责「按什么顺序调、什么时候清理」。**不要在这里改解码细节。**
"""

import os
import threading

import msu1_to_cd as core
import spc_wav
import stream_audio

#: 判「光盘镜像」用的扩展名（与 core 保持同一份事实源）
DISC_EXTS = (".cue",) + core.DISC_IMG_EXTS

#: PSX 的 ``.txtp`` 轨道索引 —— 它自己不含音频，只指向包里的 ``.xa`` / ``.DA``。
#: 解码走 ``psx_txtp``（纯 Python，不依赖 foobar2000），所以能进主流水线。
TXTP_EXT = ".txtp"

#: PS1 的**独立** CD-XA 音频流（v5.2.4）。
#: JoshW 的 PS1 包有两种形态：一种有 ``.txtp`` 索引，一种是「一堆 ``.XA`` +
#: 一个 ``.PSF``」平铺。后者以前只转得出那个 ``.PSF``，``.XA`` 全丢 ——
#: 因为 ``.xa`` 不在 ``CHIP_EXTS`` 里（libgme 不吃），也没人把它当音源。
#: 现在它和 ``.txtp`` 走同一个模块，只是入口是 ``psx_txtp.decode_xa_tracks``。
XA_EXT = ".xa"

#: PS1/PS2 的 PSF 家族 —— 解的是「内存快照 + 程序」（PS1 是 R3000A + SPU，
#: PS2 是 IOP + SPU2），必须模拟 CPU 与声音芯片。
#: 走 ``psf_native`` → ``psf2wav.exe``（psflib + Highly Experimental）。
#: **需要一份 BIOS**（PS2 的 BIOS 同时覆盖 PS1 的 HE 引导，见 ``psf_native``
#: 模块注释）。PS2 的 `.psf2`/`.minipsf2` 采样率是 48000，PS1 是 44100。
PSF_EXTS = (".psf", ".psf1", ".minipsf", ".minipsf1", ".psf2", ".minipsf2")

#: N64 的 USF 系（v5.3）—— 解的是「状态快照 + .usflib 采样库」，必须模拟
#: R4300 CPU 与 RSP 的音频微码（HLE）。走 ``usf_native`` → ``usf2wav.exe``
#: （psflib + lazyusf2 + zlib）。**不需要 BIOS** —— N64 没有这东西。
#: 时长只来自 ``length`` / ``fade`` 标签，没有「按内容算总长」这回事。
USF_EXTS = (".miniusf", ".usf")

#: NDS 的 2SF 系（v5.3）—— 解的是「快照 / 自制驱动 ROM + .2sflib ROM 库」，
#: 必须模拟 ARM9/ARM7 双核与 DeSmuME 的 SPU。走 ``vio_native`` →
#: ``vio2wav.exe``（psflib + vio2sf + zlib）。**不需要 BIOS** ——
#: vio2sf 的 ``bios.c`` 是 HLE 实现。时长同样只来自 ``length`` / ``fade``。
VIO_EXTS = (".mini2sf", ".2sf")

#: Saturn/Dreamcast 的 SSF/DSF 系（v5.3）—— 解的是「驱动程序 + 声音 RAM」，
#: 必须跑 SH-2 + SCSP（Saturn）或 ARM7 + AICA（Dreamcast）。走 ``ssf_native``
#: → ``ssf2wav.exe``（psflib + Highly Theoretical + zlib）。**不需要 BIOS**。
#: 一个 exe 管两种机器，所以四个后缀归一组。
SSF_EXTS = (".ssf", ".minissf", ".dsf", ".minidsf")

#: GBA 的 GSF 系（v5.3）—— 解的是**一整个 GBA ROM**（常是 4~32 MB 的整卡
#: 镜像），要模拟 ARM7TDMI 与 GBA 的 DMA/定时器。走 ``gsf_native`` →
#: ``gsf2wav.exe``（psflib + mGBA 最小核 + zlib）。**不需要 BIOS** ——
#: mGBA 走 HLE，``skipBios = true``。时长同样只来自 ``length`` / ``fade``。
#:
#: ⚠ 这是四种快照格式里唯一**原生采样率不是 44100** 的（GSF 是 32768）。
#: ``gsf2wav.exe`` 出厂就让 blip_buf 直接出 44100，所以这边不用管。
GSF_EXTS = (".gsf", ".minigsf")

#: Capcom QSound 的 QSF 系（v5.9.10）—— 容器是 PSF（版本字节 0x41），
#: 但 program section 里装的是「11 字节块头 + 载荷」的块列表：Z80 程序 ROM、
#: SMP 采样 ROM、可选的 KEY 密钥。要模拟 Z80 + QSound DSP 的 16 个声道。
#: 走 ``qsf_native`` → ``qsf2wav.exe``（psflib + highly_quixotic + zlib）。
#: **不需要 BIOS** —— QSound 是街机板上的独立声音子系统。
#:
#: ⚠ 原生采样率是 **24038**（QSound DAC 的硬件节拍，改大只会变调），
#: 但 ``qsf2wav.exe`` 内部用 64 抽头 Kaiser sinc 直接重采样到 44100，
#: 所以这边和 GSF 一样**不用管采样率**。
#:
#: ⚠ 一个包里是「1 个 .qsflib + N 个 .miniqsf」，每首的差异**只有 Z80 ROM
#:   偏移 0x21 的那一个字节**（曲号）。曲号 ≠ 文件序号。
QSF_EXTS = (".qsf", ".miniqsf")

#: **流式音频**（v5.2.6）：游戏自己存好的预录 BGM，一条文件就是一首成品。
#: 和上面三类都不同 —— 不是芯片合成、不是内存快照、也不是 CD 音轨。
#:
#: * ``.adx``  —— CRI ADX（Dreamcast / PS2 / 街机），PyAV 原生支持；
#: * ``.hwas`` —— Vicarious Visions HWAS（NDS），裸 IMA ADPCM，纯 Python 解。
#:
#: 判据在 ``stream_audio.EXT_KIND``（单一事实源），这里只做转发，
#: 免得两边各写一份扩展名列表、加格式时漏一处。
STREAM_EXTS = tuple(stream_audio.ALL_EXTS)


def _txtp_targets_psf(folder, names):
    """这个 ``.txtp`` 包的目标是不是 ``.psf``（= GameCube 的 Pivotal 包）。

    PS1 的包指向 ``.xa`` / ``.DA``；GameCube 的《Conflict》系列指向
    ``stream/STREAM__004.psf#005``。两者的处置完全一样（都走
    ``psx_txtp`` → ``decode_pcm``），只有**界面上的标签**不同 ——
    把 GC 的包标成「PSX 音频」会让用户以为下错了。
    """
    return _txtp_target_ext(folder, names) == ".psf"


def _txtp_target_ext(folder, names):
    """包里第一个 ``.txtp`` 指向什么扩展名（小写带点）。读不到返回 ``""``。

    三个主人，处置相同、标签不同（v5.9.12 补上第三种）::

        .xa / .DA   PS1（JoshW 的 23 个 txtp 索引）
        .psf        GameCube 的 Pivotal 包（stream/STREAM__004.psf#005）
        .wem        PS4 的 Wwise 序列（wem/591727928.wem #i #b …）

    ⚠ **看一个就够** —— 一个包里不会混两种目标。而 ``.txtp`` 可能有
      186 个，逐个读一遍在「添加文件夹」时就是 186 次读盘。
    """
    for n in sorted(names):
        if not n.endswith(TXTP_EXT):
            continue
        try:
            # v5.9.12：长路径。PS4 的 AC4 包里有 38 个 .txtp 的完整路径
            # 263~315 字符，超过 Windows MAX_PATH(260)，直接 open 报
            # FileNotFoundError —— 这个包就会被误判成「读不到 txtp」。
            with open(core.long_path(os.path.join(folder, n)), "r",
                      encoding="utf-8", errors="replace") as fh:
                line = fh.read(400).strip()
        except OSError:
            continue
        head = line.partition("#")[0].strip().lower()
        return os.path.splitext(head)[1]
    return ""


def classify(path):
    """把一个路径归成界面上的「格式」列。只做展示分类，不决定解码路径
    —— 解码统一交给 ``core.load_source``，它自己会按内容判。"""
    p = str(path)
    if os.path.isdir(p):
        try:
            names = [n.lower() for n in os.listdir(p)]
        except OSError:
            return "未知"
        if any(n.endswith(".pcm") for n in names):
            return "MSU-1"
        if any(n.endswith(".cue") for n in names) and \
                any(n.endswith(core.DISC_IMG_EXTS) for n in names):
            return "光盘镜像"
        if any(n.endswith(".spc") for n in names):
            return "SPC"
        # PS1 下载包解压出来就是「BGM.XA + 一堆 .txtp」的文件夹 —— 加整个
        # 文件夹才是正常用法（23 首逐个挑太荒唐）。没有这一条会归成「未知」
        # 直接加不进来。
        if any(n.endswith(TXTP_EXT) for n in names):
            # v5.9.11：GameCube 的 Pivotal 包也是「一堆 .txtp」，但目标是
            # .psf 而不是 .xa/.DA —— 看一个就知道，标签别写错。
            # v5.9.12：PS4 的 Wwise 包同理（目标 .wem）。**三种主人**
            # 处置完全一样（都走 psx_txtp），只有界面标签不同。
            _tk = _txtp_target_ext(p, names)
            if _tk == ".psf":
                return "GameCube 音频"
            if _tk == ".wem":
                return "PS4 音频"
            return "PSX 音频"
        # ⚠ 有 ``.xa`` 但**没有** ``.txtp`` 的目录（JoshW 的「15 个 .XA + 一个
        # .PSF」平铺包）**故意归成「未知」**，不是漏写。
        #
        # 归成一个整包音源的话，得在同一个音源里同时跑 XA 解码器和 PSF 解码器
        # （PSF 还要 BIOS，失败就整包废），而且下载页是**逐文件**入队的，
        # 两边产出的编号/目录结构会对不上。归成「未知」→ ``scan_dir_sources``
        # 逐文件展开 → 每个 .XA 一个音源、同目录的 .PSF 也各算一个，
        # 与下载页行为一致，单首坏掉也只废那一首。
        if any(n.endswith(XA_EXT) for n in names):
            return "未知"
        if any(n.endswith(PSF_EXTS) for n in names):
            return "PSF"
        # 流式音频的目录（一整包 .adx / .hwas 平铺）**故意归「未知」** ——
        # 和上面 ``.xa`` 同一个理由：下载页是逐文件入队的，归成一个整包音源
        # 两边编号就对不上了；逐文件展开才能「一首坏掉只废那一首」。
        if any(n.endswith(STREAM_EXTS) for n in names):
            return "未知"
        # USF 的目录（一堆 .miniusf + 一个 .usflib）**故意归「未知」** ——
        # 和上面同一个理由：逐文件展开才能「一首坏掉只废那一首」。
        # 而且 psflib 是靠 `_lib` 标签去**同目录**找 `.usflib` 的，
        # 文件原地不动就能找到，不需要整包一起处理。
        if any(n.endswith(USF_EXTS) for n in names):
            return "未知"
        # 2SF 的目录同理（一堆 .mini2sf + 一个 .2sflib）—— 逐文件展开。
        if any(n.endswith(VIO_EXTS) for n in names):
            return "未知"
        # SSF/DSF 的目录同理 —— 逐文件展开，一首坏掉只废那一首。
        if any(n.endswith(SSF_EXTS) for n in names):
            return "未知"
        # GSF 的目录同理（一堆 .minigsf + 一个 .gsflib）—— 逐文件展开。
        if any(n.endswith(GSF_EXTS) for n in names):
            return "未知"
        # QSF 的目录同理（一堆 .miniqsf + 一个 .qsflib）—— 逐文件展开。
        # ⚠ 归成整包会有个具体坏处：45 首共用一个 4MB 的 .qsflib，整包处理
        #   得自己管「库只解一次」，而 psflib 本来就是按 ``_lib`` 标签去
        #   同目录找的 —— 文件原地不动就能找到，不需要整包一起处理。
        if any(n.endswith(QSF_EXTS) for n in names):
            return "未知"
        if any(n.endswith(core.CHIP_EXTS) for n in names):
            return "芯片音源"
        if any(n.endswith(".mp3") for n in names):
            return "MP3"
        return "未知"
    ext = os.path.splitext(p)[1].lower()
    if ext in DISC_EXTS:
        return "光盘镜像"
    if ext == TXTP_EXT:
        return "PSX 音频"
    if ext == XA_EXT:
        return "PSX 音频"
    if ext in PSF_EXTS:
        # v5.9.11：``.psf`` 有两个主人 —— PS1/PS2 的快照式 PSF，和 GameCube
        # 的 Pivotal 预录流。靠第 4 个字节（类型字节）分，互不重叠。
        # ⚠ 这里**必须容错**：classify() 是「加音源」的第一步，抛异常会让
        # 整个添加循环挂掉（界面上表现为拖进去没反应）。模块缺失时退回
        # "PSF" —— 真正转换时再报那句「解码模块没装上」更清楚。
        try:
            import pivotal_psf
            return "GameCube 音频" if pivotal_psf.is_pivotal(p) else "PSF"
        except Exception:                        # noqa: BLE001
            return "PSF"
    if ext in USF_EXTS:
        return "USF"
    if ext in VIO_EXTS:
        return "2SF"
    if ext in SSF_EXTS:
        return "DSF" if ext in (".dsf", ".minidsf") else "SSF"
    if ext in GSF_EXTS:
        return "GSF"
    if ext in QSF_EXTS:
        return "QSF"
    if ext in STREAM_EXTS:
        return "流式音频"
    if ext == ".spc":
        return "SPC"
    if ext == ".pcm":
        return "MSU-1"
    if ext == ".mp3":
        return "MP3"
    # ⚠ 必须用 CHIP_EXTS（全部）而不是 P0_CHIP_EXTS（只有 6 种）。
    #   用 P0 时 P1 的 `.kss` / `.gym` / `.sap` / `.ay` 以及 v5.4 的 `.sgc`
    #   会落到「未知」—— 症状是「**单文件**拖进来加不进去，但整个文件夹可以」
    #   （目录分支用的是 CHIP_EXTS，所以只有单文件路径坏）。
    #   实测这 5 种后缀单文件确实全返回「未知」，而它们早就该能转。
    if ext in core.CHIP_EXTS:
        return "芯片音源"
    return "未知"


def fmt_dur(secs):
    secs = float(secs or 0)
    h, rem = divmod(int(round(secs)), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


class ConvertJob:
    """跑一次转换。**在主线程之外调用 ``run()``**，它会一直阻塞到结束。

    回调约定（都可为 None）：
        on_log(str)                 一行日志，原样给 UI
        on_progress(float, str)     0~100 + 状态文字
        on_track(int, int)          已完成轨数 / 总轨数
    """

    def __init__(self, sources, opts, on_log=None, on_progress=None, on_track=None):
        self.sources = list(sources)
        self.opts = dict(opts)
        self.on_log = on_log
        self.on_progress = on_progress
        self.on_track = on_track
        self._cancel = threading.Event()
        self._used_wav = set()
        self._wav_idx = 0
        self._done = 0
        self._total = 0
        # 「写 WAV」这一段的进度落在当前音源那半个切片里（见 _track_prog）。
        # 以前是裸的 100*i/n，于是解码走到 46% 之后写 WAV 又从 12% 开始 ——
        # 进度条往回跳，看着像重启。单音源时默认就是 50%..100%。
        self._pct_base = 50.0
        self._pct_span = 50.0

    # -- 外部控制 ---------------------------------------------------------
    def cancel(self):
        self._cancel.set()

    def is_cancelled(self):
        return self._cancel.is_set()

    # -- 内部 -------------------------------------------------------------
    def _log(self, msg):
        if self.on_log:
            self.on_log(msg)

    def _prog(self, pct, text):
        if not self._cancel.is_set() and self.on_progress:
            self.on_progress(max(0.0, min(100.0, float(pct))), text)

    def _track_prog(self, i, n, name="", secs=0.0):
        self._done = i
        self._total = n
        if self.on_track:
            self.on_track(i, n)
        if n:
            # 落在当前音源切片的**后半段**：解码占前半段（load_source 上报
            # 0..50/n），写 WAV 占后半段。这样整条进度是单调的，不会往回跳。
            # 文案跟着输出格式走：FLAC 模式下还写「写 WAV」，用户会以为
            # 格式选项没生效。
            _tag = str(self.opts.get("fmt") or "wav").upper()
            if _tag not in ("FLAC", "MP3"):
                _tag = "WAV"
            self._prog(self._pct_base + self._pct_span * float(i) / float(n),
                       f"写 {_tag} {i}/{n}")

    def _to_cd(self, tracks, out_dir, name):
        o = self.opts
        cd_opts = {
            "gap": o["gap"], "album": o["album"] or name,
            "performer": o["performer"], "use_cdtext": True,
            "verify": o["verify"],
            "wav_dir": out_dir if o["cd_wav"] else None,
            "cd_wav": False,
        }
        bin_path, cue_path, infos, sector = core.tracks_to_cd(
            tracks, out_dir, name, cd_opts,
            on_progress=self._track_prog,
            on_stage=lambda t: self._prog(50.0, t))
        for i, t in enumerate(infos, 1):
            dur = t["nbytes"] / 4 / 44100
            self._log(f"   CD 轨 {i:02d}/{len(infos):02d}: {t['title']} "
                      f"({dur / 60:.1f} 分钟)")
        self._log(f"   ✅ 写出 {os.path.basename(bin_path)} + "
                  f"{os.path.basename(cue_path)}（{sector} 扇区）")
        return len(infos)

    def _merge_wavs(self, name, sub):
        """把**本音源**刚产出的分轨拼成一个整轨文件。

        口径与 tkinter 版（``msu1_gui.ConvertWorker._merge_wavs``）逐条一致：
        * 少于 2 个文件不合并 —— 单轨音源没有合并的意义
        * 输出名固定 ``<源名>（合并）.wav``，**同名即覆盖**（不做重名规避，
          否则重转一次就多一个「（合并） 2.wav」，越积越多）
        * 失败只写日志，不影响本次转换的成败计数
        """
        if not self.opts.get("merge_wav"):
            return
        files = [str(p) for p in (sub.get("files") or ()) if p]
        if len(files) < 2:
            return
        try:
            import wavmerge
        except ImportError:
            self._log("   ⚠ 多轨合并不可用（wavmerge 未打包）")
            return
        out = wavmerge.merge_target(self.opts["out_dir"], name)
        self._prog(99.0, f"合并 {len(files)} 首…")
        res = wavmerge.merge_wavs(files, out, on_log=None, cancel=self._cancel)
        if res.get("ok"):
            secs = float(res.get("seconds") or 0)
            self._log("   🔗 已合并 %d 首 → %s（%d:%04.1f）"
                      % (res.get("files") or len(files),
                         os.path.basename(out), secs // 60, secs % 60))
        elif not self._cancel.is_set():
            self._log("   ⚠ 合并失败：%s" % (res.get("error") or "未知错误"))

    def _txtp_tracks(self, path):
        """``.txtp`` → 音轨（PCM 已在内存里）。**文件或目录都行**。

        ``.txtp`` 是 PSX 下载包里的一行索引，指向包内 ``.xa``（XA-ADPCM）
        或 ``.DA``（裸 PCM16LE）。解码只用 ``psx_txtp``，**不需要 foobar2000**
        —— 这是它跟 ``.psf`` 的关键区别，也是它能进主流水线的原因。

        目录：一个 ``.txtp`` 一轨。PS1 下载包解压出来就是「``BGM.XA`` +
        一堆 0 字节的 ``BGM.XA#01.txtp``」这种目录，用户直接加整个文件夹
        —— 转成一个 BIN+CUE 里 23 轨，正是「这张盘的 BGM」该有的样子。
        单首坏了只记一行日志，不能连累其余 22 首。
        """
        import psx_txtp                              # 延迟导入：只有真用到才付这个钱
        if os.path.isdir(str(path)):
            paths = psx_txtp.list_txtp(path)
            if not paths:
                raise RuntimeError("目录里没有 .txtp")
            tracks, bad = [], []
            for i, one in enumerate(paths, 1):
                if self._cancel.is_set():
                    raise RuntimeError("已取消")
                self._prog(50.0 * (i - 1) / len(paths), "解 XA 音频… %d/%d"
                           % (i, len(paths)))
                r = psx_txtp.decode_pcm(one, cancel=self._cancel)
                if not r["ok"]:
                    bad.append("%s: %s" % (os.path.basename(one), r["error"]))
                    continue
                tracks.append({"name": r["title"], "pcm": r["pcm"],
                               "little_endian": True})
            for msg in bad:
                self._log("   ⚠ 跳过 %s" % msg)
            if not tracks:
                raise RuntimeError("目录里 %d 个 .txtp 全部解不出来：%s"
                                   % (len(paths), bad[0] if bad else "未知原因"))
            self._log("   ℹ .txtp 目录 → %d 轨（%d 个解不出已跳过）"
                      % (len(tracks), len(bad)))
            return tracks
        r = psx_txtp.decode_pcm(
            path, cancel=self._cancel,
            on_progress=lambda f: self._prog(50.0 * float(f), "解 XA 音频…"))
        if not r["ok"]:
            raise RuntimeError(r["error"])
        self._log("   ℹ .txtp 索引 → %s（%s, %s）"
                  % (r["kind"], fmt_dur(r["seconds"]),
                     fmt_size(len(r["pcm"]) + 44)))
        return [{"name": r["title"], "pcm": r["pcm"], "little_endian": True}]

    # -- 主流程 -----------------------------------------------------------
    def _xa_tracks(self, path):
        """独立的 ``.XA`` → 音轨（一条子流一轨）。**纯 Python，不要 BIOS。**

        和 ``.txtp`` 的区别只是「谁告诉你这条流在哪」：``.txtp`` 里写着
        ``sound/xxx.xa #I 2153088``，而这里整个 ``.XA`` 文件自己就是一条
        （或几条）流，没有任何索引文件。

        ⚠ 这些 ``.XA`` 多半带 44 字节的 ``RIFF…CDXA`` 头，扇区从 ``data``
        之后才开始 —— 偏移由 ``msu1_to_cd._xa_scan_start`` 自动判断，
        这里不用管。
        """
        import psx_txtp                              # 延迟导入：只有真用到才付这个钱
        tracks = psx_txtp.decode_xa_tracks(
            path, cancel=self._cancel,
            on_progress=lambda f: self._prog(50.0 * float(f), "解 XA 音频…"))
        if not tracks:
            raise RuntimeError("文件里没有 XA 音频扇区")
        secs = sum(len(t["pcm"]) / 4.0 / 44100.0 for t in tracks)
        self._log("   ℹ .xa → %d 轨（%s, %s）"
                  % (len(tracks), fmt_dur(secs),
                     fmt_size(sum(len(t["pcm"]) + 44 for t in tracks))))
        return tracks

    # -- 主流程 -----------------------------------------------------------
    def _psf_tracks(self, path):
        """``.psf`` / ``.minipsf`` → 单条音轨（PCM 已在内存里）。

        和 ``.txtp`` 的区别：``.txtp`` 里是 CD-XA 或裸 PCM，纯 Python 能解；
        ``.psf`` 里是**一份 PS1 内存快照 + 一段 PS-X EXE**，必须真的模拟
        R3000A CPU 和 SPU 的 24 个声道。所以这里调 ``psf2wav.exe``
        （源码 ``tools/psf2wav/``，psflib + Highly Experimental + zlib）。

        **需要一份 BIOS**：HE 的 PS1 核心硬性要求，而且要带 "Highly
        Experimental" banner —— 标准做法是把一份 4MB 的 PS2 BIOS 用 HE 的
        ``mkhebios`` 转成 512KB 的 HE BIOS（这一步顺带打进 A0/B0/C0 桩、
        中断派发、事件系统、定时器 2 的 VSync 中断）。没有 BIOS 就解不出声，
        ``psf_native`` 会直接报错，绝不产出静音 WAV 冒充成功。

        ⚠ **v5.9.11：``.psf`` 现在有两个主人。** GameCube 的《Conflict》系列
        也用这个扩展名，但那是 Pivotal 自家的预录流（PSX-ADPCM / NGC-DSP），
        **没有程序段、不需要 BIOS**。判据是第 4 个字节（类型字节）：
        PS1/PS2 是 0x01/0x02/0x11/0x12/0x21/0x22，Pivotal 是
        0x31/0x40/0x60/0x80/0x81/0xA1/0xC0/0xD1，互不重叠。
        判错了症状很误导：会把能解的包报成「BIOS 有问题」。
        """
        import pivotal_psf                            # 延迟导入：只有真用到才付这个钱
        if pivotal_psf.is_pivotal(path):
            r = pivotal_psf.probe(path)
            pcm = pivotal_psf.decode_pcm(
                path, cancel=self._cancel,
                on_progress=lambda f: self._prog(50.0 * float(f), "解 PSF 音源…"))
            if not pcm:
                raise RuntimeError(r.get("error") or "解不出音频")
            self._log("   ℹ .psf（Pivotal）→ %s（%s, %s）"
                      % (r.get("codec") or "Pivotal PSF",
                         fmt_dur(len(pcm) / 4.0 / 44100.0),
                         fmt_size(len(pcm) + 44)))
            return [{"name": pivotal_psf.probe(path)["title"],
                     "pcm": pcm, "little_endian": True}]

        import psf_native                            # 延迟导入：只有真用到才付这个钱
        r = psf_native.decode_pcm(
            path, cancel=self._cancel,
            on_progress=lambda f: self._prog(50.0 * float(f), "解 PSF 音源…"))
        if not r["ok"]:
            raise RuntimeError(r["error"])
        self._log("   ℹ .psf → %s（%s, %s）"
                  % (r["kind"], fmt_dur(r["seconds"]),
                     fmt_size(len(r["pcm"]) + 44)))
        return [{"name": r["title"], "pcm": r["pcm"], "little_endian": True}]

    def _usf_tracks(self, path):
        """``.miniusf`` / ``.usf`` → 单条音轨（PCM 已在内存里）。

        和 ``_psf_tracks`` 几乎一样，两个区别：

        1. **不需要 BIOS**。N64 没有 BIOS 这个概念，核里自带 PIF/CIC 的
           HLE 引导，所以这里没有 ``bios=`` 参数，也没有「没 BIOS 就报错」
           那条路。
        2. 时长只来自 ``length`` / ``fade`` 标签。解码器按标签渲染完
           ``length`` 再**追加** ``fade`` 段做线性淡出 —— 所以下面的 WAV
           写出**不能**再按 ``max_secs`` 截断，否则会把淡出尾巴切掉。

        解码走 ``usf_native`` → ``usf2wav.exe``（psflib + lazyusf2 + zlib）。
        正确性：与上游 lazyusf2 自带的 ``dumpresampled`` 逐样本比对过，
        263 个真实文件最大差 0（``vendor/_check_usf_ref.py``）。
        """
        import usf_native                             # 延迟导入：只有真用到才付这个钱
        r = usf_native.decode_pcm(
            path, cancel=self._cancel,
            on_progress=lambda f: self._prog(50.0 * float(f), "解 USF 音源…"))
        if not r["ok"]:
            raise RuntimeError(r["error"])
        self._log("   ℹ .miniusf → %s（%s, %s）"
                  % (r["kind"], fmt_dur(r["seconds"]),
                     fmt_size(len(r["pcm"]) + 44)))
        return [{"name": r["title"], "pcm": r["pcm"], "little_endian": True}]

    def _vio_tracks(self, path):
        """``.mini2sf`` / ``.2sf`` → 单条音轨（PCM 已在内存里）。

        和 ``_usf_tracks`` 一个模子：不需要 BIOS、时长只来自标签、
        写出时不能按 ``max_secs`` 截断。

        一个 2SF 特有的点：``.mini2sf`` **不一定带内存快照**。带快照的
        是「打几个 ROM 补丁 + SAVE 块」，不带快照的是「自制驱动 + 单曲
        ROM」，从复位开始跑就出声。两种情况 ``vio2wav`` 都处理，这里不用分。

        解码走 ``vio_native`` → ``vio2wav.exe``（psflib + vio2sf + zlib）。
        正确性：与上游 ``audacious-xsf/plugin.cc`` 的 2SF 原始实现逐样本
        比对过，最大差 0（``vendor/_check_2sf_ref.py``）。
        """
        import vio_native                             # 延迟导入：只有真用到才付这个钱
        r = vio_native.decode_pcm(
            path, cancel=self._cancel,
            on_progress=lambda f: self._prog(50.0 * float(f), "解 2SF 音源…"))
        if not r["ok"]:
            raise RuntimeError(r["error"])
        self._log("   ℹ .mini2sf → %s（%s, %s）"
                  % (r["kind"], fmt_dur(r["seconds"]),
                     fmt_size(len(r["pcm"]) + 44)))
        return [{"name": r["title"], "pcm": r["pcm"], "little_endian": True}]

    def _ssf_tracks(self, path):
        """``.ssf`` / ``.minissf`` / ``.dsf`` / ``.minidsf`` → 单条音轨。

        和 ``_vio_tracks`` 一个模子：不需要 BIOS、时长只来自标签、
        写出时不能按 ``max_secs`` 截断。

        一个 exe 管两种机器：核里 ``sega_get_state_size(version)`` 的
        version 1 = Saturn、2 = Dreamcast，对应容器版本 0x11 / 0x12。
        所以四个后缀走同一个解码器，``classify`` 只是把 Saturn 和
        Dreamcast 分开显示（界面上用户看得出是哪台机器的）。

        解码走 ``ssf_native`` → ``ssf2wav.exe``（psflib + Highly
        Theoretical + zlib）。正确性：与上游 ``audacious-xsf/plugin.cc``
        的 SSF/DSF 原始实现逐样本比对过，最大差 0
        （``vendor/_check_ssf_ref.py``）。
        """
        import ssf_native                             # 延迟导入：只有真用到才付这个钱
        r = ssf_native.decode_pcm(
            path, cancel=self._cancel,
            on_progress=lambda f: self._prog(50.0 * float(f), "解 SSF/DSF 音源…"))
        if not r["ok"]:
            raise RuntimeError(r["error"])
        self._log("   ℹ %s → %s（%s, %s）"
                  % (os.path.splitext(path)[1].lower(), r["kind"],
                     fmt_dur(r["seconds"]), fmt_size(len(r["pcm"]) + 44)))
        return [{"name": r["title"], "pcm": r["pcm"], "little_endian": True}]

    def _gsf_tracks(self, path):
        """``.gsf`` / ``.minigsf`` → 单条音轨。

        和 ``_ssf_tracks`` 一个模子：不需要 BIOS、时长只来自标签、
        写出时不能按 ``max_secs`` 截断。

        解码走 ``gsf_native`` → ``gsf2wav.exe``（psflib + mGBA 最小核 +
        zlib）。正确性：与上游 ``audacious-xsf/plugin.cc`` 的 GSF 原始实现
        逐样本比对过，最大差 0（``vendor/_check_gsf_ref.py``）。
        """
        import gsf_native                              # 延迟导入：只有真用到才付这个钱
        r = gsf_native.decode_pcm(
            path, cancel=self._cancel,
            on_progress=lambda f: self._prog(50.0 * float(f), "解 GSF 音源…"))
        if not r["ok"]:
            raise RuntimeError(r["error"])
        self._log("   ℹ %s → %s（%s, %s）"
                  % (os.path.splitext(path)[1].lower(), r["kind"],
                     fmt_dur(r["seconds"]), fmt_size(len(r["pcm"]) + 44)))
        return [{"name": r["title"], "pcm": r["pcm"], "little_endian": True}]

    def _qsf_tracks(self, path):
        """``.qsf`` / ``.miniqsf`` → 单条音轨（PCM 已在内存里）。

        和 ``_gsf_tracks`` 一个模子：不需要 BIOS、时长只来自标签、
        写出时不能按 ``max_secs`` 截断。

        解码走 ``qsf_native`` → ``qsf2wav.exe``（psflib + highly_quixotic
        + zlib）。原生 24038 Hz 在解码器内部重采样到 44100，所以这里
        **不用传采样率** —— 和 ``_gsf_tracks`` 对 32768 的处理方式一致。

        ⚠ 这个包里 45 首的差异**只有 Z80 ROM 偏移 0x21 的那一个字节**
        （曲号），其余 4.4 MB 完全一样。psflib 按 ``_lib`` 标签去**同目录**
        找 ``.qsflib``，所以文件原地不动就能解，不需要整包一起处理。
        """
        import qsf_native                             # 延迟导入：只有真用到才付这个钱
        r = qsf_native.decode_pcm(
            path, cancel=self._cancel,
            on_progress=lambda f: self._prog(50.0 * float(f), "解 QSF 音源…"))
        if not r["ok"]:
            raise RuntimeError(r["error"])
        self._log("   ℹ %s → %s（%s, %s）"
                  % (os.path.splitext(path)[1].lower(), r["kind"],
                     fmt_dur(r["seconds"]), fmt_size(len(r["pcm"]) + 44)))
        return [{"name": r["title"], "pcm": r["pcm"], "little_endian": True}]

    # -- 主流程 -----------------------------------------------------------
    def run(self):                                    # noqa: C901 线性流程
        o = self.opts
        out_dir = o["out_dir"]
        ok = fail = 0
        seconds = 0.0
        results = []
        try:
            os.makedirs(out_dir, exist_ok=True)
            n_src = len(self.sources)
            for si, src in enumerate(self.sources, 1):
                if self._cancel.is_set():
                    self._log("⛔ 已取消")
                    break
                self._log(f"▶ [{si}/{n_src}] {src['label']}  ({src['kind']})")
                # 开局这句要按类型分开写。芯片音源/SPC 的第一批解码要跑几十秒
                # （8 首 × 152 秒音频 / 约 50 倍速 ≈ 24 秒），这期间进度还是 0%。
                # 以前一律写「读取音源…」—— 用户看到的是「0% + 读取音源」，
                # 而 CPU 在满载，第一反应就是「卡住了」。
                self._prog(100.0 * (si - 1) / n_src,
                           "解码芯片音源… 0%"
                           if src["kind"] in ("芯片音源", "SPC") else "读取音源…")
                # 单个音源坏了不能拖垮整批：计一次失败、写一行原因，接着下一个。
                # 早先这里是裸的 —— 一个坏 .txtp 会让整批 20 个音源全部作废，
                # 用户看到的只是一句「转换失败」，前面转好的也不记账。
                try:
                    xa_stats = {}
                    _low = str(src["path"]).lower()
                    # 「单曲最长」只对需要我们自己定长的格式生效：镜像 / .txtp /
                    # .psf / .usf / .2sf / .ssf 都自带 length+fade 标签，解码器
                    # 已按它渲染完，再截一刀正好切掉淡出尾巴（等于毁数据）。
                    # 这段判定原来写在 do_wav 分支里，现在提到解码之前 ——
                    # 解码阶段也要用同一个上限，见下面的 _decode_cap。
                    cap = 0.0 if src["kind"] in ("光盘镜像", "PSX 音频",
                                                 "PSF", "USF", "2SF",
                                                 "SSF", "DSF", "GSF",
                                                 "QSF") \
                        else o["max_secs"]
                    # 解码上限 = 成品上限 + 2 秒余量。
                    # ⚠ 余量不能省：write_tracks 的 _limit_len 只在「超长」时
                    #   才截断并加 1.5s 淡出；正好解到 150 秒就不触发截断，
                    #   成品反而少了淡出尾巴 → 与旧输出不一致。
                    # 不改成品、只省时间：整轨循环的包(SPC 的 Chrono Trigger
                    # 92 首)原来为留前 150 秒硬解满 600 秒，实测 18 分钟 → 4.7 分钟。
                    _decode_cap = (cap + 2.0) if cap else None
                    # 目录形态的 .txtp 包（BGM.XA + 一堆 .txtp）也要走 txtp 分支
                    # —— 光看路径后缀认不出来，得看目录里有没有 .txtp
                    if _low.endswith(TXTP_EXT) or (
                            os.path.isdir(str(src["path"]))
                            and classify(src["path"]) in ("PSX 音频",
                                                          "GameCube 音频",
                                                          "PS4 音频")):
                        tracks = self._txtp_tracks(src["path"])
                    elif _low.endswith(XA_EXT):
                        # 独立 .XA（平铺包里的 15 个 XA 就是这种）
                        tracks = self._xa_tracks(src["path"])
                    elif _low.endswith(PSF_EXTS):
                        tracks = self._psf_tracks(src["path"])
                    elif _low.endswith(USF_EXTS):
                        tracks = self._usf_tracks(src["path"])
                    elif _low.endswith(VIO_EXTS):
                        tracks = self._vio_tracks(src["path"])
                    elif _low.endswith(SSF_EXTS):
                        tracks = self._ssf_tracks(src["path"])
                    elif _low.endswith(GSF_EXTS):
                        tracks = self._gsf_tracks(src["path"])
                    elif _low.endswith(QSF_EXTS):
                        tracks = self._qsf_tracks(src["path"])
                    else:
                        # 文案按音源类型分开：光盘镜像走 XA 抽取，芯片音源是
                        # 逐曲解码。以前一律写「解 XA 音频」—— 芯片音源以前
                        # 根本不上报进度所以没人看见，现在 WSR 会报，写错就露馅。
                        # ⚠ "SPC" 必须一起算进来：纯 .spc 目录的 kind 就是
                        #   "SPC"（见 classify），漏了它 92 首的 SPC 包会显示
                        #   「解 XA 音频…」—— 用户看到的第一反应是「它认错格式了」。
                        _verb = ("解码芯片音源" if src["kind"] in ("芯片音源", "SPC")
                                 else "解 XA 音频")
                        tracks = core.load_source(
                            src["path"],
                            on_progress=lambda f: self._prog(
                                100.0 * (si - 1) / n_src + f * 100.0 / n_src / 2,
                                f"{_verb}… {f * 100:.0f}%"),
                            xa_music_only=bool(o.get("xa_music_only")),
                            xa_stats=xa_stats,
                            xa_split=bool(o.get("xa_split")),
                            decode_cap=_decode_cap)
                    if not tracks:
                        self._log("   ⚠ 未找到音轨, 跳过")
                        continue
                    self._log(f"   {len(tracks)} 条音轨")
                    # XA 改抽 / 只导音乐 / 静音切曲 都要说出来，否则轨数变化莫名其妙
                    if any(" - XA" in t.get("name", "") for t in tracks):
                        secs = sum(t.get("xa_secs") or 0.0 for t in tracks)
                        self._log(
                            "   ℹ 没有可用的 CD-DA 音轨（整轨静音或纯数据盘），"
                            f"已改抽数据轨里的 XA-ADPCM（{len(tracks)} 条, "
                            f"{fmt_dur(secs)}）")
                        if int(xa_stats.get("skipped") or 0):
                            self._log(
                                f"   ℹ 已开启「XA 只导音乐」：{xa_stats['total']} 条流里"
                                f"滤掉 {xa_stats['skipped']} 条音效/语音，"
                                f"保留 {len(tracks)} 条音乐")
                        if int(xa_stats.get("split_streams") or 0):
                            self._log(
                                f"   ℹ 已开启「XA 按静音切曲」："
                                f"{xa_stats['split_streams']} 条流切成 {len(tracks)} 段")
                    name = src["label"]
                    try:
                        if o["do_cd"]:
                            if len(tracks) > 99:
                                self._log("   ⚠ 超过 99 轨, 音频 CD 只保留前 99 轨")
                            n = self._to_cd(tracks, out_dir, name)
                            results.append(("CD", name, n))
                        if o["do_wav"]:
                            # cap 在上面统一算过了（解码阶段也用同一个值，
                            # 见 _decode_cap）。这里只管写盘截断。
                            # 本音源的进度切片：解码 0..50/n，写 WAV 50/n..100/n
                            self._pct_base = (100.0 * (si - 1) / n_src
                                              + 50.0 / n_src)
                            self._pct_span = 50.0 / n_src
                            # fmt 只换容器（WAV / FLAC / MP3），音频规格与解码
                            # 路径完全不变，所以这里不需要任何额外分支。
                            # v5.9.15：MP3 是**有损**，但对解码侧依然透明。
                            _fmt = str(o.get("fmt") or "wav").lower()
                            # v5.9.14：FLAC 嵌封面。**必须在这里显式给** ——
                            # 这一页的 ``out_dir`` 是所有音源共用的一个目录，
                            # 靠 ``write_tracks`` 按目录名猜会猜成同一个名字
                            # （甚至猜成 "FLAC"），一张封面糊到所有游戏上。
                            # 下载页那边反过来：``FLAC/<包名>/`` 每包一个目录，
                            # 所以那边交给自动推断。
                            _cover = ""
                            # v5.9.17：MP3 也能带封面（ID3v2 APIC），
                            # 所以不再只看 flac。
                            if _fmt in spc_wav.ENC_FMTS:
                                try:
                                    import cover_art
                                    _cover = cover_art.cover_for_title(name) or ""
                                except Exception:      # noqa: BLE001
                                    _cover = ""
                            sub = spc_wav.write_tracks(
                                tracks, out_dir, cap,
                                on_log=self._log,
                                on_progress=self._track_prog,
                                cancel=self._cancel,
                                used=self._used_wav,
                                start_idx=self._wav_idx,
                                total=None,
                                fmt=_fmt,
                                cover=_cover)
                            self._wav_idx = sub["last_idx"]
                            ok += sub["ok"]
                            fail += sub["fail"]
                            seconds += sub["seconds"]
                            _tag = _fmt.upper() if _fmt in spc_wav.ENC_FMTS \
                                else "WAV"
                            if sub["fail"]:
                                self._log(
                                    f"   ❌ {_tag} 失败 {sub['fail']} 首: "
                                    + "; ".join(sub["errors"][:3]))
                            self._log(
                                f"   ✅ {_tag} 完成 {sub['ok']} 首, "
                                f"{fmt_dur(sub['seconds'])}")
                            # 多轨合并只支持 WAV（wavmerge 用标准库 wave 读盘）。
                            # FLAC 模式下静默跳过等于「勾了没反应」，所以明说。
                            if _fmt != "wav":
                                if o.get("merge_wav"):
                                    self._log("   ℹ %s 模式下不合并整轨"
                                              "（合并只支持 WAV）" % _fmt.upper())
                            else:
                                self._merge_wavs(name, sub)
                    finally:
                        core.cleanup_spc_temp(tracks)
                    self._prog(100.0 * si / n_src, "完成")
                except Exception as exc:              # noqa: BLE001 兜底给 UI 看原因
                    if self._cancel.is_set():
                        self._log("⛔ 已取消")
                        break
                    fail += 1
                    self._log(f"   ❌ 音源失败：{exc}")
                    continue
            return {
                "ok": ok, "fail": fail, "seconds": seconds,
                "results": results, "cancelled": self._cancel.is_set(),
            }
        except Exception as e:                        # noqa: BLE001 兜底报给 UI
            import traceback
            traceback.print_exc()
            raise RuntimeError(str(e)) from e
