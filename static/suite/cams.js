/* Camera pictures: every <img data-cam="ID" data-every="ms"> on the page shows that camera's latest picture from the
   server (/api/cameras/ID/frame), one fetch at a time per picture, only while the page is on screen. A picture that
   can't be had shows why ([data-cam-msg] beside it). Tap a tile ([data-cam-tile]) to make it big; tap again to go back.

   Live video: a <video data-live="ID"> in the tile plays the camera's own video from /video/ws?src=camID (go2rtc,
   passed on by Apache: cameras/live.py), using Media Source Extensions. While it plays, the pictures stop; if it can't
   play (no live video on this server, a codec the tablet can't play) the tile keeps showing pictures and tries live
   video again later. */
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
  // ------------------------------------------------------------ live video
  var CODECS = ["avc1.640029", "avc1.64002A", "avc1.640033", "avc1.4D401F", "avc1.42E01E", "hvc1.1.6.L153.B0", "hev1.1.6.L153.B0"];
  var lives = new Map();   // video element -> its connection
  function liveCodecs() {
    if (!window.MediaSource) return "";
    return CODECS.filter(function (c) { return MediaSource.isTypeSupported('video/mp4; codecs="' + c + '"'); }).join(",");
  }
  function startLive(v) {
    var codecs = liveCodecs();
    if (!codecs) return;   // no Media Source on this browser: pictures only
    var st = lives.get(v) || { fails: 0, next: 0 };
    lives.set(v, st);
    if (st.ws || Date.now() < st.next) return;
    var tile = v.closest("[data-cam-tile]"), img = tile && tile.querySelector("img[data-cam]");
    var ms = new MediaSource(), sb = null, q = [], mime = "", playing = false;
    var ws = st.ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/video/ws?src=cam" + v.dataset.live);
    ws.binaryType = "arraybuffer";
    var wait = setTimeout(function () { ws.close(); }, 10000);   // no video within 10 s: pictures
    function pump() {
      if (!sb || sb.updating) return;
      try {
        if (q.length) { sb.appendBuffer(q.shift()); return; }
        var b = v.buffered;
        if (b.length && v.currentTime - b.start(0) > 30) sb.remove(b.start(0), v.currentTime - 10);   // keep the last 10 s
      } catch (e) { q.length = 0; }
    }
    function edge() {   // stay at the live edge
      var b = v.buffered;
      if (!b.length) return;
      var end = b.end(b.length - 1);
      if (end - v.currentTime > 1.5 || v.currentTime < b.start(0)) v.currentTime = Math.max(b.start(0), end - 0.3);
      if (v.paused) v.play().catch(function () {});
    }
    ms.addEventListener("sourceopen", function () {
      try { sb = ms.addSourceBuffer(mime); sb.mode = "segments"; } catch (e) { ws.close(); return; }
      sb.addEventListener("updateend", function () { edge(); pump(); });
      pump();
    }, { once: true });
    ws.onopen = function () { ws.send(JSON.stringify({ type: "mse", value: codecs })); };
    ws.onmessage = function (e) {
      if (!v.isConnected || document.hidden) { ws.close(); return; }
      if (typeof e.data === "string") {
        var m = {};
        try { m = JSON.parse(e.data); } catch (x) { /* not ours */ }
        if (m.type === "mse" && !mime) { mime = m.value; v.src = URL.createObjectURL(ms); }
        else if (m.type === "error") { st.err = m.value; ws.close(); }
        return;
      }
      q.push(e.data);
      if (q.length > 60) q.splice(0, q.length - 60);   // fallen behind: drop the oldest
      pump();
    };
    v.onplaying = function () {
      if (playing) return;
      playing = true; clearTimeout(wait); st.fails = 0;
      v.hidden = false;
      if (img) img.dataset.paused = "1";
      say(img || v, "");
    };
    ws.onclose = function () {
      clearTimeout(wait);
      st.ws = null;
      try { if (ms.readyState === "open") ms.endOfStream(); } catch (e) { /* closed already */ }
      if (v.src) { URL.revokeObjectURL(v.src); v.removeAttribute("src"); v.load(); }
      v.hidden = true;
      if (img) delete img.dataset.paused;   // pictures again
      if (!v.isConnected || document.hidden) { st.next = 0; return; }
      st.fails = playing ? 0 : st.fails + 1;
      st.next = Date.now() + (playing ? 2000 : st.fails < 3 ? 4000 : 60000);
    };
  }
  function liveTick() {
    lives.forEach(function (st, v) {
      if (!v.isConnected) { if (st.ws) st.ws.close(); lives.delete(v); }
      else if (document.hidden && st.ws) st.ws.close();
    });
    if (!document.hidden) document.querySelectorAll("video[data-live]").forEach(function (v) { if (v.isConnected) startLive(v); });
  }

  function tick() {
    liveTick();
    if (!document.hidden) {
      var now = Date.now();
      document.querySelectorAll("img[data-cam]").forEach(function (img) {
        if (img.dataset.paused || busy.get(img) || (due.get(img) || 0) > now || !img.isConnected) return;
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
