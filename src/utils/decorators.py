import logging
from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes
from src.config import OWNER_ID
from src.database.queries import get_admins

logger = logging.getLogger(__name__)

def owner_only(func):
    """Decorator to restrict command to the bot Owner only."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        # Handle both message updates and callback queries (inline buttons)
        user = update.effective_user
        if not user or user.id != OWNER_ID:
            logger.warning(f"Unauthorized OWNER attempt by user {user.id if user else 'Unknown'}")
            if update.message:
                await update.message.reply_text("❌ This command is restricted to the bot Owner.")
            elif update.callback_query:
                await update.callback_query.answer("❌ Owner privileges required.", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

def admin_only(func):
    """Decorator to restrict command to the Owner AND registered Admins."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user:
            return
            
        admins = get_admins()
        
        if user.id != OWNER_ID and user.id not in admins:
            logger.warning(f"Unauthorized ADMIN attempt by user {user.id}")
            if update.message:
                await update.message.reply_text("❌ You do not have permission to use this bot.")
            elif update.callback_query:
                await update.callback_query.answer("❌ Admin privileges required.", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapper