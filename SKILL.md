---
name: codex-history-unify
description: "Restore and unify local Codex session history after switching between Codex accounts or providers (DeepSeek vs OpenAI/Codex account). Use when conversations disappear from the sidebar after switching, local history is hidden per model_provider, or the user asks to refresh/show/restore old dialogs after switching in Codex or CC Switch. 用于切换 DeepSeek 与 Codex 账号后恢复侧边栏消失的本地会话。"
---

# Codex History Unify

## Overview

Unify local Codex session history to the currently selected provider so that
conversations created under another account (for example `openai` vs `custom`
DeepSeek) reappear in the sidebar. The underlying data is never deleted; this
skill relabels provider metadata and keeps baseline backups.

## Workflow

1. Read `~/.codex/config.toml` to determine the current `model_provider`.
2. If the user uses CC Switch, prefer its built-in unified history:
   run `scripts/enable_ccswitch_unify.py`, then have the user switch providers
   once through CC Switch so its migration runs.
3. Otherwise, or as a fallback, run the standalone sync:
   - Dry-run first: `python3 scripts/sync_provider.py --dry-run`
   - Confirm with the user that local databases and rollout files may be
     relabeled, then run: `python3 scripts/sync_provider.py --apply`
4. Verify with another dry run; it should report zero pending threads.
5. If the sidebar still does not refresh, ask the user to restart Codex or
   toggle the sidebar view. Do not touch `archived` state or delete files.

## Windows

The same workflow works on Windows because Codex Desktop uses the same
provider filtering and local file layout there. Install by cloning
`https://github.com/aiden23333/codex-history-unify` and copying the folder to
`%USERPROFILE%\.codex\skills\codex-history-unify` (or
`%CODEX_HOME%\skills\codex-history-unify` when CODEX_HOME is set).

Python 3.11 or newer is required for the built-in `tomllib` parser. Run the
scripts with `python` instead of `python3`:

```powershell
python scripts\sync_provider.py --dry-run
python scripts\sync_provider.py --apply
```

`scripts/enable_ccswitch_unify.py` searches both `~/.cc-switch/settings.json`
and the `%APPDATA%` locations used by Windows builds of CC Switch. If the file
lives somewhere else, pass it explicitly with `--settings`.

## Rollback

Restore the oldest baseline backup with:

```bash
python3 scripts/sync_provider.py --restore
```

Restoring also requires a Codex restart to take effect.

## Safety rules

- Always run `--dry-run` first and get explicit user approval before `--apply`.
- Do not modify rollout files for threads with an active writer lock; the
  script skips those automatically.
- Keep backups in `~/.codex/skill-backups/unify-codex-history/`.
- Do not claim data is lost; it is only hidden by provider filtering.

See [mechanism.md](references/mechanism.md) for the full data layout and why
both the state database and rollout `session_meta` must be updated.
