"""Keycloak ExApp - Nextcloud External Application wrapper for Keycloak identity management."""

import asyncio
import logging
import os
import secrets
import subprocess
import threading
import typing
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from nc_py_api import NextcloudApp
from nc_py_api.ex_app import (
    nc_app,
    run_app,
    setup_nextcloud_logging,
)
from nc_py_api.ex_app.integration_fastapi import AppAPIAuthMiddleware


# -- Logging -----------------------------------------------------------------
logging.basicConfig(
    level=logging.WARNING,
    format="[%(funcName)s]: %(message)s",
    datefmt="%H:%M:%S",
)
LOGGER = logging.getLogger("keycloak")
LOGGER.setLevel(logging.DEBUG)


# -- Configuration -----------------------------------------------------------
KEYCLOAK_PORT = 8080
KEYCLOAK_MGMT_PORT = 9000
KEYCLOAK_URL = f"http://localhost:{KEYCLOAK_PORT}"
KEYCLOAK_MGMT_URL = f"http://localhost:{KEYCLOAK_MGMT_PORT}"
KEYCLOAK_PROCESS = None

KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "commonground")
KEYCLOAK_ADMIN_USER = os.environ.get("KC_BOOTSTRAP_ADMIN_USERNAME", "admin")
KEYCLOAK_ADMIN_PASSWORD = os.environ.get("KC_BOOTSTRAP_ADMIN_PASSWORD", "admin")

# Detect HaRP mode and set proxy prefix accordingly
APP_ID = os.environ.get("APP_ID", "keycloak")
HARP_ENABLED = bool(os.environ.get("HP_SHARED_KEY"))
if HARP_ENABLED:
    PROXY_PREFIX = f"/exapps/{APP_ID}"
else:
    PROXY_PREFIX = f"/index.php/apps/app_api/proxy/{APP_ID}"

# In-memory store for Keycloak passwords (keyed by NC user ID).
# On restart, users are re-synced and new passwords are generated.
_USER_PASSWORDS: dict[str, str] = {}

# Keycloak admin token cache
_ADMIN_TOKEN: str = ""
_ADMIN_TOKEN_EXPIRES: float = 0


# -- Keycloak Process Management ---------------------------------------------
def start_keycloak():
    """Start the Keycloak subprocess."""
    global KEYCLOAK_PROCESS

    if KEYCLOAK_PROCESS is not None and KEYCLOAK_PROCESS.poll() is None:
        return

    env = os.environ.copy()

    # Keycloak configuration
    env.setdefault("KC_HEALTH_ENABLED", "true")
    env.setdefault("KC_HTTP_ENABLED", "true")
    env.setdefault("KC_HOSTNAME_STRICT", "false")
    env.setdefault("KC_HTTP_PORT", str(KEYCLOAK_PORT))
    env.setdefault("KC_HTTP_MANAGEMENT_PORT", str(KEYCLOAK_MGMT_PORT))

    KEYCLOAK_PROCESS = subprocess.Popen(
        ["/opt/keycloak/bin/kc.sh", "start-dev"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def log_output():
        for line in KEYCLOAK_PROCESS.stdout:
            LOGGER.info("[keycloak] %s", line.decode().strip())

    threading.Thread(target=log_output, daemon=True).start()
    LOGGER.info("Keycloak started with PID: %d", KEYCLOAK_PROCESS.pid)


def stop_keycloak():
    """Stop the Keycloak subprocess."""
    global KEYCLOAK_PROCESS
    if KEYCLOAK_PROCESS is not None:
        KEYCLOAK_PROCESS.terminate()
        try:
            KEYCLOAK_PROCESS.wait(timeout=30)
        except subprocess.TimeoutExpired:
            KEYCLOAK_PROCESS.kill()
        KEYCLOAK_PROCESS = None
        LOGGER.info("Keycloak stopped")


async def wait_for_keycloak(timeout: int = 120) -> bool:
    """Wait for Keycloak to become healthy."""
    for _ in range(timeout):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{KEYCLOAK_MGMT_URL}/health/ready",
                    timeout=5,
                )
                if resp.status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)
    return False


