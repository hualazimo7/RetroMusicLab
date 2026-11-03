# 游戏音乐转换器（SPC → Pico-Plus WAV / 音频 CD）

把 SNES 的 `.spc`、MSU-1 的 `*.pcm`、光盘镜像（cue/bin）、MP3 转成 WAV / FLAC / MP3 / OGG / 音频 CD：

- **WAV**：16-bit / 44100Hz / 立体声（pico-infonesPlus 硬要求，无重采样器，采样率必须恰好 44.1k）
- **FLAC**：同一段音频的无损容器，体积约 WAV 的 47%（芯片音乐噪声多，实测 25%~69%），可嵌封面
- **MP3**：同一段音频的有损容器（**320 kbps CBR**，libsndfile 自带 LAME），体积约 WAV 的 23%，
  可嵌封面（ID3v2.3 的 `APIC` 帧，v5.9.17 起）
  > ⚠ v5.9.18 修了两个一直存在的坑：① **int16 直传 libsndfile 会变成满耳杂音**
  > （它只吃 float 样本），必须自己 `/32768.0` 归一化成 float32；
  > ② 不显式 `bitrate_mode="CONSTANT"` 就只有 102~170 kbps VBR，不是 320k CBR。
  > 见 `flac_out._write` 的注释与 `vendor/_feasibility_mp3.py`。
  >
  > 🛡 v5.9.19 起**每首 MP3 落盘后自动自检**（`flac_out.verify_lossy`）：解回来
  > 跟源比 SNR / 波形相关 / 峰值比，任意一项不达标就判失败、删掉半成品。
  > 上面那个 bug 的坏文件所有结构指标（时长 / 采样率 / 声道 / 体积 / 帧结构 /
  > 三个解码器解出来一致）都是对的，只有「内容」不对 —— 所以只能靠
  > 「解回来逐样本比」来兜底。
  > 阈值：SNR ≥ 8 dB、相关 ≥ 0.80、峰值比 ≤ 1.6。
  > 实测正常音乐 36~43 dB / 0.9999 / 1.02~1.12，满量程白噪 15.3 dB / 0.99 / 1.00，
  > 老 bug 是 -21.6 dB / 0.1 / 2.0 —— 两边余量都够（20 dB 会误杀白噪，踩过）。
- **OGG**：同一段音频的有损容器（**Vorbis q10**，约 395 kbps VBR，libsndfile 自带
  libvorbis），体积约 WAV 的 27%（实测 21%~29%）。**v6.0.0 新增**，不能嵌封面
  （图另存 `cover.png`）
  > ⚠ 档位**必须 q10**（`OGG_LEVEL = 0.0`）：低一档会把满量程方波（NES/GB 的真实
  > 音色）的 SNR 压到 3 dB 以下，被自检误判成坏文件。
  >
  > ⚠ libsndfile 1.2.2 的 Vorbis 写入器**一次性写 >17 秒会栈溢出杀进程**
  > （Windows 退出码 `0xC00000FD`，无异常无日志）。所以 OGG 走
  > `flac_out._sf_write()` **分块写**（`OGG_BLOCK_FRAMES` = 5 秒一块）。
  > 短样本验不出来，必须用 ≥30 秒真素材验 —— 门禁 `t_v600` 里是 40 秒子进程用例。
- **音频 CD**：BIN + CUE，可直接用 ImgBurn 烧 CD-R

## 快速使用

```
双击 启动转换器.bat          # 优先跑 dist\MSU1Converter.exe
```

界面里「➕ 添加音源…」可在一个窗口里混选文件和文件夹，也可把文件夹直接拖进列表。
一次添加的多个音源，输出全部平铺在**同一个**输出目录根部，不建子目录；拖入文件夹时
输出目录默认为「源文件夹名 + output」。


## 中文名（游戏名中文化）

下载窗口的列表多一列 **中文名**，搜索框里中文也能搜到。三层匹配，越靠后越"直译"：

1. **精确词典** —— 已确认通行的官方/通用译名（超级马里奥、魂斗罗、时空之轮、龙珠 Z…）
2. **词组替换** —— 系列名（final fantasy → 最终幻想、castlevania → 恶魔城）
3. **分词直译** —— 逐词翻译，命中率低于 55% 就放弃，不硬拼

拿不准时显示 `—`，而不是编一个假译名。内置词典能覆盖复古机种约 15~18%；
配合下面的「一键联网翻译」跑完全库后，**38,805 条里有 38,270 条（98.6%）有中文名**，
最低的机种也有 97.6%。日志里每次换机种会打印一行实际覆盖率。

### 一键联网翻译（谷歌翻译）

下载窗口工具行有 **🌐 翻译未收录** 按钮：

- 把当前机种**还没有中文名**的条目标题批量送去谷歌翻译（`translate.googleapis.com` 免密端点，
  12 条拼一次请求，逐条 0.35s 限速），单次上限 3000 条；
- 翻完**自动写进 `zh_titles_user.json`**，下次打开直接生效，不再联网；
- 翻译中按钮变成「■ 停止翻译」，可随时中断，**已翻好的部分照样保存**；
- 只翻词典里没有的，手工确认过的译名不会被覆盖；
- 单条上限是为了不把谷歌刷太狠；失败会写一行日志，保留已完成部分。

⚠️ 机器翻译对游戏名是**直译**，例如 `El Viento` 会翻成「风」，翻得不准的直接编辑
`zh_titles_user.json` 改掉即可（你的词典优先级最高）。

