/* Reports: line summary with drill up / down. The server does every calculation (reports/engine.py);
   this page only draws what /api/report sends and remembers where you are in the address (#scope=...&period=...). */
(function () {
  'use strict';
  const $ = s => document.querySelector(s);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const n0 = v => v == null ? '—' : Math.round(v).toLocaleString();
  const n1 = v => v == null ? '—' : Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 });
  const pct = v => v == null ? '—' : `${Number(v).toFixed(1)}%`;
  const money = (v, d = 2) => v == null ? '—' : '$' + Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  const hrs = m => m == null ? '—' : (m / 60).toLocaleString(undefined, { maximumFractionDigits: 1 }) + ' h';
  function csrf() { const m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; }
  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

  let R = null, picking = false, gp = null;
  function state() {
    const h = new URLSearchParams(location.hash.slice(1));
    const t = new Date();
    return { scope: h.get('scope') || 'plant', period: h.get('period') || `month:${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}` };
  }
  function go(scope, period) {
    const s = state();
    const h = new URLSearchParams({ scope: scope || s.scope, period: period || s.period });
    if (location.hash.slice(1) !== h.toString()) location.hash = h.toString(); else load();
  }
  function shift(period, dir) {
    const [k, v] = period.split(':');
    if (k === 'year') return `year:${+v + dir}`;
    if (k === 'month') { const [y, m] = v.split('-').map(Number); const d = new Date(y, m - 1 + dir, 1); return `month:${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`; }
    const [y, m, d] = v.split('-').map(Number); const dt = new Date(y, m - 1, d + dir * (k === 'week' ? 7 : 1)); return `${k}:${iso(dt)}`;
  }

  async function load() {
    const s = state();
    $('#rp-body').style.opacity = .6;
    let r;
    try { r = await (await fetch(`/api/report?scope=${encodeURIComponent(s.scope)}&period=${encodeURIComponent(s.period)}`, { credentials: 'same-origin' })).json(); }
    catch (e) { $('#rp-body').innerHTML = '<p class="bad">The server is not answering. Try again.</p>'; $('#rp-body').style.opacity = 1; return; }
    $('#rp-body').style.opacity = 1;
    if (!r.ok) { $('#rp-body').innerHTML = `<p class="bad">${esc(r.error)}</p>`; return; }
    R = r; draw();
    if (r.grafana.ready && r.grafana.panels.length) loadGrafana(); else gp = null;
  }

  function crumbs(list, onKey) {
    return list.map((c, i) => i === list.length - 1 ? `<span class="cur">${esc(c.label)}</span>` : `<a href="#" data-${onKey}="${esc(c.key)}">${esc(c.label)}</a><span class="sep">›</span>`).join('');
  }

  function draw() {
    const T = R.total, L = R.labor && R.lead;
    $('#scopeCrumbs').innerHTML = crumbs(R.scope.crumbs, 'scope');
    $('#periodCrumbs').innerHTML = crumbs(R.period.crumbs, 'period') + (R.period.week && R.period.kind === 'day' ? ` <a href="#" data-period="${esc(R.period.week)}" style="font-size:13px">(week)</a>` : '');
    const isMo = R.scope.level === 'mo';
    const kpi = (label, val, sub = '') => `<div class="kpi"><span>${label}</span><b>${val}</b>${sub ? `<small>${sub}</small>` : ''}</div>`;
    const kpis = [
      kpi('Filters made', n0(T.units)),
      isMo ? kpi('Run time', hrs(T.obs)) : kpi('Uptime', `<span class="${T.uptime == null ? '' : T.uptime >= 85 ? 'ok' : T.uptime >= 70 ? 'warn' : 'bad'}">${pct(T.uptime)}</span>`, `${hrs(T.run)} running of ${hrs(T.obs)}`),
      kpi('Downtime', hrs(T.down), T.reasons[0] ? `Top: ${esc(T.reasons[0].reason)}` : ''),
      isMo ? '' : kpi('Average speed', T.ppmAvg == null ? '—' : `${n1(T.ppmAvg)} P/m`, 'while running'),
      kpi('Operator QC', pct(T.qcPass), `${n0(T.qc)} checks · ${n0(T.qcFail)} failed`),
      T.aq ? kpi('Machine vision QC', pct(T.aqPass), `${n0(T.aq)} measured · ${n0(T.aqFail)} failed`) : '',
      kpi('MOs finished', n0(T.mos), `${n0(T.moQty)} filters`),
      L ? kpi('Crew wages', money(T.laborCost, 0), T.laborPerUnit != null ? `${money(T.laborPerUnit, 4)} per filter` : '') : ''
    ].join('');
    // chart
    const B = R.buckets, max = Math.max(1, ...B.map(b => b.units || 0));
    const chart = `<div class="card"><h2>Filters made · ${esc(R.period.label)}</h2><div class="chart">${B.map(b => {
      const h = b.future ? 0 : Math.max(1, (b.units || 0) / max * 100);
      const tip = b.future ? `${b.label}: still to come` : `${b.label}: ${n0(b.units)} filters · uptime ${pct(b.uptime)} · ${hrs(b.down)} down${b.qcFail ? ` · ${b.qcFail} QC fails` : ''}`;
      return `<div class="col ${b.key && !b.future ? 'drill' : ''} ${b.future ? 'future' : ''}" ${b.key && !b.future ? `data-period="${esc(b.key)}"` : ''} title="${esc(tip)}">
        <div class="bar" style="height:${h}%"></div>${b.uptime != null ? `<div class="up" style="bottom:calc(${b.uptime}% * .82 + 18px)"></div>` : ''}<label>${esc(b.label)}</label></div>`;
    }).join('')}</div><div class="legend"><span><i style="background:var(--s-accent)"></i>Filters made</span><span><i style="background:#e3b341;border-radius:50%"></i>Uptime %</span>${R.drill.buckets ? '<span>Click a bar to look closer</span>' : ''}</div></div>`;
    // table
    const childName = { plant: 'Line', line: 'Shift', shift: 'MO' }[R.scope.level];
    const cols = [['Filters', r => n0(r.units)], ...(R.scope.level === 'shift' ? [['Run time', r => hrs(r.obs)]] : [['Uptime', r => pct(r.uptime)], ['Running', r => hrs(r.run)], ['Avg P/m', r => n1(r.ppmAvg)]]),
      ['Down', r => hrs(r.down)], ['Top reason', r => r.reasons && r.reasons[0] ? esc(r.reasons[0].reason) : '—'], ['QC pass', r => pct(r.qcPass)],
      ...(T.aq ? [['Vision pass', r => pct(r.aqPass)]] : []), ['MOs done', r => n0(r.mos)], ...(L ? [['Wages', r => money(r.laborCost, 0)], ['$ / filter', r => money(r.laborPerUnit, 4)]] : [])];
    const table = R.rows.length ? `<div class="card"><h2>By ${childName.toLowerCase()}</h2><div class="tw"><table class="rt"><thead><tr><th>${childName}</th>${cols.map(c => `<th>${c[0]}</th>`).join('')}</tr></thead>
      <tbody>${R.rows.map(r => `<tr class="${R.drill.rows ? 'drill' : ''}" data-scope="${esc(r.key)}" tabindex="0"><td>${esc(r.label)}</td>${cols.map(c => `<td>${c[1](r)}</td>`).join('')}</tr>`).join('')}</tbody>
      <tfoot><tr><td>Total</td>${cols.map(c => `<td>${c[1](T)}</td>`).join('')}</tr></tfoot></table></div>${R.drill.rows ? '<p class="muted" style="margin:8px 0 0;font-size:13px">Click a row to look closer; use the names at the top to go back up.</p>' : ''}</div>`
      : (isMo ? '' : `<div class="card"><p class="muted">No ${childName.toLowerCase()}s with records in this period.</p></div>`);
    const rmax = Math.max(1, ...T.reasons.map(x => x.min));
    const reasons = T.reasons.length ? `<div class="card"><h2>Downtime by reason</h2><div class="reasons">${T.reasons.map(x => `<div class="r"><span>${esc(x.reason)}</span><i style="width:${x.min / rmax * 100}%"></i><span style="text-align:right">${hrs(x.min)}</span></div>`).join('')}</div>
      <p class="muted" style="font-size:13px;margin:8px 0 0">Planned breaks, lunches and start-up time are not downtime.</p></div>` : '';
    const gsec = R.grafana.ready ? `<div class="card"><div style="display:flex;gap:8px;align-items:center"><h2 style="flex:1;margin:0">From Grafana</h2>${R.lead ? '<button class="btn ghost" id="gpick">Choose panels</button>' : ''}</div>
      <div id="gpicker"></div><div id="gpanels">${R.grafana.panels.length ? '<p class="muted">Loading Grafana data…</p>' : `<p class="muted">${R.lead ? 'No Grafana panels chosen for reports yet.' : 'A shift lead can add Grafana panels here.'}</p>`}</div></div>` : '';
    $('#rp-body').innerHTML = `<div class="kpis">${kpis}</div>${chart}${table}<div class="grid2">${reasons}</div>${gsec}`;
    if (gp) drawGrafana();
  }

  async function loadGrafana() {
    const s = state();
    try { gp = await (await fetch(`/api/report/grafana/data?scope=${encodeURIComponent(s.scope)}&period=${encodeURIComponent(s.period)}`, { credentials: 'same-origin' })).json(); }
    catch (e) { gp = { ok: false, error: 'The server is not answering.' }; }
    drawGrafana();
  }
  function spark(points) {
    if (!points || points.length < 2) return '';
    const xs = points.map(p => p[0]), ys = points.map(p => p[1]), x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const X = x => (x - x0) / Math.max(1, x1 - x0) * 300, Y = y => 56 - (y - y0) / Math.max(1e-9, y1 - y0) * 52;
    return `<svg viewBox="0 0 300 60" preserveAspectRatio="none" aria-hidden="true"><polyline fill="none" stroke="#5B9BD5" stroke-width="1.6" points="${points.map(p => `${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(' ')}"/></svg>`;
  }
  function drawGrafana() {
    const box = $('#gpanels'); if (!box || !gp) return;
    if (!gp.ok) { box.innerHTML = `<p class="bad">${esc(gp.error)}</p>`; return; }
    box.innerHTML = `<div class="grid2">${gp.panels.map(p => `<div class="gp"><h3>${esc(p.title)}</h3>${p.error ? `<p class="bad">${esc(p.error)}</p>` : !p.series.length ? '<p class="muted">No data in this period.</p>' :
      p.series.map(s => `${spark(s.points)}<table><tr><th>${esc(s.name)}</th><th>Last</th><th>Avg</th><th>Min</th><th>Max</th></tr><tr><td></td><td>${n1(s.last)}</td><td>${n1(s.avg)}</td><td>${n1(s.min)}</td><td>${n1(s.max)}</td></tr></table>`).join('')}</div>`).join('')}</div>`;
  }
  async function openPicker() {
    const box = $('#gpicker'); if (!box) return;
    if (picking) { picking = false; box.innerHTML = ''; return; }
    picking = true; box.innerHTML = '<p class="muted">Loading the dashboard…</p>';
    const r = await (await fetch('/api/report/grafana/panels', { credentials: 'same-origin' })).json();
    if (!r.ok) { box.innerHTML = `<p class="bad">${esc(r.error)}</p>`; return; }
    const sel = new Set((R.grafana.panels || []).map(Number));
    box.innerHTML = `<div class="pick">${r.panels.map(p => `<label><input type="checkbox" value="${p.id}" ${sel.has(p.id) ? 'checked' : ''}> ${esc(p.title)}</label>`).join('')}</div><button class="btn" id="gsave">Save</button> <span class="muted" id="gerr"></span>`;
    $('#gsave').onclick = async () => {
      const ids = [...box.querySelectorAll('input:checked')].map(x => +x.value);
      const res = await (await fetch('/api/hmi/cmd', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf() },
        body: JSON.stringify({ name: 'settingSet', p: { k: 'grafana.report', v: ids } }) })).json();
      if (!res.ok) { $('#gerr').textContent = res.error || 'Not saved.'; return; }
      picking = false; load();
    };
  }

  function csv() {
    if (!R) return;
    const head = ['Name', 'Filters', 'Uptime %', 'Running min', 'Observed min', 'Avg P/m', 'Down min', 'Top reason', 'QC checks', 'QC fails', 'Vision measured', 'Vision fails', 'MOs done', ...(R.labor && R.lead ? ['Crew wages', '$ per filter'] : [])];
    const line = r => [r.label, r.units, r.uptime, r.run, r.obs, r.ppmAvg, r.down, r.reasons && r.reasons[0] ? r.reasons[0].reason : '', r.qc, r.qcFail, r.aq, r.aqFail, r.mos,
      ...(R.labor && R.lead ? [r.laborCost, r.laborPerUnit] : [])];
    const rows = [head, ...R.rows.map(line), line({ ...R.total, label: 'Total' })];
    const text = rows.map(r => r.map(v => { const s = v == null ? '' : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; }).join(',')).join('\r\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
    a.download = `report ${R.scope.crumbs.map(c => c.label).join(' - ')} ${R.period.label}.csv`.replace(/[\\/:*?"<>|]/g, '');
    document.body.appendChild(a); a.click(); a.remove();
  }

  document.addEventListener('click', e => {
    const t = e.target.closest('[data-scope],[data-period],[data-q],#prev,#next,#csv,#gpick');
    if (!t) return;
    if (t.dataset.scope) { e.preventDefault(); if (t.closest('tr') && !(R && R.drill.rows)) return; go(t.dataset.scope, null); return; }
    if (t.dataset.period) { e.preventDefault(); go(null, t.dataset.period); return; }
    if (t.dataset.q) {
      const d = new Date(); const q = t.dataset.q;
      go(null, q === 'year' ? `year:${d.getFullYear()}` : q === 'month' ? `month:${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}` : `${q}:${iso(d)}`); return;
    }
    if (t.id === 'prev' || t.id === 'next') { go(null, shift(state().period, t.id === 'next' ? 1 : -1)); return; }
    if (t.id === 'csv') { csv(); return; }
    if (t.id === 'gpick') { openPicker(); }
  });
  document.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.matches('tr.drill')) go(e.target.dataset.scope, null); });
  window.addEventListener('hashchange', load);
  load();
  setInterval(() => { if (document.visibilityState === 'visible' && !picking) load(); }, 60000);
})();
