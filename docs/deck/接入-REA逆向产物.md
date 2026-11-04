# AURORA Player REA 逆向产物接入报告

> 日期：2026-11-04 · 分支：`deck` · 执行依据：[`参数对齐清单.md`](参数对齐清单.md)
> 结论先行：**接入完成，但参数对齐没有完成。** 逆向产物确证了「五组参数名与选项」和「基础配色」，
> **7 组外观常量一个都没拿到实测值**，因此 `proto-aura.html` 的目测常量保持不动，`点阵与辉光-动效方案.md` 不标「已对齐」。

---

## 1. 接入了什么

极光风格的参考对象（`params.template.json` 的 `refProduct: auroraplay`）就是本机已逆向的
`D:\Programs\AURORA Player\AURORA_Player.exe`。此前用 REA 6.8.0 + Ghidra 12.2 做过完整逆向，
产物清单：

| 类别 | 路径 | 内容 |
|---|---|---|
| 会话工作区 | `D:\workbuddy\2026-11-01-15-11-25` | 逆向分析报告、`aurora_lite/` 复刻实现 |
| 字符串 dump | `…\aurora_dump\aurora_ui_strings.txt` | 19639 条去重中文 UI 字符串 |
| 字符串 dump | `…\aurora_dump\aurora_ascii_strings.txt` | 3.96 MB，含完整 QSS 样式表 |
| 解包产物 | `D:\Tools\aurora_unpacked` | 711 文件（`run_dev.dll` 79.9 MB 等） |
| 反编译输出 | `D:\Tools\aurora_pc` | 659 函数中 583 文件（`functions/*.c` 伪 C） |
| 分析结果 | `D:\Tools\aurora_symbols.json` / `aurora_analyze.json` / `aurora_da.json` | 98 个 `aurora.*` 模块符号与分析元数据 |
| REA 工具本体 | `D:\workbuddy AI\2026-10-29-23-22-37\rea` | morluto/rea，commit `bf115be`，rea-agents 6.8.0 |

逆向结论（已写入报告）：Nuitka `--onefile` 打包的 **Python 3.14 + PySide6/Qt 6.11.2** 桌面程序，
版本 2.1.24.0，`KAY` + zstd 载荷 @ `0x237B0`，业务逻辑全在 `run_dev.dll`。

---

## 2. 按清单实际跑过的流程

```bat
py -3.14 docs\deck\tools\scan_ref.py "D:\workbuddy\2026-11-01-15-11-25\aurora_dump" --ext txt --max 40 --out .workbuddy\aurora_scan_strings.json
py -3.14 docs\deck\tools\scan_ref.py "D:\Tools\aurora_pc" --ext c,json --max 40 --out .workbuddy\aurora_scan_pc.json
```

另核对了工具可用性：`scan_ref.py --help` 与 `shot_aura.py --help` 均可运行；
`params.template.json` 经 `json.tool` 校验合法。

| 扫描 | 文件数 | 命中类别 | 未命中 |
|---|---|---|---|
| A · 字符串 dump | 2 | `gap` 3 条（8px / 4px / 1.5px）、`transition` 1 条、40 个 hex 颜色 | 其余 19 类 |
| B · Ghidra 伪 C | 576 | **零** | 全部 21 类 |

扫描候选 JSON 落在 `.workbuddy/`（被仓库 `.gitignore` 忽略，**不进仓库**）：
`.workbuddy/aurora_scan_strings.json`、`.workbuddy/aurora_scan_pc.json`。

---

## 3. 确证到的三类信息

### 3.1 五组参数名与选项（价值最高的一项）

`点阵与辉光-动效方案.md` §3 那张表此前是「按截图反推」，现在有实测中文文案支撑：

| 参数名 | 证据行 |
|---|---|
| 透光 | `aurora_ui_strings.txt:19468` |
| 频谱灯光 | `…:19591`（另有 `…:19592`「频谱灯光 / 氛围灯颜色 / 氛围灯动态」整组文案） |
| 氛围灯颜色 / 氛围灯动态 | `…:1277` / `…:1276` |
| 面板材质 | `…:19524` |

选项词实测出现：冰蓝(304)、冷银(305)、彩虹(755)、呼吸(516)、律动(762)、环绕(1382)、
静态(19523)、光面玻璃(256)、清透玻璃试验版(1333)、金属面板(19513)、鎏金(19514)、
选中金色(19456)、赛博朋克(16479)。

与 `shot_aura.py` 的 CLI 对照：`--light copper/gold/silver/cyber`、`--glow off/low/mid/high`、
`--ambient off/gold/green/ice/violet/rainbow`、`--dyn static/breathe/pulse/circle`、
`--mat glass/metal` —— **选项集合与原产品文案基本对应，原型的建模方向正确**。

### 3.2 基础配色（全局 UI，不是频谱灯色）

