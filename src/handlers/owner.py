import logging
from telegram import Update
from telegram.ext import ContextTypes
from src.utils.decorators import owner_only
from src.database import queries

logger = logging.getLogger(__name__)

def _strip_outline_label(value: str, label: str) -> str:
    """Accept values copied from access.txt lines such as `apiUrl:...`."""
    prefix = f"{label}:"
    return value[len(prefix):].strip() if value.startswith(prefix) else value.strip()

@owner_only
async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Usage: `/addadmin <user_id>`", parse_mode='Markdown')
        return
    try:
        user_id = int(context.args[0])
        if queries.add_admin(user_id):
            await update.message.reply_text(f"✅ User `{user_id}` added as Admin.", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"⚠️ User `{user_id}` is already an Admin.", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ User ID must be a number.")

@owner_only
async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Usage: `/removeadmin <user_id>`", parse_mode='Markdown')
        return
    try:
        user_id = int(context.args[0])
        queries.remove_admin(user_id)
        await update.message.reply_text(f"🗑️ Admin `{user_id}` removed.", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ User ID must be a number.")

@owner_only
async def list_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = queries.get_admins()
    if not admins:
        await update.message.reply_text("No admins found.")
        return
    msg = "🛡️ *Current Admins:*\n" + "\n".join([f"- `{uid}`" for uid in admins])
    await update.message.reply_text(msg, parse_mode='Markdown')

@owner_only
async def add_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3:
        await update.message.reply_text("Usage: `/addserver <alias> <api_url> <cert_sha256>`\nExample: `/addserver vps1 https://1.2.3.4:54321/xxx ABC123...`", parse_mode='Markdown')
        return
    
    alias, api_url, cert_sha256 = context.args
    api_url = _strip_outline_label(api_url, "apiUrl")
    cert_sha256 = _strip_outline_label(cert_sha256, "certSha256")

    if not api_url.startswith("http://") and not api_url.startswith("https://"):
        await update.message.reply_text("❌ Invalid API URL. It must start with http:// or https://")
        return

    # By default, max_key_count is 0 (which we will treat as unlimited)
    if queries.add_server(alias, api_url, cert_sha256):
        await update.message.reply_text(f"✅ Server `{alias}` added successfully.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"⚠️ Server alias `{alias}` already exists.", parse_mode='Markdown')

@owner_only
async def list_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command: /listserver - list configured server aliases and limits."""
    servers = queries.get_servers()
    if not servers:
        await update.message.reply_text("No servers configured yet.")
        return

    lines = ["🌐 *Configured Servers:*"]
    for alias, data in servers.items():
        limit = data.get("max_key_count", 0)
        limit_text = str(limit) if limit and limit > 0 else "Unlimited"
        url_flag = " ⚠️ Check URL" if str(data.get("api_url", "")).startswith("apiUrl:") else ""
        lines.append(f"- `{alias}` | Max Keys: *{limit_text}*{url_flag}")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

@owner_only
async def delete_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Usage: `/deleteserver <alias>`", parse_mode='Markdown')
        return
    
    alias = context.args[0]
    queries.remove_server(alias)
    await update.message.reply_text(f"🗑️ Server `{alias}` removed from database.", parse_mode='Markdown')

@owner_only
async def set_key_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text("Usage: `/setkeylimit <server_alias> <max_number_of_keys>`\nExample: `/setkeylimit vps1 20`\n(Set to 0 for unlimited)", parse_mode='Markdown')
        return
    
    alias, limit_str = context.args
    try:
        limit = int(limit_str)
        if queries.update_server_limit(alias, limit):
            await update.message.reply_text(f"✅ Max key limit for `{alias}` set to *{limit}*.", parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ Server `{alias}` not found.", parse_mode='Markdown')
    except ValueError:
        await update.message.reply_text("❌ Limit must be an integer.")