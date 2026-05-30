import sqlite3
import json
from datetime import datetime, timezone
from src.config import OWNER_ID
from src.database.connection import get_connection

USAGE_RESET_TOLERANCE_BYTES = 50 * 1_000_000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_key_metadata_row(conn, server_alias: str, key_id: str):
    cursor = conn.execute(
        'SELECT 1 FROM key_metadata WHERE server_alias = ? AND key_id = ?',
        (server_alias, key_id),
    )
    if not cursor.fetchone():
        conn.execute(
            '''
            INSERT INTO key_metadata (server_alias, key_id, is_sold, used_up_notified, is_expired, renew_count, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (server_alias, key_id, False, False, False, 0, _utc_now_iso()),
        )

# --- Admin Operations ---
def add_admin(user_id: int, username: str | None = None) -> bool:
    try:
        with get_connection() as conn:
            conn.execute('INSERT INTO admins (user_id, username) VALUES (?, ?)', (user_id, username))
            return True
    except sqlite3.IntegrityError:
        return False # Already an admin

def update_admin_username(user_id: int, username: str | None):
    with get_connection() as conn:
        conn.execute('UPDATE admins SET username = ? WHERE user_id = ?', (username, user_id))

def remove_admin(user_id: int):
    with get_connection() as conn:
        conn.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))

def get_admins() -> list[int]:
    with get_connection() as conn:
        cursor = conn.execute('SELECT user_id FROM admins')
        return [row['user_id'] for row in cursor.fetchall()]

def get_admin_profiles() -> list[dict]:
    with get_connection() as conn:
        cursor = conn.execute('SELECT user_id, username FROM admins ORDER BY user_id')
        return [
            {
                'user_id': row['user_id'],
                'username': row['username'],
            }
            for row in cursor.fetchall()
        ]

# --- Server Operations ---
def add_server(alias: str, api_url: str, cert_sha256: str, max_key_count: int = 0) -> bool:
    try:
        with get_connection() as conn:
            conn.execute(
                'INSERT INTO servers (alias, api_url, cert_sha256, max_key_count) VALUES (?, ?, ?, ?)', 
                (alias, api_url, cert_sha256, max_key_count)
            )
            return True
    except sqlite3.IntegrityError:
        return False # Alias already exists

def remove_server(alias: str):
    with get_connection() as conn:
        conn.execute('DELETE FROM servers WHERE alias = ?', (alias,))
        # Key metadata will be deleted automatically due to ON DELETE CASCADE (if PRAGMA foreign_keys is ON)
        conn.execute('DELETE FROM key_metadata WHERE server_alias = ?', (alias,))

def get_servers() -> dict:
    with get_connection() as conn:
        cursor = conn.execute('SELECT alias, api_url, cert_sha256, max_key_count FROM servers')
        # Return a dictionary mapped by alias for easy lookup
        return {
            row['alias']: {
                "api_url": row['api_url'], 
                "cert_sha256": row['cert_sha256'],
                "max_key_count": row['max_key_count']
            } 
            for row in cursor.fetchall()
        }

def get_server(alias: str) -> dict | None:
    with get_connection() as conn:
        cursor = conn.execute('SELECT api_url, cert_sha256, max_key_count FROM servers WHERE alias = ?', (alias,))
        row = cursor.fetchone()
        return dict(row) if row else None

def update_server_limit(alias: str, limit: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute('UPDATE servers SET max_key_count = ? WHERE alias = ?', (limit, alias))
        return cursor.rowcount > 0

# --- Key Metadata Operations (The "Sold" tag) ---
def toggle_key_sold(server_alias: str, key_id: str) -> bool:
    """Toggles the 'is_sold' status. Returns the new status."""
    with get_connection() as conn:
        # Check if it exists
        cursor = conn.execute('SELECT is_sold FROM key_metadata WHERE server_alias = ? AND key_id = ?', (server_alias, key_id))
        row = cursor.fetchone()
        
        if row:
            new_status = not row['is_sold']
            conn.execute('UPDATE key_metadata SET is_sold = ? WHERE server_alias = ? AND key_id = ?', 
                         (new_status, server_alias, key_id))
        else:
            new_status = True
            conn.execute('INSERT INTO key_metadata (server_alias, key_id, is_sold) VALUES (?, ?, ?)', 
                         (server_alias, key_id, new_status))
        return new_status

def set_key_sold(server_alias: str, key_id: str, is_sold: bool):
    """Sets sold status explicitly without toggling."""
    with get_connection() as conn:
        cursor = conn.execute(
            'SELECT 1 FROM key_metadata WHERE server_alias = ? AND key_id = ?',
            (server_alias, key_id),
        )
        if cursor.fetchone():
            conn.execute(
                'UPDATE key_metadata SET is_sold = ? WHERE server_alias = ? AND key_id = ?',
                (is_sold, server_alias, key_id),
            )
        else:
            conn.execute(
                '''
                INSERT INTO key_metadata
                (server_alias, key_id, is_sold, used_up_notified)
                VALUES (?, ?, ?, ?)
                ''',
                (server_alias, key_id, is_sold, False),
            )

def get_sold_keys(server_alias: str) -> set[str]:
    """Returns a set of key_ids that are marked as sold for a specific server."""
    with get_connection() as conn:
        cursor = conn.execute('SELECT key_id FROM key_metadata WHERE server_alias = ? AND is_sold = 1', (server_alias,))
        return {row['key_id'] for row in cursor.fetchall()}

def set_key_creator(server_alias: str, key_id: str, user_id: int | None, username: str | None):
    with get_connection() as conn:
        cursor = conn.execute(
            'SELECT 1 FROM key_metadata WHERE server_alias = ? AND key_id = ?',
            (server_alias, key_id),
        )
        if cursor.fetchone():
            conn.execute(
                '''
                UPDATE key_metadata
                SET created_by_user_id = ?, created_by_username = ?
                WHERE server_alias = ? AND key_id = ?
                ''',
                (user_id, username, server_alias, key_id),
            )
        else:
            conn.execute(
                '''
                INSERT INTO key_metadata
                (server_alias, key_id, is_sold, used_up_notified, created_by_user_id, created_by_username)
                VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (server_alias, key_id, False, False, user_id, username),
            )

