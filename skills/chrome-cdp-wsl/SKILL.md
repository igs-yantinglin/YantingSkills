---
name: chrome-cdp-wsl
description: 從 WSL2 內部連線並操控 Windows 端的 Chrome（透過 Windows interop 執行 curl.exe/node.exe，不需要改 WSL 網路模式），用來截圖、讀 DOM、模擬點擊操作頁面、或用 CDP Network domain 擷取瀏覽器的 HTTP/WebSocket 封包，也包含遇到加密的應用層協定時，不用逆向演算法、直接 hook TextDecoder/JSON.parse 攔截解密後明文的技巧（例如「抓封包」「monitor network requests」「capture websocket traffic」「chrome remote debugging from wsl」「封包加密看不懂內容」「decode encrypted payload」）。適用情境：Claude Code 跑在 WSL2 裡，但要控制的 Chrome 開在 Windows 主機上。不要跟一般的 chrome-cdp（Linux 原生連線）搞混——那個假設 Chrome 跟 CLI 在同一台機器。
---

# WSL2 → Windows Chrome CDP（含封包擷取）

## 背景/血淚教訓

之前寫過一版指示「打開 `chrome://inspect/#remote-debugging` 切換開關」，那是錯的——那個開關是給
USB/Android 裝置探索用的，桌面版 Chrome 不會因此開放 CDP debug port。已刪除該版本，這份是實測過、
真的能用的流程。

真正卡關過的兩個點，務必記住：

1. **Chrome 136+ 對「預設 profile」封鎖了 `--remote-debugging-port`**（安全性修補，防止竊取
   已登入 session）。旗標會出現在程序的 command line 裡，但 port 根本不會開始監聽（curl 連線會
   timeout / connection refused / empty reply）。**解法：一定要額外帶 `--user-data-dir` 指到
   非預設路徑**，才會真的開 port。
2. **不要用 WSL 網路（NAT/mirrored/portproxy）去跨越 WSL↔Windows 這層**。改用 WSL 對 Windows
   的 interop：直接執行 `/mnt/c/Windows/System32/curl.exe`、`cmd.exe`、`node.exe` 這些
   Windows 端的執行檔，它們在 Windows 那一側原生跑，打 `127.0.0.1:9222` 直接就通，完全不用碰
   `.wslconfig`、`netsh portproxy`、防火牆規則。之前繞了一大圈想用 mirrored networking / netsh
   portproxy 都是不必要的（甚至 portproxy 綁住 0.0.0.0:9222 反而會跟 Chrome 自己搶 port，導致
   Chrome 開不了 debug server——如果之前踩過這個雷，記得先 `netsh interface portproxy delete
   v4tov4 listenaddress=0.0.0.0 listenport=9222`，用系統管理員權限跑）。

## 啟動 Chrome（debug 用，獨立 profile，不影響使用者原本的視窗）

```bash
/mnt/c/Windows/System32/cmd.exe /c start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="C:\Users\<user>\chrome-cdp-profile" ^
  --no-first-run --no-default-browser-check ^
  https://example.com
```

不需要關掉使用者原本開著的 Chrome——用不同的 `--user-data-dir` 就是完全獨立的一份 profile/程序，
兩邊互不干擾。

驗證有沒有真的開起來（跑在 Windows 端，用 interop）：

```bash
/mnt/c/Windows/System32/curl.exe -s -m 5 http://127.0.0.1:9222/json/version
```

回傳 JSON（含 `webSocketDebuggerUrl`）才算成功；空字串/timeout/`404` 都代表沒起來，回頭檢查
`--user-data-dir` 有沒有帶、port 有沒有被搶。

## 列出分頁 / 建立新分頁

```bash
/mnt/c/Windows/System32/curl.exe -s http://127.0.0.1:9222/json                     # 列出所有 target
/mnt/c/Windows/System32/curl.exe -s -X PUT "http://127.0.0.1:9222/json/new?<url>"  # 開新分頁（這版 Chrome 要 PUT，不是 GET）
```

## 用 node.exe（Windows 端）寫 CDP 腳本

WSL 裡的 `node` 版本可能太舊（沒有內建 `WebSocket`，Node < 22 需要 `--experimental-websocket`）。
更省事的作法是直接呼叫 **Windows 端**的 `node.exe`（通常在 `C:\Program Files\nodejs\node.exe`，
`where.exe node` 可查），新版通常是 Node 22+，內建 `WebSocket` 和 `fetch`：

