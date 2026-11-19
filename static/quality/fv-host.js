/* Production Suite host for the Pleated Filter 3D Viewer (Quality › 3D viewer, and the Operator Kiosk's 3D Model).
   The viewer was built to ask its host for two things: a "db" (its part library, collection "parts") and
   "downloads" (STL and Excel files). This gives it both: the part library is the suite's own database
   (/api/quality/fv/...), read by anyone on the plant network and saved only with the Quality settings permission;
   downloads are ordinary browser downloads. */
(function () {
  "use strict";
  var H = window.FV_HOST || {};
  var csrf = function () { var m = document.cookie.match(/csrftoken=([^;]+)/); return m ? m[1] : ""; };
  var docs = [], ver = null, subs = [], loaded = false, readyRes;
  var ready = new Promise(function (r) { readyRes = r; });
  var copy = function (o) { return JSON.parse(JSON.stringify(o)); };

  function snapFor(s) {
    var arr = docs.slice().sort(function (a, b) { return String(a.data[s.field] == null ? "" : a.data[s.field]).localeCompare(String(b.data[s.field] == null ? "" : b.data[s.field])); });
    return { metadata: { fromCache: false }, size: arr.length, empty: !arr.length,
             docs: arr.map(function (d) { return { id: d.id, exists: true, data: function () { return copy(d.data); } }; }) };
  }
  function emit() { subs.forEach(function (s) { try { s.cb(snapFor(s)); } catch (e) { console.error(e); } }); }
  async function pull(force) {
    try {
      var r = await fetch("/api/quality/fv/parts" + (ver && !force ? "?v=" + encodeURIComponent(ver) : ""), { credentials: "same-origin", cache: "no-store" });
      var j = await r.json();
      if (!j.ok) throw new Error(j.error || "The part library can't be read.");
      if (!j.same) { docs = j.docs; ver = j.v; emit(); }
      else if (!loaded) emit();
      loaded = true; readyRes();
    } catch (e) {
      subs.forEach(function (s) { if (s.err) s.err({ code: "unavailable", message: String(e.message || e) }); });
    }
  }
  async function post(path, body) {
    if (!H.canEdit) {
      var e0 = new Error(H.view ? "This screen is view only." : "Saving to the part library needs the Quality settings permission.");
      e0.code = "permission-denied"; throw e0;
    }
    var r = await fetch(path, { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() }, body: JSON.stringify(body) });
    var j = await r.json().catch(function () { return { ok: false, error: "The server is not answering." }; });
    if (!j.ok) { var e = new Error(j.error || "Not saved."); e.code = r.status === 403 ? "permission-denied" : "failed"; throw e; }
    await pull(true);
    return j;
  }
  function collection() {
    var api = {
      doc: function (id) {
        return {
          id: id,
          set: function (data) { return post("/api/quality/fv/parts/set", { id: id, data: data }); },
          update: function (data) { var d = docs.find(function (x) { return x.id === id; }); return post("/api/quality/fv/parts/set", { id: id, data: Object.assign({}, d ? d.data : {}, data) }); },
          delete: function () { return post("/api/quality/fv/parts/delete", { id: id }); },
          get: async function () { await ready; var d = docs.find(function (x) { return x.id === id; }); return { id: id, exists: !!d, data: function () { return d ? copy(d.data) : undefined; } }; }
        };
      },
      orderBy: function (field) {
        return {
          onSnapshot: function (cb, err) {
            var s = { cb: cb, err: err, field: field || "part" }; subs.push(s);
            if (loaded) cb(snapFor(s));
            return function () { subs = subs.filter(function (x) { return x !== s; }); };
          },
          get: async function () { await ready; return snapFor({ field: field || "part" }); }
        };
      }
    };
    api.onSnapshot = function (cb, err) { return api.orderBy("part").onSnapshot(cb, err); };
    api.get = function () { return api.orderBy("part").get(); };
    return api;
  }
  var db = { collection: collection };
  var downloads = {
    save: async function (o) {
      var data = o.data, blob = data instanceof Blob ? data : new Blob([data], { type: o.type || "application/octet-stream" });
      var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = o.filename || "download";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
      return { ok: true };
    }
  };
  window.claude = { use: function (cap) {
    if (cap === "db") return Promise.resolve(db);
    if (cap === "downloads") return Promise.resolve(downloads);
    return Promise.reject(new Error(cap + " isn't available here"));
  } };
  window.__fvHost = { ready: ready, reload: function () { return pull(true); } };
  pull(true);
  setInterval(function () { if (!document.hidden) pull(false); }, 6000);
})();
