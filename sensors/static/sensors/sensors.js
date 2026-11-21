(function () {   // Sensor form: show the chosen connection's settings, number or on/off parts, presets and a scaling preview
  var f = document.getElementById('sn-form');
  if (!f) return;
  var kind = document.getElementById('sn-kind'), dtype = document.getElementById('sn-dtype'), scale = document.getElementById('sn-scale');
  var readKinds = {};
  Array.prototype.forEach.call(kind.querySelectorAll('optgroup'), function (g, i) {
    Array.prototype.forEach.call(g.querySelectorAll('option'), function (o) { readKinds[o.value] = i === 0; });
  });
  function show() {
    f.querySelectorAll('.sn-kind').forEach(function (fs) {
      var on = fs.dataset.kind === kind.value;
      fs.hidden = !on;
      fs.querySelectorAll('input,select,textarea').forEach(function (x) { x.disabled = !on; });
    });
    f.querySelectorAll('[data-group="read"]').forEach(function (x) { x.hidden = !readKinds[kind.value]; });
    f.querySelectorAll('[data-show]').forEach(function (x) {
      var on = x.dataset.show === dtype.value;
      x.hidden = !on;
      x.querySelectorAll('input,select').forEach(function (i) { if (i.id !== 'sn-try') i.disabled = !on; });
    });
    var sc = document.getElementById('sn-scaling'), u2 = document.getElementById('sn-unit2');
    if (sc && scale) {
      sc.hidden = !scale.checked;
      sc.querySelectorAll('input').forEach(function (i) { if (dtype.value === 'number') i.disabled = !scale.checked; });
      if (u2) { u2.hidden = scale.checked; u2.querySelector('input').disabled = scale.checked || dtype.value !== 'number'; }
    }
    preview();
  }
  function num(id) { var v = parseFloat(document.getElementById(id).value); return isNaN(v) ? null : v; }
  function preview() {
    var out = document.getElementById('sn-tryout'), t = num('sn-try');
    if (!out) return;
    var rl = num('sn-rlo'), rh = num('sn-rhi'), el = num('sn-elo'), eh = num('sn-ehi');
    if (t === null || rl === null || rh === null || el === null || eh === null || rl === rh) { out.textContent = ''; return; }
    var v = el + (t - rl) * (eh - el) / (rh - rl);
    out.textContent = '= ' + (Math.round(v * 1000) / 1000) + ' ' + document.getElementById('sn-u').value;
  }
  kind.addEventListener('change', show);
  dtype.addEventListener('change', show);
  if (scale) scale.addEventListener('change', show);
  ['sn-try', 'sn-rlo', 'sn-rhi', 'sn-elo', 'sn-ehi', 'sn-u'].forEach(function (id) { var e = document.getElementById(id); if (e) e.addEventListener('input', preview); });
  f.querySelectorAll('[data-preset]').forEach(function (b) {
    b.addEventListener('click', function () {
      var p = b.dataset.preset.split(',');
      document.getElementById('sn-rlo').value = p[0]; document.getElementById('sn-rhi').value = p[1]; document.getElementById('sn-ru').value = p[2];
      preview();
    });
  });
  show();
})();
(function () {   // Rule form: show the limit boxes the condition needs
  var f = document.getElementById('sn-rule');
  if (!f) return;
  var op = document.getElementById('sn-op');
  var none = { on: 1, off: 1, fault: 1 }, two = { 'in': 1, out: 1 }, mins = { stuck: 1, stale: 1 }, noDb = { on: 1, off: 1, fault: 1, stuck: 1, stale: 1 };
  var aLabel = f.querySelector('[data-a-label]'), aText = aLabel.textContent;
  function show() {
    var v = op.value;
    f.querySelector('[data-op-a]').hidden = !!none[v];
    f.querySelector('[data-op-b]').hidden = !two[v];
    f.querySelector('[data-op-db]').hidden = !!noDb[v];
    aLabel.textContent = mins[v] ? 'Minutes' : (v === 'rise' || v === 'fall') ? aText.replace('Limit', 'Change per minute') : two[v] ? aText.replace('Limit', 'Between') : aText;
  }
  op.addEventListener('change', show);
  show();
})();
