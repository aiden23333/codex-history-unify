# Automatic CC Switch → Codex Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make DeepSeek ↔ GPT switches in CC Switch reconcile every local unarchived Codex task before its first usable launch, without user confirmation, commands, or a second manual restart.

**Architecture:** Add an idempotent preflight command that reads a stable CC Switch/Codex snapshot, enforces native Responses invariants, synchronizes provider metadata, and performs target-aware rollout migration. A macOS LaunchAgent daemon detects configuration changes and runs preflight while Codex is closed; if the app wins the launch race, the daemon performs at most one controlled quit, repair, and reopen.

**Tech Stack:** Python 3.11+ standard library, SQLite, TOML, JSON/JSONL, `lsof`, macOS `launchd`, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-15-automatic-cc-switch-codex-preflight-design.md`

## Global Constraints

- DeepSeek must use its native Responses upstream format; Chat Completions is outside the reliability guarantee.
- Only local unarchived Codex tasks are migrated automatically.
- Never mutate a rollout while Codex owns the rollout or its writer-lock file descriptor.
- Preserve visible messages, tool results, and valid encrypted GPT reasoning.
- Keep credentials, tokens, prompts, and response bodies out of logs and guard state.
- Keep at most three protocol archives and allow only one automated quit/reopen per configuration fingerprint.
- All live mutations require a successful dry run, a backup, atomic writes, and post-write validation.

---

### Task 1: Make protocol migration target-aware and descriptor-safe

**Files:**
- Modify: `scripts/migrate_protocol.py`
- Modify: `tests/test_migrate_protocol.py`

**Interfaces:**
- Produces: `transform(rows: list[dict], target: str) -> tuple[list[dict], int, int]`
- Produces: `writer_in_use(path: Path, lock_dir: Path, lsof_bin: Path | None = None) -> bool`
- Produces: CLI behavior where both targets normalize assistant IDs, while only GPT removes incompatible reasoning.

- [ ] **Step 1: Add failing target-behavior tests**

Add tests proving DeepSeek normalization preserves all reasoning and GPT normalization removes only plaintext or unpersisted `rs_resp_*` reasoning:

```python
def test_deepseek_normalizes_ids_but_preserves_reasoning(self) -> None:
    result = self.run_script("--target", "deepseek", "--apply")
    self.assertEqual(result.returncode, 0, result.stderr)
    rows = read_jsonl(self.rollout)
    self.assertEqual(rows[0]["payload"]["id"], "msg_abc")
    self.assertEqual(rows[1]["payload"]["item"]["id"], "msg_abc")
    reasoning = [r for r in rows if r.get("type") == "response_item" and r["payload"].get("type") == "reasoning"]
    self.assertEqual(len(reasoning), 3)

def test_gpt_preserves_encrypted_reasoning(self) -> None:
    result = self.run_script("--target", "gpt", "--apply")
    self.assertEqual(result.returncode, 0, result.stderr)
    rows = read_jsonl(self.rollout)
    ids = [r["payload"]["id"] for r in rows if r.get("type") == "response_item" and r["payload"].get("type") == "reasoning"]
    self.assertEqual(ids, ["rs_valid"])
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_migrate_protocol -v`

Expected: DeepSeek test fails because the current CLI exits with `diagnostic_only`; existing GPT tests remain green.

- [ ] **Step 3: Implement target-aware transformation**

Change the transformation signature and gate reasoning deletion by target:

```python
def transform(rows: list[dict], target: str) -> tuple[list[dict], int, int]:
    output: list[dict] = []
    assistant_ids = 0
    reasoning_items = 0
    linked_ids: dict[str, str] = {}
    for row in rows:
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            output.append(row)
            continue
        if (
            target == "gpt"
            and row.get("type") == "response_item"
            and payload.get("type") == "reasoning"
            and is_incompatible_reasoning(payload)
        ):
            reasoning_items += 1
            continue
        if row.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
            old_id = payload.get("id")
            if isinstance(old_id, str) and not old_id.startswith("msg"):
                new_id = normalize_message_id(old_id)
                payload["id"] = new_id
                linked_ids[old_id] = new_id
                assistant_ids += 1
        output.append(row)
    # retain the existing linked AgentMessage update pass
    return output, assistant_ids, reasoning_items
