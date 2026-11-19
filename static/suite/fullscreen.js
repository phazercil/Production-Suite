// Full screen: the button in the top bar (or the corner, on screens without one) fills the whole display with the
// suite, for a kiosk or a TV. Esc, or the button again, leaves it. The browser remembers nothing: a reload leaves it.
(function () {
  var d = document, root = d.documentElement;
  var can = root.requestFullscreen || root.webkitRequestFullscreen;
  function on() { return !!(d.fullscreenElement || d.webkitFullscreenElement); }
  function sync() {
    var full = on();
    d.querySelectorAll("[data-fullscreen]").forEach(function (b) {
      b.classList.toggle("on", full);
      b.setAttribute("aria-pressed", full ? "true" : "false");
      b.title = full ? "Leave full screen (Esc)" : "Full screen (Esc leaves it)";
      var s = b.querySelector("span");
      if (s) s.textContent = full ? "Exit full screen" : "Full screen";
    });
  }
  function toggle() {
    if (on()) {
      (d.exitFullscreen || d.webkitExitFullscreen).call(d);
    } else {
      var r = (root.requestFullscreen || root.webkitRequestFullscreen).call(root);
      if (r && r.catch) r.catch(function () {});
    }
  }
  if (!can) return;   // this browser can't: the button stays hidden
  d.querySelectorAll("[data-fullscreen]").forEach(function (b) {
    b.hidden = false;
    b.addEventListener("click", toggle);
  });
  d.addEventListener("fullscreenchange", sync);
  d.addEventListener("webkitfullscreenchange", sync);
  sync();
})();
