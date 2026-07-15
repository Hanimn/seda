## Agent skills

### Issue tracker

Issues and specs live as GitHub issues on github.com, managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, each label string equal to its name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Project board

Issues are on a GitHub Project (Kanban + roadmap). When `/implement` **starts** a ticket, set its project card to **In Progress** before doing the work. At the **end**, before closing: tick the acceptance-criteria checkboxes you verified in the issue body, then close the issue by hand (which lets the "Item closed" workflow move the card to **Done** — never set Done by hand). Do **not** put `Closes #N` in the commit, or the push would auto-close the issue before the boxes are ticked. See `docs/agents/project-board.md`.
