# -*- coding: utf-8 -*-
"""把 8 个机种的真实目录跑一遍 v5.2.6 的新提示，看用户实际会看到什么。

不是门禁（门禁用合成样本），这是一次性核对脚本。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dl_convert as dl
from web_ui import dl_api
from web_ui import server

ROOT = r"D:\MSU1Downloads\joshw"


def main():
    tot_files = tot_ok = 0
    for machine in sorted(os.listdir(ROOT)):
        mdir = os.path.join(ROOT, machine)
        if not os.path.isdir(mdir):
            continue
        files, ok = [], []
        for game in sorted(os.listdir(mdir)):
            gdir = os.path.join(mdir, game)
            if not os.path.isdir(gdir):
                continue
            files.extend(dl.walk_files(gdir))
        files = [f for f in files if os.path.splitext(f)[1].lower() not in
                 dl.NON_AUDIO_EXTS]
        ok = [f for f in files if dl.is_convertible(f)]
        tot_files += len(files)
        tot_ok += len(ok)
        print("\n=== %s ===" % machine)
        print("  音频文件 %d 个，能转 %d 个" % (len(files), len(ok)))
        if ok:
            print("  ✔ 能转的：" + "、".join(
                sorted({os.path.splitext(f)[1].lower() for f in ok})))
        hint = dl.explain_skipped(files)
        print("  下载页会显示：%s" % (hint or "（无）"))

        # 手动「添加文件夹」：挑第一个游戏目录，看它给什么
        games = [d for d in sorted(os.listdir(mdir))
                 if os.path.isdir(os.path.join(mdir, d))]
        if games:
            g = os.path.join(mdir, games[0])
            found, _cut = server.scan_dir_sources(g)
            if found:
                print("  添加文件夹「%s」→ 收进 %d 个音源"
                      % (games[0][:28], len(found)))
            else:
                print("  添加文件夹「%s」→ %s"
                      % (games[0][:28], server._no_source_reason(g)[:110]))
    print("\n" + "=" * 60)
    print("合计：音频文件 %d 个，能转 %d 个，转不了 %d 个"
          % (tot_files, tot_ok, tot_files - tot_ok))


if __name__ == "__main__":
    main()
