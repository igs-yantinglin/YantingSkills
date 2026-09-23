#!/usr/bin/env python3
"""把像素動畫頁面逐幀輸出成 GIF：直接讀索引色 framebuffer，不截圖、不量化，輸出後解碼回來驗收。

頁面需提供（動畫要是 frame 編號的純函式）：
    window.__export = { W, H, LOOP, FPS, PALETTE, frame(f) { render(f); return base64(fb); } };
    （可選）sheet() 回傳字表畫面的 base64(fb)，搭配 --glyphs 檢查點陣字

用法：
    python3 gif_export.py page.html --out anim.gif [--scale 4] [--sheet keyframes.png --sheet-frames 0,10,20]
"""
import argparse, base64, io, os, sys
from playwright.sync_api import sync_playwright
from PIL import Image, ImageSequence

HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument('html')
ap.add_argument('--out', required=True)
ap.add_argument('--scale', type=int, default=4)
ap.add_argument('--sheet', help='關鍵幀縮圖表輸出路徑')
ap.add_argument('--sheet-frames', help='逗號分隔的 frame 編號，預設均勻取 12 幀')
ap.add_argument('--glyphs', help='字表 PNG 輸出路徑（檢查點陣字）')
a = ap.parse_args()


def to_image(b64, W, H, pal_bytes):
    im = Image.frombytes('P', (W, H), base64.b64decode(b64))
    im.putpalette(pal_bytes)
    return im


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={'width': 1024, 'height': 640})
    errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto('file://' + os.path.abspath(a.html) + '?f=0')
    pg.wait_for_timeout(300)
    if errs or not pg.evaluate('!!window.__export'): sys.exit('page error: ' + '; '.join(errs))
    meta = pg.evaluate('({W: __export.W, H: __export.H, LOOP: __export.LOOP, FPS: __export.FPS, PALETTE: __export.PALETTE})')
    W, H, LOOP, FPS = meta['W'], meta['H'], meta['LOOP'], meta['FPS']
    pal = [tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) for h in meta['PALETTE']]
    pal_bytes = bytes(v for rgb in pal for v in rgb) + bytes(3 * (256 - len(pal)))
    if a.glyphs:
        to_image(pg.evaluate('__export.sheet()'), W, H, pal_bytes).resize((W * 4, H * 4), Image.NEAREST).save(a.glyphs)
        print('glyph sheet ->', a.glyphs)
    frames = [to_image(pg.evaluate(f'__export.frame({f})'), W, H, pal_bytes) for f in range(LOOP)]
    b.close()

s = a.scale
big = [im.resize((W * s, H * s), Image.NEAREST) for im in frames]
big[0].save(a.out, save_all=True, append_images=big[1:], duration=1000 // FPS, loop=0, optimize=False, disposal=1)
size = os.path.getsize(a.out)

# ---- 解碼回來驗收：不相信編碼器 ----
expect = [im.convert('RGB') for im in big]
dec = Image.open(a.out)
t, bad_frames, off_palette, total_ms, n_dec = 0, 0, 0, 0, 0
palset = set(pal)
for fr in ImageSequence.Iterator(dec):
    d = fr.info.get('duration', 0)
    rgb = fr.convert('RGB')
    src = expect[(total_ms * FPS) // 1000]           # 被合併的重複幀：用時間對回原始幀
    if rgb.tobytes() != src.tobytes(): bad_frames += 1
    off_palette += sum(n for n, c in rgb.getcolors(1 << 24) if c not in palset)
    total_ms += d; n_dec += 1
# 每個邏輯像素是 s×s 等色方塊（抽查所有幀）
blocks_bad = 0
for im in expect[::7]:
    px = im.load()
    for y in range(0, H * s, s):
        for x in range(0, W * s, s):
            c = px[x, y]
            if px[x + s - 1, y] != c or px[x, y + s - 1] != c or px[x + s - 1, y + s - 1] != c: blocks_bad += 1
ok = bad_frames == 0 and off_palette == 0 and total_ms == LOOP * 1000 // FPS and blocks_bad == 0
print(f'{a.out}: {W * s}x{H * s}, {LOOP} frames -> {n_dec} GIF frames after merging duplicates, '
      f'{total_ms} ms, {size / 1024:.0f} KB')
print(f'decode check: mismatched frames={bad_frames}, off-palette pixels={off_palette}, '
      f'non-uniform {s}x{s} blocks={blocks_bad} -> {"OK" if ok else "FAIL"}')

if a.sheet:
    idx = [int(x) for x in a.sheet_frames.split(',')] if a.sheet_frames else [i * LOOP // 12 for i in range(12)]
    cols, tw, th = 4, W * 2, H * 2
    rows = (len(idx) + cols - 1) // cols
    sheet = Image.new('RGB', (cols * (tw + 8) + 8, rows * (th + 8) + 8), (40, 40, 40))
    for i, f in enumerate(idx):
        sheet.paste(frames[f].convert('RGB').resize((tw, th), Image.NEAREST), (8 + (i % cols) * (tw + 8), 8 + (i // cols) * (th + 8)))
    sheet.save(a.sheet)
    print('contact sheet ->', a.sheet, 'frames', idx)
sys.exit(0 if ok else 1)
