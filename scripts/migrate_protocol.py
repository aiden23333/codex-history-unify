#!/usr/bin/env python3
"""Repair local Codex rollout compatibility after switching model providers."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


THREAD_ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


@dataclass
class Change:
    path: Path
    rows: list[dict]
    assistant_ids: int
    reasoning_items: int


def default_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def detect_target(home: Path) -> str:
    config = home / "config.toml"
    if not config.exists():
        return "gpt"
    with config.open("rb") as fh:
        model = str(tomllib.load(fh).get("model", "")).lower()
    return "deepseek" if "deepseek" in model else "gpt"


def normalize_message_id(value: str) -> str:
    if value.startswith("msg"):
        return value
    if value.startswith("resp_") and value.endswith("_msg"):
        return "msg_" + value[5:-4]
    return "msg_" + value


def is_incompatible_reasoning(payload: dict) -> bool:
    content = payload.get("content")
    if isinstance(content, list) and content:
        return True
    item_id = payload.get("id")
    return (
        isinstance(item_id, str)
        and item_id.startswith("rs_resp_")
        and payload.get("encrypted_content") is None
    )


def transform(rows: list[dict]) -> tuple[list[dict], int, int]:
    output: list[dict] = []
    assistant_ids = 0
    reasoning_items = 0
    linked_ids: dict[str, str] = {}

    for row in rows:
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            output.append(row)
            continue
        if row.get("type") == "response_item" and payload.get("type") == "reasoning" and is_incompatible_reasoning(payload):
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

    for row in output:
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict) or row.get("type") != "event_msg":
            continue
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "AgentMessage" and item.get("id") in linked_ids:
            item["id"] = linked_ids[item["id"]]
    return output, assistant_ids, reasoning_items


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL: {path}:{number}: {exc}") from exc
    return rows


def writer_locked(path: Path, lock_dir: Path) -> bool:
    match = THREAD_ID_RE.search(path.name)
    return bool(match and (lock_dir / f"{match.group(1)}.lock").exists())


def scan(sessions_dir: Path, lock_dir: Path) -> tuple[list[Change], int]:
    changes: list[Change] = []
    skipped = 0
    for path in sorted(sessions_dir.rglob("*.jsonl")):
        if writer_locked(path, lock_dir):
            skipped += 1
            continue
        rows = read_jsonl(path)
        migrated, ids, reasoning = transform(rows)
        if ids or reasoning:
            changes.append(Change(path, migrated, ids, reasoning))
    return changes, skipped


def create_backup(changes: list[Change], sessions_dir: Path, backup_root: Path) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    archive_path = backup_root / f"protocol-{stamp}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for change in changes:
            archive.add(change.path, arcname=change.path.relative_to(sessions_dir))
    return archive_path


def write_rows(path: Path, rows: list[dict]) -> None:
    encoded = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False) as fh:
        temp_path = Path(fh.name)
        fh.write(encoded)
    try:
        read_jsonl(temp_path)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def prune_backups(backup_root: Path, keep: int) -> int:
    archives = sorted(backup_root.glob("protocol-*.tar.gz"), reverse=True)
    removed = 0
    for path in archives[max(keep, 1):]:
        path.unlink()
        removed += 1
    return removed


def restore_latest(sessions_dir: Path, backup_root: Path) -> int:
    archives = sorted(backup_root.glob("protocol-*.tar.gz"), reverse=True)
    if not archives:
        print("No protocol backup found", file=sys.stderr)
        return 1
    root = sessions_dir.resolve()
    with tarfile.open(archives[0], "r:gz") as archive:
        for member in archive.getmembers():
            destination = (root / member.name).resolve()
            if root not in destination.parents:
                print(f"Unsafe backup member: {member.name}", file=sys.stderr)
                return 1
        archive.extractall(root, filter="data")
    print(f"restored={archives[0]}")
    return 0


def main() -> int:
    home = default_home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("auto", "gpt", "deepseek"), default="auto")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--restore-latest", action="store_true")
    parser.add_argument("--sessions-dir", type=Path, default=home / "sessions")
    parser.add_argument("--backup-root", type=Path, default=home / "skill-backups" / "unify-codex-history" / "protocol")
    parser.add_argument("--keep", type=int, default=3)
    args = parser.parse_args()

    if args.restore_latest:
        return restore_latest(args.sessions_dir, args.backup_root)
    target = detect_target(home) if args.target == "auto" else args.target
    if target == "deepseek":
        print("diagnostic_only: GPT-to-DeepSeek conversion is error-specific; no files changed")
        return 2 if args.apply else 0

    changes, skipped = scan(args.sessions_dir, home / "thread-writer-locks")
    print(f"target={target}")
    print(f"files_to_change={len(changes)}")
    print(f"assistant_ids={sum(c.assistant_ids for c in changes)}")
    print(f"reasoning_items={sum(c.reasoning_items for c in changes)}")
    print(f"locked_files_skipped={skipped}")
    if not args.apply:
        print("dry_run=true")
        return 0
    if not changes:
        print("changed=0")
        return 0

    archive = create_backup(changes, args.sessions_dir, args.backup_root)
    for change in changes:
        write_rows(change.path, change.rows)
    remaining, _ = scan(args.sessions_dir, home / "thread-writer-locks")
    if remaining:
        print(f"verification_failed={len(remaining)}", file=sys.stderr)
        return 1
    removed = prune_backups(args.backup_root, args.keep)
    print(f"backup={archive}")
    print(f"changed={len(changes)}")
    print(f"old_backups_pruned={removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
