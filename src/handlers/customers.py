import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from src.config import OWNER_ID
from src.database import queries
from src.services.outline_api import get_vpn_client
from src.utils.decorators import admin_only
from src.utils.datetime_utils import to_yangon_display

logger = logging.getLogger(__name__)
USER_STATUSES = ["pending", "approved", "rejected", "staff"]
BYTES_PER_GB = 1_000_000_000


def _is_privileged_principal(principal_type: str) -> bool:
    return principal_type in {"owner", "admin"}


def _can_manage_principal(actor_user_id: int, target_user_id: int, principal_type: str) -> bool:
    if actor_user_id == OWNER_ID:
        return True
    if _is_privileged_principal(principal_type):
        return actor_user_id == target_user_id
    return True


def _principal_type_token(principal_type: str) -> str:
    token_map = {"customer": "c", "owner": "o", "admin": "a"}
    return token_map.get(principal_type, "c")


def _principal_type_from_token(token: str | None) -> str:
    token_map = {"c": "customer", "o": "owner", "a": "admin"}
    return token_map.get(token or "", "customer")


def _resolve_principal_type(target_id: int) -> str:
    if target_id == OWNER_ID:
        return "owner"
    if target_id in queries.get_admins():
        return "admin"
    return "customer"


def _build_staff_items() -> list[dict]:
    customers_by_id = {
        int(item["user_id"]): item
        for item in (
            queries.get_customers_by_status("pending")
            + queries.get_customers_by_status("approved")
            + queries.get_customers_by_status("rejected")
        )
    }
    admin_profiles = {int(item["user_id"]): item for item in queries.get_admin_profiles()}
    staff_ids = [OWNER_ID] + [uid for uid in sorted(admin_profiles.keys()) if uid != OWNER_ID]

    items: list[dict] = []
    for user_id in staff_ids:
        customer = customers_by_id.get(user_id, {})
        admin_profile = admin_profiles.get(user_id, {})
        username = customer.get("username") or admin_profile.get("username")
        first_name = customer.get("first_name") or "N/A"
        principal_type = "owner" if user_id == OWNER_ID else "admin"
        items.append(
            {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "status": "staff",
                "principal_type": principal_type,
            }
        )
    return items


def _review_recipients() -> list[int]:
    if queries.is_admin_registration_review_notifications_enabled():
        return sorted(set([OWNER_ID, *queries.get_admins()]))
    return [OWNER_ID]


def _is_reviewer(user_id: int | None) -> bool:
    if not user_id:
        return False
    return user_id == OWNER_ID or user_id in queries.get_admins()


def _registration_review_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"ureg_a_{user_id}"),
                InlineKeyboardButton("⛔ Reject", callback_data=f"ureg_r_{user_id}"),
            ]
        ]
    )


def _manage_user_shortcut_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⚙️ Manage This User", callback_data=f"umgr|user|{user_id}")]]
    )


def _users_status_keyboard(selected: str) -> InlineKeyboardMarkup:
    labels = {
        "pending": "⏳ Pending",
        "approved": "✅ Approved",
        "rejected": "⛔ Rejected",
        "staff": "🛡️ Staff",
    }
    row = []
    for status in USER_STATUSES:
        label = labels[status]
        if status == selected:
            label = f"• {label}"
        row.append(InlineKeyboardButton(label, callback_data=f"uadm_view_{status}"))
    return InlineKeyboardMarkup([row])


def _users_action_keyboard(status: str, items: list[dict]) -> InlineKeyboardMarkup | None:
    rows = []
    for item in items[:12]:
        user_id = int(item["user_id"])
        principal_type = item.get("principal_type", "customer")
        role_tag = "👑" if principal_type == "owner" else "🛡️" if principal_type == "admin" else ""
        ptoken = _principal_type_token(principal_type)
        rows.append(
            [InlineKeyboardButton(f"⚙️ Manage {user_id} {role_tag}".strip(), callback_data=f"uadm_manage_{status}_{user_id}_{ptoken}")]
        )

    # Always keep the status tab row visible, even when there are no users in current view.
    rows.extend(_users_status_keyboard(status).inline_keyboard)
    rows.append([InlineKeyboardButton("❎ Close", callback_data="uadm_close_0")])
    return InlineKeyboardMarkup(rows)


