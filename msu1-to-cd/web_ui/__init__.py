#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web_ui — Web 版界面（Edge ``--app`` 外壳 + 本地 HTTP 后端）。

模块分工::

    server.py         HTTP + REST API + 任务状态机（后端全部逻辑）
    native_dialog.py  ctypes 直调 comdlg32 / shell32 的原生文件对话框
    shell.py          起服务 + 拉起 Edge ``--app`` 窗口 + 生命周期
    index.html        DOM 骨架
    app.css           主题令牌与全部样式（深/浅两套）
    app.js            轮询 /api/state、渲染、试听、拖放

为什么要有 ``__init__.py``：Nuitka 是**静态**解析 import 的。作为包导入
（``from web_ui import shell``）时，``shell.py`` 里的 ``from . import server``
才解析得到；写成裸 ``import server`` 在 standalone 包里会 ImportError。
"""