# -- Keycloak Admin API Client -----------------------------------------------
async def get_admin_token() -> str:
    """Get a Keycloak admin access token, refreshing if expired."""
    import time

    global _ADMIN_TOKEN, _ADMIN_TOKEN_EXPIRES

    if _ADMIN_TOKEN and time.time() < _ADMIN_TOKEN_EXPIRES - 10:
        return _ADMIN_TOKEN

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{KEYCLOAK_URL}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": KEYCLOAK_ADMIN_USER,
                "password": KEYCLOAK_ADMIN_PASSWORD,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        _ADMIN_TOKEN = data["access_token"]
        _ADMIN_TOKEN_EXPIRES = time.time() + data.get("expires_in", 300)
        return _ADMIN_TOKEN


async def ensure_realm_exists() -> None:
    """Ensure the target realm exists in Keycloak."""
    token = await get_admin_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            LOGGER.info("Realm '%s' already exists", KEYCLOAK_REALM)
            return

        LOGGER.info("Creating realm '%s'", KEYCLOAK_REALM)
        resp = await client.post(
            f"{KEYCLOAK_URL}/admin/realms",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "realm": KEYCLOAK_REALM,
                "enabled": True,
                "registrationAllowed": False,
            },
            timeout=10,
        )
        resp.raise_for_status()
        LOGGER.info("Realm '%s' created", KEYCLOAK_REALM)


async def get_keycloak_user(username: str) -> dict | None:
    """Look up a user in Keycloak by username."""
    token = await get_admin_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}/users",
            headers={"Authorization": f"Bearer {token}"},
            params={"username": username, "exact": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        users = resp.json()
        return users[0] if users else None


async def create_keycloak_user(
    username: str,
    email: str = "",
    first_name: str = "",
    last_name: str = "",
) -> str:
    """Create a user in Keycloak and return their generated password."""
    password = secrets.token_urlsafe(32)
    token = await get_admin_token()

    user_payload = {
        "username": username,
        "enabled": True,
        "emailVerified": True,
        "credentials": [
            {
                "type": "password",
                "value": password,
                "temporary": False,
            }
        ],
    }
    if email:
        user_payload["email"] = email
    if first_name:
        user_payload["firstName"] = first_name
    if last_name:
        user_payload["lastName"] = last_name

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}/users",
            headers={"Authorization": f"Bearer {token}"},
            json=user_payload,
            timeout=10,
        )
        if resp.status_code == 409:
            # User already exists — reset their password instead
            return await reset_keycloak_user_password(username)
        resp.raise_for_status()

    _USER_PASSWORDS[username] = password
    LOGGER.info("Created Keycloak user: %s", username)
    return password


async def reset_keycloak_user_password(username: str) -> str:
    """Reset a Keycloak user's password and return the new password."""
    kc_user = await get_keycloak_user(username)
    if not kc_user:
        return await create_keycloak_user(username)

    password = secrets.token_urlsafe(32)
    token = await get_admin_token()

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}/users/{kc_user['id']}/reset-password",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "type": "password",
                "value": password,
                "temporary": False,
            },
            timeout=10,
        )
        resp.raise_for_status()

    _USER_PASSWORDS[username] = password
    LOGGER.info("Reset password for Keycloak user: %s", username)
    return password


async def update_keycloak_user(
    username: str,
    email: str = "",
    first_name: str = "",
    last_name: str = "",
) -> None:
    """Update an existing Keycloak user's profile."""
    kc_user = await get_keycloak_user(username)
    if not kc_user:
        await create_keycloak_user(username, email, first_name, last_name)
        return

    token = await get_admin_token()
    update_payload: dict[str, str] = {}
    if email:
        update_payload["email"] = email
    if first_name:
        update_payload["firstName"] = first_name
    if last_name:
        update_payload["lastName"] = last_name

    if not update_payload:
        return

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}/users/{kc_user['id']}",
            headers={"Authorization": f"Bearer {token}"},
            json=update_payload,
            timeout=10,
        )
        resp.raise_for_status()
    LOGGER.info("Updated Keycloak user: %s", username)