def _user_manage_keyboard(
    view_status: str,
    user_id: int,
    customer_status: str,
    principal_type: str,
    can_manage: bool,
) -> InlineKeyboardMarkup:
    rows = []
    if can_manage:
        rows.append([InlineKeyboardButton("🗝️ Manage Assigned Keys", callback_data=f"umgr|user|{user_id}")])

    if principal_type == "customer" and customer_status == "pending":
        rows.append(
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"uadm_approve_{user_id}_{view_status}"),
                InlineKeyboardButton("⛔ Reject", callback_data=f"uadm_reject_{user_id}_{view_status}"),
            ]
        )
    if principal_type == "customer" and customer_status == "approved" and can_manage:
        rows.append([InlineKeyboardButton("⛔ Ban User", callback_data=f"uadm_banconfirm_{user_id}_{view_status}_c")])
    if principal_type == "customer" and can_manage:
        remove_label = "♻️ Unban User" if customer_status == "rejected" else "🗑 Remove User"
        rows.append([InlineKeyboardButton(remove_label, callback_data=f"uadm_rmconfirm_{user_id}_{view_status}_c")])
    rows.append(
        [
            InlineKeyboardButton("⬅️ Back", callback_data=f"uadm_view_{view_status}"),
            InlineKeyboardButton("❎ Close", callback_data="uadm_close_0"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _remove_confirm_keyboard(view_status: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm Remove", callback_data=f"uadm_rmyes_{user_id}_{view_status}_c"),
            ],
            [
                InlineKeyboardButton("❎ Cancel", callback_data=f"uadm_rmcancel_{user_id}_{view_status}_c"),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data=f"uadm_manage_{view_status}_{user_id}_c"),
                InlineKeyboardButton("❎ Close", callback_data="uadm_close_0"),
            ],
        ]
    )


def _ban_confirm_keyboard(view_status: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm Ban", callback_data=f"uadm_banyes_{user_id}_{view_status}_c"),
            ],
            [
                InlineKeyboardButton("❎ Cancel", callback_data=f"uadm_bancancel_{user_id}_{view_status}_c"),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data=f"uadm_manage_{view_status}_{user_id}_c"),
                InlineKeyboardButton("❎ Close", callback_data="uadm_close_0"),
            ],
        ]
    )


def _format_user_line(item: dict) -> str:
    user_id = item["user_id"]
    username = item.get("username")
    first_name = item.get("first_name") or "N/A"
    principal_type = item.get("principal_type", "customer")
    role_text = "👑 OWNER" if principal_type == "owner" else "🛡️ ADMIN" if principal_type == "admin" else "👤 USER"
    uname = f"@{username}" if username else "(no username)"
    return f"- {role_text} | ID: `{user_id}` | Username: {uname} | Name: {first_name}"


def _build_users_status_text(status: str) -> tuple[str, list[dict]]:
    pending = queries.get_customers_by_status("pending")
    approved = queries.get_customers_by_status("approved")
    rejected = queries.get_customers_by_status("rejected")
    staff = _build_staff_items()

    for item in pending:
        item["principal_type"] = "customer"
    for item in approved:
        item["principal_type"] = "customer"
    for item in rejected:
        item["principal_type"] = "customer"

    by_status = {
        "pending": pending,
        "approved": approved,
        "rejected": rejected,
        "staff": staff,
    }
    items = by_status[status]

    title_map = {
        "pending": "⏳ *Pending Users*",
        "approved": "✅ *Approved Users*",
        "rejected": "⛔ *Rejected Users*",
        "staff": "🛡️ *Owner and Admins*",
    }

    lines = [
        "👥 *User Registry*",
        f"Pending: *{len(pending)}* | Approved: *{len(approved)}* | Rejected: *{len(rejected)}* | Staff: *{len(staff)}*",
        "",
        title_map[status],
    ]

    if items:
        lines.extend([_format_user_line(item) for item in items])
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Use inline buttons below to switch status or manage users.")
    return "\n".join(lines), items


async def _notify_user_status(context: ContextTypes.DEFAULT_TYPE, target_id: int, status: str):
    if status == "approved":
        text = "✅ Your registration has been approved. You can now use /mykeys."
    elif status == "rejected":
        text = "⛔ Your registration was rejected. Contact admin for details."
    elif status == "removed":
        text = "🗑️ Your account was removed by admin. Use /register again if you need access."
    else:
        return

    try:
        await context.bot.send_message(chat_id=target_id, text=text)
    except Exception as e:
        logger.info(f"Could not notify user {target_id} for status {status}: {e}")


