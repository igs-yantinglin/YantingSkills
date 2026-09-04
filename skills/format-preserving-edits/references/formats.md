# 各格式的保排版做法

通則：**parser 只用來讀與驗證，寫一律走文字層級。** 下面按格式列出「這個格式特別容易被
round-trip 弄壞的東西」和「實務上怎麼動刀」。

---

## JSON

`scripts/json_surgery.py` 已涵蓋。額外提醒：

- **陣列元素**：工具處理的是物件成員（`"key": value`）。陣列裡插一筆通常用 Edit 工具最快；
  注意逗號在哪一端（`[1, 2, 3]` 同行 vs 每行一個）。
- **最外層是陣列**的檔案（例如一批紀錄）：工具會直接拒絕，用 Edit 工具處理。
- **JSONC / JSON5**（`tsconfig.json`、VS Code 設定）：`json_surgery` 的掃描容忍 `//`
  與 `/* */`，但寫檔前的合法性檢查會失敗——加 `--no-validate`，改用肉眼加
  `verify_edit.py`（它會退回純文字檢查）確認。
- **格式化過的巨大單行 JSON**（整份檔案一行）：那本來就沒有排版可保留，直接
  `json.dumps(..., separators=(',',':'))` 也不會製造 diff 噪音——但先確認它真的是單行。

---

## YAML

最脆弱的格式。`yaml.safe_load` + `yaml.dump` 會**吃掉全部註解**、重排 key、改引號、
把 `- ` 清單縮排統一、把 `yes/no/on/off` 變成布林、把 `1.0` 變 `1.0`、把長字串折行。

做法優先序：

1. **Edit 工具**。YAML 是行導向的，多數改動（改一個值、加一個 key、加一筆 list item）
   用 Edit 精準得很。注意縮排要跟同層兄弟一致。
2. **`ruamel.yaml` round-trip 模式**（若環境有裝）——這是唯一會保留註解與引號的 YAML 函式庫：
   ```python
   from ruamel.yaml import YAML
   y = YAML()                       # 預設就是 round-trip
   y.preserve_quotes = True
   y.width = 4096                   # 不要幫我折行
   with open(p) as f: data = y.load(f)
   data['spec']['replicas'] = 3
   with open(p, 'w') as f: y.dump(data, f)
   ```
   即使如此，改完仍要跑 `verify_edit.py` 確認沒有夾帶重排。
3. `PyYAML` 只准 `safe_load` 來**驗證**改完的檔還解得開。

YAML 特有的觀察點：清單是與 key 齊平還是內縮、字串加不加引號、多份文件的 `---` 分隔、
錨點 `&`/`*`（round-trip 會展開成重複內容）。

---

## TOML

- 讀：`tomllib`（Py3.11+）或 `tomli`——**唯讀，沒有寫的能力**，這其實是好事。
- 寫：**Edit 工具**，或 `tomlkit`（保留註解與排版的 round-trip 函式庫）。
- `toml.dump` / `tomli_w` 會重排 table 順序、吃註解、改陣列的多行寫法。
- 注意點：`[tool.x]` table 的順序有語意上的閱讀價值；多行陣列的尾逗號；`"""` 字串。

`pyproject.toml`、`Cargo.toml` 幾乎都有註解，一定要走 Edit 或 tomlkit。

---

## INI / CFG / .properties / .env

- **一律用 Edit 工具。** `ConfigParser.write()` 會吃註解、把 key 小寫、統一 `=` 兩側空白、
  丟掉 section 順序。
- 先用 `sniff_format.py` 確認是 `k = v` 還是 `k=v`（它會報比數）。
- `.env` 額外注意：值要不要引號、有沒有 `export ` 前綴、行內註解 `#` 的處理各家不同。
- `.properties` 的 `\` 續行、`:` 也可當分隔字元、Unicode 逸出。

---

## XML

- `ET.tostring` 會改自閉合標籤寫法（`<a/>` vs `<a></a>`）、丟掉屬性順序（舊版）、
  丟掉 DOCTYPE 與註解；`minidom.toprettyxml` 會重排整份縮排並塞進一堆空白文字節點。
- 做法：**Edit 工具**。若真的要程式化，`lxml` 保留得比 `ElementTree` 好
  （`etree.parse` + `remove_blank_text=False`，寫回時不要 `pretty_print=True`）。
- 常見於 `pom.xml`、`.csproj`、Android `AndroidManifest.xml`、strings.xml。

---

## CSV / TSV

- `csv.writer` 會重新決定哪些欄位要加引號、統一換行字元、可能改掉 `\r\n`。
- 只加幾列 → 直接 append 文字。改某一格 → Edit 工具。
- 大量處理時用 `csv.reader` 讀、但輸出時**保留原始行**，只替換要動的那幾行。
- 注意：BOM（Excel 產的 CSV 常有）、行尾 `\r\n`、欄位裡的換行。

---

## Markdown / 程式碼 / Dockerfile / Makefile

原則相同：只動要動的行。特別注意 Makefile 的 **TAB 縮排是語法**，
不能被編輯器換成空白。

---

## 判斷「這個 repo 是不是有 formatter 在管」

有的話，跟著 formatter 跑才是正確做法。動手前檢查：

```bash
ls -a | grep -E '\.editorconfig|\.prettierrc|\.pre-commit-config|setup\.cfg|pyproject\.toml'
git log --oneline -5 -- <目標檔>      # 歷史上的 diff 是乾淨的還是整檔重排？
```

`git log -p` 看前幾筆針對這個檔的 commit，diff 形狀就是答案：
歷次都是小範圍增刪 → 沒有 formatter，照本 skill 做；
歷次都整檔重寫 → 可能有 formatter 或本來就是機器產生的檔。
