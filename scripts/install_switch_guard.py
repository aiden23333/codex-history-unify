#!/usr/bin/env python3
"""Install or remove the per-user Codex switch guard LaunchAgent."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Callable, Sequence


LABEL = "com.aiden.codex-switch-guard"
PLIST_NAME = f"{LABEL}.plist"
Runner = Callable[[Sequence[str]], object]


@dataclass(frozen=True)
class InstallReport:
    changed: bool
    plist_path: Path


def _render(skill_dir: Path, codex_home: Path) -> str:
    template_path = Path(__file__).resolve().parent.parent / "assets" / f"{PLIST_NAME}.template"
    template = template_path.read_text(encoding="utf-8")
    log_dir = codex_home / "switch-guard"
    values = {
        "python": sys.executable,
        "guard_script": skill_dir / "scripts" / "codex_switch_guard.py",
        "codex_home": codex_home,
        "cc_home": codex_home.parent / ".cc-switch",
        "stdout_path": log_dir / "guard.log",
        "stderr_path": log_dir / "guard.err.log",
    }
    return template.format(**{key: escape(str(value)) for key, value in values.items()})


def _launchctl(runner: Runner, action: str, plist_path: Path) -> None:
    domain = f"gui/{os.getuid()}"
    if action == "bootout":
        runner(["/bin/launchctl", "bootout", domain, str(plist_path)])
    else:
        runner(["/bin/launchctl", "bootstrap", domain, str(plist_path)])


def install(
    codex_home: Path,
    skill_dir: Path,
    launch_agents_dir: Path,
    *,
    apply: bool,
    runner: Runner | None = None,
) -> InstallReport:
    plist_path = launch_agents_dir / PLIST_NAME
    rendered = _render(skill_dir.resolve(), codex_home.resolve())
    existing = None
    try:
        existing = plist_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    changed = existing != rendered
    if apply and changed:
        launch_agents_dir.mkdir(parents=True, exist_ok=True)
        (codex_home / "switch-guard").mkdir(parents=True, exist_ok=True)
        temp = plist_path.with_name(plist_path.name + ".tmp")
        temp.write_text(rendered, encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, plist_path)
        plist_path.chmod(0o600)
        if runner is not None:
            try:
                _launchctl(runner, "bootout", plist_path)
            except Exception:
                pass
            _launchctl(runner, "bootstrap", plist_path)
    return InstallReport(changed=changed, plist_path=plist_path)


def uninstall(
    codex_home: Path,
    launch_agents_dir: Path,
    *,
    apply: bool,
    runner: Runner | None = None,
) -> InstallReport:
    del codex_home  # State, backups, and session history are deliberately preserved.
    plist_path = launch_agents_dir / PLIST_NAME
    changed = plist_path.exists()
    if apply and changed:
        if runner is not None:
            try:
                _launchctl(runner, "bootout", plist_path)
            except Exception:
                pass
        plist_path.unlink()
    return InstallReport(changed=changed, plist_path=plist_path)


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--uninstall", action="store_true")
    parser.add_argument("--codex-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--cc-home", type=Path, default=Path.home() / ".cc-switch")
    parser.add_argument(
        "--launch-agents-dir",
        type=Path,
        default=Path.home() / "Library" / "LaunchAgents",
    )
    args = parser.parse_args()
    skill_dir = Path(__file__).resolve().parent.parent
    if args.uninstall:
        report = uninstall(
            args.codex_home,
            args.launch_agents_dir,
            apply=True,
            runner=_run_command,
        )
    else:
        # cc-home is encoded by convention in the template; reject surprising layouts.
        expected_cc_home = args.codex_home.parent / ".cc-switch"
        if args.cc_home.resolve() != expected_cc_home.resolve():
            parser.error("--cc-home must be the sibling .cc-switch directory")
        report = install(
            args.codex_home,
            skill_dir,
            args.launch_agents_dir,
            apply=args.apply,
            runner=_run_command if args.apply else None,
        )
    print(
        f"changed={str(report.changed).lower()} plist={report.plist_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
