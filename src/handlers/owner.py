import logging
import os
import time
from telegram import Update
from telegram.ext import ContextTypes
from src.utils.decorators import owner_only, admin_only
from src.database import queries
from src.services.backup_service import generate_backup_file, get_latest_backup_file
from src.services.notifier import monitor_used_up_keys

logger = logging.getLogger(__name__)
REVIEW_MODE_BROADCAST_COOLDOWN_SECONDS = 120

def _strip_outline_label(value: str, label: str) -> str:
    """Accept values copied from access.txt lines such as `apiUrl:...`."""
    prefix = f"{label}:"
    return value[len(prefix):].strip() if value.startswith(prefix) else value.strip()


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
            username_text = f" (@{username})" if username else ""
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

        username_text = f"@{username}" if username else "(username unavailable)"
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