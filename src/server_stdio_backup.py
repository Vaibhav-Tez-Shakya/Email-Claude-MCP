from mcp.server import MCPServer
from dotenv import load_dotenv
import psycopg
import os

base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base, ".env"))

mcp = MCPServer("Email Claude MCP")


@mcp.tool()
def search_emails(query: str) -> list[dict]:
    """Search stored emails by subject, sender, receiver, or body."""

    conn = psycopg.connect(os.getenv("DATABASE_URL"))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, sender, receiver, subject, received_at
            FROM emails
            WHERE
                sender ILIKE %s
                OR receiver ILIKE %s
                OR subject ILIKE %s
                OR body_text ILIKE %s
            ORDER BY received_at DESC
            LIMIT 10
            """,
            tuple([f"%{query}%"] * 4)
        )

        rows = cur.fetchall()

    conn.close()

    return [
        {
            "id": row[0],
            "sender": row[1],
            "receiver": row[2],
            "subject": row[3],
            "received_at": row[4].isoformat() if row[4] else None
        }
        for row in rows
    ]


@mcp.tool()
def get_email(email_id: int) -> dict:
    """Get the complete stored email by database ID."""

    conn = psycopg.connect(os.getenv("DATABASE_URL"))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                id,
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
            WHERE id = %s
            """,
            (email_id,)
        )

        row = cur.fetchone()

    conn.close()

    if not row:
        return {"error": "Email not found"}

    return {
        "id": row[0],
        "message_id": row[1],
        "thread_id": row[2],
        "sender": row[3],
        "receiver": row[4],
        "subject": row[5],
        "body_text": row[6],
        "body_html": row[7],
        "received_at": row[8].isoformat() if row[8] else None,
        "has_attachments": row[9],
        "created_at": row[10].isoformat() if row[10] else None
    }


if __name__ == "__main__":
    import asyncio
    asyncio.run(mcp.run_stdio_async())
