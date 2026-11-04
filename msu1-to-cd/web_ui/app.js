/* ═══════════════════════════════════════════════════════════
   RetroMusic Lab · Web UI
   后端是 web_ui/server.py（同源，无需跨域）
   ═══════════════════════════════════════════════════════════ */

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const ICONS = {
  disc: `<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8.4" stroke="currentColor" stroke-width="1.6"/><circle cx="12" cy="12" r="2.4" stroke="currentColor" stroke-width="1.6"/></svg>`,
  chip: `<svg viewBox="0 0 24 24" fill="none"><rect x="6.5" y="6.5" width="11" height="11" rx="2" stroke="currentColor" stroke-width="1.6"/><path d="M10 3.5v3M14 3.5v3M10 17.5v3M14 17.5v3M3.5 10h3M3.5 14h3M17.5 10h3M17.5 14h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`,
  play: `<svg class="play-ico" viewBox="0 0 24 24" fill="currentColor"><path d="M8.5 5.6v12.8L18.6 12Z"/></svg>`,
  pause: `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="8" y="5.5" width="3" height="13" rx="1"/><rect x="13" y="5.5" width="3" height="13" rx="1"/></svg>`,
};

/* ---------- 全局状态（镜像后端 /api/state） ---------- */
const S = {
  state: "idle",
  progress: 0,
  status: "",
  doneTracks: -1,      /* 已写盘的曲目数（后端给；-1 = 老后端，退回按进度反推） */
  error: "",
  result: null,
  sources: [],
  outputs: [],
  tracks: [],          /* 当前选中音源的曲目清单 */
  tracksFor: -1,       /* 上面这份清单属于第几个音源（-1 = 还没拉） */
  tracksNote: "",
  opts: {},
  upload: { running: false, progress: 0, ok: 0, skip: 0, fail: 0, error: "" },
  seq: 0,
  sel: 0,
  /* 多选集合（存**索引**）。Ctrl / Shift + 点击往里加，
     普通点击清空 —— 和 `sel`（当前查看哪一个）是两件事。 */
  checked: new Set(),
  tab: "sources",
  playingId: 0,
  /* 用户亲手选的输出目录。**不能**从 #pathText 读 —— 那里的占位提示文字
     一旦被当成路径发给后端，后端就会在磁盘上建一个名叫
     「（默认：音源同级的 output 目录）」的目录。 */
  outDir: "",
  lastSig: {},
  wasRunning: false,   /* 用来在「转换刚结束」那一刻刷新 PSF 的 BIOS 状态 */
};

const OUT_PLACEHOLDER = "（默认：音源同级的 output 目录）";

/* ---------- 输出容器（v6.0.0 起四种） ----------
   ⚠ 这两个常量必须在**模块级**：以前 AUDIO_FMTS 是写在点击处理函数里的
   局部变量，collectOpts() 里想用就得再抄一份 —— 抄的那份漏了 OGG，
   表现是「界面选了 OGG，后端收到的 fmt 却是 wav」。
   现在只有这一处定义，改格式只改这里。 */
const AUDIO_FMTS = ["wav", "flac", "mp3", "ogg"];

/*: 四者理论上互斥（前端保证 + 后端兜底），但「万一同时为真」时按这个
   优先级取一个，保证结果**确定**、不随 DOM 顺序漂移。
   与后端 dl_api._opts_fmt() 的口径一致：越靠后的有损格式优先。 */
const FMT_PRIORITY = ["ogg", "mp3", "flac", "wav"];

/* ---------- API ---------- */
async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.ok === false) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}

/* ---------- 主题 ----------
   真正的切换在 theme.js 里（两页共用）。这里只负责「按钮高亮 + 重绘波形」。
   ⚠ 复古主题（retro:xxx）没有对应的图标按钮，高亮给调色板那颗。 */
function setTheme(t) {
  ThemeKit.apply(t);
  paintThemeBtns();
  VIZ.invalidateColors();   /* 让频谱下一帧按新主题取色 */
}

/* ---------- 主题面板（6 套复古配色）----------
   tkinter / Qt 版一直有这 6 套，Web 版原来只有深/浅两套。配色来自
   retro_palette.THEMES，后端翻成 CSS 变量。
   面板本身造在 theme.js 的 `ThemeKit.mountPanel()` 里 —— 下载页也要用同一套，
   复制一份的话两边迟早长得不一样。这里只管按钮高亮。 */
function buildThemePanel() {
  const pop = ThemeKit.mountPanel("#themeSeg");
  if (!pop) return;
  // 选完之后把「调色板那颗」也点亮，否则复古配色开着、按钮却还是深色亮着
  pop.addEventListener("click", () => setTimeout(paintThemeBtns, 0));
  paintThemeBtns();
}

function paintThemeBtns() {
  const cur = ThemeKit.now();
  const isRetro = String(cur).startsWith("retro:");
  $$("#themeSeg button").forEach((b) => {
    if (b.dataset.themeBtn === "retro") b.classList.toggle("on", isRetro);
    else b.classList.toggle("on", !isRetro && b.dataset.themeBtn === cur);
  });
}


/* ---------- 格式化 ---------- */
function fmtDur(secs) {
  secs = Math.max(0, Math.round(Number(secs) || 0));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  return h ? h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0")
           : m + ":" + String(s).padStart(2, "0");
}

