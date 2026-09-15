#!/usr/bin/env python3
"""Watch CC Switch state and reconcile Codex before the first usable launch."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

try:
    from .codex_switch_preflight import (
        ReconcileReport,
        TargetSnapshot,
        read_stable_snapshot,
        reconcile,
    )
except ImportError:
    from codex_switch_preflight import (  # type: ignore
        ReconcileReport,
        TargetSnapshot,
        read_stable_snapshot,
        reconcile,
    )


class ProcessProbe(Protocol):
    def codex_rollouts_open(self) -> bool: ...


class AppController(Protocol):
    def quit_codex(self) -> None: ...
    def open_codex(self) -> None: ...


@dataclass(frozen=True)
class GuardDecision:
    action: str
    fingerprint: str


class LsofProcessProbe:
    def __init__(self, codex_home: Path) -> None:
        self.codex_home = codex_home

    def codex_rollouts_open(self) -> bool:
        sessions = self.codex_home / "sessions"
        if not sessions.exists():
            return False
        result = subprocess.run(
            ["/usr/sbin/lsof", "-t", "+D", str(sessions)],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())


class MacAppController:
    def quit_codex(self) -> None:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                'tell application id "com.openai.codex" to quit',
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def open_codex(self) -> None:
        subprocess.run(
            ["/usr/bin/open", "-b", "com.openai.codex"],
            check=True,
            capture_output=True,
            text=True,
        )


class Guard:
    def __init__(
        self,
        *,
        codex_home: Path,
        cc_home: Path,
        state_path: Path,
        process_probe: ProcessProbe,
        app_controller: AppController,
        snapshot_reader: Callable[..., TargetSnapshot] = read_stable_snapshot,
        reconciler: Callable[..., ReconcileReport] = reconcile,
        close_timeout: float = 20.0,
    ) -> None:
        self.codex_home = codex_home
        self.cc_home = cc_home
        self.state_path = state_path
        self.process_probe = process_probe
        self.app_controller = app_controller
        self.snapshot_reader = snapshot_reader
        self.reconciler = reconciler
        self.close_timeout = close_timeout

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_name(self.state_path.name + ".tmp")
        temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, self.state_path)

    def _wait_until_closed(self) -> None:
        deadline = time.monotonic() + self.close_timeout
        while self.process_probe.codex_rollouts_open():
            if time.monotonic() >= deadline:
                raise RuntimeError("Codex rollout descriptors did not close")
            time.sleep(0.1)

    def run_once(self) -> GuardDecision:
        snapshot = self.snapshot_reader(
            self.codex_home, self.cc_home, stable_delay=0.8
        )
        state = self._load_state()
        if state.get("last_successful_fingerprint") == snapshot.fingerprint:
            return GuardDecision("noop", snapshot.fingerprint)
        planned = self.reconciler(
            snapshot, self.codex_home, self.cc_home, apply=False
        )
        if not planned.changed:
            state["last_successful_fingerprint"] = snapshot.fingerprint
            state.pop("last_error", None)
            self._save_state(state)
            return GuardDecision("noop", snapshot.fingerprint)

        restart = self.process_probe.codex_rollouts_open()
        if restart:
            if state.get("restart_attempted_fingerprint") == snapshot.fingerprint:
                raise RuntimeError("automatic restart already attempted for this switch")
            state["restart_attempted_fingerprint"] = snapshot.fingerprint
            self._save_state(state)
            self.app_controller.quit_codex()
            self._wait_until_closed()
        try:
            self.reconciler(snapshot, self.codex_home, self.cc_home, apply=True)
        except Exception as exc:
            state["last_error"] = type(exc).__name__
            self._save_state(state)
            raise
        state["last_successful_fingerprint"] = snapshot.fingerprint
        state.pop("last_error", None)
        self._save_state(state)
        if restart:
            self.app_controller.open_codex()
            return GuardDecision("restarted_after_reconcile", snapshot.fingerprint)
        return GuardDecision("reconciled", snapshot.fingerprint)


def _watch_signature(codex_home: Path, cc_home: Path) -> tuple:
    paths = [
        codex_home / "config.toml",
        codex_home / "cc-switch-model-catalog.json",
        cc_home / "settings.json",
        cc_home / "cc-switch.db",
        cc_home / "cc-switch.db-wal",
    ]
    values = []
    for path in paths:
        try:
            stat = path.stat()
            values.append((str(path), stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            values.append((str(path), None, None))
    return tuple(values)


def run_forever(guard: Guard, poll_seconds: float = 0.5) -> None:
    previous = None
    retries = (0.5, 1.0, 2.0, 4.0)
    while True:
        current = _watch_signature(guard.codex_home, guard.cc_home)
        if current != previous:
            previous = current
            for retry_delay in (0.0, *retries):
                if retry_delay:
                    time.sleep(retry_delay)
                try:
                    decision = guard.run_once()
                    print(
                        f"action={decision.action} fingerprint={decision.fingerprint[:12]}",
                        flush=True,
                    )
                    break
                except Exception as exc:
                    print(f"guard_error={type(exc).__name__}", file=sys.stderr, flush=True)
        time.sleep(poll_seconds)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--cc-home", type=Path, default=Path.home() / ".cc-switch")
    args = parser.parse_args()
    state = args.codex_home / "switch-guard" / "state.json"
    guard = Guard(
        codex_home=args.codex_home,
        cc_home=args.cc_home,
        state_path=state,
        process_probe=LsofProcessProbe(args.codex_home),
        app_controller=MacAppController(),
    )
    if args.once:
        decision = guard.run_once()
        print(f"action={decision.action} fingerprint={decision.fingerprint}")
        return 0
    run_forever(guard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
