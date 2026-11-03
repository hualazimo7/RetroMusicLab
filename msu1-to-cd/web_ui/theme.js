/* ═══════════════════════════════════════════════════════════════════════
   theme.js — 主题：2 套现代（dark / light）+ 6 套复古。**两页共用。**

   复古那 6 套的配色来自 `retro_palette.THEMES`（tkinter / Qt 版一直用的
   那 6 套，纯数据）。后端 `/api/themes` 已经把它们翻成 CSS 变量，这里只负责
   往 `<html>` 上 setProperty —— **不依赖 app.css 里任何默认值**，因为
   `data-theme="retro"` 不匹配 `html[data-theme="dark"|"light"]` 那两个块，
   里面的变量根本不会生效。

   ⚠ 切回 dark/light 时必须把上次注入的 28 个变量**逐个 removeProperty**。
     只改 data-theme 不清 inline style 的话，深色底配着米黄的 --surface，
     会变成一块一块的色斑 —— 而且只有切换过主题的人才看得到。
   ═══════════════════════════════════════════════════════════════════════ */
(function (global) {
  "use strict";

  const KEY = "rml-theme";
  let LIST = [];             // [{key,label,group,swatch,vars}]
  let VARS = {};             // key -> {css变量: 值}
  let applied = [];          // 当前注入过的变量名（切走时要清）
  let current = "";

  async function load() {
    try {
      const r = await fetch("/api/themes");
      const d = await r.json();
      if (d && Array.isArray(d.themes) && d.themes.length) {
        LIST = d.themes;
        VARS = {};
        LIST.forEach((t) => { VARS[t.key] = t.vars || {}; });
      }
    } catch (e) {
      /* 后端没起来：只留 dark / light，不影响其它功能 */
    }
    return LIST;
  }

  function apply(key) {
    const root = document.documentElement;
    const vars = VARS[key];

    // 1) 清掉上一次注入的
    applied.forEach((k) => root.style.removeProperty(k));
    applied = [];

    // 2) 决定 data-theme
    if (vars && Object.keys(vars).length) {
      root.dataset.theme = "retro";
      applied = Object.keys(vars);
      applied.forEach((k) => root.style.setProperty(k, vars[k]));
    } else {
      root.dataset.theme = (key === "light") ? "light" : "dark";
    }

    current = key;
    try { localStorage.setItem(KEY, key); } catch (e) { /* 隐私模式可能禁写 */ }
    return key;
  }

  function saved() {
    try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; }
  }

  /** 启动时调用：URL 的 ?theme= 优先（下载页靠它继承主界面的选择）。 */
  async function boot() {
    await load();
    let q = "";
    try { q = new URLSearchParams(location.search).get("theme") || ""; } catch (e) {}
    const want = q || saved() || "dark";
    // 存的 key 可能已经不在列表里（旧版本 / 手改过 localStorage / 后端挂了）
    // → 退回 dark。注意不能写成 `VARS[want] || want === "light" ? …`，
    //   那会因为优先级变成 `(VARS[want] || (want==="light")) ? …`。
    const known = Object.prototype.hasOwnProperty.call(VARS, want)
                  || want === "dark" || want === "light";
    apply(known ? want : "dark");
    return current;
  }

  function list() { return LIST.slice(); }
  function now() { return current; }

  /* ── 配色面板（两页共用）─────────────────────────────────────────
     造一个挂在 body 上的浮层，点 `[data-theme-btn="retro"]` 开合。
     `host` 是放那颗按钮的容器（主界面 `#themeSeg`、下载页 `#dlThemeSeg`）。

     ⚠ 这颗按钮的 onclick **必须在这里挂**，别让页面自己的
       「遍历容器里所有按钮 → setTheme(dataset.themeBtn)」循环覆盖掉 ——
       覆盖之后点它只会 setTheme("retro")（= 退回深色），面板永远弹不出来。
       主界面 `app.js` 的 `bind()` 里已经显式跳过它了。 */
  function mountPanel(host) {
    const seg = typeof host === "string" ? document.querySelector(host) : host;
    if (!seg) return null;
    const btn = seg.querySelector('[data-theme-btn="retro"]');
    if (!btn) return null;
    if (seg.__themePop) return seg.__themePop;      // 只挂一次

    const esc = (s) => String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

    const groups = [];
    LIST.forEach((t) => {
      let g = groups.find((x) => x.name === t.group);
      if (!g) { g = { name: t.group || "其它", items: [] }; groups.push(g); }
      g.items.push(t);
    });

    const pop = document.createElement("div");
    pop.className = "theme-pop";
    pop.id = "themePop";        // 调试 / 截图脚本（shot.py --retro）靠它定位
    pop.hidden = true;
    pop.innerHTML = groups.map((g) => (
      `<div class="theme-pop-grp">${esc(g.name)}</div>` +
      g.items.map((t) => (
        `<button class="theme-item" data-theme-key="${esc(t.key)}">
           <i class="theme-swatch">` +
        (t.swatch || []).map((c) => `<b style="background:${esc(c)}"></b>`).join("") +
        `</i><span>${esc(t.label)}</span>
           <svg class="theme-tick" viewBox="0 0 16 16" aria-hidden="true">
             <path d="M3.5 8.5 6.5 11.5 12.5 5" fill="none" stroke="currentColor"
                   stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
           </svg>
         </button>`
      )).join("")
    )).join("");
    document.body.appendChild(pop);
    seg.__themePop = pop;

    function paintTicks() {
      pop.querySelectorAll(".theme-item").forEach((b) => {
        b.classList.toggle("on", b.dataset.themeKey === current);
      });
    }
    function place() {
      const r = btn.getBoundingClientRect();
      pop.style.top = (r.bottom + 8) + "px";
      const w = pop.offsetWidth || 220;
      pop.style.left = Math.max(8, Math.min(r.right - w, innerWidth - w - 8)) + "px";
    }
    function onDoc(e) {
      if (!pop.contains(e.target) && !seg.contains(e.target)) close();
    }
    function open() {
      paintTicks();
      pop.hidden = false;
      place();
      setTimeout(() => document.addEventListener("mousedown", onDoc), 0);
    }
    function close() {
      pop.hidden = true;
      document.removeEventListener("mousedown", onDoc);
    }

    pop.querySelectorAll(".theme-item").forEach((b) => {
      b.onclick = () => { apply(b.dataset.themeKey); paintTicks(); close(); };
    });
    btn.onclick = (e) => { e.stopPropagation(); pop.hidden ? open() : close(); };
    addEventListener("resize", () => { if (!pop.hidden) place(); });
    addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });

    return pop;
  }

  global.ThemeKit = { load, apply, boot, list, now, saved, mountPanel, KEY };
})(window);
