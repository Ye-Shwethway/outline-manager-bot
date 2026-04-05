import logging
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from src.utils.decorators import admin_only
from src.database import queries
from src.services.outline_api import get_vpn_client
from src.utils.keyboards import get_server_list_keyboard
from src.utils.inline_messages import (
    close_active_inline_message,
    set_active_inline_message,
    clear_if_matches,
)

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1_000_000_000

# Conversation States
SELECT_SERVER, ASK_NAME, ASK_LIMIT = range(3)

@admin_only
async def newkey_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    servers = queries.get_servers()
    if not servers:
        await update.message.reply_text("No servers available.")
        return ConversationHandler.END

    await close_active_inline_message(update, context)
    keyboard = get_server_list_keyboard(servers, prefix="newkey")
    sent = await update.message.reply_text("🪄 *New Key Wizard*\n\nSelect a server:", reply_markup=keyboard, parse_mode='Markdown')
    set_active_inline_message(context, sent.message_id)
    return SELECT_SERVER

async def newkey_server_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    alias = query.data.split("_", 1)[1]
    context.user_data['wizard_alias'] = alias
    
    # 1. Enforce Server Key Limits
    server_data = queries.get_server(alias)
    max_keys = server_data.get('max_key_count', 0)
    
    if max_keys > 0:
        client = get_vpn_client(alias)
        if not client:
            await query.edit_message_text("❌ Cannot reach server to verify limits.")
            if query.message:
                clear_if_matches(context, query.message.message_id)
            return ConversationHandler.END

        try:
            current_keys = client.get_keys()
            if len(current_keys) >= max_keys:
                await query.edit_message_text(f"⚠️ *Limit Reached!*\n\nServer `{alias}` is capped at {max_keys} keys.", parse_mode='Markdown')
                if query.message:
                    clear_if_matches(context, query.message.message_id)
                return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error validating server limits for {alias}: {e}")
            await query.edit_message_text("❌ Failed to verify server key limits.")
            if query.message:
                clear_if_matches(context, query.message.message_id)
            return ConversationHandler.END

    await query.edit_message_text(f"Server selected: `{alias}`\n\n📝 Please type a **Name** for this key:", parse_mode='Markdown')
    if query.message:
        clear_if_matches(context, query.message.message_id)
    return ASK_NAME

async def newkey_ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['wizard_name'] = update.message.text
    await update.message.reply_text("💾 Please type the **Data Limit in GB** (e.g., 50).\nType `0` for no limit.", parse_mode='Markdown')
    return ASK_LIMIT

async def newkey_ask_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        limit_gb = float(update.message.text)
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number. Try again.")
        return ASK_LIMIT

    alias = context.user_data['wizard_alias']
    key_name = context.user_data['wizard_name']
    client = get_vpn_client(alias)
    
    if not client:
        await update.message.reply_text("❌ Connection to server failed during creation.")
        return ConversationHandler.END

    try:
        # Create Key
        new_key = client.create_key()
        client.rename_key(new_key.key_id, key_name)
        
        # Apply Limit if > 0
        if limit_gb > 0:
            limit_bytes = int(limit_gb * BYTES_PER_GB)
            client.add_data_limit(new_key.key_id, limit_bytes)
            
        msg = (
            f"✅ *Key Created Successfully!*\n\n"
            f"Server: `{alias}`\n"
            f"Name: {key_name}\n"
            f"Limit: {limit_gb if limit_gb > 0 else 'Unlimited'} GB\n\n"
            f"*Access URL:* `{new_key.access_url}`"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error creating key: {e}")
        await update.message.reply_text("❌ An error occurred while creating the key.")

    # Clear memory
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Wizard cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# Build the Conversation Handler to be imported by main.py
newkey_conv_handler = ConversationHandler(
    entry_points=[CommandHandler('newkey', newkey_start)],
    states={
        SELECT_SERVER: [CallbackQueryHandler(newkey_server_selected, pattern="^newkey_")],
        ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, newkey_ask_name)],
        ASK_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, newkey_ask_limit)],
    },
    fallbacks=[CommandHandler('cancel', cancel_wizard)]
)