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
                user_id INTEGER PRIMARY KEY,
                username TEXT
            )
        ''')
        
        # 3. Key Metadata Table (to track "sold" status)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS key_metadata (
                server_alias TEXT,
                key_id TEXT,
                is_sold BOOLEAN DEFAULT 0,
                used_up_notified BOOLEAN DEFAULT 0,
                created_by_user_id INTEGER,
                created_by_username TEXT,
                expiry_at_utc TEXT,
                is_expired BOOLEAN DEFAULT 0,
                auto_disabled_at_utc TEXT,
                configured_limit_mode TEXT,
                configured_limit_bytes INTEGER,
                quota_blocked_at_utc TEXT,
                quota_block_limit_bytes INTEGER,
                assigned_user_id INTEGER,
                renew_count INTEGER DEFAULT 0,
                last_renewed_at_utc TEXT,
                last_renewed_quota_gb REAL,
                created_at_utc TEXT,
                PRIMARY KEY (server_alias, key_id),
                FOREIGN KEY (server_alias) REFERENCES servers(alias) ON DELETE CASCADE
            )
        ''')

        # 3.1 Customers Table (user role + approval workflow)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS customers (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                status TEXT DEFAULT 'pending',
                approved_by INTEGER,
                approved_at_utc TEXT,
                created_at_utc TEXT,
                updated_at_utc TEXT
            )
        ''')

        # 3.2 Key Lifecycle Events (audit trail)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS key_lifecycle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_alias TEXT NOT NULL,
                key_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor_user_id INTEGER,
                actor_username TEXT,
                event_payload_json TEXT,
                created_at_utc TEXT
            )
        ''')

        # 3.3 Key accounting totals (durable lifetime stats per key).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS key_accounting_totals (
                server_alias TEXT NOT NULL,
                key_id TEXT NOT NULL,
                current_assigned_user_id INTEGER,
                total_purchased_bytes INTEGER DEFAULT 0,
                total_consumed_bytes INTEGER DEFAULT 0,
                total_renewed_bytes INTEGER DEFAULT 0,
                purchase_event_count INTEGER DEFAULT 0,
                renewal_event_count INTEGER DEFAULT 0,
                unlimited_grant_count INTEGER DEFAULT 0,
                last_grant_bytes INTEGER DEFAULT 0,
                last_grant_unlimited BOOLEAN DEFAULT 0,
                last_grant_at_utc TEXT,
                last_consumed_at_utc TEXT,
                created_at_utc TEXT,
                updated_at_utc TEXT,
                PRIMARY KEY (server_alias, key_id),
                FOREIGN KEY (server_alias) REFERENCES servers(alias) ON DELETE CASCADE
            )
        ''')

        # 3.4 Customer accounting totals (durable lifetime stats per customer).
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS customer_accounting_totals (
                user_id INTEGER PRIMARY KEY,
                total_purchased_bytes INTEGER DEFAULT 0,
                total_consumed_bytes INTEGER DEFAULT 0,
                total_renewed_bytes INTEGER DEFAULT 0,
                purchase_event_count INTEGER DEFAULT 0,
                renewal_event_count INTEGER DEFAULT 0,
                unlimited_grant_count INTEGER DEFAULT 0,
                last_grant_bytes INTEGER DEFAULT 0,
                last_grant_unlimited BOOLEAN DEFAULT 0,
                first_recorded_at_utc TEXT,
                last_grant_at_utc TEXT,
                last_consumed_at_utc TEXT,
                updated_at_utc TEXT
            )
        ''')

        # 3.5 Durable accounting event ledger for backup and future loyalty analytics.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS service_accounting_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_alias TEXT NOT NULL,
                key_id TEXT NOT NULL,
                customer_user_id INTEGER,
                event_type TEXT NOT NULL,
                purchased_bytes INTEGER DEFAULT 0,
                consumed_bytes INTEGER DEFAULT 0,
                is_unlimited BOOLEAN DEFAULT 0,
                metadata_json TEXT,
                created_at_utc TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS accounting_backfill_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                backfill_version TEXT,
                completed_at_utc TEXT
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

        # 6. Registration review notification routing setting.
        # When disabled, only OWNER receives /register review alerts.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS registration_review_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                notify_admins_enabled BOOLEAN DEFAULT 1
            )
        ''')

        # Backward-compatible migration for older DBs missing used_up_notified.
        cursor.execute("PRAGMA table_info(key_metadata)")
        key_metadata_columns = {row[1] for row in cursor.fetchall()}
        if "used_up_notified" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN used_up_notified BOOLEAN DEFAULT 0")
        if "created_by_user_id" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN created_by_user_id INTEGER")
        if "created_by_username" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN created_by_username TEXT")
        if "expiry_at_utc" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN expiry_at_utc TEXT")
        if "is_expired" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN is_expired BOOLEAN DEFAULT 0")
        if "auto_disabled_at_utc" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN auto_disabled_at_utc TEXT")
        if "configured_limit_mode" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN configured_limit_mode TEXT")
        if "configured_limit_bytes" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN configured_limit_bytes INTEGER")
        if "quota_blocked_at_utc" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN quota_blocked_at_utc TEXT")
        if "quota_block_limit_bytes" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN quota_block_limit_bytes INTEGER")
        if "assigned_user_id" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN assigned_user_id INTEGER")
        if "renew_count" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN renew_count INTEGER DEFAULT 0")
        if "last_renewed_at_utc" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN last_renewed_at_utc TEXT")
        if "last_renewed_quota_gb" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN last_renewed_quota_gb REAL")
        if "created_at_utc" not in key_metadata_columns:
            cursor.execute("ALTER TABLE key_metadata ADD COLUMN created_at_utc TEXT")

        cursor.execute("PRAGMA table_info(admins)")
        admins_columns = {row[1] for row in cursor.fetchall()}
        if "username" not in admins_columns:
            cursor.execute("ALTER TABLE admins ADD COLUMN username TEXT")

        # Ensure settings row always exists.
        cursor.execute(
            "INSERT OR IGNORE INTO notification_settings (id, is_enabled) VALUES (1, 1)"
        )
        cursor.execute(
            "INSERT OR IGNORE INTO registration_review_settings (id, notify_admins_enabled) VALUES (1, 1)"
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

        # Performance indexes for refinement features.
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_key_metadata_server_key ON key_metadata (server_alias, key_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_key_metadata_assigned_user ON key_metadata (assigned_user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_key_metadata_expiry ON key_metadata (expiry_at_utc, is_expired)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_server_key_time ON key_lifecycle_events (server_alias, key_id, created_at_utc)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_key_accounting_assigned_user ON key_accounting_totals (current_assigned_user_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_service_accounting_key_time ON service_accounting_events (server_alias, key_id, created_at_utc)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_service_accounting_customer_time ON service_accounting_events (customer_user_id, created_at_utc)"
        )
        
        conn.commit()
        logger.info("Database initialized successfully.")