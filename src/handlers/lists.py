import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from src.utils.decorators import admin_only
from src.database import queries
from src.services.outline_api import get_vpn_client
from src.utils.datetime_utils import utc_now_iso, add_days_from_utc, to_yangon_display
from src.utils.keyboards import (
    get_server_list_keyboard,
    get_key_management_keyboard,
    get_delete_confirmation_keyboard,
    get_expiry_preset_keyboard,
    get_renew_duration_keyboard,
)
from src.utils.inline_messages import (
    close_active_inline_message,
    set_active_inline_message,
    clear_if_matches,
)
from src.handlers.customers import notify_user_assigned_keys_snapshot

logger = logging.getLogger(__name__)

BYTES_PER_MB = 1_000_000
BYTES_PER_GB = 1_000_000_000
PENDING_DELETE_KEY = "pending_sold_delete"
PENDING_RENEW_KEY = "pending_renew"
PENDING_ASSIGN_KEY = "pending_assign"


async def _render_key_management_panel(
    query,
    alias: str,
    key_id: str,
    notice: str | None = None,
):
    """Renders the current key management panel so admins can chain multiple actions."""
    client = get_vpn_client(alias)
    if not client:
        await query.edit_message_text(
            f"❌ Could not connect to server `{alias}`.",
            parse_mode='Markdown'
        )
        return

    try:
        keys = client.get_keys()
    except Exception as e:
        logger.error(f"Manage key refresh error on {alias}: {e}")
        await query.edit_message_text(
            f"❌ Failed to refresh key `{key_id}` on `{alias}`.",
            parse_mode='Markdown'
        )
        return

    target_key = next((key for key in keys if str(key.key_id) == str(key_id)), None)
    if not target_key:
        await query.edit_message_text(
            f"❌ Key `{key_id}` was not found on `{alias}`.",
            parse_mode='Markdown'
        )
        return

    sold_keys = queries.get_sold_keys(alias)
    is_sold = str(key_id) in sold_keys
    lifecycle = queries.get_key_lifecycle(alias, str(key_id)) or {}
    used_gb = (target_key.used_bytes or 0) / BYTES_PER_GB
    if target_key.data_limit:
        limit_gb = target_key.data_limit / BYTES_PER_GB
        usage_line = f"{used_gb:.2f} GB / {limit_gb:.2f} GB"
    else:
        usage_line = f"{used_gb:.2f} GB / Unlimited"

    expiry_at_utc = lifecycle.get("expiry_at_utc")
    can_renew = bool(expiry_at_utc)
    expiry_state = "Expired" if lifecycle.get("is_expired") else "Active"
    expiry_line = f"{to_yangon_display(expiry_at_utc)} ({expiry_state})" if expiry_at_utc else "Not set"
    assigned_user_id = lifecycle.get("assigned_user_id")
    owner_line = f"{assigned_user_id}" if assigned_user_id else "Unassigned"
    notice_line = f"{notice}\n\n" if notice else ""

    await query.edit_message_text(
        (
            f"{notice_line}"
            f"⚙️ *Manage Key*\n\n"
            f"Server: `{alias}`\n"
            f"Key ID: `{target_key.key_id}`\n"
            f"Name: *{target_key.name or 'Unnamed'}*\n"
            f"Usage: {usage_line}\n"
            f"Expiry: *{expiry_line}*\n"
            f"Owner User ID: *{owner_line}*\n\n"
            "Use buttons below to continue, then close manually when done."
        ),
        reply_markup=get_key_management_keyboard(alias, key_id, is_sold, can_renew),
        parse_mode='Markdown'
    )