async def delete_keycloak_user(username: str) -> None:
    """Delete a user from Keycloak."""
    kc_user = await get_keycloak_user(username)
    if not kc_user:
        return

    token = await get_admin_token()
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}/users/{kc_user['id']}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()

    _USER_PASSWORDS.pop(username, None)
    LOGGER.info("Deleted Keycloak user: %s", username)


async def get_user_token(username: str, client_id: str = "opentalk") -> dict:
    """Get a Keycloak token for a user using the direct access grant.

    Returns dict with access_token, refresh_token, expires_in etc.
    """
    password = _USER_PASSWORDS.get(username)
    if not password:
        # User not synced yet — sync on demand
        password = await reset_keycloak_user_password(username)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": client_id,
                "username": username,
                "password": password,
                "scope": "openid profile email",
            },
            timeout=10,
        )
        if resp.status_code == 401:
            # Password may be stale — reset and retry once
            password = await reset_keycloak_user_password(username)
            resp = await client.post(
                f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token",
                data={
                    "grant_type": "password",
                    "client_id": client_id,
                    "username": username,
                    "password": password,
                    "scope": "openid profile email",
                },
                timeout=10,
            )
        resp.raise_for_status()
        return resp.json()


# -- User Sync ---------------------------------------------------------------
async def sync_all_users(nc: NextcloudApp) -> int:
    """Sync all Nextcloud users to Keycloak. Returns count of synced users."""
    await ensure_realm_exists()

    # Use nc_py_api's built-in OCS calls (handles AppAPI auth automatically)
    nextcloud_url = os.environ.get("NEXTCLOUD_URL", "http://nextcloud")

    # List users via OCS provisioning API
    try:
        resp = nc.ocs("GET", "/ocs/v1.php/cloud/users")
        users = resp.get("users", [])
    except Exception as e:
        LOGGER.error("Failed to list NC users: %s", str(e))
        return 0

    count = 0
    for uid in users:
        try:
            # Get user details
            try:
                user_data = nc.ocs("GET", f"/ocs/v1.php/cloud/users/{uid}")
                email = user_data.get("email", "")
                display_name = user_data.get("displayname", "")
                parts = display_name.split(" ", 1) if display_name else [""]
                first_name = parts[0]
                last_name = parts[1] if len(parts) > 1 else ""
            except Exception:
                email = ""
                first_name = ""
                last_name = ""

            existing = await get_keycloak_user(uid)
            if existing:
                await update_keycloak_user(uid, email, first_name, last_name)
                # Reset password so we have it in memory
                await reset_keycloak_user_password(uid)
            else:
                await create_keycloak_user(uid, email, first_name, last_name)
            count += 1
        except Exception as e:
            LOGGER.error("Failed to sync user %s: %s", uid, str(e))

    LOGGER.info("Synced %d users to Keycloak", count)
    return count


# -- Ensure Direct Access Grant on Clients -----------------------------------
async def ensure_direct_access_grant(client_id: str) -> None:
    """Ensure a Keycloak client has direct access grants enabled."""
    token = await get_admin_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}/clients",
            headers={"Authorization": f"Bearer {token}"},
            params={"clientId": client_id},
            timeout=10,
        )
        resp.raise_for_status()
        clients = resp.json()
        if not clients:
            LOGGER.warning("Client '%s' not found in realm '%s'", client_id, KEYCLOAK_REALM)
            return

        kc_client = clients[0]
        if kc_client.get("directAccessGrantsEnabled"):
            return

        LOGGER.info("Enabling direct access grants on client '%s'", client_id)
        resp = await client.put(
            f"{KEYCLOAK_URL}/admin/realms/{KEYCLOAK_REALM}/clients/{kc_client['id']}",
            headers={"Authorization": f"Bearer {token}"},
            json={"directAccessGrantsEnabled": True},
            timeout=10,
        )
        resp.raise_for_status()


