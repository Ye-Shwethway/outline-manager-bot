from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def get_server_list_keyboard(servers: dict, prefix: str) -> InlineKeyboardMarkup:
    """
    Generates a keyboard with a list of servers. 
    'prefix' helps us route the callback data (e.g., 'listkeys_vps1' vs 'newkey_vps1').
    """
    keyboard = []
    for alias in servers.keys():
        # Example callback_data: "newkey_vps1"
        keyboard.append([InlineKeyboardButton(f"🌐 {alias}", callback_data=f"{prefix}_{alias}")])
    
    return InlineKeyboardMarkup(keyboard)

def get_key_management_keyboard(server_alias: str, key_id: str, is_sold: bool) -> InlineKeyboardMarkup:
    """
    Generates the management buttons for a specific key.
    """
    sold_text = "🟢 Mark Unsold" if is_sold else "🔴 Mark Sold"
    
    keyboard = [
        [
            InlineKeyboardButton("🔑 View Key", callback_data=f"view_{server_alias}_{key_id}")
        ],
        [
            InlineKeyboardButton(sold_text, callback_data=f"toggle_{server_alias}_{key_id}")
        ],
        [
            InlineKeyboardButton("🗑️ Delete Key", callback_data=f"delete_{server_alias}_{key_id}")
        ],
        [
            InlineKeyboardButton("⬅️ Back to Keys", callback_data=f"listkeys_{server_alias}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)