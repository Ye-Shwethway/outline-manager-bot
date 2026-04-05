import sqlite3
from src.database.connection import get_connection

# --- Admin Operations ---
def add_admin(user_id: int) -> bool:
    try:
        with get_connection() as conn:
            conn.execute('INSERT INTO admins (user_id) VALUES (?)', (user_id,))
            return True
    except sqlite3.IntegrityError:
        return False # Already an admin

def remove_admin(user_id: int):
    with get_connection() as conn:
        conn.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))

def get_admins() -> list[int]:
    with get_connection() as conn:
        cursor = conn.execute('SELECT user_id FROM admins')
        return [row['user_id'] for row in cursor.fetchall()]

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

def get_sold_keys(server_alias: str) -> set[str]:
    """Returns a set of key_ids that are marked as sold for a specific server."""
    with get_connection() as conn:
        cursor = conn.execute('SELECT key_id FROM key_metadata WHERE server_alias = ? AND is_sold = 1', (server_alias,))
        return {row['key_id'] for row in cursor.fetchall()}

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

def is_notification_enabled() -> bool:
    with get_connection() as conn:
        cursor = conn.execute('SELECT is_enabled FROM notification_settings WHERE id = 1')
        row = cursor.fetchone()
        return bool(row['is_enabled']) if row else True

def set_notification_enabled(is_enabled: bool):
    with get_connection() as conn:
        conn.execute('UPDATE notification_settings SET is_enabled = ? WHERE id = 1', (is_enabled,))