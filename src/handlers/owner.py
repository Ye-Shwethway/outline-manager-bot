import logging
import os
import time
import asyncio
from telegram import Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown
from telegram.ext import ContextTypes
from src.config import OWNER_ID
from src.utils.decorators import owner_only, admin_only
from src.database import queries
from src.services.backup_service import generate_backup_file, get_latest_backup_file
from src.services.notifier import monitor_used_up_keys
from src.services.outline_api import get_vpn_client
from src.utils.inline_messages import clear_if_matches
from src.utils.datetime_utils import to_utc_display

logger = logging.getLogger(__name__)
REVIEW_MODE_BROADCAST_COOLDOWN_SECONDS = 120
BYTES_PER_GB = 1_000_000_000


def _format_username_markdown(username: str | None, default: str = "(username unavailable)") -> str:
    if username:
        return escape_markdown(f"@{username}", version=1)
    return default

def _strip_outline_label(value: str, label: str) -> str:
    """Accept values copied from access.txt lines such as `apiUrl:...`."""
    prefix = f"{label}:"
    return value[len(prefix):].strip() if value.startswith(prefix) else value.strip()


def _keyusage_servers_keyboard(servers: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🌐 {alias}", callback_data=f"kdiag|srv|{alias}")]
        for alias in sorted(servers.keys())
    ]
    rows.append([InlineKeyboardButton("❎ Close", callback_data="kdiag|close")])
    return InlineKeyboardMarkup(rows)


def _keyusage_keys_keyboard(alias: str, keys: list) -> InlineKeyboardMarkup:
    rows = []
    for key in keys[:30]:
        key_name = str(key.name or "Unnamed")[:18]
        rows.append(
            [
                InlineKeyboardButton(
                    f"🔑 {key.key_id} | {key_name}",
                    callback_data=f"kdiag|key|{alias}|{key.key_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back Servers", callback_data="kdiag|servers"),
            InlineKeyboardButton("❎ Close", callback_data="kdiag|close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _keyusage_result_keyboard(alias: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Back Keys", callback_data=f"kdiag|srv|{alias}")],
            [InlineKeyboardButton("🌐 Back Servers", callback_data="kdiag|servers")],
            [InlineKeyboardButton("❎ Close", callback_data="kdiag|close")],
        ]
    )


def _key_accounting_servers_keyboard(servers: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🌐 {alias}", callback_data=f"kacct|srv|{alias}")]
        for alias in sorted(servers.keys())
    ]
    rows.append([InlineKeyboardButton("❎ Close", callback_data="kacct|close")])
    return InlineKeyboardMarkup(rows)


