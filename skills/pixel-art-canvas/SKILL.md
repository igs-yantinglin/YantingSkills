---
name: pixel-art-canvas
description: 用純程式（vanilla JS + Canvas 2D、不靠圖檔素材）做像素風作品時的開發準則——低解析度索引色 framebuffer、整數倍縮放（含 Windows 125%/150% 螢幕縮放的 devicePixelRatio 陷阱）、固定調色盤、姿勢參數化 + 階梯取樣做出 8-12fps 手繪感、固定步長主迴圈、零配置粒子池、無縫循環，附 Playwright 驗收腳本檢查「每個像素都在調色盤內、每個邏輯像素都是等大方塊」，以及直接讀索引色 framebuffer 輸出 GIF 並解碼回來驗收的腳本。觸發詞：「像素風」「像素藝術」「pixel art」「點陣」「8-bit」「16-bit」「sprite 動畫」「復古遊戲畫面」「canvas 像素動畫」「像素特效」「像素 GIF」「做成 gif」，以及任何要用程式畫出像素風角色、場景、特效、loading 動畫的請求。一般網頁 UI 設計交給 frontend-design，這份只管像素渲染與動畫。
---

# 像素風 Canvas 作品開發準則

## 問題

「看起來像向量圖縮小」而不是像 16-bit sprite，根源幾乎都是**某個地方偷偷混了色**：
smoothing 內插、`globalAlpha` 半透明、`arc`/`stroke` 反鋸齒、小數座標、非整數倍縮放。
只要一個混色來源存在，就會冒出調色盤外的顏色或寬窄不一的像素，整張圖就破功。

麻煩的是這些問題**寫的當下看不出來**：在自己的視窗大小、100% 縮放下一切正常，換個
視窗尺寸、改個 resize、或在 Windows 125% 螢幕縮放下打開才露餡。所以做法是**從架構上
讓混色不可能發生**，再用腳本驗收，而不是靠肉眼。

## 核心決策：索引色 framebuffer

畫面不直接畫在 canvas 上，而是畫進一個 `Uint8Array(W*H)`，每格只存**調色盤 index**。
每幀把 index 經查表（LUT）轉成 RGBA、`putImageData` 到離屏 canvas，再整數倍放大貼到
顯示 canvas。這個選擇一次解決很多事：

- 「每個像素都來自調色盤」變成**構造上必然成立**，不用自律。
- 閃白、受擊、日夜色調 = 換一張 LUT（palette swap），不用重畫。
- 淡出 = 沿色階 ramp 換 index，不需要 alpha。
- 描邊、rim light、剪影檢查 = 對 index buffer 做鄰格查詢，幾行迴圈。

退而求其次的做法是只用 `fillRect` + 預先存好的 `fillStyle` 字串，但要自律不碰下面的禁用清單。

### 程式骨架（已實測：dpr 1/1.25/1.5/2 下都是零調色盤外像素、方塊等寬）

```html
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; background: #0b0a1a; } /* = PALETTE[0] */
  #view { position: fixed; inset: 0; width: 100%; height: 100%; display: block; image-rendering: pixelated; }
</style>
<canvas id="view"></canvas>
```

