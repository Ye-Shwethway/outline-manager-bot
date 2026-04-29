import logging
from telegram.ext import ContextTypes

from src.database import queries
from src.services.outline_api import get_vpn_client
from src.utils.datetime_utils import utc_now_iso, to_yangon_display

logger = logging.getLogger(__name__)


async def monitor_expired_keys(context: ContextTypes.DEFAULT_TYPE):
    """Auto-disable keys whose expiry timestamp has passed."""
    result = {
        "keys_checked": 0,
        "keys_expired": 0,
        "alerts_sent": 0,
    }

    now_utc = utc_now_iso()
    due_keys = queries.list_keys_needing_expiry_check(now_utc)
    recipients = queries.get_notification_recipients()

    for item in due_keys:
        result["keys_checked"] += 1
        alias = item["server_alias"]
        key_id = str(item["key_id"])
        expiry_at_utc = item.get("expiry_at_utc")

        client = get_vpn_client(alias)
        if not client:
            continue

        try:
            # Set limit to 0 to disable traffic for expired keys.
            client.add_data_limit(key_id, 0)
        except Exception as e:
            logger.error(f"Failed auto-disable for {alias}/{key_id}: {e}")
            continue

        queries.mark_key_expired(alias, key_id, now_utc)
        queries.add_key_lifecycle_event(
            server_alias=alias,
            key_id=key_id,
            event_type="auto_disabled_expiry",
            actor_user_id=None,
            actor_username="system",
            payload={
                "reason": "expiry_reached",
                "expiry_at_utc": expiry_at_utc,
                "auto_disabled_at_utc": now_utc,
            },
            created_at_utc=now_utc,
        )
        result["keys_expired"] += 1

        if recipients:
            text = (
                "⛔ *Key Auto-Disabled (Expired)*\n\n"
                f"Server: `{alias}`\n"
                f"Key ID: `{key_id}`\n"
                f"Expired At (Yangon): *{to_yangon_display(expiry_at_utc)}*\n"
                f"Disabled At (Yangon): *{to_yangon_display(now_utc)}*"
            )
            for user_id in recipients:
                try:
                    await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
                    result["alerts_sent"] += 1
                except Exception as send_err:
                    logger.warning(f"Failed expiry alert delivery to {user_id}: {send_err}")

    return result
