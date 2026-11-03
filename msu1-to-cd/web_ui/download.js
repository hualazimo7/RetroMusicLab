/* ═══════════════════════════════════════════════════════════
   音源下载页 —— 前端逻辑
   轮询 /api/dl/state（300ms），只做「状态搬运」，判定全在后端。
   ═══════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);

  /* ── 虚拟滚动（v5.9.1）────────────────────────────────
     原先列表最多渲染 800 行，超了就在底部写「还有 N 条未显示 —— 用搜索
     缩小范围」。可机种一大就废了：Switch 3512 条、街机 VGM 1450 条，
     想翻到底根本做不到。

     现在改成只渲染**可视区**那几十行：外层 .dl-vs 撑出 total×ROW_H 的
     假高度负责滚动条，内层 .dl-vs-win 用 translateY 定位到当前窗口。
     DOM 节点数恒定（约 60 行），3000 条和 30000 条一样流畅。
     ⚠ ROW_H 必须与 download.css 里 `.dl-row { height: 27px }` 一致，
       否则滚动位置会漂。改行高时两处一起改。 */
  const ROW_H = 27;              // 单行高度（px），与 CSS 严格对应
  const OVERSCAN = 16;           // 可视区上下各多渲染几行，快速滚动不留白

  const S = {
    seq: 0,
    platforms: [],
    key: "",
    rows: [],          // 后端给的原始行
    idx: [],           // 过滤后命中 S.rows 的**下标**（不是行对象引用）
    filter: "",
    busy: false,
    sig: "",           // 行的静态签名，变了才重建 DOM
    lastFilter: "",    // 上次重建时的过滤词，用来判断要不要回到顶部
    nodeOf: {},        // url -> tr 节点（只有可视窗口内的行在里面）
    vs: { start: -1, end: -1 },   // 当前渲染的窗口 [start, end)
    snap: null,        // 最近一次 snapshot（上传云盘要读 wav_dir）
    refreshing: false, // 上一次轮询时「更新全部列表」是否在跑（判断跑完的瞬间）
    toastTimer: null,
    // ── 试听 ──
    wavs: [],          // /api/dl/wavs 给的列表
    /* rel 串起来的签名，变了才重建列表。**null = 还没扫过 / 要求强制重建**：
       不能用 "" 当哨兵 —— 列表被清空时签名正好也是 ""，两者相等就直接
       return，于是「计数变 0、列表还是旧的」（v5.9.2 实测踩到）。 */
    wavSig: null,
    wavAt: 0,          // 上次拉列表的时间（节流用）
    wavDest: null,     // 上次列表对应的保存目录（切机种会变，必须重扫）
    playRel: "",       // 正在放的那首（相对路径）
    seeking: false,    // 正在拖进度条 —— 期间 ontimeupdate 不许回写（v6.0.3）
    coverGame: "",     // 封面区当前显示的**游戏名**（同一游戏连播不重拉图）
    coverN: 0,         // 这个游戏有几张图（v6.0.7 轮播）
    coverIdx: 0,       // 现在显示的是第几张
  };

  // ── 工具 ──────────────────────────────────────────────
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function toast(msg, ms) {
    const el = $("#dlToast");
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(S.toastTimer);
    S.toastTimer = setTimeout(() => { el.hidden = true; }, ms || 2600);
  }

  async function api(path, body) {
    const opt = body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(body),
    };
    const r = await fetch(path, opt);
    let d = {};
    try { d = await r.json(); } catch (e) { d = {}; }
    if (!r.ok && !d.error) d.error = "HTTP " + r.status;
    return d;
  }

  // ── 主题 ──────────────────────────────────────────────
  // 真正的切换在 theme.js 里（两页共用，含 6 套复古配色）。
  // 下载页靠 URL 的 ?theme= 继承主界面的选择，页内也能直接改。
  function paintThemeBtns() {
    const retro = String(ThemeKit.now()).startsWith("retro:");
    const seg = $("#dlThemeSeg");
    if (!seg) return;
    seg.querySelectorAll("button").forEach((b) => {
      if (b.dataset.themeBtn === "retro") b.classList.toggle("on", retro);
      else b.classList.toggle("on", !retro);
    });
  }
  function applyTheme(t) { ThemeKit.apply(t); paintThemeBtns(); }
  ThemeKit.boot().then(() => {
    ThemeKit.mountPanel("#dlThemeSeg");
    paintThemeBtns();
  });

  // ── 机种树 ────────────────────────────────────────────
  // v5.9：机种从 30 涨到 49（kss 拆成 GG/MSX/SMS 三条、hoot 拆成 18 个平台、
  // 新增 switch），平铺下来左栏一屏放不下，所以分组改成可折叠。
  //
  // 默认只展开**第一组**：49 行全铺开反而更难找（用户要的是「先看有哪些
  // 大类」）。状态记在 localStorage 的 `rml-dl-grp`（{"复古主机":true,...}），
  // 记住用户上次收起了哪几组 —— 不然每次打开都要重收一遍。
  const GRP_KEY = "rml-dl-grp";

  function grpSaved() {
    try {
      const o = JSON.parse(localStorage.getItem(GRP_KEY) || "{}");
      return (o && typeof o === "object") ? o : {};
    } catch (e) { return {}; }
  }
  function grpRemember(name, open) {
    const o = grpSaved();
    o[name] = !!open;
    try { localStorage.setItem(GRP_KEY, JSON.stringify(o)); } catch (e) { }
  }

  // 展开/收起一组：箭头靠 CSS 的 .open 类转 90°，可见性靠 body.hidden。
  // 两个都要设 —— 只设 hidden 的话箭头不转，用户看不出这组是开是关。
  function setGroupOpen(head, open) {
    const body = head.nextElementSibling;
    if (!body) return;
    head.classList.toggle("open", !!open);
    head.setAttribute("aria-expanded", open ? "true" : "false");
    body.hidden = !open;
  }

  function groupHeadOf(el) {
    const body = el && el.closest ? el.closest(".dl-grp-body") : null;
    return body ? body.previousElementSibling : null;
  }

  async function loadPlatforms() {
    const d = await api("/api/dl/platforms");
    if (!d.ok) {
      $("#dlPlat").innerHTML = '<div class="dl-empty">' + esc(d.error || "读取失败") + "</div>";
      return;
    }
    S.platforms = d.groups || [];
    const box = $("#dlPlat");
    const saved = grpSaved();
    box.innerHTML = "";
    S.platforms.forEach((g, gi) => {
      const head = document.createElement("button");
      head.type = "button";
      head.className = "dl-grp";
      head.dataset.group = g.group;
      head.innerHTML = '<span class="dl-grp-arrow" aria-hidden="true">▸</span>' +
        '<span class="dl-grp-name">' + esc(g.group) + "</span>" +
        '<span class="dl-grp-n">' + g.items.length + "</span>";
      const open = (g.group in saved) ? !!saved[g.group] : (gi === 0);
      head.onclick = () => {
        const now = !head.classList.contains("open");
        setGroupOpen(head, now);
        grpRemember(g.group, now);
        paintGrpAll();
      };
      box.appendChild(head);

      const body = document.createElement("div");
      body.className = "dl-grp-body";
      g.items.forEach((p) => {
        const it = document.createElement("div");
        it.className = "dl-plat-item";
        it.dataset.key = p.key;
        it.dataset.group = g.group;
        it.innerHTML = '<span class="dl-plat-name">' + esc(p.name) +
          '</span><span class="dl-plat-fmt">' + esc(p.fmt) + "</span>";
        it.onclick = () => pickPlatform(p.key);
        body.appendChild(it);
      });
      box.appendChild(body);
      setGroupOpen(head, open);
    });
    paintGrpAll();
  }

  // 「全部」按钮：全开就变全收，否则全展开。文案跟着状态走，
  // 不然用户点一次之后不知道下一次点会发生什么。
  function paintGrpAll() {
    const btn = $("#dlGrpAll");
    if (!btn) return;
    const heads = Array.from(document.querySelectorAll(".dl-grp"));
    if (!heads.length) { btn.textContent = "全部"; return; }
    const allOpen = heads.every((h) => h.classList.contains("open"));
    btn.textContent = allOpen ? "全收" : "全开";
    btn.title = allOpen ? "收起所有分组" : "展开所有分组";
  }

  function toggleGrpAll() {
    const heads = Array.from(document.querySelectorAll(".dl-grp"));
    if (!heads.length) return;
    const allOpen = heads.every((h) => h.classList.contains("open"));
    const want = !allOpen;
    heads.forEach((h) => {
      setGroupOpen(h, want);
      grpRemember(h.dataset.group, want);
    });
    paintGrpAll();
  }

  async function pickPlatform(key) {
    if (S.busy) { toast("正在下载/转换，先停止再切机种"); return; }
    if (S.key === key) return;
    S.key = key;
    markPlatform(key);
    S.filter = "";
    $("#dlSearch").value = "";
    $("#dlRows").innerHTML = '<div class="dl-empty">加载中…</div>';
    const d = await api("/api/dl/load", { key: key });
    if (!d.ok) toast(d.error || "加载失败");
    poll();
  }

  function markPlatform(key) {
    let sel = null;
    document.querySelectorAll(".dl-plat-item").forEach((el) => {
      const on = el.dataset.key === key;
      el.classList.toggle("on", on);
      if (on) sel = el;
    });
    // 选中的机种若落在**收起**的组里，自动把那组展开 —— 否则用户点了列表
    // 却看不到左侧高亮在哪（折叠后这是最容易踩的坑）。
    if (sel) {
      const head = groupHeadOf(sel);
      if (head && head.classList.contains("dl-grp") &&
          !head.classList.contains("open")) {
        setGroupOpen(head, true);
        grpRemember(sel.dataset.group, true);
        paintGrpAll();
      }
    }
  }

  // ── 列表 ──────────────────────────────────────────────
  function match(row, q) {
    if (!q) return true;
    const s = (row.name + " " + (row.zh || "")).toLowerCase();
    return s.indexOf(q) >= 0;
  }

  /* 造一行 DOM。抽出来是因为窗口滚动时会被反复调用 —— 逻辑只留一份，
     免得「首次渲染」和「滚动补渲染」两处字段慢慢长歪。 */
  function buildRow(r) {
    const tr = document.createElement("div");
    tr.className = "dl-row" + (r.sel ? " on" : "");
    tr.dataset.url = r.url;
    tr.innerHTML =
      '<span class="c-sel">✓</span>' +
      '<span class="c-name" title="' + esc(r.name) + '">' + esc(r.name) + "</span>" +
      '<span class="c-zh" title="' + esc(r.zh || "") + '">' + esc(r.zh || "—") + "</span>" +
      '<span class="c-date">' + esc(r.mtime || "") + "</span>" +
      '<span class="c-size">' + esc(r.size_text || "") + "</span>" +
      '<span class="c-state"></span>' +
      '<span class="c-act"></span>';
    tr.onclick = () => toggleRow(r.url);
    return tr;
  }

  /* 只渲染可视窗口 [start, end)。force=true 时无条件重建。 */
  function renderWindow(force) {
    const box = $("#dlRows");
    const sizer = box.firstElementChild;
    if (!sizer || !sizer.classList.contains("dl-vs")) return;
    const win = sizer.firstElementChild;
    const total = S.idx.length;
    if (!total || !win) return;

    const top = box.scrollTop;
    const h = box.clientHeight || 400;
    const visStart = Math.floor(top / ROW_H);
    const visEnd = Math.ceil((top + h) / ROW_H);

    /* 可视区还稳稳落在已渲染窗口的「内芯」里就什么都不做。
       不这么写的话，每滚 1px 都要重建 60 行 DOM，滚轮一推就掉帧。
       留 4 行余量，等于每滚过 12 行才重建一次。 */
    if (!force && S.vs.start >= 0 &&
        visStart - S.vs.start >= 4 && S.vs.end - visEnd >= 4) return;

    const start = Math.max(0, visStart - OVERSCAN);
    const end = Math.min(total, visEnd + OVERSCAN);
    if (!force && start === S.vs.start && end === S.vs.end) return;

    S.vs.start = start;
    S.vs.end = end;
    win.style.transform = "translateY(" + (start * ROW_H) + "px)";

    const frag = document.createDocumentFragment();
    S.nodeOf = {};
    for (let i = start; i < end; i++) {
      const r = S.rows[S.idx[i]];
      if (!r) continue;
      const tr = buildRow(r);
      S.nodeOf[r.url] = tr;
      frag.appendChild(tr);
    }
    win.innerHTML = "";
    win.appendChild(frag);
  }

  function rebuildRows() {
    const q = S.filter.trim().toLowerCase();
    /* 存**下标**而不是行对象：poll 每 300ms 会把 S.rows 换成新数组，
       若这里存的是旧对象引用，滚出屏幕再滚回来的行就会显示上一次的
       选中/状态。存下标则永远读最新。 */
    S.idx = [];
    for (let i = 0; i < S.rows.length; i++) {
      if (!q || match(S.rows[i], q)) S.idx.push(i);
    }

    const box = $("#dlRows");
    const keepTop = (q === S.lastFilter) ? box.scrollTop : 0;
    S.lastFilter = q;
    S.nodeOf = {};
    S.vs.start = -1;
    S.vs.end = -1;
    box.innerHTML = "";

    if (!S.idx.length) {
      const d = document.createElement("div");
      d.className = "dl-empty";
      d.textContent = S.rows.length ? "没有匹配的条目" : "这个机种还没有条目";
      box.appendChild(d);
      return;
    }

    // 假高度撑出滚动条；真正的行都挂在 .dl-vs-win 里
    const sizer = document.createElement("div");
    sizer.className = "dl-vs";
    sizer.style.height = (S.idx.length * ROW_H) + "px";
    const win = document.createElement("div");
    win.className = "dl-vs-win";
    sizer.appendChild(win);
    box.appendChild(sizer);

    /* 后端还有一层硬上限（dl_api.MAX_ROWS）。真撞上了要说清楚，
       否则用户会以为「这个机种就这么多」。正常情况下这行不会出现。 */
    const snap = S.snap;
    if (snap && snap.truncated) {
      const more = document.createElement("div");
      more.className = "dl-more";
      more.textContent = "条目过多，只载入了前 " + S.rows.length.toLocaleString() +
        " 条（共 " + (snap.count || 0).toLocaleString() + " 条）";
      box.appendChild(more);
    }

    box.scrollTop = keepTop;
    renderWindow(true);
  }

  async function toggleRow(url) {
    // 点整行就是切换。原先这里写了个
    //   ev.ctrlKey ? "toggle" : "toggle"
    // 两个分支一样，纯属噪音 —— 行内没有别的点击语义。
    await api("/api/dl/select", { urls: [url], mode: "toggle" });
    poll();
  }

  // ── 试听 ──────────────────────────────────────────────
  /* 下载页也要能放刚转出来的 WAV（tkinter 版的下载页本来就有）。
     两件事：
       1. 列表：GET /api/dl/wavs → 递归扫 WAV 根目录，**新的在前**；
       2. 播放：GET /api/dl/audio?rel=<相对路径> → 后端做越界校验后发字节。
     路径一律用后端给的 rel，前端不拼绝对路径。 */
  const audio = new Audio();
  audio.preload = "none";
  /* 探针：audio 是本 IIFE 的闭包变量，shot.py / 门禁在外面读不到。
     挂到 window 上，跟 index.html 的 window.__errs 同一个思路 ——
     播放没声音时能直接读出 paused / currentTime / error.code，
     不用对着界面猜。 */
  window.__dlAudio = audio;

  /* 频谱 / 波形（v5.9.6）：和主界面同一块面板，实现全在 viz.js 里。
     ⚠ AudioContext 是**懒建**的（第一次真播放才建）—— Chrome 只在用户
       手势里才肯把它从 suspended 恢复，这里就建的话频谱永远是一条平线。 */
  const viz = VizKit.create({
    audio: audio,
    canvas: "#dlVizCanvas",
    wrap: "#dlVizWrap",
    empty: "#dlVizEmpty",
    toggle: "#dlVizTog",
    key: "msu1.dl.viz",        /* 和主界面分开记：两页可能一个想开一个想关 */
  });
  window.__dlViz = viz;        /* 探针，同 window.__dlAudio 的理由 */

  /* 音量（v5.2.4）：喇叭键 = 静音开关，滑块 = 音量，值记在 localStorage。
     ⚠ `new Audio()` 的默认音量是 **1.0** —— 不记住的话每次打开下载页
     第一下播放都会「突然最大声」，尤其夜里戴着耳机时很要命。
     口径：**只用 audio.volume 表示音量**，不碰 audio.muted
     （两个开关各存一份状态，必然有一天对不上：滑块 40% 但 muted=true，
     界面到底该显示哪个？）。静音 = volume 归 0，取消静音 = 恢复原值。 */
  const VOL_KEY = "dl_vol";
  let volBefore = 0.8;                 // 静音前的音量，取消静音时恢复到它

  function paintVol() {
    const v = audio.volume;
    const muted = v <= 0;
    const btn = $("#dlVolBtn");
    btn.classList.toggle("muted", muted);
    btn.setAttribute("aria-pressed", muted ? "true" : "false");
    btn.title = muted ? "取消静音" : "静音（可滚轮微调）";
    $("#dlVolW1").style.display = muted ? "none" : "";
    $("#dlVolW2").style.display = muted ? "none" : "";
    $("#dlVolX").style.display = muted ? "" : "none";
    const el = $("#dlVol");
    el.value = String(Math.round(v * 100));
    // 轨道是自绘的两段式渐变，填充比例靠这个变量传给 CSS
    el.style.setProperty("--vol-pct", (v * 100).toFixed(1) + "%");
  }

  function setVol(v, save) {
    v = Math.max(0, Math.min(1, Number(v) || 0));
    audio.volume = v;
    if (v > 0) volBefore = v;          // 只有非 0 才记，否则静音会把原值冲掉
    if (save) {
      try { localStorage.setItem(VOL_KEY, String(Math.round(v * 100))); } catch (e) {}
    }
    paintVol();
  }

  (function initVol() {
    let pct = 80;
    try {
      const raw = localStorage.getItem(VOL_KEY);
      if (raw !== null && raw !== "") {
        const n = parseInt(raw, 10);
        if (isFinite(n)) pct = Math.max(0, Math.min(100, n));
      }
    } catch (e) { /* 隐私模式下 localStorage 会抛，忽略即可 */ }
    if (pct > 0) volBefore = pct / 100;
    setVol(pct / 100, false);
  })();

  $("#dlVol").oninput = (e) => setVol(Number(e.target.value) / 100, true);
  $("#dlVolBtn").onclick = () => {
    setVol(audio.volume > 0 ? 0 : (volBefore > 0 ? volBefore : 0.8), true);
  };
  $("#dlVolBtn").onwheel = (e) => {
    e.preventDefault();                // 别让滚轮去滚整个侧栏
    setVol(audio.volume + (e.deltaY < 0 ? 0.05 : -0.05), true);
  };

  function fmtDur(s) {
    if (!isFinite(s) || s < 0) s = 0;
    const m = Math.floor(s / 60);
    const r = Math.floor(s % 60);
    return m + ":" + (r < 10 ? "0" : "") + r;
  }

  async function loadWavs(force) {
    S.wavAt = Date.now();
    let d = {};
    try {
      d = await api("/api/dl/wavs" + (force ? "?force=1" : ""));
    } catch (e) {
      return;                                   // 轮询里失败不打扰用户
    }
    if (!d.ok) return;
    const list = d.wavs || [];
    $("#dlPlayCount").textContent = (d.total || 0) + " 首";
    /* v5.9.9：签名要带格式。同一个游戏的 WAV 和 FLAC 是两条独立记录，
       只按 rel 拼签名的话，先转 WAV 再转 FLAC 时 rel 会从
       ``FF7/01_x.wav`` 变成 ``FF7/01_x.flac``（不相等，能触发重建），
       但两种都有时列表里两条 rel 不同、能共存 —— 带 fmt 更稳。 */
    const sig = list.map((w) => (w.fmt || "wav") + ":" + w.rel).join("|");
    if (sig === S.wavSig) return;                // 没变化就不动 DOM（别打断滚动）
    S.wavSig = sig;
    S.wavs = list;
    const box = $("#dlPlayList");
    if (!list.length) {
      /* 空列表有两种含义，别混成一句话：
         「还没转过」和「转过但被你清空了」—— 后者要是也显示「下载并转
         WAV 后这里会出现曲目」，用户会以为文件被删了。 */
      box.innerHTML = '<div class="dl-empty">' + (d.cleared && d.hidden
        ? "列表已清空（" + d.hidden + " 首）—— 音频文件还在磁盘上"
        : "下载并转换后，这里会出现可试听的曲目") + "</div>";
      showCover(null);                 /* 没有可放的了，封面区跟着回到空态 */
      syncSkipBtns();                  /* v6.0.3：列表空了，上/下一曲置灰 */
      return;
    }
    const frag = document.createDocumentFragment();
    list.forEach((w) => {
      const row = document.createElement("div");
      row.className = "dl-play-row";
      row.dataset.rel = w.rel;
      row.title = w.rel + "　" + (w.size_text || "");
      /* v6.0.0：四种格式通用化 —— 以前是 `isFlac ? "FLAC" : "WAV"` 的三元式，
         加了 MP3 时忘了改，结果 .mp3 在试听列表里被标成「WAV」。
         现在从扩展名推，加格式不用再动这里。 */
      const tag = fmtOfName(w.name);
      row.innerHTML =
        '<span class="p-ico">▶</span>' +
        '<span class="p-name">' + esc(String(w.name || "")
          .replace(/\.(wav|flac|mp3|ogg)$/i, "")) + "</span>" +
        '<span class="p-fmt' + (tag === "wav" ? "" : " " + tag) + '">' +
          tag.toUpperCase() + "</span>" +
        '<span class="p-game">' + esc(w.game || "") + "</span>" +
        '<span class="p-size">' + esc(w.size_text || "") + "</span>";
      row.onclick = () => playWav(w);
      frag.appendChild(row);
    });
    box.innerHTML = "";
    box.appendChild(frag);
    markPlaying();
    syncSkipBtns();                    /* v6.0.3：列表重建后同步上/下一曲的可用性 */
  }

  function markPlaying() {
    document.querySelectorAll(".dl-play-row").forEach((el) => {
      const on = el.dataset.rel === S.playRel;
      el.classList.toggle("on", on);
      const ico = el.querySelector(".p-ico");
      if (ico) ico.textContent = (on && !audio.paused) ? "❙❙" : "▶";
    });
  }

  function playWav(w) {
    const same = S.playRel === w.rel;
    if (same && !audio.paused) { audio.pause(); return; }   // 再点一次 = 暂停
    if (same && audio.src) { audio.play().catch(() => {}); return; }
    S.playRel = w.rel;
    audio.src = "/api/dl/audio?rel=" + encodeURIComponent(w.rel);
    /* v6.0.3：换曲先把进度条清零。不清的话上一首的百分比会留在新曲目上
       （实测：切歌后进度条还显示上一首的 14%，看着像「这首从 14% 开始」）。
       此刻 duration 还是上一首的旧值，所以不能靠 paintProgress 算。 */
    $("#dlPlayBar").style.width = "0%";
    $("#dlPlayKnob").style.left = "0%";
    $("#dlPlayTime").textContent = "0:00";
    audio.play().catch((e) => {
      toast("播放失败：" + (e && e.message ? e.message : "格式不支持"));
    });
    const nm = String(w.name || "").replace(/\.(wav|flac|mp3|ogg)$/i, "");
    $("#dlPlayName").textContent = w.game ? nm + " · " + w.game : nm;
    $("#dlPlayName").classList.add("on");
    $("#dlPlayBtn").disabled = false;
    viz.touched = true;
    viz.paintEmpty();
    if (viz.on && viz.ensure()) viz.start();
    showCover(w);                 /* v6.0.1：封面跟着「正在试听的那首」走 */
    markPlaying();
    syncSkipBtns();               /* v6.0.3：有当前曲目，上/下一曲才可点 */
  }

  /* ── 上一曲 / 下一曲（v6.0.3）────────────────────────────
     用户报：「也没有下一曲、上一曲按钮」。

     顺序 = 试听列表的显示顺序（``S.wavs``，后端按 WAV→FLAC→MP3→OGG、
     再按文件名排）。两端**循环** —— 列表只有 3 首时，「下一曲」按到底
     不该变成一个死键。

     只有一首时**重新从头播**，不走 ``playWav``：同一首 rel 在 ``playWav``
     里的语义是「暂停」，用户按「下一曲」却暂停了，那是 bug 不是特性。 */
  function stepTrack(dir) {
    const list = S.wavs || [];
    if (!list.length) return;
    let i = list.findIndex((w) => w.rel === S.playRel);
    if (i < 0) i = dir > 0 ? -1 : 0;
    const n = list.length;
    const j = ((i + dir) % n + n) % n;
    if (j === i) {
      audio.currentTime = 0;
      audio.play().catch(() => {});
      return;
    }
    playWav(list[j]);
  }

  function syncSkipBtns() {
    const on = (S.wavs || []).length > 0 && !!S.playRel;
    $("#dlPrev").disabled = !on;
    $("#dlNext").disabled = !on;
  }

  /* ── 进度条：可点、可拖、可键盘（v6.0.3）─────────────────
     用户报：「播放器进度无法调节」。原来那条 ``.dl-play-bar`` 只是个纯展示
     的 div，没有任何事件；宽度也只有曲名那一截（约 150px），就算绑了事件
     也很难点中。

     三条实现要点：

     1. ``pointerdown/move/up`` + ``setPointerCapture`` —— 鼠标、触控笔、
        手指一套代码。不用 mousedown/touchstart 各写一份。
     2. 拖动期间置 ``S.seeking``，让 ``ontimeupdate`` 让位。否则每 250ms
        一次的时间更新会把用户拖到的位置往回拽 —— 表现就是「拖不动」。
     3. 视觉 4px、命中区 16px（CSS 里 ``::after`` 撑开）。细条好看但点不中，
        粗条又抢曲名的注意力，两头都要。 */
  function seekPct() {
    const d = Number(audio.duration);
    if (!isFinite(d) || d <= 0) return 0;
    return Math.min(100, Math.max(0, 100 * audio.currentTime / d));
  }

  function paintProgress() {
    const pct = seekPct();
    const ready = isFinite(Number(audio.duration)) && Number(audio.duration) > 0;
    $("#dlPlayBar").style.width = pct.toFixed(1) + "%";
    $("#dlPlayKnob").style.left = pct.toFixed(1) + "%";
    $("#dlPlayTime").textContent = fmtDur(audio.currentTime || 0);
    const box = $("#dlPlaySeek");
    box.classList.toggle("idle", !ready);
    box.setAttribute("aria-valuenow", String(Math.round(pct)));
    box.setAttribute("aria-valuetext", fmtDur(audio.currentTime || 0));
  }

  function seekToRatio(r) {
    const d = Number(audio.duration);
    if (!isFinite(d) || d <= 0) return;
    audio.currentTime = Math.min(1, Math.max(0, r)) * d;
    /* 立刻重画：Chrome 要等 seek 落定才发 timeupdate，拖的时候会「一卡一卡」。 */
    paintProgress();
  }

  function ratioAt(ev) {
    const b = $("#dlPlaySeek").getBoundingClientRect();
    if (b.width <= 0) return 0;
    return (ev.clientX - b.left) / b.width;
  }

  (function initSeek() {
    const box = $("#dlPlaySeek");
    let drag = false;

    box.addEventListener("pointerdown", (ev) => {
      if (!(Number(audio.duration) > 0)) return;      // 还没加载出时长，忽略
      if (ev.pointerType === "mouse" && ev.button !== 0) return;
      drag = true;
      S.seeking = true;
      box.classList.add("dragging");
      document.body.classList.add("dl-seek-drag");
      try { box.setPointerCapture(ev.pointerId); } catch (e) {}
      seekToRatio(ratioAt(ev));
      ev.preventDefault();
    });
    box.addEventListener("pointermove", (ev) => {
      if (drag) seekToRatio(ratioAt(ev));
    });
    const end = (ev) => {
      if (!drag) return;
      drag = false;
      S.seeking = false;
      box.classList.remove("dragging");
      document.body.classList.remove("dl-seek-drag");
      try { box.releasePointerCapture(ev.pointerId); } catch (e) {}
      paintProgress();
    };
    box.addEventListener("pointerup", end);
    box.addEventListener("pointercancel", end);
    box.addEventListener("lostpointercapture", end);

    /* 键盘：←/→ 5 秒，Home/End 首尾。焦点环已经在 CSS 里给了。 */
    box.addEventListener("keydown", (ev) => {
      const d = Number(audio.duration);
      if (!isFinite(d) || d <= 0) return;
      if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
        audio.currentTime = Math.min(d, Math.max(0,
          audio.currentTime + (ev.key === "ArrowRight" ? 5 : -5)));
        paintProgress();
        ev.preventDefault();
      } else if (ev.key === "Home" || ev.key === "End") {
        audio.currentTime = ev.key === "Home" ? 0 : d;
        paintProgress();
        ev.preventDefault();
      }
    });
  })();

  /* ── 封面（v6.0.1，v6.0.7 改成轮播）────────────────────
     用户要求：「日志栏、试听栏之间开辟单独的一块，用于试听音乐时，
     显示对应游戏的封面图」。

     v6.0.7 又加了一条：「一个游戏的封面资源应该有若干个，下载时全下载，
     然后在试听时轮播」。

     图从哪来：后端 /api/dl/covers?game=xxx 回**张数**，
     /api/dl/cover?game=xxx&i=N 回第 N 张。前端不做任何路径拼接 ——
     保存目录在后端手上，前端拼出来的路径十有八九是错的。

     ⚠ 同一游戏连播几首时**不重置轮播**（记在 S.coverGame 里）：
     一张 400 KB 的图反复拉，切歌会明显卡一下；而且用户正在看第 3 张，
     切到同一游戏的下一首又跳回第 1 张，很烦。

     ⚠ 只在**真的在放**的时候轮播（audio.paused 时跳过）：暂停了画面
     还在跳，会让人以为程序出问题了。 */
  const COVER_EVERY_MS = 3000;   // 3 秒一张（用户 2026-10-28 选的）

  function setCoverImg(game, i, force) {
    const img = $("#dlCoverImg");
    img.dataset.i = String(i);
    img.src = "/api/dl/cover?game=" + encodeURIComponent(game) + "&i=" + i
      + (force ? "&t=" + Date.now() : "");
  }

  async function loadCoverList(game, force) {
    let n = 0;
    try {
      const q = "/api/dl/covers?game=" + encodeURIComponent(game)
        + (force ? "&t=" + Date.now() : "");
      const r = await api(q);
      n = parseInt(r && r.n, 10) || 0;
    } catch (e) { n = 0; }
    if (S.coverGame !== game) return;      // 等待期间又切了歌，作废
    S.coverN = n;
    S.coverIdx = 0;
    if (n > 0) setCoverImg(game, 0, force);
    else $("#dlCoverBox").classList.remove("has");
  }

  function showCover(w) {
    const box = $("#dlCoverBox");
    const img = $("#dlCoverImg");
    const game = (w && w.game) ? String(w.game) : "";
    const nm = w ? String(w.name || "").replace(/\.(wav|flac|mp3|ogg)$/i, "") : "";
    $("#dlCoverGame").textContent = game || "还没有在试听";
    $("#dlCoverTrack").textContent = w
      ? (nm || "正在播放")
      : "点一首曲子，这里显示它所属游戏的封面";
    if (!game) {
      S.coverGame = "";
      S.coverN = 0;
      S.coverIdx = 0;
      box.classList.remove("has");
      img.removeAttribute("src");
      return;
    }
    if (S.coverGame === game && img.getAttribute("src")) return;   // 同一游戏，别重拉
    S.coverGame = game;
    S.coverN = 0;
    S.coverIdx = 0;
    box.classList.remove("has");          /* 先藏旧的，避免上一张图闪一下 */
    loadCoverList(game);
  }

  /* 轮播：3 秒一张，只在播放中、且这个游戏真的有 2 张以上时才转。 */
  setInterval(() => {
    if (!S.coverGame || S.coverN < 2) return;
    if (audio.paused) return;
    S.coverIdx = (S.coverIdx + 1) % S.coverN;
    setCoverImg(S.coverGame, S.coverIdx);
  }, COVER_EVERY_MS);

  (function initCover() {
    const img = $("#dlCoverImg");
    const box = $("#dlCoverBox");
    img.onload = () => box.classList.add("has");
    /* 这张图 404 / 坏了 → 试下一张；全试完才显示音符占位。
       不弹 toast：封面是锦上添花，为它报错只会打扰用户。 */
    img.onerror = () => {
      const i = parseInt(img.dataset.i || "0", 10) || 0;
      if (S.coverGame && S.coverN > 1 && i + 1 < S.coverN) {
        S.coverIdx = i + 1;
        setCoverImg(S.coverGame, S.coverIdx);
        return;
      }
      box.classList.remove("has");
    };

    /* 折叠状态记住：窗口矮的时候用户会收起封面区，下次打开不该又弹回来。 */
    const KEY = "msu1.dl.cover.fold";
    let fold = false;
    try { fold = localStorage.getItem(KEY) === "1"; } catch (e) {}
    box.classList.toggle("fold", fold);
    $("#dlCoverFold").onclick = () => {
      const on = !box.classList.contains("fold");
      box.classList.toggle("fold", on);
      try { localStorage.setItem(KEY, on ? "1" : "0"); } catch (e) {}
    };
  })();

  audio.onplay = () => {
    $("#dlPlayBtn").textContent = "❙❙";
    markPlaying();
    if (viz.on && viz.ensure()) viz.start();
  };
  audio.onpause = () => {
    $("#dlPlayBtn").textContent = "▶";
    markPlaying();
    viz.stop();
  };
  audio.onerror = () => {
    $("#dlPlayBtn").textContent = "▶";
    // 404 / 格式不支持都会到这里。别静默失败 —— 用户点了没反应最难受。
    toast("播放失败：文件可能已被移动或删除，正在刷新列表");
    S.playRel = "";
    S.wavSig = null;
    syncSkipBtns();                    /* v6.0.3：当前曲目没了，上/下一曲置灰 */
    loadWavs(true);
  };
  audio.ontimeupdate = () => {
    /* v6.0.3：拖动期间让位 —— 否则这里每 250ms 把进度条按「真实播放位置」
       重画一次，用户手指拖到哪儿都会被拽回去，看起来就是「拖不动」。 */
    if (S.seeking) return;
    if (!audio.duration) return;
    paintProgress();
  };
  /* 元数据到位 = 知道总时长了：把进度条从 idle 状态放出来，滑块头才跟得上。 */
  audio.onloadedmetadata = () => paintProgress();
  audio.onended = () => {
    S.playRel = "";
    $("#dlPlayBar").style.width = "0%";
    $("#dlPlayKnob").style.left = "0%";
    $("#dlPlayTime").textContent = "0:00";
    markPlaying();
    syncSkipBtns();
    viz.stop();
  };

  // ── 状态渲染 ──────────────────────────────────────────
  /* 当前「默认」输出格式（"WAV" / "FLAC" / "MP3"），给行内那颗按钮和各种文案用。
     v5.9.7：写死「转 WAV」的话，用户选了 FLAC、点那颗按钮，
     按钮说「转 WAV」、产物却是 .flac —— 会以为格式选项没生效。

     v5.9.9：底部两个「下载后自动转」复选框换成了两颗按钮，不再有
     `opts.auto_flac` 这个开关。行内按钮改成**跟随最近一次点的按钮**：
     点了「转 FLAC」之后，行内那颗也变成「转 FLAC」，两处不会互相打脸。
     v5.9.15：多了第三颗「转 MP3」，同一套规则。 */
  /* 文件名 -> 格式词（小写）。认不出就当 wav。 */
  function fmtOfName(n) {
    const m = String(n || "").toLowerCase().match(/\.(wav|flac|mp3|ogg)$/);
    return m ? m[1] : "wav";
  }

  let lastFmt = "wav";
  function fmtTag() {
    return lastFmt.toUpperCase();
  }

  /* 这一行该给哪颗粒按钮（v5.9.4）。
     用户报的两件事都在这里：
       · 「已下载、解压完成但没转 wav 的，没有入口」→ 给「转 WAV」；
       · 「已转成 wav 的，加不进试听列表」→ 给「＋试听」。
     状态串是后端给的（见 dl_api.snapshot），这里只做「认表情」的映射，
     不自己推断磁盘事实 —— 两边各推一套迟早会漂。 */
  function actOf(row) {
    const st = String(row.state || (row.on_disk ? "✓ 已下载" : ""));
    if (/🎵/.test(st)) {
      return { k: "play", t: "＋试听", tip: "把这个游戏转好的音频加入试听列表" };
    }
    if (/解压中|⏳/.test(st)) {
      return { k: "wait", t: "转换中",
               tip: "正在转换，转完这一行会变成「🎵 已转 WAV / FLAC / MP3 / OGG」" };
    }
    if (/📦/.test(st)) {
      return { k: "wav", t: "转 " + fmtTag(),
               tip: "把已解压的音源转成 " + fmtTag() + "（不重新下载）" };
    }
    if (/✓/.test(st)) {
      return { k: "wav", t: "转 " + fmtTag(),
               tip: "先解压再转 " + fmtTag() + "（不重新下载）" };
    }
    if (/⚠/.test(st)) {
      return { k: "wav", t: "重转", tip: "上次没转成功，再试一次" };
    }
    return null;
  }

  /* 行内按钮的点击。两种动作都只发一个 url —— 用户点的是**这一行**，
     别顺手把他勾选的一堆也带上（勾选是给「开始下载」用的）。 */
  async function onAct(kind, url, btn) {
    if (btn) btn.disabled = true;
    try {
      if (kind === "wav") {
        const d = await api("/api/dl/convert_wav",
                            { urls: [url], fmt: lastFmt });
        if (!d.ok) { toast(d.error || ("转 " + fmtTag() + " 失败")); return; }
        toast("开始转 " + fmtTag() + "，进度看右侧日志");
        poll();
      } else if (kind === "play") {
        const d = await api("/api/dl/wav_add", { urls: [url] });
        if (!d.ok) { toast(d.error || "加入试听失败"); return; }
        toast("已加入试听 " + (d.added || 0) + " 首" +
              (d.new === 0 ? "（列表里已经有了）" : ""));
        /* 强制重建：清空过列表时 cut 比这些文件的 mtime 新，
           列表签名可能没变但内容该变了 —— 走 force 最省心。 */
        S.wavSig = null;
        await loadWavs(true);
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function paintState(row) {
    const tr = S.nodeOf[row.url];
    if (!tr) return;
    tr.classList.toggle("on", !!row.sel);
    const st = tr.querySelector(".c-state");
    const txt = row.state || (row.on_disk ? "✓ 已下载" : "");
    if (st.textContent !== txt) st.textContent = txt;
    st.classList.toggle("ok", /✅|✓|🎵|📦/.test(txt));
    st.classList.toggle("bad", /❌|⚠/.test(txt));
    /* 按钮只在「该显示什么」变了时才重建 —— 这个函数每 300ms 跑一遍，
       无脑 innerHTML="" 会把用户正按着的按钮从 DOM 里摘掉（点击丢失）。 */
    const cell = tr.querySelector(".c-act");
    if (cell) {
      const a = actOf(row);
      const sig = a ? a.k + "|" + a.t : "";
      if (cell.dataset.sig !== sig) {
        cell.dataset.sig = sig;
        cell.innerHTML = "";
        if (a) {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "dl-act" + (a.k === "play" ? " primary" : "");
          b.textContent = a.t;
          b.title = a.tip;
          b.disabled = a.k === "wait";
          b.onclick = (ev) => {
            // 整行点击 = 勾选/取消，按钮点击不能顺带把行也切了
            ev.stopPropagation();
            if (b.disabled) return;
            onAct(a.k, row.url, b);
          };
          cell.appendChild(b);
        }
      }
    }
    const zh = tr.querySelector(".c-zh");
    if (zh && row.zh && zh.textContent !== row.zh) {
      zh.textContent = row.zh;
      zh.title = row.zh;
    }
  }

  function paintLogs(logs) {
    if (!logs || !logs.length) return;
    const box = $("#dlLog");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const frag = document.createDocumentFragment();
    logs.forEach((row) => {
      const t = String(row[1] || "");
      const d = document.createElement("div");
      if (/^✅|^🏁|^🎵|^🔗/.test(t)) d.className = "ok";
      else if (/^❌|^⚠/.test(t)) d.className = "bad";
      else if (/^ℹ|^⏭/.test(t)) d.className = "warn";
      d.textContent = t;
      frag.appendChild(d);
    });
    box.appendChild(frag);
    while (box.childElementCount > 1500) box.removeChild(box.firstChild);
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  // ── 主轮询 ────────────────────────────────────────────
  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const d = await api("/api/dl/state?since=" + S.seq);
      if (d.seq) S.seq = d.seq;
      S.snap = d;                       // 上传云盘要读 d.wav_dir
      $("#dlConn").textContent = "已连接";
      $("#dlConn").classList.remove("bad");
      paint(d);
    } catch (e) {
      $("#dlConn").textContent = "已断开";
      $("#dlConn").classList.add("bad");
    } finally {
      polling = false;
    }
  }

  function paint(d) {
    // 机种高亮
    if (d.key && d.key !== S.key) { S.key = d.key; markPlatform(d.key); }

    // 列表：静态签名变了才重建
    const sig = [d.key, d.count, S.filter, (d.rows || []).length].join("|");
    S.rows = d.rows || [];
    if (sig !== S.sig) {
      S.sig = sig;
      rebuildRows();
    }
    // 动态部分逐行更新
    S.rows.forEach(paintState);

    /* 「已下载」这一句是 v5.9.3 加的：状态列现在按磁盘算，但一屏只有几十行，
       用户还是想知道「这个机种我下了多少」。0 条时不显示，免得占地方。
       title 里写清楚**是按哪个目录扫出来的** —— 用户把保存目录改成别的路径后，
       状态列没变化时能自己看出「扫的是不是我以为的那个目录」。 */
    $("#dlCount").textContent =
      "共 " + (d.count || 0) + " 条，已选 " + (d.sel || 0) + " 条" +
      (d.n_on_disk ? "，已下载 " + d.n_on_disk + " 条" : "");
    if (d.scan && d.scan.dir) {
      $("#dlCount").title =
        "状态按保存目录扫描：" + d.scan.dir +
        "\n扫到 " + (d.scan.files || 0) + " 个文件 / " +
        (d.scan.dirs || 0) + " 个目录 / " +
        (d.scan.wav || 0) + " 个已转音频的游戏" +
        (d.scan.limited ? "\n⚠ 目录太大，只扫了一部分（可在保存目录里再选细一点）" : "");
    }

    // 进度 / 状态
    const pct = Math.max(0, Math.min(100, d.progress || 0));
    $("#dlBar").style.width = pct.toFixed(1) + "%";
    /* v5.9.14：封面和下载/转码是**并行的三条线**，状态栏只有一行，
       所以把封面的进度拼在后面。不拼的话用户在「下载已完成」之后
       还会看到日志在滚，会以为程序卡住了。 */
    let stText = d.status || "";
    const cst = d.stats || {};
    if (cst.cover_total) {
      const cd = cst.cover_done || 0, ct = cst.cover_total;
      let piece = "🖼 封面 " + cd + "/" + ct;
      if (cd >= ct) piece += "（成功 " + (cst.cover_ok || 0) + "）";
      else if (cst.cover_current) piece += "（" + cst.cover_current + "）";
      stText += (stText ? "  ·  " : "") + piece;
    }
    /* v6.0.8：更新全部列表是**第三条**并行线（和下载/转码、封面并列）。
       状态栏只有一行，同样拼在后面；跑完的那一下弹个 toast ——
       用户可能早就切去干别的了，不给个结论他不知道好了没有。 */
    const rf = d.refresh || {};
    const rBtn = $("#dlRefreshAll");
    if (rf.running) {
      const rp = "🔄 更新列表 " + (rf.done || 0) + "/" + (rf.total || 0) +
        (rf.name ? " · " + rf.name : "");
      stText += (stText ? "  ·  " : "") + rp;
      rBtn.textContent = "停止更新 " + (rf.done || 0) + "/" + (rf.total || 0);
      rBtn.classList.add("on");
      rBtn.title = "正在更新" + (rf.name ? "（" + rf.name + "）" : "") +
        "…点一下停止；已扫完的机种会保留";
    } else {
      rBtn.textContent = "更新全部列表";
      rBtn.classList.remove("on");
      rBtn.title = "把所有机种的列表重新扫一遍（不用逐个机种点「清除缓存」）";
      if (S.refreshing) {
        toast("全部列表已更新：" + (rf.ok || 0) + " 个成功"
              + (rf.fail ? " / " + rf.fail + " 个失败（看日志）" : ""), 4000);
      }
    }
    S.refreshing = !!rf.running;
    $("#dlStatus").textContent = stText;

    // 忙碌态
    S.busy = !!d.busy || !!d.scanning;
    $("#dlStart").disabled = S.busy;
    $("#dlStop").disabled = !S.busy;
    $("#dlStart").textContent = d.busy ? "进行中…" : "开始下载";

    // 选项回填（只在没被用户改动时同步，避免打架）
    const o = d.opts || {};
    if (document.activeElement !== $("#dlDest")) $("#dlDest").value = d.dest || "";
    if (document.activeElement !== $("#dlWorkers")) $("#dlWorkers").value = o.workers || 2;
    $("#dlAutoExtract").checked = !!o.auto_extract;
    $("#dlDelArchive").checked = !!o.del_archive;
    $("#dlMerge").checked = !!o.merge_wav;
    $("#dlDelArchive").disabled = !o.auto_extract;
    /* v5.9.9：转换按钮 —— 忙 / 一条都没勾时禁用。
       注意这里**不看** o.auto_wav / o.auto_flac：那两个开关已经删了，
       转换改成手动触发，跟「下载」完全解耦。
       v5.9.15：三颗（WAV / FLAC / MP3）；v6.0.0：四颗（加 OGG）；
       v6.0.4：五颗（加「下载封面」）。
       ⚠ v6.0.0~v6.0.3 这里漏了 ``#dlConvOgg`` —— 它 HTML 里带 disabled，
         没人把它打开过，等于「转 OGG」一直是灰的。v6.0.4 一并补上。 */
    const nSel = (S.rows || []).filter((r) => r.sel).length;
    ["#dlConvWav", "#dlConvFlac", "#dlConvMp3", "#dlConvOgg", "#dlCoverFetch"]
      .forEach((s) => {
        const b = $(s);
        if (b) b.disabled = S.busy || !nSel;
      });

    // 云上传
    const c = d.cloud || {};
    $("#dlCloud").disabled = !!c.running;
    $("#dlCloud").textContent = c.running
      ? "上传中 " + (c.progress || 0) + "%" : "上传云盘";
    $("#dlCloudNote").textContent = c.error ? c.error : "";

    paintLogs(d.logs);

    /* 试听列表：转换中每 2 秒重扫一次（刚转完的立刻能放），
       空闲时只在第一次进页面扫一遍 —— 300ms 轮询每次都扫目录纯属浪费。
       ⚠ 切机种会改保存目录（WAV 根目录跟着变），必须立刻重扫：
         不重扫的话列表还是上一个机种的，点播放直接 404（实测踩过）。 */
    const ph = d.phase || "";
    if (d.dest !== S.wavDest) {
      S.wavDest = d.dest || "";
      S.wavSig = null;
      S.playRel = "";
      loadWavs(true);
    } else {
      const wantScan = (ph === "converting" || ph === "done" || S.busy);
      if ((wantScan || !S.wavAt) && Date.now() - S.wavAt > 2000) loadWavs(false);
    }
  }

  // ── 事件绑定 ──────────────────────────────────────────
  function bind() {
    $("#dlTheme").onclick = () => {
      // 只在这两套现代主题之间跳；复古主题点一下回深色
      applyTheme(ThemeKit.now() === "dark" ? "light" : "dark");
    };

    let ft = null;
    $("#dlSearch").oninput = (e) => {
      S.filter = e.target.value;
      clearTimeout(ft);
      ft = setTimeout(() => { S.sig = ""; poll(); }, 180);
    };
    $("#dlSearchClear").onclick = () => {
      $("#dlSearch").value = "";
      S.filter = "";
      S.sig = "";
      poll();
    };

    /* 虚拟滚动：滚动 / 缩放都要重算窗口。
       滚动用 rAF 合并 —— 滚一次会连发几十个 scroll 事件，逐个重排必掉帧。 */
    let sraf = 0;
    $("#dlRows").onscroll = () => {
      if (sraf) return;
      sraf = requestAnimationFrame(() => { sraf = 0; renderWindow(false); });
    };
    window.addEventListener("resize", () => renderWindow(true));

    // 机种分组的「全开 / 全收」（v5.9）
    const grpAllBtn = $("#dlGrpAll");
    if (grpAllBtn) grpAllBtn.onclick = () => toggleGrpAll();

    const sel = async (mode) => {
      await api("/api/dl/select", { mode: mode });
      poll();
    };
    $("#dlAll").onclick = () => sel("all");
    $("#dlInvert").onclick = () => sel("invert");
    $("#dlNone").onclick = () => sel("none");

    $("#dlZh").onclick = async () => {
      const d = await api("/api/dl/zh", {});
      if (!d.ok) toast(d.error || "无法开始翻译");
      else toast("正在翻译中文名…");
      poll();
    };

    $("#dlClearCache").onclick = async () => {
      if (!S.key) { toast("先选机种"); return; }
      const d = await api("/api/dl/clear_cache", { key: S.key });
      if (!d.ok) { toast(d.error || "清除失败"); return; }
      toast("已清除缓存，正在重扫…");
      await api("/api/dl/load", { key: S.key, force: true });
      poll();
    };

    /* v6.0.8：「更新全部列表」。一颗键跑完所有机种 —— 原来要挨个机种点
       「清除缓存」，33 个机种 33 遍。
       跑起来之后同一颗键变成「停止更新 12/33」：5 分钟的等待里没个出口
       不合适，而多摆一颗「停止」键又会让这一排更挤。
       ⚠ 状态以**后端**为准（d.refresh.running），不靠前端自己记 ——
         否则在另一个窗口点过、或者页面刷新过，按钮状态就错。 */
    $("#dlRefreshAll").onclick = async () => {
      const rf = (S.snap && S.snap.refresh) || {};
      if (rf.running) {
        const d = await api("/api/dl/refresh_cancel", {});
        if (!d.ok) toast(d.error || "停不下来");
        else toast("正在停止…（已扫完的机种保留）");
        poll();
        return;
      }
      const d = await api("/api/dl/refresh_all", {});
      if (!d.ok) { toast(d.error || "无法开始更新"); return; }
      toast("开始更新全部列表：" + (d.total || 0) + " 个机种，可以继续做别的", 3600);
      poll();
    };

    /* 改保存目录：**扫描以这里填的路径为准**，所以改完必须给一句明确反馈 ——
       目录填错（不存在 / 填成别的盘）时状态列会一片空白，用户会以为功能坏了。
       这句 toast 直接把「按哪个目录扫的、扫到多少」说清楚。 */
    function destToast(d) {
      if (!d || !d.dest) return;
      const sc = d.scan || {};
      if (!d.dest_exists) {
        toast("已设为 " + d.dest + "（目录还不存在，第一次下载时会自动建）", 3600);
      } else {
        toast("状态已按 " + d.dest + " 重扫："
              + (sc.files || 0) + " 个文件 / " + (sc.wav || 0) + " 个已转音频"
              + (sc.limited ? "（目录太大只扫了一部分）" : ""), 3200);
      }
    }

    $("#dlPickDest").onclick = async () => {
      const d = await api("/api/dl/pick_dest", {});
      if (d.path) {
        $("#dlDest").value = d.path;
        const r2 = await api("/api/dl/set_opts", { dest: d.path });
        destToast(r2);
        poll();
      }
    };

    $("#dlOpenDir").onclick = async () => {
      const d = await api("/api/dl/open_dir", {});
      if (!d.ok) toast(d.error || "打不开目录");
      // 没转过 WAV 时后端会退到保存目录 / 上级目录 —— 说一句，
      // 免得用户以为「打开的目录不对」。
      else if (d.note) toast(d.note, 3600);
    };

    $("#dlDest").onchange = async () => {
      const v = $("#dlDest").value.trim();
      const r = await api("/api/dl/set_opts", { dest: v });
      destToast(r);
      poll();
    };

    const syncOpts = async () => {
      await api("/api/dl/set_opts", {
        workers: parseInt($("#dlWorkers").value, 10) || 2,
        auto_extract: $("#dlAutoExtract").checked,
        del_archive: $("#dlDelArchive").checked,
        merge_wav: $("#dlMerge").checked,
      });
      poll();
    };
    ["#dlWorkers", "#dlAutoExtract", "#dlDelArchive", "#dlMerge"]
      .forEach((s) => { $(s).onchange = syncOpts; });

    /* v5.9.9：底部「转 WAV / 转 FLAC」（v5.9.15 再加「转 MP3」）。
       原来这里是两个「下载后自动转」复选框 —— 它们只能表达「下载时顺手
       转一种」，而「已经下完的补转」「两种格式都要」都表达不了，所以
       改成按钮：对**当前勾选的条目**转换，不重新下载。
       各点一次 = 都要（产物分别在 WAV/、FLAC/、MP3/ 三个目录）。 */
    const CONV_BTNS = {
      wav: "#dlConvWav",
      flac: "#dlConvFlac",
      mp3: "#dlConvMp3",
      ogg: "#dlConvOgg",
    };
    const convSelected = async (fmt) => {
      /* 选中状态在**后端**（每行的 row.sel），前端不另存一份集合 ——
         两处各记一套迟早会漂。 */
      const urls = (S.rows || []).filter((r) => r.sel).map((r) => r.url);
      if (!urls.length) { toast("先在列表里勾选要转的游戏"); return; }
      const btn = $(CONV_BTNS[fmt] || CONV_BTNS.wav);
      const tag = fmt.toUpperCase();
      if (btn) btn.disabled = true;
      try {
        const d = await api("/api/dl/convert_wav", { urls: urls, fmt: fmt });
        if (!d.ok) { toast(d.error || ("转 " + tag + " 失败")); return; }
        lastFmt = fmt;                    /* 行内按钮跟着用这个格式 */
        toast("开始转 " + tag + "（" + (d.count || urls.length) + " 个游戏）"
              + "，进度看右侧日志");
        poll();
      } finally {
        if (btn) btn.disabled = false;
      }
    };
    $("#dlConvWav").onclick = () => convSelected("wav");
    $("#dlConvFlac").onclick = () => convSelected("flac");
    if ($("#dlConvMp3")) $("#dlConvMp3").onclick = () => convSelected("mp3");
    if ($("#dlConvOgg")) $("#dlConvOgg").onclick = () => convSelected("ogg");

    /* v6.0.4：手动「下载封面」（原「顺带下载封面」复选框）。
       用户要求：「改成下载封面按钮，可主动选择什么时候下载封面」。

       和「转 WAV / FLAC / MP3」一样作用于**勾选的条目**，但语义不同：
       它不改音频，只抓图 —— 抓回来的图进封面缓存，并复制一份
       cover.png 到已有的 <格式>/<游戏>/ 目录；已经转好的 FLAC / MP3
       由后端顺带补图（``_backfill_audio``，绕开转换记账）。 */
    const coverSelected = async () => {
      const urls = (S.rows || []).filter((r) => r.sel).map((r) => r.url);
      if (!urls.length) { toast("先在列表里勾选要抓封面的游戏"); return; }
      const btn = $("#dlCoverFetch");
      if (btn) btn.disabled = true;
      try {
        const d = await api("/api/dl/fetch_covers", { urls: urls });
        if (!d.ok) { toast(d.error || "抓封面失败", 4600); return; }
        toast("开始抓封面（" + (d.count || urls.length) + " 个游戏），进度看右侧日志");
        /* 正在试听的那首所属游戏可能刚拿到图（而且是好几张）—— 重拉列表。
           ⚠ 必须带时间戳：/api/dl/cover 有 60 秒 Cache-Control，刚才那次
             404 还躺在浏览器缓存里，不带 t= 的话重拉还是「没有封面」。 */
        if (S.coverGame) {
          S.coverN = 0;
          S.coverIdx = 0;
          loadCoverList(S.coverGame, true);
        }
        poll();
      } finally {
        if (btn) btn.disabled = false;
      }
    };
    if ($("#dlCoverFetch")) $("#dlCoverFetch").onclick = coverSelected;

    $("#dlStart").onclick = async () => {
      const d = await api("/api/dl/start", {});
      if (!d.ok) toast(d.error || "无法开始");
      else if (!d.todo) toast("没有需要下载的文件（" + (d.skipped || 0) + " 个已存在）");
      poll();
    };

    $("#dlStop").onclick = async () => {
      await api("/api/dl/stop", {});
      toast("正在停止…");
      poll();
    };

    /* 上传云盘：整套流程（列清单 → 选范围 → 确认 → 没登录就弹登录窗 → 上传）
       都在 cloud.js 里，与主界面共用同一份。这里多给两样东西：
         · dir  = WAV 根目录（下载页的在 <dest>/WAV，跟主界面的输出目录不同）；
         · games= 已转好的游戏清单 → 用户先挑「这次传哪几个」（v5.9.4）。
       ⚠ 以前这里传的是 ``S.snap.wav_dir``，而它是**后端 start() 时冻结的**
         值；程序重启后为空 → 退回 <dest>/WAV，dest 被改过就扫错目录。
         现在由后端 _wav_dir_now() 自己算，这里只传「后端给的目录」作参考。 */
    $("#dlCloud").onclick = async () => {
      $("#dlCloud").disabled = true;
      try {
        const g = await api("/api/dl/wav_games");
        if (!g.ok || !(g.games || []).length) {
          toast(g.error || "还没有转好的 WAV —— 先给某个游戏点「转 WAV」");
          return;
        }
        await CloudUI.upload({
          endpoint: "/api/dl/upload_cloud",
          dir: g.dir || (S.snap && S.snap.wav_dir) || "",
          games: g.games,
        });
      } finally {
        $("#dlCloud").disabled = false;
        poll();
      }
    };

    $("#dlLogClear").onclick = () => { $("#dlLog").innerHTML = ""; };

    // 试听：播放/暂停那颗按钮只作用于「当前选中的那首」
    $("#dlPlayBtn").onclick = () => {
      if (!S.playRel) return;
      if (audio.paused) audio.play().catch(() => {});
      else audio.pause();
    };
    /* v6.0.3：上一曲 / 下一曲 */
    $("#dlPrev").onclick = () => stepTrack(-1);
    $("#dlNext").onclick = () => stepTrack(1);
    $("#dlPlayRefresh").onclick = () => {
      S.wavSig = null;               // 强制重建列表
      loadWavs(true);
    };

    /* 清空试听列表（v5.9.2）。
       ⚠ 后端**只记一个时刻、不删文件**：这张列表是「刚转出来的 WAV」的入口，
       WAV 本身就是用户要拿去用的产物（上传云盘、拖进播放器、压 MSU-1 包）。
       点一下「清空」就删掉几百 MB 是不可逆的，而且「清空列表」也没这个意思。
       所以这里只负责：停掉正在放的那首 → 让列表重扫 → 说清「文件没删」。 */
    $("#dlPlayClear").onclick = async () => {
      const d = await api("/api/dl/wav_clear", {});
      if (!d.ok) { toast(d.error || "清空失败"); return; }
      /* 正在放的那首被清掉了 —— 停下来。不停的话播放条会对着一个
         列表里已经不存在的曲目继续走，看着像卡住。 */
      audio.pause();
      S.playRel = "";
      $("#dlPlayBar").style.width = "0%";
      $("#dlPlayKnob").style.left = "0%";
      $("#dlPlayTime").textContent = "0:00";
      $("#dlPlayName").textContent = "还没有转出的音频";
      $("#dlPlayBtn").disabled = true;
      syncSkipBtns();                // v6.0.3：没当前曲目了，上/下一曲跟着灰
      S.wavSig = null;               // 强制重建（否则签名没变会直接 return）
      await loadWavs(true);
      toast(d.hidden
        ? ("已清空 " + d.hidden + " 首（WAV 文件没删，用「打开文件夹」还能找到）")
        : "列表已经是空的", d.hidden ? 3600 : 2200);
    };
  }

  // ── 列宽可拖拽（v6.0.1 起，v6.0.10 改成「每列独立」）────────
  /* 用户要求：「游戏列表各列尺寸要求可以任意调整」。

     ⚠ **v6.0.10 改了分配模型**，是用户在两个方案里明确选的：
       拖宽某一列时，**其它列一概不动** —— 宽度和位置都不变，表格整体
       向右延伸，超出窗口就出横向滚动条（`.dl-rows` 自己滚，表头靠
       `translateX` 跟着走，不然滚完表头和内容会错位）。

     之前（v6.0.1 ~ v6.0.9）是「总宽锁死、有人让位」，让位顺序是
     「名称列优先、然后中文名」。名称列和中文名各有 60px 余量，
     于是永远是这两列在互相抢，日期/大小/状态一动不动 ——
     用户报的「日期、大小、状态列还是不动，中文名列被压缩」就是这么来的。

     ⚠ **v6.0.9 修过另一个 bug**：用户报「只有英文名称列可以调整」。
     实测（wrap.clientWidth = 648，可用宽 568）：6 个非名称列的默认值
     加起来就要 478，名称列被夹到下限 120 → 总宽 598 **溢出 30px**；
     名称列已经贴死下限，于是谁都没余量、一格都拖不动。
     更早的根因是 `initCols()` 在**布局完成前**量宽度（读到 0）。
     所以「默认值必须装得下」和「等布局就绪再算」这两条**保留**，
     变的只是拖动时不再互相借。 */
  const COL_KEY = "msu1.dl.cols2";   /* v2：分配模型变了，旧存档作废 */
  const COL_ORDER = ["sel", "name", "zh", "date", "size", "state", "act"];
  const COL_MIN = { sel: 26, name: 120, zh: 60, date: 64, size: 56,
                    state: 70, act: 52 };
  const COL_DEF = { sel: 26, name: 0, zh: 150, date: 84, size: 70,
                    state: 96, act: 52 };
  const COL_GAP = 8;        /* 与 download.css 的 gap 一致 */
  const COL_PAD = 24;       /* 与 .dl-thead 的 padding: 0 12px 一致 */

  let cols = null;          /* {sel,name,...}，单位 px；null = 还没初始化 */
  /* 用户动过列宽没有。没动过时 resize 重算默认值（名称列重新吃满）；
     动过就**一律不动** —— 每列独立，用户定的宽度不该被窗口变化改掉。 */
  let colsTouched = false;

  function colWrap() { return document.querySelector(".dl-table-wrap"); }

  /* 可用宽度 = 容器宽 − 左右内边距 − 7 个间隙（8 条轨道之间） */
  function colAvail() {
    const w = colWrap();
    return Math.max(240, (w ? w.clientWidth : 600) - COL_PAD - COL_GAP * 7);
  }

  /* 布局算完了没有。boot() 里跑的时候 clientWidth 常常还是 0 ——
     那时候算出来的「可用宽度」是假的，名称列会被钉在下限。 */
  function colLaidOut() {
    const w = colWrap();
    return !!(w && w.clientWidth > 200);
  }

  function colTotal(c) {
    return COL_ORDER.reduce((s, k) => s + (c[k] || 0), 0);
  }

  function colApply() {
    const w = colWrap();
    if (!w || !cols) return;
    w.style.setProperty("--dl-cols",
      COL_ORDER.map((k) => Math.round(cols[k]) + "px").join(" "));
    /* 列宽合计超过容器时把表格撑开 —— 横向滚动条才会出来。
       + 7 个间隙 + 左右内边距（全局 box-sizing: border-box，算进 min-width）。 */
    w.style.setProperty("--dl-w",
      Math.round(colTotal(cols) + COL_GAP * 7 + COL_PAD) + "px");
    /* 列宽一改，横向可滚动的余量跟着变；scrollLeft 可能被浏览器夹回 0，
       表头要立刻跟上，别等下一次 scroll 事件。 */
    colSyncHead();
  }

  function colSave() {
    try { localStorage.setItem(COL_KEY, JSON.stringify(cols)); } catch (e) {}
  }

  /* 默认：名称列吃掉剩下的全部宽度（和改造前的视觉一致）。
     装不下就匀 —— 默认 1280 窗口下可用宽只有 ~568，6 个非名称列的默认值
     就要 478，名称列连 120 的下限都不够。不匀的话一打开就溢出 30px，
     而且整排都拖不动（v6.0.9 的 bug）。 */
  function colDefaults() {
    const c = Object.assign({}, COL_DEF);
    const avail = colAvail();
    const others = COL_ORDER.filter((k) => k !== "name")
      .reduce((s, k) => s + c[k], 0);
    c.name = avail - others;
    if (c.name >= COL_MIN.name) return c;
    c.name = COL_MIN.name;
    let need = colTotal(c) - avail;
    const donors = COL_ORDER.filter((k) => k !== "name")
      .sort((a, b) => (c[b] - COL_MIN[b]) - (c[a] - COL_MIN[a]));
    for (const k of donors) {
      if (need <= 0) break;
      const take = Math.min(Math.max(0, c[k] - COL_MIN[k]), need);
      c[k] -= take;
      need -= take;
    }
    return c;
  }

  function colLoad() {
    let c = null;
    try {
      const raw = localStorage.getItem(COL_KEY);
      if (raw) c = JSON.parse(raw);
    } catch (e) { c = null; }
    if (!c || typeof c !== "object") return colDefaults();
    const d = colDefaults();
    COL_ORDER.forEach((k) => {
      const v = Number(c[k]);
      c[k] = (isFinite(v) && v >= COL_MIN[k]) ? Math.round(v) : d[k];
    });
    colsTouched = true;      /* 有存档 = 用户动过，别拿默认值覆盖他 */
    return c;
  }

  function colDrag(e, key) {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const start = cols[key];
    const grip = e.currentTarget;
    grip.classList.add("on");
    document.body.classList.add("dl-col-drag");

    const move = (ev) => {
      /* 只改这一列。不向任何人借、也不让任何人让 —— 差额交给横向滚动条。 */
      cols[key] = Math.max(COL_MIN[key], Math.round(start + ev.clientX - startX));
      colApply();
    };
    const up = () => {
      grip.classList.remove("on");
      document.body.classList.remove("dl-col-drag");
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      colsTouched = true;
      colSave();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  /* 表头不在滚动容器里（`.dl-rows` 只装行），横向滚动时靠 translateX 跟着走，
     否则拖动横向滚动条之后表头和内容就错位了。 */
  function colSyncHead() {
    const box = document.getElementById("dlRows");
    const head = document.querySelector(".dl-thead");
    if (!box || !head) return;
    head.style.transform = "translateX(" + (-box.scrollLeft) + "px)";
  }

  function initCols() {
    cols = colLoad();
    colApply();
    /* 把手挂在表头各列右边界。**不挂行里** —— 每一行都挂一层命中区，
       点行选中会时灵时不灵。
       ⚠ 复选框列（c-sel）不挂：26px 的勾选列没有加宽的必要，挂了反而
         和「点一下全选」抢命中区。**操作列（c-act）要挂** —— 用户说的
         「其他几列无法调整」里就包括它，按钮列宽窄也得能自己定。
         （两个都是空 span，高度靠 CSS 的 ``.dl-thead > span{min-height}``
         撑起来，否则把手是 0 高、点不中。） */
    const head = document.querySelector(".dl-thead");
    if (!head) return;
    [["c-name", "name"], ["c-zh", "zh"], ["c-date", "date"],
     ["c-size", "size"], ["c-state", "state"], ["c-act", "act"]]
      .forEach(([cls, key]) => {
        const cell = head.querySelector("." + cls);
        if (!cell || cell.querySelector(".c-grip")) return;
        const g = document.createElement("i");
        g.className = "c-grip";
        g.dataset.col = key;
        g.title = "拖动调整列宽（双击恢复默认）";
        g.onmousedown = (e) => colDrag(e, key);
        g.ondblclick = (e) => {
          e.preventDefault();
          e.stopPropagation();
          cols = colDefaults();
          colsTouched = false;
          colApply();
          colSave();
          toast("列宽已恢复默认");
        };
        cell.appendChild(g);
      });

    /* 布局没算完时量到的宽度是假的（boot() 里 clientWidth 常是 0）——
       名称列会被钉在下限，之后整排都拖不动。等两帧再校一次。
       只在用户没动过列宽时重算，别覆盖他自己调的值。 */
    const refit = () => {
      if (colsTouched || !colLaidOut()) return;
      cols = colDefaults();
      colApply();
    };
    if (!colsTouched) {
      requestAnimationFrame(() => requestAnimationFrame(refit));
    }

    /* 横向滚动时表头跟着走（见 colSyncHead 的说明） */
    const box = document.getElementById("dlRows");
    if (box) box.addEventListener("scroll", colSyncHead, { passive: true });

    /* 窗口变了：没动过就重算默认（名称列重新吃满），动过就**一律不动** ——
       每列独立之后，用户定的宽度跟窗口大小无关，改它反而是 bug。 */
    let t = 0;
    window.addEventListener("resize", () => {
      clearTimeout(t);
      t = setTimeout(() => {
        if (!cols || !colLaidOut() || colsTouched) return;
        cols = colDefaults();
        colApply();
      }, 180);
    });
  }

  // ── 启动 ──────────────────────────────────────────────
  async function boot() {
    viz.init();          /* 频谱开关 + 空态；AudioContext 留到第一次播放再建 */
    initCols();          /* v6.0.1：列宽拖拽把手 */
    bind();
    await loadPlatforms();
    poll();
    setInterval(poll, 300);
  }

  boot();
})();