`aurora_ascii_strings.txt:25352-25431` 是完整 Qt 样式表，可读出黑金配色：

| 用途 | hex |
|---|---|
| 主窗背景 | `#06080b` |
| 控件底 | `#080a0d` |
| 主金 / 按钮 | `#d7aa52` |
| Logo 亮金 | `#e6ba63` |
| 金色描边 | `#e0b45c` / `#b98b3c` |
| 进度/滑块金 | `#d8ac57` / `#efc979` |
| 主文字 | `#eeeae2` |

⚠️ **这些是全局 UI 主题色，不能填进 `lights.*.bands`**（四套频谱灯光的三段色）。
一旦填进去，就等于用主题金冒充灯光色，比留空更有害。已放进 `params.json` 的 `unmapped` 并明确标注。

### 3.3 视觉相关模块符号

`aurora_symbols.json`：`visual`(117)、`spectrum_scale`(34)、`theme`(219)、
`_demo_board.paint_backlight`(104-108)。说明点阵/背光由 QPainter 侧代码绘制，
但**函数体已被 Nuitka 编译成机器码**，符号表只给名字，不给数值。

---

## 4. 7 组常量的对齐状态

| # | 常量 | 模板字段 | 状态 | 原因 |
|---|---|---|---|---|
| ① | 点阵列/行 | `dots.cols` / `rows` | **null** | 字符串与伪 C 中无 `COLS`/`ROWS`/`barCount` 等可识别常量 |
| ② | 三段色分界 | `dots.bandEdge` | **null** | 无 `bandEdge`/`threshold`，Qt 侧也没有 `linear-gradient(...%)` 这类 CSS 渐变 |
| ③ | 四档透光 | `glowLevels` / `ringOpacity` | **null** | 无强度表；QSS 里的 `opacity` 全是控件态，不是透光档位 |
| ④ | 光晕 blur / inset | `halo` | **null** | Qt 无 `blur(24px)` / `box-shadow` 语法，效果在 QPainter 里算 |
| ⑤ | 呼吸 / 环绕周期 | `motion.breatheSec` / `spinSec` | **null** | 无 `animation: …s`，QTimer 间隔是编译后的立即数 |
| ⑥ | 律动映射 | `motion.pulseFormula` | **null** | 无可读公式 |
| ⑦ | 材质噪声 | `material` | **null** | 参考实现无 `feTurbulence`；SVG 噪声是本项目自己的实现 |

**为什么拿不到（技术原因）**：`scan_ref.py` 的规则表是为 Web / QML / Flutter 文本栈写的
（`COLS=`, `blur(`, `animation:`, `baseFrequency`, `fftSize`…）。而本参考对象经 Nuitka 编译后，
视觉数值以**立即数、数组初始化、QPainter 运行时计算**存在于 `run_dev.dll` 的机器码中；
Ghidra 反编译出的伪 C 里这些值表现为十六进制常量，且缺少变量名，与「点阵列数」之间无法建立可验证的映射。
按清单红线「找不到留 null，不要按截图猜」，**不填任何推测值**。

---

## 5. 红线遵守情况

| 红线 | 执行 |
|---|---|
| 不把反编译代码放进仓库 | ✅ 扫描候选留在 `.workbuddy/`（gitignore）；`params.json` 无函数体、无 QSS 全文 |
| 只填数字 / hex / 一句话描述 | ✅ `params.json` 仅含 null、版本号、栈标识、hex 与简短说明 |
| 不搬素材 | ✅ 未复制任何 PNG / 字体 / 图标 / 音频；仓库里原有两张 `shot-aura-*.png` 未改动 |
| 反编译产物不上传云端 | ✅ 全程本机读写，未上传 |
| 对外命名用「极光」 | ✅ 未引入 `AURORA` 名称或 logo |
| 参考对象无 LICENSE | ✅ 只取功能描述、颜色值与几何观感结论 |

---

## 6. 明确「没有动」的东西

- `proto-aura.html` 的 8 处常量**一行未改**（`LIGHTS` 406 / `AMBIENT` 416 / `GLOW` 425 /
  `RING_OP` 426 / `.aura` 121-130 / `animation` 141、146 / `.surface::after` 182-190 /
  `COLS` 521 / `ROWS` 522 / `BAND_EDGE` 533 / 主循环 692）—— 行号已逐条实测核对，
  仅 `AMBIENT` 与清单差 1 行（清单写 417，实际 416）。
- `点阵与辉光-动效方案.md` §5/§6 **没有**标注「已对齐实测值」，因为还没有实测值。
- `params.template.json` 模板保留，未覆盖。
- 未运行 `shot_aura.py` 重拍截图（没有新数值可拍，且需 playwright，非必需）。

---

## 7. 若要把 7 组数值真正对齐，下一步怎么做

