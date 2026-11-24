(function () {
  function csrf() { var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; }
  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify(body || {}) }).then(function (r) { return r.json(); });
  }
  var f = document.getElementById('sv-login');
  if (f) {
    var inp = document.getElementById('lpin'), err = document.getElementById('lerr');
    f.addEventListener('submit', function (e) {
      e.preventDefault(); err.textContent = '';
      post('/api/hmi/login', { pin: inp.value, perm: f.dataset.perm || 'section.supervisor' }).then(function (r) {
        if (r.ok && r.me) location.reload();
        else { err.textContent = r.error || 'That PIN cannot open ' + (f.dataset.title || 'Supervisor') + '.'; inp.select(); }
      }).catch(function () { err.textContent = 'The server is not answering. Try again.'; });
    });
  }
  var out = document.getElementById('sv-logout');
  if (out) out.addEventListener('click', function () { post('/api/hmi/logout').finally(function () { location.reload(); }); });
  document.querySelectorAll('tr.go[data-href]').forEach(function (tr) {
    tr.addEventListener('click', function (e) { if (!e.target.closest('a,button')) location.href = tr.dataset.href; });
  });
  var ar = document.querySelector('[data-autorefresh]');   // Line status: fresh every few seconds
  if (ar) setTimeout(function () { if (!document.hidden) location.reload(); else document.addEventListener('visibilitychange', function () { location.reload(); }, { once: true }); },
    1000 * (parseInt(ar.dataset.autorefresh, 10) || 20));
  var pr = document.getElementById('sv-print');
  if (pr) pr.addEventListener('click', function () { window.print(); });
  var cp = document.getElementById('sv-copy');
  if (cp) cp.addEventListener('click', function () {
    var t = cp.dataset.text || '';
    (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject()).then(function () { cp.textContent = 'Copied'; },
      function () { var a = document.createElement('textarea'); a.value = t; document.body.appendChild(a); a.select(); document.execCommand('copy'); a.remove(); cp.textContent = 'Copied'; });
    setTimeout(function () { cp.textContent = 'Copy report'; }, 2000);
  });
})();
(function () {   // Line status: fresh every 30 seconds while the page is open (only while nobody is typing)
  var g = document.getElementById('sv-lines');
  if (!g) return;
  setInterval(function () {
    if (document.hidden) return;
    fetch(location.href, { credentials: 'same-origin' }).then(function (r) { return r.text(); }).then(function (h) {
      var d = new DOMParser().parseFromString(h, 'text/html');
      var n = d.getElementById('sv-lines'), k = d.querySelector('.sv-kpis'), mk = document.querySelector('.sv-kpis');
      if (n) g.innerHTML = n.innerHTML;
      if (k && mk) mk.innerHTML = k.innerHTML;
    }).catch(function () {});
  }, (+g.dataset.refresh || 30) * 1000);
})();
(function () {   // History builder: show the next empty paper-log row
  var b = document.getElementById('sv-addrow');
  if (!b) return;
  b.addEventListener('click', function () {
    var r = document.querySelector('.sv-rows tr[hidden]');
    if (!r) { b.disabled = true; b.textContent = 'No more rows: save, then add the rest'; return; }
    r.hidden = false;
    var i = r.querySelector('input');
    if (i) i.focus();
    if (!document.querySelector('.sv-rows tr[hidden]')) b.disabled = true;
  });
})();

// Supervisor › Downtime: tick stops (the header box ticks every one, Shift-click a range), then change or delete them together
(function () {
  var f = document.getElementById("dbatch");
  if (!f) return;
  var all = document.getElementById("dball"), n = document.getElementById("dbn"), go = document.getElementById("dbgo"), del = document.getElementById("dbdel");
  function boxes() { return [].slice.call(document.querySelectorAll('input[name="ids"][form="dbatch"]')); }
  function upd() {
    var list = boxes(), k = list.filter(function (b) { return b.checked; }).length;
    n.textContent = k; go.disabled = !k; del.disabled = !k;
    if (all) { all.checked = !!k && k === list.length; all.indeterminate = !!k && k < list.length; }
  }
  if (all) all.addEventListener("change", function () { boxes().forEach(function (b) { b.checked = all.checked; }); upd(); });
  document.addEventListener("change", function (e) { if (e.target.matches('input[name="ids"]')) upd(); });
  var last = null;
  document.addEventListener("click", function (e) {
    var b = e.target;
    if (!b.matches || !b.matches('input[name="ids"]')) return;
    if (e.shiftKey && last) {
      var list = boxes(), a = list.indexOf(last), c = list.indexOf(b);
      list.slice(Math.min(a, c), Math.max(a, c) + 1).forEach(function (x) { x.checked = b.checked; });
      upd();
    }
    last = b;
  });
  upd();
})();
