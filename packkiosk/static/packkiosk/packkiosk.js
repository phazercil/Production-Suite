(function () {   // New pallet tag: picking an MO or typing a part fills in the next pallet number and last-used values
  var f = document.getElementById('pk-new');
  if (!f) return;
  var mo = document.getElementById('pk-mo'), part = document.getElementById('pk-part'), pal = document.getElementById('pk-pallet'),
    qty = document.getElementById('pk-qty'), line = document.getElementById('pk-line'), desc = document.getElementById('pk-desc'),
    hint = document.getElementById('pk-hint'), timer;
  function fmt(n) { return Number(n).toLocaleString(); }
  function fill(fromMo) {
    var q = new URLSearchParams({ mo: mo.value, part: fromMo ? '' : part.value });
    fetch('/packaging/suggest?' + q, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (s) {
      if (!s.ok) return;
      if (fromMo && s.part) part.value = s.part;
      pal.value = s.pallet;
      if (s.qty && !qty.value) qty.value = s.qty;
      if (s.line && !line.value) line.value = s.line;
      if (s.description && !desc.value) desc.value = s.description;
      hint.textContent = s.moQty ? 'MO ' + s.mo + ': ' + fmt(s.packed || 0) + ' of ' + fmt(s.moQty) + ' pcs tagged so far. Next pallet: ' + s.pallet + '.'
        : (s.part ? 'Next pallet for ' + s.part + ' today: ' + s.pallet + '.' : '');
    }).catch(function () {});
  }
  mo.addEventListener('change', function () { fill(true); });
  part.addEventListener('input', function () { part.value = part.value.toUpperCase(); clearTimeout(timer); timer = setTimeout(function () { fill(false); }, 350); });
  if (!pal.value && (mo.value || part.value)) fill(!!mo.value);
  var pr = document.querySelector('[data-print]');   // "Save & print": open the tag in a new tab, which prints itself
  if (pr) {
    var w = window.open('/packaging/tag/' + pr.dataset.print + '/?auto=1', '_blank');
    if (!w) location.href = '/packaging/tag/' + pr.dataset.print + '/?auto=1';
    history.replaceState(null, '', location.pathname + location.search.replace(/([?&])print=\d+&?/, '$1').replace(/[?&]$/, ''));
  }
})();

(function () {   // Find: filters as you type (no Show button to press)
  var inp = document.querySelector('#pk-find input[data-live]'), t;
  if (!inp) return;
  inp.addEventListener('input', function () { clearTimeout(t); t = setTimeout(function () { inp.form.submit(); }, 450); });
  if (inp.value) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
})();