```

Remove the diagnostic-only DeepSeek exit and pass `target` through `scan()`.

- [ ] **Step 4: Replace marker-only locking with an injectable descriptor check**

Implement `writer_in_use()` so production uses `/usr/sbin/lsof` on macOS and tests can inject a missing binary; treat a live file descriptor as locked and a stale marker as unlocked:

```python
def writer_in_use(path: Path, lock_dir: Path, lsof_bin: Path | None = None) -> bool:
    match = THREAD_ID_RE.search(path.name)
    if not match:
        return False
    lock = lock_dir / f"{match.group(1)}.lock"
    if not lock.exists():
        return False
    binary = lsof_bin or Path("/usr/sbin/lsof")
    if not binary.exists():
        return True
    result = subprocess.run([str(binary), "-t", str(path), str(lock)], capture_output=True, text=True, check=False)
    return result.returncode == 0 and bool(result.stdout.strip())
```

Add tests using a temporary executable that exits `0` with a PID for an active lock and `1` for a stale marker.

- [ ] **Step 5: Run focused and full tests**

Run:

```bash
python3 -m unittest tests.test_migrate_protocol -v
python3 -m unittest discover -s tests -v
```

Expected: all tests pass with no live `~/.codex` access.

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate_protocol.py tests/test_migrate_protocol.py
git commit -m "fix: canonicalize histories for both targets"
```

---

### Task 2: Add stable switch-state detection and invariant reconciliation

**Files:**
- Create: `scripts/codex_switch_preflight.py`
- Create: `tests/test_switch_preflight.py`
- Modify: `scripts/sync_provider.py`

**Interfaces:**
- Produces: `TargetSnapshot(target: Literal["gpt", "deepseek"], provider_id: str, model: str, provider_bucket: str, fingerprint: str)`
- Produces: `read_stable_snapshot(codex_home: Path, cc_home: Path) -> TargetSnapshot`
- Produces: `reconcile(snapshot: TargetSnapshot, codex_home: Path, cc_home: Path, apply: bool) -> ReconcileReport`
- Consumes: protocol migration and provider synchronization as Python functions, not subprocess text parsing.

- [ ] **Step 1: Write failing snapshot tests**

Create temporary Codex/CC Switch fixtures with SQLite databases and assert:

```python
def test_detects_deepseek_only_when_config_and_cc_switch_agree(self) -> None:
    self.write_codex_config(model="deepseek-v4-flash", provider="custom", wire_api="responses")
    self.write_cc_state(current_provider="deepseek", api_format="openai_responses", proxy_enabled=True)
    snapshot = preflight.read_stable_snapshot(self.codex_home, self.cc_home)
    self.assertEqual(snapshot.target, "deepseek")

def test_rejects_partially_written_mixed_state(self) -> None:
    self.write_codex_config(model="deepseek-v4-flash", provider="custom", wire_api="responses")
    self.write_cc_state(current_provider="codex-official", api_format="openai_responses", proxy_enabled=False)
    with self.assertRaises(preflight.UnstableSwitchState):
        preflight.read_stable_snapshot(self.codex_home, self.cc_home)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_switch_preflight.SwitchSnapshotTest -v`

Expected: import fails because `codex_switch_preflight.py` does not exist.

- [ ] **Step 3: Implement snapshot parsing and fingerprinting**

Use `tomllib`, `json`, and read-only SQLite connections. Hash only non-secret normalized fields:

```python
@dataclass(frozen=True)
class TargetSnapshot:
    target: Literal["gpt", "deepseek"]
    provider_id: str
    model: str
    provider_bucket: str
    fingerprint: str

def fingerprint(fields: dict[str, object]) -> str:
    raw = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()
```