def _remove_user_workflow(target_id: int, actor_user_id: int | None, actor_username: str | None):
    assigned = queries.get_user_assigned_keys(target_id)
    for item in assigned:
        alias = item["server_alias"]
        key_id = str(item["key_id"])
        queries.set_key_assignment(alias, key_id, None)
        queries.add_key_lifecycle_event(
            server_alias=alias,
            key_id=key_id,
            event_type="unassigned_user",
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            payload={"removed_user_id": target_id},
        )

    queries.clear_user_key_assignments(target_id)
    queries.remove_customer(target_id)


def _ban_user_workflow(target_id: int, actor_user_id: int | None, actor_username: str | None):
    """Ban an approved customer by unlinking all keys and marking status as rejected."""
    assigned = queries.get_user_assigned_keys(target_id)
    for item in assigned:
        alias = item["server_alias"]
        key_id = str(item["key_id"])
        queries.set_key_assignment(alias, key_id, None)
        queries.add_key_lifecycle_event(
            server_alias=alias,
            key_id=key_id,
            event_type="unassigned_user",
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            payload={"banned_user_id": target_id},
        )

    queries.clear_user_key_assignments(target_id)
    queries.set_customer_status(target_id, "rejected", approved_by=actor_user_id)


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
            await context.bot.send_message(
                chat_id=reviewer_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=_registration_review_keyboard(applied_user_id),
            )
        except Exception as e:
            logger.warning(f"Failed to notify reviewer {reviewer_id} for registration {applied_user_id}: {e}")


