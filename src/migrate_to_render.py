import os
import psycopg
from dotenv import load_dotenv

base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

load_dotenv(os.path.join(base, ".env"))

local_db = os.getenv("DATABASE_URL")
render_db = os.getenv("RENDER_DB_URL")

local_conn = psycopg.connect(local_db)
render_conn = psycopg.connect(render_db)

with local_conn.cursor() as local_cur:
    local_cur.execute("""
        SELECT
            message_id,
            thread_id,
            sender,
            receiver,
            subject,
            body_text,
            body_html,
            received_at,
            has_attachments,
            created_at
        FROM emails
        ORDER BY id
    """)

    emails = local_cur.fetchall()

with render_conn.cursor() as render_cur:
    for email in emails:
        render_cur.execute(
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
                created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (message_id) DO NOTHING
            """,
            email
        )

render_conn.commit()

local_conn.close()
render_conn.close()

print(f"Migrated {len(emails)} local emails to Render PostgreSQL")