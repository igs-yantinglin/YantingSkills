---
name: readable-nested-literal-formatting
description: 把巢狀資料結構（Python dict/list 字面量、JSON、YAML 等）序列化成給人讀的原始碼時，用「先試單行、裝不下才展開（不算縮排）」+「純量陣列（含矩陣）一律收一行，不管多長」+「撞長度的欄位共用欄寬對齊逗號、連 key 本身的印出寬度也對齊」三條規則排版，取代預設的逐項強制換行（`json.dumps(indent=2)`、手寫 pretty-printer 那種每個值都自己一行的寫法）。觸發詞：「這格式太難讀」「排版太亂」「一行一個數字滑不完」「這個 array 好長」「幫我排版讓人看得懂」「pretty print」「這個 config 讀起來很痛苦」，以及任何要產生/重新產生給人 review 的巢狀資料檔（機率表、遊戲設定、轉輪帶資料等）的請求。
---

# 巢狀資料的人讀排版

## 問題

序列化巢狀資料給人讀（review、debug、diff）時，常見的預設做法是「逐項強制換行」——
每個 dict 的每個 key、每個 list 的每個元素都自己一行（`json.dumps(indent=2)`、
`pprint`、手寫的遞迴 pretty-printer 幾乎都長這樣）。這在資料淺、元素少時沒問題，
但遇到兩種情況會變得完全沒法讀：

1. **深度巢狀但每層都很小**：像 `{'children': [], 'name': 'Hit3', 'weights': []}`
   這種只有兩三個短欄位的 dict，硬拆成 5 行，人眼要跳來跳去才能拼回「這其實是一組」。
2. **扁平的純量陣列**：像機率權重表、轉輪帶原始數列，一行一個數字要滑幾百行，
   而且就算真的滑完，也看不出裡面有什麼結構可言——這種資料本來就不是「按行分組」
   的，多包幾行不會變得比較好讀，乾脆整串收一行，一眼看出「這是一大串不用細看
   的資料」就好。

## 三條規則

跟 black／prettier 的排版邏輯不完全一樣——關鍵差異在規則 1：**判斷「塞不塞得進
一行」只看內容本身的長度，不把目前縮排算進去。** 這是踩過一次坑才確定的：
黑箱式排版工具是連縮排一起算的（物理欄寬），但那樣同一個結構簡單的 leaf dict
（例如 `{'name': 'Hit3', 'weights': []}`），會因為它剛好在樹的第 8 層、縮排比較
深，就被展開成好幾行；同一種結構在不同深度排得不一致，讀者反而更難辨認「這是
同一種東西」。

1. **一行優先，裝不下才展開，但判斷基準是內容本身、不含縮排**：每個 dict/list
   先試著壓縮成一行（遞迴壓縮子節點），如果壓完的長度（不含目前縮排）在寬度
   門檻內（例如 88 字元）且不含換行，直接用那一行；裝不下才逐項展開。展開後
   每個子項一樣先試單行——只有真的裝不下的那幾層才會被拆開，不會整棵樹無差別
   拆到底，也不會因為深度不同讓同一種 leaf 結構表現不一致。代價是深層的短內容
   印出來那一行實際欄寬可能超過門檻、跑到螢幕外——但只有內容本身真的短才會
   發生，本來就複雜的 dict 不受影響。
2. **純量清單（list 裡全是數字/字串，沒有巢狀 dict/list）一律收一行，不管多長**：
   不做「填充式換行」也不做行數門檻判斷——這種資料本來就沒有「每行放幾個才好讀」
   的意義，硬要拆成好幾行反而只是多滑幾頁去找下一個欄位，一律收一行最單純。
   （這條規則本身沒有跟規則 1 衝突：規則 1 對 dict/list 都成立，這條只是把
   「純量清單裝不下也硬展開」的分支直接拿掉，改成永遠回傳壓縮後的單行。）

## 實作參考（Python 字面量版本，改自
`/mnt/d/slot-odds-manager/backend/app/vcs/repo.py::_py_literal`）

```python
_WIDTH = 88

def _compact(value):
    """永遠壓成一行，用來判斷一個 dict/list 擺不擺得進一行寬度。"""
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = ", ".join(f"{_compact(str(k))}: {_compact(v)}" for k, v in sorted(value.items()))
        return "{" + items + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return "[" + ", ".join(_compact(v) for v in value) + "]"
    if isinstance(value, bool):
        return "True" if value else "False"
    if value is None:
        return "None"
    return repr(value)  # str / int / float


def literal(value, level=0):
    pad = "  " * level
    compact = _compact(value)
    # 只看內容本身壓縮後多長，不把目前縮排算進門檻——同一個 leaf dict
    # 不該因為它剛好在樹的第 8 層、縮排比較深，就被展開成好幾行；那樣
    # 同一種結構在不同深度會排得不一致。代價是深層的短內容印出來那一行
    # 實際欄寬可能超過 _WIDTH，但只有內容本身真的短才會發生。
    if "\n" not in compact and len(compact) <= _WIDTH:
        return compact

    inner = "  " * (level + 1)
    if isinstance(value, dict):
        lines = ",\n".join(f"{inner}{literal(str(k))}: {literal(v, level + 1)}" for k, v in sorted(value.items()))
        return "{\n" + lines + ",\n" + pad + "}"

    if isinstance(value, list):
        if all(not isinstance(v, (dict, list)) for v in value):
            return compact  # 純量清單一律收一行，不管多長
        body = ",\n".join(f"{inner}{literal(v, level + 1)}" for v in value)
        return "[\n" + body + ",\n" + pad + "]"
```

