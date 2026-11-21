(function () {
  var b = document.getElementById('tag-print');
  if (b) b.addEventListener('click', function () { window.print(); });
  if (document.body.dataset.auto === '1') window.addEventListener('load', function () { setTimeout(function () { window.print(); }, 200); });
})();
