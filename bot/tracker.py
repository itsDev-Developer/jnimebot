"""Auto-detect new episodes for tracked (ongoing) series.

/track <name> saves a series with its current episode count as the baseline.
Every TRACK_POLL_SECONDS, we re-check each tracked series; if the site now has
more episodes than the baseline, a normal autopost task is queued for the
difference (existing episodes are skipped automatically by the file cache).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from pyrogram import Client, enums

from api.client import api
from config.settings import settings
from utils.helpers import esc

log = logging.getLogger(__name__)


def _episode_count(series) -> int:
    return sum(len(s.episodes) for s in series.seasons.values())


class Tracker:
    def __init__(self, client: Client):
        self.client = client
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._loop())
        log.info("Episode tracker started (every %ds)", settings.bot.track_poll_seconds)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self):
        from bot.database import db
        while True:
            try:
                interval = settings.bot.track_poll_seconds
                if db:
                    interval = await db.get_config("track_poll_seconds", interval)
                await asyncio.sleep(max(60, int(interval)))
                await self._scan()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Tracker scan failed")

    async def _scan(self):
        from bot.database import db
        from bot.taskqueue import task_manager

        docs = await db.tracked.find({"active": True}).to_list(length=500)
        if not docs:
            return
        now = datetime.now(timezone.utc).isoformat()

        for t in docs:
            try:
                series = await api.get_series(t["slug"])
            except Exception as e:
                log.warning("Tracker: could not fetch %s: %s", t["slug"], e)
                await db.tracked.update_one({"_id": t["_id"]}, {"$set": {"last_checked": now, "last_error": str(e)[:200]}})
                continue

            total_eps = _episode_count(series)
            await db.tracked.update_one(
                {"_id": t["_id"]}, {"$set": {"last_checked": now, "title": series.title, "last_error": None}}
            )

            baseline = t.get("last_seen_count", 0)
            if total_eps <= baseline or not task_manager:
                continue

            new_count = total_eps - baseline
            tid = await task_manager.next_id()
            await db.tasks.insert_one({
                "_id": tid, "owner_id": t["added_by"], "status": "queued",
                "slug": t["slug"], "title": series.title, "is_movie": False,
                "season": "all", "qualities": t.get("qualities") or ["720p"],
                "ep_from": None, "ep_to": None, "template": t.get("template"),
                "scheduled_at": None, "priority": tid, "auto": True,
                "done": 0, "total": 0, "created_at": now,
            })
            await db.tracked.update_one({"_id": t["_id"]}, {"$set": {"last_seen_count": total_eps}})
            task_manager.wake()
            log.info("Tracker: %d new episode(s) for %s, queued task #%d", new_count, series.title, tid)
            try:
                await self.client.send_message(
                    t["added_by"],
                    f"🆕 <b>{esc(series.title)}</b> has {new_count} new episode(s) — "
                    f"task #{tid} queued automatically.",
                    parse_mode=enums.ParseMode.HTML,
                )
            except Exception:
                pass


tracker: Tracker | None = None