### 翻译服务与兜底

翻译按顺序尝试，谁通用谁：

| 顺序 | 服务 | 备注 |
|---|---|---|
| 1 | 谷歌 `translate.googleapis.com` | 主用，免密钥 |
| 2 | 谷歌 `translate.google.com` | 主域名抽风时的备用域名 |
| 3 | MyMemory 公共接口 | 最后一档，质量一般 |

单个服务失败会**原地重试 2 次（1.5s / 3s 退避）**再换下一个 —— 实测遇到的 `502 Tunnel
connection failed` 多半是代理抖动，退避一下就成了，不必降级到质量更差的源。

> 实测结论（2026-10-18）：有道的免密钥端点已 404、`cn.bing.com` 网页版 `ttranslatev3`
> 带 cookie + token 仍返回 401，所以**目前没有能免密钥用的国内翻译接口**。若你有百度 /
> 腾讯 / 译文的 API Key，告诉我可以接进来当第二优先源。

### 一次翻完整个库

```bash
python vendor/bulk_translate.py                    # 全部 30 个机种
python vendor/bulk_translate.py --only nsf,spc     # 只翻指定机种
python vendor/bulk_translate.py --limit 200        # 每个机种只翻 200 条(试跑)
```

- 结果**增量**写进 `zh_titles_user.json`，断了重跑会自动跳过已翻过的；
- 只翻内置词典里没有的，手工确认过的译名不会被覆盖；
- 已翻好的条目会随 exe 一起分发（`dist/zh_titles_user.json`）。

### 自己补词典

