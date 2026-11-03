/* Production Suite thin client.
 *
 * The server builds every screen as HTML and decides everything on it. This script only:
 *   - asks the server every few seconds whether anything changed, and if so lets the page's regions re-load
 *     themselves (htmx swaps in the server's new HTML; regions say what to load with hx-get);
 *   - never replaces a region while someone is typing in it (it is refreshed as soon as they leave it);
 *   - shows the server's messages (toasts) and its dialogs (modals);
 *   - ticks clocks and timers the server put on the page (data-since / data-clock), so seconds move between refreshes;
 *   - keeps wall screens awake.
 * There are no plant rules here: what a screen shows, when something is due and what a button does are all
 * decided on the server.
 */
"use strict";
(function () {
  const body = document.body;
  const $ = s => document.querySelector(s);
  htmx.config.allowEval = false;          // no inline JavaScript from attributes
  htmx.config.allowScriptTags = false;    // swapped HTML never runs scripts
  htmx.config.historyCacheSize = 0;
  htmx.config.defaultSwapStyle = "outerHTML";
  htmx.config.timeout = 20000;

  // ------------------------------------------------------------ server clock
  let offset = 0;  // server time minus this screen's time
  function setNow(ms) { if (ms) offset = +ms - Date.now(); }
  setNow(body.dataset.now);
  const now = () => Date.now() + offset;

  // ------------------------------------------------------------ CSRF for every request htmx makes
  function csrf() { const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ""; }
  body.addEventListener("htmx:configRequest", e => { e.detail.headers["X-CSRFToken"] = csrf(); });

  // ------------------------------------------------------------ "has anything changed?"
  const watchUrl = body.dataset.watch;               // e.g. /thin/watch?scope=data
  const every = +(body.dataset.every || 3000);
  let data = null, config = null, online = true, timer = null;
  function setOnline(ok) {
    if (ok === online) return;
    online = ok;
    body.classList.toggle("offline", !ok);
    htmx.trigger(body, "suite:online", { ok });
  }
  async function watch() {
    clearTimeout(timer);
    try {
      const r = await fetch(watchUrl, { cache: "no-store", credentials: "same-origin" });
      if (r.status === 401) { location.reload(); return; }
      const j = await r.json();
      setOnline(true); setNow(j.now);
      if (config !== null && j.config !== config) htmx.trigger(body, "suite:config");
      else if (data !== null && j.data !== data) htmx.trigger(body, "suite:changed");
      data = j.data; config = j.config;
    } catch (e) { setOnline(false); }
    timer = setTimeout(watch, every);
  }
  if (watchUrl) watch();
  // after any action of this screen, refresh straight away instead of waiting for the next check
  body.addEventListener("suite:changed-now", () => { data = null; htmx.trigger(body, "suite:changed"); watch(); });

  // ------------------------------------------------------------ don't swap a region under someone's fingers
  const typing = el => el && el.matches && el.matches("input:not([type=checkbox]):not([type=radio]):not([type=file]),textarea,select");
  const held = new Set();
  body.addEventListener("htmx:beforeSwap", e => {
    const t = e.detail.target, a = document.activeElement;
    if (e.detail.requestConfig && e.detail.requestConfig.triggeringEvent && e.detail.requestConfig.triggeringEvent.type.startsWith("suite:")
        && t && typing(a) && t.contains(a)) {
      e.detail.shouldSwap = false;
      held.add(t.id || t);
    }
  });
  document.addEventListener("focusout", () => setTimeout(() => {
    if (!held.size || typing(document.activeElement)) return;
    held.clear();
    htmx.trigger(body, "suite:changed");
  }, 0));

  // ------------------------------------------------------------ offline and errors
  body.addEventListener("htmx:sendError", () => { setOnline(false); toast("The server is not answering. Check the network and try again.", "bad"); });
  body.addEventListener("htmx:responseError", e => {
    if (e.detail.xhr.status === 401) { location.reload(); return; }
    toast("The server hit an error and nothing was changed. Try again; if it keeps happening, tell the shift lead.", "bad");
  });

  // ------------------------------------------------------------ messages from the server (HX-Trigger: {"suite:toast": ...})
  function toast(msg, kind) {
    if (!msg) return;
    let t = $(".toast");
    if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); body.appendChild(t); }
    t.textContent = msg; t.className = "toast show " + (kind || "");
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), kind === "bad" ? 6000 : 3200);
  }
  body.addEventListener("suite:toast", e => { const d = e.detail || {}; toast(d.value || d.text || d.msg, d.kind); });

  // ------------------------------------------------------------ dialogs: the server sends the HTML into #modal
  const modal = () => $("#modal");
  function openModal() { const m = modal(); if (!m) return; m.hidden = false; const f = m.querySelector("[autofocus]"); if (f) setTimeout(() => f.focus(), 30); }
  function closeModal() { const m = modal(); if (!m) return; m.hidden = true; m.innerHTML = ""; }
  body.addEventListener("htmx:afterSwap", e => { if (e.detail.target && e.detail.target.id === "modal") openModal(); tick(); });
  body.addEventListener("suite:closeModal", closeModal);
  document.addEventListener("click", e => {
    if (e.target.closest("[data-close]")) { e.preventDefault(); closeModal(); }
    else if (e.target.id === "modal") closeModal();
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && modal() && !modal().hidden) closeModal(); });

  // ------------------------------------------------------------ clocks and timers the server placed on the page
  const pad = n => String(n).padStart(2, "0");
  function fmt(ms, how) {
    if (how === "clock") {
      const d = new Date(ms); let h = d.getHours(); const ap = h >= 12 ? "pm" : "am"; h = h % 12 || 12;
      return `${h}:${pad(d.getMinutes())}<small> ${ap}</small>`;
    }
    const s = Math.max(0, Math.floor(ms / 1000));
    return `${Math.floor(s / 3600)}:${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
  }
  function tick() {
    const t = now();
    document.querySelectorAll("[data-clock]").forEach(el => { el.innerHTML = fmt(t, "clock"); });
    document.querySelectorAll("[data-since]").forEach(el => { el.textContent = fmt(t - +el.dataset.since, "timer"); });
  }
  setInterval(tick, 1000); tick();

  // ------------------------------------------------------------ wall screens stay awake where the browser allows it
  if (body.dataset.awake && navigator.wakeLock) {
    const wake = () => navigator.wakeLock.request("screen").catch(() => {});
    wake(); document.addEventListener("visibilitychange", () => { if (!document.hidden) wake(); });
  }

  window.Suite = { toast, closeModal, now };
})();
