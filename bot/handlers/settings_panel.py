"""Owner-only /settings panel — view & edit the bot's runtime-configurable
settings without touching .env or restarting. Fields that must be fixed at
process start (RAM/CPU tuning knobs, since they size semaphores and thread
pools once at boot) are shown read-only with their env var name, so the
owner can see everything in one place even though those need a restart.
"""

import logging

from pyrogram import Client, enums
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.auth import is_owner, require_owner
from config.settings import settings
from utils.helpers import esc

log = logging.getLogger(__name__)
HTML = enums.ParseMode.HTML

# key -> field spec. "env_default" is called lazily so settings module edits pick up.
_FIELDS = {
    "main_channel_id": {
        "label": "📣 Main channel ID",
        "env_default": lambda: settings.bot.main_channel,
        "hint": "Send the channel's numeric ID (e.g. -1001234567890). The bot must be an admin there.",
        "kind": "channel",
    },
    "download_channel_id": {
        "label": "🗂 Download channel ID",
        "env_default": lambda: settings.bot.storage_channel or settings.bot.main_channel,
        "hint": "Send the channel's numeric ID. This is the second channel with quality buttons.",
        "kind": "channel",
    },
    "audio_label": {
        "label": "🔊 Audio label",
        "env_default": lambda: settings.bot.audio_label,
        "hint": "What shows on the episode notice, e.g. Hindi, English, Multi Audio.",
        "kind": "text",
    },
    "track_poll_seconds": {
        "label": "🔁 Auto-detect check interval (s)",
        "env_default": lambda: settings.bot.track_poll_seconds,
        "hint": "How often /track'ed series are checked for new episodes, in seconds (min 60).",
        "kind": "int",
    },
}

_READONLY = [
    ("MAX_DOWNLOADS", "Parallel downloads", lambda: settings.bot.max_downloads),
    ("MAX_UPLOADS", "Parallel uploads", lambda: settings.bot.max_uploads),
    ("M3U8_THREADS", "Segment download threads", lambda: settings.bot.m3u8_threads),
    ("MAX_CONCURRENT_TASKS", "Concurrent autopost tasks", lambda: settings.bot.max_concurrent_tasks),
    ("TIMEZONE", "Timezone (for at=/reschedule)", lambda: settings.bot.timezone),
]

# user_id -> key currently being edited
_awaiting: dict[int, str] = {}


async def _current(db, key: str):
    spec = _FIELDS[key]
    val = await db.get_config(key, None)
    return val if val is not None else spec["env_default"]()


async def _panel_text(db) -> str:
    lines = ["⚙️ <b>Bot Settings</b>\n", "<b>Live — apply immediately:</b>"]
    for key, spec in _FIELDS.items():
        val = await _current(db, key)
        lines.append(f"• {spec['label']}: <code>{esc(str(val))}</code>")
    lines.append("\n<b>Fixed at startup — edit .env and restart:</b>")
    for env_name, label, getter in _READONLY:
        lines.append(f"• {label}: <code>{esc(str(getter()))}</code> <i>({env_name})</i>")
    lines.append("\n<i>Episode notice design: use /settemplate (visual picker)</i>")
    return "\n".join(lines)


def _panel_markup() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for key, spec in _FIELDS.items():
        row.append(InlineKeyboardButton(f"✏️ {spec['label'].split(' ', 1)[-1]}", callback_data=f"set:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="set:_refresh")])
    return InlineKeyboardMarkup(rows)


@require_owner
async def cmd_settings(client: Client, message: Message):
    from bot.database import db
    if not db:
        await message.reply_text("⚠️ Database not ready yet.")
        return
    await message.reply_text(await _panel_text(db), parse_mode=HTML, reply_markup=_panel_markup())


async def settings_callback(client: Client, q: CallbackQuery):
    from bot.database import db
    if not is_owner(q.from_user.id):
        await q.answer("Owner only", show_alert=True)
        return
    _, key = q.data.split(":", 1)

    if key == "_refresh":
        try:
            await q.message.edit_text(await _panel_text(db), parse_mode=HTML, reply_markup=_panel_markup())
        except Exception:
            pass
        await q.answer()
        return

    spec = _FIELDS.get(key)
    if not spec:
        await q.answer("Unknown setting", show_alert=True)
        return

    _awaiting[q.from_user.id] = key
    current = await _current(db, key)
    await q.message.reply_text(
        f"✏️ <b>{spec['label']}</b>\nCurrent: <code>{esc(str(current))}</code>\n\n"
        f"{spec['hint']}\n\n<i>Send the new value, or /cancel.</i>",
        parse_mode=HTML,
    )
    await q.answer()


def awaiting_input(_, __, message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in _awaiting and is_owner(message.from_user.id))


async def handle_settings_input(client: Client, message: Message):
    from bot.database import db
    uid = message.from_user.id
    key = _awaiting.pop(uid, None)
    if not key:
        return
    spec = _FIELDS[key]
    text = (message.text or "").strip()

    if text == "/cancel":
        await message.reply_text("Cancelled.")
        return

    if spec["kind"] == "channel":
        try:
            value = int(text)
        except ValueError:
            _awaiting[uid] = key  # let them retry
            await message.reply_text("⚠️ That's not a numeric channel ID. Try again, or /cancel.")
            return
        try:
            chat = await client.get_chat(value)
            confirm = f"✅ Resolved: {esc(chat.title or str(value))}"
        except Exception as e:
            _awaiting[uid] = key
            await message.reply_text(
                f"⚠️ Couldn't access that chat ({esc(str(e)[:150])}). "
                f"Make sure the bot is an admin there, then try again, or /cancel.",
            )
            return
        await db.set_config(key, value)
        await message.reply_text(f"{confirm}\nSaved — takes effect immediately.")

    elif spec["kind"] == "int":
        try:
            value = max(1, int(text))
        except ValueError:
            _awaiting[uid] = key
            await message.reply_text("⚠️ That's not a number. Try again, or /cancel.")
            return
        await db.set_config(key, value)
        await message.reply_text(f"✅ Saved — {spec['label']} is now <code>{value}</code>.", parse_mode=HTML)

    else:  # text
        if not text:
            _awaiting[uid] = key
            await message.reply_text("⚠️ Send some text, or /cancel.")
            return
        await db.set_config(key, text)
        await message.reply_text(f"✅ Saved — {spec['label']} is now <code>{esc(text)}</code>.", parse_mode=HTML)
