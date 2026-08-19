#!/usr/bin/env python3
"""Unify Codex local session history to the current model provider.

Codex Desktop hides local threads whose model_provider differs from the
provider currently selected in ~/.codex/config.toml. This happens when users
switch between providers such as the OpenAI Codex account and a DeepSeek
custom provider with tools like CC Switch.

This script relabels user-visible local threads and their rollout session_meta
records to the current provider so all conversations reappear in the sidebar.
It keeps a timestamped baseline backup; use --restore to undo.

Usage:
  sync_provider.py --dry-run
  sync_provider.py --apply
  sync_provider.py --restore
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

USER_VISIBLE_SOURCES = ("vscode", "exec")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def current_provider(home: Path) -> str:
    config = home / "config.toml"
    if not config.exists():
        return "openai"
    try:
        with config.open("rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError):
        return "openai"
    return data.get("model_provider", "openai")


def connect_ro(path: Path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def read_rollout_provider(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "session_meta":
                    payload = obj.get("payload") or {}
                    return payload.get("model_provider")
    except OSError:
        return None
    return None


def discover_threads(state_db: Path) -> list[dict]:
    if not state_db.exists():
        print(f"State DB not found: {state_db}", file=sys.stderr)
        return []
    rows = []
    with connect_ro(state_db) as con:
        cur = con.execute(
            "SELECT id, rollout_path, model_provider, source, archived, preview "
            "FROM threads"
        )
        for thread_id, rollout_path, db_provider, source, archived, preview in cur.fetchall():
            if not rollout_path or source not in USER_VISIBLE_SOURCES:
                continue
            rows.append(
                {
                    "thread_id": thread_id,
                    "rollout_path": rollout_path,
                    "db_provider": db_provider,
                    "source": source,
                    "archived": bool(archived),
                    "preview": preview,
                    "rollout_provider": read_rollout_provider(Path(rollout_path)),
                }
            )
    return rows


def active_locks(home: Path) -> set[str]:
    lock_dir = home / "thread-writer-locks"
    if not lock_dir.is_dir():
        return set()
    return {p.stem for p in lock_dir.glob("*.lock")}


def compute_changes(rows: list[dict], target: str, home: Path) -> list[dict]:
    locks = active_locks(home)
    changes = []
    for row in rows:
        if row["db_provider"] != target or row["rollout_provider"] != target:
            item = dict(row)
            item["skipped"] = item["thread_id"] in locks
            changes.append(item)
    return changes


def sqlite_backup(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(src)) as src_con, sqlite3.connect(str(dst)) as dst_con:
        src_con.backup(dst_con)
    return True


def backup_snapshot(home: Path, target: str, changes: list[dict]) -> Path:
    root = home / "skill-backups" / "unify-codex-history"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir = root / f"{ts}-{target}"
    rollouts_dir = backup_dir / "rollouts"
    rollouts_dir.mkdir(parents=True, exist_ok=True)

    state_db = home / "state_5.sqlite"
    catalog_db = home / "sqlite" / "codex-dev.db"
    state_backed_up = sqlite_backup(state_db, backup_dir / "state_5.sqlite")
    catalog_backed_up = sqlite_backup(catalog_db, backup_dir / "codex-dev.db") if catalog_db.exists() else False

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_provider": target,
        "state_db_backup": "state_5.sqlite" if state_backed_up else None,
        "catalog_db_backup": "codex-dev.db" if catalog_backed_up else None,
        "rollouts": [],
    }

    for change in changes:
        if change["skipped"]:
            continue
        src = Path(change["rollout_path"])
        if not src.exists():
            continue
        backup_name = f"{change['thread_id']}.jsonl"
        shutil.copy2(src, rollouts_dir / backup_name)
        manifest["rollouts"].append(
            {
                "thread_id": change["thread_id"],
                "rollout_path": str(src),
                "backup": f"rollouts/{backup_name}",
                "db_provider": change["db_provider"],
                "rollout_provider": change["rollout_provider"],
            }
        )

    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return backup_dir


def update_state_db(home: Path, target: str, changes: list[dict]) -> int:
    state_db = home / "state_5.sqlite"
    ids = [c["thread_id"] for c in changes if not c["skipped"]]
    if not ids:
        return 0
    with sqlite3.connect(str(state_db)) as con:
        con.executemany(
            "UPDATE threads SET model_provider = ? WHERE id = ?",
            [(target, thread_id) for thread_id in ids],
        )
        con.commit()
    return len(ids)


def update_catalog_db(home: Path, target: str, changes: list[dict]) -> int:
    catalog_db = home / "sqlite" / "codex-dev.db"
    ids = [c["thread_id"] for c in changes if not c["skipped"]]
    if not catalog_db.exists() or not ids:
        return 0
    with sqlite3.connect(str(catalog_db)) as con:
        row = con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='local_thread_catalog'"
        ).fetchone()
        if not row:
            return 0
        con.executemany(
            "UPDATE local_thread_catalog SET model_provider = ? WHERE thread_id = ?",
            [(target, thread_id) for thread_id in ids],
        )
        con.commit()
    return len(ids)


def update_rollouts(changes: list[dict], target: str) -> int:
    updated = 0
    for change in changes:
        if change["skipped"]:
            continue
        path = Path(change["rollout_path"])
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = raw.splitlines()
        out_lines = []
        changed = False
        for line in lines:
            if not line.strip():
                out_lines.append(line)
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                out_lines.append(line)
                continue
            if isinstance(obj, dict) and obj.get("type") == "session_meta":
                payload = obj.get("payload")
                if isinstance(payload, dict) and payload.get("model_provider") != target:
                    payload["model_provider"] = target
                    obj["payload"] = payload
                    changed = True
            out_lines.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        if not changed:
            continue
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            continue
        updated += 1
    return updated


def baseline_dir(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    candidates = sorted(p for p in root.iterdir() if (p / "manifest.json").exists())
    return candidates[0] if candidates else None


def restore(home: Path, backup_dir: Path | None) -> int:
    root = home / "skill-backups" / "unify-codex-history"
    target = backup_dir or baseline_dir(root)
    if target is None:
        print("No baseline backup found under", root)
        return 1
    manifest_path = target / "manifest.json"
    if not manifest_path.exists():
        print("Backup manifest missing:", manifest_path)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    state_name = manifest.get("state_db_backup")
    if state_name:
        src = target / state_name
        dst = home / "state_5.sqlite"
        if src.exists():
            shutil.copy2(src, dst)
            print("Restored", dst)

    catalog_name = manifest.get("catalog_db_backup")
    if catalog_name:
        src = target / catalog_name
        dst = home / "sqlite" / catalog_name
        if src.exists():
            shutil.copy2(src, dst)
            print("Restored", dst)

    restored_rollouts = 0
    for item in manifest.get("rollouts", []):
        src = target / item["backup"]
        dst = Path(item["rollout_path"])
        if src.exists():
            shutil.copy2(src, dst)
            restored_rollouts += 1
    print(f"Restored {restored_rollouts} rollout file(s) from {target}")
    return 0


def print_report(target: str, changes: list[dict]) -> None:
    changed = [c for c in changes if not c["skipped"]]
    skipped = [c for c in changes if c["skipped"]]
    print(f"Current provider: {target}")
    print(f"Threads to relabel: {len(changed)}")
    print(f"Locked threads skipped: {len(skipped)}")
    for item in changed[:20]:
        print(
            f"  {item['thread_id']}  db={item['db_provider']!r} "
            f"rollout={item['rollout_provider']!r}  {item['source']}"
        )
    if len(changed) > 20:
        print(f"  ... and {len(changed) - 20} more")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Show pending changes without writing (default)")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--restore", action="store_true", help="Restore the oldest baseline backup")
    parser.add_argument("--provider", help="Target provider id (default: from config.toml)")
    parser.add_argument("--backup-dir", type=Path, help="Specific backup dir to restore")
    args = parser.parse_args()

    home = codex_home()
    if args.restore:
        return restore(home, args.backup_dir)

    target = args.provider or current_provider(home)
    rows = discover_threads(home / "state_5.sqlite")
    changes = compute_changes(rows, target, home)
    print_report(target, changes)

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to perform the sync.")
        return 0

    backup_dir = backup_snapshot(home, target, changes)
    print("\nBaseline backup created:", backup_dir)

    state_count = update_state_db(home, target, changes)
    catalog_count = update_catalog_db(home, target, changes)
    rollout_count = update_rollouts(changes, target)

    print(f"Updated state DB threads: {state_count}")
    print(f"Updated local thread catalog rows: {catalog_count}")
    print(f"Updated rollout files: {rollout_count}")
    print("\nRestart Codex (or toggle the sidebar view) if threads do not appear.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
