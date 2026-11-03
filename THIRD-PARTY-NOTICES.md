# 第三方组件与许可声明

本项目本体采用 **MIT**（见 [LICENSE](LICENSE)）。但为了让 48 种扩展名真的能出声，
仓库与发布包里包含了若干**第三方解码器**、**运行时库**和**数据素材**，它们各自
遵循原始许可，**不受 MIT 覆盖**。

本文件按「随包分发的可执行文件 → 运行时依赖 → 数据与素材 → 合规说明」四段列出。
所有许可原文都随仓库提供，路径见表格最后一列。

> 更新规则：新增/升级任何第三方组件时，同步更新本文件与对应源码目录下的许可文件。
> 只写「开源」不写具体许可是不合格的。

---

## 一、随包分发的解码器可执行文件

这 10 个 `.exe` 与 `vgmstream-*.dll` 会被打进发布包，运行时由主程序**以子进程方式**
调用（参数 + 临时文件交换数据），不属于本项目的派生作品。

| 可执行文件 | 用途 | 上游 / 内核 | 许可 | 许可原文位置 |
|---|---|---|---|---|
| `psf2wav.exe` | PS1 / PS2 `.psf` `.minipsf` `.psf2` | psflib + Highly Experimental (PSX 核) | psflib **MIT**；HE 核上游未附许可文件，本项目按 **GPL-2.0** 兼容处理 | `msu1-to-cd/tools/psf2wav/src/psflib/LICENSE` |
| `gsf2wav.exe` | GBA `.gsf` `.minigsf` | mGBA 0.10.5 最小核 + psflib | mGBA **MPL-2.0**；psflib **MIT** | `tools/gsf2wav/src/mgba/LICENSE.txt`、`tools/gsf2wav/src/psflib/LICENSE` |
| `ssf2wav.exe` | Saturn `.ssf`、Dreamcast `.dsf`、`.dvi` | Sega 核 + psflib | **GPL-3.0**；psflib **MIT** | `tools/ssf2wav/src/sega/COPYING.txt`、`tools/ssf2wav/src/psflib/LICENSE` |
| `usf2wav.exe` | N64 `.usf` `.miniusf` | lazyusf2 + psflib | lazyusf2 **GPL-2.0+**；psflib **MIT** | `tools/usf2wav/src/psflib/LICENSE`、`tools/usf2wav/src/lazyusf2/` |
| `vio2wav.exe` | NDS `.2sf` `.mini2sf` | vio2sf（DeSmuME 抽取核）+ psflib | vio2sf **GPL-2.0+**；psflib **MIT** | `tools/vio2wav/src/vio2sf/desmume/COPYING` |
| `kss2wav.exe` | MSX / SMS / GG `.kss` `.sgc` | libkss + emu2149/2212/2413/76489/8950 | libkss **ISC**；各芯片模块另附 | `tools/kss2wav/src/libkss/LICENSE.md`、`.../modules/*/LICENSE` |
| `wsr2wav.exe` | WonderSwan `.wsr` | beetle-wswan-libretro | **GPL-2.0** | `tools/wswan2wav/src/beetle-wswan-libretro/COPYING` |
| `s982wav.exe` | PC-98 / PC-88 `.s98`（YM2608） | ymfm | **BSD-3-Clause** | `tools/s982wav/src/ymfm/LICENSE.txt` |
| `qsf2wav.exe` | Capcom QSound `.qsf` `.miniqsf` | HQ 核 + psflib | HQ 核 **GPL-3.0**；psflib **MIT** | `tools/qsf2wav/src/hq/LICENSE.TXT`、`.../psflib/LICENSE` |
| `vgmstream-cli.exe` + 11 个 `vgmstream-*.dll` | `.wem` `.fsb` `.adx` `.brstm` `.hca` `.at9` 等预录流 | vgmstream | **ISC** | `msu1-to-cd/vgmstream-COPYING.txt` |

注：
- `psf2wav.exe` **不含任何 BIOS**。`tools/psf2wav/src/he/bios.c` 只是运行时装载桩，
  BIOS 由用户指向自己机器上的文件。
- mGBA / MPL-2.0 组件：本项目未修改其源码；如修改，需按 MPL-2.0 公开修改部分。
- `libgme`（`msu1-to-cd/libgme.dll`，Game Music Emu）为 **LGPL-2.1+**，以独立 DLL 形式
  随包分发，未静态链接进主程序。

