// discrip in Jellyfin's web client (and the apps that wrap it): a disc icon in the
// header, which turns into a status pill when there's something to see (a rip in
// progress, prompts waiting, a problem). Tapping it opens discrip in a pop-up over
// Jellyfin, in Jellyfin's current theme.
// Loaded by a one-line snippet in Jellyfin's JavaScript Injector plugin:
//   (function(){var s=document.createElement('script');
//     s.src=location.protocol+'//'+location.hostname+':8080/pill.js';document.head.appendChild(s);})();
(function () {
  "use strict";
  if (window.__discripPill) return;
  window.__discripPill = true;

  const BASE = new URL(document.currentScript ? document.currentScript.src : location.href).origin;
  const STAGE = { detecting: "Checking disc", scanning: "Scanning", ripping: "Ripping",
                  moving: "Filing", ejecting: "Ejecting" };
  const COLORS = { busy: "#2f6fd6", need: "#b26a00", bad: "#c0352b" };
  // Material "album" icon
  const DISC = '<svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" style="flex:none">' +
    '<path fill="currentColor" d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 ' +
    '14.5c-2.49 0-4.5-2.01-4.5-4.5S9.51 7.5 12 7.5s4.5 2.01 4.5 4.5-2.01 4.5-4.5 4.5zm0-5.5c-.55 0-1 ' +
    '.45-1 1s.45 1 1 1 1-.45 1-1-.45-1-1-1z"/></svg>';

  // Material "open_in_new" icon
  const NEWTAB = '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">' +
    '<path fill="currentColor" d="M19 19H5V5h7V3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 ' +
    '2-.9 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z"/></svg>';

  const css = document.createElement("style");
  css.textContent = `
.discrip-btn { display:none; align-items:center; gap:6px; margin:0 4px; padding:8px;
  border:0; border-radius:999px; background:transparent; color:inherit; cursor:pointer;
  font:600 13px/1.6 -apple-system,BlinkMacSystemFont,"Noto Sans",sans-serif; white-space:nowrap;
  max-width:46vw; overflow:hidden; align-self:center; z-index:1000; -webkit-tap-highlight-color:transparent; }
.discrip-btn:hover { background:rgba(128,128,128,.18); }
.discrip-btn.pill { padding:4px 12px 4px 8px; color:#fff; }
.discrip-btn.pill svg { width:18px; height:18px; }
.discrip-btn .t { overflow:hidden; text-overflow:ellipsis; }
.discrip-ov { position:fixed; inset:0; z-index:100000; background:rgba(0,0,0,.55);
  display:flex; align-items:center; justify-content:center; }
.discrip-panel { display:flex; flex-direction:column; width:min(720px, calc(100vw - 32px));
  height:min(900px, calc(100vh - 48px)); border-radius:12px; overflow:hidden;
  box-shadow:0 12px 48px rgba(0,0,0,.5); }
.discrip-bar { display:flex; align-items:center; gap:4px; padding:4px 4px 4px 16px; flex:none;
  font:600 17px/1 -apple-system,BlinkMacSystemFont,"Noto Sans",sans-serif; }
.discrip-bar span { flex:1; }
.discrip-bar a { display:inline-flex; align-items:center; justify-content:center; width:44px;
  height:44px; border-radius:50%; color:inherit; }
.discrip-bar a:hover { background:rgba(128,128,128,.2); }
.discrip-bar button { width:44px; height:44px; border:0; border-radius:50%; background:transparent;
  color:inherit; font-size:22px; line-height:44px; cursor:pointer; padding:0; }
.discrip-bar button:hover { background:rgba(128,128,128,.2); }
.discrip-panel iframe { flex:1; width:100%; border:0; display:block; }
@media (max-width: 760px), (max-height: 560px) {
  .discrip-ov { align-items:stretch; }
  .discrip-panel { width:100%; height:100%; border-radius:0; box-shadow:none; }
  .discrip-bar { padding-top:calc(4px + env(safe-area-inset-top)); }
}`;
  document.head.appendChild(css);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "discrip-btn";
  btn.innerHTML = DISC + '<span class="t"></span>';
  btn.title = "discrip";
  btn.setAttribute("aria-label", "discrip");
  btn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); open(); });

  let version = null;  // pill_version from the first status; a change means reload
  let timer = null;
  let retired = false;

  // Where the button went, reported to discrip as ?pill=... on status requests
  // (shows in the discrip-web log, which helps when Jellyfin's layout changes).
  let placement = "none";

  const visible = (el) =>
    el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";

  function visibleHeader() {
    // Jellyfin 12: the visible header is an MUI AppBar whose toolbar holds a
    // flex-grow box of buttons justified to flex-end (measured on 12.2). Older
    // layouts: .headerRight. Hidden headers of other views stay in the page.
    for (const bar of document.querySelectorAll(".MuiAppBar-root .MuiToolbar-root, .skinHeader .MuiToolbar-root")) {
      if (!visible(bar)) continue;
      const right = [...bar.children].find((el) =>
        el.tagName === "DIV" && getComputedStyle(el).justifyContent === "flex-end");
      if (right) return right;
    }
    return [...document.querySelectorAll(".headerRight")].find(visible);
  }

  function diag() {
    // What the page offers, for fixing placement when Jellyfin's layout changes.
    const n = (sel) => {
      const all = [...document.querySelectorAll(sel)];
      return all.length + "/" + all.filter(visible).length;
    };
    const bar = [...document.querySelectorAll(".MuiToolbar-root")].find(visible);
    const kids = bar ? [...bar.children].map((el) => el.tagName.toLowerCase() + "." +
      (el.className && el.className.baseVal === undefined ? String(el.className) : "")
        .split(" ").filter((c) => !/^css-/.test(c)).slice(0, 2).join(".") + ":" +
      getComputedStyle(el).justifyContent).join(",") : "-";
    const barUp = bar ? (bar.parentElement.className || "").toString().split(" ").slice(0, 3).join(".") : "-";
    return `skinHeader=${n(".skinHeader")} toolbar=${n(".MuiToolbar-root")} ` +
           `headerRight=${n(".headerRight")} appbar=${n(".MuiAppBar-root")} barParent=${barUp} kids=${kids}`;
  }

  // What tick() last decided: null = icon only, else [text, kind, title].
  let status = null;

  function place() {
    const header = visibleHeader();
    if (header) {
      if (btn.parentElement !== header) {
        header.prepend(btn);
        btn.style.position = btn.style.top = btn.style.left = btn.style.transform = "";
      }
      placement = "header";
    } else if (status) {
      // No header (e.g. the video player): only a status pill floats; the plain
      // icon stays out of the way.
      if (btn.parentElement !== document.body) document.body.append(btn);
      btn.style.position = "fixed";
      btn.style.top = "calc(8px + env(safe-area-inset-top))";
      btn.style.left = "50%";
      btn.style.transform = "translateX(-50%)";
      placement = "float";
    } else {
      placement = "none";
    }
    btn.style.display = placement === "none" ? "none" : "inline-flex";
    const r = btn.getBoundingClientRect();
    if (placement !== "none" && (r.width === 0 || r.height === 0)) placement += "-invisible";
  }

  function render() {
    const t = btn.querySelector(".t");
    if (status) {
      const [text, kind, title] = status;
      btn.classList.add("pill");
      btn.style.background = COLORS[kind];
      t.textContent = text;
      btn.title = title || text;
    } else {
      btn.classList.remove("pill");
      btn.style.background = "";
      t.textContent = "";
      btn.title = "discrip";
    }
    place();
  }

  // ------------------------------------------------------------ theme
  // Jellyfin 12 publishes its current theme as MUI CSS variables (--jf-palette-*).
  function rgb(color) {
    const probe = document.createElement("div");
    probe.style.color = color;
    probe.style.display = "none";
    document.body.append(probe);
    const m = getComputedStyle(probe).color.match(/[\d.]+/g);
    probe.remove();
    return m ? m.slice(0, 3).map(Number) : null;
  }

  function theme() {
    const cs = getComputedStyle(document.body);
    const v = (name) => cs.getPropertyValue("--jf-palette-" + name).trim();
    let bg = v("background-default");
    if (!bg) {
      const html = getComputedStyle(document.documentElement).backgroundColor;
      bg = html && !/rgba\(0, 0, 0, 0\)|transparent/.test(html) ? html : "#101010";
    }
    const c = rgb(bg) || [16, 16, 16];
    const lum = (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255;
    const t = {
      mode: lum > 0.5 ? "light" : "dark", bg, card: v("background-paper"),
      text: v("text-primary") || getComputedStyle(document.body).color,
      muted: v("text-secondary"), line: v("divider"), accent: v("primary-main"),
      accentText: v("primary-contrastText"), ok: v("success-main"), warn: v("warning-main"),
      bad: v("error-main"),
    };
    for (const k of Object.keys(t)) if (!t[k]) delete t[k];
    return t;
  }

  // ------------------------------------------------------------ pop-up
  let ov = null;

  function open() {
    if (ov) return;
    const t = theme();
    ov = document.createElement("div");
    ov.className = "discrip-ov";
    ov.innerHTML = '<div class="discrip-panel" role="dialog" aria-label="discrip">' +
      '<div class="discrip-bar"><span>discrip</span>' +
      // Not in Jellyfin's phone apps (window.NativeShell): a new window there
      // opens outside the app with no way back.
      (window.NativeShell ? "" : '<a class="discrip-tab" target="_blank" rel="noopener" ' +
        'title="Open in a new tab" aria-label="Open in a new tab">' + NEWTAB + '</a>') +
      '<button type="button" class="discrip-x" title="Close" aria-label="Close">✕</button></div>' +
      '<iframe title="discrip"></iframe></div>';
    const panel = ov.firstChild, bar = panel.firstChild;
    panel.style.background = t.bg;
    bar.style.background = t.card || t.bg;
    bar.style.color = t.text;
    panel.querySelector("iframe").src = BASE + "/?embed=1&theme=" + encodeURIComponent(JSON.stringify(t));
    bar.querySelector(".discrip-x").onclick = close;
    const tab = bar.querySelector(".discrip-tab");
    if (tab) {
      tab.href = BASE + "/";
      tab.addEventListener("click", () => setTimeout(close, 0));
    }
    ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
    document.body.append(ov);
    document.documentElement.style.overflow = "hidden";
  }

  function close() {
    if (!ov) return;
    ov.remove();
    ov = null;
    document.documentElement.style.overflow = "";
    tick();
  }

  // Esc here, or inside the pop-up (the page posts {discrip: "close"}); Jellyfin
  // navigating (back button/gesture) also closes it.
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && ov) { e.stopPropagation(); close(); } }, true);
  window.addEventListener("message", (e) => {
    if (e.origin === BASE && e.data && e.data.discrip === "close") close();
  });
  window.addEventListener("popstate", close);
  window.addEventListener("hashchange", close);

  // ------------------------------------------------------------ status
  async function tick() {
    if (retired || document.hidden) return;
    let s;
    try {
      const r = await fetch(BASE + "/api/status?pill=" + placement +
                            (placement === "float" ? "&dbg=" + encodeURIComponent(diag()) : ""),
                            { cache: "no-store" });
      s = await r.json();
    } catch (e) {
      btn.style.display = "none"; // discrip unreachable: stay out of the way
      return;
    }
    // A newer pill.js was installed: swap this one for it (no Jellyfin reload needed).
    if (version === null) version = s.pill_version;
    else if (s.pill_version !== version) {
      retired = true;
      clearInterval(timer);
      observer.disconnect();
      close();
      btn.remove();
      css.remove();
      window.__discripPill = false;
      const next = document.createElement("script");
      next.src = BASE + "/pill.js?v=" + s.pill_version;
      document.head.appendChild(next);
      return;
    }
    const job = s.state.job, pending = s.state.pending || [], d = s.state.drive || {};
    const flags = (s.health && s.health.throttle_flags) || [];
    if (d.status === "missing" || d.status === "unresponsive") {
      status = ["Drive not responding", "bad", "Power-cycle the DVD drive"];
    } else if (flags.some((f) => f.endsWith("now"))) {
      status = [flags[0], "bad"];
    } else if (job) {
      const pct = job.percent == null ? "" : " " + Math.round(job.percent) + "%";
      const name = job.title || d.label || "disc";
      status = [(STAGE[job.stage] || job.stage) + " " + name + pct, "busy", job.detail || ""];
    } else if (pending.length) {
      // A season with several discs is one question.
      const asks = new Set(pending.map((p) => p.group || p.id)).size;
      status = [asks + (asks === 1 ? " disc needs" : " discs need") + " you", "need",
                "Open discrip to answer"];
    } else if (s.last_job && s.last_job.result === "error" &&
               Date.parse(s.now) - Date.parse(s.last_job.finished_at) < 6 * 3600e3) {
      status = ["Rip failed: " + (s.last_job.title || "disc"), "bad", s.last_job.error || ""];
    } else {
      status = null;
    }
    render();
  }

  // Jellyfin is a single-page app that redraws its header: re-place at most once
  // per frame after DOM changes.
  let queued = false;
  const observer = new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; if (!retired) place(); });
  });
  observer.observe(document.body, { childList: true, subtree: true });
  tick();
  timer = setInterval(tick, 5000);
  document.addEventListener("visibilitychange", tick);
})();
