---
name: codex-history-unify
description: "Use when Codex conversations disappear or fail after switching between DeepSeek and OpenAI/GPT, especially invalid_id_prefix, array_above_max_length, store=false item not found, remote compact failures, or model_provider history filtering in Codex or CC Switch."
---

# Codex History Unify

## Overview

Unify local Codex session history to the currently selected provider so that
conversations created under another account (for example `openai` vs `custom`
DeepSeek) reappear in the sidebar. The underlying data is never deleted; this
skill relabels provider metadata and keeps baseline backups.

It also repairs verified DeepSeek-to-GPT rollout incompatibilities without
rewriting visible messages or valid GPT encrypted reasoning.

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

## Automatic mode (macOS LaunchAgent)

The skill ships a background guard so a provider switch needs no manual step:
close Codex, switch in CC Switch, open Codex once, and every local unarchived
task works on that first launch.

```bash
python3 scripts/codex_switch_preflight.py --dry-run   # inspect one reconciliation
python3 scripts/codex_switch_guard.py --plan-only     # inspect the guard decision, read-only
python3 scripts/install_switch_guard.py --dry-run     # show what would be installed
python3 scripts/install_switch_guard.py --apply       # install the LaunchAgent
python3 scripts/install_switch_guard.py --uninstall   # remove it again
```

Installing writes `~/Library/LaunchAgents/com.aiden.codex-switch-guard.plist`
(mode 600) and boots label `com.aiden.codex-switch-guard` into `gui/<uid>`.
Guard state and logs stay in `~/.codex/switch-guard/`; no credentials, tokens,
or response bodies are ever written there.

What the guard does on every detected switch:

- Watches `config.toml`, CC Switch `settings.json`, the CC Switch database, and
  the generated model catalog, then waits until they stop changing.
- Repairs assistant response IDs, unreplayable reasoning items, provider labels,
  the DeepSeek provider card (native Responses), and the DeepSeek catalog
  `text`/`image` modalities.
- Restarts Codex at most once per switch, and only when the app was launched
  within the last three minutes and no task is mid-turn. A long-running session
  is never interrupted; pending repairs are applied the next time Codex closes.
- Skips rollouts a live writer holds and never touches archived conversations.
- Retries a repair that another process blocks with backoff (20 s up to 300 s)
  instead of looping or reporting success it did not achieve; a repair blocked
  while Codex runs is retried as soon as the app closes.

Keep the DeepSeek provider card on native Responses. A Chat Completions
upstream converts Codex `function_call` history into paired chat messages and
has already produced orphan tool messages on this machine, so the guard always
restores `apiFormat = "openai_responses"`.

## Protocol migration after a model switch

When a visible conversation fails after switching providers:

1. Read the complete first API error and identify the target provider.
2. Run `python3 scripts/migrate_protocol.py --target auto --dry-run`.
3. For a GPT target, report the affected file and item counts, then run
   `python3 scripts/migrate_protocol.py --target gpt --apply`. This performs
   the three verified repairs in one pass:
   - normalize non-`msg*` assistant response IDs and linked UI event IDs;
   - remove DeepSeek plaintext reasoning response items;
   - remove `rs_resp_*` reasoning references lacking encrypted payloads,
     which cannot be replayed after `store=false`.
4. Restart Codex before retrying the old conversation.
5. For a DeepSeek target, the script is diagnostic-only. Do not reverse the
   GPT cleanup mechanically; inspect the first real CC Switch/DeepSeek error
   and preserve valid encrypted GPT reasoning until evidence identifies the
   incompatible item type.

Each GPT apply creates one compressed archive containing only changed rollout
files. It retains the newest three protocol archives. Restore the latest with
`python3 scripts/migrate_protocol.py --restore-latest`.

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
- Use protocol migration only for provider-switch compatibility errors. Do
  not remove reasoning items merely to reduce file size.
- Never modify archived sessions unless the user asks to repair an archived
  conversation.

See [mechanism.md](references/mechanism.md) for the full data layout and why
both the state database and rollout `session_meta` must be updated.
