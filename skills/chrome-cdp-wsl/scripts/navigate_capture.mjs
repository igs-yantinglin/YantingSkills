import { readFileSync } from "fs";

const CDP_HOST = "127.0.0.1:9222";
let TARGET_URL = process.argv[2];
if (TARGET_URL && TARGET_URL.startsWith("@")) {
  // read URL from file to avoid shell quoting issues with & in the URL
  TARGET_URL = readFileSync(TARGET_URL.slice(1), "utf8").trim();
}
const DURATION_MS = Number(process.argv[3] || 30000);
const OUT_FILE = process.argv[4] || "capture.jsonl";

if (!TARGET_URL) {
  console.error("Usage: node capture.mjs <url> [durationMs] [outFile]");
  process.exit(1);
}

const fs = await import("fs");
const out = fs.createWriteStream(OUT_FILE, { flags: "w" });
let eventCount = 0;

function log(obj) {
  eventCount++;
  out.write(JSON.stringify({ t: Date.now(), ...obj }) + "\n");
}

async function jsonGet(path) {
  const res = await fetch(`http://${CDP_HOST}${path}`);
  return res.json();
}

// create a fresh page target for a clean capture
const newTargetRes = await fetch(`http://${CDP_HOST}/json/new?${encodeURIComponent(TARGET_URL)}`, { method: "PUT" });
const newTarget = await newTargetRes.json();
const wsUrl = newTarget.webSocketDebuggerUrl;
console.error("Attached to target:", newTarget.id, wsUrl);

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
  if (process.env.CDP_DEBUG) console.error("RAW:", ev.data.slice(0, 300));
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
      log({
        type: "ws_sent",
        requestId: msg.params.requestId,
        payload: msg.params.response.payloadData,
      });
      break;
    case "Network.webSocketFrameReceived":
      log({
        type: "ws_recv",
        requestId: msg.params.requestId,
        payload: msg.params.response.payloadData,
      });
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
const navResult = await send("Page.navigate", { url: TARGET_URL });
console.error("Navigate result:", JSON.stringify(navResult));

console.error(`Capturing for ${DURATION_MS}ms -> ${OUT_FILE}`);
await new Promise((r) => setTimeout(r, DURATION_MS));

ws.close();
await new Promise((resolve) => out.end(resolve));
console.error(`Done. ${eventCount} events written.`);
process.exit(0);
