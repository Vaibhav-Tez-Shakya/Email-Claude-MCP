import time

from main import sync_emails


SYNC_INTERVAL = 60


while True:
    print()
    print("========================================")
    print("Starting automatic email sync...")
    print("========================================")

    try:
        sync_emails()
    except Exception as e:
        print("Sync failed:")
        print(type(e).__name__, "-", e)

    print()
    print(f"Next sync in {SYNC_INTERVAL} seconds...")
    time.sleep(SYNC_INTERVAL)
