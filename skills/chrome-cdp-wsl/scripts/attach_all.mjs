import { createWriteStream } from "fs";

const CDP_HOST = "127.0.0.1:9222";
const DURATION_MS = Number(process.argv[2] || 120000);
const OUT_FILE = process.argv[3] || "capture_all.jsonl";

const out = createWriteStream(OUT_FILE, { flags: "w" });
let eventCount = 0;
function log(obj) {
  eventCount++;
  out.write(JSON.stringify({ t: Date.now(), ...obj }) + "\n");
}

const verRes = await fetch(`http://${CDP_HOST}/json/version`);
const ver = await verRes.json();
const ws = new WebSocket(ver.webSocketDebuggerUrl);

let msgId = 1;
const pending = new Map();
function send(method, params = {}, sessionId) {
  const id = msgId++;
  const payload = { id, method, params };
  if (sessionId) payload.sessionId = sessionId;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    ws.send(JSON.stringify(payload));
  });
}

await new Promise((resolve, reject) => {
  ws.onopen = resolve;
  ws.onerror = reject;
});

const knownSessions = new Set();

ws.onmessage = async (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg.result);
    pending.delete(msg.id);
    return;
  }
  if (!msg.method) return;

  if (msg.method === "Target.attachedToTarget") {
    const { sessionId, targetInfo } = msg.params;
    if (!knownSessions.has(sessionId)) {
      knownSessions.add(sessionId);
      log({ type: "target_attached", sessionId, targetId: targetInfo.targetId, targetType: targetInfo.type, url: targetInfo.url });
      await send("Network.enable", {}, sessionId);
      await send("Runtime.runIfWaitingForDebugger", {}, sessionId);
    }
    return;
  }
  if (msg.method === "Target.detachedFromTarget") {
    log({ type: "target_detached", sessionId: msg.params.sessionId });
    return;
  }

  const sessionId = msg.sessionId;
  switch (msg.method) {
    case "Network.requestWillBeSent":
      log({ type: "request", sessionId, requestId: msg.params.requestId, url: msg.params.request.url, method: msg.params.request.method, headers: msg.params.request.headers, postData: msg.params.request.postData });
      break;
    case "Network.responseReceived":
      log({ type: "response", sessionId, requestId: msg.params.requestId, url: msg.params.response.url, status: msg.params.response.status, headers: msg.params.response.headers, mimeType: msg.params.response.mimeType });
      break;
    case "Network.webSocketCreated":
      log({ type: "ws_created", sessionId, requestId: msg.params.requestId, url: msg.params.url });
      break;
    case "Network.webSocketFrameSent":
      log({ type: "ws_sent", sessionId, requestId: msg.params.requestId, payload: msg.params.response.payloadData });
      break;
    case "Network.webSocketFrameReceived":
      log({ type: "ws_recv", sessionId, requestId: msg.params.requestId, payload: msg.params.response.payloadData });
      break;
    case "Network.webSocketClosed":
      log({ type: "ws_closed", sessionId, requestId: msg.params.requestId });
      break;
    case "Network.loadingFailed":
      log({ type: "failed", sessionId, requestId: msg.params.requestId, errorText: msg.params.errorText });
      break;
  }
};

await send("Target.setDiscoverTargets", { discover: true });
await send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true });

console.error(`Auto-attach active, capturing for ${DURATION_MS}ms -> ${OUT_FILE}`);
await new Promise((r) => setTimeout(r, DURATION_MS));

ws.close();
await new Promise((resolve) => out.end(resolve));
console.error(`Done. ${eventCount} events written.`);
process.exit(0);
