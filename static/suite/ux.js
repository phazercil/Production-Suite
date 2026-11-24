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
  function signIn() {
    var box = document.createElement("div");
    box.className = "suite-signin";
    box.innerHTML = '<form><h2 style="margin:0 0 8px">Sign in</h2><input type="password" name="pin" inputmode="numeric" autocomplete="off" placeholder="PIN or badge" aria-label="PIN" autofocus>' +
      '<p class="err" aria-live="polite"></p><div style="display:flex;gap:8px;margin-top:8px"><button type="submit" class="btn go">Sign in</button><button type="button" class="btn ghost" data-x>Cancel</button></div></form>';
    document.body.appendChild(box);
    var f = box.querySelector("form"), inp = f.querySelector("input"), err = f.querySelector(".err");
    function close() { box.remove(); }
    box.addEventListener("click", function (e) { if (e.target === box || e.target.closest("[data-x]")) close(); });
    box.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      post("/api/hmi/login", { pin: inp.value }).then(function (r) {
        if (r && r.ok) location.reload(); else { err.textContent = (r && r.error) || "That PIN doesn't match."; inp.value = ""; inp.focus(); }
      });
    });
    keypad(inp);
    setTimeout(function () { inp.focus(); }, 30);
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
// a row a link pointed to (a notice's MO on the kiosk schedule): bring it into view once
document.addEventListener("htmx:afterSettle", function () {
  var el = document.querySelector("[data-scroll-here]");
  if (el) { el.removeAttribute("data-scroll-here"); el.scrollIntoView({ block: "center" }); }
});
