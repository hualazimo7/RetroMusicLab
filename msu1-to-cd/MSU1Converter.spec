# -*- mode: python ; coding: utf-8 -*-
import glob as _glob                                    # noqa: E402
import os as _os                                        # noqa: E402

from PyInstaller.utils.hooks import collect_all, get_package_paths

datas = [('index', 'index')]
# exe 文件图标（资源管理器 / 任务栏固定后显示的那个）。由
# `python vendor/make_app_ico.py` 从 retro_theme.app_icon() 的同一份像素
# 逻辑导出，改窗口图标后必须重跑一次。文件缺失时退回 PyInstaller 默认
# 图标，不阻断打包——只影响观感，不影响功能。
_ICON = _os.path.join('assets', 'app.ico')
_ICON = _ICON if _os.path.exists(_ICON) else None
binaries = [('libgme.dll', '.'), ('libstdc++-6.dll', '.'), ('zlib1.dll', '.'), ('libgcc_s_seh-1.dll', '.'), ('libwinpthread-1.dll', '.')]
hiddenimports = [
    # 多格式芯片音源模块。
    # chip_formats 被 msu1_to_cd 模块级 import（可见），但 chip_decode 是
    # **惰性 import**（在 load_source / maybe_run_spc_worker 函数体内，
    # 用来避免 core <-> chip_decode 的循环依赖），PyInstaller 静态分析看不到，
    # 必须显式列出，否则打包后 --chip-decode 与 chip 解码路径会 ImportError（设计 H16）。
    'chip_formats',
    'chip_decode',
    # v4.0 下载后自动转 WAV 的转换基座。
    # dl_convert 在 msu1_gui 里是**模块级 import**（可见），但它内部对
    # chip_decode 用的是函数内惰性 import（convert_one 的 chip 分支，
    # 避免模块级就要求 libgme 可用），静态分析看不到 -> 显式列出更保险。
    # 漏了它的后果：exe 里 dl_convert 变成 None -> 下载窗口的转 WAV 开关
    # 直接禁用（其余功能不受影响，属降级不属崩溃）。
    'dl_convert',
]
tmp_ret = collect_all('miniaudio')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cffi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('numpy')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# v4.19 云盘上传的三个可选依赖：
#   soundfile   —— WAV→FLAC 编码，**必须 collect_all** 才能带上
#                  _soundfile_data 里的 libsndfile DLL（漏了会 ImportError）
#   Crypto      —— pycryptodome，只用到 AES-128-CBC
#   segno       —— 纯 Python，生成二维码矩阵（**不依赖 Pillow**）
# 三者任一缺失都只让「上传云盘」按钮提示缺包，不影响转换主流程。
for _mod in ('soundfile',):
    tmp_ret = collect_all(_mod)
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# ⚠⚠ pycryptodome **不能 collect_all**：那会把 Crypto.SelfTest 整套自测
# （两百多个模块）和全部算法（ARC2/Blowfish/CAST/DES/ECC/RSA/Salsa20…）
# 都打进 exe，实测白涨 13MB，而我们只用 AES-128-CBC 一种。
# 这里只收 AES 真正需要的两个 C 扩展（_raw_api / _raw_aes）。

_crypto_dir = get_package_paths('Crypto')[0]
for _pattern, _dest in (('Util/_raw_api*.pyd', 'Crypto/Util'),
                        ('Cipher/_raw_aes*.pyd', 'Crypto/Cipher')):
    for _pyd in _glob.glob(_os.path.join(_crypto_dir, _pattern)):
        binaries.append((_pyd, _dest))
hiddenimports += [
    'Crypto.Cipher.AES',
    'Crypto.Cipher._mode_cbc',      # AES.new(..., MODE_CBC) 走这里
    'Crypto.Util._raw_api',
    'Crypto.Util._cpu_features',
    'Crypto.Util.py3compat',
]
hiddenimports += ['netease_cloud', 'segno', 'segno.encoder', 'segno.consts']
# v4.24 多轨合并。msu1_gui / dl_convert 都是模块级 import（静态可见），
# 显式列出只是防「某天改成惰性 import 后静默漏包」—— 纯 stdlib 模块，代价 0。
hiddenimports += ['wavmerge']
# v4.40 PSF（PS1/PS2 芯片音源）→ WAV 的半自动桥接。msu1_gui 是模块级
# `try: import psf_bridge`（静态可见），显式列出同样只是防将来改成惰性
# import 后静默漏包。纯 stdlib，代价 0。
hiddenimports += ['psf_bridge']
# v4.41 PS1 的 .txtp 包原生解码（.txtp 索引 -> .xa / .DA）。msu1_gui 与
# dl_convert 都是模块级 `try: import psx_txtp`，显式列出防将来改惰性后漏包。
# 纯 stdlib（array/os/re/wave），代价 0。
hiddenimports += ['psx_txtp']
# v4.44 临时目录统一走 D 盘。`msu1_to_cd` / `chip_decode` 都是模块级
# `import tmpdir`，静态可见；显式列出防将来改成惰性 import 后漏包。
# 纯 stdlib（os/shutil/tempfile/time），代价 0。
hiddenimports += ['tmpdir']
# v5.0-qt: 主题配色数据搬到 retro_palette.py（纯数据、不 import tkinter），
# retro_theme.py 与 qt_ui/theme.py 都从它取。
hiddenimports += ['retro_palette']

# ---- v4.30: CD-DA 音轨解码（PyAV / FFmpeg）----------------------------
# PCE-CD / MD-CD 的包是「.hes + .ape」混装，CD 音轨要靠 PyAV 解。
# ⚠ PyAV 的 ffmpeg DLL 不在 `av/` 包里，而在**同级的 `av.libs/`** ——
#   `av/__init__.py` 开头会用 `os.add_dll_directory(<包目录>/../av.libs)` 注册。
#   `collect_all('av')` 只收包内的 .pyd，收不到 av.libs，必须自己按
#   `('dll', 'av.libs')` 的 dest 显式加进去，否则打包后 import av 会
#   "DLL load failed while importing _core"。
# ⚠ av.libs 里的 DLL 名字带 hash 后缀（delvewheel 风格），与根部那份
#   `libgcc_s_seh-1.dll` / `libstdc++-6.dll` **不冲突**，不要改名。
hiddenimports += ['cd_audio']
tmp_ret = collect_all('av')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
_av_dir = get_package_paths('av')[0]
_av_libs = _os.path.join(_os.path.dirname(_av_dir), 'av.libs')
if _os.path.isdir(_av_libs):
    for _dll in _glob.glob(_os.path.join(_av_libs, '*.dll')):
        binaries.append((_dll, 'av.libs'))


a = Analysis(
    ['msu1_gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='MSU1Converter',
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
