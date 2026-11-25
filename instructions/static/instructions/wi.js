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
      var list = form.querySelector('[data-wi-list="' + (btn.getAttribute("data-wi-into") || kind) + '"]');
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
    var form = sel.closest("form");
    var mine = lastFormula && document.body.contains(lastFormula) && (!form || form.contains(lastFormula)) ? lastFormula : null;
    var box = mine || (form && form.querySelector("[data-wi-formula]"));
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

// Preview, inserting a parameter into a step, and never losing unsaved work.
(function () {
  "use strict";
  var form = document.querySelector("[data-wi-form]");
  function csrf() { var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ""; }

  // ---- unsaved changes: shown by the Save button, and asked about before leaving the page
  var saved = form ? snap() : "", submitting = false;
  function snap() {
    var parts = [];
    new FormData(form).forEach(function (v, k) {
      if (k === "csrfmiddlewaretoken") return;
      parts.push(k + "=" + (typeof v === "string" ? v : (v && v.name) || ""));
    });
    return parts.join("&");
  }
  function dirty() { return !!form && snap() !== saved; }
  function show() {
    if (!form) return;
    var d = dirty();
    var a = form.querySelector("[data-wi-dirty]"), b = form.querySelector("[data-wi-clean]"), c = form.querySelector("[data-wi-discard]");
    if (a) a.hidden = !d;
    if (b) b.hidden = d;
    if (c) c.hidden = !d;
    var bar = form.querySelector("[data-wi-savebar]");
    if (bar) bar.classList.toggle("on", d);
  }
  if (form) {
    form.addEventListener("input", show);
    form.addEventListener("change", show);
    form.addEventListener("click", function () { setTimeout(show, 0); });   // steps added, moved or removed
    form.addEventListener("submit", function () { submitting = true; });
    window.addEventListener("beforeunload", function (e) {
      if (submitting || !dirty()) return;
      e.preventDefault();
      e.returnValue = "";
    });
    var disc = form.querySelector("[data-wi-discard]");
    if (disc) disc.addEventListener("click", function () {
      if (!window.confirm("Discard your unsaved changes to this draft?")) return;
      submitting = true;
      location.assign(location.pathname);
    });
    show();
  }

  // ---- insert a parameter ({Name}) into the step last typed in
  var lastStep = null;
  document.addEventListener("focusin", function (e) {
    if (e.target.matches && e.target.matches('textarea[name="s_text"], input[name="s_caution"]')) lastStep = e.target;
  });
  function fillParams(sel) {
    var keep = sel.value, names = [];
    document.querySelectorAll('input[name="p_name"]').forEach(function (i) { var v = i.value.trim(); if (v && names.indexOf(v) < 0) names.push(v); });
    sel.textContent = "";
    var o = document.createElement("option");
    o.value = ""; o.textContent = names.length ? "Pick a parameter…" : "Add a parameter below first";
    sel.appendChild(o);
    names.forEach(function (n) {
      [["", n], [":range", n + " (range)"]].forEach(function (x) {
        var op = document.createElement("option");
        op.value = n + x[0]; op.textContent = x[1];
        sel.appendChild(op);
      });
    });
    sel.value = keep;
  }
  document.addEventListener("pointerdown", function (e) { var s = e.target.closest && e.target.closest("[data-wi-param]"); if (s) fillParams(s); });
  document.addEventListener("focusin", function (e) { if (e.target.matches && e.target.matches("[data-wi-param]")) fillParams(e.target); });
  document.addEventListener("change", function (e) {
    var sel = e.target.closest && e.target.closest("[data-wi-param]");
    if (!sel || !sel.value) return;
    var box = lastStep && document.body.contains(lastStep) ? lastStep : document.querySelector('textarea[name="s_text"]');
    var token = "{" + sel.value + "}";
    sel.value = "";
    if (!box) return;
    var a = box.selectionStart == null ? box.value.length : box.selectionStart, b = box.selectionEnd == null ? box.value.length : box.selectionEnd;
    box.value = box.value.slice(0, a) + token + box.value.slice(b);
    box.focus();
    try { box.setSelectionRange(a + token.length, a + token.length); } catch (err) { /* not a text box */ }
    box.dispatchEvent(new Event("input", { bubbles: true }));
  });

  // ---- preview: what's in the editor now (or a revision), for a part, step by step; nothing is saved
  var modal = document.querySelector("[data-wi-modal]"), opener = null;
  function close() { if (modal) { modal.hidden = true; modal.textContent = ""; } if (opener) opener.focus(); }
  function step(pv, d) {
    var all = pv.querySelector("[data-wi-pv-all]");
    var items = pv.querySelectorAll("[data-wi-pv-step]");
    if (!items.length) return;
    var at = +(pv.getAttribute("data-at") || 0);
    if (d === "all") at = at; else at = Math.max(0, Math.min(items.length - 1, at + d));
    pv.setAttribute("data-at", at);
    var showAll = all && all.checked;
    items.forEach(function (li, i) { li.hidden = !showAll && i !== at; });
    var lbl = pv.querySelector("[data-wi-pv-at]");
    if (lbl) lbl.textContent = showAll ? "All " + items.length + " steps" : "Step " + (at + 1) + " of " + items.length;
    var p = pv.querySelector("[data-wi-pv-prev]"), n = pv.querySelector("[data-wi-pv-next]");
    if (p) p.disabled = showAll || at === 0;
    if (n) n.disabled = showAll || at === items.length - 1;
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-wi-preview], [data-wi-pv-close], [data-wi-pv-prev], [data-wi-pv-next]");
    if (!b) return;
    if (b.hasAttribute("data-wi-pv-close")) { close(); return; }
    var pv = b.closest("[data-wi-pv]");
    if (b.hasAttribute("data-wi-pv-prev")) { step(pv, -1); return; }
    if (b.hasAttribute("data-wi-pv-next")) { step(pv, 1); return; }
    e.preventDefault();
    var bar = b.closest("[data-wi-preview-bar]");
    var fd = new FormData(), rev = bar.getAttribute("data-rev");
    if (rev) fd.append("rev", rev);
    else if (form) {
      var n = 0;
      new FormData(form).forEach(function (v, k) {
        if (/^s_files_/.test(k)) { if (v && v.name) n += 1; return; }   // picked photos aren't uploaded just to preview
        fd.append(k, v);
      });
      fd.append("new_files", n);
    }
    fd.append("part", (bar.querySelector("[data-wi-part]") || {}).value || "");
    fd.append("from_part", (bar.querySelector("[data-wi-from]") || {}).value || "");
    opener = b;
    b.disabled = true;
    fetch(bar.getAttribute("data-url"), { method: "POST", body: fd, headers: { "X-CSRFToken": csrf() }, credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status === 403 ? "Sign in again to preview." : "The preview couldn't be made (" + r.status + ")."); return r.text(); })
      .then(function (html) {
        modal.innerHTML = html;
        modal.hidden = false;
        var pv = modal.querySelector("[data-wi-pv]");
        if (pv) step(pv, 0);
        var c = modal.querySelector("[data-wi-pv-close]");
        if (c) c.focus();
      })
      .catch(function (err) { window.alert(err.message); })
      .then(function () { b.disabled = false; });
  });
  document.addEventListener("change", function (e) {
    if (e.target.matches && e.target.matches("[data-wi-pv-all]")) step(e.target.closest("[data-wi-pv]"), "all");
  });
  document.addEventListener("keydown", function (e) {
    if (!modal || modal.hidden) return;
    if (e.key === "Escape") close();
    var pv = modal.querySelector("[data-wi-pv]");
    if (pv && e.key === "ArrowRight") step(pv, 1);
    if (pv && e.key === "ArrowLeft") step(pv, -1);
  });
  // Enter in the part number box previews (and never submits the draft form)
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && e.target.matches && e.target.matches("[data-wi-part], [data-wi-from]")) {
      e.preventDefault();
      var b = e.target.closest("[data-wi-preview-bar]").querySelector("[data-wi-preview]");
      if (b) b.click();
    }
  });
  if (modal) modal.addEventListener("click", function (e) { if (e.target === modal) close(); });
})();