function fmtSize(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- 左栏：音源 ---------- */

/* 「删除选中 / 清空」两个按钮的可用态 + 计数。
   多选时强制显示整组按钮（.active），否则左栏那么窄、按钮又是 hover
   才出现的，用户根本发现不了「还能批量删」。 */
function syncRailActs() {
  const n = S.checked.size;
  const del = $("#btnDelSel");
  const clr = $("#btnClearSrc");
  if (del) {
    del.disabled = n === 0;
    del.textContent = n ? `删除选中 (${n})` : "删除选中";
  }
  if (clr) clr.disabled = !S.sources.length;
  const acts = document.querySelector(".rail-acts");
  if (acts) acts.classList.toggle("active", n > 0);
}

function renderSources() {
  const box = $("#srcList");
  const list = S.sources;
  $("#srcCount").textContent = list.length;

  // 清掉已经不存在的 path（删除后 / 重新添加后），否则「删除选中」会带着
  // 幽灵项去删，用户看到计数和实际不符。
  const alive = new Set(list.map((s) => s.path));
  for (const p of Array.from(S.checked)) {
    if (!alive.has(p)) S.checked.delete(p);
  }
  syncRailActs();

  if (!list.length) {
    box.innerHTML = `<div class="empty">
      <span class="empty-ico">${ICONS.disc}</span>
      <span>还没有音源</span>
      <em>点下面的「添加音源」，或把文件 / 文件夹拖进窗口</em>
    </div>`;
    return;
  }

  box.innerHTML = list.map((s, i) => {
    const ico = /SPC|芯片/.test(s.kind) ? ICONS.chip : ICONS.disc;
    const meta = s.n_tracks ? (s.n_tracks + " 轨 · " + fmtDur(s.total_secs)) : s.kind;
    const on = i === S.sel ? " on" : "";
    const ck = S.checked.has(s.path) ? " checked" : "";
    return `<div class="src${on}${ck}" data-i="${i}">
      <span class="src-ico">${ico}</span>
      <div class="src-body">
        <div class="src-name" title="${esc(s.path)}">${esc(s.label)}</div>
        <div class="src-meta">${esc(meta)}</div>
      </div>
      <button class="src-del" data-del="${i}" title="${ck ? "取消选择" : "移除"}">${ck ? "✓" : "×"}</button>
    </div>`;
  }).join("");

  box.querySelectorAll(".src").forEach((el) => {
    el.onclick = (e) => {
      const i = +el.dataset.i;
      const path = S.sources[i] && S.sources[i].path;
      // Ctrl / Cmd / Shift + 点击 = 切换这一项的选中状态（多选）
      if ((e.ctrlKey || e.metaKey || e.shiftKey) && path) {
        e.preventDefault();
        if (S.checked.has(path)) S.checked.delete(path);
        else S.checked.add(path);
        renderSources();
        return;
      }
      if (e.target.dataset.del !== undefined) return;
      S.sel = i;
      S.checked.clear();          // 普通点击 = 退出多选
      renderSources(); renderStage(); renderTable();
      ensureTracks();
    };
  });
  box.querySelectorAll("[data-del]").forEach((el) => {
    el.onclick = async (e) => {
      e.stopPropagation();
      const i = +el.dataset.del;
      const path = S.sources[i] && S.sources[i].path;
      // 已选中的项，点 ✓ 只是取消选择，不真删
      if (path && S.checked.has(path)) {
        S.checked.delete(path);
        renderSources();
        return;
      }
      await api("/api/remove_source", { index: i });
      await tick();
    };
  });
}

/* ---------- 主区标题 ---------- */
function renderStage() {
  if (S.tab === "outputs") {
    $("#stageName").textContent = "输出结果";
    const total = S.outputs.reduce((a, x) => a + (x.size || 0), 0);
    $("#stageSub").innerHTML = `<span class="chip">WAV</span>
      <span>${S.outputs.length} 个文件</span><i class="sep"></i><span>${fmtSize(total)}</span>`;
    return;
  }
  const s = S.sources[S.sel];
  if (!s) {
    $("#stageName").textContent = "还没有音源";
    $("#stageSub").innerHTML = `<span>添加一个音源开始</span>`;
    return;
  }
  $("#stageName").textContent = s.label;
  const nTracks = (S.tracksFor === S.sel && S.tracks.length) ? S.tracks.length : s.n_tracks;
  const bits = [`<span class="chip">${esc(s.kind)}</span>`];
  if (nTracks) bits.push(`<span>${nTracks} 条音轨</span>`);
  if (s.total_secs) { bits.push(`<i class="sep"></i>`, `<span>${fmtDur(s.total_secs)}</span>`); }
  if (s.size) { bits.push(`<i class="sep"></i>`, `<span>${fmtSize(s.size)}</span>`); }
  $("#stageSub").innerHTML = bits.join("");
}

/* ---------- 中间表格 ---------- */
/* 曲目清单按需从 /api/tracks 拉，只探一次（后端有缓存） */
async function ensureTracks() {
  if (S.tab !== "sources") return;
  const idx = S.sel;
  if (S.tracksFor === idx) return;
  S.tracksFor = idx;
  S.tracks = [];
  S.tracksNote = "";
  renderTable();
  try {
    const d = await api("/api/tracks?i=" + idx);
    if (S.tracksFor !== idx) return;      // 期间用户已经切到别的音源了
    S.tracks = d.tracks || [];
    S.tracksNote = d.note || "";
  } catch (e) {
    if (S.tracksFor !== idx) return;
    S.tracks = [];
    S.tracksNote = e.message;
  }
  renderTable();
}

function renderTable() {
  const head = $("#tableHead");
  const body = $("#trackBody");
  const q = ($("#searchBox").value || "").trim().toLowerCase();

  if (S.tab === "outputs") {
    head.innerHTML = `<span class="c-idx">#</span><span class="c-title">文件名</span>
      <span class="c-kind">格式</span><span class="c-dur">大小</span><span class="c-stat">状态</span>`;
    const rows = S.outputs.filter((o) => !q || o.name.toLowerCase().includes(q));
    if (!rows.length) {
      body.innerHTML = `<div class="empty pad">还没有输出。转换完成后这里会列出生成的 WAV，点一行就能试听。</div>`;
      return;
    }
    body.innerHTML = rows.map((o, i) => `
      <div class="tr ${o.id === S.playingId ? "on playing" : ""}" data-oid="${o.id}">
        <span class="c-idx"><span class="num">${String(i + 1).padStart(2, "0")}</span>${ICONS.play}</span>
        <span class="c-title"><span class="bars"><i></i><i></i><i></i></span>
          <span class="tt">${esc(o.name.replace(/\.wav$/i, ""))}</span></span>
        <span class="c-kind">WAV</span>
        <span class="c-dur">${fmtSize(o.size)}</span>
        <span class="c-stat"><span class="stat ok"><i></i>已输出</span></span>
      </div>`).join("");
    body.querySelectorAll("[data-oid]").forEach((el) => {
      el.onclick = () => playOutput(+el.dataset.oid);
    });
    return;
  }

  head.innerHTML = `<span class="c-idx">#</span><span class="c-title">曲目</span>
    <span class="c-kind">类型</span><span class="c-dur">时长</span><span class="c-stat">状态</span>`;
  if (!S.sources.length) {
    body.innerHTML = `<div class="empty pad">点左栏「添加音源」加入文件，或直接把文件拖进窗口。</div>`;
    return;
  }
  if (S.tracksFor !== S.sel) {
    body.innerHTML = `<div class="empty pad">读取曲目…</div>`;
    return;
  }
  const rows = S.tracks.filter((t) => !q || String(t.title).toLowerCase().includes(q));
  if (!rows.length) {
    body.innerHTML = `<div class="empty pad">${esc(
      S.tracksNote || (q ? "没有匹配「" + q + "」的曲目" : "这个音源列不出曲目清单"))}</div>`;
    return;
  }
  /* 转换中的「已输出 / 转换中」。
     首选后端给的 done_tracks（真实写盘数）；老后端没有这个字段时退回按
     进度反推 —— 那个反推在解码阶段会偏大（见 applyState 里的注释）。 */
  const active = S.state === "running";
  const doneN = !active ? -1
    : (S.doneTracks >= 0 ? S.doneTracks
       : Math.floor((S.progress / 100) * S.tracks.length));
  body.innerHTML = rows.map((t) => {
    let st;
    if (!active) st = `<span class="stat ok"><i></i>就绪</span>`;
    else if (t.n - 1 < doneN) st = `<span class="stat ok"><i></i>已输出</span>`;
    else if (t.n - 1 === doneN) st = `<span class="stat wait"><i></i>转换中</span>`;
    else st = `<span class="stat skip"><i></i>等待</span>`;
    return `<div class="tr plain" data-tn="${t.n}">
      <span class="c-idx"><span class="num">${String(t.n).padStart(2, "0")}</span></span>
      <span class="c-title"><span class="tt">${esc(t.title || ("第 " + t.n + " 首"))}</span></span>
      <span class="c-kind">${esc(t.kind || "")}</span>
      <span class="c-dur">${t.secs ? fmtDur(t.secs) : "—"}</span>
      <span class="c-stat">${st}</span>
    </div>`;
  }).join("");
}

/* ---------- 进度 ---------- */
function renderProgress() {
  const running = S.state === "running";
  const strip = $("#progStrip");
  strip.hidden = !running && S.state !== "failed";
  $("#progBar").style.width = Math.max(0, Math.min(100, S.progress)) + "%";
  $("#progText").textContent = S.state === "failed"
    ? ("失败：" + S.error)
    : (S.status || "准备中…");
  strip.classList.toggle("bad", S.state === "failed");

  const btn = $("#btnGo");
  /* v5.4：输出格式默认**一个都不勾**，所以「没选格式」也必须挡在按钮上 ——
     否则点下去后端只会回 400，用户还得自己猜哪里没填。 */
  const fmtOn = $$("#fmtGrid .fmt-card.on").length > 0;
  btn.disabled = running || !S.sources.length || !fmtOn;
  btn.querySelector(".go-label").textContent = running ? "转换中…" : "开始转换";
  btn.querySelector(".go-sub").textContent = running
    ? (S.progress.toFixed(0) + "%")
    : (!S.sources.length ? "先添加音源"
       : (!fmtOn ? "先选输出格式" : (S.sources.length + " 个音源")));

  $("#bCancel").hidden = !running;

  /* 云盘按钮：上传中换成百分比，别让用户连点 */
  const up = S.upload || {};
  const cl = $("#cloudLabel");
  if (cl) cl.textContent = up.running ? ("上传中 " + (up.progress || 0) + "%") : "上传云盘";
  $("#btnCloud").disabled = !!up.running;

  /* 日志抽屉要贴着「当前最下面那条横条」。
     进度条收起来之后还留 26px 空档，会露出一条不该有的缝。 */
  $("#logPanel").style.bottom = strip.hidden ? "86px" : "112px";
}

function appendLogs(rows) {
  if (!rows || !rows.length) return;
  const box = $("#logBox");
  const frag = document.createDocumentFragment();
  for (const row of rows) {
    const text = row[1];
    const d = document.createElement("div");
    d.className = "log-line" + (/✅|══/.test(text) ? " ok" : (/❌|⚠/.test(text) ? " warn" : ""));
    d.textContent = text;
    frag.appendChild(d);
  }
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.appendChild(frag);
  while (box.childElementCount > 2000) box.removeChild(box.firstChild);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

/* ---------- 波形 ---------- */
function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* ---------- 播放（统一走 PlayerCore，方案 §4）----------
   ⚠ 本页不再自建 audio 实例：单例、状态、进度全部由内核持有。
   本段只做两件事：把输出列表接到内核、把内核状态同步回表格高亮。 */
const PL = PlayerCore.init({
  source: PlayerSources.converter({ outputs: () => S.outputs })
});

PL.on("state", (s) => {
  const id = s.id ? Number(String(s.id).slice(3)) : 0;   /* "id:12" -> 12 */
  if (id !== S.playingId) { S.playingId = id; renderTable(); }
  if (s.playing) {
    VIZ.touched = true;
    if (VIZ.on && VIZ.ensure()) VIZ.start();
  } else {
    VIZ.stop();
  }
});

/* 表格里的播放按钮：同一首再点 = 暂停/继续 */
function playOutput(id) {
  const key = "id:" + id;
  if (PL.state.id === key && PL.state.playing) { PL.pause(); return; }
  if (PL.state.id === key) { PL.play(); return; }
  PL.load(key, true);
}

function stepTrack(d) {
  if (!S.outputs.length) return;
  if (d > 0) PL.next(false); else PL.prev();
}

/* ---------- 试听频谱 / 波形（v5.9.6） ----------
   实现全在 web_ui/viz.js 里 —— 下载页也要同一块面板，各写一份迟早不一致
   （theme.js 的注释里已经写过同样的结论）。这里只负责把它接到本页的
   单例 audio 和那几个 DOM 上。
   ⚠ 必须懒建 AudioContext（第一次真播放才建）：Chrome 只在用户手势里
     才肯把 AudioContext 从 suspended 恢复，页面加载时建就是永久平线。 */
const VIZ = VizKit.create({
  audio: PlayerCore.audio,
  canvas: "#vizCanvas",
  wrap: "#vizWrap",
  empty: "#vizEmpty",
  toggle: "#vizTog",
  key: "msu1.viz",
});

/* ---------- 输出目录 ---------- */
function paintOutDir() {
  const el = $("#pathText");
  const shown = S.outDir || S.opts.out_dir || "";
  el.textContent = shown || OUT_PLACEHOLDER;
  el.title = shown || OUT_PLACEHOLDER;
}

/* ---------- 与后端同步 ---------- */
function applyState(d) {
  S.state = d.state;
  S.progress = d.progress;
  S.status = d.status;
  /* 已写盘的曲目数由后端给（ConvertJob 的 on_track），不要拿 progress 反推。
     解码阶段也报进度（0..50%），反推会在还没写任何文件时就说「已输出 32 首」。
     done_tracks 缺失（老后端）时退回反推，至少不会崩。 */
  S.doneTracks = (typeof d.done_tracks === "number")
    ? d.done_tracks : -1;
  S.error = d.error || "";
  S.result = d.result;
  S.sources = d.sources || [];
  S.outputs = d.outputs || [];
  S.opts = d.opts || {};
  S.upload = d.upload || S.upload;
  paintOutDir();
  if (S.sel >= S.sources.length) S.sel = Math.max(0, S.sources.length - 1);

  /* 转换结束时刷新一次 PSF 状态：如果刚才是因为缺 BIOS 失败的，
     右侧那块会从「已就绪」变成黄灯，用户一眼看到该点什么。 */
  if (S.state === "running") {
    S.wasRunning = true;
  } else if (S.wasRunning) {
    S.wasRunning = false;
    paintPsf();
  }

  /* 音源增删会让下标整体移位，缓存的曲目清单必须作废 */
  const pathsSig = S.sources.map((s) => s.path).join("|");
  if (pathsSig !== S.lastSig.paths) {
    S.lastSig.paths = pathsSig;
    S.tracksFor = -1;
  }

  appendLogs(d.logs);

  const sig = JSON.stringify([S.sources.length, S.outputs.length, S.state, S.sel, S.tab]);
  if (sig !== S.lastSig.sig) {
    S.lastSig.sig = sig;
    renderSources(); renderStage(); renderTable();
    ensureTracks();
  }
  /* 转换中：曲目表的「已输出 / 转换中」跟着**真实写盘数**走，
     但不能每 250ms 全表重绘 */
  if (S.state === "running") {
    const dn = S.doneTracks >= 0 ? S.doneTracks
      : Math.floor((S.progress / 100) * S.tracks.length);
    if (dn !== S.lastSig.doneN) {
      S.lastSig.doneN = dn;
      renderTable();
    }
  }
  renderProgress();

  // 转换一结束就切到「输出结果」，用户马上能听
  if (d.state === "done" && S.tab === "sources" && S.outputs.length) {
    S.tab = "outputs";
    S.lastSig.sig = "";
    renderStage(); renderTable();
  }
}

async function tick() {
  try {
    const d = await api("/api/state?since=" + S.seq);
    S.seq = d.seq;
    $("#conn").textContent = "已连接";
    $("#conn").classList.remove("bad");
    applyState(d);
  } catch (e) {
    $("#conn").textContent = "已断开";
    $("#conn").classList.add("bad");
  }
}

/* ---------- PS1 音源（.psf）的 BIOS 状态 ----------
   PSF 存的是 PS1 的内存快照，解码必须真跑 CPU + SPU，所以绕不过 BIOS。
   这块只做三件事：显示状态、告诉用户为什么不能用、给一个「指定 BIOS…」的按钮。
   后端是 /api/psf/status 和 /api/psf/set_bios（见 web_ui/server.py）。 */
async function paintPsf(st) {
  const dot = $("#psfDot"), txt = $("#psfText"), sub = $("#psfSub");
  if (!dot || !txt) return;
  if (!st) {
    try { st = await api("/api/psf/status"); }
    catch (e) {
      dot.dataset.st = "warn"; txt.dataset.st = "warn";
      txt.textContent = "状态读不到";
      sub.textContent = e.message;
      return;
    }
  }
  if (st.available) {
    dot.dataset.st = "ok"; txt.dataset.st = "ok";
    txt.textContent = "已就绪";
    // 只显示文件名 + 一句短类型。整条路径 + 完整说明会把这一栏撑成三行
    // （实测：break-all 会把「HE BIOS」切成「HE BIO / S」）。
    const base = String(st.bios || "").split(/[\\/]/).pop();
    const short = /512KB/i.test(st.bios_kind || "") ? "512KB HE BIOS" : "4MB PS2 BIOS";
    const bundled = !!st.bios_bundled;
    sub.textContent = base + " · " + short + (bundled ? " · 已集成" : "");
    sub.title = (st.bios || "") + (st.bios_src ? "（来源：" + st.bios_src + "）" : "");
    // 已经能用：固化过的就只留「换一份」，没固化的给「集成到程序目录」——
    // 后者是用户真正想要的（「不要再次指定」），点一下把文件复制到 exe 旁边。
    const b = $("#btnPsfBios");
    if (b) {
      b.dataset.mode = bundled ? "ready" : "unbundled";
      if (!b.disabled) b.textContent = bundled ? "指定 BIOS…" : "集成到程序目录";
      b.title = bundled
        ? "换一份 BIOS"
        : "把这份 BIOS 复制到程序目录的 bios/ 里，以后换版本、源文件被删也不用再指定";
    }
    return;
  }
  dot.dataset.st = "warn"; txt.dataset.st = "warn";
  const b2 = $("#btnPsfBios");
  if (b2) {
    b2.dataset.mode = "missing";
    if (!b2.disabled) b2.textContent = "自动查找 BIOS";
  }
  if (!st.decoder) {
    txt.textContent = "解码器缺失";
    /* v5.9.6：这里原来是一整句解释，把右栏撑成四行。
       右栏要让位给频谱面板 —— 短句留屏上，完整说明降级到 title。 */
    sub.textContent = "psf2wav.exe 未随主程序打包";
    sub.title = "psf2wav.exe 没跟主程序放在一起，PSF 无法解码。";
  } else {
    txt.textContent = "未指定 BIOS";
    sub.textContent = "需要 4MB PS2 BIOS（如 scph39001.bin）";
    sub.title = "PSF 需要一份 4MB 的 PS2 BIOS（如 scph39001.bin），"
              + "程序会转成 512KB 的 HE BIOS。本工具不分发 BIOS。";
  }
}

async function loop() {
  await tick();
  setTimeout(loop, S.state === "running" ? 250 : 900);
}

/* ---------- 事件 ---------- */
function collectOpts() {
  return {
    out_dir: S.outDir || "",          // 空 = 让后端自己推导，别把提示文字当路径
    do_cd: $('[data-fmt="cd"]').classList.contains("on"),
    do_wav: AUDIO_FMTS.some((f) => {
      const el = $(`[data-fmt="${f}"]`);
      return el && el.classList.contains("on");
    }),
    /* 分轨音频的容器。do_wav 是「要不要出分轨音频」的总开关，
       fmt 只决定它落成 WAV / FLAC / MP3 / OGG —— 后端按 flac_out.WRITE_FMTS 走。
       v5.9.15：三选一；v6.0.0：四选一。优先级与后端 dl_api._opts_fmt() 一致
       （ogg > mp3 > flac > wav，见下面的 FMT_PRIORITY）；
       正常情况下四者互斥，走不到「同时为真」。 */
    fmt: FMT_PRIORITY.find((f) => {
      const el = $(`[data-fmt="${f}"]`);
      return el && el.classList.contains("on");
    }) || "wav",
    /* v5.9.6：单曲最长 / 速度补偿 已从界面移除 —— 这两项对绝大多数音源
       没有可见效果，留着只会让人以为能调。固定成 150 秒 / 不补偿。
       ⚠ 字段名不能删：后端 ConvertJob 直接读这两个 key，
         删了会退回它自己的默认值，将来改默认值两边就对不上了。 */
    max_secs: 150,
    default_secs: 150,
    gap: 2,
    speed: 0,
    xa_music_only: $("#ckXaMusic").checked,
    xa_split: $("#ckXaSplit").checked,
    merge_wav: $("#ckMerge").checked,
    verify: false, cd_wav: false, album: "", performer: "",
  };
}

let toastTimer = null;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3800);
}