def _umgr_users_keyboard(users: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for item in users[:20]:
        uid = int(item["user_id"])
        uname = f"@{item['username']}" if item.get("username") else "(no username)"
        rows.append([InlineKeyboardButton(f"⚙️ Manage {uid} {uname}", callback_data=f"umgr|user|{uid}")])
    rows.append([InlineKeyboardButton("❎ Close", callback_data="umgr|close")])
    return InlineKeyboardMarkup(rows)


def _umgr_manage_user_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Assign Key", callback_data=f"umgr|assignsrv|{user_id}")],
            [InlineKeyboardButton("➖ Unassign Key", callback_data=f"umgr|unassign|{user_id}")],
            [
                InlineKeyboardButton("⬅️ Back Users", callback_data="umgr|users"),
                InlineKeyboardButton("❎ Close", callback_data="umgr|close"),
            ],
        ]
    )


def _umgr_assign_servers_keyboard(user_id: int, servers: dict) -> InlineKeyboardMarkup:
    rows = []
    for alias in servers.keys():
        rows.append([InlineKeyboardButton(f"🌐 {alias}", callback_data=f"umgr|srv|{user_id}|{alias}")])
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"umgr|user|{user_id}"),
            InlineKeyboardButton("❎ Close", callback_data="umgr|close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _umgr_assign_keys_keyboard(user_id: int, alias: str, keys: list) -> InlineKeyboardMarkup:
    rows = []
    for key in keys[:20]:
        if key["assigned_user_id"]:
            continue
        key_name = key["name"] or "Unnamed"
        status_prefix = key["status_prefix"]
        rows.append(
            [
                InlineKeyboardButton(
                    f"{status_prefix} {key['key_id']} - {key_name[:20]}",
                    callback_data=f"umgr|assign|{user_id}|{alias}|{key['key_id']}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back Servers", callback_data=f"umgr|assignsrv|{user_id}"),
            InlineKeyboardButton("❎ Close", callback_data="umgr|close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _umgr_unassign_keyboard(user_id: int, assigned: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for item in assigned[:20]:
        alias = item["server_alias"]
        key_id = str(item["key_id"])
        rows.append(
            [
                InlineKeyboardButton(
                    f"➖ {alias} / {key_id}",
                    callback_data=f"umgr|unas|{user_id}|{alias}|{key_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"umgr|user|{user_id}"),
            InlineKeyboardButton("❎ Close", callback_data="umgr|close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _show_manage_users_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    approved_users = queries.get_customers_by_status("approved")
    lines = [
        "👥 *Manage Approved Users*",
        f"Total approved users: *{len(approved_users)}*",
        "",
    ]
    if approved_users:
        lines.append("Select a user to manage key assignment.")
    else:
        lines.append("No approved users found.")

    text = "\n".join(lines)
    kb = _umgr_users_keyboard(approved_users)

    if update.callback_query and edit:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=kb)
    elif update.effective_message:
        await close_active_inline_message(update, context)
        sent = await update.effective_message.reply_text(text, parse_mode='Markdown', reply_markup=kb)
        set_active_inline_message(context, sent.message_id)

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
            lifecycle = queries.get_key_lifecycle(alias, str(key.key_id)) or {}
            is_used_up = bool(key.data_limit) and (key.used_bytes or 0) >= key.data_limit
            if is_used_up:
                status_tag = "🟠 [USED UP]"
            elif key.key_id in sold_keys:
                status_tag = "🔴 [SOLD]"
            else:
                status_tag = "🟢 [AVAILABLE]"

            expiry_at_utc = lifecycle.get("expiry_at_utc")
            expiry_tag = "⛔ EXPIRED" if lifecycle.get("is_expired") else "✅ ACTIVE"
            expiry_text = to_yangon_display(expiry_at_utc) if expiry_at_utc else "Not set"
            owner_user_id = lifecycle.get("assigned_user_id") or "Unassigned"

            key_id_str = str(key.key_id)
            creator_username = key_creators.get(key_id_str)
            creator_tag = f" | By: @{creator_username}" if creator_username else ""
            
            msg += f"ID: `{key.key_id}` | Name: *{key.name or 'Unnamed'}* {status_tag}{creator_tag}\n"
            if isinstance(limit_gb, float):
                msg += f"Usage: {used_gb:.2f} GB / {limit_gb:.2f} GB\n"
            else:
                msg += f"Usage: {used_gb:.2f} GB / Unlimited\n"
            msg += f"Expiry: {expiry_text} ({expiry_tag})\n"
            msg += f"Owner User ID: {owner_user_id}\n"
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
    """Command: /manage - user-management panel, or /manage <alias> <key_id> for key actions."""
    if len(context.args) == 0:
        await _show_manage_users_panel(update, context)
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: `/manage` for user management or `/manage <server_alias> <key_id>` for key management.",
            parse_mode='Markdown'
        )
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
    lifecycle = queries.get_key_lifecycle(alias, str(key_id)) or {}
    used_gb = (target_key.used_bytes or 0) / BYTES_PER_GB
    if target_key.data_limit:
        limit_gb = target_key.data_limit / BYTES_PER_GB
        usage_line = f"{used_gb:.2f} GB / {limit_gb:.2f} GB"
    else:
        usage_line = f"{used_gb:.2f} GB / Unlimited"

    expiry_at_utc = lifecycle.get("expiry_at_utc")
    expiry_state = "Expired" if lifecycle.get("is_expired") else "Active"
    expiry_line = f"{to_yangon_display(expiry_at_utc)} ({expiry_state})" if expiry_at_utc else "Not set"
    assigned_user_id = lifecycle.get("assigned_user_id")
    owner_line = f"{assigned_user_id}" if assigned_user_id else "Unassigned"

    await close_active_inline_message(update, context)
    can_renew = bool(expiry_at_utc)
    keyboard = get_key_management_keyboard(alias, key_id, is_sold, can_renew)
    sent = await update.message.reply_text(
        (
            f"⚙️ *Manage Key*\n\n"
            f"Server: `{alias}`\n"
            f"Key ID: `{target_key.key_id}`\n"
            f"Name: *{target_key.name or 'Unnamed'}*\n"
            f"Usage: {usage_line}\n"
            f"Expiry: *{expiry_line}*\n"
            f"Owner User ID: *{owner_line}*"
        ),
        reply_markup=keyboard,
        parse_mode='Markdown'
    )
    set_active_inline_message(context, sent.message_id)


@admin_only
async def handle_user_manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline user-manage workflow launched from /manage without args."""
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split("|")
    if len(parts) < 2 or parts[0] != "umgr":
        await query.answer("Invalid action.", show_alert=True)
        return

    action = parts[1]

    if action == "close":
        await query.answer("Closed.")
        await query.edit_message_text("✅ Manage panel closed.")
        if query.message:
            clear_if_matches(context, query.message.message_id)
        return

    if action == "users":
        await query.answer()
        await _show_manage_users_panel(update, context, edit=True)
        return

    if action == "user" and len(parts) == 3:
        await query.answer()
        user_id = int(parts[2])
        customer = queries.get_customer(user_id)
        if not customer or (customer.get("status") or "").lower() != "approved":
            await query.answer("User is not approved anymore.", show_alert=True)
            await _show_manage_users_panel(update, context, edit=True)
            return

        uname = f"@{customer.get('username')}" if customer.get("username") else "(no username)"
        first_name = customer.get("first_name") or "N/A"
        assigned = queries.get_user_assigned_keys(user_id)
        await query.edit_message_text(
            (
                "👤 *Manage Approved User*\n\n"
                f"User ID: `{user_id}`\n"
                f"Username: {uname}\n"
                f"Name: {first_name}\n"
                f"Assigned Keys: *{len(assigned)}*"
            ),
            parse_mode='Markdown',
            reply_markup=_umgr_manage_user_keyboard(user_id),
        )
        return

    if action == "assignsrv" and len(parts) == 3:
        await query.answer()
        user_id = int(parts[2])
        servers = queries.get_servers()
        if not servers:
            await query.answer("No servers configured.", show_alert=True)
            return
        await query.edit_message_text(
            f"➕ *Assign Key*\n\nChoose a server for user `{user_id}`.",
            parse_mode='Markdown',
            reply_markup=_umgr_assign_servers_keyboard(user_id, servers),
        )
        return

    if action == "srv" and len(parts) == 4:
        await query.answer()
        user_id = int(parts[2])
        alias = parts[3]
        client = get_vpn_client(alias)
        if not client:
            await query.answer("Could not connect to server.", show_alert=True)
            return
        try:
            keys = client.get_keys()
        except Exception as e:
            logger.error(f"User-manage assign key list error on {alias}: {e}")
            await query.answer("Failed to fetch keys.", show_alert=True)
            return

        if not keys:
            await query.answer("No keys found on this server.", show_alert=True)
            return

        key_entries = []
        free_count = 0
        assigned_count = 0
        for key in keys:
            key_id = str(key.key_id)
            lifecycle = queries.get_key_lifecycle(alias, key_id) or {}
            assigned_user_id = lifecycle.get("assigned_user_id")
            if assigned_user_id:
                status_prefix = "🔒"
                assigned_count += 1
            else:
                status_prefix = "🟢"
                free_count += 1
            key_entries.append(
                {
                    "key_id": key_id,
                    "name": key.name,
                    "assigned_user_id": assigned_user_id,
                    "status_prefix": status_prefix,
                }
            )

        # Show free keys first, then assigned keys for clarity.
        key_entries.sort(key=lambda x: (x["assigned_user_id"] is not None, str(x["key_id"])))

        if free_count == 0:
            await query.edit_message_text(
                (
                    "🔑 *Select Key To Assign*\n\n"
                    f"User ID: `{user_id}`\n"
                    f"Server: `{alias}`\n"
                    f"Free: *{free_count}* | Assigned: *{assigned_count}*\n"
                    "No free keys are available on this server right now."
                ),
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("⬅️ Back Servers", callback_data=f"umgr|assignsrv|{user_id}")],
                        [InlineKeyboardButton("❎ Close", callback_data="umgr|close")],
                    ]
                ),
            )
            return

        await query.edit_message_text(
            (
                "🔑 *Select Key To Assign*\n\n"
                f"User ID: `{user_id}`\n"
                f"Server: `{alias}`\n"
                f"Free: *{free_count}* | Assigned: *{assigned_count}*\n"
                "Legend: 🟢 free (selectable), 🔒 already assigned (not selectable)"
            ),
            parse_mode='Markdown',
            reply_markup=_umgr_assign_keys_keyboard(user_id, alias, key_entries),
        )
        return

    if action == "assign" and len(parts) == 5:
        await query.answer()
        user_id = int(parts[2])
        alias = parts[3]
        key_id = parts[4]

        customer = queries.get_customer(user_id)
        if not customer or (customer.get("status") or "").lower() != "approved":
            await query.answer("Target user is not approved.", show_alert=True)
            return

        queries.set_key_assignment(alias, key_id, user_id)
        queries.add_key_lifecycle_event(
            server_alias=alias,
            key_id=key_id,
            event_type="assigned_user",
            actor_user_id=update.effective_user.id if update.effective_user else None,
            actor_username=update.effective_user.username if update.effective_user else None,
            payload={"assigned_user_id": user_id, "source": "manage_user_flow"},
        )

        try:
            await notify_user_assigned_keys_snapshot(context, user_id)
        except Exception as e:
            logger.info(f"Could not notify user {user_id} after assignment: {e}")

        await query.edit_message_text(
            (
                "✅ *Key Assigned*\n\n"
                f"User ID: `{user_id}`\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`"
            ),
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back User", callback_data=f"umgr|user|{user_id}")],
                 [InlineKeyboardButton("❎ Close", callback_data="umgr|close")]]
            ),
        )
        return

    if action == "unassign" and len(parts) == 3:
        await query.answer()
        user_id = int(parts[2])
        assigned = queries.get_user_assigned_keys(user_id)
        if not assigned:
            await query.edit_message_text(
                f"➖ *Unassign Key*\n\nNo keys currently assigned to user `{user_id}`.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back User", callback_data=f"umgr|user|{user_id}")],
                     [InlineKeyboardButton("❎ Close", callback_data="umgr|close")]]
                ),
            )
            return

        await query.edit_message_text(
            f"➖ *Unassign Key*\n\nSelect a key to unassign from user `{user_id}`.",
            parse_mode='Markdown',
            reply_markup=_umgr_unassign_keyboard(user_id, assigned),
        )
        return

    if action == "unas" and len(parts) == 5:
        await query.answer()
        user_id = int(parts[2])
        alias = parts[3]
        key_id = parts[4]

        queries.set_key_assignment(alias, key_id, None)
        queries.add_key_lifecycle_event(
            server_alias=alias,
            key_id=key_id,
            event_type="unassigned_user",
            actor_user_id=update.effective_user.id if update.effective_user else None,
            actor_username=update.effective_user.username if update.effective_user else None,
            payload={"assigned_user_id": user_id, "source": "manage_user_flow"},
        )

        await query.edit_message_text(
            (
                "✅ *Key Unassigned*\n\n"
                f"User ID: `{user_id}`\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`"
            ),
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back User", callback_data=f"umgr|user|{user_id}")],
                 [InlineKeyboardButton("❎ Close", callback_data="umgr|close")]]
            ),
        )
        return

    await query.answer("Unknown manage action.", show_alert=True)

@admin_only
async def handle_key_actions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles Mark Sold / Delete actions from the manage key keyboard."""
    query = update.callback_query
    data = query.data.split("_", 2)
    action = data[0]
    alias = data[1]
    key_id = data[2]

    if action == "close":
        await query.answer("Closed.")
        await query.edit_message_text(
            f"✅ Key management panel closed for `{key_id}` on `{alias}`.",
            parse_mode='Markdown'
        )
        if query.message:
            clear_if_matches(context, query.message.message_id)
        return
    
    if action == "toggle":
        new_status = queries.toggle_key_sold(alias, key_id)
        status_text = "Sold" if new_status else "Available"
        await query.answer(f"Key marked as {status_text}!")
        await _render_key_management_panel(
            query,
            alias,
            str(key_id),
            notice=f"✅ Key marked as *{status_text}*."
        )

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
            lifecycle = queries.get_key_lifecycle(alias, str(key_id)) or {}
            expiry_at_utc = lifecycle.get("expiry_at_utc")
            expiry_text = to_yangon_display(expiry_at_utc) if expiry_at_utc else "Not set"
            owner_user_id = lifecycle.get("assigned_user_id") or "Unassigned"
            renew_count = lifecycle.get("renew_count") or 0
            used_gb = (target_key.used_bytes or 0) / BYTES_PER_GB
            if target_key.data_limit:
                limit_gb = target_key.data_limit / BYTES_PER_GB
                available_bytes = max(target_key.data_limit - (target_key.used_bytes or 0), 0)
                available_gb = available_bytes / BYTES_PER_GB
                usage_line = f"Available Usage: *{available_gb:.2f} GB* (Used: {used_gb:.2f} GB / {limit_gb:.2f} GB)"
            else:
                usage_line = f"Available Usage: *Unlimited* (Used: {used_gb:.2f} GB / Unlimited)"

            await query.message.reply_text(
                (
                    f"🔑 *Key Access URL*\n\n"
                    f"Server: `{alias}`\n"
                    f"Key ID: `{key_id}`\n"
                    f"Name: *{key_name}*\n"
                    f"{creator_line}\n"
                    f"{usage_line}\n"
                    f"Expiry: *{expiry_text}*\n"
                    f"Owner User ID: *{owner_user_id}*\n"
                    f"Renew Count: *{renew_count}*\n\n"
                    f"`{target_key.access_url}`"
                ),
                parse_mode='Markdown'
            )
            await _render_key_management_panel(
                query,
                alias,
                str(key_id),
                notice="✅ View details sent below."
            )
        except Exception as e:
            await query.answer("Failed to fetch key details.", show_alert=True)
            logger.error(f"View key error: {e}")
            await _render_key_management_panel(
                query,
                alias,
                str(key_id),
                notice="❌ View action failed."
            )

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
        lifecycle = queries.get_key_lifecycle(alias, str(key_id)) or {}
        expiry_at_utc = lifecycle.get("expiry_at_utc")
        expiry_text = to_yangon_display(expiry_at_utc) if expiry_at_utc else "Not set"
        owner_user_id = lifecycle.get("assigned_user_id") or "Unassigned"
        renew_count = lifecycle.get("renew_count") or 0
        await query.answer("Confirm delete to proceed.", show_alert=True)
        await query.edit_message_text(
            (
                f"⚠️ *Delete Confirmation*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n"
                f"Name: *{target_key.name or 'Unnamed'}*\n"
                f"Usage: {usage_line}\n"
                f"Expiry: *{expiry_text}*\n"
                f"Owner User ID: *{owner_user_id}*\n"
                f"Renew Count: *{renew_count}*\n"
                f"Status: *{status_tag}*\n\n"
                "This action cannot be undone."
            ),
            reply_markup=keyboard,
            parse_mode='Markdown',
        )

    elif action == "delno":
        await query.answer("Delete cancelled.")
        await _render_key_management_panel(
            query,
            alias,
            str(key_id),
            notice="✅ Delete cancelled."
        )

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

    elif action == "expiry":
        await query.answer()
        lifecycle = queries.get_key_lifecycle(alias, str(key_id)) or {}
        expiry_at_utc = lifecycle.get("expiry_at_utc")
        expiry_state = "Expired" if lifecycle.get("is_expired") else "Active"
        expiry_line = f"{to_yangon_display(expiry_at_utc)} ({expiry_state})" if expiry_at_utc else "Not set"

        await query.edit_message_text(
            (
                f"⏳ *Set Expiry*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n"
                f"Current Expiry: *{expiry_line}*\n\n"
                "Choose preset duration from now."
            ),
            reply_markup=get_expiry_preset_keyboard(alias, key_id),
            parse_mode='Markdown'
        )

    elif action in {"expd30", "expd90", "expd180", "expd360"}:
        await query.answer()
        day_map = {
            "expd30": 30,
            "expd90": 90,
            "expd180": 180,
            "expd360": 360,
        }
        days = day_map[action]
        now_utc = utc_now_iso()
        expiry_at_utc = add_days_from_utc(now_utc, days)

        queries.set_key_expiry(alias, str(key_id), expiry_at_utc)
        queries.add_key_lifecycle_event(
            server_alias=alias,
            key_id=str(key_id),
            event_type="set_expiry",
            actor_user_id=update.effective_user.id if update.effective_user else None,
            actor_username=update.effective_user.username if update.effective_user else None,
            payload={"days": days, "expiry_at_utc": expiry_at_utc},
        )

        await _render_key_management_panel(
            query,
            alias,
            str(key_id),
            notice=(
                "✅ *Expiry Updated*\n"
                f"New Expiry (Yangon): *{to_yangon_display(expiry_at_utc)}*\n"
                f"Applied: *+{days} days from now*"
            )
        )

    elif action == "expclr":
        await query.answer()
        queries.set_key_expiry(alias, str(key_id), None)
        queries.clear_key_expired(alias, str(key_id))
        queries.add_key_lifecycle_event(
            server_alias=alias,
            key_id=str(key_id),
            event_type="manual_override",
            actor_user_id=update.effective_user.id if update.effective_user else None,
            actor_username=update.effective_user.username if update.effective_user else None,
            payload={"expiry_cleared": True},
        )
        await _render_key_management_panel(
            query,
            alias,
            str(key_id),
            notice="✅ Expiry cleared."
        )

    elif action == "expcancel":
        await query.answer("Cancelled.")
        await _render_key_management_panel(
            query,
            alias,
            str(key_id),
            notice="✅ Expiry update cancelled."
        )

    elif action == "renew":
        await query.answer()
        lifecycle = queries.get_key_lifecycle(alias, str(key_id)) or {}
        expiry_at_utc = lifecycle.get("expiry_at_utc")
        if not expiry_at_utc:
            await query.answer("Set expiry first, then renew.", show_alert=True)
            await _render_key_management_panel(
                query,
                alias,
                str(key_id),
                notice="⚠️ Renew is disabled until expiry is set."
            )
            return
        current_expiry = to_yangon_display(expiry_at_utc) if expiry_at_utc else "Not set"
        await query.edit_message_text(
            (
                "🔄 *Renew Key*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n"
                f"Current Expiry (Yangon): *{current_expiry}*\n\n"
                "Step 1/2: Choose renewal duration.\n"
                "Step 2/2: You will then type the new quota in GB."
            ),
            reply_markup=get_renew_duration_keyboard(alias, key_id),
            parse_mode='Markdown'
        )

    elif action in {"rnd30", "rnd90", "rnd180", "rnd360"}:
        await query.answer()
        day_map = {
            "rnd30": 30,
            "rnd90": 90,
            "rnd180": 180,
            "rnd360": 360,
        }
        days = day_map[action]
        context.user_data[PENDING_RENEW_KEY] = {
            "alias": alias,
            "key_id": str(key_id),
            "days": days,
            "actor_user_id": update.effective_user.id if update.effective_user else None,
            "actor_username": update.effective_user.username if update.effective_user else None,
        }
        await query.edit_message_text(
            (
                "📝 *Renew Quota Input Required*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n"
                f"Duration: *+{days} days*\n\n"
                "Now type the *new quota in GB* (example: `50`).\n"
                "Type `0` for unlimited.\n"
                "Type `cancel` to abort."
            ),
            parse_mode='Markdown'
        )
        if query.message:
            clear_if_matches(context, query.message.message_id)

    elif action == "rncancel":
        await query.answer("Cancelled.")
        context.user_data.pop(PENDING_RENEW_KEY, None)
        await _render_key_management_panel(
            query,
            alias,
            str(key_id),
            notice="✅ Renew cancelled."
        )

    elif action == "assign":
        await query.answer()
        context.user_data[PENDING_ASSIGN_KEY] = {
            "alias": alias,
            "key_id": str(key_id),
            "actor_user_id": update.effective_user.id if update.effective_user else None,
            "actor_username": update.effective_user.username if update.effective_user else None,
        }
        await query.edit_message_text(
            (
                "👤 *Assign User To Key*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n\n"
                "Type the target *approved user_id* in your next message.\n"
                "Type `cancel` to abort."
            ),
            parse_mode='Markdown'
        )
        if query.message:
            clear_if_matches(context, query.message.message_id)

    elif action == "unassign":
        await query.answer()
        queries.set_key_assignment(alias, str(key_id), None)
        queries.add_key_lifecycle_event(
            server_alias=alias,
            key_id=str(key_id),
            event_type="unassigned_user",
            actor_user_id=update.effective_user.id if update.effective_user else None,
            actor_username=update.effective_user.username if update.effective_user else None,
            payload={"assigned_user_id": None},
        )
        await _render_key_management_panel(
            query,
            alias,
            str(key_id),
            notice="✅ User unassigned from key."
        )

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


async def handle_manual_renew_quota_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final text input step for renew flow: receives new quota GB."""
    pending = context.user_data.get(PENDING_RENEW_KEY)
    if not pending or not update.message:
        return

    user = update.effective_user
    if not user:
        return

    actor_user_id = pending.get("actor_user_id")
    if actor_user_id and user.id != actor_user_id:
        return

    text = (update.message.text or "").strip().lower()
    alias = pending.get("alias")
    key_id = pending.get("key_id")
    days = int(pending.get("days", 0))
    actor_username = pending.get("actor_username")

    if text == "cancel":
        context.user_data.pop(PENDING_RENEW_KEY, None)
        await update.message.reply_text(
            f"✅ Renew cancelled for key `{key_id}` on `{alias}`.",
            parse_mode='Markdown'
        )
        return

    try:
        quota_gb = float(text)
        if quota_gb < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Invalid quota. Please enter a non-negative number in GB (example: `50`), or `cancel`.",
            parse_mode='Markdown'
        )
        return

    client = get_vpn_client(alias)
    if not client:
        context.user_data.pop(PENDING_RENEW_KEY, None)
        await update.message.reply_text(
            f"❌ Could not connect to server `{alias}`.",
            parse_mode='Markdown'
        )
        return

    try:
        if quota_gb == 0:
            client.delete_data_limit(key_id)
        else:
            quota_bytes = int(quota_gb * BYTES_PER_GB)
            client.add_data_limit(key_id, quota_bytes)

        now_utc = utc_now_iso()
        new_expiry_utc = add_days_from_utc(now_utc, days)

        queries.set_key_expiry(alias, key_id, new_expiry_utc)
        queries.record_key_renewal(alias, key_id, quota_gb, renewed_at_utc=now_utc)
        queries.add_key_lifecycle_event(
            server_alias=alias,
            key_id=key_id,
            event_type="renew",
            actor_user_id=user.id,
            actor_username=actor_username or user.username,
            payload={
                "days": days,
                "quota_gb": quota_gb,
                "expiry_at_utc": new_expiry_utc,
                "renewed_at_utc": now_utc,
            },
            created_at_utc=now_utc,
        )

        quota_text = "Unlimited" if quota_gb == 0 else f"{quota_gb:.2f} GB"
        await update.message.reply_text(
            (
                "✅ *Renew Completed*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n"
                f"New Quota: *{quota_text}*\n"
                f"New Expiry (Yangon): *{to_yangon_display(new_expiry_utc)}*\n"
                f"Policy: *+{days} days from renewal time*"
            ),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Manual renew error: {e}")
        await update.message.reply_text(
            f"❌ Renew failed for key `{key_id}` on `{alias}`.",
            parse_mode='Markdown'
        )
    finally:
        context.user_data.pop(PENDING_RENEW_KEY, None)


async def handle_manual_assign_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final text input step for assignment flow: receives approved user id."""
    pending = context.user_data.get(PENDING_ASSIGN_KEY)
    if not pending or not update.message:
        return

    user = update.effective_user
    if not user:
        return

    actor_user_id = pending.get("actor_user_id")
    if actor_user_id and user.id != actor_user_id:
        return

    text = (update.message.text or "").strip().lower()
    alias = pending.get("alias")
    key_id = pending.get("key_id")
    actor_username = pending.get("actor_username")

    if text == "cancel":
        context.user_data.pop(PENDING_ASSIGN_KEY, None)
        await update.message.reply_text(
            f"✅ Assignment cancelled for key `{key_id}` on `{alias}`.",
            parse_mode='Markdown'
        )
        return

    try:
        target_user_id = int(text)
    except ValueError:
        await update.message.reply_text(
            "⚠️ Invalid user id. Please enter a numeric user id, or `cancel`.",
            parse_mode='Markdown'
        )
        return

    customer = queries.get_customer(target_user_id)
    if not customer or (customer.get("status") or "").lower() != "approved":
        await update.message.reply_text(
            "❌ Target user is not approved. Approve first with /approve <user_id>.",
            parse_mode='Markdown'
        )
        return

    try:
        queries.set_key_assignment(alias, key_id, target_user_id)
        queries.add_key_lifecycle_event(
            server_alias=alias,
            key_id=key_id,
            event_type="assigned_user",
            actor_user_id=user.id,
            actor_username=actor_username or user.username,
            payload={"assigned_user_id": target_user_id},
        )
        try:
            await notify_user_assigned_keys_snapshot(context, target_user_id)
        except Exception as e:
            logger.info(f"Could not notify user {target_user_id} after assignment: {e}")
        await update.message.reply_text(
            (
                "✅ *Key Assigned*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n"
                f"Assigned User ID: *{target_user_id}*"
            ),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Manual assign user error: {e}")
        await update.message.reply_text(
            f"❌ Assign failed for key `{key_id}` on `{alias}`.",
            parse_mode='Markdown'
        )
    finally:
        context.user_data.pop(PENDING_ASSIGN_KEY, None)