/* Connector builder (Admin › Connector builder): a wire view. On the left, a card for every table in use (and any
   added with "Add a source"): each column is a dot. On the right, every input the suite needs is a dot. Drag from
   one dot to the other to wire them (either way round), click a wire to unwire it, or tap a column and then tap an
   input. Wires are always drawn from the card of the table a box reads, saved (blue) or not yet saved (dashed).
   One table can feed many boxes (a table that logs every machine: each machine box wires "Which machine").
   Works with a mouse, a touchscreen (pointer events) and the keyboard (Enter on a column, then Enter on an input).
   Each box on the right keeps its own wires until its Save; saving redraws only that box. */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";
  const DBN = {machines: "machine data database", assets: "asset database", s2k: "S2K database"};
  let picked = null;     // column picked up by a tap
  let drag = null;       // {from: tag|slot, x0, y0, moved, id, root}

  function state(g) {
    if (!g._st) {
      try { g._st = JSON.parse(g.dataset.state || "{}"); } catch (e) { g._st = {}; }
      g._st.map = g._st.map || {};
      g._orig = JSON.stringify(g._st);
    }
    return g._st;
  }
  const orig = g => { state(g); try { return JSON.parse(g._orig); } catch (e) { return {map: {}}; } };
  const label = sl => sl.querySelector(".sl").textContent.replace("*", "").trim();
  const card = (root, db, table) => [...root.querySelectorAll(".srcard")].find(c => c.dataset.db === db && c.dataset.table === table);
  const portOf = (root, db, table, col) => { const c = card(root, db, table); return c && c.querySelector(`.port.out[data-col="${CSS.escape(col)}"]`); };

  function say(g, text, bad) {
    const m = g.querySelector(".gmsg");
    if (m) { m.innerHTML = ""; const s = document.createElement("span"); s.className = bad ? "badt" : "okt"; s.textContent = text; m.appendChild(s); }
  }

  function paint(g) {
    const st = state(g), root = g.closest(".connector");
    g.querySelectorAll(".slot").forEach(sl => {
      const col = st.map[sl.dataset.slot] || "";
      sl.classList.toggle("set", !!col);
      sl.querySelector(".sv").textContent = col;
      const x = sl.querySelector(".sx"); if (x) x.hidden = !col;
      sl.setAttribute("aria-label", label(sl) + ": " + (col || "empty"));
    });
    const tb = g.querySelector(".gsrc .tb"); if (tb) tb.textContent = st.table || "none yet";
    const changed = JSON.stringify(st) !== g._orig;
    const save = g.querySelector("[data-save]"); if (save) save.disabled = !changed;
    const undo = g.querySelector("[data-undo]"); if (undo) undo.hidden = !changed;
    if (changed) g.dataset.dirty = "1"; else delete g.dataset.dirty;
    const missing = [...g.querySelectorAll(".slot.req")].filter(sl => !st.map[sl.dataset.slot]).map(label);
    if (changed) {
      const shared = g.dataset.kind === "machine" && st.table && !st.map.keyCol &&
        [...root.querySelectorAll('.cgroup[data-kind="machine"]')].some(o => o !== g && state(o).table === st.table && state(o).db === st.db);
      say(g, missing.length ? "Still needed: " + missing.join(", ") :
             shared ? "Other machines read this table too: wire \"Which machine\" and type this machine's value." : "Ready to save.",
          !!missing.length || shared);
    }
    soon(root);
  }

  function flash(el, kind) { el.classList.remove("fl-ok", "fl-no"); void el.offsetWidth; el.classList.add("fl-" + kind); }

  function place(slot, tag) {
    const g = slot.closest(".cgroup"); if (!g || g.closest("[data-readonly]")) return;
    const st = state(g);
    const dbs = (g.dataset.dbs || "").split(",");
    if (!dbs.includes(tag.dataset.db)) {
      say(g, "This needs a column from the " + dbs.map(d => DBN[d] || d).join(" or ") + ". Add a table from that database on the left.", true);
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
  function clipRect(el) {   // the visible part of a column: its card's list, within the scrolling column of cards
    let top = -Infinity, bottom = Infinity;
    for (let p = el.parentElement; p && !p.matches("[data-wire]"); p = p.parentElement) {
      if (p.matches(".ports, .srcs")) { const r = p.getBoundingClientRect(); top = Math.max(top, r.top); bottom = Math.min(bottom, r.bottom); }
    }
    return {top, bottom};
  }
  function dotAt(el, side, box, clip) {
    const d = el.querySelector(".dot") || el, r = d.getBoundingClientRect();
    let y = r.top + r.height / 2, out = false;
    if (clip) {
      const c = clipRect(el);
      if (y < c.top + 4) { y = c.top + 4; out = true; } else if (y > c.bottom - 4) { y = c.bottom - 4; out = true; }
    }
    return {x: (side === "r" ? r.right - 2 : r.left + 2) - box.left, y: y - box.top, out};
  }
  function curve(a, b) {
    const dx = Math.max(40, Math.abs(b.x - a.x) / 2);
    return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`;
  }
  function redraw(root) {
    const wire = root && root.querySelector("[data-wire]"); if (!wire) return;
    const svg = wire.querySelector("svg.wires"); if (!svg || getComputedStyle(svg).display === "none") return;
    const box = wire.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
    svg.innerHTML = "";
    const used = new Set(), count = new Map();
    root.querySelectorAll(".cgroup").forEach(g => {
      const st = state(g), o = orig(g);
      if (!st.table) return;
      const c = card(root, st.db, st.table);
      g.classList.toggle("nocard", !c);
      if (!c) return;
      for (const [k, col] of Object.entries(st.map)) {
        const tag = portOf(root, st.db, st.table, col);
        const sl = g.querySelector(`.slot[data-slot="${CSS.escape(k)}"]`);
        if (!tag || !sl) continue;
        used.add(tag); count.set(c, (count.get(c) || 0) + 1);
        if (tag.hidden) continue;
        const a = dotAt(tag, "r", box, true), b = dotAt(sl, "l", box);
        const p = document.createElementNS(NS, "path");
        const saved = o.table === st.table && o.db === st.db && (o.map || {})[k] === col;
        p.setAttribute("d", curve(a, b));
        p.setAttribute("class", saved ? "saved" : "pending");
        if (a.out) p.setAttribute("opacity", ".4");
        p.dataset.group = g.dataset.group; p.dataset.slot = k; p.dataset.col = col;
        const t = document.createElementNS(NS, "title");
        t.textContent = `${st.table}.${col} → ${label(sl)} (${g.querySelector(".wh b").textContent}). Click to unwire.`;
        p.appendChild(t);
        svg.appendChild(p);
      }
    });
    root.querySelectorAll(".port.out").forEach(t => t.classList.toggle("wired", used.has(t)));
    root.querySelectorAll(".srcard").forEach(c => { const w = c.querySelector(".wcount"), n = count.get(c) || 0; if (w) w.textContent = n ? n + " wire" + (n > 1 ? "s" : "") : ""; });
    if (drag && drag.moved && drag.end) {
      const fromOut = drag.from.classList.contains("out"), end = {x: drag.end.x - box.left, y: drag.end.y - box.top};
      const a = fromOut ? dotAt(drag.from, "r", box, true) : end, b = fromOut ? end : dotAt(drag.from, "l", box);
      const p = document.createElementNS(NS, "path"); p.setAttribute("d", curve(a, b)); p.setAttribute("class", "live"); svg.appendChild(p);
    }
  }
  let raf = 0;
  function soon(root) {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; document.querySelectorAll(".connector").forEach(redraw); });
  }

  // ---------------------------------------------------------------- the "Add a source" picker
  function fillTables(form) {
    let all = {};
    try { all = JSON.parse(form.dataset.tables || "{}"); } catch (e) { all = {}; }
    const db = form.querySelector("select[name=db]").value, sel = form.querySelector("select[name=table]");
    sel.innerHTML = "";
    const first = document.createElement("option"); first.value = ""; first.textContent = (all[db] || []).length ? "Pick a table…" : "No tables (or the database didn't answer)";
    sel.appendChild(first);
    for (const t of all[db] || []) { const o = document.createElement("option"); o.value = o.textContent = t; sel.appendChild(o); }
    sel.value = "";
    form.querySelector("[data-add]").disabled = true;
  }

  function init(root) {
    if (root._ready) return; root._ready = true;
    root.querySelectorAll(".cgroup").forEach(g => { state(g); paint(g); });
    const add = root.querySelector(".addsrc");
    if (add) {
      fillTables(add);
      add.addEventListener("change", e => {
        if (e.target.name === "db") fillTables(add);
        if (e.target.name === "table") add.querySelector("[data-add]").disabled = !e.target.value;
        add.querySelector(".addmsg").textContent = "";
      });
      add.addEventListener("htmx:confirm", e => {   // a table that already has a card: just show it
        const db = add.querySelector("select[name=db]").value, t = add.querySelector("select[name=table]").value;
        const c = card(root, db, t);
        if (c) { e.preventDefault(); c.scrollIntoView({behavior: "smooth", block: "nearest"}); flash(c, "ok"); add.querySelector(".addmsg").textContent = t + " is already on the left."; }
      });
      add.addEventListener("htmx:afterRequest", e => {
        if (e.detail.successful) { add.querySelector("select[name=table]").value = ""; add.querySelector("[data-add]").disabled = true; }
      });
    }
    root.addEventListener("input", e => {
      if (!e.target.matches(".cfilter")) return;
      const v = e.target.value.trim().toLowerCase();
      e.target.closest(".srcard").querySelectorAll(".port.out").forEach(t => { t.hidden = v && !t.dataset.col.toLowerCase().includes(v); });
      soon(root);
    });
    root.addEventListener("change", e => {
      if (!e.target.matches(".gcopy") || !e.target.value) return;
      const g = e.target.closest(".cgroup"), from = root.querySelector(`.cgroup[data-group="${CSS.escape(e.target.value)}"]`);
      const name = e.target.selectedOptions[0].textContent;
      e.target.value = "";
      if (!from || !state(from).table) { say(g, name + " isn't wired to a table yet.", true); return; }
      const f = state(from), st = state(g);
      st.db = f.db; st.table = f.table; st.map = Object.assign({}, f.map);
      const kv = g.querySelector("input[name=keyVal]");
      if (kv && !kv.value.trim()) kv.value = g.querySelector(".wh b").textContent.trim();
      paint(g);
      say(g, `Copied ${name}'s wires (${f.table}).` + (st.map.keyCol ? ` Check "${kv ? kv.value : ""}" is this machine's value in ${st.map.keyCol}.` : ' Wire "Which machine" so each machine takes only its rows.'), !st.map.keyCol);
    });
    root.addEventListener("click", e => {
      const chip = e.target.closest("[data-goto]");
      if (chip) { const g = root.querySelector(`.cgroup[data-group="${CSS.escape(chip.dataset.goto)}"]`); if (g) { g.scrollIntoView({behavior: "smooth", block: "center"}); flash(g, "ok"); } return; }
      const close = e.target.closest(".srcard [data-close]");
      if (close) {
        const c = close.closest(".srcard");
        const users = [...root.querySelectorAll(".cgroup")].filter(g => state(g).table === c.dataset.table && state(g).db === c.dataset.db);
        if (users.length) { c.querySelector(".wcount").textContent = "In use by " + users.map(g => g.querySelector(".wh b").textContent).join(", "); flash(c, "no"); return; }
        if (picked && c.contains(picked)) unpick();
        c.remove(); soon(root); return;
      }
      const x = e.target.closest(".sx");
      if (x) { const sl = x.closest(".slot"), g = sl.closest(".cgroup"); delete state(g).map[sl.dataset.slot]; paint(g); e.stopPropagation(); return; }
      const undo = e.target.closest("[data-undo]");
      if (undo) { const g = undo.closest(".cgroup"); g._st = JSON.parse(g._orig); paint(g); say(g, "Changes undone.", false); return; }
      const tag = e.target.closest(".port.out");
      if (tag) { if (!tag._dragged) pick(tag); tag._dragged = false; return; }
      const sl = e.target.closest(".slot");
      if (sl && picked) { place(sl, picked); unpick(); return; }
      if (sl && !sl._dragged) {   // show where an input's wire goes
        const g = sl.closest(".cgroup"), st = state(g), col = st.map[sl.dataset.slot];
        root.querySelectorAll(".wires path").forEach(p => p.classList.toggle("hl", p.dataset.group === g.dataset.group && p.dataset.slot === sl.dataset.slot));
        const t = col && portOf(root, st.db, st.table, col); if (t) t.scrollIntoView({block: "nearest"});
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
    root.addEventListener("pointerdown", e => {
      if (e.button > 0 || e.target.closest(".sx,select,input,textarea,[data-close]")) return;
      const from = e.target.closest(".port.out") || (e.target.closest(".slot .dot") && e.target.closest(".slot"));
      if (!from) return;
      e.preventDefault();   // no text selection while drawing a wire
      drag = {from, x0: e.clientX, y0: e.clientY, moved: false, id: e.pointerId, root};
    });
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

  // dragging a wire (once for the page; the builder can be opened again and again)
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
  function end(e) {
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
  }
  window.addEventListener("pointerup", end);
  window.addEventListener("pointercancel", end);

  // the wires follow the page and the column lists as they scroll or resize
  document.addEventListener("scroll", () => soon(), true);
  window.addEventListener("resize", () => soon());
  function scan(el) {
    if (!el || !el.querySelectorAll) return;
    if (el.matches && el.matches("[data-connector]")) init(el);
    el.querySelectorAll("[data-connector]").forEach(init);
    // a box redrawn after its Save, or a new source card: take it in
    const root = el.closest && el.closest("[data-connector]");
    if (root && root._ready) {
      if (el.matches(".cgroup")) { state(el); paint(el); }
      soon(root);
    }
  }
  document.addEventListener("DOMContentLoaded", () => scan(document.body));
  document.addEventListener("htmx:load", e => scan(e.detail.elt));
  document.addEventListener("htmx:afterSettle", () => soon());
})();
