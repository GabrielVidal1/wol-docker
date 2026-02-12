import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import uvicorn
import wakeonlan
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

# ----------------------------
# Configuration (env vars)
# ----------------------------
MAC_ADDRESS = os.getenv("MAC_ADDRESS")
HEALTH_URL = os.getenv("HEALTH_URL")  # e.g. "http://192.168.1.50/api/health"
TARGET_URL = (
    os.getenv("TARGET_URL") or HEALTH_URL
)  # base target URL (defaults to HEALTH_URL)
MAX_TIMEOUT = float(os.getenv("MAX_TIMEOUT", "30"))  # seconds
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5.0"))  # seconds

WOL_HOST = os.getenv("WOL_HOST", "255.255.255.255")
WOL_PORT = int(os.getenv("WOL_PORT", "9"))
WOL_INTERFACE = os.getenv("WOL_INTERFACE") or None


# ----------------------------
# Helpers
# ----------------------------
def log(message: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {message}", flush=True)


def validate_url(name: str, value: Optional[str]) -> str:
    if not value:
        raise ValueError(f"{name} is not set")
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(
            f"{name} must be an absolute URL like http(s)://host[:port]/path (got: {value})"
        )
    return value


def build_target_request_url(base: str, path: str) -> str:
    # Join base + path safely (handles missing/extra slashes)
    base = base.rstrip("/") + "/"
    path = path.lstrip("/")
    return urljoin(base, path)


def remove_hop_hop_headers(headers: httpx.Headers) -> dict:

    HOP_BY_HOP_HEADERS = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }

    res = {}
    for k, v in headers.items():
        if k.lower() in HOP_BY_HOP_HEADERS:
            continue
        res[k] = v
    return res


# ----------------------------
# FastAPI app + lifespan
# ----------------------------
app = FastAPI(title="Wake-on-LAN Proxy")


async def poll_health(app_: FastAPI) -> None:
    health_url = app_.state.health_url
    target_up_event: asyncio.Event = app_.state.target_up_event
    shutdown_event: asyncio.Event = app_.state.shutdown_event
    client: httpx.AsyncClient = app_.state.client

    log(f"Starting health poll loop: {health_url} (interval={POLL_INTERVAL}s)")

    was_up = False
    while not shutdown_event.is_set():
        try:
            resp = await client.get(
                health_url,
                timeout=httpx.Timeout(
                    2.0,
                    connect=2.0,
                ),
            )
            is_up = resp.status_code != 503
            if is_up:
                target_up_event.set()
                if not was_up:
                    log("✅ Target is now UP")
            else:
                target_up_event.clear()
                if was_up:
                    log(
                        f"⚠️ Health check status={resp.status_code}; marking target DOWN"
                    )
            was_up = is_up
        except Exception as e:
            target_up_event.clear()
            if was_up:
                log(
                    f"⚠️ Health check failed ({type(e).__name__}: {e}); marking target DOWN"
                )
            was_up = False

        app_.state.target_last_checked_at = datetime.now(timezone.utc)
        await asyncio.sleep(POLL_INTERVAL)


async def ensure_target_woken(app_: FastAPI) -> bool:
    target_up_event: asyncio.Event = app_.state.target_up_event
    wake_lock: asyncio.Lock = app_.state.wake_lock

    # Fast path
    if target_up_event.is_set():
        return True

    async with wake_lock:
        # Re-check after acquiring lock
        if target_up_event.is_set():
            return True

        log(f"🌙 Target is DOWN; sending WoL packet to {app_.state.mac_address} ...")

        try:
            kwargs = {}
            if WOL_INTERFACE:
                kwargs["interface"] = WOL_INTERFACE
            wakeonlan.send_magic_packet(
                app_.state.mac_address,
                ip_address=WOL_HOST,
                port=WOL_PORT,
                **kwargs,
            )
        except Exception as e:
            log(f"⚠️ Failed to send WoL packet ({type(e).__name__}: {e})")

        # Wait until health poll marks it up (or timeout)
        try:
            await asyncio.wait_for(target_up_event.wait(), timeout=MAX_TIMEOUT)
            return True
        except asyncio.TimeoutError:
            log("❌ Wake timeout exceeded (target did not become healthy in time).")
            return False