```js
const W = 128, H = 96;
const PALETTE = [
  '#0b0a1a', '#1a1733', '#2b2656', '#453d80', // 0-3 夜空 ramp
  '#f4f1de', '#c9c2a8',                       // 4-5 星/月
  '#3a2a2a', '#5e3b36', '#8c5a44', '#b8835a', // 6-9 暖色 ramp
  '#1e4d6b', '#2f8fb8', '#6fd6f2', '#ffffff', // 10-13 魔法 ramp
];
const LUT = new Uint32Array(PALETTE.length);          // index -> RGBA (little-endian ABGR)
PALETTE.forEach((hex, i) => {
  const n = parseInt(hex.slice(1), 16);
  LUT[i] = 0xff000000 | (n & 0xff) << 16 | (n & 0xff00) | n >>> 16;
});

const fb  = new Uint8Array(W * H);                    // 畫面只存調色盤 index
const off = document.createElement('canvas'); off.width = W; off.height = H;
const offCtx = off.getContext('2d');
const img = offCtx.createImageData(W, H);
const px  = new Uint32Array(img.data.buffer);

const view = document.getElementById('view');
const vctx = view.getContext('2d', { alpha: false });
let scale = 1, ox = 0, oy = 0;

function resize(bw, bh) {                             // bw/bh = 裝置像素
  view.width = bw; view.height = bh;                  // 這行會重置 context 狀態
  vctx.imageSmoothingEnabled = false;                 // 所以每次 resize 都要重設
  scale = Math.max(1, Math.floor(Math.min(bw / W, bh / H)));
  ox = (bw - W * scale) >> 1; oy = (bh - H * scale) >> 1;
  present(LUT, 0, 0);                                 // 改寬高會清空畫面，而這個 callback 在 render 之後、paint 之前跑
}
const ro = new ResizeObserver(([e]) => {             // 視窗大小、瀏覽器縮放、換螢幕(dpr 變)都會觸發
  const s = e.devicePixelContentBoxSize?.[0];         // 精確的裝置像素尺寸
  resize(s ? s.inlineSize : Math.round(e.contentRect.width  * devicePixelRatio),
         s ? s.blockSize  : Math.round(e.contentRect.height * devicePixelRatio));
});
try { ro.observe(view, { box: 'device-pixel-content-box' }); } catch { ro.observe(view); } // Safari 退路

function present(lut, shakeX, shakeY) {               // shake 單位是邏輯像素（整數）
  for (let i = 0; i < px.length; i++) px[i] = lut[fb[i]];
  offCtx.putImageData(img, 0, 0);
  vctx.fillStyle = PALETTE[0];                        // 預先存好的字串，不在 loop 裡組
  vctx.fillRect(0, 0, view.width, view.height);
  vctx.drawImage(off, ox + shakeX * scale, oy + shakeY * scale, W * scale, H * scale);
}

// 畫圖原語：全部寫進 fb，座標一律 Math.floor（不要用 |0，負數會往 0 截）
function pset(x, y, c) {
  x = Math.floor(x); y = Math.floor(y);
  if (x >= 0 && x < W && y >= 0 && y < H) fb[y * W + x] = c;
}
function rect(x, y, w, h, c) {
  const x0 = Math.max(0, Math.floor(x)), x1 = Math.min(W, Math.floor(x) + w);
  const y0 = Math.max(0, Math.floor(y)), y1 = Math.min(H, Math.floor(y) + h);
  for (let yy = y0; yy < y1; yy++) fb.fill(c, yy * W + x0, yy * W + x1);
}

window.__pixel = { W, H, PALETTE, canvas: view, get scale() { return scale; } }; // 驗收腳本用
```

閃白幀的 LUT 在初始化時預先做好，例如 `const LUT_FLASH = LUT.map((v, i) => i < 4 ? LUT[3] : LUT[13]);`，
播放時 `present(LUT_FLASH, ...)` 1-2 幀即可。

## 做法

### 1. 解析度與縮放

- 邏輯解析度從主角尺寸反推：主角約佔畫面高度 1/4～1/3。常見選擇 128x96、160x120、
  256x144 / 320x180（16:9）。
- 先量目標瀏覽器的**實際可視範圍**再算能拿到幾倍（例：1920x1080 螢幕、100% 縮放扣掉分頁列網址列後
  約 1912x914 → 128x96 拿 9 倍（1152x864），320x180 拿 5 倍（1600x900））。
  倍數太小（≤3）像素會糊成一片看不出手工感，太大則畫面細節不夠撐。
- 只用**整數倍**，放不滿的邊用背景最暗色補（letterbox 色要在調色盤內）。

### 2. 繪圖紀律（禁用清單）

