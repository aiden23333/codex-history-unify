#!/usr/bin/env python3
"""Enable CC Switch's built-in unified Codex session history.

CC Switch (https://github.com/farion1231/cc-switch) ships a setting named
`unifyCodexSessionHistory`. When enabled, switching Codex providers keeps the
same local session history visible under the newly selected provider, with its
own backup and restore flow. This script flips that setting on.

Usage:
  enable_ccswitch_unify.py
  enable_ccswitch_unify.py --settings /path/to/settings.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, help="Path to CC Switch settings.json")
    args = parser.parse_args()

    path = args.settings or Path.home() / ".cc-switch" / "settings.json"
    if not path.exists():
        print(f"CC Switch settings not found: {path}", file=sys.stderr)
        return 1

    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    changed = False
    if not data.get("unifyCodexSessionHistory"):
        data["unifyCodexSessionHistory"] = True
        changed = True
    if not data.get("unifyCodexMigrateExisting"):
        data["unifyCodexMigrateExisting"] = True
        changed = True

    if not changed:
        print("CC Switch unified history is already enabled.")
        return 0

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)

    print("Enabled unifyCodexSessionHistory in", path)
    print("If CC Switch is running, restart it or switch providers once to run the migration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
