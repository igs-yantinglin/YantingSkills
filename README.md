# YantingSkills

Personal Claude Code skills & slash commands, for restoring quickly on a new machine.

## Restore

```bash
git clone https://github.com/igs-yantinglin/YantingSkills.git
cp -r YantingSkills/skills/* ~/.claude/skills/
cp -r YantingSkills/commands/* ~/.claude/commands/
```

## Contents

### skills/
- `caveman` — terse output mode
- `chrome-cdp-wsl` — control Windows Chrome from WSL2 via CDP
- `dashi-ppt` — generate HTML slide decks (offline-editable, export to PPTX/PDF)
- `form-ux` — internal dashboard form usability review
- `ms-bopomofo-fix` — fix Windows Microsoft Bopomofo (微軟注音) learned typos: find, replace with homophones, delete, merge
- `format-preserving-edits` — edit config files without reformatting
- `pixel-art-canvas` — pixel-art canvas animations (indexed framebuffer, integer scaling, GIF export + audit scripts)
- `readable-nested-literal-formatting` — pretty-print nested data literals for humans
- `screenshot-ready-html` — one-page no-scroll HTML reports that fit the real browser viewport for screenshots

### commands/
- `taste-skill` — anti-slop frontend design
- `frontend-design` — visual design guidance for new UI
