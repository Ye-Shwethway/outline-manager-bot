import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown
from telegram.ext import ContextTypes
from src.utils.decorators import admin_only
from src.config import OWNER_ID
from src.database import queries
from src.services.outline_api import get_vpn_client
from src.utils.datetime_utils import utc_now_iso, add_days_from_utc, to_yangon_display, parse_utc_iso
from src.utils.keyboards import (
    get_server_list_keyboard,
    get_key_management_keyboard,
    get_delete_confirmation_keyboard,
    get_expiry_preset_keyboard,
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


def _is_privileged_account(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in queries.get_admins()


def _can_manage_account(actor_user_id: int | None, target_user_id: int) -> bool:
    if actor_user_id == OWNER_ID:
        return True
    if _is_privileged_account(target_user_id):
        return actor_user_id == target_user_id
    return True


def _resolve_username_for_user(user_id: int) -> str | None:
    customer = queries.get_customer(user_id)
    if customer and customer.get("username"):
        return customer.get("username")

    for item in queries.get_admin_profiles():
        if int(item["user_id"]) == user_id and item.get("username"):
            return item.get("username")
    return None


def _format_user_identity_markdown(user_id: int | None) -> str:
    if not user_id:
        return "*Unassigned*"
    username = _resolve_username_for_user(int(user_id))
    if username:
        uname_safe = escape_markdown(f"@{username}", version=1)
        return f"`{user_id}` ({uname_safe})"
    return f"`{user_id}`"


def _format_username_markdown(username: str | None) -> str:
    return escape_markdown(f"@{username}", version=1) if username else "(no username)"


def _format_text_markdown(value: str | None, default: str = "N/A") -> str:
    if value is None or str(value).strip() == "":
        return default
    return escape_markdown(str(value), version=1)


def _raw_used_bytes(key) -> int:
    return max(int(key.used_bytes or 0), 0)


def _display_limit_bytes(key, lifecycle: dict) -> int:
    if lifecycle.get("configured_limit_bytes"):
        return int(lifecycle.get("configured_limit_bytes") or 0)
    if key.data_limit:
        return int(key.data_limit)
    if lifecycle.get("quota_block_limit_bytes"):
        return int(lifecycle.get("quota_block_limit_bytes") or 0)
    return 0


def _is_unlimited_config(lifecycle: dict, key) -> bool:
    if lifecycle.get("configured_limit_mode") == "unlimited":
        return True
    if lifecycle.get("configured_limit_mode") == "limited":
        return False
    return bool(key.data_limit is None)


def _assignment_sale_grant_params(lifecycle: dict, live_key) -> tuple[int, bool]:
    configured_bytes = int(lifecycle.get("configured_limit_bytes") or 0)
    if configured_bytes > 0:
        return configured_bytes, False
    if lifecycle.get("configured_limit_mode") == "unlimited":
        return 0, True
    live_limit_bytes = max(int(live_key.data_limit or 0), 0)
    if live_limit_bytes > 0:
        return live_limit_bytes, False
    quota_block_bytes = int(lifecycle.get("quota_block_limit_bytes") or 0)
    if quota_block_bytes > 0:
        return quota_block_bytes, False
    return 0, False


def _is_quota_blocked(lifecycle: dict) -> bool:
    return bool(lifecycle.get("quota_blocked_at_utc"))


def _status_line_for_display(key_id: str, sold_keys: set[str], lifecycle: dict, raw_used_bytes: int, limit_bytes: int) -> str:
    if lifecycle.get("is_expired"):
        return "⛔ EXPIRED"
    if _is_quota_blocked(lifecycle):
        return "⛔ QUOTA BLOCKED"
    if limit_bytes and raw_used_bytes >= limit_bytes:
        return "🟠 USED UP"
    if str(key_id) in sold_keys:
        return "🔴 SOLD"
    return "🟢 AVAILABLE"


def _format_lifetime_bytes(total_bytes: int | None, unlimited_count: int | None = 0) -> str:
    bytes_value = max(int(total_bytes or 0), 0)
    unlimited_value = max(int(unlimited_count or 0), 0)
    if bytes_value <= 0 and unlimited_value <= 0:
        return "0.00 GB"
    if unlimited_value <= 0:
        return f"{bytes_value / BYTES_PER_GB:.2f} GB"
    if bytes_value <= 0:
        return f"Unlimited x{unlimited_value}"
    return f"{bytes_value / BYTES_PER_GB:.2f} GB + Unlimited x{unlimited_value}"


def _renew_servers_keyboard(servers: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🌐 {alias}", callback_data=f"rflow|srv|{alias}")]
        for alias in sorted(servers.keys())
    ]
    rows.append([InlineKeyboardButton("❎ Close", callback_data="rflow|close")])
    return InlineKeyboardMarkup(rows)


def _renew_keys_keyboard(alias: str, keys: list) -> InlineKeyboardMarkup:
    rows = []
    for key in keys[:30]:
        key_name = str(key.name or "Unnamed")[:18]
        rows.append([
            InlineKeyboardButton(
                f"🔑 {key.key_id} | {key_name}",
                callback_data=f"rflow|key|{alias}|{key.key_id}",
            )
        ])
    rows.append(
        [
            InlineKeyboardButton("🌐 Back Servers", callback_data="rflow|servers"),
            InlineKeyboardButton("❎ Close", callback_data="rflow|close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _renew_duration_keyboard(alias: str, key_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("+30 days", callback_data=f"rflow|dur|{alias}|{key_id}|30"),
                InlineKeyboardButton("+90 days", callback_data=f"rflow|dur|{alias}|{key_id}|90"),
            ],
            [
                InlineKeyboardButton("+180 days", callback_data=f"rflow|dur|{alias}|{key_id}|180"),
                InlineKeyboardButton("+360 days", callback_data=f"rflow|dur|{alias}|{key_id}|360"),
            ],
            [
                InlineKeyboardButton("⬅️ Back Keys", callback_data=f"rflow|srv|{alias}"),
                InlineKeyboardButton("❎ Close", callback_data="rflow|close"),
            ],
        ]
    )


def _renew_action_keyboard(alias: str, key_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✍️ Renew Quota Only", callback_data=f"rflow|quotaonly|{alias}|{key_id}"),
            ],
            [
                InlineKeyboardButton("⏳ Renew Quota + Expiry", callback_data=f"rflow|expiry|{alias}|{key_id}"),
            ],
            [
                InlineKeyboardButton("⬅️ Back Keys", callback_data=f"rflow|srv|{alias}"),
                InlineKeyboardButton("❎ Close", callback_data="rflow|close"),
            ],
        ]
    )


