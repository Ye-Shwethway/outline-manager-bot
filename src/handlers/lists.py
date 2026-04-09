import logging
from telegram import Update
from telegram.ext import ContextTypes
from src.utils.decorators import admin_only
from src.database import queries
from src.services.outline_api import get_vpn_client
from src.utils.keyboards import (
    get_server_list_keyboard,
    get_key_management_keyboard,
    get_delete_confirmation_keyboard,
)
from src.utils.inline_messages import (
    close_active_inline_message,
    set_active_inline_message,
    clear_if_matches,
)

logger = logging.getLogger(__name__)

BYTES_PER_MB = 1_000_000
BYTES_PER_GB = 1_000_000_000
PENDING_DELETE_KEY = "pending_sold_delete"

@admin_only
async def list_servers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /keys"""
    servers = queries.get_servers()
    if not servers:
        await update.message.reply_text("No servers configured yet. Owner needs to use /addserver.")
        return

    await close_active_inline_message(update, context)
    keyboard = get_server_list_keyboard(servers, prefix="listkeys")
    sent = await update.message.reply_text("🌐 *Select a server to view its keys:*", reply_markup=keyboard, parse_mode='Markdown')
    set_active_inline_message(context, sent.message_id)

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
        if query.message:
            clear_if_matches(context, query.message.message_id)
        return

    try:
        keys = client.get_keys()
        sold_keys = queries.get_sold_keys(alias)
        key_creators = queries.get_key_creators(alias)
        server_data = queries.get_server(alias)
        
        limit_text = f" (Max: {server_data['max_key_count']})" if server_data['max_key_count'] > 0 else " (No Limit)"
        msg = f"🔑 *Keys for {alias}* {limit_text}:\n\n"
        
        if not keys:
            msg += "No keys found on this server."
            await query.edit_message_text(msg, parse_mode='Markdown')
            if query.message:
                clear_if_matches(context, query.message.message_id)
            return

        for key in keys:
            used_gb = (key.used_bytes or 0) / BYTES_PER_GB
            limit_gb = key.data_limit / BYTES_PER_GB if key.data_limit else "No Limit"
            limit_str = f"{limit_gb:.2f} GB" if isinstance(limit_gb, float) else limit_gb
            is_used_up = bool(key.data_limit) and (key.used_bytes or 0) >= key.data_limit
            if is_used_up:
                status_tag = "🟠 [USED UP]"
            elif key.key_id in sold_keys:
                status_tag = "🔴 [SOLD]"
            else:
                status_tag = "🟢 [AVAILABLE]"

            key_id_str = str(key.key_id)
            creator_username = key_creators.get(key_id_str)
            creator_tag = f" | By: @{creator_username}" if creator_username else ""
            
            msg += f"ID: `{key.key_id}` | Name: *{key.name or 'Unnamed'}* {status_tag}{creator_tag}\n"
            if isinstance(limit_gb, float):
                msg += f"Usage: {used_gb:.2f} GB / {limit_gb:.2f} GB\n"
            else:
                msg += f"Usage: {used_gb:.2f} GB / Unlimited\n"
            msg += f"Manage: `/manage {alias} {key.key_id}`\n\n"
            
        await query.edit_message_text(msg, parse_mode='Markdown')
        if query.message:
            clear_if_matches(context, query.message.message_id)
    except Exception as e:
        logger.error(f"Error listing keys: {e}")
        await query.edit_message_text("❌ Error communicating with the Outline server.")
        if query.message:
            clear_if_matches(context, query.message.message_id)

@admin_only
async def manage_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /manage <alias> <key_id> - Opens the inline keyboard for a specific key."""
    if len(context.args) != 2:
        await update.message.reply_text("Usage: `/manage <server_alias> <key_id>`", parse_mode='Markdown')
        return
        
    alias, key_id = context.args
    client = get_vpn_client(alias)
    if not client:
        await update.message.reply_text(f"❌ Could not connect to server `{alias}`.", parse_mode='Markdown')
        return

    try:
        keys = client.get_keys()
    except Exception as e:
        logger.error(f"Manage key fetch error on {alias}: {e}")
        await update.message.reply_text("❌ Error communicating with the Outline server.")
        return

    target_key = next((key for key in keys if str(key.key_id) == str(key_id)), None)
    if not target_key:
        await update.message.reply_text(f"❌ Key `{key_id}` was not found on `{alias}`.", parse_mode='Markdown')
        return

    sold_keys = queries.get_sold_keys(alias)
    is_sold = str(key_id) in sold_keys
    used_gb = (target_key.used_bytes or 0) / BYTES_PER_GB
    if target_key.data_limit:
        limit_gb = target_key.data_limit / BYTES_PER_GB
        usage_line = f"{used_gb:.2f} GB / {limit_gb:.2f} GB"
    else:
        usage_line = f"{used_gb:.2f} GB / Unlimited"

    await close_active_inline_message(update, context)
    keyboard = get_key_management_keyboard(alias, key_id, is_sold)
    sent = await update.message.reply_text(
        (
            f"⚙️ *Manage Key*\n\n"
            f"Server: `{alias}`\n"
            f"Key ID: `{target_key.key_id}`\n"
            f"Name: *{target_key.name or 'Unnamed'}*\n"
            f"Usage: {usage_line}"
        ),
        reply_markup=keyboard,
        parse_mode='Markdown'
    )
    set_active_inline_message(context, sent.message_id)

