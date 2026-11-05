/* Connector builder (Admin › Connector builder): a wire view. Every column of the source table is a dot on the
   left; every input the suite needs is a dot on the right. Drag from one dot to the other to wire them (either
   way round), click a wire to unwire it, or tap a column and then tap an input. Works with a mouse, a
   touchscreen (pointer events) and the keyboard (Enter on a column, then Enter on an input). Each box on the
   right keeps its own wires until its Save. */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const DBN = {machines: "machine data database", assets: "asset database", s2k: "S2K database"};
  let picked = null;     // column picked up by a tap
  let drag = null;       // {from: tag|slot, x0, y0, moved, id}

  function state(g) {
    if (!g._st) {
      try { g._st = JSON.parse(g.dataset.state || "{}"); } catch (e) { g._st = {}; }
      g._st.map = g._st.map || {};
      g._orig = JSON.stringify(g._st);
    }
    return g._st;
  }
  const orig = g => { state(g); try { return JSON.parse(g._orig); } catch (e) { return {map: {}}; } };
  function src(root) {
    const s = root.querySelector(".src select[name=db]"), t = root.querySelector(".src select[name=table]");
    return {db: s ? s.value : "", table: t ? t.value : ""};
  }

  function say(g, text, bad) {
    const m = g.querySelector(".gmsg");
    if (m) { m.innerHTML = ""; const s = document.createElement("span"); s.className = bad ? "badt" : "okt"; s.textContent = text; m.appendChild(s); }
  }

  function paint(g) {
    const st = state(g), root = g.closest(".connector"), cur = src(root);
    const other = st.table && (st.table !== cur.table || st.db !== cur.db);
    g.querySelectorAll(".slot").forEach(sl => {
      const col = st.map[sl.dataset.slot] || "";
      sl.classList.toggle("set", !!col);
      sl.querySelector(".sv").textContent = col ? (other ? st.table + "." + col : col) : "";
      const x = sl.querySelector(".sx"); if (x) x.hidden = !col;
      sl.setAttribute("aria-label", sl.querySelector(".sl").textContent.replace("*", "").trim() + ": " + (col || "empty"));
    });
    const tb = g.querySelector(".gsrc .tb"); if (tb) tb.textContent = st.table || "none yet";
    const changed = JSON.stringify(st) !== g._orig;
    const save = g.querySelector("[data-save]"); if (save) save.disabled = !changed;
    const undo = g.querySelector("[data-undo]"); if (undo) undo.hidden = !changed;
    if (changed) g.dataset.dirty = "1"; else delete g.dataset.dirty;
    const missing = [...g.querySelectorAll(".slot.req")].filter(sl => !st.map[sl.dataset.slot]).map(sl => sl.querySelector(".sl").textContent.replace("*", "").trim());
    if (changed) say(g, missing.length ? "Still needed: " + missing.join(", ") : "Ready to save.", !!missing.length);
    redraw(root);
  }

  function flash(el, kind) { el.classList.remove("fl-ok", "fl-no"); void el.offsetWidth; el.classList.add("fl-" + kind); }

  function place(slot, tag) {
    const g = slot.closest(".cgroup"); if (!g || g.closest("[data-readonly]")) return;
    const st = state(g);
    const dbs = (g.dataset.dbs || "").split(",");
    if (!dbs.includes(tag.dataset.db)) {
      say(g, "This needs a column from the " + dbs.map(d => DBN[d] || d).join(" or ") + ". Pick that database in the Source box.", true);
      flash(slot, "no"); return;
    }
    if (st.table && (st.table !== tag.dataset.table || st.db !== tag.dataset.db) && Object.keys(st.map).length) {
      st.map = {};   // one box reads one table: switching tables starts this box's wires over
      say(g, "Switched to " + tag.dataset.table + ": this box's other wires were cleared.", false);
    }
    st.db = tag.dataset.db; st.table = tag.dataset.table;
    st.map[slot.dataset.slot] = tag.dataset.col;
    paint(g); flash(slot, "ok");
  }

  function unpick() { if (picked) picked.classList.remove("picked"); picked = null; document.querySelectorAll(".connector").forEach(c => c.classList.remove("picking")); }
  function pick(tag) {
    if (picked === tag) { unpick(); return; }
    unpick(); picked = tag; tag.classList.add("picked"); tag.closest(".connector").classList.add("picking");
  }

  // ---------------------------------------------------------------- wires
  function dotAt(el, side, box, clip) {
    const d = el.querySelector(".dot") || el, r = d.getBoundingClientRect();
    let y = r.top + r.height / 2;
    let out = false;
    if (clip) { const c = clip.getBoundingClientRect(); if (y < c.top + 4) { y = c.top + 4; out = true; } else if (y > c.bottom - 4) { y = c.bottom - 4; out = true; } }
    return {x: (side === "r" ? r.right - 2 : r.left + 2) - box.left, y: y - box.top, out};
  }
  function curve(a, b) {
    const dx = Math.max(40, Math.abs(b.x - a.x) / 2);
    return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
  }
  function redraw(root) {
    const wire = root && root.querySelector("[data-wire]"); if (!wire) return;
    const svg = wire.querySelector("svg.wires"); if (!svg || getComputedStyle(svg).display === "none") return;
    const box = wire.getBoundingClientRect(), cur = src(root), clip = root.querySelector(".src .ports");
    svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
    svg.innerHTML = "";
    const used = new Set();
    root.querySelectorAll(".cgroup").forEach(g => {
      const st = state(g), o = orig(g);
      const ok = (g.dataset.dbs || "").split(",").includes(cur.db);
      g.classList.toggle("other-db", !!cur.db && !ok);
      if (st.table !== cur.table || st.db !== cur.db) return;
      for (const [k, col] of Object.entries(st.map)) {
        const tag = root.querySelector(`.port.out[data-col="${CSS.escape(col)}"]`);
        const sl = g.querySelector(`.slot[data-slot="${CSS.escape(k)}"]`);
        if (!tag || !sl || tag.hidden) continue;
        used.add(tag);
        const a = dotAt(tag, "r", box, clip), b = dotAt(sl, "l", box);
        const p = document.createElementNS(NS, "path");
        const saved = o.table === st.table && o.db === st.db && (o.map || {})[k] === col;
        p.setAttribute("d", curve(a, b));
        p.setAttribute("class", saved ? "saved" : "pending");
        if (a.out) p.setAttribute("opacity", ".45");
        p.dataset.group = g.dataset.group; p.dataset.slot = k; p.dataset.col = col;
        const t = document.createElementNS(NS, "title");
        t.textContent = `${col} → ${sl.querySelector(".sl").textContent.replace("*", "").trim()} (${g.querySelector(".wh b").textContent}). Click to unwire.`;
        p.appendChild(t);
        svg.appendChild(p);
      }
    });
    root.querySelectorAll(".port.out").forEach(t => t.classList.toggle("wired", used.has(t)));
    if (drag && drag.moved && drag.end) {
      const a = drag.from.classList.contains("out") ? dotAt(drag.from, "r", box) : {x: drag.end.x - box.left, y: drag.end.y - box.top};
      const b = drag.from.classList.contains("out") ? {x: drag.end.x - box.left, y: drag.end.y - box.top} : dotAt(drag.from, "l", box);
      const p = document.createElementNS(NS, "path"); p.setAttribute("d", curve(a, b)); p.setAttribute("class", "live"); svg.appendChild(p);
    }
  }
  let raf = 0;
  const soon = root => { if (raf) return; raf = requestAnimationFrame(() => { raf = 0; redraw(root || document.querySelector(".connector")); }); };

  function init(root) {
    if (root._ready) return; root._ready = true;
    root.querySelectorAll(".cgroup").forEach(g => { state(g); paint(g); });
    const filt = root.querySelector(".cfilter");
    if (filt) filt.addEventListener("input", () => {
      const v = filt.value.trim().toLowerCase();
      root.querySelectorAll(".port.out").forEach(t => { t.hidden = v && !t.dataset.col.toLowerCase().includes(v); });
      soon(root);
    });
    root.addEventListener("click", e => {
      const chip = e.target.closest("[data-goto]");
      if (chip) { const g = root.querySelector(`.cgroup[data-group="${CSS.escape(chip.dataset.goto)}"]`); if (g) { g.scrollIntoView({behavior: "smooth", block: "center"}); flash(g, "ok"); } return; }
      const x = e.target.closest(".sx");
      if (x) { const sl = x.closest(".slot"), g = sl.closest(".cgroup"); delete state(g).map[sl.dataset.slot]; paint(g); e.stopPropagation(); return; }
      const undo = e.target.closest("[data-undo]");
      if (undo) { const g = undo.closest(".cgroup"); g._st = JSON.parse(g._orig); paint(g); say(g, "Changes undone.", false); return; }
      const tag = e.target.closest(".port.out");
      if (tag) { if (!tag._dragged) pick(tag); tag._dragged = false; return; }
      const sl = e.target.closest(".slot");
      if (sl && picked) { place(sl, picked); unpick(); return; }
      if (sl && !sl._dragged) {   // show where an input's wire goes
        const g = sl.closest(".cgroup"), col = state(g).map[sl.dataset.slot];
        root.querySelectorAll(".wires path").forEach(p => p.classList.toggle("hl", p.dataset.group === g.dataset.group && p.dataset.slot === sl.dataset.slot));
        if (col) { const t = root.querySelector(`.port.out[data-col="${CSS.escape(col)}"]`); if (t) t.scrollIntoView({block: "nearest"}); }
      }
      if (sl) sl._dragged = false;
    });
    // a wire is clicked to unwire it (the SVG sits over the page; only the wire strokes take clicks)
    root.querySelector("svg.wires")?.addEventListener("click", e => {
      const p = e.target.closest("path[data-slot]"); if (!p) return;
      const g = root.querySelector(`.cgroup[data-group="${CSS.escape(p.dataset.group)}"]`); if (!g) return;
      delete state(g).map[p.dataset.slot]; paint(g); say(g, `Unwired ${p.dataset.col}.`, false);
    });
    root.addEventListener("keydown", e => {
      if ((e.key === "Enter" || e.key === " ") && e.target.matches(".slot") && picked) { e.preventDefault(); place(e.target, picked); unpick(); }
      if (e.key === "Escape") unpick();
    });
    // dragging a wire, from a column or from an input
    root.addEventListener("pointerdown", e => {
      if (e.button > 0 || e.target.closest(".sx,select,input,textarea")) return;
      const from = e.target.closest(".port.out") || (e.target.closest(".slot .dot") && e.target.closest(".slot"));
      if (!from) return;
      e.preventDefault();   // no text selection while drawing a wire
      drag = {from, x0: e.clientX, y0: e.clientY, moved: false, id: e.pointerId, root};
    });
    window.addEventListener("pointermove", e => {
      if (!drag || e.pointerId !== drag.id) return;
      if (!drag.moved && Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < 6) return;
      if (!drag.moved) { drag.moved = true; unpick(); document.body.classList.add("c-dragging"); }
      drag.end = {x: e.clientX, y: e.clientY};
      document.querySelectorAll(".port.over").forEach(s => s.classList.remove("over"));
      const over = document.elementFromPoint(e.clientX, e.clientY);
      const tgt = over && (drag.from.classList.contains("out") ? over.closest(".slot") : over.closest(".port.out"));
      if (tgt) tgt.classList.add("over");
      redraw(drag.root);
      e.preventDefault();
    }, {passive: false});
    const end = e => {
      if (!drag || e.pointerId !== drag.id) return;
      const d = drag; drag = null;
      document.body.classList.remove("c-dragging");
      document.querySelectorAll(".port.over").forEach(s => s.classList.remove("over"));
      if (!d.moved) { redraw(d.root); return; }
      d.from._dragged = true;
      const over = document.elementFromPoint(e.clientX, e.clientY);
      if (d.from.classList.contains("out")) { const sl = over && over.closest(".slot"); if (sl) place(sl, d.from); }
      else { const tag = over && over.closest(".port.out"); if (tag) place(d.from, tag); }
      redraw(d.root);
    };
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    // Save sends this box's wires
    root.addEventListener("htmx:configRequest", e => {
      const g = e.detail.elt.closest && e.detail.elt.closest(".cgroup"); if (!g) return;
      const st = state(g);
      e.detail.parameters.map = JSON.stringify(st.map);
      e.detail.parameters.db = st.db || "";
      e.detail.parameters.table = st.table || "";
    });
    soon(root);
  }

  // the wires follow the page and the column list as they scroll or resize
  document.addEventListener("scroll", () => soon(), true);
  window.addEventListener("resize", () => soon());
  function scan(el) { (el || document).querySelectorAll("[data-connector]").forEach(init); if (el && el.matches && el.matches("[data-connector]")) init(el); }
  document.addEventListener("DOMContentLoaded", () => scan(document));
  document.addEventListener("htmx:load", e => scan(e.detail.elt));
  document.addEventListener("htmx:afterSettle", () => soon());
})();
