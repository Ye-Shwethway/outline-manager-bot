# Outline Bot Refinement Plan (Pre-Implementation)

Date: 29-04-26
Status: Draft for approval before coding

## 1. Scope and Objectives

This refinement expands the bot from admin-only key operations to a fuller lifecycle system:

1. Expiry date management per key.
2. Renewal flow that updates both quota and expiry.
3. User registration + approval model with key ownership visibility.
4. Inline management UX for expiry/renew/link actions.
5. Extended backup output with lifecycle and ownership fields.
6. Backward-safe migration strategy for production continuity.

## 2. Confirmed Product Decisions

1. Expiry duration model:

- 1 month = 30 days
- 3 months = 90 days
- 6 months = 180 days
- 12 months = 360 days
- Durations are counted from key creation date/time (or renewal action time when renewed).

1. Expiry enforcement:

- On expiry, auto-disable key by setting Outline data limit to 0.

1. Renewal behavior:

- Renew updates both expiry and quota.
- Owner/Admin sets absolute new expiry and absolute new quota manually.

1. User role and ownership:

- User registration requires approval by Owner/Admin.
- Users can only view keys explicitly linked to them.
- One key belongs to one user.
- One user can own multiple keys.

1. Timezone:

- Business timezone: Asia/Yangon (UTC+6:30).
- Store canonical timestamps in UTC; convert to Yangon for UI display.

1. Existing keys support:

- Must support linking preexisting generated keys to users.

## 3. Non-Goals (for this phase)

1. No billing/payment processing.
2. No key recreation for renew.
3. No breaking changes to existing bot commands already in use.

## 4. Data Model and Migration Plan (Backward-Safe)

## 4.1 Key Metadata Extensions (existing key_metadata)

Add columns (nullable/default-safe):

1. expiry_at_utc TEXT
2. is_expired BOOLEAN DEFAULT 0
3. auto_disabled_at_utc TEXT
4. assigned_user_id INTEGER
5. renew_count INTEGER DEFAULT 0
6. last_renewed_at_utc TEXT
7. last_renewed_quota_gb REAL
8. created_at_utc TEXT (for legacy rows, nullable and lazily backfilled)

Notes:

- Keep existing columns intact.
- No destructive schema edits.

## 4.2 Customer Table

Create table customers:

1. user_id INTEGER PRIMARY KEY
2. username TEXT
3. first_name TEXT
4. status TEXT DEFAULT 'pending'  # pending/approved/rejected
5. approved_by INTEGER
6. approved_at_utc TEXT
7. created_at_utc TEXT
8. updated_at_utc TEXT

## 4.3 Lifecycle Events Table

Create table key_lifecycle_events:

1. id INTEGER PRIMARY KEY AUTOINCREMENT
2. server_alias TEXT NOT NULL
3. key_id TEXT NOT NULL
4. event_type TEXT NOT NULL
5. actor_user_id INTEGER
6. actor_username TEXT
7. event_payload_json TEXT
8. created_at_utc TEXT

Event examples:

- set_expiry
- renew
- assigned_user
- unassigned_user
- auto_disabled_expiry
- manual_override

## 4.4 Indexes

Add indexes for performance:

1. idx_key_metadata_server_key on (server_alias, key_id)
2. idx_key_metadata_assigned_user on (assigned_user_id)
3. idx_key_metadata_expiry on (expiry_at_utc, is_expired)
4. idx_events_server_key_time on (server_alias, key_id, created_at_utc)

## 4.5 Migration Safety Rules

1. Use CREATE TABLE IF NOT EXISTS.
2. Use PRAGMA table_info checks before ALTER TABLE ADD COLUMN.
3. Ensure defaults for new boolean/integer fields.
4. Do not drop/rename existing columns.
5. Keep bot operational if migration runs multiple times.

## 5. Role and Permission Matrix

1. Owner:

- Full control.
- Approve/reject users.
- All admin actions.

1. Admin:

- Manage keys and lifecycle fields.
- Set expiry.
- Renew keys.
- Link/unlink user ownership.
- Approve/reject users.

1. User:

- Register request.
- View only assigned keys and status.

## 6. Command and UX Plan

## 6.1 Existing Command Group Extensions

Integrate new lifecycle actions into /manage inline workflow:

1. Set Expiry
2. Renew
3. Assign User
4. Unassign User
5. View Owner/User Link

Keep existing actions:

1. View Key
2. Mark Sold/Unsold
3. Delete (with sold-key typed confirmation)

## 6.2 New/Extended Commands

Admin/Owner:

1. /renew (entry point or optional helper, but primary UX inline from /manage)
2. /setexpiry (optional helper, inline first)
3. /users (pending/approved overview)
4. /approve <user_id>
5. /reject <user_id>

