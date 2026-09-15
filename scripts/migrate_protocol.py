#!/usr/bin/env python3
"""Repair local Codex rollout compatibility after switching model providers."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


THREAD_ID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")

# Bump whenever transform() changes, so cached clean verdicts are re-checked.
SCAN_RULES_VERSION = 2

# Item types the Responses API requires a call_id on in both directions.
TOOL_ITEM_TYPES = (
    "function_call",
    "function_call_output",
    "custom_tool_call",
    "custom_tool_call_output",
)


@dataclass
class Change:
    path: Path
    rows: list[dict]
    assistant_ids: int
    reasoning_items: int
    locked: bool = False
    truncated: bool = False
    structural: bool = False
    orphan_tool_items: int = 0

    @property
    def writable(self) -> bool:
        return not (self.locked or self.truncated)


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


def transform(rows: list[dict], target: str = "gpt") -> tuple[list[dict], int, int, int]:
    output: list[dict] = []
    assistant_ids = 0
    reasoning_items = 0
    orphan_tool_items = 0
    linked_ids: dict[str, str] = {}

    for row in rows:
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            output.append(row)
            continue
        if row.get("type") == "response_item" and payload.get("type") in TOOL_ITEM_TYPES:
            # Every provider requires call_id on a tool call and on its output.
            # A row written without one cannot be replayed at all, so it is
            # dropped instead of failing the whole conversation.
            if not payload.get("call_id"):
                orphan_tool_items += 1
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

    for row in output:
        payload = row.get("payload") if isinstance(row, dict) else None
        if not isinstance(payload, dict) or row.get("type") != "event_msg":
            continue
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "AgentMessage" and item.get("id") in linked_ids:
            item["id"] = linked_ids[item["id"]]
    return output, assistant_ids, reasoning_items, orphan_tool_items


def _parse_line(line: str, path: Path, number: int) -> dict:
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSONL: {path}:{number}: {exc}") from exc


def _escape_raw_controls(text: str) -> tuple[str, bool]:
    """Escape control characters that appear inside JSON strings.

    Codex rollouts are appended as one JSON object per line, but a row written
    from a multi-line tool output can end up with raw newlines inside a string.
    Such a row spans several physical lines and makes both a line-based reader
    and a strict JSON reader fail, even though the content is recoverable.
    """

    out: list[str] = []
    in_string = False
    escaped = False
    changed = False
    for char in text:
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            out.append(char)
            escaped = False
            continue
        if char == "\\":
            out.append(char)
            escaped = True
            continue
        if char == '"':
            out.append(char)
            in_string = False
            continue
        if ord(char) < 0x20:
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(char, f"\\u{ord(char):04x}"))
            changed = True
            continue
        out.append(char)
    return "".join(out), changed


def _decode_stream(text: str) -> list[dict]:
    """Decode concatenated JSON values, allowing rows to span physical lines."""

    decoder = json.JSONDecoder()
    rows: list[dict] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t\r\n":
            index += 1
        if index >= length:
            break
        value, index = decoder.raw_decode(text, index)
        if not isinstance(value, dict):
            raise ValueError("rollout row is not a JSON object")
        rows.append(value)
    return rows


@dataclass
class RolloutRead:
    rows: list[dict]
    truncated: bool = False
    structural: bool = False
    unreadable: str | None = None


def read_rollout(path: Path, tolerate_truncated_tail: bool = False) -> RolloutRead:
    """Read one rollout, reporting truncation, structural damage or unreadability.

    A partial final line (a live writer mid-append) is reported as `truncated`.
    A file whose rows are not one-per-line is reported as `structural` so it can
    be rewritten as canonical JSONL. Anything else is `unreadable` and is never
    rewritten.
    """

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    strict_rows: list[dict] = []
    truncated = False
    strict_ok = True
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            strict_rows.append(json.loads(line))
        except json.JSONDecodeError:
            if tolerate_truncated_tail and number == len(lines):
                truncated = True
                continue
            strict_ok = False
            break
    if strict_ok:
        return RolloutRead(rows=strict_rows, truncated=truncated)

    escaped, changed = _escape_raw_controls(text)
    try:
        rows = _decode_stream(escaped)
    except (json.JSONDecodeError, ValueError) as exc:
        return RolloutRead(rows=[], unreadable=f"{type(exc).__name__}: {exc}")
    if not rows:
        return RolloutRead(rows=[], unreadable="no decodable rows")
    return RolloutRead(rows=rows, structural=changed)


def read_jsonl_rows(
    path: Path, tolerate_truncated_tail: bool = False
) -> tuple[list[dict], bool]:
    read = read_rollout(path, tolerate_truncated_tail)
    return read.rows, read.truncated


def read_jsonl(path: Path) -> list[dict]:
    rows, _ = read_jsonl_rows(path)
    return rows


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
    result = subprocess.run(
        [str(binary), "-t", str(path), str(lock)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


@dataclass
class PlanResult:
    changes: list[Change]
    unreadable: list[tuple[Path, str]]


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


class ScanCache:
    """Remember rollouts already verified clean so a rescan stays cheap.

    Reading every local rollout costs seconds of CPU per scan. A file that has
    not changed since it was verified clean cannot need repair, so its recorded
    signature lets the next scan skip it. Any append changes the size or mtime
    and forces a fresh read. Skipping is never applied to a file a writer holds
    or to one whose read was truncated.
    """

    def __init__(self, path: Path, target: str) -> None:
        self.path = path
        self.target = target
        self.entries: dict[str, tuple[int, int]] = {}
        self.dirty = False
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if (
            data.get("target") != self.target
            or data.get("rules") != SCAN_RULES_VERSION
        ):
            return
        for key, value in (data.get("clean") or {}).items():
            if isinstance(value, list) and len(value) == 2:
                try:
                    self.entries[key] = (int(value[0]), int(value[1]))
                except (TypeError, ValueError):
                    continue

    def is_clean(self, path: Path, signature: tuple[int, int] | None) -> bool:
        if signature is None:
            return False
        return self.entries.get(str(path)) == signature

    def mark_clean(self, path: Path, signature: tuple[int, int] | None) -> None:
        if signature is None:
            return
        if self.entries.get(str(path)) != signature:
            self.entries[str(path)] = signature
            self.dirty = True

    def forget(self, path: Path) -> None:
        if self.entries.pop(str(path), None) is not None:
            self.dirty = True

    def prune(self, seen: set[str]) -> None:
        for key in [key for key in self.entries if key not in seen]:
            del self.entries[key]
            self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "target": self.target,
            "rules": SCAN_RULES_VERSION,
            "clean": {key: list(value) for key, value in sorted(self.entries.items())},
        }
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temp, self.path)
        self.dirty = False


def plan(
    sessions_dir: Path,
    lock_dir: Path,
    target: str = "gpt",
    lsof_bin: Path | None = None,
    cache: ScanCache | None = None,
) -> PlanResult:
    """Plan repairs for every rollout, marking entries that must not be written.

    Files a live writer holds, and files with an unparsable final line, are
    reported with `locked`/`truncated` set so a caller can decide whether the
    app has to be restarted before the repair can be applied. Files whose rows
    are not one-per-line are reported as `structural` because rewriting them as
    canonical JSONL is itself the repair. Files that cannot be decoded at all
    are listed separately so one damaged conversation never blocks the rest.
    """

    changes: list[Change] = []
    unreadable: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path in sorted(sessions_dir.rglob("*.jsonl")):
        seen.add(str(path))
        locked = writer_in_use(path, lock_dir, lsof_bin)
        if cache is not None and not locked and cache.is_clean(path, _signature(path)):
            continue
        read = read_rollout(path, tolerate_truncated_tail=locked)
        if read.unreadable:
            unreadable.append((path, read.unreadable))
            if cache is not None:
                cache.forget(path)
            continue
        migrated, ids, reasoning, orphans = transform(read.rows, target)
        if ids or reasoning or orphans or read.structural:
            if cache is not None:
                cache.forget(path)
            changes.append(
                Change(
                    path,
                    migrated,
                    ids,
                    reasoning,
                    locked=locked,
                    truncated=read.truncated,
                    structural=read.structural,
                    orphan_tool_items=orphans,
                )
            )
        elif cache is not None and not locked and not read.truncated:
            cache.mark_clean(path, _signature(path))
    if cache is not None:
        cache.prune(seen)
    return PlanResult(changes=changes, unreadable=unreadable)


def scan(
    sessions_dir: Path,
    lock_dir: Path,
    target: str = "gpt",
    lsof_bin: Path | None = None,
    cache: ScanCache | None = None,
) -> tuple[list[Change], int]:
    planned = plan(sessions_dir, lock_dir, target, lsof_bin, cache=cache)
    writable = [change for change in planned.changes if change.writable]
    return writable, len(planned.changes) - len(writable)


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
    parser.add_argument("--lock-dir", type=Path, default=home / "thread-writer-locks")
    parser.add_argument("--lsof-bin", type=Path)
    parser.add_argument(
        "--scan-cache",
        type=Path,
        default=home / "switch-guard" / "scan-cache.json",
    )
    parser.add_argument("--keep", type=int, default=3)
    args = parser.parse_args()

    if args.restore_latest:
        return restore_latest(args.sessions_dir, args.backup_root)
    target = detect_target(home) if args.target == "auto" else args.target
    cache = ScanCache(args.scan_cache, target)
    planned = plan(args.sessions_dir, args.lock_dir, target, args.lsof_bin, cache=cache)
    changes = [change for change in planned.changes if change.writable]
    skipped = len(planned.changes) - len(changes)
    print(f"target={target}")
    print(f"files_to_change={len(changes)}")
    print(f"assistant_ids={sum(c.assistant_ids for c in changes)}")
    print(f"reasoning_items={sum(c.reasoning_items for c in changes)}")
    print(f"orphan_tool_items={sum(c.orphan_tool_items for c in changes)}")
    print(f"locked_files_skipped={skipped}")
    print(f"structural_repairs={sum(1 for c in changes if c.structural)}")
    print(f"unreadable_files={len(planned.unreadable)}")
    for path, reason in planned.unreadable[:5]:
        print(f"warning=unreadable_rollout:{path.name}:{reason}", file=sys.stderr)
    if not args.apply:
        cache.save()
        print("dry_run=true")
        return 0
    if not changes:
        cache.save()
        print("changed=0")
        return 0

    archive = create_backup(changes, args.sessions_dir, args.backup_root)
    for change in changes:
        write_rows(change.path, change.rows)
    cache.save()
    remaining, _ = scan(args.sessions_dir, args.lock_dir, target, args.lsof_bin, cache=cache)
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
