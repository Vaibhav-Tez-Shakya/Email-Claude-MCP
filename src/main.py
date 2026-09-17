from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pathlib import Path
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from email import message_from_bytes
from email.utils import parsedate_to_datetime, getaddresses
import psycopg
import os
import base64
import hashlib
import re
import time
from datetime import datetime, timezone

from src.attachment_extractors import extract_attachment_text
from src.attachment_storage import (
    get_attachment_directory,
    decode_attachment_data,
    safe_filename,
    extract_attachment_parts,
    attachment_hash
)


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

MAX_EMAILS = 100

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
        SELECT id
        FROM emails
        WHERE id NOT IN (
            SELECT id
            FROM emails
            ORDER BY received_at DESC NULLS LAST, id DESC
            LIMIT %s
        )
        """,
        (MAX_EMAILS,)
    )

    deleted_ids = [
        row[0]
        for row in cur.fetchall()
    ]

    if not deleted_ids:
        return 0

    cur.execute(
        """
        SELECT DISTINCT storage_path
        FROM email_attachments
        WHERE email_id = ANY(%s)
          AND storage_path LIKE 'attachments\\_shared\\%%'
        """,
        (deleted_ids,)
    )

    candidate_paths = [
        row[0]
        for row in cur.fetchall()
    ]

    cur.execute(
        """
        DELETE FROM emails
        WHERE id = ANY(%s)
        """,
        (deleted_ids,)
    )

    import shutil

    for email_id in deleted_ids:
        attachment_dir = (
            base / "attachments" / str(email_id)
        )

        if attachment_dir.exists():
            shutil.rmtree(
                attachment_dir,
                ignore_errors=True
            )

    for storage_path in candidate_paths:
        cur.execute(
            """
            SELECT 1
            FROM email_attachments
            WHERE storage_path = %s
            LIMIT 1
            """,
            (storage_path,)
        )

        still_referenced = cur.fetchone()

        if still_referenced:
            continue

        file_path = base / Path(storage_path)

        if file_path.exists():
            file_path.unlink()

    return len(deleted_ids)

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


def save_gmail_attachments(service, gmail_message_id, email_id, conn):
    full_message = gmail_execute(
        service.users().messages().get(
            userId="me",
            id=gmail_message_id,
            format="full"
        )
    )

    parts = extract_attachment_parts(
        full_message.get("payload") or {}
    )

    if not parts:
        return 0

    shared_dir = base / "attachments"
    shared_dir.mkdir(parents=True, exist_ok=True)

    saved = 0

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE emails
            SET has_attachments = TRUE
            WHERE id = %s
            """,
            (email_id,)
        )

    for part in parts:
        filename = safe_filename(part.get("filename"))
        mime_type = part.get("mime_type") or "application/octet-stream"
        gmail_attachment_id = part.get("attachment_id")
        data = part.get("data")

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM email_attachments
                WHERE email_id = %s
                  AND gmail_attachment_id = %s
                LIMIT 1
                """,
                (
                    email_id,
                    gmail_attachment_id
                )
            )

            existing = cur.fetchone()

        if existing:
            continue

        if gmail_attachment_id:
            attachment_response = gmail_execute(
                service.users().messages().attachments().get(
                    userId="me",
                    messageId=gmail_message_id,
                    id=gmail_attachment_id
                )
            )

            data = attachment_response.get("data")

        file_data = decode_attachment_data(data)

        if not file_data:
            continue


        digest = attachment_hash(file_data)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM email_attachments
                WHERE email_id = %s
                  AND content_hash = %s
                LIMIT 1
                """,
                (
                    email_id,
                    digest
                )
            )

            existing_email_attachment = cur.fetchone()

        if existing_email_attachment:
            continue

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    storage_path,
                    content_text,
                    content_status
                FROM email_attachments
                WHERE content_hash = %s
                ORDER BY id
                LIMIT 1
                """,
                (digest,)
            )

            existing_file = cur.fetchone()

        if existing_file:
            (
                existing_attachment_id,
                existing_storage_path,
                content_text,
                content_status
            ) = existing_file

            storage_path = Path(existing_storage_path)

            print(
                "Attachment reused:",
                filename,
                "| existing attachment:",
                existing_attachment_id
            )

        else:
            extension = Path(filename).suffix
            safe_stem = Path(filename).stem

            storage_name = (
                f"{digest}_{safe_stem}{extension}"
            )

            storage_path = shared_dir / storage_name

            if not storage_path.exists():
                storage_path.write_bytes(file_data)

            content_text = None
            content_status = "pending"

            try:
                content_text, content_status = extract_attachment_text(
                    storage_path,
                    mime_type
                )
            except Exception as e:
                content_status = "failed"

                print(
                    "Attachment extraction failed:",
                    filename,
                    "|",
                    type(e).__name__,
                    "-",
                    e
                )

            existing_storage_path = str(
                storage_path.relative_to(base)
            )

        if existing_file:
            stored_path = existing_storage_path
        else:
            stored_path = str(
                storage_path.relative_to(base)
            )

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO email_attachments (
                    email_id,
                    gmail_attachment_id,
                    filename,
                    mime_type,
                    file_size,
                    storage_path,
                    content_hash,
                    content_text,
                    content_status
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                """,
                (
                    email_id,
                    gmail_attachment_id,
                    filename,
                    mime_type,
                    len(file_data),
                    stored_path,
                    digest,
                    content_text,
                    content_status
                )
            )

        if existing_file:
            print(
                "Attachment linked:",
                filename,
                "| status:",
                content_status
            )
        else:
            print(
                "Attachment saved:",
                filename,
                "| status:",
                content_status,
                "| text length:",
                len(content_text or "")
            )

        saved += 1

    return saved