def _renew_expiry_back_keyboard(alias: str, key_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬅️ Back Renew Options", callback_data=f"rflow|key|{alias}|{key_id}"),
                InlineKeyboardButton("❎ Close", callback_data="rflow|close"),
            ],
        ]
    )


def _build_renew_key_snapshot(alias: str, key_id: str) -> tuple[dict | None, str | None]:
    client = get_vpn_client(alias)
    if not client:
        return None, f"❌ Could not connect to server `{alias}`."

    try:
        keys = client.get_keys()
    except Exception as e:
        logger.error(f"Renew snapshot fetch error on {alias}: {e}")
        return None, f"❌ Failed to fetch key `{key_id}` from `{alias}`."

    target_key = next((key for key in keys if str(key.key_id) == str(key_id)), None)
    if not target_key:
        return None, f"❌ Key `{key_id}` was not found on `{alias}`."

    lifecycle = queries.get_key_lifecycle(alias, str(key_id)) or {}
    sold_keys = queries.get_sold_keys(alias)
    is_sold = str(key_id) in sold_keys
    raw_used_bytes = _raw_used_bytes(target_key)
    limit_bytes = _display_limit_bytes(target_key, lifecycle)
    used_gb = raw_used_bytes / BYTES_PER_GB

    if limit_bytes:
        limit_gb = limit_bytes / BYTES_PER_GB
        quota_line = f"{limit_gb:.2f} GB"
        usage_line = f"{used_gb:.2f} GB / {limit_gb:.2f} GB"
    elif _is_unlimited_config(lifecycle, target_key):
        quota_line = "Unlimited"
        usage_line = f"{used_gb:.2f} GB / Unlimited"
    else:
        quota_line = "Not set"
        usage_line = f"{used_gb:.2f} GB / Not set"
    status_line = _status_line_for_display(str(key_id), sold_keys, lifecycle, raw_used_bytes, limit_bytes)

    expiry_at_utc = lifecycle.get("expiry_at_utc")
    expiry_state = "Expired" if lifecycle.get("is_expired") else "Active"
    expiry_line = f"{to_yangon_display(expiry_at_utc)} ({expiry_state})" if expiry_at_utc else "Not set"
    assigned_user_id = lifecycle.get("assigned_user_id")
    owner_line = _format_user_identity_markdown(int(assigned_user_id)) if assigned_user_id else "*Unassigned*"
    accounting = queries.get_key_accounting_totals(alias, str(key_id)) or {}

    return {
        "server_alias": alias,
        "key_id": str(target_key.key_id),
        "name": _format_text_markdown(target_key.name, default="Unnamed"),
        "usage_line": usage_line,
        "quota_line": quota_line,
        "expiry_at_utc": expiry_at_utc,
        "expiry_line": expiry_line,
        "owner_line": owner_line,
        "renew_count": lifecycle.get("renew_count") or 0,
        "status_line": status_line,
        "auto_disabled_at_utc": lifecycle.get("auto_disabled_at_utc"),
        "lifetime_bought_line": _format_lifetime_bytes(
            accounting.get("total_purchased_bytes"),
            accounting.get("unlimited_grant_count"),
        ),
        "lifetime_used_line": _format_lifetime_bytes(accounting.get("total_consumed_bytes")),
    }, None