Read the snapshot twice 800 ms apart; require matching stat tuples and matching normalized state before returning.

- [ ] **Step 4: Write failing invariant tests**

Cover native Responses correction, catalog modalities, provider synchronization, idempotence, and invalid input rollback:

```python
def test_deepseek_apply_enforces_native_responses_and_catalog(self) -> None:
    report = preflight.reconcile(self.deepseek_snapshot(), self.codex_home, self.cc_home, apply=True)
    provider = self.read_provider_settings("deepseek")
    self.assertEqual(provider["apiFormat"], "openai_responses")
    catalog = json.loads((self.codex_home / "cc-switch-model-catalog.json").read_text())
    self.assertTrue(all({"text", "image"} <= set(m["input_modalities"]) for m in catalog["models"] if "deepseek" in m["slug"]))
    self.assertTrue(report.changed)

def test_same_fingerprint_is_noop(self) -> None:
    first = preflight.reconcile(self.gpt_snapshot(), self.codex_home, self.cc_home, apply=True)
    second = preflight.reconcile(self.gpt_snapshot(), self.codex_home, self.cc_home, apply=True)
    self.assertTrue(first.changed)
    self.assertFalse(second.changed)
```

- [ ] **Step 5: Refactor provider sync into callable functions**

Keep CLI compatibility while exposing:

```python
def plan_provider_sync(home: Path, target: str) -> list[dict]: ...
def apply_provider_sync(home: Path, target: str, changes: list[dict]) -> ProviderSyncReport: ...
```

Do not back up or write when `changes` is empty. Replace marker-only locks with the descriptor-safe helper from Task 1.

- [ ] **Step 6: Implement backed-up invariant reconciliation**

Before changing `settings.json`, `cc-switch.db`, or the catalog, create a timestamped directory under `~/.codex/switch-guard/backups/`. Use SQLite's backup API for the database and atomic `os.replace()` for JSON. Update only the DeepSeek provider's `apiFormat`; preserve credentials and unrelated JSON key order.

Run protocol canonicalization for both targets, reasoning cleanup only for GPT, then provider sync. Validate all changed JSONL files, run `PRAGMA quick_check` on both Codex databases, validate the catalog JSON, and write the successful fingerprint only after every check passes.

- [ ] **Step 7: Run focused and full tests**

Run:

```bash
python3 -m unittest tests.test_switch_preflight -v
python3 -m unittest discover -s tests -v
```

Expected: all tests pass; fixture backups exist only for mutating cases.

- [ ] **Step 8: Commit**

```bash
git add scripts/codex_switch_preflight.py scripts/sync_provider.py tests/test_switch_preflight.py
git commit -m "feat: add idempotent switch preflight"
```

---

### Task 3: Add the macOS switch guard and launch-race handling

**Files:**
- Create: `scripts/codex_switch_guard.py`
- Create: `tests/test_switch_guard.py`

**Interfaces:**
- Consumes: `read_stable_snapshot()` and `reconcile()` from Task 2.
- Produces: `Guard.run_once() -> GuardDecision`
- Produces: `Guard.run_forever(poll_seconds: float = 0.5) -> None`
- Produces: one-restart-per-fingerprint state at `~/.codex/switch-guard/state.json`.

- [ ] **Step 1: Write failing state-machine tests**

Use real temporary files and injected process/application adapters:

```python
def test_closed_codex_reconciles_without_restart(self) -> None:
    guard = self.make_guard(codex_open=False, pending=True)
    decision = guard.run_once()
    self.assertEqual(decision.action, "reconciled")
    self.assertEqual(self.apps.quit_calls, 0)
    self.assertEqual(self.apps.open_calls, 0)

def test_launch_race_quits_and_reopens_once(self) -> None:
    guard = self.make_guard(codex_open=True, pending=True)
    first = guard.run_once()
    second = guard.run_once()
    self.assertEqual(first.action, "restarted_after_reconcile")
    self.assertEqual(second.action, "noop")
    self.assertEqual(self.apps.quit_calls, 1)
    self.assertEqual(self.apps.open_calls, 1)
```

