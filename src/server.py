import contextlib
import io
import os

import psycopg
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp.server import MCPServer

from main import sync_emails


base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(base, ".env"))

mcp = MCPServer("Email Claude MCP")


@mcp.tool()
def search_emails(query: str, limit: int = 10) -> list[dict]:
    """Smart search emails using keywords and filters."""

    import re

    conn = psycopg.connect(os.getenv("DATABASE_URL"))

    try:
        query = query.strip()
        limit = max(1, min(limit, 50))

        filters = []
        filter_params = []
        search_terms = []

        tokens = re.findall(r'"[^"]+"|\S+', query)

        for token in tokens:
            if token.startswith("from:"):
                value = token[5:].strip('"')
                filters.append("sender ILIKE %s")
                filter_params.append(f"%{value}%")

            elif token.startswith("subject:"):
                value = token[8:].strip('"')
                filters.append("subject ILIKE %s")
                filter_params.append(f"%{value}%")

            elif token.startswith("category:"):
                value = token[9:].strip('"')
                filters.append("category ILIKE %s")
                filter_params.append(f"%{value}%")

            elif token.startswith("mailbox:"):
                value = token[8:].strip('"')
                filters.append("mailbox ILIKE %s")
                filter_params.append(f"%{value}%")

            elif token.startswith("after:"):
                value = token[6:].strip('"')
                filters.append("received_at >= %s::timestamptz")
                filter_params.append(value)

            elif token.startswith("before:"):
                value = token[7:].strip('"')
                filters.append("received_at < %s::timestamptz")
                filter_params.append(value)

            else:
                value = token.strip('"')
                if value:
                    search_terms.append(value)

        where_sql = ""

        if search_terms:
            for term in search_terms:
                pattern = f"%{term}%"

                filters.append(
                    """
                    (
                        sender ILIKE %s
                        OR receiver ILIKE %s
                        OR subject ILIKE %s
                        OR body_text ILIKE %s
                    )
                    """
                )

                filter_params.extend(
                    [pattern, pattern, pattern, pattern]
                )

        if filters:
            where_sql = "WHERE " + " AND ".join(filters)

        ranking_parts = []
        ranking_params = []

        for term in search_terms:
            pattern = f"%{term}%"

            ranking_parts.append(
                """
                (
                    CASE WHEN subject ILIKE %s THEN 10 ELSE 0 END
                    + CASE WHEN sender ILIKE %s THEN 7 ELSE 0 END
                    + CASE WHEN receiver ILIKE %s THEN 5 ELSE 0 END
                    + CASE WHEN body_text ILIKE %s THEN 2 ELSE 0 END
                )
                """
            )

            ranking_params.extend(
                [pattern, pattern, pattern, pattern]
            )

        # Reward explicit filter matches in relevance ranking.
        filter_ranking_parts = []
        filter_ranking_params = []

        for filter_sql, filter_param in zip(filters, filter_params):
            if "sender ILIKE %s" in filter_sql:
                filter_ranking_parts.append(
                    "CASE WHEN sender ILIKE %s THEN 7 ELSE 0 END"
                )
                filter_ranking_params.append(filter_param)

            elif "subject ILIKE %s" in filter_sql:
                filter_ranking_parts.append(
                    "CASE WHEN subject ILIKE %s THEN 10 ELSE 0 END"
                )
                filter_ranking_params.append(filter_param)

            elif "category ILIKE %s" in filter_sql:
                filter_ranking_parts.append(
                    "CASE WHEN category ILIKE %s THEN 3 ELSE 0 END"
                )
                filter_ranking_params.append(filter_param)

            elif "mailbox ILIKE %s" in filter_sql:
                filter_ranking_parts.append(
                    "CASE WHEN mailbox ILIKE %s THEN 3 ELSE 0 END"
                )
                filter_ranking_params.append(filter_param)

        if filter_ranking_parts:
            ranking_sql = " + ".join(ranking_parts + filter_ranking_parts)
            ranking_params.extend(filter_ranking_params)
        else:
            ranking_sql = " + ".join(ranking_parts) if ranking_parts else "0"

        sql = f"""
            SELECT
                id,
                sender,
                receiver,
                subject,
                category,
                mailbox,
                received_at,
                ({ranking_sql}) AS relevance
            FROM emails
            {where_sql}
            ORDER BY relevance DESC, received_at DESC NULLS LAST, id DESC
            LIMIT %s
        """

        params = ranking_params + filter_params + [limit]

        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()

        return [
            {
                "id": row[0],
                "sender": row[1],
                "receiver": row[2],
                "subject": row[3],
                "category": row[4],
                "mailbox": row[5],
                "received_at": row[6].isoformat() if row[6] else None,
                "relevance": row[7]
            }
            for row in rows
        ]

    finally:
        conn.close()
@mcp.tool()
def get_email(email_id: int) -> dict:
    """Get the complete stored email by database ID, including its Gmail category."""

    conn = psycopg.connect(os.getenv("DATABASE_URL"))

    try:
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
                    created_at,
                    category,
                    mailbox
                FROM emails
                WHERE id = %s
                """,
                (email_id,)
            )

            row = cur.fetchone()

            if row:
                cur.execute(
                    "SELECT COUNT(*) FROM email_attachments WHERE email_id = %s",
                    (email_id,)
                )
                attachment_count = cur.fetchone()[0]
            else:
                attachment_count = 0

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
            "attachment_count": attachment_count,
            "created_at": row[10].isoformat() if row[10] else None,
            "category": row[11],
            "mailbox": row[12]
        }

    finally:
        conn.close()

@mcp.tool()
def get_email_attachments(email_id: int, include_content: bool = True, max_chars: int = 20000) -> list[dict]:
    """Get attachments for an email, including extracted attachment text when available."""

    max_chars = max(1000, min(max_chars, 100000))

    conn = psycopg.connect(os.getenv("DATABASE_URL"))

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    gmail_attachment_id,
                    filename,
                    mime_type,
                    file_size,
                    content_status,
                    content_text,
                    created_at,
                    content_hash
                FROM email_attachments
                WHERE email_id = %s
                ORDER BY id
                """,
                (email_id,)
            )

            rows = cur.fetchall()

        return [
            {
                "id": row[0],
                "filename": row[2],
                "mime_type": row[3],
                "file_size": row[4],
                "content_status": row[5],
                "content_text": (
                    row[6][:max_chars]
                    if include_content and row[6]
                    else None
                ),
                "content_text_length": len(row[6]) if row[6] else 0,
                "content_truncated": bool(
                    include_content and row[6] and len(row[6]) > max_chars
                ),
                "created_at": row[7].isoformat() if row[7] else None
            }
            for row in rows
        ]

    finally:
        conn.close()


@mcp.tool()
def list_emails(limit: int = 20) -> list[dict]:
    """List the most recent emails stored in the database, including Gmail category."""

    limit = max(1, min(limit, 100))

    conn = psycopg.connect(os.getenv("DATABASE_URL"))

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, sender, receiver, subject, category, mailbox, received_at
                FROM emails
                ORDER BY received_at DESC NULLS LAST, id DESC
                LIMIT %s
                """,
                (limit,)
            )

            rows = cur.fetchall()

        return [
            {
                "id": row[0],
                "sender": row[1],
                "receiver": row[2],
                "subject": row[3],
                "category": row[4],
                "mailbox": row[5],
                "received_at": row[6].isoformat() if row[6] else None
            }
            for row in rows
        ]

    finally:
        conn.close()


@mcp.tool()
def filter_emails(category: str, limit: int = 20) -> list[dict]:
    """Filter emails by Gmail category. Valid categories are PRIMARY, PROMOTIONS, SOCIAL, UPDATES, and SPAM."""

    category = category.strip().upper()

    valid_categories = {
        "PRIMARY",
        "PROMOTIONS",
        "SOCIAL",
        "UPDATES",
        "SPAM"
    }

    if category not in valid_categories:
        return [
            {
                "error": "Invalid category",
                "valid_categories": sorted(valid_categories)
            }
        ]

    limit = max(1, min(limit, 100))

    conn = psycopg.connect(os.getenv("DATABASE_URL"))

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, sender, receiver, subject, category, mailbox, received_at
                FROM emails
                WHERE category = %s
                ORDER BY received_at DESC NULLS LAST, id DESC
                LIMIT %s
                """,
                (category, limit)
            )

            rows = cur.fetchall()

        return [
            {
                "id": row[0],
                "sender": row[1],
                "receiver": row[2],
                "subject": row[3],
                "category": row[4],
                "mailbox": row[5],
                "received_at": row[6].isoformat() if row[6] else None
            }
            for row in rows
        ]

    finally:
        conn.close()


@mcp.tool()
def get_spam_status() -> dict:
    """Return spam detection status from the latest email sync."""

    conn = psycopg.connect(os.getenv("DATABASE_URL"))

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT detected_count, checked_count, last_sync_at
                FROM spam_status
                WHERE id = 1
                """
            )

            row = cur.fetchone()

        if not row:
            return {
                "spam_detected": False,
                "spam_count": 0,
                "checked_count": 0,
                "last_sync_at": None,
                "message": "No spam detection data is available yet."
            }

        detected_count = row[0]
        checked_count = row[1]
        last_sync_at = row[2]

        if detected_count > 0:
            message = (
                f"Yes, I detected {detected_count} spam email"
                f"{'s' if detected_count != 1 else ''} during the latest sync. "
                "They were not saved to the database."
            )
        else:
            message = (
                "No spam detected during the latest sync."
            )

        return {
            "spam_detected": detected_count > 0,
            "spam_count": detected_count,
            "checked_count": checked_count,
            "last_sync_at": last_sync_at.isoformat() if last_sync_at else None,
            "message": message
        }

    finally:
        conn.close()

@mcp.tool()
def count_emails() -> dict:
    """Return the exact number of emails stored in the database."""

    conn = psycopg.connect(os.getenv("DATABASE_URL"))

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM emails")
            count = cur.fetchone()[0]

        return {
            "total_emails": count
        }

    finally:
        conn.close()


async def sync_endpoint(request: Request):
    provided_token = request.headers.get("X-Sync-Token")
    expected_token = os.getenv("SYNC_TOKEN")

    if not expected_token:
        return JSONResponse(
            {"error": "SYNC_TOKEN is not configured"},
            status_code=500
        )

    if provided_token != expected_token:
        return JSONResponse(
            {"error": "Unauthorized"},
            status_code=401
        )

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            processed = sync_emails()

        return JSONResponse(
            {
                "success": True,
                "processed": processed
            }
        )

    except Exception as e:
        print("Sync endpoint failed:")
        print(type(e).__name__, "-", e)

        return JSONResponse(
            {
                "success": False,
                "error": type(e).__name__
            },
            status_code=500
        )


async def health_endpoint(request: Request):
    return JSONResponse(
        {
            "status": "ok"
        }
    )


@contextlib.asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


mcp_app = mcp.streamable_http_app(
    host="0.0.0.0"
)


app = Starlette(
    routes=[
        Route("/sync", sync_endpoint, methods=["POST"]),
        Route("/health", health_endpoint, methods=["GET"]),
        Mount("/", app=mcp_app),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000"))
    )