def _key_accounting_keys_keyboard(alias: str, keys: list) -> InlineKeyboardMarkup:
    rows = []
    for key in keys[:30]:
        key_name = str(key.name or "Unnamed")[:18]
        rows.append(
            [
                InlineKeyboardButton(
                    f"🔑 {key.key_id} | {key_name}",
                    callback_data=f"kacct|key|{alias}|{key.key_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back Servers", callback_data="kacct|servers"),
            InlineKeyboardButton("❎ Close", callback_data="kacct|close"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _key_accounting_result_keyboard(alias: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⬅️ Back Keys", callback_data=f"kacct|srv|{alias}")],
            [InlineKeyboardButton("🌐 Back Servers", callback_data="kacct|servers")],
            [InlineKeyboardButton("❎ Close", callback_data="kacct|close")],
        ]
    )


def _user_accounting_users_keyboard(users: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for item in users[:30]:
        user_id = int(item["user_id"])
        username = item.get("username") or "no-username"
        rows.append(
            [
                InlineKeyboardButton(
                    f"👤 {user_id} | @{str(username)[:18]}",
                    callback_data=f"uacct|user|{user_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("❎ Close", callback_data="uacct|close")])
    return InlineKeyboardMarkup(rows)


def _user_accounting_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 Back Users", callback_data="uacct|users")],
            [InlineKeyboardButton("❎ Close", callback_data="uacct|close")],
        ]
    )


def _loyalty_metric_keyboard(selected_metric: str | None = None) -> InlineKeyboardMarkup:
    labels = [
        ("buyers", "💰 Top Buyers"),
        ("used", "📈 Top Consumers"),
        ("renewals", "🔁 Top Renewers"),
    ]
    row = []
    for metric, label in labels:
        display = f"• {label}" if metric == selected_metric else label
        row.append(InlineKeyboardButton(display, callback_data=f"loyal|metric|{metric}"))
    return InlineKeyboardMarkup([
        row,
        [InlineKeyboardButton("❎ Close", callback_data="loyal|close")],
    ])


def _format_accounting_bytes(total_bytes: int | None, unlimited_count: int | None = 0) -> str:
    bytes_value = max(int(total_bytes or 0), 0)
    unlimited_value = max(int(unlimited_count or 0), 0)
    if bytes_value <= 0 and unlimited_value <= 0:
        return "0.00 GB"
    if unlimited_value <= 0:
        return f"{bytes_value / BYTES_PER_GB:.2f} GB"
    if bytes_value <= 0:
        return f"Unlimited x{unlimited_value}"
    return f"{bytes_value / BYTES_PER_GB:.2f} GB + Unlimited x{unlimited_value}"


async def _build_key_accounting_text(alias: str, key_id: str) -> str | None:
    client = get_vpn_client(alias)
    if not client:
        return None

    keys = client.get_keys()
    target_key = next((key for key in keys if str(key.key_id) == str(key_id)), None)
    if not target_key:
        return None

    totals = queries.get_key_accounting_totals(alias, str(key_id)) or {}
    events = queries.get_service_accounting_events(server_alias=alias, key_id=str(key_id), limit=8)
    key_name = escape_markdown(str(target_key.name or 'Unnamed'), version=1)
    owner_user_id = totals.get('current_assigned_user_id')
    owner_line = f"`{owner_user_id}`" if owner_user_id else "*Unassigned*"

    lines = [
        "📊 *Key Accounting Diagnostic*",
        "",
        f"Server: `{alias}`",
        f"Key ID: `{key_id}`",
        f"Name: *{key_name}*",
        f"Current Owner: {owner_line}",
        f"Lifetime Bought: *{escape_markdown(_format_accounting_bytes(totals.get('total_purchased_bytes'), totals.get('unlimited_grant_count')), version=1)}*",
        f"Lifetime Used: *{escape_markdown(_format_accounting_bytes(totals.get('total_consumed_bytes')), version=1)}*",
        f"Purchase Events: *{int(totals.get('purchase_event_count') or 0)}*",
        f"Renewal Events: *{int(totals.get('renewal_event_count') or 0)}*",
        f"Last Grant UTC: *{escape_markdown(to_utc_display(totals.get('last_grant_at_utc')), version=1)}*",
        f"Last Usage UTC: *{escape_markdown(to_utc_display(totals.get('last_consumed_at_utc')), version=1)}*",
        "",
        "*Recent Accounting Events*",
    ]

    if not events:
        lines.append("- none")
    else:
        for event in events:
            purchased_text = escape_markdown(
                _format_accounting_bytes(event.get('purchased_bytes'), 1 if event.get('is_unlimited') else 0),
                version=1,
            )
            consumed_text = escape_markdown(_format_accounting_bytes(event.get('consumed_bytes')), version=1)
            event_type = escape_markdown(str(event.get('event_type') or 'unknown'), version=1)
            created = escape_markdown(to_utc_display(event.get('created_at_utc')), version=1)
            customer_user_id = event.get('customer_user_id')
            customer_line = f" | User: `{customer_user_id}`" if customer_user_id else ""
            lines.append(
                f"- *{event_type}* | Bought: *{purchased_text}* | Used: *{consumed_text}*{customer_line} | {created}"
            )

    return "\n".join(lines)


def _accounting_subjects() -> list[dict]:
    unique_users: dict[int, dict] = {
        OWNER_ID: {"user_id": OWNER_ID, "username": None},
    }
    for item in queries.get_admin_profiles():
        unique_users[int(item["user_id"])] = dict(item)
    for item in (
        queries.get_customers_by_status("pending")
        + queries.get_customers_by_status("approved")
        + queries.get_customers_by_status("rejected")
    ):
        unique_users[int(item["user_id"])] = {
            "user_id": int(item["user_id"]),
            "username": item.get("username"),
        }
    return sorted(unique_users.values(), key=lambda item: int(item["user_id"]))


def _format_customer_accounting_text(user_id: int) -> str:
    totals = queries.get_customer_accounting_totals(user_id) or {}
    events = queries.get_service_accounting_events(customer_user_id=user_id, limit=10)
    customer = queries.get_customer(user_id)
    username = customer.get("username") if customer else None
    admin_map = {int(item["user_id"]): item for item in queries.get_admin_profiles()}
    if not username:
        username = admin_map.get(user_id, {}).get("username")

    role = "OWNER" if user_id == OWNER_ID else "ADMIN" if user_id in queries.get_admins() else "USER"
    username_line = _format_username_markdown(username, default="(no username)")
    lines = [
        "👤 *User Accounting Diagnostic*",
        "",
        f"User ID: `{user_id}`",
        f"Role: *{role}*",
        f"Username: {username_line}",
        f"Lifetime Bought: *{escape_markdown(_format_accounting_bytes(totals.get('total_purchased_bytes'), totals.get('unlimited_grant_count')), version=1)}*",
        f"Lifetime Used: *{escape_markdown(_format_accounting_bytes(totals.get('total_consumed_bytes')), version=1)}*",
        f"Purchase Events: *{int(totals.get('purchase_event_count') or 0)}*",
        f"Renewal Events: *{int(totals.get('renewal_event_count') or 0)}*",
        f"First Recorded UTC: *{escape_markdown(to_utc_display(totals.get('first_recorded_at_utc')), version=1)}*",
        f"Last Grant UTC: *{escape_markdown(to_utc_display(totals.get('last_grant_at_utc')), version=1)}*",
        f"Last Usage UTC: *{escape_markdown(to_utc_display(totals.get('last_consumed_at_utc')), version=1)}*",
        "",
        "*Recent Accounting Events*",
    ]

    if not events:
        lines.append("- none")
    else:
        for event in events:
            purchased_text = escape_markdown(
                _format_accounting_bytes(event.get('purchased_bytes'), 1 if event.get('is_unlimited') else 0),
                version=1,
            )
            consumed_text = escape_markdown(_format_accounting_bytes(event.get('consumed_bytes')), version=1)
            event_type = escape_markdown(str(event.get('event_type') or 'unknown'), version=1)
            created = escape_markdown(to_utc_display(event.get('created_at_utc')), version=1)
            lines.append(
                f"- *{event_type}* | Key: `{event.get('server_alias')}` / `{event.get('key_id')}` | Bought: *{purchased_text}* | Used: *{consumed_text}* | {created}"
            )
    return "\n".join(lines)


def _resolve_subject_profile(user_id: int) -> tuple[str, str]:
    customer = queries.get_customer(user_id)
    username = customer.get("username") if customer else None
    admin_map = {int(item["user_id"]): item for item in queries.get_admin_profiles()}
    if not username:
        username = admin_map.get(user_id, {}).get("username")
    role = "OWNER" if user_id == OWNER_ID else "ADMIN" if user_id in queries.get_admins() else "USER"
    return role, _format_username_markdown(username, default="(no username)")


def _build_loyalty_text(metric: str) -> str:
    metric_title = {
        'buyers': '💰 *Top Buyers*',
        'used': '📈 *Top Consumers*',
        'renewals': '🔁 *Top Renewers*',
    }.get(metric, '💰 *Top Buyers*')
    query_metric = 'bought' if metric == 'buyers' else metric
    rows = queries.get_customer_accounting_leaderboard(query_metric, limit=10)

    lines = [
        "🏅 *Customer Loyalty Leaderboard*",
        "",
        metric_title,
    ]

    if not rows:
        lines.append("- No customer accounting totals recorded yet.")
        lines.append("")
        lines.append("Choose another leaderboard below.")
        return "\n".join(lines)

    for index, item in enumerate(rows, start=1):
        user_id = int(item['user_id'])
        role, username_line = _resolve_subject_profile(user_id)
        bought_line = escape_markdown(
            _format_accounting_bytes(item.get('total_purchased_bytes'), item.get('unlimited_grant_count')),
            version=1,
        )
        used_line = escape_markdown(_format_accounting_bytes(item.get('total_consumed_bytes')), version=1)
        renewed_line = escape_markdown(_format_accounting_bytes(item.get('total_renewed_bytes')), version=1)
        renewal_count = int(item.get('renewal_event_count') or 0)

        lines.append(
            f"{index}. `{user_id}` | *{role}* | {username_line}\n"
            f"   Bought: *{bought_line}* | Used: *{used_line}* | Renewed: *{renewed_line}* | Renewals: *{renewal_count}*"
        )

    lines.append("")
    lines.append("Choose another leaderboard below.")
    return "\n".join(lines)


async def _build_keyusage_diagnostic_text(alias: str, key_id: str) -> str | None:
    client = get_vpn_client(alias)
    if not client:
        return None

    keys = client.get_keys()
    target_key = next((key for key in keys if str(key.key_id) == str(key_id)), None)
    if not target_key:
        return None

    raw_used_bytes = max(int(target_key.used_bytes or 0), 0)
    effective_used_bytes = queries.observe_key_usage(alias, str(key_id), raw_used_bytes)
    lifecycle = queries.get_key_lifecycle(alias, str(key_id)) or {}

    limit_bytes = int(target_key.data_limit or 0)
    raw_remaining_bytes = max(limit_bytes - raw_used_bytes, 0) if limit_bytes else 0
    effective_remaining_bytes = max(limit_bytes - effective_used_bytes, 0) if limit_bytes else 0
    key_name = escape_markdown(str(target_key.name or 'Unnamed'), version=1)

    if limit_bytes:
        limit_line = f"{limit_bytes / BYTES_PER_GB:.2f} GB"
        raw_remaining_line = f"{raw_remaining_bytes / BYTES_PER_GB:.2f} GB"
        effective_remaining_line = f"{effective_remaining_bytes / BYTES_PER_GB:.2f} GB"
    else:
        limit_line = "Unlimited"
        raw_remaining_line = "Unlimited"
        effective_remaining_line = "Unlimited"

    return (
        "🧪 *Key Usage Diagnostic*\n\n"
        f"Server: `{alias}`\n"
        f"Key ID: `{key_id}`\n"
        f"Name: *{key_name}*\n"
        f"Data Limit: *{limit_line}*\n\n"
        "*Live Outline Counter*\n"
        f"Raw Used: *{raw_used_bytes / BYTES_PER_GB:.2f} GB*\n"
        f"Raw Remaining: *{raw_remaining_line}*\n\n"
        "*Bot Effective Counter*\n"
        f"Effective Used: *{effective_used_bytes / BYTES_PER_GB:.2f} GB*\n"
        f"Effective Remaining: *{effective_remaining_line}*\n\n"
        "*Tracking State*\n"
        f"Last Observed Raw: *{int(lifecycle.get('last_observed_used_bytes') or 0) / BYTES_PER_GB:.2f} GB*\n"
        f"Reset Offset: *{int(lifecycle.get('usage_reset_offset_bytes') or 0) / BYTES_PER_GB:.2f} GB*\n"
        f"Max Effective Used: *{int(lifecycle.get('max_effective_used_bytes') or 0) / BYTES_PER_GB:.2f} GB*\n"
        f"Last Usage Sync UTC: *{escape_markdown(to_utc_display(lifecycle.get('last_usage_sync_at_utc')), version=1)}*"
    )


async def _notify_admins_review_mode_change(context: ContextTypes.DEFAULT_TYPE, enabled: bool) -> tuple[bool, int]:
    """Broadcast maintenance/live notice to admins when review notification routing is toggled."""
    now_ts = int(time.time())
    last_ts = int(context.application.bot_data.get("review_mode_broadcast_last_ts", 0))
    elapsed = now_ts - last_ts
    if elapsed < REVIEW_MODE_BROADCAST_COOLDOWN_SECONDS:
        return False, REVIEW_MODE_BROADCAST_COOLDOWN_SECONDS - elapsed

    admin_ids = queries.get_admins()
    if not admin_ids:
        context.application.bot_data["review_mode_broadcast_last_ts"] = now_ts
        return True, 0

    if enabled:
        text = (
            "✅ *Bot Back Online*\n\n"
            "Registration-review workflow is back to normal.\n"
            "You will receive new user registration review alerts again."
        )
    else:
        text = (
            "🛠️ *Bot Under Maintenance*\n\n"
            "Registration-review alerts are temporarily paused for admins.\n"
            "Owner is refining workflows; alerts will resume when maintenance ends."
        )

    for admin_id in admin_ids:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, parse_mode='Markdown')
        except Exception as e:
            logger.info(f"Could not notify admin {admin_id} for review mode change: {e}")

    context.application.bot_data["review_mode_broadcast_last_ts"] = now_ts
    return True, 0

@owner_only
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Usage: `/addadmin <user_id>`", parse_mode='Markdown')
        return
    try:
        user_id = int(context.args[0])
        username = None
        try:
            chat = await context.bot.get_chat(user_id)
            username = chat.username
        except Exception:
            # User may not have started bot yet or privacy restrictions may apply.
            pass

        if queries.add_admin(user_id, username):
            username_text = f" ({_format_username_markdown(username, default='')})" if username else ""
            await update.message.reply_text(f"✅ User `{user_id}`{username_text} added as Admin.", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"⚠️ User `{user_id}` is already an Admin.", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ User ID must be a number.")

@owner_only
async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Usage: `/removeadmin <user_id>`", parse_mode='Markdown')
        return
    try:
        user_id = int(context.args[0])
        queries.remove_admin(user_id)
        await update.message.reply_text(f"🗑️ Admin `{user_id}` removed.", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ User ID must be a number.")

@owner_only
async def list_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_profiles = queries.get_admin_profiles()
    if not admin_profiles:
        await update.message.reply_text("No admins found.")
        return

    lines = ["🛡️ *Current Admins:*"]
    for admin in admin_profiles:
        user_id = admin['user_id']
        username = admin.get('username')

        # Lazy-refresh missing usernames when possible.
        if not username:
            try:
                chat = await context.bot.get_chat(user_id)
                username = chat.username
                if username:
                    queries.update_admin_username(user_id, username)
            except Exception:
                username = None

        username_text = _format_username_markdown(username)
        lines.append(f"- `{user_id}` | {username_text}")

    msg = "\n".join(lines)
    await update.message.reply_text(msg, parse_mode='Markdown')

@owner_only
async def add_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3:
        await update.message.reply_text("Usage: `/addserver <alias> <api_url> <cert_sha256>`\nExample: `/addserver vps1 https://1.2.3.4:54321/xxx ABC123...`", parse_mode='Markdown')
        return
    
    alias, api_url, cert_sha256 = context.args
    api_url = _strip_outline_label(api_url, "apiUrl")
    cert_sha256 = _strip_outline_label(cert_sha256, "certSha256")

    if not api_url.startswith("http://") and not api_url.startswith("https://"):
        await update.message.reply_text("❌ Invalid API URL. It must start with http:// or https://")
        return

    # By default, max_key_count is 0 (which we will treat as unlimited)
    if queries.add_server(alias, api_url, cert_sha256):
        await update.message.reply_text(f"✅ Server `{alias}` added successfully.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"⚠️ Server alias `{alias}` already exists.", parse_mode='Markdown')

@owner_only
async def list_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /listserver - list configured server aliases and limits."""
    servers = queries.get_servers()
    if not servers:
        await update.message.reply_text("No servers configured yet.")
        return

    lines = ["🌐 *Configured Servers:*"]
    for alias, data in servers.items():
        limit = data.get("max_key_count", 0)
        limit_text = str(limit) if limit and limit > 0 else "Unlimited"
        url_flag = " ⚠️ Check URL" if str(data.get("api_url", "")).startswith("apiUrl:") else ""
        lines.append(f"- `{alias}` | Max Keys: *{limit_text}*{url_flag}")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

@owner_only
async def delete_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Usage: `/deleteserver <alias>`", parse_mode='Markdown')
        return
    
    alias = context.args[0]
    queries.remove_server(alias)
    await update.message.reply_text(f"🗑️ Server `{alias}` removed from database.", parse_mode='Markdown')

@owner_only
async def set_key_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: `/setkeylimit <server_alias> <max_number_of_keys>`\nExample: `/setkeylimit vps1 20`\n(Set to 0 for unlimited)", parse_mode='Markdown')
        return
    
    alias, limit_str = context.args
    try:
        limit = int(limit_str)
        if queries.update_server_limit(alias, limit):
            await update.message.reply_text(f"✅ Max key limit for `{alias}` set to *{limit}*.", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ Server `{alias}` not found.", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ Limit must be an integer.")

@admin_only
async def set_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /noti <on|off> - toggle per-user used-up key notifications."""
    user = update.effective_user
    if not user:
        await update.message.reply_text("❌ Could not determine your user id.")
        return

    if len(context.args) != 1:
        status = "ON" if queries.is_user_notification_enabled(user.id) else "OFF"
        await update.message.reply_text(
            f"Usage: `/noti <on|off>`\nYour notification status: *{status}*",
            parse_mode='Markdown',
        )
        return

    arg = context.args[0].strip().lower()
    if arg not in {"on", "off"}:
        await update.message.reply_text("❌ Invalid option. Use `/noti on` or `/noti off`.", parse_mode='Markdown')
        return

    enabled = arg == "on"
    queries.set_user_notification_enabled(user.id, enabled)
    state_text = "ON" if enabled else "OFF"
    await update.message.reply_text(f"🔔 Your notifications are now *{state_text}*.", parse_mode='Markdown')


@owner_only
async def set_review_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /reviewnoti <on|off> - owner controls if admins receive registration-review alerts."""
    if len(context.args) != 1:
        status = "ON" if queries.is_admin_registration_review_notifications_enabled() else "OFF"
        await update.message.reply_text(
            (
                "Usage: `/reviewnoti <on|off>`\n"
                f"Current admin review notification status: *{status}*\n"
                "When OFF, only owner receives new `/register` review alerts."
            ),
            parse_mode='Markdown',
        )
        return

    arg = context.args[0].strip().lower()
    if arg not in {"on", "off"}:
        await update.message.reply_text("❌ Invalid option. Use `/reviewnoti on` or `/reviewnoti off`.", parse_mode='Markdown')
        return

    enabled = arg == "on"
    current_enabled = queries.is_admin_registration_review_notifications_enabled()
    if enabled == current_enabled:
        status_text = "ON" if enabled else "OFF"
        scope_text = "Owner + Admins" if enabled else "Owner only"
        await update.message.reply_text(
            (
                f"ℹ️ Admin review notifications are already *{status_text}*.\n"
                f"Current recipients: *{scope_text}*."
            ),
            parse_mode='Markdown',
        )
        return

    queries.set_admin_registration_review_notifications_enabled(enabled)
    sent, remaining = await _notify_admins_review_mode_change(context, enabled)
    status_text = "ON" if enabled else "OFF"
    scope_text = "Owner + Admins" if enabled else "Owner only"
    cooldown_note = (
        ""
        if sent
        else f"\nAdmin broadcast skipped due to cooldown. Retry in ~{remaining}s if needed."
    )
    await update.message.reply_text(
        (
            f"📣 Registration-review notifications to admins are now *{status_text}*.\n"
            f"Current recipients: *{scope_text}*."
            f"{cooldown_note}"
        ),
        parse_mode='Markdown',
    )

@admin_only
async def scan_used_up_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /scan - run immediate used-up scan and notify recipients."""
    await update.message.reply_text("🔎 Running immediate key usage scan...")
    result = await monitor_used_up_keys(context)
    await update.message.reply_text(
        (
            "✅ Scan finished.\n"
            f"Servers scanned: *{result['servers_scanned']}*\n"
            f"Keys scanned: *{result['keys_scanned']}*\n"
            f"New alerts sent: *{result['alerts_sent']}*"
        ),
        parse_mode='Markdown',
    )

@admin_only
async def backup_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /backup - generate and send immediate manual backup file."""
    await update.message.reply_text("🗂️ Generating manual backup file...")
    try:
        file_path = generate_backup_file("manual")
    except Exception as e:
        logger.error(f"Manual backup failed: {e}")
        await update.message.reply_text("❌ Manual backup failed.")
        return

    with open(file_path, "rb") as backup_file:
        await update.message.reply_document(
            document=backup_file,
            filename=os.path.basename(file_path),
            caption="🗂️ Latest manual backup",
        )

@admin_only
async def get_last_auto_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /autobackup - send last generated automatic backup file."""
    file_path = get_latest_backup_file("auto")
    if not file_path:
        await update.message.reply_text("No automatic backup file found yet.")
        return

    with open(file_path, "rb") as backup_file:
        await update.message.reply_document(
            document=backup_file,
            filename=os.path.basename(file_path),
            caption="🗂️ Last auto backup",
        )


async def _restart_process_after_ack():
    await asyncio.sleep(1)
    os._exit(0)


@owner_only
async def restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /restart - restart bot process without touching persisted data."""
    if update.message:
        await update.message.reply_text(
            "♻️ Restarting bot process now...\n"
            "Data in database/backups will be preserved.",
            parse_mode='Markdown',
        )
    asyncio.create_task(_restart_process_after_ack())


@owner_only
async def key_usage_diagnostic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /keyusage <server_alias> <key_id> - inspect raw vs tracked usage for a key."""
    if not update.message:
        return

    if len(context.args) == 0:
        servers = queries.get_servers()
        if not servers:
            await update.message.reply_text("No servers configured yet.")
            return

        await update.message.reply_text(
            "🧪 *Key Usage Diagnostic*\n\nSelect a server to inspect usage counters.",
            parse_mode='Markdown',
            reply_markup=_keyusage_servers_keyboard(servers),
        )
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: `/keyusage` or `/keyusage <server_alias> <key_id>`",
            parse_mode='Markdown',
        )
        return

    alias, key_id = context.args
    try:
        text = await _build_keyusage_diagnostic_text(alias, key_id)
    except Exception as e:
        logger.error(f"Key usage diagnostic fetch error on {alias}: {e}")
        await update.message.reply_text("❌ Failed to fetch keys from Outline server.")
        return

    if not text:
        await update.message.reply_text(
            f"❌ Key `{key_id}` was not found on `{alias}` or the server is unreachable.",
            parse_mode='Markdown',
        )
        return

    await update.message.reply_text(text, parse_mode='Markdown')


@owner_only
async def key_accounting_diagnostic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if len(context.args) == 0:
        servers = queries.get_servers()
        if not servers:
            await update.message.reply_text("No servers configured yet.")
            return
        await update.message.reply_text(
            "📊 *Key Accounting Diagnostic*\n\nSelect a server to inspect lifetime accounting.",
            parse_mode='Markdown',
            reply_markup=_key_accounting_servers_keyboard(servers),
        )
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: `/keyaccounting` or `/keyaccounting <server_alias> <key_id>`",
            parse_mode='Markdown',
        )
        return

    alias, key_id = context.args
    try:
        text = await _build_key_accounting_text(alias, key_id)
    except Exception as e:
        logger.error(f"Key accounting diagnostic fetch error on {alias}/{key_id}: {e}")
        await update.message.reply_text("❌ Failed to fetch key accounting details.")
        return

    if not text:
        await update.message.reply_text(
            f"❌ Key `{key_id}` was not found on `{alias}` or the server is unreachable.",
            parse_mode='Markdown',
        )
        return

    await update.message.reply_text(text, parse_mode='Markdown')


@owner_only
async def user_accounting_diagnostic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if len(context.args) == 0:
        await update.message.reply_text(
            "👤 *User Accounting Diagnostic*\n\nSelect a user to inspect lifetime accounting.",
            parse_mode='Markdown',
            reply_markup=_user_accounting_users_keyboard(_accounting_subjects()),
        )
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "Usage: `/useraccounting` or `/useraccounting <user_id>`",
            parse_mode='Markdown',
        )
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ User ID must be a number.")
        return

    await update.message.reply_text(_format_customer_accounting_text(user_id), parse_mode='Markdown')


@owner_only
async def loyalty_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    metric = 'buyers'
    if len(context.args) == 1:
        arg = context.args[0].strip().lower()
        if arg in {'buyers', 'used', 'renewals'}:
            metric = arg

    await update.message.reply_text(
        _build_loyalty_text(metric),
        parse_mode='Markdown',
        reply_markup=_loyalty_metric_keyboard(metric),
    )


@owner_only
async def handle_key_usage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    parts = query.data.split("|")
    if len(parts) < 2 or parts[0] != "kdiag":
        return

    action = parts[1]
    if action == "close":
        await query.edit_message_text("✅ Key usage diagnostic panel closed.")
        if query.message:
            clear_if_matches(context, query.message.message_id)
        return

    if action == "servers":
        servers = queries.get_servers()
        if not servers:
            await query.edit_message_text("No servers configured yet.")
            return
        await query.edit_message_text(
            "🧪 *Key Usage Diagnostic*\n\nSelect a server to inspect usage counters.",
            parse_mode='Markdown',
            reply_markup=_keyusage_servers_keyboard(servers),
        )
        return

    if action == "srv" and len(parts) == 3:
        alias = parts[2]
        client = get_vpn_client(alias)
        if not client:
            await query.edit_message_text(
                f"❌ Could not connect to server `{alias}`.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 Back Servers", callback_data="kdiag|servers")],
                    [InlineKeyboardButton("❎ Close", callback_data="kdiag|close")],
                ]),
            )
            return
        try:
            keys = client.get_keys()
        except Exception as e:
            logger.error(f"Key usage diagnostic list error on {alias}: {e}")
            await query.edit_message_text(
                f"❌ Failed to fetch keys from `{alias}`.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 Back Servers", callback_data="kdiag|servers")],
                    [InlineKeyboardButton("❎ Close", callback_data="kdiag|close")],
                ]),
            )
            return

        if not keys:
            await query.edit_message_text(
                f"🧪 *Key Usage Diagnostic*\n\nNo keys found on `{alias}`.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 Back Servers", callback_data="kdiag|servers")],
                    [InlineKeyboardButton("❎ Close", callback_data="kdiag|close")],
                ]),
            )
            return

        await query.edit_message_text(
            f"🧪 *Key Usage Diagnostic*\n\nSelect a key on `{alias}`.",
            parse_mode='Markdown',
            reply_markup=_keyusage_keys_keyboard(alias, keys),
        )
        return

    if action == "key" and len(parts) == 4:
        alias = parts[2]
        key_id = parts[3]
        try:
            text = await _build_keyusage_diagnostic_text(alias, key_id)
        except Exception as e:
            logger.error(f"Key usage diagnostic detail error on {alias}/{key_id}: {e}")
            text = None
        if not text:
            await query.edit_message_text(
                f"❌ Key `{key_id}` was not found on `{alias}` or the server is unreachable.",
                parse_mode='Markdown',
                reply_markup=_keyusage_result_keyboard(alias),
            )
            return

        await query.edit_message_text(
            text,
            parse_mode='Markdown',
            reply_markup=_keyusage_result_keyboard(alias),
        )
        return

    await query.answer("Unknown diagnostic action.", show_alert=True)


