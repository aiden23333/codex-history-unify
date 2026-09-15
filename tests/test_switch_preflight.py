import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts import codex_switch_preflight as preflight


class SwitchPreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.codex_home = root / "codex"
        self.cc_home = root / "cc-switch"
        (self.codex_home / "sessions" / "2026" / "09" / "15").mkdir(parents=True)
        (self.codex_home / "sqlite").mkdir(parents=True)
        self.cc_home.mkdir(parents=True)
        self.rollout = self.codex_home / "sessions" / "2026" / "09" / "15" / "rollout-2026-09-15T00-00-00-11111111-1111-1111-1111-111111111111.jsonl"
        self.rollout.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    {"type": "session_meta", "payload": {"model_provider": "openai"}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant", "id": "resp_abc_msg", "content": []}},
                    {"type": "event_msg", "payload": {"item": {"type": "AgentMessage", "id": "resp_abc_msg"}}},
                    {"type": "response_item", "payload": {"type": "reasoning", "id": "deepseek-reasoning", "content": [{"type": "reasoning_text", "text": "internal"}], "encrypted_content": None}},
                    {"type": "response_item", "payload": {"type": "reasoning", "id": "rs_valid", "content": None, "encrypted_content": "x" * 200}},
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self._write_codex_databases()
        self._write_catalog()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_codex_databases(self) -> None:
        with closing(sqlite3.connect(self.codex_home / "state_5.sqlite")) as con:
            con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, model_provider TEXT, source TEXT, archived INTEGER, preview TEXT)")
            con.execute("INSERT INTO threads VALUES (?, ?, 'openai', 'vscode', 0, 'fixture')", ("11111111-1111-1111-1111-111111111111", str(self.rollout)))
            con.commit()
        with closing(sqlite3.connect(self.codex_home / "sqlite" / "codex-dev.db")) as con:
            con.execute("CREATE TABLE local_thread_catalog (thread_id TEXT PRIMARY KEY, model_provider TEXT)")
            con.execute("INSERT INTO local_thread_catalog VALUES ('11111111-1111-1111-1111-111111111111', 'openai')")
            con.commit()

    def _write_catalog(self) -> None:
        (self.codex_home / "cc-switch-model-catalog.json").write_text(
            json.dumps({"models": [{"slug": "deepseek-v4-flash", "input_modalities": ["text"], "other": 1}]}),
            encoding="utf-8",
        )

    def write_state(self, *, model: str, current_provider: str, api_format: str, proxy_enabled: bool) -> None:
        provider = "custom" if "deepseek" in model else "openai"
        (self.codex_home / "config.toml").write_text(
            f'model = "{model}"\nmodel_provider = "{provider}"\n[model_providers.custom]\nwire_api = "responses"\n',
            encoding="utf-8",
        )
        (self.cc_home / "settings.json").write_text(
            json.dumps({"currentProviderCodex": current_provider, "unifyCodexSessionHistory": True}),
            encoding="utf-8",
        )
        with closing(sqlite3.connect(self.cc_home / "cc-switch.db")) as con:
            con.execute("CREATE TABLE IF NOT EXISTS providers (id TEXT, app_type TEXT, name TEXT, settings_config TEXT, meta TEXT, is_current INTEGER)")
            con.execute("DELETE FROM providers")
            deepseek_current = int(current_provider == "deepseek")
            official_current = int(current_provider == "codex-official")
            con.execute("INSERT INTO providers VALUES ('deepseek', 'codex', 'DeepSeek', '{}', ?, ?)", (json.dumps({"apiFormat": api_format}), deepseek_current))
            con.execute("INSERT INTO providers VALUES ('codex-official', 'codex', 'OpenAI Official', '{}', '{}', ?)", (official_current,))
            con.execute("CREATE TABLE IF NOT EXISTS proxy_config (app_type TEXT, proxy_enabled INTEGER, enabled INTEGER, live_takeover_active INTEGER)")
            con.execute("DELETE FROM proxy_config")
            con.execute("INSERT INTO proxy_config VALUES ('codex', ?, ?, ?)", (int(proxy_enabled), int(proxy_enabled), int(proxy_enabled)))
            con.commit()

    def test_detects_deepseek_only_when_config_and_cc_switch_agree(self) -> None:
        self.write_state(model="deepseek-v4-flash", current_provider="deepseek", api_format="openai_chat", proxy_enabled=True)
        snapshot = preflight.read_stable_snapshot(self.codex_home, self.cc_home, stable_delay=0)
        self.assertEqual(snapshot.target, "deepseek")
        self.assertEqual(snapshot.provider_bucket, "custom")

    def test_rejects_partially_written_mixed_state(self) -> None:
        self.write_state(model="deepseek-v4-flash", current_provider="codex-official", api_format="openai_responses", proxy_enabled=False)
        with self.assertRaises(preflight.UnstableSwitchState):
            preflight.read_stable_snapshot(self.codex_home, self.cc_home, stable_delay=0)

    def test_catalog_regeneration_changes_fingerprint(self) -> None:
        self.write_state(model="deepseek-v4-flash", current_provider="deepseek", api_format="openai_responses", proxy_enabled=True)
        first = preflight.read_stable_snapshot(self.codex_home, self.cc_home, stable_delay=0)
        catalog = json.loads((self.codex_home / "cc-switch-model-catalog.json").read_text())
        catalog["models"][0]["input_modalities"] = ["text", "image"]
        (self.codex_home / "cc-switch-model-catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        second = preflight.read_stable_snapshot(self.codex_home, self.cc_home, stable_delay=0)
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_deepseek_reconcile_enforces_responses_and_preserves_reasoning(self) -> None:
        self.write_state(model="deepseek-v4-flash", current_provider="deepseek", api_format="openai_chat", proxy_enabled=True)
        snapshot = preflight.read_stable_snapshot(self.codex_home, self.cc_home, stable_delay=0)
        report = preflight.reconcile(snapshot, self.codex_home, self.cc_home, apply=True)
        with closing(sqlite3.connect(self.cc_home / "cc-switch.db")) as con:
            meta = json.loads(con.execute("SELECT meta FROM providers WHERE id='deepseek'").fetchone()[0])
        self.assertEqual(meta["apiFormat"], "openai_responses")
        catalog = json.loads((self.codex_home / "cc-switch-model-catalog.json").read_text())
        self.assertEqual(catalog["models"][0]["input_modalities"], ["text", "image"])
        rows = [json.loads(line) for line in self.rollout.read_text().splitlines()]
        self.assertEqual(rows[1]["payload"]["id"], "msg_abc")
        self.assertEqual(len([row for row in rows if row.get("type") == "response_item" and row["payload"].get("type") == "reasoning"]), 2)
        self.assertTrue(report.changed)

    def test_unreadable_rollout_is_reported_without_blocking_repairs(self) -> None:
        damaged = self.codex_home / "sessions" / "2026" / "09" / "15" / "rollout-2026-09-15T00-00-01-33333333-3333-3333-3333-333333333333.jsonl"
        damaged.write_text('{"type": "response_item", "payload": {\n', encoding="utf-8")
        self.write_state(model="gpt-5.6-sol", current_provider="codex-official", api_format="openai_responses", proxy_enabled=False)
        snapshot = preflight.read_stable_snapshot(self.codex_home, self.cc_home, stable_delay=0)
        report = preflight.reconcile(snapshot, self.codex_home, self.cc_home, apply=True)
        self.assertTrue(report.changed)
        self.assertEqual(report.unreadable, 1)
        rows = [json.loads(line) for line in self.rollout.read_text().splitlines()]
        reasoning = [row["payload"]["id"] for row in rows if row.get("type") == "response_item" and row["payload"].get("type") == "reasoning"]
        self.assertEqual(reasoning, ["rs_valid"])
        self.assertEqual(damaged.read_text(), '{"type": "response_item", "payload": {\n')

    def test_gpt_reconcile_removes_plaintext_reasoning_and_is_idempotent(self) -> None:
        self.write_state(model="gpt-5.6-sol", current_provider="codex-official", api_format="openai_responses", proxy_enabled=False)
        snapshot = preflight.read_stable_snapshot(self.codex_home, self.cc_home, stable_delay=0)
        first = preflight.reconcile(snapshot, self.codex_home, self.cc_home, apply=True)
        second = preflight.reconcile(snapshot, self.codex_home, self.cc_home, apply=True)
        rows = [json.loads(line) for line in self.rollout.read_text().splitlines()]
        reasoning = [row["payload"]["id"] for row in rows if row.get("type") == "response_item" and row["payload"].get("type") == "reasoning"]
        self.assertEqual(reasoning, ["rs_valid"])
        self.assertTrue(first.changed)
        self.assertFalse(second.changed)


if __name__ == "__main__":
    unittest.main()
