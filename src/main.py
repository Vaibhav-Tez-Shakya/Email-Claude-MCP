from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pathlib import Path
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from email import message_from_bytes
from email.utils import parsedate_to_datetime
import psycopg
import os
import base64


SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

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

def sync_emails():
    print("Starting email sync...")

    service = get_gmail_service()

    result = service.users().messages().list(
        userId="me",
        maxResults=10,
        includeSpamTrash=True
    ).execute()

    messages = result.get("messages", [])

    conn = psycopg.connect(
        os.getenv("DATABASE_URL")
    )

    processed = 0
    spam_detected = 0

    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM emails WHERE category = 'SPAM'"
            )

        for item in messages:

            message = service.users().messages().get(
                userId="me",
                id=item["id"],
                format="raw"
            ).execute()

            label_ids = message.get("labelIds", [])

            category = get_email_category(label_ids)

            if category == "SPAM":
                spam_detected += 1

                print(
                    "Spam detected:",
                    item["id"],
                    "| not saved to database"
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
                        message.get("X-GM-THRID"),
                        msg.get("From"),
                        msg.get("To"),
                        msg.get("Subject"),
                        body_text,
                        body_html,
                        received_at,
                        has_attachments,
                        category
                    )
                )

            processed += 1

            print(
                "Synced:",
                msg.get("Subject"),
                "| category:",
                category,
                "| body:",
                len(body_text),
                "chars"
            )

        conn.commit()

    finally:
        conn.close()

    update_spam_status(spam_detected, len(messages))

    print()
    print("Sync complete")
    print("Processed:", processed)
    print("Spam detected:", spam_detected)
    print("Spam saved to database: 0")

    return processed

if __name__ == "__main__":
    sync_emails()