// Work instructions list: New + opens the new-instruction form over the list
(function () {
  "use strict";
  var dlg = document.querySelector("[data-wi-newdlg]");
  if (!dlg) return;
  function open() { if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", ""); var t = dlg.querySelector('input[name="title"]'); if (t) t.focus(); }
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-wi-new]")) open();
    if (e.target.closest && e.target.closest("[data-wi-newclose]")) dlg.close();
    if (e.target === dlg) dlg.close();   // a click on the backdrop
  });
  if (dlg.hasAttribute("data-open")) open();
})();

// Insert variable (by Save): one picker for every field. It goes into the field you were last in, written the way
// that field takes it: [Name] in a Target / Min / Max box or a condition; {Parameter} or {[Name]} in a step.
(function () {
  "use strict";
  var FIELDS = 'textarea[name="s_text"], input[name="s_caution"], textarea[name="s_same"], [data-wi-watch], [data-wi-formula]';
  var last = null;
  document.addEventListener("focusin", function (e) { if (e.target.matches && e.target.matches(FIELDS)) last = e.target; });
  function panel(from) { var root = from && from.closest("form, .wi-actions") || document; return root.querySelector("[data-wi-ins]") || document.querySelector("[data-wi-ins]"); }
  function isStep(el) { return !!el && el.matches('textarea[name="s_text"], input[name="s_caution"], textarea[name="s_same"]'); }
  function label(el) {
    if (!el) return "Click into a step, a Target / Min / Max box or a condition first.";
    if (isStep(el)) return "Into the step you were last in.";
    if (el.matches("[data-wi-watch]")) return "As what this step compares.";
    var n = el.getAttribute("name") || "";
    return "Into " + ({ p_target: "the Target box", p_min: "the Min box", p_max: "the Max box", condition: "Show when", exclude: "Not for filters where" }[n] || "the box you were last in") + ".";
  }
  function params(box) {
    var g = box.querySelector("[data-wi-ins-params]");
    if (!g) return;
    g.querySelectorAll("button").forEach(function (b) { b.remove(); });
    var names = [];
    document.querySelectorAll('input[name="p_name"]').forEach(function (i) { var v = i.value.trim(); if (v && names.indexOf(v) < 0) names.push(v); });
    names.forEach(function (n) {
      [["", n], [":range", n + " (range)"]].forEach(function (x) {
        var b = document.createElement("button");
        b.type = "button"; b.className = "wi-ins-i"; b.setAttribute("data-wi-ins-param", n + x[0]); b.textContent = x[1];
        g.appendChild(b);
      });
    });
    g.hidden = !names.length || !(isStep(last) || (last && last.matches("[data-wi-watch]")));   // a parameter goes into a step
  }
  function filter(box) {
    var q = (box.querySelector("[data-wi-ins-find]").value || "").trim().toLowerCase();
    box.querySelectorAll(".wi-ins-g").forEach(function (g) {
      var any = false;
      g.querySelectorAll(".wi-ins-i").forEach(function (b) { var ok = !q || b.textContent.toLowerCase().indexOf(q) >= 0; b.hidden = !ok; any = any || ok; });
      if (!g.hasAttribute("data-wi-ins-params") || !g.hidden) g.style.display = any ? "" : "none";
    });
  }
  function open(btn) {
    var box = panel(btn);
    if (!box) return;
    params(box);
    box.querySelector("[data-wi-ins-into]").textContent = label(last && document.body.contains(last) ? last : null);
    box.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    var f = box.querySelector("[data-wi-ins-find]");
    f.value = ""; filter(box); f.focus();
  }
  function close(box) {
    if (!box) return;
    box.hidden = true;
    document.querySelectorAll("[data-wi-ins-open]").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
  }
  function insert(token) {
    var el = last && document.body.contains(last) ? last : document.querySelector('textarea[name="s_text"]');
    if (!el) return;
    var a = el.selectionStart == null ? el.value.length : el.selectionStart, b = el.selectionEnd == null ? el.value.length : el.selectionEnd;
    el.value = el.value.slice(0, a) + token + el.value.slice(b);
    el.focus();
    try { el.setSelectionRange(a + token.length, a + token.length); } catch (err) { /* not a text box */ }
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest("[data-wi-ins-open], [data-wi-ins-var], [data-wi-ins-param]");
    if (t && t.hasAttribute("data-wi-ins-open")) {
      var box = panel(t);
      if (box && !box.hidden) close(box); else open(t);
      return;
    }
    if (t) {
      var box2 = t.closest("[data-wi-ins]"), v = t.getAttribute("data-wi-ins-var"), p = t.getAttribute("data-wi-ins-param");
      var step = isStep(last);
      var watch = last && last.matches && last.matches("[data-wi-watch]");
      if (watch) { last.value = (p || v).replace(/:range$/, ""); last.dispatchEvent(new Event("input", { bubbles: true })); close(box2); return; }
      insert(p ? "{" + p + "}" : step ? "{" + v + "}" : "[" + v + "]");
      close(box2);
      return;
    }
    document.querySelectorAll("[data-wi-ins]").forEach(function (box) {
      if (!box.hidden && !(e.target.closest && e.target.closest("[data-wi-ins]"))) close(box);
    });
  });
  document.addEventListener("input", function (e) { if (e.target.matches && e.target.matches("[data-wi-ins-find]")) filter(e.target.closest("[data-wi-ins]")); });
  document.addEventListener("keydown", function (e) {
    if (!(e.target.matches && e.target.matches("[data-wi-ins-find]"))) return;
    var box = e.target.closest("[data-wi-ins]");
    if (e.key === "Escape") { close(box); if (last) last.focus(); }
    if (e.key === "Enter") {
      e.preventDefault();
      var q = e.target.value.trim().toLowerCase();
      var shown = Array.prototype.filter.call(box.querySelectorAll(".wi-ins-i"), function (b) { return !b.hidden && b.closest(".wi-ins-g").style.display !== "none" && !b.closest(".wi-ins-g").hidden; });
      var rank = function (b) { var t = b.textContent.trim().toLowerCase(); return t === q ? 0 : t.indexOf(q) === 0 ? 1 : 2; };   // exact, then starts with
      shown.sort(function (a, b) { return rank(a) - rank(b); });
      if (shown[0]) shown[0].click();
    }
  });
})();
