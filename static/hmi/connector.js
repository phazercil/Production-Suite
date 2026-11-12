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
      g._st.where = g._st.where || {};
      if (g.dataset.kind === "machine") g._st.run2Name = g._st.run2Name || "";
      g._orig = JSON.stringify(g._st);
    }
    return g._st;
  }
  const orig = g => { state(g); try { return JSON.parse(g._orig); } catch (e) { return {map: {}}; } };
  const label = sl => sl.querySelector(".sl").textContent.replace("*", "").trim();
  const card = (root, src) => src ? root.querySelector(`.srcard[data-src="${CSS.escape(String(src))}"]`) : null;
  const portOf = (root, src, col) => { const c = card(root, src); return c && c.querySelector(`.port.out[data-col="${CSS.escape(col)}"]`); };
  const title = (root, src) => { const c = card(root, src); return c ? c.querySelector(".ctitle").textContent : ""; };

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
      const f = sl.querySelector(".sf"); if (f) { f.hidden = !col; f.classList.toggle("on", !!(st.where[sl.dataset.slot] || []).length); }
      sl.setAttribute("aria-label", label(sl) + ": " + (col || "empty"));
    });
    paintFilters(g);
    const r2 = g.querySelector(".g-run2");
    if (r2) { r2.hidden = !st.map.run2; const i = r2.querySelector(".run2name"); if (document.activeElement !== i) i.value = st.run2Name || ""; }
    const tb = g.querySelector(".gsrc .tb"); if (tb) tb.textContent = title(root, st.src) || "none yet";
    const changed = JSON.stringify(st) !== g._orig;
    const save = g.querySelector("[data-save]"); if (save) save.disabled = !changed;
    const undo = g.querySelector("[data-undo]"); if (undo) undo.hidden = !changed;
    if (changed) g.dataset.dirty = "1"; else delete g.dataset.dirty;
    const missing = [...g.querySelectorAll(".slot.req")].filter(sl => !st.map[sl.dataset.slot]).map(label);
    if (st.map.run2 && !(st.run2Name || "").trim()) missing.push("a name for the second run signal");
    if (changed) {
      const shared = g.dataset.kind === "machine" && st.src &&
        [...root.querySelectorAll('.cgroup[data-kind="machine"]')].filter(o => o !== g && String(state(o).src) === String(st.src)).map(o => o.querySelector(".wh b").textContent);
      const none = !Object.keys(st.map).length && Object.keys(orig(g).map || {}).length;
      say(g, none ? "No wires left: Save disconnects this from the database." :
             !st.src && Object.keys(st.map).length ? "Drag one column from this box's card to pick the card it reads." :
             missing.length ? "Still needed: " + missing.join(", ") :
             shared && shared.length ? shared.join(", ") + " read" + (shared.length > 1 ? "" : "s") + " this card too: give each machine its own card with a filter for its rows (✎ on the card, or Duplicate)." : "Ready to save.",
          !none && (!!missing.length || !!(shared && shared.length) || !st.src));
    }
    soon(root);
  }

  /* an input's own rows: "rows where <column> = <value>" for a table with one value per row and a tag */
  const cardCols = (root, src) => { const c = card(root, src); return c ? [...c.querySelectorAll('.cpick input[name=col]')].map(x => x.value) : []; };
  function paintFilters(g) {
    const st = state(g), root = g.closest(".connector");
    g.querySelectorAll(".sfil").forEach(row => {
      const k = row.dataset.for, f = (st.where[k] || [])[0];
      row.hidden = !f || !st.map[k];
      if (row.hidden) return;
      const sel = row.querySelector(".sfcol"), cols = cardCols(root, st.src);
      if (sel.dataset.src !== String(st.src)) {   // the columns of the card this box reads
        sel.innerHTML = '<option value="">Column…</option>' + cols.map(c => `<option>${c.replace(/[&<>"]/g, m => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[m])}</option>`).join("");
        sel.dataset.src = String(st.src);
      }
      if (document.activeElement !== sel) sel.value = f.col || "";
      const v = row.querySelector(".sfval"); if (document.activeElement !== v) v.value = f.val || "";
    });
  }
  function suggest(input, src, col) {   // the values the column has, as a dropdown (a datalist)
    const dl = input.list; if (!dl || !src || !col || (dl.dataset.for === src + "|" + col)) return;
    dl.dataset.for = src + "|" + col;
    if (window.htmx) htmx.ajax("POST", "/kiosk/do/connectorValues", {target: dl, swap: "innerHTML", values: {src: String(src), col}});
  }
  const TAGGY = /^(tag|tagname|tag_name|name|id|item|label|point|signal|key|variable|metric)$/i;

  function flash(el, kind) { el.classList.remove("fl-ok", "fl-no"); void el.offsetWidth; el.classList.add("fl-" + kind); }

  function place(slot, tag) {
    const g = slot.closest(".cgroup"); if (!g || g.closest("[data-readonly]")) return;
    const st = state(g);
    const dbs = (g.dataset.dbs || "").split(",");
    if (!dbs.includes(tag.dataset.db)) {
      say(g, "This needs a column from the " + dbs.map(d => DBN[d] || d).join(" or ") + ". Add a table from that database on the left.", true);
      flash(slot, "no"); return;
    }
    const same = st.table === tag.dataset.table && st.db === tag.dataset.db;
    if (st.table && !same && Object.keys(st.map).length) {
      st.map = {};   // one box reads one card: a card of another table starts this box's wires over
      say(g, "Switched to " + title(g.closest(".connector"), tag.dataset.src) + ": this box's other wires were cleared.", false);
    }
    // a card of the same table (another machine's rows) keeps the wires: only the rows read change
    st.src = Number(tag.dataset.src); st.db = tag.dataset.db; st.table = tag.dataset.table;
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
      if (!st.src) return;
      const c = card(root, st.src);
      g.classList.toggle("nocard", !c);
      if (!c) return;
      for (const [k, col] of Object.entries(st.map)) {
        const tag = portOf(root, st.src, col);
        const sl = g.querySelector(`.slot[data-slot="${CSS.escape(k)}"]`);
        if (!tag || !sl) continue;
        used.add(tag); count.set(c, (count.get(c) || 0) + 1);
        if (tag.hidden) continue;
        const a = dotAt(tag, "r", box, true), b = dotAt(sl, "l", box);
        const p = document.createElementNS(NS, "path");
        const saved = String(o.src) === String(st.src) && (o.map || {})[k] === col;
        p.setAttribute("d", curve(a, b));
        p.setAttribute("class", saved ? "saved" : "pending");
        if (a.out) p.setAttribute("opacity", ".4");
        p.dataset.group = g.dataset.group; p.dataset.slot = k; p.dataset.col = col;
        const t = document.createElementNS(NS, "title");
        t.textContent = `${title(root, st.src)}: ${col} → ${label(sl)} (${g.querySelector(".wh b").textContent}). Click to unwire.`;
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

  let stash = null;
  document.addEventListener("htmx:beforeSwap", e => {
    const t = e.detail.target;
    if (!t || t.id !== "connector") return;
    stash = {};
    t.querySelectorAll(".cgroup[data-dirty]").forEach(g => { stash[g.dataset.group] = state(g); });
  });

  function init(root) {
    if (root._ready) return; root._ready = true;
    root.querySelectorAll(".cgroup").forEach(g => {
      state(g);
      if (stash && stash[g.dataset.group]) g._st = JSON.parse(JSON.stringify(stash[g.dataset.group]));   // unsaved wires survive a card change
      paint(g);
    });
    stash = null;
    const ed = root.querySelector(".srcard.editing"); if (ed) ed.scrollIntoView({block: "nearest"});
    const add = root.querySelector(".addsrc");
    if (add) {
      fillTables(add);
      add.addEventListener("change", e => {
        if (e.target.name === "db") fillTables(add);
        if (e.target.name === "table") add.querySelector("[data-add]").disabled = !e.target.value;
        const am = add.querySelector(".addmsg"); if (am) am.textContent = "";
      });
      add.addEventListener("htmx:afterRequest", e => {
        if (e.detail.successful) { add.querySelector("select[name=table]").value = ""; add.querySelector("[data-add]").disabled = true; }
      });
    }
    root.addEventListener("input", e => {
      if (e.target.matches(".run2name")) { const g = e.target.closest(".cgroup"); state(g).run2Name = e.target.value; paint(g); return; }
      const row = e.target.closest(".sfil");
      if (row && e.target.matches(".sfcol,.sfval")) {
        const g = row.closest(".cgroup"), st = state(g), f = (st.where[row.dataset.for] || [])[0];
        if (f) { f[e.target.matches(".sfcol") ? "col" : "val"] = e.target.value; paint(g); }
        if (e.target.matches(".sfcol")) suggest(row.querySelector(".sfval"), st.src, e.target.value);
        return;
      }
      if (!e.target.matches(".cfilter")) return;
      const v = e.target.value.trim().toLowerCase();
      e.target.closest(".srcard").querySelectorAll(".port.out").forEach(t => { t.hidden = v && !t.dataset.col.toLowerCase().includes(v); });
      soon(root);
    });
    root.addEventListener("focusin", e => {
      if (e.target.matches(".sfil .sfval")) {
        const row = e.target.closest(".sfil"), st = state(row.closest(".cgroup"));
        suggest(e.target, st.src, row.querySelector(".sfcol").value);
      } else if (e.target.matches(".cedit .frow input[name=fval]")) {   // a card's own filter
        const fr = e.target.closest(".frow"), c = e.target.closest(".srcard");
        if (!e.target.list) {
          const dl = document.createElement("datalist"); dl.id = "dlc-" + Math.random().toString(36).slice(2);
          fr.appendChild(dl); e.target.setAttribute("list", dl.id);
        }
        suggest(e.target, c.dataset.src, fr.querySelector("select[name=fcol]").value);
      }
    });
    root.addEventListener("change", e => {
      if (e.target.matches(".cedit .frow select[name=fcol]")) {
        const inp = e.target.closest(".frow").querySelector("input[name=fval]");
        if (inp.list) { inp.list.dataset.for = ""; suggest(inp, e.target.closest(".srcard").dataset.src, e.target.value); }
        return;
      }
      if (!e.target.matches(".gcopy") || !e.target.value) return;
      const g = e.target.closest(".cgroup"), from = root.querySelector(`.cgroup[data-group="${CSS.escape(e.target.value)}"]`);
      const name = e.target.selectedOptions[0].textContent;
      e.target.value = "";
      if (!from || !state(from).table) { say(g, name + " isn't wired to a table yet.", true); return; }
      const f = state(from), st = state(g);
      st.db = f.db; st.table = f.table; st.map = Object.assign({}, f.map); st.src = null;
      st.where = JSON.parse(JSON.stringify(f.where || {}));   // the same tags, to change to this machine's (e.g. Salvatore_Speed)
      paint(g);
      say(g, `Copied ${name}'s wires. Now drag any column from this machine's card to pick the rows it reads` + (Object.keys(st.where).length ? `, and change the ⚑ tags to this machine's (e.g. ${g.querySelector(".wh b").textContent.trim()}_Speed).` : "."), false);
    });
    root.addEventListener("click", e => {
      const chip = e.target.closest("[data-goto]");
      if (chip) { const g = root.querySelector(`.cgroup[data-group="${CSS.escape(chip.dataset.goto)}"]`); if (g) { g.scrollIntoView({behavior: "smooth", block: "center"}); flash(g, "ok"); } return; }
      const ed = e.target.closest(".srcard [data-edit]"), cancel = e.target.closest(".srcard [data-cancel]");
      if (ed || cancel) {
        const c = (ed || cancel).closest(".srcard"), f = c.querySelector(".cedit");
        const open = ed ? f.hidden : false;
        if (!open) f.reset();
        f.hidden = !open; c.classList.toggle("editing", open);
        const b = c.querySelector("[data-edit]"); if (b) b.setAttribute("aria-expanded", String(open));
        if (open) f.querySelector("input[name=title]").focus();
        soon(root); return;
      }
      const fadd = e.target.closest("[data-fadd]");
      if (fadd) { const fs = fadd.closest("fieldset"); fs.querySelector(".frows").appendChild(fs.querySelector("template").content.cloneNode(true)); fs.querySelector(".frows .frow:last-child select").focus(); soon(root); return; }
      const frm = e.target.closest("[data-frm]");
      if (frm) { frm.closest(".frow").remove(); soon(root); return; }
      const all = e.target.closest("[data-all],[data-none]");
      if (all) { all.closest("fieldset").querySelectorAll("input[type=checkbox]").forEach(x => { x.checked = all.hasAttribute("data-all"); }); return; }
      const tc = e.target.closest("[data-ctest-close]");
      if (tc) { tc.closest(".gtest").innerHTML = ""; soon(root); return; }
      const sf = e.target.closest(".sf");
      if (sf) {
        const sl = sf.closest(".slot"), g = sl.closest(".cgroup"), st = state(g), k = sl.dataset.slot;
        if (!(st.where[k] || []).length) {
          const cols = cardCols(root, st.src), used = new Set(Object.values(st.map));
          // the column this box's other inputs already filter on (PAID for every part spec), else a tag-like one
          const prev = Object.values(st.where).map(v => (v[0] || {}).col).find(c => c && cols.includes(c));
          const guess = g.dataset.kind === "machine" ? cols.find(c => TAGGY.test(c) && !used.has(c)) || cols.find(c => TAGGY.test(c)) : "";
          st.where[k] = [{col: prev || guess || "", val: ""}];
          paint(g);
          const row = g.querySelector(`.sfil[data-for="${CSS.escape(k)}"]`); if (row) row.querySelector(st.where[k][0].col ? ".sfval" : ".sfcol").focus();
        } else { const row = g.querySelector(`.sfil[data-for="${CSS.escape(k)}"]`); if (row) row.querySelector(".sfval").focus(); }
        e.stopPropagation(); return;
      }
      const sfx = e.target.closest(".sfx");
      if (sfx) { const row = sfx.closest(".sfil"), g = row.closest(".cgroup"); delete state(g).where[row.dataset.for]; paint(g); return; }
      const x = e.target.closest(".sx");
      if (x) { const sl = x.closest(".slot"), g = sl.closest(".cgroup"), st = state(g); delete st.map[sl.dataset.slot]; delete st.where[sl.dataset.slot];
        if (!Object.keys(st.map).length) { st.src = null; st.db = ""; st.table = ""; st.where = {}; }
        paint(g); e.stopPropagation(); return; }
      const undo = e.target.closest("[data-undo]");
      if (undo) { const g = undo.closest(".cgroup"); g._st = JSON.parse(g._orig); paint(g); say(g, "Changes undone.", false); return; }
      const tag = e.target.closest(".port.out");
      if (tag) { if (!tag._dragged) pick(tag); tag._dragged = false; return; }
      const sl = e.target.closest(".slot");
      if (sl && picked) { place(sl, picked); unpick(); return; }
      if (sl && !sl._dragged) {   // show where an input's wire goes
        const g = sl.closest(".cgroup"), st = state(g), col = st.map[sl.dataset.slot];
        root.querySelectorAll(".wires path").forEach(p => p.classList.toggle("hl", p.dataset.group === g.dataset.group && p.dataset.slot === sl.dataset.slot));
        const t = col && portOf(root, st.src, col); if (t) t.scrollIntoView({block: "nearest"});
      }
      if (sl) sl._dragged = false;
    });
    // a wire is clicked to unwire it (the SVG sits over the page; only the wire strokes take clicks)
    root.querySelector("svg.wires")?.addEventListener("click", e => {
      const p = e.target.closest("path[data-slot]"); if (!p) return;
      const g = root.querySelector(`.cgroup[data-group="${CSS.escape(p.dataset.group)}"]`); if (!g) return;
      const st = state(g); delete st.map[p.dataset.slot]; delete st.where[p.dataset.slot];
      if (!Object.keys(st.map).length) { st.src = null; st.db = ""; st.table = ""; st.where = {}; }
      paint(g); if (Object.keys(st.map).length) say(g, `Unwired ${p.dataset.col}.`, false);
    });
    root.addEventListener("keydown", e => {
      if ((e.key === "Enter" || e.key === " ") && e.target.matches(".slot") && picked) { e.preventDefault(); place(e.target, picked); unpick(); }
      if (e.key === "Escape") unpick();
    });
    root.addEventListener("pointerdown", e => {
      if (e.button > 0 || e.target.closest(".sx,.sf,.sfil,select,input,textarea,.cedit,[data-edit]")) return;
      const from = e.target.closest(".port.out") || (e.target.closest(".slot .dot") && e.target.closest(".slot"));
      if (!from) return;
      e.preventDefault();   // no text selection while drawing a wire
      drag = {from, x0: e.clientX, y0: e.clientY, moved: false, id: e.pointerId, root};
    });
    // Test data comes from inside the box's form, and a form the server answered counts as saved (thin.js), which
    // would let the next automatic refresh replace the box and drop its unsaved wires: a box still differing from
    // what's saved stays marked unsaved (a Save that went through replaces the box, so it's gone by then)
    root.addEventListener("htmx:afterRequest", e => {
      const g = e.detail.elt.closest && e.detail.elt.closest(".cgroup"); if (!g || !g._st) return;
      setTimeout(() => { if (g.isConnected && JSON.stringify(g._st) !== g._orig) g.dataset.dirty = "1"; }, 0);
    });
    // Save sends this box's wires
    root.addEventListener("htmx:configRequest", e => {
      const g = e.detail.elt.closest && e.detail.elt.closest(".cgroup"); if (!g) return;
      const st = state(g);
      e.detail.parameters.map = JSON.stringify(st.map);
      e.detail.parameters.where = JSON.stringify(st.where || {});
      e.detail.parameters.run2Name = st.run2Name || "";
      e.detail.parameters.src = st.src || "";
      e.detail.parameters.db = st.db || "";
      e.detail.parameters.table = st.table || "";
    });
    soon(root);
  }

  // what is under the pointer, looking through the wires drawn over the page (a wire ends on the input it feeds)
  function under(x, y, sel) {
    for (const el of document.elementsFromPoint(x, y)) { const hit = el.closest && el.closest(sel); if (hit) return hit; }
    return null;
  }

  // dragging a wire (once for the page; the builder can be opened again and again)
  window.addEventListener("pointermove", e => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved && Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < 6) return;
    if (!drag.moved) { drag.moved = true; unpick(); document.body.classList.add("c-dragging"); }
    drag.end = {x: e.clientX, y: e.clientY};
    document.querySelectorAll(".port.over").forEach(s => s.classList.remove("over"));
    const tgt = under(e.clientX, e.clientY, drag.from.classList.contains("out") ? ".slot" : ".port.out");
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
    if (d.from.classList.contains("out")) { const sl = under(e.clientX, e.clientY, ".slot"); if (sl) place(sl, d.from); }
    else { const tag = under(e.clientX, e.clientY, ".port.out"); if (tag) place(d.from, tag); }
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
