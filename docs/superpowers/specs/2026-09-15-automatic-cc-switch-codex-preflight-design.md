# Automatic CC Switch → Codex Preflight Design

Date: 2026-09-15
Status: Approved in chat; awaiting written-spec review

## Goal

After the user closes Codex, switches between DeepSeek and GPT in CC Switch, and opens Codex again, every local unarchived task must be usable on the first user-visible launch. The user must not confirm a repair, run a command, or restart Codex a second time.

The DeepSeek provider will use its native Responses upstream format. Chat Completions conversion is outside the reliability guarantee because it changes Codex `function_call`/`function_call_output` history into paired chat `assistant.tool_calls`/`tool` messages and has already produced orphan tool messages on this machine.

## Confirmed local facts

- Codex stores task metadata in `~/.codex/state_5.sqlite`, the desktop catalog in `~/.codex/sqlite/codex-dev.db`, and rollout history in `~/.codex/sessions/**/*.jsonl`.
- CC Switch rewrites Codex configuration and authentication state and has `unifyCodexSessionHistory`, but that feature only unifies provider buckets. It does not normalize response-item IDs or remove reasoning items that cannot be replayed after `store=false`.
- A DeepSeek run can persist assistant IDs such as `resp_UUID_msg`. GPT rejects these when replayed because assistant message IDs must begin with `msg`.
- Writer-lock marker existence is not a sufficient safety check. The guard must use `lsof` to determine whether Codex actually has a rollout or its lock file open.
- CC Switch may regenerate `cc-switch-model-catalog.json`, reverting DeepSeek input modalities to text-only.

## Architecture

The existing `codex-history-unify` skill gains two reusable components:

1. `scripts/codex_switch_preflight.py`
   - Performs one idempotent reconciliation for the stable target configuration.
   - Can run manually with `--dry-run` or `--apply` and is also called by the daemon.
2. `scripts/codex_switch_guard.py`
   - Runs as a lightweight macOS LaunchAgent.
   - Watches stable fingerprints of Codex config, CC Switch settings, CC Switch database/WAL state, and the generated model catalog.
   - Debounces a switch until the relevant files stop changing.
   - Runs preflight while Codex is closed.
   - If Codex starts before a pending preflight completes, it closes Codex once, completes the repair, and reopens it automatically. The first usable launch is therefore healthy, although the icon may briefly bounce during this race.

The guard stores only a target fingerprint, last successful result, and timestamps in `~/.codex/switch-guard/`. It never stores credentials or request bodies.

## Target detection

The preflight derives a coherent target snapshot from all of these signals:

- `~/.codex/config.toml`: active model, provider, base URL, and wire API;
- `~/.cc-switch/settings.json`: selected Codex provider and proxy settings;
- `~/.cc-switch/cc-switch.db`: current Codex provider, proxy state, and provider `settings_config`.

The snapshot is accepted only when the files are stable and the signals agree. A DeepSeek model or DeepSeek provider card selects `deepseek`; an official Codex provider or GPT model selects `gpt`. Contradictory or partially written state is retried and never applied.

## Preflight state machine

1. Detect a configuration fingerprint change.
2. Wait for 800 ms of file stability.
3. Acquire a single-instance guard lock.
4. Determine whether Codex currently owns rollout file descriptors with `lsof`.
5. If Codex is closed, run reconciliation immediately.
6. If Codex starts during a pending reconciliation, request one graceful quit, wait for all rollout descriptors to close, reconcile, then reopen Codex.
7. Validate the final state and persist the successful fingerprint.
8. Do nothing when the same fingerprint is seen again.

Failures leave Codex files untouched when possible, preserve backups for any started mutation, write a concise local error log, and do not enter an automatic restart loop. At most one automated quit/reopen is allowed per fingerprint.

## Reconciliation rules

### Shared rules

- Update provider metadata consistently in the state database, desktop catalog, and unarchived rollout `session_meta` records.
- Normalize every assistant response-item ID that does not start with `msg`; update linked UI-event IDs in the same transaction.
- Never edit an archived rollout automatically.
- Never edit a rollout while Codex owns its file descriptor.
- Validate JSON/JSONL and both SQLite databases before recording success.
- Keep compressed protocol backups and provider baselines; retain the newest three protocol archives.

### DeepSeek target

- Require the CC Switch DeepSeek provider card to use native Responses format.
- Keep valid GPT encrypted reasoning so switching back to GPT remains lossless.
- Preserve DeepSeek plaintext reasoning while DeepSeek is active.
- Ensure the generated DeepSeek catalog entries include both `text` and `image`, preserving key order and unrelated fields.

### GPT target

- Select the GPT model/provider written by CC Switch; do not substitute another GPT model.
- Remove DeepSeek plaintext reasoning response items that GPT cannot replay.
- Remove `rs_resp_*` reasoning references without encrypted payloads when they cannot be replayed after `store=false`.
- Preserve visible user and assistant messages, tool results, and valid encrypted GPT reasoning.

## CC Switch integration

CC Switch remains the source of truth for the selected provider. Installation performs one backed-up correction of the DeepSeek provider card to native Responses format and enables unified Codex history. The guard revalidates those invariants after each switch because CC Switch can regenerate the model catalog or restore provider configuration from a backup.

The implementation does not patch the CC Switch application bundle, so application upgrades do not overwrite the guard. If CC Switch later adds a supported post-switch hook, the daemon can call the same preflight entry point from that hook without changing reconciliation logic.

## Backup and rollback

- Before changing CC Switch configuration, copy `settings.json` and `cc-switch.db` to a timestamped backup directory.
- Provider-history synchronization keeps its existing baseline backup.
- Protocol migration keeps compressed copies of only changed rollouts and retains the newest three archives.
- Catalog correction keeps the previous JSON file.
- An uninstall command removes the LaunchAgent and guard state but leaves all task history and backups intact.
- Rollback restores the most recent compatible backup and requires only one controlled app reopen.

## Testing

Tests use temporary fake Codex and CC Switch homes; they never touch live data.

- DeepSeek → GPT: bad `resp_*_msg` IDs and plaintext/unpersisted reasoning are repaired.
- GPT → DeepSeek: encrypted GPT reasoning and visible history are preserved.
- Provider metadata is consistent across both databases and rollout metadata.
- Native Responses and DeepSeek model-catalog invariants are restored after simulated CC Switch rewrites.
- Active rollout descriptors prevent mutation.
- Partially written or contradictory configuration is retried.
- Repeated identical fingerprints are no-ops.
- A launch race causes at most one controlled quit/reopen.
- Invalid JSON/JSONL or failed SQLite validation aborts and preserves the original data.
- Backup retention and restore work.

## Acceptance criteria

- With Codex closed, switching DeepSeek → GPT and opening Codex once allows previously used unarchived tasks to accept a message without `invalid_id_prefix`, unsupported-model, missing-item, or remote-compaction errors.
- With Codex closed, switching GPT → DeepSeek and opening Codex once allows the same tasks to accept a message without orphan-tool or unsupported-model errors.
- No confirmation, manual command, or second user-initiated restart is required.
- Repeating either switch does not rewrite unchanged history.
- The guard never logs credentials, tokens, full prompts, or response bodies.
- A failed repair never silently reports success and never loops application restarts.

## Known boundary

The guarantee covers local, unarchived Codex tasks and the workflow “close Codex → switch provider in CC Switch → open Codex.” Archived tasks are repaired only on explicit request. Keeping DeepSeek on Chat Completions would require maintaining a live Responses↔Chat protocol translator and is deliberately excluded from this design.