Also test unstable snapshots, active rollout descriptors, reconciliation failure, and retry exhaustion.

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_switch_guard -v`

Expected: import fails because the guard module does not exist.

- [ ] **Step 3: Implement adapters and state machine**

Define narrow injectable interfaces:

```python
class ProcessProbe(Protocol):
    def codex_rollouts_open(self) -> bool: ...

class AppController(Protocol):
    def quit_codex(self) -> None: ...
    def open_codex(self) -> None: ...
```

Production `ProcessProbe` uses `/usr/sbin/lsof` against rollout and writer-lock directories. Production `AppController` uses `osascript` with bundle ID `com.openai.codex` for graceful quit and `/usr/bin/open -b com.openai.codex` to reopen.

Persist `last_successful_fingerprint`, `restart_attempted_fingerprint`, `last_error_code`, and timestamps atomically. Never store raw config, auth, prompts, or tool output.

- [ ] **Step 4: Implement daemon polling and debounce**

Track stat tuples for `config.toml`, `settings.json`, `cc-switch.db`, `cc-switch.db-wal`, and the model catalog. Require two identical observations separated by 800 ms before preflight. Sleep 500 ms between idle polls and use bounded exponential retry for transient partial writes: 0.5, 1, 2, and 4 seconds.

- [ ] **Step 5: Run focused and full tests**

Run:

```bash
python3 -m unittest tests.test_switch_guard -v
python3 -m unittest discover -s tests -v
```

Expected: all state-machine tests pass and no real application is opened or closed.

- [ ] **Step 6: Commit**

```bash
git add scripts/codex_switch_guard.py tests/test_switch_guard.py
git commit -m "feat: guard Codex startup during provider switches"
```

---

### Task 4: Add reversible LaunchAgent installation

**Files:**
- Create: `scripts/install_switch_guard.py`
- Create: `tests/test_install_switch_guard.py`
- Create: `assets/com.aiden.codex-switch-guard.plist.template`

**Interfaces:**
- Produces: `install(codex_home: Path, skill_dir: Path, launch_agents_dir: Path, apply: bool) -> InstallReport`
- Produces: `uninstall(codex_home: Path, launch_agents_dir: Path, apply: bool) -> InstallReport`
- CLI: `install_switch_guard.py --dry-run|--apply|--uninstall`.

- [ ] **Step 1: Write failing installer tests**

Assert deterministic plist rendering, mode `0600`, no credential content, idempotent reinstall, and uninstall that leaves history/backups untouched:

```python
def test_install_writes_reversible_launch_agent(self) -> None:
    report = installer.install(self.codex_home, self.skill_dir, self.launch_agents, apply=True)
    plist = self.launch_agents / "com.aiden.codex-switch-guard.plist"
    self.assertTrue(plist.exists())
    self.assertEqual(stat.S_IMODE(plist.stat().st_mode), 0o600)
    self.assertNotIn("token", plist.read_text().lower())
    self.assertTrue(report.changed)

