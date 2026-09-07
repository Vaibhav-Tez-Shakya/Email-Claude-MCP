import contextlib
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
