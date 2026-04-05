import logging
from telegram import Update
from telegram.ext import ContextTypes
from src.utils.decorators import admin_only
from src.database import queries
from src.services.outline_api import get_vpn_client
from src.utils.keyboards import get_server_list_keyboard, get_key_management_keyboard

logger = logging.getLogger(__name__)

@admin_only
async def list_servers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /servers"""
    servers = queries.get_servers()
    if not servers:
        await update.message.reply_text("No servers configured yet. Owner needs to use /addserver.")
        return
    
    keyboard = get_server_list_keyboard(servers, prefix="listkeys")
    await update.message.reply_text("🌐 *Select a server to view its keys:*", reply_markup=keyboard, parse_mode='Markdown')

@admin_only
async def handle_listkeys_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles clicks from the /servers inline keyboard."""
    query = update.callback_query
    await query.answer()
    
    # Extract alias from callback data (e.g., "listkeys_vps1")
    alias = query.data.split("_", 1)[1]
    client = get_vpn_client(alias)
    
    if not client:
        await query.edit_message_text(f"❌ Could not connect to server `{alias}`.", parse_mode='Markdown')
        return

    try:
        keys = client.get_keys()
        sold_keys = queries.get_sold_keys(alias)
        server_data = queries.get_server(alias)
        
        limit_text = f" (Max: {server_data['max_key_count']})" if server_data['max_key_count'] > 0 else " (No Limit)"
        msg = f"🔑 *Keys for {alias}* {limit_text}:\n\n"
        
        if not keys:
            msg += "No keys found on this server."
            await query.edit_message_text(msg, parse_mode='Markdown')
            return

        for key in keys:
            used_mb = key.used_bytes / (1024 * 1024) if key.used_bytes else 0
            limit_gb = key.data_limit / (1024 * 1024 * 1024) if key.data_limit else "No Limit"
            limit_str = f"{limit_gb:.2f} GB" if isinstance(limit_gb, float) else limit_gb
            sold_tag = "🔴 [SOLD]" if key.key_id in sold_keys else "🟢 [AVAILABLE]"
            
            msg += f"ID: `{key.key_id}` | Name: *{key.name or 'Unnamed'}* {sold_tag}\n"
            msg += f"Usage: {used_mb:.2f} MB / {limit_str}\n"
            msg += f"Manage: `/manage {alias} {key.key_id}`\n\n"
            
        await query.edit_message_text(msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Error listing keys: {e}")
        await query.edit_message_text("❌ Error communicating with the Outline server.")

@admin_only
async def manage_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /manage <alias> <key_id> - Opens the inline keyboard for a specific key."""
    if len(context.args) != 2:
        await update.message.reply_text("Usage: `/manage <server_alias> <key_id>`", parse_mode='Markdown')
        return
        
    alias, key_id = context.args
    sold_keys = queries.get_sold_keys(alias)
    is_sold = key_id in sold_keys
    
    keyboard = get_key_management_keyboard(alias, key_id, is_sold)
    await update.message.reply_text(f"⚙️ *Managing Key {key_id} on {alias}*", reply_markup=keyboard, parse_mode='Markdown')

@admin_only
async def handle_key_actions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Mark Sold / Delete actions from the manage key keyboard."""
    query = update.callback_query
    data = query.data.split("_")
    action = data[0]
    alias = data[1]
    key_id = data[2]
    
    if action == "toggle":
        new_status = queries.toggle_key_sold(alias, key_id)
        status_text = "Sold" if new_status else "Available"
        await query.answer(f"Key marked as {status_text}!")
        
        # Refresh the keyboard to show the opposite toggle button
        keyboard = get_key_management_keyboard(alias, key_id, new_status)
        await query.edit_message_reply_markup(reply_markup=keyboard)

    elif action == "delete":
        client = get_vpn_client(alias)
        if client:
            try:
                client.delete_key(key_id)
                queries.remove_key_metadata(alias, key_id)
                await query.answer("Key deleted successfully!", show_alert=True)
                await query.edit_message_text(f"🗑️ Key `{key_id}` was deleted from `{alias}`.", parse_mode='Markdown')
            except Exception as e:
                await query.answer("Failed to delete key.", show_alert=True)
                logger.error(f"Delete error: {e}")