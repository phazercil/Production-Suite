/* Production Suite: small things every page shares.
 *   - the top bar's More and sign-in menus (one sign-in place on every screen)
 *   - an on-screen PIN keypad under every PIN box (touch screens with no keyboard), sending by itself once the PIN
 *     is as long as everyone's (when all PINs are one length) and on a badge scanner's Enter
 *   - Undo for a few things that are easy to do by mistake: a toast with an Undo button for 8 seconds
 *   - keyboard: "/" goes to the page's search box, Esc closes menus (dialogs already close on Esc)
 */
(function () {
  "use strict";
  var UNDO_MS = 8000;
  function csrf() { var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ""; }
  function post(url, body) {
    return fetch(url, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(body || {}) }).then(function (r) { return r.json().catch(function () { return { ok: r.ok }; }); });
  }
  function pinLens() {
    var m = document.querySelector('meta[name="pin-lens"]');
    return m ? m.content.split(",").filter(Boolean).map(Number) : [];
  }

  // ------------------------------------------------------------ top bar menus
  function placeMenu(d) {
    var menu = d.querySelector(".suitemenu"), r = d.querySelector("summary").getBoundingClientRect();
    if (!menu) return;
    menu.style.top = (r.bottom) + "px";
    if (menu.classList.contains("right")) { menu.style.right = Math.max(8, innerWidth - r.right) + "px"; menu.style.left = "auto"; }
    else menu.style.left = Math.max(8, r.left) + "px";
  }
  document.addEventListener("toggle", function (e) {
    var d = e.target;
    if (!d.matches || !d.matches(".suitemore,.suiteuser")) return;
    if (d.open) { document.querySelectorAll(".suitemore[open],.suiteuser[open]").forEach(function (x) { if (x !== d) x.open = false; }); placeMenu(d); }
  }, true);
  document.addEventListener("click", function (e) {
    document.querySelectorAll(".suitemore[open],.suiteuser[open]").forEach(function (d) { if (!d.contains(e.target)) d.open = false; });
    var t = e.target.closest("[data-suite-logout],[data-suite-signin]");
    if (!t) return;
    if (t.hasAttribute("data-suite-logout")) post("/api/hmi/logout").finally(function () { location.reload(); });
    else signIn();
  });

  // ------------------------------------------------------------ sign in from the bar
  // switching person: pick the name, then the PIN (both are checked on the server)
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return "&#" + c.charCodeAt(0) + ";"; }); }
  function signIn() {
    var box = document.createElement("div");
    box.className = "suite-signin";
    box.innerHTML = '<form><h2 style="margin:0 0 8px">Switch person</h2><p class="note" style="margin:0 0 8px">Pick your name, then enter your PIN.</p>' +
      '<div class="suite-people" role="radiogroup" aria-label="Who are you?"><span class="note">Loading names…</span></div>' +
      '<input type="password" name="pin" inputmode="numeric" autocomplete="off" placeholder="PIN" aria-label="PIN" disabled>' +
      '<p class="err" aria-live="polite"></p><div style="display:flex;gap:8px;margin-top:8px"><button type="submit" class="btn go">Sign in</button><button type="button" class="btn ghost" data-x>Cancel</button></div></form>';
    document.body.appendChild(box);
    var f = box.querySelector("form"), inp = f.querySelector("input[name=pin]"), err = f.querySelector(".err"), list = f.querySelector(".suite-people");
    var switching = true;
    fetch("/api/hmi/people", { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (r) {
      if (!r.ok) {   // nobody signed in on this screen: not a switch, the PIN (or badge) alone signs in
        switching = false; list.remove(); f.querySelector("h2").textContent = "Sign in"; f.querySelector("p.note").remove();
        inp.disabled = false; inp.placeholder = "PIN or badge"; inp.focus();
        return;
      }
      list.innerHTML = (r.people || []).map(function (u) {
        return '<label><input type="radio" name="who" value="' + u.id + '"><span>' + esc(u.name) + '<small>' + esc(u.role || "") + '</small></span></label>';
      }).join("") || '<span class="note">No people set up yet.</span>';
    }).catch(function () { list.innerHTML = '<span class="err">Couldn\'t load the names.</span>'; });
    list.addEventListener("change", function () { inp.disabled = false; err.textContent = ""; inp.focus(); });
    function who() { var c = f.querySelector("input[name=who]:checked"); return c ? c.value : ""; }
    function close() { box.remove(); }
    box.addEventListener("click", function (e) { if (e.target === box || e.target.closest("[data-x]")) close(); });
    box.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      if (switching && !who()) { err.textContent = "Pick your name first."; return; }
      post("/api/hmi/login", switching ? { pin: inp.value, userId: who(), "switch": true } : { pin: inp.value }).then(function (r) {
        if (r && r.ok) location.reload(); else { err.textContent = (r && r.error) || "That PIN doesn't match."; inp.value = ""; inp.focus(); }
      });
    });
    keypad(inp);
  }

  // ------------------------------------------------------------ PIN keypad
  function keypad(inp) {
    if (!inp || inp.dataset.keypad === "on" || inp.dataset.keypad === "off") return;
    inp.dataset.keypad = "on";
    var pad = document.createElement("div");
    pad.className = "keypad";
    pad.setAttribute("aria-label", "Number pad");
    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "Clear", "0", "⌫"].forEach(function (k) {
      var b = document.createElement("button");
      b.type = "button"; b.textContent = k; b.dataset.k = k;
      if (k.length > 1) b.className = "k-sm";
      if (k === "⌫") b.setAttribute("aria-label", "Delete");
      pad.appendChild(b);
    });
    (inp.closest("label") || inp).insertAdjacentElement("afterend", pad);
    pad.addEventListener("pointerdown", function (e) { e.preventDefault(); });   // keep the box focused (no OS keyboard)
    pad.addEventListener("click", function (e) {
      var b = e.target.closest("button"); if (!b) return;
      var k = b.dataset.k;
      if (k === "Clear") inp.value = "";
      else if (k === "⌫") inp.value = inp.value.slice(0, -1);
      else inp.value += k;
      inp.dispatchEvent(new Event("input", { bubbles: true }));
      var lens = pinLens();
      if (/^\d+$/.test(k) && lens.length === 1 && inp.value.length === lens[0] && inp.form) {
        if (inp.form.requestSubmit) inp.form.requestSubmit(); else inp.form.submit();
      }
    });
  }
  function pads(root) {
    (root || document).querySelectorAll('input[name="pin"]').forEach(keypad);
  }
  document.addEventListener("DOMContentLoaded", function () { pads(); undoFromUrl(); });
  document.addEventListener("htmx:afterSwap", function (e) { pads(e.target); });

  // ------------------------------------------------------------ undo
  var undoEl = null, undoTimer = null;
  function undo(text, run) {
    if (undoEl) undoEl.remove();
    clearTimeout(undoTimer);
    undoEl = document.createElement("div");
    undoEl.className = "suite-undo"; undoEl.setAttribute("role", "status");
    undoEl.innerHTML = "<span></span><button type=\"button\">Undo</button>";
    undoEl.querySelector("span").textContent = text;
    undoEl.querySelector("button").onclick = function () { var x = undoEl; undoEl = null; x.remove(); clearTimeout(undoTimer); run(); };
    document.body.appendChild(undoEl);
    undoTimer = setTimeout(function () { if (undoEl) { undoEl.remove(); undoEl = null; } }, UNDO_MS);
  }
  window.suiteUndo = undo;
  // from the server: HX-Trigger {"suite:undo": {"text", "post", "vals"}} (htmx screens), or ?undo=URL&undoText=… after a form
  document.addEventListener("suite:undo", function (e) {
    var d = e.detail || {};
    undo(d.text || "Done.", function () {
      fetch(d.post, { method: "POST", credentials: "same-origin", headers: { "X-CSRFToken": csrf(), "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(d.vals || {}) }).then(function () { if (window.htmx) htmx.trigger(document.body, "suite:changed-now"); else location.reload(); });
    });
  });
  function undoFromUrl() {
    var q = new URLSearchParams(location.search), url = q.get("undo");
    if (!url || url.charAt(0) !== "/" || url.charAt(1) === "/") return;
    var text = q.get("undoText") || "Done.";
    q.delete("undo"); q.delete("undoText");
    history.replaceState(null, "", location.pathname + (q.toString() ? "?" + q : "") + location.hash);
    undo(text, function () {
      var f = document.createElement("form");
      f.method = "post"; f.action = url;
      f.innerHTML = '<input type="hidden" name="csrfmiddlewaretoken">';
      f.firstChild.value = csrf();
      document.body.appendChild(f); f.submit();
    });
  }

  // ------------------------------------------------------------ keyboard
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.querySelectorAll(".suitemore[open],.suiteuser[open]").forEach(function (d) { d.open = false; });
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    var a = document.activeElement;
    if (a && (/^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) || a.isContentEditable)) return;
    var s = document.querySelector("[data-search]:not([disabled]), .mode input[type=search]:not([disabled])");
    if (s && s.offsetParent !== null) { e.preventDefault(); s.focus(); if (s.select) s.select(); }
  });
  // a button or link that asks first (data-confirm="…"); the page's own scripts can't be inline (Content-Security-Policy)
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-confirm]");
    if (b && !window.confirm(b.getAttribute("data-confirm"))) { e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);
})();
// each section's tab has its own address: /supervisor/?tab=down shows as /supervisor/down/, Admin's open tab as /admin/backup/
(function () {
  function fix() {
    var u = new URL(location.href), t = u.searchParams.get("tab");
    var m = location.pathname.match(/^\/(supervisor|quality|maintenance|sensors|admin)\/(?:([\w-]+)\/)?$/);
    if (!m) return;
    if (m[1] === "admin") {
      var el = document.querySelector("[data-admin-tab]");
      t = el && el.getAttribute("data-admin-tab");
    }
    if (!t || !/^[\w-]+$/.test(t)) return;
    u.searchParams.delete("tab");
    var want = "/" + m[1] + "/" + t + "/" + u.search + u.hash;
    if (location.pathname + location.search + location.hash !== want) history.replaceState(history.state, "", want);
  }
  fix();
  document.addEventListener("htmx:afterSettle", fix);
})();
// a row a link pointed to (a notice's MO on the kiosk schedule): bring it into view once
document.addEventListener("htmx:afterSettle", function () {
  var el = document.querySelector("[data-scroll-here]");
  if (el) { el.removeAttribute("data-scroll-here"); el.scrollIntoView({ block: "center" }); }
});
// Admin › Connections: show one section (Status, Machine data, S2K…) and one machine's data at a time, and find /
// sort the status table. The choice is kept in the address (?sec=machines&mach=Salvatore) and for the session.
(function () {
  "use strict";
  function store(k, v) { try { if (v === undefined) return sessionStorage.getItem(k) || ""; sessionStorage.setItem(k, v); } catch (e) { return ""; } }
  function want(key, sk) {
    var u = new URL(location.href);
    return u.searchParams.has(key) ? u.searchParams.get(key) || "" : store(sk);
  }
  function remember(key, sk, v) {
    store(sk, v);
    var u = new URL(location.href);
    if (v) u.searchParams.set(key, v); else u.searchParams.delete(key);
    history.replaceState(history.state, "", u.pathname + u.search + u.hash);
  }
  function apply() {
    var nav = document.querySelector("[data-csecnav]");
    if (!nav) return;
    var sec = want("sec", "csec"), have = nav.querySelector('[data-csec-go="' + sec + '"]');
    if (!have) sec = "";
    nav.querySelectorAll("[data-csec-go]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-csec-go") === sec)); });
    document.querySelectorAll("[data-csec]").forEach(function (el) { el.hidden = !!sec && el.getAttribute("data-csec") !== sec; });
    var mach = want("mach", "cmach"), mnav = document.querySelector("[data-cmachnav]");
    if (mnav && !mnav.querySelector('[data-cmach-go="' + CSS.escape(mach) + '"]')) mach = "";
    if (mnav) mnav.querySelectorAll("[data-cmach-go]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.getAttribute("data-cmach-go") === mach)); });
    document.querySelectorAll("[data-cmach]").forEach(function (el) { el.hidden = !!mach && el.getAttribute("data-cmach") !== mach; });
    table();
  }
  function table() {
    var t = document.querySelector("[data-ctable]");
    if (!t) return;
    var q = ((document.querySelector("[data-cfind]") || {}).value || "").trim().toLowerCase();
    var kind = (document.querySelector("[data-ckind]") || {}).value || "";
    var sort = (document.querySelector("[data-csort]") || {}).value || "";
    var body = t.tBodies[0], rows = Array.prototype.slice.call(body.querySelectorAll("[data-crow]"));
    var by = {
      "": function (a, b) { return a.dataset.i - b.dataset.i; },
      name: function (a, b) { return a.dataset.name.localeCompare(b.dataset.name); },
      problem: function (a, b) { return (a.dataset.state - b.dataset.state) || (a.dataset.i - b.dataset.i); },
      kind: function (a, b) { return a.dataset.kind.localeCompare(b.dataset.kind) || (a.dataset.i - b.dataset.i); }
    }[sort] || function (a, b) { return a.dataset.i - b.dataset.i; };
    rows.sort(by).forEach(function (r) {
      body.appendChild(r);
      r.hidden = (kind && r.dataset.kind !== kind) || (q && r.textContent.toLowerCase().indexOf(q) < 0);
    });
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-csec-go], [data-cmach-go], [data-cmach-open]");
    if (!b) return;
    if (b.hasAttribute("data-csec-go")) remember("sec", "csec", b.getAttribute("data-csec-go"));
    if (b.hasAttribute("data-cmach-go")) remember("mach", "cmach", b.getAttribute("data-cmach-go"));
    if (b.hasAttribute("data-cmach-open")) {   // a machine's row in Connection status: open its machine data
      remember("sec", "csec", "machines");
      remember("mach", "cmach", b.getAttribute("data-cmach-open"));
      apply();
      var nav = document.querySelector("[data-csecnav]");
      if (nav) nav.scrollIntoView({ block: "start" });
      return;
    }
    apply();
  });
  document.addEventListener("input", function (e) { if (e.target.matches && e.target.matches("[data-cfind]")) table(); });
  document.addEventListener("change", function (e) { if (e.target.matches && e.target.matches("[data-ckind], [data-csort]")) table(); });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", apply); else apply();
  document.addEventListener("htmx:afterSettle", apply);
})();
// the bar's plant clock: the server's time when the page was made, then kept by this screen's clock (no requests)
(function () {
  "use strict";
  var el = document.querySelector("[data-clock]");
  if (!el) return;
  var skew = (+el.getAttribute("data-now") || Date.now()) - Date.now();
  var opts = { hour: "numeric", minute: "2-digit" };
  try { new Intl.DateTimeFormat(undefined, { timeZone: el.getAttribute("data-tz") }); opts.timeZone = el.getAttribute("data-tz"); } catch (e) { /* the screen's own zone */ }
  var fmt = new Intl.DateTimeFormat(undefined, opts);
  function tick() { el.textContent = fmt.format(new Date(Date.now() + skew)); }
  tick();
  setInterval(tick, 15000);
})();
// a page part that should fill the rest of the screen (Quality › 3D viewer): sized to what's left below it
(function () {
  "use strict";
  function fit() {
    document.querySelectorAll("[data-fit-height]").forEach(function (el) {
      var scroller = el.closest(".mode") || document.documentElement;
      var top = el.getBoundingClientRect().top - scroller.getBoundingClientRect().top + (scroller.scrollTop || 0);
      var room = (scroller.clientHeight || window.innerHeight) - top - 16;
      el.style.height = Math.max(360, room) + "px";
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fit); else fit();
  window.addEventListener("resize", fit);
})();