---

## 二、运行时依赖（打进主程序或随 exe 分发的库）

| 组件 | 用途 | 许可 |
|---|---|---|
| Python 3.14 | 运行时 | PSF-2.0 |
| PyInstaller | 单文件打包引导器 | GPL-2.0 **with bootloader exception** |
| PyAV | 光盘音轨 / 流式容器解封装 | BSD-3-Clause |
| FFmpeg（PyAV 内置） | 解码 AAC / ATRAC9 / XVAG 等 | LGPL-2.1-or-later |
| libsndfile | WAV / FLAC / MP3 / OGG 读写 | LGPL-2.1-or-later |
| soundfile | libsndfile 的 Python 绑定 | BSD-3-Clause |
| NumPy | PCM 运算 | BSD-3-Clause |
| segno | 二维码（云盘登录） | BSD-3-Clause |
| py7zr | 7z 解包兜底 | LGPL-2.1-or-later |
| pycryptodome | 网易云 weapi 加密 | BSD-2-Clause / Public Domain |
| zlib | psflib 解压 | zlib License |
| libgme | NSF/SPC/GBS/HES/KSS/VGM 等芯片解码 | LGPL-2.1-or-later |

以上 LGPL 组件均以**动态库形式**随包分发，可替换；如需重新链接，使用
`build_web_pyi.py` 与 `msu1-to-cd/*.spec` 即可复现。

---

## 三、数据与素材

| 内容 | 来源 | 说明 |
|---|---|---|
| 下载页索引 `msu1-to-cd/index/*.json.gz` | [joshw.info](https://joshw.info/) 各机种子站 | 仅保存**元数据**（标题、大小、直链、时间），不含音频本体 |
| 游戏封面 | [libretro-thumbnails](https://thumbnails.libretro.com/)、IGDB（经 `design.zaparoo.org`） | 封面版权归各游戏权利人，本项目仅用于本地识别展示，不随包分发 |
| 中文标题表 `zh_titles.py` | 社区译名 + 人工整理 | 仅标题文本 |

**本项目不包含、也不分发**：游戏 ROM、音源包本体、PS1/PS2 BIOS、任何商业游戏数据。
下载页只提供指向第三方站点的链接，下载行为由用户自行发起。

---

## 四、GPL / LGPL 合规说明

1. **聚合而非派生**：上述 GPL 解码器均为**独立可执行文件**，主程序通过
   `subprocess` 调用、以文件交换数据，不与主程序链接，属 GPL 术语中的
   "mere aggregation"。因此主程序可以继续以 MIT 发布。
2. **源码随附**：每个 GPL 解码器的完整源码、构建脚本（`build.py`）都在
   `msu1-to-cd/tools/<name>/` 下，与发布包中二进制**同版本**。
3. **许可全文**：GPL-2.0 / GPL-3.0 / LGPL / MPL / ISC / BSD / MIT 原文分别在
   上表所列路径，未做删改。
4. **获取方式**：仓库 <https://github.com/hualazimo7/MSU1Converter> 即为源码分发点；
   Release 页的每个二进制都能在该 tag 的 `tools/` 下找到对应源码。
5. **HE 核的特殊说明**：Highly Experimental 源码树内没有独立许可文件。
   本项目按最严格的 GPL-2.0 处理并随附全部源码；若上游另有条款，以 upstream 为准。

---

## 五、致谢

- [Game Music Emu](https://github.com/libgme/game-music-emu) — 芯片音乐解码的地基
- [vgmstream](https://github.com/vgmstream/vgmstream) — 预录流格式的万能钥匙
- [mGBA](https://mgba.io/)、[DeSmuME](https://desmume.org/)、lazyusf2、
  [libkss](https://github.com/digital-sound-antiques/libkss)、[ymfm](https://github.com/aaronsgiles/ymfm)、
  [beetle-wswan-libretro](https://github.com/libretro/beetle-wswan-libretro) — 各机种模拟核
- [psflib](https://github.com/kode54/psflib) — PSF 家族容器解析
- [joshw.info](https://joshw.info/)、[libretro-thumbnails](https://thumbnails.libretro.com/) — 音源索引与封面
- [RetroArch](https://www.retroarch.com/) 社区 — 大量格式细节的公开资料
