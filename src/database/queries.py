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


def _ensure_key_accounting_row(conn, server_alias: str, key_id: str):
    cursor = conn.execute(
        'SELECT 1 FROM key_accounting_totals WHERE server_alias = ? AND key_id = ?',
        (server_alias, key_id),
    )
    if not cursor.fetchone():
        now_utc = _utc_now_iso()
        conn.execute(
            '''
            INSERT INTO key_accounting_totals (
                server_alias, key_id, created_at_utc, updated_at_utc
            )
            VALUES (?, ?, ?, ?)
            ''',
            (server_alias, key_id, now_utc, now_utc),
        )


def _ensure_customer_accounting_row(conn, user_id: int):
    cursor = conn.execute(
        'SELECT 1 FROM customer_accounting_totals WHERE user_id = ?',
        (user_id,),
    )
    if not cursor.fetchone():
        now_utc = _utc_now_iso()
        conn.execute(
            '''
            INSERT INTO customer_accounting_totals (
                user_id, first_recorded_at_utc, updated_at_utc
            )
            VALUES (?, ?, ?)
            ''',
            (user_id, now_utc, now_utc),
        )


def _record_accounting_event(
    conn,
    server_alias: str,
    key_id: str,
    event_type: str,
    customer_user_id: int | None = None,
    purchased_bytes: int = 0,
    consumed_bytes: int = 0,
    is_unlimited: bool = False,
    metadata: dict | None = None,
    created_at_utc: str | None = None,
):
    ts = created_at_utc or _utc_now_iso()
    purchased_value = max(int(purchased_bytes or 0), 0)
    consumed_value = max(int(consumed_bytes or 0), 0)
    unlimited_value = bool(is_unlimited)

    _ensure_key_accounting_row(conn, server_alias, key_id)
    conn.execute(
        '''
        INSERT INTO service_accounting_events (
            server_alias, key_id, customer_user_id, event_type,
            purchased_bytes, consumed_bytes, is_unlimited, metadata_json, created_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''',
        (
            server_alias,
            key_id,
            customer_user_id,
            event_type,
            purchased_value,
            consumed_value,
            unlimited_value,
            json.dumps(metadata or {}, ensure_ascii=False),
            ts,
        ),
    )

    purchase_event_increment = 1 if event_type == 'grant' else 0
    renewal_event_increment = 1 if event_type == 'renewal_grant' else 0
    renewed_bytes_increment = purchased_value if event_type == 'renewal_grant' else 0

    conn.execute(
        '''
        UPDATE key_accounting_totals
        SET total_purchased_bytes = total_purchased_bytes + ?,
            total_consumed_bytes = total_consumed_bytes + ?,
            total_renewed_bytes = total_renewed_bytes + ?,
            purchase_event_count = purchase_event_count + ?,
            renewal_event_count = renewal_event_count + ?,
            unlimited_grant_count = unlimited_grant_count + ?,
            last_grant_bytes = CASE WHEN ? > 0 OR ? THEN ? ELSE last_grant_bytes END,
            last_grant_unlimited = CASE WHEN ? > 0 OR ? THEN ? ELSE last_grant_unlimited END,
            last_grant_at_utc = CASE WHEN ? > 0 OR ? THEN ? ELSE last_grant_at_utc END,
            last_consumed_at_utc = CASE WHEN ? > 0 THEN ? ELSE last_consumed_at_utc END,
            updated_at_utc = ?
        WHERE server_alias = ? AND key_id = ?
        ''',
        (
            purchased_value,
            consumed_value,
            renewed_bytes_increment,
            purchase_event_increment,
            renewal_event_increment,
            1 if unlimited_value else 0,
            purchased_value,
            1 if unlimited_value else 0,
            purchased_value,
            purchased_value,
            1 if unlimited_value else 0,
            1 if unlimited_value else 0,
            purchased_value,
            1 if unlimited_value else 0,
            ts,
            consumed_value,
            ts,
            ts,
            server_alias,
            key_id,
        ),
    )

    if customer_user_id is not None:
        _ensure_customer_accounting_row(conn, int(customer_user_id))
        conn.execute(
            '''
            UPDATE customer_accounting_totals
            SET total_purchased_bytes = total_purchased_bytes + ?,
                total_consumed_bytes = total_consumed_bytes + ?,
                total_renewed_bytes = total_renewed_bytes + ?,
                purchase_event_count = purchase_event_count + ?,
                renewal_event_count = renewal_event_count + ?,
                unlimited_grant_count = unlimited_grant_count + ?,
                last_grant_bytes = CASE WHEN ? > 0 OR ? THEN ? ELSE last_grant_bytes END,
                last_grant_unlimited = CASE WHEN ? > 0 OR ? THEN ? ELSE last_grant_unlimited END,
                last_grant_at_utc = CASE WHEN ? > 0 OR ? THEN ? ELSE last_grant_at_utc END,
                last_consumed_at_utc = CASE WHEN ? > 0 THEN ? ELSE last_consumed_at_utc END,
                updated_at_utc = ?
            WHERE user_id = ?
            ''',
            (
                purchased_value,
                consumed_value,
                renewed_bytes_increment,
                purchase_event_increment,
                renewal_event_increment,
                1 if unlimited_value else 0,
                purchased_value,
                1 if unlimited_value else 0,
                purchased_value,
                purchased_value,
                1 if unlimited_value else 0,
                1 if unlimited_value else 0,
                purchased_value,
                1 if unlimited_value else 0,
                ts,
                consumed_value,
                ts,
                ts,
                int(customer_user_id),
            ),
        )


