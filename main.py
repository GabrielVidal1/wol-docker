import asyncio
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
import uvicorn
import wakeonlan
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

# Configuration from environment variables
MAC_ADDRESS = os.getenv("MAC_ADDRESS")
HEALTH_URL = os.getenv("HEALTH_URL")  # e.g., "http://192.168.1.50/api/health"
TARGET_URL = os.getenv("TARGET_URL", HEALTH_URL)  # default to HEALTH_URL if not set
MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "30"))  # Default: 30 seconds
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")
POLL_INTERVAL = 1.0  # Fixed at 1s as per request

WOL_HOST = os.getenv("WOL_HOST", "255.255.255.255")
WOL_PORT = int(os.getenv("WOL_PORT", "9"))
WOL_INTERFACE = os.getenv("WOL_INTERFACE") or None

# State management
target_is_up = False
target_last_checked_at = None
wake_lock = asyncio.Lock()  # To prevent concurrent wake-ups
shutdown_event = asyncio.Event()

app = FastAPI(title="Wake-on-LAN Proxy")

client = httpx.AsyncClient(
    timeout=httpx.Timeout(MAX_TIMEOUT, connect=10.0),
    limits=httpx.Limits(max_keepalive_connections=10),
)


def log(message: str):
    """Simple logger with UTC timestamp."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {message}", flush=True)


def parse_url_with_port(url_str: str) -> dict:
    """
    Parse a URL and return components with explicit port (or None if not present).
    Handles both `http://host` and `http://host:port`.
    Returns: {'scheme':..., 'hostname':..., 'path':..., 'query':..., 'port': int|None}
    """
    try:
        parsed = urlparse(url_str)
        scheme = parsed.scheme or "http"
        hostname = parsed.hostname
        # Default ports: 80 for http, 443 for https — but we keep port=None if omitted in URL.
        port = parsed.port
        path = parsed.path or "/"
        query = parsed.query

        return {
            "scheme": scheme,
            "hostname": hostname,
            "port": port,  # e.g., 8080, or None if not specified
            "path": path.rstrip("/")
            or "/",  # ensure trailing slash is handled consistently
            "query": query,
        }
    except Exception as e:
        raise ValueError(f"Invalid URL: {url_str} ({e})")


def build_url_from_parts(parts: dict) -> str:
    """Reconstruct URL from parsed parts, handling port and path correctly."""
    scheme = parts["scheme"]
    hostname = parts["hostname"]
    path = parts["path"].rstrip("/") or "/"

    # Port is only included if explicitly set
    host_with_port = f"{hostname}:{parts['port']}" if parts.get("port") else hostname

    # Rebuild without trailing slash in path (except root)
    return f"{scheme}://{host_with_port}{path}"


# Pre-parse URLs at startup to catch errors early and avoid per-request parsing
try:
    _health_parsed = parse_url_with_port(HEALTH_URL) if HEALTH_URL else None
    _target_parsed = parse_url_with_port(TARGET_URL) if TARGET_URL else None

    # Warn if health or target missing critical fields
    for name, parts in [("HEALTH_URL", _health_parsed), ("TARGET_URL", _target_parsed)]:
        if not parts or not parts["hostname"]:
            raise ValueError(f"{name} is invalid: must contain a hostname")
except Exception as e:
    log(f"❌ Critical URL parse error: {e}")
    sys.exit(1)


async def poll_health():
    """
    Poll `HEALTH_URL` every POLL_INTERVAL seconds.
    Sets target_is_up to True once a 2xx or 3xx response is received.
    On failure, resets target_is_up to False.
    """
    global target_is_up
    global target_last_checked_at

    health_url = build_url_from_parts(_health_parsed)
    log(f"Starting health poll loop for {health_url}")

    while not shutdown_event.is_set():
        try:
            async with httpx.AsyncClient(timeout=2.0) as short_client:
                resp = await short_client.get(health_url)
                if 200 <= resp.status_code < 400:
                    if not target_is_up:
                        log("✅ Target is now UP (health check passed)")
                    target_is_up = True
                else:
                    raise httpx.HTTPStatusError(
                        f"Unexpected status: {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
        except Exception as e:
            if target_is_up:
                log(f"⚠️ Health check failed ({e}); marking target DOWN")
            target_is_up = False

        target_last_checked_at = datetime.now(timezone.utc)
        # Use asyncio.sleep() instead of time.sleep()
        await asyncio.sleep(POLL_INTERVAL)


async def ensure_target_woken():
    """
    Ensure the target computer is awake (send WoL packet and wait for health check).
    Returns True if target is up before timeout; False otherwise.
    """
    global target_is_up

    async with wake_lock:
        # If already up, nothing to do
        if target_is_up:
            return True

        log(f" waking target ({MAC_ADDRESS})...")
        try:
            kwargs = {}
            if WOL_INTERFACE:
                kwargs["interface"] = WOL_INTERFACE
            wakeonlan.send_magic_packet(
                MAC_ADDRESS, ip_address=WOL_HOST, port=WOL_PORT, **kwargs
            )
        except Exception as e:
            log(f"⚠️ Failed to send WoL packet: {e}")

        # Wait up to MAX_TIMEOUT seconds for health check to succeed
        deadline = asyncio.get_event_loop().time() + MAX_TIMEOUT

        while asyncio.get_event_loop().time() < deadline:
            if target_is_up:
                return True
            await asyncio.sleep(0.1)

        log("❌ Wake timeout exceeded.")
        return False


@app.on_event("startup")
async def startup():
    # Start health poll background task
    task = asyncio.create_task(poll_health())
    # Optional: keep reference to avoid GC issues in some Python versions
    asyncio.gather(task)


@app.on_event("shutdown")
async def shutdown():
    global client
    log("Shutting down...")
    shutdown_event.set()
    await client.aclose()


@app.get("/health")
async def health_check():
    """Health check endpoint for the proxy itself."""
    return {
        "status": "ok",
        "target_is_up": target_is_up,
        "target_last_checked_at": (
            target_last_checked_at.isoformat() if target_last_checked_at else None
        ),
    }


@app.api_route("/{path:path}", include_in_schema=False)
async def proxy(request: Request, path: str):
    if not MAC_ADDRESS or not HEALTH_URL:
        return JSONResponse(
            status_code=500,
            content={"error": "MAC_ADDRESS and/or HEALTH_URL not configured"},
        )

    target_url = build_url_from_parts(_target_parsed)

    # Preserve original query parameters (from request, not parsed URL)
    query_params = dict(request.query_params)

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length"}
    }

    # If target is down, wake it first
    if not target_is_up:
        log(
            f"Target is DOWN; attempting to wake before request: {request.method} /{path}"
        )
        if not await ensure_target_woken():
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Target computer could not be woken within timeout",
                    "timeout_seconds": MAX_TIMEOUT,
                },
                headers={"Retry-After": str(int(MAX_TIMEOUT))},
            )

    try:
        # Get method and build async call
        method = request.method

        # Read body (even for GET/HEAD in FastAPI) to forward raw bytes if needed
        body = await request.body()
        kwargs = {
            "params": query_params,
            "headers": headers,
            "timeout": httpx.Timeout(MAX_TIMEOUT),
        }
        if method in ["POST", "PUT", "PATCH"]:
            kwargs["content"] = body

        resp = await client.request(method, target_url, **kwargs)

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(resp.headers),
        )

    except httpx.TimeoutException:
        log(f"Timeout forwarding request to {target_url}")
        return JSONResponse(status_code=504, content={"error": "Target timeout"})
    except Exception as e:
        log(f"Error proxying request: {e}")
        return JSONResponse(status_code=502, content={"error": str(e)})


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
