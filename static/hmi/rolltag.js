/* Roll tags › Take a picture of the tag.
 *
 * The tablet reads the picture itself: the text with Tesseract (served by the suite from static/vendor/tesseract,
 * so nothing goes to the internet) and any barcodes with the browser's own barcode reader where it has one
 * (Chrome on Android does). It sends the picture and what it read to the server (/kiosk/do/rollRead), which
 * decides which text is the roll ID, lot, material and footage (hmi/rolltag.py) and sends the dialog back with the
 * fields filled in. No plant rules here: this only reads.
 */
"use strict";
(function () {
  const MAX_SIDE = 2400;   // the copy that is read; the server keeps its own copy of the picture
  let worker = null, loading = null;

  function loadScript(src) {
    return new Promise((ok, bad) => {
      if (window.Tesseract) return ok();
      const s = document.createElement("script");
      s.src = src; s.onload = ok; s.onerror = () => bad(new Error("couldn't load the text reader"));
      document.head.appendChild(s);
    });
  }

  async function reader(box, status) {
    if (worker) return worker;
    if (!loading) {
      loading = (async () => {
        await loadScript(box.dataset.lib);
        const w = await Tesseract.createWorker("eng", 1, {
          workerPath: box.dataset.worker, corePath: box.dataset.core, langPath: box.dataset.lang,
          workerBlobURL: false, gzip: true,
          logger: m => { if (m && m.status && status.isConnected) status.textContent = step(m); },
        });
        await w.setParameters({ preserve_interword_spaces: "1" });
        return w;
      })();
      loading.catch(() => { loading = null; });
    }
    worker = await loading;
    return worker;
  }

  function step(m) {
    const pct = m.progress ? ` ${Math.round(m.progress * 100)}%` : "";
    if (/load|initializ/i.test(m.status)) return "Getting the text reader ready (the first time takes a moment)…";
    if (/recogniz/i.test(m.status)) return `Reading the tag…${pct}`;
    return `${m.status}${pct}`;
  }

  async function picture(file) {
    const img = await createImageBitmap(file, { imageOrientation: "from-image" });
    const k = Math.min(2, MAX_SIDE / Math.max(img.width, img.height));   // small pictures are enlarged: small print reads better
    const c = document.createElement("canvas");
    c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
    const g = c.getContext("2d", { willReadFrequently: true });
    g.imageSmoothingQuality = "high";
    g.drawImage(img, 0, 0, c.width, c.height);
    if (img.close) img.close();
    const px = g.getImageData(0, 0, c.width, c.height), d = px.data;   // grey: colored tags and lighting read better
    for (let i = 0; i < d.length; i += 4) { const y = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2]; d[i] = d[i + 1] = d[i + 2] = y; }
    g.putImageData(px, 0, 0);
    return c;
  }

  async function barcodes(canvas) {
    if (!("BarcodeDetector" in window)) return [];
    try {
      const formats = await BarcodeDetector.getSupportedFormats();
      const found = await new BarcodeDetector(formats.length ? { formats } : undefined).detect(canvas);
      return found.map(b => ({ format: b.format, value: b.rawValue })).filter(b => b.value);
    } catch (e) { return []; }
  }

  function lines(data) {
    const out = [];
    (data.blocks || []).forEach(b => (b.paragraphs || []).forEach(p => (p.lines || []).forEach(l => {
      const t = (l.text || "").trim();
      if (t && l.confidence > 20) out.push({ text: t, box: [l.bbox.x0, l.bbox.y0, l.bbox.x1, l.bbox.y1] });
    })));
    if (!out.length && data.text) data.text.split("\n").forEach(t => { if (t.trim()) out.push({ text: t.trim() }); });
    return out;
  }

  document.addEventListener("change", async e => {
    const input = e.target;
    if (!input.matches || !input.matches("[data-rolltag-file]") || !input.files || !input.files.length) return;
    const box = input.closest("[data-rolltag]");
    const status = box.querySelector("[data-rolltag-status]");
    const out = box.querySelector("[data-rolltag-ocr]");
    const send = box.querySelector("[data-rolltag-send]");
    const file = input.files[0];
    box.classList.add("busy");
    status.textContent = "Reading the tag…";
    const res = { lines: [], barcodes: [] };
    try {
      const canvas = await picture(file);
      res.w = canvas.width; res.h = canvas.height;
      res.barcodes = await barcodes(canvas);
      const w = await reader(box, status);
      const r = await w.recognize(canvas, {}, { blocks: true, text: true });
      res.lines = lines(r.data || {});
    } catch (err) {
      res.error = String((err && err.message) || err || "error").slice(0, 200);
    }
    if (!box.isConnected) return;   // the dialog was closed while reading
    status.textContent = "Filling in the fields…";
    out.value = JSON.stringify(res);
    send.click();   // posts the picture and what was read with the dialog's form (htmx)
  });
})();