| 禁用 | 原因 | 改用 |
|---|---|---|
| `imageSmoothingEnabled` 預設值 | 放大時內插出中間色 | 每次 resize 後重設 `false` |
| `globalAlpha`、rgba 半透明 | 混出調色盤外的色 | 沿 ramp 換 index，或 Bayer 有序抖色(dither) |
| `arc` / `stroke` / `lineTo` | 反鋸齒邊緣 | 直線用 Bresenham，圓用中點圓演算法，逐點 `pset` |
| `ctx.rotate` / `scale` 變換 | 旋轉後重取樣 = 混色 + 鋸齒爛掉 | 逐像素最近鄰取樣，或預先算好 8/16 個離散角度 |
| 漸層、`shadowBlur`、`filter` | 產生連續色階 | 2-4 階色帶 + 抖色 |
| `x \| 0` 取整 | 負數往 0 截，物體經過 x=0 會卡一格 | 全部統一 `Math.floor`（或統一 `Math.round`） |

**狀態保留浮點數，只在畫的那一刻取整**。不要把 `x` 本身存成整數，否則慢速移動會永遠卡住。

### 3. 調色盤

- 16-32 色，按**材質分 ramp**（每條 3-5 階，暗 → 亮），不是散裝挑色。
- ramp 要**偏移色相**：暗部往藍紫、亮部往黃，飽和度在中間階最高。只調明度的 ramp 看起來髒、塑膠感。
- 特效色（魔法、火焰）專屬，不要拿來畫角色或背景，才會「跳」出來。
- 預先建查表：`LIGHTER[c]`、`DARKER[c]`（同 ramp 往上/下一階），rim light、受光變化都靠查表換 index。

### 4. 角色 / sprite 建構

- 程式化建構：用 `rect` 與像素 run，或字串圖（`'..11..'` 每個字元對應 palette index），
  各部件相對於**錨點（pivot）**擺放，姿勢參數只改錨點位置 / 部件選擇。
- **描邊**：先畫進一張 mask，再把 mask 外、且四鄰有 mask 的格子塗描邊色。描邊色用相鄰材質
  ramp 的最暗階（selective outline），比一律純黑精緻。
- **單一光源**，所有明暗都照同一方向。rim light 也用 mask 做：角色像素中，**朝光源方向的鄰格
  不屬於角色**的，就換成 `LIGHTER[c]`（或特效色）；強度靠換不同階，不是 alpha。
- **剪影檢查**：把角色 mask 整塊塗成單色，一眼要認得出是什麼、在做什麼動作。認不出來就改造型，
  不是加細節。
- 避免三個經典錯誤：pillow shading（從輪廓往內一圈圈變暗）、banding（兩條色帶平行排成樓梯）、
  不規則鋸齒（斜線的階梯長度要規律，如 1-1-1 或 2-2-2，不要 2-1-3-1）。

### 5. 動畫

- **兩個時鐘**：模擬固定 60Hz 跑，但**角色姿勢用階梯時鐘取樣**（每 5-6 tick 才換一次 = 10-12fps）。
  光把位置取整做不出 8-12fps 手繪感——快速動作還是每 60Hz 動一格，看起來像滑動。
  粒子、閃光這類特效可以跑在 60Hz 快時鐘上，反而更有層次。
- 姿勢參數（角度、抬手、傾頭、擺動）在關鍵幀之間用 easing 平滑插值，畫的時候才取整。
- **狀態機寫成「循環相位的純函式」**：`t = tick % LOOP_TICKS`，每個狀態佔固定 tick 數、總和 =
  `LOOP_TICKS`，由 `t` 推出目前狀態與區段進度。不要用「上一個狀態結束時切換」的可變狀態機，
  那種寫法會累積誤差，循環接縫會跳。
- **無縫循環三條件**：
  1. 所有週期性東西（星星閃爍、呼吸、浮動）的週期都要**整除** `LOOP_TICKS`。
  2. 用有種子的 PRNG（mulberry32），每圈 `t === 0` 時重設種子。`Math.random` 必破接縫。
  3. 最後一個狀態的長度 > 最長粒子壽命，確保回到起點時畫面上沒有殘留的暫時粒子。
- 打擊感：閃白 1-2 幀（換 LUT）、畫面震動 1-2 **邏輯像素**的整數位移且快速衰減。