async def handle_registration_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline approve/reject action from registration notifications."""
    query = update.callback_query
    if not query or not update.effective_user:
        return

    if not _is_reviewer(update.effective_user.id):
        await query.answer("❌ Owner/Admin privileges required.", show_alert=True)
        return

    parts = (query.data or "").split("_", 2)
    if len(parts) != 3:
        await query.answer("Invalid action payload.", show_alert=True)
        return

    action = parts[1]
    try:
        target_id = int(parts[2])
    except ValueError:
        await query.answer("Invalid user id.", show_alert=True)
        return

    existing = queries.get_customer(target_id)
    if not existing:
        queries.upsert_customer(target_id, None, None, status="pending")
        existing = queries.get_customer(target_id)

    current_status = (existing.get("status") or "pending").lower() if existing else "pending"
    if current_status != "pending":
        await query.answer(f"Already {current_status}.", show_alert=True)
        await query.edit_message_reply_markup(reply_markup=None)
        return

    actor = update.effective_user
    if action == "a":
        queries.set_customer_status(target_id, "approved", approved_by=actor.id)
        await query.answer("User approved.")
        await query.edit_message_text(
            (
                "✅ *User Approved via Inline Review*\n\n"
                f"Target Telegram ID: `{target_id}`\n"
                f"Reviewed By: @{actor.username or actor.id}\n\n"
                "Next: use *Manage This User* below or `/manage` for assignment workflow."
            ),
            parse_mode="Markdown",
            reply_markup=_manage_user_shortcut_keyboard(target_id),
        )
        try:
            await _notify_user_status(context, target_id, "approved")
        except Exception:
            pass
        return

    if action == "r":
        queries.set_customer_status(target_id, "rejected", approved_by=actor.id)
        await query.answer("User rejected.")
        await query.edit_message_text(
            (
                "⛔ *User Rejected via Inline Review*\n\n"
                f"Target Telegram ID: `{target_id}`\n"
                f"Reviewed By: @{actor.username or actor.id}"
            ),
            parse_mode="Markdown",
        )
        try:
            await _notify_user_status(context, target_id, "rejected")
        except Exception:
            pass
        return

    await query.answer("Unknown action.", show_alert=True)


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

    if existing and (existing.get("status") or "").lower() == "rejected":
        await update.message.reply_text(
            "⛔ Your account is permanently blocked from re-registering. Contact Owner/Admin for manual removal from the rejected list.",
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

    text = build_mykeys_snapshot_text(user.id)
    await update.message.reply_text(text, parse_mode="Markdown")


def build_mykeys_snapshot_text(user_id: int) -> str:
    """Builds the same content shown by /mykeys for reuse in notifications."""
    items = queries.get_user_assigned_keys(user_id)
    if not items:
        return "No keys are assigned to your account yet."

    server_key_map: dict[str, dict[str, object]] = {}
    for alias in sorted({item["server_alias"] for item in items}):
        client = get_vpn_client(alias)
        if not client:
            server_key_map[alias] = {}
            continue
        try:
            keys = client.get_keys()
            server_key_map[alias] = {str(key.key_id): key for key in keys}
        except Exception as e:
            logger.error(f"mykeys snapshot fetch error on {alias}: {e}")
            server_key_map[alias] = {}

    lines = ["🔑 *Your Assigned Keys*", ""]
    for item in items:
        alias = item["server_alias"]
        key_id = str(item["key_id"])
        expiry = to_yangon_display(item.get("expiry_at_utc")) if item.get("expiry_at_utc") else "Not set"
        state = "Expired" if item.get("is_expired") else "Active"
        renew_count = item.get("renew_count") or 0

        live_key = server_key_map.get(alias, {}).get(key_id)
        if live_key:
            used_bytes = live_key.used_bytes or 0
            used_gb = used_bytes / BYTES_PER_GB
            if live_key.data_limit:
                limit_bytes = live_key.data_limit
                limit_gb = limit_bytes / BYTES_PER_GB
                remaining_gb = max(limit_bytes - used_bytes, 0) / BYTES_PER_GB
                usage_line = f"Usage: *{used_gb:.2f} GB / {limit_gb:.2f} GB*"
                remaining_line = f"Remaining: *{remaining_gb:.2f} GB*"
            else:
                usage_line = f"Usage: *{used_gb:.2f} GB / Unlimited*"
                remaining_line = "Remaining: *Unlimited*"
            key_url_line = f"🔵 *Key URL:* `{live_key.access_url}`" if live_key.access_url else "🔵 *Key URL:* *Unavailable right now*"
        else:
            usage_line = "Usage: *Unavailable right now*"
            remaining_line = "Remaining: *Unavailable right now*"
            key_url_line = "🔵 *Key URL:* *Unavailable right now*"

        lines.append(
            f"- Server: `{alias}` | Key ID: `{key_id}`\n"
            f"  {usage_line}\n"
            f"  {remaining_line}\n"
            f"  {key_url_line}\n"
            f"  Expiry: *{expiry}* ({state})\n"
            f"  Renew Count: *{renew_count}*"
        )

    return "\n".join(lines)


async def notify_user_assigned_keys_snapshot(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Pushes current assigned-keys snapshot to user after assignment changes."""
    text = build_mykeys_snapshot_text(user_id)
    await context.bot.send_message(
        chat_id=user_id,
        text="✅ A key has been assigned to your account. Here is your latest key status:\n\n" + text,
        parse_mode="Markdown",
    )


def _collect_search_principals() -> list[dict]:
    """Builds de-duplicated principal index for /search from owner/admin/customer sources."""
    principals: dict[int, dict] = {}

    # 1) Seed owner/admin from admin table first.
    admin_profiles = {int(item["user_id"]): item for item in queries.get_admin_profiles()}

    principals[OWNER_ID] = {
        "user_id": OWNER_ID,
        "username": admin_profiles.get(OWNER_ID, {}).get("username"),
        "first_name": None,
        "role": "owner",
    }

    for admin_id, profile in admin_profiles.items():
        if admin_id == OWNER_ID:
            continue
        principals[admin_id] = {
            "user_id": admin_id,
            "username": profile.get("username"),
            "first_name": None,
            "role": "admin",
        }

    # 2) Merge customers (all statuses), preserving higher privilege role when overlaps happen.
    customer_rows = (
        queries.get_customers_by_status("pending")
        + queries.get_customers_by_status("approved")
        + queries.get_customers_by_status("rejected")
    )
    for row in customer_rows:
        user_id = int(row["user_id"])
        existing = principals.get(user_id)
        username = row.get("username")
        first_name = row.get("first_name")

        if existing:
            if not existing.get("username") and username:
                existing["username"] = username
            if not existing.get("first_name") and first_name:
                existing["first_name"] = first_name
            principals[user_id] = existing
        else:
            principals[user_id] = {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "role": "customer",
            }

    return sorted(principals.values(), key=lambda item: int(item["user_id"]))