# -- Path Rewriting ----------------------------------------------------------
_REWRITE_PREFIXES = (
    "/js/",
    "/resources/",
    "/admin/",
    "/realms/",
    "/auth/",
)


def rewrite_content(content: bytes, content_type: str) -> bytes:
    """Rewrite absolute Keycloak paths to use the proxy prefix."""
    if not any(t in content_type for t in ("text/html", "javascript", "text/css", "json")):
        return content

    text = content.decode("utf-8", errors="replace")

    for prefix in _REWRITE_PREFIXES:
        text = text.replace(f'"{prefix}', f'"{PROXY_PREFIX}{prefix}')
        text = text.replace(f"'{prefix}", f"'{PROXY_PREFIX}{prefix}")

    return text.encode("utf-8")


# -- Lifespan ----------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_nextcloud_logging("keycloak", logging_level=logging.WARNING)
    LOGGER.info("Starting Keycloak ExApp")
    start_keycloak()
    await wait_for_keycloak()
    yield
    stop_keycloak()
    LOGGER.info("Keycloak ExApp shutdown complete")


# -- FastAPI App -------------------------------------------------------------
# Internal API secret for ExApp-to-ExApp calls (bypasses AppAPI middleware)
INTERNAL_API_SECRET = os.environ.get("KEYCLOAK_API_SECRET", "keycloak-exapp-internal-secret")

APP = FastAPI(lifespan=lifespan)
# Disable AppAPIAuthMiddleware for /api/ routes (they use shared secret auth)
# Note: fnmatch matches against path WITHOUT leading slash
APP.add_middleware(AppAPIAuthMiddleware, disable_for=["api/*"])


# -- Inline iframe loader JS ------------------------------------------------
IFRAME_LOADER_JS = f"""
(function() {{
    var style = document.createElement('style');
    style.textContent =
        '#content.app-app_api {{' +
        '  margin-top: var(--header-height) !important;' +
        '  height: var(--body-height) !important;' +
        '  width: calc(100% - var(--body-container-margin) * 2) !important;' +
        '  border-radius: var(--body-container-radius) !important;' +
        '  overflow: hidden !important;' +
        '  padding: 0 !important;' +
        '}}' +
        '#content.app-app_api > iframe {{ width: 100%; height: 100%; border: none; display: block; }}';
    document.head.appendChild(style);

    function setup() {{
        var content = document.getElementById('content');
        if (!content) return;
        content.innerHTML = '';
        var iframe = document.createElement('iframe');
        iframe.src = '{PROXY_PREFIX}/';
        content.appendChild(iframe);
    }}

    if (document.readyState === 'loading') {{
        document.addEventListener('DOMContentLoaded', setup);
    }} else {{
        setup();
    }}
}})();
""".strip()


@APP.get("/js/keycloak-iframe-loader.js")
async def iframe_loader():
    """Serve the inline iframe loader script."""
    return Response(
        content=IFRAME_LOADER_JS,
        media_type="application/javascript",
    )


# -- Enabled Handler ---------------------------------------------------------
def enabled_handler(enabled: bool, nc: NextcloudApp) -> str:
    """Handle app enable/disable events."""
    if enabled:
        LOGGER.info("Enabling Keycloak ExApp")
        nc.ui.resources.set_script("top_menu", "keycloak", "js/keycloak-iframe-loader")
        nc.ui.top_menu.register("keycloak", "Keycloak", "ex_app/img/app.svg", True)
        start_keycloak()
    else:
        LOGGER.info("Disabling Keycloak ExApp")
        nc.ui.resources.delete_script("top_menu", "keycloak", "js/keycloak-iframe-loader")
        nc.ui.top_menu.unregister("keycloak")
        stop_keycloak()
    return ""


# -- Required Endpoints ------------------------------------------------------
@APP.get("/heartbeat")
async def heartbeat_callback():
    """Heartbeat endpoint for AppAPI health checks."""
    return JSONResponse(content={"status": "ok"})


