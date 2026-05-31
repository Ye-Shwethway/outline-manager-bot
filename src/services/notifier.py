import logging
from telegram.ext import ContextTypes

from src.database import queries
from src.services.outline_api import get_vpn_client
from src.utils.datetime_utils import utc_now_iso, to_yangon_display

logger = logging.getLogger(__name__)


def _enforced_limit_bytes(key, lifecycle: dict) -> int:
    configured_mode = str(lifecycle.get("configured_limit_mode") or "").strip().lower()
    if configured_mode == "unlimited":
        return 0

    live_limit_bytes = max(int(key.data_limit or 0), 0)
    if live_limit_bytes > 0:
        return live_limit_bytes

    quota_block_bytes = max(int(lifecycle.get("quota_block_limit_bytes") or 0), 0)
    if quota_block_bytes > 0:
        return quota_block_bytes

    configured_bytes = max(int(lifecycle.get("configured_limit_bytes") or 0), 0)
    if configured_mode == "limited" and configured_bytes > 0:
        return configured_bytes
    return 0


def _is_key_used_up(limit_bytes: int, raw_used_bytes: int) -> bool:
    if limit_bytes <= 0:
        return False
    return raw_used_bytes >= limit_bytes


def _format_gb(value_bytes: int) -> str:
    return f"{max(int(value_bytes or 0), 0) / 1_000_000_000:.2f} GB"


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
            raw_used_bytes = max(int(key.used_bytes or 0), 0)
            lifecycle = queries.get_key_lifecycle(alias, key_id) or {}
            limit_bytes = _enforced_limit_bytes(key, lifecycle)
            used_up = _is_key_used_up(limit_bytes, raw_used_bytes)
            already_blocked = bool(lifecycle.get("quota_blocked_at_utc"))

            if used_up and not already_blocked:
                blocked_at_utc = utc_now_iso()
                original_limit_bytes = limit_bytes
                try:
                    client.add_data_limit(key_id, 0)
                except Exception as disable_err:
                    logger.error(f"Failed quota auto-disable for {alias}/{key_id}: {disable_err}")
                    continue

                queries.mark_key_quota_blocked(alias, key_id, original_limit_bytes, blocked_at_utc)
                queries.add_key_lifecycle_event(
                    server_alias=alias,
                    key_id=key_id,
                    event_type="auto_disabled_quota",
                    actor_user_id=None,
                    actor_username="system",
                    payload={
                        "reason": "raw_outline_usage_reached_limit",
                        "raw_used_bytes": raw_used_bytes,
                        "original_limit_bytes": original_limit_bytes,
                        "quota_blocked_at_utc": blocked_at_utc,
                    },
                    created_at_utc=blocked_at_utc,
                )

                key_name = key.name or "Unnamed"
                text = (
                    "⛔ *Key Auto-Disabled (Quota Guard)*\n\n"
                    f"Server: `{alias}`\n"
                    f"Key ID: `{key_id}`\n"
                    f"Name: *{key_name}*\n"
                    f"Raw Usage: {_format_gb(raw_used_bytes)} / {_format_gb(original_limit_bytes)}\n"
                    f"Blocked At (Yangon): *{to_yangon_display(blocked_at_utc)}*\n\n"
                    "Action: Renew the key or issue a new key before restoring access."
                )

                for user_id in recipients:
                    try:
                        await context.bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
                        result["alerts_sent"] += 1
                    except Exception as send_err:
                        logger.warning(f"Failed to send used-up alert to {user_id}: {send_err}")

    return result
