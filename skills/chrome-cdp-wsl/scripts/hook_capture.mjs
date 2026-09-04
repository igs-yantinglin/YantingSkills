import { createWriteStream } from "fs";

const CDP_HOST = "127.0.0.1:9222";
const TARGET_ID = process.argv[2];
const DURATION_MS = Number(process.argv[3] || 60000);
const OUT_FILE = process.argv[4] || "hook_out.jsonl";

if (!TARGET_ID) {
  console.error("Usage: node hook_capture.mjs <targetId> [durationMs] [outFile]");
  process.exit(1);
}

const out = createWriteStream(OUT_FILE, { flags: "w" });
function log(obj) {
  out.write(JSON.stringify({ t: Date.now(), ...obj }) + "\n");
}

const wsUrl = `ws://${CDP_HOST}/devtools/page/${TARGET_ID}`;
const ws = new WebSocket(wsUrl);
let msgId = 1;
const pending = new Map();

function send(method, params = {}) {
  const id = msgId++;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

await new Promise((resolve, reject) => {
  ws.onopen = resolve;
  ws.onerror = reject;
});

ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg.result);
    pending.delete(msg.id);
    return;
  }
  if (!msg.method) return;
  if (msg.method === "Runtime.consoleAPICalled") {
    const args = msg.params.args.map(a => a.value !== undefined ? a.value : a.description);
    log({ type: "console", args });
  }
  if (msg.method === "Runtime.exceptionThrown") {
    log({ type: "exception", detail: msg.params.exceptionDetails });
  }
};

await send("Runtime.enable", {});
await send("Page.enable", {});

