import tempfile
import unittest
from pathlib import Path

from scripts import sync_provider


class ComputeChangesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / "thread-writer-locks").mkdir(parents=True)
        self.rollout = self.home / "sessions" / "rollout-2026-09-15T00-00-00-22222222-2222-2222-2222-222222222222.jsonl"
        self.rollout.parent.mkdir(parents=True)
        self.rollout.write_text('{"type": "session_meta", "payload": {"model_provider": "openai"}}\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def row(self, **overrides) -> dict:
        base = {
            "thread_id": "22222222-2222-2222-2222-222222222222",
            "rollout_path": str(self.rollout),
            "db_provider": "openai",
            "source": "vscode",
            "archived": False,
            "preview": "fixture",
            "rollout_provider": "openai",
        }
        base.update(overrides)
        return base

    def test_mismatching_visible_thread_is_planned(self) -> None:
        changes = sync_provider.compute_changes([self.row()], "custom", self.home)
        self.assertEqual(len(changes), 1)
        self.assertFalse(changes[0]["skipped"])

    def test_matching_visible_thread_is_skipped(self) -> None:
        row = self.row(db_provider="custom", rollout_provider="custom")
        self.assertEqual(sync_provider.compute_changes([row], "custom", self.home), [])

    def test_archived_thread_is_never_planned(self) -> None:
        row = self.row(archived=True, rollout_provider=None)
        self.assertEqual(sync_provider.compute_changes([row], "custom", self.home), [])

    def test_archived_thread_in_archive_dir_is_never_planned(self) -> None:
        row = self.row(
            archived=True,
            rollout_path=str(self.home / "archived_sessions" / "rollout-2026-08-30T03-42-10-22222222-2222-2222-2222-222222222222.jsonl"),
            rollout_provider=None,
            db_provider="custom",
        )
        self.assertEqual(sync_provider.compute_changes([row], "custom", self.home), [])

    def test_missing_rollout_file_does_not_loop_forever(self) -> None:
        row = self.row(rollout_path=str(self.home / "sessions" / "gone.jsonl"), db_provider="custom", rollout_provider=None)
        self.assertEqual(sync_provider.compute_changes([row], "custom", self.home), [])

    def test_locked_thread_is_planned_but_marked_skipped(self) -> None:
        (self.home / "thread-writer-locks" / "22222222-2222-2222-2222-222222222222.lock").touch()
        changes = sync_provider.compute_changes([self.row()], "custom", self.home)
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0]["skipped"])


if __name__ == "__main__":
    unittest.main()
