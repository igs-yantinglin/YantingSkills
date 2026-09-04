const CDP_HOST = "127.0.0.1:9222";
const TARGET_ID = process.argv[2];
const X = Number(process.argv[3]);
const Y = Number(process.argv[4]);
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
await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: X, y: Y, buttons: 0 });
await send("Input.dispatchMouseEvent", { type: "mousePressed", x: X, y: Y, button: "left", buttons: 1, clickCount: 1 });
await new Promise((r) => setTimeout(r, 60));
await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: X, y: Y, button: "left", buttons: 0, clickCount: 1 });
console.error("clicked", X, Y);
ws.close();
process.exit(0);