def get_key_creators(server_alias: str) -> dict[str, str]:
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            SELECT key_id, created_by_username
            FROM key_metadata
            WHERE server_alias = ?
              AND created_by_username IS NOT NULL
              AND TRIM(created_by_username) != ''
            ''',
            (server_alias,),
        )
        return {row['key_id']: row['created_by_username'] for row in cursor.fetchall()}

def remove_key_metadata(server_alias: str, key_id: str):
    with get_connection() as conn:
        conn.execute('DELETE FROM key_metadata WHERE server_alias = ? AND key_id = ?', (server_alias, key_id))

def is_key_used_up_notified(server_alias: str, key_id: str) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            'SELECT used_up_notified FROM key_metadata WHERE server_alias = ? AND key_id = ?',
            (server_alias, key_id),
        )
        row = cursor.fetchone()
        return bool(row['used_up_notified']) if row else False

def set_key_used_up_notified(server_alias: str, key_id: str, notified: bool):
    with get_connection() as conn:
        cursor = conn.execute(
            'SELECT 1 FROM key_metadata WHERE server_alias = ? AND key_id = ?',
            (server_alias, key_id),
        )
        if cursor.fetchone():
            conn.execute(
                'UPDATE key_metadata SET used_up_notified = ? WHERE server_alias = ? AND key_id = ?',
                (notified, server_alias, key_id),
            )
        else:
            conn.execute(
                'INSERT INTO key_metadata (server_alias, key_id, is_sold, used_up_notified) VALUES (?, ?, ?, ?)',
                (server_alias, key_id, False, notified),
            )

def is_user_notification_enabled(user_id: int) -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            'SELECT is_enabled FROM user_notification_settings WHERE user_id = ?',
            (user_id,),
        )
        row = cursor.fetchone()
        return bool(row['is_enabled']) if row else True

def set_user_notification_enabled(user_id: int, is_enabled: bool):
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO user_notification_settings (user_id, is_enabled)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET is_enabled = excluded.is_enabled
            ''',
            (user_id, is_enabled),
        )

def get_notification_recipients() -> list[int]:
    candidates = sorted(set([OWNER_ID, *get_admins()]))
    return [user_id for user_id in candidates if is_user_notification_enabled(user_id)]


def is_admin_registration_review_notifications_enabled() -> bool:
    with get_connection() as conn:
        cursor = conn.execute(
            'SELECT notify_admins_enabled FROM registration_review_settings WHERE id = 1'
        )
        row = cursor.fetchone()
        return bool(row['notify_admins_enabled']) if row else True


