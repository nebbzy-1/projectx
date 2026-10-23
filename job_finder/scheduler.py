from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


LABEL = "com.projectx.jobfinder.daily"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def remove_schedule() -> bool:
    if sys.platform != "darwin":
        return False
    if not PLIST_PATH.exists():
        return False
    domain = f"gui/{os.getuid()}/{LABEL}"
    subprocess.run(["launchctl", "bootout", domain], check=False)
    PLIST_PATH.unlink()
    return True
