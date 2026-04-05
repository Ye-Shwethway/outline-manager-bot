import sqlite3
import os
import logging
from src.config import DB_PATH

logger = logging.getLogger(__name__)

def get_connection():
    """Helper to get a database connection."""
    # SQLite natively returns rows as tuples. Row factory allows dict-like access.
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize the database and create tables if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    
    with get_connection() as conn:
        cursor = conn.cursor()
        
        # 1. Servers Table (now includes max_key_count)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS servers (
                alias TEXT PRIMARY KEY,
                api_url TEXT NOT NULL,
                cert_sha256 TEXT NOT NULL,
                max_key_count INTEGER DEFAULT 0
            )
        ''')
        
        # 2. Admins Table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        ''')
        
        # 3. Key Metadata Table (to track "sold" status)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS key_metadata (
                server_alias TEXT,
                key_id TEXT,
                is_sold BOOLEAN DEFAULT 0,
                used_up_notified BOOLEAN DEFAULT 0,
                PRIMARY KEY (server_alias, key_id),
                FOREIGN KEY (server_alias) REFERENCES servers(alias) ON DELETE CASCADE
            )
        ''')

        # 4. Notification Settings Table (single-row config)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                is_enabled BOOLEAN DEFAULT 1
            )
        ''')

        # 5. Per-user notification preferences (owner/admin each can toggle).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_notification_settings (
                user_id INTEGER PRIMARY KEY,
                is_enabled BOOLEAN DEFAULT 1
            )
        ''')

        # Backward-compatible migration for older DBs missing used_up_notified.
        cursor.execute("PRAGMA table_info(key_metadata)")
        key_metadata_columns = {row[1] for row in cursor.fetchall()}
        if "used_up_notified" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN used_up_notified BOOLEAN DEFAULT 0")

        # Ensure settings row always exists.
        cursor.execute(
            "INSERT OR IGNORE INTO notification_settings (id, is_enabled) VALUES (1, 1)"
        )

        # Backfill per-user settings from current owner/admin list, respecting old global switch.
        cursor.execute("SELECT is_enabled FROM notification_settings WHERE id = 1")
        global_enabled_row = cursor.fetchone()
        global_enabled = bool(global_enabled_row[0]) if global_enabled_row else True

        cursor.execute("SELECT user_id FROM admins")
        admin_ids = [row[0] for row in cursor.fetchall()]

        from src.config import OWNER_ID
        for user_id in [OWNER_ID, *admin_ids]:
            cursor.execute(
                "INSERT OR IGNORE INTO user_notification_settings (user_id, is_enabled) VALUES (?, ?)",
                (user_id, global_enabled),
            )
        
        conn.commit()
        logger.info("Database initialized successfully.")