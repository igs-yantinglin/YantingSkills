#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""json_surgery.py — 在不重排整份檔案的前提下，對 JSON 做文字層級的增 / 刪 / 改。

為什麼不要用 json.load() + json.dump()：
    那是一次 round-trip，等於用序列化器的品味覆寫維護者的品味。縮排、鍵序、
    空行分段、逗號位置、非 ASCII 逸出、數字寫法（1.0 / 1e3）、重複 key、註解
    全部會被改掉或吃掉。一行的異動會變成上千行的 diff，review 的人只能盲簽。

這支工具把 JSON 當「有結構的文字」看：用字串感知（並容忍 // 與 /* */ 註解）的
括號配對，找出某個 key 佔據的字元範圍，只動那段，其餘位元組原封不動。

常用：
    python3 json_surgery.py keys   f.json --scope feature_setting
    python3 json_surgery.py show   f.json --key ITEM_A
    python3 json_surgery.py clone  f.json --key ITEM_A --to ITEM_B -i
    python3 json_surgery.py insert f.json --after ITEM_A --block-file new.txt -i
    python3 json_surgery.py remove f.json --key OLD_ITEM --scope feature_setting.old_item -i
    python3 json_surgery.py set    f.json --key Price --scope ItemBeta --value '"5999"' -i

