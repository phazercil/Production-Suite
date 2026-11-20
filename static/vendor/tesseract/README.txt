Tesseract.js 7.0.0 (Apache-2.0, LICENSE.md) with tesseract.js-core 7 (Apache-2.0, core/LICENSE) and the English
"best_int" model from @tesseract.js-data/eng 1.0.0 (MIT; Tesseract's tessdata_best, Apache-2.0).

Served by the suite itself so the Operator Kiosk can read roll tags from a picture without any internet
connection (Roll tags > Take a picture of the tag; static/hmi/rolltag.js). The screen picks the core that suits it:
relaxed SIMD, SIMD, or plain WebAssembly.

To update: npm install tesseract.js @tesseract.js-data/eng, then copy
  node_modules/tesseract.js/dist/{tesseract.min.js,worker.min.js}           -> here
  node_modules/tesseract.js-core/tesseract-core-*lstm.wasm.js (3 files)    -> core/
  node_modules/@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz    -> lang/
