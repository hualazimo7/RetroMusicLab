"""音频播放：WAV / CD 镜像(BIN+CUE) / 原始芯片音源 —— 统一成 PCM 播放。

======================================================================
设计前提（2026-10-18 实测，改之前先读）
======================================================================

1. **三种格式本来就归一化到同一种数据**
   * WAV        : 44.1k/16bit/立体声 PCM + 44 字节头
   * CD-DA(BIN) : 本身就是 44.1k/16bit/立体声**裸 PCM**（1 扇区 = 2352 字节
                  = 588 帧），CUE 只是索引 —— **零解码**
   * 芯片音源    : libgme 解码，实测 30 秒单轨耗时 0.13 秒
   所以播放器只需要一个「PCM 片段」+ 一个「播放设备」，不需要三套逻辑。

2. **必须用 miniaudio 而不是 winsound/MCI**
   它已经是打包依赖（`MSU1Converter.spec` 里有 `collect_all('miniaudio')`，
   原本用于解 MP3），且实测设备就是 `WASAPI 44100Hz 2ch` —— 与我们的 PCM
   格式完全一致，**零重采样**。换成 MCI 要落临时文件，winsound 根本不支持
   seek/暂停。

3. ⚠ **暂停不能用 `device.stop()`** —— 实测 stop 要 **424ms**（等缓冲排空），
   手感是「点暂停还要响半秒」。所以暂停走「**继续喂静音但不推进位置**」，
   设备全程不停，暂停/恢复/seek 都是瞬时的。

4. ⚠ **进度不能用 generator 的读指针算** —— 实测 1.5 秒的音频在 1.0 秒时
   读指针已经走完（设备会预读缓冲）。所以播放位置一律按
   **墙钟时间**（`time.monotonic`）推算，读指针只用来取数据。

5. ⚠ **generator 必须自己 priming**（miniaudio `start()` 的文档原话：
   "The generator should already be started before passing it in"）。
   首次 `next(gen)` 返回空块，之后 `send(framecount)` 才是取数据。
   不 priming 会报 `can't send non-None value to a just-started generator`。

======================================================================
线程模型（与项目其它模块同一纪律）
======================================================================
音频回调跑在 **miniaudio 自己的线程**里，绝对不碰 Tk。UI 侧只轮询
:meth:`PcmPlayer.position` / :meth:`PcmPlayer.is_playing` 刷新界面。
"""

from __future__ import annotations

import os
import struct
import time

SR = 44100                 # 全项目统一的采样率
NCH = 2
SAMPLE_WIDTH = 2           # 16bit
BYTES_PER_FRAME = NCH * SAMPLE_WIDTH      # 4
BYTES_PER_SEC = SR * BYTES_PER_FRAME      # 176400

# CD-DA 扇区大小。2352 = 纯音频；2448 = 含 96 字节 subchannel（部分抓轨）。
CD_SECTOR = 2352
CD_SECTOR_SUB = 2448
CD_FRAMES_PER_SEC = 75     # CD 的 1 秒 = 75 帧（CUE 的 MM:SS:FF 就是它）


def scale_pcm(raw: bytes, vol: float) -> bytes:
    """把 16bit 小端 PCM 整体乘一个系数（音量）。

    优先用 numpy（已是打包依赖，单块 4096 帧只要约 0.05ms）；
    没有 numpy 时退回 array —— 每块约 1ms，而一块要播 93ms，仍然安全。
    ``vol >= 1.0`` 时**直接原样返回**，不做任何拷贝（默认音量零开销）。
    """
    if vol >= 0.999:
        return raw
    vol = max(0.0, vol)
    try:
        import numpy as np
        a = np.frombuffer(raw, dtype="<i2")
        # 先 float 再截断，避免 int16 乘法溢出
        return (a.astype(np.float32) * vol).astype("<i2").tobytes()
    except ImportError:                                      # pragma: no cover
        import array as _array
        a = _array.array("h")
        a.frombytes(raw)
        for i, v in enumerate(a):
            a[i] = int(v * vol)
        return a.tobytes()


class PlayerError(RuntimeError):
    """播放相关错误（文案可直接展示给用户）。"""


# ---------------------------------------------------------------------------
# 统一音频片段
# ---------------------------------------------------------------------------

