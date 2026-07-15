# Project board sync

This repo's issues are tracked on a GitHub **Project** (Projects v2) that gives a Kanban board and a roadmap view.

- **Project:** Local Flow — Roadmap — <https://github.com/users/Hanimn/projects/1>
- **Owner:** `Hanimn` · **Project number:** `1`
- **Host:** public github.com (run all `gh` commands with `GH_HOST=github.com`)

## Status field

| Purpose | Value |
| --- | --- |
| Project node id | `PVT_kwHOAOSEFs4Bdfm_` |
| Status field id | `PVTSSF_lAHOAOSEFs4Bdfm_zhYAwvs` |
| Status option `Todo` | `f75ad846` |
| Status option `In Progress` | `47fc9ee4` |
| Status option `Done` | `98236657` |

## Start of a ticket — move the card to In Progress

When `/implement` **starts work on a ticket** whose issue is on this project, move its card to **In Progress** as the first board write:

```sh
# 1. Resolve the project item id for issue #<N> (content number → item id):
ITEM_ID=$(GH_HOST=github.com gh project item-list 1 --owner Hanimn --format json \
  --jq '.items[] | select(.content.number == <N>) | .id')

# 2. Set Status = In Progress:
GH_HOST=github.com gh project item-edit \
  --project-id PVT_kwHOAOSEFs4Bdfm_ \
  --id "$ITEM_ID" \
  --field-id PVTSSF_lAHOAOSEFs4Bdfm_zhYAwvs \
  --single-select-option-id 47fc9ee4
```

## Completion — tick the boxes, then close by hand

At the **end** of a ticket, before closing, do these in order:

1. **Tick the acceptance criteria.** Edit the issue body, flipping each
   `- [ ]` you have actually verified to `- [x]`. Only tick criteria that
   genuinely passed — if one is partial or skipped, leave it unchecked and say
   so in the closing comment. Fetch the body, edit the checkboxes, write it
   back:

   ```sh
   GH_HOST=github.com gh issue view <N> --repo Hanimn/seda --json body --jq .body > /tmp/issue-<N>.md
   # edit /tmp/issue-<N>.md: change verified "- [ ]" lines to "- [x]"
   GH_HOST=github.com gh issue edit <N> --repo Hanimn/seda --body-file /tmp/issue-<N>.md
   ```

2. **Close the issue explicitly**, with a summary comment:

   ```sh
   GH_HOST=github.com gh issue close <N> --repo Hanimn/seda --comment "<summary>"
   ```

   Closing fires the project's **"Item closed" workflow**, which moves the card
   to **Done** automatically. Do **not** `item-edit` Status to `Done` by hand.

**Ordering matters.** Do **not** put `Closes #<N>` / `Fixes #<N>` in the commit
message. When such a commit lands on `main`, GitHub auto-closes the issue on
push — which would close it *before* step 1 runs, leaving the boxes unticked on
an already-closed issue. Reference the issue as a plain `#<N>` (no closing
keyword) in the commit, then tick and close by hand at the very end.

## If the board write fails

The board is a convenience, not a gate. If any `gh project` call errors (scope missing, project renamed, offline), **log it and continue** — never block the actual implementation work on a board sync.