User:

1. /register
2. /mykeys
3. /mystatus (optional detailed summary)

Decision:

- Main operational flow should be inline buttons under /manage and key lists.
- Text commands remain as fallback/admin shortcuts.

## 7. Expiry and Renew Logic

## 7.1 Expiry Calculation

For preset durations from action timestamp (creation or renewal):

1. +30 days for 1 month
2. +90 days for 3 months
3. +180 days for 6 months
4. +360 days for 12 months

Store expiry_at_utc; display converted Asia/Yangon time.

## 7.2 Auto-Disable Job

Periodic scheduler task:

1. Scan active keys with expiry_at_utc not null and is_expired = 0.
2. If now_utc >= expiry_at_utc:

- set Outline key data limit to 0
- set is_expired = 1
- set auto_disabled_at_utc = now_utc
- log lifecycle event
- notify recipients (Owner/Admin with notifications enabled)

Idempotency:

- Skip keys already marked expired unless renewed later.

## 7.3 Renew Flow

Renew action updates both fields in one transaction-like flow:

1. Validate target key exists.
2. Set new quota limit on Outline key (absolute).
3. Set new expiry (absolute date/time selected by admin or preset extension from now).
4. Reset is_expired = 0 when renewed.
5. Increment renew_count and update last_renewed fields.
6. Emit lifecycle event.

## 8. User Registration and Ownership Flow

1. User sends /register:

- create/update customer row as pending.

1. Owner/Admin approval:

- approve -> status = approved, approved_by, approved_at_utc set.
- reject -> status = rejected.

1. Key assignment:

- assign selected key to approved user only.
- enforce one key -> one user.

1. User view:

- /mykeys shows only keys where assigned_user_id = caller id.

## 9. Display/UI Updates

Update key display areas to include:

1. Expiry date/time (Yangon)
2. Expiry state (active/expired)
3. Assigned user (username + id)
4. Renew metadata (optional compact fields)
5. Existing fields retained:

- usage
- available usage
- generated by
- sold/used-up tags

Areas to update:

1. /keys list output
2. /manage header
3. View Key output
4. Delete confirmation output
5. /mykeys output

## 10. Backup Format Updates

Include lifecycle and ownership fields in backup files:

1. Expiry at (UTC and Yangon display)
2. is_expired
3. auto_disabled_at_utc
4. assigned_user_id and username
5. renew_count and last_renewed_at_utc
6. status summary per key

Auto and manual backup behavior remains unchanged:

1. /backup -> immediate file
2. /autobackup -> latest auto file
3. Daily auto backup schedule retained

## 11. Implementation Phases

## Phase A: Schema + Query Layer

1. Add migration-safe schema updates.
2. Add query helpers for expiry, assignment, customer approval, lifecycle events.
3. Add timezone conversion utility helpers.

## Phase B: Expiry Management

1. Implement inline Set Expiry flow in /manage.
2. Implement scheduler auto-disable logic.
3. Add event logging and notifications.

## Phase C: Renew Workflow

1. Inline renew action in /manage.
2. Update quota + expiry together.
3. Add summary confirmation and event log.

## Phase D: User Role + Ownership

1. /register, /mykeys.
2. Approve/reject flows for owner/admin.
3. Assign/unassign user to key (including preexisting keys).

## Phase E: Display + Backup Completion

1. Update all key info views with expiry/owner fields.
2. Extend backup export format.
3. Regression check all existing commands.

## 12. Risks and Mitigations

1. Risk: accidental mass-disable due to timezone mistakes.

- Mitigation: central timezone utility + UTC storage + tests.

1. Risk: migration bugs in live DB.

- Mitigation: additive idempotent migration checks only.

1. Risk: inconsistent key ownership data.

- Mitigation: single assignment source (key_metadata.assigned_user_id) + validation before assign.

1. Risk: noisy notifications.

- Mitigation: dedupe flags and event-based suppression.

## 13. Acceptance Criteria

1. Existing bot features continue working after migration.
2. Expiry presets use 30/90/180/360-day rules.
3. Expired keys auto-disable reliably.
4. Renew updates both quota and expiry without key recreation.
5. Users can only view assigned keys.
6. Preexisting keys can be assigned to approved users.
7. Key info and backups include expiry and ownership fields.

## 14. Open Clarifications Before Coding

1. Renew expiry input mode:

- absolute date picker only, or both absolute + preset buttons?

1. Approval UX:

- command-based (/approve, /reject) only, or inline pending queue buttons too?

1. User visibility scope:

- should user see access URL in /mykeys, or status-only for security?