在 exe 同目录，或 `%LOCALAPPDATA%\MSU1Converter\` 下放一个 `zh_titles_user.json`：

```json
{
  "Metroid Prime 3 Corruption": "密特罗德 Prime 3 腐化",
  "Ōkami": "大神"
}
```

格式就是 `{"英文标题": "中文名"}`，键会自动忽略大小写、空格和标点。**下次启动自动生效，
且优先级高于内置词典**。不想用了直接删掉这个文件即可。

## 界面风格（16-bit 复古 / 合成波）

主窗口和「音源下载」窗口共用一套 `retro_theme.py`：深紫夜空底、霓虹青/品红描边、
Consolas 等宽数字，配一条会缓慢滚动的扫描线横幅。纯 ttk 样式，**零第三方依赖、
不引入任何图片文件**，打包体积不变。

- 视觉全部由 `retro_theme.py` 提供：调色板 `PALETTE`、字体 `FONT / FONT_S / FONT_B
  / FONT_TITLE / FONT_MONO`、横幅 `RetroBanner`、窗口图标 `app_icon`。
- 主窗口进来先 `theme.install(root)`（进程级装一次 clam 衍生主题），子窗口
  （添加音源 / 音源下载）自动继承，**不要**在子窗口里重复 install。
- 想换配色只改 `PALETTE` 一处即可，`msu1_gui.py` 里没有任何硬编码色值 —— 只有
  日志框是固定深色（`LOG_BG/LOG_FG`），因为 `tk.Text` 不跟随 ttk 主题。
- 窗口尺寸是**按屏幕算**的，别写死：
  - 主窗宽 820，高度 = `min(980, 屏幕高 - 87)`（87 = 标题栏 31 + 边框 8 + 任务栏 48），
    并显式定位成「水平居中 + 贴顶」。**不能只改高度后交给窗口管理器**：Windows 按整屏
    居中、不按工作区，1080p 上交给 WM 会被摆到 y=57、底边压到任务栏（上沿 1032），
    日志最下面一截被盖住。实测 980 高 + `+y=0`：外框 836x1019，底边 995 < 客户区底 1011。
  - 下载窗固定 `980x700`（横幅 60px，最小 `860x620`）。
  - 标题栏：`theme.dark_title_bar(win)` 把 Windows 原生标题栏切成深色（DWM），三处
    窗口都在 `after(50)` 里调一次（`apply_dark_title_bar()` 包了 `hasattr` + 异常保护，
    主题没有这个函数时静默跳过）。返回 `True` 表示设置成功。
  - 日志区是唯一可伸缩的控件，窗口变矮只裁它；`ScrolledText` 的 `height` 只给 4 行
    （请求值），富余高度由 `expand=True` 吃 —— 请求 9 行会让总请求高度超过窗口，
    pack 会把日志底部连边框一起裁掉。
- 高度是按**实测**内容自然高度定的，不是 `810+92` 的算术：当前内容自然高度 948px
  （`Card.TLabelframe` 内边距 `10 5` ×6 块、按钮高 33px、横幅 80px）。照 902 开窗实测
  会把日志挤成 **1px**。**改主题内边距/横幅高度后必须重新量一遍**（且必须新起进程量：
  `style.configure` 改 padding 对已创建的控件不生效，实测 708→708 不变）。
- 下载窗右侧列表四列必须**合计不超过树宽**，否则靠后的列被直接切掉（曾出现
  `430+100+92+112=734` > 可用宽 660，日期只剩 "2021"、大小/状态完全不可见）。
  现为 300/105/80/90，只有名称列 `stretch=True`，且 `_autosize()` 的上限取
  「树宽 − 其余三列 − 6」，不会再撑爆。
- 关窗时务必先 `banner.stop()` 再 `destroy()`（`App._on_close` 与
  `DownloadDialog._shutdown` 都已处理），否则 `after` 自循环会在销毁后
  抛 `invalid command name`。

## 按机种下载音源（joshw.info）

主窗口 ① 音源 里的「⬇ 下载音源…」会打开下载窗口，可直接从 JoshW 镜像按机种抓
`.7z` 音源包，省得自己去翻目录。

- **数据源按机种分子站**，不是单站目录：`spc.joshw.info`(SNES/SFC)、`nsf`(NES/FC)、
  `gbs`(GB)、`gsf`(GBA)、`smd`(MD/Sega CD)、`hes`(PCE)、`usf`(N64)、`ssf`(Saturn)、
  `dsf`(DC)、`2sf`(NDS)、`psf`~`psf4`、`xbox`/`x360`、`3do`、`wsr`、`kss`、`s98`、
  `hoot`、`fmtowns`、`pc` 等共 30 个机种；PS5 用主站 `joshw.info`。
- 每个机种根下按 `0-9 / a-z / zzz_prototypes / zzz_unlicensed` 分字母目录，单页就有
  几百条（`spc/s/` = 410 条），所以**选中机种才开始扫**，首次联网约 30–40 秒。
- **内置索引快照**：`index/<机种>.json.gz` 里预先打包了 30 个机种的目录索引
  （快照日期以文件名旁的打包日期为准，界面状态行会显示成
  `spc · 1,918 条 · 1004.8 MB（内置索引 2026-10-18）`）。
  第一次选机种时**直接同步填表，不联网、不等待**；只有在本地缓存新鲜时才会显示
  `（缓存）`，两者都没有才退回联网扫描。
- 快照会过期（站点每天都有新 dump）：如果发现列表里缺新条目、或下载时报
  `404`（日志会提示「该条目可能已从站点移除」），点窗口里的
  **「🗑 清除缓存」** 即可清掉本地缓存并立刻联网重扫，重扫完状态行变回`（缓存）`。
  内置快照不会被删除，它是最后一道兜底。
- 缓存：`%LOCALAPPDATA%\MSU1Converter\joshw_cache\<机种>.json`，有效期 7 天；
  窗口里的「🗑 清除缓存」可强制重扫。
- 下载：支持 Range 断点续传，未完成的文件保留 `.part`，中断后再点会接着下；
  目标文件已存在且大小吻合的条目显示「✓ 已下载」并自动跳过。
  （Apache 目录里的大小是四舍五入值，判定带 3% / 2KB 容差。）
  站点没给大小的条目，大小列显示 `—`。
- 默认落盘到 `%USERPROFILE%\Music\JoshW\<机种>`，例如 `Music\JoshW\SNES-SFC\`。
- 站点 `robots.txt` 标了 `Disallow: /`，工具只在**你手动选机种**时抓取该子站，
  请求间隔 ≥0.2 秒，并按 7 天缓存 —— 请勿高频刷新或清缓存重扫。

## 封面（libretro-thumbnails）

下载页勾上「顺带下载封面」（默认开）后，每下载一个音源包就会顺手抓一张该游戏的
盒绘；转 FLAC 时自动写进文件的 `METADATA_BLOCK_PICTURE`，播放器/文件管理器直接
显示封面。

### 为什么不是 The Cover Project

最初想用的是 `thecoverproject.net`，**它用不了**，两道硬墙：

1. 整站挂在 **Cloudflare 人机验证**后面 —— 不是限速，是 `HTTP 403` +
   `Cf-Mitigated: challenge`，连 `index.php` 都只回那个 5 KB 的 "Just a
   moment..." 挑战页。程序无法通过，也不应该去绕。
2. `robots.txt` 另外写着 `Disallow: *.jpg$` / `/images/` / `/uploads/` /
   `/download_cover.php` —— 就算没有挑战，封面文件本身也是明确不许抓的。

改用 **libretro-thumbnails**（RetroArch 官方缩略图库）：Apache 目录索引可列、
直连可用、不需要 API key，覆盖的机种和本工具几乎一一对应。

### 匹配规则（宁可漏，不可错）

joshw 的条目名是 No-Intro 风格，元信息全在括号里；libretro 是
`<游戏名> (<区域>).png`。两边**砍到第一个 `(`** 就对齐了：

| joshw 条目 | libretro 文件 | 归一化后 |
|---|---|---|
| `3 Ninjas Kick Back (1994-11)(Malibu)(Sony Imagesoft)[SNES]` | `3 Ninjas Kick Back (USA).png` | `3 ninjas kick back` |
| `Sonic & Knuckles (1994-06-23)(Sega)[SMD]` | `Sonic & Knuckles (World).png` | `sonic and knuckles` |

判据顺序：完全相等 → **副标题前缀**（分隔符必须是 ` - `）→ `difflib` 相似度
≥ **0.86**。三条都不过就**放弃**。

阈值和分隔符这两条是硬要求，不是保守：`Contra` 会匹到 `Contra Hard Corps`、
`Ys` 会匹到 `Ys III` —— 那是**不同的游戏**，贴错封面比没有封面更糟。

同分时按区域偏好：`World` > `USA` > `Europe` > `Japan` > 其它。
⚠ 多区合版（`(Japan, USA)`、`(USA, Europe)`）要**按逗号拆开**再认，整串比对
认不出来、排名掉到「未知」，于是输给纯欧版（实测就这样拿到过
`Super Metroid (Europe) (En,Fr,De)`）。

### 一个 joshw 机种可能对应多个 libretro 目录

joshw 的一个子站常常混装两代机器，libretro 却拆成不同目录。不合并的话这些条目
**成片地没封面**：

| 机种 | 混装了什么 | 合并的目录 | 命中率 |
|---|---|---|---|
| `smd` | Mega Drive + Sega CD | `Sega - Mega Drive - Genesis` + `Sega - Mega-CD - Sega CD` | 65% → **82%** |
| `hes` | HuCard + PC Engine CD | `NEC - PC Engine - TurboGrafx 16` + `… CD …` + `… SuperGrafx` | 28% → **82%** |
| `gbs` | GB + GBC | `Nintendo - Game Boy` + `Nintendo - Game Boy Color` | 45% → **85%** |
| `nsf` | FC + Famicom Disk System | `Nintendo - Nintendo Entertainment System` + `… Disk System` | — |
| `2sf` | DS + DSi | `Nintendo - Nintendo DS` + `Nintendo - Nintendo DSi` | — |

实测命中率（`vendor/_audit_cover_hit.py`，每机种随机抽 40 条 joshw 条目，
固定随机种子）：

| 机种 | 封面库 | 命中 | 机种 | 封面库 | 命中 |
|---|---|---|---|---|---|
| `gsf` GBA | 5961 | **98%** | `wsr` WS | 538 | **85%** |
| `ssf` Saturn | 2294 | **98%** | `smd` MD+SCD | 2961 | **82%** |
| `spc` SNES | 3689 | **92%** | `hes` PCE+CD | 1430 | **82%** |
| `nsf` NES | 13835 | **92%** | `dsf` DC | 1332 | **82%** |
| `3do` | 653 | **92%** | `2sf` NDS | 7348 | **78%** |
| `usf` N64 | 1115 | **90%** | `s98` PC-98 | 1370 | **28%** |
| `psf` PS1 | 9301 | **88%** | **合计** | | **84%** |
| `gbs` GB+GBC | 3171 | **85%** | | | |

没命中的绝大多数是**库里真没有**（日版独占、原型、欧版独占的小品），少数是
命名差异（`Pokemon Gold` vs libretro 的 `Pokemon - Gold Version`）。`s98` 只有
28% 是意料之中 —— PC-98 那一代大量是同人作品，libretro 只收商业大作；而且
PC-98 游戏名重复度高（一堆 `Rance` / `Ys` 变体），放宽相似度会大面积贴错。

### 缓存与关闭

```
%LOCALAPPDATA%\MSU1Converter\covers\
    _idx\<机种>.json     该机种的全部封面文件名（30 天有效）
    _lookup.json         归一化游戏名 → 封面相对路径（转换时反查）
    <机种>\<文件名>.png  封面本体
```

机种列表一次约 1 MB（SNES 3689 条 / 975 KB），所以按机种缓存 30 天，不是每次
下载都去抓。封面本体按需下载，一次一张，全局请求间隔 0.3 秒。

抓不到就**跳过**：封面是锦上添花，`fetch()` 的所有对外函数都不抛异常，
嵌封面失败也只是在转换日志里写一句 `⚠ 封面没嵌上：…`，**不影响音源和转码**。
不想要封面就把下载页那个复选框去掉。

### FLAC 图片块（纯标准库实现）

`flac_out.embed_picture()` 直接改 FLAC 的元数据块，不引 mutagen（GPL + 体积）：

* 块体格式：`4B 类型(3=正面封面)` + `4B MIME 长度` + MIME + `4B 描述长度` +
  描述 + `4B 宽/高/色深/调色板数` + `4B 图片长度` + 图片；
* 插在 STREAMINFO **之后**（STREAMINFO 必须仍是第一个块）；
* 已有的图片块**替换**不叠加 —— 否则「转一遍 WAV 再转一遍 FLAC」会让文件
  越滚越大；
* 写完**重算 last-block 标志**（`1B last<<7|type`），漏了这一步播放器会认为
  后面还有块，直接报文件损坏；
* 先写 `.picpart` 再 `os.replace`，中途失败原文件保持不动。

音频字节**一个都不变**（封面是元数据）。实测：嵌入前后音频段 md5 相同、
PyAV 读回来是 2 条流（`audio` + `video`）。

## 目录结构

| 路径 | 说明 |
|---|---|
| `msu1_gui.py` | Tk 界面 + 后台转换线程 + 「按机种下载音源」窗口（入口，`--selftest` / `--convert` 命令行模式） |
| `retro_theme.py` | 16-bit 复古/合成波主题：ttk 样式、调色板、字体、扫描线横幅、窗口图标（纯标准库） |
| `joshw_source.py` | 30 个机种子站索引抓取/解析、条目缓存（7 天）、内置快照读取（`best_index`） |
| `index/*.json.gz` | 内置索引快照（30 个机种，打包进 exe；`python vendor/build_index.py` 生成） |
| `vendor/build_index.py` | 预抓 30 个机种的目录索引，产出 `index/*.json.gz` |
| `joshw_dl.py` | 多线程断点续传下载器（`.part` + Range） |
| `cover_art.py` | 封面：libretro-thumbnails 目录索引、名字归一化/匹配、按机种缓存（纯标准库） |
| `flac_out.py` | FLAC / MP3 / OGG 编码（soundfile，libsndfile 自带 LAME 与 libvorbis）+ 封面嵌入（FLAC 走 `METADATA_BLOCK_PICTURE`，MP3 走 ID3v2.3 `APIC`，都是纯标准库；OGG 不嵌）；`WRITE_FMTS` 是格式白名单的唯一出处；`_sf_write()` 给 OGG 分块写（见 `OGG_BLOCK_FRAMES`） |
| `msu1_to_cd.py` | 解码核心：libgme 子进程解码、BIN+CUE 写出、校验；也是 SPC 解码子进程入口 |
| `spc_wav.py` | WAV 输出策略（ID666 时长、截断+淡出、跨批序号/去重） |
| `MSU1Converter.spec` | PyInstaller 打包配置（onefile、无控制台） |
| `启动转换器.bat` | 启动脚本 |
| `dist/MSU1Converter.exe` | 已打包成品（34.5MB，含 30 个机种的内置索引，免 Python 运行） |
| `test/*.pcm` | 3 个 MSU-1 样本，`--selftest` 的 MSU-1 分支用 |
| `vendor/sample.spc` | 单个 SPC 样本，新机器上没有 SPC 素材时可用它验证 |
| `vendor/test_*.py` | 回归测试（拖放 / 选择框 / 会话锁 / 进度） |
| `vendor/audit_wav.py` | WAV 体检：相邻样本重复率、规律静音、峰值 |
| `*.dll` | libgme 及其依赖，源码方式运行必需，打包时也会被收进 exe |

## 开发 / 重新打包

```bash
python msu1_gui.py                                  # 源码方式运行
python msu1_gui.py --selftest <某个.spc>            # 结构自检（输出无声、成对、44100/2ch/16bit）
python vendor/build_index.py                        # 刷新内置索引快照 index/*.json.gz
python vendor/build_index.py --only spc,nsf         # 只刷指定机种
python vendor/build_index.py --force                # 忽略本地缓存强制重扫
python -m PyInstaller MSU1Converter.spec --noconfirm # 产出 dist\MSU1Converter.exe
```

`index/` 会被 `MSU1Converter.spec` 的 `datas` 一起打进 exe，所以**打包前先跑
`build_index.py`**。首次抓全量约 8–10 分钟（串行 + 0.2 秒限速，尊重站点的
`Disallow: /`），已有本地缓存（2 天内）的机种会直接复用，几秒就能跑完。

## 多格式芯片音源支持（NSF / NSFE / GBS / VGM / VGZ / HES）

除了原有的 SPC，现在可以一次拖入混合目录，直接把十种芯片音乐转成同样规格的 WAV。

### 支持矩阵

| 格式 | 机种 | 识别魔数 | 轨数来源 | 时长来源 | 备注 |
|---|---|---|---|---|---|
| **SPC** | SNES / SFC | `SNES-SPC700 Sound File Data` | 恒 1 | ID666 `0xA9` | 走既有稳定路径，零回归 |
| **NSF** | NES / FC | `NESM\x1A` | `u8 @0x06` | 默认单遍 + 静音检测 | 曲名 `0x0E:0x2E` |
| **NSFE** | NES / FC | `NSFE` | `u32` @`plst` 前 | 默认单遍 | **唯一自带逐轨曲名**的格式 |
| **GBS** | Game Boy | `GBS\x01` | `u8 @0x04` | `loop_end @0x16 ÷ 59.7275` | 永不 ended，只能靠帧数截 |
| **VGM** | Mega Drive / Sega CD | `Vgm ` | 恒 1 | `u32le @0x18 ÷ 44100` | 最可靠，头里直接给总采样数 |
| **VGZ** | 同 VGM | `\x1f\x8b`（gzip） | 恒 1 | 同 VGM（先解压头） | joshw 的 `.vgm` 实为此格式 |
| **HES** | PC Engine / TurboGrafx | `HESM` | 恒 256 | 默认单遍 + 静音检测 | **含大量空轨**，自动跳过 |
| **KSS** | MSX / Master System / Game Gear | `KSCC` / `KSSX` | 恒 256 | 默认单遍 + 静音检测 | libgme 写死 256 轨；无文件内时长 |
| **GYM** | Sega Genesis / Mega Drive | `GYMX` | 恒 1 | **帧数 ÷ 60**（可信） | 仅对有 `GYMX` 头者填时长；无头者回落默认 |
| **SAP** | Atari 8-bit | `SAP\x0D\x0A`（**5 字节**） | `SONGS` 标签 | 默认单遍 + 静音检测 | `TIME` 是自由文本，不可用 |
| **AY** | ZX Spectrum / Amstrad CPC | `ZXAYEMUL` | `max_track`+1 | **逐轨 `SongLength` ×20ms**（可信） | 唯一逐轨自带时长的 P1 格式 |
| **S98** | PC-98 / PC-88 | `S98` + 版本**字符** `'0'`..`'3'` | 恒 1 | **扫数据流到 `0xFD`，tick × tickMult/tickDiv**（可信） | v5.9.13 新增，走 `s982wav.exe`（ymfm 核） |

**P1 四格式（KSS/GYM/SAP/AY）已启用**，素材与解码均已实测
（KSS 1 个、GYM 2 个、SAP 7 个、AY 7 个真实样本）。
⚠ 三处**公开规范与实际不符**、已按 libgme 源码 + 真实样本 hexdump 校正：
AY 魔数是 `ZXAYEMUL`（不是常说的 `AYM`/`EMAY`）、SAP 魔数是 **5 字节**
`SAP\x0D\x0A`（不是 4 字节）、KSS 只认 `KSCC`/`KSSX`（站上大量存在的 `.sgc`
会被 libgme 判Wrong file type，**不支持**）。
可复跑：`python vendor/probe_p1_offsets.py`（偏移自洽性）、
`python vendor/verify_p1_e2e.py`（端到端）。

**不支持**（拖入会给出可执行提示，不进列表、不崩溃）：`USF / SSF / DSF / 2SF /
AIF / WSR / GSF`（真实硬件音频日志，需对应机种的模拟器，本工具未内置）。
SAP 的 `TYPE D`（Digimusic）会被明确识别并提示"解码库不支持"。

### S98（PC-98 / PC-88）为什么不能交给 libgme

`.s98` 和 `.vgm` 同族，都是「芯片寄存器写入日志」，看起来转成 VGM 就能复用
libgme。**这条路是死的，而且死得很难发现**：

`gme/Vgm_Emu_Impl.cpp` 的 `run_commands()` 只实现 5 条芯片指令
（`0x4F` gg_stereo / `0x50` psg / `0x51` ym2413 / `0x52` `0x53` ym2612）。
YM2608 的 VGM 指令是 `0x56`/`0x57`，落到 `default:` 分支
`pos += command_len(cmd) - 1` **静默跳过** —— 输出是**精确静音**，
不报错、时长还对、文件还合法。判「支不支持某个芯片」要看它有没有那条
指令的分支，不能看它能不能打开文件。

所以 v5.9.13 自己编了一个核：`tools/s982wav/`（源在 `src/ymfm/`，
Aaron Giles 的 ymfm，**BSD 3-Clause**），产物 `s982wav.exe` 488 KB，
由 `build_web_pyi.py` 的 `EXTRA_EXE` 带进包。

三个容易踩的点，改这个解码器前先看：

1. **fidelity 必须 MED**。MIN 档的 SSG 内部率会**下采样**，124.8 kHz 以上的
   谐波折回可听带内，听感是「沙」。
2. **SSG 是单极性 0..32764**，必须一阶隔直（真机靠 AC 耦合），否则带几千的
   直流偏置、吃掉一半动态范围。
3. **`0xA0-0xAF` 是锁存对**：`0xA4` 只锁存（block + fnum 高 3 位），
   `0xA0` 才应用（fnum 低字节），且**必须先写 0xA4**。写反的症状是音高
   低一个八度，波形完全正常。

### 输出规格（与 SPC 完全一致）

**16-bit / 44100Hz / 立体声 / 未压缩 PCM**。单声道源（GB、PCE 等）由解码库自己
补成 L=R，我们**原样保留，不做二次立体声化，也不做峰值归一化** —— 各格式峰值差异
很大（VGM 约 7800、SPC 可达 22000），归一化会改变听感。这是正常的。

### 时长策略（按优先级取第一个可用的）

| 优先级 | 来源 | 适用 | 静音检测 |
|---|---|---|---|
| 1 | 用户填的「单曲最长」 | 全部 | **开**（谁先到听谁的） |
| 2 | 文件自带时长（NSFE / VGM / SPC ID666） | VGM/VGZ/NSFE/SPC | 关 |
| 3 | GBS `loop_end ÷ 59.7275` | GBS | 关 |
| 4 | 「默认单遍」N 秒（默认 150，界面可改 30~600） | NSF / HES | **开** |
| 5 | 硬顶 600 秒 | 全部 | — |

已知确切时长时**必须关掉静音检测**、且开头静音上限传 0 —— 否则 VGM 前 10 秒静音
会被误判成空曲（这正是新增 `lead_sil_limit` 参数的原因）。

### 多轨与空轨

- 每个文件的每一轨产出一个 WAV，序号 `01_` 起跨文件连续，不跳号。
- 曲名优先级：**文件内嵌标题 → 文件名 + 轨号**。
- **空轨自动跳过**：HES 每个文件 256 轨里只有一部分真出声（实测 73~155 个空轨，
  首帧峰值 0）。解码前先测首帧峰值，低于阈值就跳过，日志记 `⏭`，不产出空 WAV。

### 临时文件与批次

解码仍在**子进程**里跑（崩溃/卡死不影响界面，取消 ≤2 秒生效）。批次大小按
**音频总秒数**切分而不是文件数 —— 256 轨 HES 按文件数切会让临时盘爆到 GB 级。

- 单个 run 上限 1800 秒音频 → 峰值临时 PCM ≈ 303 MB（实测 22 个 run）
- PCM 全程落磁盘，`write_tracks` 一次只读一轨，内存占用与总时长无关
- 转换完成或取消后自动清理；worker 崩溃时 `crash.log` 里有 `CHIPDECODE_ERROR` 与
  段错误栈（`faulthandler`）

### 常见提示与原因

| 看到 | 含义 |
|---|---|
| `⚠ xxx: 扩展名与实际格式不符（.vgm → VGZ），已按实际格式处理` | 文件扩展名和真实内容不同，**已按真实内容处理**，正常出声 |
| `⚠ xxx: 无法校验内容，按扩展名 .nsf 作为 NSF 处理` | 文件正在写入/为空，无法读头 |
| `xxx: 暂不支持 USF（Nintendo 64）—— 需要该机种的模拟器…` | 真实硬件音频日志，需外部工具 |
| `xxx: 不认识的扩展名 .qqq —— 请反馈给我们补充支持` | 未知格式 |
| `⏭ 空轨已跳过` | 该轨没有声音（HES 常见），已跳过不产出 |

### 开发期测试素材

验收样本放在 `vendor/samples/`（**不随产品分发**），来源是 joshw 各机种子站：

```bash
python vendor/fetch_samples.py --copy-from C:/temp/gmev2   # 复用已有样本
python vendor/fetch_samples.py                             # 联网抓取（限速 0.3s）
```

## 预录流式音频（PS3 / PSP / PS Vita / Wii / 3DS / GameCube / PS4 / X360 / Xbox / Wii U）

v5.9.11 起支持，v5.9.12 补上 PS4 / Xbox 360 / Xbox / Wii U。这一类和上面的**芯片音源 / 内存快照完全不同**：发行商在压盘前
就把音乐编码好了，和 MP3 同性质 —— 没有模拟器核、没有外挂 exe、不需要 BIOS。

| 机种 | 扩展名 | 解码 | 原生率 |
|---|---|---|---|
| PS3 | `.XVAG` `.AT3` | PyAV（`mp3float` / `atrac3plus`） | 48000 |
| PSP | `.hca` `.at3` | PyAV（`hca` / `atrac3plus`） | 44100 |
| PS Vita | `.at9` | PyAV（`atrac9`） | 48000 |
| PS Vita | `.vag` | **原生** `stream_audio.decode_vag`（PSX-ADPCM 28 样本/块） | 44100 / 22050 |
| GameCube | `.psf`（Pivotal） | **原生** `pivotal_psf.py`（PSX-ADPCM 30 样本/块 / NGC-DSP） | 44100 |
| Wii | `.brstm` | PyAV（`adpcm_thp`） | 44100 |
| 3DS | `.laac` | PyAV（`aac`，裸 ADTS） | 48000 |
| PS4 | `.wem` | **vgmstream**（Wwise Custom Vorbis） | 48000 |
| PS4 | `.txtp` | **vgmstream**（Wwise 分层/循环/裁剪序列） | — |
| PS4 | `.bik` | PyAV（`binkaudio_dct`） | 48000 |
| Xbox 360 | `.xma` | PyAV（`xma1`） | 22047 |
| Xbox | `.araw` | PyAV（实为 RIFF/WAVE `pcm_s16le`） | 44100 |
| Wii U | `.fsb` | **vgmstream**（FMOD FSB5 + Nintendo DSP ADPCM） | 44100 |
| Wii U | `.btsnd` | **原生** `stream_audio.decode_btsnd`（PCM16 **大端**） | 48000 |

**`.psf` 现在有两个主人**，判据是**第 4 个字节**：

```
PS1 / PS2 PSF :  0x01 0x02 0x11 0x12 0x21 0x22
Pivotal       :  0x31 0x40 0x60 0x80 0x81 0xA1 0xC0 0xD1
```

`0x01` / `0x21` 两边都用（Pivotal 单声道变体 vs PS1/PS2 版本号），
**故意让给 PSF 家族**（`pivotal_psf.SINGLE_FLAGS` 里没有这两个值）。
判错的表现是「能解的包被报成 BIOS 有问题」，门禁里有一条专门遍历这 14 个字节。

`.txtp` 的目标类型也从「只认 `.xa` / `.DA` / `.psf`」放开到
`.at9` / `.vag` / `.wav` / `.mp3` / `.flac` / `.ogg`（PS Vita 的包是
66 个索引指向 `root/assets_psvita/sk1sound.arc/*.at9`）。

## 回归测试（`vendor/`）

```bash
python vendor/test_progress.py    <SPC文件夹>      # 进度曲线单调、ETA 有值
python vendor/test_progress_gui.py <SPC文件夹>     # 真开窗口 mainloop 端到端
python vendor/test_drop.py                        # 7 个真实 HDROP 拖放场景
python vendor/test_pick.py                        # 统一选择框：文件/文件夹混选
python vendor/test_exe_session.py                 # 会话锁与拖放降级
python vendor/test_joshw.py --offline             # 索引解析/格式化（不联网）
python vendor/test_joshw.py --bundled             # 内置索引快照齐全性（30 个机种）
python vendor/test_chip_formats.py                # 格式识别/时长链（离线，合成样本）
python vendor/test_chip_formats.py --dir vendor/samples   # 追加真实样本交叉验证
python vendor/test_chip_batching.py               # 批次切分不变量（无丢失/峰值有界）
python vendor/test_chip_decode.py --dir vendor/samples    # 端到端解码（需样本）
python vendor/audit_wav.py <WAV目录>               # 输出体检
```

## 几个踩过的坑（改代码前务必看）

1. **`gme_play` 的 `count` 是 short 数，不是帧数**。立体声要传 `帧数 × 2`，
   否则每块只填一半缓冲区，听起来"变慢一倍、每 93ms 断一下"。时长/ID666 校验
   发现不了这种错，只能用 `audit_wav.py` 看相邻样本重复率（故障时 62%，正常 <5%）。
2. **ID666 时长在 `0xA9`**（3 位 ASCII 秒），淡出在 `0xAC`（5 位毫秒）。`0xA3` 是日期，读错会得到全 0 时长。
3. **WAV 体积压不动**：44100×2ch×2B = 176,400 B/s ≈ 10.1MB/分钟。唯一的瘦身开关是「单曲时长上限」。
4. **拖放不能碰 Tk**：Windows 的 `WM_DROPFILES` 回调发生在 Shell 模态拖放循环里，
   在里面调任何 Tcl 函数都会重入解释器 → 卡死/进程消失。窗口过程只做纯 C 操作
   （取路径 + 塞 queue），Tk 侧用 `after` 轮询取队列。
5. **进度要按「首」推，不能只按音源推**：一个文件夹 = 一个音源，只按音源更新会全程停在 0%。
   现在 `write_tracks` / `tracks_to_cd` / `load_spc_paths` 都有逐轨或逐批回调，
   `ConvertWorker` 用「已处理音频秒 / 预估总秒」算百分比（按遍数放大：只出 WAV=1 遍，
   要 CD=解码+写 BIN=2 遍，再加 WAV=3 遍）。
6. **`tk.*` 控件不跟 ttk 主题**：`tk.Listbox`、`tk.Text`（以及 `ScrolledText` 内部
   自带的那根 `tk.Scrollbar`）都不吃 ttk 样式，深色窗口里会闪出白块。换肤时必须显式
   给 `bg / fg / selectbackground / highlightthickness`；日志框统一走
   `msu1_gui.py` 里的 `skin_log()`。
7. **`.psf` 撞名，判据只有第 4 个字节**（v5.9.11）。PS1/PS2 的快照 PSF 和
   GameCube 的 Pivotal 预录流共用这个扩展名，前者要 BIOS、后者不要。判错的
   症状是「能解的包被报成 BIOS 有问题」——用户翻遍 BIOS 目录也没用。
   ⚠ `convert_job.classify()` 里那个 `import pivotal_psf` **必须包 try**：
   它是「加音源」的第一步，抛异常会让整个添加循环挂掉（界面表现为拖进去没反应）。
8. **`.vag` 不能走 FFmpeg**（v5.9.11）。FFmpeg 的 vag 解复用器能 probe 出
   44100/单声道/时长，但**解码时 `avcodec_send_packet()` 报 InvalidDataError**
   （实测 2 个真样本全这样）。所以 `PYAV_KINDS` 里**故意没有** `vag`，它走
   `stream_audio.decode_vag()` → 已有的 PSX-ADPCM 内核（`.npsf` 在用）。
   探到了解不了，比探不到更坑 —— 别看到 `probe ok` 就以为这条路通了。
9. **4 声道不能按 `stereo` 声明**（v5.9.12）。Wwise 的 `.wem` 实测 195/224 个是
   4 声道（FL FR BL BR）。`_resample_to_out` 原来有两条快路径会踩坑：
   `if rate == OUT_RATE: return src.reshape(-1)` 把 4 声道数据原样当立体声返回，
   `layout = "stereo"` 又让 SWR 把「前两个样本当左右、后两个当下一帧」。
   后果是**速度翻倍、音调全错**且不报错。修法：`if nch == 2 and rate == OUT_RATE`，
   并加 `_SRC_LAYOUT`（4 → `"quad"`，SWR 按 FL+BL→L / FR+BR→R 降混）。
10. **Windows MAX_PATH(260) 会让人静默消失**（v5.9.12）。PS4 的 AC4 包里有
   **38 个 `.txtp` 的完整路径是 263~315 字符**。`os.path.isfile()` 对它们返回
   **`False` 而不是抛异常** —— 于是这 38 首在列表里直接不见，不报错、日志无痕，
   `list_txtp` 只返回 148/186。修法：`core.long_path()` 加 `\\?\` 前缀
   （UNC 要拼成 `\\?\UNC\…`，且必须先 `abspath` —— 加了前缀就不再归一化）。
   `open()` / `os.listdir()` / vgmstream 都认这个前缀，三处都实测过。
11. **txtp 行首的 `?` 是「可选文件」标记**（v5.9.12）。WWISER 会写
   `?wem/163303038.wem #i #b 30.0`，意思是「缺了就跳过这一段」。
   当成文件名的一部分会拼出 `\?wem\163303038.wem` → 「目标不存在」，
   而那个 wem 其实好端端在 `wem/` 里（实测 AC4 的 4 首 Plantations_Day）。
   解析时 `lstrip("?")`。
12. **vgmstream 的 `-i` 不能省，11 个 DLL 也不能少**（v5.9.12）。
   不带 `-i` 时它按默认「循环 2 遍 + 淡出 10 秒」渲染，35 秒的 BGM 会变成 80 秒；
   exe 旁边的 11 个 DLL 是**载入时**硬依赖，少一个就 `exit 127`
   （实测报过 `libspeex-1.dll` 和 `avutil-vgmstream-57.dll`，砍不掉）。
   另外它的管道模式（`-p`）WAV 头没有长度字段，PyAV 会提前收尾少 0.18 秒 ——
   **必须落临时文件**。
13. **probe 的时长字段两个都会错**（v5.9.12）。vgmstream `-I` 的
   `numberOfSamples` 是**素材全长**（裁短的 txtp 多报 1.15 秒），
   `playSamples` 对 **layered** 是**各层之和**（70 秒的曲子报成 151 秒）。
   判据：`playSamples` 只在**不超过** `numberOfSamples` 时才可信，否则用后者。
   这个差会直接进循环点计算（MSU-1 靠时长推 loop），所以必须取准。

## 已知限制

- 分批解码（每批 8 首），批边界处百分比会停几秒，状态行显示「解码第 X/N 批」。
- 音频 CD 规范上限 99 轨。
- 回读校验（勾选时）走第二遍读盘，此时进度停在约 2/3 处，状态行显示「回读校验」。
