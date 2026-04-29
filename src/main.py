import logging
from datetime import time
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from src.config import BOT_TOKEN
from src.database.connection import init_db
from src.services.backup_service import run_auto_backup_job
from src.services.expiry_service import monitor_expired_keys
from src.services.notifier import monitor_used_up_keys

# Import our handlers
from src.handlers import owner, lists, wizards, customers

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "🛡️ *Outline Server Manager Bot Guide*\n\n"
    "*Who can use what*\n"
    "- *Owner only:* `/addadmin`, `/removeadmin`, `/listadmin`, `/addserver`, `/listserver`, `/deleteserver`, `/setkeylimit`\n"
    "- *Admins + Owner:* `/keys`, `/newkey`, `/manage`, `/noti`, `/scan`, `/backup`, `/autobackup`, `/users`, `/approve`, `/reject`\n"
    "- *Everyone:* `/start`, `/help`, `/id`, `/register`, `/mykeys`\n\n"
    "*Quick start*\n"
    "1. Owner adds a server with `/addserver <alias> <api_url> <cert_sha256>`\n"
    "2. Owner sets capacity with `/setkeylimit <alias> <max_keys>` (0 = unlimited)\n"
    "3. Admin uses `/newkey` to create keys interactively\n"
    "4. Admin uses `/keys` to inspect keys and statuses\n"
    "5. Admin uses `/manage <alias> <key_id>` to view key URL, mark sold/unsold, or delete\n\n"
    "*Status tags in `/keys`*\n"
    "- `🟢 [AVAILABLE]` key has remaining quota\n"
    "- `🟠 [USED UP]` key reached data limit\n"
    "- `🔴 [SOLD]` key is marked sold in bot metadata\n\n"
    "*Commands*\n"
    "- `/start` Show welcome message\n"
    "- `/help` Show this guide\n"
    "- `/id` Show your Telegram user id\n"
    "- `/keys` Show servers as inline buttons for key management\n"
    "- `/newkey` Start interactive key creation wizard\n"
    "- `/manage <server_alias> <key_id>` Open key actions (View URL, Set Expiry, Renew, Mark Sold, Delete)\n"
    "- `/cancel` Cancel active wizard\n"
    "- `/addadmin <user_id>` Add admin (owner only)\n"
    "- `/removeadmin <user_id>` Remove admin (owner only)\n"
    "- `/listadmin` List admins (owner only)\n"
    "- `/addserver <alias> <api_url> <cert_sha256>` Add Outline server (owner only)\n"
    "- `/listserver` List configured server aliases (owner only)\n"
    "- `/deleteserver <alias>` Delete server (owner only)\n"
    "- `/setkeylimit <alias> <max_keys>` Set server key limit (owner only)\n"
    "- `/noti <on|off>` Toggle your own used-up key alerts (admin/owner)\n"
    "- `/scan` Run immediate used-up scan and alert delivery (admin/owner)\n"
    "- `/backup` Generate and send latest manual backup file (admin/owner)\n"
    "- `/autobackup` Send latest daily auto backup file (admin/owner)\n\n"
    "- `/users` Show user registration overview (admin/owner)\n"
    "- `/approve <user_id>` Approve registered user (admin/owner)\n"
    "- `/reject <user_id>` Reject user (admin/owner)\n"
    "- `/register` Submit your user registration request\n"
    "- `/mykeys` Show keys assigned to your account\n\n"
    "*Examples*\n"
    "- `/addadmin 123456789`\n"
    "- `/addserver vps1 https://1.2.3.4:12345/abcd E1F2A3...`\n"
    "- `/setkeylimit vps1 50`\n"
    "- `/noti on`\n"
    "- `/scan`\n"
    "- `/backup`\n"
    "- `/autobackup`\n"
    "- `/manage vps1 7`"
)

async def post_init(application):
    """Register periodic background jobs after application startup."""
    if application.job_queue:
        application.job_queue.run_repeating(
            monitor_used_up_keys,
            interval=300,
            first=45,
            name="used-up-key-notifier",
        )
        application.job_queue.run_repeating(
            monitor_expired_keys,
            interval=300,
            first=75,
            name="expiry-auto-disable",
        )
        application.job_queue.run_daily(
            run_auto_backup_job,
            time=time(hour=0, minute=0),
            name="daily-auto-backup",
        )
    else:
        logger.warning("Job queue is unavailable; used-up key notifications are disabled.")

