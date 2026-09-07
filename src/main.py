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

load_dotenv(base / ".env")


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


def sync_emails():
    print("Starting email sync...")

    service = get_gmail_service()

    result = service.users().messages().list(
        userId="me",
        maxResults=10
    ).execute()

    messages = result.get("messages", [])

    if not messages:
        print("No emails found")
        return 0

    conn = psycopg.connect(
        os.getenv("DATABASE_URL")
    )

    processed = 0

    try:
        for item in messages:

            message = service.users().messages().get(
                userId="me",
                id=item["id"],
                format="raw"
            ).execute()

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
                        has_attachments
                    )
                    VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
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
                        has_attachments = EXCLUDED.has_attachments
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
                        has_attachments
                    )
                )

            processed += 1

            print(
                "Synced:",
                msg.get("Subject"),
                "| body:",
                len(body_text),
                "chars"
            )

        conn.commit()

    finally:
        conn.close()

    print()
    print("Sync complete")
    print("Processed:", processed)

    return processed


if __name__ == "__main__":
    sync_emails()
