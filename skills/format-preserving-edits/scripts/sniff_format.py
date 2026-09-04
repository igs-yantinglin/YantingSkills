#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sniff_format.py — 在動手改設定檔之前，先把維護者的排版慣例量出來。

目的不是「找出正確格式」，而是「找出這份檔案的多數習慣」，好讓新加的內容
混進去看不出來。人工維護的檔案一定有離群值（多數縮排 4 格、某一塊 2 格），
本工具會把主流與離群值分開列，讓你跟主流走、不要跟著離群值學、也不要順手
把離群值「修正」掉——那是另一個 commit 的事。

用法：
    python3 sniff_format.py path/to/file.json
    python3 sniff_format.py a.json b.json c.json      # 一次比對多份同族檔案
"""
from __future__ import print_function

import collections
import json
import os
import re
import sys
import unicodedata


def _dw(s):
    """字串的顯示寬度（CJK 算兩格），用來對齊報告欄位。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def row(label, value, width=14):
    return "%s%s: %s" % (label, " " * max(1, width - _dw(label)), value)


def pct(n, total):
    return 0.0 if not total else 100.0 * n / total


def top(counter, k=4):
    return ", ".join("%r×%d" % (v, c) for v, c in counter.most_common(k))


# --------------------------------------------------------------------------

def report_bytes(raw):
    out = []
    bom = raw.startswith(b"\xef\xbb\xbf")
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    eol = "CRLF" if crlf and not lf else ("LF" if lf and not crlf else
                                          "混用 (CRLF %d / LF %d)" % (crlf, lf))
    out.append(row("BOM", "有 (UTF-8 BOM，寫回時要保留)" if bom else "無"))
    out.append(row("換行", eol))
    out.append(row("檔尾換行", "有" if raw.endswith((b"\n", b"\r\n"))
                   else "沒有（寫回時不要擅自補）"))
    try:
        raw.decode("utf-8")
        enc = "UTF-8 可解"
    except UnicodeDecodeError:
        enc = "不是合法 UTF-8（可能是 Big5 / GBK，先確認再動）"
    out.append(row("編碼", enc))
    return out


def report_indent(lines):
    out = []
    tab_lines = sum(1 for ln in lines if ln[:1] == "\t")
    space_widths = collections.Counter()
    for ln in lines:
        if not ln.strip():
            continue
        if ln[:1] == " ":
            space_widths[len(ln) - len(ln.lstrip(" "))] += 1

    if tab_lines:
        out.append(row("縮排字元", "TAB (%d 行) / 空白 (%d 行)"
                       % (tab_lines, sum(space_widths.values()))))
    elif space_widths:
        out.append(row("縮排字元", "空白"))

    if space_widths:
        total = sum(space_widths.values())
        # 取「涵蓋率仍達 85% 的最大階距」——只看整除率的話 2 永遠會贏
        best, best_cov = 2, pct(total, total)
        for step in (8, 4, 3, 2):
            cov = pct(sum(c for w, c in space_widths.items() if w % step == 0), total)
            if cov >= 85.0:
                best, best_cov = step, cov
                break
        out.append(row("縮排階距", "%d 格（%.0f%% 的縮排行整除）" % (best, best_cov)))
        out.append(row("寬度分佈", top(space_widths, 6)))
        bad = sorted(w for w in space_widths if w % best)
        if bad:
            out.append(row("  ⚠ 離群縮排",
                           "%s → 跟主流走，不要學它，也不要順手改掉它"
                           % ", ".join(str(w) for w in bad)))
    return out


