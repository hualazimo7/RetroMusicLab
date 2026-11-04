# 参考对象拆解：Re:Play 官网

> 对象：`https://github.com/Howard9088/replay-cassette-player-site`（main 分支，2026-11-04 快照）
> 性质：**产品官网源码**，不是播放器软件本体。播放器是 Windows exe，站上只给下载链接。
>
> **许可证边界**：仓库**没有 LICENSE 文件**。按 GitHub 条款，无许可证 = 默认保留所有权利，
> 代码与素材**均不可复制进本仓库**。下文只记录可复用的**思路与几何参数**，无任何代码/图片拷贝。

---

## 1. 仓库定位

一个零构建、零框架的静态多页站，部署在 GitHub Pages（`.nojekyll`）。技术栈和老派的
`web_ui/` 完全同构：IIFE + `'use strict'` + `data-i18n` 属性，没有 npm、没有打包器。

| 维度 | 实测值 |
|---|---|
| 文件总数 | 约 50 个（含 3 个 node 校验脚本、1 个 Actions 工作流） |
| 总字节 | ≈ 28.7 MB |
| 其中图片 | ≈ 28.4 MB，**占 99%** |
| 纯代码 | HTML + CSS + JS 合计不到 300 KB |

> 结论先摆出来：**它的视觉质量几乎全部来自图片预算，不是代码技巧**。这一点决定了哪些能学、哪些学不了。

## 2. 体积构成

| 目录/文件 | 内容 | 体积 |
|---|---|---|
| `assets/cassettes/` | 3 张卡带照片（`metal-c90-v1` `classic-c60-v1` `classic-c90-v1`） | 7.7 MB |
| `assets/scenes/` | 5 张场景 PNG + 3 张 JPG（雨夜客厅、夜桌、海窗、街灯、晴天书桌） | 11.7 MB |
| `assets/brand/` | logo 从 16px 到 1024px 共 16 个文件 | 2.9 MB |
| `media/` | 真实播放器照片、创作者板截图、8 张封面拼成的精灵图 | 5.7 MB |
| `styles.css` / `site-live.js` / `app.js` | 全部样式、i18n、交互 | 32 / 28 / 21 KB |

播放器用到的就是 `metal-c90-v1.png`（2.35 MB，1440×876 有效区）。

## 3. 页面地图

| 页面 | 作用 |
|---|---|
| `index.html` | 首页，hero + 磁带机展示（`#playerShell` 在这里） |
| `experience.html` | 产品介绍：Gold / Retro 两台机器 + 创作者板预览 |
| `shop.html` | 三款卡带商品（现金购买未开放） |
| `gallery.html` | 用户封面画廊 |
| `creator.html` + `admin-preview.html` | 卡带设计板 / 受保护的后台预览 |
| `points.html` | 积分（RP）说明与兑换 |
| `account.html` + `account.css` + `account-ui.js` | 账户，跳转到 `https://202.182.111.237:8443/account.html`（裸 IP + 8443） |
| `policies.html` / `support.html` / `activities.html` | 条款、支持、活动 |
| `404.html` / `robots.txt` / `sitemap.xml` | 常规 |

## 4. 播放器实现（对我们最相关）

### 4.1 分层与坐标

`styles.css` 里播放器是**位图套位图**：外层 `.player-shell` 用 `aspect-ratio: 1670 / 870`
锁死比例，里面全部绝对定位，坐标一律百分比。

| 层 | 选择器 | left / top / width / height |
|---|---|---|
| 金皮肤壳体 | `.skin-gold-image` | `inset: 0` |
| 复古壳体 | `.skin-retro-image` | `inset: 0` |
| 卡带（金） | `.tape-gold` | 15.2% / 15.8% / 36.3% / 37% |
| 卡带（复古） | `.tape-retro` | 4.9% / 7.9% / 32.5% / 39.8% |
| 专辑封面 | `.player-cover-art` | 79.6% / 24.8% / 13.45% / 39.5% |

### 4.2 卷盘旋转：裁圆 + 旋转 + 重画同一张图

`tape-demo.js`（4.7 KB）整个文件只干这一件事，**不需要单独的卷盘素材**：

```js
const source = { x:48, y:64, width:1440, height:876,
                 hubs: [[450,438,270],[1087,438,270]] };   // 绝对坐标 + 半径
// 每个 hub：裁圆 → 平移到圆心 → 旋转 → 把整张图反向偏移重画
ctx.arc(x, y, radius, 0, Math.PI*2); ctx.clip();
ctx.translate(x, y); ctx.rotate(angles[i]); ctx.translate(-x, -y);
ctx.drawImage(image, -source.x, -source.y);
```

- 转速写死：`angles[0] += elapsed*5.2`、`angles[1] += elapsed*4.8`（rad/s），**恒定，不随进度变化**。
- 帧率节流：`now - lastFrame > 32`（约 30fps），页面 `document.hidden` 时停。
- 尊重 `prefers-reduced-motion`：命中时初始 `playing = false`。

