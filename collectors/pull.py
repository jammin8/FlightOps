#!/usr/bin/env python3
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402
import capture

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ControlMaster=auto",
       "-o", "ControlPath=~/.ssh/cm-flightops-%r@%h", "-o", "ControlPersist=10m"]
TARGET = settings.CONFIG["ssh_target"]
PUBLISHED = settings.CONFIG["published_status"]
LIMIT = 32 * 1024 * 1024
SECONDS = 15


def pull(run=capture.run):
    try:
        answer = run(SSH + [TARGET, "cat", PUBLISHED], limit=LIMIT, timeout=SECONDS)
    except (OSError, subprocess.SubprocessError, capture.TooLarge):
        return None
    return answer.stdout if answer.returncode == 0 and answer.stdout else None


def main():
    data = pull()
    if data is None:
        return 1
    sys.stdout.buffer.write(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
