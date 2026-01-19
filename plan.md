# Reminder Pipeline v2 (Rules + Persistence + Scheduling)

This plan upgrades the current MVP (socket daemon + CLI + spawned iTerm2 reminder terminals) to support **multiple reminders per event occurrence**, **persistent state**, and **user actions** (`ack`, `drop`, `snooze/reschedule`).

## Goals (what we’re building)
- For each **event occurrence** within a rolling window (default: next 30 days), generate multiple reminders via **rules**:
  - week before (no ack)
  - day before (ack)
  - 06:00 day-of (no ack)
  - 30 min before (ack)
- A reminder firing spawns a terminal window that shows the reminder and asks for input.
- User can:
  - **ack**: acknowledge the reminder (and stop it repeating)
  - **drop**: cancel **current + all remaining** reminders for that event occurrence
  - **snooze**: add a new **custom** reminder for a provided datetime (always ack-required)
- Persist all state in **SQLite** (single file), so daemon restarts do not lose state.
- Add **structured logging** to `daemon.log` (rotation + retention) for:
  - event sync changes (added/updated/removed-from-window)
  - reminder lifecycle (created/fired/acked/dropped/snoozed)
  - socket requests (cmd + outcome)

Non-goals for this iteration:
- Storing full event snapshots for “missing event” cases (if event lookup fails, reminder terminal can show empty/limited info).
- Background service (launchd/systemd) configuration.

## Key decisions / assumptions
- **Identity**: a reminder is tied to an **event occurrence** identified by `(event_id, event_start_utc)`.
- **Timezones**:
  - Convert event start/end to **local timezone** for rule computation and display.
  - Store `*_utc` as integer epoch seconds for indexing/scheduling.
  - **All-day normalization (at load time)**: store all-day events as ordinary timed events starting at **09:00 local** on the event date (and set `end` accordingly). Downstream code should not need special-case all-day times.
- **Custom reminders survive** GCal inconsistencies:
  - If an event disappears from the fetched window, **rule-based reminders** may be removed.
  - **Custom reminders are kept** and may still fire (even if event lookup fails at that moment).
- **Rules stored by name**:
  - In code: `RULES: dict[str, Callable[[event_start_local, event_end_local, all_day], list[ReminderSpec]]]`
  - In DB: rule reminders reference `rule_name` (string).

## Logging
Use **Loguru** for daemon logs.

- **Dependency**: add `loguru` to `pyproject.toml`
- **Output**: `daemon.log` in project root
- **Suggested settings**:
  - rotation: `10 MB` 
  - retention: `14 days`
  - compression: `zip`
- **Level**:
  - default: `INFO`
  - allow `LOG_LEVEL` env var override

### What to log (minimum)
- **Daemon lifecycle**:
  - startup (paths, pid, db path, socket path)
  - shutdown (clean exit, exceptions)
- **Socket requests**:
  - `cmd`, key params (limit/id), and `{ok,error}` response
- **Sync pass**:
  - start/end timestamps and durations
  - counts: `seen_from_gcal`, `occ_upserted`, `occ_marked_dropped`, `rule_reminders_created`, `rule_reminders_cancelled`
  - per-occurrence *change* logs (only when something changes) including `(event_id,start_utc)` and computed local start time
- **Reminder lifecycle**:
  - created (rule/custom, trigger time, requires_ack)
  - fired (reminder id, trigger time, spawn command)
  - acked (reminder id)
  - dropped (occurrence key + how many reminders cancelled)
  - snoozed (source reminder id → new custom reminder id + trigger time)

## Data model (SQLite)
Create `reminder.db` in project root.

### Tables

1) `occurrences`
- `event_id TEXT NOT NULL`
- `start_utc INTEGER NOT NULL`
- `end_utc INTEGER NOT NULL`
- `all_day INTEGER NOT NULL` (0/1)
- `dropped INTEGER NOT NULL DEFAULT 0` (tombstone: drop cancels current + future reminders)
- `last_seen_utc INTEGER NOT NULL` (when it was last observed in the GCal sync window)
- Primary key: `(event_id, start_utc)`

2) `rule_reminders`
- `id INTEGER PRIMARY KEY`
- `event_id TEXT NOT NULL`
- `occ_start_utc INTEGER NOT NULL`
- `rule_name TEXT NOT NULL`
- `trigger_utc INTEGER NOT NULL`
- `requires_ack INTEGER NOT NULL` (0/1)
- `created_utc INTEGER NOT NULL`
- `acked_utc INTEGER NULL`
- `fired_utc INTEGER NULL`
- `cancelled_utc INTEGER NULL` (set on drop or other cleanup)
- Foreign key: `(event_id, occ_start_utc)` → `occurrences`
- Unique: `(event_id, occ_start_utc, rule_name)` (idempotent regen)
- Index: `(trigger_utc)` for due queries

3) `custom_reminders`
- `id INTEGER PRIMARY KEY`
- `event_id TEXT NOT NULL`
- `occ_start_utc INTEGER NOT NULL`
- `trigger_utc INTEGER NOT NULL`
- `requires_ack INTEGER NOT NULL` (always 1 for now)
- `created_utc INTEGER NOT NULL`
- `acked_utc INTEGER NULL`
- `fired_utc INTEGER NULL`
- `cancelled_utc INTEGER NULL` (set on drop, or manual cancel later)
- Foreign key: `(event_id, occ_start_utc)` → `occurrences`
- Index: `(trigger_utc)` for due queries

### Why keep ack/cancel timestamps instead of deleting rows?
- Avoid “regen resurrects deleted reminders” problems.
- Make operations idempotent and safe in daemon restarts.
- Support “drop cancels current + remaining” by bulk-updating rows.

## Protocol changes (socket)
Extend the existing JSON protocol with reminder-centric commands. Each request/response is still one-per-connection, newline-delimited JSON.

