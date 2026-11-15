"""Owner commands: /addtask /queue /tasks /canceltask /reschedule /settemplate (+ callbacks)."""

import logging
from dataclasses import replace
from datetime import datetime, timezone

from pyrogram import Client, enums
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from api.client import api
from bot import templates
from bot.auth import is_owner, require_owner
from bot.taskqueue import ACTIVE_STATUSES
from config.settings import settings
from utils import scheduling
from utils.helpers import esc

log = logging.getLogger(__name__)
HTML = enums.ParseMode.HTML

ADDTASK_HELP = (
    "<b>➕ Add an autopost task</b>\n\n"
    "<code>/addtask &lt;name&gt; [quality] [sN] [eA-B] [tpl=name] [at=time]</code>\n\n"
    "Examples:\n"
    "• <code>/addtask Jujutsu Kaisen 720p s1</code>\n"
    "• <code>/addtask Naruto 480p,1080p s1 e1-12</code>\n"
    "• <code>/addtask One Piece 720p sall tpl=cinematic</code>\n"
    "• <code>/addtask Your Name 1080p</code> (movie)\n"
    "• <code>/addtask Bleach 720p at=20:30</code> (today/tomorrow at 20:30)\n"
    "• <code>/addtask Bleach 720p at=+2h</code> (2 hours from now)\n\n"
    "Defaults: 720p, season 1, all episodes, default template, starts immediately.\n"
    "Templates: " + ", ".join(templates.template_names()) + ", random\n\n"
    "Manage the queue with /queue — pause, resume, reorder or cancel tasks."
)


async def _queue(client: Client, chat_id: int, tid: int, slug: str, title: str, is_movie: bool):
    from bot.database import db
    from bot.taskqueue import task_manager
    await db.tasks.update_one(
        {"_id": tid},
        {"$set": {"slug": slug, "title": title, "is_movie": is_movie, "status": "queued", "priority": tid}},
    )
    if task_manager:
        task_manager.wake()
    task = await db.tasks.find_one({"_id": tid}, {"scheduled_at": 1})
    when = (task or {}).get("scheduled_at")
    pending = await db.tasks.count_documents({"status": {"$in": ["queued", "running"]}})
    extra = f"\n⏰ Scheduled for {scheduling.human(when)}" if when else ""
    await client.send_message(
        chat_id,
        f"✅ <b>Task #{tid}</b> queued — {esc(title)}{extra}\n📋 {pending} task(s) in queue. "
        f"Posts will appear in the main channel automatically. Use /queue to manage.",
        parse_mode=HTML,
    )


@require_owner
async def cmd_addtask(client: Client, message: Message):
    from bot.database import db
    from bot.taskqueue import parse_task_args, task_manager

    if not settings.bot.main_channel:
        await message.reply_text("⚠️ Set MAIN_CHANNEL first — autopost needs a channel to post in.")
        return
    if task_manager is None:
        await message.reply_text("⚠️ Task manager is not running yet, try again in a few seconds.")
        return

    args = parse_task_args(message.text or "")
    if not args["query"]:
        await message.reply_text(ADDTASK_HELP, parse_mode=HTML)
        return
    if args["template"] and args["template"] != templates.RANDOM and args["template"] not in templates.TEMPLATES:
        await message.reply_text("⚠️ Unknown template. Use: " + ", ".join(templates.template_names()) + ", random")
        return

    scheduled_at = None
    if args["at"]:
        try:
            scheduled_at = scheduling.parse_at(args["at"])
        except ValueError as e:
            await message.reply_text(f"⚠️ {e}")
            return

    wait = await message.reply_text("🔍 Searching...")
    try:
        results = await api.search(args["query"])
    except Exception as e:
        await wait.edit_text(f"⚠️ Search error: {esc(str(e)[:150])}")
        return
    if not results:
        await wait.edit_text("❌ No results found. Try a different name.")
        return
    await wait.delete()

    tid = await task_manager.next_id()
    choices = [{"slug": r.slug, "title": r.title, "is_movie": not r.is_series} for r in results[:5]]
    await db.tasks.insert_one({
        "_id": tid, "owner_id": message.from_user.id, "status": "draft",
        "query": args["query"], "qualities": args["qualities"], "season": args["season"],
        "ep_from": args["ep_from"], "ep_to": args["ep_to"], "template": args["template"],
        "scheduled_at": scheduled_at, "priority": tid,
        "choices": choices, "done": 0, "total": 0,
        "created_at": message.date.isoformat() if message.date else "",
    })

    if len(choices) == 1:
        c = choices[0]
        await _queue(client, message.chat.id, tid, c["slug"], c["title"], c["is_movie"])
        return

    buttons = [
        [InlineKeyboardButton(f"{'🎬' if c['is_movie'] else '📺'} {c['title'][:45]}", callback_data=f"tkpick:{tid}:{i}")]
        for i, c in enumerate(choices)
    ]
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data=f"tkpick:{tid}:x")])
    opts = f"{', '.join(args['qualities'])} · S{args['season']}"
    if scheduled_at:
        opts += f" · ⏰ {scheduling.human(scheduled_at)}"
    await message.reply_text(
        f"🎯 <b>Which one?</b> ({esc(opts)})", parse_mode=HTML, reply_markup=InlineKeyboardMarkup(buttons)
    )


