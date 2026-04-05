
# Outline Server Manager Bot - Project Roadmap (Final Draft)

## 1. Modular Project Architecture

Instead of a single file, we will use a scalable Python package structure:

```text
outline-bot/
│── data/                     # Mounted volume for SQLite DB
│── src/
│   │── main.py               # Application entry point & bot initialization
│   │── config.py             # Environment variables and constants
│   │── database/
│   │   │── __init__.py
│   │   │── connection.py     # SQLite setup and connection pooling
│   │   └── queries.py        # CRUD operations (servers, admins, keys)
│   │── services/
│   │   └── outline_api.py    # Wrapper for outline-vpn-api (VPN interactions)
│   │── handlers/
│   │   │── __init__.py
│   │   │── owner.py          # /addserver, /addadmin, /setkeylimit
│   │   │── lists.py          # /servers, /listkeys (and their inline callbacks)
│   │   └── wizards.py        # ConversationHandlers for /newkey wizard
│   └── utils/
│       │── decorators.py     # @owner_only, @admin_only
│       └── keyboards.py      # Helpers to generate InlineKeyboardMarkup
│── requirements.txt
│── Dockerfile
└── docker-compose.yml
```

## 2. Data Management (State Persistence - SQLite)

- [ ] **Admins Table:** `user_id` (Primary Key).
- [ ] **Servers Table:** `alias` (PK), `api_url`, `cert_sha256`, and **`max_key_count`** (Integer) to limit how many keys can exist on this server.
- [ ] **Key Metadata Table:** `server_alias`, `key_id`, and `is_sold` (Boolean). This acts as a bridge to store our custom business data alongside Outline's system data.

## 3. User Management (Owner Only)

- [ ] `/addadmin <user_id>` - Grant admin privileges.
- [ ] `/removeadmin <user_id>` - Revoke admin privileges.
- [ ] `/listadmin` - View all current admins.

## 4. Server Management (Owner Only)

- [ ] `/addserver <alias> <api_url> <cert_sha256>` - Register a new Outline server.
- [ ] `/deleteserver <alias>` - Remove a server from the database.
- [ ] `/setkeylimit <server_alias> <max_number_of_keys>` - Set the absolute maximum number of keys allowed on a specific server (e.g., max 20 keys).

## 5. VPN Management & UI Wizards (Admins & Owner)

- [ ] **Server Menu (`/servers`):** - Displays a list of servers as inline buttons.
  - Clicking a server reveals server stats (current key count vs. `max_key_count`).
- [ ] **Key List (`/listkeys <server_alias>`):** - Fetches keys from the server.
  - Cross-references SQLite to append a `[Sold]` tag to relevant keys.
  - Each key has an inline button attached for management (e.g., `[⚙️ Manage Key #5]`).
- [ ] **Manage Key Menu (Inline Callback):** - Triggered by clicking a key in the list.
  - Buttons: `[Mark as Sold]` (toggles `is_sold` state), `[Delete Key]`.
- [ ] **Interactive Key Wizard (`/newkey`):**
  - **Step 1:** Bot displays inline buttons of available servers. Admin clicks one.
  - *(Validation Check: If server has reached `max_key_count`, abort and warn).*
  - **Step 2:** Bot asks, "Please type a name for the new key:"
  - **Step 3:** Bot asks, "Please type the data limit in GB (or type 0 for no limit):"
  - **Step 4:** Bot generates the key via API, saves it, and outputs the `access_url`.

## 6. Security & Error Handling

- [ ] Custom decorators to strictly enforce Owner vs. Admin roles.
- [ ] Global error handler to catch API timeouts or invalid user inputs without crashing the bot.

## 7. Dockerization & Deployment

- [ ] `Dockerfile` using `python:3.12-slim`.
- [ ] `docker-compose.yml` defining environment variables (`BOT_TOKEN`, `OWNER_ID`) and binding the `/data` volume for database permanence.

***
