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
            InlineKeyboardButton("⏳ Set Expiry", callback_data=f"expiry_{server_alias}_{key_id}")
        ],
        [
            InlineKeyboardButton("🔄 Renew", callback_data=f"renew_{server_alias}_{key_id}")
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


def get_expiry_preset_keyboard(server_alias: str, key_id: str) -> InlineKeyboardMarkup:
    """Preset-based expiry options from current action time."""
    keyboard = [
        [
            InlineKeyboardButton("+30 days", callback_data=f"expd30_{server_alias}_{key_id}"),
            InlineKeyboardButton("+90 days", callback_data=f"expd90_{server_alias}_{key_id}"),
        ],
        [
            InlineKeyboardButton("+180 days", callback_data=f"expd180_{server_alias}_{key_id}"),
            InlineKeyboardButton("+360 days", callback_data=f"expd360_{server_alias}_{key_id}"),
        ],
        [
            InlineKeyboardButton("🧹 Clear Expiry", callback_data=f"expclr_{server_alias}_{key_id}"),
        ],
        [
            InlineKeyboardButton("❎ Cancel", callback_data=f"expcancel_{server_alias}_{key_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_renew_duration_keyboard(server_alias: str, key_id: str) -> InlineKeyboardMarkup:
    """Renew duration options from current action time."""
    keyboard = [
        [
            InlineKeyboardButton("+30 days", callback_data=f"rnd30_{server_alias}_{key_id}"),
            InlineKeyboardButton("+90 days", callback_data=f"rnd90_{server_alias}_{key_id}"),
        ],
        [
            InlineKeyboardButton("+180 days", callback_data=f"rnd180_{server_alias}_{key_id}"),
            InlineKeyboardButton("+360 days", callback_data=f"rnd360_{server_alias}_{key_id}"),
        ],
        [
            InlineKeyboardButton("❎ Cancel", callback_data=f"rncancel_{server_alias}_{key_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_delete_confirmation_keyboard(server_alias: str, key_id: str) -> InlineKeyboardMarkup:
    """Confirmation keyboard shown before destructive delete action."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm Delete", callback_data=f"delyes_{server_alias}_{key_id}")
        ],
        [
            InlineKeyboardButton("❎ Cancel", callback_data=f"delno_{server_alias}_{key_id}")
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_post_create_sold_keyboard(server_alias: str, key_id: str) -> InlineKeyboardMarkup:
    """Keyboard shown after key creation to quickly mark it sold or keep available."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Mark Sold", callback_data=f"postsold_yes_{server_alias}_{key_id}")
        ],
        [
            InlineKeyboardButton("➡️ Keep Available", callback_data=f"postsold_no_{server_alias}_{key_id}")
        ],
    ]
    return InlineKeyboardMarkup(keyboard)