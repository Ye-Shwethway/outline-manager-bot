import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler
from src.config import BOT_TOKEN
from src.database.connection import init_db

# Import our handlers
from src.handlers import owner, lists, wizards

logger = logging.getLogger(__name__)

async def start_command(update: Update, context):
    """The /start command."""
    help_text = (
        "🛡️ *Outline Server Manager*\n\n"
        "*Admin Commands:*\n"
        "`/servers` - Interactive server list\n"
        "`/newkey` - Interactive key creator\n"
        "`/manage <server> <key_id>` - Edit/Delete a specific key\n\n"
        "*Owner Commands:*\n"
        "`/addadmin` | `/removeadmin` | `/listadmin`\n"
        "`/addserver` | `/deleteserver` | `/setkeylimit`"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

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
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # 3. Register Basic Commands
    app.add_handler(CommandHandler("start", start_command))
    
    # 4. Register Owner Commands
    app.add_handler(CommandHandler("addadmin", owner.add_admin))
    app.add_handler(CommandHandler("removeadmin", owner.remove_admin))
    app.add_handler(CommandHandler("listadmin", owner.list_admin))
    app.add_handler(CommandHandler("addserver", owner.add_server))
    app.add_handler(CommandHandler("deleteserver", owner.delete_server))
    app.add_handler(CommandHandler("setkeylimit", owner.set_key_limit))
    
    # 5. Register List & Manage Commands
    app.add_handler(CommandHandler("servers", lists.list_servers))
    app.add_handler(CommandHandler("manage", lists.manage_key_command))
    
    # 6. Register Callbacks (Inline Buttons)
    app.add_handler(CallbackQueryHandler(lists.handle_listkeys_callback, pattern="^listkeys_"))
    app.add_handler(CallbackQueryHandler(lists.handle_key_actions_callback, pattern="^(toggle|delete)_"))
    
    # 7. Register Wizards
    app.add_handler(wizards.newkey_conv_handler)

    # 8. Register Global Error Handler
    app.add_error_handler(global_error_handler)

    # 9. Start Polling
    logger.info("Bot is starting...")
    app.run_polling()

if __name__ == '__main__':
    main()