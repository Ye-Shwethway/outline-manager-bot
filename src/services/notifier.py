import logging
from telegram.ext import ContextTypes

from src.database import queries
from src.services.outline_api import get_vpn_client

logger = logging.getLogger(__name__)


def _is_key_used_up(key) -> bool:
    if not key.data_limit:
        return False
    return (key.used_bytes or 0) >= key.data_limit


async def monitor_used_up_keys(context: ContextTypes.DEFAULT_TYPE):
    """Background monitor that alerts owner/admins when keys reach data limit."""
    result = {
        "servers_scanned": 0,
        "keys_scanned": 0,
        "alerts_sent": 0,
    }

    recipients = queries.get_notification_recipients()
    if not recipients:
        return result

    for alias in queries.get_servers().keys():
        result["servers_scanned"] += 1
        client = get_vpn_client(alias)
        if not client:
            continue

        try:
            keys = client.get_keys()
        except Exception as e:
            logger.error(f"Notification scan failed for server {alias}: {e}")
            continue

        for key in keys:
            result["keys_scanned"] += 1
            key_id = str(key.key_id)
            used_up = _is_key_used_up(key)
            already_notified = queries.is_key_used_up_notified(alias, key_id)

            if used_up and not already_notified:
                key_name = key.name or "Unnamed"
                used_gb = (key.used_bytes or 0) / 1_000_000_000
                limit_gb = key.data_limit / 1_000_000_000
                text = (
                    "⚠️ *Key Used Up Alert*\n\n"
                    f"Server: `{alias}`\n"
                    f"Key ID: `{key_id}`\n"
                    f"Name: *{key_name}*\n"
                    f"Usage: {used_gb:.2f} GB / {limit_gb:.2f} GB\n\n"
                    "Action: Contact user for renewal/new key."
                )

                for user_id in recipients:
                    try:
                        await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
                        result["alerts_sent"] += 1
                    except Exception as send_err:
                        logger.warning(f"Failed to send used-up alert to {user_id}: {send_err}")

                queries.set_key_used_up_notified(alias, key_id, True)

            elif not used_up and already_notified:
                # Reset state so this key can alert again in the future.
                queries.set_key_used_up_notified(alias, key_id, False)

    return result
