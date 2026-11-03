#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qt_ui.player_bar — 右下角试听播放条（Qt 版）。

业务逻辑**全部**来自 ``player.py``：``PcmPlayer`` 一个 Tk/Qt 对象都不碰，只
暴露 ``position()`` / ``is_playing()`` 这类轮询接口。这里只做三件事：

1. **后台线程解码** —— 150 秒的芯片曲目同步解会卡住界面（``libgme`` 实测
   30 秒单轨 0.13 秒，150 秒就是 0.65 秒；CD 镜像整轨更久）。
2. **100ms 定时器刷进度** —— 播放位置按**墙钟**推算，不看读指针
   （``player.py`` 的设计前提 ④：设备会预读缓冲，读指针会跑到前面）。
3. **播放 / 暂停 / 停止 / 换轨 / seek / 音量** 六个交互。

⚠ 三条踩过的坑，改之前先读：

* ``QSlider`` 默认点凹槽是 ``pageStep`` 翻页，不是跳到点击位置 —— 试听时
  最常用的操作就是「跳到 1:30 听副歌」，所以 ``_SeekSlider`` 自己算位置。
* ``PcmPlayer.pause()`` 走的是「继续喂静音但不推进位置」，设备不停，所以
  暂停/恢复/seek 都是瞬时的。**不要**改成 ``device.stop()``（实测 424ms）。
* 播放器构造可能失败（没声卡 / WASAPI 被独占），失败时整个播放条置灰，
  不能抛异常 —— 转换功能不该因为没声卡就用不了。