def report_generic(text, lines):
    out = []
    lens = sorted(len(ln) for ln in lines if ln.strip())
    if lens:
        out.append(row("行長 p50/p95/max", "%d / %d / %d"
                       % (lens[len(lens) // 2], lens[int(len(lens) * 0.95)], lens[-1]),
                       width=16))
    trail = sum(1 for ln in lines if ln.strip() and ln != ln.rstrip())
    if trail:
        out.append(row("行尾空白", "%d 行有（原本就有，別順手清掉）" % trail))
    blanks = collections.Counter()
    run = 0
    for ln in lines:
        if ln.strip():
            if run:
                blanks[run] += 1
            run = 0
        else:
            run += 1
    if blanks:
        out.append(row("空行分段", "%s（區塊之間慣用的空行數）" % top(blanks, 3)))
    hashes = sum(1 for ln in lines if ln.lstrip()[:1] == "#")
    slashes = sum(1 for ln in lines if ln.lstrip()[:2] == "//")
    if hashes or slashes:
        out.append(row("註解", "# ×%d / // ×%d（parser round-trip 會全部吃掉）"
                       % (hashes, slashes)))
    return out


def report_json(text):
    out = []
    try:
        data = json.loads(text)
    except ValueError as e:
        out.append(row("JSON", "解析失敗（%s）— 可能含註解或尾逗號" % e))
        data = None

    colon_sp = len(re.findall(r'"\s*:\s', text))
    colon_tight = len(re.findall(r'":\S', text))
    if colon_sp or colon_tight:
        out.append(row("冒號後", '有空白 ×%d / 沒空白 ×%d → 照多數'
                       % (colon_sp, colon_tight)))
    if re.search(r'"\s{2,}:', text):
        out.append(row("  ⚠ 對齊", "有 key 後補空白對齊冒號 → 新增的 key 也要對齊"))

    out.append(row("逗號後", "換行 ×%d / 空格 ×%d"
                   % (len(re.findall(r",\n", text)), len(re.findall(r",[ ]", text)))))

    esc = len(re.findall(r"\\u[0-9a-fA-F]{4}", text))
    nonascii = sum(1 for ch in text if ord(ch) > 127)
    if esc or nonascii:
        out.append(row("非 ASCII", "直接寫 %d 字 / \\uXXXX 逸出 %d 處 → 新增時照多數"
                       % (nonascii, esc)))
        if esc and nonascii:
            out.append(row("  ⚠ 混用",
                           "json.dump 的 ensure_ascii 只能二選一，會改掉另一半"))

    out.append(row("同行容器", "{...} ×%d / [...] ×%d（單行寫完的物件/陣列）"
                   % (len(re.findall(r"\{[^{}\n]*\}", text)),
                      len(re.findall(r"\[[^\[\]\n]*\]", text)))))

    if isinstance(data, dict) and data:
        keys = list(data.keys())
        out.append(row("最外層 key", "%d 個，%s" % (
            len(keys), "已排序" if keys == sorted(keys) else "維持插入序（不要排序）")))
    if re.search(r":\s*-?\d+\.0\b", text):
        out.append(row("數字寫法", "有 1.0 這種浮點寫法 → 別讓它變成 1"))
    big = re.findall(r":\s*(\d{16,})", text)
    if big:
        out.append(row("  ⚠ 大整數", "%s… → round-trip 可能掉精度" % big[0][:20]))
    return out


def report_inline_comments(lines):
    """行內註解（`key: v  # 說明`）——round-trip 的第一個受害者。"""
    n = sum(1 for ln in lines
            if ln.strip() and ln.lstrip()[:1] != "#" and re.search(r"\s#", ln))
    if not n:
        return []
    return [row("行內註解", "%d 行有 → 任何 parser round-trip 都會刪光" % n)]


def report_ini(lines):
    out = []
    eq_sp = sum(1 for ln in lines if re.match(r"^\s*[\w.\-]+\s+=\s", ln))
    eq_tight = sum(1 for ln in lines if re.match(r"^\s*[\w.\-]+=", ln))
    if eq_sp or eq_tight:
        out.append(row("賦值寫法", "'k = v' ×%d / 'k=v' ×%d" % (eq_sp, eq_tight)))
    out.extend(report_inline_comments(lines))
    return out


def report_yaml(lines):
    out = []
    flush = sum(1 for ln in lines if re.match(r"^- ", ln))
    indented = sum(1 for ln in lines if re.match(r"^\s+- ", ln))
    if flush or indented:
        out.append(row("清單縮排", "與 key 齊平 ×%d / 內縮 ×%d" % (flush, indented)))
    out.append(row("引號習慣", "單引號 %d / 雙引號 %d（多數純量可能根本沒引號）"
                   % (sum(ln.count("'") for ln in lines),
                      sum(ln.count('"') for ln in lines))))
    if any(re.search(r"[&*]\w", ln) for ln in lines):
        out.append(row("⚠ 錨點", "有 & / * 錨點 → round-trip 會展開成重複內容"))
    out.extend(report_inline_comments(lines))
    return out


def sniff(path):
    with open(path, "rb") as f:
        raw = f.read()
    text = raw.decode("utf-8", "replace")
    if text.startswith("﻿"):
        text = text[1:]
    # CRLF 檔的每一行都會殘留 \r，若不剝掉，行尾空白與行長會全部誤判
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]
    ext = os.path.splitext(path)[1].lower()

    print("=" * 72)
    print("%s  (%d bytes, %d lines)" % (path, len(raw), len(lines)))
    print("=" * 72)
    blocks = [report_bytes(raw), report_indent(lines), report_generic(text, lines)]
    # 副檔名優先——`[db]` 開頭的 INI 會被 "{[" 的猜測誤判成 JSON
    if ext in (".json", ".jsonc", ".json5"):
        blocks.append(report_json(text))
    elif ext in (".yaml", ".yml"):
        blocks.append(report_yaml(lines))
    elif ext in (".ini", ".cfg", ".conf", ".properties", ".env", ".toml"):
        blocks.append(report_ini(lines))
    elif text.lstrip()[:1] in "{[":
        blocks.append(report_json(text))
    for block in blocks:
        for line in block:
            print("  " + line)
    print()
    print("  → 新增內容時照上面的多數慣例手寫；最可靠的做法是複製一筆同類的既有"
          "條目當骨架，只改值。")
    print()


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    for path in argv[1:]:
        sniff(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
