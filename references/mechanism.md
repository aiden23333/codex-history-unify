# Why conversations disappear and how this skill restores them

## Root cause

Codex Desktop keeps one local thread store under `~/.codex` for every account
and provider. The sidebar lists threads from `~/.codex/state_5.sqlite` and
the local thread catalog at `~/.codex/sqlite/codex-dev.db`, and it filters by
the `model_provider` column:

- `openai`: sessions created while signed in to the Codex / OpenAI account.
- `custom`: sessions created through a custom provider such as the DeepSeek
  profile configured by CC Switch.

When a user switches from the Codex account to DeepSeek (or back), the active
provider changes from `openai` to `custom` (or back). Threads belonging to the
other provider are still in the databases and rollout files, but the sidebar
does not show them. The data is not lost.

## Files involved

- `~/.codex/config.toml` contains the current `model_provider` value.
- `~/.codex/state_5.sqlite` has the `threads` table used for thread listings.
- `~/.codex/sqlite/codex-dev.db` has `local_thread_catalog`, the desktop app's
  provider-scoped catalog.
- `~/.codex/sessions/**/*.jsonl` and `~/.codex/archived_sessions/*.jsonl` are
  rollout files. The first `session_meta` JSON line records
  `payload.model_provider`.

## Why both the database and rollouts must be updated

Codex reconciles its state database from rollout files. If only the database
`model_provider` is changed, a later reconcile can overwrite it back to the
provider recorded in `session_meta`. Updating both keeps the two sources
consistent.

## Backup and restore

The sync script writes a baseline snapshot to
`~/.codex/skill-backups/unify-codex-history/<timestamp>-<provider>/`:

- SQLite copies of `state_5.sqlite` and `codex-dev.db`
- copies of every rollout file it modifies
- `manifest.json` recording original provider values and paths

`--restore` restores the oldest baseline snapshot, undoing every sync made by
this skill.

## CC Switch integration

CC Switch is a separate provider-switching app that rewrites `~/.codex`
config and auth files. It has its own `unifyCodexSessionHistory` setting; when
enabled, it performs the same provider relabeling (database plus rollout
`session_meta`) with its own backups. Prefer that built-in path when CC Switch
is installed, and use `scripts/sync_provider.py` as a standalone fallback.
