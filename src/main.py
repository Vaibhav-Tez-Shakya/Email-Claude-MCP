from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pathlib import Path
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from email import message_from_bytes
from email.utils import parsedate_to_datetime
import psycopg
import os
import base64
import time
from datetime import datetime, timezone


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

MAX_EMAILS = 1000

SYNC_LOCK_ID = 84736291

base = Path(__file__).parent.parent

load_dotenv(base / ".env", override=True)


def get_token_path():
    render_token = Path("/etc/secrets/token.json")

    if render_token.exists():
        return render_token

    return base / "token.json"


def get_gmail_service():
    token_path = get_token_path()

    print("Using Gmail token:", token_path)

    creds = Credentials.from_authorized_user_file(
        token_path,
        SCOPES
    )

    return build(
        "gmail",
        "v1",
        credentials=creds
    )


def get_email_category(label_ids):
    if "SPAM" in label_ids:
        return "SPAM"

    if "CATEGORY_PROMOTIONS" in label_ids:
        return "PROMOTIONS"

    if "CATEGORY_SOCIAL" in label_ids:
        return "SOCIAL"

    if "CATEGORY_UPDATES" in label_ids:
        return "UPDATES"

    return "PRIMARY"


def parse_email(raw_data):
    raw_bytes = base64.urlsafe_b64decode(raw_data)

    msg = message_from_bytes(raw_bytes)

    text_parts = []
    html_parts = []
    has_attachments = False

    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()

        if disposition == "attachment" or filename:
            has_attachments = True
            continue

        if content_type == "text/plain":
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True)
                content = (
                    payload.decode("utf-8", errors="replace")
                    if payload
                    else ""
                )

            if content:
                text_parts.append(content)

        elif content_type == "text/html":
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True)
                content = (
                    payload.decode("utf-8", errors="replace")
                    if payload
                    else ""
                )

            if content:
                html_parts.append(content)

    body_text = "\n\n".join(text_parts).strip()
    body_html = "\n\n".join(html_parts).strip()

    if not body_text and body_html:
        body_text = BeautifulSoup(
            body_html,
            "html.parser"
        ).get_text(
            "\n",
            strip=True
        )

    return (
        msg,
        body_text,
        body_html,
        has_attachments
    )


def update_spam_status(detected_count, checked_count):
    conn = psycopg.connect(
        os.getenv("DATABASE_URL")
    )

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS spam_status (
                    id INTEGER PRIMARY KEY,
                    detected_count INTEGER NOT NULL DEFAULT 0,
                    checked_count INTEGER NOT NULL DEFAULT 0,
                    last_sync_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                INSERT INTO spam_status (
                    id,
                    detected_count,
                    checked_count,
                    last_sync_at
                )
                VALUES (
                    1,
                    %s,
                    %s,
                    NOW()
                )
                ON CONFLICT (id)
                DO UPDATE SET
                    detected_count = EXCLUDED.detected_count,
                    checked_count = EXCLUDED.checked_count,
                    last_sync_at = EXCLUDED.last_sync_at
                """,
                (
                    detected_count,
                    checked_count
                )
            )

        conn.commit()

    finally:
        conn.close()


def gmail_execute(request, attempts=7):
    delay = 2

    for attempt in range(attempts):
        try:
            return request.execute()

        except HttpError as error:
            error_text = str(error)

            if error.resp.status in (403, 429) and (
                "rateLimitExceeded" in error_text
                or "userRateLimitExceeded" in error_text
                or "quotaExceeded" in error_text
            ):
                if attempt == attempts - 1:
                    raise

                print(
                    "Gmail rate limit reached. Waiting",
                    delay,
                    "seconds..."
                )

                time.sleep(delay)
                delay = min(delay * 2, 60)

            else:
                raise


def ensure_sync_state_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS email_sync_state (
            id INTEGER PRIMARY KEY,
            last_sync_at TIMESTAMPTZ
        )
        """
    )


def get_last_sync(cur):
    cur.execute(
        """
        SELECT last_sync_at
        FROM email_sync_state
        WHERE id = 1
        """
    )

    row = cur.fetchone()

    if not row:
        return None

    return row[0]


def save_sync_time(cur, sync_time):
    cur.execute(
        """
        INSERT INTO email_sync_state (
            id,
            last_sync_at
        )
        VALUES (
            1,
            %s
        )
        ON CONFLICT (id)
        DO UPDATE SET
            last_sync_at = EXCLUDED.last_sync_at
        """,
        (sync_time,)
    )


def prune_emails(cur):
    cur.execute(
        """
        DELETE FROM emails
        WHERE id NOT IN (
            SELECT id
            FROM emails
            ORDER BY received_at DESC NULLS LAST, id DESC
            LIMIT %s
        )
        """,
        (MAX_EMAILS,)
    )

    return cur.rowcount