def _render_renew_key_snapshot_text(snapshot: dict, footer: str) -> str:
    return (
        "🔄 *Renew Key*\n\n"
        f"Server: `{snapshot['server_alias']}`\n"
        f"Key ID: `{snapshot['key_id']}`\n"
        f"Name: *{snapshot['name']}*\n"
        f"Status: *{snapshot['status_line']}*\n"
        f"Current Quota: *{snapshot['quota_line']}*\n"
        f"Current Usage: {snapshot['usage_line']}\n"
        f"Current Expiry (Yangon): *{snapshot['expiry_line']}*\n"
        f"Lifetime Bought: *{snapshot['lifetime_bought_line']}*\n"
        f"Lifetime Used: *{snapshot['lifetime_used_line']}*\n"
        f"Owner: {snapshot['owner_line']}\n"
        f"Renew Count: *{snapshot['renew_count']}*\n\n"
        f"{footer}"
    )


async def _show_renew_server_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    servers = queries.get_servers()
    if not servers:
        text = "No servers configured yet."
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text)
        elif update.message:
            await update.message.reply_text(text)
        return

    text = "🔄 *Renew Key*\n\nSelect a server to start the renew workflow."
    keyboard = _renew_servers_keyboard(servers)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=keyboard)


async def _show_renew_key_picker(query, alias: str):
    client = get_vpn_client(alias)
    if not client:
        await query.edit_message_text(
            f"❌ Could not connect to server `{alias}`.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Back Servers", callback_data="rflow|servers")],
                [InlineKeyboardButton("❎ Close", callback_data="rflow|close")],
            ]),
        )
        return

    try:
        keys = client.get_keys()
    except Exception as e:
        logger.error(f"Renew key list error on {alias}: {e}")
        await query.edit_message_text(
            f"❌ Failed to fetch keys from `{alias}`.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Back Servers", callback_data="rflow|servers")],
                [InlineKeyboardButton("❎ Close", callback_data="rflow|close")],
            ]),
        )
        return

    if not keys:
        await query.edit_message_text(
            f"🔄 *Renew Key*\n\nNo keys found on `{alias}`.",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Back Servers", callback_data="rflow|servers")],
                [InlineKeyboardButton("❎ Close", callback_data="rflow|close")],
            ]),
        )
        return

    keys.sort(key=lambda item: str(item.key_id))
    await query.edit_message_text(
        f"🔄 *Renew Key*\n\nSelect a key on `{alias}`.",
        parse_mode='Markdown',
        reply_markup=_renew_keys_keyboard(alias, keys),
    )


async def _show_renew_action_picker(query, alias: str, key_id: str):
    snapshot, error_text = _build_renew_key_snapshot(alias, key_id)
    if error_text:
        await query.edit_message_text(error_text, parse_mode='Markdown')
        return

    await query.edit_message_text(
        _render_renew_key_snapshot_text(
            snapshot,
            "Choose what to renew.\n"
            "Quota Only keeps the current expiry unchanged.\n"
            "Quota + Expiry will also set a new expiry from now.",
        ),
        parse_mode='Markdown',
        reply_markup=_renew_action_keyboard(alias, key_id),
    )


async def _show_renew_duration_picker(query, alias: str, key_id: str):
    snapshot, error_text = _build_renew_key_snapshot(alias, key_id)
    if error_text:
        await query.edit_message_text(error_text, parse_mode='Markdown')
        return

    await query.edit_message_text(
        _render_renew_key_snapshot_text(
            snapshot,
            "Choose the expiry extension first.\n"
            "After that, you will type the new quota manually.",
        ),
        parse_mode='Markdown',
        reply_markup=_renew_duration_keyboard(alias, key_id),
    )


async def _prompt_manual_renew_quota(query, context: ContextTypes.DEFAULT_TYPE, alias: str, key_id: str, days: int | None):
    snapshot, error_text = _build_renew_key_snapshot(alias, key_id)
    if error_text:
        await query.edit_message_text(error_text, parse_mode='Markdown')
        return

    context.user_data[PENDING_RENEW_KEY] = {
        "alias": alias,
        "key_id": str(key_id),
        "days": days,
        "actor_user_id": query.from_user.id if query.from_user else None,
        "actor_username": query.from_user.username if query.from_user else None,
    }

    footer = (
        "Send the new quota in your next message.\n"
        "Examples: `50`, `75.5`, or `unlimited`.\n"
        "Type `cancel` to abort."
    )
    if days is None:
        footer = "Quota-only renew selected. Expiry will stay unchanged.\n\n" + footer
    else:
        footer = f"Quota + Expiry selected. New expiry will be *+{days} days from now*.\n\n" + footer

    await query.edit_message_text(
        _render_renew_key_snapshot_text(snapshot, footer),
        parse_mode='Markdown',
        reply_markup=_renew_expiry_back_keyboard(alias, key_id),
    )