class AudioClip:
    """一段可直接播放的 PCM：16bit / 44100Hz / 立体声 / 小端。

    构造后 :attr:`pcm` 就是原始字节，长度 = ``frames * 4``。
    """

    __slots__ = ("pcm", "name", "detail")

    def __init__(self, pcm: bytes, name: str = "", detail: str = ""):
        if len(pcm) % BYTES_PER_FRAME:
            # 补齐到整帧，否则最后一帧半截会让播放器计算错位
            pcm = pcm[:len(pcm) - (len(pcm) % BYTES_PER_FRAME)]
        self.pcm = pcm
        self.name = name
        self.detail = detail        # 例如「第 3 / 17 轨」或「CD 第 2 轨」

    @property
    def frames(self) -> int:
        return len(self.pcm) // BYTES_PER_FRAME

    @property
    def secs(self) -> float:
        return self.frames / float(SR)

    def __repr__(self) -> str:                                   # pragma: no cover
        return "<AudioClip %s %.1fs>" % (self.name or "?", self.secs)

    # -- 载入器 ------------------------------------------------------------

    @classmethod
    def from_audio_file(cls, path: str, name: str = "") -> "AudioClip":
        """WAV / MP3 等「音频容器」文件 —— 交给 miniaudio 解码并统一格式。

        用 ``decode_file`` 而不是自己解析 WAV：它会顺带处理采样率/声道不一致
        （非 44.1k 的 WAV 会被重采样），而试听场景对这点质量损失不敏感。
        """
        try:
            import miniaudio
        except ImportError as e:                                 # pragma: no cover
            raise PlayerError("缺少 miniaudio，无法播放：%s" % e)
        try:
            d = miniaudio.decode_file(
                path,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=NCH, sample_rate=SR)
        except Exception as e:                                   # noqa: BLE001
            raise PlayerError("解码失败：%s（%s）" % (os.path.basename(path), e))
        return cls(bytes(d.samples), name or os.path.basename(path))

    @classmethod
    def from_cue(cls, cue_or_bin: str, track_pos: int = 0,
                 name: str = "") -> "AudioClip":
        """CD 镜像里的一条 CD-DA 音轨（**零解码**，直接切裸 PCM）。

        ``track_pos`` 是「第几条 AUDIO 轨」（0 基，按 CUE 顺序），不是 CUE 里的
        轨道号 —— 数据轨会被跳过，两者通常不同。
        """
        import msu1_to_cd as core          # 复用已有的 CUE 解析，避免两套实现

        cue_path = cue_or_bin
        if cue_or_bin.lower().endswith(".bin"):
            cand = os.path.splitext(cue_or_bin)[0] + ".cue"
            if not os.path.exists(cand):
                raise PlayerError("%s：未找到同名 .cue，无法定位音轨"
                                  % os.path.basename(cue_or_bin))
            cue_path = cand
        try:
            parsed = core.parse_cue(cue_path)
        except Exception as e:                                   # noqa: BLE001
            raise PlayerError("CUE 解析失败：%s" % e)

        audios = [t for t in parsed
                  if t.get("type") == "AUDIO" and t.get("start") is not None]
        if not audios:
            raise PlayerError("这张镜像里没有 AUDIO 轨（可能全是数据轨）")
        if not (0 <= track_pos < len(audios)):
            raise PlayerError("轨道序号越界：%d（共 %d 条 AUDIO 轨）"
                              % (track_pos, len(audios)))

        base = os.path.dirname(os.path.abspath(cue_path))
        cur = audios[track_pos]
        fname = cur.get("file") or ""
        bin_path = os.path.join(base, fname) if fname else os.path.splitext(cue_path)[0] + ".bin"
        if not os.path.exists(bin_path):
            raise PlayerError("找不到镜像文件：%s" % os.path.basename(bin_path))

        size = os.path.getsize(bin_path)
        # 2448 的盘（带 subchannel）也能播：按扇区切掉那 96 字节即可
        if size % CD_SECTOR == 0:
            sector, has_sub = CD_SECTOR, False
        elif size % CD_SECTOR_SUB == 0:
            sector, has_sub = CD_SECTOR_SUB, True
        else:
            # 两种都对不上：按 2352 强行切，能播多少算多少
            sector, has_sub = CD_SECTOR, False

        start_sector = int(cur["start"])
        off = start_sector * sector
        # 终点 = 下一条 AUDIO 轨的起点（CUE 不写轨道长度）；末轨到文件尾
        nxt = None
        for t in audios[track_pos + 1:]:
            if (t.get("file") or "") == fname and t.get("start") is not None:
                nxt = int(t["start"])
                break
        end = (nxt * sector) if nxt is not None else size
        end = min(end, size)
        if end <= off:
            raise PlayerError("该轨长度异常（起点 %d ≥ 终点 %d），镜像可能损坏"
                              % (off, end))

        try:
            with open(bin_path, "rb") as fh:
                fh.seek(off)
                raw = fh.read(end - off)
        except OSError as e:
            raise PlayerError("读取镜像失败：%s" % e)

        if has_sub:
            # 每 2448 字节只保留前 2352（user data），丢掉 subchannel
            chunks = []
            for i in range(0, len(raw), CD_SECTOR_SUB):
                chunks.append(raw[i:i + CD_SECTOR])
            raw = b"".join(chunks)

        # ⚠⚠ **CD-DA 是 big-endian（MSB first），本项目的 PCM 全是小端。**
        # 这里必须字节交换，否则每个样本的高低字节颠倒 —— 听感是一片噪声。
        # 实测（2026-10-18，真实产物 `102 Chrono Trigger.bin`）：
        #   相邻样本差中位数  按小端解释 = 19197 / 按大端解释 = 369（差 52 倍）
        # 大端才让波形连续，所以文件确实是大端。
        # 这条对**外部导入的 CD 镜像同样成立**（CD-DA 标准就是大端）。
        import array as _array
        if len(raw) % 2:                     # 理论不会发生（2352 是偶数）
            raw = raw[:len(raw) - 1]
        arr = _array.array("h")
        arr.frombytes(raw)
        arr.byteswap()
        raw = arr.tobytes()

        num = cur.get("num") or (track_pos + 1)
        title = (cur.get("title") or "").strip()
        detail = "CD 第 %s 轨" % num
        if title:
            detail += "：%s" % title
        return cls(raw, name or os.path.basename(bin_path), detail)

    @classmethod
    def from_chip(cls, path: str, track_index: int = 0,
                  max_secs: float = 0.0) -> "AudioClip":
        """芯片音源（SPC/NSF/GBS/HES/KSS/SAP/GYM/AY/VGM）的一条轨。

        ``max_secs`` 传 0 表示「按文件自身/默认时长（150s）完整解码」。
        实测 30 秒单轨只需 0.13 秒，完整曲目也在 1 秒内，所以同步解码可接受。
        """
        import chip_decode
        import chip_formats as cf

        # ⚠ 先判「这个格式到底能不能解」，再进 libgme。
        # libgme 只覆盖 11 种扩展名（CHIP_EXTS），USF/SSF/DSF/2SF/GSF/WSR/AIF
        # 这 7 种是「内存快照 + 各自机器码」，需要对应机种的模拟器，libgme 根本没有
        # 它们的实现 —— 直接喂进去只会得到一句底层解码错误，用户看不出原因。
        # 注意：**不能用 cf.unsupported_reason() 判断** —— 它对「已支持」的格式
        # 也会返回一句「是已支持的格式，不属于不支持列表」的文案，恒真。
        ext = os.path.splitext(path)[1].lower()
        if ext not in cf.CHIP_EXTS:
            try:
                info = cf.probe(path)
            except Exception:                                    # noqa: BLE001
                info = None
            reason = cf.unsupported_reason(path, info) or ""
            raise PlayerError(reason.strip() or ("暂不支持 %s 格式的试听" % ext))

        try:
            info = cf.probe(path)
            n_tracks = int(getattr(info, "n_tracks", 0) or 0)
        except Exception:                                        # noqa: BLE001
            info, n_tracks = None, 0

        # 只解需要的那一条：用 worker 的 "path\x1ffirst\x1fcount" 切分约定，
        # 避免为了播第 3 轨把 17 轨全解一遍。
        item = "%s\x1f%d\x1f1" % (path, track_index)
        try:
            tracks = chip_decode.load_chip_paths(
                [item], default_secs=max_secs or 150.0,
                cap_secs=600.0, fade_ms=0)
        except Exception as e:                                   # noqa: BLE001
            raise PlayerError("解码失败：%s（%s）" % (os.path.basename(path), e))
        if not tracks:
            raise PlayerError("这条轨解不出声音（可能是空轨）")

        pcm = bytes(tracks[0]["pcm"])
        detail = "第 %d 轨" % (track_index + 1)
        if n_tracks > 1:
            detail += " / 共 %d 轨" % n_tracks
        return cls(pcm, name=os.path.basename(path), detail=detail)

    # -- 探测（给 UI 用，不真正解码）--------------------------------------

    @staticmethod
    def list_tracks(path: str):
        """返回 [(pos, 显示名)]，供 UI 让用户选轨。失败返回空列表。

        * 芯片音源：一轨一项
        * CD 镜像  ：一条 AUDIO 项
        * 其它     ：只有一项（整体播放）
        """
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in (".cue", ".bin"):
                import msu1_to_cd as core
                cue = path
                if ext == ".bin":
                    cue = os.path.splitext(path)[0] + ".cue"
                    if not os.path.exists(cue):
                        return []
                audios = [t for t in core.parse_cue(cue)
                          if t.get("type") == "AUDIO" and t.get("start") is not None]
                out = []
                for i, t in enumerate(audios):
                    label = "CD 第 %s 轨" % (t.get("num") or i + 1)
                    if (t.get("title") or "").strip():
                        label += "：%s" % t["title"].strip()
                    out.append((i, label))
                return out
            import chip_formats as cf
            if ext in cf.CHIP_EXTS:
                info = cf.probe(path)
                n = int(getattr(info, "n_tracks", 0) or 1)
                return [(i, "第 %d 轨" % (i + 1)) for i in range(n)]
        except Exception:                                        # noqa: BLE001
            return []
        return [(0, "整首")]


