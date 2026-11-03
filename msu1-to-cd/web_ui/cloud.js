/* ═══════════════════════════════════════════════════════════════════════
   cloud.js — 网易云云盘：登录 / 账号 / 上传范围。**主界面与下载页共用。**

   以前两个页面的「上传云盘」都是硬编码 ``{merged:false}``，没登录就报一句
   「请先用 tkinter 版登录一次」—— 用户被迫开旧版。现在整套搬进来了：

     点按钮 → 列清单 → 选范围（合并/分轨/全传）→ 确认体积
            → 没登录就弹登录窗 → 登完**自动回来接着传**

   三条登录路径与 tkinter 版一一对应：
     browser  开真浏览器走官方网页登录（推荐，不触发 8821 风控）
     cookie   粘贴请求头里的 Cookie（浏览器登录不可用时兜底）
     qr       扫码（可能被 8821 拦，撞上了自动切到 cookie 页）

   ⚠ 二维码**必须纯黑白**，不跟随主题。砖红/橄榄绿的二维码手机扫不出来 ——
     这是全项目唯一一处刻意不套主题的地方，改之前先想清楚。
   ═══════════════════════════════════════════════════════════════════════ */
(function (global) {
  "use strict";

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  /* 不走 app.js 的 api()：那个在 d.ok===false 时直接抛，
     而「未登录」是个**正常的**分支（need_login），不能当异常处理。 */
  async function req(path, body) {
    const opt = body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify(body),
    };
    try {
      const r = await fetch(path, opt);
      const d = await r.json().catch(() => ({}));
      if (!d.error && !r.ok) d.error = "HTTP " + r.status;
      return d;
    } catch (e) {
      return { ok: false, error: "连不上后端：" + e.message };
    }
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
    return (n / 1e3).toFixed(1) + " KB";
  }

  /* ── 轻提示 ──────────────────────────────────────────────────────── */
  let toastTimer = null;
  function toast(msg) {
    let el = $("#clToast");
    if (!el) {
      el = document.createElement("div");
      el.id = "clToast";
      el.className = "cl-toast";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
  }

  /* ── 通用对话框骨架 ──────────────────────────────────────────────── */
  function mask(html, cls) {
    const m = document.createElement("div");
    m.className = "cl-mask" + (cls ? " " + cls : "");
    m.innerHTML = html;
    document.body.appendChild(m);
    return m;
  }

  function closeMask(m) {
    if (m && m.parentNode) m.parentNode.removeChild(m);
  }

  /* ═══════════════════════════════════════════════════════════════════
     登录窗
     ═══════════════════════════════════════════════════════════════════ */

  const LOGIN_HTML = `
  <div class="cl-dlg" role="dialog" aria-modal="true">
    <div class="cl-head">
      <span class="cl-title">登录网易云音乐</span>
      <button class="cl-x" data-cl="close" title="关闭">×</button>
    </div>
    <div class="cl-tabs">
      <button data-cl-tab="browser" class="on">浏览器登录</button>
      <button data-cl-tab="cookie">粘贴 Cookie</button>
      <button data-cl-tab="qr">扫码（可能被风控）</button>
    </div>
    <div class="cl-body">

      <div class="cl-page" data-cl-page="browser">
        <p class="cl-lead">用浏览器登录（推荐）</p>
        <p class="cl-sub">会打开一个独立浏览器窗口，在里面正常登录一次即可。<br>
           之后每次上传都会自动复用，不用再登录。</p>
        <div class="cl-row">
          <button class="cl-btn primary" data-cl="browser">打开浏览器登录</button>
        </div>
        <p class="cl-stat" data-cl="browser-stat">尚未开始</p>
        <ul class="cl-tips">
          <li>用的是独立 profile，不会动你日常那个 Chrome</li>
          <li>扫码或账号密码都行，走的是网易云官方网页</li>
          <li>登录成功后浏览器会自动关闭</li>
          <li>凭据只存在本机，不会发往任何第三方</li>
        </ul>
        <div class="cl-sep"></div>
        <p class="cl-lead small">为什么不直接扫码？</p>
        <p class="cl-sub">网易云给网页扫码加了行为验证码风控（8821），手机点确认时会被拒。
           浏览器登录走的是官方网页流程，不触发这套风控。</p>
      </div>

      <div class="cl-page" data-cl-page="cookie" hidden>
        <p class="cl-lead">从浏览器复制 Cookie</p>
        <ol class="cl-steps">
          <li>浏览器打开 music.163.com 并登录</li>
          <li>按 F12 → Network，刷新页面</li>
          <li>点任意一条 music.163.com 请求</li>
          <li>右侧 Request Headers，复制 Cookie 整行</li>
        </ol>
        <textarea class="cl-textarea" data-cl="cookie-input" rows="5"
                  placeholder="MUSIC_U=...; __csrf=...; ..."></textarea>
        <div class="cl-row">
          <button class="cl-btn primary" data-cl="cookie">验证并登录</button>
        </div>
        <p class="cl-stat" data-cl="cookie-stat">提示：MUSIC_U 是 HttpOnly，
           document.cookie 拿不到，必须从请求头里复制。</p>
      </div>

      <div class="cl-page" data-cl-page="qr" hidden>
        <p class="cl-lead">用网易云音乐 App 扫描下方二维码</p>
        <p class="cl-sub">手机端：我的 → 右上角「扫一扫」</p>
        <div class="cl-qrbox"><canvas data-cl="qr"></canvas></div>
        <p class="cl-stat center" data-cl="qr-stat">正在申请二维码…</p>
        <p class="cl-warn">手机上提示「请切换其他登陆方式」？<br>
           → 那是风控（8821），请切到「粘贴 Cookie」页</p>
      </div>

    </div>
    <div class="cl-foot">
      <span class="cl-foot-note" data-cl="note"></span>
      <button class="cl-btn" data-cl="close">关闭</button>
    </div>
  </div>`;

  /**
   * 打开登录窗。返回 Promise<boolean>（true = 登录成功）。
   * 已经在登录中（浏览器任务还在跑）就直接挂上去看进度，不会重复开浏览器。
   */
  function openLogin() {
    return new Promise((resolve) => {
      const m = mask(LOGIN_HTML);
      const dlg = $(".cl-dlg", m);
      let done = false;
      let pollTimer = null;
      let qrTimer = null;
      let qrShown = false;
      let tab = "browser";

      const note = $('[data-cl="note"]', m);
      const setNote = (t) => { note.textContent = t || ""; };

      function finish(ok, who) {
        if (done) return;
        done = true;
        clearInterval(pollTimer);
        clearInterval(qrTimer);
        pollTimer = qrTimer = null;
        req("/api/cloud/dismiss", {});          // 清掉已结束的任务，别让下次误判
        closeMask(m);
        resolve(!!ok);
        if (ok) toast("已登录网易云：" + (who || ""));
      }

      function close() {
        if (done) { closeMask(m); return; }
        // 关窗时把还在跑的浏览器任务一起收掉 —— 否则用户关了窗，
        // 后台还挂着一个 Chrome 等 4 分钟
        req("/api/cloud/cancel", {});
        clearInterval(pollTimer);
        clearInterval(qrTimer);
        done = true;
        closeMask(m);
        resolve(false);
      }

      function switchTab(name) {
        tab = name;
        $$("[data-cl-tab]", m).forEach((b) => {
          b.classList.toggle("on", b.dataset.clTab === name);
        });
        $$("[data-cl-page]", m).forEach((p) => {
          p.hidden = p.dataset.clPage !== name;
        });
        if (name === "qr" && !qrShown) startQr();
      }

      /* ---- 二维码 ---- */
      async function startQr() {
        qrShown = true;
        const st = $('[data-cl="qr-stat"]', m);
        st.textContent = "正在申请二维码…";
        const d = await req("/api/cloud/qr_start", {});
        if (!d.ok) {
          st.textContent = "❌ " + (d.error || "申请失败");
          qrShown = false;
          return;
        }
        drawQr($('[data-cl="qr"]', m), d.rows, d.n);
        st.textContent = d.msg || "等待手机扫码…";
        clearInterval(qrTimer);
        qrTimer = setInterval(pollQr, 1600);
      }

      function drawQr(cv, rows, n) {
        // ⚠ 纯黑白，不跟主题（理由见文件头）
        const cell = Math.max(3, Math.floor(252 / Math.max(1, n)));
        cv.width = n * cell;
        cv.height = n * cell;
        const g = cv.getContext("2d");
        g.fillStyle = "#FFFFFF";
        g.fillRect(0, 0, cv.width, cv.height);
        g.fillStyle = "#000000";
        for (let y = 0; y < rows.length; y++) {
          const line = rows[y];
          for (let x = 0; x < line.length; x++) {
            if (line[x] === "1") g.fillRect(x * cell, y * cell, cell, cell);
          }
        }
      }

      async function pollQr() {
        const d = await req("/api/cloud/qr_poll");
        const st = $('[data-cl="qr-stat"]', m);
        if (!d.ok) { st.textContent = "⚠ " + (d.error || "轮询失败"); return; }
        if (d.busy) return;
        st.textContent = d.msg || "";
        if (d.done) {
          clearInterval(qrTimer);
          finish(true, d.user_name || "");
        } else if (d.switch_to === "cookie") {
          // 8821 风控：别再傻等，直接把用户送到能用的那一页
          clearInterval(qrTimer);
          switchTab("cookie");
          $('[data-cl="cookie-stat"]', m).textContent =
            "❌ 扫码被风控拦截（8821），请按上面的步骤复制 Cookie";
          const ta = $('[data-cl="cookie-input"]', m);
          if (ta) ta.focus();
        } else if (d.expired || d.code === 800) {
          clearInterval(qrTimer);
          qrShown = false;
        }
      }

      /* ---- 浏览器登录 ---- */
      async function startBrowser() {
        const btn = $('[data-cl="browser"]', m);
        const st = $('[data-cl="browser-stat"]', m);
        btn.disabled = true;
        st.textContent = "正在启动浏览器…";
        const d = await req("/api/cloud/login_browser", {});
        if (!d.ok) {
          btn.disabled = false;
          st.textContent = "❌ " + (d.error || "启动失败");
          return;
        }
        clearInterval(pollTimer);
        pollTimer = setInterval(pollJob, 800);
      }

      async function pollJob() {
        const d = await req("/api/cloud/status");
        const job = d.job;
        const st = $('[data-cl="browser-stat"]', m);
        if (!job) return;
        if (job.kind !== "browser" && job.kind !== "switch") return;
        if (job.msg) st.textContent = job.msg;
        if (job.state === "done") {
          clearInterval(pollTimer);
          finish(true, job.user || d.user_name || "");
        } else if (job.state === "failed") {
          clearInterval(pollTimer);
          $('[data-cl="browser"]', m).disabled = false;
          st.textContent = "❌ " + (job.error || "登录失败");
        } else if (job.state === "cancelled") {
          clearInterval(pollTimer);
          $('[data-cl="browser"]', m).disabled = false;
          st.textContent = "已取消";
        }
      }

      /* ---- 粘贴 Cookie ---- */
      async function submitCookie() {
        const ta = $('[data-cl="cookie-input"]', m);
        const st = $('[data-cl="cookie-stat"]', m);
        const btn = $('[data-cl="cookie"]', m);
        const raw = (ta.value || "").trim();
        if (!raw) { st.textContent = "❌ 输入框是空的，请先粘贴 Cookie"; return; }
        btn.disabled = true;
        st.textContent = "正在验证…（会向网易云拉一次账号信息）";
        const d = await req("/api/cloud/login_cookie", { cookie: raw });
        btn.disabled = false;
        if (d.ok) {
          st.textContent = "✅ 登录成功：" + (d.user_name || "");
          setTimeout(() => finish(true, d.user_name || ""), 500);
        } else {
          st.textContent = "❌ " + (d.error || "验证失败");
        }
      }

      /* ---- 绑定 ---- */
      $$("[data-cl-tab]", m).forEach((b) => {
        b.onclick = () => switchTab(b.dataset.clTab);
      });
      $$('[data-cl="close"]', m).forEach((b) => { b.onclick = close; });
      $('[data-cl="browser"]', m).onclick = startBrowser;
      $('[data-cl="cookie"]', m).onclick = submitCookie;
      m.onclick = (e) => { if (e.target === m) close(); };
      document.addEventListener("keydown", function esc(e) {
        if (e.key === "Escape") { document.removeEventListener("keydown", esc); close(); }
      });

      /* ---- 进来先看后端有没有在跑的登录任务 ---- */
      (async () => {
        const st = await req("/api/cloud/status");
        const job = st.job;
        if (job && job.running) {
          // 上一次点「浏览器登录」后关掉了窗，任务还在跑 —— 挂上去看进度
          $('[data-cl="browser"]', m).disabled = true;
          $('[data-cl="browser-stat"]', m).textContent = job.msg || "进行中…";
          pollTimer = setInterval(pollJob, 800);
        } else if (!st.can_browser) {
          $('[data-cl="browser"]', m).disabled = true;
          $('[data-cl="browser-stat"]', m).textContent =
            "❌ 没找到 Chrome / Edge，请改用「粘贴 Cookie」";
          switchTab("cookie");
        } else {
          setNote(st.browser ? "将使用 " + st.browser : "");
        }
        if (st.deps && st.deps.segno === false) {
          $$("[data-cl-tab]", m).forEach((b) => {
            if (b.dataset.clTab === "qr") b.textContent = "扫码（缺 segno）";
          });
        }
      })();

      setTimeout(() => {
        const ta = $('[data-cl="cookie-input"]', m);
        if (ta) ta.focus({ preventScroll: true });
      }, 60);
    });
  }

  /**
   * 弹「选择要上传的游戏」。返回 Promise<string[] | null>（null = 取消）。
   *
   * 为什么要有这一步：下载页的产物是 ``WAV/<游戏名>/xx.wav``，一个机种
   * 可能已经转好几十个游戏、上千首。以前点「上传云盘」是**全量**上传，
   * 用户想「先传这两个听听效果」做不到。
   *
   * 默认全勾 —— 多数人是想全传的，勾选框只是给他一个「少传点」的口子。
   */
  function chooseGames(games, st) {
    return new Promise((resolve) => {
      const who = (st && st.user_name) || "";
      const total = games.reduce((a, g) => a + (Number(g.count) || 0), 0);
      const bytes = games.reduce((a, g) => a + (Number(g.size) || 0), 0);
      const html = `
      <div class="cl-dlg narrow" role="dialog" aria-modal="true">
        <div class="cl-head">
          <span class="cl-title">选择要上传的游戏</span>
          <button class="cl-x" data-cl="close" title="关闭">×</button>
        </div>
        <div class="cl-body">
          <div class="cl-acct">
            <span class="cl-acct-dot ${who ? "ok" : ""}"></span>
            <span class="cl-acct-name">${who ? "已登录：" + who : "未登录"}</span>
          </div>
          <p class="cl-lead">已转好 ${games.length} 个游戏，共 ${total} 首 · ${fmtBytes(bytes)}</p>
          <p class="cl-sub">勾选这次要传到云盘的游戏（默认全选）。</p>
          <div class="cl-games">
            ${games.map((g, i) => `
              <label class="cl-game">
                <input type="checkbox" value="${encodeURIComponent(g.game || "")}"
                       data-count="${Number(g.count) || 0}" checked>
                <span class="g-name">${maskHtml(g.game || "（未分目录）")}</span>
                <span class="g-meta">${Number(g.count) || 0} 首 · ${g.size_text || fmtBytes(g.size)}</span>
              </label>`).join("")}
          </div>
          <p class="cl-warn">注意：平台已有版权的曲目可能被拒绝或匹配成平台版本。</p>
        </div>
        <div class="cl-foot">
          <button class="cl-link" data-cl="all">全选</button>
          <button class="cl-link" data-cl="none">全不选</button>
          <span class="cl-foot-note" data-cl="note"></span>
          <button class="cl-btn" data-cl="close">取消</button>
          <button class="cl-btn primary" data-cl="go">下一步</button>
        </div>
      </div>`;

      const m = mask(html, "narrow");
      const boxes = () => $$(".cl-game input", m);
      const picked = () => boxes().filter((b) => b.checked);
      const note = $('[data-cl="note"]', m);
      const paint = () => {
        const ps = picked();
        const n = ps.reduce((a, b) => a + (Number(b.dataset.count) || 0), 0);
        note.textContent = "已选 " + ps.length + " 个游戏 / " + n + " 首";
        $('[data-cl="go"]', m).disabled = !ps.length;
      };
      boxes().forEach((b) => { b.onchange = paint; });
      paint();

      let settled = false;
      const fin = (v) => {
        if (settled) return;
        settled = true;
        closeMask(m);
        resolve(v);
      };
      $$('[data-cl="close"]', m).forEach((b) => { b.onclick = () => fin(null); });
      m.onclick = (e) => { if (e.target === m) fin(null); };
      $('[data-cl="all"]', m).onclick = () => {
        boxes().forEach((b) => { b.checked = true; });
        paint();
      };
      $('[data-cl="none"]', m).onclick = () => {
        boxes().forEach((b) => { b.checked = false; });
        paint();
      };
      $('[data-cl="go"]', m).onclick = () => {
        const ps = picked();
        if (!ps.length) { note.textContent = "请至少选一个游戏"; return; }
        fin(ps.map((b) => decodeURIComponent(b.value)));
      };
    });
  }

  /* 对话框里的文本一律转义 —— 游戏名来自 joshw 的目录名，可能带 < > & */
  function maskHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  /* ═══════════════════════════════════════════════════════════════════
     上传范围选择（合并 / 分轨 / 全传）+ 账号行
     ═══════════════════════════════════════════════════════════════════ */

  /**
   * 弹「选择上传内容 + 上传格式」。返回 Promise<{scope, fmt} | null>（null = 取消）。
   * 顺带把账号管理（切换 / 退出）放在这里 —— 主界面顶栏只有一颗按钮，
   * 再塞一个「▾」会挤，而用户点上传时本来就该看到「现在是谁」。
   *
   * v6.0.1：加了**上传格式**（FLAC / MP3）。两种情况下都要显示 ——
   * 「范围」决定传哪些文件，「格式」决定每个文件多大，两者正交。
   * 体积估算不是前端按比例算的：后端 ``plan.est_scope[scope][fmt]`` 直接给
   * 三档范围的实数（口径见 ``cloud_api._est_mb``），因为用户是拿这个数判断
   * 「5 GB 够不够」的。
   */
  function chooseScope(plan, st) {
    return new Promise((resolve) => {
      const need = plan.need_choose;
      const who = (st && st.user_name) || "";
      const merged = plan.merged || [];
      const tracks = plan.tracks || [];
      const opts = need ? [
        { v: "merged", t: `只传合并文件（${merged.length} 个）— 整张连着听`,
          dis: !merged.length },
        { v: "tracks", t: `只传分轨（${tracks.length} 个）— 云盘里能单曲播放`,
          dis: !tracks.length },
        { v: "all", t: `全都传（${merged.length + tracks.length} 个）— 内容会重复`,
          dis: false },
      ] : [];

      // 每个范围 × 每种格式的体积（MB）。缺项一律当 0，不让 undefined 上屏。
      const est = plan.est_scope || { all: plan.est || { flac: plan.est_mb } };
      const estOf = (scope, fmt) => {
        const row = est[scope] || est.all || {};
        const v = row[fmt];
        return typeof v === "number" ? v : 0;
      };
      const fmtText = (fmt) => (fmt === "mp3" ? "MP3" : "FLAC");

      const html = `
      <div class="cl-dlg narrow" role="dialog" aria-modal="true">
        <div class="cl-head">
          <span class="cl-title">${need ? "选择上传内容" : "确认上传"}</span>
          <button class="cl-x" data-cl="close" title="关闭">×</button>
        </div>
        <div class="cl-body">
          <div class="cl-acct">
            <span class="cl-acct-dot ${who ? "ok" : ""}"></span>
            <span class="cl-acct-name">${who ? "已登录：" + who : "未登录"}</span>
            <span class="cl-acct-acts">
              ${who ? `<button class="cl-link" data-cl="switch">⇄ 切换账号</button>
                       <button class="cl-link" data-cl="logout">⎋ 退出登录</button>` : ""}
            </span>
          </div>
          ${need ? `
            <p class="cl-lead">这个目录里既有合并文件，也有分轨</p>
            <p class="cl-sub">
              · 合并文件 ${merged.length} 个，共 ${plan.m_text}　（每个源一个整轨）<br>
              · 分轨　　 ${tracks.length} 个，共 ${plan.t_text}　（每首一个文件）
            </p>
            ${plan.hint ? `<p class="cl-warn">${plan.hint}</p>` : ""}
            <div class="cl-radios">
              ${opts.map((o, i) => `
                <label class="cl-radio${o.dis ? " dis" : ""}">
                  <input type="radio" name="clScope" value="${o.v}"
                         ${i === 0 && !o.dis ? "checked" : ""}
                         ${o.dis ? "disabled" : ""}>
                  <span>${o.t}</span>
                </label>`).join("")}
            </div>` : `
            <p class="cl-lead">将把 ${plan.count} 个 WAV 转成音频上传到云盘</p>
            <p class="cl-sub">每个 WAV 先按下面的格式转码，再上传；临时文件传完即删。</p>`}

          <p class="cl-lead cl-sec-head">上传格式</p>
          <div class="cl-radios inline">
            <label class="cl-radio">
              <input type="radio" name="clFmt" value="flac" checked>
              <span>FLAC（无损）<em class="cl-radio-tag"
                    data-est-fmt="flac">—</em></span>
            </label>
            <label class="cl-radio">
              <input type="radio" name="clFmt" value="mp3">
              <span>MP3（320 kbps）<em class="cl-radio-tag"
                    data-est-fmt="mp3">—</em></span>
            </label>
          </div>
          <p class="cl-sub" data-cl="est"></p>
          <p class="cl-warn">注意：平台已有版权的曲目可能被拒绝或匹配成平台版本。</p>
        </div>
        <div class="cl-foot">
          <span class="cl-foot-note" data-cl="note"></span>
          <button class="cl-btn" data-cl="close">取消</button>
          <button class="cl-btn primary" data-cl="go">开始上传</button>
        </div>
      </div>`;

      const m = mask(html, "narrow");
      let settled = false;
      const fin = (v) => {
        if (settled) return;
        settled = true;
        closeMask(m);
        resolve(v);
      };

      /* ---- 估算刷新：范围 × 格式 → 每个 radio 上的数 + 一行合计 ---- */
      const curScope = () => {
        if (!need) return "all";
        const p = $('input[name="clScope"]:checked', m);
        return p ? p.value : "all";
      };
      const curFmt = () => {
        const p = $('input[name="clFmt"]:checked', m);
        return p ? p.value : "flac";
      };
      const refresh = () => {
        const sc = curScope();
        const f = curFmt();
        $$("[data-est-fmt]", m).forEach((el) => {
          const k = el.getAttribute("data-est-fmt");
          el.textContent = "约 " + estOf(sc, k) + " MB";
        });
        const n = sc === "merged" ? merged.length
          : sc === "tracks" ? tracks.length
            : (need ? merged.length + tracks.length : (plan.count || 0));
        const box = $('[data-cl="est"]', m);
        if (box) {
          box.innerHTML = `将上传 <b>${n}</b> 个文件，按当前选择预计占用约 `
            + `<b>${estOf(sc, f)}</b> MB（${fmtText(f)}）。<br>`
            + "免费额度通常 5 GB，超出需要会员扩容。";
        }
      };
      $$('input[name="clScope"], input[name="clFmt"]', m)
        .forEach((r) => { r.onchange = refresh; });
      refresh();

      $$('[data-cl="close"]', m).forEach((b) => { b.onclick = () => fin(null); });
      m.onclick = (e) => { if (e.target === m) fin(null); };
      $('[data-cl="go"]', m).onclick = () => {
        let scope = "all";
        if (need) {
          const pick = $('input[name="clScope"]:checked', m);
          if (!pick) { $('[data-cl="note"]', m).textContent = "请先选一项"; return; }
          scope = pick.value;
        }
        fin({ scope: scope, fmt: curFmt() });
      };

      const sw = $('[data-cl="switch"]', m);
      if (sw) {
        sw.onclick = async () => {
          const n = $('[data-cl="note"]', m);
          n.textContent = "正在清空旧登录态…";
          const d = await req("/api/cloud/switch", {});
          if (!d.ok) { n.textContent = "❌ " + (d.error || "切换失败"); return; }
          fin(null);
          toast("已清空旧登录态，请在新窗口里登录要用的账号");
          // 换号本身也是个后台任务，复用登录窗的进度显示
          const ok = await openLogin();
          if (ok) toast("账号已切换");
        };
      }
      const lo = $('[data-cl="logout"]', m);
      if (lo) {
        lo.onclick = async () => {
          const n = $('[data-cl="note"]', m);
          n.textContent = "正在退出…";
          lo.disabled = true;
          const d = await req("/api/cloud/logout", {});
          if (!d.ok) {
            n.textContent = "❌ " + (d.error || "退出失败");
            lo.disabled = false;
            return;
          }
          toast("已退出网易云登录（云端文件没动）");
          fin(null);
        };
      }
    });
  }

  /* ═══════════════════════════════════════════════════════════════════
     对外：完整上传流程
     ═══════════════════════════════════════════════════════════════════ */

  /**
   * @param {object} o
   *   o.dir        WAV 目录（下载页传 wav_dir；主界面留空 = 用输出目录）
   *   o.endpoint   "/api/upload_cloud" 或 "/api/dl/upload_cloud"
   *   o.games      可选，[{game, count, size, size_text}]。
   *                给了就先弹「选哪些游戏」——下载页的产物按游戏分目录，
   *                用户要能只传自己挑的那几个。主界面不传 = 不弹这一步。
   *   o.onLog      可选，(msg) => void
   *   o.onDone     可选，(d) => void
   * @returns {Promise<boolean>} 是否真的开始上传了
   */
  async function upload(o) {
    o = o || {};
    const endpoint = o.endpoint || "/api/upload_cloud";
    const log = typeof o.onLog === "function" ? o.onLog : () => {};

    // 0) 账号先看一眼 —— 「选游戏」对话框里要显示「现在是谁」
    const st = await req("/api/cloud/status");
    if (!st.can_upload) {
      toast(st.why || "云盘功能不可用");
      return false;
    }

    // 1) 选游戏（只在调用方给了清单时才弹）
    let games = null;
    if (Array.isArray(o.games) && o.games.length) {
      games = await chooseGames(o.games, st);
      if (!games) return false;                 // 取消
      if (!games.length) { toast("没有选中任何游戏"); return false; }
    }

    // 2) 列清单（带上游戏过滤 —— 数目要对得上「已选 N 个游戏」）
    let q = o.dir ? "?dir=" + encodeURIComponent(o.dir) : "";
    if (games) {
      // ``|`` 分隔：Windows 目录名不能含 ``|``，而逗号在游戏名里很常见
      q += (q ? "&" : "?") + "games=" + encodeURIComponent(games.join("|"));
    }
    const plan = await req("/api/cloud/plan" + q);
    if (!plan.ok) { toast(plan.error || "没有可上传的 WAV"); return false; }

    // 3) 上传范围（合并 / 分轨 / 全传）+ 上传格式（FLAC / MP3）
    const pick = await chooseScope(plan, st);
    if (!pick) return false;

    // 4) 真上传；未登录就弹登录窗，登完**自动重试**
    const body = { scope: pick.scope, fmt: pick.fmt };
    if (games) body.games = games;
    const send = () => req(endpoint, body);
    let d = await send();
    if (d.need_login) {
      const ok = await openLogin();
      if (!ok) return false;
      d = await send();
    }
    if (!d.ok) { toast(d.error || "无法上传"); return false; }
    const n = d.count || d.files || 0;
    const f = (d.fmt || pick.fmt || "flac").toUpperCase();
    toast(`开始上传 ${n} 个文件（${f}）${d.user ? "· " + d.user : ""}，进度看日志`);
    log(`☁ 开始上传 ${n} 个文件（${f}）`);
    if (typeof o.onDone === "function") o.onDone(d);
    return true;
  }

  /** 只取状态（页面启动时刷一下按钮标题用）。 */
  async function status() { return req("/api/cloud/status"); }

  global.CloudUI = { upload, status, openLogin, toast, req };
})(window);