```bash
cd /mnt/c/Users/<user>/chrome-cdp-profile
/mnt/c/Windows/System32/cmd.exe /c "node script.mjs arg1 arg2"
```

**URL 裡有 `&` 時千萬別直接當 argv 傳**——經過 `cmd.exe /c "..."` 這層會被截斷（`&` 是 cmd 的
命令分隔字元，即使外層加了引號也可能被錯誤解析）。改成把 URL 寫進檔案，腳本裡用 `@檔名` 的慣例去
讀檔案內容，避開所有 shell/cmd quoting 問題（`scripts/navigate_capture.mjs` 已經這樣處理）。

## 封包擷取（Network domain）

三支腳本都在 `scripts/`：

- **`navigate_capture.mjs <url|@file> [durationMs] [outFile]`**：開一個全新分頁、導航到指定
  URL、擷取該分頁的 HTTP request/response（不含子 iframe 的獨立 target，見下）。
- **`attach.mjs <targetId> [durationMs] [outFile]`**：附加到一個「已經存在」的分頁/iframe
  target（用 `/json` 查到的 `id`），不做導航，適合使用者自己手動操作時同步側錄。
- **`attach_all.mjs [durationMs] [outFile]`**：連 browser 層 WebSocket，`Target.setAutoAttach`
  自動附加所有新開的 **page** 類型 target。

**已知坑**：`Target.setAutoAttach` 預設只會自動附加 `page` 類型的 target，**不會**自動附加
`iframe` 類型的 target。很多網頁遊戲/SPA 會把主要邏輯（含 WebSocket）放在跨網域的 iframe
裡，這種 iframe 在新版 Chrome 會被列成獨立的 `"type": "iframe"` target，必須：

1. 先跑 `attach_all.mjs`（或直接 `curl http://127.0.0.1:9222/json`）確認有沒有 HTTP 流量，
   如果遲遲抓不到預期的 WebSocket 封包；
2. 重新 `curl http://127.0.0.1:9222/json`，找 `"type": "iframe"` 且 URL 看起來像遊戲/應用主體
   的那個 target，記下它的 `id`；
3. 改用 `attach.mjs <那個 iframe 的 id>` 直接連上去，才抓得到裡面的 WebSocket 封包
   （`Network.webSocketFrameSent` / `Network.webSocketFrameReceived`，payload 在
   `msg.params.response.payloadData`）。

輸出格式是 JSONL，每行一個事件，欄位視 `type` 而定：`request` / `response` / `ws_sent` /
`ws_recv` / `ws_created` / `ws_closed` / `failed` / `target_attached` / `target_detached`。

## 要抓完整的 HTTP body（不只 header）

`attach.mjs`/`navigate_capture.mjs`/`attach_all.mjs` 預設只記 `request`/`response` 的 header
（`Network.requestWillBeSent`/`Network.responseReceived` 給的資訊本來就不含 body）。如果目標
API 是走 binary POST（例如某些遊戲的即時對戰指令不走 WebSocket，是自訂 binary protocol 打
REST endpoint），要看到實際內容得用 **`scripts/attach_body.mjs`**，它額外做兩件事：

1. `Network.requestWillBeSent` 給的 `request.postData` 如果是 `null` 但 `request.hasPostData`
   是 `true`（常見於二進位 body），要另外呼叫 `Network.getRequestPostData(requestId)` 才拿得到
   內容，兩者都用 `Buffer.from(str, "binary").toString("base64")` 轉存，避免非 UTF-8 bytes 在
   JSON 字串化時被破壞。
2. 在 `Network.loadingFinished` 事件觸發時呼叫 `Network.getResponseBody(requestId)`，用
   `base64Encoded` 欄位判斷回傳的是不是已經是 base64。

**已知坑（今天真的踩到兩次才抓到）**：`Network.getResponseBody` 只有在對應的 request **還在
Chrome 的記憶體 buffer 裡**時才拿得到，一旦這個 CDP session 斷線（我們的腳本跑完 `ws.close()`）
或分頁 navigate 走了，之前的 response body 就直接消失、之後補呼叫也拿不回來。**這代表你不能先
讓使用者操作、事後才想起來要抓 body**——一定要先把 `attach_body.mjs` 啟動、確認它在跑（印出
`Attached (with bodies), capturing for ...`），**再**請使用者去做觸發流量的操作（點 spin、送出
表單等），時間窗開好再叫使用者動作，不要反過來。