@app.on_event("startup")
async def startup() -> None:
    try:
        # Validate config early
        app.state.mac_address = MAC_ADDRESS
        app.state.health_url = validate_url("HEALTH_URL", HEALTH_URL)
        app.state.target_base_url = validate_url("TARGET_URL", TARGET_URL)

        if not app.state.mac_address:
            raise ValueError("MAC_ADDRESS is not set")

        # Async primitives must be created inside the running event loop
        app.state.shutdown_event = asyncio.Event()
        app.state.target_up_event = asyncio.Event()
        app.state.wake_lock = asyncio.Lock()
        app.state.target_last_checked_at = None

        # One shared HTTP client (connection pooled)
        app.state.client = httpx.AsyncClient(
            timeout=httpx.Timeout(MAX_TIMEOUT, connect=10.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            follow_redirects=False,
        )

        # Start background health poll
        app.state.health_task = asyncio.create_task(poll_health(app))

        log("✅ Startup complete")
    except Exception as e:
        log(f"❌ Startup error: {e}")
        # Hard fail so container/orchestrator restarts quickly rather than serving a broken app
        raise


@app.on_event("shutdown")
async def shutdown() -> None:
    log("Shutting down...")
    app.state.shutdown_event.set()

    task = getattr(app.state, "health_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(
                f"⚠️ Health task ended with error during shutdown: {type(e).__name__}: {e}"
            )

    client: httpx.AsyncClient = getattr(app.state, "client", None)
    if client:
        await client.aclose()


# ----------------------------
# Proxy endpoint (catch-all)
# ----------------------------
@app.api_route(
    "/{path:path}",
    include_in_schema=False,
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy(request: Request, path: str):
    # Sanity check (should not happen if startup succeeded)
    if not getattr(app.state, "mac_address", None) or not getattr(
        app.state, "health_url", None
    ):
        return JSONResponse(
            status_code=500,
            content={"error": "MAC_ADDRESS and/or HEALTH_URL not configured"},
        )

    # Ensure target is awake
    if not app.state.target_up_event.is_set():
        log(f"Target is DOWN; attempting wake before request: {request.method} /{path}")
        ok = await ensure_target_woken(app)
        if not ok:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "Target computer could not be woken within timeout",
                    "timeout_seconds": MAX_TIMEOUT,
                },
                headers={"Retry-After": str(int(MAX_TIMEOUT))},
            )

    # Build upstream request
    upstream_url = build_target_request_url(app.state.target_base_url, path)
    body = await request.body()
    query_params = dict(request.query_params)

    try:
        upstream_request = app.state.client.build_request(
            method=request.method,
            url=upstream_url,
            params=query_params,
            headers=remove_hop_hop_headers(request.headers),
            content=body if body else None,
        )

        upstream_response = await app.state.client.send(
            upstream_request,
            stream=True,
        )

        content_type = upstream_response.headers.get("content-type", "")
        is_upstream_stream = "text/event-stream" in content_type.lower()

        # Detect client-requested streaming (OpenAI style)
        is_client_stream = False
        if body:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict) and payload.get("stream") is True:
                    is_client_stream = True
            except Exception:
                pass

        if is_client_stream or is_upstream_stream:

            async def stream_generator():
                try:
                    async for chunk in upstream_response.aiter_raw():
                        yield chunk
                finally:
                    await upstream_response.aclose()

            response_headers = remove_hop_hop_headers(upstream_response.headers)

            response_headers.setdefault("Cache-Control", "no-cache")
            return StreamingResponse(
                stream_generator(),
                status_code=upstream_response.status_code,
                media_type=content_type or None,
                headers=response_headers,
            )

        content = await upstream_response.aread()
        await upstream_response.aclose()

        return Response(
            content=content,
            status_code=upstream_response.status_code,
            headers=remove_hop_hop_headers(upstream_response.headers),
        )

    except httpx.TimeoutException:
        log(f"⏱️ Timeout forwarding request to {upstream_url}")
        return JSONResponse(
            status_code=504,
            content={"error": "Target timeout"},
        )

    except Exception as e:
        log(f"❌ Error proxying request to {upstream_url}: {type(e).__name__}: {e}")
        return JSONResponse(
            status_code=502,
            content={"error": str(e)},
        )


# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
