#!/usr/bin/env python3
"""像素作品驗收：在多組 dpr × 視窗大小下檢查「每個像素都在調色盤內、每個邏輯像素都是等大方塊」。

頁面需提供測試鉤子（見 SKILL.md）：
    window.__pixel = { W, H, PALETTE, canvas: view, get scale() { return scale; } };

用法：
    python3 pixel_audit.py page.html
    python3 pixel_audit.py page.html --dpr 1,1.25,1.5 --size 1000x700,777x555,1912x914 --alloc

注意：dpr 一定要用 --force-device-scale-factor 啟動瀏覽器 + CDP 截圖。
Playwright 的 device_scale_factor 是模擬的，devicePixelContentBoxSize 會回報 CSS 像素，
page.screenshot() 在強制 DSF 下又會縮回 CSS 尺寸，兩者都會讓測試失真。
"""
import argparse, base64, io, os, sys
from collections import Counter
from playwright.sync_api import sync_playwright
from PIL import Image

ap = argparse.ArgumentParser()
ap.add_argument('page')
ap.add_argument('--dpr', default='1,1.25,1.5,2')
ap.add_argument('--size', default='1000x700,777x555,1912x914')
ap.add_argument('--samples', type=int, default=12, help='每組抽樣幾個時間點（間隔 173ms，錯開動畫相位）')
ap.add_argument('--alloc', action='store_true', help='另外量 JIT 暖機後的堆配置量')
a = ap.parse_args()
url = 'file://' + os.path.abspath(a.page)
dprs = [float(x) for x in a.dpr.split(',')]
sizes = [tuple(int(v) for v in s.split('x')) for s in a.size.split(',')]


def shot(cdp):
    data = cdp.send('Page.captureScreenshot', {'format': 'png'})['data']
    return Image.open(io.BytesIO(base64.b64decode(data))).convert('RGB')


def runs_ok(pix, fixed, length, step, horizontal):
    """同色連續段長度必須是 scale 的整數倍（掐頭去尾，邊緣段可能被裁到）。"""
    bad, prev, cur, runs = 0, None, 0, []
    for i in range(length):
        c = pix[i, fixed] if horizontal else pix[fixed, i]
        if c == prev: cur += 1
        else:
            if prev is not None: runs.append(cur)
            prev, cur = c, 1
    for r in runs[1:]:
        if r % step: bad += 1
    return bad


