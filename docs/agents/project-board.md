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

## What `/implement` must do

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

## Completion is automatic — do NOT set Done by hand

The project's **"Item closed" workflow is enabled**: closing the issue auto-sets Status to `Done`. So `/implement` should **close the issue** at the end (per the normal flow) and let the workflow move the card. Do not `item-edit` Status to `Done` directly — the **"Auto-close issue"** workflow also being on means a manual Done write is redundant and can double-fire.

## If the board write fails

The board is a convenience, not a gate. If any `gh project` call errors (scope missing, project renamed, offline), **log it and continue** — never block the actual implementation work on a board sync.
