import { createWriteStream } from "fs";

const CDP_HOST = "127.0.0.1:9222";
const TARGET_ID = process.argv[2];
const DURATION_MS = Number(process.argv[3] || 60000);
const OUT_FILE = process.argv[4] || "capture_body.jsonl";

const out = createWriteStream(OUT_FILE, { flags: "w" });
let eventCount = 0;
function log(obj) {
  eventCount++;
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

ws.onmessage = async (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg.result);
    pending.delete(msg.id);
    return;
  }
  if (!msg.method) return;

  switch (msg.method) {
    case "Network.requestWillBeSent": {
      const req = msg.params.request;
      const entry = { type: "request", requestId: msg.params.requestId, url: req.url, method: req.method, headers: req.headers };
      if (req.hasPostData && req.postData == null) {
        const body = await send("Network.getRequestPostData", { requestId: msg.params.requestId });
        entry.postDataBase64 = body && body.postData ? Buffer.from(body.postData, "binary").toString("base64") : undefined;
      } else if (req.postData != null) {
        entry.postDataBase64 = Buffer.from(req.postData, "binary").toString("base64");
      }
      log(entry);
      break;
    }
    case "Network.responseReceived":
      log({ type: "response", requestId: msg.params.requestId, url: msg.params.response.url, status: msg.params.response.status, headers: msg.params.response.headers, mimeType: msg.params.response.mimeType });
      break;
    case "Network.loadingFinished": {
      try {
        const body = await send("Network.getResponseBody", { requestId: msg.params.requestId });
        if (body) {
          log({ type: "response_body", requestId: msg.params.requestId, base64Encoded: body.base64Encoded, body: body.base64Encoded ? body.body : Buffer.from(body.body, "binary").toString("base64") });
        }
      } catch (e) {
        log({ type: "response_body_error", requestId: msg.params.requestId, error: String(e) });
      }
      break;
    }
    case "Network.webSocketFrameSent":
      log({ type: "ws_sent", requestId: msg.params.requestId, payload: msg.params.response.payloadData });
      break;
    case "Network.webSocketFrameReceived":
      log({ type: "ws_recv", requestId: msg.params.requestId, payload: msg.params.response.payloadData });
      break;
    case "Network.loadingFailed":
      log({ type: "failed", requestId: msg.params.requestId, errorText: msg.params.errorText });
      break;
  }
};

await send("Network.enable", {});
await send("Page.enable", {});
await send("Page.bringToFront", {});

console.error(`Attached (with bodies), capturing for ${DURATION_MS}ms -> ${OUT_FILE}`);
await new Promise((r) => setTimeout(r, DURATION_MS));

ws.close();
await new Promise((resolve) => out.end(resolve));
console.error(`Done. ${eventCount} events written.`);
process.exit(0);
