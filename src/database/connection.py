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
                PRIMARY KEY (server_alias, key_id),
                FOREIGN KEY (server_alias) REFERENCES servers(alias) ON DELETE CASCADE
            )
        ''')
        
        conn.commit()
        logger.info("Database initialized successfully.")