def record_key_data_grant(
    server_alias: str,
    key_id: str,
    quota_bytes: int | None,
    customer_user_id: int | None = None,
    *,
    is_renewal: bool = False,
    is_unlimited: bool = False,
    created_at_utc: str | None = None,
    metadata: dict | None = None,
):
    grant_bytes = max(int(quota_bytes or 0), 0)
    with get_connection() as conn:
        _record_accounting_event(
            conn,
            server_alias,
            key_id,
            event_type='renewal_grant' if is_renewal else 'grant',
            customer_user_id=customer_user_id,
            purchased_bytes=grant_bytes,
            consumed_bytes=0,
            is_unlimited=is_unlimited,
            metadata=metadata,
            created_at_utc=created_at_utc,
        )


def record_assignment_sale_grant(
    server_alias: str,
    key_id: str,
    customer_user_id: int,
    quota_bytes: int | None,
    *,
    is_unlimited: bool = False,
    created_at_utc: str | None = None,
    metadata: dict | None = None,
):
    record_key_data_grant(
        server_alias,
        key_id,
        quota_bytes,
        customer_user_id=customer_user_id,
        is_renewal=False,
        is_unlimited=is_unlimited,
        created_at_utc=created_at_utc,
        metadata=metadata,
    )


def get_key_accounting_totals(server_alias: str, key_id: str) -> dict | None:
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            SELECT server_alias, key_id, current_assigned_user_id,
                   total_purchased_bytes, total_consumed_bytes, total_renewed_bytes,
                   purchase_event_count, renewal_event_count, unlimited_grant_count,
                   last_grant_bytes, last_grant_unlimited, last_grant_at_utc,
                   last_consumed_at_utc, created_at_utc, updated_at_utc
            FROM key_accounting_totals
            WHERE server_alias = ? AND key_id = ?
            ''',
            (server_alias, key_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_customer_accounting_totals(user_id: int) -> dict | None:
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            SELECT user_id, total_purchased_bytes, total_consumed_bytes, total_renewed_bytes,
                   purchase_event_count, renewal_event_count, unlimited_grant_count,
                   last_grant_bytes, last_grant_unlimited, first_recorded_at_utc,
                   last_grant_at_utc, last_consumed_at_utc, updated_at_utc
            FROM customer_accounting_totals
            WHERE user_id = ?
            ''',
            (user_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def list_customer_accounting_totals() -> list[dict]:
    with get_connection() as conn:
        cursor = conn.execute(
            '''
            SELECT user_id, total_purchased_bytes, total_consumed_bytes, total_renewed_bytes,
                   purchase_event_count, renewal_event_count, unlimited_grant_count,
                   last_grant_bytes, last_grant_unlimited, first_recorded_at_utc,
                   last_grant_at_utc, last_consumed_at_utc, updated_at_utc
            FROM customer_accounting_totals
            ORDER BY total_purchased_bytes DESC, total_consumed_bytes DESC, user_id ASC
            '''
        )
        return [dict(row) for row in cursor.fetchall()]


def get_customer_accounting_leaderboard(metric: str, limit: int = 10) -> list[dict]:
    metric_sql_map = {
        'bought': 'total_purchased_bytes DESC, unlimited_grant_count DESC, user_id ASC',
        'used': 'total_consumed_bytes DESC, user_id ASC',
        'renewals': 'renewal_event_count DESC, total_renewed_bytes DESC, user_id ASC',
    }
    order_sql = metric_sql_map.get(metric, metric_sql_map['bought'])

    with get_connection() as conn:
        cursor = conn.execute(
            f'''
            SELECT user_id, total_purchased_bytes, total_consumed_bytes, total_renewed_bytes,
                   purchase_event_count, renewal_event_count, unlimited_grant_count,
                   last_grant_bytes, last_grant_unlimited, first_recorded_at_utc,
                   last_grant_at_utc, last_consumed_at_utc, updated_at_utc
            FROM customer_accounting_totals
            ORDER BY {order_sql}
            LIMIT ?
            ''',
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]


def get_service_accounting_events(
    server_alias: str | None = None,
    key_id: str | None = None,
    customer_user_id: int | None = None,
    limit: int = 50,
) -> list[dict]:
    where_clauses = []
    params: list = []
    if server_alias is not None:
        where_clauses.append('server_alias = ?')
        params.append(server_alias)
    if key_id is not None:
        where_clauses.append('key_id = ?')
        params.append(key_id)
    if customer_user_id is not None:
        where_clauses.append('customer_user_id = ?')
        params.append(customer_user_id)

    where_sql = ('WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''
    with get_connection() as conn:
        cursor = conn.execute(
            f'''
            SELECT id, server_alias, key_id, customer_user_id, event_type,
                   purchased_bytes, consumed_bytes, is_unlimited, metadata_json, created_at_utc
            FROM service_accounting_events
            {where_sql}
            ORDER BY id DESC
            LIMIT ?
            ''',
            (*params, limit),
        )
        return [dict(row) for row in cursor.fetchall()]


def _get_accounting_backfill_version(conn) -> str | None:
    cursor = conn.execute('SELECT backfill_version FROM accounting_backfill_state WHERE id = 1')
    row = cursor.fetchone()
    return row['backfill_version'] if row else None


def _set_accounting_backfill_version(conn, version: str):
    conn.execute(
        '''
        INSERT INTO accounting_backfill_state (id, backfill_version, completed_at_utc)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            backfill_version = excluded.backfill_version,
            completed_at_utc = excluded.completed_at_utc
        ''',
        (version, _utc_now_iso()),
    )


def _key_has_accounting_events(conn, server_alias: str, key_id: str) -> bool:
    cursor = conn.execute(
        '''
        SELECT 1
        FROM service_accounting_events
        WHERE server_alias = ? AND key_id = ?
        LIMIT 1
        ''',
        (server_alias, key_id),
    )
    return cursor.fetchone() is not None


def _backfill_key_accounting_from_history(conn, key_row: sqlite3.Row) -> bool:
    server_alias = key_row['server_alias']
    key_id = str(key_row['key_id'])
    if _key_has_accounting_events(conn, server_alias, key_id):
        return False

    _ensure_key_accounting_row(conn, server_alias, key_id)

    current_assigned_user_id = int(key_row['assigned_user_id']) if key_row['assigned_user_id'] else None
    conn.execute(
        '''
        UPDATE key_accounting_totals
        SET current_assigned_user_id = ?, updated_at_utc = ?
        WHERE server_alias = ? AND key_id = ?
        ''',
        (current_assigned_user_id, _utc_now_iso(), server_alias, key_id),
    )

    cursor = conn.execute(
        '''
        SELECT event_type, event_payload_json, created_at_utc, id
        FROM key_lifecycle_events
        WHERE server_alias = ? AND key_id = ?
        ORDER BY COALESCE(created_at_utc, ''), id
        ''',
        (server_alias, key_id),
    )
    lifecycle_events = cursor.fetchall()

    assigned_user_id = None
    saw_grant_event = False
    saw_usage_event = False

    for row in lifecycle_events:
        event_type = row['event_type']
        payload_raw = row['event_payload_json'] or '{}'
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError:
            payload = {}
        created_at_utc = row['created_at_utc'] or _utc_now_iso()

        if event_type == 'assigned_user':
            candidate = payload.get('assigned_user_id')
            assigned_user_id = int(candidate) if candidate is not None else None
            continue
        if event_type == 'unassigned_user':
            assigned_user_id = None
            continue
        if event_type != 'renew':
            continue

        try:
            quota_gb_value = float(payload.get('quota_gb') or 0)
        except (TypeError, ValueError):
            quota_gb_value = 0.0

        _record_accounting_event(
            conn,
            server_alias,
            key_id,
            event_type='renewal_grant',
            customer_user_id=assigned_user_id,
            purchased_bytes=int(quota_gb_value * 1_000_000_000) if quota_gb_value > 0 else 0,
            consumed_bytes=0,
            is_unlimited=quota_gb_value == 0,
            metadata={
                'source': 'historical_lifecycle_backfill',
                'days': payload.get('days'),
                'expiry_at_utc': payload.get('expiry_at_utc'),
            },
            created_at_utc=created_at_utc,
        )
        saw_grant_event = True

    if not saw_grant_event and key_row['last_renewed_quota_gb'] is not None:
        try:
            last_quota_gb = float(key_row['last_renewed_quota_gb'] or 0)
        except (TypeError, ValueError):
            last_quota_gb = 0.0

        _record_accounting_event(
            conn,
            server_alias,
            key_id,
            event_type='renewal_grant',
            customer_user_id=current_assigned_user_id,
            purchased_bytes=int(last_quota_gb * 1_000_000_000) if last_quota_gb > 0 else 0,
            consumed_bytes=0,
            is_unlimited=last_quota_gb == 0,
            metadata={'source': 'renew_snapshot_backfill'},
            created_at_utc=key_row['last_renewed_at_utc'] or key_row['created_at_utc'] or _utc_now_iso(),
        )
        saw_grant_event = True

    return saw_grant_event or saw_usage_event


def run_accounting_backfill(version: str = 'v1') -> dict:
    with get_connection() as conn:
        current_version = _get_accounting_backfill_version(conn)
        if current_version == version:
            return {'ran': False, 'version': version, 'backfilled_keys': 0}

        cursor = conn.execute(
            '''
            SELECT server_alias, key_id, assigned_user_id, last_renewed_at_utc,
                                         last_renewed_quota_gb, created_at_utc
            FROM key_metadata
            ORDER BY server_alias, key_id
            '''
        )
        rows = cursor.fetchall()

        backfilled_keys = 0
        for row in rows:
            if _backfill_key_accounting_from_history(conn, row):
                backfilled_keys += 1

        _set_accounting_backfill_version(conn, version)
        return {'ran': True, 'version': version, 'backfilled_keys': backfilled_keys}

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


def set_key_configured_limit(
    server_alias: str,
    key_id: str,
    quota_bytes: int | None,
    *,
    limit_mode: str,
):
    mode = (limit_mode or "").strip().lower()
    if mode not in {"limited", "unlimited"}:
        raise ValueError("limit_mode must be 'limited' or 'unlimited'")

    configured_bytes = max(int(quota_bytes or 0), 0) if mode == "limited" else None
    with get_connection() as conn:
        _ensure_key_metadata_row(conn, server_alias, key_id)
        conn.execute(
            '''
            UPDATE key_metadata
            SET configured_limit_mode = ?,
                configured_limit_bytes = ?
            WHERE server_alias = ? AND key_id = ?
            ''',
            (mode, configured_bytes, server_alias, key_id),
        )


def set_key_assignment(server_alias: str, key_id: str, assigned_user_id: int | None):
    with get_connection() as conn:
        _ensure_key_metadata_row(conn, server_alias, key_id)
        _ensure_key_accounting_row(conn, server_alias, key_id)
        conn.execute(
            '''
            UPDATE key_metadata
            SET assigned_user_id = ?
            WHERE server_alias = ? AND key_id = ?
            ''',
            (assigned_user_id, server_alias, key_id),
        )
        conn.execute(
            '''
            UPDATE key_accounting_totals
            SET current_assigned_user_id = ?, updated_at_utc = ?
            WHERE server_alias = ? AND key_id = ?
            ''',
            (assigned_user_id, _utc_now_iso(), server_alias, key_id),
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


def mark_key_quota_blocked(server_alias: str, key_id: str, limit_bytes: int, blocked_at_utc: str | None = None):
    with get_connection() as conn:
        _ensure_key_metadata_row(conn, server_alias, key_id)
        conn.execute(
            '''
            UPDATE key_metadata
            SET quota_blocked_at_utc = ?,
                quota_block_limit_bytes = ?,
                used_up_notified = 1
            WHERE server_alias = ? AND key_id = ?
            ''',
            (blocked_at_utc or _utc_now_iso(), max(int(limit_bytes or 0), 0), server_alias, key_id),
        )


def clear_key_quota_block(server_alias: str, key_id: str):
    with get_connection() as conn:
        _ensure_key_metadata_row(conn, server_alias, key_id)
        conn.execute(
            '''
            UPDATE key_metadata
            SET quota_blocked_at_utc = NULL,
                quota_block_limit_bytes = NULL,
                used_up_notified = 0
            WHERE server_alias = ? AND key_id = ?
            ''',
            (server_alias, key_id),
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


def record_key_renewal(
    server_alias: str,
    key_id: str,
    quota_gb: float | None,
    renewed_at_utc: str | None = None,
    baseline_used_bytes: int | None = None,
):
    ts = renewed_at_utc or _utc_now_iso()
    baseline_bytes = max(int(baseline_used_bytes or 0), 0)
    configured_bytes = None if float(quota_gb or 0) == 0 else baseline_bytes + int(float(quota_gb or 0) * 1_000_000_000)
    with get_connection() as conn:
        _ensure_key_metadata_row(conn, server_alias, key_id)
        _ensure_key_accounting_row(conn, server_alias, key_id)
        conn.execute(
            '''
            UPDATE key_metadata
            SET renew_count = COALESCE(renew_count, 0) + 1,
                last_renewed_at_utc = ?,
                last_renewed_quota_gb = ?,
                configured_limit_mode = ?,
                configured_limit_bytes = ?,
                is_expired = 0,
                auto_disabled_at_utc = NULL,
                quota_blocked_at_utc = NULL,
                quota_block_limit_bytes = NULL,
                used_up_notified = 0
            WHERE server_alias = ? AND key_id = ?
            ''',
            (
                ts,
                quota_gb,
                "unlimited" if float(quota_gb or 0) == 0 else "limited",
                configured_bytes,
                server_alias,
                key_id,
            ),
        )


def get_key_lifecycle(server_alias: str, key_id: str) -> dict | None:
    with get_connection() as conn:
        cursor = conn.execute(
            '''
                 SELECT expiry_at_utc, is_expired, auto_disabled_at_utc,
                     configured_limit_mode, configured_limit_bytes, assigned_user_id,
                     quota_blocked_at_utc, quota_block_limit_bytes,
                                     renew_count, last_renewed_at_utc, last_renewed_quota_gb, created_at_utc
            FROM key_metadata
            WHERE server_alias = ? AND key_id = ?
            ''',
            (server_alias, key_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


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