## 第三條規則：撞長度的欄位要對齊（含矩陣）

同一個 dict 裡如果有兩個以上的純量陣列長度相同（最常見的例子：`Result`／
`Weights`，index i 的 Result 對應 index i 的 Weight；或 `Multiple` 搭配矩陣式
`Weights: [[...], [...]]`，矩陣每一列都對應同一組 Multiple index），**不能各自
獨立套用規則 1～2**——那樣兩邊各自壓縮，數字寬度不一樣，逗號對不齊，讀者看不出
「這一格對應那一格」。要做的事：

1. 找出同一個 dict 裡「長度相同」的純量陣列群組（群組要 ≥2 個陣列才有對齊的
   意義，單一陣列沒有「對齊誰」的問題）。**矩陣（list of 等長純量清單）也要算
   進來**——矩陣本身不是純量清單會被規則 2 的偵測跳過，但它的每一列都是純量
   清單，長度跟其他欄位撞了就該一起對齊；矩陣自己的列彼此之間，就算沒有其他
   扁平欄位陪同，也該互相對齊。
2. 這個群組**共用同一個欄寬**：取群組內所有陣列（含矩陣的每一列）、所有元素裡
   壓縮後最長的字串長度當作 `field_width`，每個元素都 `rjust(field_width)`
   （數字/字串都靠右對齊，讓逗號自然對在同一欄）。
3. 因為規則 2 已經讓純量陣列一律收一行，「對齊」在這裡單純是「同一組陣列共用
   一個 field_width」——不需要再額外同步換行點或行數，一行本來就只有一行。

```python
def is_scalar_list(v):
    return isinstance(v, list) and bool(v) and all(not isinstance(x, (dict, list)) for x in v)


def scalar_arrays_by_length(d):
    """長度 -> 該長度下所有可對齊陣列 (key, row_or_-1, items)，只留 >=2 個的。"""
    pool = {}
    for k, v in d.items():
        if is_scalar_list(v):
            pool.setdefault(len(v), []).append((k, -1, v))
        elif isinstance(v, list) and v and all(is_scalar_list(row) for row in v):
            lengths = {len(row) for row in v}
            if len(lengths) == 1:
                n = lengths.pop()
                for i, row in enumerate(v):
                    pool.setdefault(n, []).append((k, i, row))
    return {n: entries for n, entries in pool.items() if len(entries) >= 2}


def render_aligned(items, field_width):
    return "[" + ", ".join(_compact(x).rjust(field_width) for x in items) + "]"


def render_aligned_matrix(rows, field_width):
    # 矩陣跟純量清單一樣（規則 2）不逐列強制換行，整個收一行；
    # 列數再多，拆成一列一行也不會比較好讀，只是多滑幾頁。
    return "[" + ", ".join(render_aligned(row, field_width) for row in rows) + "]"
```

呼叫端（dict 展開時）：先算好每個群組的 `field_width`，扁平欄位用
`render_aligned(value[key], field_width)`、矩陣欄位用
`render_aligned_matrix(value[key], field_width)` 取代原本的
`literal(value[key], level+1)`，其他不在任何群組裡的欄位照原本規則走。

這條規則是「長度相同」這個**純結構訊號**觸發的，不用去猜欄位名稱是不是真的有
語意關聯——就算兩個同長度陣列剛好無關，對齊了也不會錯，只是看起來多此一舉；
比起漏掉真正有對應關係的欄位，這個誤判成本低得多。

**key 本身的印出寬度也要對齊**：光是陣列內部元素對齊還不夠——如果同一組裡
有 `'result'`（8 字元）跟 `'weight_2500'`（13 字元）這種長短差很多的 key，
`[` 出現的欄位會因為 key 名稱長短不一而錯開，組內每個陣列看起來還是對不上。
要在組裡再取一次 `key_width = max(len(repr(k)) for k in 這組的 key)`，
組員的 key 都 `ljust(key_width)`（不在任何組裡的 key 不用管）。

```python
key_width = max(len(_compact(k)) for k in group_keys)
line = f"{repr(k).ljust(key_width)}: {rendered_value}"
```

## 使用時要注意

- **先確認格式是不是純展示用**。如果輸出的檔案會被其他程式用 parser 讀回去
  （`ast.literal_eval`、`json.loads`……），排版怎麼改都不影響語意——但下手前還是
  要驗證：改格式前後都 parse 一次，斷言兩邊相等，再寫檔。不要只憑肉眼覺得「應該
  一樣」。
- **如果檔案有既有排版慣例**（既有 repo、維護者手寫的設定檔），這個 skill不適用，
  改用 [[format-preserving-edits]]：那邊管的是「不要動到不該動的排版」，這個
  skill 管的是「怎麼產生新的、給人讀的排版」，兩者目標不同、不要混用。
- 寬度門檻（88）是經驗值，不是鐵律——如果資料形狀差很多，依實際 review 起來
  舒不舒服調整即可。
- 這套邏輯不限 Python 字面量，JSON／YAML 手寫 serializer 都適用同一套規則，只是
  `_compact`／`literal` 裡的語法要換成對應格式的寫法。
