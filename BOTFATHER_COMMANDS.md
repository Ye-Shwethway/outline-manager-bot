# BotFather Commands

This file contains copy-paste ready BotFather command sets for the current bot command surface.

## Recommended Default Set

Use this if you want regular users to see only safe public commands.

```text
start - Start the bot
help - Show the command guide
id - Show your Telegram user id
register - Submit access request
mykeys - Show your assigned keys
```

## Full Private-Bot Set

Use this if you want one single command list containing every implemented command.

```text
start - Start the bot
help - Show the command guide
id - Show your Telegram user id
register - Submit access request
mykeys - Show your assigned keys
keys - Open key management
search - Search users or keys
newkey - Create a new key
manage - Manage users or a key
renew - Renew a key quota or expiry
cancel - Cancel the active wizard
noti - Toggle your alerts
scan - Run used-up key scan
backup - Generate manual backup
autobackup - Get latest auto backup
users - Open user registry
approve - Approve a user
reject - Reject a user
removeuser - Remove or unban a user
addadmin - Owner add admin
removeadmin - Owner remove admin
listadmin - Owner list admins
addserver - Owner add server
listserver - Owner list servers
deleteserver - Owner delete server
keyusage - Owner key usage diagnostic
keyaccounting - Owner key accounting diagnostic
useraccounting - Owner user accounting diagnostic
loyalty - Owner loyalty leaderboard
setkeylimit - Owner set server key limit
restart - Owner restart the bot
reviewnoti - Owner toggle review alerts
```

## Owner/Admin Working Set

Use this if you are applying commands only to your own private chat scope and want the operational set visible.

```text
start - Start the bot
help - Show the command guide
id - Show your Telegram user id
keys - Open key management
search - Search users or keys
newkey - Create a new key
manage - Manage users or a key
renew - Renew a key quota or expiry
cancel - Cancel the active wizard
noti - Toggle your alerts
scan - Run used-up key scan
backup - Generate manual backup
autobackup - Get latest auto backup
users - Open user registry
approve - Approve a user
reject - Reject a user
removeuser - Remove or unban a user
addadmin - Owner add admin
removeadmin - Owner remove admin
listadmin - Owner list admins
addserver - Owner add server
listserver - Owner list servers
deleteserver - Owner delete server
keyusage - Owner key usage diagnostic
keyaccounting - Owner key accounting diagnostic
useraccounting - Owner user accounting diagnostic
loyalty - Owner loyalty leaderboard
setkeylimit - Owner set server key limit
restart - Owner restart the bot
reviewnoti - Owner toggle review alerts
```

## Maintenance Rule

Whenever a command is added, removed, renamed, or its purpose changes, update both:

- `BOTFATHER_COMMANDS.md`
- `BOTFATHER_COMMANDS.txt`