預設輸出到 stdout（不改檔）；加 -i / --in-place 才寫回。
"""
from __future__ import print_function

import argparse
import io
import json
import sys

WS = " \t\r\n"


class SurgeryError(Exception):
    pass


# --------------------------------------------------------------------------
# 掃描：字串感知 + 容忍註解
# --------------------------------------------------------------------------

def _skip_ws(t, i):
    """跳過空白與註解，回傳下一個有意義字元的位置。"""
    n = len(t)
    while i < n:
        c = t[i]
        if c in WS:
            i += 1
        elif c == "/" and i + 1 < n and t[i + 1] == "/":
            j = t.find("\n", i)
            i = n if j == -1 else j + 1
        elif c == "/" and i + 1 < n and t[i + 1] == "*":
            j = t.find("*/", i + 2)
            if j == -1:
                raise SurgeryError("未閉合的區塊註解 @%d" % i)
            i = j + 2
        else:
            return i
    return i


def _skip_string(t, i):
    """t[i] 必須是開頭的雙引號；回傳結尾引號的下一個位置。"""
    if t[i] != '"':
        raise SurgeryError("預期字串開頭 @%d，實際是 %r" % (i, t[i]))
    i += 1
    n = len(t)
    while i < n:
        c = t[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    raise SurgeryError("未閉合的字串")


def _skip_value(t, i):
    """回傳 value 結束後的位置（不含後面的逗號 / 空白）。"""
    i = _skip_ws(t, i)
    if i >= len(t):
        raise SurgeryError("檔案在 value 之前就結束了")
    c = t[i]
    if c == '"':
        return _skip_string(t, i)
    if c in "{[":
        depth = 0
        n = len(t)
        while i < n:
            i2 = _skip_ws(t, i)
            if i2 != i:
                i = i2
                continue
            ch = t[i]
            if ch == '"':
                i = _skip_string(t, i)
                continue
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        raise SurgeryError("未閉合的 %s" % c)
    # scalar: true / false / null / number
    n = len(t)
    j = i
    while j < n and t[j] not in ",}]" and t[j] not in WS:
        j += 1
    if j == i:
        raise SurgeryError("空的 value @%d" % i)
    return j


class Member(object):
    """物件裡的一組 key: value。所有欄位都是字元索引。"""

    def __init__(self, key, key_start, key_end, value_start, value_end):
        self.key = key
        self.key_start = key_start
        self.key_end = key_end
        self.value_start = value_start
        self.value_end = value_end

    def __repr__(self):
        return "<Member %r %d..%d>" % (self.key, self.key_start, self.value_end)


def iter_members(t, obj_start):
    """obj_start 指向 '{'；依檔案順序 yield Member。"""
    if t[obj_start] != "{":
        raise SurgeryError("預期物件 '{' @%d，實際是 %r" % (obj_start, t[obj_start]))
    i = obj_start + 1
    n = len(t)
    while True:
        i = _skip_ws(t, i)
        if i >= n:
            raise SurgeryError("未閉合的物件")
        if t[i] == "}":
            return
        if t[i] == ",":
            i += 1
            continue
        if t[i] != '"':
            raise SurgeryError("預期 key 的雙引號 @%d，實際是 %r" % (i, t[i]))
        ks = i
        ke = _skip_string(t, i)
        key = json.loads(t[ks:ke])
        i = _skip_ws(t, ke)
        if i >= n or t[i] != ":":
            raise SurgeryError("key %r 後面缺冒號" % key)
        vs = _skip_ws(t, i + 1)
        ve = _skip_value(t, vs)
        yield Member(key, ks, ke, vs, ve)
        i = ve


def root_start(t):
    i = _skip_ws(t, 0)
    if i >= len(t) or t[i] != "{":
        raise SurgeryError("這支工具只處理最外層是物件 '{' 的 JSON；"
                           "最外層是陣列的話請改用 Edit 工具手動處理。")
    return i


def resolve_scope(t, scope, sep="."):
    """把 'a.b.c' 這種路徑解析成該物件 '{' 的位置。"""
    start = root_start(t)
    if not scope:
        return start
    for part in scope.split(sep):
        found = None
        for m in iter_members(t, start):
            if m.key == part:
                found = m
                break
        if found is None:
            raise SurgeryError("scope 路徑找不到 %r（在 %r 底下）" % (part, scope))
        vs = _skip_ws(t, found.value_start)
        if t[vs] != "{":
            raise SurgeryError("scope 的 %r 不是物件，無法再往下走" % part)
        start = vs
    return start


def find_member(t, key, scope=None, sep="."):
    obj = resolve_scope(t, scope, sep)
    hits = [m for m in iter_members(t, obj) if m.key == key]
    if not hits:
        where = scope or "（最外層）"
        raise SurgeryError("在 %s 底下找不到 key %r" % (where, key))
    if len(hits) > 1:
        raise SurgeryError("key %r 在同一層出現 %d 次（重複 key）。"
                           "請縮小 --scope 或改用 Edit 工具手動處理。" % (key, len(hits)))
    return hits[0]


# --------------------------------------------------------------------------
# 行 / 縮排 / 空行 輔助
# --------------------------------------------------------------------------

def line_start(t, i):
    return t.rfind("\n", 0, i) + 1


def line_end(t, i):
    """回傳含換行字元在內的行尾位置。"""
    j = t.find("\n", i)
    return len(t) if j == -1 else j + 1


def lineno(t, i):
    return t.count("\n", 0, i) + 1


def indent_of(t, i):
    ls = line_start(t, i)
    return t[ls:i] if t[ls:i].strip() == "" else ""


def trailing_comma(t, value_end):
    """回傳 (有沒有逗號, 逗號之後的位置)。"""
    j = _skip_ws(t, value_end)
    if j < len(t) and t[j] == ",":
        return True, j + 1
    return False, value_end


def member_block(t, m, with_comma=False):
    """取出這個 member 的原文；若它自成整行則以整行為單位。"""
    has_comma, cut = trailing_comma(t, m.value_end)
    end = cut if with_comma else m.value_end
    ls = line_start(t, m.key_start)
    if t[ls:m.key_start].strip() == "":
        le = line_end(t, cut - 1 if has_comma else m.value_end)
        if t[(cut if has_comma else m.value_end):le].strip() == "":
            return t[ls:le].rstrip("\n")
    return t[m.key_start:end]


def blank_lines_between(t, a_end, b_start):
    seg = t[a_end:b_start]
    return max(0, seg.count("\n") - 1)


def neighbour_gap(t, obj_start, m):
    """推測這份檔案裡「同層區塊之間」慣用幾行空行。"""
    members = list(iter_members(t, obj_start))
    idx = next((k for k, x in enumerate(members) if x.key_start == m.key_start), None)
    if idx is None:
        return 0
    if idx + 1 < len(members):
        _, cut = trailing_comma(t, m.value_end)
        return blank_lines_between(t, cut, line_start(t, members[idx + 1].key_start))
    if idx > 0:
        prev = members[idx - 1]
        _, cut = trailing_comma(t, prev.value_end)
        return blank_lines_between(t, cut, line_start(t, m.key_start))
    return 0


def reindent(block, target):
    """把 block 的基準縮排平移到 target，內部相對縮排不動。"""
    lines = block.split("\n")
    body = [ln for ln in lines if ln.strip()]
    if not body:
        return block
    base = min(len(ln) - len(ln.lstrip()) for ln in body)
    out = []
    for ln in lines:
        if not ln.strip():
            out.append("")
        else:
            out.append(target + ln[base:])
    return "\n".join(out)


def set_block_comma(block, want):
    stripped = block.rstrip()
    tail = block[len(stripped):]
    if stripped.endswith(","):
        stripped = stripped[:-1]
    if want:
        stripped += ","
    return stripped + tail


# --------------------------------------------------------------------------
# 操作
# --------------------------------------------------------------------------

def op_remove(t, key, scope=None, sep=".", absorb_blanks=True):
    m = find_member(t, key, scope, sep)
    has_comma, cut = trailing_comma(t, m.value_end)
    ls = line_start(t, m.key_start)
    own_head = t[ls:m.key_start].strip() == ""
    le = line_end(t, cut - 1 if has_comma else m.value_end)
    own_tail = t[cut:le].strip() == ""

    if own_head and own_tail:
        a, b = ls, le
        if absorb_blanks and has_comma:
            # 後面還有成員：分隔用的空行跟著這個區塊一起走
            while b < len(t):
                nb = line_end(t, b)
                if nb <= b or t[b:nb].strip() != "":
                    break
                b = nb
        elif absorb_blanks:
            # 它是最後一個成員：分隔空行在它「前面」，否則會留下孤兒空行
            while a > 0:
                pa = line_start(t, a - 1)
                if t[pa:a].strip() != "":
                    break
                a = pa
    else:
        a, b = m.key_start, cut

    new = t[:a] + t[b:]
    if not has_comma:
        # 它本來是最後一個成員；前一個成員的逗號現在變成多餘的尾逗號
        k = a - 1
        while k >= 0 and new[k] in WS:
            k -= 1
        if k >= 0 and new[k] == ",":
            new = new[:k] + new[k + 1:]
    return new


def op_insert(t, block, anchor, before=False, scope=None, sep=".", do_reindent=True):
    obj = resolve_scope(t, scope, sep)
    m = find_member(t, anchor, scope, sep)
    ind = indent_of(t, m.key_start)
    gap = neighbour_gap(t, obj, m)
    body = reindent(block.rstrip("\n"), ind) if do_reindent else block.rstrip("\n")
    has_comma, cut = trailing_comma(t, m.value_end)

    if before:
        ins_at = line_start(t, m.key_start)
        body = set_block_comma(body, True)
        return t[:ins_at] + body + "\n" + "\n" * gap + t[ins_at:]

    anchor_le = line_end(t, cut - 1 if has_comma else m.value_end)
    if has_comma:
        body = set_block_comma(body, True)
        return t[:anchor_le] + "\n" * gap + body + "\n" + t[anchor_le:]
    # anchor 是最後一個成員：幫它補逗號，新區塊不帶逗號
    body = set_block_comma(body, False)
    return (t[:m.value_end] + "," + t[m.value_end:anchor_le]
            + "\n" * gap + body + "\n" + t[anchor_le:])


def op_append(t, block, scope=None, sep=".", do_reindent=True):
    """插到該層的最後一個成員後面（空物件也可以）。"""
    obj = resolve_scope(t, scope, sep)
    members = list(iter_members(t, obj))
    if members:
        return op_insert(t, block, members[-1].key, before=False, scope=scope,
                         sep=sep, do_reindent=do_reindent)
    close = t.index("}", obj)
    ind = indent_of(t, obj) + "    "
    body = reindent(block.rstrip("\n"), ind) if do_reindent else block.rstrip("\n")
    body = set_block_comma(body, False)
    return t[:obj + 1] + "\n" + body + "\n" + indent_of(t, obj) + t[close:]


def op_clone(t, key, new_key, scope=None, sep=".", after=None):
    m = find_member(t, key, scope, sep)
    block = member_block(t, m, with_comma=False)
    old_q = json.dumps(m.key, ensure_ascii=False)
    new_q = json.dumps(new_key, ensure_ascii=False)
    pos = block.find(old_q)
    if pos == -1:  # key 用了逸出寫法，退而求其次以行首的引號為準
        raise SurgeryError("在區塊裡找不到原 key 的字面 %s，請改用 insert" % old_q)
    block = block[:pos] + new_q + block[pos + len(old_q):]
    return op_insert(t, block, after or key, before=False, scope=scope, sep=sep,
                     do_reindent=False)


def op_set_value(t, key, value_text, scope=None, sep="."):
    m = find_member(t, key, scope, sep)
    return t[:m.value_start] + value_text + t[m.value_end:]


def op_replace(t, key, block, scope=None, sep=".", do_reindent=True):
    m = find_member(t, key, scope, sep)
    ind = indent_of(t, m.key_start)
    has_comma, _ = trailing_comma(t, m.value_end)
    body = reindent(block.rstrip("\n"), ind) if do_reindent else block.rstrip("\n")
    body = set_block_comma(body, has_comma)
    ls = line_start(t, m.key_start)
    if t[ls:m.key_start].strip() == "":
        le = line_end(t, m.value_end)
        return t[:ls] + body + "\n" + t[le:]
    end = m.value_end + (1 if has_comma else 0)
    return t[:m.key_start] + body + t[end:]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def read_text(path):
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def read_block(args):
    if args.block_file:
        return read_text(args.block_file)
    if args.block is not None:
        return args.block
    data = sys.stdin.read()
    if not data.strip():
        raise SurgeryError("沒有給區塊內容（--block / --block-file / stdin 擇一）")
    return data


def emit(args, original, new):
    if args.validate:
        try:
            json.loads(new)
        except ValueError as e:
            raise SurgeryError("改完之後 JSON 不合法：%s\n（沒有寫檔）" % e)
    if args.in_place:
        with io.open(args.file, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        before, after = original.count("\n"), new.count("\n")
        sys.stderr.write("已寫入 %s（行數 %d -> %d）\n" % (args.file, before, after))
    else:
        sys.stdout.write(new)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("op", choices=["keys", "show", "range", "remove", "insert",
                                  "append", "clone", "replace", "set"])
    p.add_argument("file")
    p.add_argument("--key")
    p.add_argument("--scope", help="限定在這個路徑底下，例如 feature_setting.old_item")
    p.add_argument("--sep", default=".", help="scope 的分隔字元（預設 .）")
    p.add_argument("--after", help="insert/clone：插在這個 key 後面")
    p.add_argument("--before", help="insert：插在這個 key 前面")
    p.add_argument("--to", help="clone：新的 key 名稱")
    p.add_argument("--value", help="set：新的 value 原文（自己帶引號）")
    p.add_argument("--block", help="insert/replace：區塊原文")
    p.add_argument("--block-file", help="insert/replace：從檔案讀區塊原文")
    p.add_argument("--no-reindent", dest="reindent", action="store_false",
                   help="不要把區塊縮排對齊到 anchor")
    p.add_argument("--no-absorb-blanks", dest="absorb_blanks", action="store_false",
                   help="remove：不要一併吃掉後面的空行")
    p.add_argument("--no-validate", dest="validate", action="store_false",
                   help="不要在輸出前檢查 JSON 合法性（檔案有註解時需要）")
    p.add_argument("-i", "--in-place", action="store_true")
    args = p.parse_args(argv)

    t = read_text(args.file)

    if args.op == "keys":
        obj = resolve_scope(t, args.scope, args.sep)
        for m in iter_members(t, obj):
            sys.stdout.write("%6d  %s\n" % (lineno(t, m.key_start), m.key))
        return 0

    if args.op in ("show", "range"):
        if not args.key:
            raise SurgeryError("%s 需要 --key" % args.op)
        m = find_member(t, args.key, args.scope, args.sep)
        if args.op == "range":
            sys.stdout.write("%d-%d\n" % (lineno(t, m.key_start), lineno(t, m.value_end)))
        else:
            sys.stdout.write(member_block(t, m, with_comma=True) + "\n")
        return 0

    if args.op == "remove":
        new = op_remove(t, args.key, args.scope, args.sep, args.absorb_blanks)
    elif args.op == "insert":
        anchor = args.after or args.before
        if not anchor:
            raise SurgeryError("insert 需要 --after 或 --before")
        new = op_insert(t, read_block(args), anchor, before=bool(args.before),
                        scope=args.scope, sep=args.sep, do_reindent=args.reindent)
    elif args.op == "append":
        new = op_append(t, read_block(args), args.scope, args.sep, args.reindent)
    elif args.op == "clone":
        if not (args.key and args.to):
            raise SurgeryError("clone 需要 --key 與 --to")
        new = op_clone(t, args.key, args.to, args.scope, args.sep, args.after)
    elif args.op == "replace":
        new = op_replace(t, args.key, read_block(args), args.scope, args.sep,
                         args.reindent)
    elif args.op == "set":
        if args.value is None:
            raise SurgeryError("set 需要 --value")
        new = op_set_value(t, args.key, args.value, args.scope, args.sep)
    else:
        raise SurgeryError("未知操作 %s" % args.op)

    emit(args, t, new)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SurgeryError as exc:
        sys.stderr.write("json_surgery: %s\n" % exc)
        sys.exit(2)
