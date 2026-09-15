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
    def codex_app_running(self) -> bool: ...
    def codex_app_uptime_seconds(self) -> float | None: ...
    def codex_rollouts_open(self) -> bool: ...


class AppController(Protocol):
    def quit_codex(self) -> None: ...
    def open_codex(self) -> None: ...


@dataclass(frozen=True)
class GuardDecision:
    action: str
    fingerprint: str
    unreadable: int = 0


class CodexProcessProbe:
    """Report whether the Codex desktop app owns local rollout files."""

    APP_ID = "com.openai.codex"
    APP_MARKER = "/ChatGPT.app/Contents/MacOS/"

    def __init__(self, codex_home: Path) -> None:
        self.codex_home = codex_home

    def codex_app_running(self) -> bool:
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", f'application id "{self.APP_ID}" is running'],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip().lower() == "true"
        # LaunchServices refused to answer; fall back to file handles so an
        # unreadable app state can never hide a pending repair.
        return self.codex_rollouts_open()

    def codex_app_uptime_seconds(self) -> float | None:
        """How long the desktop app has been running, or None when unknown.

        The value distinguishes the launch race (the user opened Codex seconds
        after a switch, so one automatic bounce is expected) from a long session
        that a repair must never interrupt.
        """

        result = subprocess.run(
            ["/bin/ps", "-Ao", "etime=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if self.APP_MARKER not in line:
                continue
            elapsed = line.strip().split(" ", 1)[0]
            seconds = _parse_elapsed(elapsed)
            if seconds is not None:
                return seconds
        return None

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


LsofProcessProbe = CodexProcessProbe


def _parse_elapsed(value: str) -> float | None:
    """Parse a `ps etime` value such as `07:12`, `01:07:12` or `2-01:07:12`."""

    days = 0.0
    text = value.strip()
    if "-" in text:
        day_part, _, text = text.partition("-")
        try:
            days = float(day_part)
        except ValueError:
            return None
    parts = text.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
    elif len(numbers) == 2:
        hours, (minutes, seconds) = 0.0, numbers
    else:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


class MacAppController:
    APP_ID = CodexProcessProbe.APP_ID
    APP_MARKERS = "ChatGPT.app/Contents/MacOS/|/Contents/Resources/codex"

    def quit_codex(self) -> None:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                f'tell application id "{self.APP_ID}" to quit',
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def force_quit_codex(self) -> None:
        subprocess.run(
            ["/usr/bin/pkill", "-TERM", "-f", self.APP_MARKERS],
            check=False,
            capture_output=True,
            text=True,
        )

    def open_codex(self) -> None:
        subprocess.run(
            ["/usr/bin/open", "-b", self.APP_ID],
            check=False,
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
        fresh_app_seconds: float = 180.0,
    ) -> None:
        self.codex_home = codex_home
        self.cc_home = cc_home
        self.state_path = state_path
        self.process_probe = process_probe
        self.app_controller = app_controller
        self.snapshot_reader = snapshot_reader
        self.reconciler = reconciler
        self.close_timeout = close_timeout
        self.fresh_app_seconds = fresh_app_seconds

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

    def _wait_for(self, predicate: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _close_codex(self) -> None:
        """Quit Codex, escalating only when the graceful request is ignored."""

        self.app_controller.quit_codex()
        if self._wait_for(
            lambda: not self.process_probe.codex_app_running(), self.close_timeout
        ):
            return
        force_quit = getattr(self.app_controller, "force_quit_codex", None)
        if force_quit is not None:
            force_quit()
        if not self._wait_for(
            lambda: not self.process_probe.codex_app_running(), self.close_timeout
        ):
            raise RuntimeError("Codex did not quit after a graceful request")

    def _wait_until_closed(self) -> None:
        """Wait for rollout handles to drop; a foreign handle must not block us.

        Reconciliation never writes a rollout another process holds, so a
        handle that outlives the app is reported as a deferred repair instead of
        turning into a restart loop.
        """

        self._wait_for(
            lambda: not self.process_probe.codex_rollouts_open(), self.close_timeout
        )

    def plan_once(self) -> tuple[GuardDecision, ReconcileReport]:
        """Report what a switch needs without touching the app or any file."""

        snapshot = self._read_snapshot()
        planned = self.reconciler(
            snapshot, self.codex_home, self.cc_home, apply=False
        )
        if not planned.changed:
            return GuardDecision("noop", snapshot.fingerprint), planned
        if not self.process_probe.codex_app_running():
            return GuardDecision("would_reconcile", snapshot.fingerprint), planned
        if self._restart_allowed(planned):
            return GuardDecision(
                "would_restart_and_reconcile", snapshot.fingerprint
            ), planned
        return GuardDecision("would_defer_until_close", snapshot.fingerprint), planned

    def _restart_allowed(self, planned: ReconcileReport) -> bool:
        """Only bounce an app that the user just launched, and only when idle.

        Quitting Codex kills every running task on this machine, so the guard
        restricts the automatic bounce to the switch race it was built for.
        """

        if getattr(planned, "busy", False):
            return False
        uptime = self.process_probe.codex_app_uptime_seconds()
        if uptime is None:
            return False
        return uptime <= self.fresh_app_seconds

    def _read_snapshot(self) -> TargetSnapshot:
        return self.snapshot_reader(self.codex_home, self.cc_home, stable_delay=0.8)

    def run_once(self) -> GuardDecision:
        snapshot = self._read_snapshot()
        state = self._load_state()
        # The plan decides whether work is needed. A changed target is not the
        # only trigger: a later turn can write a broken item while the selected
        # provider stays the same, and that damage must still be repaired.
        planned = self.reconciler(
            snapshot, self.codex_home, self.cc_home, apply=False
        )
        unreadable = int(getattr(planned, "unreadable", 0) or 0)
        if not planned.changed:
            state["last_successful_fingerprint"] = snapshot.fingerprint
            state.pop("last_error", None)
            self._save_state(state)
            return GuardDecision("noop", snapshot.fingerprint, unreadable)

        restarted = False
        if self.process_probe.codex_app_running():
            if not self._restart_allowed(planned):
                # The app has been running for a while or a turn is in flight.
                # Bouncing it would destroy the user's work for no gain: the
                # repairs are applied when Codex is next closed.
                state["last_error"] = "deferred_app_running"
                state["pending_fingerprint"] = snapshot.fingerprint
                self._save_state(state)
                return GuardDecision("deferred", snapshot.fingerprint, unreadable)
            if state.get("restart_attempted_fingerprint") == snapshot.fingerprint:
                # One automatic bounce per switch. Anything still pending waits
                # for the app to close instead of restarting again or erroring.
                state["last_error"] = "restart_already_attempted"
                state["pending_fingerprint"] = snapshot.fingerprint
                self._save_state(state)
                return GuardDecision("deferred", snapshot.fingerprint, unreadable)
            state["restart_attempted_fingerprint"] = snapshot.fingerprint
            self._save_state(state)
            self._close_codex()
            self._wait_until_closed()
            restarted = True

        try:
            if restarted:
                # The app can rewrite its config while closing, so repair the
                # state that is actually on disk now, not the pre-quit snapshot.
                snapshot = self._read_snapshot()
            report = self.reconciler(
                snapshot, self.codex_home, self.cc_home, apply=True
            )
        except Exception as exc:
            state["last_error"] = type(exc).__name__
            self._save_state(state)
            if restarted:
                self.app_controller.open_codex()
            raise

        deferred = int(getattr(report, "deferred", 0) or 0)
        if deferred:
            state["last_error"] = "deferred"
            state["deferred_fingerprint"] = snapshot.fingerprint
            self._save_state(state)
            if restarted:
                self.app_controller.open_codex()
                return GuardDecision(
                    "deferred_after_restart", snapshot.fingerprint, unreadable
                )
            return GuardDecision("deferred", snapshot.fingerprint, unreadable)

        state["last_successful_fingerprint"] = snapshot.fingerprint
        state.pop("last_error", None)
        state.pop("deferred_fingerprint", None)
        state.pop("pending_fingerprint", None)
        self._save_state(state)
        if restarted:
            self.app_controller.open_codex()
            return GuardDecision(
                "restarted_after_reconcile", snapshot.fingerprint, unreadable
            )
        return GuardDecision("reconciled", snapshot.fingerprint, unreadable)


# CC Switch checkpoints its database continuously, which bumps the file mtime
# without changing any relevant value. Size-only watching for the database
# keeps the guard idle instead of re-planning on every checkpoint.
_MTIME_WATCHED = ("config.toml", "cc-switch-model-catalog.json", "settings.json")
_SIZE_WATCHED = ("cc-switch.db", "cc-switch.db-wal", "cc-switch.db-shm")


def _stamp() -> str:
    """Local timestamp so a log entry can be placed on a real timeline."""

    return time.strftime("%Y-%m-%d %H:%M:%S")


def _watch_signature(codex_home: Path, cc_home: Path) -> tuple:
    values = []
    for name in _MTIME_WATCHED:
        path = (
            codex_home / name
            if name != "settings.json"
            else cc_home / name
        )
        try:
            stat = path.stat()
            values.append((str(path), stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            values.append((str(path), None, None))
    for name in _SIZE_WATCHED:
        path = cc_home / name
        try:
            values.append((str(path), path.stat().st_size, None))
        except FileNotFoundError:
            values.append((str(path), None, None))
    return tuple(values)


def run_forever(
    guard: Guard,
    poll_seconds: float = 0.5,
    pending_poll_seconds: float = 20.0,
    max_pending_poll_seconds: float = 300.0,
    app_poll_seconds: float = 2.0,
) -> None:
    """Watch for switch-state changes and finish deferred repairs off-peak.

    A repair blocked while Codex runs is retried when the app closes (the app
    state transition is watched directly). A repair blocked by something else
    (a rollout another process still holds) is retried with exponential backoff
    instead of being abandoned, so the next launch is still healthy without the
    user running anything by hand.
    """

    previous = None
    logged_error = None
    pending_delay: float | None = None
    app_running: bool | None = None
    app_polled_at = 0.0
    print(
        f"{_stamp()} guard_started pid={os.getpid()} poll={poll_seconds}s",
        flush=True,
    )
    retries = (0.5, 1.0, 2.0, 4.0)
    while True:
        current = _watch_signature(guard.codex_home, guard.cc_home)
        now = time.monotonic()
        switched_app_state = False
        if app_running is None or now - app_polled_at >= app_poll_seconds:
            running = guard.process_probe.codex_app_running()
            app_polled_at = now
            switched_app_state = app_running is not None and running != app_running
            app_running = running
        if current != previous or pending_delay is not None or switched_app_state:
            previous = current
            for retry_delay in (0.0, *retries):
                if retry_delay:
                    time.sleep(retry_delay)
                try:
                    decision = guard.run_once()
                except Exception as exc:
                    # A persistent condition (for example a rollout a foreign
                    # process still holds) must not flood the log on every retry.
                    if type(exc).__name__ != logged_error:
                        logged_error = type(exc).__name__
                        print(
                            f"{_stamp()} guard_error={type(exc).__name__}: {exc}",
                            file=sys.stderr,
                            flush=True,
                        )
                    break
                logged_error = None
                detail = (
                    f" unreadable={decision.unreadable}"
                    if decision.unreadable
                    else ""
                )
                print(
                    f"{_stamp()} action={decision.action}"
                    f" fingerprint={decision.fingerprint[:12]}{detail}",
                    flush=True,
                )
                if decision.action.startswith("deferred") and not app_running:
                    pending_delay = min(
                        (pending_delay or pending_poll_seconds / 2) * 2,
                        max_pending_poll_seconds,
                    )
                else:
                    pending_delay = None
                break
        time.sleep(poll_seconds if pending_delay is None else pending_delay)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="print the planned action without changing the app or any file",
    )
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--cc-home", type=Path, default=Path.home() / ".cc-switch")
    args = parser.parse_args()
    state = args.codex_home / "switch-guard" / "state.json"
    guard = Guard(
        codex_home=args.codex_home,
        cc_home=args.cc_home,
        state_path=state,
        process_probe=CodexProcessProbe(args.codex_home),
        app_controller=MacAppController(),
    )
    if args.plan_only:
        decision, planned = guard.plan_once()
        print(f"action={decision.action} fingerprint={decision.fingerprint[:12]}")
        print(f"changed={int(planned.changed)}")
        print(f"deferred={planned.deferred}")
        print(f"busy={int(planned.busy)}")
        print(f"protocol_files={planned.protocol_files}")
        print(f"provider_threads={planned.provider_threads}")
        print(f"cc_switch_changed={int(planned.cc_switch_changed)}")
        print(f"catalog_changed={int(planned.catalog_changed)}")
        print(f"unreadable={planned.unreadable}")
        print(f"orphan_tool_items={planned.orphan_tool_items}")
        return 0
    if args.once:
        decision = guard.run_once()
        print(f"action={decision.action} fingerprint={decision.fingerprint}")
        return 0
    run_forever(guard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
