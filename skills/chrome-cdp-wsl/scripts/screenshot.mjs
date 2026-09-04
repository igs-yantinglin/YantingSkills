import { writeFileSync } from "fs";
const CDP_HOST = "127.0.0.1:9222";
const TARGET_ID = process.argv[2];
const OUT = process.argv[3] || "shot.png";
const ws = new WebSocket(`ws://${CDP_HOST}/devtools/page/${TARGET_ID}`);
let msgId = 1;
const pending = new Map();
function send(method, params = {}) {
  const id = msgId++;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
}
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg.result); pending.delete(msg.id); }
};
const res = await send("Page.captureScreenshot", { format: "png" });
writeFileSync(OUT, Buffer.from(res.data, "base64"));
console.error("saved", OUT);
ws.close();
process.exit(0);
