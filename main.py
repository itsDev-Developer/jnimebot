#!/usr/bin/env python3
"""AnimeDekho Telegram Bot — entrypoint (Pyrogram/MTProto)."""

import logging
import sys
import time

from config.settings import settings
from bot.app import create_app


import asyncio
from pyrogram import idle

async def async_main():
    # Small shared thread pool (cloudscraper + file writes) instead of ~cpu*5 threads
    from concurrent.futures import ThreadPoolExecutor
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=6, thread_name_prefix="io"))
    app = create_app()
    await app.start()
    
    if hasattr(app, "on_start") and callable(app.on_start):
        await app.on_start(app)
        
    await idle()
    
    if hasattr(app, "on_stop") and callable(app.on_stop):
        await app.on_stop(app)
        
    await app.stop()

def main():
    logging.basicConfig(
        level=getattr(logging, settings.bot.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    log = logging.getLogger("animedekho")
    log.info("Starting AnimeDekho Bot (Pyrogram/MTProto)...")

    try:
        import uvloop  # faster, lighter event loop (Linux/macOS)
        uvloop.install()
    except ImportError:
        pass

    # Auto-restart with backoff on transient crashes (network blips, Telegram
    # hiccups, a bad MongoDB reconnect). Configuration errors (missing env
    # vars, raised as RuntimeError by create_app) exit immediately instead of
    # looping forever on something a restart can't fix.
    backoff = 5
    while True:
        started = time.monotonic()
        try:
            asyncio.run(async_main())
            log.info("Shut down cleanly.")
            break
        except KeyboardInterrupt:
            log.info("Shutting down...")
            break
        except RuntimeError as e:
            log.critical("Configuration error: %s", e)
            sys.exit(1)
        except Exception as e:
            if time.monotonic() - started > 120:
                backoff = 5  # ran fine for a while — don't punish it for a fresh blip
            log.error("Bot crashed: %s — restarting in %ds", e, backoff, exc_info=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


if __name__ == "__main__":
    main()
