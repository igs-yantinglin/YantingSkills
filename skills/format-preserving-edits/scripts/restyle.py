#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""restyle.py — 把某一段內容，照「同檔另一段」的排版重新輸出。

用在 `clone` 撐不住的時候：clone 只能原封不動複製一個結構相同的區塊，
但真實需求常常是「B 這一層要跟 A 這一層長一樣」，而 B 的結構跟 A 並不完全相同
（陣列長度不同、多幾個 key、key 名字不一樣）。

做法：把樣板層的**原始文字**解析成保留所有空白的樣板樹（每個容器記住
open / 分隔 / close 的原文），再拿目標層的資料照著樣板遞迴渲染。
全程文字層級輸出，不經過 json.dump，所以字面量寫法（1.0 / 1e3 / \\u4e2d）不會被改。

用法：
    python3 restyle.py f.json --model default --target AW           # dry-run
    python3 restyle.py f.json --model default --target AW --write
    python3 restyle.py *.json --write                               # 預設 default -> AW

安全機制（每一項不過就中止，不寫檔）：
  1. 自我還原測試：先用樣板重繪樣板層自己，必須**逐字元**還原原文。
     這一關同時驗掉 parser bug、renderer bug、以及重複 key（有重複 key 就還原不了）。
  2. 渲染結果重新 parse，必須與目標層原資料語意相同（含鍵序）。
  3. 換回全檔後重新 parse，必須與原檔語意相同。
  4. 字面量寫法逐一比對，必須零改動。
BOM / 換行字元 / 檔尾換行都原樣保留（用 newline="" 讀寫）。

改完務必再跑：
    python3 verify_edit.py --restyle <備份檔> <現檔>
