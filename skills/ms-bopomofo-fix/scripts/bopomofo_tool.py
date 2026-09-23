#!/usr/bin/env python3
"""微軟注音(Microsoft Bopomofo)學習詞工具 — 讀、找可疑、同音替換、刪除、備份/還原。

只用標準函式庫。Windows 原生(py -3)或 WSL(python3)都能跑。
所有寫入都是：先備份 → 產生暫存檔 → 一次系統管理員提權(停 ctfmon → 確認檔案沒被動過 → 換檔 → 啟動 ctfmon)。

  info                      環境、資料夾、檔案格式檢查、學習設定
  dump [--grep S] [--sort count] [--limit N]
  udp                       使用者自建詞
  suspects                  同一串注音學到不同寫法的詞(可能有錯字或異體混用)
  plan  --fix 錯=正 ... --delete 詞 ... --delete-index N ... --dedupe   試跑，不寫入
  apply (同上)              備份後寫入(會跳 UAC)
  backup | restore <備份資料夾> | restart-ctfmon
  共用: --json  --dir <IMETC 資料夾>  --backup-root <資料夾>
"""
import argparse, datetime, hashlib, json, os, re, shutil, struct, subprocess, sys, time

# ---------- 環境 ----------
IS_WIN = os.name == 'nt'
IS_WSL = (not IS_WIN) and ('microsoft' in open('/proc/version').read().lower() if os.path.exists('/proc/version') else False)


def win_env(var):
    if IS_WIN:
        return os.environ[var]
    out = subprocess.run(['cmd.exe', '/c', 'echo %' + var + '%'], cwd='/mnt/c', capture_output=True).stdout
    return out.decode(errors='replace').strip()


def to_local(winpath):
    return winpath if IS_WIN else subprocess.run(['wslpath', '-u', winpath], capture_output=True, text=True).stdout.strip()


def to_win(path):
    return path if IS_WIN else subprocess.run(['wslpath', '-w', path], capture_output=True, text=True).stdout.strip()


def default_dir():
    return os.path.join(to_local(win_env('APPDATA')), 'Microsoft', 'IME', '15.0', 'IMETC')


def default_backup_root():
    return os.path.join(to_local(win_env('USERPROFILE')), 'IME_backup')


def run_win(args):
    """跑 Windows 指令並回傳文字(處理 cp950 / WSL)。"""
    kw = dict(capture_output=True)
    if IS_WSL:
        kw['cwd'] = '/mnt/c'
    r = subprocess.run(args, **kw)
    for enc in ('utf-8', 'cp950', 'mbcs' if IS_WIN else 'latin-1'):
        try:
            return r.returncode, r.stdout.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return r.returncode, r.stdout.decode('latin-1')


# ---------- 注音碼 ----------
CONS = ' ㄅㄆㄇㄈㄉㄊㄋㄌㄍㄎㄏㄐㄑㄒㄓㄔㄕㄖㄗㄘㄙ'
MED = ' ㄧㄨㄩ'
RHY = ' ㄚㄛㄜㄝㄞㄟㄠㄡㄢㄣㄤㄥㄦ'
TONE = ['?', '', 'ˊ', 'ˇ', 'ˋ', '˙', '?', '?']


def bpmf(c):
    return (CONS[c >> 11] + MED[(c >> 9) & 3] + RHY[(c >> 5) & 15] + TONE[(c >> 2) & 7]).replace(' ', '')


# ---------- TCHFTL.DAT ----------
MAGIC = bytes.fromhex('131010204c54654d')
HDR, RSZ, UI_LIMIT = 0x40, 0x44, 2500   # 官方造詞工具只顯示前 2500 筆


class FormatError(Exception):
    pass