## 「新視窗」開遊戲 vs. iframe 開遊戲，是兩種完全不同的抓法

同一類網頁遊戲大廳，有的用 `window.open()` 開一個新分頁載入遊戲（`type: "page"`），有的把遊戲塞進
同一頁的跨網域 `<iframe>`（`type: "iframe"`）。兩種在 `Target.setAutoAttach` 底下行為不一樣：

- **新視窗（`page`）**：`attach_all.mjs` 直接就會自動附加，不用額外處理，直接跑就對了。
- **iframe**：如上一節說的，預設不會自動附加，要手動去 `/json` 找 `"type": "iframe"` 的
  target id 再用 `attach.mjs` 接上去。

不確定是哪一種的時候，先跑 `attach_all.mjs`，抓不到預期封包再去查 `/json` 裡有沒有多出
`"type": "iframe"` 的 target，那就是漏網的那個。

## 用 cmd.exe 開 Chrome/GUI 程式時，記得背景執行

```bash
/mnt/c/Windows/System32/cmd.exe /c start "" "chrome.exe" ...
```

`cmd.exe /c start` 理論上會立刻把子行程丟到背景、自己馬上結束，但實測透過 WSL interop 呼叫時，
如果跟在同一個 bash 指令區塊裡緊接著再下一行別的指令（例如馬上接 `curl.exe` 測連線），偶爾會卡住、
整個 bash tool call 撞到 120 秒逾時被丟去背景執行，白白浪費一輪等待。保險作法：獨立一個指令呼叫
`start`，用 `nohup ... &` 或工具的背景執行選項丟出去，`sleep` 個幾秒再另外發一次
`curl.exe http://127.0.0.1:9222/json/version` 確認，不要預期 `start` 那個指令本身能在同一個
呼叫裡串接後續驗證動作。

## 事後整理：解 WS payload、抓圖片資源

- **`scripts/decode_ws.mjs <capture.jsonl>`**：把 `ws_sent`/`ws_recv` 的 payload 去掉常見的
  4 位數字長度前綴（例如 `"0156{...}"` → `{...}`）、解成 JSON，如果訊息裡有 `data.proto` 這種
  base64 欄位（常見於用 protobuf 包裝、但沒附 schema 的自製協定），順便 base64 decode 出
  byte 長度和 hex 預覽，方便肉眼比對「這個欄位有沒有變」而不用完整解 protobuf。
- **抓圖片/靜態資源不用透過瀏覽器**：從 capture 檔裡 `grep` 出 `mimeType` 是 `image/*` 或副檔名
  像圖片的 URL，直接用 WSL 自己的 `curl`（這些通常是一般公開網址，不是 `127.0.0.1`，不需要
  Windows interop）下載即可。瀏覽器內有些圖片會因為 CORS/ORB（`net::ERR_BLOCKED_BY_ORB`）在
  Network domain 裡顯示 `loadingFailed`，但那只是瀏覽器的同源限制擋住了頁面 JS 讀取，直接用
  `curl` 對同一個 URL 發請求完全不受影響，一樣抓得到檔案。

## 遇到加密協定：不用找出演算法，直接攔截「解密後」那一刻

如果 `/mpt/req` 這類 API 的 request/response body 是加密的（bytes 熵高、沒有 protobuf 該有的
tag 規律），**不要一開始就去啃混淆過的 JS 找加密演算法**——那通常要對付 Jscrambler 之類的
control-flow flattening + 字串陣列加密，static grep 幾乎沒用，是量級大很多的工作。更快的路是用
`scripts/hook_capture.mjs`：透過 `Runtime.evaluate` 把一段 hook script 注入到遊戲 iframe/page
target 裡，同時攔截四層：

1. `window.fetch` / `XMLHttpRequest.prototype.send`：記下 request/response 的原始 bytes
   （加密前後都在，方便比對長度差）。
