import base64
import os
import secrets
from datetime import datetime, timezone

from starlette.responses import HTMLResponse, RedirectResponse
from starlette.requests import Request

_pending_tokens = {}

from src.token_admin import (
    create_user,
    create_token,
    get_users,
    get_tokens,
    revoke_token,
)


def admin_credentials_valid(request):
    authorization = request.headers.get("Authorization", "")

    expected_username = os.getenv("ADMIN_USERNAME")
    expected_password = os.getenv("ADMIN_PASSWORD")

    print(
        f"ADMIN_AUTH_DEBUG username_present={bool(expected_username)} "
        f"password_present={bool(expected_password)} "
        f"password_length={len(expected_password or "")} "
        f"authorization_present={bool(authorization)} "
        f"basic_auth={authorization.startswith("Basic ")}"
    )

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

    if one_time_token is None:
        token_key = request.query_params.get("token_notice")
        if token_key:
            one_time_token = _pending_tokens.pop(token_key, None)

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
                <button class="button-danger" type="submit">Revoke</button>
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
            <strong>New token - copy it now:</strong>
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
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Email Claude MCP Admin</title>

        <style>
            :root {{
                --bg: #070505;
                --panel: #100c0c;
                --panel-soft: #151010;
                --border: #2a1d1d;
                --text: #f2ece9;
                --muted: #9b8f8b;
                --accent: #d34a24;
                --accent-dark: #a20903;
                --success: #57b987;
                --danger: #e05b5b;
                --shadow: 0 18px 45px rgba(0, 0, 0, 0.25);
            }}

            * {{
                box-sizing: border-box;
            }}

            html {{
                scroll-behavior: smooth;
            }}

            body {{
                margin: 0;
                min-height: 100vh;
                background: var(--bg);
                color: var(--text);
                font-family:
                    Inter, ui-sans-serif, system-ui, -apple-system,
                    BlinkMacSystemFont, "Segoe UI", sans-serif;
            }}

            button,
            input {{
                font: inherit;
            }}

            button {{
                cursor: pointer;
            }}

            .app {{
                min-height: 100vh;
                display: flex;
            }}

            .sidebar {{
                width: 240px;
                min-height: 100vh;
                position: fixed;
                left: 0;
                top: 0;
                bottom: 0;
                padding: 28px 18px;
                background: #090606;
                border-right: 1px solid var(--border);
                display: flex;
                flex-direction: column;
                z-index: 10;
            }}

            .brand {{
                display: flex;
                align-items: center;
                gap: 12px;
                padding: 0 10px;
                margin-bottom: 38px;
            }}

            .brand-mark {{
                width: 34px;
                height: 34px;
                border-radius: 10px;
                display: grid;
                place-items: center;
                background: linear-gradient(
                    145deg,
                    var(--accent),
                    var(--accent-dark)
                );
                color: white;
                font-size: 15px;
                font-weight: 800;
            }}

            .brand-text {{
                font-size: 14px;
                font-weight: 700;
                letter-spacing: -0.2px;
            }}

            .brand-subtitle {{
                margin-top: 3px;
                color: var(--muted);
                font-size: 10px;
            }}

            .nav-label {{
                padding: 0 11px;
                margin-bottom: 10px;
                color: #665b58;
                font-size: 10px;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 1.3px;
            }}

            .nav {{
                display: flex;
                flex-direction: column;
                gap: 5px;
            }}

            .nav-item {{
                display: flex;
                align-items: center;
                gap: 11px;
                padding: 11px 12px;
                border-radius: 9px;
                color: var(--muted);
                text-decoration: none;
                font-size: 13px;
                transition: 0.18s ease;
            }}

            .nav-item:hover {{
                background: var(--panel-soft);
                color: var(--text);
            }}

            .nav-item.active {{
                background: rgba(162, 9, 3, 0.16);
                color: #f0d8d3;
                border: 1px solid rgba(162, 9, 3, 0.28);
            }}

            .nav-item.active .nav-icon {{
                border-color: rgba(211, 74, 36, 0.45);
                background: rgba(211, 74, 36, 0.12);
                color: #f0b19d;
            }}

            .nav-icon {{
                width: 22px;
                height: 22px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                flex: 0 0 22px;
                border: 1px solid var(--border);
                border-radius: 6px;
                background: rgba(255, 255, 255, 0.025);
                color: #b9aaa5;
                font-size: 10px;
                font-weight: 800;
                letter-spacing: 0.2px;
            }}

            .sidebar-bottom {{
                margin-top: auto;
                padding: 12px;
                border: 1px solid var(--border);
                border-radius: 11px;
                background: var(--panel);
            }}

            .sidebar-bottom-title {{
                font-size: 11px;
                font-weight: 700;
                margin-bottom: 5px;
            }}

            .sidebar-bottom-text {{
                color: var(--muted);
                font-size: 10px;
                line-height: 1.5;
            }}

            .main {{
                width: calc(100% - 240px);
                margin-left: 240px;
                padding: 28px 34px 50px;
            }}

            .topbar {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 20px;
                margin-bottom: 32px;
            }}

            .page-title {{
                margin: 0;
                font-size: 26px;
                font-weight: 700;
                letter-spacing: -0.8px;
            }}

            .page-subtitle {{
                margin: 7px 0 0;
                color: var(--muted);
                font-size: 12px;
            }}

            .admin-badge {{
                display: flex;
                align-items: center;
                gap: 8px;
                padding: 9px 12px;
                border: 1px solid var(--border);
                border-radius: 10px;
                background: var(--panel);
                color: var(--muted);
                font-size: 11px;
            }}

            .status-dot {{
                width: 7px;
                height: 7px;
                border-radius: 50%;
                background: var(--success);
                box-shadow: 0 0 9px rgba(87, 185, 135, 0.55);
            }}

            .dashboard-grid {{
                display: grid;
                grid-template-columns: repeat(4, minmax(0, 1fr));
                gap: 14px;
                margin-bottom: 22px;
            }}

            .stat-card {{
                min-height: 112px;
                padding: 18px;
                background: var(--panel);
                border: 1px solid var(--border);
                border-radius: 14px;
                box-shadow: var(--shadow);
            }}

            .stat-label {{
                color: var(--muted);
                font-size: 11px;
                font-weight: 600;
            }}

            .stat-value {{
                margin-top: 12px;
                font-size: 27px;
                font-weight: 750;
                letter-spacing: -1px;
            }}

            .stat-note {{
                margin-top: 5px;
                color: #706561;
                font-size: 10px;
            }}

            .content-card {{
                margin-bottom: 18px;
                padding: 21px;
                background: var(--panel);
                border: 1px solid var(--border);
                border-radius: 14px;
                box-shadow: var(--shadow);
            }}

            .section-header {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 15px;
                margin-bottom: 16px;
            }}

            .section-title {{
                margin: 0;
                font-size: 15px;
                font-weight: 700;
            }}

            .section-description {{
                margin: 4px 0 0;
                color: var(--muted);
                font-size: 11px;
            }}

            .create-user-form {{
                display: flex;
                gap: 9px;
                flex-wrap: wrap;
            }}

            input {{
                min-width: 220px;
                padding: 10px 12px;
                border: 1px solid var(--border);
                border-radius: 9px;
                outline: none;
                background: #0b0808;
                color: var(--text);
            }}

            input::placeholder {{
                color: #665c59;
            }}

            input:focus {{
                border-color: rgba(211, 74, 36, 0.65);
                box-shadow: 0 0 0 3px rgba(211, 74, 36, 0.08);
            }}

            button {{
                border: 0;
                border-radius: 8px;
                padding: 9px 13px;
                background: var(--accent-dark);
                color: white;
                font-size: 11px;
                font-weight: 700;
                transition: 0.18s ease;
            }}

            button:hover {{
                background: var(--accent);
                transform: translateY(-1px);
            }}

            .button-danger {{
                background: rgba(176, 35, 35, 0.18);
                border: 1px solid rgba(224, 91, 91, 0.32);
                color: #f0aaa8;
            }}

            .button-danger:hover {{
                background: rgba(224, 91, 91, 0.22);
                border-color: rgba(224, 91, 91, 0.48);
                color: #ffd0ce;
            }}

            .table-wrap {{
                overflow-x: auto;
                border: 1px solid var(--border);
                border-radius: 10px;
            }}

            table {{
                width: 100%;
                min-width: 760px;
                border-collapse: collapse;
            }}

            th,
            td {{
                padding: 12px 13px;
                border-bottom: 1px solid var(--border);
                text-align: left;
                white-space: nowrap;
                font-size: 11px;
            }}

            th {{
                background: #0c0909;
                color: #817572;
                font-size: 9px;
                text-transform: uppercase;
                letter-spacing: 0.8px;
            }}

            td {{
                color: #d7ceca;
            }}

            .cell-secondary {{
                color: var(--muted);
                font-family: Consolas, monospace;
                font-size: 10px;
            }}

            tr:last-child td {{
                border-bottom: 0;
            }}

            tr:hover td {{
                background: rgba(255, 255, 255, 0.015);
            }}

            td form {{
                margin: 0;
            }}

            .token-box {{
                margin-bottom: 18px;
                padding: 17px;
                background: rgba(211, 74, 36, 0.08);
                border: 1px solid rgba(211, 74, 36, 0.3);
                border-radius: 12px;
            }}

            .token-box strong {{
                display: block;
                margin-bottom: 9px;
                color: #f1d9d3;
                font-size: 12px;
            }}

            .token-box input {{
                width: min(650px, 75%);
                margin-right: 7px;
                font-family: Consolas, monospace;
                font-size: 11px;
            }}

            .token-box p {{
                margin: 9px 0 0;
                color: var(--muted);
                font-size: 10px;
            }}

            .message {{
                margin-bottom: 18px;
                padding: 12px 14px;
                background: rgba(87, 185, 135, 0.08);
                border: 1px solid rgba(87, 185, 135, 0.2);
                border-radius: 10px;
                color: #a9d9c0;
                font-size: 11px;
            }}

            @media (max-width: 1050px) {{
                .dashboard-grid {{
                    grid-template-columns: repeat(2, minmax(0, 1fr));
                }}
            }}

            @media (max-width: 760px) {{
                .sidebar {{
                    position: relative;
                    width: 100%;
                    min-height: auto;
                    border-right: 0;
                    border-bottom: 1px solid var(--border);
                }}

                .app {{
                    display: block;
                }}

                .nav {{
                    display: grid;
                    grid-template-columns: repeat(2, 1fr);
                }}

                .sidebar-bottom {{
                    display: none;
                }}

                .main {{
                    width: 100%;
                    margin-left: 0;
                    padding: 22px 16px 40px;
                }}

                .topbar {{
                    align-items: flex-start;
                    flex-direction: column;
                }}

                .dashboard-grid {{
                    grid-template-columns: 1fr;
                }}

                .content-card {{
                    padding: 15px;
                }}

                .token-box input {{
                    width: 100%;
                    margin: 0 0 9px;
                }}
            }}
        </style>
    </head>

    <body>
        <div class="app">

            <aside class="sidebar">
                <div class="brand">
                    <div class="brand-mark">E</div>
                    <div>
                        <div class="brand-text">Email Claude MCP</div>
                        <div class="brand-subtitle">Admin Console</div>
                    </div>
                </div>

                <div class="nav-label">Workspace</div>

                <nav class="nav">
                    <a class="nav-item active" href="/admin">
                        <span class="nav-icon">D</span>
                        <span>Dashboard</span>
                    </a>

                    <a class="nav-item" href="#users">
                        <span class="nav-icon">U</span>
                        <span>Users</span>
                    </a>

                    <a class="nav-item" href="#tokens">
                        <span class="nav-icon">T</span>
                        <span>Tokens</span>
                    </a>

                    <a class="nav-item" href="#create-user">
                        <span class="nav-icon">+</span>
                        <span>Create User</span>
                    </a>
                </nav>

                <div class="sidebar-bottom">
                    <div class="sidebar-bottom-title">MCP Status</div>
                    <div class="sidebar-bottom-text">
                        Admin console is connected to the Email Claude MCP backend.
                    </div>
                </div>
            </aside>

            <main class="main">

                <header class="topbar">
                    <div>
                        <h1 class="page-title">Dashboard</h1>
                        <p class="page-subtitle">
                            Manage MCP users, access tokens and administration.
                        </p>
                    </div>

                    <div class="admin-badge">
                        <span class="status-dot"></span>
                        Admin session active
                    </div>
                </header>

                {message_box}
                {token_box}

                <section class="dashboard-grid">
                    <div class="stat-card">
                        <div class="stat-label">Total Users</div>
                        <div class="stat-value">{len(users)}</div>
                        <div class="stat-note">Registered MCP users</div>
                    </div>

                    <div class="stat-card">
                        <div class="stat-label">Total Tokens</div>
                        <div class="stat-value">{len(tokens)}</div>
                        <div class="stat-note">Issued access tokens</div>
                    </div>

                    <div class="stat-card">
                        <div class="stat-label">Active Tokens</div>
                        <div class="stat-value">{sum(1 for t in tokens if status_for_token(t[4], t[5]) == "ACTIVE")}</div>
                        <div class="stat-note">Currently usable</div>
                    </div>

                    <div class="stat-card">
                        <div class="stat-label">Revoked Tokens</div>
                        <div class="stat-value">{sum(1 for t in tokens if t[5] is not None)}</div>
                        <div class="stat-note">Access removed</div>
                    </div>
                </section>

                <section class="content-card" id="create-user">
                    <div class="section-header">
                        <div>
                            <h2 class="section-title">Create User</h2>
                            <p class="section-description">
                                Add a user before generating an MCP access token.
                            </p>
                        </div>
                    </div>

                    <form class="create-user-form" method="post" action="/admin/create-user">
                        <input
                            type="text"
                            name="name"
                            placeholder="User name"
                            required
                        >
                        <button type="submit">Create User</button>
                    </form>
                </section>

                <section class="content-card" id="users">
                    <div class="section-header">
                        <div>
                            <h2 class="section-title">Users</h2>
                            <p class="section-description">
                                Manage users and generate their MCP credentials.
                            </p>
                        </div>
                    </div>

                    <div class="table-wrap">
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
                    </div>
                </section>

                <section class="content-card" id="tokens">
                    <div class="section-header">
                        <div>
                            <h2 class="section-title">Tokens</h2>
                            <p class="section-description">
                                Review token status and revoke access when required.
                            </p>
                        </div>
                    </div>

                    <div class="table-wrap">
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
                    </div>
                </section>

            </main>
        </div>

        <script>
            function copyToken() {{
                const input = document.getElementById("newToken");

                if (!input) {{
                    return;
                }}

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

    token_key = secrets.token_urlsafe(16)
    _pending_tokens[token_key] = result["token"]

    return RedirectResponse(
        f"/admin?token_notice={token_key}",
        status_code=303,
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




