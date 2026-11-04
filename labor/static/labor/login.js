(function () {
  var f = document.getElementById('lform'), inp = document.getElementById('lpin'), err = document.getElementById('lerr');
  function csrf() { var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; }
  f.addEventListener('submit', function (e) {
    e.preventDefault();
    err.textContent = '';
    fetch('/api/hmi/login', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify({ pin: inp.value, perm: 'labor.view' }) })
      .then(function (r) { return r.json(); })
      .then(function (r) {
        if (r.ok && r.me) location.reload();
        else { err.textContent = r.error || 'That PIN cannot open the Labor Budget.'; inp.select(); }
      })
      .catch(function () { err.textContent = 'The server is not answering. Try again.'; });
  });
})();
