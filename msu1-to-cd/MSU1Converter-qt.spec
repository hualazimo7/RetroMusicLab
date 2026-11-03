# -*- mode: python ; coding: utf-8 -*-
"""MSU1Converter-qt.spec — Qt 版（v5.0-qt）打包配置。

与 ``MSU1Converter.spec``（tkinter 版 v4.44）的关系：**两个 exe 并存**，
互不影响。区别只有三处：

1. 入口是 ``msu1_qt.py``，依赖 PySide6；
2. **excludes 掉 tkinter 与只服务下载窗口的模块**（dl_convert / joshw_* /
   zh_* / psf_bridge / psx_txtp）—— Qt 版还没有下载窗口，带进去纯属浪费；
3. hiddenimports 换成 ``qt_ui.*`` 与 ``cloud_upload``。

保留的东西一条都不能少：libgme（芯片音源）、av（CD-DA）、miniaudio（试听）、
soundfile（WAV→FLAC）、Crypto（网易云 weapi 加密）。

⚠ PyInstaller 的 ``excludes`` 是**按模块名精确排除**，写错名字不会报错、
只会静默不生效。所以 ``vendor/test_qt_ui.py`` 里有一条断言专门查
「spec 里没有 tkinter」。
"""
import glob as _glob                                    # noqa: E402
import os as _os                                        # noqa: E402

from PyInstaller.utils.hooks import collect_all, get_package_paths

datas = [('index', 'index')]
_ICON = _os.path.join('assets', 'app.ico')
_ICON = _ICON if _os.path.exists(_ICON) else None

binaries = [('libgme.dll', '.'), ('libstdc++-6.dll', '.'), ('zlib1.dll', '.'),
            ('libgcc_s_seh-1.dll', '.'), ('libwinpthread-1.dll', '.')]

hiddenimports = [
    # ---- Qt 界面自己 ----
    'qt_ui.main_window',
    'qt_ui.theme',
    'qt_ui.widgets',
    'qt_ui.worker',
    'qt_ui.player_bar',
    'qt_ui.cloud_dialog',
    'qt_ui.selftest',
    # ---- 复用模块 ----
    # cloud_upload 被 qt_ui.cloud_dialog 模块级 import（静态可见），显式列出
    # 防将来改惰性后漏包；它同时被 msu1_gui 引用（tk 版那边有自己的一条）。
    'cloud_upload',
    # v4.44 临时目录统一走 D 盘。qt_ui.widgets 模块级 import tmpdir。
    'tmpdir',
# v5.0-qt: 主题配色数据搬到 retro_palette.py（纯数据、不 import tkinter），
# retro_theme.py 与 qt_ui/theme.py 都从它取。
    'retro_palette',
    # v4.24 多轨合并。qt_ui.worker 是**函数内惰性 import**（避免没勾合并也
    # 去加载），PyInstaller 静态分析看不到 -> 必须显式列出，否则打包后
    # 「多轨合并」勾了也没反应（会走 ImportError 分支提示不可用）。
    'wavmerge',
    # 芯片音源解码：chip_decode 在 msu1_to_cd 里是惰性 import（循环依赖），
    # 静态分析看不到，必须显式列出（与 tk 版同款理由）。
    'chip_formats',
    'chip_decode',
    # CD-DA 解码（PyAV）。cd_audio 在 msu1_to_cd 里也是惰性 import。
    'cd_audio',
    # 试听播放器（miniaudio）。qt_ui.player_bar 里是函数内 import。
    'player',
    # 云盘上传
    'netease_cloud',
    'netease_cookie',
]

# ---- 第三方：整包收集 --------------------------------------------------
# miniaudio 带 CFFI 扩展；soundfile 带 libsndfile DLL（漏了就 ImportError）；
# numpy 是 soundfile 的顶层依赖，必须 collect_all 才能带上它的 .libs。
for _mod in ('miniaudio', 'cffi', 'soundfile', 'numpy'):
    _r = collect_all(_mod)
    datas += _r[0]
    binaries += _r[1]
    hiddenimports += _r[2]

# ⚠ pycryptodome 不能 collect_all（会白涨 13MB 自测与无关算法），
# 只收 AES-128-CBC 真正需要的两个 C 扩展 —— 与 tk 版同一份取舍。
_crypto_dir = get_package_paths('Crypto')[0]
for _pattern, _dest in (('Util/_raw_api*.pyd', 'Crypto/Util'),
                        ('Cipher/_raw_aes*.pyd', 'Crypto/Cipher')):
    for _pyd in _glob.glob(_os.path.join(_crypto_dir, _pattern)):
        binaries.append((_pyd, _dest))
hiddenimports += [
    'Crypto.Cipher.AES',
    'Crypto.Cipher._mode_cbc',
    'Crypto.Util._raw_api',
    'Crypto.Util._cpu_features',
    'Crypto.Util.py3compat',
]

# ---- PyAV（CD-DA）：ffmpeg 的 DLL 在**同级 av.libs/**，collect_all 收不到 --
hiddenimports += ['av']
_r = collect_all('av')
datas += _r[0]
binaries += _r[1]
hiddenimports += _r[2]
_av_dir = get_package_paths('av')[0]
_av_libs = _os.path.join(_os.path.dirname(_av_dir), 'av.libs')
if _os.path.isdir(_av_libs):
    for _dll in _glob.glob(_os.path.join(_av_libs, '*.dll')):
        binaries.append((_dll, 'av.libs'))

# ---- 明确不打包 --------------------------------------------------------
# tkinter：Qt 版的意义就在这儿。PySide6 不需要它，排除掉省 ~10MB 与一堆 Tcl/Tk 数据。
# 下载窗口专用的四个模块：Qt 版没有下载窗，带了也用不上。
# ⚠ dl_convert 里**没有** tkinter，但它只服务下载窗；留着的唯一好处是
#   「以后移植下载窗时不用改 spec」，代价是几十 KB，先排除保持干净。
excludes = [
    'tkinter', '_tkinter', 'turtle', 'turtledemo', 'idlelib',
    'dl_convert', 'joshw_dl', 'joshw_source', 'zh_titles', 'zh_online',
    'psf_bridge', 'psx_txtp',
    # ⚠ numpy **不能排除**：`soundfile` 是顶层 `import numpy`（转 FLAC 要用），
    # `player` 的音量缩放也用它。排掉的表现不是「少个功能」，而是
    # 「exe 一启动就弹 traceback 对话框、永远不退」（PyInstaller 的
    # 窗口版会拦异常弹窗，命令行看起来就是卡死）。实测踩过一次。
    'matplotlib', 'scipy', 'pandas',
]

a = Analysis(
    ['msu1_qt.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='MSU1Converter-qt',
    icon=_ICON,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
