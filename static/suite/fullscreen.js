// Full screen: the button in the top bar (the kiosk's left rail; the corner on screens without either) fills the
// whole display with the suite, for a kiosk or a TV. Esc, or the button again, leaves it. A reload leaves it too.
// Buttons may come and go (the kiosk redraws its rail), so clicks are caught on the page and the look is set by
// classes on <html>: fs-can (this browser can), fs-on (full screen now).
(function () {
  var d = document, root = d.documentElement;
  if (!(root.requestFullscreen || root.webkitRequestFullscreen)) return;   // can't: the buttons stay hidden
  root.classList.add("fs-can");
  function on() { return !!(d.fullscreenElement || d.webkitFullscreenElement); }
  function sync() {
    var full = on();
    root.classList.toggle("fs-on", full);
    d.querySelectorAll("[data-fullscreen]").forEach(function (b) {
      b.setAttribute("aria-pressed", full ? "true" : "false");
      b.title = full ? "Leave full screen (Esc)" : "Full screen (Esc leaves it)";
      var s = b.querySelector("span");
      if (s) s.textContent = full ? "Exit full screen" : "Full screen";
    });
  }
  d.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-fullscreen]");
    if (!b) return;
    e.preventDefault();
    if (on()) (d.exitFullscreen || d.webkitExitFullscreen).call(d);
    else { var r = (root.requestFullscreen || root.webkitRequestFullscreen).call(root); if (r && r.catch) r.catch(function () {}); }
  });
  d.addEventListener("fullscreenchange", sync);
  d.addEventListener("webkitfullscreenchange", sync);
  d.addEventListener("htmx:afterSwap", sync);
  sync();
})();
