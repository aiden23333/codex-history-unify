import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "migrate_protocol.py"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class ProtocolMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.sessions = root / "sessions"
        self.backups = root / "backups"
        self.locks = root / "locks"
        self.rollout = self.sessions / "2026" / "09" / "15" / "rollout-2026-09-15T00-00-00-11111111-1111-1111-1111-111111111111.jsonl"
        self.original = [
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "id": "resp_abc_msg", "content": [{"type": "output_text", "text": "answer"}]}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "id": "resp_abc_msg"}}},
            {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "id": "bare-ui-uuid"}}},
            {"type": "response_item", "payload": {"type": "reasoning", "id": "deepseek-reasoning", "content": [{"type": "reasoning_text", "text": "internal"}], "encrypted_content": "short-placeholder", "summary": []}},
            {"type": "response_item", "payload": {"type": "reasoning", "id": "rs_resp_missing", "content": None, "encrypted_content": None, "summary": [{"type": "summary_text", "text": "local only"}]}},
            {"type": "response_item", "payload": {"type": "reasoning", "id": "rs_valid", "content": None, "encrypted_content": "x" * 200, "summary": []}},
        ]
        write_jsonl(self.rollout, self.original)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--sessions-dir",
                str(self.sessions),
                "--backup-root",
                str(self.backups),
                "--lock-dir",
                str(self.locks),
                *args,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_dry_run_is_read_only(self) -> None:
        result = self.run_script("--target", "gpt", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("files_to_change=1", result.stdout)
        self.assertIn("assistant_ids=1", result.stdout)
        self.assertIn("reasoning_items=2", result.stdout)
        self.assertEqual(read_jsonl(self.rollout), self.original)
        self.assertFalse(self.backups.exists())

    def test_apply_and_restore(self) -> None:
        result = self.run_script("--target", "gpt", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = read_jsonl(self.rollout)
        self.assertEqual(rows[0]["payload"]["id"], "msg_abc")
        self.assertEqual(rows[1]["payload"]["item"]["id"], "msg_abc")
        self.assertEqual(rows[2]["payload"]["item"]["id"], "bare-ui-uuid")
        reasoning = [r for r in rows if r.get("type") == "response_item" and r["payload"].get("type") == "reasoning"]
        self.assertEqual([r["payload"]["id"] for r in reasoning], ["rs_valid"])
        archives = list(self.backups.glob("*.tar.gz"))
        self.assertEqual(len(archives), 1)
        with tarfile.open(archives[0], "r:gz") as archive:
            self.assertIn(self.rollout.relative_to(self.sessions).as_posix(), archive.getnames())

        restored = self.run_script("--restore-latest")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(read_jsonl(self.rollout), self.original)

    def test_deepseek_normalizes_ids_but_preserves_reasoning(self) -> None:
        result = self.run_script("--target", "deepseek", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = read_jsonl(self.rollout)
        self.assertEqual(rows[0]["payload"]["id"], "msg_abc")
        self.assertEqual(rows[1]["payload"]["item"]["id"], "msg_abc")
        reasoning = [
            row
            for row in rows
            if row.get("type") == "response_item"
            and row["payload"].get("type") == "reasoning"
        ]
        self.assertEqual(len(reasoning), 3)

    def test_stale_lock_marker_does_not_skip(self) -> None:
        self.locks.mkdir(parents=True)
        (self.locks / "11111111-1111-1111-1111-111111111111.lock").touch()
        lsof = Path(self.temp.name) / "lsof-stale"
        lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        os.chmod(lsof, 0o700)

        result = self.run_script(
            "--target", "gpt", "--apply", "--lsof-bin", str(lsof)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("locked_files_skipped=0", result.stdout)
        self.assertEqual(read_jsonl(self.rollout)[0]["payload"]["id"], "msg_abc")

    def test_open_descriptor_skips_rollout(self) -> None:
        self.locks.mkdir(parents=True)
        (self.locks / "11111111-1111-1111-1111-111111111111.lock").touch()
        lsof = Path(self.temp.name) / "lsof-active"
        lsof.write_text("#!/bin/sh\necho 4242\nexit 0\n", encoding="utf-8")
        os.chmod(lsof, 0o700)

        result = self.run_script(
            "--target", "gpt", "--apply", "--lsof-bin", str(lsof)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("locked_files_skipped=1", result.stdout)
        self.assertEqual(read_jsonl(self.rollout), self.original)


if __name__ == "__main__":
    unittest.main()
