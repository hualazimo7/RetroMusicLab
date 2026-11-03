#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qt_ui.worker — QThread 薄壳。

业务流程在 `convert_job.ConvertJob`（框架无关），这里只做三件事：
把它的回调转发成 Qt 信号、把 cancel 转过去、把异常转成 ``failed``。

**不要再往这里加业务逻辑** —— Web 版（``web_ui/server.py``）用的是同一个
``ConvertJob``，两边必须共用一份口径。
"""

from PySide6.QtCore import QThread, Signal

from convert_job import (ConvertJob, DISC_EXTS, classify,          # noqa: F401
                         fmt_dur, fmt_size)


class ConvertWorker(QThread):
    log = Signal(str)
    progress = Signal(float, str)
    track_done = Signal(int, int)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, sources, opts, parent=None):
        super().__init__(parent)
        self.sources = list(sources)
        self.opts = dict(opts)
        self._job = None

    # -- 外部控制 ---------------------------------------------------------
    def cancel(self):
        if self._job is not None:
            self._job.cancel()

    def is_cancelled(self):
        return bool(self._job is not None and self._job.is_cancelled())

    # -- 线程体 -----------------------------------------------------------
    def run(self):
        self._job = ConvertJob(
            self.sources, self.opts,
            on_log=self.log.emit,
            on_progress=self.progress.emit,
            on_track=self.track_done.emit,
        )
        try:
            self.finished_ok.emit(self._job.run())
        except Exception as e:                        # noqa: BLE001 兜底报给 UI
            self.failed.emit(str(e))