1. 在 Ghidra 里定位 `visual` / `_demo_board.paint_backlight` 对应的函数体
   （⚠️ 注意：`D:\Tools\aurora_pc` 的 659 个函数是 **exe 启动器**、全部为 `FUN_` 无名函数，
   不含 `aurora$` 符号，**不能**用来反查业务函数 —— 见 §9），
   人工读反编译伪 C 里的循环上界与颜色数组，把**数字**记进 `params.json`。
2. 或走动态路径：运行时抓 QPainter 绘制参数 / QTimer 间隔，只记录数值。
3. 拿到值后再按 `参数对齐清单.md` §4 改 `proto-aura.html` 的 8 处、重拍截图、
   并在 `点阵与辉光-动效方案.md` §5/§6 注明「实测 / 推断」。

**在那之前，参数对齐清单 §1 仍标记为「待执行」，不要按任何截图或直觉填数。**

---

## 8. 交付物

| 文件 | 说明 |
|---|---|
| `docs/deck/params.json` | 基于模板的部分填充：可确证的 `_meta` 与 `unmapped`，7 组常量保持 null |
| `docs/deck/接入-REA逆向产物.md` | 本报告 |
| `.workbuddy/aurora_scan_*.json` | 两次扫描的原始候选（本地，不进仓库） |

---

## 9. 2026-11-04 重跑记录（REA 直跑主程序）

### 9.1 跑了什么

| 目标 | 命令 | 耗时 | 结果 |
|---|---|---|---|
| `AURORA_Player.exe` | `rea analyze --provider ghidra` | **46 s** | 9 段 / **659 函数** / 495 字符串；evidence_id `ev_a5fe67c3…` **与 2026-11-01 完全一致** |
| `run_dev.dll`（业务体） | 同上 | 首次 **332 s 超时**（默认 330 s 启动窗口）；设 `REA_GHIDRA_STARTUP_TIMEOUT_MS=1800000` 后 **23m58s 成功** | 8 段 / **32,102 函数** / **259,983 字符串** / `.text` 62.3 MB |

### 9.2 三个新结论

1. **REA 的 PE 分析是确定性的**：同一个 exe 两次分析得到同一个 `evidence_id`（`ev_a5fe67c3…`），结论可复现、可回归。
2. **业务体是启动器的 48 倍**：659 → 32,102 个函数；495 → 259,983 条字符串。此前"98 个 Python 模块"的定性判断，现在有了规模量化。
3. **纠正一处理解偏差**：`D:\Tools\aurora_pc` 的 `decompile-index.json` 里 `target_sha256` 是 **exe** 的哈希，659 个函数全是 `FUN_` 无名函数、**零条 `aurora$` 符号**。也就是说上次反编译的是 **Nuitka 启动器**，不是业务逻辑。§7 第 1 条已据此修正。

### 9.3 对「7 组常量」的影响

- 仍然**一个数值都没拿到**；
- 原因更明确了：`run_dev.dll` 无符号表（`symbol table ptr=0`）、无导出（export size=0），32,102 个函数全部是 `FUN_`；
  而 Nuitka 的 `aurora$visual$$$function__N_name` 只以**字符串**形式存在于 `.rdata`
  （`aurora$visual` 20 次、`aurora$_demo_board` 64 次、`paint_backlight` 6 次、`spectrum_scale` 7 次），
  Ghidra 不会把它关联成函数名；
- `.rdata` 里 668 个唯一 hex 绝大多数是第三方库调色板（pygments 等），光效相关 QSS 只有 9 条，且没有 `#Aura` / `#Glow` 类选择器。

### 9.4 为什么不能靠「全量反编译」解决

- 3.2 万函数，`decompile-all` 受安全策略限制单次 `--limit 40` → 约 800 批；
- REA 文档明确「下一次 provider 查询会重新导入原始产物」，单次导入分析就是 24 分钟，总成本不可接受；
- 即使反编译完，也没有 xref 索引，只能在伪 C 里全文 grep 字符串名。

### 9.5 若仍要拿数值，两条现实路径

1. **Ghidra GUI 人工定位**：先 `analyze --snapshot` 保存一次分析库（约 24 分钟），再在 GUI 里对 `aurora$visual` 等字符串做 xref，逐个反编译引用函数。仍是小时级工作量，且不保证有常量。
2. **动态抓取（更现实）**：启动 AURORA Player，用 Qt/RenderDoc 或注入脚本抓 `paint_backlight` 的实际绘制参数，只记录数值、不读代码。

### 9.6 本次产物

| 路径 | 内容 |
|---|---|
| `.workbuddy/aurora-reanalysis/exe.json` | exe 概览（6.9 KB） |
| `.workbuddy/aurora-reanalysis/dll2.json` | DLL 概览（6.8 KB，24 分钟那次） |
| `.workbuddy/aurora-reanalysis/dll.json` | 首次超时的错误 JSON（保留作证据） |
