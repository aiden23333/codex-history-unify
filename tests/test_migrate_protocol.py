import json
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
        self.rollout = self.sessions / "2026" / "09" / "15" / "rollout-test.jsonl"
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
            [sys.executable, str(SCRIPT), "--sessions-dir", str(self.sessions), "--backup-root", str(self.backups), *args],
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
            self.assertIn("2026/09/15/rollout-test.jsonl", archive.getnames())

        restored = self.run_script("--restore-latest")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(read_jsonl(self.rollout), self.original)

    def test_deepseek_target_only_diagnoses(self) -> None:
        result = self.run_script("--target", "deepseek", "--apply")
        self.assertEqual(result.returncode, 2)
        self.assertIn("diagnostic_only", result.stdout)
        self.assertEqual(read_jsonl(self.rollout), self.original)


if __name__ == "__main__":
    unittest.main()