"""

import os

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget,
)

import msu1_to_cd as core

#: 本工具自己能解的扩展名（与 ``msu1_gui.SELF_PLAYABLE_EXTS`` 同一份事实源
#: —— 那边是 tkinter 版，这里不能 import 它，否则 PyInstaller 会把 tkinter
#: 整个打进来）。两处改动必须同步。
SELF_PLAYABLE_EXTS = frozenset(
    {".spc", ".nsf", ".nsfe", ".gbs", ".hes", ".kss", ".sap", ".gym", ".ay",
     ".vgm", ".vgz", ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".cue",
     ".bin", ".iso", ".img", ".raw"})

#: 进度轮询间隔。100ms 是「看不出卡顿」和「别白烧 CPU」的折中。
POLL_MS = 100


def fmt_time(sec):
    try:
        sec = max(0, int(float(sec)))
    except (TypeError, ValueError):
        sec = 0
    return "%d:%02d" % (sec // 60, sec % 60)


class _SeekSlider(QSlider):
    """点凹槽直接跳过去，而不是翻一页。

    ``QSlider`` 默认的 ``pageStep`` 行为在试听里很难用：想听 1:30 得点好几下。
    """

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self.maximum() > 0:
            w = self.width()
            frac = min(1.0, max(0.0, ev.position().x() / max(1, w)))
            self.setValue(int(round(frac * self.maximum())))
            self.sliderMoved.emit(self.value())
            ev.accept()
            return
        super().mousePressEvent(ev)


class LoadClipThread(QThread):
    """后台解码一个音源成 ``AudioClip``。只发信号，不碰任何控件。"""

    ready = Signal(object)
    error = Signal(str)

    def __init__(self, path, track=0, parent=None):
        super().__init__(parent)
        self.path = str(path)
        self.track = int(track)

    def run(self):
        try:
            import player as pm
            ext = os.path.splitext(self.path)[1].lower()
            if ext in (".cue",) + core.DISC_IMG_EXTS:
                clip = pm.AudioClip.from_cue(self.path, self.track)
            elif ext in (".wav", ".mp3", ".flac", ".ogg", ".m4a"):
                clip = pm.AudioClip.from_audio_file(self.path)
            else:
                clip = pm.AudioClip.from_chip(self.path, self.track)
            self.ready.emit(clip)
        except Exception as e:                        # noqa: BLE001 兜底报给 UI
            self.error.emit(str(e))


class PlayerBar(QWidget):
    """播放条。``on_log(str)`` 用来把播放事件写进主窗日志。"""

    def __init__(self, on_log=None, parent=None):
        super().__init__(parent)
        self._log = on_log or (lambda _t: None)
        self.player = None
        self.clip = None
        self.path = ""
        self.track = 0
        self.tracks = [(0, "整首")]
        self._loading = False
        self._thread = None
        self._dragging = False

        try:
            import player as pm
            self.player = pm.PcmPlayer()
        except Exception as e:                        # noqa: BLE001 没声卡也得能转
            self._log("⚠ 试听不可用（音频子系统初始化失败）：%s" % e)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._tick)

        self._build()
        self._refresh()

    # ------------------------------------------------------------------ 构建
    def _build(self):
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        self.b_play = QPushButton("▶")
        self.b_play.setObjectName("small")
        self.b_play.setFixedWidth(38)
        self.b_play.setToolTip("播放 / 暂停")
        self.b_stop = QPushButton("■")
        self.b_stop.setObjectName("small")
        self.b_stop.setFixedWidth(38)
        self.b_stop.setToolTip("停止")
        r1.addWidget(self.b_play)
        r1.addWidget(self.b_stop)

        self.sl_prog = _SeekSlider(Qt.Horizontal)
        self.sl_prog.setRange(0, 1000)
        self.sl_prog.setValue(0)
        r1.addWidget(self.sl_prog, 1)

        self.lb_time = QLabel("0:00 / 0:00")
        self.lb_time.setObjectName("mono")
        self.lb_time.setMinimumWidth(88)
        r1.addWidget(self.lb_time)

        r1.addSpacing(6)
        self.lb_vol = QLabel("VOL")
        self.lb_vol.setObjectName("mono")
        r1.addWidget(self.lb_vol)
        self.sl_vol = QSlider(Qt.Horizontal)
        self.sl_vol.setRange(0, 100)
        self.sl_vol.setValue(100)
        self.sl_vol.setFixedWidth(104)
        r1.addWidget(self.sl_vol)
        col.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(6)
        self.lb_name = QLabel("双击音源列表里的条目试听")
        self.lb_name.setObjectName("muted")
        r2.addWidget(self.lb_name, 1)
        self.b_prev = QPushButton("◀")
        self.b_prev.setObjectName("small")
        self.b_prev.setFixedWidth(30)
        self.b_prev.setToolTip("上一轨")
        self.b_next = QPushButton("▶")
        self.b_next.setObjectName("small")
        self.b_next.setFixedWidth(30)
        self.b_next.setToolTip("下一轨")
        r2.addWidget(self.b_prev)
        r2.addWidget(self.b_next)
        col.addLayout(r2)

        self.b_play.clicked.connect(self.toggle)
        self.b_stop.clicked.connect(self.stop)
        self.b_prev.clicked.connect(lambda: self.switch_track(-1))
        self.b_next.clicked.connect(lambda: self.switch_track(1))
        self.sl_prog.sliderPressed.connect(self._drag_start)
        self.sl_prog.sliderReleased.connect(self._drag_end)
        self.sl_vol.valueChanged.connect(self._on_volume)

    # ------------------------------------------------------------------ 状态
    def _refresh(self):
        has = self.clip is not None
        ok = has and self.player is not None
        self.b_play.setEnabled(ok)
        self.b_stop.setEnabled(ok)
        self.b_prev.setEnabled(ok and len(self.tracks) > 1)
        self.b_next.setEnabled(ok and len(self.tracks) > 1)
        playing = bool(ok and self.player.is_active()
                       and not self.player.is_paused())
        self.b_play.setText("⏸" if playing else "▶")
        self.lb_time.setText("%s / %s" % (
            fmt_time(self.player.position() if ok else 0),
            fmt_time(self.player.duration() if ok else 0)))

    # ------------------------------------------------------------------ 载入
    def play_path(self, path, track=0):
        """载入并播放一个源。同一个源再点一次 = 播放/暂停切换。"""
        path = str(path or "")
        if not path or not os.path.exists(path):
            self._log("❌ 试听失败：源文件不存在")
            return
        if os.path.splitext(path)[1].lower() not in SELF_PLAYABLE_EXTS:
            self._log("⚠ 本工具解不了 %s，请用 foobar2000 打开"
                      % os.path.splitext(path)[1])
            return
        if self.player is None:
            self._log("❌ 试听不可用：音频子系统未初始化")
            return
        if path == self.path and self.player.is_active():
            self.toggle()
            return
        if self._loading:
            return
        self._loading = True
        self.path = path
        self.track = int(track)
        try:
            import player as pm
            self.tracks = pm.AudioClip.list_tracks(path) or [(0, "整首")]
        except Exception:                             # noqa: BLE001 列不出就当整首
            self.tracks = [(0, "整首")]
        self.lb_name.setText("解码中… %s" % os.path.basename(path))
        self._thread = LoadClipThread(path, int(track), self)
        self._thread.ready.connect(self._on_ready)
        self._thread.error.connect(self._on_error)
        self._thread.start()

    def _on_ready(self, clip):
        self._loading = False
        self.clip = clip
        try:
            self.player.load(clip)
            self.player.play()
        except Exception as e:                        # noqa: BLE001
            self._log("❌ 试听失败：%s" % e)
            self.lb_name.setText("试听失败")
            return
        self.lb_name.setText("%s  ·  %s  ·  %s"
                             % (clip.name, clip.detail, fmt_time(clip.secs)))
        self._timer.start()
        self._refresh()
        self._log("▶ 试听 %s（%s，%s）"
                  % (clip.name, clip.detail, fmt_time(clip.secs)))

    def _on_error(self, text):
        self._loading = False
        self.lb_name.setText("试听失败")
        self._log("❌ 试听失败：" + text)

    # ------------------------------------------------------------------ 控制
    def toggle(self):
        pl = self.player
        if pl is None or pl.clip is None:
            return
        try:
            if pl.is_active():
                pl.resume() if pl.is_paused() else pl.pause()
            else:
                pl.play()
        except Exception as e:                        # noqa: BLE001
            self._log("❌ 播放失败：%s" % e)
            return
        self._timer.start()
        self._refresh()

    def stop(self):
        if self.player is None:
            return
        try:
            self.player.stop()
        except Exception:                             # noqa: BLE001
            pass
        self._timer.stop()
        self.sl_prog.setValue(0)
        self._refresh()

    def switch_track(self, delta):
        """◀▶ 换轨。``self.tracks`` 是 ``[(index, title)…]``。"""
        if not self.tracks:
            return
        pos = 0
        for i, (idx, _t) in enumerate(self.tracks):
            if idx == self.track:
                pos = i
                break
        pos = (pos + int(delta)) % len(self.tracks)
        self.play_path(self.path, self.tracks[pos][0])

    def _drag_start(self):
        self._dragging = True

    def _drag_end(self):
        self._dragging = False
        self._seek(self.sl_prog.value())

    def _seek(self, value):
        pl = self.player
        if pl is None or pl.clip is None or pl.duration() <= 0:
            return
        try:
            pl.seek(self.sl_prog.value() / 1000.0 * pl.duration())
        except Exception as e:                        # noqa: BLE001
            self._log("❌ 跳转失败：%s" % e)

    def _on_volume(self, v):
        if self.player is None:
            return
        try:
            self.player.set_volume(v / 100.0)
        except Exception:                             # noqa: BLE001
            pass

    def _tick(self):
        pl = self.player
        if pl is None or pl.clip is None:
            self._timer.stop()
            return
        dur = pl.duration()
        pos = pl.position()
        if not self._dragging and dur > 0:
            self.sl_prog.setValue(int(round(1000.0 * pos / dur)))
        self.lb_time.setText("%s / %s" % (fmt_time(pos), fmt_time(dur)))
        if pl.has_finished():
            self._timer.stop()
            self._refresh()

    # ------------------------------------------------------------------ 收尾
    def shutdown(self):
        self._timer.stop()
        if self._thread is not None and self._thread.isRunning():
            self._thread.wait(1500)
        if self.player is not None:
            try:
                self.player.close()
            except Exception:                         # noqa: BLE001
                pass
            self.player = None
