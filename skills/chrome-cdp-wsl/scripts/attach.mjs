import { createWriteStream } from "fs";

const CDP_HOST = "127.0.0.1:9222";
const TARGET_ID = process.argv[2];
const DURATION_MS = Number(process.argv[3] || 120000);
const OUT_FILE = process.argv[4] || "capture_live.jsonl";

if (!TARGET_ID) {
  console.error("Usage: node attach.mjs <targetId> [durationMs] [outFile]");
  process.exit(1);
}

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

ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg.result);
    pending.delete(msg.id);
    return;
  }
  if (!msg.method) return;

  switch (msg.method) {
    case "Network.requestWillBeSent":
      log({
        type: "request",
        requestId: msg.params.requestId,
        url: msg.params.request.url,
        method: msg.params.request.method,
        headers: msg.params.request.headers,
        postData: msg.params.request.postData,
      });
      break;
    case "Network.responseReceived":
      log({
        type: "response",
        requestId: msg.params.requestId,
        url: msg.params.response.url,
        status: msg.params.response.status,
        headers: msg.params.response.headers,
        mimeType: msg.params.response.mimeType,
      });
      break;
    case "Network.webSocketCreated":
      log({ type: "ws_created", requestId: msg.params.requestId, url: msg.params.url });
      break;
    case "Network.webSocketFrameSent":
      log({ type: "ws_sent", requestId: msg.params.requestId, payload: msg.params.response.payloadData });
      break;
    case "Network.webSocketFrameReceived":
      log({ type: "ws_recv", requestId: msg.params.requestId, payload: msg.params.response.payloadData });
      break;
    case "Network.webSocketClosed":
      log({ type: "ws_closed", requestId: msg.params.requestId });
      break;
    case "Network.loadingFailed":
      log({ type: "failed", requestId: msg.params.requestId, errorText: msg.params.errorText });
      break;
  }
};

await send("Network.enable", {});
await send("Page.enable", {});
await send("Page.bringToFront", {});

console.error(`Attached, capturing for ${DURATION_MS}ms -> ${OUT_FILE}`);
await new Promise((r) => setTimeout(r, DURATION_MS));

ws.close();
await new Promise((resolve) => out.end(resolve));
console.error(`Done. ${eventCount} events written.`);
process.exit(0);