def test_uninstall_preserves_backups_and_history(self) -> None:
    installer.uninstall(self.codex_home, self.launch_agents, apply=True)
    self.assertTrue((self.codex_home / "switch-guard" / "backups").exists())
    self.assertTrue((self.codex_home / "sessions").exists())
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m unittest tests.test_install_switch_guard -v`

Expected: import fails because the installer does not exist.

- [ ] **Step 3: Implement plist rendering and CLI**

Render absolute paths for the current Python executable, guard script, Codex home, CC Switch home, stdout log, and stderr log. The plist uses `RunAtLoad=true` and `KeepAlive=true`; the daemon itself performs low-frequency polling. Install with `launchctl bootstrap gui/<uid> <plist>` and replace an existing service with `bootout` followed by `bootstrap`.

Dry run prints intended paths and validation results without writing. Uninstall boots out the label and removes only the installed plist; it does not delete state, backups, sessions, or logs.

- [ ] **Step 4: Run focused and full tests**

Run:

```bash
python3 -m unittest tests.test_install_switch_guard -v
python3 -m unittest discover -s tests -v
```

Expected: all tests pass; launchctl is injected in tests and is never called against the real user session.

- [ ] **Step 5: Commit**

```bash
git add scripts/install_switch_guard.py tests/test_install_switch_guard.py assets/com.aiden.codex-switch-guard.plist.template
git commit -m "feat: install persistent switch guard"
```

---

### Task 5: Document, install, and verify the live workflow

**Files:**
- Modify: `SKILL.md`
- Modify: `README.md`
- Modify: `agents/openai.yaml`
- Modify: `/Users/aiden.wang/Documents/Obsidian/Codex/20-故障排查/Codex 切换 DeepSeek 与 GPT 后的窗口修复.md`

**Interfaces:**
- Consumes: installer and preflight CLIs from Tasks 2–4.
- Produces: installed LaunchAgent label `com.aiden.codex-switch-guard` and documented rollback commands.

- [ ] **Step 1: Add automatic-mode instructions**

Document these exact commands and behavior:

```bash
python3 scripts/codex_switch_preflight.py --dry-run
python3 scripts/install_switch_guard.py --dry-run
python3 scripts/install_switch_guard.py --apply
python3 scripts/install_switch_guard.py --uninstall
```

State that native Responses is required for DeepSeek, archived tasks are excluded, backups retain three protocol archives, and the guard may perform one automatic quit/reopen if the app wins the launch race.

- [ ] **Step 2: Run documentation and package validation**

Run:

```bash
rg -n 'TBD|TODO|PLACEHOLDER|FIXME' SKILL.md README.md agents/openai.yaml docs scripts tests assets
python3 /Users/aiden.wang/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/aiden.wang/.codex/skills/codex-history-unify
python3 -m unittest discover -s tests -v
git diff --check
```

Expected: no placeholders, skill validation succeeds, all tests pass, and no whitespace errors are reported.

- [ ] **Step 3: Run live dry runs**

Run:

```bash
python3 scripts/codex_switch_preflight.py --dry-run
python3 scripts/install_switch_guard.py --dry-run
```

Expected: target snapshot is coherent; planned mutations list no archived rollouts or credential values.

- [ ] **Step 4: Install the guard**

Run:

```bash
python3 scripts/install_switch_guard.py --apply
launchctl print "gui/$(id -u)/com.aiden.codex-switch-guard"
```

Expected: service state is `running`, paths point to this skill, and logs contain no credentials.

- [ ] **Step 5: Perform controlled end-to-end switch verification**

With Codex closed, switch to DeepSeek in CC Switch and wait for the guard's successful fingerprint. Open Codex once and send a no-tool message in representative previously-GPT tasks. Then close Codex, switch to the official GPT provider, wait for success, open once, and send a no-tool message in representative previously-DeepSeek tasks.

Acceptance requires no unsupported-model, `invalid_id_prefix`, orphan-tool, missing-item, or remote-compaction errors; no manual repair command; and no second user-initiated restart.

- [ ] **Step 6: Update the Obsidian troubleshooting note**

Record the verified date, installed service label, preflight/rollback commands, native Responses requirement, observed end-to-end results, and evidence paths. Mark any direction not actually exercised as “待确认,” never as confirmed.

- [ ] **Step 7: Commit**

```bash
git add SKILL.md README.md agents/openai.yaml
git commit -m "docs: add automatic switch guard workflow"
```

- [ ] **Step 8: Final verification**

Run:

```bash
python3 -m unittest discover -s tests -v
python3 /Users/aiden.wang/.codex/skills/.system/skill-creator/scripts/quick_validate.py /Users/aiden.wang/.codex/skills/codex-history-unify
git status --short
launchctl print "gui/$(id -u)/com.aiden.codex-switch-guard" | rg 'state =|pid =|last exit code|runs ='
```

Expected: tests and skill validation pass, only the pre-existing ignored/untracked cache remains, and the LaunchAgent is running without a failure exit code.
