#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/test_qt_ui.py — Qt 界面门禁（开发期入口）。

真正的断言在 ``qt_ui/selftest.py`` —— 打包后的 exe 也要跑同一套
（``MSU1Converter-qt.exe --qt-selftest``），所以**只写一份**。

这个脚本额外做的两件事（源码树里才有意义）：

1. 把源码目录塞进 ``sys.path``，这样 ``import qt_ui`` 能找到；
2. 断言「spec 里该有的 hiddenimports 真的存在」—— 打包漏模块是
   最常见的「源码能跑、exe 一按就炸」。

跑法::

    python vendor/test_qt_ui.py
    python vendor/test_qt_ui.py --fast     # 跳过真实转换
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 本工程约定：临时文件一律走 tmpdir（D:\MSU1Temp），别在 C 盘留垃圾
import tmpdir                                                   # noqa: E402
tmpdir.init(sweep_old=False)


def check_spec():
    """spec 里必须声明 Qt 相关模块，否则打包出来的 exe 会缺件。"""
    from qt_ui import selftest as st
    spec = os.path.join(ROOT, "MSU1Converter-qt.spec")
    if not os.path.isfile(spec):
        return st._mark(False, "MSU1Converter-qt.spec 存在")
    st._mark(True, "MSU1Converter-qt.spec 存在")
    with open(spec, "r", encoding="utf-8") as f:
        text = f.read()
    for name in ("qt_ui.main_window", "qt_ui.theme", "qt_ui.widgets",
                 "qt_ui.worker", "qt_ui.player_bar", "qt_ui.cloud_dialog",
                 "qt_ui.selftest", "cloud_upload", "tmpdir", "wavmerge",
                 "player", "netease_cloud", "netease_cookie"):
        st._mark(name in text, "spec 含 hiddenimports: " + name)

    # 「不打包 tkinter」不能简单查全文 —— spec 的 excludes 里**本来就要写**
    # 'tkinter' 这个词。只查 excludes 块里有没有它。
    try:
        blk = text[text.index("excludes = ["):]
        blk = blk[:blk.index("]")]
    except ValueError:
        blk = ""
    st._mark("'tkinter'" in blk, "spec 的 excludes 里有 tkinter")
    st._mark("collect_all('tkinter')" not in text, "没有 collect_all tkinter")
    # numpy 反过来：soundfile / player 顶层依赖它，排掉会让 exe 一启动就崩
    st._mark("'numpy'" not in blk, "spec 没有排除 numpy")
    st._mark("'soundfile'" in text and "'numpy'" in text,
             "spec 收了 soundfile 与 numpy")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    from qt_ui import selftest as st

    # 先跑公共自检（它会重置计数器与输出缓冲），再补源码树独有的检查，
    # 最后一次性打印 —— 顺序反了会把前面的结果冲掉。
    st.run(with_e2e="--fast" not in argv, verbose=False)
    check_spec()

    for line in st._LINES:
        print(line)
    print("")
    print("=" * 62)
    print("结果: %d 通过 / %d 失败" % (st.PASS, st.FAIL))
    print("=" * 62)
    return 0 if st.FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
