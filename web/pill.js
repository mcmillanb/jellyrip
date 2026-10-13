// discrip status pill for Jellyfin's web client (and the apps that wrap it).
// Loaded by a one-line snippet in Jellyfin's JavaScript Injector plugin:
//   (function(){var s=document.createElement('script');
//     s.src=location.protocol+'//'+location.hostname+':8080/pill.js';document.head.appendChild(s);})();
// Shows only when there is something to see: a rip in progress, prompts waiting,
// or a problem. Tapping it opens the discrip web app.
(function () {
  "use strict";
  if (window.__discripPill) return;
  window.__discripPill = true;

  const BASE = new URL(document.currentScript ? document.currentScript.src : location.href).origin;
  const STAGE = { detecting: "Checking disc", scanning: "Scanning", ripping: "Ripping",
                  moving: "Filing", ejecting: "Ejecting" };
  const COLORS = { busy: "#2f6fd6", need: "#b26a00", bad: "#c0352b" };

  const pill = document.createElement("a");
  pill.href = BASE + "/";
  pill.target = "_blank";
  pill.rel = "noopener";
  pill.style.cssText = [
    "display:none", "align-items:center", "gap:6px", "margin:0 8px", "padding:4px 12px",
    "border-radius:999px", "font:600 13px/1.6 -apple-system,BlinkMacSystemFont,sans-serif",
    "color:#fff", "text-decoration:none", "align-self:center", "white-space:nowrap", "max-width:46vw",
    "overflow:hidden", "text-overflow:ellipsis", "z-index:1000",
  ].join(";");

  let version = null;  // pill_version from the first status; a change means reload
  let timer = null;

  // Where the pill went, reported to discrip as ?pill=... on status requests
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

  function place() {
    const header = visibleHeader();
    if (header) {
      if (pill.parentElement !== header) {
        header.prepend(pill);
        pill.style.position = pill.style.top = pill.style.left = pill.style.transform = "";
      }
      placement = "header";
    } else {
      if (pill.parentElement !== document.body) document.body.append(pill);
      pill.style.position = "fixed";
      pill.style.top = "calc(8px + env(safe-area-inset-top))";
      pill.style.left = "50%";
      pill.style.transform = "translateX(-50%)";
      placement = "float";
    }
    const r = pill.getBoundingClientRect();
    if (pill.style.display !== "none" && (r.width === 0 || r.height === 0)) placement += "-invisible";
  }

  function show(text, kind, title) {
    place();
    pill.textContent = text;
    pill.title = title || text;
    pill.style.background = COLORS[kind];
    pill.style.display = "inline-flex";
  }

  let retired = false;

  async function tick() {
    if (retired || document.hidden) return;
    let s;
    try {
      const r = await fetch(BASE + "/api/status?pill=" + placement +
                            (placement === "float" ? "&dbg=" + encodeURIComponent(diag()) : ""),
                            { cache: "no-store" });
      s = await r.json();
    } catch (e) {
      pill.style.display = "none"; // discrip unreachable: stay out of the way
      return;
    }
    // A newer pill.js was installed: swap this one for it (no Jellyfin reload needed).
    if (version === null) version = s.pill_version;
    else if (s.pill_version !== version) {
      retired = true;
      clearInterval(timer);
      observer.disconnect();
      pill.remove();
      window.__discripPill = false;
      const next = document.createElement("script");
      next.src = BASE + "/pill.js?v=" + s.pill_version;
      document.head.appendChild(next);
      return;
    }
    const job = s.state.job, pending = s.state.pending || [], d = s.state.drive || {};
    const flags = (s.health && s.health.throttle_flags) || [];
    if (d.status === "missing" || d.status === "unresponsive") {
      show("💿 Drive not responding", "bad", "Power-cycle the DVD drive");
    } else if (flags.some((f) => f.endsWith("now"))) {
      show("⚡ " + flags[0], "bad");
    } else if (job) {
      const pct = job.percent == null ? "" : " " + Math.round(job.percent) + "%";
      const name = job.title || d.label || "disc";
      show("💿 " + (STAGE[job.stage] || job.stage) + " " + name + pct, "busy",
           job.detail || "");
    } else if (pending.length) {
      // A season with several discs is one question.
      const asks = new Set(pending.map((p) => p.group || p.id)).size;
      show("💿 " + asks + (asks === 1 ? " disc needs" : " discs need") + " you", "need",
           "Open discrip to answer");
    } else if (s.last_job && s.last_job.result === "error" &&
               Date.parse(s.now) - Date.parse(s.last_job.finished_at) < 6 * 3600e3) {
      show("💿 Rip failed: " + (s.last_job.title || "disc"), "bad", s.last_job.error || "");
    } else {
      pill.style.display = "none";
    }
  }

  // Jellyfin is a single-page app that redraws its header: re-place at most once
  // per frame after DOM changes.
  let queued = false;
  const observer = new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; if (pill.style.display !== "none") place(); });
  });
  observer.observe(document.body, { childList: true, subtree: true });
  tick();
  timer = setInterval(tick, 5000);
  document.addEventListener("visibilitychange", tick);
})();
