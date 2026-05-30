import logging
from datetime import time
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ApplicationHandlerStop
from src.config import BOT_TOKEN, OWNER_ID
from src.database.connection import init_db
from src.database import queries
from src.services.backup_service import run_auto_backup_job
from src.services.expiry_service import monitor_expired_keys
from src.services.notifier import monitor_used_up_keys

# Import our handlers
from src.handlers import owner, lists, wizards, customers

logger = logging.getLogger(__name__)

OWNER_ONLY_COMMANDS = {
    "addadmin",
    "removeadmin",
    "listadmin",
    "addserver",
    "listserver",
    "deleteserver",
    "keyusage",
    "keyaccounting",
    "useraccounting",
    "loyalty",
    "setkeylimit",
    "restart",
    "reviewnoti",
}

ADMIN_OWNER_COMMANDS = {
    "keys",
    "search",
    "newkey",
    "manage",
    "renew",
    "cancel",
    "noti",
    "scan",
    "backup",
    "autobackup",
    "users",
    "approve",
    "reject",
    "removeuser",
}

PRIVILEGED_HELP_TEXT = (
    "🛡️ *Outline Server Manager Bot Guide*\n\n"
    "*Who can use what*\n"
    "- *Owner only:* `/addadmin`, `/removeadmin`, `/listadmin`, `/addserver`, `/listserver`, `/deleteserver`, `/keyusage`, `/keyaccounting`, `/useraccounting`, `/loyalty`, `/setkeylimit`, `/restart`, `/reviewnoti`\n"
    "- *Admins + Owner:* `/keys`, `/search`, `/newkey`, `/manage`, `/renew`, `/cancel`, `/noti`, `/restart`, `/scan`, `/backup`, `/autobackup`, `/users`, `/approve`, `/reject`, `/removeuser`\n"
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
    "- `/search <owner_user_id|owner_username|key_name>` Search owners by id/username or assigned key name across servers\n"
    "- `/newkey` Start interactive key creation wizard\n"
    "- `/manage` Open approved-user management flow (assign/unassign keys)\n"
    "- `/manage <server_alias> <key_id>` Open key actions (View URL, Set Expiry Only, Renew, Mark Sold, Delete)\n"
    "- `/renew` Open renew flow (choose server/key, then manual quota with optional expiry update)\n"
    "- `/cancel` Cancel active wizard\n"
    "- `/addadmin <user_id>` Add admin (owner only)\n"
    "- `/removeadmin <user_id>` Remove admin (owner only)\n"
    "- `/listadmin` List admins (owner only)\n"
    "- `/addserver <alias> <api_url> <cert_sha256>` Add Outline server (owner only)\n"
    "- `/listserver` List configured server aliases (owner only)\n"
    "- `/deleteserver <alias>` Delete server (owner only)\n"
    "- `/keyusage` Open inline server/key picker for raw Outline usage vs tracked effective usage (owner only)\n"
    "- `/keyaccounting` Open inline server/key picker for lifetime accounting totals and recent accounting events (owner only)\n"
    "- `/useraccounting` Open inline user picker for customer lifetime accounting totals and recent events (owner only)\n"
    "- `/loyalty` Open inline customer loyalty leaderboard (top buyers, consumers, renewers) (owner only)\n"
    "- `/setkeylimit <alias> <max_keys>` Set server key limit (owner only)\n"
    "- `/noti <on|off>` Toggle your own used-up key alerts (admin/owner)\n"
    "- `/restart` Restart bot process (owner only, data preserved)\n"
    "- `/reviewnoti <on|off>` Toggle whether admins receive new registration-review alerts (owner only)\n"
    "- `/scan` Run immediate used-up scan and alert delivery (admin/owner)\n"
    "- `/backup` Generate and send latest manual backup file (admin/owner)\n"
    "- `/autobackup` Send latest daily auto backup file (admin/owner)\n\n"
    "- `/users` Show user registration overview (admin/owner), including approved-user ban flow\n"
    "- `/approve <user_id>` Approve registered user (admin/owner)\n"
    "- `/reject <user_id>` Reject user (admin/owner)\n"
    "- `/removeuser <user_id>` Remove user from registry and unlink assigned keys, or unban from rejected list (admin/owner)\n"
    "- `/register` Submit your user registration request\n"
    "- `/mykeys` Show keys assigned to your account\n\n"
    "*Examples*\n"
    "- `/addadmin 123456789`\n"
    "- `/addserver vps1 https://1.2.3.4:12345/abcd E1F2A3...`\n"
    "- `/keyusage`\n"
    "- `/keyaccounting`\n"
    "- `/useraccounting`\n"
    "- `/loyalty`\n"
    "- `/keyusage vps1 7`\n"
    "- `/setkeylimit vps1 50`\n"
    "- `/noti on`\n"
    "- `/restart`\n"
    "- `/scan`\n"
    "- `/search drthorne`\n"
    "- `/search 1802096079`\n"
    "- `/reviewnoti off`\n"
    "- `/reviewnoti on`\n"
    "- `/backup`\n"
    "- `/autobackup`\n"
    "- `/manage vps1 7`\n"
    "- `/renew`"
)

