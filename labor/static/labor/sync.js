/* Production Suite: keeps the Labor Calculator's plan on the server instead of in one browser.
   The calculator calls LaborSync.boot() when it starts and LaborSync.save(state) after every change. */
(function () {
  'use strict';
  var S = { version: 0, user: '', pending: null, timer: null, inflight: null, migrateKey: null, retry: null, blocked: false };

  function csrf() { var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; }
  function fmt(iso) { try { return new Date(iso).toLocaleString([], { month: 'numeric', day: 'numeric', hour: 'numeric', minute: '2-digit' }); } catch (e) { return ''; } }
  function status(kind, text) {
    var el = document.getElementById('labor-sync');
    if (el) { el.textContent = text; el.setAttribute('data-state', kind); }
  }
  function toast(msg, kind) {
    if (typeof window.showToast === 'function') window.showToast(msg, kind || 'info'); else console.log(msg);
  }
  function post(url, obj) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
      body: JSON.stringify(obj) }).then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { j.status = r.status; return j; }); });
  }

  // ---- bars and the PIN box (plain DOM; the page's own styles don't reach them)
  function bar(id, text, buttons, tone) {
    var old = document.getElementById(id); if (old) old.remove();
    var d = document.createElement('div');
    d.id = id;
    d.setAttribute('role', 'alert');
    d.style.cssText = 'position:fixed;left:50%;top:60px;transform:translateX(-50%);z-index:9999;max-width:min(680px,94vw);padding:12px 16px;border-radius:10px;' +
      'font:600 14px system-ui,Segoe UI,sans-serif;display:flex;gap:10px;align-items:center;flex-wrap:wrap;box-shadow:0 8px 24px rgba(0,0,0,.25);' +
      (tone === 'bad' ? 'background:#7f1d1d;color:#fff' : 'background:#78350f;color:#fff');
    var t = document.createElement('span'); t.textContent = text; t.style.flex = '1 1 260px'; d.appendChild(t);
    (buttons || []).forEach(function (b) {
      var x = document.createElement('button'); x.type = 'button'; x.textContent = b[0];
      x.style.cssText = 'background:#fff;color:#111;border:0;border-radius:7px;padding:7px 12px;font:600 13px system-ui,sans-serif;cursor:pointer';
      x.addEventListener('click', b[1]); d.appendChild(x);
    });
    document.body.appendChild(d);
    return d;
  }
  function clearBar(id) { var o = document.getElementById(id); if (o) o.remove(); }

  function askPin(note) {
    if (document.getElementById('labor-pin')) return;
    var w = document.createElement('div');
    w.id = 'labor-pin';
    w.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(15,23,42,.6);display:flex;align-items:center;justify-content:center;padding:16px';
    w.innerHTML = '<form style="background:#fff;color:#0f172a;border-radius:12px;padding:22px;width:min(360px,94vw);font:14px system-ui,Segoe UI,sans-serif;box-shadow:0 12px 40px rgba(0,0,0,.35)">' +
      '<div style="font-weight:700;font-size:16px;margin-bottom:6px">Shift lead PIN</div>' +
      '<div id="labor-pin-note" style="color:#475569;margin-bottom:12px"></div>' +
      '<input id="labor-pin-in" type="password" inputmode="numeric" autocomplete="off" style="width:100%;font-size:20px;padding:10px;border:1px solid #cbd5e1;border-radius:8px">' +
      '<div id="labor-pin-err" style="color:#b91c1c;min-height:20px;margin-top:6px"></div>' +
      '<button type="submit" style="width:100%;margin-top:4px;background:#2563eb;color:#fff;border:0;border-radius:8px;padding:11px;font-weight:700;font-size:15px;cursor:pointer">Log in and save</button></form>';
    document.body.appendChild(w);
    w.querySelector('#labor-pin-note').textContent = note || 'You were logged out. Your changes are kept on this screen; log in to save them.';
    var inp = w.querySelector('#labor-pin-in'); inp.focus();
    w.querySelector('form').addEventListener('submit', function (e) {
      e.preventDefault();
      post('/api/hmi/login', { pin: inp.value, adminOnly: true }).then(function (r) {
        if (r.ok && r.me && (r.me.role === 'lead' || r.me.role === 'admin')) { w.remove(); S.blocked = false; send(); }
        else { w.querySelector('#labor-pin-err').textContent = r.error || 'That PIN is not a shift lead PIN.'; inp.select(); }
      }).catch(function () { w.querySelector('#labor-pin-err').textContent = 'The server is not answering. Try again.'; });
    });
  }

  // ---- saving
  function send(force) {
    if (S.inflight) return S.inflight.then(function () { return send(force); });
    if (!S.pending || S.blocked) return Promise.resolve(!S.pending);
    var data = S.pending; S.pending = null;
    clearTimeout(S.retry);
    status('saving', 'Saving…');
    S.inflight = post('/api/labor/save', { version: S.version, data: data, force: !!force }).then(function (r) {
      S.inflight = null;
      if (r.ok) {
        S.version = r.version;
        clearBar('labor-conflict'); clearBar('labor-offline');
        status('saved', 'Saved ' + fmt(r.updatedAt));
        if (S.migrateKey) { try { localStorage.removeItem(S.migrateKey); } catch (e) {} S.migrateKey = null; }
        return S.pending ? send() : true;
      }
      if (!S.pending) S.pending = data;
      if (r.conflict) {
        S.blocked = true;
        status('error', 'Not saved');
        bar('labor-conflict', (r.updatedBy || 'Someone') + ' changed the labor plan on another screen' + (r.updatedAt ? ' at ' + fmt(r.updatedAt) : '') +
          '. Your latest change is not saved.', [
          ['Load their version', function () { S.pending = null; window.onbeforeunload = null; location.reload(); }],
          ['Keep mine', function () { S.blocked = false; clearBar('labor-conflict'); send(true); }]], 'warn');
        return false;
      }
      if (r.login || r.status === 403) { S.blocked = true; status('error', 'Not saved'); askPin(r.error); return false; }
      if (r.status === 400) { status('error', 'Not saved'); toast('Not saved: ' + (r.error || 'the server refused the plan.'), 'error'); S.pending = null; return false; }
      throw new Error(r.error || ('HTTP ' + r.status));
    }).catch(function () {
      S.inflight = null;
      if (!S.pending) S.pending = data;
      status('error', 'Not saved, retrying');
      bar('labor-offline', 'The server is not answering, so your latest change is not saved yet. It will keep trying.', [['Try now', function () { send(); }]], 'bad');
      S.retry = setTimeout(send, 10000);
      return false;
    });
    return S.inflight;
  }

  window.LaborSync = {
    boot: function (oldKey) {
      var b = JSON.parse(document.getElementById('labor-boot').textContent || '{}');
      S.version = b.version || 0; S.user = b.user || '';
      if (b.data) { status('saved', 'Saved ' + (b.updatedAt ? fmt(b.updatedAt) : '') + (b.updatedBy ? ' by ' + b.updatedBy : '')); return b.data; }
      // Nothing on the server yet: bring over a plan the stand-alone calculator saved in this browser, if any.
      try {
        var old = localStorage.getItem(oldKey);
        if (old) {
          var d = JSON.parse(old);
          S.migrateKey = oldKey;
          setTimeout(function () {
            S.pending = d; send().then(function (ok) { if (ok) toast('Moved the labor plan saved in this browser to the server. Every shift lead now sees the same plan.', 'success'); });
          }, 300);
          return d;
        }
      } catch (e) { }
      status('saved', 'Not saved yet');
      return null;
    },
    save: function (state) {
      S.pending = state;
      clearTimeout(S.timer);
      status('pending', 'Saving…');
      S.timer = setTimeout(function () { send(); }, 1200);
    },
    flush: function () { clearTimeout(S.timer); return send(); },
    showHistory: function () {
      LaborSync.flush().then(function () { return fetch('/api/labor/history', { credentials: 'same-origin' }); })
        .then(function (r) { return r.json(); }).then(function (r) {
          if (!r.ok) { toast(r.error || 'Could not load the saved versions.', 'error'); if (r.login) askPin(r.error); return; }
          var w = document.createElement('div');
          w.id = 'labor-history';
          w.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(15,23,42,.6);display:flex;align-items:center;justify-content:center;padding:16px';
          var c = document.createElement('div');
          c.style.cssText = 'background:#fff;color:#0f172a;border-radius:12px;padding:20px;width:min(520px,94vw);max-height:80vh;overflow:auto;font:14px system-ui,Segoe UI,sans-serif';
          var h = document.createElement('div'); h.style.cssText = 'font-weight:700;font-size:16px;margin-bottom:4px'; h.textContent = 'Saved versions';
          var sub = document.createElement('div'); sub.style.cssText = 'color:#475569;margin-bottom:12px';
          sub.textContent = 'Every save is kept (the last 200). Restoring one makes it the current plan; the plan you have now stays in this list.';
          c.appendChild(h); c.appendChild(sub);
          if (!r.items.length) { var e = document.createElement('div'); e.textContent = 'No saves yet.'; c.appendChild(e); }
          r.items.forEach(function (it, i) {
            var row = document.createElement('div');
            row.style.cssText = 'display:flex;gap:10px;align-items:center;padding:8px 0;border-top:1px solid #e2e8f0';
            var t = document.createElement('span'); t.style.flex = '1';
            t.textContent = 'v' + it.version + ' · ' + fmt(it.at) + ' · ' + (it.by || '') + (i === 0 ? '  (current)' : '');
            row.appendChild(t);
            if (i > 0) {
              var b = document.createElement('button'); b.type = 'button'; b.textContent = 'Restore';
              b.style.cssText = 'background:#2563eb;color:#fff;border:0;border-radius:7px;padding:6px 12px;font-weight:600;cursor:pointer';
              b.addEventListener('click', function () {
                if (!confirm('Restore version ' + it.version + ' from ' + fmt(it.at) + '?')) return;
                post('/api/labor/restore', { id: it.id }).then(function (x) {
                  if (x.ok) { window.onbeforeunload = null; location.reload(); } else toast(x.error || 'Could not restore.', 'error');
                });
              });
              row.appendChild(b);
            }
            c.appendChild(row);
          });
          var close = document.createElement('button'); close.type = 'button'; close.textContent = 'Close';
          close.style.cssText = 'margin-top:12px;background:#e2e8f0;border:0;border-radius:7px;padding:8px 14px;font-weight:600;cursor:pointer';
          close.addEventListener('click', function () { w.remove(); });
          c.appendChild(close); w.appendChild(c);
          w.addEventListener('click', function (ev) { if (ev.target === w) w.remove(); });
          document.body.appendChild(w);
        }).catch(function () { toast('The server is not answering.', 'error'); });
    }
  };

  window.addEventListener('beforeunload', function (e) {
    if (S.pending || S.inflight) { LaborSync.flush(); e.preventDefault(); e.returnValue = ''; return ''; }
  });
  document.addEventListener('DOMContentLoaded', function () {
    var out = document.getElementById('labor-logout');
    if (out) out.addEventListener('click', function () {
      LaborSync.flush().then(function () { return post('/api/hmi/logout', {}); }).then(function () { window.onbeforeunload = null; location.href = '/labor/'; });
    });
  });
})();