### Requests
- `{"cmd":"sync"}` → trigger a sync pass (daemon can also do this internally on timers).
- `{"cmd":"next","limit":N}` → unchanged: list next N events (still useful).
- `{"cmd":"due","limit":N}` → list due reminders (optional CLI convenience).
- `{"cmd":"fire_next"}` → daemon finds the next due reminder, spawns terminal, returns reminder id.
- `{"cmd":"get_reminder","id":<reminder_id>}` → return reminder + event details if available.
- `{"cmd":"ack_reminder","id":<reminder_id>}` → mark acked.
- `{"cmd":"drop_occurrence","event_id":"...","occ_start_utc":123}` → tombstone + cancel pending.
- `{"cmd":"snooze","id":<reminder_id>,"trigger_utc":<epoch>}` → add custom reminder + (optionally) ack current reminder.

### Responses
All responses include `ok: bool`. On failure: `{ok:false, error:"..."}`.

## CLI changes (`main.py`)
Keep existing commands, add/adjust:

- `start`:
  - starts daemon in foreground
  - daemon now also runs a **scheduler loop** (see below)

- `next N`:
  - unchanged: list next N events in next 30 days

- `test [--important]`:
  - keep as a manual trigger (useful for debugging), but implement it by selecting the next due reminder or next upcoming rule reminder.

- Replace `show <event_id>` with reminder-centric view:
  - `show-reminder <reminder_id> [--important]`
  - spawned terminals should run `uv run python main.py show-reminder <id>`

Reminder terminal interaction:
- prints event details (best-effort; may be empty if event not found)
- prompts:
  - if reminder requires ack or `--important`: require explicit input
  - else allow Enter
- accept commands:
  - `ack` (or Enter for simple)
  - `drop`
  - `snooze <datetime>` (natural language parsing can be added later; for now accept ISO / `YYYY-MM-DD HH:MM` local)
- sends corresponding socket commands
- exits

## Daemon scheduler loop
Daemon becomes responsible for automatically firing reminders, not just on `test`.

Loop structure (foreground daemon):
- On startup:
  - open SQLite DB, run migrations, load rules
  - run initial `sync()` and `generate_rule_reminders()`
- Periodic:
  - every X seconds:
    - sync events from GCal for `[now, now+30d]`
    - upsert occurrences, update `last_seen_utc`
    - for each non-dropped occurrence: ensure rule reminders exist (upsert by `(occ, rule_name)`)
    - cleanup:
      - for occurrences not seen recently (optional policy): cancel pending **rule** reminders
    - fire due reminders:
      - query reminders where `trigger_utc <= now_utc` and `acked_utc IS NULL` and `cancelled_utc IS NULL`
      - choose ordering by `trigger_utc`, and throttle spawning (e.g. max 1 spawn per 10s) to avoid spam
      - for each fired reminder: set `fired_utc` and spawn terminal

Important: keep scheduler idempotent so restarts don’t duplicate reminders.

## Rules implementation details
Define rules in code, keyed by name:
- `"week_before"`: trigger at `event_start_local - 7 days`, `requires_ack = 0`
- `"day_before"`: trigger at `event_start_local - 1 day`, `requires_ack = 1`
- `"six_am"`: trigger at `06:00 local` on the event’s local date, `requires_ack = 0`
- `"minus_30m"`: trigger at `event_start_local - 30 minutes`, `requires_ack = 1`

Edge cases:
- **Never create** new reminders whose computed `trigger_at` is already in the past at generation time.
- **Catch-up behavior**: if a reminder row already exists in the DB and its `trigger_utc` is now in the past, but it has not been fired/acked/cancelled (e.g. machine was off), then the scheduler should **fire it immediately** on next daemon run.
- All-day events:
  - already normalized at load time (see Timezones) to start at **09:00 local**.

## Implementation steps (file-by-file)

0) Add logging + dependency
- Add `loguru` dependency.
- Add `logging.py` (or `log.py`) module:
  - `configure_logger(project_root: Path) -> None`
  - sets up Loguru sink to `project_root / "daemon.log"` with rotation/retention/compression
  - exports `logger` (from loguru) for the daemon to use

1) Add SQLite module
- Add `db.py`:
  - connection helper
  - migrations (create tables / indexes)
  - CRUD helpers for occurrences and reminders

2) Add reminders/rules module
- Add `reminders.py`:
  - `RULES` dict
  - `compute_rule_reminders(occurrence) -> list[RuleReminderRow]`
  - timezone conversion helpers (local tz)

3) Update daemon
- Update `daemon.py`:
  - call `configure_logger(...)` early in startup
  - open DB on startup
  - add `sync()` to refresh occurrences and generate reminders
  - add scheduler loop to fire due reminders
  - implement new socket commands (`get_reminder`, `ack_reminder`, `drop_occurrence`, `snooze`)
  - update spawn command to `show-reminder <reminder_id>`

4) Update CLI
- Update `main.py`:
  - add `show-reminder` command
  - adjust `test` to trigger a reminder selection via daemon
  - update printing/interaction to accept `ack/drop/snooze`

5) Docs
- Update `README.md`:
  - mention `reminder.db`
  - note persistence and where it lives

## Manual test plan
- Start daemon: `uv run python main.py start`
- Verify initial sync doesn’t crash.
- List events: `uv run python main.py next 5`
- Force a reminder:
  - temporarily add a debug rule for “now + 1 min”, or add a custom reminder via `snooze` flow
- Confirm spawned iTerm2 window appears and prompts.
- `ack`: ensure reminder doesn’t fire again after restart (DB persists).
- `drop`: ensure current reminder stops and no more reminders fire for that occurrence.
- `snooze`: ensure a custom reminder is created and later fires.

