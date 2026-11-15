"""Download hub: one quality-picker post per episode in the DOWNLOAD channel,
plus a single "episode added" notice in the MAIN channel the first time an
episode appears. Both channels are posted to via the LibraryManager below,
which every download path (interactive, batch, autopost, AI tool) calls into
through the same save_to_library() entrypoint it always has.

Quality buttons are deep links (t.me/<bot>?start=get_<slug>_<quality>_<ep>) so
clicking one reuses the existing /start file-request handler in
bot/handlers/commands.py — no separate callback plumbing needed — and keeps
working with the child-bot load-balancer (bot/child_bots.py) exactly as
before.
"""

from __future__ import annotations
import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timezone

import aiohttp
from pyrogram import Client, enums
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot import templates
from bot.database import Database
from utils.helpers import esc

log = logging.getLogger(__name__)


async def _download_poster(url: str) -> str | None:
    """Download poster image to a temp file, return path or None."""
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning("Poster download failed: HTTP %d for %s", resp.status, url[:80])
                    return None
                ct = resp.content_type or ""
                ext = ".jpg"
                if "png" in ct:
                    ext = ".png"
                elif "webp" in ct:
                    ext = ".webp"
                if (resp.content_length or 0) > 5_000_000:
                    return None
                data = await resp.read()
                if len(data) < 1000:
                    log.warning("Poster too small (%d bytes), skipping", len(data))
                    return None
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir=tempfile.gettempdir())
                tmp.write(data)
                tmp.close()
                return tmp.name
    except Exception as e:
        log.warning("Poster download error: %s", e)
        return None


# Per-(series, episode) lock to prevent concurrent posts racing each other
_locks: dict[str, asyncio.Lock] = {}


def _get_lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    if len(_locks) > 500:
        oldest = list(_locks.keys())[:200]
        for k in oldest:
            if not _locks[k].locked():
                _locks.pop(k, None)
    return _locks[key]


def _parse_ep_key(key: str) -> tuple[int, int]:
    m = re.match(r"S(\d+)E(\d+)", key or "", re.IGNORECASE)
    return (int(m.group(1)), int(m.group(2))) if m else (1, 1)


