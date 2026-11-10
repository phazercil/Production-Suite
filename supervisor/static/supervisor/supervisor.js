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
      post('/api/hmi/login', { pin: inp.value, perm: 'section.supervisor' }).then(function (r) {
        if (r.ok && r.me) location.reload();
        else { err.textContent = r.error || 'That PIN cannot open Supervisor.'; inp.select(); }
      }).catch(function () { err.textContent = 'The server is not answering. Try again.'; });
    });
  }
  var out = document.getElementById('sv-logout');
  if (out) out.addEventListener('click', function () { post('/api/hmi/logout').finally(function () { location.reload(); }); });
  document.querySelectorAll('tr.go[data-href]').forEach(function (tr) {
    tr.addEventListener('click', function (e) { if (!e.target.closest('a,button')) location.href = tr.dataset.href; });
  });
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