def _quota_bytes_from_gb(quota_gb: float) -> int:
    return int(quota_gb * BYTES_PER_GB)


async def renew_key_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /renew - open renew workflow with inline pickers and manual quota input."""
    if not update.message:
        return
    await _show_renew_server_picker(update, context)


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
    raw_used_bytes = _raw_used_bytes(target_key)
    used_gb = raw_used_bytes / BYTES_PER_GB
    limit_bytes = _display_limit_bytes(target_key, lifecycle)
    if limit_bytes:
        limit_gb = limit_bytes / BYTES_PER_GB
        usage_line = f"{used_gb:.2f} GB / {limit_gb:.2f} GB"
    elif _is_unlimited_config(lifecycle, target_key):
        usage_line = f"{used_gb:.2f} GB / Unlimited"
    else:
        usage_line = f"{used_gb:.2f} GB / Not set"

    expiry_at_utc = lifecycle.get("expiry_at_utc")
    can_renew = True
    expiry_state = "Expired" if lifecycle.get("is_expired") else "Active"
    expiry_line = f"{to_yangon_display(expiry_at_utc)} ({expiry_state})" if expiry_at_utc else "Not set"
    assigned_user_id = lifecycle.get("assigned_user_id")
    owner_line = _format_user_identity_markdown(int(assigned_user_id)) if assigned_user_id else "*Unassigned*"
    accounting = queries.get_key_accounting_totals(alias, str(key_id)) or {}
    lifetime_bought_line = _format_lifetime_bytes(
        accounting.get("total_purchased_bytes"),
        accounting.get("unlimited_grant_count"),
    )
    lifetime_used_line = _format_lifetime_bytes(accounting.get("total_consumed_bytes"))
    notice_line = f"{notice}\n\n" if notice else ""

    await query.edit_message_text(
        (
            f"{notice_line}"
            f"⚙️ *Manage Key*\n\n"
            f"Server: `{alias}`\n"
            f"Key ID: `{target_key.key_id}`\n"
            f"Name: *{_format_text_markdown(target_key.name, default='Unnamed')}*\n"
            f"Usage: {usage_line}\n"
            f"Expiry: *{expiry_line}*\n"
            f"Lifetime Bought: *{lifetime_bought_line}*\n"
            f"Lifetime Used: *{lifetime_used_line}*\n"
            f"Owner: {owner_line}\n\n"
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

    server_labels = {}
    for alias, server_data in servers.items():
        limit = int(server_data.get("max_key_count") or 0)
        limit_text = str(limit) if limit > 0 else "∞"
        current_count_text = "?"

        client = get_vpn_client(alias)
        if client:
            try:
                current_count_text = str(len(client.get_keys()))
            except Exception as e:
                logger.warning(f"Could not fetch key count for {alias}: {e}")

        server_labels[alias] = f"{alias} - {current_count_text}/{limit_text}"

    await close_active_inline_message(update, context)
    keyboard = get_server_list_keyboard(servers, prefix="listkeys", server_labels=server_labels)
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
        admin_username_map = {int(item["user_id"]): item.get("username") for item in queries.get_admin_profiles()}
        server_data = queries.get_server(alias)
        
        max_keys = int(server_data.get('max_key_count') or 0) if server_data else 0
        current_count = len(keys)
        ratio_limit = str(max_keys) if max_keys > 0 else "∞"
        limit_text = f" (Max: {max_keys})" if max_keys > 0 else " (No Limit)"
        msg = f"🔑 *Keys for {alias}*{limit_text}: Current *{current_count}/{ratio_limit}*\n\n"
        
        if not keys:
            msg += "No keys found on this server."
            await query.edit_message_text(msg, parse_mode='Markdown')
            if query.message:
                clear_if_matches(context, query.message.message_id)
            return

        for key in keys:
            raw_used_bytes = _raw_used_bytes(key)
            used_gb = raw_used_bytes / BYTES_PER_GB
            limit_str = f"{limit_gb:.2f} GB" if isinstance(limit_gb, float) else limit_gb
            lifecycle = queries.get_key_lifecycle(alias, str(key.key_id)) or {}
            limit_bytes = _display_limit_bytes(key, lifecycle)
            limit_gb = limit_bytes / BYTES_PER_GB if limit_bytes else ("Unlimited" if _is_unlimited_config(lifecycle, key) else "Not Set")
            limit_str = f"{limit_gb:.2f} GB" if isinstance(limit_gb, float) else limit_gb
            status_value = _status_line_for_display(str(key.key_id), sold_keys, lifecycle, raw_used_bytes, limit_bytes)
            status_tag = {
                "⛔ EXPIRED": "⛔ [EXPIRED]",
                "⛔ QUOTA BLOCKED": "⛔ [QUOTA BLOCKED]",
                "🟠 USED UP": "🟠 [USED UP]",
                "🔴 SOLD": "🔴 [SOLD]",
            }.get(status_value, "🟢 [AVAILABLE]")

            expiry_at_utc = lifecycle.get("expiry_at_utc")
            expiry_tag = "⛔ EXPIRED" if lifecycle.get("is_expired") else "✅ ACTIVE"
            expiry_text = to_yangon_display(expiry_at_utc) if expiry_at_utc else "Not set"
            assigned_user_id = lifecycle.get("assigned_user_id")
            if assigned_user_id:
                try:
                    owner_user_id = int(assigned_user_id)
                except (TypeError, ValueError):
                    owner_user_id = assigned_user_id
                owner_username = admin_username_map.get(int(owner_user_id)) if str(owner_user_id).isdigit() else None
                if not owner_username:
                    customer = queries.get_customer(int(owner_user_id)) if str(owner_user_id).isdigit() else None
                    owner_username = customer.get("username") if customer else None
                owner_id_line = _format_user_identity_markdown(int(owner_user_id)) if str(owner_user_id).isdigit() else f"`{owner_user_id}`"
                owner_username_line = _format_username_markdown(owner_username) if owner_username else "*Unknown*"
            else:
                owner_id_line = "*Unassigned*"
                owner_username_line = "*Unassigned*"

            key_id_str = str(key.key_id)
            creator_username = key_creators.get(key_id_str)
            creator_tag = f" | By: {_format_username_markdown(creator_username)}" if creator_username else ""
            key_name_text = _format_text_markdown(key.name, default="Unnamed")
            
            msg += f"ID: `{key.key_id}` | Name: *{key_name_text}* {status_tag}{creator_tag}\n"
            if isinstance(limit_gb, float):
                msg += f"Usage: {used_gb:.2f} GB / {limit_gb:.2f} GB\n"
            else:
                msg += f"Usage: {used_gb:.2f} GB / Unlimited\n"
            msg += f"Expiry: {expiry_text} ({expiry_tag})\n"
            msg += f"Owner: {owner_id_line}\n"
            msg += f"Owner Username: {owner_username_line}\n"
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
    raw_used_bytes = _raw_used_bytes(target_key)
    used_gb = raw_used_bytes / BYTES_PER_GB
    limit_bytes = _display_limit_bytes(target_key, lifecycle)
    if limit_bytes:
        limit_gb = limit_bytes / BYTES_PER_GB
        usage_line = f"{used_gb:.2f} GB / {limit_gb:.2f} GB"
    else:
        usage_line = f"{used_gb:.2f} GB / Unlimited"

    expiry_at_utc = lifecycle.get("expiry_at_utc")
    expiry_state = "Expired" if lifecycle.get("is_expired") else "Active"
    expiry_line = f"{to_yangon_display(expiry_at_utc)} ({expiry_state})" if expiry_at_utc else "Not set"
    assigned_user_id = lifecycle.get("assigned_user_id")
    owner_line = _format_user_identity_markdown(int(assigned_user_id)) if assigned_user_id else "*Unassigned*"

    await close_active_inline_message(update, context)
    can_renew = True
    keyboard = get_key_management_keyboard(alias, key_id, is_sold, can_renew)
    sent = await update.message.reply_text(
        (
            f"⚙️ *Manage Key*\n\n"
            f"Server: `{alias}`\n"
            f"Key ID: `{target_key.key_id}`\n"
            f"Name: *{_format_text_markdown(target_key.name, default='Unnamed')}*\n"
            f"Usage: {usage_line}\n"
            f"Expiry: *{expiry_line}*\n"
            f"Owner: {owner_line}"
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
        actor_user_id = update.effective_user.id if update.effective_user else None
        if not _can_manage_account(actor_user_id, user_id):
            await query.answer("You can only manage your own staff account.", show_alert=True)
            return

        customer = queries.get_customer(user_id)
        is_privileged = _is_privileged_account(user_id)
        if (not is_privileged) and (not customer or (customer.get("status") or "").lower() != "approved"):
            await query.answer("User is not approved anymore.", show_alert=True)
            await _show_manage_users_panel(update, context, edit=True)
            return

        admin_profiles = {int(item["user_id"]): item for item in queries.get_admin_profiles()}
        username = (customer.get("username") if customer else None) or admin_profiles.get(user_id, {}).get("username")
        uname = _format_username_markdown(username)
        first_name = _format_text_markdown((customer.get("first_name") if customer else None), default="N/A")
        role_line = "👑 OWNER" if user_id == OWNER_ID else "🛡️ ADMIN" if user_id in queries.get_admins() else "👤 APPROVED USER"
        assigned = queries.get_user_assigned_keys(user_id)
        await query.edit_message_text(
            (
                "👤 *Manage Approved User*\n\n"
                f"User: {_format_user_identity_markdown(user_id)}\n"
                f"Role: *{role_line}*\n"
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
        actor_user_id = update.effective_user.id if update.effective_user else None
        if not _can_manage_account(actor_user_id, user_id):
            await query.answer("You can only manage your own staff account.", show_alert=True)
            return

        servers = queries.get_servers()
        if not servers:
            await query.answer("No servers configured.", show_alert=True)
            return
        await query.edit_message_text(
            f"➕ *Assign Key*\n\nChoose a server for user {_format_user_identity_markdown(user_id)}.",
            parse_mode='Markdown',
            reply_markup=_umgr_assign_servers_keyboard(user_id, servers),
        )
        return

    if action == "srv" and len(parts) == 4:
        await query.answer()
        user_id = int(parts[2])
        actor_user_id = update.effective_user.id if update.effective_user else None
        if not _can_manage_account(actor_user_id, user_id):
            await query.answer("You can only manage your own staff account.", show_alert=True)
            return

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
                    f"User: {_format_user_identity_markdown(user_id)}\n"
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
                f"User: {_format_user_identity_markdown(user_id)}\n"
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
        actor_user_id = update.effective_user.id if update.effective_user else None
        if not _can_manage_account(actor_user_id, user_id):
            await query.answer("You can only manage your own staff account.", show_alert=True)
            return

        alias = parts[3]
        key_id = parts[4]
        client = get_vpn_client(alias)
        if not client:
            await query.answer("Could not connect to server.", show_alert=True)
            return

        customer = queries.get_customer(user_id)
        is_privileged = _is_privileged_account(user_id)
        if (not is_privileged) and (not customer or (customer.get("status") or "").lower() != "approved"):
            await query.answer("Target user is not approved.", show_alert=True)
            return

        try:
            keys = client.get_keys()
        except Exception as e:
            logger.error(f"Assignment grant fetch error on {alias}: {e}")
            await query.answer("Failed to fetch key details.", show_alert=True)
            return

        live_key = next((item for item in keys if str(item.key_id) == str(key_id)), None)
        if not live_key:
            await query.answer("Key not found on server.", show_alert=True)
            return

        queries.set_key_assignment(alias, key_id, user_id)
        lifecycle = queries.get_key_lifecycle(alias, key_id) or {}
        quota_bytes, is_unlimited = _assignment_sale_grant_params(lifecycle, live_key)
        queries.record_assignment_sale_grant(
            alias,
            key_id,
            user_id,
            quota_bytes,
            is_unlimited=is_unlimited,
            metadata={
                "source": "manage_user_flow",
                "assigned_by_user_id": update.effective_user.id if update.effective_user else None,
                "assigned_by_username": update.effective_user.username if update.effective_user else None,
            },
        )
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
                f"User: {_format_user_identity_markdown(user_id)}\n"
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
        actor_user_id = update.effective_user.id if update.effective_user else None
        if not _can_manage_account(actor_user_id, user_id):
            await query.answer("You can only manage your own staff account.", show_alert=True)
            return

        assigned = queries.get_user_assigned_keys(user_id)
        if not assigned:
            await query.edit_message_text(
                f"➖ *Unassign Key*\n\nNo keys currently assigned to user {_format_user_identity_markdown(user_id)}.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back User", callback_data=f"umgr|user|{user_id}")],
                     [InlineKeyboardButton("❎ Close", callback_data="umgr|close")]]
                ),
            )
            return

        await query.edit_message_text(
            f"➖ *Unassign Key*\n\nSelect a key to unassign from user {_format_user_identity_markdown(user_id)}.",
            parse_mode='Markdown',
            reply_markup=_umgr_unassign_keyboard(user_id, assigned),
        )
        return

    if action == "unas" and len(parts) == 5:
        await query.answer()
        user_id = int(parts[2])
        actor_user_id = update.effective_user.id if update.effective_user else None
        if not _can_manage_account(actor_user_id, user_id):
            await query.answer("You can only manage your own staff account.", show_alert=True)
            return

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
                f"User: {_format_user_identity_markdown(user_id)}\n"
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
            key_name = _format_text_markdown(target_key.name, default="Unnamed")
            key_creators = queries.get_key_creators(alias)
            creator_username = key_creators.get(str(target_key.key_id))
            creator_line = f"Generated By: *{_format_username_markdown(creator_username)}*" if creator_username else "Generated By: *Unknown*"
            lifecycle = queries.get_key_lifecycle(alias, str(key_id)) or {}
            expiry_at_utc = lifecycle.get("expiry_at_utc")
            expiry_text = to_yangon_display(expiry_at_utc) if expiry_at_utc else "Not set"
            owner_user_id = lifecycle.get("assigned_user_id")
            owner_line = _format_user_identity_markdown(int(owner_user_id)) if owner_user_id else "*Unassigned*"
            renew_count = lifecycle.get("renew_count") or 0
            raw_used_bytes = _raw_used_bytes(target_key)
            used_gb = raw_used_bytes / BYTES_PER_GB
            limit_bytes = _display_limit_bytes(target_key, lifecycle)
            if limit_bytes:
                limit_gb = limit_bytes / BYTES_PER_GB
                available_bytes = 0 if _is_quota_blocked(lifecycle) else max(limit_bytes - raw_used_bytes, 0)
                available_gb = available_bytes / BYTES_PER_GB
                usage_line = f"Available Usage: *{available_gb:.2f} GB* (Used: {used_gb:.2f} GB / {limit_gb:.2f} GB)"
            elif _is_unlimited_config(lifecycle, target_key):
                usage_line = f"Available Usage: *Unlimited* (Used: {used_gb:.2f} GB / Unlimited)"
            else:
                usage_line = f"Available Usage: *Not set* (Used: {used_gb:.2f} GB / Not set)"

            await query.message.reply_text(
                (
                    f"🔑 *Key Access URL*\n\n"
                    f"Server: `{alias}`\n"
                    f"Key ID: `{key_id}`\n"
                    f"Name: *{key_name}*\n"
                    f"{creator_line}\n"
                    f"{usage_line}\n"
                    f"Expiry: *{expiry_text}*\n"
                    f"Owner: {owner_line}\n"
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

        lifecycle = queries.get_key_lifecycle(alias, str(key_id)) or {}
        raw_used_bytes = _raw_used_bytes(target_key)
        used_gb = raw_used_bytes / BYTES_PER_GB
        limit_bytes = _display_limit_bytes(target_key, lifecycle)
        if limit_bytes:
            limit_gb = limit_bytes / BYTES_PER_GB
            usage_line = f"{used_gb:.2f} GB / {limit_gb:.2f} GB"
        elif _is_unlimited_config(lifecycle, target_key):
            usage_line = f"{used_gb:.2f} GB / Unlimited"
        else:
            usage_line = f"{used_gb:.2f} GB / Not set"

        sold_keys = queries.get_sold_keys(alias)
        status_tag = _status_line_for_display(str(key_id), sold_keys, lifecycle, raw_used_bytes, limit_bytes)

        keyboard = get_delete_confirmation_keyboard(alias, key_id)
        expiry_at_utc = lifecycle.get("expiry_at_utc")
        expiry_text = to_yangon_display(expiry_at_utc) if expiry_at_utc else "Not set"
        owner_user_id = lifecycle.get("assigned_user_id")
        owner_line = _format_user_identity_markdown(int(owner_user_id)) if owner_user_id else "*Unassigned*"
        renew_count = lifecycle.get("renew_count") or 0
        await query.answer("Confirm delete to proceed.", show_alert=True)
        await query.edit_message_text(
            (
                f"⚠️ *Delete Confirmation*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n"
                f"Name: *{_format_text_markdown(target_key.name, default='Unnamed')}*\n"
                f"Usage: {usage_line}\n"
                f"Expiry: *{expiry_text}*\n"
                f"Owner: {owner_line}\n"
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
                f"⏳ *Set Expiry Only*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n"
                f"Current Expiry: *{expiry_line}*\n\n"
                "Use this only when you want to change expiry without renewing quota."
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
        await _show_renew_action_picker(query, alias, str(key_id))

    elif action in {"rnd30", "rnd90", "rnd180", "rnd360"}:
        await query.answer()
        day_map = {
            "rnd30": 30,
            "rnd90": 90,
            "rnd180": 180,
            "rnd360": 360,
        }
        days = day_map[action]
        await _prompt_manual_renew_quota(query, context, alias, str(key_id), days)

    elif action == "rncancel":
        await query.answer("Cancelled.")
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


async def handle_renew_workflow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Button-driven renew workflow used by /renew and renew action shortcuts."""
    query = update.callback_query
    if not query or not query.data:
        return

    parts = query.data.split("|")
    if len(parts) < 2 or parts[0] != "rflow":
        return

    await query.answer()
    action = parts[1]

    if action == "close":
        await query.edit_message_text("✅ Renew panel closed.")
        if query.message:
            clear_if_matches(context, query.message.message_id)
        return

    if action == "servers":
        await _show_renew_server_picker(update, context, edit=True)
        return

    if action == "srv" and len(parts) == 3:
        await _show_renew_key_picker(query, parts[2])
        return

    if action == "key" and len(parts) == 4:
        await _show_renew_action_picker(query, parts[2], parts[3])
        return

    if action == "quotaonly" and len(parts) == 4:
        await _prompt_manual_renew_quota(query, context, parts[2], parts[3], None)
        return

    if action == "expiry" and len(parts) == 4:
        await _show_renew_duration_picker(query, parts[2], parts[3])
        return

    if action == "dur" and len(parts) == 5:
        await _prompt_manual_renew_quota(query, context, parts[2], parts[3], int(parts[4]))
        return

    await query.answer("Unknown renew action.", show_alert=True)