async def tkpick_callback(client: Client, q: CallbackQuery):
    from bot.database import db
    if not is_owner(q.from_user.id):
        await q.answer("Owner only", show_alert=True)
        return
    _, tid, idx = q.data.split(":")
    tid = int(tid)
    task = await db.tasks.find_one({"_id": tid, "status": "draft"})
    if not task:
        await q.answer("Task already handled.", show_alert=True)
        return
    if idx == "x":
        await db.tasks.update_one({"_id": tid}, {"$set": {"status": "cancelled"}})
        await q.message.edit_text("✖ Cancelled.")
        return
    c = task["choices"][int(idx)]
    await q.message.delete()
    await _queue(client, q.message.chat.id, tid, c["slug"], c["title"], c["is_movie"])
    await q.answer("Queued")


@require_owner
async def cmd_tasks(client: Client, message: Message):
    """History view — every task, most recent first. Use /queue to manage active ones."""
    from bot.database import db
    docs = await db.tasks.find({"status": {"$ne": "draft"}}).sort("_id", -1).to_list(length=10)
    if not docs:
        await message.reply_text("📋 No tasks yet. Use /addtask")
        return
    icon = {"queued": "🕒", "running": "▶️", "paused": "⏸", "done": "✅",
            "done_with_errors": "⚠️", "failed": "❌", "cancelled": "🛑"}
    lines = ["📋 <b>Task history</b> (latest 10)\n"]
    for t in docs:
        auto = " 🔁" if t.get("auto") else ""
        lines.append(
            f"{icon.get(t['status'], '•')} <b>#{t['_id']}</b> {esc(t.get('title', '?'))}{auto} "
            f"[{', '.join(t.get('qualities', []))}] — {t.get('done', 0)}/{t.get('total', '?')}"
        )
    lines.append("\n<i>/queue to manage active tasks (pause, reorder, cancel)</i>")
    await message.reply_text("\n".join(lines), parse_mode=HTML)


@require_owner
async def cmd_canceltask(client: Client, message: Message):
    from bot.database import db
    from bot.taskqueue import task_manager
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply_text("Usage: /canceltask <id>")
        return
    res = await db.tasks.update_one(
        {"_id": int(parts[1]), "status": {"$in": list(ACTIVE_STATUSES)}}, {"$set": {"status": "cancelled"}}
    )
    if task_manager:
        task_manager.wake()
    await message.reply_text(
        "🛑 Cancelled (a running task stops after the current episode)." if res.modified_count
        else "ℹ️ Task not found or already finished."
    )


@require_owner
async def cmd_reschedule(client: Client, message: Message):
    from bot.database import db
    from bot.taskqueue import task_manager
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.reply_text(f"Usage: <code>/reschedule &lt;id&gt; &lt;time&gt;</code>\n{scheduling.HELP}", parse_mode=HTML)
        return
    tid = int(parts[1])
    try:
        scheduled_at = scheduling.parse_at(parts[2])
    except ValueError as e:
        await message.reply_text(f"⚠️ {e}")
        return
    res = await db.tasks.update_one(
        {"_id": tid, "status": {"$in": ["queued", "paused"]}}, {"$set": {"scheduled_at": scheduled_at}}
    )
    if not res.modified_count:
        await message.reply_text("ℹ️ Task not found, or it's already running/finished.")
        return
    if task_manager:
        task_manager.wake()
    await message.reply_text(f"⏰ Task #{tid} rescheduled for {scheduling.human(scheduled_at)}.")


# ── Live queue view: pause / resume / reorder / cancel ─────────────────

def _status_icon(t: dict, now_iso: str) -> str:
    if t["status"] == "paused":
        return "⏸"
    if t["status"] == "running":
        return "▶️"
    if t.get("scheduled_at") and t["scheduled_at"] > now_iso:
        return "🕒"
    return "🕓"


