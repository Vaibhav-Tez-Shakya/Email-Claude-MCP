import hashlib
import os
import secrets
from datetime import datetime, timezone

import psycopg
from dotenv import load_dotenv

load_dotenv(".env")


def get_database_url():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")
    return database_url


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token():
    return secrets.token_urlsafe(32)


def create_user(name):
    conn = psycopg.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mcp_users (name)
                VALUES (%s)
                RETURNING id, name, active, created_at
                """,
                (name,)
            )
            row = cur.fetchone()

        conn.commit()
        return row

    finally:
        conn.close()


def create_token(user_id):
    token = generate_token()
    token_hash = hash_token(token)

    conn = psycopg.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mcp_tokens (user_id, token_hash)
                VALUES (%s, %s)
                RETURNING id, user_id, created_at
                """,
                (user_id, token_hash)
            )
            row = cur.fetchone()

        conn.commit()

        return {
            "token": token,
            "id": row[0],
            "user_id": row[1],
            "created_at": row[2],
        }

    finally:
        conn.close()


def validate_token(token):
    if not token:
        return None

    token_hash = hash_token(token)

    conn = psycopg.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    u.id,
                    u.name,
                    u.active,
                    t.id,
                    t.expires_at,
                    t.revoked_at
                FROM mcp_tokens t
                JOIN mcp_users u ON u.id = t.user_id
                WHERE t.token_hash = %s
                """,
                (token_hash,)
            )

            row = cur.fetchone()

            if not row:
                return None

            user_id, name, active, token_id, expires_at, revoked_at = row

            now = datetime.now(timezone.utc)

            if not active:
                return None

            if revoked_at is not None:
                return None

            if expires_at is not None and expires_at <= now:
                return None

            cur.execute(
                """
                UPDATE mcp_tokens
                SET last_used_at = NOW()
                WHERE id = %s
                """,
                (token_id,)
            )

        conn.commit()

        return {
            "user_id": user_id,
            "name": name,
            "token_id": token_id,
        }

    finally:
        conn.close()


def revoke_token(token_id):
    conn = psycopg.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE mcp_tokens
                SET revoked_at = NOW()
                WHERE id = %s
                RETURNING id, revoked_at
                """,
                (token_id,)
            )
            row = cur.fetchone()

        conn.commit()
        return row

    finally:
        conn.close()
