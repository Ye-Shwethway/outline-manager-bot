from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

ACTIVE_INLINE_MESSAGE_KEY = "active_inline_message_id"


def set_active_inline_message(context: ContextTypes.DEFAULT_TYPE, message_id: int):
    """Remember the currently active inline keyboard message for this user context."""
    context.user_data[ACTIVE_INLINE_MESSAGE_KEY] = message_id


def clear_active_inline_message(context: ContextTypes.DEFAULT_TYPE):
    """Forget tracked inline keyboard message id."""
    context.user_data.pop(ACTIVE_INLINE_MESSAGE_KEY, None)


def clear_if_matches(context: ContextTypes.DEFAULT_TYPE, message_id: int):
    """Clear tracked message id only if it matches the provided one."""
    if context.user_data.get(ACTIVE_INLINE_MESSAGE_KEY) == message_id:
        clear_active_inline_message(context)


async def close_active_inline_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Close previous inline keyboard message, if still editable in current chat."""
    message_id = context.user_data.get(ACTIVE_INLINE_MESSAGE_KEY)
    chat = update.effective_chat
    if not message_id or not chat:
        return

    try:
        await context.bot.edit_message_reply_markup(chat_id=chat.id, message_id=message_id, reply_markup=None)
    except BadRequest:
        # Message may be too old, already edited, or not found; safe to ignore.
        pass
    finally:
        clear_active_inline_message(context)