```js
const STEP_MS = 1000 / 60;
const LOOP_TICKS = 480;                               // 8 秒一圈；所有週期都要整除它
const ANIM_HOLD = 6;                                  // 60/6 = 10fps 的姿勢取樣
let tick = 0, acc = 0, last = performance.now();

function frame(now) {
  acc += Math.min(now - last, 250); last = now;       // 切分頁回來不要一口氣補幾千步
  while (acc >= STEP_MS) { update(); acc -= STEP_MS; } // update() 結尾 tick++
  render();
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// render() 裡：
//   const t = tick % LOOP_TICKS;
//   const poseT = t - t % ANIM_HOLD;                 // 姿勢用這個取樣；粒子/閃光用 t
```

### 6. 零配置與粒子池

迴圈裡（`update` / `render` / 每幀呼叫的函式）不准出現：物件/陣列字面量、closure、
`forEach`/`map`/`filter`、解構、spread、樣板字串或字串相加（例如每幀組 `rgb(...)` 給 `fillStyle`）、`new`。
粒子用 **SoA typed array + 固定上限 + ring 覆蓋最舊**：

```js
const MAXP = 256;
const pX = new Float32Array(MAXP), pY = new Float32Array(MAXP);
const pVX = new Float32Array(MAXP), pVY = new Float32Array(MAXP);
const pAge = new Uint16Array(MAXP), pLife = new Uint16Array(MAXP); // pLife 0 = 空槽
const RAMP = new Uint8Array([13, 12, 11, 10, 2]);     // 白 -> 魔法色 -> 暗，用 index 淡出不用 alpha
let pNext = 0;
function spawn(x, y, vx, vy, life) {
  const i = pNext; pNext = (pNext + 1) % MAXP;        // 滿了就覆蓋最舊的
  pX[i] = x; pY[i] = y; pVX[i] = vx; pVY[i] = vy; pAge[i] = 0; pLife[i] = life;
}
function updateParticles() {
  for (let i = 0; i < MAXP; i++) {
    if (!pLife[i]) continue;
    if (++pAge[i] >= pLife[i]) { pLife[i] = 0; continue; }
    pX[i] += pVX[i]; pY[i] += pVY[i];
  }
}
function drawParticles() {
  for (let i = 0; i < MAXP; i++)
    if (pLife[i]) pset(pX[i], pY[i], RAMP[Math.floor(pAge[i] * RAMP.length / pLife[i])]);
}

let seed = 1;                                         // mulberry32；每圈 t === 0 時 seed = 1
function rand() {
  seed |= 0; seed = seed + 0x6D2B79F5 | 0;
  let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
  t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
  return ((t ^ t >>> 14) >>> 0) / 4294967296;
}
```

環繞、內旋、爆散都只是 `spawn` 時給不同的初速，或在 `updateParticles` 裡加一條依 `t` 選的速度規則，
不用為每種特效開新的資料結構。

### 7. 場景

- 背景低對比、低飽和，主角是全畫面**明度對比最高**的東西；剪影要跟背景在明度上分得開。
- 星星 1px、閃爍用換 index（亮 ↔ 暗兩階），週期整除 `LOOP_TICKS`。
- 元素越少越好：天空、月亮、一條地面線就夠，細節留給主角。

## 踩過的雷（都有實測）

- **只在初始化設一次 `imageSmoothingEnabled = false`**：任何一次 resize 改了 `canvas.width` 就被重置回
  `true`。實測在 dpr 1.25 下整張顯示 canvas 冒出 23 萬個調色盤外像素，但在自己螢幕上肉眼只覺得「有點糊」。
- **用 CSS 像素算整數倍**：Windows 125% 縮放時，CSS 7 倍 = 裝置 8.75 倍，棋盤格實測出現 9px 與 8px
  交錯的不等寬像素（95 格 9px、32 格 8px）。顏色都還在調色盤內，所以只驗調色盤抓不到，要驗方塊寬度。
  一定要用 `device-pixel-content-box` 拿裝置像素尺寸、backing store 設成裝置像素。
