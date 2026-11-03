#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cloud_upload.py — WAV → FLAC → 网易云云盘。**一个 UI 对象都不碰。**

原本这个类长在 ``msu1_gui.py`` 里（v4.27），v5.0 做 Qt 界面时搬到这里 ——
理由很实际：Qt 版不能 import ``msu1_gui``，那会把整个 tkinter 拖进 PyInstaller
的依赖图里（实测 65MB vs 48MB），而上传逻辑本身跟 Tk 一点关系都没有。

用法（在两个窗口的后台线程里）::

    CloudUploader(sess, emit, cancel).run(wavs)

``emit(kind, payload)`` 由工作线程调用，实现里**只能往队列塞**：
kind = ``"log"`` / ``"prog"``(%) / ``"done"``(ok, skip, fail)。
``cancel`` 是可选的 ``threading.Event``：关窗后能尽快收手，不会把几百首 WAV
全传完才发现窗口已经没了。
"""

import os
import time

try:
    import netease_cloud as ncm
except Exception:                                    # noqa: BLE001 缺依赖时置空
    ncm = None

try:
    import wavmerge
except Exception:                                    # noqa: BLE001
    wavmerge = None

#: 合并文件的后缀标记。**只从 wavmerge 取**，不在这里另写一份 ——
#: 命名规则漂移的话，「哪些是合并文件」就会判错，上传里就会出现重复内容。
MERGE_TAG = getattr(wavmerge, "MERGE_TAG", None) or "（合并）"

#: 上传用的音频容器（v6.0.1）。**只有这两个**：FLAC 无损、MP3 省空间。
#: 加新格式要同时改 ``netease_cloud.wav_to_audio`` 与前端 radio 组。
UPLOAD_FMTS = ("flac", "mp3")


def norm_fmt(fmt) -> str:
    """把外部传来的格式串归一化；不认识的**退回 flac**（老行为）。"""
    f = str(fmt or "").strip().lower().lstrip(".")
    return f if f in UPLOAD_FMTS else "flac"


# ---------------------------------------------------------------------------
# 上传前的清单整理（纯函数，两个界面共用）
# ---------------------------------------------------------------------------
def collect_wavs(*folders, deep=False):
    """按目录顺序收集 ``.wav``（去重、排序、跳过不存在的目录）。

    ``deep=True`` 时**递归**往下找。为什么需要：下载页的产物是
    ``WAV/<游戏名>/xx.wav``（v5.2-web-game 起每个游戏一个子目录），
    只扫一层会一首也找不到 —— 用户点「上传云盘」看到「这个目录里没有 WAV」，
    而文件明明就在那儿。

    ⚠ 默认仍是 ``deep=False``：tkinter / Qt 两个界面的输出目录是扁平的，
    改默认值会让「某个子目录里放了备份」也一起传上去。要递归的调用方
    自己显式传。
    """
    seen, out = set(), []
    for d in folders:
        if not d or not os.path.isdir(d):
            continue
        if deep:
            for dirpath, dirs, names in os.walk(d):
                dirs.sort()
                for n in sorted(names):
                    if not n.lower().endswith(".wav"):
                        continue
                    p = os.path.normpath(os.path.join(dirpath, n))
                    if p in seen:
                        continue
                    seen.add(p)
                    out.append(p)
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.lower().endswith(".wav"):
                continue
            p = os.path.normpath(os.path.join(d, n))
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
    return out


def is_merged_wav(path):
    """是不是「多轨合并」产出的整轨文件（``<源名>（合并）.wav``）。"""
    try:
        name = os.path.basename(str(path))
    except (TypeError, ValueError):
        return False
    return name.lower().endswith((MERGE_TAG + ".wav").lower())


def split_merged(wavs):
    """把「合并文件」与「分轨」分开，返回 ``(merged, tracks)``。"""
    merged, tracks = [], []
    for p in (wavs or ()):
        (merged if is_merged_wav(p) else tracks).append(p)
    return merged, tracks


def fmt_bytes(nbytes):
    """人话体积：1.24 GB / 340.5 MB。

    ⚠ MB 保留一位小数：合并文件与分轨的体量本来就只差曲间静音那点
    （几十 MB 的源只差几百 KB），取整会让两行数字看起来一模一样，
    用户没法判断"到底重不重复"。
    """
    if nbytes >= 1e9:
        return "%.2f GB" % (nbytes / 1e9)
    if nbytes >= 1e6:
        return "%.1f MB" % (nbytes / 1e6)
    return "%.1f KB" % (nbytes / 1e3)


def sum_size(paths):
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


class CloudUploader:
    """WAV → FLAC / MP3 → 云盘。**一个 Tk 对象都不碰**。

    ``fmt`` 决定临时文件的容器（v6.0.1）：``"flac"``（默认，无损）或
    ``"mp3"``（320 kbps CBR，约 1/4 体积）。临时文件转完即删，云盘里只留
    最终那一份 —— 所以选 MP3 时**不会**在用户机器上留下 FLAC。
    """

    def __init__(self, sess, emit, cancel=None, fmt="flac"):
        self.sess = sess
        self.emit = emit
        self.cancel = cancel
        self.fmt = norm_fmt(fmt)

    def _emit(self, kind, payload=None):
        try:
            self.emit(kind, payload)
        except Exception:                        # noqa: BLE001
            pass

    # ---- v4.27：核对相关 ---------------------------------------------------
    def _known_md5s(self):
        """批次开始时拉一次「云盘里已有什么」。

        ⚠⚠ 这是**唯一**可信的跳过依据。``upload/check`` 的 ``needUpload``
        只看 NOS 内容库，不代表用户云盘里有这首歌（实测 97.8MB 的文件
        needUpload=false 而云盘 count=0）。拉不到就返回 None：宁可不跳过，
        也不能再出现「日志说跳过、网盘里没有」。
        """
        try:
            known = self.sess.cloud_md5s()
            self._emit("log", "☁ 云盘现有 %d 首（用于跳过判断）" % len(known))
            return known
        except Exception as exc:                     # noqa: BLE001
            self._emit("log", "⚠ 读取云盘列表失败（%s），本次不做跳过判断" % exc)
            return None

    def _one(self, wav, known, force=False):
        """转 FLAC / MP3 + 上传一首；临时文件在这里删掉。返回 upload_flac 的结果。"""
        done, tmp, detail = ncm.wav_to_audio(wav, fmt=self.fmt)
        if not done:
            raise ncm.NeteaseError("转 %s 失败：%s"
                                   % (self.fmt.upper(), detail))
        try:
            self._emit("log", "   %s %.1f MB（%s）"
                       % (self.fmt.upper(), os.path.getsize(tmp) / 1e6, detail))

            # 进度只在跨过 20% 台阶时上报，避免每 256KB 刷一次日志
            state = {"next": 20}

            def _prog(sent, tot, _s=state):
                pct = int(100.0 * sent / max(1, tot))
                if pct >= _s["next"]:
                    _s["next"] = pct + 20
                    self._emit("prog", pct)

            # 每一步都有日志：失败时一眼看出卡在哪一步
            return self.sess.upload_flac(
                tmp, progress=_prog,
                on_step=lambda m: self._emit("log", "   " + m),
                known_md5=known, force=force, fmt=self.fmt)
        finally:
            try:
                os.remove(tmp)                   # 临时文件，传完即删
            except OSError:
                pass

    def _verify(self, claimed, ok, fail, known):
        """核对：日志说成功的那几首，云盘列表里到底在不在。

        为什么值得多打这一轮请求：v4.26 之前「✅ 已上传」只代表接口都回了
        2xx，可云盘里可能什么都没有（提交/发布被静默丢弃、多账号看错等）。
        现在把话说死 —— 不在列表里就重试一次，仍不在就从成功数里扣掉。
        """
        if not claimed or known is None:
            return ok, fail
        self._emit("log", "🔎 核对云盘列表（%d 首）…" % len(claimed))
        try:
            have = self.sess.cloud_md5s()
        except Exception as exc:                     # noqa: BLE001
            self._emit("log", "⚠ 核对失败（%s），本次结果未经验证" % exc)
            return ok, fail

        missing = [(w, m) for w, m in claimed if m not in have]
        if not missing:
            self._emit("log", "✅ 核对通过：%d 首都能在云盘里看到" % len(claimed))
            return ok, fail

        self._emit("log", "⚠ 有 %d 首云盘里查不到，重试一次…" % len(missing))
        for wav, md5 in missing:
            self._emit("log", "   ↻ %s" % os.path.basename(wav))
            try:
                self._one(wav, known, force=True)
                have.add(md5)
            except Exception as exc:                 # noqa: BLE001
                self._emit("log", "      ❌ 重试失败：%s" % exc)

        try:
            have = self.sess.cloud_md5s()
        except Exception:                            # noqa: BLE001
            pass
        still = [m for _, m in missing if m not in have]
        if still:
            self._emit("log", "❌ 最终仍有 %d 首没进云盘（已从成功数里扣掉）"
                       % len(still))
            ok = max(0, ok - len(still))
            fail += len(still)
        else:
            self._emit("log", "✅ 重试后核对通过")
        return ok, fail

    def run(self, wavs):
        ok = skip = fail = 0
        total = len(wavs)
        self._emit("log", "🎛 上传格式：%s%s"
                   % (self.fmt.upper(),
                      "（无损）" if self.fmt == "flac" else "（320 kbps CBR）"))
        known = None
        if self.cancel is None or not self.cancel.is_set():
            known = self._known_md5s()
        claimed = []                              # [(wav, md5)] 声称成功的
        for idx, wav in enumerate(wavs, 1):
            if self.cancel is not None and self.cancel.is_set():
                self._emit("log", "⛔ 已取消上传（还剩 %d 个未传）"
                           % (total - idx + 1))
                break
            name = os.path.basename(wav)
            self._emit("log", "[%d/%d] %s" % (idx, total, name))
            try:
                result = self._one(wav, known)
                if result.get("skipped"):
                    self._emit("log", "   云盘里已有同一文件，跳过")
                    skip += 1
                else:
                    self._emit("log", "   ✅ 已上传（songid=%s）"
                               % result.get("songid"))
                    ok += 1
                    md5 = result.get("md5")
                    if md5:
                        claimed.append((wav, md5))
                        if known is not None:
                            known.add(md5)        # 同批里别重复传
            except Exception as exc:                 # noqa: BLE001
                self._emit("log", "   ❌ %s" % exc)
                fail += 1
            time.sleep(1.2)                          # 限速，别把风控招来

        if self.cancel is None or not self.cancel.is_set():
            ok, fail = self._verify(claimed, ok, fail, known)
        self._emit("done", (ok, skip, fail))
