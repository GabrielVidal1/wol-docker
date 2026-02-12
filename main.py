import asyncio
import os
import signal
from datetime import datetime, timezone

import httpx
import uvicorn
import wakeonlan
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

# Configuration from environment variables
MAC_ADDRESS = os.getenv("MAC_ADDRESS")
HEALTH_URL = os.getenv("HEALTH_URL")
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


async def poll_health():
    """
    Poll `HEALTH_URL` every POLL_INTERVAL seconds.
    Sets target_is_up to True once a 2xx or 3xx response is received.
    On failure, resets target_is_up to False.
    """
    global target_is_up
    global target_last_checked_at

    log(f"Starting health poll loop for {HEALTH_URL}")

    while not shutdown_event.is_set():
        try:
            async with httpx.AsyncClient(timeout=2.0) as short_client:
                resp = await short_client.get(HEALTH_URL)
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
            wakeonlan.send_magic_packet(MAC_ADDRESS, ip_address=WOL_HOST, port=WOL_PORT, **kwargs)
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


@app.api_route("/{path:path}", include_in_schema=False)
async def proxy(request: Request, path: str):
    if not MAC_ADDRESS or not HEALTH_URL:
        return JSONResponse(
            status_code=500,
            content={"error": "MAC_ADDRESS and/or HEALTH_URL not configured"},
        )

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

    # Build the target URL: HEALTH_URL + path (if needed), preserve query & body
    target_url = HEALTH_URL.rstrip("/") + "/" + path.lstrip("/")

    # Extract query params, headers, and body from original request
    query_params = dict(request.query_params)
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length"}
    }

    try:
        # Get method and build async call
        method = request.method
        if method == "HEAD":
            # httpx doesn't support HEAD with body; skip it safely
            resp = await client.head(
                target_url,
                params=query_params,
                headers=headers,
                timeout=httpx.Timeout(MAX_TIMEOUT),
            )
        else:
            # Read body (even for GET/HEAD in FastAPI) to forward raw bytes if needed
            body = await request.body()
            kwargs = {
                "params": query_params,
                "headers": headers,
                "timeout": httpx.Timeout(MAX_TIMEOUT),
            }
            if method == "POST":
                resp = await client.post(target_url, content=body, **kwargs)
            elif method == "PUT":
                resp = await client.put(target_url, content=body, **kwargs)
            elif method == "PATCH":
                resp = await client.patch(target_url, content=body, **kwargs)
            elif method == "DELETE":
                resp = await client.delete(target_url, **kwargs)
            else:  # GET, OPTIONS, etc.
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
    # Graceful shutdown handling (e.g., Ctrl+C in docker)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def signal_handler(sig, frame):
        log("Received SIGTERM/SIGINT; initiating shutdown...")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    uvicorn.run(
        app, host=HOST, port=PORT, log_level="info"
    )
