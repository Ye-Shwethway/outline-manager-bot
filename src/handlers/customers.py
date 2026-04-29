import logging

from telegram import Update
from telegram.ext import ContextTypes

from src.config import OWNER_ID
from src.database import queries
from src.utils.decorators import admin_only
from src.utils.datetime_utils import to_yangon_display

logger = logging.getLogger(__name__)


def _review_recipients() -> list[int]:
    return sorted(set([OWNER_ID, *queries.get_admins()]))


async def _notify_registration_reviewers(
    context: ContextTypes.DEFAULT_TYPE,
    applied_user_id: int,
    username: str | None,
    first_name: str | None,
):
    uname = f"@{username}" if username else "(no username)"
    first_name_text = first_name or "N/A"
    text = (
        "🆕 *New User Registration Request*\n\n"
        f"Telegram ID: `{applied_user_id}`\n"
        f"Username: {uname}\n"
        f"First Name: {first_name_text}\n\n"
        "Review commands:\n"
        f"`/approve {applied_user_id}`\n"
        f"`/reject {applied_user_id}`\n\n"
        "After approval, assign keys via `/manage <alias> <key_id>` -> *Assign User*."
    )

    for reviewer_id in _review_recipients():
        try:
            await context.bot.send_message(chat_id=reviewer_id, text=text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Failed to notify reviewer {reviewer_id} for registration {applied_user_id}: {e}")


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /register - users request access approval."""
    user = update.effective_user
    if not user or not update.message:
        return

    existing = queries.get_customer(user.id)
    if existing and (existing.get("status") or "").lower() == "approved":
        await update.message.reply_text(
            "✅ Your account is already approved. Use /mykeys to view assigned keys.",
            parse_mode="Markdown",
        )
        return

    if existing and (existing.get("status") or "").lower() == "pending":
        await update.message.reply_text(
            "⏳ Your registration is already pending review. Please wait for Owner/Admin approval.",
            parse_mode="Markdown",
        )
        return

    queries.upsert_customer(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
        status="pending",
    )
    await _notify_registration_reviewers(
        context=context,
        applied_user_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )
    await update.message.reply_text(
        "✅ Registration request submitted. Please wait for Owner/Admin approval.",
        parse_mode="Markdown",
    )


async def mykeys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /mykeys - show keys assigned to the requesting user."""
    user = update.effective_user
    if not user or not update.message:
        return

    customer = queries.get_customer(user.id)
    if not customer:
        await update.message.reply_text("You are not registered yet. Use /register first.")
        return

    status = (customer.get("status") or "pending").lower()
    if status != "approved":
        await update.message.reply_text(
            f"Your account status is *{status.upper()}*. Please wait for approval.",
            parse_mode="Markdown",
        )
        return

    items = queries.get_user_assigned_keys(user.id)
    if not items:
        await update.message.reply_text("No keys are assigned to your account yet.")
        return

    lines = ["🔑 *Your Assigned Keys*", ""]
    for item in items:
        expiry = to_yangon_display(item.get("expiry_at_utc")) if item.get("expiry_at_utc") else "Not set"
        state = "Expired" if item.get("is_expired") else "Active"
        renew_count = item.get("renew_count") or 0
        lines.append(
            f"- Server: `{item['server_alias']}` | Key: `{item['key_id']}`\n"
            f"  Expiry: *{expiry}* ({state})\n"
            f"  Renew Count: *{renew_count}*"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def users_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /users - list pending and approved users."""
    if not update.message:
        return

    pending = queries.get_customers_by_status("pending")
    approved = queries.get_customers_by_status("approved")
    rejected = queries.get_customers_by_status("rejected")

    def _fmt(item: dict) -> str:
        username = item.get("username")
        uname = f"@{username}" if username else "(no username)"
        return f"- `{item['user_id']}` {uname}"

    lines = [
        "👥 *User Registry*",
        f"Pending: *{len(pending)}* | Approved: *{len(approved)}* | Rejected: *{len(rejected)}*",
        "",
        "*Pending:*",
    ]

    if pending:
        lines.extend([_fmt(item) for item in pending[:30]])
    else:
        lines.append("- none")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def approve_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /approve <user_id>"""
    if not update.message:
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /approve <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User id must be a number.")
        return

    existing = queries.get_customer(target_id)
    if not existing:
        queries.upsert_customer(target_id, None, None, status="pending")

    actor = update.effective_user
    queries.set_customer_status(target_id, "approved", approved_by=actor.id if actor else None)
    await update.message.reply_text(f"✅ User `{target_id}` approved.", parse_mode="Markdown")


@admin_only
async def reject_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /reject <user_id>"""
    if not update.message:
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /reject <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User id must be a number.")
        return

    existing = queries.get_customer(target_id)
    if not existing:
        queries.upsert_customer(target_id, None, None, status="pending")

    actor = update.effective_user
    queries.set_customer_status(target_id, "rejected", approved_by=actor.id if actor else None)
    await update.message.reply_text(f"⛔ User `{target_id}` rejected.", parse_mode="Markdown")
