"""Minimal HTTP health endpoint for hosting platforms (Railway/Render/Fly/Docker
healthcheck) that expect a bound TCP port to consider the service alive.

GET /health -> 200 {"status": "ok", ...} or 503 if MongoDB is unreachable.
"""

from __future__ import annotations

import logging
import time

from aiohttp import web

log = logging.getLogger(__name__)

_start = time.time()
_runner: web.AppRunner | None = None


async def _health(request: web.Request) -> web.Response:
    from bot.database import db

    detail: dict = {"uptime_s": int(time.time() - _start)}
    ok = True

    if db is None:
        ok = False
        detail["mongo"] = "not connected"
    else:
        try:
            await db.ping()
            detail["mongo"] = "ok"
            detail["queue"] = {
                "queued": await db.tasks.count_documents({"status": "queued"}),
                "running": await db.tasks.count_documents({"status": "running"}),
                "paused": await db.tasks.count_documents({"status": "paused"}),
            }
            detail["tracked"] = await db.tracked.count_documents({"active": True})
        except Exception as e:
            ok = False
            detail["mongo"] = f"error: {e}"

    detail["status"] = "ok" if ok else "degraded"
    return web.json_response(detail, status=200 if ok else 503)


async def start(port: int) -> None:
    global _runner
    app = web.Application()
    app.router.add_get("/health", _health)
    app.router.add_get("/", _health)
    _runner = web.AppRunner(app, access_log=None)
    await _runner.setup()
    site = web.TCPSite(_runner, "0.0.0.0", port)
    try:
        await site.start()
        log.info("Health server listening on :%d/health", port)
    except OSError as e:
        # Non-fatal: some hosts don't expose a port at all. The bot still works.
        log.warning("Could not bind health server on :%d (%s) — skipping", port, e)
        _runner = None


async def stop() -> None:
    global _runner
    if _runner:
        await _runner.cleanup()
        _runner = None