def set_admin_registration_review_notifications_enabled(is_enabled: bool):
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO registration_review_settings (id, notify_admins_enabled)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET notify_admins_enabled = excluded.notify_admins_enabled
            ''',
            (is_enabled,),
        )


# --- Phase A: Key Lifecycle and Ownership Helpers ---
def set_key_expiry(server_alias: str, key_id: str, expiry_at_utc: str | None):
    with get_connection() as conn:
        _ensure_key_metadata_row(conn, server_alias, key_id)
        conn.execute(
            '''
            UPDATE key_metadata
            SET expiry_at_utc = ?, is_expired = 0, auto_disabled_at_utc = NULL
            WHERE server_alias = ? AND key_id = ?
            ''',
            (expiry_at_utc, server_alias, key_id),
        )


def set_key_assignment(server_alias: str, key_id: str, assigned_user_id: int | None):
    with get_connection() as conn:
        _ensure_key_metadata_row(conn, server_alias, key_id)
        conn.execute(
            '''
            UPDATE key_metadata
            SET assigned_user_id = ?
            WHERE server_alias = ? AND key_id = ?
            ''',
            (assigned_user_id, server_alias, key_id),
        )


def mark_key_expired(server_alias: str, key_id: str, auto_disabled_at_utc: str | None = None):
    with get_connection() as conn:
        _ensure_key_metadata_row(conn, server_alias, key_id)
        conn.execute(
            '''
            UPDATE key_metadata
            SET is_expired = 1,
                auto_disabled_at_utc = COALESCE(?, auto_disabled_at_utc)
            WHERE server_alias = ? AND key_id = ?
            ''',
            (auto_disabled_at_utc or _utc_now_iso(), server_alias, key_id),
        )


def clear_key_expired(server_alias: str, key_id: str):
    with get_connection() as conn:
        _ensure_key_metadata_row(conn, server_alias, key_id)
        conn.execute(
            '''
            UPDATE key_metadata
            SET is_expired = 0,
                auto_disabled_at_utc = NULL
            WHERE server_alias = ? AND key_id = ?
            ''',
            (server_alias, key_id),
        )


def record_key_renewal(server_alias: str, key_id: str, quota_gb: float | None, renewed_at_utc: str | None = None):
    ts = renewed_at_utc or _utc_now_iso()
    with get_connection() as conn:
        _ensure_key_metadata_row(conn, server_alias, key_id)
        conn.execute(
            '''
            UPDATE key_metadata
            SET renew_count = COALESCE(renew_count, 0) + 1,
                last_renewed_at_utc = ?,
                last_renewed_quota_gb = ?,
                last_observed_used_bytes = 0,
                usage_reset_offset_bytes = 0,
                max_effective_used_bytes = 0,
                last_usage_sync_at_utc = ?,
                is_expired = 0,
                auto_disabled_at_utc = NULL
            WHERE server_alias = ? AND key_id = ?
            ''',
            (ts, quota_gb, ts, server_alias, key_id),
        )


def get_key_lifecycle(server_alias: str, key_id: str) -> dict | None:
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            SELECT expiry_at_utc, is_expired, auto_disabled_at_utc, assigned_user_id,
                   renew_count, last_renewed_at_utc, last_renewed_quota_gb,
                   last_observed_used_bytes, usage_reset_offset_bytes, max_effective_used_bytes,
                   last_usage_sync_at_utc, created_at_utc
            FROM key_metadata
            WHERE server_alias = ? AND key_id = ?
            ''',
            (server_alias, key_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def observe_key_usage(server_alias: str, key_id: str, live_used_bytes: int | None, observed_at_utc: str | None = None) -> int:
    """Tracks effective usage so quota cannot increase when upstream counters reset."""
    ts = observed_at_utc or _utc_now_iso()
    live_bytes = max(int(live_used_bytes or 0), 0)

    with get_connection() as conn:
        _ensure_key_metadata_row(conn, server_alias, key_id)
        cursor = conn.execute(
            '''
            SELECT last_observed_used_bytes, usage_reset_offset_bytes, max_effective_used_bytes
            FROM key_metadata
            WHERE server_alias = ? AND key_id = ?
            ''',
            (server_alias, key_id),
        )
        row = cursor.fetchone()

        last_observed = int(row["last_observed_used_bytes"] or 0) if row else 0
        reset_offset = int(row["usage_reset_offset_bytes"] or 0) if row else 0
        max_effective = int(row["max_effective_used_bytes"] or 0) if row else 0

        if last_observed > live_bytes + USAGE_RESET_TOLERANCE_BYTES:
            reset_offset += last_observed

        effective_used = reset_offset + live_bytes
        if effective_used < max_effective:
            effective_used = max_effective
        else:
            max_effective = effective_used

        conn.execute(
            '''
            UPDATE key_metadata
            SET last_observed_used_bytes = ?,
                usage_reset_offset_bytes = ?,
                max_effective_used_bytes = ?,
                last_usage_sync_at_utc = ?
            WHERE server_alias = ? AND key_id = ?
            ''',
            (live_bytes, reset_offset, max_effective, ts, server_alias, key_id),
        )

    return effective_used


def list_keys_needing_expiry_check(now_utc: str) -> list[dict]:
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            SELECT server_alias, key_id, expiry_at_utc
            FROM key_metadata
            WHERE expiry_at_utc IS NOT NULL
              AND TRIM(expiry_at_utc) != ''
              AND COALESCE(is_expired, 0) = 0
              AND expiry_at_utc <= ?
            ''',
            (now_utc,),
        )
        return [dict(row) for row in cursor.fetchall()]


# --- Phase A: Customer Registration and Approval Helpers ---
def upsert_customer(user_id: int, username: str | None, first_name: str | None, status: str = 'pending'):
    now = _utc_now_iso()
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO customers (user_id, username, first_name, status, created_at_utc, updated_at_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                status = CASE
                    WHEN customers.status = 'approved' THEN customers.status
                    ELSE excluded.status
                END,
                updated_at_utc = excluded.updated_at_utc
            ''',
            (user_id, username, first_name, status, now, now),
        )


