/* Connector builder (Admin › Connector builder): drag a column onto what the suite needs.
   Works with a mouse, a touchscreen (pointer events, not HTML drag and drop) and the keyboard: tap or Enter on a
   column picks it up, then tap or Enter on a slot puts it there. Every card keeps its own links until Save. */
(function () {
  "use strict";
  let picked = null;     // the column tag picked up by a tap
  let drag = null;       // {tag, ghost, x0, y0, moved}

  function state(g) {
    if (!g._st) {
      try { g._st = JSON.parse(g.dataset.state || "{}"); } catch (e) { g._st = {}; }
      g._st.map = g._st.map || {};
      g._orig = JSON.stringify(g._st);
    }
    return g._st;
  }

  function say(g, text, bad) {
    const m = g.querySelector(".gmsg");
    if (m) { m.innerHTML = ""; const s = document.createElement("span"); s.className = bad ? "badt" : "okt"; s.textContent = text; m.appendChild(s); }
  }

  function paint(g) {
    const st = state(g);
    g.querySelectorAll(".slot").forEach(sl => {
      const col = st.map[sl.dataset.slot] || "";
      sl.classList.toggle("set", !!col);
      sl.querySelector(".sv").textContent = col || "drop a column";
      const x = sl.querySelector(".sx"); if (x) x.hidden = !col;
      sl.setAttribute("aria-label", sl.querySelector(".sl").textContent.replace("*", "") + ": " + (col || "empty"));
    });
    const tb = g.querySelector(".gsrc .tb"); if (tb) tb.textContent = st.table || "no table yet";
    const changed = JSON.stringify(st) !== g._orig;
    const save = g.querySelector("[data-save]"); if (save) save.disabled = !changed;
    const undo = g.querySelector("[data-undo]"); if (undo) undo.hidden = !changed;
    if (changed) g.dataset.dirty = "1"; else delete g.dataset.dirty;
    const missing = [...g.querySelectorAll(".slot.req")].filter(sl => !st.map[sl.dataset.slot]).map(sl => sl.querySelector(".sl").textContent.replace("*", "").trim());
    if (changed) say(g, missing.length ? "Still needed: " + missing.join(", ") : "Ready to save.", !!missing.length);
  }

  function place(slot, tag) {
    const g = slot.closest(".cgroup"); if (!g || g.closest("[data-readonly]")) return;
    const st = state(g);
    const dbs = (g.dataset.dbs || "").split(",");
    if (!dbs.includes(tag.dataset.db)) {
      say(g, "This needs a column from the " + dbs.map(d => ({machines: "machine data database", assets: "asset database", s2k: "S2K database"})[d] || d).join(" or ") + ". Pick that database on the left.", true);
      flash(slot, "no"); return;
    }
    if (st.table && (st.table !== tag.dataset.table || st.db !== tag.dataset.db) && Object.keys(st.map).length) {
      // one card reads one table: switching tables starts this card's links over
      st.map = {};
      say(g, "Switched to " + tag.dataset.table + ": the other links on this card were cleared.", false);
    }
    st.db = tag.dataset.db; st.table = tag.dataset.table;
    st.map[slot.dataset.slot] = tag.dataset.col;
    paint(g); flash(slot, "ok");
  }

  function flash(el, kind) { el.classList.remove("fl-ok", "fl-no"); void el.offsetWidth; el.classList.add("fl-" + kind); }

  function unpick() { if (picked) picked.classList.remove("picked"); picked = null; document.querySelectorAll(".connector").forEach(c => c.classList.remove("picking")); }

  function pick(tag) {
    if (picked === tag) { unpick(); return; }
    unpick(); picked = tag; tag.classList.add("picked");
    tag.closest(".connector").classList.add("picking");
  }

  function init(root) {
    if (root._ready) return; root._ready = true;
    root.querySelectorAll(".cgroup").forEach(g => { state(g); });
    const filt = root.querySelector(".cfilter");
    if (filt) filt.addEventListener("input", () => {
      const v = filt.value.trim().toLowerCase();
      root.querySelectorAll(".ctag").forEach(t => { t.hidden = v && !t.dataset.col.toLowerCase().includes(v); });
    });
    root.addEventListener("click", e => {
      const chip = e.target.closest("[data-goto]");
      if (chip) { const g = root.querySelector(`.cgroup[data-group="${CSS.escape(chip.dataset.goto)}"]`); if (g) { g.scrollIntoView({behavior: "smooth", block: "center"}); flash(g, "ok"); } return; }
      const x = e.target.closest(".sx");
      if (x) { const sl = x.closest(".slot"), g = sl.closest(".cgroup"); delete state(g).map[sl.dataset.slot]; paint(g); e.stopPropagation(); return; }
      const undo = e.target.closest("[data-undo]");
      if (undo) { const g = undo.closest(".cgroup"); g._st = JSON.parse(g._orig); paint(g); say(g, "Changes undone.", false); return; }
      const tag = e.target.closest(".ctag");
      if (tag) { if (!tag._dragged) pick(tag); tag._dragged = false; return; }
      const sl = e.target.closest(".slot");
      if (sl && picked) { place(sl, picked); unpick(); }
    });
    root.addEventListener("keydown", e => {
      if ((e.key === "Enter" || e.key === " ") && e.target.matches(".slot") && picked) { e.preventDefault(); place(e.target, picked); unpick(); }
      if (e.key === "Escape") unpick();
    });
    // pointer dragging: mouse, pen and touch
    root.addEventListener("pointerdown", e => {
      const tag = e.target.closest(".ctag"); if (!tag || e.button > 0) return;
      drag = {tag, x0: e.clientX, y0: e.clientY, moved: false, ghost: null, id: e.pointerId};
    });
    window.addEventListener("pointermove", e => {
      if (!drag || e.pointerId !== drag.id) return;
      if (!drag.moved && Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < 8) return;
      if (!drag.moved) {
        drag.moved = true; unpick();
        drag.ghost = drag.tag.cloneNode(true); drag.ghost.classList.add("ghost"); document.body.appendChild(drag.ghost);
        document.body.classList.add("c-dragging");
        try { drag.tag.releasePointerCapture(e.pointerId); } catch (_) { /* not captured */ }
      }
      drag.ghost.style.transform = `translate(${e.clientX + 8}px, ${e.clientY + 8}px)`;
      document.querySelectorAll(".slot.over").forEach(s => s.classList.remove("over"));
      const over = document.elementFromPoint(e.clientX, e.clientY);
      const sl = over && over.closest(".slot"); if (sl) sl.classList.add("over");
      e.preventDefault();
    }, {passive: false});
    const end = e => {
      if (!drag || e.pointerId !== drag.id) return;
      const d = drag; drag = null;
      document.body.classList.remove("c-dragging");
      document.querySelectorAll(".slot.over").forEach(s => s.classList.remove("over"));
      if (!d.moved) return;
      d.tag._dragged = true;
      if (d.ghost) d.ghost.remove();
      const over = document.elementFromPoint(e.clientX, e.clientY);
      const sl = over && over.closest(".slot");
      if (sl) place(sl, d.tag);
    };
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    // Save sends this card's links
    root.addEventListener("htmx:configRequest", e => {
      const g = e.detail.elt.closest && e.detail.elt.closest(".cgroup"); if (!g) return;
      const st = state(g);
      e.detail.parameters.map = JSON.stringify(st.map);
      e.detail.parameters.db = st.db || "";
      e.detail.parameters.table = st.table || "";
    });
  }

  function scan(el) { (el || document).querySelectorAll("[data-connector]").forEach(init); if (el && el.matches && el.matches("[data-connector]")) init(el); }
  document.addEventListener("DOMContentLoaded", () => scan(document));
  document.addEventListener("htmx:load", e => scan(e.detail.elt));
})();