### 4.3 皮肤切换

`.player-shell` 挂 `skin-gold` / `skin-retro` 类名，两套图层同时存在，靠 `opacity` + `blur(10px)`
交叉淡化（`transition: .55s`），并给卡带加不同 `brightness(.82 / .73)` 微调。

### 4.4 场景轮播与其它

- hero 背景 5 张场景图，每 5 秒双层 `opacity` 交叉淡化，预加载全部图。
- 8 张封面用**一张 4×2 精灵图** + `background-position` 裁切，省 8 次请求。
- 全站 `@media (prefers-reduced-motion: reduce)` 关动效；`site-live.js` 用 `data-i18n` 做中英切换。

### 4.5 与本项目草案的差异

| 维度 | Re:Play 官网 | 本项目草案 |
|---|---|---|
| 材质来源 | 照片，单张 2.3–2.6 MB | CSS 渐变 + inline SVG，**0 位图** |
| 卷盘运动 | 视觉演示，恒定转速，与音频无关 | 与 `currentTime / duration` 联动，半径随进度变化 |
| 皮肤 | gold / retro 两套位图，类名切换 | 同结论（固定两套材质），但用变量不画位图 |
| 尺寸适配 | `aspect-ratio` + 百分比坐标 | 草案是固定 1040px 设计稿（**需改**，见 §7） |
| 音频 | **完全没有**，demo 不读任何音频 | Web Audio `AnalyserNode`，真实频谱 + VU |
| 仪表 | 无频谱、无 VU | 15 列 LED + 双 VU |
| 双语 | `data-i18n` + 独立字典 | 中文为主 |

**这张表最重要的一行是「材质来源」**：它用 7.7 MB 照片换来的质感，本项目只能用 CSS/SVG 逼近，
所以 deck 的验收标准应该是「形似 + 有真实数据」，不是「像照片一样真」。

## 5. 商业系统（`catalog.json`）

| 字段 | 值 | 含义 |
|---|---|---|
| `mode` | `global-beta` | 全球 Beta |
| `welcomeRewardRp` | 2000 | 新用户赠送积分 |
| `blankCassetteCostRp` | 400 | 空白卡带 |
| `coverRedemptionCostRp` | 400 | 封面兑换 |
| `creatorRewardOnCoverRedemptionRp` | 400 | 创作者每被兑换一版所得 |
| `creatorRewardPercent` | 100 | 分成比例 |
| `cashPurchaseEnabled` | **false** | 现金购买**未开放** |
| `products` | Classic C60 $0.99 / C90 $1.99 / Metal C90 $2.99 | 三款，均带 `rewardPriceRp` |

`api-config.json` 目前 `enabled: false`、`baseUrl: ""`，写明「后端部署后再打开，绝不在此放密钥」——
账户/积分那套**还没真正接通**，站上展示的是 Beta 形态。

## 6. 工程化的可学之处

| 做法 | 说明 |
|---|---|
| `.github/workflows/verify-site.yml` + `tools/verify-site.mjs` | CI 里跑站点结构/品牌资源校验，防「改完页面忘了改资源」 |
| `test_account_auth_gate.mjs` | 注册邀请码逻辑单独有测试脚本 |
| `prefers-reduced-motion` 全站生效 | 动效默认可关，属于基本素养 |
| 精灵图 / 多尺寸 logo | 请求数与首屏体积控制 |

这几条对应到本项目：`_smoke_ui.py`、`_shot_*.py` 已经承担类似角色，不必照搬。

## 7. 对 deck 方案的三处修订（已同步进 `方案分析.md`）

1. **缩放策略换掉**：草案原来打算 `transform: scale()`，改用 **`aspect-ratio` 锁比例 + 百分比绝对定位**。
   理由：`transform` 会让 canvas 变糊、旋钮命中区偏移；百分比坐标在任意窗口宽度下都不错位。
2. **卷盘实现给了一个已验证的最小做法**：不必单独准备卷盘贴图，**裁出 hub 圆 → 旋转 → 重画本体**。
   本项目是 SVG 绘制，同构做法是把卷盘图案放进 `<g>`，对其加 `transform: rotate()`，不必重绘整图。
3. **皮肤方案确认**：它也是「固定两套材质 + 类名切换」，验证了「不跟随 6 套主题」的判断。

## 8. 不可照搬清单

| 项 | 原因 |
|---|---|
| 任何代码 / 图片 | 无 LICENSE，默认保留所有权利 |
| 照片材质方案 | 7.7 MB 卡带图，exe 体积与「0 位图」约束都不允许 |
| 恒定转速的卷盘 | 它只是视觉演示；本项目有真实播放进度，做成恒速会被用户看出「假」 |
| 裸 IP + 8443 的账户站 | 工程上不该这么放，本项目也无账户体系 |