# ---------------------------------------------------------------------------
# 播放器
# ---------------------------------------------------------------------------

class PcmPlayer:
    """一个「单曲循环器」：load → play / pause / resume / seek → stop。

    只做一件事：把 :class:`AudioClip` 的 PCM 连续送给 WASAPI。
    不做播放列表、不自动下一首（用户明确不要）。
    """

    def __init__(self):
        self._dev = None
        self._clip = None
        self._gen = None
        self._playing = False          # 设备在跑
        self._paused = False
        # 位置用「墙钟基准」表达：audible_pos = _base_sec + (now - _base_wall)
        self._base_sec = 0.0
        self._base_wall = 0.0
        self._frozen_sec = 0.0         # 暂停/停止时的定格位置
        # 读写指针（设备线程读，UI 线程写；int 赋值在 CPython 下是原子的）
        self._read_frame = 0
        self._ended = False
        # 音量 0.0~1.0（设备线程读、UI 线程写；float 单次赋值在 CPython 下原子）
        self._volume = 1.0

    # -- 生命周期 ----------------------------------------------------------

    @property
    def clip(self):
        return self._clip

    def load(self, clip: AudioClip) -> None:
        """换曲（会先停掉当前播放）。"""
        self.stop()
        self._clip = clip
        self._base_sec = 0.0
        self._frozen_sec = 0.0
        self._read_frame = 0
        self._ended = False

    def play(self) -> None:
        """从当前位置开始/继续播放。"""
        if self._clip is None:
            return
        if self._playing:
            if self._paused:
                self.resume()
            return
        if self._ended or self._frozen_sec >= self._clip.secs - 0.01:
            # 播完了再点播放 → 从头来
            self._frozen_sec = 0.0
            self._read_frame = 0
            self._ended = False

        self._ensure_device()
        self._paused = False
        self._base_sec = self._frozen_sec
        self._base_wall = time.monotonic()
        self._read_frame = int(self._base_sec * SR)

        self._gen = _PcmGenerator(self)
        next(self._gen)                     # ← priming，必须（见模块头第 5 条）
        try:
            self._dev.start(self._gen)
        except Exception as e:                                   # noqa: BLE001
            raise PlayerError("无法启动音频设备：%s" % e)
        self._playing = True

    def pause(self) -> None:
        """暂停：**不停设备**，只是不再推进位置（见模块头第 3 条）。"""
        if not self._playing or self._paused:
            return
        self._frozen_sec = self.position()
        self._paused = True

    def resume(self) -> None:
        if not self._playing or not self._paused:
            return
        self._paused = False
        self._base_sec = self._frozen_sec
        self._base_wall = time.monotonic()

    def stop(self) -> None:
        """真正停止（停设备并回到 0）。"""
        self._frozen_sec = 0.0
        self._base_sec = 0.0          # ← 必须一起归零：position() 在非播放态读 _base_sec
        self._read_frame = 0
        self._paused = False
        self._ended = False           # 停止 ≠ 播完，否则下次 play 会被当成「已结束」
        self._shutdown_device()

    def seek(self, sec: float) -> None:
        """跳到指定秒。播放中跳转不会中断声音（只换读指针）。"""
        if self._clip is None:
            return
        sec = max(0.0, min(float(sec), self._clip.secs))
        self._read_frame = int(sec * SR)
        self._frozen_sec = sec
        self._ended = False
        if self._playing and not self._paused:
            self._base_sec = sec
            self._base_wall = time.monotonic()

    def close(self) -> None:
        self.stop()
        self._clip = None

    def _ensure_device(self) -> None:
        if self._dev is not None:
            return
        try:
            import miniaudio
        except ImportError as e:                                 # pragma: no cover
            raise PlayerError("缺少 miniaudio，无法播放：%s" % e)
        try:
            self._dev = miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=NCH, sample_rate=SR)
        except Exception as e:                                   # noqa: BLE001
            raise PlayerError("找不到可用的音频输出设备：%s" % e)

    def _shutdown_device(self) -> None:
        self._playing = False
        self._gen = None
        if self._dev is not None:
            try:
                self._dev.stop()
            except Exception:                                    # noqa: BLE001
                pass
            try:
                self._dev.close()
            except Exception:                                    # noqa: BLE001
                pass
            self._dev = None

    # -- 状态查询（UI 轮询用）----------------------------------------------

    def is_playing(self) -> bool:
        return self._playing and not self._paused and not self._ended

    def is_active(self) -> bool:
        """设备在跑（含暂停态）。UI 用这个决定要不要继续轮询。"""
        return self._playing and not self._ended

    def is_paused(self) -> bool:
        return self._paused

    def has_finished(self) -> bool:
        """整首播完了（UI 看到它变 True 就该 stop() 释放设备）。"""
        return self._ended

    def position(self) -> float:
        """当前播放位置（秒），按墙钟推算（见模块头第 4 条）。"""
        if self._clip is None:
            return 0.0
        if self._ended:
            pos = self._clip.secs
        elif self._paused or not self._playing:
            pos = self._frozen_sec
        else:
            pos = self._base_sec + (time.monotonic() - self._base_wall)
        return max(0.0, min(pos, self._clip.secs))

    def set_volume(self, v) -> None:
        """设置音量（0.0~1.0）。播放中改也立即生效。"""
        try:
            v = float(v)
        except (TypeError, ValueError):
            return
        self._volume = max(0.0, min(1.0, v))

    def volume(self) -> float:
        return self._volume

    def duration(self) -> float:
        return self._clip.secs if self._clip is not None else 0.0


