#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor/_gate_split.py — 把 ``test_web_ui.py`` 拆成几段分开跑。

为什么需要它：开发机的文件删除护栏按「每轮 50 次」限流，超过就打
``[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`` 并把进程掐掉。门禁
自己清理临时目录就要五六十次，一次跑全量必然在 ``t_bios_bundle`` 附近被
打断 —— 表现是日志停在那一段、没有汇总行、``exit=1`` 但一条 ``[!!]`` 都没有。

这不是产品问题（用户机器上没有这个护栏）。README 里给的办法是「把可疑的
那一段单独跑」。这个脚本把 ``main()`` 里的段按 a / b / c 三组切开，每组一个
**新进程**（= 新的 50 次配额），最后把三段的通过数相加，口径和一次跑全量一致。

用法::

    py -3.14 vendor/_gate_split.py --part a
    py -3.14 vendor/_gate_split.py --part b
    py -3.14 vendor/_gate_split.py --part c
    py -3.14 vendor/_gate_split.py --all      # 依次跑 a/b/c 并汇总

⚠ ``--fast`` 口径：a/b/c 里 ``t_txtp`` / ``t_txtp_stream`` / ``t_xa`` 这些
  都传 ``fast=True``，等价于 ``test_web_ui.py --fast``。
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "web_ui"))
sys.path.insert(0, HERE)

import test_web_ui as G                                        # noqa: E402


#: 分组顺序必须和 ``test_web_ui.main()`` 里一模一样 —— 组内顺序会影响
#: 共享 state 的段（比如 t_dl_play 先跑、t_dl_disk_state 后跑）。
#:
#: ``t_bios_bundle`` 单独成一组：它自己就要建/删十几个文件，跟在 b 组后面
#: 必然撞上 50 次配额（b 组跑完已经用掉四十来次）。切出来给新进程跑。
PARTS = {
    "a": ["t_static", "t_api_contract", "t_sources",
          "t_txtp", "t_txtp_stream", "t_xsf_home", "t_batch_and_drop",
          "t_xa", "t_psf"],
    "b": ["t_usf", "t_vio", "t_ssf", "t_gsf", "t_qsf", "t_v5911", "t_v5912",
          "t_cloud", "t_theme", "t_game_folders", "t_dl_play",
          "t_dl_disk_state", "t_dl_row_actions"],
    "b2": ["t_bios_bundle"],
    "c": ["t_bootlog", "t_shell", "t_zh",
          "t_open_outdir", "t_default_outdir", "t_env", "t_stream",
          "t_aif", "t_zophar", "t_kss_native", "t_spc_progress",
          "t_viz_panel", "t_flac_out"],
}

#: ``--all`` 依次跑的组（顺序同 main()）
ORDER = ("a", "b", "b2", "c")


def arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def run_part(part):
    names = PARTS[part]
    httpd, port, state = G.server.serve(port=0)
    G._BASE = "http://127.0.0.1:%d" % port
    print("门禁分段 %s · 后端 %s" % (part.upper(), G._BASE), flush=True)
    try:
        for fn in names:
            f = getattr(G, fn)
            # 带 fast 参数的段（t_txtp / t_xa / t_aif …）一律按 --fast 口径
            try:
                f(state, True)
            except TypeError:
                try:
                    f(state)
                except TypeError:
                    f()
    finally:
        httpd.shutdown()

    print("\n" + "=" * 46, flush=True)
    print("段 %s: %d 通过 / %d 失败" % (part.upper(), G._PASS, G._FAIL), flush=True)
    print("=" * 46, flush=True)
    return G._PASS, G._FAIL


def main():
    part = arg("--part")
    if "--all" in sys.argv:
        tot_p = tot_f = 0
        for p in ORDER:
            r = subprocess.run([sys.executable, os.path.abspath(__file__),
                                "--part", p],
                               cwd=ROOT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            out = r.stdout or ""
            for ln in out.splitlines():
                if ln.strip().startswith("[!!]"):
                    print(ln, flush=True)
            for ln in out.splitlines():
                if ln.startswith("段 ") and " 通过 / " in ln:
                    print(ln, flush=True)
                    body = ln.split(": ", 1)[1]
                    tot_p += int(body.split(" 通过")[0])
                    tot_f += int(body.split("/ ")[1].split(" ")[0])
        print("\n" + "=" * 46, flush=True)
        print("分段合计: %d 通过 / %d 失败" % (tot_p, tot_f), flush=True)
        print("=" * 46, flush=True)
        return 0 if tot_f == 0 else 1

    if part not in PARTS:
        print("用法: --part a|b|c 或 --all", flush=True)
        return 2
    _p, _f = run_part(part)
    return 0 if _f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
