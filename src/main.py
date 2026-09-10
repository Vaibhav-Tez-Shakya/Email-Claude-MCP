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
import hashlib
import re
import time
from datetime import datetime, timezone


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

MAX_EMAILS = 500

SYNC_LOCK_ID = 84736291

base = Path(__file__).parent.parent

load_dotenv(base / ".env", override=True)


def get_token_path(mailbox="account_1"):
    if mailbox == "account_1":
        render_token = Path("/etc/secrets/token.json")

        if render_token.exists():
            return render_token

        return base / "token.json"

    if mailbox == "account_2":
        render_token = Path("/etc/secrets/token_account_2.json")

        if render_token.exists():
            return render_token

        return base / "token_account_2.json"

    raise ValueError(f"Unknown mailbox: {mailbox}")


def get_gmail_service(mailbox="account_1"):
    token_path = get_token_path(mailbox)

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

    cur.execute(
        """
        ALTER TABLE email_sync_state
        ADD COLUMN IF NOT EXISTS mailbox TEXT
        """
    )

    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS email_sync_state_mailbox_idx
        ON email_sync_state(mailbox)
        WHERE mailbox IS NOT NULL
        """
    )


def get_last_sync(cur, mailbox):
    cur.execute(
        """
        SELECT last_sync_at
        FROM email_sync_state
        WHERE mailbox = %s
        """,
        (mailbox,)
    )

    row = cur.fetchone()

    if not row:
        return None

    return row[0]


def save_sync_time(cur, mailbox, sync_time):
    cur.execute(
        """
        SELECT id
        FROM email_sync_state
        WHERE mailbox = %s
        LIMIT 1
        """,
        (mailbox,)
    )

    row = cur.fetchone()

    if row:
        cur.execute(
            """
            UPDATE email_sync_state
            SET last_sync_at = %s
            WHERE id = %s
            """,
            (sync_time, row[0])
        )
    else:
        cur.execute(
            """
            INSERT INTO email_sync_state (
                id,
                mailbox,
                last_sync_at
            )
            VALUES (
                COALESCE(
                    (SELECT MAX(id) + 1 FROM email_sync_state),
                    1
                ),
                %s,
                %s
            )
            """,
            (mailbox, sync_time)
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


def normalize_for_hash(value):
    value = str(value or "")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def calculate_email_hash(sender, subject, body_text):
    canonical = "\n".join([
        normalize_for_hash(sender),
        normalize_for_hash(subject),
        normalize_for_hash(body_text),
    ])

    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


def sync_emails():
    print()
    print("=" * 60)
    print("Starting two-account email sync...")
    print("=" * 60)

    accounts = [
        "account_1",
        "account_2",
    ]

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

        total_processed = 0
        total_skipped_duplicates = 0
        total_spam = 0

        for mailbox in accounts:
            print()
            print("-" * 60)
            print("Syncing:", mailbox)
            print("-" * 60)

            with conn.cursor() as cur:
                last_sync = get_last_sync(cur, mailbox)

            if last_sync is None:
                sync_mode = "INITIAL"
                gmail_query = None

                print("Sync mode: INITIAL")
                print("Maximum Gmail messages:", MAX_EMAILS)

            else:
                sync_mode = "INCREMENTAL"
                after_timestamp = int(last_sync.timestamp())
                gmail_query = f"after:{after_timestamp}"

                print("Sync mode: INCREMENTAL")
                print("Last sync:", last_sync)
                print("Gmail query:", gmail_query)

            service = get_gmail_service(mailbox)

            processed = 0
            skipped_duplicates = 0
            spam_detected = 0
            checked_count = 0
            listed_count = 0
            page_token = None
            page_number = 0

            category_counts = {
                "PRIMARY": 0,
                "PROMOTIONS": 0,
                "SOCIAL": 0,
                "UPDATES": 0,
            }

            while True:
                if (
                    sync_mode == "INITIAL"
                    and listed_count >= MAX_EMAILS
                ):
                    print(
                        "Initial sync limit reached:",
                        MAX_EMAILS
                    )
                    break

                page_number += 1

                request_kwargs = {
                    "userId": "me",
                    "maxResults": 100,
                    "includeSpamTrash": True,
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
                    if (
                        sync_mode == "INITIAL"
                        and checked_count >= MAX_EMAILS
                    ):
                        break

                    message = gmail_execute(
                        service.users().messages().get(
                            userId="me",
                            id=item["id"],
                            format="raw"
                        )
                    )

                    checked_count += 1

                    label_ids = message.get(
                        "labelIds",
                        []
                    )

                    category = get_email_category(
                        label_ids
                    )

                    if category == "SPAM":
                        spam_detected += 1

                        if (
                            spam_detected % 10 == 0
                            or spam_detected == 1
                        ):
                            print(
                                "Spam detected:",
                                spam_detected,
                                "| latest message:",
                                item["id"]
                            )

                        continue

                    (
                        msg,
                        body_text,
                        body_html,
                        has_attachments
                    ) = parse_email(
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

                    sender = str(
                        msg.get("From") or ""
                    )

                    receiver = str(
                        msg.get("To") or ""
                    )

                    subject = str(
                        msg.get("Subject") or ""
                    )

                    email_hash = calculate_email_hash(
                        sender,
                        subject,
                        body_text
                    )

                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT id, mailbox
                            FROM emails
                            WHERE email_hash = %s
                            LIMIT 20
                            """,
                            (email_hash,)
                        )

                        duplicate_row = None

                        for candidate_id, candidate_mailbox in cur.fetchall():
                            candidate_accounts = [
                                x.strip()
                                for x in str(candidate_mailbox or "").split(",")
                                if x.strip()
                            ]

                            if candidate_accounts and mailbox not in candidate_accounts:
                                duplicate_row = (
                                    candidate_id,
                                    candidate_accounts
                                )
                                break

                        if duplicate_row:
                            existing_id, existing_accounts = duplicate_row

                            existing_accounts.append(mailbox)

                            cur.execute(
                                """
                                UPDATE emails
                                SET mailbox = %s
                                WHERE id = %s
                                """,
                                (
                                    ",".join(existing_accounts),
                                    existing_id
                                )
                            )

                            skipped_duplicates += 1
                            total_skipped_duplicates += 1

                            print(
                                "Cross-account duplicate detected:",
                                item["id"],
                                "| existing email id:",
                                existing_id,
                                "| mailboxes:",
                                ",".join(existing_accounts)
                            )

                            continue

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
                                category,
                                mailbox,
                                email_hash
                            )
                            VALUES (
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s,
                                %s, %s
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
                                category = EXCLUDED.category,
                                mailbox = EXCLUDED.mailbox,
                                email_hash = EXCLUDED.email_hash
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
                                category,
                                mailbox,
                                email_hash
                            )
                        )

                    processed += 1
                    total_processed += 1
                    category_counts[category] += 1

                    if processed % 25 == 0:
                        conn.commit()

                        print(
                            "Progress:",
                            processed,
                            "saved |",
                            checked_count,
                            "checked | spam:",
                            spam_detected,
                            "| cross-account duplicates:",
                            skipped_duplicates
                        )

                    time.sleep(0.2)

                conn.commit()

                if (
                    sync_mode == "INITIAL"
                    and checked_count >= MAX_EMAILS
                ):
                    break

                page_token = result.get(
                    "nextPageToken"
                )

                if not page_token:
                    break

                time.sleep(1)

            sync_finished_at = datetime.now(
                timezone.utc
            )

            with conn.cursor() as cur:
                save_sync_time(
                    cur,
                    mailbox,
                    sync_finished_at
                )

            conn.commit()

            with conn.cursor() as cur:
                pruned = prune_emails(cur)

            conn.commit()

            if pruned:
                print("Pruned old emails:", pruned)

            total_spam += spam_detected

            print()
            print(mailbox, "sync complete.")
            print("Saved:", processed)
            print(
                "Cross-account duplicates skipped:",
                skipped_duplicates
            )
            print("Spam:", spam_detected)
            print("Categories:", category_counts)

        print()
        print("=" * 60)
        print("Two-account sync complete.")
        print("Total saved:", total_processed)
        print(
            "Total cross-account duplicates skipped:",
            total_skipped_duplicates
        )
        print("Total spam detected:", total_spam)
        print("=" * 60)

        return total_processed

    finally:
        if lock_acquired:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_unlock(%s)",
                        (SYNC_LOCK_ID,)
                    )

                conn.commit()

            except Exception:
                pass

        conn.close()