USER_HELP_TEXT = (
    "🧭 *Outline User Guide*\n\n"
    "*Available commands for normal users*\n"
    "- `/start` Show welcome message\n"
    "- `/help` Show this guide\n"
    "- `/id` Show your Telegram user id\n"
    "- `/register` Submit your registration request\n"
    "- `/mykeys` Show your assigned keys\n\n"
    "*How to use*\n"
    "1. Run `/register` once\n"
    "2. Wait for Owner/Admin approval\n"
    "3. Run `/mykeys` to view your active key URLs and usage\n\n"
    "Owner/Admin commands are restricted and cannot be used from normal user accounts."
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

    try:
        await application.bot.send_message(chat_id=OWNER_ID, text="The Bot is Online")
    except Exception as e:
        logger.warning(f"Failed to send startup online message to owner {OWNER_ID}: {e}")

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
    user = update.effective_user
    if user and (user.id == OWNER_ID or user.id in queries.get_admins()):
        await update.message.reply_text(PRIVILEGED_HELP_TEXT, parse_mode='Markdown')
        return

    await update.message.reply_text(USER_HELP_TEXT, parse_mode='Markdown')


async def forbidden_command_guard(update: Update, context):
    """Blocks privileged commands for non-admin accounts with a single clear response."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message or not message.text:
        return

    command = message.text.split()[0].lstrip('/').split('@')[0].lower()
    is_owner = user.id == OWNER_ID
    is_admin = user.id in queries.get_admins()

    if command in OWNER_ONLY_COMMANDS and not is_owner:
        await message.reply_text(
            "❌ This command is restricted to the bot Owner.\n"
            "Allowed user commands: `/help`, `/register`, `/mykeys`, `/id`.",
            parse_mode='Markdown',
        )
        raise ApplicationHandlerStop

    if command in ADMIN_OWNER_COMMANDS and not (is_owner or is_admin):
        await message.reply_text(
            "❌ You do not have permission to use this command.\n"
            "Allowed user commands: `/help`, `/register`, `/mykeys`, `/id`.",
            parse_mode='Markdown',
        )
        raise ApplicationHandlerStop

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
    try:
        backfill_result = queries.run_accounting_backfill()
        if backfill_result.get("ran"):
            logger.info(
                "Accounting backfill %s completed. Keys backfilled: %s",
                backfill_result.get("version"),
                backfill_result.get("backfilled_keys"),
            )
    except Exception as e:
        logger.error(f"Accounting backfill failed during startup: {e}")
    
    # 2. Build the Application
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # 3. Register Basic Commands
    app.add_handler(
        CommandHandler(
            list(OWNER_ONLY_COMMANDS | ADMIN_OWNER_COMMANDS),
            forbidden_command_guard,
            block=False,
        ),
        group=-1,
    )
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
    app.add_handler(CommandHandler("keyusage", owner.key_usage_diagnostic))
    app.add_handler(CommandHandler("keyaccounting", owner.key_accounting_diagnostic))
    app.add_handler(CommandHandler("useraccounting", owner.user_accounting_diagnostic))
    app.add_handler(CommandHandler("loyalty", owner.loyalty_leaderboard))
    app.add_handler(CommandHandler("setkeylimit", owner.set_key_limit))
    app.add_handler(CommandHandler("noti", owner.set_notifications))
    app.add_handler(CommandHandler("restart", owner.restart_bot))
    app.add_handler(CommandHandler("reviewnoti", owner.set_review_notifications))
    app.add_handler(CommandHandler("scan", owner.scan_used_up_keys))
    app.add_handler(CommandHandler("backup", owner.backup_now))
    app.add_handler(CommandHandler("autobackup", owner.get_last_auto_backup))
    app.add_handler(CommandHandler("users", customers.users_overview))
    app.add_handler(CommandHandler("search", customers.search_user))
    app.add_handler(CommandHandler("renew", lists.renew_key_command))
    app.add_handler(CommandHandler("approve", customers.approve_user))
    app.add_handler(CommandHandler("reject", customers.reject_user))
    app.add_handler(CommandHandler("removeuser", customers.remove_user))
    app.add_handler(CommandHandler("register", customers.register))
    app.add_handler(CommandHandler("mykeys", customers.mykeys))
    
    # 5. Register List & Manage Commands
    app.add_handler(CommandHandler("keys", lists.list_servers))
    app.add_handler(CommandHandler("manage", lists.manage_key_command))
    
    # 6. Register Callbacks (Inline Buttons)
    app.add_handler(CallbackQueryHandler(lists.handle_listkeys_callback, pattern="^listkeys_"))
    app.add_handler(CallbackQueryHandler(lists.handle_user_manage_callback, pattern=r"^umgr\|"))
    app.add_handler(CallbackQueryHandler(lists.handle_key_actions_callback, pattern="^(view|toggle|delete|delyes|delno|expiry|expd30|expd90|expd180|expd360|expclr|expcancel|renew|rnd30|rnd90|rnd180|rnd360|rncancel|assign|unassign|close)_"))
    app.add_handler(CallbackQueryHandler(wizards.handle_post_create_sold_callback, pattern="^postkey_(open|back|close)_"))
    app.add_handler(CallbackQueryHandler(customers.handle_registration_review_callback, pattern="^ureg_(a|r)_"))
    app.add_handler(CallbackQueryHandler(customers.handle_users_admin_callback, pattern="^uadm_"))
    app.add_handler(CallbackQueryHandler(owner.handle_key_usage_callback, pattern=r"^kdiag\|"))
    app.add_handler(CallbackQueryHandler(owner.handle_key_accounting_callback, pattern=r"^kacct\|"))
    app.add_handler(CallbackQueryHandler(owner.handle_user_accounting_callback, pattern=r"^uacct\|"))
    app.add_handler(CallbackQueryHandler(owner.handle_loyalty_callback, pattern=r"^loyal\|"))
    app.add_handler(CallbackQueryHandler(lists.handle_renew_workflow_callback, pattern=r"^rflow\|"))
    
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