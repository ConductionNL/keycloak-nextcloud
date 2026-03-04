"""Keycloak ExApp - Nextcloud External Application wrapper for Keycloak identity management."""

import asyncio
import logging
import os
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


# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="[%(funcName)s]: %(message)s",
    datefmt="%H:%M:%S",
)
LOGGER = logging.getLogger("keycloak")
LOGGER.setLevel(logging.DEBUG)


# ── Configuration ───────────────────────────────────────────────────
KEYCLOAK_PORT = 8080
KEYCLOAK_MGMT_PORT = 9000
KEYCLOAK_URL = f"http://localhost:{KEYCLOAK_PORT}"
KEYCLOAK_MGMT_URL = f"http://localhost:{KEYCLOAK_MGMT_PORT}"
KEYCLOAK_PROCESS = None

# Detect HaRP mode and set proxy prefix accordingly
APP_ID = os.environ.get("APP_ID", "keycloak")
HARP_ENABLED = bool(os.environ.get("HP_SHARED_KEY"))
if HARP_ENABLED:
    PROXY_PREFIX = f"/exapps/{APP_ID}"
else:
    PROXY_PREFIX = f"/index.php/apps/app_api/proxy/{APP_ID}"


# ── Keycloak Process Management ──────────────────────────────────────
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


# ── Path Rewriting ─────────────────────────────────────────────────
# Keycloak's admin console uses absolute paths that need rewriting
# when accessed through the ExApp proxy prefix.
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


# ── Lifespan ────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_nextcloud_logging("keycloak", logging_level=logging.WARNING)
    LOGGER.info("Starting Keycloak ExApp")
    start_keycloak()
    await wait_for_keycloak()
    yield
    stop_keycloak()
    LOGGER.info("Keycloak ExApp shutdown complete")


# ── FastAPI App ─────────────────────────────────────────────────────
APP = FastAPI(lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware)


# ── Inline iframe loader JS ────────────────────────────────────────
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


# ── Enabled Handler ────────────────────────────────────────────────
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


# ── Required Endpoints ──────────────────────────────────────────────
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
        nc.set_init_status(80)
        nc.ui.resources.set_script("top_menu", "keycloak", "js/keycloak-iframe-loader")
        nc.ui.top_menu.register("keycloak", "Keycloak", "ex_app/img/app.svg", True)
        nc.set_init_status(100)
        LOGGER.info("Keycloak initialization complete")
    else:
        LOGGER.error("Keycloak failed to start within timeout")


# ── Catch-All Proxy ────────────────────────────────────────────────
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


# ── Entry Point ─────────────────────────────────────────────────────
if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    run_app(APP, log_level="info")
