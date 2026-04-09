import os
import logging
from dotenv import load_dotenv

# Load variables from .env file for local development
load_dotenv()

# --- Environment Variables ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Convert OWNER_ID to integer. Default to 0 if not set to prevent crashes.
OWNER_ID = int(os.getenv("OWNER_ID", 0)) 

if not BOT_TOKEN or not OWNER_ID:
    raise ValueError("CRITICAL: BOT_TOKEN or OWNER_ID is missing from environment variables.")

# --- Logging Configuration ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Silence the httpx spam (hides the long-polling INFO logs)
logging.getLogger("httpx").setLevel(logging.WARNING)

# --- Application Constants ---
DB_PATH = os.path.join("data", "bot_database.db")
BACKUP_DIR = os.path.join("data", "backups")