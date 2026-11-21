/* Camera pictures: every <img data-cam="ID" data-every="ms"> on the page shows that camera's latest picture from the
   server (/api/cameras/ID/frame), one fetch at a time per picture, only while the page is on screen. A picture that
   can't be had shows why ([data-cam-msg] beside it). Tap a tile ([data-cam-tile]) to make it big; tap again to go back. */
(function () {
  if (window.__suiteCams) return;
  window.__suiteCams = true;
  var busy = new WeakMap(), due = new WeakMap(), urls = new WeakMap();
  function msgFor(img) { var t = img.closest("[data-cam-tile]"); return t ? t.querySelector("[data-cam-msg]") : null; }
  function say(img, text, stale) {
    var m = msgFor(img);
    if (m) { m.textContent = text || ""; m.hidden = !text; m.classList.toggle("stale", !!stale); }
  }
  function load(img) {
    busy.set(img, true);
    fetch("/api/cameras/" + img.dataset.cam + "/frame?t=" + Date.now(), { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(t || ("No picture (" + r.status + ")")); });
        var age = +(r.headers.get("X-Frame-Age") || 0), err = r.headers.get("X-Frame-Error");
        return r.blob().then(function (b) {
          var old = urls.get(img), u = URL.createObjectURL(b);
          img.src = u; urls.set(img, u);
          if (old) setTimeout(function () { URL.revokeObjectURL(old); }, 2000);
          img.classList.remove("nopic");
          say(img, err ? "Last picture " + age + " s ago: " + err : (age > 15 ? "Last picture " + age + " s ago" : ""), !!err || age > 15);
        });
      })
      .catch(function (e) { img.classList.add("nopic"); img.dataset.failed = "1"; say(img, String(e.message || e).slice(0, 300), true); })
      .then(function () {
        busy.delete(img);
        var wait = +img.dataset.every || 1000;
        if (img.classList.contains("nopic")) wait = Math.max(wait, 5000);   // no picture: ask again every 5 s
        due.set(img, Date.now() + wait);
      });
  }
  function tick() {
    if (!document.hidden) {
      var now = Date.now();
      document.querySelectorAll("img[data-cam]").forEach(function (img) {
        if (busy.get(img) || (due.get(img) || 0) > now || !img.isConnected) return;
        var r = img.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) return;   // hidden
        load(img);
      });
    }
    setTimeout(tick, 200);
  }
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-cam-tile]");
    if (!t || e.target.closest("a,button,form")) return;
    var on = !t.classList.contains("big");
    document.querySelectorAll("[data-cam-tile].big").forEach(function (x) { x.classList.remove("big"); });
    if (on) t.classList.add("big");
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.querySelectorAll("[data-cam-tile].big").forEach(function (x) { x.classList.remove("big"); });
  });
  tick();
})();