function bind() {
  $$("#themeSeg button").forEach((b) => {
    // 复古那颗是**开面板**，不是切主题 —— 它的 onclick 在 buildThemePanel()
    // 里单独挂（open/close + 定位）。这里不跳过的话会把它覆盖成
    // setTheme("retro")，结果就是点了没反应、面板永远弹不出来。
    if (b.dataset.themeBtn === "retro") return;
    b.onclick = () => setTheme(b.dataset.themeBtn);
  });

  $("#searchBox").oninput = renderTable;

  /* 「添加音源」和「添加文件夹」走同一条链路：先拿路径，再 /api/add_sources。
     后端 add_paths 认识目录（会递归展开，见 scan_dir_sources），
     所以前端不需要区分「这是文件还是文件夹」。 */
  async function addPaths(paths, emptyMsg) {
    if (!paths || !paths.length) return 0;
    const a = await api("/api/add_sources", { paths });
    const n = a.added || 0;
    if (n) {
      toast("已添加 " + n + " 个音源");
    } else {
      const why = (a.skipped && a.skipped[0] && a.skipped[0].why) || "";
      toast(emptyMsg || ("没有新增" + (why ? "：" + why : "（可能已在列表里）")));
    }
    await tick();
    return n;
  }

  $("#btnAdd").onclick = async () => {
    try {
      const r = await api("/api/pick_files", {});
      await addPaths(r.paths, "没有新增（可能已在列表里）");
    } catch (e) { toast("选择文件失败：" + e.message); }
  };

  $("#btnAddDir").onclick = async () => {
    try {
      const r = await api("/api/pick_folder_sources", {});
      if (!r.path) return;
      await addPaths([r.path], "这个文件夹里没有可识别的音源");
    } catch (e) { toast("选择文件夹失败：" + e.message); }
  };

  /* 批量删除：把选中的 path 换成当前索引，**倒序**删 —— 正序删会让
     后面的索引全部前移，删掉的就错位了。 */
  $("#btnDelSel").onclick = async () => {
    const n = S.checked.size;
    if (!n) return;
    const idx = [];
    S.sources.forEach((s, i) => { if (S.checked.has(s.path)) idx.push(i); });
    if (!idx.length) return;
    idx.sort((a, b) => b - a);
    try {
      await api("/api/remove_sources", { indexes: idx });
      S.checked.clear();
      await tick();
      toast(`已删除 ${idx.length} 个音源`);
    } catch (e) { toast("删除失败：" + e.message); }
  };

  /* 清空音源列表：只清 sources，**不动输出结果** —— 用户清列表常常只是
     想重新挑一批音源，把已经转好的 WAV 一起抹掉是不可接受的。 */
  $("#btnClearSrc").onclick = async () => {
    if (!S.sources.length) return;
    try {
      await api("/api/clear_sources", {});
      S.checked.clear();
      S.sel = 0;
      await tick();
      toast("已清空音源列表");
    } catch (e) { toast("清空失败：" + e.message); }
  };

  $("#btnDownload").onclick = () => {
    // 下载页是独立的单页（/download.html）：主界面的音源列表和转换任务都在
    // 服务端，跳过去再跳回来状态全在，所以不需要做成同一页里的视图切换。
    // ⚠ 用 ThemeKit.now() 而不是 dataset.theme：复古时 dataset 只有 "retro"，
    //   传过去就丢了「到底是哪一套」，下载页会退回深色。
    const t = ThemeKit.now();
    location.href = "/download.html?theme=" + encodeURIComponent(t);
  };

  $("#btnGo").onclick = async () => {
    if (S.state === "running") return;
    try {
      await api("/api/start", { opts: collectOpts() });
      S.tab = "sources";
      $("#logPanel").hidden = false;
      await tick();
    } catch (e) { toast(e.message); }
  };

  $("#bCancel").onclick = async () => {
    try { await api("/api/cancel", {}); } catch (e) { toast(e.message); }
  };

  $("#pathBox").onclick = async () => {
    try {
      const r = await api("/api/pick_outdir", {});
      if (r.path) {
        S.outDir = r.path;
        paintOutDir();
      }
    } catch (e) { toast("选择目录失败：" + e.message); }
  };

  $("#btnPsfBios").onclick = async () => {
    const btn = $("#btnPsfBios");
    const mode = btn.dataset.mode || "missing";
    btn.disabled = true;
    try {
      if (mode === "unbundled") {
        // 已经能用，只是没固化：把当前这份复制进程序目录 bios/
        btn.textContent = "集成中…";
        const st = await api("/api/psf/status");
        const r = await api("/api/psf/set_bios", { path: st.bios });
        toast(r.bundled
          ? "已复制到程序目录，以后不用再指定"
          : ("已设置：" + (r.kind || "BIOS")));
        paintPsf(r.status);
        return;
      }
      if (mode === "missing") {
        // 用户诉求：本机明明有 BIOS，别让他手动指。先自动找一遍。
        btn.textContent = "正在查找…";
        try {
          const r = await api("/api/psf/autofind", {});
          if (r.ok) {
            toast("已自动找到并集成：" + (r.kind || "BIOS"));
            paintPsf(r.status);
            return;
          }
          toast("常见位置没找到，请手动指定一份 4MB 的 PS2 BIOS");
        } catch (_) { /* 接口不在就落到手动选择 */ }
      }
      btn.textContent = "验证中…";
      const r = await api("/api/psf/set_bios", { pick: true });
      if (r.cancelled) {
        // 用户自己关掉了对话框，什么都不用说
      } else {
        toast("BIOS 已设置：" + r.kind + (r.bundled ? "（已集成）" : ""));
        paintPsf(r.status);
      }
    } catch (e) {
      toast("BIOS 不可用：" + e.message);
      try { paintPsf(await api("/api/psf/status")); } catch (_) { /* 后端也没了 */ }
    } finally {
      btn.disabled = false;
      // 文案交给状态决定（「自动查找」/「集成到程序目录」/「指定 BIOS…」），
      // 不要硬写成「指定 BIOS…」—— 那会把上一行刚设好的 dataset.mode 骗掉。
      paintPsf();
    }
  };

  /* 上传云盘：整套流程（列清单 → 选范围 → 确认 → 没登录就弹登录窗 → 上传）
     都在 cloud.js 里，主界面与下载页共用同一份 —— 以前这里硬编码
     {merged:false} 且没登录就报「请用 tkinter 版」，用户被迫开旧版。 */
  $("#btnCloud").onclick = async () => {
    if (!S.outputs.length) return toast("先转换一次，有输出文件才能上传");
    $("#btnCloud").disabled = true;
    try {
      const ok = await CloudUI.upload({
        endpoint: "/api/upload_cloud",
        onLog: (m) => { $("#logPanel").hidden = false; },
      });
      if (ok) $("#logPanel").hidden = false;
    } catch (e) {
      toast("上传失败：" + e.message);
    } finally {
      $("#btnCloud").disabled = false;
    }
  };

  /* 打开输出目录（在「输出到」下面，转换前后都能点）
     ⚠ 必须把界面上显示的那个路径**显式传给后端**：转换开始前后端
     `state.opts.out_dir` 还是空的（只有 /api/start 时才写），只靠后端
     自己取会一律报「还没设输出目录」。
     而 #pathText 里可能是占位提示文字，不能当路径用 —— 所以取的是
     S.outDir / S.opts.out_dir，和 paintOutDir() 用同一份来源。 */
  $("#btnOpenDir").onclick = async () => {
    const dir = S.outDir || S.opts.out_dir || "";
    if (!dir) {
      toast("还没有输出目录：先添加音源并转换一次");
      return;
    }
    try {
      await api("/api/open_outdir", { dir });
    } catch (e) {
      toast("打不开目录：" + e.message);
    }
  };

  $("#progStrip").onclick = () => { $("#logPanel").hidden = !$("#logPanel").hidden; };
  $("#logClose").onclick = () => { $("#logPanel").hidden = true; };

  /* 播放控制已由 PlayerView 接管（player/ui.js），这里不再绑 dock 上的旧按钮。 */

  $$("#fmtGrid .fmt-card").forEach((c) => {
    c.onclick = () => {
      const fmt = c.dataset.fmt;
      /* v5.9.7：WAV / FLAC 互斥。
         两者是**同一段音频的两种容器**（都是 16bit/44100/立体声，
         FLAC 逐样本无损），同时勾只会把同样的音频写两份、白占一倍空间。
         所以勾一个就把另一个关掉。BIN+CUE 不受影响 —— 它是另一种产物。
         v5.9.15：MP3 加入，**三者互斥**；v6.0.0：OGG 加入，四者互斥。
         写成「把其它几个都关掉」而不是
         原来的两两分支 —— 三种格式的两两组合有 6 条，漏一条就是
         「勾了 MP3，FLAC 还亮着」，然后界面上两个都是选中态。
         ⚠ 不能改成「单选组」：CD 与分轨音频是可以同时要的（v5.4 起
         默认两个都不勾，用户自己决定要哪些）。 */
      if (!c.classList.contains("on") && AUDIO_FMTS.includes(fmt)) {
        AUDIO_FMTS.forEach((f) => {
          if (f === fmt) return;
          const other = $(`[data-fmt="${f}"]`);
          if (other) other.classList.remove("on");
        });
      }
      c.classList.toggle("on");
      /* 「开始转换」的可用态跟着格式选择走（两个都不勾时要禁用） */
      renderProgress();
    };
  });

  /* ---------- 拖放 ----------
     ⚠ 这段的前提必须写清楚：**浏览器拿不到拖入文件的真实路径**。
     ``File.path`` 是 Electron 的私有扩展，Chromium 从来没有过。我们是纯
     Edge ``--app`` 窗口（不是 Electron），所以 ``f.path`` **永远是
     undefined** —— 拖单个文件也一样失败，不只文件夹。

     按顺序试三种可能，最后一种一定可用：
       1. ``f.path`` —— 将来换成 Electron 之类的宿主时这条直接生效
       2. ``text/uri-list`` 里的 ``file:///`` —— 某些环境会带上，
          带上就是零拷贝，最理想
       3. 都拿不到（正常情况）→ 弹原生选择框，让用户选同一个文件夹/文件。
          比弹一句「请用添加音源」干等着有用。
  */
  function fileUriPaths(dt) {
    let raw = "";
    try { raw = dt.getData("text/uri-list") || ""; } catch (_) { /* 无此类型 */ }
    try { raw += "\n" + (dt.getData("text/plain") || ""); } catch (_) { /* 同上 */ }
    const out = [];
    for (const line of raw.split(/\r?\n/)) {
      const s = line.trim();
      if (!/^file:\/\//i.test(s)) continue;
      try {
        let p = decodeURIComponent(new URL(s).pathname);
        if (/^\/[A-Za-z]:/.test(p)) p = p.slice(1);   /* /D:/x → D:/x */
        p = p.replace(/\//g, "\\");
        if (!out.includes(p)) out.push(p);
      } catch (_) { /* 不是合法 URL，忽略 */ }
    }
    return out;
  }

  function droppedHasDir(dt) {
    for (const it of Array.from(dt.items || [])) {
      const en = it.webkitGetAsEntry && it.webkitGetAsEntry();
      if (en && en.isDirectory) return true;
    }
    return false;
  }

  /* 把 DataTransfer 里的每一项变成「(rel, File) 对」。
     ⚠ 必须在 drop 事件的**同步阶段**调用 —— 任何 await 之后 items 就空了。
       返回的 jobs 可以之后再 await（entry 对象已经拿到手，不受影响）。 */
  function collectDropped(dt) {
    const pairs = [];
    const jobs = [];
    const items = Array.from(dt.items || []);
    for (const it of items) {
      if (it.kind !== "file") continue;
      const entry = it.webkitGetAsEntry && it.webkitGetAsEntry();
      if (entry) {
        jobs.push(walkEntry(entry, ""));
      } else {
        const f = it.getAsFile && it.getAsFile();
        if (f) pairs.push({ rel: f.name, file: f });
      }
    }
    if (!items.length) {                 /* 老浏览器只有 files */
      for (const f of Array.from(dt.files || [])) {
        pairs.push({ rel: f.name, file: f });
      }
    }
    return { pairs, jobs };
  }

  /* 递归展开 FileSystemEntry。
     ⚠ readEntries() **一次最多回 100 条**，必须反复读到返回空数组为止 ——
       只读一次会静默丢掉后面的文件（一个 300 首的包只剩前 100 个）。 */
  function walkEntry(entry, prefix) {
    const rel = prefix ? prefix + "/" + entry.name : entry.name;
    if (entry.isFile) {
      return new Promise((resolve) => {
        entry.file((f) => resolve([{ rel, file: f }]), () => resolve([]));
      });
    }
    if (!entry.isDirectory) return Promise.resolve([]);
    const reader = entry.createReader();
    const jobs = [];
    const readMore = () => new Promise((resolve) => {
      reader.readEntries((ents) => {
        if (!ents.length) { resolve(); return; }
        for (const e of ents) jobs.push(walkEntry(e, rel));
        resolve(readMore());
      }, () => resolve());
    });
    return readMore().then(() => Promise.all(jobs)).then((all) => all.flat());
  }

  /* 把拖入的文件**内容**POST 给本地后端。
     为什么要走这条路：Chromium 出于安全**永远**不给网页真实路径
     （`File.path` 是 Electron 私有扩展，纯 Edge 窗口里恒为 undefined），
     所以「零拷贝拿路径」在本宿主上不可能实现。
     但文件内容是可以读的，而服务端就在 127.0.0.1 —— 上传等于内存拷贝，
     音源文件又都很小（.spc 64KB / .mini2sf 几百字节 + 1MB 库），够快。 */
  async function uploadDropped(pairs) {
    /* session 名会直接成为磁盘目录名（`<下载盘>/MSU1Downloads/拖入/<session>/`），
       所以用可读的时间戳而不是随机串 —— 用户在资源管理器里一眼能认出来。 */
    const now = new Date();
    const p2 = (n) => String(n).padStart(2, "0");
    const sess = "drop_" + now.getFullYear() + p2(now.getMonth() + 1)
               + p2(now.getDate()) + "-" + p2(now.getHours())
               + p2(now.getMinutes()) + p2(now.getSeconds())
               + "-" + Math.random().toString(36).slice(2, 5);
    const MAX = 200 * 1024 * 1024;     /* 单文件上限：音源不可能这么大 */
    let done = 0, sent = 0, oversize = 0;
    const total = pairs.length;
    for (const p of pairs) {
      if (p.file.size > MAX) { oversize++; continue; }
      try {
        const r = await fetch("/api/drop_blob", {
          method: "POST",
          headers: {
            "X-Rel-Path": encodeURIComponent(p.rel),
            "X-Drop-Session": encodeURIComponent(sess),
            "Content-Type": "application/octet-stream",
          },
          body: p.file,
        });
        if (r.ok) { done++; sent += p.file.size; }
      } catch (_) { /* 单个失败不打断整批 */ }
      if (total > 8 && done && done % 8 === 0) {
        toast(`正在读取拖入的文件 ${done}/${total}…`);
      }
    }
    if (!done) return { ok: false, added: 0, skipped: [], oversize };
    const res = await api("/api/drop_commit", { session: sess });
    res.uploaded = done;
    res.bytes = sent;
    res.oversize = oversize;
    return res;
  }

  document.addEventListener("dragover", (e) => {
    e.preventDefault();
    document.body.classList.add("dragging");
  });
  document.addEventListener("dragleave", () => document.body.classList.remove("dragging"));

  document.addEventListener("drop", async (e) => {
    e.preventDefault();
    document.body.classList.remove("dragging");

    const dt = e.dataTransfer;
    if (!dt) return;
    // ⚠ dataTransfer 只在事件**同步**阶段有效：任何 await 之后
    //   files / items 都会变空。所以这些必须在这里一次取完。
    const files = Array.from(dt.files || []);
    const legacy = files.map((f) => f.path).filter(Boolean);
    const uris = fileUriPaths(dt);
    const hasDir = droppedHasDir(dt);
    const dropped = collectDropped(dt);          /* ← 同步拿 entry 对象 */
    if (!files.length && !hasDir && !uris.length && !dropped.jobs.length) {
      return;                                    /* 拖的是文字之类 */
    }

    // ① 宿主给了真实路径（Electron / 某些环境带 file:// URI）→ 零拷贝
    const known = legacy.concat(uris);
    if (known.length) {
      try { await addPaths(known); } catch (err) { toast("添加失败：" + err.message); }
      return;
    }

    // ② 拿不到路径（Chromium 的常态）→ 把文件内容读上来
    const all = dropped.pairs.concat((await Promise.all(dropped.jobs)).flat());
    if (!all.length) { toast("没有读到任何文件"); return; }
    toast(all.length > 1 ? `正在读取 ${all.length} 个文件…` : "正在读取文件…");
    try {
      const r = await uploadDropped(all);
      await tick();
      if (r.added) {
        toast(`已添加 ${r.added} 个音源` +
              (r.oversize ? `（${r.oversize} 个文件过大已跳过）` : ""));
      } else {
        const why = (r.skipped && r.skipped[0] && r.skipped[0].why) || "";
        toast("没有可识别的音源" + (why ? "：" + why : ""));
      }
    } catch (err) { toast("添加失败：" + err.message); }
  });
}

/* ---------- 启动 ---------- */
async function boot() {
  const q = new URLSearchParams(location.search).get("theme");
  // ⚠ 必须**先**把主题表拉回来再 apply —— 复古主题的 28 个变量在
  //   /api/themes 里，没拉到就 apply 会静默退化成 dark
  await ThemeKit.boot();
  setTheme(q || ThemeKit.now() || "dark");
  buildThemePanel();
  bind();
  VIZ.init();          /* 频谱开关 + 空态；AudioContext 留到第一次播放再建 */
  PL.mount($("#pvHost"));   /* 播放器挂载（皮肤由 rml-player-skin 决定） */
  renderSources(); renderStage(); renderTable(); renderProgress(); paintOutDir();
  paintPsf();          /* 不 await：BIOS 检测要跑一次解码器，别挡住首屏 */
  probeFlac();         /* 不 await：同上，编码器不可用就把卡片置灰 */
  loop();
}

/* v5.9.7：FLAC 编码器（libsndfile）不可用时把那张卡片置灰。
   摆一个「点了必然失败」的按钮，比根本没有这个按钮更让人困惑 ——
   用户会以为是自己选错了音源。后端 /api/start 也会拦（400），
   两边都挡一道：界面负责提前告知，接口负责真守。

   v5.9.15：MP3 走同一套。函数名保留 probeFlac —— 它已经是「探测编码器
   可用性」的代名词，门禁也按这个名字断言（vendor/test_web_ui.py），
   为改名去动门禁不划算。实际探测的是 FMT_CARDS 里的每一种。 */
const FMT_CARDS = [
  ["flac", "FLAC"],
  ["mp3", "MP3"],
  ["ogg", "OGG"],
];

function probeFlac() {
  const cards = FMT_CARDS
    .map(([k, name]) => [k, name, $(`[data-fmt="${k}"]`)])
    .filter((x) => x[2]);
  if (!cards.length) return;
  fetch("/api/diag").then((r) => r.json()).then((d) => {
    let dirty = false;
    cards.forEach(([k, name, card]) => {
      const f = (d && d[k]) || {};
      if (f.ok) return;
      card.disabled = true;
      card.classList.remove("on");
      card.classList.add("fmt-off");
      card.title = name + " 编码器不可用：" + (f.why || "未知原因");
      dirty = true;
    });
    if (dirty) renderProgress();
  }).catch(() => { /* diag 拉不到不影响正常转换 */ });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
