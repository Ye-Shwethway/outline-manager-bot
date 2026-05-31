import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from telegram.ext import ContextTypes

from src.config import BACKUP_DIR, OWNER_ID
from src.database import queries
from src.services.outline_api import get_vpn_client
from src.utils.datetime_utils import to_yangon_display, to_utc_display

logger = logging.getLogger(__name__)

BYTES_PER_GB = 1_000_000_000


def _status_tag(key, sold_keys: set[str], raw_used_bytes: int) -> str:
    used_up = bool(key.data_limit) and raw_used_bytes >= key.data_limit
    if used_up:
        return "USED_UP"
    if str(key.key_id) in sold_keys:
        return "SOLD"
    return "AVAILABLE"


def _usage_lines(key, raw_used_bytes: int) -> tuple[str, str]:
    used_gb = raw_used_bytes / BYTES_PER_GB
    if key.data_limit:
        limit_gb = key.data_limit / BYTES_PER_GB
        available_gb = max(key.data_limit - raw_used_bytes, 0) / BYTES_PER_GB
        return (
            f"{used_gb:.2f} GB / {limit_gb:.2f} GB",
            f"{available_gb:.2f} GB",
        )
    if getattr(key, "limit_mode", None) == "unlimited":
        return (f"{used_gb:.2f} GB / Unlimited", "Unlimited")
    return (f"{used_gb:.2f} GB / Not set", "Not set")


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


def _backup_recipients() -> list[int]:
    return sorted(set([OWNER_ID, *queries.get_admins()]))


