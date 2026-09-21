import base64
import os
import secrets
from datetime import datetime, timezone

from starlette.responses import HTMLResponse, RedirectResponse
from starlette.requests import Request

from src.token_admin import (
    create_user,
    create_token,
    get_users,
    get_tokens,
    revoke_token,
)


def admin_credentials_valid(request):
    authorization = request.headers.get("Authorization", "")

    if not authorization.startswith("Basic "):
        return False

    try:
        decoded = base64.b64decode(
            authorization[6:],
            validate=True,
        ).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        return False

    expected_username = os.getenv("ADMIN_USERNAME")
    expected_password = os.getenv("ADMIN_PASSWORD")

print(
    f"ADMIN_AUTH_DEBUG username_present={bool(expected_username)} "
    f"password_present={bool(expected_password)} "
    f"password_length={len(expected_password or "")}"
)

    if not expected_username or not expected_password:
        return False

    return (
        secrets.compare_digest(username, expected_username)
        and secrets.compare_digest(password, expected_password)
    )


def unauthorized():
    return HTMLResponse(
        "<h1>401 Unauthorized</h1>",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="MCP Admin"'},
    )


def status_for_token(expires_at, revoked_at):
    now = datetime.now(timezone.utc)

    if revoked_at is not None:
        return "REVOKED"

    if expires_at is not None and expires_at <= now:
        return "EXPIRED"

    return "ACTIVE"


def admin_page(request, one_time_token=None, message=None):
    if not admin_credentials_valid(request):
        return unauthorized()

    users = get_users()
    tokens = get_tokens()

    user_rows = ""

    for user_id, name, active, created_at, token_count in users:
        status = "ACTIVE" if active else "DISABLED"

        user_rows += f"""
        <tr>
            <td>{user_id}</td>
            <td>{name}</td>
            <td>{status}</td>
            <td>{created_at}</td>
            <td>{token_count}</td>
            <td>
                <form method="post" action="/admin/create-token">
                    <input type="hidden" name="user_id" value="{user_id}">
                    <button type="submit">Generate Token</button>
                </form>
            </td>
        </tr>
        """

    token_rows = ""

    for (
        token_id,
        user_id,
        name,
        created_at,
        expires_at,
        revoked_at,
        last_used_at,
    ) in tokens:
        status = status_for_token(expires_at, revoked_at)

        action = ""

        if status == "ACTIVE":
            action = f"""
            <form method="post" action="/admin/revoke-token">
                <input type="hidden" name="token_id" value="{token_id}">
                <button type="submit">Revoke</button>
            </form>
            """

        token_rows += f"""
        <tr>
            <td>{token_id}</td>
            <td>{user_id}</td>
            <td>{name}</td>
            <td>{status}</td>
            <td>{created_at}</td>
            <td>{expires_at or "-"}</td>
            <td>{last_used_at or "Never"}</td>
            <td>{action}</td>
        </tr>
        """

    token_box = ""

    if one_time_token:
        token_box = f"""
        <div class="token-box">
            <strong>New token — copy it now:</strong>
            <input id="newToken" value="Bearer {one_time_token}" readonly>
            <button onclick="copyToken()">Copy Bearer Token</button>
            <p>This token is shown only once.</p>
        </div>
        """

    message_box = ""

    if message:
        message_box = f'<div class="message">{message}</div>'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Email Claude MCP Admin</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 40px;
                background: #f5f5f5;
                color: #222;
            }}

            h1 {{
                margin-bottom: 30px;
            }}

            section {{
                background: white;
                padding: 20px;
                margin-bottom: 25px;
                border-radius: 8px;
            }}

            table {{
                border-collapse: collapse;
                width: 100%;
                margin-top: 15px;
            }}

            th, td {{
                border: 1px solid #ddd;
                padding: 10px;
                text-align: left;
            }}

            th {{
                background: #eee;
            }}

            input {{
                padding: 8px;
                margin-right: 8px;
            }}

            button {{
                padding: 8px 12px;
                cursor: pointer;
            }}

            .token-box {{
                background: #fff3cd;
                border: 1px solid #ffe69c;
                padding: 15px;
                margin-bottom: 25px;
            }}

            .message {{
                background: #d1e7dd;
                padding: 12px;
                margin-bottom: 20px;
            }}

            .token-box input {{
                width: 500px;
                max-width: 80%;
            }}
        </style>
    </head>

    <body>
        <h1>Email Claude MCP Admin</h1>

        {message_box}
        {token_box}

        <section>
            <h2>Create User</h2>

            <form method="post" action="/admin/create-user">
                <input
                    type="text"
                    name="name"
                    placeholder="User name"
                    required
                >
                <button type="submit">Create User</button>
            </form>
        </section>

        <section>
            <h2>Users</h2>

            <table>
                <tr>
                    <th>ID</th>
                    <th>Name</th>
                    <th>Status</th>
                    <th>Created</th>
                    <th>Tokens</th>
                    <th>Action</th>
                </tr>

                {user_rows}
            </table>
        </section>

        <section>
            <h2>Tokens</h2>

            <table>
                <tr>
                    <th>Token ID</th>
                    <th>User ID</th>
                    <th>User</th>
                    <th>Status</th>
                    <th>Created</th>
                    <th>Expires</th>
                    <th>Last Used</th>
                    <th>Action</th>
                </tr>

                {token_rows}
            </table>
        </section>

        <script>
            function copyToken() {{
                const input = document.getElementById("newToken");
                navigator.clipboard.writeText(input.value);
            }}
        </script>
    </body>
    </html>
    """

    return HTMLResponse(html)


async def admin_home(request):
    return admin_page(request)


async def admin_create_user(request):
    if not admin_credentials_valid(request):
        return unauthorized()

    form = await request.form()
    name = str(form.get("name", "")).strip()

    if not name:
        return admin_page(request, message="User name is required.")

    create_user(name)

    return RedirectResponse(
        "/admin",
        status_code=303,
    )


async def admin_create_token(request):
    if not admin_credentials_valid(request):
        return unauthorized()

    form = await request.form()

    try:
        user_id = int(form.get("user_id"))
    except (TypeError, ValueError):
        return admin_page(request, message="Invalid user ID.")

    result = create_token(user_id)

    return admin_page(
        request,
        one_time_token=result["token"],
    )


async def admin_revoke_token(request):
    if not admin_credentials_valid(request):
        return unauthorized()

    form = await request.form()

    try:
        token_id = int(form.get("token_id"))
    except (TypeError, ValueError):
        return admin_page(request, message="Invalid token ID.")

    revoke_token(token_id)

    return RedirectResponse(
        "/admin",
        status_code=303,
    )