// The hook script: override window.fetch and XMLHttpRequest to intercept
// requests to /mpt/req, capturing raw request+response bytes as base64.
const hookScript = `
(function() {
  if (window.__hookInstalled) return 'already-installed';
  window.__hookInstalled = true;
  window.__captured = [];

  function ab2b64(buf) {
    try {
      const bytes = new Uint8Array(buf);
      let binary = '';
      for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
      return btoa(binary);
    } catch (e) { return null; }
  }

  // --- Hook fetch ---
  const origFetch = window.fetch;
  window.fetch = function(input, init) {
    const url = (typeof input === 'string') ? input : (input && input.url) || '';
    if (url.indexOf('mpt/req') === -1) {
      return origFetch.apply(this, arguments);
    }
    const entry = { kind: 'fetch', url, ts: Date.now() };
    let reqBody = init && init.body;
    let reqPromise = Promise.resolve();
    if (reqBody instanceof ArrayBuffer) {
      entry.reqBodyB64 = ab2b64(reqBody);
    } else if (reqBody && reqBody.buffer instanceof ArrayBuffer) {
      entry.reqBodyB64 = ab2b64(reqBody.buffer);
    } else if (reqBody instanceof Blob) {
      reqPromise = reqBody.arrayBuffer().then(function(buf) { entry.reqBodyB64 = ab2b64(buf); });
    } else if (typeof reqBody === 'string') {
      entry.reqBodyStr = reqBody;
    } else if (typeof Request !== 'undefined' && input instanceof Request) {
      reqPromise = input.clone().arrayBuffer().then(function(buf) { entry.reqBodyB64 = ab2b64(buf); }).catch(function(e){});
    }
    entry.__reqPromise = reqPromise;
    return origFetch.apply(this, arguments).then(function(resp) {
      const cloned = resp.clone();
      Promise.all([reqPromise, cloned.arrayBuffer()]).then(function(results) {
        const buf = results[1];
        entry.respBodyB64 = ab2b64(buf);
        entry.respLen = buf.byteLength;
        delete entry.__reqPromise;
        window.__captured.push(entry);
        console.log('CAPTURED_FETCH', JSON.stringify(entry).length);
      }).catch(function(e) {});
      return resp;
    });
  };

  // --- Hook XMLHttpRequest ---
  const OrigXHR = window.XMLHttpRequest;
  const origOpen = OrigXHR.prototype.open;
  const origSend = OrigXHR.prototype.send;
  OrigXHR.prototype.open = function(method, url) {
    this.__hookUrl = url;
    this.__hookMethod = method;
    return origOpen.apply(this, arguments);
  };
  OrigXHR.prototype.send = function(body) {
    const url = this.__hookUrl || '';
    if (url.indexOf && url.indexOf('mpt/req') !== -1) {
      const entry = { kind: 'xhr', url, ts: Date.now() };
      if (body instanceof ArrayBuffer) {
        entry.reqBodyB64 = ab2b64(body);
      } else if (body && body.buffer instanceof ArrayBuffer) {
        entry.reqBodyB64 = ab2b64(body.buffer);
      } else if (typeof body === 'string') {
        entry.reqBodyStr = body;
      }
      const self = this;
      const prevOnReadyStateChange = this.onreadystatechange;
      this.addEventListener('load', function() {
        try {
          let respBuf = null;
          if (self.response instanceof ArrayBuffer) {
            respBuf = self.response;
          } else if (self.responseType === '' || self.responseType === 'text') {
            entry.respBodyStr = self.responseText;
          }
          if (respBuf) {
            entry.respBodyB64 = ab2b64(respBuf);
            entry.respLen = respBuf.byteLength;
          }
          entry.status = self.status;
          window.__captured.push(entry);
          console.log('CAPTURED_XHR', url, entry.respLen);
        } catch (e) {
          console.log('CAPTURE_ERR', String(e));
        }
      });
    }
    return origSend.apply(this, arguments);
  };

  // --- Hook Web Crypto SubtleCrypto (in case AES/HMAC goes through native API) ---
  window.__cryptoLog = [];
  try {
    const subtle = window.crypto && window.crypto.subtle;
    if (subtle) {
      ['decrypt', 'encrypt', 'importKey', 'deriveKey', 'deriveBits', 'digest', 'sign', 'verify'].forEach(function(method) {
        const orig = subtle[method];
        if (!orig) return;
        subtle[method] = function() {
          const argsArr = Array.prototype.slice.call(arguments);
          const rec = { method: method, ts: Date.now() };
          try {
            // algorithm is usually first or second arg depending on method
            rec.argSummary = argsArr.map(function(a) {
              if (a instanceof ArrayBuffer) return { type: 'ArrayBuffer', b64: ab2b64(a), len: a.byteLength };
              if (a && a.buffer instanceof ArrayBuffer) return { type: 'TypedArray', b64: ab2b64(a.buffer), len: a.byteLength };
              if (a && typeof a === 'object') {
                try { return { type: 'object', json: JSON.stringify(a, function(k,v){
                  if (v instanceof ArrayBuffer) return { __ab: ab2b64(v) };
                  if (v && v.buffer instanceof ArrayBuffer) return { __ta: ab2b64(v.buffer) };
                  return v;
                }) }; } catch(e) { return { type: 'object', note: 'unstringifiable' }; }
              }
              return { type: typeof a, value: String(a) };
            });
          } catch (e) { rec.err = String(e); }
          window.__cryptoLog.push(rec);
          console.log('SUBTLECRYPTO_CALL', method);
          const result = orig.apply(this, arguments);
          if (result && typeof result.then === 'function') {
            result.then(function(res) {
              try {
                if (res instanceof ArrayBuffer) {
                  rec.resultB64 = ab2b64(res);
                  rec.resultLen = res.byteLength;
                } else if (res && res.type === 'secret') {
                  rec.resultKeyType = 'CryptoKey(secret)';
                }
              } catch (e) {}
            }).catch(function(e) { rec.resultErr = String(e); });
          }
          return result;
        };
      });
      console.log('SUBTLECRYPTO_HOOKED');
    }
  } catch (e) {
    console.log('SUBTLECRYPTO_HOOK_FAIL', String(e));
  }

  // --- Hook TextDecoder.decode and JSON.parse to catch any plaintext surfacing ---
  window.__decodeLog = [];
  try {
    const origDecode = TextDecoder.prototype.decode;
    TextDecoder.prototype.decode = function(buf) {
      const res = origDecode.apply(this, arguments);
      try {
        if (res && res.indexOf('"type"') !== -1 && res.indexOf('"data"') !== -1) {
          window.__decodeLog.push({ kind: 'TextDecoder', len: res.length, sample: res });
        } else if (res && res.length > 20 && res.length < 300) {
          window.__decodeLog.push({ kind: 'TextDecoder', len: res.length, sample: res.slice(0, 300) });
        }
      } catch (e) {}
      return res;
    };
    const origJSONParse = JSON.parse;
    JSON.parse = function(text) {
      try {
        if (typeof text === 'string' && text.indexOf('"type"') !== -1 && text.indexOf('"data"') !== -1) {
          window.__decodeLog.push({ kind: 'JSON.parse', len: text.length, sample: text });
        } else if (typeof text === 'string' && text.length > 10 && text.length < 300) {
          window.__decodeLog.push({ kind: 'JSON.parse', len: text.length, sample: text.slice(0, 300) });
        }
      } catch (e) {}
      return origJSONParse.apply(this, arguments);
    };
    console.log('DECODE_HOOKED');
  } catch (e) {
    console.log('DECODE_HOOK_FAIL', String(e));
  }

  console.log('HOOK_INSTALLED', 'fetch+xhr+subtlecrypto+decode');
  return 'installed';
})();
`;