- **用 Playwright 的 `device_scale_factor` 測 dpr**：那是模擬的，`devicePixelContentBoxSize` 會回報 CSS 像素，
  正確的程式看起來反而是錯的。要用 `--force-device-scale-factor` 啟動 + CDP `Page.captureScreenshot`
  （強制 DSF 下 `page.screenshot()` 會縮回 CSS 尺寸，又把問題藏起來）。驗收腳本已處理好。
- **量記憶體配置時沒暖機**：頭幾秒還在直譯器階段，每個浮點數都會裝箱成 HeapNumber，profile 看起來
  到處在配置。暖機 10 秒以上再量；暖機後只剩「偶爾才跑的爆發段落」（例如只在 CAST 瞬間呼叫的
  `rand()`）會有每圈幾十 KB 的數字裝箱，那是 V8 分層編譯的行為，不是你的 bug。
  把浮點狀態搬進 typed array 也消不掉（實測過），不用去追。真正要抓的是含字面量 / closure / 字串的函式。
- **resize 後沒有立刻重畫**：ResizeObserver 的 callback 在 rAF 之後、paint 之前執行，`resize()` 改
  `view.width` 會把剛畫好的畫面清掉，那一幀就以全黑（`alpha:false`）上屏。實測每次 resize 都是整張
  調色盤外；夜景看不太出來，淺色背景拖視窗時會一直閃。所以 `resize()` 結尾要用現有的 `fb` 再 `present` 一次。
  靜態截圖驗不到，驗收腳本有另外掛 observer 在 resize 當下讀畫面。
- **震動做在 blit 位移**：邊緣會露出 letterbox，所以 letterbox 色必須是調色盤裡的背景最暗色，
  不然震動那幾幀就會閃出異色邊。

## 輸出 GIF

交付物是 GIF（或任何固定幀率的影片）時，跟上面「60Hz 模擬 + 階梯取樣」的做法不同，改成：

- **時間軸以輸出幀為單位設計**（例如 10fps）。每個事件至少佔 1 幀、起訖都落在幀邊界。
  在 60Hz 下只持續 1-2 tick 的閃光、震動，匯出時會被抽樣直接抽掉。
- **整部動畫寫成 frame 編號的純函式**。粒子不用可變的池，改用 `(出生 frame, 粒子 id)` 餵給
  hash 算出角度、速度、壽命，再用解析式算當下位置。這樣任意一幀都能單獨渲染，接縫天生無縫。
  即時預覽就用牆上時鐘換算 frame 編號，不需要追趕邏輯。
- **閃光、淡出做成改 `fb` 的 index remap**（`fb[i] = MAP[fb[i]]`），不要用換 LUT。
  匯出讀的是 `fb`，換 LUT 的效果會在 GIF 裡消失。
- **匯出直接讀 index buffer**，放進 PIL `P` mode + 同一份調色盤，NEAREST 放大後 `save(optimize=False)`。
  不要截圖再量化，那會混色、抖色。現成腳本：

  ```bash
  python3 ~/.claude/skills/pixel-art-canvas/scripts/gif_export.py page.html --out anim.gif --scale 4 --sheet keyframes.png
  ```

  頁面要掛 `window.__export = { W, H, LOOP, FPS, PALETTE, frame(f) }`，`frame(f)` 回傳 base64 的 fb。
- **驗收要把 GIF 解碼回來比**，不要相信編碼器。Pillow 會把連續相同的幀合併、延長 duration，
  所以要用累計時間對回原始幀，逐幀比 RGB、檢查 duration 總和、調色盤外像素、方塊是否等寬。
  腳本已內建這些檢查。
- **一定要看關鍵幀縮圖表**（`--sheet`）。腳本判斷不了字讀不讀得出來、特效夠不夠顯眼。
  實測第一版的施法特效全是 1px 火花，縮圖表上幾乎看不見。要補上地面魔法陣、擴散衝擊波、
  5x5 光球 + 色階拖尾，才撐得起「成果」那一段。
- **點陣字先過關再蓋場景**。字集從所有要顯示的字串自動推出，缺字或超出欄寬就在建置時丟例外。
  先輸出一張字表 PNG 用眼睛看過，再開始排版。