@APP.post("/init")
async def init_callback(
    b_tasks: BackgroundTasks,
    nc: typing.Annotated[NextcloudApp, Depends(nc_app)],
):
    """Initialization endpoint called by AppAPI after installation."""
    b_tasks.add_task(init_keycloak_task, nc)
    return JSONResponse(content={})


@APP.put("/enabled")
def enabled_callback(
    enabled: bool,
    nc: typing.Annotated[NextcloudApp, Depends(nc_app)],
):
    """Enable/disable callback from AppAPI."""
    return JSONResponse(content={"error": enabled_handler(enabled, nc)})


async def init_keycloak_task(nc: NextcloudApp):
    """Background task for Keycloak initialization with progress reporting."""
    nc.set_init_status(0)
    LOGGER.info("Starting Keycloak initialization...")

    start_keycloak()
    nc.set_init_status(20)

    if await wait_for_keycloak():
        nc.set_init_status(40)

        # Ensure realm exists and sync users
        try:
            await ensure_realm_exists()
            nc.set_init_status(50)
            await sync_all_users(nc)
            nc.set_init_status(70)
            # Ensure direct access grant is enabled on the opentalk client
            await ensure_direct_access_grant("opentalk")
        except Exception as e:
            LOGGER.error("User sync failed during init: %s", str(e))

        nc.set_init_status(80)
        nc.ui.resources.set_script("top_menu", "keycloak", "js/keycloak-iframe-loader")
        nc.ui.top_menu.register("keycloak", "Keycloak", "ex_app/img/app.svg", True)
        nc.set_init_status(100)
        LOGGER.info("Keycloak initialization complete")
    else:
        LOGGER.error("Keycloak failed to start within timeout")


# -- Token API (for consumer ExApps like OpenTalk) ---------------------------
@APP.post("/api/token")
async def token_endpoint(request: Request):
    """Get a Keycloak token for a Nextcloud user.

    Called by consumer ExApps (e.g. OpenTalk) to get a token for server-side auth.
    Authentication: shared secret via X-API-SECRET header, or AppAPI proxy auth.

    Headers:
        X-NC-USER-ID: Nextcloud user ID (required for direct ExApp-to-ExApp calls)
        X-API-SECRET: Shared secret for authentication

    Query params:
        client_id: Keycloak client ID to get the token for (default: opentalk)
    """
    import base64

    # Authenticate: check shared secret or AppAPI auth
    api_secret = request.headers.get("X-API-SECRET", "")
    if api_secret != INTERNAL_API_SECRET:
        # Try AppAPI auth as fallback (when called via Nextcloud proxy)
        auth_header = request.headers.get("authorization-app-api", "")
        if not auth_header:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # Get user ID from headers
    nc_user_id = request.headers.get("X-NC-USER-ID", "")
    if not nc_user_id:
        # Fallback: decode from AppAPI authorization header
        auth_header = request.headers.get("authorization-app-api", "")
        if auth_header:
            try:
                decoded = base64.b64decode(auth_header).decode("utf-8")
                nc_user_id = decoded.split(":")[0]
            except Exception:
                pass
    LOGGER.info("Token request for NC user: %s", nc_user_id)

    if not nc_user_id:
        return JSONResponse(
            {"error": "No Nextcloud user context found"},
            status_code=401,
        )

    client_id = request.query_params.get("client_id", "opentalk")

    try:
        token_data = await get_user_token(nc_user_id, client_id)
        return JSONResponse({
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token", ""),
            "id_token": token_data.get("id_token", ""),
            "expires_in": token_data.get("expires_in", 300),
            "token_type": token_data.get("token_type", "Bearer"),
        })
    except httpx.HTTPStatusError as e:
        LOGGER.error("Token request failed for user %s: %s", nc_user_id, str(e))
        return JSONResponse(
            {"error": f"Failed to get token: {e.response.status_code}"},
            status_code=502,
        )
    except Exception as e:
        LOGGER.error("Token request error for user %s: %s", nc_user_id, str(e))
        return JSONResponse(
            {"error": f"Token error: {str(e)}"},
            status_code=500,
        )