async def start_command(update: Update, context):
    """The /start command."""
    await update.message.reply_text(
        (
            "🛡️ *Outline Server Manager*\n\n"
            "If you are a user/customer, start with `/register` and wait for Owner/Admin approval.\n"
            "After approval, use `/mykeys` to view your assigned keys.\n\n"
            "Use `/help` to view the full command guide."
        ),
        parse_mode='Markdown'
    )

async def id_command(update: Update, context):
    """Public command to show the caller's Telegram user id."""
    user = update.effective_user
    if not user:
        await update.message.reply_text("❌ Could not determine your user id.")
        return
    await update.message.reply_text(f"🆔 Your Telegram user id is: `{user.id}`", parse_mode='Markdown')

async def help_command(update: Update, context):
    """Detailed usage guide for all supported commands."""
    await update.message.reply_text(HELP_TEXT, parse_mode='Markdown')

async def global_error_handler(update: object, context):
    """Catch all uncaught handler errors so the bot does not fail silently."""
    logger.exception("Unhandled exception while processing update", exc_info=context.error)

    if isinstance(update, Update):
        if update.effective_message:
            await update.effective_message.reply_text("❌ Unexpected error occurred. Please try again.")
        elif update.callback_query:
            await update.callback_query.answer("❌ Unexpected error occurred.", show_alert=True)

def main():
    # 1. Initialize Database
    init_db()
    
    # 2. Build the Application
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # 3. Register Basic Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", id_command))
    
    # 4. Register Owner Commands
    app.add_handler(CommandHandler("addadmin", owner.add_admin))
    app.add_handler(CommandHandler("removeadmin", owner.remove_admin))
    app.add_handler(CommandHandler("listadmin", owner.list_admin))
    app.add_handler(CommandHandler("addserver", owner.add_server))
    app.add_handler(CommandHandler("listserver", owner.list_server))
    app.add_handler(CommandHandler("deleteserver", owner.delete_server))
    app.add_handler(CommandHandler("setkeylimit", owner.set_key_limit))
    app.add_handler(CommandHandler("noti", owner.set_notifications))
    app.add_handler(CommandHandler("scan", owner.scan_used_up_keys))
    app.add_handler(CommandHandler("backup", owner.backup_now))
    app.add_handler(CommandHandler("autobackup", owner.get_last_auto_backup))
    app.add_handler(CommandHandler("users", customers.users_overview))
    app.add_handler(CommandHandler("approve", customers.approve_user))
    app.add_handler(CommandHandler("reject", customers.reject_user))
    app.add_handler(CommandHandler("register", customers.register))
    app.add_handler(CommandHandler("mykeys", customers.mykeys))
    
    # 5. Register List & Manage Commands
    app.add_handler(CommandHandler("keys", lists.list_servers))
    app.add_handler(CommandHandler("manage", lists.manage_key_command))
    
    # 6. Register Callbacks (Inline Buttons)
    app.add_handler(CallbackQueryHandler(lists.handle_listkeys_callback, pattern="^listkeys_"))
    app.add_handler(CallbackQueryHandler(lists.handle_key_actions_callback, pattern="^(view|toggle|delete|delyes|delno|expiry|expd30|expd90|expd180|expd360|expclr|expcancel|renew|rnd30|rnd90|rnd180|rnd360|rncancel|assign|unassign)_"))
    app.add_handler(CallbackQueryHandler(wizards.handle_post_create_sold_callback, pattern="^postsold_(yes|no)_"))
    
    # 7. Register Wizards
    app.add_handler(wizards.newkey_conv_handler)

    # 7.1 Register manual sold-key delete text confirmations
    app.add_handler(MessageHandler(filters.Regex(r"(?i)^(delete|cancel)$"), lists.handle_manual_sold_delete_confirmation))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lists.handle_manual_renew_quota_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lists.handle_manual_assign_user_input))

    # 8. Register Global Error Handler
    app.add_error_handler(global_error_handler)

    # 9. Start Polling
    logger.info("Bot is starting...")
    app.run_polling()

if __name__ == '__main__':
    main()