class Hftl:
    """長期學習詞檔。header: 0x08 檔案大小 / 0x0c 筆數 / 0x10 每筆大小 / 0x14 header 大小 /
    0x18 紀錄區結尾 / 0x20 修改次數 / 0x28 最後修改 unix time。
    每筆 0x44: 文字 UTF-16 20B | 注音數 u32 | 注音 9×u32 | 字數 u32 | 被選次數 u32。"""

    def __init__(self, path):
        self.path = path
        self.raw = bytearray(open(path, 'rb').read())
        b = self.raw
        if b[:8] != MAGIC:
            raise FormatError('檔頭不符，不是已知版本的 TCHFTL.DAT')
        size, n, rs, hs, end = struct.unpack_from('<IIIII', b, 0x08)
        if size != len(b) or rs != RSZ or hs != HDR or (end - HDR) % RSZ:
            raise FormatError('header 欄位不符(size=%d/%d rs=%#x hs=%#x)' % (size, len(b), rs, hs))
        self.capacity = (end - HDR) // RSZ
        if n > self.capacity:
            raise FormatError('筆數超過容量')
        self.recs = []
        for i in range(n):
            o = HDR + i * RSZ
            text = b[o:o + 20].decode('utf-16-le').split('\0')[0]
            nc = struct.unpack_from('<I', b, o + 20)[0]
            ln, cnt = struct.unpack_from('<II', b, o + 60)
            if not (1 <= nc <= 9 and nc == ln):
                raise FormatError('第 %d 筆格式不符(%r nc=%d len=%d)' % (i, text, nc, ln))
            codes = struct.unpack_from('<9I', b, o + 24)[:nc]
            self.recs.append(dict(i=i, text=text, count=cnt, codes=list(codes),
                                  bpmf=' '.join(bpmf(c) for c in codes), hidden_in_ui=i >= UI_LIMIT))

    def char_codes(self):
        m = {}
        for r in self.recs:
            for ch, c in zip(r['text'], r['codes']):
                m.setdefault(ch, set()).add(c)
        return m

    def build(self, fixes, deletes, delete_idx=(), dedupe=False):
        """回傳 (新檔 bytes, 變更清單)。
        fixes: [(錯, 正)] 子字串替換；若改完跟既有的同注音詞重複，就併進那筆(次數相加)並刪掉這筆。
        deletes: 要刪的完整詞；delete_idx: 要刪的筆數編號；dedupe: 同字同注音的重複紀錄併成一筆。"""
        b = bytearray(self.raw)
        cc = self.char_codes()
        out = {}
        for r in self.recs:
            new = r['text']
            for w, s in fixes:
                new = new.replace(w, s)
            out[r['i']] = new
        home = {}
        for r in self.recs:   # 沒被改到的詞當合併目標
            if out[r['i']] == r['text'] and r['text'] not in deletes and r['i'] not in delete_idx:
                home.setdefault((r['text'], tuple(r['codes'])), r['i'])
        changes, drop, add = [], set(), {}
        for r in self.recs:
            i, new = r['i'], out[r['i']]
            if r['text'] in deletes or i in delete_idx:
                changes.append(dict(op='delete', i=i, text=r['text'], count=r['count'], bpmf=r['bpmf'],
                                    hidden_in_ui=r['hidden_in_ui']))
                drop.add(i)
                continue
            if new == r['text']:
                key = (new, tuple(r['codes']))
                if dedupe and home.get(key, i) != i:
                    changes.append(dict(op='merge', i=i, text=r['text'], new=new, count=r['count'], into=home[key], chars=[]))
                    add[home[key]] = add.get(home[key], 0) + r['count']
                    drop.add(i)
                continue
            chars = [dict(old=x, new=y, bpmf=bpmf(r['codes'][k]), evidence=r['codes'][k] in cc.get(y, ()))
                     for k, (x, y) in enumerate(zip(r['text'], new)) if x != y]
            key = (new, tuple(r['codes']))
            if key in home:
                changes.append(dict(op='merge', i=i, text=r['text'], new=new, count=r['count'], into=home[key], chars=chars))
                add[home[key]] = add.get(home[key], 0) + r['count']
                drop.add(i)
            else:
                changes.append(dict(op='fix', i=i, text=r['text'], new=new, count=r['count'], chars=chars))
                home[key] = i
                enc = new.encode('utf-16-le')
                o = HDR + i * RSZ
                b[o:o + len(enc)] = enc
        for i, n in add.items():
            o = HDR + i * RSZ + 64
            struct.pack_into('<I', b, o, struct.unpack_from('<I', b, o)[0] + n)
        if drop:   # 刪除：保留原順序往前補、尾端清零
            keep = [r['i'] for r in self.recs if r['i'] not in drop]
            body = b''.join(bytes(b[HDR + i * RSZ:HDR + (i + 1) * RSZ]) for i in keep)
            b[HDR:HDR + len(self.recs) * RSZ] = body + bytes(len(self.recs) * RSZ - len(body))
            struct.pack_into('<I', b, 0x0c, len(keep))
        if changes:
            struct.pack_into('<I', b, 0x20, struct.unpack_from('<I', b, 0x20)[0] + 1)
            struct.pack_into('<I', b, 0x28, int(time.time()))
        return bytes(b), changes