@admin_only
async def handle_manual_renew_quota_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Final text input step for renew flow: receives manual quota with optional expiry extension."""
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
    days = pending.get("days")
    actor_username = pending.get("actor_username")

    if text == "cancel":
        context.user_data.pop(PENDING_RENEW_KEY, None)
        await update.message.reply_text(
            f"✅ Renew cancelled for key `{key_id}` on `{alias}`.",
            parse_mode='Markdown'
        )
        return

    if text in {"unlimited", "limitless", "nolimit", "no limit"}:
        quota_gb = 0.0
    else:
        if text == "0":
            await update.message.reply_text(
                "⚠️ To renew as unlimited, type `unlimited` instead of `0`.",
                parse_mode='Markdown'
            )
            return
        try:
            quota_gb = float(text)
            if quota_gb < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid quota. Enter a positive number in GB, `unlimited`, or `cancel`.",
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

    lifecycle = queries.get_key_lifecycle(alias, str(key_id)) or {}
    current_expiry_utc = lifecycle.get("expiry_at_utc")
    current_auto_disabled_at_utc = lifecycle.get("auto_disabled_at_utc")

    try:
        live_keys = client.get_keys()
        live_key = next((item for item in live_keys if str(item.key_id) == str(key_id)), None)
        if not live_key:
            await update.message.reply_text(
                f"❌ Key `{key_id}` was not found on `{alias}` during renew.",
                parse_mode='Markdown'
            )
            context.user_data.pop(PENDING_RENEW_KEY, None)
            return
        baseline_used_bytes = int(live_key.used_bytes or 0)

        if quota_gb == 0:
            client.delete_data_limit(key_id)
        else:
            client.add_data_limit(key_id, baseline_used_bytes + _quota_bytes_from_gb(quota_gb))

        now_utc = utc_now_iso()
        new_expiry_utc = current_expiry_utc
        if days is not None:
            new_expiry_utc = add_days_from_utc(now_utc, int(days))
            queries.set_key_expiry(alias, key_id, new_expiry_utc)

        queries.record_key_renewal(
            alias,
            key_id,
            quota_gb,
            renewed_at_utc=now_utc,
            baseline_used_bytes=baseline_used_bytes,
        )
        assigned_user_id = int(lifecycle["assigned_user_id"]) if lifecycle.get("assigned_user_id") else None
        queries.record_key_data_grant(
            alias,
            key_id,
            _quota_bytes_from_gb(quota_gb) if quota_gb > 0 else 0,
            customer_user_id=assigned_user_id,
            is_renewal=True,
            is_unlimited=quota_gb == 0,
            created_at_utc=now_utc,
            metadata={
                "source": "manual_renew",
                "days": days,
                "renewed_by_user_id": user.id,
                "renewed_by_username": actor_username or user.username,
            },
        )

        if days is None:
            expiry_dt = parse_utc_iso(current_expiry_utc)
            now_dt = parse_utc_iso(now_utc)
            if expiry_dt and now_dt and expiry_dt <= now_dt:
                queries.mark_key_expired(alias, key_id, current_auto_disabled_at_utc)
            else:
                queries.clear_key_expired(alias, key_id)

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
                "source": "manual_quota_flow",
            },
            created_at_utc=now_utc,
        )

        quota_text = "Unlimited" if quota_gb == 0 else f"{quota_gb:.2f} GB"
        expiry_text = "Unchanged" if days is None else to_yangon_display(new_expiry_utc)
        policy_text = "Quota only" if days is None else f"Quota + expiry (+{int(days)} days from now)"
        await update.message.reply_text(
            (
                "✅ *Renew Completed*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n"
                f"New Quota: *{quota_text}*\n"
                f"Expiry: *{expiry_text}*\n"
                f"Mode: *{policy_text}*"
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

    client = get_vpn_client(alias)
    if not client:
        context.user_data.pop(PENDING_ASSIGN_KEY, None)
        await update.message.reply_text(
            f"❌ Could not connect to server `{alias}`.",
            parse_mode='Markdown'
        )
        return

    try:
        live_keys = client.get_keys()
        live_key = next((item for item in live_keys if str(item.key_id) == str(key_id)), None)
        if not live_key:
            await update.message.reply_text(
                f"❌ Key `{key_id}` was not found on `{alias}` during assignment.",
                parse_mode='Markdown'
            )
            context.user_data.pop(PENDING_ASSIGN_KEY, None)
            return

        queries.set_key_assignment(alias, key_id, target_user_id)
        lifecycle = queries.get_key_lifecycle(alias, key_id) or {}
        quota_bytes, is_unlimited = _assignment_sale_grant_params(lifecycle, live_key)
        queries.record_assignment_sale_grant(
            alias,
            key_id,
            target_user_id,
            quota_bytes,
            is_unlimited=is_unlimited,
            metadata={
                "source": "manual_assign_flow",
                "assigned_by_user_id": user.id,
                "assigned_by_username": actor_username or user.username,
            },
        )
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
                f"Assigned User: {_format_user_identity_markdown(target_user_id)}"
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