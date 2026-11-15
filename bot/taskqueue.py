"""Autopost task queue.

/addtask  ->  task saved in MongoDB  ->  one worker downloads episode by episode
          ->  file uploaded to STORAGE_CHANNEL  ->  post created/updated in MAIN_CHANNEL.

One task runs at a time (RAM friendly). Tasks survive restarts: anything that was
"running" goes back to "queued" and already-downloaded episodes are skipped via the
file cache.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from pymongo import ReturnDocument
from pyrogram import Client, enums

from api.client import api
from bot.downloader import download_and_upload, make_episode_filename, make_movie_filename
from config.settings import settings
from utils.helpers import esc
from utils.memory import trim_memory

log = logging.getLogger(__name__)

DEFAULT_QUALITY = "720p"
_QUALITY_RE = re.compile(r"^(\d{3,4}p|4k)(,(\d{3,4}p|4k))*$", re.I)
_SEASON_RE = re.compile(r"^s(\d+|all)$", re.I)
_EP_RE = re.compile(r"^e(\d+)(?:-(\d+))?$", re.I)
_TPL_RE = re.compile(r"^(?:tpl|t)=(\w+)$", re.I)
_AT_RE = re.compile(r"^at=(.+)$", re.I)

ACTIVE_STATUSES = ("queued", "running", "paused")


# ── Argument parsing ──────────────────────────────────────────────────

def parse_task_args(text: str) -> dict:
    """
    '/addtask Jujutsu Kaisen 720p,1080p s1 e1-12 tpl=cinematic at=20:30'
      -> {query, qualities, season, ep_from, ep_to, template, at}
    Options may appear anywhere after the name; everything else is the search query.
    'at' is returned raw (unparsed) — the caller validates it with utils.scheduling.
    """
    tokens = text.split()[1:]
    out = {"query": "", "qualities": [], "season": 1, "ep_from": None, "ep_to": None,
           "template": None, "at": None}
    words = []
    for tok in tokens:
        if _QUALITY_RE.match(tok):
            out["qualities"] = [("4K" if q.lower() == "4k" else q.lower()) for q in tok.split(",")]
        elif (m := _SEASON_RE.match(tok)):
            out["season"] = "all" if m.group(1).lower() == "all" else int(m.group(1))
        elif (m := _EP_RE.match(tok)):
            out["ep_from"] = int(m.group(1))
            out["ep_to"] = int(m.group(2) or m.group(1))
        elif (m := _TPL_RE.match(tok)):
            out["template"] = m.group(1).lower()
        elif (m := _AT_RE.match(tok)):
            out["at"] = m.group(1)
        else:
            words.append(tok)
    out["query"] = " ".join(words).strip()
    out["qualities"] = out["qualities"] or [DEFAULT_QUALITY]
    return out


# ── Source resolution (AnimeDekho -> AnimeDrive -> ToonFlix) ──────────

async def _iter_sources(title: str, season: int, ep_number: int, ep_slug: str | None,
                        raw_servers: list | None, quality: str):
    """
    Yield download candidates lazily, best source first. The next source is only
    contacted if the previous candidates failed (saves time and RAM).
    4K order: AnimeDrive -> ToonFlix -> AnimeDekho. Otherwise: AnimeDekho first.
    """
    from bot.handlers.callbacks import _lazy_resolve_servers, _find_quality_candidates

    is_4k = quality.lower() in ("4k", "2160p", "2160")
    order = ["drive", "toonflix", "dekho"] if is_4k else ["dekho", "drive", "toonflix"]

    for src in order:
        cands: list[dict] = []
        try:
            if src == "dekho":
                servers = raw_servers
                if servers is None and ep_slug:
                    servers = (await api.get_episode(ep_slug)).servers
                if servers:
                    resolved = await _lazy_resolve_servers(servers, quality)
                    for _srv, q in _find_quality_candidates(resolved or servers, quality):
                        cands.append({"url": q.master_url or q.url, "variant": q.url,
                                      "quality": q.resolution, "referer": "", "poster": ""})
            elif src == "drive":
                from extractors.animedrive import animedrive
                r = await animedrive.resolve_episode(title, season=season, episode=ep_number, quality_pref=quality)
                if r and r.get("url"):
                    cands.append({"url": r["url"], "variant": "", "quality": r.get("quality", quality),
                                  "referer": r.get("referer", "https://hubcloud.ist/"), "poster": r.get("poster", "")})
            else:
                from extractors.toonflix import toonflix
                r = await toonflix.resolve_episode(title, season=season, episode=ep_number, quality_pref=quality)
                if r and r.get("url"):
                    cands.append({"url": r["url"], "variant": "", "quality": r.get("quality", quality),
                                  "referer": r.get("referer", "https://drive.toonflix.in/"), "poster": r.get("poster", "")})
        except Exception as e:
            log.warning("Source %s failed for %s S%sE%s: %s", src, title, season, ep_number, e)
        for c in cands:
            yield c


# ── Manager ───────────────────────────────────────────────────────────
#
# Scalability note: N "lanes" (asyncio tasks, see max_concurrent_tasks) compete
# for the next queued task via an atomic find_one_and_update — the same claim
# a second bot *process* would use, so running several replicas of this bot
# against the same MongoDB is also safe (no double-processing). The actual
# bandwidth/RAM ceiling is enforced separately by the download/upload
# semaphores in bot/downloader.py, so raising lanes only lets more *series*
# interleave — it does not bypass the RAM budget.

class TaskManager:
    def __init__(self, client: Client):
        self.client = client
        self._wake = asyncio.Event()
        self._lanes: list[asyncio.Task] = []

    # -- lifecycle --
    async def start(self):
        from bot.database import db
        await db.tasks.update_many({"status": "running"}, {"$set": {"status": "queued"}})
        n = max(1, settings.bot.max_concurrent_tasks)
        self._lanes = [asyncio.create_task(self._loop(i)) for i in range(n)]
        log.info("TaskManager started (%d lane%s)", n, "" if n == 1 else "s")

    async def stop(self):
        for t in self._lanes:
            t.cancel()
        for t in self._lanes:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._lanes = []

    def wake(self):
        self._wake.set()

    # -- DB helpers --
    @staticmethod
    async def next_id() -> int:
        from bot.database import db
        doc = await db.counters.find_one_and_update(
            {"_id": "task"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER
        )
        return doc["seq"]

    async def _status(self, tid: int) -> str:
        from bot.database import db
        doc = await db.tasks.find_one({"_id": tid}, {"status": 1})
        return (doc or {}).get("status", "cancelled")

    async def _patch(self, tid: int, **fields):
        from bot.database import db
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        await db.tasks.update_one({"_id": tid}, {"$set": fields})

    async def _claim_next(self):
        """Atomically grab the highest-priority *due* task. Paused/future-scheduled
        tasks are left alone so multiple lanes never step on each other."""
        from bot.database import db
        now = datetime.now(timezone.utc).isoformat()
        return await db.tasks.find_one_and_update(
            {"status": "queued", "$or": [{"scheduled_at": None}, {"scheduled_at": {"$lte": now}}]},
            {"$set": {"status": "running"}},
            sort=[("priority", 1), ("_id", 1)],
            return_document=ReturnDocument.AFTER,
        )

    async def _next_wake_delay(self) -> float:
        """How long to sleep when idle: wake early for the nearest scheduled task."""
        from bot.database import db
        doc = await db.tasks.find_one(
            {"status": "queued", "scheduled_at": {"$ne": None}}, sort=[("scheduled_at", 1)]
        )
        if not doc:
            return 60.0
        try:
            when = datetime.fromisoformat(doc["scheduled_at"])
            delay = (when - datetime.now(timezone.utc)).total_seconds()
            return max(1.0, min(delay, 60.0))
        except Exception:
            return 60.0

    # -- worker loop (one per lane) --
    async def _loop(self, lane_id: int):
        while True:
            try:
                task = await self._claim_next()
                if not task:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=await self._next_wake_delay())
                    except asyncio.TimeoutError:
                        pass
                    continue
                await self._run_task(task)
                trim_memory()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("TaskManager lane %d error", lane_id)
                await asyncio.sleep(5)

    # -- one task --
    async def _run_task(self, task: dict):
        from bot.database import db
        from bot.library import library_manager
        from bot.channels import get_download_channel

        tid, owner = task["_id"], task["owner_id"]
        slug, qualities = task["slug"], task["qualities"]
        storage = await get_download_channel()

        status = await self.client.send_message(
            owner, f"▶️ <b>Task #{tid}</b> started — {esc(task['title'])}", parse_mode=enums.ParseMode.HTML
        )

        try:
            # Build the job list: (season, ep_number, ep_slug, ep_key)
            if task["is_movie"]:
                movie = await api.get_movie(slug)
                title, poster = movie.title, movie.poster
                desc, genres = movie.description, movie.genres
                raw_servers = movie.servers
                jobs = [(1, 1, None, "movie")]
            else:
                series = await api.get_series(slug)
                title, poster = series.title, series.poster
                desc, genres = series.description, series.genres
                raw_servers = None
                wanted = sorted(series.seasons) if task["season"] == "all" else [task["season"]]
                jobs = []
                for sn in wanted:
                    season = series.seasons.get(sn)
                    if not season:
                        continue
                    for ep in season.episodes:
                        if task.get("ep_from") and not (task["ep_from"] <= ep.number <= task["ep_to"]):
                            continue
                        jobs.append((sn, ep.number, ep.slug, f"S{sn}E{ep.number:02d}"))
                if not jobs:
                    raise RuntimeError(f"No matching episodes (available seasons: {sorted(series.seasons)})")

            total = len(jobs) * len(qualities)
            await self._patch(tid, total=total, title=title)
            done = skipped = 0
            failed: list[str] = []

            n = 0
            for season_no, ep_no, ep_slug, ep_key in jobs:
                for quality in qualities:
                    n += 1
                    st = await self._status(tid)
                    if st == "cancelled":
                        await status.edit_text(f"🛑 Task #{tid} cancelled ({done} posted).")
                        return
                    if st == "paused":
                        await status.edit_text(
                            f"⏸ <b>Task #{tid} paused</b> — {done} posted so far.\n"
                            f"Resume it from /queue.", parse_mode=enums.ParseMode.HTML,
                        )
                        return

                    if await db.get_cached_file(slug, quality, ep_key):
                        skipped += 1
                        continue

                    label = title if task["is_movie"] else f"{title} S{season_no}E{ep_no}"
                    try:
                        await status.edit_text(
                            f"📥 <b>Task #{tid}</b> — {n}/{total}\n{esc(label)} [{quality}]\n"
                            f"✅ {done} posted · ❌ {len(failed)} failed",
                            parse_mode=enums.ParseMode.HTML,
                        )
                    except Exception:
                        pass

                    sent, chosen = None, quality
                    async for cand in _iter_sources(title, season_no, ep_no, ep_slug, raw_servers, quality):
                        fname = (make_movie_filename(title, cand["quality"]) if task["is_movie"]
                                 else make_episode_filename(title, season_no, ep_no, cand["quality"]))
                        ok, msg = await download_and_upload(
                            storage, cand["url"], cand["quality"], fname, label, status, self.client,
                            variant_url=cand["variant"], referer=cand["referer"],
                            poster_url=poster or cand["poster"] or "",
                        )
                        if ok and msg:
                            sent, chosen = msg, cand["quality"]
                            break

                    if not sent:
                        failed.append(f"{ep_key}[{quality}]")
                        continue

                    media = sent.video or sent.document
                    if not media:
                        failed.append(f"{ep_key}[{quality}]")
                        continue

                    await db.save_file(
                        series_slug=slug, series_title=title, quality=chosen, episode_key=ep_key,
                        file_id=media.file_id, file_unique_id=media.file_unique_id,
                        storage_channel_id=sent.chat.id, storage_message_id=sent.id,
                        is_video=bool(sent.video),
                    )
                    if library_manager:
                        # Posts the quality button immediately, and — the first time this
                        # episode gets any quality — the main-channel "added" notice too.
                        await library_manager.save_to_library(
                            slug, title, chosen, ep_key, media.file_id, media.file_unique_id,
                            poster_url=poster, is_movie=task["is_movie"], template=task.get("template"),
                        )
                    done += 1
                    await self._patch(tid, done=done, failed=len(failed))
                    await asyncio.sleep(2)  # be nice to Telegram / sources

            final = "done" if not failed else "done_with_errors"
            await self._patch(tid, status=final, failed_items=failed[:50])
            text = (
                f"{'✅' if not failed else '⚠️'} <b>Task #{tid} finished</b> — {esc(title)}\n"
                f"📤 {done} posted · ⏭ {skipped} already in library · ❌ {len(failed)} failed"
            )
            if failed:
                text += "\n<code>" + esc(", ".join(failed[:15])) + "</code>\n<i>Run /addtask again to retry only the missing ones.</i>"
            await status.edit_text(text, parse_mode=enums.ParseMode.HTML)

        except asyncio.CancelledError:
            await self._patch(tid, status="queued")   # restart-safe
            raise
        except Exception as e:
            log.exception("Task #%s failed", tid)
            await self._patch(tid, status="failed", error=str(e)[:300])
            try:
                await status.edit_text(f"❌ <b>Task #{tid} failed</b>\n<code>{esc(str(e)[:300])}</code>",
                                       parse_mode=enums.ParseMode.HTML)
            except Exception:
                pass


task_manager: TaskManager | None = None
