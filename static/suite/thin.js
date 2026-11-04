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

  const held = new Set();
  // ------------------------------------------------------------ the person's own actions come first
  // While a button or form of this screen is waiting for the server, automatic refreshes wait too: a refresh
  // replacing the dialog would make the browser drop the answer to the button that was pressed.
  let busy = 0;
  const auto = e => { const t = e.detail && e.detail.requestConfig && e.detail.requestConfig.triggeringEvent; return !!(t && t.type && t.type.startsWith("suite:")); };
  body.addEventListener("htmx:beforeRequest", e => {
    if (auto(e)) { if (busy) { e.preventDefault(); held.add("busy"); } return; }
    if ((e.detail.requestConfig.verb || "get") === "get") return;
    busy++;
    // counted on the request itself: the button that sent it may be gone from the page by the time it finishes
    e.detail.xhr.addEventListener("loadend", () => {
      busy = Math.max(0, busy - 1);
      if (!busy && held.has("busy")) { held.delete("busy"); setTimeout(() => htmx.trigger(body, "suite:changed"), 0); }
    });
  });

  // ------------------------------------------------------------ don't swap a region under someone's fingers
  const typing = el => el && el.matches && el.matches("input:not([type=checkbox]):not([type=radio]):not([type=file]),textarea,select");
  body.addEventListener("htmx:beforeSwap", e => {
    const t = e.detail.target, a = document.activeElement;
    if (auto(e) && (busy || (t && typing(a) && t.contains(a)) || (t && (t.matches("form[data-dirty],[data-hold]") || t.querySelector("form[data-dirty],[data-hold]"))))) {
      e.detail.shouldSwap = false;
      held.add(t.id || t);
    }
  });
  // a form someone has typed into but not sent yet, or a panel showing a result someone asked for ([data-hold]: a test,
  // a new key, an open editor), is never replaced by an automatic refresh
  document.addEventListener("input", e => { const f = e.target.closest && e.target.closest("form"); if (f && !f.hasAttribute("data-autosave")) f.dataset.dirty = "1"; });
  // a form the server took (not one it refused) has nothing unsaved any more, so refreshes may replace it again
  body.addEventListener("htmx:afterRequest", e => {
    const d = e.detail, f = d.elt && d.elt.closest && d.elt.closest("form");
    if (!f || !d.successful || (d.requestConfig && d.requestConfig.verb === "get")) return;
    if (/"kind":\s*"bad"/.test(d.xhr.getResponseHeader("HX-Trigger") || "")) return;
    delete f.dataset.dirty;
    if (f.matches("[data-reset]")) f.reset();
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
  function openModal() { const m = modal(); if (!m || !m.innerHTML.trim()) { closeModal(); return; } m.hidden = false; const f = m.querySelector("[autofocus]"); if (f) setTimeout(() => f.focus(), 30); }
  function closeModal() { const m = modal(); if (!m) return; m.hidden = true; m.innerHTML = ""; }
  body.addEventListener("htmx:afterSwap", e => { if (e.detail.target && e.detail.target.id === "modal") openModal(); tick(); });
  body.addEventListener("suite:closeModal", closeModal);
  // a dialog button marked data-close-after closes its dialog once the server has accepted it
  body.addEventListener("htmx:afterRequest", e => { const el = e.detail.elt; if (el && el.hasAttribute && el.hasAttribute("data-close-after") && e.detail.successful) closeModal(); });
  document.addEventListener("click", e => {
    if (e.target.closest("[data-close]")) { e.preventDefault(); closeModal(); }
    else if (e.target.id === "modal") closeModal();
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && modal() && !modal().hidden) closeModal(); });

  // ------------------------------------------------------------ a part the server marked "open this dialog" (only if none is open)
  body.addEventListener("htmx:afterSettle", () => {
    const o = document.querySelector("[data-open-modal]");
    if (!o) return;
    const url = o.dataset.openModal; o.removeAttribute("data-open-modal");
    if (modal() && modal().hidden) htmx.ajax("GET", url, { target: "#modal", swap: "innerHTML" });
  });
  // the server's verdict on a field as it is typed (e.g. a QC measurement in or out of tolerance)
  body.addEventListener("suite:mark", e => {
    const d = e.detail || {}, el = d.id && document.getElementById(d.id);
    if (el) { el.classList.remove("in", "out"); if (d.cls) el.classList.add(d.cls); }
  });
  // keep a scrolled strip (e.g. the schedule timeline) where it was when the server sends it again
  const scrolls = {};
  body.addEventListener("htmx:beforeSwap", () => document.querySelectorAll("[data-keep-scroll]").forEach(el => { scrolls[el.dataset.keepScroll] = el.scrollLeft; }));
  body.addEventListener("htmx:afterSettle", () => document.querySelectorAll("[data-keep-scroll]").forEach(el => {
    const k = el.dataset.keepScroll;
    el.scrollLeft = k in scrolls ? scrolls[k] : +(el.dataset.scrollTo || 0);
  }));
  // "Copy" buttons: copy the text the server put on the page (plain http pages have no clipboard API, so fall back)
  document.addEventListener("click", e => {
    const b = e.target.closest && e.target.closest("[data-copy]");
    if (!b) return;
    const el = document.querySelector(b.dataset.copy); if (!el) return;
    const text = el.value !== undefined && el.tagName !== "PRE" ? el.value : el.textContent;
    const done = ok => toast(ok ? "Copied" : "Select the text and copy it", ok ? "" : "bad");
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(() => done(true), () => done(legacyCopy(text))); }
    else done(legacyCopy(text));
  });
  function legacyCopy(text) {
    const ta = document.createElement("textarea"); ta.value = text; ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0";
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
    let ok = false; try { ok = document.execCommand("copy"); } catch (x) { ok = false; }
    ta.remove(); return ok;
  }
  // show how many pictures (files) are picked in a form, before it is sent
  document.addEventListener("change", e => {
    const f = e.target.closest && e.target.closest("form");
    if (!f || e.target.type !== "file") return;
    const n = [...f.querySelectorAll("input[type=file]")].reduce((a, i) => a + (i.files ? i.files.length : 0), 0);
    f.querySelectorAll("[data-files-note]").forEach(el => { el.textContent = n ? `${n} picture${n > 1 ? "s" : ""} ready to send with the ticket.` : el.dataset.filesNote; });
  });
  // keep a shift lead's session alive while they use the screen
  let pinged = 0;
  ["pointerdown", "keydown"].forEach(ev => document.addEventListener(ev, () => {
    if (body.dataset.ping && Date.now() - pinged > 30000) { pinged = Date.now(); fetch(body.dataset.ping, { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": csrf() } }).catch(() => {}); }
  }, true));

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

  window.Suite = { toast, closeModal, now, state: () => ({ busy, held: [...held] }) };
})();