all_ok = True
with sync_playwright() as p:
    for dpr in dprs:
        for w, h in sizes:
            b = p.chromium.launch(args=[f'--force-device-scale-factor={dpr}'])
            pg = b.new_page(viewport={'width': w, 'height': h})
            cdp = pg.context.new_cdp_session(pg)
            errs = []
            pg.on('pageerror', lambda e: errs.append(str(e)))
            pg.on('console', lambda m: m.type == 'error' and errs.append(m.text))
            pg.goto(url); pg.wait_for_timeout(400)
            if not pg.evaluate('!!window.__pixel'):
                sys.exit('頁面沒有 window.__pixel 測試鉤子')
            info = pg.evaluate('''() => { const t = __pixel, c = t.canvas;
                return { W: t.W, H: t.H, pal: t.PALETTE, scale: t.scale, bw: c.width, bh: c.height,
                         smooth: c.getContext('2d').imageSmoothingEnabled } }''')
            pal = {tuple(int(x[i:i + 2], 16) for i in (1, 3, 5)) for x in info['pal']}
            s = info['scale']

            bad_canvas = pg.evaluate('''(n) => new Promise(done => {
                const set = new Set(__pixel.PALETTE.map(x => parseInt(x.slice(1), 16)));
                let bad = 0, k = 0;
                const go = () => {
                  const c = __pixel.canvas, d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                  for (let i = 0; i < d.length; i += 4) if (!set.has(d[i] << 16 | d[i + 1] << 8 | d[i + 2])) bad++;
                  if (++k < n) setTimeout(go, 173); else done(bad);
                };
                go(); })''', a.samples)

            bad_shot = bad_runs = 0
            for _ in range(4):
                im = shot(cdp); pix = im.load()
                bad_shot += sum(n for n, rgb in im.getcolors(1 << 24) if rgb not in pal)
                if im.size == (info['bw'], info['bh']):
                    for y in range(0, im.size[1], max(1, im.size[1] // 23)):
                        bad_runs += runs_ok(pix, y, im.size[0], s, True)
                    for x in range(0, im.size[0], max(1, im.size[0] // 31)):
                        bad_runs += runs_ok(pix, x, im.size[1], s, False)
                pg.wait_for_timeout(211)

            # resize 當下那一幀：另掛一個 observer（排在頁面自己的之後），在 paint 前讀畫面
            pg.evaluate('''() => { window.__resizeBad = 0;
                const set = new Set(__pixel.PALETTE.map(x => parseInt(x.slice(1), 16)));
                new ResizeObserver(() => { const c = __pixel.canvas, d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                  for (let i = 0; i < d.length; i += 4) if (!set.has(d[i] << 16 | d[i + 1] << 8 | d[i + 2])) __resizeBad++; }
                ).observe(__pixel.canvas); }''')
            for rw, rh in ((w - 37, h - 23), (w, h)):
                pg.set_viewport_size({'width': rw, 'height': rh}); pg.wait_for_timeout(200)
            bad_resize = pg.evaluate('__resizeBad')

            device_match = im.size == (info['bw'], info['bh'])
            ok = (not errs and info['smooth'] is False and bad_canvas == 0 and bad_shot == 0 and bad_runs == 0
                  and bad_resize == 0 and device_match)
            all_ok &= ok
            print(f"dpr={dpr:<4} viewport={w}x{h:<5} backing={info['bw']}x{info['bh']} screenshot={im.size[0]}x{im.size[1]} "
                  f"scale={s} smoothing={info['smooth']} 調色盤外(canvas)={bad_canvas} 調色盤外(截圖)={bad_shot} "
                  f"方塊不等寬={bad_runs} resize幀調色盤外={bad_resize} errors={errs or 0} -> {'OK' if ok else 'FAIL'}")
            if bad_resize:
                print('  resize 當下那一幀是空的：resize() 改完 canvas 寬高後要立刻用現有 fb 再 present 一次')
            if not device_match:
                print('  backing store 不等於裝置像素：縮放沒用 device-pixel-content-box，會出現 8/9 px 交錯的不等寬像素')
            b.close()

    if a.alloc:
        b = p.chromium.launch()
        pg = b.new_page(viewport={'width': 1000, 'height': 700})
        cdp = pg.context.new_cdp_session(pg)
        pg.goto(url); pg.wait_for_timeout(12000)          # 先讓 JIT 暖機，前幾秒的 double 裝箱不是你的 bug
        cdp.send('HeapProfiler.enable')
        cdp.send('HeapProfiler.startSampling', {'samplingInterval': 32,
                 'includeObjectsCollectedByMajorGC': True, 'includeObjectsCollectedByMinorGC': True})
        pg.wait_for_timeout(3000)
        prof = cdp.send('HeapProfiler.stopSampling')['profile']
        agg = Counter()

        def walk(n):
            cf = n['callFrame']
            if n['selfSize'] and cf['url'].startswith('file://'):
                agg[f"{cf['functionName'] or '(anonymous)'}:{cf['lineNumber'] + 1}"] += n['selfSize']
            for c in n['children']: walk(c)
        walk(prof['head'])
        total = sum(agg.values())
        print(f"\n暖機後 3 秒內頁面程式配置 ≈ {total} bytes（{total / 3 / 60:.0f} B/frame）")
        for k, v in agg.most_common(8): print(f'  {v:>8}  {k}')
        b.close()

print('ALL OK' if all_ok else 'SOME FAIL')
sys.exit(0 if all_ok else 1)