const evalResult = await send("Runtime.evaluate", { expression: hookScript, returnByValue: true });
console.error("Hook install result:", JSON.stringify(evalResult));

await send("Runtime.setAsyncCallStackDepth", { maxDepth: 0 });

console.error(`Hook installed on target ${TARGET_ID}. Listening for ${DURATION_MS}ms -> ${OUT_FILE}`);
console.error("Please click SPIN a few times in the game now.");

await new Promise((r) => setTimeout(r, DURATION_MS));

// Pull final captured array via Runtime.evaluate
const finalRes = await send("Runtime.evaluate", {
  expression: "JSON.stringify(window.__captured || [])",
  returnByValue: true,
});
if (finalRes && finalRes.result && finalRes.result.value) {
  try {
    const arr = JSON.parse(finalRes.result.value);
    log({ type: "final_captured", count: arr.length, data: arr });
    console.error(`Captured ${arr.length} mpt/req exchanges.`);
  } catch (e) {
    console.error("Failed to parse final captured data:", e);
  }
}

const cryptoRes = await send("Runtime.evaluate", {
  expression: "JSON.stringify(window.__cryptoLog || [])",
  returnByValue: true,
});
if (cryptoRes && cryptoRes.result && cryptoRes.result.value) {
  try {
    const arr = JSON.parse(cryptoRes.result.value);
    log({ type: "crypto_log", count: arr.length, data: arr });
    console.error(`Captured ${arr.length} SubtleCrypto calls.`);
  } catch (e) {
    console.error("Failed to parse crypto log:", e);
  }
}

const decodeRes = await send("Runtime.evaluate", {
  expression: "JSON.stringify(window.__decodeLog || [])",
  returnByValue: true,
});
if (decodeRes && decodeRes.result && decodeRes.result.value) {
  try {
    const arr = JSON.parse(decodeRes.result.value);
    log({ type: "decode_log", count: arr.length, data: arr });
    console.error(`Captured ${arr.length} decode/JSON.parse calls.`);
  } catch (e) {
    console.error("Failed to parse decode log:", e);
  }
}

ws.close();
await new Promise((resolve) => out.end(resolve));
console.error("Done.");
process.exit(0);