@owner_only
async def handle_key_accounting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    parts = query.data.split("|")
    if len(parts) < 2 or parts[0] != "kacct":
        return

    action = parts[1]
    if action == "close":
        await query.edit_message_text("✅ Key accounting diagnostic panel closed.")
        if query.message:
            clear_if_matches(context, query.message.message_id)
        return

    if action == "servers":
        servers = queries.get_servers()
        if not servers:
            await query.edit_message_text("No servers configured yet.")
            return
        await query.edit_message_text(
            "📊 *Key Accounting Diagnostic*\n\nSelect a server to inspect lifetime accounting.",
            parse_mode='Markdown',
            reply_markup=_key_accounting_servers_keyboard(servers),
        )
        return

    if action == "srv" and len(parts) == 3:
        alias = parts[2]
        client = get_vpn_client(alias)
        if not client:
            await query.edit_message_text(
                f"❌ Could not connect to server `{alias}`.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 Back Servers", callback_data="kacct|servers")],
                    [InlineKeyboardButton("❎ Close", callback_data="kacct|close")],
                ]),
            )
            return
        try:
            keys = client.get_keys()
        except Exception as e:
            logger.error(f"Key accounting diagnostic list error on {alias}: {e}")
            await query.edit_message_text(
                f"❌ Failed to fetch keys from `{alias}`.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 Back Servers", callback_data="kacct|servers")],
                    [InlineKeyboardButton("❎ Close", callback_data="kacct|close")],
                ]),
            )
            return
        if not keys:
            await query.edit_message_text(
                f"📊 *Key Accounting Diagnostic*\n\nNo keys found on `{alias}`.",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 Back Servers", callback_data="kacct|servers")],
                    [InlineKeyboardButton("❎ Close", callback_data="kacct|close")],
                ]),
            )
            return
        await query.edit_message_text(
            f"📊 *Key Accounting Diagnostic*\n\nSelect a key on `{alias}`.",
            parse_mode='Markdown',
            reply_markup=_key_accounting_keys_keyboard(alias, keys),
        )
        return

    if action == "key" and len(parts) == 4:
        alias = parts[2]
        key_id = parts[3]
        try:
            text = await _build_key_accounting_text(alias, key_id)
        except Exception as e:
            logger.error(f"Key accounting diagnostic detail error on {alias}/{key_id}: {e}")
            text = None
        if not text:
            await query.edit_message_text(
                f"❌ Key `{key_id}` was not found on `{alias}` or the server is unreachable.",
                parse_mode='Markdown',
                reply_markup=_key_accounting_result_keyboard(alias),
            )
            return
        await query.edit_message_text(
            text,
            parse_mode='Markdown',
            reply_markup=_key_accounting_result_keyboard(alias),
        )
        return

    await query.answer("Unknown accounting action.", show_alert=True)