async def _queue_view(limit: int = 25):
    from bot.database import db
    docs = await db.tasks.find({"status": {"$in": list(ACTIVE_STATUSES)}}) \
        .sort([("priority", 1), ("_id", 1)]).to_list(length=limit)
    if not docs:
        return "📭 <b>Queue is empty.</b>\n\nUse /addtask to add something, or /track to auto-post new episodes.", None

    now_iso = datetime.now(timezone.utc).isoformat()
    lines = ["📋 <b>Active queue</b>\n"]
    rows = []
    for i, t in enumerate(docs, 1):
        icon = _status_icon(t, now_iso)
        sched = f" · ⏰ {scheduling.human(t['scheduled_at'])}" if t.get("scheduled_at") else ""
        auto = " 🔁" if t.get("auto") else ""
        lines.append(
            f"{i}. {icon} <b>{esc(t.get('title', '?'))}</b>{auto} — "
            f"{t.get('done', 0)}/{t.get('total') or '?'}{sched}"
        )
        row = []
        if t["status"] == "paused":
            row.append(InlineKeyboardButton("▶️", callback_data=f"tq:resume:{t['_id']}"))
        else:
            row.append(InlineKeyboardButton("⏸", callback_data=f"tq:pause:{t['_id']}"))
        if i > 1:
            row.append(InlineKeyboardButton("🔼", callback_data=f"tq:up:{t['_id']}"))
        if i < len(docs):
            row.append(InlineKeyboardButton("🔽", callback_data=f"tq:down:{t['_id']}"))
        row.append(InlineKeyboardButton(f"✖ #{t['_id']}", callback_data=f"tq:cancel:{t['_id']}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="tq:refresh:0")])
    lines.append("\n<i>Buttons apply to the task on that line.</i>")
    return "\n".join(lines), InlineKeyboardMarkup(rows)


@require_owner
async def cmd_queue(client: Client, message: Message):
    text, markup = await _queue_view()
    await message.reply_text(text, parse_mode=HTML, reply_markup=markup)


async def _reorder(tid: int, direction: str):
    from bot.database import db
    docs = await db.tasks.find({"status": {"$in": list(ACTIVE_STATUSES)}}) \
        .sort([("priority", 1), ("_id", 1)]).to_list(length=200)
    ids = [d["_id"] for d in docs]
    if tid not in ids:
        return
    idx = ids.index(tid)
    swap_idx = idx - 1 if direction == "up" else idx + 1
    if swap_idx < 0 or swap_idx >= len(ids):
        return
    a, b = docs[idx], docs[swap_idx]
    pa, pb = a.get("priority", a["_id"]), b.get("priority", b["_id"])
    await db.tasks.update_one({"_id": a["_id"]}, {"$set": {"priority": pb}})
    await db.tasks.update_one({"_id": b["_id"]}, {"$set": {"priority": pa}})


async def queue_callback(client: Client, q: CallbackQuery):
    from bot.database import db
    from bot.taskqueue import task_manager
    if not is_owner(q.from_user.id):
        await q.answer("Owner only", show_alert=True)
        return
    _, action, tid = q.data.split(":")
    tid = int(tid)

    if action == "pause":
        await db.tasks.update_one({"_id": tid, "status": {"$in": ["queued", "running"]}}, {"$set": {"status": "paused"}})
    elif action == "resume":
        await db.tasks.update_one({"_id": tid, "status": "paused"}, {"$set": {"status": "queued"}})
        if task_manager:
            task_manager.wake()
    elif action == "cancel":
        await db.tasks.update_one({"_id": tid, "status": {"$in": list(ACTIVE_STATUSES)}}, {"$set": {"status": "cancelled"}})
        if task_manager:
            task_manager.wake()
    elif action in ("up", "down"):
        await _reorder(tid, action)
    # "refresh" falls through to just re-rendering below

    text, markup = await _queue_view()
    try:
        await q.message.edit_text(text, parse_mode=HTML, reply_markup=markup)
    except Exception:
        pass
    await q.answer()


# ── Template picker ───────────────────────────────────────────────────

def _tpl_keyboard(current: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for name in list(templates.TEMPLATES) + [templates.RANDOM]:
        label = "🎲 Random" if name == templates.RANDOM else templates.TEMPLATES[name][0]
        row.append(InlineKeyboardButton(("✅ " if name == current else "") + label, callback_data=f"tpl:{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _tpl_text(name: str, audio: str) -> str:
    if name == templates.RANDOM:
        return "🎲 <b>Random</b> — each new notification picks a design at random.\n\nTap a design to preview it."
    sample = replace(templates.SAMPLE, audio=audio or "Hindi")
    return (
        f"🎨 <b>Episode notice design: {templates.TEMPLATES[name][0]}</b>\n\n"
        f"{templates.render(name, sample)}"
    )


@require_owner
async def cmd_settemplate(client: Client, message: Message):
    from bot.database import db
    current = await db.get_config("notify_template", settings.bot.notify_template)
    if current not in templates.TEMPLATES and current != templates.RANDOM:
        current = templates.DEFAULT_TEMPLATE
    audio = await db.get_config("audio_label", settings.bot.audio_label)
    await message.reply_text(_tpl_text(current, audio), parse_mode=HTML, reply_markup=_tpl_keyboard(current))


async def tpl_callback(client: Client, q: CallbackQuery):
    from bot.database import db
    if not is_owner(q.from_user.id):
        await q.answer("Owner only", show_alert=True)
        return
    _, name = q.data.split(":", 1)
    if name != templates.RANDOM and name not in templates.TEMPLATES:
        await q.answer("Unknown template", show_alert=True)
        return

    await db.set_config("notify_template", name)
    audio = await db.get_config("audio_label", settings.bot.audio_label)
    try:
        await q.message.edit_text(_tpl_text(name, audio), parse_mode=HTML, reply_markup=_tpl_keyboard(name))
    except Exception:
        pass
    await q.answer("Saved — new episode notices use this design")