2. `window.crypto.subtle` 的 `decrypt`/`encrypt`/`importKey`/... 全部方法：如果遊戲真的用
   瀏覽器原生 WebCrypto，這裡一定攔得到，呼叫次數是 0 就代表解密邏輯是純 JS/WASM 自己實作、
   沒有經過原生 API。
3. **`TextDecoder.prototype.decode` 和 `JSON.parse`**：這是關鍵一招。不管遊戲用什麼演算法
   解密（AES、自製 XOR、RC4...），解密完的 bytes 幾乎一定要转成字串再 `JSON.parse` 成物件才能
   讓遊戲邏輯用，所以在這兩個函式上攔截，**不需要理解中間的加密邏輯，直接看到解密後的明文**。
   （`hook_capture.mjs` 裡用 `text.indexOf('"type"') !== -1 && text.indexOf('"data"') !== -1`
   這種內容特徵去篩選要不要記錄，實際用時依目標協定的明文長相調整判斷條件；程式裡的
   `mpt/req` URL 關鍵字也要換成你的目標 API 路徑。）

驗證解出來的是不是真的明文，別只看格式對不對，要拿畫面上實際看得到的數字去對——例如解出的欄位
數值跟畫面上的 Balance／下注額／Transaction ID 完全吻合，才能確定不是誤判、不是隨機噪音。

這個方法拿不到「演算法叫什麼名字、金鑰怎麼衍生」，但通常這不重要——`hook_capture.mjs` 這種
持續運行的攔截器本身就是一個可重複使用的明文擷取工具，接上任何有同樣加密協定的頁面就能直接吐出
解密後的資料，QA/資料分析用途上比知道「用了 AES」更實用。

## 自己操控頁面觸發流量（不用等真人點）

要觸發加密協定分析，往往需要真的送出一次操作（點 spin、送出表單）才有流量可以攔截。如果現場沒有
人可以配合，可以用 CDP 自己模擬：

```bash
# scripts/click.mjs <targetId> <x> <y>  -- CSS pixel 座標，不是螢幕像素
```

用 `Input.dispatchMouseEvent` 依序送 `mouseMoved` → `mousePressed` → （等 ~60ms）→
`mouseReleased`，這個完整序列對 Canvas/WebGL 遊戲（Cocos、Unity WebGL 等）比較挑，只送單一個
`click` 類事件常常沒反應，一定要照這個順序、中間留一點間隔。座標要先用 `scripts/screenshot.mjs`
截圖或 `snap` 拿到的座標系去對齊，抓錯座標系（例如拿螢幕實體像素當 CSS 像素用，沒除以 DPR）點下去
會點空。

**授權範圍要說清楚**：這種自動點擊等於完全代替使用者操作頁面（包含在正式營運站台上真的送出下注
請求）。只有在使用者明確授權「這是我們自己的產品，在做 QA/封包測試」的前提下才這樣做；面對不確定
是不是自己資產的網站，不要自動化下注/送出這類有實際後果的操作，先跟使用者確認範圍。

## 解 protobuf 沒有 .proto schema 時

`scripts/pbdump.py <hex string>`：純 Python、零依賴的 protobuf wire-format dumper，照
varint tag 遞迴拆解 bytes，印出每個 field 的 field number、wire type、值（varint/64bit
double/32bit float/length-delimited bytes，bytes 欄位會嘗試遞迴當 submessage 解、也會嘗試當
UTF-8 字串解），沒有 `.proto` 定義時拿不到欄位名稱，但足夠拿去跟畫面上的已知數值（餘額、下注額）
比對，反推出「這個 field number 對應什麼意思」。

## 企業受控電腦的地雷（先排除，別急著懷疑政策擋你）

公司網域機器的 Chrome 常見 `HKLM\SOFTWARE\Policies\Google\Chrome` 登錄檔政策，第一時間看到
debug port 打不開容易誤判成「IT 政策鎖了 remote debugging」。實測下來，`RemoteDebuggingAllowed`
政策**沒有**設定的話，問題幾乎都是上面第 1 點（沒帶 `--user-data-dir`）或 port 被
`netsh portproxy` 搶走，跟公司政策無關。真的要確認，查：

```bash
/mnt/c/Windows/System32/reg.exe query "HKLM\SOFTWARE\Policies\Google\Chrome" /s
```

看有沒有 `RemoteDebuggingAllowed` 這個值，沒有就不是政策問題。
