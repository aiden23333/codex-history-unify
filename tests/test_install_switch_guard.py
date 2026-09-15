import json
import stat
import tempfile
import unittest
from pathlib import Path

from scripts import install_switch_guard as installer


class InstallSwitchGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.codex_home = root / "codex"
        self.skill_dir = root / "skill"
        self.launch_agents = root / "LaunchAgents"
        self.cc_home = root / ".cc-switch"
        (self.codex_home / "sessions").mkdir(parents=True)
        (self.codex_home / "switch-guard" / "backups").mkdir(parents=True)
        (self.skill_dir / "scripts").mkdir(parents=True)
        (self.skill_dir / "scripts" / "codex_switch_guard.py").write_text("# fixture\n")
        self.cc_home.mkdir(parents=True)
        self.write_cc_settings({"currentProviderCodex": "deepseek", "showInTray": True})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_cc_settings(self, data: dict) -> None:
        (self.cc_home / "settings.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def cc_settings(self) -> dict:
        return json.loads((self.cc_home / "settings.json").read_text(encoding="utf-8"))

    def test_install_writes_reversible_launch_agent(self) -> None:
        report = installer.install(
            self.codex_home, self.skill_dir, self.launch_agents, apply=True
        )
        plist = self.launch_agents / installer.PLIST_NAME
        self.assertTrue(plist.exists())
        self.assertEqual(stat.S_IMODE(plist.stat().st_mode), 0o600)
        text = plist.read_text(encoding="utf-8")
        self.assertIn(str(self.codex_home), text)
        self.assertIn(str(self.skill_dir / "scripts" / "codex_switch_guard.py"), text)
        self.assertNotIn("api_key", text.lower())
        self.assertTrue(report.changed)

    def test_reinstall_is_idempotent(self) -> None:
        installer.install(self.codex_home, self.skill_dir, self.launch_agents, apply=True)
        second = installer.install(
            self.codex_home, self.skill_dir, self.launch_agents, apply=True
        )
        self.assertFalse(second.changed)

    def test_uninstall_preserves_backups_and_history(self) -> None:
        installer.install(self.codex_home, self.skill_dir, self.launch_agents, apply=True)
        report = installer.uninstall(
            self.codex_home, self.launch_agents, apply=True
        )
        self.assertTrue(report.changed)
        self.assertFalse((self.launch_agents / installer.PLIST_NAME).exists())
        self.assertTrue((self.codex_home / "switch-guard" / "backups").exists())
        self.assertTrue((self.codex_home / "sessions").exists())

    def test_dry_run_writes_nothing(self) -> None:
        report = installer.install(
            self.codex_home, self.skill_dir, self.launch_agents, apply=False
        )
        self.assertTrue(report.changed)
        self.assertFalse(self.launch_agents.exists())

    def test_install_enables_cc_switch_unified_history(self) -> None:
        report = installer.install(
            self.codex_home,
            self.skill_dir,
            self.launch_agents,
            cc_home=self.cc_home,
            apply=True,
        )
        self.assertTrue(report.cc_settings_changed)
        settings = self.cc_settings()
        self.assertIs(settings["unifyCodexSessionHistory"], True)
        self.assertEqual(settings["currentProviderCodex"], "deepseek")
        self.assertIs(settings["showInTray"], True)

    def test_reinstall_leaves_enabled_cc_switch_setting_untouched(self) -> None:
        installer.install(
            self.codex_home,
            self.skill_dir,
            self.launch_agents,
            cc_home=self.cc_home,
            apply=True,
        )
        before = (self.cc_home / "settings.json").read_bytes()
        second = installer.install(
            self.codex_home,
            self.skill_dir,
            self.launch_agents,
            cc_home=self.cc_home,
            apply=True,
        )
        self.assertFalse(second.cc_settings_changed)
        self.assertEqual((self.cc_home / "settings.json").read_bytes(), before)

    def test_dry_run_does_not_touch_cc_switch_settings(self) -> None:
        before = (self.cc_home / "settings.json").read_bytes()
        report = installer.install(
            self.codex_home,
            self.skill_dir,
            self.launch_agents,
            cc_home=self.cc_home,
            apply=False,
        )
        self.assertTrue(report.cc_settings_changed)
        self.assertEqual((self.cc_home / "settings.json").read_bytes(), before)

    def test_install_without_cc_switch_settings_is_allowed(self) -> None:
        (self.cc_home / "settings.json").unlink()
        report = installer.install(
            self.codex_home,
            self.skill_dir,
            self.launch_agents,
            cc_home=self.cc_home,
            apply=True,
        )
        self.assertFalse(report.cc_settings_changed)
        self.assertTrue((self.launch_agents / installer.PLIST_NAME).exists())

    def test_invalid_cc_switch_settings_are_reported_not_overwritten(self) -> None:
        (self.cc_home / "settings.json").write_text("{not json", encoding="utf-8")
        report = installer.install(
            self.codex_home,
            self.skill_dir,
            self.launch_agents,
            cc_home=self.cc_home,
            apply=True,
        )
        self.assertFalse(report.cc_settings_changed)
        self.assertEqual(
            (self.cc_home / "settings.json").read_text(encoding="utf-8"), "{not json"
        )


if __name__ == "__main__":
    unittest.main()