"""
from __future__ import print_function

import argparse
import io
import json
import os
import re
import sys

WS = " \t\r\n"
NUM = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?|true|false|null")


class Fallback(Exception):
    """樣板撐不住目標資料的形狀（例如樣板是空容器但目標非空）。"""


# --------------------------------------------------------------------------
# 解析：保留所有空白原文

class Parser(object):
    def __init__(self, t, i=0):
        self.t = t
        self.i = i

    def skip_ws(self):
        t, j = self.t, self.i
        while j < len(t) and t[j] in WS:
            j += 1
        self.i = j

    def parse(self):
        self.skip_ws()
        c = self.t[self.i]
        if c == "{":
            return self.container("}", "o")
        if c == "[":
            return self.container("]", "a")
        return self.scalar()

    def scalar(self):
        t, s = self.t, self.i
        if t[s] == '"':
            j = s + 1
            esc = False
            while True:
                ch = t[j]
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    break
                j += 1
            self.i = j + 1
        else:
            m = NUM.match(t, s)
            if not m:
                raise ValueError("bad scalar at %d: %r" % (s, t[s:s + 20]))
            self.i = m.end()
        raw = t[s:self.i]
        return {"k": "s", "raw": raw, "val": json.loads(raw)}

    def container(self, cb, kind):
        t = self.t
        start = self.i
        self.i += 1
        self.skip_ws()
        if t[self.i] == cb:                       # 空容器：整段原文留著
            self.i += 1
            return {"k": kind, "empty": t[start:self.i], "children": [], "seps": []}
        open_text = t[start:self.i]
        children, seps = [], []
        while True:
            if kind == "o":
                key = self.scalar()
                p = self.i
                self.skip_ws()
                assert t[self.i] == ":", "expect : at %d" % self.i
                self.i += 1
                gap = t[p:self.i]                 # key 與 : 之間（含 :）
                p = self.i
                self.skip_ws()
                after = t[p:self.i]               # : 與 value 之間
                children.append({"key": key["val"], "keyraw": key["raw"],
                                 "gap": gap, "after": after, "val": self.parse()})
            else:
                children.append({"val": self.parse()})
            p = self.i
            self.skip_ws()
            if t[self.i] == ",":
                self.i += 1
                self.skip_ws()
                seps.append(t[p:self.i])          # 含前後空白與逗號
            else:
                assert t[self.i] == cb, "expect %s at %d" % (cb, self.i)
                close = t[p:self.i + 1]
                self.i += 1
                return {"k": kind, "open": open_text, "children": children,
                        "seps": seps, "close": close, "empty": None}


# --------------------------------------------------------------------------
# 渲染：照樣板輸出目標資料

def lit(v):
    return json.dumps(v, ensure_ascii=False)


class Renderer(object):
    def __init__(self):
        self.warnings = []

    def render(self, val, node):
        if isinstance(val, dict):
            if node is None or node["k"] != "o":
                raise Fallback("目標是物件，樣板是 %s" % (node and node["k"]))
            return self.container(list(val.items()), node, True)
        if isinstance(val, list):
            if node is None or node["k"] != "a":
                raise Fallback("目標是陣列，樣板是 %s" % (node and node["k"]))
            return self.container([(None, v) for v in val], node, False)
        # 純量：值相同就沿用原字面量寫法，保住 1.0 / 1e3 / 0.10 / 中
        if (node is not None and node["k"] == "s" and node["val"] == val
                and isinstance(node["val"], bool) == isinstance(val, bool)):
            return node["raw"]
        return lit(val)

    def container(self, items, node, is_obj):
        if not items:
            return node.get("empty") or ("{}" if is_obj else "[]")
        tmpl, seps = node["children"], node["seps"]
        if not tmpl:
            raise Fallback("樣板是空容器，沒有可依循的排版")
        by_key = {}
        if is_obj:
            for c in tmpl:
                by_key.setdefault(c["key"], c)

        out = [node["open"]]
        for i, item in enumerate(items):
            pos = tmpl[min(i, len(tmpl) - 1)]     # 位置對應（後備）
            if is_obj:
                k, v = item
                c = by_key.get(k)
                if c is None:
                    # 樣板沒有這個 key，只能用位置猜。同層條目排版若不一致就可能抄錯，
                    # 所以明確警告，讓人去看一眼。
                    c = pos
                    self.warnings.append(
                        "key %r 樣板中不存在，改用位置 [%d]（樣板該格是 %r）的排版"
                        % (k, min(i, len(tmpl) - 1), c["key"]))
                keyraw = c["keyraw"] if c["key"] == k else lit(k)
                out.append(keyraw + c["gap"] + c["after"])
            else:
                c, v = pos, item[1]
            out.append(self.render(v, c["val"]))
            if i < len(items) - 1:
                out.append(seps[min(i, len(seps) - 1)] if seps else ", ")
        out.append(node["close"])
        return "".join(out)


# --------------------------------------------------------------------------

def find_block(text, key):
    """回傳 (key 起點, '{' 位置, '}' 之後位置)。"""
    m = re.search(r'"%s"\s*:\s*\{' % re.escape(key), text)
    if not m:
        raise KeyError(key)
    i = m.end() - 1
    depth = 0
    instr = esc = False
    for j in range(i, len(text)):
        c = text[j]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
        elif c == '"':
            instr = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return m.start(), i, j + 1
    raise ValueError("括號不對稱")


def literals(text):
    """掃出每個字面量的原文寫法，用來確認沒有被 round-trip 改寫。"""
    out = []
    node = Parser(text).parse()

    def walk(n):
        if n["k"] == "s":
            out.append(n["raw"])
        else:
            for c in n["children"]:
                walk(c["val"])
    walk(node)
    return out


def restyle(path, model="default", target="AW", write=False):
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    bom = raw.startswith(u"﻿")
    body = raw[1:] if bom else raw
    data = json.loads(body)

    _, mb, me = find_block(body, model)
    tmpl_text = body[mb:me]
    tmpl = Parser(tmpl_text).parse()

    r = Renderer()
    # 安全機制 1：用樣板重繪樣板層自己，必須逐字元還原
    if r.render(data[model], tmpl) != tmpl_text:
        raise AssertionError("樣板自我還原失敗——parser/renderer 有 bug，或該層有重複 key")

    r = Renderer()
    new_block = r.render(data[target], tmpl)
    # 安全機制 2
    if json.loads(new_block) != data[target]:
        raise AssertionError("渲染結果與原資料語意不符")

    _, tb, te = find_block(body, target)
    old_block = body[tb:te]
    new_body = body[:tb] + new_block + body[te:]
    # 安全機制 3
    if json.loads(new_body) != data:
        raise AssertionError("換回全檔後語意不符")
    # 安全機制 4
    lo, ln = literals(old_block), literals(new_block)
    changed = [(x, y) for x, y in zip(lo, ln) if x != y]
    if len(lo) != len(ln) or changed:
        raise AssertionError("字面量寫法被改動：%s" % (changed[:5] or "數量不符"))

    if write:
        with io.open(path, "w", encoding="utf-8", newline="") as f:
            f.write((u"﻿" if bom else u"") + new_body)
    return {"old": te - tb, "new": len(new_block), "warnings": r.warnings}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+")
    p.add_argument("--model", default="default", help="當排版樣板的頂層 key（預設 default）")
    p.add_argument("--target", default="AW", help="要被重排的頂層 key（預設 AW）")
    p.add_argument("--write", action="store_true", help="真的寫檔（預設 dry-run）")
    args = p.parse_args(argv)

    rc = 0
    for path in args.files:
        name = os.path.basename(path)
        try:
            res = restyle(path, args.model, args.target, args.write)
        except (AssertionError, Fallback, KeyError, ValueError) as e:
            print("%-26s ✗ %s: %s" % (name, type(e).__name__, e))
            rc = 1
            continue
        print("%-26s %7d -> %7d bytes  %s"
              % (name, res["old"], res["new"], "已寫入" if args.write else "(dry-run)"))
        for w in res["warnings"]:
            print("      ⚠ %s" % w)
    if not args.write:
        print("\n  → 這是 dry-run。加 --write 才會寫檔；寫完再跑 "
              "verify_edit.py --restyle <備份> <現檔>")
    return rc


if __name__ == "__main__":
    sys.exit(main())
