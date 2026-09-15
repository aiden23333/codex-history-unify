#!/usr/bin/env python3
"""Reconcile Codex history and CC Switch invariants before Codex starts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from contextlib import closing

try:
    from . import migrate_protocol, sync_provider
except ImportError:
    import migrate_protocol  # type: ignore
    import sync_provider  # type: ignore


class UnstableSwitchState(RuntimeError):
    pass


# A rollout written this recently means Codex is still producing a turn, so an
# automatic restart would throw away the user's in-flight work.
ACTIVE_WRITE_WINDOW_SECONDS = 15.0


@dataclass(frozen=True)
class TargetSnapshot:
    target: Literal["gpt", "deepseek"]
    provider_id: str
    model: str
    provider_bucket: str
    api_format: str | None
    fingerprint: str


@dataclass
class ReconcileReport:
    changed: bool
    protocol_files: int = 0
    provider_threads: int = 0
    cc_switch_changed: bool = False
    catalog_changed: bool = False
    backup_dir: Path | None = None
    deferred: int = 0
    busy: bool = False
    unreadable: int = 0


def _sessions_busy(sessions: Path, window: float) -> bool:
    """True when any rollout was appended recently, i.e. a turn is running."""

    if not sessions.exists():
        return False
    now = time.time()
    for path in sessions.rglob("*.jsonl"):
        try:
            if now - path.stat().st_mtime <= window:
                return True
        except OSError:
            continue
    return False


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns
    except FileNotFoundError:
        return None


def _read_snapshot(codex_home: Path, cc_home: Path) -> TargetSnapshot:
    config_path = codex_home / "config.toml"
    settings_path = cc_home / "settings.json"
    database_path = cc_home / "cc-switch.db"
    with config_path.open("rb") as fh:
        config = tomllib.load(fh)
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    with closing(sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)) as con:
        row = con.execute(
            "SELECT id, name, meta FROM providers WHERE app_type='codex' AND is_current=1"
        ).fetchone()
        if not row:
            raise UnstableSwitchState("CC Switch has no current Codex provider")
    provider_id, provider_name, meta_raw = row
    meta = json.loads(meta_raw or "{}")
    model = str(config.get("model", ""))
    model_is_deepseek = "deepseek" in model.lower()
    provider_is_deepseek = "deepseek" in f"{provider_id} {provider_name}".lower()
    provider_is_gpt = provider_id == "codex-official" or "openai" in str(provider_name).lower()
    if model_is_deepseek and provider_is_deepseek:
        target: Literal["gpt", "deepseek"] = "deepseek"
    elif not model_is_deepseek and provider_is_gpt:
        target = "gpt"
    else:
        raise UnstableSwitchState(
            f"Codex model {model!r} and CC Switch provider {provider_id!r} disagree"
        )
    configured_provider = settings.get("currentProviderCodex")
    if configured_provider and configured_provider not in {provider_id, "deepseek" if target == "deepseek" else "codex-official"}:
        raise UnstableSwitchState("CC Switch settings and database disagree")
    provider_bucket = str(config.get("model_provider", "openai"))
    catalog_path = codex_home / "cc-switch-model-catalog.json"
    catalog_modalities: list[tuple[str, tuple[str, ...]]] = []
    if catalog_path.exists():
        catalog_data = json.loads(catalog_path.read_text(encoding="utf-8"))
        for item in catalog_data.get("models", []):
            slug = str(item.get("slug") or item.get("model") or "")
            if "deepseek" in slug.lower():
                catalog_modalities.append(
                    (slug, tuple(item.get("input_modalities") or []))
                )
    safe_fields = {
        "target": target,
        "provider_id": provider_id,
        "model": model,
        "provider_bucket": provider_bucket,
        "api_format": meta.get("apiFormat"),
        "catalog_modalities": catalog_modalities,
    }
    digest = hashlib.sha256(
        json.dumps(safe_fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return TargetSnapshot(
        target=target,
        provider_id=str(provider_id),
        model=model,
        provider_bucket=provider_bucket,
        api_format=meta.get("apiFormat"),
        fingerprint=digest,
    )


def _read_snapshot_retryable(codex_home: Path, cc_home: Path) -> TargetSnapshot:
    """Read the switch state, reporting half-written files as instability.

    CC Switch rewrites its configuration and database while it applies a
    switch, so a torn read is an expected transient condition rather than a
    damaged installation.
    """

    try:
        return _read_snapshot(codex_home, cc_home)
    except (sqlite3.Error, json.JSONDecodeError, KeyError, OSError, tomllib.TOMLDecodeError) as exc:
        raise UnstableSwitchState(f"CC Switch state is unreadable: {type(exc).__name__}") from exc


def read_stable_snapshot(
    codex_home: Path, cc_home: Path, stable_delay: float = 0.8
) -> TargetSnapshot:
    # Only the small files whose contents select the target are compared by
    # signature. The CC Switch database is rewritten continuously while it runs
    # (checkpoints touch its mtime without changing any relevant value), so the
    # parsed snapshot comparison below is the authority for that state.
    paths = [
        codex_home / "config.toml",
        cc_home / "settings.json",
        codex_home / "cc-switch-model-catalog.json",
    ]
    before = [_file_signature(path) for path in paths]
    first = _read_snapshot_retryable(codex_home, cc_home)
    if stable_delay:
        time.sleep(stable_delay)
    after = [_file_signature(path) for path in paths]
    second = _read_snapshot_retryable(codex_home, cc_home)
    if before != after or first != second:
        raise UnstableSwitchState("CC Switch state is still changing")
    return second


def _catalog_plan(path: Path) -> tuple[dict | None, bool]:
    if not path.exists():
        return None, False
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = False
    for model in data.get("models", []):
        slug = str(model.get("slug") or model.get("model") or "")
        if "deepseek" not in slug.lower():
            continue
        modalities = list(model.get("input_modalities") or [])
        for modality in ("text", "image"):
            if modality not in modalities:
                modalities.append(modality)
                changed = True
        model["input_modalities"] = modalities
    return data, changed


def _cc_meta_plan(database: Path, provider_id: str) -> tuple[dict, bool]:
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as con:
        row = con.execute(
            "SELECT meta FROM providers WHERE app_type='codex' AND id=?", (provider_id,)
        ).fetchone()
    if not row:
        raise UnstableSwitchState(f"CC Switch provider {provider_id!r} disappeared")
    meta = json.loads(row[0] or "{}")
    changed = meta.get("apiFormat") != "openai_responses"
    if changed:
        meta["apiFormat"] = "openai_responses"
    return meta, changed


def _atomic_json(path: Path, data: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.loads(temp.read_text(encoding="utf-8"))
    os.replace(temp, path)


def _backup_switch_files(codex_home: Path, cc_home: Path, files: list[Path]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    root = codex_home / "switch-guard" / "backups" / stamp
    root.mkdir(parents=True, exist_ok=False)
    for source in files:
        if not source.exists():
            continue
        target = root / source.name
        if source.suffix == ".db":
            with closing(sqlite3.connect(str(source))) as src, closing(sqlite3.connect(str(target))) as dst:
                src.backup(dst)
        else:
            shutil.copy2(source, target)
    return root


def _validate_sqlite(path: Path) -> None:
    if not path.exists():
        return
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as con:
        result = con.execute("PRAGMA quick_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite validation failed: {path}")


def reconcile(
    snapshot: TargetSnapshot,
    codex_home: Path,
    cc_home: Path,
    *,
    apply: bool,
) -> ReconcileReport:
    sessions = codex_home / "sessions"
    protocol_plan = migrate_protocol.plan(
        sessions,
        codex_home / "thread-writer-locks",
        snapshot.target,
    )
    planned_protocol = protocol_plan.changes
    protocol_changes = [change for change in planned_protocol if change.writable]
    deferred_changes = [change for change in planned_protocol if not change.writable]
    provider_changes = sync_provider.plan_provider_sync(
        codex_home, snapshot.provider_bucket
    )
    actionable_provider = [item for item in provider_changes if not item["skipped"]]
    cc_database = cc_home / "cc-switch.db"
    meta: dict | None = None
    cc_changed = False
    if snapshot.target == "deepseek":
        meta, cc_changed = _cc_meta_plan(cc_database, snapshot.provider_id)
    catalog_path = codex_home / "cc-switch-model-catalog.json"
    catalog, catalog_changed = _catalog_plan(catalog_path)
    changed = bool(planned_protocol or actionable_provider or cc_changed or catalog_changed)
    report = ReconcileReport(
        changed=changed,
        protocol_files=len(planned_protocol),
        provider_threads=len(actionable_provider),
        cc_switch_changed=cc_changed,
        catalog_changed=catalog_changed,
        deferred=len(deferred_changes),
        busy=_sessions_busy(sessions, ACTIVE_WRITE_WINDOW_SECONDS),
        unreadable=len(protocol_plan.unreadable),
    )
    if not apply or not changed:
        return report

    guarded_files = [cc_home / "settings.json", cc_database, catalog_path]
    if cc_changed or catalog_changed:
        report.backup_dir = _backup_switch_files(codex_home, cc_home, guarded_files)
    if cc_changed and meta is not None:
        with closing(sqlite3.connect(str(cc_database))) as con:
            con.execute(
                "UPDATE providers SET meta=? WHERE app_type='codex' AND id=?",
                (json.dumps(meta, ensure_ascii=False, separators=(",", ":")), snapshot.provider_id),
            )
            con.commit()
    if catalog_changed and catalog is not None:
        _atomic_json(catalog_path, catalog)
    if protocol_changes:
        backup = migrate_protocol.create_backup(
            protocol_changes,
            sessions,
            codex_home / "skill-backups" / "unify-codex-history" / "protocol",
        )
        for change in protocol_changes:
            migrate_protocol.write_rows(change.path, change.rows)
        migrate_protocol.prune_backups(backup.parent, 3)
    sync_provider.apply_provider_sync(
        codex_home, snapshot.provider_bucket, provider_changes
    )
    for path in (codex_home / "state_5.sqlite", codex_home / "sqlite" / "codex-dev.db", cc_database):
        _validate_sqlite(path)
    if catalog_path.exists():
        json.loads(catalog_path.read_text(encoding="utf-8"))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--cc-home", type=Path, default=Path.home() / ".cc-switch")
    parser.add_argument("--stable-delay", type=float, default=0.8)
    args = parser.parse_args()
    try:
        snapshot = read_stable_snapshot(args.codex_home, args.cc_home, args.stable_delay)
        report = reconcile(snapshot, args.codex_home, args.cc_home, apply=args.apply)
    except (OSError, ValueError, sqlite3.Error, RuntimeError, tomllib.TOMLDecodeError) as exc:
        print(f"preflight_error={exc}", file=sys.stderr)
        return 1
    print(f"target={snapshot.target}")
    print(f"model={snapshot.model}")
    print(f"provider_bucket={snapshot.provider_bucket}")
    print(f"fingerprint={snapshot.fingerprint}")
    print(f"changed={int(report.changed)}")
    print(f"protocol_files={report.protocol_files}")
    print(f"provider_threads={report.provider_threads}")
    print(f"cc_switch_changed={int(report.cc_switch_changed)}")
    print(f"catalog_changed={int(report.catalog_changed)}")
    print(f"deferred={report.deferred}")
    print(f"unreadable={report.unreadable}")
    if report.deferred:
        print(
            "warning=rollout_files_owned_by_another_process",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