def _principal_matches_query(principal: dict, q: str) -> bool:
    user_id = str(principal.get("user_id") or "")
    username = (principal.get("username") or "").casefold()
    first_name = (principal.get("first_name") or "").casefold()
    return q in user_id or q in username or q in first_name


def _collect_key_records() -> list[dict]:
    """Collects live key records including key names and optional assigned owner for /search."""
    records: list[dict] = []
    servers = queries.get_servers()

    for alias in sorted(servers.keys()):
        client = get_vpn_client(alias)
        if not client:
            continue

        try:
            keys = client.get_keys()
        except Exception as e:
            logger.warning(f"Search key fetch failed on {alias}: {e}")
            continue

        for key in keys:
            key_id = str(key.key_id)
            lifecycle = queries.get_key_lifecycle(alias, key_id) or {}
            assigned_user_id = lifecycle.get("assigned_user_id")
            owner_user_id = None
            if assigned_user_id:
                try:
                    owner_user_id = int(assigned_user_id)
                except (TypeError, ValueError):
                    owner_user_id = None

            records.append(
                {
                    "user_id": owner_user_id,
                    "server_alias": alias,
                    "key_id": key_id,
                    "key_name": key.name or "Unnamed",
                }
            )

    return records


@admin_only
async def search_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /search <owner_id|owner_username|key_name> - find owners and assigned keys across servers."""
    if not update.message:
        return

    if len(context.args) == 0:
        await update.message.reply_text(
            "Usage: `/search <owner_user_id|owner_username|key_name>`\n"
            "Example: `/search 1802096079` or `/search office-macbook`",
            parse_mode="Markdown",
        )
        return

    q = " ".join(context.args).strip().casefold()
    principals = _collect_search_principals()
    key_records = _collect_key_records()

    assigned_by_user: dict[int, list[dict]] = {}
    key_name_match_user_ids: set[int] = set()
    unassigned_key_matches: list[dict] = []
    for item in key_records:
        user_id = item.get("user_id")
        if user_id is not None:
            assigned_by_user.setdefault(int(user_id), []).append(item)

        key_name = (item.get("key_name") or "").casefold()
        key_id = str(item.get("key_id") or "")
        if q in key_name or q in key_id:
            if user_id is not None:
                key_name_match_user_ids.add(int(user_id))
            else:
                unassigned_key_matches.append(item)

    matched = [
        p
        for p in principals
        if _principal_matches_query(p, q) or int(p["user_id"]) in key_name_match_user_ids
    ]

    if not matched and not unassigned_key_matches:
        await update.message.reply_text(
            f"No matching owners found for: `{q}`",
            parse_mode="Markdown",
        )
        return

    lines = [
        "🔎 *Search Results*",
        f"Query: `{q}`",
        f"Matched Assigned Users: *{len(matched)}*",
        "",
    ]

    for principal in matched[:20]:
        user_id = int(principal["user_id"])
        username = principal.get("username")
        first_name = principal.get("first_name") or "N/A"
        role = principal.get("role") or "customer"
        role_label = "👑 OWNER" if role == "owner" else "🛡️ ADMIN" if role == "admin" else "👤 USER"
        username_text = f"@{username}" if username else "(no username)"

        assigned = assigned_by_user.get(user_id, [])
        lines.append(f"{role_label} | ID: `{user_id}` | Username: {username_text} | Name: {first_name}")
        lines.append(f"Assigned Keys: *{len(assigned)}*")

        if assigned:
            for item in assigned[:15]:
                alias = item["server_alias"]
                key_id = item["key_id"]
                key_name = item.get("key_name") or "Unnamed"
                lines.append(f"- `{alias}` / `{key_id}` | Name: *{key_name}* -> `/manage {alias} {key_id}`")
            if len(assigned) > 15:
                lines.append(f"- ... and *{len(assigned) - 15}* more keys")
        lines.append("")

    if len(matched) > 20:
        lines.append(f"Showing first 20 results out of {len(matched)} matches.")

    if unassigned_key_matches:
        lines.append("")
        lines.append("🔑 *Unassigned Key Matches*")
        for item in unassigned_key_matches[:20]:
            alias = item["server_alias"]
            key_id = item["key_id"]
            key_name = item.get("key_name") or "Unnamed"
            lines.append(f"- `{alias}` / `{key_id}` | Name: *{key_name}* -> `/manage {alias} {key_id}`")
        if len(unassigned_key_matches) > 20:
            lines.append(f"- ... and *{len(unassigned_key_matches) - 20}* more unassigned key matches")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@admin_only
async def users_overview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /users - open status tabs and user management workflow."""
    if not update.message:
        return

    status = "pending"
    text, items = _build_users_status_text(status)
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=_users_action_keyboard(status, items),
    )