def backfill_missing_attachments(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                e.id,
                e.message_id,
                e.mailbox
            FROM emails e
            WHERE e.has_attachments = TRUE
              AND NOT EXISTS (
                  SELECT 1
                  FROM email_attachments ea
                  WHERE ea.email_id = e.id
              )
            ORDER BY e.received_at DESC NULLS LAST, e.id DESC
            """
        )

        rows = cur.fetchall()

    processed = 0

    for email_id, message_id, mailbox in rows:
        accounts = [
            value.strip()
            for value in str(mailbox or "").split(",")
            if value.strip()
        ]

        for account in accounts:
            try:
                service = get_gmail_service(account)

                count = save_gmail_attachments(
                    service,
                    message_id,
                    email_id,
                    conn
                )

                if count:
                    processed += count
                    break

            except Exception:
                continue

    return processed


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
                after_timestamp = int(last_sync.timestamp()) - 50
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

                    to_values = msg.get_all("To", [])
                    cc_values = msg.get_all("Cc", [])
                    bcc_values = msg.get_all("Bcc", [])

                    receiver = ", ".join(
                        address
                        for _, address in getaddresses(to_values)
                        if address
                    )

                    cc = ", ".join(
                        address
                        for _, address in getaddresses(cc_values)
                        if address
                    )

                    bcc = ", ".join(
                        address
                        for _, address in getaddresses(bcc_values)
                        if address
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
                            SELECT
                                id,
                                message_id,
                                mailbox
                            FROM emails
                            WHERE message_id = %s
                            LIMIT 1
                            """,
                            (item["id"],)
                        )

                        message_row = cur.fetchone()

                        if message_row:
                            existing_id, existing_message_id, existing_mailbox = message_row

                            existing_accounts = [
                                x.strip()
                                for x in str(existing_mailbox or "").split(",")
                                if x.strip()
                            ]

                            if mailbox not in existing_accounts:
                                existing_accounts.append(mailbox)

                            cur.execute(
                                """
                                UPDATE emails
                                SET
                                    thread_id = %s,
                                    sender = %s,
                                    receiver = %s,
                                    cc = %s,
                                    bcc = %s,
                                    subject = %s,
                                    body_text = %s,
                                    body_html = %s,
                                    received_at = %s,
                                    has_attachments = %s,
                                    category = %s,
                                    mailbox = %s,
                                    email_hash = %s
                                WHERE id = %s
                                """,
                                (
                                    thread_id,
                                    sender,
                                    receiver,
                                    cc,
                                    bcc,
                                    subject,
                                    body_text,
                                    body_html,
                                    received_at,
                                    has_attachments,
                                    category,
                                    ",".join(existing_accounts),
                                    email_hash,
                                    existing_id
                                )
                            )

                            skipped_duplicates += 1
                            total_skipped_duplicates += 1

                            print(
                                "Existing message updated:",
                                item["id"],
                                "| email id:",
                                existing_id,
                                "| mailboxes:",
                                ",".join(existing_accounts)
                            )

                            if has_attachments:
                                attachment_count = save_gmail_attachments(
                                    service,
                                    item["id"],
                                    existing_id,
                                    conn
                                )

                                if attachment_count:
                                    print(
                                        "Attachments saved:",
                                        attachment_count,
                                        "| email id:",
                                        existing_id
                                    )

                            continue

                        cur.execute(
                            """
                            SELECT id, mailbox
                            FROM emails
                            WHERE email_hash = %s
                            LIMIT 1
                            """,
                            (email_hash,)
                        )

                        hash_row = cur.fetchone()

                        if hash_row:
                            existing_id, existing_mailbox = hash_row

                            existing_accounts = [
                                x.strip()
                                for x in str(existing_mailbox or "").split(",")
                                if x.strip()
                            ]

                            if mailbox not in existing_accounts:
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
                                "Logical duplicate detected:",
                                item["id"],
                                "| existing email id:",
                                existing_id,
                                "| mailboxes:",
                                ",".join(existing_accounts)
                            )

                            if has_attachments:
                                attachment_count = save_gmail_attachments(
                                    service,
                                    item["id"],
                                    existing_id,
                                    conn
                                )

                                if attachment_count:
                                    print(
                                        "Attachments saved:",
                                        attachment_count,
                                        "| email id:",
                                        existing_id
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
                                email_hash,
                                cc,
                                bcc
                            )
                            VALUES (
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s,
                                %s, %s, %s, %s
                            )
                            RETURNING id
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
                                email_hash,
                                cc,
                                bcc
                            )
                        )

                        email_id = cur.fetchone()[0]

                        if has_attachments:
                            attachment_count = save_gmail_attachments(
                                service,
                                item["id"],
                                email_id,
                                conn
                            )

                            if attachment_count:
                                print(
                                    "Attachments saved:",
                                    attachment_count,
                                    "| email id:",
                                    email_id
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

            backfilled = backfill_missing_attachments(conn)

            if backfilled:
                print(
                    "Attachments backfilled:",
                    backfilled
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