def sync_emails():
    print()
    print("=" * 60)
    print("Starting email sync...")
    print("=" * 60)

    conn = psycopg.connect(
        os.getenv("DATABASE_URL")
    )

    lock_acquired = False

    try:
        with conn.cursor() as cur:

            ensure_sync_state_table(cur)
            conn.commit()

            cur.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (SYNC_LOCK_ID,)
            )

            lock_acquired = cur.fetchone()[0]

            if not lock_acquired:
                print("A sync is already running. Skipping this run.")
                return 0

            sync_started_at = datetime.now(timezone.utc)

            last_sync = get_last_sync(cur)

            if last_sync is None:
                sync_mode = "INITIAL"

                print("Sync mode: INITIAL")
                print("Maximum Gmail messages:", MAX_EMAILS)

                gmail_query = None

            else:
                sync_mode = "INCREMENTAL"

                after_timestamp = int(last_sync.timestamp())

                gmail_query = f"after:{after_timestamp}"

                print("Sync mode: INCREMENTAL")
                print("Last sync:", last_sync)
                print("Gmail query:", gmail_query)

            conn.commit()

        service = get_gmail_service()

        processed = 0
        spam_detected = 0
        checked_count = 0
        listed_count = 0
        page_token = None
        page_number = 0

        category_counts = {
            "PRIMARY": 0,
            "PROMOTIONS": 0,
            "SOCIAL": 0,
            "UPDATES": 0
        }

        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM emails WHERE category = 'SPAM'"
            )

        conn.commit()

        while True:

            if sync_mode == "INITIAL" and listed_count >= MAX_EMAILS:
                print(
                    "Initial sync limit reached:",
                    MAX_EMAILS
                )
                break

            page_number += 1

            request_kwargs = {
                "userId": "me",
                "maxResults": 100,
                "includeSpamTrash": True
            }

            if page_token:
                request_kwargs["pageToken"] = page_token

            if gmail_query:
                request_kwargs["q"] = gmail_query

            result = gmail_execute(
                service.users().messages().list(
                    **request_kwargs
                )
            )

            messages = result.get("messages", [])

            if sync_mode == "INITIAL":
                remaining = MAX_EMAILS - listed_count

                if len(messages) > remaining:
                    messages = messages[:remaining]

            listed_count += len(messages)

            print(
                "Page:",
                page_number,
                "| listed:",
                len(messages),
                "| total listed:",
                listed_count
            )

            if not messages:
                break

            for item in messages:

                if sync_mode == "INITIAL" and checked_count >= MAX_EMAILS:
                    break

                message = gmail_execute(
                    service.users().messages().get(
                        userId="me",
                        id=item["id"],
                        format="raw"
                    )
                )

                checked_count += 1

                label_ids = message.get("labelIds", [])

                category = get_email_category(label_ids)

                if category == "SPAM":
                    spam_detected += 1

                    if spam_detected % 10 == 0 or spam_detected == 1:
                        print(
                            "Spam detected:",
                            spam_detected,
                            "| latest message:",
                            item["id"]
                        )

                    continue

                msg, body_text, body_html, has_attachments = parse_email(
                    message["raw"]
                )

                received_at = None

                if msg.get("Date"):
                    try:
                        received_at = parsedate_to_datetime(
                            msg.get("Date")
                        )
                    except (TypeError, ValueError):
                        pass

                thread_id = message.get("threadId")

                sender = str(msg.get("From") or "")
                receiver = str(msg.get("To") or "")
                subject = str(msg.get("Subject") or "")

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO emails (
                            message_id,
                            thread_id,
                            sender,
                            receiver,
                            subject,
                            body_text,
                            body_html,
                            received_at,
                            has_attachments,
                            category
                        )
                        VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s
                        )
                        ON CONFLICT (message_id)
                        DO UPDATE SET
                            thread_id = EXCLUDED.thread_id,
                            sender = EXCLUDED.sender,
                            receiver = EXCLUDED.receiver,
                            subject = EXCLUDED.subject,
                            body_text = EXCLUDED.body_text,
                            body_html = EXCLUDED.body_html,
                            received_at = EXCLUDED.received_at,
                            has_attachments = EXCLUDED.has_attachments,
                            category = EXCLUDED.category
                        """,
                        (
                            item["id"],
                            thread_id,
                            sender,
                            receiver,
                            subject,
                            body_text,
                            body_html,
                            received_at,
                            has_attachments,
                            category
                        )
                    )

                processed += 1
                category_counts[category] += 1

                if processed % 25 == 0:
                    conn.commit()

                    print(
                        "Progress:",
                        processed,
                        "saved |",
                        checked_count,
                        "checked | spam:",
                        spam_detected
                    )

                time.sleep(0.2)

            conn.commit()

            if sync_mode == "INITIAL" and checked_count >= MAX_EMAILS:
                break

            page_token = result.get("nextPageToken")

            if not page_token:
                break

            time.sleep(1)

        with conn.cursor() as cur:

            deleted_count = prune_emails(cur)

            print(
                "Pruned old emails:",
                deleted_count
            )

            save_sync_time(cur, sync_started_at)

        conn.commit()

        update_spam_status(
            spam_detected,
            checked_count
        )

        print()
        print("=" * 60)
        print("Email sync complete")
        print("=" * 60)
        print("Mode:", sync_mode)
        print("Pages processed:", page_number)
        print("Total listed:", listed_count)
        print("Total checked:", checked_count)
        print("Total saved/updated:", processed)
        print("Spam detected:", spam_detected)
        print("Spam saved: 0")
        print("Database maximum:", MAX_EMAILS)
        print()
        print("Category distribution:")

        for category, count in category_counts.items():
            print(category + ":", count)

        return processed

    finally:

        if lock_acquired:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(%s)",
                        (SYNC_LOCK_ID,)
                    )

                conn.commit()

            except Exception as error:
                print(
                    "Could not release sync lock:",
                    error
                )

        conn.close()


if __name__ == "__main__":
    sync_emails()
