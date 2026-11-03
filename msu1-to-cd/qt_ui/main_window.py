#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qt_ui.main_window — 主窗（Qt 版）。

布局与 tkinter 版**分区一一对应**，只是形态现代化了：

    顶栏：标题 + 副标题                      主题下拉
    左栏：① 音源  ② 输出格式  ③ 时间与淡出  ④ 选项
    右栏：日志
    底部：开始 / 取消 / 打开目录 / 上传云盘   进度 + 状态

刻意保留的东西（不做「现代化」删减）：
  * 「时间与淡出」四个输入框 —— 老用户按肌肉记忆找它们
  * 日志里 XA 改抽 / 只导音乐 / 静音切曲 的三条提示 —— 轨数变了要能解释
  * 光盘镜像不吃「单曲最长」的口径
"""

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

import msu1_to_cd as core
from qt_ui import theme as th
from qt_ui import widgets as W
from qt_ui.player_bar import PlayerBar
from qt_ui.worker import ConvertWorker, classify, fmt_dur, fmt_size

APP_VER = "5.0-qt"

#: 记住的主题 / 输出目录（与 tkinter 版同一个 json，切回来还能认）
_UI_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                       "MSU1Converter")


def _store_path():
    os.makedirs(_UI_DIR, exist_ok=True)
    return os.path.join(_UI_DIR, "qt_ui.json")


def _load_store():
    import json
    try:
        with open(_store_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_store(data):
    import json
    try:
        with open(_store_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.sources = []
        self.worker = None
        self._reg = th.registry()
        self._store = _load_store()
        self._theme_key = self._store.get("theme") or "modern_light"
        if self._theme_key not in self._reg:
            self._theme_key = "modern_light"
        self._check_icon = ""
        self._shot_mode = False        # --shot 渲染截图时不要覆盖用户存的主题
        self.setWindowTitle(f"RetroMusic Lab · 游戏音乐转换器 {APP_VER}")
        self.resize(1180, 800)
        self.setAcceptDrops(True)
        self._build()
        self.apply_theme(self._theme_key)
        self.log(f"RetroMusic Lab {APP_VER} · Qt 界面")
        self.log("把 SPC / NSF / 光盘镜像(.cue) 拖进窗口，或点「添加音源」")
        self.log(f"临时目录: {__import__('tmpdir').init(sweep_old=False)}")

    # ------------------------------------------------------------------ 构建
    def _build(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        outer.addLayout(self._build_top())

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(12)
        left.addWidget(self._build_sources())
        left.addWidget(self._build_formats())
        left.addWidget(self._build_timing())
        left.addWidget(self._build_options())
        left.addStretch(1)
        body.addLayout(left, 3)
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(self._build_log(), 1)
        right.addWidget(self._build_player())
        body.addLayout(right, 2)
        outer.addLayout(body, 1)
        outer.addLayout(self._build_bottom())
        self.setCentralWidget(root)

    def _build_top(self):
        row = QHBoxLayout()
        row.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel("RetroMusic Lab")
        title.setObjectName("h1")
        sub = QLabel("SPC · PCM · CD AUDIO — 8/16-BIT 音乐工作站")
        sub.setObjectName("muted")
        col.addWidget(title)
        col.addWidget(sub)
        row.addLayout(col)
        row.addStretch(1)
        row.addWidget(QLabel("主题"))
        self.cb_theme = QComboBox()
        for key, tok in self._reg.items():
            self.cb_theme.addItem(tok["label"], key)
        idx = self.cb_theme.findData(self._theme_key)
        self.cb_theme.setCurrentIndex(max(0, idx))
        self.cb_theme.setFixedWidth(180)
        self.cb_theme.currentIndexChanged.connect(
            lambda _i: self.apply_theme(self.cb_theme.currentData()))
        row.addWidget(self.cb_theme)
        return row

    def _build_sources(self):
        card, lay = W.card()
        head = QHBoxLayout()
        t = QLabel("音源")
        t.setObjectName("h2")
        head.addWidget(t)
        head.addStretch(1)
        self.lb_count = QLabel("共 0 个")
        self.lb_count.setObjectName("muted")
        head.addWidget(self.lb_count)
        lay.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.tbl = QTableWidget(0, 5)
        self.tbl.setHorizontalHeaderLabels(["名称", "格式", "曲目", "大小", "时长"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setShowGrid(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.tbl.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        # 表头默认居中，第一列会和左对齐的名称错开
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.tbl.setMinimumHeight(196)
        row.addWidget(self.tbl, 1)

        btns = QVBoxLayout()
        btns.setSpacing(6)
        self.b_add = QPushButton("添加音源…")
        self.b_dl = QPushButton("下载音源…")
        self.b_del = QPushButton("移除选中")
        self.b_clr = QPushButton("清空")
        for b in (self.b_add, self.b_dl, self.b_del, self.b_clr):
            b.setMinimumWidth(104)
            btns.addWidget(b)
        btns.addStretch(1)
        hint = QLabel("支持多选")
        hint.setObjectName("hint")
        hint.setAlignment(Qt.AlignCenter)
        btns.addWidget(hint)
        row.addLayout(btns)
        lay.addLayout(row)

        self.b_add.clicked.connect(self.pick_files)
        self.b_dl.clicked.connect(self._not_yet)
        self.b_del.clicked.connect(self.remove_selected)
        self.b_clr.clicked.connect(self.clear_sources)
        self.tbl.cellDoubleClicked.connect(self._on_row_play)
        return card

    def _on_row_play(self, row, _col):
        """双击一行 = 试听该音源（与 tkinter 版点首列 ▶ 等价）。"""
        if 0 <= row < len(self.sources):
            self.player_bar.play_path(self.sources[row]["path"])

    def _build_formats(self):
        card, lay = W.card()
        t = QLabel("输出格式")
        t.setObjectName("h2")
        lay.addWidget(t)
        self.ck_cd = QCheckBox("音频 CD 镜像（BIN+CUE）— 用 ImgBurn 烧 CD-R，普通 CD 机播放")
        self.ck_wav = QCheckBox("WAV（Pico-Plus）— 16-bit/44.1kHz/立体声，拷到 SD 卡菜单播放")
        self.ck_merge = QCheckBox("多轨合并成单文件 — 每个音源除分轨外，另出一个「源名 (合并).wav」")
        self.ck_cd.setChecked(True)
        self.ck_wav.setChecked(True)
        for c in (self.ck_cd, self.ck_wav, self.ck_merge):
            lay.addWidget(c)
        return card

    def _build_timing(self):
        card, lay = W.card()
        head = QHBoxLayout()
        t = QLabel("时间与淡出")
        t.setObjectName("h2")
        head.addWidget(t)
        head.addStretch(1)
        h = QLabel("播放长度与速度补偿仅对 SPC 生效")
        h.setObjectName("hint")
        head.addWidget(h)
        lay.addLayout(head)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        r1.addWidget(QLabel("SPC 播放长度"))
        self.cb_policy = QComboBox()
        self.cb_policy.addItem("单遍（按 ID666 标注时长，推荐）", "id666")
        self.cb_policy.addItem("循环两遍", "id666_x2")
        self.cb_policy.addItem("自然长度", "natural")
        r1.addWidget(self.cb_policy, 1)
        r1.addWidget(QLabel("速度补偿"))
        self.sp_speed = QSpinBox()
        self.sp_speed.setRange(50, 200)
        self.sp_speed.setValue(100)
        self.sp_speed.setSuffix(" %")
        self.sp_speed.setFixedWidth(88)
        r1.addWidget(self.sp_speed)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        r2.addWidget(QLabel("默认单曲"))
        self.sp_default = QSpinBox()
        self.sp_default.setRange(10, 3600)
        self.sp_default.setValue(150)
        self.sp_default.setSuffix(" 秒")
        self.sp_default.setFixedWidth(96)
        r2.addWidget(self.sp_default)
        h2 = QLabel("芯片音源读不到自带时长时按这个截取")
        h2.setObjectName("hint")
        r2.addWidget(h2)
        r2.addStretch(1)
        r2.addWidget(QLabel("单曲最长"))
        self.sp_max = QSpinBox()
        self.sp_max.setRange(0, 3600)
        self.sp_max.setValue(0)
        self.sp_max.setSuffix(" 秒")
        self.sp_max.setFixedWidth(96)
        self.sp_max.setSpecialValueText("0（不限）")
        r2.addWidget(self.sp_max)
        lay.addLayout(r2)
        return card

    def _build_options(self):
        card, lay = W.card()
        t = QLabel("选项")
        t.setObjectName("h2")
        lay.addWidget(t)

        r1 = QHBoxLayout()
        r1.setSpacing(8)
        r1.addWidget(QLabel("输出目录"))
        self.ed_out = QLineEdit(self._store.get("out_dir") or "")
        self.ed_out.setPlaceholderText("留空则用「源文件夹 + output」")
        r1.addWidget(self.ed_out, 1)
        b = QPushButton("浏览…")
        b.clicked.connect(self.pick_out_dir)
        r1.addWidget(b)
        lay.addLayout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(8)
        r2.addWidget(QLabel("专辑名"))
        self.ed_album = QLineEdit()
        self.ed_album.setPlaceholderText("CD-TEXT，留空用源名")
        r2.addWidget(self.ed_album, 2)
        r2.addWidget(QLabel("演奏者"))
        self.ed_perf = QLineEdit()
        r2.addWidget(self.ed_perf, 1)
        r2.addWidget(QLabel("轨间距"))
        self.sp_gap = QSpinBox()
        self.sp_gap.setRange(0, 10)
        self.sp_gap.setValue(2)
        self.sp_gap.setSuffix(" 秒")
        self.sp_gap.setFixedWidth(84)
        r2.addWidget(self.sp_gap)
        lay.addLayout(r2)

        r3 = QHBoxLayout()
        r3.setSpacing(16)
        self.ck_verify = QCheckBox("转换后回读校验")
        self.ck_cdwav = QCheckBox("导出 CD 模式 WAV 试听")
        self.ck_xa_music = QCheckBox("XA 只导音乐（滤掉音效/语音）")
        self.ck_xa_split = QCheckBox("XA 按静音切曲")
        self.ck_xa_music.setChecked(True)
        for c in (self.ck_verify, self.ck_cdwav, self.ck_xa_music, self.ck_xa_split):
            r3.addWidget(c)
        r3.addStretch(1)
        lay.addLayout(r3)
        return card

    def _build_log(self):
        card, lay = W.card()
        head = QHBoxLayout()
        t = QLabel("日志")
        t.setObjectName("h2")
        head.addWidget(t)
        head.addStretch(1)
        b = QPushButton("清空日志")
        b.setObjectName("ghost")
        b.clicked.connect(lambda: self.te.clear())
        head.addWidget(b)
        lay.addLayout(head)
        self.te = QTextEdit()
        self.te.setObjectName("log")
        self.te.setReadOnly(True)
        self.te.setFont(QFont("Cascadia Mono", 9))
        lay.addWidget(self.te, 1)
        return card

    def _build_player(self):
        card, lay = W.card()
        head = QHBoxLayout()
        t = QLabel("试听")
        t.setObjectName("h2")
        head.addWidget(t)
        head.addStretch(1)
        h = QLabel("双击音源列表里的条目即可播放")
        h.setObjectName("hint")
        head.addWidget(h)
        lay.addLayout(head)
        self.player_bar = PlayerBar(on_log=self.log)
        lay.addWidget(self.player_bar)
        return card

    def _build_bottom(self):
        row = QHBoxLayout()
        row.setSpacing(10)
        self.b_go = QPushButton("开始转换")
        self.b_go.setObjectName("primary")
        self.b_cancel = QPushButton("取消")
        self.b_open = QPushButton("打开输出目录")
        self.b_cloud = QPushButton("上传云盘")
        self.b_cancel.setEnabled(False)
        for b in (self.b_go, self.b_cancel, self.b_open, self.b_cloud):
            row.addWidget(b)
        row.addStretch(1)
        self.pb = QProgressBar()
        self.pb.setRange(0, 100)
        self.pb.setValue(0)
        self.pb.setTextVisible(False)
        self.pb.setFixedWidth(220)
        self.pb.setFixedHeight(10)
        row.addWidget(self.pb)
        self.lb_status = QLabel("就绪")
        self.lb_status.setObjectName("status")
        self.lb_status.setMinimumWidth(150)
        self.lb_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.lb_status)

        self.b_go.clicked.connect(self.start)
        self.b_cancel.clicked.connect(self.cancel)
        self.b_open.clicked.connect(self.open_out_dir)
        self.b_cloud.clicked.connect(self.open_cloud)
        return row

    # ------------------------------------------------------------------ 主题
    def apply_theme(self, key):
        if key not in self._reg:
            key = "modern_light"
        self._theme_key = key
        tok = self._reg[key]
        self._check_icon = W.check_icon(tok["check"])
        self._chev_icon = W.chevron_icon(tok["fg2"])
        self._radio_icon = W.radio_dot(tok["check"])
        self.setStyleSheet(th.qss(tok, self._check_icon, self._chev_icon,
                                  self._radio_icon))
        i = self.cb_theme.findData(key)
        if i >= 0 and self.cb_theme.currentIndex() != i:
            self.cb_theme.blockSignals(True)
            self.cb_theme.setCurrentIndex(i)
            self.cb_theme.blockSignals(False)
        self._store["theme"] = key
        if not self._shot_mode:
            _save_store(self._store)

    # ------------------------------------------------------------------ 音源
    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择音源", "",
            "音源 (*.spc *.nsf *.nsfe *.gbs *.vgm *.vgz *.hes *.kss *.sap *.gym *.ay "
            "*.mp3 *.cue *.bin *.iso);;所有文件 (*.*)")
        if paths:
            self.add_paths(paths)

    def pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出目录", self.ed_out.text())
        if d:
            self.ed_out.setText(d)
            self._store["out_dir"] = d
            _save_store(self._store)

    def add_paths(self, paths):
        added = 0
        for p in paths:
            p = os.path.normpath(str(p))
            if any(s["path"] == p for s in self.sources):
                continue
            kind = classify(p)
            if kind == "未知":
                ext = os.path.splitext(p)[1].lower()
                if ext in (".psf", ".minipsf", ".psf2", ".minipsf2", ".txtp"):
                    # PSF / txtp 在 tkinter 版里走的是 psf_bridge / psx_txtp
                    # 那套半自动桥接（要调外部解码器），Qt 版还没接。
                    self.log("⚠ %s 需要 PSF 桥接流程，Qt 版暂未接入 —— "
                             "请用 v4.44 的 MSU1Converter.exe 转"
                             % os.path.basename(p))
                else:
                    self.log(f"⚠ 不认识的音源类型，已跳过: {p}")
                continue
            label = os.path.basename(p.rstrip("\\/")) or p
            if os.path.isfile(p):
                label = os.path.splitext(label)[0] or label
            entry = {"label": label, "path": p, "kind": kind,
                     "n_tracks": 0, "total_secs": 0.0, "size": 0}
            if os.path.isfile(p):
                try:
                    entry["size"] = os.path.getsize(p)
                except OSError:
                    pass
            if kind == "光盘镜像":
                try:
                    meta = core.probe_disc_audio(p)
                    entry["n_tracks"] = meta.get("n_tracks") or 0
                    entry["total_secs"] = meta.get("total_secs") or 0.0
                    entry["size"] = meta.get("size") or entry["size"]
                except (RuntimeError, OSError):
                    pass
            self.sources.append(entry)
            added += 1
        if added:
            self.refresh_table()
            self.log(f"已添加 {added} 个音源")
        else:
            self.log("⚠ 没有新音源（可能重复）")

    def refresh_table(self):
        self.tbl.setRowCount(len(self.sources))
        for r, s in enumerate(self.sources):
            cells = [
                s["label"],
                s["kind"],
                str(s["n_tracks"]) if s["n_tracks"] else "—",
                fmt_size(s["size"]) if s["size"] else "—",
                fmt_dur(s["total_secs"]) if s["total_secs"] else "—",
            ]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if c:
                    it.setTextAlignment(Qt.AlignCenter)
                self.tbl.setItem(r, c, it)
        self.lb_count.setText(f"共 {len(self.sources)} 个")

    def remove_selected(self):
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self.sources):
                self.sources.pop(r)
        self.refresh_table()

    def clear_sources(self):
        self.sources.clear()
        self.refresh_table()

    # ------------------------------------------------------------------ 拖放
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_paths(paths)
            ev.acceptProposedAction()

    # ------------------------------------------------------------------ 转换
    def _opts(self):
        out = self.ed_out.text().strip()
        if not out and self.sources:
            first = self.sources[0]["path"]
            base = first if os.path.isdir(first) else os.path.dirname(first)
            out = os.path.join(base, os.path.basename(base.rstrip("\\/")) + "output")
        return {
            "out_dir": out,
            "do_cd": self.ck_cd.isChecked(),
            "do_wav": self.ck_wav.isChecked(),
            "policy": self.cb_policy.currentData(),
            "speed": self.sp_speed.value() / 100.0,
            "max_secs": float(self.sp_max.value()),
            "default_secs": float(self.sp_default.value()),
            "gap": float(self.sp_gap.value()),
            "album": self.ed_album.text().strip(),
            "performer": self.ed_perf.text().strip(),
            "verify": self.ck_verify.isChecked(),
            "cd_wav": self.ck_cdwav.isChecked(),
            "xa_music_only": self.ck_xa_music.isChecked(),
            "xa_split": self.ck_xa_split.isChecked(),
            "merge_wav": self.ck_merge.isChecked(),
        }

    def start(self):
        if self.worker is not None and self.worker.isRunning():
            return
        if not self.sources:
            self.log("⚠ 先添加音源")
            return
        opts = self._opts()
        if not opts["do_cd"] and not opts["do_wav"]:
            self.log("⚠ 至少选一种输出格式")
            return
        if not opts["out_dir"]:
            self.log("⚠ 请选择输出目录")
            return
        try:
            os.makedirs(opts["out_dir"], exist_ok=True)
        except OSError as e:
            self.log(f"❌ 无法创建输出目录: {e}")
            return
        self._store["out_dir"] = opts["out_dir"]
        _save_store(self._store)
        self.te.clear()
        self.log(f"开始: {len(self.sources)} 个音源")
        self.log(f"输出: {opts['out_dir']}")
        self.pb.setValue(0)
        self.lb_status.setText("转换中…")
        self.b_go.setEnabled(False)
        self.b_cancel.setEnabled(True)

        w = ConvertWorker(self.sources, opts, self)
        w.log.connect(self.log)
        w.progress.connect(self._on_progress)
        w.finished_ok.connect(self._on_done)
        w.failed.connect(self._on_failed)
        self.worker = w
        w.start()

    def cancel(self):
        if self.worker is not None:
            self.worker.cancel()
            self.lb_status.setText("正在取消…")

    def _on_progress(self, pct, text):
        self.pb.setValue(int(pct))
        if text:
            self.lb_status.setText(text)

    def _on_done(self, res):
        self.b_go.setEnabled(True)
        self.b_cancel.setEnabled(False)
        if res.get("cancelled"):
            self.lb_status.setText("已取消")
            self.log("⛔ 已取消")
        elif res.get("fail"):
            self.lb_status.setText(f"完成 · {res['ok']} 成功 / {res['fail']} 失败")
            self.log(f"❌ 完成但有失败: ok={res['ok']} fail={res['fail']}")
        else:
            self.pb.setValue(100)
            self.lb_status.setText(f"✅ 完成 · {res['ok']} 首 / "
                                   f"{fmt_dur(res['seconds'])}")
            self.log("=" * 54)
            self.log(f"🎉 全部完成! 输出目录: {self._opts()['out_dir']}")
        self.worker = None

    def _on_failed(self, msg):
        self.b_go.setEnabled(True)
        self.b_cancel.setEnabled(False)
        self.lb_status.setText("转换失败")
        self.log(f"❌ {msg}")
        self.worker = None

    def open_out_dir(self):
        d = self._opts()["out_dir"]
        if not d or not os.path.isdir(d):
            self.log(f"⚠ 目录还不存在: {d}")
            return
        os.startfile(d)                                # noqa: S606 Windows 专用

    def _not_yet(self):
        QMessageBox.information(
            self, "暂未接入",
            "Qt 版当前只做了主窗转换流程。\n"
            "下载音源 / 云盘上传 / 试听播放器请在 v4.44 里用。")

    def open_cloud(self):
        """上传到网易云云盘。窗口沿用主窗当前主题（QSS 是逐控件继承的）。"""
        from qt_ui.cloud_dialog import CloudUploadDialog
        dlg = CloudUploadDialog(self._opts()["out_dir"], self)
        dlg.setStyleSheet(self.styleSheet())
        dlg.exec()

    def log(self, text):
        self.te.append(str(text))
        sb = self.te.verticalScrollBar()
        sb.setValue(sb.maximum())

    def closeEvent(self, ev):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(4000)
        self.player_bar.shutdown()
        ev.accept()
