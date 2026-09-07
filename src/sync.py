import subprocess
import sys
import time
from pathlib import Path

base = Path(__file__).parent

while True:
    print()
    print("Starting email sync...")

    result = subprocess.run(
        [sys.executable, str(base / "main.py")],
        cwd=base.parent
    )

    if result.returncode != 0:
        print("Sync failed")

    print("Next sync in 60 seconds...")
    time.sleep(60)