async def handle_users_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline admin workflow for /users status tabs and actions."""
    query = update.callback_query
    if not query or not update.effective_user:
        return

    if not _is_reviewer(update.effective_user.id):
        await query.answer("❌ Owner/Admin privileges required.", show_alert=True)
        return

    parts = (query.data or "").split("_")
    if len(parts) < 3:
        await query.answer("Invalid admin action.", show_alert=True)
        return

    action = parts[1]
    actor = update.effective_user

    if action == "close":
        await query.answer("Closed.")
        await query.edit_message_text("✅ User panel closed.")
        return

    if action == "view":
        payload = parts[2]
        if payload not in USER_STATUSES:
            await query.answer("Invalid status.", show_alert=True)
            return
        text, items = _build_users_status_text(payload)
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=_users_action_keyboard(payload, items),
        )
        await query.answer()
        return

    if action == "manage":
        if len(parts) not in {4, 5}:
            await query.answer("Invalid manage action.", show_alert=True)
            return
        view_status = parts[2]
        try:
            target_id = int(parts[3])
        except ValueError:
            await query.answer("Invalid user id.", show_alert=True)
            return

        principal_type = _principal_type_from_token(parts[4]) if len(parts) == 5 else _resolve_principal_type(target_id)
        if not _can_manage_principal(actor.id, target_id, principal_type):
            await query.answer("You can only manage your own staff account.", show_alert=True)
            return

        customer = queries.get_customer(target_id)
        if principal_type == "customer" and not customer:
            await query.answer("User not found in registry.", show_alert=True)
            text, items = _build_users_status_text(view_status if view_status in USER_STATUSES else "pending")
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=_users_action_keyboard(view_status if view_status in USER_STATUSES else "pending", items),
            )
            return

        if principal_type == "customer":
            username = customer.get("username") if customer else None
            first_name = customer.get("first_name") if customer else None
            customer_status = (customer.get("status") or "pending").lower() if customer else "pending"
            role_line = "👤 USER"
        else:
            admin_map = {int(item["user_id"]): item for item in queries.get_admin_profiles()}
            profile = admin_map.get(target_id, {})
            username = (customer.get("username") if customer else None) or profile.get("username")
            first_name = (customer.get("first_name") if customer else None) or "N/A"
            customer_status = "staff"
            role_line = "👑 OWNER" if principal_type == "owner" else "🛡️ ADMIN"

        uname = f"@{username}" if username else "(no username)"
        first_name = first_name or "N/A"
        assigned_count = len(queries.get_user_assigned_keys(target_id))
        can_manage = _can_manage_principal(actor.id, target_id, principal_type)

        text = (
            "👤 *Manage User*\n\n"
            f"ID: `{target_id}`\n"
            f"Role: *{role_line}*\n"
            f"Username: {uname}\n"
            f"Name: {first_name}\n"
            f"Status: *{customer_status.upper()}*\n"
            f"Assigned Keys: *{assigned_count}*"
        )
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=_user_manage_keyboard(
                view_status if view_status in USER_STATUSES else "pending",
                target_id,
                customer_status,
                principal_type,
                can_manage,
            ),
        )
        await query.answer()
        return

    if action == "rmconfirm":
        if len(parts) != 5:
            await query.answer("Invalid remove action.", show_alert=True)
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            await query.answer("Invalid user id.", show_alert=True)
            return
        view_status = parts[3] if parts[3] in USER_STATUSES else "pending"
        principal_type = _principal_type_from_token(parts[4])
        if principal_type != "customer":
            await query.answer("Staff accounts cannot be removed from /users.", show_alert=True)
            return

        customer = queries.get_customer(target_id)
        customer_status = (customer.get("status") or "pending").lower() if customer else "pending"
        action_title = "⚠️ *Confirm Unban*" if customer_status == "rejected" else "⚠️ *Confirm User Removal*"
        action_desc = (
            "This will remove the user from the rejected list (unban)."
            if customer_status == "rejected"
            else "This will remove the user from registry and unlink all assigned keys."
        )
        await query.edit_message_text(
            (
                f"{action_title}\n\n"
                f"Target ID: `{target_id}`\n\n"
                f"{action_desc}"
            ),
            parse_mode="Markdown",
            reply_markup=_remove_confirm_keyboard(view_status, target_id),
        )
        await query.answer()
        return

    if action == "rmcancel":
        if len(parts) != 5:
            await query.answer("Invalid cancel action.", show_alert=True)
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            await query.answer("Invalid user id.", show_alert=True)
            return
        view_status = parts[3] if parts[3] in USER_STATUSES else "pending"
        principal_type = _principal_type_from_token(parts[4])
        customer = queries.get_customer(target_id)
        if not customer:
            text, items = _build_users_status_text(view_status)
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=_users_action_keyboard(view_status, items),
            )
            await query.answer("User not found.", show_alert=True)
            return

        uname = f"@{customer.get('username')}" if customer.get("username") else "(no username)"
        first_name = customer.get("first_name") or "N/A"
        customer_status = (customer.get("status") or "pending").lower()
        assigned_count = len(queries.get_user_assigned_keys(target_id))
        await query.edit_message_text(
            (
                "👤 *Manage User*\n\n"
                f"ID: `{target_id}`\n"
                f"Username: {uname}\n"
                f"Name: {first_name}\n"
                f"Status: *{customer_status.upper()}*\n"
                f"Assigned Keys: *{assigned_count}*"
            ),
            parse_mode="Markdown",
            reply_markup=_user_manage_keyboard(
                view_status,
                target_id,
                customer_status,
                principal_type,
                _can_manage_principal(actor.id, target_id, principal_type),
            ),
        )
        await query.answer("Cancelled.")
        return

    if action == "banconfirm":
        if len(parts) != 5:
            await query.answer("Invalid ban action.", show_alert=True)
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            await query.answer("Invalid user id.", show_alert=True)
            return
        view_status = parts[3] if parts[3] in USER_STATUSES else "pending"
        principal_type = _principal_type_from_token(parts[4])
        if principal_type != "customer":
            await query.answer("Staff accounts cannot be banned from /users.", show_alert=True)
            return

        customer = queries.get_customer(target_id)
        customer_status = (customer.get("status") or "pending").lower() if customer else "pending"
        if customer_status != "approved":
            await query.answer("Only approved users can be banned from this action.", show_alert=True)
            return

        await query.edit_message_text(
            (
                "⚠️ *Confirm User Ban*\n\n"
                f"Target ID: `{target_id}`\n\n"
                "This will move the user to rejected, unlink all assigned keys, and block re-registration."
            ),
            parse_mode="Markdown",
            reply_markup=_ban_confirm_keyboard(view_status, target_id),
        )
        await query.answer()
        return

    if action == "bancancel":
        if len(parts) != 5:
            await query.answer("Invalid cancel action.", show_alert=True)
            return
        try:
            target_id = int(parts[2])
        except ValueError:
            await query.answer("Invalid user id.", show_alert=True)
            return
        view_status = parts[3] if parts[3] in USER_STATUSES else "pending"
        principal_type = _principal_type_from_token(parts[4])
        customer = queries.get_customer(target_id)
        if not customer:
            text, items = _build_users_status_text(view_status)
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=_users_action_keyboard(view_status, items),
            )
            await query.answer("User not found.", show_alert=True)
            return

        uname = f"@{customer.get('username')}" if customer.get("username") else "(no username)"
        first_name = customer.get("first_name") or "N/A"
        customer_status = (customer.get("status") or "pending").lower()
        assigned_count = len(queries.get_user_assigned_keys(target_id))
        await query.edit_message_text(
            (
                "👤 *Manage User*\n\n"
                f"ID: `{target_id}`\n"
                f"Username: {uname}\n"
                f"Name: {first_name}\n"
                f"Status: *{customer_status.upper()}*\n"
                f"Assigned Keys: *{assigned_count}*"
            ),
            parse_mode="Markdown",
            reply_markup=_user_manage_keyboard(
                view_status,
                target_id,
                customer_status,
                principal_type,
                _can_manage_principal(actor.id, target_id, principal_type),
            ),
        )
        await query.answer("Cancelled.")
        return

    if action in {"approve", "reject"} and len(parts) not in {3, 4}:
        await query.answer("Invalid review action.", show_alert=True)
        return

    if action == "rmyes" and len(parts) != 5:
        await query.answer("Invalid remove action.", show_alert=True)
        return

    if action == "banyes" and len(parts) != 5:
        await query.answer("Invalid ban action.", show_alert=True)
        return

    try:
        target_id = int(parts[2])
    except ValueError:
        await query.answer("Invalid user id.", show_alert=True)
        return

    view_status = parts[3] if len(parts) >= 4 and parts[3] in USER_STATUSES else "pending"
    principal_type = _principal_type_from_token(parts[4]) if len(parts) == 5 else _resolve_principal_type(target_id)

    existing = queries.get_customer(target_id)
    if action in {"approve", "reject"}:
        if principal_type != "customer":
            await query.answer("Staff accounts cannot be approved/rejected.", show_alert=True)
            return
        if not existing:
            queries.upsert_customer(target_id, None, None, status="pending")
            existing = queries.get_customer(target_id)

        current_status = (existing.get("status") or "pending").lower() if existing else "pending"
        if current_status != "pending":
            await query.answer(f"Already {current_status}.", show_alert=True)
        else:
            next_status = "approved" if action == "approve" else "rejected"
            queries.set_customer_status(target_id, next_status, approved_by=actor.id)
            await _notify_user_status(context, target_id, next_status)
            await query.answer(f"User {next_status}.")

    elif action == "rmyes":
        if principal_type != "customer":
            await query.answer("Staff accounts cannot be removed from /users.", show_alert=True)
            return
        if not existing:
            await query.answer("User not found in registry.", show_alert=True)
        else:
            existing_status = (existing.get("status") or "pending").lower()
            _remove_user_workflow(target_id, actor.id, actor.username)
            await _notify_user_status(context, target_id, "removed")
            if existing_status == "rejected":
                await query.answer("User unbanned.")
            else:
                await query.answer("User removed.")
    elif action == "banyes":
        if principal_type != "customer":
            await query.answer("Staff accounts cannot be banned from /users.", show_alert=True)
            return
        if not existing:
            await query.answer("User not found in registry.", show_alert=True)
        else:
            current_status = (existing.get("status") or "pending").lower()
            if current_status != "approved":
                await query.answer("Only approved users can be banned from this action.", show_alert=True)
            else:
                _ban_user_workflow(target_id, actor.id, actor.username)
                await _notify_user_status(context, target_id, "rejected")
                await query.answer("User banned.")
    else:
        await query.answer("Unknown admin action.", show_alert=True)
        return

    text, items = _build_users_status_text(view_status)
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=_users_action_keyboard(view_status, items),
    )


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
    await _notify_user_status(context, target_id, "approved")
    await update.message.reply_text(
        f"✅ User `{target_id}` approved.",
        parse_mode="Markdown",
        reply_markup=_manage_user_shortcut_keyboard(target_id),
    )


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
    await _notify_user_status(context, target_id, "rejected")
    await update.message.reply_text(f"⛔ User `{target_id}` rejected.", parse_mode="Markdown")


@admin_only
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /removeuser <user_id> - remove user and unassign all linked keys."""
    if not update.message:
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /removeuser <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User id must be a number.")
        return

    existing = queries.get_customer(target_id)
    if not existing:
        await update.message.reply_text(f"❌ User `{target_id}` not found in registry.", parse_mode="Markdown")
        return

    actor = update.effective_user
    existing_status = (existing.get("status") or "pending").lower()
    _remove_user_workflow(target_id, actor.id if actor else None, actor.username if actor else None)
    await _notify_user_status(context, target_id, "removed")
    if existing_status == "rejected":
        await update.message.reply_text(
            f"♻️ User `{target_id}` unbanned (removed from rejected list).",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"🗑️ User `{target_id}` removed and all assigned keys were unlinked.",
            parse_mode="Markdown",
        )
