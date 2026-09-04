#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_edit.py — 改完設定檔之後，證明「只改了該改的，沒有夾帶重排」。

驗兩件事，缺一不可：
  1. 語意對不對：真的加了 / 刪了 / 改了那些東西，沒有多也沒有少。
  2. diff 形狀對不對：純新增 (N+ / 0-)、純刪除 (0+ / N-)、改值 (n+ / n-)。
     如果一堆被刪的行和被加的行「去掉空白後長得一模一樣」，那就是重排噪音——
     這種 diff 沒有人 review 得動，必須清掉。

用法：
    python3 verify_edit.py path/to/file.json          # 跟 git HEAD 比
    python3 verify_edit.py --base :0 path/to/f.json   # 跟 index 比
    python3 verify_edit.py old.json new.json          # 比兩個檔（優先用這個：拿動刀前的備份比）
    python3 verify_edit.py --restyle old.json new.json   # 刻意重排：驗收標準反過來

只要偵測到重排噪音就 exit 1。

--restyle 是「刻意換排版」的情境（例如把某一層改成跟另一層一樣的版型）。
這時重排是目的不是罪，驗收標準整組換掉：
    重排噪音        → 預期內，不算失敗
    diff 形狀       → 不判定
    語意差異        → 必須為 0
    字面量寫法差異  → 必須為 0