def read_udp(path):
    """TCEUDP.UPT: 0x1c = 筆數，紀錄從 0x80 開始，每筆開頭 u16 = 該筆長度，文字在後半段。"""
    b = open(path, 'rb').read()
    n = struct.unpack_from('<I', b, 0x1c)[0]
    o, out = 0x80, []
    while len(out) < n and o < len(b):
        L = struct.unpack_from('<H', b, o)[0]
        if L == 0:
            break
        s = b[o:o + L].decode('utf-16-le', errors='ignore')
        out.append(''.join(re.findall(r'[㐀-鿿\U00020000-\U0002ffff]+', s[len(s) // 2:])))
        o += L
    return out


# ---------- 提權換檔 ----------
SWAP_PS1 = r'''param([string]$Manifest, [string]$Result)
$ErrorActionPreference = 'Stop'
$lines = @()
try {
  Stop-Process -Name ctfmon -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 700
  $items = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($it in $items) {
    if ($it.expect -ne '*') {
      $h = (Get-FileHash -LiteralPath $it.target -Algorithm SHA256).Hash
      if ($h -ne $it.expect) { $lines += "CHANGED $($it.target)"; continue }
    }
    Copy-Item -LiteralPath $it.staged -Destination $it.target -Force
    $lines += "OK $($it.target)"
  }
} catch { $lines += "ERROR $($_.Exception.Message)" }
finally {
  Start-Process "$env:SystemRoot\System32\ctfmon.exe"
  Set-Content -LiteralPath $Result -Value $lines -Encoding UTF8
}
'''


def sha256(path):
    return hashlib.sha256(open(path, 'rb').read()).hexdigest().upper()


def elevated_swap(pairs, stage):
    """pairs: [(暫存檔, 目標檔, 預期目前 hash 或 '*')]。回傳每行結果。"""
    ps1, man, res = (os.path.join(stage, f) for f in ('swap.ps1', 'manifest.json', 'result.txt'))
    open(ps1, 'w', encoding='utf-8-sig').write(SWAP_PS1)
    json.dump([dict(staged=to_win(s), target=to_win(t), expect=e) for s, t, e in pairs],
              open(man, 'w', encoding='utf-8'), ensure_ascii=False)
    arg = "-NoProfile -ExecutionPolicy Bypass -File \"%s\" -Manifest \"%s\" -Result \"%s\"" % (to_win(ps1), to_win(man), to_win(res))
    cmd = "Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '%s'" % arg.replace("'", "''")
    code, out = run_win(['powershell.exe', '-NoProfile', '-Command', cmd])
    if not os.path.exists(res):
        return ['ERROR 沒有取得系統管理員權限(UAC 被取消？) ' + out.strip()]
    return [l.strip().lstrip('﻿') for l in open(res, encoding='utf-8-sig') if l.strip()]


def ctfmon_pid():
    code, out = run_win(['tasklist.exe', '/fi', 'imagename eq ctfmon.exe', '/fo', 'csv', '/nh'])
    m = re.search(r'"ctfmon\.exe","(\d+)"', out)
    return int(m.group(1)) if m else None


# ---------- 指令 ----------
def emit(a, obj, text_fn):
    if a.json:
        print(json.dumps(obj, ensure_ascii=False, indent=1))
    else:
        text_fn(obj)


def load(a):
    return Hftl(os.path.join(a.dir, 'TCHFTL.DAT'))


def backup(a):
    dst = os.path.join(a.backup_root, datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copytree(a.dir, dst)
    return dst


def cmd_info(a):
    info = dict(env='windows' if IS_WIN else ('wsl' if IS_WSL else 'other'), dir=a.dir, backup_root=a.backup_root)
    try:
        h = load(a)
        info.update(format_ok=True, records=len(h.recs), capacity=h.capacity, hidden_in_ui=max(0, len(h.recs) - UI_LIMIT),
                    edits=struct.unpack_from('<I', h.raw, 0x20)[0],
                    last_modified=datetime.datetime.fromtimestamp(struct.unpack_from('<I', h.raw, 0x28)[0]).isoformat())
    except (FormatError, OSError) as e:
        info.update(format_ok=False, error=str(e))
    code, out = run_win(['reg.exe', 'query', r'HKCU\Software\Microsoft\IME\15.0\IMETC'])
    for k in ('Enable New Phrase Learning', 'Enable Personal Regulating', 'Enable User Defined Phrases'):
        m = re.search(re.escape(k) + r'\s+REG_\w+\s+(\S+)', out)
        info[k] = m.group(1) if m else None
    code, out = run_win(['cmd.exe', '/c', 'ver'])
    info['windows'] = out.strip()
    info['ctfmon_pid'] = ctfmon_pid()
    emit(a, info, lambda o: [print('%s: %s' % kv) for kv in o.items()])


def cmd_dump(a):
    recs = load(a).recs
    if a.grep:
        recs = [r for r in recs if a.grep in r['text']]
    if a.sort == 'count':
        recs = sorted(recs, key=lambda r: -r['count'])
    if a.limit:
        recs = recs[:a.limit]
    out = [dict(i=r['i'], count=r['count'], text=r['text'], bpmf=r['bpmf'], hidden_in_ui=r['hidden_in_ui']) for r in recs]
    emit(a, out, lambda o: [print('%d\t%d\t%s\t%s%s' % (r['i'], r['count'], r['text'], r['bpmf'],
                                                       '\t(官方工具看不到)' if r['hidden_in_ui'] else '')) for r in o])


def cmd_udp(a):
    emit(a, read_udp(os.path.join(a.dir, 'TCEUDP.UPT')), lambda o: print(' '.join(o)))


def cmd_suspects(a):
    groups = {}
    for r in load(a).recs:
        groups.setdefault(r['bpmf'], []).append(r)
    out = [dict(bpmf=k, variants=[dict(text=r['text'], count=r['count'], i=r['i']) for r in v])
           for k, v in groups.items() if len({r['text'] for r in v}) > 1]
    emit(a, out, lambda o: [print(g['bpmf'] + '\t' + '  '.join('%s(%d)' % (v['text'], v['count']) for v in g['variants'])) for g in o])


def parse_edits(a):
    fixes = []
    for f in a.fix or []:
        w, _, s = f.partition('=')
        if not s or len(w) != len(s):
            sys.exit('--fix 要寫成 錯=正，且兩邊字數相同: ' + f)
        fixes.append((w, s))
    if a.fixes:
        for ln in open(a.fixes, encoding='utf-8-sig'):
            p = ln.split('#', 1)[0].split()
            if len(p) >= 2:
                if len(p[0]) != len(p[1]):
                    sys.exit('字數不同: %s %s' % (p[0], p[1]))
                fixes.append((p[0], p[1]))
    short = [w for w, _ in fixes if len(w) < 2]
    if short and not a.allow_single:
        sys.exit('單字替換會改到所有含這個字的詞(例如 在=再)，太危險: %s。確定要的話加 --allow-single' % ' '.join(short))
    return fixes, set(a.delete or []), set(a.delete_index or []), a.dedupe


def show_plan(o):
    for c in o['changes']:
        if c['op'] == 'delete':
            print('刪  #%d %s(%d) %s%s' % (c['i'], c['text'], c['count'], c['bpmf'], '  (官方工具看不到)' if c['hidden_in_ui'] else ''))
            continue
        ch = ' '.join('%s→%s[%s]%s' % (x['old'], x['new'], x['bpmf'], '' if x['evidence'] else '(無佐證，請人工確認讀音)')
                      for x in c['chars'])
        tail = '  併入 #%d' % c['into'] if c['op'] == 'merge' else ''
        if c['op'] == 'merge' and not c['chars']:
            print('併  #%d %s(%d) 併入 #%d (重複)' % (c['i'], c['text'], c['count'], c['into']))
            continue
        print('改  #%d %s(%d) → %s    %s%s' % (c['i'], c['text'], c['count'], c['new'], ch, tail))
    for w in o['not_found']:
        print('找不到:', w)
    print('共 %d 筆' % len(o['changes']))
    for k in ('backup', 'result', 'verify', 'hint'):
        if o.get(k):
            print('%s: %s' % (k, o[k]))


def cmd_plan(a, write=False):
    fixes, deletes, didx, dedupe = parse_edits(a)
    if not (fixes or deletes or didx or dedupe):
        sys.exit('沒有指定 --fix / --fixes / --delete / --delete-index / --dedupe')
    h = load(a)
    data, changes = h.build(fixes, deletes, didx, dedupe)
    out = dict(changes=changes, not_found=sorted(deletes - {r['text'] for r in h.recs}))
    if write and changes:
        out['backup'] = backup(a)
        stage = new_stage(a)
        staged = os.path.join(stage, 'TCHFTL.DAT')
        open(staged, 'wb').write(data)
        Hftl(staged)   # 寫出前再驗一次格式
        pid0 = ctfmon_pid()
        target = os.path.join(a.dir, 'TCHFTL.DAT')
        out['result'] = elevated_swap([(staged, target, sha256(h.path))], stage)
        out['verify'] = dict(written=sha256(target) == sha256(staged),
                             remaining=len(load(a).build(fixes, deletes, (), dedupe)[1]),
                             ctfmon_restarted=ctfmon_pid() not in (None, pid0))
        if any(l.startswith('CHANGED') for l in out['result']):
            out['hint'] = '準備期間學習檔被輸入法改過，已放棄寫入；重跑 apply 即可'
        elif out['verify']['written'] and not out['verify']['remaining']:
            shutil.rmtree(stage, ignore_errors=True)
    emit(a, out, show_plan)


def new_stage(a):
    stage = os.path.join(a.backup_root, 'staging', datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    os.makedirs(stage)
    return stage


def cmd_backup(a):
    emit(a, dict(backup=backup(a)), lambda o: print('備份:', o['backup']))


def cmd_restore(a):
    src = os.path.abspath(a.src)
    if not os.path.isfile(os.path.join(src, 'TCHFTL.DAT')):
        sys.exit('不是備份資料夾: ' + src)
    Hftl(os.path.join(src, 'TCHFTL.DAT'))
    bk = backup(a)
    stage = new_stage(a)
    for f in os.listdir(src):
        shutil.copy2(os.path.join(src, f), stage)
    pairs = [(os.path.join(stage, f), os.path.join(a.dir, f), '*') for f in os.listdir(src) if f.upper().startswith('TC')]
    res = elevated_swap(pairs, stage)
    emit(a, dict(backup=bk, result=res), lambda o: (print('備份:', o['backup']), print('\n'.join(o['result']))))


def cmd_restart(a):
    stage = new_stage(a)
    pid0 = ctfmon_pid()
    res = elevated_swap([], stage)
    emit(a, dict(result=res, before=pid0, after=ctfmon_pid()), lambda o: print(o))


def main():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, 'reconfigure'):
            s.reconfigure(encoding='utf-8' if not s.isatty() else s.encoding, errors='replace')
    p = argparse.ArgumentParser(description='微軟注音學習詞工具')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true')
    common.add_argument('--dir')
    common.add_argument('--backup-root')
    sp = p.add_subparsers(dest='cmd', required=True)
    sp.add_parser('info', parents=[common]).set_defaults(f=cmd_info)
    d = sp.add_parser('dump', parents=[common])
    d.add_argument('--grep'); d.add_argument('--sort', choices=['order', 'count'], default='order'); d.add_argument('--limit', type=int)
    d.set_defaults(f=cmd_dump)
    sp.add_parser('udp', parents=[common]).set_defaults(f=cmd_udp)
    sp.add_parser('suspects', parents=[common]).set_defaults(f=cmd_suspects)
    for name, w in (('plan', False), ('apply', True)):
        e = sp.add_parser(name, parents=[common])
        e.add_argument('--fix', action='append', help='錯=正，可重複')
        e.add_argument('--fixes', help='檔案：每行「錯 正」')
        e.add_argument('--delete', action='append', help='要刪除的完整詞，可重複')
        e.add_argument('--delete-index', action='append', type=int, help='要刪除的筆數編號(dump 第一欄)，可重複')
        e.add_argument('--dedupe', action='store_true', help='同字同注音的重複紀錄併成一筆(次數相加)')
        e.add_argument('--allow-single', action='store_true', help='允許單字替換(會套用到所有含該字的詞)')
        e.set_defaults(f=lambda a, w=w: cmd_plan(a, w))
    sp.add_parser('backup', parents=[common]).set_defaults(f=cmd_backup)
    r = sp.add_parser('restore', parents=[common]); r.add_argument('src'); r.set_defaults(f=cmd_restore)
    sp.add_parser('restart-ctfmon', parents=[common]).set_defaults(f=cmd_restart)
    a = p.parse_args()
    a.dir = a.dir or default_dir()
    a.backup_root = a.backup_root or default_backup_root()
    a.f(a)


if __name__ == '__main__':
    main()
