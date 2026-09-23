# ms-bopomofo-fix

讓 AI 幫你修正 Windows「微軟注音」自動選字亂選的問題。

微軟注音會把你打錯、沒改就送出的詞學起來（例如「事什麼」），之後跟正確的同音詞（「是什麼」）搶排序。
這個工具直接讀學習詞檔，找出學壞的詞，把它們換成同音的正確寫法、刪除或合併，並自動處理「要用系統管理員權限強制重啟 ctfmon 才會生效」這一步。

## 給 AI 用

- **Claude Code**：把整個資料夾複製到 `~/.claude/skills/ms-bopomofo-fix/`（Windows 是 `%USERPROFILE%\.claude\skills\`），然後對它說「注音一直選錯字，幫我修」。
- **其他 AI（Codex、Cursor、Gemini CLI…）**：叫它先讀 `SKILL.md`，照裡面的流程做。

## 自己用

需要 Python 3（Windows 用 `py -3`，WSL 用 `python3`），不用另外安裝套件。

```
py -3 scripts\bopomofo_tool.py info                     # 檢查環境和檔案格式
py -3 scripts\bopomofo_tool.py dump --sort count        # 列出學到的詞
py -3 scripts\bopomofo_tool.py suspects                 # 同音不同字的詞
py -3 scripts\bopomofo_tool.py plan  --fix 事什麼=是什麼 --delete <要刪的詞>
py -3 scripts\bopomofo_tool.py apply --fix 事什麼=是什麼 --delete <要刪的詞>   # 會跳 UAC
py -3 scripts\bopomofo_tool.py restore %USERPROFILE%\IME_backup\<時間戳>
```

每次 `apply` 前都會自動備份到 `%USERPROFILE%\IME_backup\<時間戳>\`。

## 限制

- 只支援微軟注音（繁中）。只在 Windows 11 24H2 驗證過，檔案格式不符時工具會拒絕寫入。
- 替換只能換成同注音的字。
- 系統內建的檢視工具（`IMCCPHR.exe /TC /PHRUI` 的「常用新詞」分頁）只顯示前 2500 筆，這個工具沒有這個限制。

格式細節見 `reference/format.md`。
