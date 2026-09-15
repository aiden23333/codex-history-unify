import tempfile
import unittest
from pathlib import Path

from scripts.codex_switch_guard import Guard
from scripts.codex_switch_preflight import ReconcileReport, TargetSnapshot


class FakeProbe:
    def __init__(self, opened: bool) -> None:
        self.opened = opened

    def codex_rollouts_open(self) -> bool:
        return self.opened


class FakeApps:
    def __init__(self, probe: FakeProbe) -> None:
        self.probe = probe
        self.quit_calls = 0
        self.open_calls = 0

    def quit_codex(self) -> None:
        self.quit_calls += 1
        self.probe.opened = False

    def open_codex(self) -> None:
        self.open_calls += 1
        self.probe.opened = True


class SwitchGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshot = TargetSnapshot("gpt", "codex-official", "gpt-5.6-sol", "openai", "openai_responses", "fingerprint-1")
        self.apply_calls: list[bool] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_guard(self, opened: bool, changed: bool = True) -> tuple[Guard, FakeApps]:
        probe = FakeProbe(opened)
        apps = FakeApps(probe)

        def reader(*args, **kwargs):
            return self.snapshot

        def reconciler(snapshot, codex_home, cc_home, *, apply):
            self.apply_calls.append(apply)
            return ReconcileReport(changed=changed if not apply else True)

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
        return guard, apps

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

    def test_unchanged_state_is_noop(self) -> None:
        guard, apps = self.make_guard(opened=True, changed=False)
        decision = guard.run_once()
        self.assertEqual(decision.action, "noop")
        self.assertEqual(apps.quit_calls, 0)
        self.assertEqual(apps.open_calls, 0)
        self.assertEqual(self.apply_calls, [False])

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
        with self.assertRaises(RuntimeError):
            guard.run_once()
        self.assertEqual(apps.quit_calls, 1)
        self.assertEqual(apps.open_calls, 0)


if __name__ == "__main__":
    unittest.main()
