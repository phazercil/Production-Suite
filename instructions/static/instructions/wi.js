// Work instruction editor: add, remove and reorder steps and parameters. Each step's photo checkboxes are named
// s_media_<its position>, so they are renumbered after every change (and before saving).
(function () {
  "use strict";
  function renumber(form) {
    form.querySelectorAll('[data-wi-list="step"] > [data-wi-row]').forEach(function (row, i) {
      var n = row.querySelector(".wi-n");
      if (n) n.textContent = String(i + 1);
      row.querySelectorAll("[data-wi-media]").forEach(function (cb) { cb.name = "s_media_" + i; });
      row.querySelectorAll("[data-wi-files]").forEach(function (inp) { inp.name = "s_files_" + i; });
    });
  }
  function fresh(kind) {
    var tpl = document.getElementById("wi-tpl-" + kind);
    return tpl ? tpl.content.firstElementChild.cloneNode(true) : null;
  }
  function focusFirst(row) {
    var f = row && row.querySelector("textarea, input:not([type=checkbox])");
    if (f) f.focus();
  }
  function rowHasContent(row) {
    var any = false;
    row.querySelectorAll("textarea, input").forEach(function (el) {
      if (el.type === "checkbox" ? el.checked : el.value.trim()) any = true;
    });
    return any;
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-wi-add], [data-wi-insert], [data-wi-remove], [data-wi-up], [data-wi-down]");
    if (!btn) return;
    var form = btn.closest("[data-wi-form]");
    if (!form) return;
    e.preventDefault();
    if (btn.hasAttribute("data-wi-add")) {
      var kind = btn.getAttribute("data-wi-add");
      var list = form.querySelector('[data-wi-list="' + kind + '"]');
      var row = fresh(kind);
      if (list && row) { list.appendChild(row); renumber(form); focusFirst(row); }
      return;
    }
    var cur = btn.closest("[data-wi-row]");
    if (!cur) return;
    if (btn.hasAttribute("data-wi-insert")) {
      var nrow = fresh("step");
      if (nrow) { cur.after(nrow); renumber(form); focusFirst(nrow); }
    } else if (btn.hasAttribute("data-wi-remove")) {
      if (rowHasContent(cur) && !window.confirm("Remove this " + (cur.tagName === "TR" ? "parameter" : "step") + "? It's gone once you save the draft.")) return;
      var parent = cur.parentNode;
      cur.remove();
      if (!parent.querySelector("[data-wi-row]")) {   // keep one empty row to type into
        var kind2 = parent.getAttribute("data-wi-list");
        var empty = fresh(kind2);
        if (empty) parent.appendChild(empty);
      }
      renumber(form);
    } else if (btn.hasAttribute("data-wi-up")) {
      var prev = cur.previousElementSibling;
      if (prev) { cur.parentNode.insertBefore(cur, prev); renumber(form); btn.focus(); }
    } else if (btn.hasAttribute("data-wi-down")) {
      var next = cur.nextElementSibling;
      if (next) { cur.parentNode.insertBefore(next, cur); renumber(form); btn.focus(); }
    }
  });
  // Insert variable: [name] into the Target / Min / Max box last in (the first Target if none yet)
  var lastFormula = null;
  document.addEventListener("focusin", function (e) {
    if (e.target.matches && e.target.matches("[data-wi-formula]")) lastFormula = e.target;
  });
  document.addEventListener("change", function (e) {
    var sel = e.target.closest && e.target.closest("[data-wi-var]");
    if (!sel || !sel.value) return;
    var form = sel.closest("[data-wi-form]");
    var box = lastFormula && document.body.contains(lastFormula) ? lastFormula : form && form.querySelector("[data-wi-formula]");
    var token = "[" + sel.value + "]";
    sel.value = "";
    if (!box) return;
    var a = box.selectionStart == null ? box.value.length : box.selectionStart;
    var b = box.selectionEnd == null ? box.value.length : box.selectionEnd;
    box.value = box.value.slice(0, a) + token + box.value.slice(b);
    box.focus();
    var at = a + token.length;
    try { box.setSelectionRange(at, at); } catch (err) { /* not a text box */ }
  });
  // Add photo / video on a step: show what was picked (photos as thumbnails); they're saved with the draft
  document.addEventListener("change", function (e) {
    var inp = e.target.closest && e.target.closest("[data-wi-files]");
    if (!inp) return;
    var out = inp.closest("[data-wi-row]").querySelector("[data-wi-picked]");
    if (!out) return;
    out.textContent = "";
    Array.prototype.forEach.call(inp.files, function (f) {
      var chip = document.createElement("span");
      chip.className = "wi-new";
      if (f.type.indexOf("image/") === 0) {
        var img = document.createElement("img");
        img.src = URL.createObjectURL(f);
        img.alt = f.name;
        chip.appendChild(img);
      } else {
        chip.textContent = "▶ " + f.name;
      }
      out.appendChild(chip);
    });
    if (inp.files.length) {
      var note = document.createElement("span");
      note.className = "note";
      note.textContent = inp.files.length === 1 ? "added when you save the draft" : inp.files.length + " added when you save the draft";
      out.appendChild(note);
    }
  });
  document.addEventListener("submit", function (e) {
    var form = e.target.closest && e.target.closest("[data-wi-form]");
    if (form) renumber(form);
  }, true);
})();
