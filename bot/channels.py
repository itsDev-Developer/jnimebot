"""Resolve which channels to post to. Checks a runtime DB override first
(set via /settings) so the owner can change channels without editing .env
and restarting; falls back to the env-configured defaults otherwise.
"""

from __future__ import annotations

import logging

from pyrogram import Client

from config.settings import settings

log = logging.getLogger(__name__)

_link_cache: dict[int, str | None] = {}  # channel_id -> username (or None if private)


async def get_main_channel() -> int:
    from bot.database import db
    if db:
        val = await db.get_config("main_channel_id", 0)
        if val:
            return int(val)
    return settings.bot.main_channel


async def get_download_channel() -> int:
    """The second channel: where the actual video + quality buttons live."""
    from bot.database import db
    if db:
        val = await db.get_config("download_channel_id", 0)
        if val:
            return int(val)
    return settings.bot.storage_channel or settings.bot.main_channel


async def channel_post_link(client: Client, channel_id: int, message_id: int) -> str:
    """Build a t.me link to a specific message, using the channel's public
    username if it has one, else the private t.me/c/ form (members-only)."""
    username = _link_cache.get(channel_id, "__unset__")
    if username == "__unset__":
        try:
            chat = await client.get_chat(channel_id)
            username = chat.username
        except Exception as e:
            log.debug("Could not resolve channel %s username: %s", channel_id, e)
            username = None
        _link_cache[channel_id] = username

    if username:
        return f"https://t.me/{username}/{message_id}"
    cid = str(channel_id)
    cid = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f"https://t.me/c/{cid}/{message_id}"
