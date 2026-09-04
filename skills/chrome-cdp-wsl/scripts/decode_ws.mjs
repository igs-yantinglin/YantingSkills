import { readFileSync, writeFileSync } from "fs";

const file = process.argv[2];
const lines = readFileSync(file, "utf8").trim().split("\n").filter(Boolean);

const out = [];
for (const l of lines) {
  const o = JSON.parse(l);
  if (o.type !== "ws_sent" && o.type !== "ws_recv") continue;

  // frames often have a 4-char length-prefix header before the JSON, e.g. "0156{...}"
  let raw = o.payload;
  let body = raw;
  const m = raw.match(/^(\d{4})(\{.*\})$/s);
  if (m) body = m[2];

  let parsed;
  try {
    parsed = JSON.parse(body);
  } catch {
    out.push({ t: o.t, dir: o.type, raw });
    continue;
  }

  // decode base64 "proto" field if present (no .proto schema available -> just show byte length + hex preview)
  if (parsed.data && typeof parsed.data.proto === "string") {
    try {
      const buf = Buffer.from(parsed.data.proto, "base64");
      parsed.data.proto_decoded = {
        byteLength: buf.length,
        hexPreview: buf.subarray(0, 64).toString("hex"),
      };
    } catch {}
  }

  out.push({ t: o.t, dir: o.type, sys: parsed.sys, cmd: parsed.cmd, sn: parsed.sn, data: parsed.data });
}

const outFile = file.replace(/\.jsonl$/, "_decoded.json");
writeFileSync(outFile, JSON.stringify(out, null, 2));
console.log(`Decoded ${out.length} WS frames -> ${outFile}`);
