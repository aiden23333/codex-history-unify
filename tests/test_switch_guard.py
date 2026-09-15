import json
import tempfile
import unittest
from pathlib import Path

from scripts.codex_switch_guard import (
    CodexProcessProbe,
    Guard,
    _parse_elapsed,
    _stamp,
    run_forever,
)
from scripts.codex_switch_preflight import ReconcileReport, TargetSnapshot


class FakeProbe:
    def __init__(
        self,
        app_running: bool,
        rollouts_open: bool | None = None,
        uptime: float | None = 5.0,
    ) -> None:
        self.app_running = app_running
        self.rollouts_open = app_running if rollouts_open is None else rollouts_open
        self.uptime = uptime

    def codex_app_running(self) -> bool:
        return self.app_running

    def codex_app_uptime_seconds(self) -> float | None:
        return self.uptime

    def codex_rollouts_open(self) -> bool:
        return self.rollouts_open


class FakeApps:
    def __init__(self, probe: FakeProbe) -> None:
        self.probe = probe
        self.quit_calls = 0
        self.open_calls = 0

    def quit_codex(self) -> None:
        self.quit_calls += 1
        self.probe.app_running = False
        self.probe.rollouts_open = False

    def open_codex(self) -> None:
        self.open_calls += 1
        self.probe.app_running = True


class SwitchGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot = TargetSnapshot(
            "gpt", "codex-official", "gpt-5.6-sol", "openai", "openai_responses", "fingerprint-1"
        )
        self.apply_calls: list[bool] = []
        self.repaired = {"done": False}
        self.damage = {"active": False}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_guard(
        self,
        opened: bool,
        changed: bool = True,
        *,
        rollouts_open: bool | None = None,
        report: ReconcileReport | None = None,
    ) -> tuple[Guard, FakeApps]:
        probe = FakeProbe(opened, rollouts_open)
        apps = FakeApps(probe)

        def reader(*args, **kwargs):
            return self.snapshot

        def reconciler(snapshot, codex_home, cc_home, *, apply, scope="all"):
            self.apply_calls.append(apply)
            if report is not None:
                return report
            if apply:
                self.repaired["done"] = True
                return ReconcileReport(changed=True)
            # Mirrors the real reconciler: once repairs are applied the next
            # plan reports nothing left to do.
            pending = changed or self.damage["active"]
            return ReconcileReport(changed=pending and not self.repaired["done"])

        guard = Guard(
            codex_home=self.root / "codex",
            cc_home=self.root / "cc",
            state_path=self.root / "state.json",
            process_probe=probe,
            app_controller=apps,
            snapshot_reader=reader,
            reconciler=reconciler,
            close_timeout=0.1,
        )
        guard.probe = probe
        return guard, apps

    def state(self, guard: Guard) -> dict:
        return json.loads(guard.state_path.read_text(encoding="utf-8"))

    def test_closed_codex_reconciles_without_restart(self) -> None:
        guard, apps = self.make_guard(opened=False)
        decision = guard.run_once()
        self.assertEqual(decision.action, "reconciled")
        self.assertEqual(apps.quit_calls, 0)
        self.assertEqual(apps.open_calls, 0)
        self.assertEqual(self.apply_calls, [False, True])

    def test_launch_race_quits_and_reopens_once(self) -> None:
        guard, apps = self.make_guard(opened=True)
        first = guard.run_once()
        second = guard.run_once()
        self.assertEqual(first.action, "restarted_after_reconcile")
        self.assertEqual(second.action, "noop")
        self.assertEqual(apps.quit_calls, 1)
        self.assertEqual(apps.open_calls, 1)

    def test_needed_repairs_while_fresh_app_runs_restart_once(self) -> None:
        guard, apps = self.make_guard(opened=True)
        decision = guard.run_once()
        self.assertEqual(decision.action, "restarted_after_reconcile")
        self.assertEqual(apps.quit_calls, 1)
        self.assertEqual(apps.open_calls, 1)
        self.assertEqual(self.apply_calls, [False, True])
        self.assertEqual(self.state(guard)["last_successful_fingerprint"], "fingerprint-1")

    def test_unchanged_state_is_noop(self) -> None:
        guard, apps = self.make_guard(opened=True, changed=False)
        decision = guard.run_once()
        self.assertEqual(decision.action, "noop")
        self.assertEqual(apps.quit_calls, 0)
        self.assertEqual(apps.open_calls, 0)
        self.assertEqual(self.apply_calls, [False])

    def test_damage_that_appears_after_a_success_is_still_repaired(self) -> None:
        """A later turn can write a broken item without changing the target."""

        guard, apps = self.make_guard(opened=False, changed=False)
        self.assertEqual(guard.run_once().action, "noop")
        # The switch fingerprint is unchanged, but new damage now exists.
        self.damage["active"] = True
        decision = guard.run_once()
        self.assertEqual(decision.action, "reconciled")
        self.assertEqual(apps.quit_calls, 0)
        self.assertEqual(self.apply_calls, [False, False, True])

    def test_long_running_session_is_never_interrupted(self) -> None:
        guard, apps = self.make_guard(opened=True)
        guard.probe.uptime = 3600.0
        decision = guard.run_once()
        self.assertEqual(decision.action, "deferred")
        self.assertEqual(apps.quit_calls, 0)
        self.assertEqual(apps.open_calls, 0)
        # Nothing writable, so the guard re-plans and waits for a close.
        self.assertEqual(self.apply_calls, [False, False])
        state = self.state(guard)
        self.assertEqual(state["last_error"], "deferred_app_running")
        self.assertEqual(state["pending_fingerprint"], "fingerprint-1")
        self.assertNotIn("last_successful_fingerprint", state)

    def test_active_turn_defers_restart_instead_of_interrupting(self) -> None:
        guard, apps = self.make_guard(
            opened=True,
            report=ReconcileReport(changed=True, deferred=2, busy=True),
        )
        decision = guard.run_once()
        self.assertEqual(decision.action, "deferred")
        self.assertEqual(apps.quit_calls, 0)
        self.assertEqual(apps.open_calls, 0)
        # No writable rollout work, so the guard re-plans and waits for a close.
        self.assertEqual(self.apply_calls, [False, False])

    def test_unknown_app_uptime_never_restarts(self) -> None:
        guard, apps = self.make_guard(opened=True)
        guard.probe.uptime = None
        decision = guard.run_once()
        self.assertEqual(decision.action, "deferred")
        self.assertEqual(apps.quit_calls, 0)

    def test_foreign_file_handle_does_not_restart_app(self) -> None:
        guard, apps = self.make_guard(opened=False, rollouts_open=True)
        decision = guard.run_once()
        self.assertEqual(decision.action, "reconciled")
        self.assertEqual(apps.quit_calls, 0)
        self.assertEqual(apps.open_calls, 0)
        self.assertEqual(self.apply_calls, [False, True])

    def test_deferred_repairs_reopen_codex_without_recording_success(self) -> None:
        guard, apps = self.make_guard(
            opened=True,
            report=ReconcileReport(changed=True, deferred=1),
        )
        decision = guard.run_once()
        self.assertEqual(decision.action, "deferred_after_restart")
        self.assertEqual(apps.quit_calls, 1)
        self.assertEqual(apps.open_calls, 1)
        state = self.state(guard)
        self.assertNotIn("last_successful_fingerprint", state)
        self.assertEqual(state["last_error"], "deferred")
        self.assertEqual(state["restart_attempted_fingerprint"], "fingerprint-1")
        # The same switch never bounces twice; the repair waits for the close.
        again = guard.run_once()
        self.assertEqual(again.action, "deferred")
        self.assertEqual(apps.quit_calls, 1)

    def test_deferred_repairs_retry_while_codex_is_closed(self) -> None:
        guard, apps = self.make_guard(
            opened=False,
            report=ReconcileReport(changed=True, deferred=1),
        )
        first = guard.run_once()
        second = guard.run_once()
        self.assertEqual(first.action, "deferred")
        self.assertEqual(second.action, "deferred")
        self.assertEqual(apps.quit_calls, 0)
        self.assertEqual(apps.open_calls, 0)
        self.assertNotIn("last_successful_fingerprint", self.state(guard))
        self.assertEqual(self.apply_calls, [False, True, False, True])

    def test_failure_does_not_restart_loop(self) -> None:
        probe = FakeProbe(True)
        apps = FakeApps(probe)

        def failing(*args, **kwargs):
            if kwargs.get("apply"):
                raise RuntimeError("repair failed")
            return ReconcileReport(changed=True)

        guard = Guard(
            codex_home=self.root / "codex",
            cc_home=self.root / "cc",
            state_path=self.root / "state.json",
            process_probe=probe,
            app_controller=apps,
            snapshot_reader=lambda *a, **k: self.snapshot,
            reconciler=failing,
            close_timeout=0.1,
        )
        with self.assertRaises(RuntimeError):
            guard.run_once()
        self.assertEqual(self.state(guard)["last_error"], "RuntimeError")
        # A failed repair must not leave the app closed, restart it again, or
        # keep reporting an error for a switch that already used its one bounce.
        again = guard.run_once()
        self.assertEqual(again.action, "deferred")
        self.assertEqual(apps.quit_calls, 1)
        self.assertEqual(apps.open_calls, 1)
        self.assertEqual(self.state(guard)["last_error"], "restart_already_attempted")

    def test_snapshot_failure_after_quit_reopens_codex(self) -> None:
        probe = FakeProbe(True)
        apps = FakeApps(probe)
        calls = {"count": 0}

        def reader(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] > 1:
                raise RuntimeError("state still changing")
            return self.snapshot

        guard = Guard(
            codex_home=self.root / "codex",
            cc_home=self.root / "cc",
            state_path=self.root / "state.json",
            process_probe=probe,
            app_controller=apps,
            snapshot_reader=reader,
            reconciler=lambda *a, **k: ReconcileReport(changed=True),
            close_timeout=0.1,
        )
        with self.assertRaises(RuntimeError):
            guard.run_once()
        self.assertEqual(apps.quit_calls, 1)
        self.assertEqual(apps.open_calls, 1)


    def test_writable_rollouts_are_repaired_while_codex_runs(self) -> None:
        """A closed window releases its rollout, so no restart is needed."""

        probe = FakeProbe(True, uptime=3600.0)
        apps = FakeApps(probe)
        calls: list[tuple[bool, str]] = []
        left = {"dirty": True}

        def reconciler(snapshot, codex_home, cc_home, *, apply, scope="all"):
            calls.append((apply, scope))
            if apply:
                left["dirty"] = False
                return ReconcileReport(changed=True, protocol_files=1)
            return ReconcileReport(
                changed=left["dirty"], protocol_files=int(left["dirty"])
            )

        guard = Guard(
            codex_home=self.root / "codex",
            cc_home=self.root / "cc",
            state_path=self.root / "state.json",
            process_probe=probe,
            app_controller=apps,
            snapshot_reader=lambda *a, **k: self.snapshot,
            reconciler=reconciler,
            close_timeout=0.1,
        )
        decision = guard.run_once()
        self.assertEqual(decision.action, "repaired_rollouts_while_running")
        self.assertEqual(apps.quit_calls, 0)
        self.assertEqual(apps.open_calls, 0)
        self.assertEqual(calls, [(False, "all"), (True, "rollouts"), (False, "all")])
        self.assertEqual(self.state(guard)["last_successful_fingerprint"], "fingerprint-1")

    def test_pending_repair_stays_pending_when_other_work_remains(self) -> None:
        probe = FakeProbe(True, uptime=3600.0)
        apps = FakeApps(probe)
        calls: list[tuple[bool, str]] = []

        def reconciler(snapshot, codex_home, cc_home, *, apply, scope="all"):
            calls.append((apply, scope))
            # Rollouts are writable, but the provider labels still need a
            # closed app, so the plan never becomes clean.
            return ReconcileReport(
                changed=True, protocol_files=1, provider_threads=1
            )

        guard = Guard(
            codex_home=self.root / "codex",
            cc_home=self.root / "cc",
            state_path=self.root / "state.json",
            process_probe=probe,
            app_controller=apps,
            snapshot_reader=lambda *a, **k: self.snapshot,
            reconciler=reconciler,
            close_timeout=0.1,
        )
        decision = guard.run_once()
        self.assertEqual(decision.action, "deferred")
        self.assertEqual(apps.quit_calls, 0)
        self.assertEqual(calls[1][1], "rollouts")
        self.assertNotIn("last_successful_fingerprint", self.state(guard))