- 只在低幀率才重算的函式比較難被 JIT 最佳化，數字裝箱的配置量會比 60fps 迴圈多一些，這是正常的。
  但 `--alloc` 抓到的陣列字面量（例如每次呼叫都 `const C = [RED, YELLOW, GREEN]`）是真的要搬出迴圈。

## 3D 物件（旋轉展示、爆炸圖）

產品轉一圈、拆解這類鏡頭，用一個小型正交軟體渲染器做，不要逐幀手畫：
- 物件由平面貼圖四邊形組成，每個四邊形用反向仿射映射取樣。
- 貼圖在像素中心做最近鄰取樣。
- 用 z-buffer 處理遮擋，用預先存好的外法線做背面剔除。
- 平面著色時，把法線對光的結果量化成色階，再用 index remap 套上去。

實測踩過的：

- **側對的面會消失**：只有正反兩面的薄片轉到 90° 會整個不見。要做成擠出的圓角矩形稜柱，
  沿分段的圓角輪廓生出真的側面四邊形。
- **每一片貼圖都要套同樣的圓角遮罩**，包括只有單色的底面。方形的單色貼圖會從圓角的機身背後露出一圈方角。
- **內部零件在組合狀態下要藏在外殼體積裡**，頂面比外殼頂面低一點。這樣開始拆解那一刻畫面不會跳，
  零件是「從殼裡升起來」。
- **爆炸圖只沿法線抬高會互相蓋住**：從上方看，上層會把下層整個遮掉，要秀的零件就看不到了。
  每層要再沿長軸錯開。一定要看過截圖，確認主角零件在畫面上是露出來的。
- **亮一階的查表不能跨出材質**：PCB 綠的 LIGHTER 接到銀色，受光時整片主機板會變成金屬灰。
  每條色階都要有自己的最亮階，不夠就補一色。
- **不要拉近鏡頭**：放大低解析貼圖就是 mixel。特寫另外用原生解析度畫一個場景，再用 index 空間的
  圓形轉場（iris）接過去。特寫內容要跟著圓心平移對齊，不然圓裡露出來的是特寫的邊角。
- **旋轉用 20fps**（GIF 50ms 可以精確表示），10fps 轉一圈太頓；15fps 的 66.7ms 存不進 GIF。
- **反光帶**：面轉到跟半向量對齊時讓一條斜線掃過，幾行程式就有產品片的質感。
- **輪廓光**：物件左、上兩邊的邊緣像素亮一階，旋轉時輪廓才不會融進暗背景。
- **驗證順序**：先用棋盤格貼圖驗證渲染器，看 0-360° 的角度條和傾斜姿勢，檢查有沒有裂縫、剔除錯誤、
  z-fighting，再開始畫美術。

## 驗收

寫完先跑腳本，全部 OK 才算完成：

```bash
python3 ~/.claude/skills/pixel-art-canvas/scripts/pixel_audit.py path/to/page.html          # dpr 1/1.25/1.5/2 × 三種視窗
python3 ~/.claude/skills/pixel-art-canvas/scripts/pixel_audit.py path/to/page.html --alloc  # 加量暖機後的配置
```

腳本會抽多個時間點（錯開動畫相位，才抽得到閃白/震動幀）檢查：顯示 canvas 與裝置像素截圖都
**零調色盤外像素**、backing store = 裝置像素、同色連續段長度都是 scale 的整數倍、改視窗大小當下那一幀也沒有調色盤外像素、沒有 console error。
頁面要掛 `window.__pixel` 鉤子（骨架裡已有）。

腳本驗不到、要自己看的：

- **剪影**：暫時把角色 mask 塗單色截一張圖，認不認得出動作。
- **循環接縫**：在 `t = LOOP_TICKS - 1 → 0` 附近截連續幾幀，不能跳。
- **節奏**：姿勢換格要看得出是 10fps 左右的一格一格，不是滑的；特效則是順的。
- **截圖給使用者看之前**，自己先放大看一次描邊、rim light、鋸齒規不規律。