# -- User Sync API (for admin / event-triggered sync) -----------------------
@APP.post("/api/sync-user")
async def sync_user_endpoint(request: Request):
    """Sync a single Nextcloud user to Keycloak.

    Called by event listeners when a user is created or modified.

    Body JSON: {"user_id": "...", "email": "...", "display_name": "..."}
    """
    api_secret = request.headers.get("X-API-SECRET", "")
    auth_header = request.headers.get("authorization-app-api", "")
    if api_secret != INTERNAL_API_SECRET and not auth_header:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    user_id = body.get("user_id", "")
    if not user_id:
        return JSONResponse({"error": "user_id is required"}, status_code=400)

    email = body.get("email", "")
    display_name = body.get("display_name", "")
    parts = display_name.split(" ", 1) if display_name else [""]
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else ""

    try:
        existing = await get_keycloak_user(user_id)
        if existing:
            await update_keycloak_user(user_id, email, first_name, last_name)
            await reset_keycloak_user_password(user_id)
        else:
            await create_keycloak_user(user_id, email, first_name, last_name)

        return JSONResponse({"status": "ok", "user_id": user_id})
    except Exception as e:
        LOGGER.error("User sync failed for %s: %s", user_id, str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


@APP.post("/api/delete-user")
async def delete_user_endpoint(request: Request):
    """Delete a user from Keycloak when they are removed from Nextcloud."""
    api_secret = request.headers.get("X-API-SECRET", "")
    auth_header = request.headers.get("authorization-app-api", "")
    if api_secret != INTERNAL_API_SECRET and not auth_header:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    user_id = body.get("user_id", "")
    if not user_id:
        return JSONResponse({"error": "user_id is required"}, status_code=400)

    try:
        await delete_keycloak_user(user_id)
        return JSONResponse({"status": "ok", "user_id": user_id})
    except Exception as e:
        LOGGER.error("User deletion failed for %s: %s", user_id, str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


@APP.post("/api/sync-all")
async def sync_all_endpoint(
    nc: typing.Annotated[NextcloudApp, Depends(nc_app)],
):
    """Trigger a full user sync from Nextcloud to Keycloak."""
    try:
        count = await sync_all_users(nc)
        return JSONResponse({"status": "ok", "synced": count})
    except Exception as e:
        LOGGER.error("Full sync failed: %s", str(e))
        return JSONResponse({"error": str(e)}, status_code=500)


# -- Catch-All Proxy ---------------------------------------------------------
@APP.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy(request: Request, path: str):
    """Proxy all requests to Keycloak."""
    # Serve ex_app static files (icons, JS) directly from disk
    if path.startswith("ex_app/"):
        file_path = Path(__file__).parent.parent.parent / path
        if file_path.is_file():
            from starlette.responses import FileResponse

            return FileResponse(str(file_path))

    # Build headers, stripping host/cookie
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower()
        not in (
            "host",
            "connection",
            "transfer-encoding",
            "accept-encoding",
        )
    }

    try:
        async with httpx.AsyncClient() as client:
            url = f"{KEYCLOAK_URL}/{path}"

            resp = await client.request(
                method=request.method,
                url=url,
                content=await request.body(),
                headers=headers,
                cookies=dict(request.cookies),
                params=request.query_params,
                timeout=300,
            )

            content = resp.content
            content_type = resp.headers.get("content-type", "")
            content = rewrite_content(content, content_type)

            # Forward response headers, filtering problematic ones
            resp_headers = {
                k: v
                for k, v in resp.headers.items()
                if k.lower()
                not in (
                    "content-encoding",
                    "transfer-encoding",
                    "content-length",
                )
            }

            return Response(
                content=content,
                status_code=resp.status_code,
                headers=resp_headers,
            )
    except httpx.RequestError as e:
        LOGGER.error("Proxy error: %s", str(e))
        return JSONResponse(
            {"error": f"Proxy error: {str(e)}"},
            status_code=502,
        )


# -- Entry Point -------------------------------------------------------------
if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    run_app(APP, log_level="info")