@admin_only
async def handle_key_actions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Mark Sold / Delete actions from the manage key keyboard."""
    query = update.callback_query
    data = query.data.split("_", 2)
    action = data[0]
    alias = data[1]
    key_id = data[2]
    
    if action == "toggle":
        new_status = queries.toggle_key_sold(alias, key_id)
        status_text = "Sold" if new_status else "Available"
        await query.answer(f"Key marked as {status_text}!")

        # Auto-close manage action buttons after a terminal action.
        await query.edit_message_text(
            f"✅ Key `{key_id}` on `{alias}` marked as *{status_text}*.",
            parse_mode='Markdown'
        )
        if query.message:
            clear_if_matches(context, query.message.message_id)

    elif action == "view":
        client = get_vpn_client(alias)
        if not client:
            await query.answer("Could not connect to server.", show_alert=True)
            await query.edit_message_text(
                f"❌ View action failed for key `{key_id}` on `{alias}`.",
                parse_mode='Markdown'
            )
            if query.message:
                clear_if_matches(context, query.message.message_id)
            return

        try:
            keys = client.get_keys()
            target_key = next((key for key in keys if str(key.key_id) == str(key_id)), None)
            if not target_key:
                await query.answer("Key not found on server.", show_alert=True)
                await query.edit_message_text(
                    f"❌ Key `{key_id}` was not found on `{alias}`.",
                    parse_mode='Markdown'
                )
                if query.message:
                    clear_if_matches(context, query.message.message_id)
                return

            if not target_key.access_url:
                await query.answer("Access URL unavailable for this key.", show_alert=True)
                await query.edit_message_text(
                    f"❌ Access URL is unavailable for key `{key_id}` on `{alias}`.",
                    parse_mode='Markdown'
                )
                if query.message:
                    clear_if_matches(context, query.message.message_id)
                return

            await query.answer("Access URL sent.", show_alert=True)
            key_name = target_key.name or "Unnamed"
            key_creators = queries.get_key_creators(alias)
            creator_username = key_creators.get(str(target_key.key_id))
            creator_line = f"Generated By: *@{creator_username}*" if creator_username else "Generated By: *Unknown*"
            used_gb = (target_key.used_bytes or 0) / BYTES_PER_GB
            if target_key.data_limit:
                limit_gb = target_key.data_limit / BYTES_PER_GB
                available_bytes = max(target_key.data_limit - (target_key.used_bytes or 0), 0)
                available_gb = available_bytes / BYTES_PER_GB
                usage_line = f"Available Usage: *{available_gb:.2f} GB* (Used: {used_gb:.2f} GB / {limit_gb:.2f} GB)"
            else:
                usage_line = f"Available Usage: *Unlimited* (Used: {used_gb:.2f} GB / Unlimited)"

            await query.message.reply_text(
                f"🔑 *Key Access URL*\n\nServer: `{alias}`\nKey ID: `{key_id}`\nName: *{key_name}*\n{creator_line}\n{usage_line}\n\n`{target_key.access_url}`",
                parse_mode='Markdown'
            )
            await query.edit_message_text(
                f"✅ View action finished for key `{key_id}` on `{alias}`.",
                parse_mode='Markdown'
            )
            if query.message:
                clear_if_matches(context, query.message.message_id)
        except Exception as e:
            await query.answer("Failed to fetch key details.", show_alert=True)
            logger.error(f"View key error: {e}")
            await query.edit_message_text(
                f"❌ View action failed for key `{key_id}` on `{alias}`.",
                parse_mode='Markdown'
            )
            if query.message:
                clear_if_matches(context, query.message.message_id)

    elif action == "delete":
        client = get_vpn_client(alias)
        if not client:
            await query.answer("Could not connect to server.", show_alert=True)
            await query.edit_message_text(
                f"❌ Delete action failed for key `{key_id}` on `{alias}`.",
                parse_mode='Markdown'
            )
            if query.message:
                clear_if_matches(context, query.message.message_id)
            return

        try:
            keys = client.get_keys()
        except Exception as e:
            logger.error(f"Delete confirmation fetch error on {alias}: {e}")
            await query.answer("Failed to fetch key details.", show_alert=True)
            await query.edit_message_text(
                f"❌ Delete action failed for key `{key_id}` on `{alias}`.",
                parse_mode='Markdown'
            )
            if query.message:
                clear_if_matches(context, query.message.message_id)
            return

        target_key = next((key for key in keys if str(key.key_id) == str(key_id)), None)
        if not target_key:
            await query.answer("Key not found on server.", show_alert=True)
            await query.edit_message_text(
                f"❌ Key `{key_id}` was not found on `{alias}`.",
                parse_mode='Markdown'
            )
            if query.message:
                clear_if_matches(context, query.message.message_id)
            return

        used_gb = (target_key.used_bytes or 0) / BYTES_PER_GB
        if target_key.data_limit:
            limit_gb = target_key.data_limit / BYTES_PER_GB
            usage_line = f"{used_gb:.2f} GB / {limit_gb:.2f} GB"
            is_used_up = (target_key.used_bytes or 0) >= target_key.data_limit
        else:
            usage_line = f"{used_gb:.2f} GB / Unlimited"
            is_used_up = False

        sold_keys = queries.get_sold_keys(alias)
        if is_used_up:
            status_tag = "🟠 USED UP"
        elif str(key_id) in sold_keys:
            status_tag = "🔴 SOLD"
        else:
            status_tag = "🟢 AVAILABLE"

        keyboard = get_delete_confirmation_keyboard(alias, key_id)
        await query.answer("Confirm delete to proceed.", show_alert=True)
        await query.edit_message_text(
            (
                f"⚠️ *Delete Confirmation*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n"
                f"Name: *{target_key.name or 'Unnamed'}*\n"
                f"Usage: {usage_line}\n"
                f"Status: *{status_tag}*\n\n"
                "This action cannot be undone."
            ),
            reply_markup=keyboard,
            parse_mode='Markdown',
        )

    elif action == "delno":
        await query.answer("Delete cancelled.")
        await query.edit_message_text(
            f"✅ Delete cancelled for key `{key_id}` on `{alias}`.",
            parse_mode='Markdown'
        )
        if query.message:
            clear_if_matches(context, query.message.message_id)

    elif action == "delyes":
        sold_keys = queries.get_sold_keys(alias)
        if str(key_id) in sold_keys:
            context.user_data[PENDING_DELETE_KEY] = {
                "alias": alias,
                "key_id": str(key_id),
            }
            await query.answer("Final manual confirmation required.", show_alert=True)
            await query.edit_message_text(
                (
                    "⚠️ *Final Delete Confirmation Required*\n\n"
                    f"Key `{key_id}` on `{alias}` is marked as *SOLD*.\n"
                    "To permanently delete it, type exactly `delete` in your next message.\n"
                    "Type `cancel` to abort."
                ),
                parse_mode='Markdown'
            )
            if query.message:
                clear_if_matches(context, query.message.message_id)
            return

        client = get_vpn_client(alias)
        if not client:
            await query.answer("Could not connect to server.", show_alert=True)
            await query.edit_message_text(
                f"❌ Delete action failed for key `{key_id}` on `{alias}`.",
                parse_mode='Markdown'
            )
            if query.message:
                clear_if_matches(context, query.message.message_id)

@admin_only
async def handle_manual_sold_delete_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final text confirmation step for deleting sold keys."""
    pending = context.user_data.get(PENDING_DELETE_KEY)
    if not pending or not update.message:
        return

    text = (update.message.text or "").strip().lower()
    alias = pending.get("alias")
    key_id = pending.get("key_id")

    if text == "cancel":
        context.user_data.pop(PENDING_DELETE_KEY, None)
        await update.message.reply_text(
            f"✅ Sold-key delete cancelled for `{key_id}` on `{alias}`.",
            parse_mode='Markdown'
        )
        return

    if text != "delete":
        await update.message.reply_text(
            "⚠️ Please type exactly `delete` to confirm, or `cancel` to abort.",
            parse_mode='Markdown'
        )
        return

    client = get_vpn_client(alias)
    if not client:
        context.user_data.pop(PENDING_DELETE_KEY, None)
        await update.message.reply_text(
            f"❌ Could not connect to server `{alias}`.",
            parse_mode='Markdown'
        )
        return

    try:
        client.delete_key(key_id)
        queries.remove_key_metadata(alias, key_id)
        await update.message.reply_text(
            f"🗑️ Sold key `{key_id}` was deleted from `{alias}`.",
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Manual sold delete error: {e}")
        await update.message.reply_text(
            f"❌ Delete failed for sold key `{key_id}` on `{alias}`.",
            parse_mode='Markdown'
        )
    finally:
        context.user_data.pop(PENDING_DELETE_KEY, None)
            return

        try:
            client.delete_key(key_id)
            queries.remove_key_metadata(alias, key_id)
            await query.answer("Key deleted successfully!", show_alert=True)
            await query.edit_message_text(f"🗑️ Key `{key_id}` was deleted from `{alias}`.", parse_mode='Markdown')
            if query.message:
                clear_if_matches(context, query.message.message_id)
        except Exception as e:
            await query.answer("Failed to delete key.", show_alert=True)
            logger.error(f"Delete error: {e}")
            await query.edit_message_text(
                f"❌ Delete action failed for key `{key_id}` on `{alias}`.",
                parse_mode='Markdown'
            )
            if query.message:
                clear_if_matches(context, query.message.message_id)