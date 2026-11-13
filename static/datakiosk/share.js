// Data Kiosk: copy the view-only link. The plant server is plain http, where the browser's clipboard API isn't
// offered, so the link box is selected and copied the old way when it isn't.
(function () {
  var btn = document.getElementById("dk-copy"), box = document.getElementById("dk-share");
  if (!btn || !box) return;
  function done(ok) {
    btn.textContent = ok ? "Copied" : "Select and copy";
    btn.classList.toggle("done", ok);
    setTimeout(function () { btn.textContent = "Copy link"; btn.classList.remove("done"); }, 2500);
  }
  function fallback() {
    box.focus(); box.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    done(ok);
  }
  box.addEventListener("focus", function () { box.select(); });
  btn.addEventListener("click", function () {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(box.value).then(function () { done(true); }, fallback);
    } else {
      fallback();
    }
  });
})();