@owner_only
async def handle_user_accounting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    parts = query.data.split("|")
    if len(parts) < 2 or parts[0] != "uacct":
        return

    action = parts[1]
    if action == "close":
        await query.edit_message_text("✅ User accounting diagnostic panel closed.")
        if query.message:
            clear_if_matches(context, query.message.message_id)
        return

    if action == "users":
        await query.edit_message_text(
            "👤 *User Accounting Diagnostic*\n\nSelect a user to inspect lifetime accounting.",
            parse_mode='Markdown',
            reply_markup=_user_accounting_users_keyboard(_accounting_subjects()),
        )
        return

    if action == "user" and len(parts) == 3:
        try:
            user_id = int(parts[2])
        except ValueError:
            await query.answer("Invalid user id.", show_alert=True)
            return
        await query.edit_message_text(
            _format_customer_accounting_text(user_id),
            parse_mode='Markdown',
            reply_markup=_user_accounting_result_keyboard(),
        )
        return

    await query.answer("Unknown user accounting action.", show_alert=True)


@owner_only
async def handle_loyalty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    parts = query.data.split("|")
    if len(parts) < 2 or parts[0] != "loyal":
        return

    action = parts[1]
    if action == "close":
        await query.edit_message_text("✅ Loyalty leaderboard panel closed.")
        if query.message:
            clear_if_matches(context, query.message.message_id)
        return

    if action == "metric" and len(parts) == 3:
        metric = parts[2]
        if metric not in {'buyers', 'used', 'renewals'}:
            await query.answer("Unknown leaderboard metric.", show_alert=True)
            return
        await query.edit_message_text(
            _build_loyalty_text(metric),
            parse_mode='Markdown',
            reply_markup=_loyalty_metric_keyboard(metric),
        )
        return

    await query.answer("Unknown loyalty action.", show_alert=True)