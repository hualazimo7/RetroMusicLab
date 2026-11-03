"""把工程打成可换机继续开发的 zip。

用法（工程根或任意目录都行）::

    python msu1-to-cd/vendor/make_backup_zip.py [输出.zip]

排除的东西都是**可再生的**或**纯历史的**：

- ``msu1-to-cd/build/``        PyInstaller 中间产物（50MB，重打包会重建）
- 任意 ``__pycache__/``        字节码缓存
- ``dist/`` 里的历史 exe       每个 36~38MB，12 个就 400MB+；只留当前版本
- ``dist/dist.rar``            旧的整包压缩
- ``.running`` / ``*.part``    进程锁与半成品

⚠ 不要排除：``index/``（30 个 json.gz，打进 exe 的内置索引）、
``vendor/``（门禁脚本 + 样本）、``.workbuddy/memory``（记忆）、
``dist/zh_titles_user.json``（用户翻译表，删了会丢人工条目）。
"""

import os
import sys
import time
import zipfile

# 工程根：本文件在 <root>/msu1-to-cd/vendor/ 下
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

# 当前版本 —— 与 msu1_gui.APP_VER 保持一致
VERSION = "4.27"
DEFAULT_OUT = os.path.join(ROOT, "MSU1Converter-工程备份-20261019-v%s.zip" % VERSION)

SKIP_DIR_NAMES = {"build", "__pycache__", ".git", "_probe_out", ".idea"}
SKIP_FILE_NAMES = {".running"}
SKIP_FILE_SUFFIX = (".part", ".pyc")

# msu1-to-cd/dist/ 里只留这些（其余 exe / rar / 运行日志一律不带）
DIST_KEEP = {"MSU1Converter.exe", "zh_titles_user.json"}
# 进 zip 后重命名的：带版本号，一眼看出是哪一版
DIST_RENAME = {"MSU1Converter.exe": "MSU1Converter-v%s.exe" % VERSION}


def want(rel: str) -> str:
    """返回入包后的相对路径；返回空串表示不要。``rel`` 用 / 分隔。"""
    parts = rel.split("/")
    if any(p in SKIP_DIR_NAMES for p in parts[:-1]):
        return ""
    base = parts[-1]
    if base in SKIP_FILE_NAMES or base.endswith(SKIP_FILE_SUFFIX):
        return ""
    if parts[0] == "msu1-to-cd" and len(parts) > 2 and parts[1] == "dist":
        if base not in DIST_KEEP:
            return ""
        return "/".join(parts[:2] + [DIST_RENAME.get(base, base)])
    return rel


README = """\
# MSU1Converter 工程备份 —— v{v}

打包时间：{t}
用途：换一台电脑继续开发 / 直接运行。

## 想直接跑
`msu1-to-cd/dist/MSU1Converter-v{v}.exe` 双击即可（单文件，无需装 Python）。

## 想继续开发
1. 装 **系统 Python 3.14**（`%LOCALAPPDATA%\\Programs\\Python\\Python314`）。
   ⚠ 不要用托管的 3.13 —— 那个没编 tkinter，GUI 和打包都用不了。
2. `pip install pyinstaller py7zr soundfile numpy segno`（以及 README 里列的其它依赖）
3. 打包：`cd msu1-to-cd && python -m PyInstaller MSU1Converter.spec --noconfirm`
4. 自检：`dist\\MSU1Converter.exe --selftest / --cloud-selftest / --merge-selftest / --cdp-selftest`

**详细交接文档见 `docs/开发笔记-踩坑全集.md`**，里面有全部踩坑结论（⚠ 编号章节）
和门禁清单，务必先看它再动代码。

## 这个包里有什么
- `msu1-to-cd/`   全部源码 + index 内置索引 + vendor 门禁脚本 + 图标资源
- `MSU1Converter-v{v}.exe`  当前成品（≈37MB）
- `.workbuddy/memory/`      历次改动记录与长期结论
- `docs/` `deliverables/`  设计与需求文档、WSC 烧录器
- `docs/开发笔记-踩坑全集.md` 交接主文档

## 没带什么（都可以重新生成）
- `msu1-to-cd/build/`        PyInstaller 中间产物
- `dist/` 里的历史 exe       v4.14~v4.26，本机 `dist/` 还在
- `dist/dist.rar`            旧的整包压缩
- `__pycache__/`             字节码缓存

## 首次运行注意
云盘登录态存于 `%LOCALAPPDATA%\\MSU1Converter\\`（`netease.json` + `browser/`），
**不在这个包里**，换机后需要重新登录一次（云盘按钮旁「▾」→ 切换账号）。
"""


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fn in filenames:
            rel = (rel_dir + "/" + fn) if rel_dir else fn
            kept = want(rel)
            if not kept:
                continue
            full = os.path.join(dirpath, fn)
            if os.path.abspath(full) == os.path.abspath(out):
                continue                      # 别把输出自己装进去
            files.append((full, kept))

    files.sort(key=lambda x: x[1])
    total = sum(os.path.getsize(f) for f, _ in files) / 1e6
    print("待打包 %d 个文件，共 %.1f MB → %s" % (len(files), total, out))

    t0 = time.time()
    written = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("先看这里.txt", README.format(
            v=VERSION, t=time.strftime("%Y-%m-%d %H:%M")))
        for full, arc in files:
            try:
                zf.write(full, arc)
                written += 1
            except OSError as exc:            # 文件被占用（比如 exe 正在跑）
                print("  ⚠ 跳过 %s：%s" % (arc, exc))
            if written % 50 == 0:
                print("  … %d/%d" % (written, len(files)))

    size = os.path.getsize(out) / 1e6
    print("完成：%s" % out)
    print("  %d 个文件，zip %.1f MB（原始 %.1f MB，压缩率 %.0f%%），耗时 %.1fs"
          % (written, size, total, 100.0 * size / max(total, 1e-9), time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
