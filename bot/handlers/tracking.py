"""Owner commands: /track /tracked /untrack — manage auto-detect of new episodes."""

import logging
from datetime import datetime, timezone

from pyrogram import Client, enums
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from api.client import api
from bot import templates
from bot.auth import is_owner, require_owner
from bot.taskqueue import _QUALITY_RE, _TPL_RE
from utils.helpers import esc

log = logging.getLogger(__name__)
HTML = enums.ParseMode.HTML

TRACK_HELP = (
    "<b>🔁 Auto-detect new episodes</b>\n\n"
    "<code>/track &lt;name&gt; [quality] [tpl=name]</code>\n"
    "Saves the series with its current episode count as the baseline. "
    "When the site adds new episodes, they're downloaded and posted automatically.\n\n"
    "• <code>/tracked</code> — list tracked series, untrack from there.\n"
    "• Checks run every few minutes in the background."
)


def _parse_track_args(text: str) -> dict:
    tokens = text.split()[1:]
    out = {"query": "", "qualities": [], "template": None}
    words = []
    for tok in tokens:
        if _QUALITY_RE.match(tok):
            out["qualities"] = [("4K" if q.lower() == "4k" else q.lower()) for q in tok.split(",")]
        elif (m := _TPL_RE.match(tok)):
            out["template"] = m.group(1).lower()
        else:
            words.append(tok)
    out["query"] = " ".join(words).strip()
    out["qualities"] = out["qualities"] or ["720p"]
    return out


async def _start_tracking(message_or_query, chat_id: int, owner_id: int, slug: str, title: str,
                          qualities: list[str], template: str | None):
    from bot.database import db

    wait_msg = await message_or_query.reply_text(f"🔎 Checking current episode count for {esc(title)}...", parse_mode=HTML) \
        if hasattr(message_or_query, "reply_text") else None
    try:
        series = await api.get_series(slug)
        baseline = sum(len(s.episodes) for s in series.seasons.values())
    except Exception as e:
        text = f"⚠️ Couldn't read {esc(title)}: {esc(str(e)[:150])}"
        if wait_msg:
            await wait_msg.edit_text(text, parse_mode=HTML)
        return

    now = datetime.now(timezone.utc).isoformat()
    await db.tracked.update_one(
        {"slug": slug},
        {"$set": {
            "slug": slug, "title": title, "qualities": qualities, "template": template,
            "added_by": owner_id, "active": True, "last_seen_count": baseline,
            "last_checked": now, "added_at": now,
        }},
        upsert=True,
    )
    text = (
        f"✅ <b>Tracking {esc(title)}</b>\n"
        f"📺 Baseline: {baseline} episode(s) already known\n"
        f"🎞 Quality: {', '.join(qualities)}\n\n"
        f"New episodes will be downloaded and posted automatically."
    )
    if wait_msg:
        await wait_msg.edit_text(text, parse_mode=HTML)
    else:
        await message_or_query.reply_text(text, parse_mode=HTML)


@require_owner
async def cmd_track(client: Client, message: Message):
    args = _parse_track_args(message.text or "")
    if not args["query"]:
        await message.reply_text(TRACK_HELP, parse_mode=HTML)
        return
    if args["template"] and args["template"] != templates.RANDOM and args["template"] not in templates.TEMPLATES:
        await message.reply_text("⚠️ Unknown template. Use: " + ", ".join(templates.template_names()) + ", random")
        return

    wait = await message.reply_text("🔍 Searching...")
    try:
        results = await api.search(args["query"])
    except Exception as e:
        await wait.edit_text(f"⚠️ Search error: {esc(str(e)[:150])}")
        return
    series_only = [r for r in results if r.is_series][:5]
    if not series_only:
        await wait.edit_text("❌ No series found (movies don't get new episodes, so they can't be tracked).")
        return
    await wait.delete()

    if len(series_only) == 1:
        r = series_only[0]
        await _start_tracking(message, message.chat.id, message.from_user.id, r.slug, r.title,
                              args["qualities"], args["template"])
        return

    from bot.database import db
    doc = await db.db["track_drafts"].insert_one({
        "owner_id": message.from_user.id, "qualities": args["qualities"],
        "template": args["template"], "choices": [{"slug": r.slug, "title": r.title} for r in series_only],
    })
    draft_id = str(doc.inserted_id)
    buttons = [
        [InlineKeyboardButton(f"📺 {r.title[:45]}", callback_data=f"trkpick:{draft_id}:{i}")]
        for i, r in enumerate(series_only)
    ]
    buttons.append([InlineKeyboardButton("✖ Cancel", callback_data=f"trkpick:{draft_id}:x")])
    await message.reply_text("🎯 <b>Which one?</b>", parse_mode=HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def trkpick_callback(client: Client, q: CallbackQuery):
    from bot.database import db
    from bson import ObjectId
    if not is_owner(q.from_user.id):
        await q.answer("Owner only", show_alert=True)
        return
    _, draft_id, idx = q.data.split(":")
    draft = await db.db["track_drafts"].find_one({"_id": ObjectId(draft_id)})
    if not draft:
        await q.answer("Expired, run /track again.", show_alert=True)
        return
    await db.db["track_drafts"].delete_one({"_id": draft["_id"]})
    if idx == "x":
        await q.message.edit_text("✖ Cancelled.")
        return
    c = draft["choices"][int(idx)]
    await q.message.delete()
    await _start_tracking(q.message, q.message.chat.id, draft["owner_id"], c["slug"], c["title"],
                          draft["qualities"], draft.get("template"))
    await q.answer()


async def _tracked_view():
    from bot.database import db
    docs = await db.tracked.find({"active": True}).sort("added_at", -1).to_list(length=30)
    if not docs:
        return "📭 Nothing tracked yet. Use /track <name> to start.", None
    lines = ["🔁 <b>Tracked series</b>\n"]
    rows = []
    for i, t in enumerate(docs, 1):
        err = " ⚠️" if t.get("last_error") else ""
        lines.append(
            f"{i}. <b>{esc(t.get('title', '?'))}</b>{err} — {t.get('last_seen_count', 0)} ep. known "
            f"[{', '.join(t.get('qualities', []))}]"
        )
        rows.append([InlineKeyboardButton(f"✖ Untrack #{i}", callback_data=f"trkun:{t['_id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


@require_owner
async def cmd_tracked(client: Client, message: Message):
    text, markup = await _tracked_view()
    await message.reply_text(text, parse_mode=HTML, reply_markup=markup)


async def trkun_callback(client: Client, q: CallbackQuery):
    from bot.database import db
    from bson import ObjectId
    if not is_owner(q.from_user.id):
        await q.answer("Owner only", show_alert=True)
        return
    _, oid = q.data.split(":", 1)
    doc = await db.tracked.find_one_and_delete({"_id": ObjectId(oid)})
    await q.answer(f"Untracked {doc.get('title', '')}" if doc else "Already removed.")
    text, markup = await _tracked_view()
    try:
        await q.message.edit_text(text, parse_mode=HTML, reply_markup=markup)
    except Exception:
        pass