def set_customer_status(user_id: int, status: str, approved_by: int | None = None):
    now = _utc_now_iso()
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE customers
            SET status = ?,
                approved_by = ?,
                approved_at_utc = CASE WHEN ? = 'approved' THEN ? ELSE approved_at_utc END,
                updated_at_utc = ?
            WHERE user_id = ?
            ''',
            (status, approved_by, status, now, now, user_id),
        )


def get_customer(user_id: int) -> dict | None:
    with get_connection() as conn:
        cursor = conn.execute(
            'SELECT user_id, username, first_name, status, approved_by, approved_at_utc, created_at_utc, updated_at_utc FROM customers WHERE user_id = ?',
            (user_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_customers_by_status(status: str) -> list[dict]:
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            SELECT user_id, username, first_name, status, approved_by, approved_at_utc, created_at_utc, updated_at_utc
            FROM customers
            WHERE status = ?
            ORDER BY created_at_utc
            ''',
            (status,),
        )
        return [dict(row) for row in cursor.fetchall()]


def get_user_assigned_keys(user_id: int) -> list[dict]:
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            SELECT server_alias, key_id, expiry_at_utc, is_expired, assigned_user_id,
                   renew_count, last_renewed_at_utc, last_renewed_quota_gb, created_at_utc
            FROM key_metadata
            WHERE assigned_user_id = ?
            ORDER BY server_alias, key_id
            ''',
            (user_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


def clear_user_key_assignments(user_id: int):
    with get_connection() as conn:
        conn.execute(
            '''
            UPDATE key_metadata
            SET assigned_user_id = NULL
            WHERE assigned_user_id = ?
            ''',
            (user_id,),
        )


def remove_customer(user_id: int):
    with get_connection() as conn:
        conn.execute('DELETE FROM customers WHERE user_id = ?', (user_id,))


# --- Phase A: Lifecycle Event Helpers ---
def add_key_lifecycle_event(
    server_alias: str,
    key_id: str,
    event_type: str,
    actor_user_id: int | None = None,
    actor_username: str | None = None,
    payload: dict | None = None,
    created_at_utc: str | None = None,
):
    with get_connection() as conn:
        conn.execute(
            '''
            INSERT INTO key_lifecycle_events
            (server_alias, key_id, event_type, actor_user_id, actor_username, event_payload_json, created_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                server_alias,
                key_id,
                event_type,
                actor_user_id,
                actor_username,
                json.dumps(payload or {}, ensure_ascii=False),
                created_at_utc or _utc_now_iso(),
            ),
        )


def get_key_lifecycle_events(server_alias: str, key_id: str, limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            SELECT id, server_alias, key_id, event_type, actor_user_id, actor_username, event_payload_json, created_at_utc
            FROM key_lifecycle_events
            WHERE server_alias = ? AND key_id = ?
            ORDER BY id DESC
            LIMIT ?
            ''',
            (server_alias, key_id, limit),
        )
        return [dict(row) for row in cursor.fetchall()]