from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler, CallbackQueryHandler

from .commands import cmd_start, cmd_help, cmd_search
from .callbacks import callback_router
from .messages import handle_text
from .admin import (
    cmd_adduser, cmd_removeuser, cmd_users,
    cmd_setchannellink,
    cmd_delete, delete_callback,
    cmd_addbot, cmd_delbot, cmd_bots, cmd_setbotquality, cmd_refreshalbums,
)
from .admin_ai import cmd_setai, cmd_ai
from .tasks import (
    cmd_addtask, cmd_tasks, cmd_canceltask, cmd_reschedule, cmd_queue, cmd_settemplate,
    tkpick_callback, tpl_callback, queue_callback,
)
from .tracking import cmd_track, cmd_tracked, trkpick_callback, trkun_callback
from .settings_panel import cmd_settings, settings_callback, awaiting_input, handle_settings_input

__all__ = [
    "cmd_start", "cmd_help", "cmd_search", "callback_router", "handle_text",
    "cmd_adduser", "cmd_removeuser", "cmd_users",
    "cmd_setchannellink",
    "cmd_delete",
    "cmd_setai", "cmd_ai",
    "cmd_addbot", "cmd_delbot", "cmd_bots", "cmd_setbotquality", "cmd_refreshalbums",
    "cmd_addtask", "cmd_tasks", "cmd_canceltask", "cmd_reschedule", "cmd_queue", "cmd_settemplate",
    "cmd_track", "cmd_tracked",
    "cmd_settings",
    "register_handlers",
]


def register_handlers(app: Client):
    """Register all handlers on the Pyrogram Client."""
    # Commands
    app.add_handler(MessageHandler(cmd_start, filters.command("start") & filters.private))
    app.add_handler(MessageHandler(cmd_help, filters.command("help") & filters.private))
    app.add_handler(MessageHandler(cmd_search, filters.command("search") & filters.private))

    # Owner AI commands
    app.add_handler(MessageHandler(cmd_setai, filters.command("setai") & filters.private))
    app.add_handler(MessageHandler(cmd_ai, filters.command("ai") & filters.private))

    # Admin commands (owner-only, checked inside each handler)
    app.add_handler(MessageHandler(cmd_adduser, filters.command("adduser") & filters.private))
    app.add_handler(MessageHandler(cmd_removeuser, filters.command("removeuser") & filters.private))
    app.add_handler(MessageHandler(cmd_users, filters.command("users") & filters.private))
    app.add_handler(MessageHandler(cmd_setchannellink, filters.command("setchannellink") & filters.private))
    app.add_handler(MessageHandler(cmd_delete, filters.command("delete") & filters.private))
    app.add_handler(MessageHandler(cmd_addbot, filters.command("addbot") & filters.private))
    app.add_handler(MessageHandler(cmd_delbot, filters.command("delbot") & filters.private))
    app.add_handler(MessageHandler(cmd_bots, filters.command("bots") & filters.private))
    app.add_handler(MessageHandler(cmd_setbotquality, filters.command("setbotquality") & filters.private))
    app.add_handler(MessageHandler(cmd_refreshalbums, filters.command("refreshalbums") & filters.private))

    # Autopost tasks, scheduling, queue management + templates (owner-only)
    app.add_handler(MessageHandler(cmd_addtask, filters.command("addtask") & filters.private))
    app.add_handler(MessageHandler(cmd_tasks, filters.command("tasks") & filters.private))
    app.add_handler(MessageHandler(cmd_queue, filters.command("queue") & filters.private))
    app.add_handler(MessageHandler(cmd_canceltask, filters.command("canceltask") & filters.private))
    app.add_handler(MessageHandler(cmd_reschedule, filters.command("reschedule") & filters.private))
    app.add_handler(MessageHandler(cmd_settemplate, filters.command("settemplate") & filters.private))
    app.add_handler(CallbackQueryHandler(tkpick_callback, filters.regex(r"^tkpick:")))
    app.add_handler(CallbackQueryHandler(tpl_callback, filters.regex(r"^tpl:")))
    app.add_handler(CallbackQueryHandler(queue_callback, filters.regex(r"^tq:")))

    # Auto-detect new episodes (owner-only)
    app.add_handler(MessageHandler(cmd_track, filters.command("track") & filters.private))
    app.add_handler(MessageHandler(cmd_tracked, filters.command("tracked") & filters.private))
    app.add_handler(CallbackQueryHandler(trkpick_callback, filters.regex(r"^trkpick:")))
    app.add_handler(CallbackQueryHandler(trkun_callback, filters.regex(r"^trkun:")))

    # Settings panel (owner-only) — the input interceptor must come before
    # the generic text handler below so it can capture the "next message"
    # when the owner is mid-edit of a setting.
    app.add_handler(MessageHandler(cmd_settings, filters.command("settings") & filters.private))
    app.add_handler(CallbackQueryHandler(settings_callback, filters.regex(r"^set:")))
    app.add_handler(MessageHandler(handle_settings_input, filters.private & filters.text & filters.create(awaiting_input)))

    # Delete callbacks (owner-only, before general router)
    app.add_handler(CallbackQueryHandler(delete_callback, filters.regex(r"^del:")))

    # Callback queries (inline buttons)
    app.add_handler(CallbackQueryHandler(callback_router))

    # Text messages (search) — must be last to avoid catching commands
    # Note: filters.regex matches non-command text (doesn't start with /)
    app.add_handler(MessageHandler(handle_text, filters.text & filters.private & filters.regex(r"^[^/]")))