"""
from __future__ import print_function

import argparse
import collections
import difflib
import io
import json
import os
import subprocess
import sys
import unicodedata

CAP = 40


def _dw(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def row(label, value, width=14):
    return "%s%s: %s" % (label, " " * max(1, width - _dw(label)), value)


def norm(line):
    """去掉所有空白差異，留下實質內容。"""
    return "".join(line.split())


def read_file(path):
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def read_git(rev, path):
    d = os.path.dirname(os.path.abspath(path)) or "."
    try:
        root = subprocess.check_output(
            ["git", "-C", d, "rev-parse", "--show-toplevel"],
            stderr=subprocess.PIPE).decode().strip()
    except (subprocess.CalledProcessError, OSError):
        sys.stderr.write("✗ %s 不在 git 工作區裡，無法用 %s 當基準。\n"
                         "  改用「動刀前的備份檔」比對：verify_edit.py 備份 現檔\n" % (path, rev))
        sys.exit(2)
    rel = os.path.relpath(os.path.abspath(path), root)
    spec = "%s:%s" % (rev, rel) if rev != ":0" else ":0:%s" % rel
    try:
        return subprocess.check_output(["git", "-C", root, "show", spec],
                                       stderr=subprocess.PIPE).decode("utf-8")
    except subprocess.CalledProcessError:
        sys.stderr.write("✗ %s 在 %s 裡不存在（新檔或未提交），沒有可比的基準。\n"
                         "  這正是「動刀前先備份」的理由：verify_edit.py 備份 現檔\n" % (rel, rev))
        sys.exit(2)


# --------------------------------------------------------------------------

def check_meta(old, new, eol_is_git_artifact=False):
    issues = []
    notes = []

    def eol(t):
        crlf = t.count("\r\n")
        lf = t.count("\n") - crlf
        return "CRLF" if crlf and not lf else ("LF" if lf and not crlf else "mixed")

    eo, en = eol(old), eol(new)
    if eo != en:
        msg = "換行字元從 %s 變成 %s" % (eo, en)
        # git blob 存 LF、工作區 CRLF 是 autocrlf/.gitattributes 正規化的典型指紋，
        # 而且可能是別的 client（Windows 端）做的，本機 config 查不到。
        # 文字層級的爛編輯造成的是「mixed」，不會是整齊的 CRLF，所以這裡可以安全放行。
        if eol_is_git_artifact and eo == "LF" and en == "CRLF":
            notes.append(msg + "——git blob 存 LF、工作區 CRLF 是正規化的典型樣態，"
                               "多半不是你造成的（爛編輯造成的會是 mixed）；"
                               "要確定請拿動刀前的備份檔比")
        else:
            issues.append(msg)
    if "mixed" in (eo, en) and eo != en:
        issues.append("出現混合換行字元 —— 這是編輯時沒用 newline=\"\" 的典型症狀")
    if old.startswith("﻿") != new.startswith("﻿"):
        issues.append("BOM 被加上或拿掉了")
    if old.endswith("\n") != new.endswith("\n"):
        issues.append("檔尾換行從 %s 變成 %s"
                      % (bool(old.endswith("\n")), bool(new.endswith("\n"))))

    def indent_sig(t):
        c = collections.Counter()
        for ln in t.split("\n"):
            if ln.strip():
                w = len(ln) - len(ln.lstrip())
                if w:
                    c["\t" if ln[:1] == "\t" else "sp%d" % w] += 1
        return c

    a, b = indent_sig(old), indent_sig(new)
    gone = [k for k in a if k not in b]
    added = [k for k in b if k not in a]
    if gone or added:
        notes.append("縮排寬度種類變化：消失 %s / 新增 %s"
                     % (gone or "無", added or "無"))
    return issues, notes


def check_shape(old, new):
    a = old.split("\n")
    b = new.split("\n")
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    removed, addedl = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(a[i1:i2])
        if tag in ("replace", "insert"):
            addedl.extend(b[j1:j2])

    rem_norm = collections.Counter(norm(x) for x in removed if x.strip())
    add_norm = collections.Counter(norm(x) for x in addedl if x.strip())
    churn = rem_norm & add_norm  # 兩邊都有、只差空白 → 重排噪音
    churn_n = sum(churn.values())

    if not removed and addedl:
        shape = "純新增"
    elif removed and not addedl:
        shape = "純刪除"
    elif not removed and not addedl:
        shape = "沒有差異"
    else:
        shape = "有增有刪"
    return {
        "added": len([x for x in addedl if x.strip()]),
        "removed": len([x for x in removed if x.strip()]),
        "shape": shape,
        "churn": churn_n,
        "churn_examples": [k for k, _ in churn.most_common(5)],
    }


# --------------------------------------------------------------------------

_LIT = None


def json_literals(text):
    """掃出 JSON 原文裡每一個字面量，回傳 [(值, 原文寫法)]（key 也算）。

    語意比對抓不到 `0.10`→`0.1`、`1e3`→`1000.0`、`1`→`1.0`、`\\u4e2d`→`中`
    這種「值一樣但寫法被改掉」的差異——那正是 parser round-trip 的指紋。
    """
    global _LIT
    if _LIT is None:
        import re as _re
        _LIT = _re.compile(r'-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|true|false|null')
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            esc = False
            while j < n:
                if esc:
                    esc = False
                elif text[j] == "\\":
                    esc = True
                elif text[j] == '"':
                    break
                j += 1
            raw = text[i:j + 1]
            try:
                out.append((("s", json.loads(raw)), raw))
            except ValueError:
                pass
            i = j + 1
        elif c in "-0123456789tfn":
            m = _LIT.match(text, i)
            if m:
                raw = m.group(0)
                v = json.loads(raw)
                # bool/null 與數字分開標記，避免 True == 1 混在一起
                tag = "b" if raw in ("true", "false", "null") else "n"
                out.append(((tag, v), raw))
                i = m.end()
            else:
                i += 1
        else:
            i += 1
    return out


def check_literals(old, new):
    """回傳 [(值, 舊寫法, 新寫法)]——值相同但字面量寫法被改掉的地方。"""
    a, b = json_literals(old), json_literals(new)
    ka, kb = [x[0] for x in a], [x[0] for x in b]
    bad = []
    sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            continue
        for x, y in zip(a[i1:i2], b[j1:j2]):
            if x[1] != y[1]:
                bad.append((x[0][1], x[1], y[1]))
    return bad


def load_struct(text, path):
    ext = os.path.splitext(path)[1].lower()
    known = (".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties")
    if ext in (".json", ".jsonc", ".json5") or (ext not in known
                                                and text.lstrip()[:1] in "{["):
        try:
            return json.loads(text), "json"
        except ValueError:
            return None, None
    if ext in (".yaml", ".yml"):
        try:
            import yaml
            return yaml.safe_load(text), "yaml"
        except Exception:
            return None, None
    if ext == ".toml":
        try:
            import tomllib
            return tomllib.loads(text), "toml"
        except Exception:
            return None, None
    return None, None


def diff_struct(a, b, path="$", out=None):
    out = [] if out is None else out
    if len(out) > CAP:
        return out
    if type(a) is not type(b) and not (isinstance(a, (int, float))
                                       and isinstance(b, (int, float))):
        out.append(("改型別", path, "%s -> %s" % (type(a).__name__, type(b).__name__)))
        return out
    if isinstance(a, dict):
        for k in a:
            if k not in b:
                out.append(("刪除", "%s.%s" % (path, k), ""))
        for k in b:
            if k not in a:
                out.append(("新增", "%s.%s" % (path, k), ""))
        for k in a:
            if k in b:
                diff_struct(a[k], b[k], "%s.%s" % (path, k), out)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(("長度", path, "%d -> %d" % (len(a), len(b))))
        for i in range(min(len(a), len(b))):
            diff_struct(a[i], b[i], "%s[%d]" % (path, i), out)
    elif a != b:
        out.append(("改值", path, "%r -> %r" % (a, b)))
    return out


# --------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+")
    p.add_argument("--base", default="HEAD", help="git 比較基準（預設 HEAD，:0 為 index）")
    p.add_argument("--restyle", action="store_true",
                   help="刻意重排模式：重排噪音視為預期，改為要求語意與字面量寫法零差異")
    args = p.parse_args(argv)

    from_git = False
    if len(args.files) == 2 and all(os.path.exists(f) for f in args.files):
        old, new = read_file(args.files[0]), read_file(args.files[1])
        label = "%s -> %s" % tuple(args.files)
        path = args.files[1]
    elif len(args.files) == 1:
        path = args.files[0]
        old, new = read_git(args.base, path), read_file(path)
        label = "%s (%s -> worktree)" % (path, args.base)
        from_git = True
    else:
        p.error("給 1 個檔（跟 git 比）或 2 個檔（互比）")
        return 2

    print("=" * 72)
    print(label + ("   [刻意重排模式]" if args.restyle else ""))
    print("=" * 72)
    if from_git:
        print("  " + row("· 基準", "git %s。動刀前的備份檔是更可靠的基準" % args.base))

    bad = False

    shape = check_shape(old, new)
    print("  " + row("diff 形狀", "%s  (+%d / -%d)%s"
                     % (shape["shape"], shape["added"], shape["removed"],
                        "  ← 重排模式不判定" if args.restyle else "")))

    meta_issues, meta_notes = check_meta(old, new, eol_is_git_artifact=from_git)
    for m in meta_issues:
        print("  " + row("✗ 檔案層級", m))
    for m in meta_notes:
        print("  " + row("· 註記", m))
    bad |= bool(meta_issues)

    if shape["churn"]:
        if args.restyle:
            print("  " + row("· 重排噪音", "%d 行——重排模式下這是預期結果" % shape["churn"]))
        else:
            bad = True
            print("  " + row("✗ 重排噪音",
                             "%d 行「被刪又被加、只差空白」——這些是重排，不是異動"
                             % shape["churn"]))
            for ex in shape["churn_examples"]:
                print("      例：%s" % (ex[:70] + ("…" if len(ex) > 70 else "")))
            print("      → 把這些行還原（從備份還原後重做），只動真正要動的地方")
    else:
        print("  " + row("✓ 重排噪音", "無"))

    a, kind = load_struct(old, path)
    b, kind2 = load_struct(new, path)
    if kind and kind == kind2:
        d = diff_struct(a, b)
        mark = "✓" if (args.restyle and not d) else ("✗" if args.restyle else " ")
        print("  " + row("%s 語意差異 (%s)" % (mark, kind), "%d 處" % len(d)))
        for tag, ppath, detail in d[:CAP]:
            print("      %-6s %s %s" % (tag, ppath, detail))
        if len(d) > CAP:
            print("      …（還有更多，只列前 %d 筆）" % CAP)
        if args.restyle and d:
            bad = True
            print("      → 重排不該動到任何值，這些差異必須是 0")
    else:
        print("  " + row("語意差異", "無法解析成結構（只做了文字檢查）"))

    if kind == "json" and kind2 == "json":
        lits = check_literals(old, new)
        if lits:
            bad = True
            print("  " + row("✗ 字面量寫法", "%d 處值相同但寫法被改掉" % len(lits)))
            for v, x, y in lits[:8]:
                print("      %s -> %s" % (x, y))
            print("      → 這是 parser round-trip 的指紋（json.dump 之類），"
                  "改回文字層級編輯")
        else:
            print("  " + row("✓ 字面量寫法", "無改動（沒有經過 parser round-trip）"))

    print()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