class ForeverLoopTest(unittest.TestCase):
    def test_closing_codex_triggers_reconciliation(self) -> None:
        calls: list[str] = []

        class Probe:
            def __init__(self) -> None:
                self.running = True
                self.polls = 0

            def codex_app_running(self) -> bool:
                self.polls += 1
                if self.polls > 1:
                    self.running = False
                return self.running

        class FakeGuard:
            codex_home = Path("/tmp/codex-home")
            cc_home = Path("/tmp/cc-home")
            process_probe = Probe()

            @staticmethod
            def run_once():
                calls.append("run")
                if len(calls) > 1:
                    raise KeyboardInterrupt
                return type("D", (), {"action": "noop", "fingerprint": "f" * 8, "unreadable": 0})()

        with self.assertRaises(KeyboardInterrupt):
            run_forever(
                FakeGuard(),
                poll_seconds=0.01,
                pending_poll_seconds=0.01,
                max_pending_poll_seconds=0.02,
                app_poll_seconds=0.01,
            )
        self.assertEqual(len(calls), 2)


class ElapsedParseTest(unittest.TestCase):
    def test_parses_ps_etime_formats(self) -> None:
        self.assertEqual(_parse_elapsed("07:12"), 432.0)
        self.assertEqual(_parse_elapsed("01:07:12"), 4032.0)
        self.assertEqual(_parse_elapsed("2-01:07:12"), 176832.0)

    def test_rejects_garbage(self) -> None:
        self.assertIsNone(_parse_elapsed("n/a"))

    def test_live_probe_is_read_only(self) -> None:
        probe = CodexProcessProbe(Path.home() / ".codex")
        uptime = probe.codex_app_uptime_seconds()
        self.assertTrue(uptime is None or uptime >= 0)

    def test_log_timestamp_is_sortable(self) -> None:
        self.assertRegex(_stamp(), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


if __name__ == "__main__":
    unittest.main()
