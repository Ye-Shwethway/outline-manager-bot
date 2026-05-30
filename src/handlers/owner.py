import logging
import os
import time
import asyncio
from telegram import Update
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown
from telegram.ext import ContextTypes
from src.utils.decorators import owner_only, admin_only
from src.database import queries
from src.services.backup_service import generate_backup_file, get_latest_backup_file
from src.services.notifier import monitor_used_up_keys
from src.services.outline_api import get_vpn_client
from src.utils.inline_messages import clear_if_matches

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
        f"Last Usage Sync UTC: *{escape_markdown(str(lifecycle.get('last_usage_sync_at_utc') or 'N/A'), version=1)}*"
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