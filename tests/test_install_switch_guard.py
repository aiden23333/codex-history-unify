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
        (self.codex_home / "sessions").mkdir(parents=True)
        (self.codex_home / "switch-guard" / "backups").mkdir(parents=True)
        (self.skill_dir / "scripts").mkdir(parents=True)
        (self.skill_dir / "scripts" / "codex_switch_guard.py").write_text("# fixture\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

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


if __name__ == "__main__":
    unittest.main()