class _PcmGenerator:
    """miniaudio 的数据源。

    必须实现 :meth:`__next__`（priming，返回空块）与 :meth:`send`。
    暂停时返回等长静音**且不推进 ``_read_frame``** —— 这是暂停零延迟的关键。
    """

    __slots__ = ("_p",)

    def __init__(self, player: PcmPlayer):
        self._p = player

    def __iter__(self):
        return self

    def __next__(self):
        return b""                       # priming：不产出数据

    def send(self, framecount: int):
        p = self._p
        clip = p._clip
        if clip is None or p._ended:
            raise StopIteration

        need_bytes = int(framecount) * BYTES_PER_FRAME
        if p._paused:
            # 暂停：喂静音，位置不动（设备继续跑，恢复时零延迟）
            return b"\x00" * need_bytes
        if p._ended:
            raise StopIteration

        start = p._read_frame * BYTES_PER_FRAME
        total = len(clip.pcm)
        if start >= total:
            p._ended = True
            p._frozen_sec = clip.secs
            raise StopIteration
        chunk = clip.pcm[start:start + need_bytes]
        p._read_frame += len(chunk) // BYTES_PER_FRAME
        # 音量在这里应用：读指针按**原始字节**推进，不受音量影响
        if p._volume < 0.999:
            chunk = scale_pcm(chunk, p._volume)
        if p._read_frame * BYTES_PER_FRAME >= total:
            p._ended = True
            p._frozen_sec = clip.secs
        return chunk