class LibraryManager:
    def __init__(self, client: Client, db: Database, main_channel: int, bot_username: str):
        self.client = client
        self.db = db
        self.channel = main_channel  # kept for callers that check `library_manager.channel`
        self.bot_username = bot_username

    # ── Public API (unchanged signature — every caller keeps working) ──

    async def save_to_library(
        self,
        series_slug: str,
        series_title: str,
        quality: str,
        episode_key: str,
        file_id: str,
        file_unique_id: str,
        poster_url: str | None = None,
        is_movie: bool = False,
        description: str = "",
        genres: list[str] | None = None,
        template: str | None = None,
        publish: bool = True,
    ):
        """Record a downloaded file and sync its download-channel post. The
        first time an episode gets any quality, also fires the one-time
        main-channel "added" notice. `description`/`genres`/`template` are
        accepted for backward compatibility but no longer change this call's
        behavior (there's no longer a long-lived caption to vary by them)."""
        if not self.db:
            return
        async with _get_lock(f"{series_slug}:{episode_key}"):
            await self._upsert_file(series_slug, series_title, quality, episode_key, file_id, file_unique_id)
            if publish:
                await self._sync_episode_post(series_slug, series_title, episode_key, poster_url, is_movie)

    async def publish_album(self, *args, **kwargs):
        """Deprecated: posting now happens immediately inside save_to_library."""
        return

    async def _upsert_file(self, series_slug, series_title, quality, episode_key, file_id, file_unique_id):
        now = datetime.now(timezone.utc).isoformat()
        await self.db.files.update_one(
            {"series_slug": series_slug, "quality": quality, "episode_key": episode_key},
            {"$set": {
                "file_id": file_id,
                "file_unique_id": file_unique_id,
                "series_slug": series_slug,
                "series_title": series_title,
                "episode_key": episode_key,
                "quality": quality,
                "updated_at": now,
            }},
            upsert=True,
        )

    async def _episode_qualities(self, series_slug: str, episode_key: str) -> list[str]:
        quals: set[str] = set()
        async for f in self.db.files.find({"series_slug": series_slug, "episode_key": episode_key}, {"quality": 1}):
            if f.get("quality"):
                quals.add(f["quality"])
        return _sort_qualities(quals)

    def _episode_buttons(self, series_slug: str, episode_key: str, qualities: list[str]) -> InlineKeyboardMarkup:
        from bot.child_bots import child_bot_manager
        buttons, row = [], []
        for q in qualities:
            target_bot = self.bot_username
            if child_bot_manager:
                assigned = child_bot_manager.get_bot_for_quality(q)
                if assigned:
                    target_bot = assigned
            deep = f"https://t.me/{target_bot}?start=get_{series_slug}_{q}_{episode_key}"
            row.append(InlineKeyboardButton(f"📥 {q}", url=deep))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        return InlineKeyboardMarkup(buttons)

    async def _sync_episode_post(self, series_slug, series_title, episode_key, poster_url, is_movie):
        """Create/update the quality-picker post for this one episode in the
        download channel, and fire the main-channel notice if it's new."""
        from bot.channels import get_download_channel
        dl_channel = await get_download_channel()
        if not dl_channel:
            return

        qualities = await self._episode_qualities(series_slug, episode_key)
        entry = await self.db.episode_posts.find_one({"series_slug": series_slug, "episode_key": episode_key})

        if not qualities:
            # Nothing left for this episode (e.g. all files were deleted) -> remove the post.
            if entry and entry.get("message_id"):
                try:
                    await self.client.delete_messages(entry.get("channel_id", dl_channel), entry["message_id"])
                except Exception:
                    pass
                await self.db.episode_posts.delete_one({"_id": entry["_id"]})
            return

        is_movie = is_movie or episode_key.lower() == "movie"
        season_no, ep_no = _parse_ep_key(episode_key)
        markup = self._episode_buttons(series_slug, episode_key, qualities)
        caption = (
            f"🎬 <b>{esc(series_title)}</b>\n"
            f"{'🎞 Movie' if is_movie else f'📺 Episode {ep_no:02d} · Season {season_no}'}\n\n"
            f"👇 Tap a quality to get the file"
        )

        message_id = (entry or {}).get("message_id")
        is_new = entry is None
        if message_id:
            try:
                await self.client.edit_message_reply_markup(
                    entry.get("channel_id", dl_channel), message_id, reply_markup=markup
                )
            except MessageNotModified:
                pass
            except Exception as e:
                log.warning("Download post %s unusable, recreating: %s", message_id, e)
                message_id = None

        if not message_id:
            poster_path = await _download_poster(poster_url) if poster_url else None
            try:
                if poster_path:
                    msg = await self.client.send_photo(
                        dl_channel, poster_path, caption=caption,
                        parse_mode=enums.ParseMode.HTML, reply_markup=markup,
                    )
                else:
                    msg = await self.client.send_message(
                        dl_channel, caption, parse_mode=enums.ParseMode.HTML,
                        reply_markup=markup, disable_web_page_preview=True,
                    )
                message_id = msg.id
            except Exception as e:
                log.error("Failed to create download-channel post for %s %s: %s", series_slug, episode_key, e)
                return
            finally:
                if poster_path:
                    try:
                        os.remove(poster_path)
                    except Exception:
                        pass

        await self.db.episode_posts.update_one(
            {"series_slug": series_slug, "episode_key": episode_key},
            {"$set": {
                "channel_id": dl_channel, "message_id": message_id, "series_title": series_title,
                "poster_url": poster_url, "is_movie": is_movie, "season": season_no, "episode": ep_no,
            }},
            upsert=True,
        )

        if is_new:
            await self._post_main_notification(series_title, episode_key, is_movie, poster_url, dl_channel, message_id)

    async def _post_main_notification(self, series_title, episode_key, is_movie, poster_url, dl_channel, dl_message_id):
        from bot.channels import get_main_channel, channel_post_link
        from config.settings import settings

        main_channel = await get_main_channel()
        if not main_channel:
            return

        tpl_name = await self.db.get_config("notify_template", settings.bot.notify_template)
        tpl_name = templates.resolve_name(tpl_name)
        audio = await self.db.get_config("audio_label", settings.bot.audio_label)
        season_no, ep_no = _parse_ep_key(episode_key)

        data = templates.NoticeData(
            title=series_title, season=season_no, episode=ep_no,
            is_movie=is_movie, audio=audio, bot_username=self.bot_username,
        )
        caption = templates.render(tpl_name, data)
        link = await channel_post_link(self.client, dl_channel, dl_message_id)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Download Now", url=link)]])

        poster_path = await _download_poster(poster_url) if poster_url else None
        try:
            if poster_path:
                await self.client.send_photo(
                    main_channel, poster_path, caption=caption,
                    parse_mode=enums.ParseMode.HTML, reply_markup=markup,
                )
            else:
                await self.client.send_message(
                    main_channel, caption, parse_mode=enums.ParseMode.HTML,
                    reply_markup=markup, disable_web_page_preview=True,
                )
        except Exception as e:
            log.warning("Failed to post main-channel notification for %s: %s", series_title, e)
        finally:
            if poster_path:
                try:
                    os.remove(poster_path)
                except Exception:
                    pass

    async def get_file(self, series_slug: str, quality: str, episode_key: str) -> str | None:
        """Get file_id for a specific episode."""
        entry = await self.db.files.find_one({
            "series_slug": series_slug, "quality": quality, "episode_key": episode_key,
        })
        return entry.get("file_id") if entry else None

    async def delete_album(self, series_slug: str):
        """Delete every download-channel post for a series (full series delete)."""
        async for entry in self.db.episode_posts.find({"series_slug": series_slug}):
            if entry.get("message_id"):
                try:
                    await self.client.delete_messages(entry.get("channel_id"), entry["message_id"])
                except Exception as e:
                    log.warning("Could not delete download post: %s", e)
        await self.db.episode_posts.delete_many({"series_slug": series_slug})

    async def remove_episode(self, series_slug: str, episode_key: str):
        """Remove a single episode's download-channel post (no files left for it)."""
        await self._sync_episode_post(series_slug, series_slug, episode_key, None, False)

    async def refresh_all_albums(self, template: str | None = None) -> int:
        """Re-render quality buttons on every download-channel post — handy after
        changing which child bot serves which quality. `template` is accepted
        for backward compatibility but unused (main-channel notices are
        one-shot and nothing is left to restyle)."""
        from bot.channels import get_download_channel
        dl_channel = await get_download_channel()
        if not dl_channel:
            return 0
        refreshed = 0
        async for entry in self.db.episode_posts.find({}):
            slug, ep_key = entry.get("series_slug"), entry.get("episode_key")
            msg_id = entry.get("message_id")
            if not (slug and ep_key and msg_id):
                continue
            try:
                qualities = await self._episode_qualities(slug, ep_key)
                if not qualities:
                    continue
                markup = self._episode_buttons(slug, ep_key, qualities)
                await self.client.edit_message_reply_markup(
                    entry.get("channel_id", dl_channel), msg_id, reply_markup=markup
                )
                refreshed += 1
                await asyncio.sleep(0.3)
            except MessageNotModified:
                refreshed += 1
            except Exception as e:
                log.warning("Failed to refresh download post %s: %s", msg_id, e)
        return refreshed


def _ep_sort_key(key: str):
    m = re.match(r"S(\d+)E(\d+)", key, re.IGNORECASE)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    if key.lower() == "movie":
        return (0, 0)
    return (999, 0)


def _sort_qualities(qualities: set[str]) -> list[str]:
    order = {"360p": 1, "480p": 2, "720p": 3, "1080p": 4, "4k": 5, "2160p": 5, "auto": 9}
    return sorted(qualities, key=lambda q: order.get(q.lower(), 8))


# Singleton
library_manager: LibraryManager | None = None
