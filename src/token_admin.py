import argparse
from datetime import datetime, timezone

import psycopg

from src.token_store import (
    create_user,
    create_token,
    get_database_url,
    revoke_token,
)


def list_users():
    conn = psycopg.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    u.id,
                    u.name,
                    u.active,
                    COUNT(t.id) AS token_count
                FROM mcp_users u
                LEFT JOIN mcp_tokens t
                    ON t.user_id = u.id
                GROUP BY u.id, u.name, u.active
                ORDER BY u.id
                """
            )

            for row in cur.fetchall():
                print(
                    f"ID={row[0]} | NAME={row[1]} | "
                    f"ACTIVE={row[2]} | TOKENS={row[3]}"
                )
    finally:
        conn.close()


def list_tokens():
    conn = psycopg.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id,
                    t.user_id,
                    u.name,
                    t.created_at,
                    t.expires_at,
                    t.revoked_at,
                    t.last_used_at
                FROM mcp_tokens t
                JOIN mcp_users u
                    ON u.id = t.user_id
                ORDER BY t.id
                """
            )

            now = datetime.now(timezone.utc)

            for row in cur.fetchall():
                (
                    token_id,
                    user_id,
                    name,
                    created_at,
                    expires_at,
                    revoked_at,
                    last_used_at,
                ) = row

                if revoked_at is not None:
                    status = "REVOKED"
                elif expires_at is not None and expires_at <= now:
                    status = "EXPIRED"
                else:
                    status = "ACTIVE"

                print(
                    f"ID={token_id} | USER={user_id} | NAME={name} | "
                    f"STATUS={status} | CREATED={created_at} | "
                    f"EXPIRES={expires_at} | LAST_USED={last_used_at}"
                )
    finally:
        conn.close()


def delete_user(user_id):
    conn = psycopg.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name
                FROM mcp_users
                WHERE id = %s
                """,
                (user_id,),
            )

            user = cur.fetchone()

            if not user:
                print("USER_NOT_FOUND")
                return

            cur.execute(
                """
                DELETE FROM mcp_users
                WHERE id = %s
                RETURNING id, name
                """,
                (user_id,),
            )

            deleted = cur.fetchone()

        conn.commit()

        print(f"DELETED_USER_ID: {deleted[0]}")
        print(f"DELETED_USER_NAME: {deleted[1]}")
    finally:
        conn.close()



def get_users():
    conn = psycopg.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    u.id,
                    u.name,
                    u.active,
                    u.created_at,
                    COUNT(t.id) AS token_count
                FROM mcp_users u
                LEFT JOIN mcp_tokens t
                    ON t.user_id = u.id
                GROUP BY u.id, u.name, u.active, u.created_at
                ORDER BY u.id
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


def get_tokens():
    conn = psycopg.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id,
                    t.user_id,
                    u.name,
                    t.created_at,
                    t.expires_at,
                    t.revoked_at,
                    t.last_used_at
                FROM mcp_tokens t
                JOIN mcp_users u
                    ON u.id = t.user_id
                ORDER BY t.id DESC
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


def get_user(user_id):
    conn = psycopg.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name, active, created_at
                FROM mcp_users
                WHERE id = %s
                """,
                (user_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def set_user_active(user_id, active):
    conn = psycopg.connect(get_database_url())

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE mcp_users
                SET active = %s
                WHERE id = %s
                RETURNING id, name, active
                """,
                (active, user_id),
            )
            row = cur.fetchone()

        conn.commit()
        return row
    finally:
        conn.close()

def main():
    parser = argparse.ArgumentParser(
        description="MCP token administration"
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    create_user_parser = subparsers.add_parser("create-user")
    create_user_parser.add_argument("name")

    create_token_parser = subparsers.add_parser("create-token")
    create_token_parser.add_argument("user_id", type=int)

    subparsers.add_parser("list-users")
    subparsers.add_parser("list-tokens")

    revoke_parser = subparsers.add_parser("revoke-token")
    revoke_parser.add_argument("token_id", type=int)

    delete_user_parser = subparsers.add_parser("delete-user")
    delete_user_parser.add_argument("user_id", type=int)

    args = parser.parse_args()

    if args.command == "create-user":
        row = create_user(args.name)
        print(f"USER_ID: {row[0]}")
        print(f"NAME: {row[1]}")

    elif args.command == "create-token":
        result = create_token(args.user_id)
        print(f"TOKEN_ID: {result['id']}")
        print(f"USER_ID: {result['user_id']}")
        print(f"TOKEN: {result['token']}")

    elif args.command == "list-users":
        list_users()

    elif args.command == "list-tokens":
        list_tokens()

    elif args.command == "revoke-token":
        result = revoke_token(args.token_id)

        if result:
            print(f"REVOKED_TOKEN_ID: {result[0]}")
            print(f"REVOKED_AT: {result[1]}")
        else:
            print("TOKEN_NOT_FOUND")

    elif args.command == "delete-user":
        delete_user(args.user_id)


if __name__ == "__main__":
    main()