def generate_backup_file(kind: str) -> str:
    """Builds a detailed text backup from latest live server/key state and returns file path."""
    os.makedirs(BACKUP_DIR, exist_ok=True)

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%d-%m-%y_%I:%M_%p")
    filename = f"{kind}_backup_{timestamp}.txt"
    file_path = os.path.join(BACKUP_DIR, filename)
    generated_text = now.strftime("%d-%m-%y_%I:%M_%p UTC")

    servers = queries.get_servers()
    lines = [
        "OUTLINE MANAGER BACKUP",
        f"Type: {kind}",
        f"Generated UTC: {generated_text}",
        f"Server count: {len(servers)}",
        "",
    ]

    if not servers:
        lines.append("No servers configured.")
    else:
        admin_username_map = {
            int(item["user_id"]): item.get("username")
            for item in queries.get_admin_profiles()
            if item.get("user_id") is not None
        }
        for alias in sorted(servers.keys()):
            server_data = servers[alias]
            lines.append(f"=== SERVER: {alias} ===")
            lines.append(f"API URL: {server_data['api_url']}")
            lines.append(f"CERT SHA256: {server_data['cert_sha256']}")
            max_keys = server_data.get("max_key_count", 0)
            lines.append(f"Configured Max Keys: {max_keys if max_keys > 0 else 'Unlimited'}")

            client = get_vpn_client(alias)
            if not client:
                lines.append("ERROR: Could not connect to server API.")
                lines.append("")
                continue

            try:
                keys = client.get_keys()
            except Exception as e:
                lines.append(f"ERROR: Failed to fetch keys ({e}).")
                lines.append("")
                continue

            sold_keys = queries.get_sold_keys(alias)
            key_creators = queries.get_key_creators(alias)

            lines.append(f"Current Keys: {len(keys)}")
            if not keys:
                lines.append("(No keys on this server)")
                lines.append("")
                continue

            lines.append("--- KEY DETAILS ---")
            for key in keys:
                key_id = str(key.key_id)
                creator_username = key_creators.get(key_id) or "unknown"
                lifecycle = queries.get_key_lifecycle(alias, key_id) or {}
                raw_used_bytes = max(int(key.used_bytes or 0), 0)
                if queries.clear_key_quota_block_if_restored(alias, key_id, lifecycle, key.data_limit, raw_used_bytes):
                    lifecycle = queries.get_key_lifecycle(alias, key_id) or {}
                display_limit_bytes = int(key.data_limit or 0) or int(lifecycle.get("quota_block_limit_bytes") or 0) or int(lifecycle.get("configured_limit_bytes") or 0)
                usage_key = type(
                    "DisplayKey",
                    (),
                    {"data_limit": display_limit_bytes, "limit_mode": lifecycle.get("configured_limit_mode")},
                )()
                usage, available = _usage_lines(usage_key, 0 if lifecycle.get("quota_blocked_at_utc") else raw_used_bytes)
                expiry_at_utc = lifecycle.get("expiry_at_utc")
                expiry_yangon = to_yangon_display(expiry_at_utc) if expiry_at_utc else "N/A"
                is_expired = bool(lifecycle.get("is_expired"))
                auto_disabled_at_utc = to_utc_display(lifecycle.get("auto_disabled_at_utc"))
                assigned_user_id = lifecycle.get("assigned_user_id") or "N/A"
                assigned_username = "N/A"
                if assigned_user_id != "N/A":
                    try:
                        owner_id = int(assigned_user_id)
                    except (TypeError, ValueError):
                        owner_id = None

                    if owner_id is not None:
                        customer = queries.get_customer(owner_id)
                        username = customer.get("username") if customer else None
                        if not username:
                            username = admin_username_map.get(owner_id)
                        if username:
                            assigned_username = f"@{username}"
                renew_count = lifecycle.get("renew_count") or 0
                last_renewed_at_utc = to_utc_display(lifecycle.get("last_renewed_at_utc"))
                last_renewed_quota_gb = lifecycle.get("last_renewed_quota_gb")
                last_renewed_quota_text = (
                    f"{last_renewed_quota_gb:.2f} GB" if isinstance(last_renewed_quota_gb, (int, float)) else "N/A"
                )
                accounting = queries.get_key_accounting_totals(alias, key_id) or {}
                lines.append(f"Key ID: {key_id}")
                lines.append(f"Name: {key.name or 'Unnamed'}")
                if is_expired:
                    status_value = "EXPIRED"
                elif lifecycle.get("quota_blocked_at_utc"):
                    status_value = "QUOTA_BLOCKED"
                else:
                    status_value = _status_tag(key, sold_keys, raw_used_bytes)
                lines.append(f"Status: {status_value}")
                lines.append(f"Generated By: @{creator_username}")
                lines.append(f"Usage: {usage}")
                lines.append(f"Available Usage: {available}")
                lines.append(f"Expiry At UTC: {to_utc_display(expiry_at_utc)}")
                lines.append(f"Expiry At Yangon: {expiry_yangon}")
                lines.append(f"Is Expired: {is_expired}")
                lines.append(f"Auto Disabled At UTC: {auto_disabled_at_utc}")
                lines.append(f"Assigned User ID: {assigned_user_id}")
                lines.append(f"Assigned Username: {assigned_username}")
                lines.append(f"Owner User ID: {assigned_user_id}")
                lines.append(f"Owner Username: {assigned_username}")
                lines.append(f"Renew Count: {renew_count}")
                lines.append(f"Last Renewed At UTC: {last_renewed_at_utc}")
                lines.append(f"Last Renewed Quota GB: {last_renewed_quota_text}")
                lines.append(f"Lifetime Bought: {_format_accounting_bytes(accounting.get('total_purchased_bytes'), accounting.get('unlimited_grant_count'))}")
                lines.append(f"Lifetime Used: {_format_accounting_bytes(accounting.get('total_consumed_bytes'))}")
                lines.append(f"Purchase Events: {int(accounting.get('purchase_event_count') or 0)}")
                lines.append(f"Renewal Events: {int(accounting.get('renewal_event_count') or 0)}")
                lines.append(f"Access URL: {key.access_url or 'N/A'}")
                lines.append("")

        customer_totals = queries.list_customer_accounting_totals()
        lines.append("=== CUSTOMER ACCOUNTING TOTALS ===")
        if not customer_totals:
            lines.append("No customer accounting totals recorded.")
        else:
            admin_username_map = {
                int(item["user_id"]): item.get("username")
                for item in queries.get_admin_profiles()
                if item.get("user_id") is not None
            }
            for item in customer_totals:
                user_id = int(item["user_id"])
                customer = queries.get_customer(user_id)
                username = customer.get("username") if customer else None
                if not username:
                    username = admin_username_map.get(user_id)
                lines.append(f"User ID: {user_id}")
                lines.append(f"Username: @{username}" if username else "Username: N/A")
                lines.append(f"Lifetime Bought: {_format_accounting_bytes(item.get('total_purchased_bytes'), item.get('unlimited_grant_count'))}")
                lines.append(f"Lifetime Used: {_format_accounting_bytes(item.get('total_consumed_bytes'))}")
                lines.append(f"Purchase Events: {int(item.get('purchase_event_count') or 0)}")
                lines.append(f"Renewal Events: {int(item.get('renewal_event_count') or 0)}")
                lines.append(f"Last Grant UTC: {to_utc_display(item.get('last_grant_at_utc'))}")
                lines.append(f"Last Usage UTC: {to_utc_display(item.get('last_consumed_at_utc'))}")
                lines.append("")

        recent_events = queries.get_service_accounting_events(limit=100)
        lines.append("=== RECENT ACCOUNTING EVENTS ===")
        if not recent_events:
            lines.append("No accounting events recorded.")
        else:
            for event in recent_events:
                lines.append(
                    f"{event.get('created_at_utc') or 'N/A'} | {event.get('event_type') or 'unknown'} | "
                    f"{event.get('server_alias')} / {event.get('key_id')} | "
                    f"User: {event.get('customer_user_id') or 'N/A'} | "
                    f"Bought: {_format_accounting_bytes(event.get('purchased_bytes'), 1 if event.get('is_unlimited') else 0)} | "
                    f"Used: {_format_accounting_bytes(event.get('consumed_bytes'))}"
                )
            lines.append("")

    Path(file_path).write_text("\n".join(lines), encoding="utf-8")
    return file_path


def get_latest_backup_file(kind: str) -> str | None:
    """Returns latest backup file path for given kind (manual/auto), if any."""
    backup_dir = Path(BACKUP_DIR)
    if not backup_dir.exists():
        return None

    pattern = f"{kind}_backup_*.txt"
    files = sorted(backup_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(files[0]) if files else None


async def run_auto_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Daily scheduled backup generator + sender."""
    recipients = _backup_recipients()
    if not recipients:
        return

    try:
        file_path = generate_backup_file("auto")
    except Exception as e:
        logger.error(f"Auto backup generation failed: {e}")
        return

    caption = "🗂️ Daily auto backup file"
    for user_id in recipients:
        try:
            with open(file_path, "rb") as backup_file:
                await context.bot.send_document(
                    chat_id=user_id,
                    document=backup_file,
                    filename=os.path.basename(file_path),
                    caption=caption,
                )
        except Exception as send_err:
            logger.warning(f"Failed sending auto backup to {user_id}: {send_err}")
